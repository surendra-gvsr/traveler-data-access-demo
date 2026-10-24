"""Typed event envelope. Spec: specs/001-event-stream.md"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

PIPELINE_VERSION = "0.1.0"


class EventType(StrEnum):
    SEARCH = "search"
    BOOKING = "booking"
    CANCELLATION = "cancellation"
    CONSENT_CHANGE = "consent_change"


REQUIRED_PAYLOAD_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.SEARCH: frozenset(
        {"origin", "destination", "depart_date", "adults", "cabin"}
    ),
    EventType.BOOKING: frozenset(
        {"booking_ref", "destination", "depart_date", "nights", "amount_minor", "currency"}
    ),
    EventType.CANCELLATION: frozenset({"booking_ref"}),
    EventType.CONSENT_CHANGE: frozenset({"ai_access", "purposes"}),
}


def utc(dt: datetime) -> datetime:
    """Normalise to whole-second UTC.

    Timestamps are compared lexicographically as ISO strings in SQL, which is only
    sound if every stored timestamp has the same offset and the same width. Dropping
    microseconds keeps the width fixed.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat()


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: EventType
    traveler_id: str
    occurred_at: datetime
    ingested_at: datetime
    source_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Event:
        if utc(self.occurred_at) > utc(self.ingested_at):
            raise ValueError(
                f"{self.event_id}: occurred_at is after ingested_at — we cannot know "
                "about something before it happens"
            )
        missing = REQUIRED_PAYLOAD_FIELDS[self.event_type] - set(self.payload)
        if missing:
            raise ValueError(f"{self.event_id}: {self.event_type} missing {sorted(missing)}")
        return self

    def as_json_line(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "event_type": str(self.event_type),
                "traveler_id": self.traveler_id,
                "occurred_at": iso(self.occurred_at),
                "ingested_at": iso(self.ingested_at),
                "source_version": self.source_version,
                "payload": self.payload,
            },
            sort_keys=True,
        )


class Traveler(BaseModel):
    """Identity record. The only place full PII is authored.

    Consent is deliberately absent: it arrives as `consent_change` events so that it
    inherits the same ordering problem as every other field.
    """

    model_config = ConfigDict(frozen=True)

    traveler_id: str
    full_name: str
    email: str
    phone: str
    home_market: str


def read_events(path: Path) -> Iterator[Event]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield Event.model_validate_json(line)


def read_travelers(path: Path) -> list[Traveler]:
    return [Traveler.model_validate(t) for t in json.loads(path.read_text(encoding="utf-8"))]
