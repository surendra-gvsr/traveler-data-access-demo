"""Provenance attached to every served result. Spec: 004

An agent that receives a bare number cannot tell a fresh profile from one built before
the traveler changed their mind. Provenance makes that difference visible, which is the
difference between an agent that can reason about staleness and one that cannot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .events import PIPELINE_VERSION, utc


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    version: int | None  # None where the source is not versioned (e.g. a listing)
    computed_at: datetime
    as_of: datetime
    event_count: int
    pipeline_version: str = PIPELINE_VERSION
    freshness_seconds: int | None = None


def build(
    source: str,
    version: int | None,
    as_of: datetime,
    event_count: int,
    newest_event_at: datetime | None = None,
) -> Provenance:
    """`freshness_seconds` is the age of the newest input event at `as_of`.

    This is the number an agent actually needs: "how out of date is the evidence behind
    this answer", not "when did the pipeline last run".
    """
    as_of = utc(as_of)
    freshness = (
        int((as_of - utc(newest_event_at)).total_seconds()) if newest_event_at else None
    )
    return Provenance(
        source=source,
        version=version,
        computed_at=utc(datetime.now()),
        as_of=as_of,
        event_count=event_count,
        freshness_seconds=freshness,
    )
