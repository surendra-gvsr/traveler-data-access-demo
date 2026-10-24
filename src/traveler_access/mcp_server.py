"""MCP server exposing the governed read path. Spec: specs/005-mcp-tool-contract.md

These tools are the only surface a model sees, so their output schemas are the privacy
policy as far as the model is concerned: a field that is not in a schema cannot be
requested, inferred, or prompted out. That is why the result models below name every
field explicitly instead of returning a loose dict — a `dict` output would type-check
fine and tell a reviewer nothing.

Every tool is a thin wrapper: build an `AccessRequest`, call `access.serve`, shape the
reply. No tool reads SQLite directly, which is what makes the four controls in spec 004
impossible to skip (verified by `tests/test_mcp_tools.py::test_server_has_no_direct_store_access`).

Run: python -m traveler_access.mcp_server     (stdio transport)
Env: TRAVELER_DB  — path to the profile store, default data/profiles.db
     TRAVELER_AUDIT — path to the audit log, default data/audit.jsonl
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from . import access, audit, store
from .consent import TravelerConsentStatus
from .features import FeatureSet, SearchSummary
from .profiles import TravelerPreferences
from .provenance import Provenance

DENIAL_GUIDANCE = (
    "A DENIED result is a normal, expected outcome, not an error to work around: it "
    "means the traveler has not consented to this use of their data. Report it plainly "
    "and move on. Do not retry, do not try another tool to get the same data, and do "
    "not guess at the answer from other sources."
)

mcp = FastMCP("traveler-data-access")

_conn: sqlite3.Connection | None = None


def connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = store.connect(Path(os.environ.get("TRAVELER_DB", "data/profiles.db")))
    return _conn


def audit_path() -> Path:
    return Path(os.environ.get("TRAVELER_AUDIT", str(audit.DEFAULT_AUDIT_PATH)))


class Result(BaseModel):
    """Shared envelope. One shape for success and refusal, so the model learns one pattern."""

    model_config = ConfigDict(frozen=True)

    status: Literal["OK", "DENIED"]
    request_id: str
    reason_code: str | None = None
    message: str | None = None
    provenance: Provenance | None = None


class PreferencesResult(Result):
    preferences: TravelerPreferences | None = None


class RecentSearchesResult(Result):
    window_days: int | None = None
    searches: list[SearchSummary] | None = None
    features: FeatureSet | None = None


class TravelerListingResult(Result):
    travelers: list[TravelerConsentStatus] | None = None
    note: str | None = None


def _call(request: access.AccessRequest) -> access.AccessResponse:
    return access.serve(connection(), request, audit_path())


@mcp.tool(
    description=(
        "Travel preferences derived from a traveler's own search and booking history: "
        "favourite destinations, usual cabin, party size, typical trip length and "
        "booking lead time. Returns no personal identifiers — name, email and phone "
        "are never served by this platform. Reflects the latest known state of the "
        "profile rather than a point in time. " + DENIAL_GUIDANCE
    )
)
def get_traveler_preferences(
    traveler_id: str = Field(description="Pseudonymous traveler id, e.g. trv_003."),
    purpose: str = Field(
        default=access.DEFAULT_PURPOSE,
        description=(
            "Why the data is being used. Must be a purpose the traveler consented to; "
            "'trip_planning' for itinerary and recommendation work."
        ),
    ),
) -> PreferencesResult:
    response = _call(
        access.AccessRequest(
            tool="get_traveler_preferences",
            traveler_id=traveler_id,
            purpose=purpose,
            caller="mcp",
        )
    )
    # Re-validated from the serialised payload the scrubber inspected, so what the
    # model receives is exactly what was checked.
    return PreferencesResult(
        status=response.status,
        request_id=response.request_id,
        reason_code=response.reason_code,
        message=response.message,
        provenance=response.provenance,
        preferences=TravelerPreferences.model_validate(response.data) if response.data else None,
    )


@mcp.tool(
    description=(
        "A traveler's recent searches and point-in-time activity features, as known at "
        "`as_of`. Search rows carry route, dates, party size and cabin only; the "
        "contact email present in the underlying events is stripped. Features are "
        "computed from events that had both occurred and been ingested by `as_of`, so "
        "they never include information the platform did not have at that moment. "
        + DENIAL_GUIDANCE
    )
)
def get_recent_searches(
    traveler_id: str = Field(description="Pseudonymous traveler id, e.g. trv_003."),
    window_days: int = Field(default=30, ge=1, le=365, description="Look-back window in days."),
    as_of: str | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp to compute as of. Defaults to the platform's latest "
            "knowledge watermark. Note this rewinds data only — it never reinstates "
            "consent that has since been withdrawn."
        ),
    ),
    limit: int = Field(default=10, ge=1, le=50, description="Maximum search rows to return."),
    purpose: str = Field(default=access.DEFAULT_PURPOSE, description="Consented purpose of use."),
) -> RecentSearchesResult:
    response = _call(
        access.AccessRequest(
            tool="get_recent_searches",
            traveler_id=traveler_id,
            purpose=purpose,
            caller="mcp",
            as_of=datetime.fromisoformat(as_of) if as_of else None,
            params={"window_days": window_days, "limit": limit},
        )
    )
    data = response.data or {}
    return RecentSearchesResult(
        status=response.status,
        request_id=response.request_id,
        reason_code=response.reason_code,
        message=response.message,
        provenance=response.provenance,
        window_days=data.get("window_days"),
        searches=(
            [SearchSummary.model_validate(row) for row in data["searches"]]
            if response.data
            else None
        ),
        features=FeatureSet.model_validate(data["features"]) if response.data else None,
    )


@mcp.tool(
    description=(
        "List traveler ids with their AI-access consent status. Use this to discover "
        "which travelers can be analysed before requesting their data. Returns no "
        "personal data and no profile data. Demo affordance: a production deployment "
        "would not expose an enumeration of customers to an agent."
    )
)
def list_travelers() -> TravelerListingResult:
    response = _call(access.AccessRequest(tool="list_travelers", caller="mcp"))
    data = response.data or {}
    return TravelerListingResult(
        status=response.status,
        request_id=response.request_id,
        reason_code=response.reason_code,
        message=response.message,
        provenance=response.provenance,
        travelers=(
            [TravelerConsentStatus.model_validate(row) for row in data["travelers"]]
            if response.data
            else None
        ),
        note=data.get("note"),
    )


if __name__ == "__main__":
    mcp.run()
