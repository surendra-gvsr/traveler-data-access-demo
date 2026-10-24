"""Spec: specs/001-event-stream.md — criteria A1-A8.

The point of these tests is that the *defects* are present. A generator that quietly
produced a clean, ordered, duplicate-free stream would make every downstream test pass
for the wrong reason, so each injected defect gets an assertion of its own.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from traveler_access import generate
from traveler_access.events import Event, EventType


def test_a1_same_seed_produces_identical_bytes(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    generate.write(first, generate.generate())
    generate.write(second, generate.generate())

    for name in ("events.jsonl", "travelers.json", "generation_report.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_a1_different_seed_produces_a_different_stream(tmp_path):
    """Guards against the generator ignoring its seed, which would make A1 vacuous."""
    generate.write(tmp_path / "a", generate.generate(seed=generate.DEFAULT_SEED))
    generate.write(tmp_path / "b", generate.generate(seed=generate.DEFAULT_SEED + 1))

    assert (tmp_path / "a" / "events.jsonl").read_bytes() != (
        tmp_path / "b" / "events.jsonl"
    ).read_bytes()


def test_a2_duplicates_are_byte_identical_apart_from_ingested_at(dataset):
    counts = Counter(event.event_id for event in dataset["events"])
    duplicated = [event_id for event_id, count in counts.items() if count > 1]
    assert duplicated, "the stream contains no duplicate deliveries"
    assert sorted(duplicated) == dataset["report"]["duplicate_event_ids"]

    for event_id in duplicated:
        copies = [event for event in dataset["events"] if event.event_id == event_id]
        fields = [copy.model_dump(exclude={"ingested_at"}) for copy in copies]
        assert all(field == fields[0] for field in fields), event_id
        assert len({copy.ingested_at for copy in copies}) == len(copies), event_id


def test_a3_at_least_one_traveler_is_delivered_out_of_version_order(dataset):
    highest: dict[str, int] = {}
    offenders: set[str] = set()
    for event in dataset["events"]:
        seen = highest.get(event.traveler_id, 0)
        if event.source_version < seen:
            offenders.add(event.traveler_id)
        highest[event.traveler_id] = max(seen, event.source_version)

    assert offenders, "every traveler arrived in version order; nothing to reorder"
    assert sorted(offenders) == dataset["report"]["out_of_order_travelers"]


def test_a4_at_least_one_event_arrived_more_than_twelve_hours_late(dataset):
    late = [
        event
        for event in dataset["events"]
        if event.ingested_at - event.occurred_at > timedelta(hours=12)
    ]
    assert late, "no event has a meaningful ingestion lag"
    assert dataset["report"]["late_event"]["event_id"] in {event.event_id for event in late}


def test_a5_every_event_validates_and_was_ingested_after_it_occurred(dataset):
    for event in dataset["events"]:
        # Round-tripping re-runs the model validators, which enforce the ordering and
        # the per-type required payload fields.
        assert Event.model_validate(event.model_dump()) == event
        assert event.occurred_at <= event.ingested_at, event.event_id


def test_a5_an_event_ingested_before_it_occurred_is_rejected(dataset):
    sample = dataset["events"][0]
    fields = sample.model_dump()
    fields["ingested_at"] = sample.occurred_at - timedelta(seconds=1)

    with pytest.raises(ValueError, match="ingested_at"):
        Event(**fields)


def test_a5_an_event_missing_a_required_payload_field_is_rejected(dataset):
    search = next(e for e in dataset["events"] if e.event_type is EventType.SEARCH)
    fields = search.model_dump()
    fields["payload"] = {k: v for k, v in search.payload.items() if k != "destination"}

    with pytest.raises(ValueError, match="destination"):
        Event(**fields)


def test_a6_exactly_one_traveler_revoked_and_a_stale_opt_in_follows_it(dataset):
    consent = [
        event for event in dataset["events"] if event.event_type is EventType.CONSENT_CHANGE
    ]
    newest: dict[str, Event] = {}
    for event in consent:
        current = newest.get(event.traveler_id)
        if current is None or event.source_version > current.source_version:
            newest[event.traveler_id] = event

    revoked = [tid for tid, event in newest.items() if event.payload["ai_access"] is False]
    assert revoked == [generate.OPT_OUT_TRAVELER]

    opt_out = newest[generate.OPT_OUT_TRAVELER]
    stale = next(
        event
        for event in consent
        if event.event_id == dataset["report"]["stale_opt_in"]["event_id"]
    )
    assert stale.payload["ai_access"] is True
    assert stale.source_version < opt_out.source_version, "the opt-in is not the older write"
    assert stale.ingested_at > opt_out.ingested_at, "the opt-in does not arrive last"


def test_a7_every_cancellation_references_an_earlier_booking(dataset):
    bookings = {
        event.payload["booking_ref"]: event
        for event in dataset["events"]
        if event.event_type is EventType.BOOKING
    }
    cancellations = [
        event for event in dataset["events"] if event.event_type is EventType.CANCELLATION
    ]
    assert cancellations, "no cancellations were generated"

    for event in cancellations:
        booking = bookings.get(event.payload["booking_ref"])
        assert booking is not None, event.payload["booking_ref"]
        assert booking.traveler_id == event.traveler_id
        assert booking.occurred_at < event.occurred_at


def test_a8_exactly_one_traveler_has_no_consent_event(dataset):
    with_consent = {
        event.traveler_id
        for event in dataset["events"]
        if event.event_type is EventType.CONSENT_CHANGE
    }
    everyone = {traveler.traveler_id for traveler in dataset["travelers"]}
    assert everyone - with_consent == {generate.NO_CONSENT_TRAVELER}


def test_travelers_file_holds_pii_but_no_consent(dataset):
    """Identity and permission are separate concerns, and the schema says so."""
    traveler = dataset["travelers"][0]
    assert traveler.email and traveler.full_name and traveler.phone
    assert not hasattr(traveler, "ai_access")


def test_no_event_occurred_after_the_datasets_fixed_now(dataset):
    for event in dataset["events"]:
        assert event.occurred_at <= generate.NOW, event.event_id
        assert event.ingested_at <= generate.NOW, event.event_id
