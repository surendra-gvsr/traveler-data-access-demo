"""Command line entry points: load the stream, build profiles, inspect the result.

`load` then `build` are separate on purpose. `build` resumes from a checkpoint and can
be run repeatedly or with `--limit`, which is what makes the incremental claim in spec
002 demonstrable rather than asserted: build half the stream, look at a profile, build
the rest, and the result is the same as building it in one pass.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import access, audit, events as events_mod, profiles, store

DEFAULT_DB = Path("data/profiles.db")
DEFAULT_DATA = Path("data")


def load(db_path: Path, data_dir: Path, reset: bool = False) -> int:
    if reset and db_path.exists():
        db_path.unlink()

    conn = store.connect(db_path)
    existing = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    if existing and not reset:
        raise SystemExit(
            f"{db_path} already holds {existing} deliveries. Re-run with --reset to reload."
        )

    travelers = events_mod.read_travelers(data_dir / "travelers.json")
    store.load_travelers(conn, travelers)
    deliveries = list(events_mod.read_events(data_dir / "events.jsonl"))
    store.append_deliveries(conn, deliveries)

    print(f"loaded {len(travelers)} travelers and {len(deliveries)} deliveries into {db_path}")
    print("  (duplicates retained — downstream code has to cope with them)")
    return len(deliveries)


def build(db_path: Path, limit: int | None = None) -> profiles.BuildSummary:
    conn = store.connect(db_path)
    before = store.checkpoint(conn)
    summary = profiles.build_profiles(conn, limit=limit)

    print(f"consumed deliveries {before + 1}..{summary.last_delivery_seq} "
          f"({summary.total} deliveries)")
    print(f"  applied      {summary.applied}")
    print(f"  duplicates   {summary.duplicates}   (ignored: already in the ledger)")
    print(f"  stale        {summary.stale}   (counters applied, overwrites skipped)")
    if summary.total == 0:
        print("  nothing new — the checkpoint is already at the end of the stream")
    return summary


def status(db_path: Path) -> None:
    conn = store.connect(db_path)
    counts = {
        "deliveries": "SELECT COUNT(*) AS n FROM events",
        "distinct events": "SELECT COUNT(DISTINCT event_id) AS n FROM events",
        "processed": "SELECT COUNT(*) AS n FROM processed_events",
        "profiles": "SELECT COUNT(*) AS n FROM profiles",
        "consent records": "SELECT COUNT(*) AS n FROM consent_state",
    }
    for label, sql in counts.items():
        print(f"  {label:<16} {conn.execute(sql).fetchone()['n']}")
    print(f"  {'checkpoint':<16} delivery_seq {store.checkpoint(conn)}")
    print(f"  {'knowledge as of':<16} {store.knowledge_watermark(conn)}")


def show(db_path: Path, traveler_id: str, tool: str, as_of: str | None) -> None:
    """Read through the governed path, exactly as the MCP server does."""
    conn = store.connect(db_path)
    response = access.serve(
        conn,
        access.AccessRequest(
            tool=tool,
            traveler_id=traveler_id,
            caller="pipeline-cli",
            as_of=datetime.fromisoformat(as_of) if as_of else None,
        ),
    )
    print(json.dumps(response.model_dump(mode="json"), indent=2, sort_keys=True))


def audit_tail(count: int) -> None:
    for record in audit.tail(count):
        print(json.dumps(record, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load travelers and the raw delivery log")
    p_load.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p_load.add_argument("--reset", action="store_true")

    p_build = sub.add_parser("build", help="apply unprocessed deliveries to profiles")
    p_build.add_argument("--limit", type=int, default=None)

    sub.add_parser("status", help="counts and checkpoint position")

    p_show = sub.add_parser("show", help="serve one traveler through the access layer")
    p_show.add_argument("traveler_id")
    p_show.add_argument("--tool", default="get_traveler_preferences")
    p_show.add_argument("--as-of", default=None)

    p_audit = sub.add_parser("audit", help="print the tail of the audit log")
    p_audit.add_argument("-n", type=int, default=5)

    args = parser.parse_args()
    if args.command == "load":
        load(args.db, args.data, args.reset)
    elif args.command == "build":
        build(args.db, args.limit)
    elif args.command == "status":
        status(args.db)
    elif args.command == "show":
        show(args.db, args.traveler_id, args.tool, args.as_of)
    elif args.command == "audit":
        audit_tail(args.n)


if __name__ == "__main__":
    main()
