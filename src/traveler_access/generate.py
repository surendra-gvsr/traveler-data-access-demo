"""Seeded synthetic event generator. Spec: specs/001-event-stream.md

Produces a stream that is deliberately hostile in three specific ways — duplicate
deliveries, out-of-order deliveries, and late arrivals — because code that has only
ever seen clean input is untested code. Every injected defect is recorded in
`generation_report.json` so tests assert against known-bad records rather than hoping
the generator happened to produce something interesting.

`source_version` is assigned in **occurrence order** per traveler, which is what an
upstream system stamping a monotonic counter at production time would do. Delivery
order is then allowed to disagree with it. Keeping those two orders conceptually
separate is the whole point of the exercise.

No real traveler data. Names, emails and phone numbers are synthetic; emails use the
reserved `.test` TLD and phones use the 555 reserved exchange.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import Event, EventType, Traveler, iso

# Fixed so the dataset and its tests never rot.
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_DAYS = 120
DEFAULT_SEED = 7

TRAVELER_COUNT = 12
DEMO_TRAVELER = "trv_003"            # consented, coherent pattern: the demo's subject
OPT_OUT_TRAVELER = "trv_010"         # revokes AI access, plus a stale opt-in arriving later
NARROW_PURPOSE_TRAVELER = "trv_011"  # consented, but only for "support"
NO_CONSENT_TRAVELER = "trv_012"      # never heard of: the fail-closed path

LATE_ARRIVAL = timedelta(days=9, hours=4)   # long enough to overtake a newer event
STALE_CONSENT_LAG = timedelta(days=2)       # opt-in arriving after the opt-out


@dataclass(frozen=True)
class Persona:
    name: str
    destinations: tuple[str, ...]
    adults: int
    children: int
    cabin: str
    nights: tuple[int, int]
    lead_days: tuple[int, int]
    nightly_minor: int
    currency: str


PERSONAS = (
    Persona("family_beach", ("CUN", "MCO", "PUJ"), 2, 2, "economy", (5, 9), (45, 90), 28000, "USD"),
    Persona("business_city", ("LHR", "FRA", "SFO"), 1, 0, "business", (1, 3), (3, 14), 41000, "USD"),
    Persona("couple_citybreak", ("BCN", "LIS", "ROM"), 2, 0, "premium_economy", (3, 5), (20, 60), 23000, "EUR"),
    Persona("ski_group", ("DEN", "GVA", "YVR"), 4, 1, "economy", (6, 8), (60, 120), 35000, "USD"),
)

FIRST_NAMES = ("Avery", "Rowan", "Imani", "Teodor", "Noor", "Priya",
               "Mateo", "Saoirse", "Dmitri", "Linnea", "Kwame", "Yuki")
LAST_NAMES = ("Okafor", "Lindqvist", "Haddad", "Novak", "Mbeki", "Rao",
              "Castellanos", "Byrne", "Volkov", "Aaltonen", "Mensah", "Tanaka")
MARKETS = ("US", "SE", "AE", "CZ", "ZA", "IN", "MX", "IE", "PL", "FI", "GH", "JP")
ORIGINS = ("JFK", "ORD", "ARN", "DXB", "LAX", "BOM", "MEX", "DUB", "WAW", "HEL", "ACC", "NRT")


@dataclass
class Generated:
    travelers: list[Traveler] = field(default_factory=list)
    deliveries: list[tuple[datetime, Event]] = field(default_factory=list)
    report: dict = field(default_factory=dict)


@dataclass
class Pending:
    """An event before a version or an arrival time has been decided."""

    event_type: EventType
    occurred_at: datetime
    payload: dict


def _traveler(i: int) -> Traveler:
    first, last = FIRST_NAMES[i], LAST_NAMES[i]
    return Traveler(
        traveler_id=f"trv_{i + 1:03d}",
        full_name=f"{first} {last}",
        email=f"{first.lower()}.{last.lower()}@mailbox.test",
        phone=f"+1-415-555-{1000 + i * 7:04d}",
        home_market=MARKETS[i],
    )


def _date(dt: datetime) -> str:
    return dt.date().isoformat()


def _traveler_events(i: int, traveler: Traveler, rng: random.Random) -> list[Pending]:
    persona = PERSONAS[i % len(PERSONAS)]
    tid = traveler.traveler_id
    start = NOW - timedelta(days=WINDOW_DAYS)
    pending: list[Pending] = []

    # Consent first, as it would be in life: you agree, then you use the product.
    if tid != NO_CONSENT_TRAVELER:
        purposes = ["support"] if tid == NARROW_PURPOSE_TRAVELER else ["trip_planning", "support"]
        pending.append(
            Pending(
                EventType.CONSENT_CHANGE,
                start + timedelta(hours=rng.randint(1, 20)),
                {"ai_access": True, "purposes": purposes, "channel": "account_signup"},
            )
        )

    activity_count = 16 if tid == DEMO_TRAVELER else rng.randint(9, 18)
    bookings: list[tuple[str, datetime]] = []

    for n in range(activity_count):
        occurred = start + timedelta(
            days=(WINDOW_DAYS - 2) * (n + 1) / (activity_count + 1),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        destination = rng.choice(persona.destinations)
        nights = rng.randint(*persona.nights)
        depart = occurred + timedelta(days=rng.randint(*persona.lead_days))
        search_payload = {
            "origin": ORIGINS[i],
            "destination": destination,
            "depart_date": _date(depart),
            "return_date": _date(depart + timedelta(days=nights)),
            "adults": persona.adults,
            "children": persona.children,
            "cabin": persona.cabin,
            "contact_email": traveler.email,  # PII in the raw payload, on purpose
        }
        pending.append(Pending(EventType.SEARCH, occurred, search_payload))

        if rng.random() < 0.28:
            booked_at = occurred + timedelta(hours=rng.randint(1, 60))
            ref = f"BK{tid[-3:]}{n:02d}"
            pax = persona.adults + persona.children
            pending.append(
                Pending(
                    EventType.BOOKING,
                    booked_at,
                    search_payload
                    | {
                        "booking_ref": ref,
                        "nights": nights,
                        "amount_minor": persona.nightly_minor * nights * pax,
                        "currency": persona.currency,
                    },
                )
            )
            bookings.append((ref, booked_at))

    for ref, booked_at in bookings:
        if rng.random() < 0.18:
            pending.append(
                Pending(
                    EventType.CANCELLATION,
                    booked_at + timedelta(days=rng.randint(1, 20)),
                    {"booking_ref": ref, "reason": rng.choice(["plans_changed", "price_drop_rebook"])},
                )
            )

    if tid == OPT_OUT_TRAVELER:
        # A re-confirmation that happened *before* the opt-out, and will be delivered
        # after it. Distinct event, lower version — this is a stale write, not a replay.
        pending.append(
            Pending(
                EventType.CONSENT_CHANGE,
                NOW - timedelta(days=20),
                {"ai_access": True, "purposes": ["trip_planning"], "channel": "marketing_prompt"},
            )
        )
        pending.append(
            Pending(
                EventType.CONSENT_CHANGE,
                NOW - timedelta(days=9),
                {"ai_access": False, "purposes": [], "channel": "privacy_centre"},
            )
        )

    # Nothing may occur after the dataset's fixed NOW. Cancellations in particular are
    # scheduled days after their booking and can otherwise fall off the end of the
    # window, which would put events in the future of every query.
    pending = [item for item in pending if item.occurred_at <= NOW]
    pending.sort(key=lambda p: p.occurred_at)
    return pending


def generate(seed: int = DEFAULT_SEED) -> Generated:
    rng = random.Random(seed)
    out = Generated()
    event_no = 0

    for i in range(TRAVELER_COUNT):
        traveler = _traveler(i)
        out.travelers.append(traveler)
        events: list[Event] = []

        for version, item in enumerate(_traveler_events(i, traveler, rng), start=1):
            event_no += 1
            events.append(
                Event(
                    event_id=f"evt_{event_no:06d}",
                    event_type=item.event_type,
                    traveler_id=traveler.traveler_id,
                    occurred_at=item.occurred_at,
                    ingested_at=item.occurred_at + timedelta(seconds=rng.randint(5, 900)),
                    source_version=version,
                    payload=item.payload,
                )
            )

        if traveler.traveler_id == OPT_OUT_TRAVELER:
            events = _delay_stale_opt_in(events, out.report)

        out.deliveries.extend((e.ingested_at, e) for e in events)

    _inject_late_arrival(out)
    _inject_duplicates(out, rng)

    # A delivery that would arrive after NOW has not arrived yet, so it is not in the
    # log at all. (Late arrivals and duplicate replays both push arrival times out.)
    out.deliveries = [pair for pair in out.deliveries if pair[0] <= NOW]

    # Delivery order is arrival order. Disagreement with occurrence order falls out.
    out.deliveries.sort(key=lambda pair: (iso(pair[0]), pair[1].event_id))

    out.report.update(
        {
            "seed": seed,
            "now": iso(NOW),
            "window_days": WINDOW_DAYS,
            "traveler_count": len(out.travelers),
            "delivery_count": len(out.deliveries),
            "distinct_event_count": len({e.event_id for _, e in out.deliveries}),
            "duplicate_event_ids": _duplicate_event_ids(out.deliveries),
            "demo_traveler": DEMO_TRAVELER,
            "opt_out_traveler": OPT_OUT_TRAVELER,
            "narrow_purpose_traveler": NARROW_PURPOSE_TRAVELER,
            "no_consent_traveler": NO_CONSENT_TRAVELER,
            "out_of_order_travelers": sorted(_out_of_order_travelers(out.deliveries)),
        }
    )
    return out


def _delay_stale_opt_in(events: list[Event], report: dict) -> list[Event]:
    """Make the opted-out traveler's earlier opt-in arrive after the opt-out."""
    opt_out = next(
        e for e in events
        if e.event_type == EventType.CONSENT_CHANGE and e.payload["ai_access"] is False
    )
    stale = next(
        e for e in events
        if e.event_type == EventType.CONSENT_CHANGE
        and e.payload.get("channel") == "marketing_prompt"
    )
    delayed = stale.model_copy(update={"ingested_at": opt_out.ingested_at + STALE_CONSENT_LAG})
    report["opt_out_event_id"] = opt_out.event_id
    report["opt_out_occurred_at"] = iso(opt_out.occurred_at)
    report["opt_out_source_version"] = opt_out.source_version
    report["stale_opt_in"] = {
        "event_id": delayed.event_id,
        "source_version": delayed.source_version,
        "occurred_at": iso(delayed.occurred_at),
        "ingested_at": iso(delayed.ingested_at),
    }
    return [delayed if e.event_id == stale.event_id else e for e in events]


