"""The single governed read path. Spec: specs/004-ai-safe-access-layer.md

Every read of traveler data goes through `serve`. That is the entire design: four
controls that must hold on every call, implemented once, in an order that cannot be
rearranged by a caller.

    consent  ->  build  ->  scrub  ->  attach provenance  ->  audit

Nothing else in the codebase reads a profile on behalf of a caller, and the MCP tools
in `mcp_server.py` are deliberately thin so there is nowhere else for a control to be
forgotten. If a future tool needs data, it gets a handler here and inherits all four.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import audit, consent, features, pii, profiles, provenance, store
from .events import utc
from .provenance import Provenance

UNKNOWN_TOOL = "UNKNOWN_TOOL"
MISSING_TRAVELER_ID = "MISSING_TRAVELER_ID"
NO_PROFILE_YET = "NO_PROFILE_YET"
PII_LEAK_BLOCKED = "PII_LEAK_BLOCKED"
INTERNAL_ERROR = "INTERNAL_ERROR"

DEFAULT_PURPOSE = "trip_planning"


class AccessRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    traveler_id: str | None = None
    purpose: str = DEFAULT_PURPOSE
    caller: str = "unknown"
    as_of: datetime | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class AccessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["OK", "DENIED"]
    request_id: str
    tool: str
    data: dict | None = None
    provenance: Provenance | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class Handler:
    build: Callable[[sqlite3.Connection, AccessRequest, datetime], tuple[BaseModel, Provenance]]
    requires_consent: bool = True
    traveler_scoped: bool = True


def serve(
    conn: sqlite3.Connection,
    request: AccessRequest,
    audit_path: Path | str = audit.DEFAULT_AUDIT_PATH,
) -> AccessResponse:
    """Serve one request, or deny it. Always audited, including on crash."""
    request_id = uuid.uuid4().hex[:12]
    started = perf_counter()
    response: AccessResponse | None = None
    decision = INTERNAL_ERROR

    def denied(reason_code: str, message: str | None) -> AccessResponse:
        return AccessResponse(
            status="DENIED",
            request_id=request_id,
            tool=request.tool,
            reason_code=reason_code,
            message=message,
        )

    try:
        handler = HANDLERS.get(request.tool)
        if handler is None:
            response = denied(UNKNOWN_TOOL, f"No tool named {request.tool!r}.")
        elif handler.traveler_scoped and not request.traveler_id:
            response = denied(MISSING_TRAVELER_ID, "This tool requires a traveler_id.")
        else:
            response = _serve_allowed(conn, request, request_id, handler, denied)
    except pii.PIILeakError as leak:
        # Fail the response rather than mask it: a masked leak ships quietly.
        response = denied(
            PII_LEAK_BLOCKED,
            "Response withheld because it contained personal data: "
            + "; ".join(leak.findings),
        )
    finally:
        decision = response.status if response else INTERNAL_ERROR
        audit.append(
            audit.AuditRecord(
                ts=audit.now(),
                request_id=request_id,
                caller=request.caller,
                tool=request.tool,
                traveler_id=request.traveler_id,
                purpose=request.purpose,
                decision=decision,
                reason_code=response.reason_code if response else INTERNAL_ERROR,
                fields_returned=sorted(response.data) if response and response.data else [],
                provenance=(
                    response.provenance.model_dump(mode="json")
                    if response and response.provenance
                    else None
                ),
                duration_ms=int((perf_counter() - started) * 1000),
            ),
            audit_path,
        )

    return response


def _serve_allowed(
    conn: sqlite3.Connection,
    request: AccessRequest,
    request_id: str,
    handler: Handler,
    denied: Callable[[str, str | None], AccessResponse],
) -> AccessResponse:
    if handler.requires_consent:
        # Deliberately evaluated at read time, never as of `request.as_of`:
        # rewinding the clock must not reinstate withdrawn permission.
        verdict = consent.decide(conn, request.traveler_id, request.purpose)
        if not verdict.allowed:
            return denied(verdict.reason_code, verdict.message)

    as_of = resolve_as_of(conn, request.as_of)
    try:
        model, prov = handler.build(conn, request, as_of)
    except LookupError as missing:
        return denied(NO_PROFILE_YET, str(missing))

    payload = pii.serialisable(model.model_dump(mode="json"))
    known = (
        store.traveler_pii_values(conn, request.traveler_id)
        if request.traveler_id
        else store.all_pii_values(conn)
    )
    pii.assert_no_pii(payload, known)  # raises; caught by serve()

    return AccessResponse(
        status="OK", request_id=request_id, tool=request.tool, data=payload, provenance=prov
    )


def resolve_as_of(conn: sqlite3.Connection, requested: datetime | None) -> datetime:
    """Default `as_of` is the platform's knowledge watermark, not wall-clock now.

    The dataset is synthetic and ends at a fixed point, so wall-clock now would put
    every query in an empty future. The newest `ingested_at` is also the more honest
    definition of "now" for a data platform: it is the latest moment we actually know
    anything about.
    """
    if requested is not None:
        return utc(requested)
    watermark = store.knowledge_watermark(conn)
    return utc(datetime.fromisoformat(watermark)) if watermark else utc(datetime.now())


# --- Handlers ---------------------------------------------------------------------


def _preferences(
    conn: sqlite3.Connection, request: AccessRequest, as_of: datetime
) -> tuple[BaseModel, Provenance]:
    """Latest-state preferences.

    Note the honest limitation: this is *not* point-in-time. A profile is a
    latest-state object, so it cannot be rewound (see README, Known limitations).
    `as_of` is used only to measure freshness. Point-in-time questions belong to
    `get_recent_searches`, which reads the event log.
    """
    profile = profiles.load_profile(conn, request.traveler_id)
    if profile is None:
        raise LookupError("No activity has been recorded for this traveler yet.")

    preferences = profiles.derive_preferences(profile)
    newest = (
        utc(datetime.fromisoformat(profile["last_occurred_at"]))
        if profile["last_occurred_at"]
        else None
    )
    prov = provenance.build(
        source="profile_store",
        version=profile["profile_version"],
        as_of=as_of,
        event_count=(
            profile["search_count"] + profile["booking_count"] + profile["cancellation_count"]
        ),
        newest_event_at=newest,
    )
    return preferences, prov


class RecentSearches(BaseModel):
    model_config = ConfigDict(frozen=True)

    traveler_id: str
    window_days: int
    searches: list[features.SearchSummary]
    features: features.FeatureSet


def _recent_searches(
    conn: sqlite3.Connection, request: AccessRequest, as_of: datetime
) -> tuple[BaseModel, Provenance]:
    window_days = int(request.params.get("window_days", features.SEARCH_WINDOW_DAYS))
    limit = int(request.params.get("limit", 10))

    visible = features.visible_events(conn, request.traveler_id, as_of)
    summaries = features.recent_searches(
        conn, request.traveler_id, as_of, window_days, limit, events=visible
    )
    feature_set = features.compute_features(conn, request.traveler_id, as_of, events=visible)
    newest = max((utc(event.occurred_at) for event in visible), default=None)

    data = RecentSearches(
        traveler_id=request.traveler_id,
        window_days=window_days,
        searches=summaries,
        features=feature_set,
    )
    prov = provenance.build(
        source="event_log",
        version=max((event.source_version for event in visible), default=0),
        as_of=as_of,
        event_count=len(visible),
        newest_event_at=newest,
    )
    return data, prov


class TravelerListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    travelers: list[consent.TravelerConsentStatus]
    note: str = (
        "Demo affordance. Ids and consent status only; a real deployment would not "
        "hand an agent an enumeration of its customers."
    )


def _list_travelers(
    conn: sqlite3.Connection, request: AccessRequest, as_of: datetime
) -> tuple[BaseModel, Provenance]:
    summary = consent.consent_summary(conn)
    prov = provenance.build(
        source="consent_state", version=None, as_of=as_of, event_count=len(summary)
    )
    return TravelerListing(travelers=summary), prov


HANDLERS: dict[str, Handler] = {
    "get_traveler_preferences": Handler(_preferences),
    "get_recent_searches": Handler(_recent_searches),
    "list_travelers": Handler(_list_travelers, requires_consent=False, traveler_scoped=False),
}
