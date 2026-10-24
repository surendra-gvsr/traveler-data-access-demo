"""Read-time consent decisions. Spec: specs/004-ai-safe-access-layer.md

Consent is stored by the profile builder (spec 002) and read here. Two properties
matter more than the code:

- **Fail closed.** Absence of a recorded "yes" is not a "yes".
- **Evaluated now, never as-of.** `as_of` rewinds facts, not permission. A traveler who
  revoked access yesterday is not re-consented by asking about last week.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from . import store

UNKNOWN_TRAVELER = "UNKNOWN_TRAVELER"
NO_CONSENT_RECORD = "NO_CONSENT_RECORD"
AI_ACCESS_REVOKED = "AI_ACCESS_REVOKED"
PURPOSE_NOT_PERMITTED = "PURPOSE_NOT_PERMITTED"

MESSAGES = {
    UNKNOWN_TRAVELER: "No such traveler.",
    NO_CONSENT_RECORD: (
        "No consent record exists for this traveler, so access is denied by default."
    ),
    AI_ACCESS_REVOKED: "This traveler has not consented to their data being used by AI agents.",
    PURPOSE_NOT_PERMITTED: (
        "This traveler consented to AI access, but not for the requested purpose."
    ),
}


class ConsentState(BaseModel):
    model_config = ConfigDict(frozen=True)

    traveler_id: str
    ai_access: bool
    purposes: list[str]
    source_version: int
    occurred_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConsentDecision:
    allowed: bool
    reason_code: str | None = None
    state: ConsentState | None = None

    @property
    def message(self) -> str | None:
        return MESSAGES.get(self.reason_code) if self.reason_code else None


def load_consent(conn: sqlite3.Connection, traveler_id: str) -> ConsentState | None:
    row = conn.execute(
        "SELECT * FROM consent_state WHERE traveler_id = ?", (traveler_id,)
    ).fetchone()
    if row is None:
        return None
    return ConsentState(
        traveler_id=row["traveler_id"],
        ai_access=bool(row["ai_access"]),
        purposes=json.loads(row["purposes"]),
        source_version=row["consent_source_version"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def decide(conn: sqlite3.Connection, traveler_id: str, purpose: str) -> ConsentDecision:
    if not store.traveler_exists(conn, traveler_id):
        return ConsentDecision(False, UNKNOWN_TRAVELER)

    state = load_consent(conn, traveler_id)
    if state is None:
        return ConsentDecision(False, NO_CONSENT_RECORD)
    if not state.ai_access:
        return ConsentDecision(False, AI_ACCESS_REVOKED, state)
    if purpose not in state.purposes:
        return ConsentDecision(False, PURPOSE_NOT_PERMITTED, state)
    return ConsentDecision(True, None, state)


class TravelerConsentStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    traveler_id: str
    has_consent_record: bool
    ai_access: bool | None
    purposes: list[str]


def consent_summary(conn: sqlite3.Connection) -> list[TravelerConsentStatus]:
    """Per-traveler consent status. Ids and flags only — no PII, no profile data."""
    rows = conn.execute(
        """SELECT t.traveler_id, c.ai_access, c.purposes
           FROM travelers t
           LEFT JOIN consent_state c ON c.traveler_id = t.traveler_id
           ORDER BY t.traveler_id"""
    ).fetchall()
    return [
        TravelerConsentStatus(
            traveler_id=row["traveler_id"],
            has_consent_record=row["ai_access"] is not None,
            ai_access=None if row["ai_access"] is None else bool(row["ai_access"]),
            purposes=json.loads(row["purposes"]) if row["purposes"] else [],
        )
        for row in rows
    ]