def _inject_late_arrival(out: Generated) -> None:
    """One search that sat in a queue long enough to overtake a newer event.

    Taken from the demo traveler's recent searches so the leakage test's subject is
    also a traveler the demo talks about.
    """
    searches = [
        (ing, e) for ing, e in out.deliveries
        if e.traveler_id == DEMO_TRAVELER and e.event_type == EventType.SEARCH
    ]
    original_ingested, late = searches[-3]
    out.deliveries.remove((original_ingested, late))
    delayed = late.model_copy(update={"ingested_at": late.occurred_at + LATE_ARRIVAL})
    out.deliveries.append((delayed.ingested_at, delayed))
    out.report["late_event"] = {
        "event_id": delayed.event_id,
        "traveler_id": delayed.traveler_id,
        "source_version": delayed.source_version,
        "occurred_at": iso(delayed.occurred_at),
        "ingested_at": iso(delayed.ingested_at),
        "destination": delayed.payload["destination"],
    }


def _inject_duplicates(out: Generated, rng: random.Random) -> None:
    """Re-deliver a handful of events verbatim, as a retrying producer would."""
    for pick in rng.sample(range(len(out.deliveries)), 5):
        _, original = out.deliveries[pick]
        replay = original.model_copy(
            update={"ingested_at": original.ingested_at + timedelta(minutes=rng.randint(3, 40))}
        )
        out.deliveries.append((replay.ingested_at, replay))


