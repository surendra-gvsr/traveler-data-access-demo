"""Ask a model the demo question through the governed path only. Spec: 004, 005

    What kind of trip is this traveler likely planning?

The point of the demo is not that a model can answer the question — it is *where* the
model is standing while it answers. It gets the MCP tools and nothing else: no database
handle, no file access, no traveler table. So every claim in its answer has to come
from data the traveler consented to share, with the identifiers already removed.

Two travelers are asked about, in separate conversations:

  trv_003  consented — the answer should cite destinations, cabin, party size, lead time
  trv_010  revoked AI access — every tool returns DENIED and the model should say so

Run:
    python demo/ask_claude.py                  # needs ANTHROPIC_API_KEY
    python demo/ask_claude.py --dry-run        # no model, no key: shows the tool payloads

`--dry-run` exists because the governance is the interesting part and it is fully
observable without spending a token: it walks the same tools over the same transport
and prints exactly what the model would have been handed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))  # run from a clone, without installing

from traveler_access import audit, generate, pii, store  # noqa: E402

MODEL = "claude-opus-5-5"
QUESTION = "What kind of trip is this traveler likely planning?"

SYSTEM = """You are a trip-planning assistant working inside a customer data platform.

You can only learn about a traveler through the tools provided. There is no other \
source of truth available to you, and you must not supplement the tool results with \
assumptions about who the traveler is.

The tools deliberately serve no personal identifiers — no name, email or phone. This \
is not an omission to work around. Do not ask for them and do not speculate about them.

A DENIED result means the traveler has not consented to this use of their data. It is \
a final answer, not a transient failure: say that the traveler cannot be analysed, and \
stop. Do not retry and do not fall back to another tool for the same data.

When you do have data, answer in three or four sentences. Name the specific evidence \
you used — destinations, cabin, party size, trip length, booking lead time — and say \
plainly how confident you are."""

MAX_TURNS = 6


# --- the transport -----------------------------------------------------------------


def load_env(path: Path) -> None:
    """Read `.env`, so the API key can live in a gitignored file instead of a shell export.

    A real environment variable wins over the file — except an empty one, which is
    treated as absent. That case is not hypothetical: an exported but empty
    ANTHROPIC_API_KEY is what the SDK reports as "could not resolve authentication
    method", several frames deep and nowhere near the cause.
    """
    if not path.exists():
        return
    # utf-8-sig, not utf-8: Notepad writes a BOM, which would otherwise become part of
    # the first key name and make the file look empty for no visible reason.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if value and not os.environ.get(key):
            os.environ[key] = value


def server_params(db: Path, audit_log: Path) -> StdioServerParameters:
    """Launch the MCP server as a real subprocess, as any client would."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "traveler_access.mcp_server"],
        env={
            **os.environ,
            "TRAVELER_DB": str(db),
            "TRAVELER_AUDIT": str(audit_log),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        },
        cwd=str(REPO_ROOT),
    )


# Every request id a tool returned in this run, so the audit section at the end shows
# exactly this run's records and not whatever earlier runs left in the append-only log.
RUN_REQUEST_IDS: set[str] = set()


async def call_tool(session: ClientSession, name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool and return its payload as JSON text, plus whether it failed."""
    result = await session.call_tool(name, arguments)
    if result.structuredContent is None:
        # A transport-level failure, as distinct from a DENIED result.
        return json.dumps({"error": "tool returned no structured content"}), True
    if result.structuredContent.get("request_id"):
        RUN_REQUEST_IDS.add(result.structuredContent["request_id"])
    return json.dumps(result.structuredContent, default=str, sort_keys=True), bool(result.isError)


# --- output ------------------------------------------------------------------------


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def show_call(name: str, arguments: dict) -> None:
    rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(arguments.items()))
    print(f"\n  -> {name}({rendered})")


def show_result(payload: str) -> None:
    data = json.loads(payload)
    status = data.get("status")
    if status == "DENIED":
        print(f"     {status}  {data.get('reason_code')}  {data.get('message')}")
        return
    print(f"     {status}  {_summarise(data)}")


def _summarise(data: dict) -> str:
    """One line per result, so the transcript stays readable."""
    if "preferences" in data and data["preferences"]:
        preferences = data["preferences"]
        tops = ", ".join(
            f"{row['destination']}x{row['interactions']}" for row in preferences["top_destinations"]
        )
        return (
            f"top=[{tops}] cabin={preferences['preferred_cabin']} "
            f"party={preferences['party_adults']}+{preferences['party_children']} "
            f"nights={preferences['avg_trip_length_nights']} "
            f"lead={preferences['avg_booking_lead_time_days']}d "
            f"searches={preferences['search_count']} bookings={preferences['booking_count']}"
        )
    if data.get("searches") is not None:
        features = data["features"]
        return (
            f"{len(data['searches'])} searches in {data['window_days']}d, features: "
            f"searches_last_30d={features['searches_last_30d']} "
            f"bookings_last_90d={features['bookings_last_90d']} "
            f"upcoming_trip={features['has_upcoming_trip']} as_of={features['as_of']}"
        )
    if data.get("travelers") is not None:
        allowed = sum(1 for row in data["travelers"] if row["ai_access"])
        return f"{len(data['travelers'])} travelers, {allowed} with AI access"
    return "(empty)"


# --- the model loop ----------------------------------------------------------------


async def ask_about(client, session: ClientSession, tools: list[dict], traveler_id: str,
                    model: str, seen: list[str]) -> str:
    """One conversation about one traveler. Returns the model's final answer."""
    messages: list[dict] = [
        {"role": "user", "content": f"Traveler {traveler_id}. {QUESTION}"}
    ]

    for _ in range(MAX_TURNS):
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            # Stable prefix first (tools, then system), volatile last (messages), so the
            # second traveler's conversation reuses the first one's cached prefix.
            cache_control={"type": "ephemeral"},
            tools=tools,
            system=SYSTEM,
            messages=messages,
            # `thinking` is omitted deliberately: on the default model thinking is always
            # on and omitting it is equivalent to {"type": "adaptive"}, while staying
            # valid if --model points at an older one.
        )
        usage = response.usage
        print(
            f"     [in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_read={usage.cache_read_input_tokens} "
            f"cache_write={usage.cache_creation_input_tokens}]"
        )

        for block in response.content:
            if block.type == "text":
                seen.append(block.text)

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})

        # All results go back in one user message, which is what the API requires when
        # the model asks for several tools at once.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            show_call(block.name, dict(block.input))
            payload, failed = await call_tool(session, block.name, dict(block.input))
            show_result(payload)
            seen.append(payload)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": payload,
                **({"is_error": True} if failed else {}),
            })
        messages.append({"role": "user", "content": results})
    else:
        return "(gave up: the model was still calling tools after MAX_TURNS)"

    return "\n".join(block.text for block in response.content if block.type == "text").strip()


