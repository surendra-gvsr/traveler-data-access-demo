"""Spec: specs/005-mcp-tool-contract.md — criteria E1-E7.

The output schemas *are* the privacy policy as far as a model is concerned: a field
that is not in a schema cannot be requested, inferred, or prompted out. So E2 is
asserted against the schema rather than against a sample response — a response can
happen to be clean, while a schema is a standing promise.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from traveler_access import audit, consent, generate, pii, store
from traveler_access import mcp_server

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = {"get_traveler_preferences", "get_recent_searches", "list_travelers"}


@pytest.fixture
def server(built_db, audit_path, monkeypatch):
    """Point the module-level server at this session's database."""
    monkeypatch.setenv("TRAVELER_DB", str(built_db))
    monkeypatch.setenv("TRAVELER_AUDIT", str(audit_path))
    monkeypatch.setattr(mcp_server, "_conn", None)
    yield mcp_server.mcp
    mcp_server._conn = None


def _payload(result):
    """FastMCP returns (content, structured) in-process; take the structured half."""
    return result[1] if isinstance(result, tuple) else result


def _schema_property_names(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            _schema_property_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _schema_property_names(item, found)
    return found


# --- E1: the surface --------------------------------------------------------------


async def test_e1_exactly_three_tools_with_complete_schemas(server):
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.description, tool.name
        assert tool.inputSchema["type"] == "object"
        assert tool.outputSchema, f"{tool.name} has no output schema"
        assert _schema_property_names(tool.outputSchema), tool.name


async def test_e1_every_tool_documents_its_arguments(server):
    for tool in await server.list_tools():
        for name, schema in tool.inputSchema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name}"


async def test_e1_denials_are_explained_in_the_tool_descriptions(server):
    """A model that treats DENIED as a transient error will retry or route around it."""
    for tool in await server.list_tools():
        if tool.name == "list_travelers":
            continue
        assert "DENIED" in tool.description, tool.name


# --- E2: schemas as policy --------------------------------------------------------


async def test_e2_no_output_schema_names_a_pii_field(server):
    for tool in await server.list_tools():
        names = _schema_property_names(tool.outputSchema)
        leaked = names & set(pii.PII_FIELD_NAMES)
        assert not leaked, f"{tool.name} exposes {leaked}"


async def test_e2_input_schemas_cannot_request_pii_either(server):
    """A `fields` or `include_email` argument would reopen what the models closed."""
    for tool in await server.list_tools():
        names = _schema_property_names(tool.inputSchema)
        assert not names & set(pii.PII_FIELD_NAMES), tool.name


async def test_e2_the_schemas_are_closed_not_loose_dicts(server):
    """A `dict` output would type-check fine and promise nothing."""
    for tool in await server.list_tools():
        names = _schema_property_names(tool.outputSchema)
        assert "status" in names and "request_id" in names, tool.name
        assert len(names) > 5, f"{tool.name} schema is suspiciously thin"


# --- E3: denial is a result, not an error -----------------------------------------


@pytest.mark.parametrize("tool", ["get_traveler_preferences", "get_recent_searches"])
async def test_e3_the_opted_out_traveler_gets_a_well_formed_denial(server, tool):
    result = _payload(await server.call_tool(tool, {"traveler_id": generate.OPT_OUT_TRAVELER}))

    assert result["status"] == "DENIED"
    assert result["reason_code"] == consent.AI_ACCESS_REVOKED
    assert result["request_id"]
    assert result["message"]
    assert result.get("preferences") is None
    assert result.get("searches") is None


async def test_e3_a_denial_uses_the_same_envelope_as_a_success(server):
    """One shape for both outcomes, so the model learns one pattern."""
    allowed = _payload(await server.call_tool(
        "get_traveler_preferences", {"traveler_id": generate.DEMO_TRAVELER}
    ))
    denied = _payload(await server.call_tool(
        "get_traveler_preferences", {"traveler_id": generate.OPT_OUT_TRAVELER}
    ))

    assert set(allowed) == set(denied)
    assert allowed["status"] == "OK"
    assert denied["status"] == "DENIED"


async def test_e3_the_no_consent_traveler_fails_closed_through_the_tool(server):
    result = _payload(await server.call_tool(
        "get_traveler_preferences", {"traveler_id": generate.NO_CONSENT_TRAVELER}
    ))
    assert result["reason_code"] == consent.NO_CONSENT_RECORD


# --- E4: no PII in any response ---------------------------------------------------


async def test_e4_no_tool_response_contains_pii_for_any_traveler(server, built_conn, dataset):
    everyones_values = store.all_pii_values(built_conn)
    assert everyones_values

    for traveler in dataset["travelers"]:
        for tool in ("get_traveler_preferences", "get_recent_searches"):
            result = _payload(await server.call_tool(tool, {"traveler_id": traveler.traveler_id}))
            blob = json.dumps(result, default=str)

            assert "contact_email" not in blob
            for value in everyones_values:
                assert value not in blob, f"{tool}/{traveler.traveler_id}"


