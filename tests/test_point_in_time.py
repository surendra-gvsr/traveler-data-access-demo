"""Spec: specs/003-point-in-time-features.md — criteria C1-C6.

Two different leaks hide behind the phrase "as of":

- *Future leakage* — counting an event that had not happened yet at `as_of`.
- *Knowledge leakage* — counting an event that had happened but that the platform did
  not know about yet at `as_of`.

The first is obvious and everyone filters for it. The second needs `ingested_at`, and a
feature store that omits it produces training data no production model can reproduce.
C6 is the test that catches both at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from traveler_access import features, generate, store
from traveler_access.events import Event, EventType, iso, utc

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _event(event_id, version, *, occurred, ingested, event_type=EventType.SEARCH,
           destination="BCN", traveler_id="trv_900"):
    payloads = {
        EventType.SEARCH: {
            "origin": "JFK", "destination": destination, "depart_date": "2026-07-01",
            "return_date": "2026-07-08", "adults": 2, "children": 0, "cabin": "economy",
        },
        EventType.BOOKING: {
            "booking_ref": f"BK-{event_id}", "origin": "JFK", "destination": destination,
            "depart_date": "2026-07-01", "nights": 7, "adults": 2, "children": 0,
            "cabin": "economy", "amount_minor": 100000, "currency": "USD",
        },
        EventType.CANCELLATION: {"booking_ref": f"BK-{event_id}", "reason": "plans_changed"},
    }
    return Event(
        event_id=event_id,
        event_type=event_type,
        traveler_id=traveler_id,
        occurred_at=occurred,
        ingested_at=ingested,
        source_version=version,
        payload=payloads[event_type],
    )


def _seed(conn, events, traveler_id="trv_900"):
    store.load_travelers(
        conn,
        [generate.Traveler(
            traveler_id=traveler_id, full_name="Test Person",
            email="test.person@mailbox.test", phone="+1-415-555-9999", home_market="US",
        )],
    )
    store.append_deliveries(conn, events)


# --- C1: future leakage -----------------------------------------------------------


def test_c1_an_event_after_as_of_does_not_change_features(fresh_conn):
    past = _event("evt_past", 1, occurred=BASE, ingested=BASE + timedelta(minutes=1))
    _seed(fresh_conn, [past])
    before = features.compute_features(fresh_conn, "trv_900", BASE + timedelta(days=1))

    future = _event(
        "evt_future", 2,
        occurred=BASE + timedelta(days=5),
        ingested=BASE + timedelta(days=5, minutes=1),
    )
    store.append_deliveries(fresh_conn, [future])
    after = features.compute_features(fresh_conn, "trv_900", BASE + timedelta(days=1))

    assert after.model_dump(exclude={"computed_at"}) == before.model_dump(exclude={"computed_at"})
    assert after.searches_last_30d == 1


# --- C2: knowledge leakage --------------------------------------------------------


def test_c2_a_late_event_is_invisible_until_it_arrives(fresh_conn):
    """One event, two vantage points, two different correct answers."""
    late = _event(
        "evt_late", 1,
        occurred=BASE,
        ingested=BASE + timedelta(days=9),
    )
    _seed(fresh_conn, [late])

    # Between occurrence and arrival: it happened, but we did not know.
    during = features.compute_features(fresh_conn, "trv_900", BASE + timedelta(days=2))
    assert during.searches_last_30d == 0
    assert during.visible_event_count == 0

    # After arrival: we know, and it still occurred at its original time.
    after = features.compute_features(fresh_conn, "trv_900", BASE + timedelta(days=10))
    assert after.searches_last_30d == 1
    assert after.visible_event_count == 1


def test_c2_holds_for_the_generated_late_arrival(built_conn, dataset):
    late = dataset["report"]["late_event"]
    traveler_id = late["traveler_id"]
    occurred = utc(datetime.fromisoformat(late["occurred_at"]))
    ingested = utc(datetime.fromisoformat(late["ingested_at"]))
    assert ingested > occurred, "the fixture is not actually late"

    between = occurred + (ingested - occurred) / 2
    visible_between = {
        event.event_id for event in features.visible_events(built_conn, traveler_id, between)
    }
    visible_after = {
        event.event_id for event in features.visible_events(built_conn, traveler_id, ingested)
    }

    assert late["event_id"] not in visible_between
    assert late["event_id"] in visible_after


def test_c2_filtering_only_on_occurred_at_would_give_a_different_answer(built_conn, dataset):
    """Proves the ingested_at clause is load-bearing rather than decorative."""
    late = dataset["report"]["late_event"]
    occurred = utc(datetime.fromisoformat(late["occurred_at"]))
    ingested = utc(datetime.fromisoformat(late["ingested_at"]))
    between = occurred + (ingested - occurred) / 2

    naive = built_conn.execute(
        "SELECT COUNT(DISTINCT event_id) AS n FROM events "
        "WHERE traveler_id = ? AND occurred_at <= ?",
        (late["traveler_id"], iso(between)),
    ).fetchone()["n"]
    honest = len(features.visible_events(built_conn, late["traveler_id"], between))

    assert naive > honest, "the two filters agree, so this dataset cannot show the bug"


# --- C3: dedupe -------------------------------------------------------------------


def test_c3_a_duplicated_delivery_counts_once(fresh_conn):
    event = _event("evt_dup", 1, occurred=BASE, ingested=BASE + timedelta(minutes=1))
    replay = event.model_copy(update={"ingested_at": BASE + timedelta(minutes=20)})
    _seed(fresh_conn, [event, replay])

    as_of = BASE + timedelta(days=1)
    assert len(features.visible_events(fresh_conn, "trv_900", as_of)) == 1
    assert features.compute_features(fresh_conn, "trv_900", as_of).searches_last_30d == 1


def test_c3_a_duplicate_uses_its_earliest_arrival(fresh_conn):
    """MIN(occurred_at) with a GROUP BY must not accidentally take the later arrival."""
    event = _event("evt_dup", 1, occurred=BASE, ingested=BASE + timedelta(hours=1))
    replay = event.model_copy(update={"ingested_at": BASE + timedelta(days=30)})
    _seed(fresh_conn, [event, replay])

    # Visible once the first copy has landed, regardless of the replay still being in
    # flight: the platform knew at that point.
    assert len(features.visible_events(fresh_conn, "trv_900", BASE + timedelta(hours=2))) == 1


# --- C4: window edges -------------------------------------------------------------


def test_c4_an_event_exactly_at_as_of_is_included(fresh_conn):
    event = _event("evt_edge", 1, occurred=BASE, ingested=BASE)
    _seed(fresh_conn, [event])

    assert features.compute_features(fresh_conn, "trv_900", BASE).searches_last_30d == 1


def test_c4_an_event_exactly_at_the_window_start_is_excluded(fresh_conn):
    as_of = BASE + timedelta(days=features.SEARCH_WINDOW_DAYS)
    boundary = as_of - timedelta(days=features.SEARCH_WINDOW_DAYS)  # == BASE
    inside = boundary + timedelta(seconds=1)

    _seed(fresh_conn, [
        _event("evt_on_edge", 1, occurred=boundary, ingested=boundary),
        _event("evt_inside", 2, occurred=inside, ingested=inside),
    ])

    # Half-open (window_start, as_of]: the event on the boundary is out, so the same
    # event cannot be counted twice by two adjacent windows.
    assert features.compute_features(fresh_conn, "trv_900", as_of).searches_last_30d == 1


# --- C5: replayability ------------------------------------------------------------


def test_c5_appending_newer_events_does_not_change_an_earlier_answer(fresh_conn):
    _seed(fresh_conn, [_event("evt_1", 1, occurred=BASE, ingested=BASE)])
    as_of = BASE + timedelta(days=2)
    first = features.compute_features(fresh_conn, "trv_900", as_of)

    for n in range(2, 6):
        moment = BASE + timedelta(days=10 * n)
        store.append_deliveries(
            fresh_conn, [_event(f"evt_{n}", n, occurred=moment, ingested=moment)]
        )

    again = features.compute_features(fresh_conn, "trv_900", as_of)
    assert again.model_dump(exclude={"computed_at"}) == first.model_dump(exclude={"computed_at"})


def test_c5_holds_for_every_traveler_in_the_dataset(built_conn, dataset):
    as_of = utc(datetime.fromisoformat(dataset["report"]["now"])) - timedelta(days=45)
    once = {
        traveler.traveler_id: features.compute_features(
            built_conn, traveler.traveler_id, as_of
        ).model_dump(exclude={"computed_at"})
        for traveler in dataset["travelers"]
    }
    twice = {
        traveler.traveler_id: features.compute_features(
            built_conn, traveler.traveler_id, as_of
        ).model_dump(exclude={"computed_at"})
        for traveler in dataset["travelers"]
    }
    assert twice == once


# --- C6: the strong property ------------------------------------------------------


def test_c6_deleting_everything_not_yet_known_at_t_changes_nothing(tmp_path, dataset):
    """The generalisation of C2, and the test that would have caught the bug first.

    If features at T depend only on what was knowable at T, then physically removing
    every event the platform had not yet received at T must be a no-op. Any dependence
    on later knowledge shows up here as a difference, whatever its cause.
    """
    # The cut is placed inside the known late event's in-flight window, so the
    # property is actually under strain: at this instant the event has occurred but
    # has not arrived, which is the only situation where the two filters disagree.
    late = dataset["report"]["late_event"]
    occurred = utc(datetime.fromisoformat(late["occurred_at"]))
    ingested = utc(datetime.fromisoformat(late["ingested_at"]))
    cut = occurred + (ingested - occurred) / 2

    full = store.connect(tmp_path / "full.db")
    store.load_travelers(full, dataset["travelers"])
    store.append_deliveries(full, dataset["events"])

    truncated = store.connect(tmp_path / "truncated.db")
    store.load_travelers(truncated, dataset["travelers"])
    store.append_deliveries(
        truncated, [event for event in dataset["events"] if utc(event.ingested_at) <= cut]
    )

    removed = len(dataset["events"]) - len(
        [e for e in dataset["events"] if utc(e.ingested_at) <= cut]
    )
    assert removed > 0, "nothing was removed, so the property is untested"

    for traveler in dataset["travelers"]:
        tid = traveler.traveler_id
        expected = features.compute_features(full, tid, cut).model_dump(exclude={"computed_at"})
        actual = features.compute_features(truncated, tid, cut).model_dump(
            exclude={"computed_at"}
        )
        assert actual == expected, tid

        assert [s.model_dump() for s in features.recent_searches(truncated, tid, cut)] == [
            s.model_dump() for s in features.recent_searches(full, tid, cut)
        ], tid

    full.close()
    truncated.close()


def test_c6_at_several_vantage_points(tmp_path, dataset):
    now = utc(datetime.fromisoformat(dataset["report"]["now"]))
    full = store.connect(tmp_path / "full_multi.db")
    store.load_travelers(full, dataset["travelers"])
    store.append_deliveries(full, dataset["events"])

    for days_back in (10, 20, 60, 90):
        cut = now - timedelta(days=days_back)
        truncated = store.connect(tmp_path / f"trunc_{days_back}.db")
        store.load_travelers(truncated, dataset["travelers"])
        store.append_deliveries(
            truncated, [e for e in dataset["events"] if utc(e.ingested_at) <= cut]
        )
        for traveler in dataset["travelers"]:
            tid = traveler.traveler_id
            assert features.compute_features(truncated, tid, cut).model_dump(
                exclude={"computed_at"}
            ) == features.compute_features(full, tid, cut).model_dump(
                exclude={"computed_at"}
            ), f"{tid} at -{days_back}d"
        truncated.close()

    full.close()


# --- the served view carries its vantage point ------------------------------------


def test_features_carry_the_as_of_they_were_computed_from(built_conn):
    """A point-in-time value passed around without its vantage point is unverifiable."""
    as_of = utc(datetime(2026, 9, 1, tzinfo=timezone.utc))
    computed = features.compute_features(built_conn, generate.DEMO_TRAVELER, as_of)

    assert computed.as_of == as_of
    assert computed.computed_at.tzinfo is not None
    # computed_at and as_of are different clocks and must not be conflated.
    assert computed.computed_at != computed.as_of


def test_search_summaries_are_newest_first_and_respect_the_limit(built_conn, dataset):
    as_of = utc(datetime.fromisoformat(dataset["report"]["now"]))
    rows = features.recent_searches(
        built_conn, generate.DEMO_TRAVELER, as_of, window_days=365, limit=3
    )

    assert len(rows) == 3
    assert [r.occurred_at for r in rows] == sorted(
        (r.occurred_at for r in rows), reverse=True
    )


def test_search_summaries_never_carry_the_contact_email(built_conn, dataset):
    """The events do hold an email; the summary model has no field for it."""
    raw = built_conn.execute(
        "SELECT payload FROM events WHERE traveler_id = ? AND event_type = 'search' LIMIT 1",
        (generate.DEMO_TRAVELER,),
    ).fetchone()
    assert "contact_email" in raw["payload"], "no email in the source, so nothing to strip"

    as_of = utc(datetime.fromisoformat(dataset["report"]["now"]))
    for summary in features.recent_searches(built_conn, generate.DEMO_TRAVELER, as_of):
        assert "contact_email" not in summary.model_dump()
