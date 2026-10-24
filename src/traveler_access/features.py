"""Point-in-time features. Spec: specs/003-point-in-time-features.md

Features are computed from the event log rather than from the profile, because the
profile is a "latest state" object with no memory of what it looked like last Tuesday.
An as-of question cannot be answered from it.

Everything here funnels through `visible_events`, which is the one place the two-clock
filter lives. If that function is right, no feature can leak; if a feature bypassed it,
no amount of care elsewhere would help.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from .events import EventType, iso, utc

SEARCH_WINDOW_DAYS = 30
BOOKING_WINDOW_DAYS = 90


class VisibleEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: EventType
    occurred_at: datetime
    source_version: int
    payload: dict


class SearchSummary(BaseModel):
    """A search, as a caller is allowed to see it.

    The raw payload carries `contact_email`. This model does not name it, so it cannot
    travel any further. Allowlisting by construction, per spec 004.
    """

    model_config = ConfigDict(frozen=True)

    occurred_at: datetime
    origin: str
    destination: str
    depart_date: str
    return_date: str | None
    adults: int
    children: int
    cabin: str


class FeatureSet(BaseModel):
    """Feature values plus the vantage point they were computed from.

    `as_of` travels with the numbers deliberately: a point-in-time feature passed around
    without its vantage point is indistinguishable from a wrong one.
    """

    model_config = ConfigDict(frozen=True)

    traveler_id: str
    as_of: datetime
    computed_at: datetime
    visible_event_count: int
    searches_last_30d: int
    distinct_destinations_last_30d: int
    bookings_last_90d: int
    cancellations_last_90d: int
    days_since_last_search: float | None
    has_upcoming_trip: bool


def visible_events(
    conn: sqlite3.Connection, traveler_id: str, as_of: datetime
) -> list[VisibleEvent]:
    """Every distinct event the platform knew about, for this traveler, at `as_of`.

    Three conditions, and dropping any one of them produces a plausible-looking lie:

    - `occurred_at <= as_of`  — no events from the future.
    - `ingested_at <= as_of`  — nothing we had not yet received. An event that happened
      before `as_of` but arrived after it was not available to any decision made at
      `as_of`, so counting it invents knowledge we did not have.
    - `GROUP BY event_id`     — the log retains duplicate deliveries (spec 001), which
      would otherwise double count.
    """
    stamp = iso(as_of)
    rows = conn.execute(
        """SELECT event_id,
                  event_type,
                  MIN(occurred_at) AS occurred_at,
                  MAX(source_version) AS source_version,
                  payload
           FROM events
           WHERE traveler_id = ?
             AND occurred_at <= ?
             AND ingested_at <= ?
           GROUP BY event_id
           ORDER BY occurred_at, event_id""",
        (traveler_id, stamp, stamp),
    ).fetchall()

    return [
        VisibleEvent(
            event_id=row["event_id"],
            event_type=row["event_type"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            source_version=row["source_version"],
            payload=json.loads(row["payload"]),
        )
        for row in rows
    ]


def _within(events: Iterable[VisibleEvent], event_type: EventType,
            start: datetime, as_of: datetime) -> list[VisibleEvent]:
    """Half-open window `(start, as_of]` — see acceptance criterion C4."""
    return [
        event for event in events
        if event.event_type is event_type and start < utc(event.occurred_at) <= utc(as_of)
    ]


def compute_features(
    conn: sqlite3.Connection,
    traveler_id: str,
    as_of: datetime,
    events: list[VisibleEvent] | None = None,
) -> FeatureSet:
    as_of = utc(as_of)
    events = visible_events(conn, traveler_id, as_of) if events is None else events

    searches = _within(events, EventType.SEARCH, as_of - timedelta(days=SEARCH_WINDOW_DAYS), as_of)
    booking_start = as_of - timedelta(days=BOOKING_WINDOW_DAYS)
    bookings = _within(events, EventType.BOOKING, booking_start, as_of)
    cancellations = _within(events, EventType.CANCELLATION, booking_start, as_of)

    all_searches = [e for e in events if e.event_type is EventType.SEARCH]
    last_search = max((utc(e.occurred_at) for e in all_searches), default=None)

    return FeatureSet(
        traveler_id=traveler_id,
        as_of=as_of,
        computed_at=utc(datetime.now()),
        visible_event_count=len(events),
        searches_last_30d=len(searches),
        distinct_destinations_last_30d=len({e.payload["destination"] for e in searches}),
        bookings_last_90d=len(bookings),
        cancellations_last_90d=len(cancellations),
        days_since_last_search=(
            round((as_of - last_search).total_seconds() / 86400, 1) if last_search else None
        ),
        has_upcoming_trip=_has_upcoming_trip(events, as_of),
    )


def _has_upcoming_trip(events: list[VisibleEvent], as_of: datetime) -> bool:
    """A live booking departing after `as_of`.

    Cancellations only count if they were *also* visible at `as_of` — the same honesty
    rule applied to the thing that negates a booking, not just to the booking.
    """
    cancelled = {
        e.payload["booking_ref"] for e in events if e.event_type is EventType.CANCELLATION
    }
    return any(
        e.event_type is EventType.BOOKING
        and e.payload["booking_ref"] not in cancelled
        and e.payload["depart_date"] > as_of.date().isoformat()
        for e in events
    )


def recent_searches(
    conn: sqlite3.Connection,
    traveler_id: str,
    as_of: datetime,
    window_days: int = SEARCH_WINDOW_DAYS,
    limit: int = 10,
    events: list[VisibleEvent] | None = None,
) -> list[SearchSummary]:
    """Newest-first search summaries inside the window, as of `as_of`."""
    as_of = utc(as_of)
    events = visible_events(conn, traveler_id, as_of) if events is None else events
    window = _within(events, EventType.SEARCH, as_of - timedelta(days=window_days), as_of)

    return [
        SearchSummary(
            occurred_at=event.occurred_at,
            origin=event.payload["origin"],
            destination=event.payload["destination"],
            depart_date=event.payload["depart_date"],
            return_date=event.payload.get("return_date"),
            adults=event.payload["adults"],
            children=event.payload["children"],
            cabin=event.payload["cabin"],
        )
        for event in sorted(window, key=lambda e: e.occurred_at, reverse=True)[:limit]
    ]
