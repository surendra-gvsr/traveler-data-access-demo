"""PII detection at the boundary. Spec: specs/004-ai-safe-access-layer.md

This module is the *detection* half of the defence. Prevention is the typed output
models, which simply do not name PII fields. Detection exists because prevention is
only correct for as long as every future contributor understands it, and a scrubber
turns a careless field addition into a loud failure instead of a silent leak.

Three rules, cheapest and most specific first:

1. **Field names.** A key called `email` is a leak regardless of its value.
2. **Known values.** We know exactly what this traveler's email and phone are, so we
   can assert those exact strings are absent. This is the strongest rule available and
   it catches PII that has been renamed, nested, or interpolated into prose.
3. **Patterns.** Generic email and phone shapes, which catch PII belonging to someone
   we did not think to look up.

Findings never contain the offending value — an error message or a log line carrying
the leaked email would simply move the leak somewhere else.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

PII_FIELD_NAMES = frozenset(
    {
        "email",
        "contact_email",
        "traveler_email",
        "e_mail",
        "full_name",
        "name",
        "first_name",
        "last_name",
        "phone",
        "phone_number",
        "contact_phone",
        "mobile",
    }
)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Deliberately narrow: 3-3-4 grouping. Dates (2026-10-24) and amounts do not match.
PHONE_PATTERN = re.compile(r"\+?\d{1,3}[-.\s]\d{3}[-.\s]\d{3}[-.\s]\d{4}")


class PIILeakError(RuntimeError):
    """Raised when a response is about to carry PII across the boundary."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__(f"PII detected in outbound payload: {', '.join(findings)}")
        self.findings = findings


def find_pii(payload: Any, known_values: Iterable[str] = ()) -> list[str]:
    """Return descriptions of every PII finding. Values are never included."""
    needles = [value.lower() for value in known_values if value]
    findings: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if str(key).lower() in PII_FIELD_NAMES:
                    findings.append(f"pii field name at {here}")
                walk(value, here)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            lowered = node.lower()
            if any(needle in lowered for needle in needles):
                findings.append(f"known traveler PII value at {path or 'root'}")
            if EMAIL_PATTERN.search(node):
                findings.append(f"email pattern at {path or 'root'}")
            if PHONE_PATTERN.search(node):
                findings.append(f"phone pattern at {path or 'root'}")

    walk(payload, "")
    return findings


def assert_no_pii(payload: Any, known_values: Iterable[str] = ()) -> None:
    """Fail the response rather than masking it.

    Masking would let a leak ship quietly in a degraded form; failing makes it a bug
    someone has to look at.
    """
    findings = find_pii(payload, known_values)
    if findings:
        raise PIILeakError(findings)


def serialisable(payload: Any) -> Any:
    """Round-trip through JSON so the scrubber sees exactly what the caller will."""
    return json.loads(json.dumps(payload, default=str))
