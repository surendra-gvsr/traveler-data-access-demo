"""Incremental, version-aware, idempotent profile builder. Spec: 002

The whole module exists to answer one question correctly: when an event arrives that we
may have seen before, and that may be older than events we have already applied, which
parts of the profile should move?

The answer depends on the field, so fields are grouped by how they behave under replay
and reordering:

- **Counters** commute, so they can be applied in any order — but only once per event.
  The `processed_events` ledger provides the "only once".
- **Last-write-wins** fields do not commute, so they are guarded by a version
  comparison and skipped when the event is older than what we already have.
- **Consent** is last-write-wins, versioned separately, and the one place where being
  wrong is a privacy incident rather than a bad metric.

A late event is therefore not a no-op and not a full apply: its counters land and its
overwrites do not. That partial outcome is reported as `APPLIED_STALE`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import store
from .events import Event, EventType, iso, utc

Status = Literal["APPLIED", "DUPLICATE", "APPLIED_STALE"]

# Fields that may only be overwritten by a higher source_version, per event type.
LWW_FIELDS: dict[EventType, dict[str, str]] = {
    EventType.SEARCH: {
        "last_search_destination": "destination",
        "last_search_depart_date": "depart_date",
        "home_origin": "origin",
        "party_adults": "adults",
        "party_children": "children",
        "contact_email": "contact_email",
    },
    EventType.BOOKING: {
        "home_origin": "origin",
        "party_adults": "adults",
        "party_children": "children",
        "preferred_currency": "currency",
        "contact_email": "contact_email",
    },
    EventType.CANCELLATION: {},
}


@dataclass(frozen=True)
class ApplyResult:
    status: Status
    event_id: str
    traveler_id: str
    profile_version: int

    @property
    def mutated(self) -> bool:
        return self.status != "DUPLICATE"


@dataclass
class BuildSummary:
    applied: int = 0
    duplicates: int = 0
    stale: int = 0
    last_delivery_seq: int = 0

    @property
    def total(self) -> int:
        return self.applied + self.duplicates + self.stale


def apply_event(conn: sqlite3.Connection, event: Event, now: datetime | None = None) -> ApplyResult:
    """Apply one event. Atomic: either all of it lands or none of it does."""
    stamp = iso(now or datetime.now(timezone.utc))

    with conn:  # one transaction per event; an exception rolls the whole thing back
        # The insert *is* the duplicate check. Checking first and then writing would
        # leave a window for a concurrent second delivery to slip between the two.
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, traveler_id, applied_at) "
            "VALUES (?, ?, ?)",
            (event.event_id, event.traveler_id, stamp),
        )
        if cur.rowcount == 0:
            return ApplyResult("DUPLICATE", event.event_id, event.traveler_id,
                               _profile_version(conn, event.traveler_id))

        if event.event_type is EventType.CONSENT_CHANGE:
            return _apply_consent(conn, event, stamp)
        return _apply_activity(conn, event, stamp)


def _profile_version(conn: sqlite3.Connection, traveler_id: str) -> int:
    row = conn.execute(
        "SELECT profile_version FROM profiles WHERE traveler_id = ?", (traveler_id,)
    ).fetchone()
    return row["profile_version"] if row else 0


def _apply_consent(conn: sqlite3.Connection, event: Event, stamp: str) -> ApplyResult:
    current = conn.execute(
        "SELECT consent_source_version FROM consent_state WHERE traveler_id = ?",
        (event.traveler_id,),
    ).fetchone()

    if current and event.source_version <= current["consent_source_version"]:
        # An older consent decision arriving late. Honouring it would silently
        # reinstate access that a newer event revoked.
        return ApplyResult("APPLIED_STALE", event.event_id, event.traveler_id,
                           _profile_version(conn, event.traveler_id))

    conn.execute(
        """INSERT INTO consent_state
               (traveler_id, ai_access, purposes, consent_source_version,
                occurred_at, ingested_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(traveler_id) DO UPDATE SET
               ai_access              = excluded.ai_access,
               purposes               = excluded.purposes,
               consent_source_version = excluded.consent_source_version,
               occurred_at            = excluded.occurred_at,
               ingested_at            = excluded.ingested_at,
               updated_at             = excluded.updated_at""",
        (
            event.traveler_id,
            1 if event.payload["ai_access"] else 0,
            json.dumps(sorted(event.payload["purposes"])),
            event.source_version,
            iso(event.occurred_at),
            iso(event.ingested_at),
            stamp,
        ),
    )
    return ApplyResult("APPLIED", event.event_id, event.traveler_id,
                       _profile_version(conn, event.traveler_id))


def _apply_activity(conn: sqlite3.Connection, event: Event, stamp: str) -> ApplyResult:
    conn.execute(
        "INSERT OR IGNORE INTO profiles (traveler_id) VALUES (?)", (event.traveler_id,)
    )
    profile = conn.execute(
        "SELECT * FROM profiles WHERE traveler_id = ?", (event.traveler_id,)
    ).fetchone()

    updates: dict[str, Any] = _counter_updates(event, profile)

    # The stale guard is per field, not per profile. A profile-wide watermark would let
    # a newer search block an older booking from ever setting `preferred_currency`, a
    # field no search writes — the overwrite would be "stale" against an unrelated one.
    versions: dict[str, int] = json.loads(profile["field_versions"])
    skipped = False
    written = False
    for column, payload_key in LWW_FIELDS[event.event_type].items():
        if payload_key not in event.payload:
            continue
        if event.source_version <= versions.get(column, 0):
            skipped = True  # a newer write already owns this field; leave it alone
            continue
        updates[column] = event.payload[payload_key]
        versions[column] = event.source_version
        written = True

    if written:
        updates["field_versions"] = json.dumps(versions, sort_keys=True)

    updates["profile_version"] = profile["profile_version"] + 1
    updates["last_source_version"] = max(profile["last_source_version"], event.source_version)
    updates["last_occurred_at"] = max(profile["last_occurred_at"] or "", iso(event.occurred_at))
    updates["updated_at"] = stamp

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE profiles SET {assignments} WHERE traveler_id = ?",
        (*updates.values(), event.traveler_id),
    )
    return ApplyResult(
        "APPLIED_STALE" if skipped else "APPLIED",
        event.event_id,
        event.traveler_id,
        updates["profile_version"],
    )


def _counter_updates(event: Event, profile: sqlite3.Row) -> dict[str, Any]:
    """Counter deltas. Commutative, so delivery order does not matter here."""
    payload = event.payload
    updates: dict[str, Any] = {}

    def bump_map(column: str, key: str | None) -> None:
        if not key:
            return
        counts = json.loads(profile[column])
        counts[key] = counts.get(key, 0) + 1
        updates[column] = json.dumps(counts, sort_keys=True)

    if event.event_type is EventType.SEARCH:
        updates["search_count"] = profile["search_count"] + 1
        bump_map("destination_counts", payload.get("destination"))
        bump_map("cabin_counts", payload.get("cabin"))

    elif event.event_type is EventType.BOOKING:
        updates["booking_count"] = profile["booking_count"] + 1
        bump_map("destination_counts", payload.get("destination"))
        bump_map("cabin_counts", payload.get("cabin"))
        updates["nights_total"] = profile["nights_total"] + int(payload["nights"])
        updates["nights_samples"] = profile["nights_samples"] + 1
        updates["booking_value_minor_total"] = (
            profile["booking_value_minor_total"] + int(payload["amount_minor"])
        )
        lead_days = (date.fromisoformat(payload["depart_date"]) - utc(event.occurred_at).date()).days
        updates["lead_time_days_total"] = profile["lead_time_days_total"] + max(lead_days, 0)
        updates["lead_time_samples"] = profile["lead_time_samples"] + 1

    elif event.event_type is EventType.CANCELLATION:
        updates["cancellation_count"] = profile["cancellation_count"] + 1

    return updates


def build_profiles(
    conn: sqlite3.Connection, now: datetime | None = None, limit: int | None = None
) -> BuildSummary:
    """Consume deliveries after the checkpoint. Re-running processes only new ones."""
    summary = BuildSummary(last_delivery_seq=store.checkpoint(conn))
    deliveries = store.unprocessed_deliveries(conn)
    if limit is not None:
        deliveries = deliveries[:limit]

    for delivery_seq, event in deliveries:
        result = apply_event(conn, event, now)
        if result.status == "APPLIED":
            summary.applied += 1
        elif result.status == "DUPLICATE":
            summary.duplicates += 1
        else:
            summary.stale += 1
        summary.last_delivery_seq = delivery_seq

    if deliveries:
        store.save_checkpoint(
            conn, summary.last_delivery_seq, len(deliveries), now or datetime.now(timezone.utc)
        )
    return summary


# --- The derived view served to callers -------------------------------------------


class TopDestination(BaseModel):
    model_config = ConfigDict(frozen=True)

    destination: str
    interactions: int


class TravelerPreferences(BaseModel):
    """What the access layer is allowed to say about a traveler.

    Deliberately contains no PII field. `contact_email` is stored on the profile row
    right next to these values and is simply never named here — see spec 004 on why
    the allowlist is the prevention and the scrubber is only the detection.
    """

    model_config = ConfigDict(frozen=True)

    traveler_id: str
    top_destinations: list[TopDestination]
    preferred_cabin: str | None
    party_adults: int | None
    party_children: int | None
    avg_trip_length_nights: float | None
    avg_booking_lead_time_days: float | None
    avg_nightly_spend_minor: int | None
    preferred_currency: str | None
    home_origin: str | None
    last_search_destination: str | None
    last_search_depart_date: str | None
    search_count: int
    booking_count: int
    cancellation_count: int
    activity_level: str


def _modal(counts: dict[str, int]) -> str | None:
    return max(counts, key=lambda key: (counts[key], key)) if counts else None


def _ratio(total: int, samples: int, places: int = 1) -> float | None:
    return round(total / samples, places) if samples else None


def derive_preferences(profile: sqlite3.Row) -> TravelerPreferences:
    """Pure function over a stored profile. No I/O, no PII."""
    destination_counts: dict[str, int] = json.loads(profile["destination_counts"])
    top = sorted(destination_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    searches = profile["search_count"]

    return TravelerPreferences(
        traveler_id=profile["traveler_id"],
        top_destinations=[TopDestination(destination=d, interactions=n) for d, n in top],
        preferred_cabin=_modal(json.loads(profile["cabin_counts"])),
        party_adults=profile["party_adults"],
        party_children=profile["party_children"],
        avg_trip_length_nights=_ratio(profile["nights_total"], profile["nights_samples"]),
        avg_booking_lead_time_days=_ratio(
            profile["lead_time_days_total"], profile["lead_time_samples"], 0
        ),
        avg_nightly_spend_minor=(
            int(profile["booking_value_minor_total"] / profile["nights_total"])
            if profile["nights_total"]
            else None
        ),
        preferred_currency=profile["preferred_currency"],
        home_origin=profile["home_origin"],
        last_search_destination=profile["last_search_destination"],
        last_search_depart_date=profile["last_search_depart_date"],
        search_count=searches,
        booking_count=profile["booking_count"],
        cancellation_count=profile["cancellation_count"],
        activity_level="low" if searches < 6 else "moderate" if searches < 14 else "high",
    )


def load_profile(conn: sqlite3.Connection, traveler_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM profiles WHERE traveler_id = ?", (traveler_id,)
    ).fetchone()
