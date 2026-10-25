"""Spec: specs/004-ai-safe-access-layer.md — criteria D1-D10.

Four controls have to hold on every call: consent, PII scrubbing, provenance, audit.
These tests are written against `serve` rather than against the MCP tools on purpose —
if a control can be bypassed, it will be bypassed here, one layer below the tools.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from traveler_access import access, audit, consent, events, generate, pii, profiles, store
from traveler_access.events import utc
from traveler_access.provenance import Provenance

ALL_TOOLS = ("get_traveler_preferences", "get_recent_searches")


def _serve(conn, audit_path, tool="get_traveler_preferences", **kwargs):
    return access.serve(conn, access.AccessRequest(tool=tool, **kwargs), audit_path)


# --- D1-D4: consent ---------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_d1_a_revoked_traveler_is_denied_by_every_tool(built_conn, audit_path, tool):
    response = _serve(built_conn, audit_path, tool, traveler_id=generate.OPT_OUT_TRAVELER)

    assert response.status == "DENIED"
    assert response.reason_code == consent.AI_ACCESS_REVOKED
    assert response.data is None
    assert response.provenance is None
    assert response.message


def test_d2_the_stale_opt_in_does_not_restore_access(built_conn, audit_path, dataset):
    """The opt-in arrived last. Arrival order is not decision order."""
    stale = dataset["report"]["stale_opt_in"]
    state = consent.load_consent(built_conn, generate.OPT_OUT_TRAVELER)

    assert state.ai_access is False
    assert state.source_version > stale["source_version"]

    response = _serve(built_conn, audit_path, traveler_id=generate.OPT_OUT_TRAVELER)
    assert response.reason_code == consent.AI_ACCESS_REVOKED


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_d3_an_unknown_consent_record_fails_closed(built_conn, audit_path, tool):
    response = _serve(built_conn, audit_path, tool, traveler_id=generate.NO_CONSENT_TRAVELER)

    assert response.status == "DENIED"
    assert response.reason_code == consent.NO_CONSENT_RECORD
    assert response.data is None


def test_d3_a_traveler_who_does_not_exist_is_denied(built_conn, audit_path):
    response = _serve(built_conn, audit_path, traveler_id="trv_does_not_exist")

    assert response.status == "DENIED"
    assert response.reason_code == consent.UNKNOWN_TRAVELER


def test_d4_a_purpose_outside_the_consent_is_denied(built_conn, audit_path):
    state = consent.load_consent(built_conn, generate.NARROW_PURPOSE_TRAVELER)
    assert state.ai_access is True, "this traveler must be consented for the test to bite"
    assert access.DEFAULT_PURPOSE not in state.purposes

    response = _serve(
        built_conn, audit_path,
        traveler_id=generate.NARROW_PURPOSE_TRAVELER, purpose=access.DEFAULT_PURPOSE,
    )

    assert response.status == "DENIED"
    assert response.reason_code == consent.PURPOSE_NOT_PERMITTED


def test_d4_the_consented_purpose_is_allowed_for_the_same_traveler(built_conn, audit_path):
    """Otherwise D4 would pass simply because the traveler is denied everything."""
    state = consent.load_consent(built_conn, generate.NARROW_PURPOSE_TRAVELER)
    response = _serve(
        built_conn, audit_path,
        traveler_id=generate.NARROW_PURPOSE_TRAVELER, purpose=state.purposes[0],
    )

    assert response.status == "OK"


def test_the_demo_traveler_is_allowed(built_conn, audit_path):
    response = _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)

    assert response.status == "OK"
    assert response.data["traveler_id"] == generate.DEMO_TRAVELER
    assert response.reason_code is None


# --- D5: as_of is not a consent bypass --------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_d5_as_of_before_the_opt_out_is_still_denied(built_conn, audit_path, dataset, tool):
    """`as_of` rewinds facts, not permission.

    If consent were evaluated as of the requested timestamp, `as_of` would be a
    one-parameter consent bypass: pick a time before the opt-out and read freely.
    """
    opt_out_at = utc(datetime.fromisoformat(dataset["report"]["opt_out_occurred_at"]))

    for delta in (timedelta(days=1), timedelta(days=30), timedelta(days=90)):
        response = _serve(
            built_conn, audit_path, tool,
            traveler_id=generate.OPT_OUT_TRAVELER, as_of=opt_out_at - delta,
        )
        assert response.status == "DENIED", delta
        assert response.reason_code == consent.AI_ACCESS_REVOKED, delta
        assert response.data is None, delta


def test_d5_as_of_does_still_rewind_the_facts(built_conn, audit_path):
    """The other half of D5: as_of has to actually work for a consented traveler."""
    now = _serve(
        built_conn, audit_path, "get_recent_searches", traveler_id=generate.DEMO_TRAVELER
    )
    earlier = _serve(
        built_conn, audit_path, "get_recent_searches",
        traveler_id=generate.DEMO_TRAVELER,
        as_of=utc(datetime.fromisoformat(generate.iso(generate.NOW))) - timedelta(days=75),
    )

    assert now.status == earlier.status == "OK"
    assert earlier.data["features"]["visible_event_count"] < (
        now.data["features"]["visible_event_count"]
    )


# --- D6, D7: PII ------------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_d6_no_response_contains_any_travelers_pii(built_conn, audit_path, dataset, tool):
    everyones_values = store.all_pii_values(built_conn)
    assert everyones_values, "the PII registry is empty, so this proves nothing"

    for traveler in dataset["travelers"]:
        response = _serve(built_conn, audit_path, tool, traveler_id=traveler.traveler_id)
        blob = json.dumps(response.model_dump(mode="json"), default=str)

        # Checked against *everyone's* values, not just this traveler's, so a
        # cross-traveler mix-up would fail too.
        for value in everyones_values:
            assert value not in blob, f"{traveler.traveler_id} leaked {value[:3]}..."

        if response.data:
            assert pii.find_pii(pii.serialisable(response.data), everyones_values) == []


def test_d6_no_response_key_matches_a_pii_field_name(built_conn, audit_path, dataset):
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    for tool in ALL_TOOLS:
        for traveler in dataset["travelers"]:
            response = _serve(built_conn, audit_path, tool, traveler_id=traveler.traveler_id)
            found = set(keys(response.model_dump(mode="json"))) & set(pii.PII_FIELD_NAMES)
            assert not found, found


def test_d7_a_leaking_handler_is_blocked_rather_than_served(built_conn, audit_path, monkeypatch):
    """Wire a deliberately bad handler through the layer.

    The allowlist (typed models with no PII fields) is the control that normally stops
    this. The scrubber is the backstop for when someone adds a field to a model without
    thinking, so it has to be tested with the allowlist deliberately broken.
    """
    real_email = store.traveler_pii_values(built_conn, generate.DEMO_TRAVELER)
    leaked = next(value for value in real_email if "@" in value)

    class LeakyPreferences(BaseModel):
        """Exactly the mistake being guarded against: a PII field on an output model."""

        traveler_id: str
        search_count: int
        contact_email: str

    def leaky_build(conn, request, as_of):
        profile = profiles.load_profile(conn, request.traveler_id)
        bad = LeakyPreferences(
            traveler_id=request.traveler_id,
            search_count=profile["search_count"],
            contact_email=leaked,
        )
        return bad, Provenance(
            source="profile_store", version=1,
            computed_at=events.now(), as_of=as_of, event_count=1,
            pipeline_version="test", freshness_seconds=0,
        )

    monkeypatch.setitem(
        access.HANDLERS, "get_traveler_preferences", access.Handler(leaky_build)
    )
    response = _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)

    assert response.status == "DENIED"
    assert response.reason_code == access.PII_LEAK_BLOCKED
    assert response.data is None
    # The denial explains the shape of the problem without repeating the value.
    assert leaked not in json.dumps(response.model_dump(mode="json"))


def test_d7_the_scrubber_catches_a_value_even_under_an_innocent_key(built_conn):
    known = store.traveler_pii_values(built_conn, generate.DEMO_TRAVELER)
    email = next(value for value in known if "@" in value)

    findings = pii.find_pii({"notes": f"contact them at {email}"}, known)
    assert findings
    assert all(email not in finding for finding in findings), "the finding repeats the value"


def test_d7_the_scrubber_catches_an_unknown_email_by_pattern(built_conn):
    findings = pii.find_pii({"free_text": "reach me at someone.else@example.test"}, set())
    assert findings


def test_d7_the_scrubber_does_not_fire_on_ordinary_travel_data(built_conn, audit_path):
    """A scrubber that cried wolf on dates and prices would get switched off."""
    response = _serve(
        built_conn, audit_path, "get_recent_searches", traveler_id=generate.DEMO_TRAVELER
    )
    assert response.status == "OK"
    assert pii.find_pii(response.data, set()) == []


# --- D8, D10: audit ---------------------------------------------------------------


def test_d8_every_call_appends_exactly_one_record(built_conn, audit_path, dataset):
    travelers = [
        generate.DEMO_TRAVELER, generate.OPT_OUT_TRAVELER,
        generate.NARROW_PURPOSE_TRAVELER, generate.NO_CONSENT_TRAVELER,
    ]
    expected = 0
    for traveler_id in travelers:
        for tool in ALL_TOOLS:
            _serve(built_conn, audit_path, tool, traveler_id=traveler_id)
            expected += 1
            assert len(audit.read_all(audit_path)) == expected


def test_d8_denials_are_recorded_with_their_reason(built_conn, audit_path):
    _serve(built_conn, audit_path, traveler_id=generate.OPT_OUT_TRAVELER)
    record = audit.read_all(audit_path)[-1]

    assert record["decision"] == "DENIED"
    assert record["reason_code"] == consent.AI_ACCESS_REVOKED
    assert record["traveler_id"] == generate.OPT_OUT_TRAVELER
    assert record["fields_returned"] == []


def test_d8_the_audit_log_records_field_names_but_no_values(built_conn, audit_path, dataset):
    everyones_values = store.all_pii_values(built_conn)

    for traveler in dataset["travelers"]:
        for tool in ALL_TOOLS:
            _serve(built_conn, audit_path, tool, traveler_id=traveler.traveler_id)

    lines = audit_path.read_text(encoding="utf-8")
    # An audit log of payloads is a second, unguarded copy of the data.
    for value in everyones_values:
        assert value not in lines

    for record in audit.read_all(audit_path):
        assert not set(record["fields_returned"]) & set(pii.PII_FIELD_NAMES)


def test_d8_a_pii_field_name_in_an_audit_record_is_rejected(audit_path):
    with pytest.raises(ValueError):
        audit.AuditRecord(
            ts=events.now(), request_id="x", caller="test", tool="t",
            traveler_id="trv_001", purpose="trip_planning", decision="OK",
            reason_code=None, fields_returned=["search_count", "email"],
            provenance=None, duration_ms=1,
        )


def test_d8_a_crashing_handler_still_leaves_an_audit_trail(built_conn, audit_path, monkeypatch):
    def explode(conn, request, as_of):
        raise RuntimeError("handler fell over")

    monkeypatch.setitem(access.HANDLERS, "get_traveler_preferences", access.Handler(explode))

    with pytest.raises(RuntimeError):
        _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)

    record = audit.read_all(audit_path)[-1]
    assert record["decision"] == access.INTERNAL_ERROR
    assert record["reason_code"] == access.INTERNAL_ERROR


def test_d10_the_audit_log_is_append_only(built_conn, audit_path):
    _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)
    first = audit_path.read_bytes()

    for traveler_id in (generate.OPT_OUT_TRAVELER, generate.DEMO_TRAVELER):
        _serve(built_conn, audit_path, traveler_id=traveler_id)

    after = audit_path.read_bytes()
    assert after.startswith(first), "earlier lines were rewritten"
    assert len(after) > len(first)


def test_an_unknown_tool_is_denied_and_audited(built_conn, audit_path):
    response = _serve(built_conn, audit_path, "get_everything", traveler_id=generate.DEMO_TRAVELER)

    assert response.status == "DENIED"
    assert response.reason_code == access.UNKNOWN_TOOL
    assert audit.read_all(audit_path)[-1]["reason_code"] == access.UNKNOWN_TOOL


def test_a_traveler_scoped_tool_requires_a_traveler_id(built_conn, audit_path):
    response = _serve(built_conn, audit_path, "get_traveler_preferences")

    assert response.status == "DENIED"
    assert response.reason_code == access.MISSING_TRAVELER_ID


def test_a_malformed_as_of_is_denied_and_audited_not_raised(built_conn, audit_path):
    """D8 says every call is audited. An exception before `serve` would break that."""
    response = _serve(
        built_conn, audit_path, "get_recent_searches",
        traveler_id=generate.DEMO_TRAVELER, as_of="last tuesday",
    )

    assert response.status == "DENIED"
    assert response.reason_code == access.INVALID_ARGUMENT
    assert response.data is None
    assert audit.read_all(audit_path)[-1]["reason_code"] == access.INVALID_ARGUMENT


def test_an_iso_string_as_of_means_the_same_as_the_datetime(built_conn, audit_path):
    instant = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    as_datetime = _serve(
        built_conn, audit_path, "get_recent_searches",
        traveler_id=generate.DEMO_TRAVELER, as_of=instant,
    )
    as_string = _serve(
        built_conn, audit_path, "get_recent_searches",
        traveler_id=generate.DEMO_TRAVELER, as_of=instant.isoformat(),
    )

    assert as_string.status == as_datetime.status == "OK"
    assert as_string.data["features"]["as_of"] == as_datetime.data["features"]["as_of"]
    assert as_string.data["searches"] == as_datetime.data["searches"]


# --- D9: provenance ---------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_d9_every_success_carries_complete_provenance(built_conn, audit_path, tool):
    response = _serve(built_conn, audit_path, tool, traveler_id=generate.DEMO_TRAVELER)

    assert response.status == "OK"
    prov = response.provenance
    assert prov is not None
    assert prov.source
    assert prov.version is not None
    assert prov.computed_at is not None
    assert prov.as_of is not None
    assert prov.pipeline_version
    assert prov.event_count > 0
    assert prov.freshness_seconds >= 0


def test_d9_computed_at_is_a_real_utc_instant_not_local_time_relabelled(
    built_conn, audit_path
):
    """`utc(datetime.now())` labels naive local time as UTC instead of converting it.

    That produces a `computed_at` wrong by the machine's offset — and `freshness_seconds`
    with it. It is invisible on a UTC host, which is exactly why it survived until the
    demo printed a timestamp five hours behind the audit record next to it.
    """
    before = datetime.now(timezone.utc)
    response = _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)
    after = datetime.now(timezone.utc)

    # Whole-second truncation can put `computed_at` up to a second before `before`.
    assert before - timedelta(seconds=1) <= response.provenance.computed_at <= after


def test_d9_provenance_is_copied_into_the_audit_record(built_conn, audit_path):
    response = _serve(built_conn, audit_path, traveler_id=generate.DEMO_TRAVELER)
    record = audit.read_all(audit_path)[-1]

    assert record["provenance"]["version"] == response.provenance.version
    assert record["provenance"]["source"] == response.provenance.source


def test_d9_as_of_defaults_to_the_knowledge_watermark_not_wall_clock(built_conn):
    watermark = store.knowledge_watermark(built_conn)
    resolved = access.resolve_as_of(built_conn, None)

    assert resolved == utc(datetime.fromisoformat(watermark))


# --- the listing tool -------------------------------------------------------------


def test_the_listing_tool_needs_no_consent_and_returns_no_profile_data(built_conn, audit_path):
    response = _serve(built_conn, audit_path, "list_travelers")

    assert response.status == "OK"
    assert len(response.data["travelers"]) == generate.TRAVELER_COUNT
    for entry in response.data["travelers"]:
        assert set(entry) == {"traveler_id", "has_consent_record", "ai_access", "purposes"}


def test_the_listing_reports_the_four_consent_cases_correctly(built_conn, audit_path):
    response = _serve(built_conn, audit_path, "list_travelers")
    by_id = {entry["traveler_id"]: entry for entry in response.data["travelers"]}

    assert by_id[generate.DEMO_TRAVELER]["ai_access"] is True
    assert by_id[generate.OPT_OUT_TRAVELER]["ai_access"] is False
    assert by_id[generate.NARROW_PURPOSE_TRAVELER]["ai_access"] is True
    assert access.DEFAULT_PURPOSE not in by_id[generate.NARROW_PURPOSE_TRAVELER]["purposes"]
    assert by_id[generate.NO_CONSENT_TRAVELER]["has_consent_record"] is False
    assert by_id[generate.NO_CONSENT_TRAVELER]["ai_access"] is None


# --- the choke point --------------------------------------------------------------


def test_a_traveler_with_no_profile_is_denied_not_invented(built_conn, audit_path, tmp_path):
    """A read path that fabricates an empty profile is worse than one that refuses."""
    conn = store.connect(tmp_path / "noprofile.db")
    store.load_travelers(conn, [generate._traveler(0)])
    # Consent, but no activity at all.
    conn.execute(
        "INSERT INTO consent_state (traveler_id, ai_access, purposes, consent_source_version,"
        " occurred_at, ingested_at, updated_at) VALUES (?, 1, ?, 1, ?, ?, ?)",
        ("trv_001", json.dumps([access.DEFAULT_PURPOSE]),
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    response = _serve(conn, audit_path, traveler_id="trv_001")
    assert response.status == "DENIED"
    assert response.reason_code == access.NO_PROFILE_YET
    conn.close()


def test_every_registered_handler_is_reachable_and_audited(built_conn, audit_path):
    for tool in access.HANDLERS:
        kwargs = {}
        if access.HANDLERS[tool].traveler_scoped:
            kwargs["traveler_id"] = generate.DEMO_TRAVELER
        response = _serve(built_conn, audit_path, tool, **kwargs)
        assert response.status == "OK", tool
        assert audit.read_all(audit_path)[-1]["tool"] == tool
