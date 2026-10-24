"""Spec: specs/002-profile-builder.md — criteria B1-B9.

Every test here is really one question: if the same event arrives twice, or late, or
out of order, does the profile still tell the truth? The field classes make the answer
different per field, so the tests assert per field class rather than per event.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from traveler_access import generate, pii, profiles, store
from traveler_access.events import Event, EventType

BASE = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


def _traveler(conn, traveler_id="trv_900"):
    store.load_travelers(
        conn,
        [
            generate.Traveler(
                traveler_id=traveler_id,
                full_name="Test Person",
                email="test.person@mailbox.test",
                phone="+1-415-555-9999",
                home_market="US",
            )
        ],
    )
    return traveler_id


def _search(traveler_id, version, destination, *, minutes=0, event_id=None):
    occurred = BASE + timedelta(minutes=minutes)
    return Event(
        event_id=event_id or f"evt_s{version:03d}",
        event_type=EventType.SEARCH,
        traveler_id=traveler_id,
        occurred_at=occurred,
        ingested_at=occurred + timedelta(seconds=30),
        source_version=version,
        payload={
            "origin": "JFK",
            "destination": destination,
            "depart_date": "2026-07-01",
            "return_date": "2026-07-08",
            "adults": 2,
            "children": 0,
            "cabin": "economy",
        },
    )


def _consent(traveler_id, version, *, ai_access, event_id=None, purposes=("trip_planning",)):
    occurred = BASE + timedelta(minutes=version)
    return Event(
        event_id=event_id or f"evt_c{version:03d}",
        event_type=EventType.CONSENT_CHANGE,
        traveler_id=traveler_id,
        occurred_at=occurred,
        ingested_at=occurred + timedelta(seconds=30),
        source_version=version,
        payload={
            "ai_access": ai_access,
            "purposes": list(purposes) if ai_access else [],
            "channel": "account_settings",
        },
    )


def _profile_snapshot(conn):
    rows = conn.execute("SELECT * FROM profiles ORDER BY traveler_id").fetchall()
    return [
        {k: row[k] for k in row.keys() if k != "updated_at"}
        for row in rows
    ]


def _consent_snapshot(conn):
    rows = conn.execute("SELECT * FROM consent_state ORDER BY traveler_id").fetchall()
    return [{k: row[k] for k in row.keys() if k != "updated_at"} for row in rows]


# --- B1, B2: replay ---------------------------------------------------------------


def test_b1_replaying_the_whole_stream_changes_nothing(tmp_path, dataset):
    conn = store.connect(tmp_path / "replay.db")
    store.load_travelers(conn, dataset["travelers"])
    store.append_deliveries(conn, dataset["events"])
    profiles.build_profiles(conn)

    once_profiles = _profile_snapshot(conn)
    once_consent = _consent_snapshot(conn)

    # Re-deliver the identical stream. Every event_id is already in the ledger.
    store.append_deliveries(conn, dataset["events"])
    summary = profiles.build_profiles(conn)

    assert summary.applied == 0
    assert summary.duplicates == len(dataset["events"])
    assert _profile_snapshot(conn) == once_profiles
    assert _consent_snapshot(conn) == once_consent
    conn.close()


def test_b2_a_duplicate_is_reported_and_leaves_counters_untouched(fresh_conn):
    traveler_id = _traveler(fresh_conn)
    event = _search(traveler_id, 1, "BCN")

    first = profiles.apply_event(fresh_conn, event)
    before = _profile_snapshot(fresh_conn)
    second = profiles.apply_event(fresh_conn, event)

    assert first.status == "APPLIED"
    assert second.status == "DUPLICATE"
    assert second.profile_version == first.profile_version
    assert _profile_snapshot(fresh_conn) == before


def test_b2_the_ledger_is_what_makes_the_duplicate_detectable(fresh_conn):
    traveler_id = _traveler(fresh_conn)
    profiles.apply_event(fresh_conn, _search(traveler_id, 1, "BCN"))

    ledger = fresh_conn.execute("SELECT COUNT(*) AS n FROM processed_events").fetchone()["n"]
    assert ledger == 1


# --- B3, B4: the stale guard ------------------------------------------------------


def test_b3_b4_a_stale_write_skips_overwrites_but_still_counts(fresh_conn):
    traveler_id = _traveler(fresh_conn)

    newer = profiles.apply_event(fresh_conn, _search(traveler_id, 5, "ROM", minutes=50))
    older = profiles.apply_event(fresh_conn, _search(traveler_id, 3, "LIS", minutes=30))

    assert newer.status == "APPLIED"
    assert older.status == "APPLIED_STALE"

    profile = profiles.load_profile(fresh_conn, traveler_id)
    # B3: the last-write-wins field reflects v5, not the v3 that arrived later.
    assert profile["last_search_destination"] == "ROM"
    # B4: the counter is commutative, so the stale event still contributes to it.
    assert profile["search_count"] == 2
    assert json.loads(profile["destination_counts"]) == {"ROM": 1, "LIS": 1}


def test_b3_a_newer_search_does_not_suppress_an_older_bookings_own_fields(fresh_conn):
    """The stale guard is per field. A profile-wide watermark fails this.

    No search event ever writes `preferred_currency`, so a search at v10 must not stop
    a booking at v4 from setting it. Before this was per-field, the currency stayed
    NULL forever whenever a search happened to be delivered first.
    """
    traveler_id = _traveler(fresh_conn)
    occurred = BASE + timedelta(minutes=40)
    booking = Event(
        event_id="evt_b004",
        event_type=EventType.BOOKING,
        traveler_id=traveler_id,
        occurred_at=occurred,
        ingested_at=occurred + timedelta(seconds=30),
        source_version=4,
        payload={
            "booking_ref": "BK-0001",
            "origin": "JFK",
            "destination": "BCN",
            "depart_date": "2026-07-01",
            "nights": 5,
            "adults": 2,
            "children": 0,
            "cabin": "economy",
            "amount_minor": 120000,
            "currency": "EUR",
        },
    )

    profiles.apply_event(fresh_conn, _search(traveler_id, 10, "ROM", minutes=100))
    profiles.apply_event(fresh_conn, booking)

    profile = profiles.load_profile(fresh_conn, traveler_id)
    assert profile["preferred_currency"] == "EUR", "the booking's own field was suppressed"
    # The search still owns the field they do compete over.
    assert profile["last_search_destination"] == "ROM"


def test_b3_equal_versions_are_treated_as_stale(fresh_conn):
    """A redelivery under a new id must not win a tie; `<=` is deliberate."""
    traveler_id = _traveler(fresh_conn)
    profiles.apply_event(fresh_conn, _search(traveler_id, 4, "ROM", event_id="evt_a"))
    result = profiles.apply_event(fresh_conn, _search(traveler_id, 4, "LIS", event_id="evt_b"))

    assert result.status == "APPLIED_STALE"
    assert profiles.load_profile(fresh_conn, traveler_id)["last_search_destination"] == "ROM"


# --- B5: order independence -------------------------------------------------------


def test_b5_shuffled_delivery_order_produces_the_same_profiles(tmp_path, dataset):
    def build(events):
        conn = store.connect(tmp_path / f"order_{len(events)}_{id(events)}.db")
        store.load_travelers(conn, dataset["travelers"])
        store.append_deliveries(conn, events)
        profiles.build_profiles(conn)
        snapshot = (_profile_snapshot(conn), _consent_snapshot(conn))
        conn.close()
        return snapshot

    in_order = build(list(dataset["events"]))

    shuffled = list(dataset["events"])
    random.Random(99).shuffle(shuffled)
    out_of_order = build(shuffled)

    in_profiles, in_consent = in_order
    out_profiles, out_consent = out_of_order

    assert out_consent == in_consent

    counters = (
        "search_count", "booking_count", "cancellation_count",
        "destination_counts", "cabin_counts", "nights_total", "nights_samples",
        "booking_value_minor_total", "lead_time_days_total", "lead_time_samples",
    )
    lww = ("last_search_destination", "last_search_depart_date", "home_origin",
           "party_adults", "party_children", "preferred_currency")

    for expected, actual in zip(in_profiles, out_profiles, strict=True):
        assert expected["traveler_id"] == actual["traveler_id"]
        for field in counters:
            assert actual[field] == expected[field], f"{actual['traveler_id']}.{field}"
        for field in lww:
            assert actual[field] == expected[field], f"{actual['traveler_id']}.{field}"


# --- B6: crash safety -------------------------------------------------------------


def test_b6_a_crash_mid_apply_leaves_no_half_written_event(fresh_conn, monkeypatch):
    traveler_id = _traveler(fresh_conn)
    profiles.apply_event(fresh_conn, _search(traveler_id, 1, "BCN"))

    before_profile = _profile_snapshot(fresh_conn)
    before_ledger = fresh_conn.execute(
        "SELECT event_id FROM processed_events ORDER BY event_id"
    ).fetchall()

    def explode(*args, **kwargs):
        raise RuntimeError("disk fell over")

    monkeypatch.setattr(profiles, "_counter_updates", explode)

    with pytest.raises(RuntimeError):
        profiles.apply_event(fresh_conn, _search(traveler_id, 2, "ROM"))

    # The ledger insert and the profile update share one transaction, so the rollback
    # must take both: otherwise the event would be marked processed but never applied.
    assert _profile_snapshot(fresh_conn) == before_profile
    assert fresh_conn.execute(
        "SELECT event_id FROM processed_events ORDER BY event_id"
    ).fetchall() == before_ledger


def test_b6_the_event_can_be_reapplied_after_a_crash(fresh_conn, monkeypatch):
    traveler_id = _traveler(fresh_conn)
    event = _search(traveler_id, 1, "BCN")

    monkeypatch.setattr(profiles, "_counter_updates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        profiles.apply_event(fresh_conn, event)

    monkeypatch.undo()
    assert profiles.apply_event(fresh_conn, event).status == "APPLIED"
    assert profiles.load_profile(fresh_conn, traveler_id)["search_count"] == 1


# --- B7: no PII in the derived view -----------------------------------------------


def test_b7_derived_preferences_contain_no_pii(built_conn, dataset):
    known = store.all_pii_values(built_conn)
    assert known, "the PII registry is empty, so this test proves nothing"

    for traveler in dataset["travelers"]:
        profile = profiles.load_profile(built_conn, traveler.traveler_id)
        if profile is None:
            continue
        payload = pii.serialisable(profiles.derive_preferences(profile).model_dump(mode="json"))
        assert pii.find_pii(payload, known) == []


def test_b7_the_profile_row_does_hold_pii_that_the_view_drops(built_conn):
    """The scrubber would be untested if the stored row were already clean."""
    profile = profiles.load_profile(built_conn, generate.DEMO_TRAVELER)
    assert profile["contact_email"], "no PII is stored, so dropping it is not a real step"
    assert "contact_email" not in profiles.derive_preferences(profile).model_dump()


# --- B8, B9: consent ---------------------------------------------------------------


def test_b8_a_replayed_opt_in_does_not_resurrect_revoked_access(fresh_conn):
    traveler_id = _traveler(fresh_conn)

    assert profiles.apply_event(
        fresh_conn, _consent(traveler_id, 2, ai_access=True, event_id="evt_in")
    ).status == "APPLIED"
    assert profiles.apply_event(
        fresh_conn, _consent(traveler_id, 9, ai_access=False, event_id="evt_out")
    ).status == "APPLIED"
    replayed = profiles.apply_event(
        fresh_conn, _consent(traveler_id, 2, ai_access=True, event_id="evt_in_again")
    )

    assert replayed.status == "APPLIED_STALE"
    row = fresh_conn.execute(
        "SELECT * FROM consent_state WHERE traveler_id = ?", (traveler_id,)
    ).fetchone()
    assert bool(row["ai_access"]) is False
    assert row["consent_source_version"] == 9


def test_b8_holds_in_the_generated_dataset(built_conn, dataset):
    """The same guard, exercised by the stream rather than by hand-built events."""
    stale = dataset["report"]["stale_opt_in"]
    row = built_conn.execute(
        "SELECT * FROM consent_state WHERE traveler_id = ?", (generate.OPT_OUT_TRAVELER,)
    ).fetchone()

    assert bool(row["ai_access"]) is False
    assert row["consent_source_version"] == dataset["report"]["opt_out_source_version"]
    assert row["consent_source_version"] > stale["source_version"]


def test_b9_a_traveler_with_no_consent_event_has_no_consent_row(built_conn):
    row = built_conn.execute(
        "SELECT * FROM consent_state WHERE traveler_id = ?", (generate.NO_CONSENT_TRAVELER,)
    ).fetchone()
    assert row is None

    # Absent, not defaulted: the traveler exists, and has activity.
    assert store.traveler_exists(built_conn, generate.NO_CONSENT_TRAVELER)


# --- incremental build ------------------------------------------------------------


def test_building_in_two_passes_matches_building_in_one(tmp_path, dataset):
    def build(limit_first):
        conn = store.connect(tmp_path / f"inc_{limit_first}.db")
        store.load_travelers(conn, dataset["travelers"])
        store.append_deliveries(conn, dataset["events"])
        if limit_first:
            first = profiles.build_profiles(conn, limit=limit_first)
            assert first.total == limit_first
            profiles.build_profiles(conn)
        else:
            profiles.build_profiles(conn)
        snapshot = (_profile_snapshot(conn), _consent_snapshot(conn), store.checkpoint(conn))
        conn.close()
        return snapshot

    assert build(100) == build(0)


def test_the_checkpoint_stops_the_second_build_doing_work(tmp_path, dataset):
    conn = store.connect(tmp_path / "ckpt.db")
    store.load_travelers(conn, dataset["travelers"])
    store.append_deliveries(conn, dataset["events"])
    profiles.build_profiles(conn)

    again = profiles.build_profiles(conn)
    assert again.total == 0
    assert store.checkpoint(conn) == len(dataset["events"])
    conn.close()