def _duplicate_event_ids(deliveries: list[tuple[datetime, Event]]) -> list[str]:
    """Event ids delivered more than once, counted from the final log."""
    seen: dict[str, int] = {}
    for _, event in deliveries:
        seen[event.event_id] = seen.get(event.event_id, 0) + 1
    return sorted(event_id for event_id, count in seen.items() if count > 1)


def _out_of_order_travelers(deliveries: list[tuple[datetime, Event]]) -> set[str]:
    """Travelers whose source_versions do not increase in delivery order."""
    highest: dict[str, int] = {}
    offenders: set[str] = set()
    for _, event in deliveries:
        seen = highest.get(event.traveler_id, 0)
        if event.source_version < seen:
            offenders.add(event.traveler_id)
        highest[event.traveler_id] = max(seen, event.source_version)
    return offenders


def write(out_dir: Path, generated: Generated) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "events.jsonl").write_text(
        "".join(event.as_json_line() + "\n" for _, event in generated.deliveries),
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / "travelers.json").write_text(
        json.dumps([t.model_dump() for t in generated.travelers], indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / "generation_report.json").write_text(
        json.dumps(generated.report, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic traveler event stream.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args()

    generated = generate(args.seed)
    write(args.out, generated)
    r = generated.report
    print(f"wrote {r['delivery_count']} deliveries ({r['distinct_event_count']} distinct events) "
          f"for {r['traveler_count']} travelers to {args.out}/events.jsonl")
    print(f"  duplicates:   {', '.join(r['duplicate_event_ids'])}")
    print(f"  late arrival: {r['late_event']['event_id']} occurred "
          f"{r['late_event']['occurred_at']}, ingested {r['late_event']['ingested_at']}")
    print(f"  out-of-order: {', '.join(r['out_of_order_travelers'])}")
    print(f"  opted out:    {r['opt_out_traveler']} (v{r['opt_out_source_version']}), "
          f"stale opt-in {r['stale_opt_in']['event_id']} "
          f"(v{r['stale_opt_in']['source_version']}) arrives after it")


if __name__ == "__main__":
    main()