async def walk_through(session: ClientSession, traveler_id: str, seen: list[str]) -> None:
    """--dry-run: the calls a model would make, without the model."""
    for name, arguments in (
        ("get_traveler_preferences", {"traveler_id": traveler_id}),
        ("get_recent_searches", {"traveler_id": traveler_id, "window_days": 30, "limit": 5}),
    ):
        show_call(name, arguments)
        payload, _ = await call_tool(session, name, arguments)
        show_result(payload)
        seen.append(payload)


# --- entry point -------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    known_pii = store.all_pii_values(conn)
    if not known_pii:
        print(f"no travelers in {args.db} — run: python -m traveler_access.pipeline load --reset")
        return 1

    client = anthropic = None
    if not args.dry_run:
        import anthropic  # imported late so --dry-run needs no API dependency

        # Checked here rather than at the call site: an exception raised inside the
        # session below surfaces as a nested anyio TaskGroup traceback.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is empty or not set. Put it in .env (see "
                  ".env.example) or export it. Or use --dry-run to see the same tool "
                  "calls and the same governance without a model.")
            return 1
        client = anthropic.AsyncAnthropic()

    # Everything the model was shown or said, checked for leaks at the end.
    seen: list[str] = []
    async with stdio_client(server_params(args.db, args.audit)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            # Sorted for a byte-stable tool block, which is what makes the cache hit.
            tools = [
                {"name": tool.name, "description": tool.description or "",
                 "input_schema": tool.inputSchema}
                for tool in sorted(listed.tools, key=lambda tool: tool.name)
            ]
            print(f"tools offered to the model: {', '.join(tool['name'] for tool in tools)}")

            for traveler_id, label in (
                (generate.DEMO_TRAVELER, "consented"),
                (generate.OPT_OUT_TRAVELER, "revoked AI access"),
            ):
                banner(f"{traveler_id} ({label}) — {QUESTION}")
                if args.dry_run:
                    await walk_through(session, traveler_id, seen)
                    continue
                try:
                    answer = await ask_about(
                        client, session, tools, traveler_id, args.model, seen
                    )
                except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
                    print(f"\n  the model call failed: {error}\n")
                    return 1
                print(f"\n  {answer}\n")

    banner("PII check across the whole conversation")
    findings = pii.find_pii(seen, known_pii)
    print(f"  {len(known_pii)} known PII values in the dataset (names, emails, phones)")
    print(f"  {sum(len(text) for text in seen)} characters shown to or written by the model")
    if findings:
        print(f"  LEAK: {findings}")
        return 1
    print("  no PII field name, no known PII value, no email or phone pattern")

    banner("Audit trail written by the server subprocess during this run")
    # The audit log is append-only and outlives runs, so select this run's records by the
    # request ids the tools returned, rather than by position or timestamp.
    this_run = [
        record for record in audit.read_all(args.audit)
        if record["request_id"] in RUN_REQUEST_IDS
    ]
    if not this_run:
        print("  (no records: the server wrote nothing during this run)")
    for record in this_run:
        print(
            f"  {record['ts']}  {record['caller']:<12}  {record['tool']:<26} "
            f"{record['traveler_id'] or '-':<8} {record['decision']:<7} "
            f"{record['reason_code'] or ''}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--dry-run", action="store_true",
                        help="exercise the tools without calling a model")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "profiles.db")
    parser.add_argument("--audit", type=Path, default=REPO_ROOT / "data" / "audit.jsonl")
    # Windows consoles default to a legacy code page that cannot encode the dashes and
    # quotes a model writes freely. Without this, the run dies on the final `print` of
    # an otherwise perfect answer. The subprocess is already given the same treatment
    # through PYTHONIOENCODING; this is the parent's half of it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env(REPO_ROOT / ".env")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