async def test_e4_the_listing_leaks_nothing_either(server, built_conn):
    result = _payload(await server.call_tool("list_travelers", {}))
    blob = json.dumps(result, default=str)

    for value in store.all_pii_values(built_conn):
        assert value not in blob


# --- E5: audit ---------------------------------------------------------------------


async def test_e5_every_tool_call_appends_an_audit_record(server, audit_path):
    before = len(audit.read_all(audit_path)) if audit_path.exists() else 0

    await server.call_tool("get_traveler_preferences", {"traveler_id": generate.DEMO_TRAVELER})
    assert len(audit.read_all(audit_path)) == before + 1

    await server.call_tool("get_traveler_preferences", {"traveler_id": generate.OPT_OUT_TRAVELER})
    records = audit.read_all(audit_path)
    assert len(records) == before + 2
    assert records[-1]["decision"] == "DENIED"
    assert records[-1]["caller"] == "mcp"


async def test_e5_the_audit_record_names_the_tool_that_was_called(server, audit_path):
    for tool in sorted(TOOL_NAMES):
        args = {} if tool == "list_travelers" else {"traveler_id": generate.DEMO_TRAVELER}
        await server.call_tool(tool, args)
        assert audit.read_all(audit_path)[-1]["tool"] == tool


# --- E6: the tools are thin -------------------------------------------------------


def test_e6_the_server_has_no_direct_store_access():
    """Every read goes through `serve`, so the four controls cannot be skipped.

    Asserted against the source text rather than behaviour: the claim is structural,
    and the point is that there is nowhere else for a future tool to read from.
    """
    source = (REPO_ROOT / "src" / "traveler_access" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    for forbidden in ("conn.execute", "SELECT ", "cursor(", "store.load_profile",
                      "profiles.load_profile", "features.compute_features",
                      "consent.decide", "pii.assert_no_pii"):
        assert forbidden not in code, f"mcp_server.py reaches past serve(): {forbidden}"

    # The one permitted doorway, and the only place the four controls live.
    assert "access.serve" in code


def test_e6_every_tool_goes_through_the_single_call_helper():
    """Exactly one call site for `serve`, so there is one place to audit."""
    tree = ast.parse(
        (REPO_ROOT / "src" / "traveler_access" / "mcp_server.py").read_text(encoding="utf-8")
    )
    serve_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "serve"
    ]
    assert len(serve_calls) == 1, "more than one read path has appeared"

    helper_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_call"
    ]
    assert len(helper_calls) == len(TOOL_NAMES), "a tool bypasses the shared helper"


def test_e6_the_mcp_server_imports_no_sql_layer_beyond_connecting():
    """`store` is imported only to open the connection, not to query through it."""
    source = (REPO_ROOT / "src" / "traveler_access" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    store_uses = [line.strip() for line in source.splitlines() if "store." in line]
    assert len(store_uses) == 1, store_uses
    assert store_uses[0].startswith("_conn = store.connect(")


# --- E7: a real stdio session -----------------------------------------------------


async def test_e7_tools_work_over_a_real_stdio_session(built_db, tmp_path):
    """Not just Python functions: launched as a subprocess and spoken to over stdio.

    This is the only test that exercises the transport, the handshake and the JSON
    serialisation the way a real client does.
    """
    env = {
        **os.environ,
        "TRAVELER_DB": str(built_db),
        "TRAVELER_AUDIT": str(tmp_path / "stdio_audit.jsonl"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "traveler_access.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == TOOL_NAMES

            allowed = await session.call_tool(
                "get_traveler_preferences", {"traveler_id": generate.DEMO_TRAVELER}
            )
            assert allowed.isError is False
            assert allowed.structuredContent["status"] == "OK"
            assert allowed.structuredContent["preferences"]["traveler_id"] == (
                generate.DEMO_TRAVELER
            )

            denied = await session.call_tool(
                "get_traveler_preferences", {"traveler_id": generate.OPT_OUT_TRAVELER}
            )
            # A refusal is a result, not a transport failure.
            assert denied.isError is False
            assert denied.structuredContent["status"] == "DENIED"
            assert denied.structuredContent["reason_code"] == consent.AI_ACCESS_REVOKED

            searches = await session.call_tool(
                "get_recent_searches", {"traveler_id": generate.DEMO_TRAVELER, "limit": 3}
            )
            assert searches.structuredContent["status"] == "OK"
            assert len(searches.structuredContent["searches"]) <= 3

    # The subprocess wrote its own audit trail, so the controls ran server-side.
    stdio_audit = audit.read_all(tmp_path / "stdio_audit.jsonl")
    assert [record["caller"] for record in stdio_audit] == ["mcp"] * 3
    assert [record["decision"] for record in stdio_audit] == ["OK", "DENIED", "OK"]


async def test_e7_an_unknown_tool_name_is_rejected_by_the_session(built_db, tmp_path):
    env = {
        **os.environ,
        "TRAVELER_DB": str(built_db),
        "TRAVELER_AUDIT": str(tmp_path / "unknown_audit.jsonl"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "traveler_access.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_everything", {})
            assert result.isError is True
