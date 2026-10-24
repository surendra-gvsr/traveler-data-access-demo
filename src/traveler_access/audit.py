"""Append-only audit log. Spec: specs/004-ai-safe-access-layer.md

One record per call, for allows **and** denials — a denial record is the evidence that
the control worked, and an audit log that only records successes cannot prove anything
about the failures.

`fields_returned` holds field *names*, never values. An audit log that records payloads
becomes a second, less guarded copy of the data it exists to police, which is the most
common way a privacy control turns into a privacy liability.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from .events import iso
from .pii import PII_FIELD_NAMES

DEFAULT_AUDIT_PATH = Path("data/audit.jsonl")


class AuditRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    request_id: str
    caller: str
    tool: str
    traveler_id: str | None
    purpose: str | None
    decision: str
    reason_code: str | None = None
    fields_returned: list[str] = []
    provenance: dict | None = None
    duration_ms: int = 0

    @field_validator("fields_returned")
    @classmethod
    def _no_pii_field_names(cls, value: list[str]) -> list[str]:
        leaked = sorted(set(value) & PII_FIELD_NAMES)
        if leaked:
            raise ValueError(
                f"audit record names PII fields as returned: {leaked} — "
                "the access layer should never have served them"
            )
        return value

    def as_json_line(self) -> str:
        record = self.model_dump()
        record["ts"] = iso(self.ts)
        return json.dumps(record, sort_keys=True, default=str)


def append(record: AuditRecord, path: Path | str = DEFAULT_AUDIT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(record.as_json_line() + "\n")


def read_all(path: Path | str = DEFAULT_AUDIT_PATH) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tail(count: int = 5, path: Path | str = DEFAULT_AUDIT_PATH) -> list[dict]:
    return read_all(path)[-count:]


def now() -> datetime:
    return datetime.now(timezone.utc)
