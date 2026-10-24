"""SQLite schema and ingestion. Specs: 001 (log), 002 (profiles, consent, checkpoint)

Two design points worth knowing before reading the DDL:

1. `events` keeps every *delivery*, not every event. Its primary key is a surrogate
   `delivery_seq`, so a duplicate delivery produces a second row with the same
   `event_id`. Deduplicating on insert would have been one line, and would have hidden
   the duplicate-handling problem that specs 002 and 003 exist to solve — downstream
   code has to cope with duplicates because real logs contain them.

2. Timestamps are stored as fixed-width ISO-8601 UTC strings so that SQL comparison is
   both correct and index-friendly. See `events.iso`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .events import Event, Traveler, iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS travelers (
    traveler_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL,
    home_market TEXT NOT NULL
);

-- One row per delivery. Duplicates are expected and retained.
CREATE TABLE IF NOT EXISTS events (
    delivery_seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL,
    traveler_id    TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_traveler_time
    ON events (traveler_id, occurred_at, ingested_at);

-- The idempotency ledger. The INSERT into this table *is* the duplicate check.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id    TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    traveler_id              TEXT PRIMARY KEY,
    profile_version          INTEGER NOT NULL DEFAULT 0,
    last_source_version      INTEGER NOT NULL DEFAULT 0,
    last_occurred_at         TEXT,
    updated_at               TEXT,
    -- counters: commutative, applied exactly once per event_id
    search_count             INTEGER NOT NULL DEFAULT 0,
    booking_count            INTEGER NOT NULL DEFAULT 0,
    cancellation_count       INTEGER NOT NULL DEFAULT 0,
    destination_counts       TEXT NOT NULL DEFAULT '{}',
    cabin_counts             TEXT NOT NULL DEFAULT '{}',
    nights_total             INTEGER NOT NULL DEFAULT 0,
    nights_samples           INTEGER NOT NULL DEFAULT 0,
    lead_time_days_total     INTEGER NOT NULL DEFAULT 0,
    lead_time_samples        INTEGER NOT NULL DEFAULT 0,
    booking_value_minor_total INTEGER NOT NULL DEFAULT 0,
    -- last-write-wins: each field keeps its own watermark in field_versions, because a
    -- newer search must not suppress an older booking's currency (spec 002, B5)
    field_versions           TEXT NOT NULL DEFAULT '{}',
    last_search_destination  TEXT,
    last_search_depart_date  TEXT,
    home_origin              TEXT,
    party_adults             INTEGER,
    party_children           INTEGER,
    preferred_currency       TEXT,
    contact_email            TEXT   -- PII, stored here, never served (spec 004)
);

-- Separate from profiles so that "we have never heard of this traveler's consent" is
-- representable, which is what fail-closed denial needs.
CREATE TABLE IF NOT EXISTS consent_state (
    traveler_id            TEXT PRIMARY KEY,
    ai_access              INTEGER NOT NULL,
    purposes               TEXT NOT NULL,
    consent_source_version INTEGER NOT NULL,
    occurred_at            TEXT NOT NULL,
    ingested_at            TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

-- Incremental consumption: the builder resumes from the last delivery it applied.
CREATE TABLE IF NOT EXISTS stream_checkpoint (
    stream_name       TEXT PRIMARY KEY,
    last_delivery_seq INTEGER NOT NULL DEFAULT 0,
    deliveries_seen   INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS audit_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STREAM_NAME = "traveler_events"


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level="")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def load_travelers(conn: sqlite3.Connection, travelers: list[Traveler]) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO travelers VALUES (?, ?, ?, ?, ?)",
            [
                (t.traveler_id, t.full_name, t.email, t.phone, t.home_market)
                for t in travelers
            ],
        )


def append_deliveries(conn: sqlite3.Connection, events: list[Event]) -> int:
    """Append raw deliveries to the log, duplicates included."""
    with conn:
        conn.executemany(
            """INSERT INTO events
               (event_id, traveler_id, event_type, occurred_at, ingested_at,
                source_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    e.event_id,
                    e.traveler_id,
                    str(e.event_type),
                    iso(e.occurred_at),
                    iso(e.ingested_at),
                    e.source_version,
                    json.dumps(e.payload, sort_keys=True),
                )
                for e in events
            ],
        )
    return len(events)


def row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        event_type=row["event_type"],
        traveler_id=row["traveler_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        source_version=row["source_version"],
        payload=json.loads(row["payload"]),
    )


def unprocessed_deliveries(conn: sqlite3.Connection) -> list[tuple[int, Event]]:
    """Deliveries after the checkpoint, in delivery order."""
    cur = conn.execute(
        """SELECT * FROM events
           WHERE delivery_seq > COALESCE(
               (SELECT last_delivery_seq FROM stream_checkpoint WHERE stream_name = ?), 0)
           ORDER BY delivery_seq""",
        (STREAM_NAME,),
    )
    return [(row["delivery_seq"], row_to_event(row)) for row in cur.fetchall()]


def checkpoint(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_delivery_seq FROM stream_checkpoint WHERE stream_name = ?",
        (STREAM_NAME,),
    ).fetchone()
    return row["last_delivery_seq"] if row else 0


def save_checkpoint(conn: sqlite3.Connection, delivery_seq: int, seen: int, now: datetime) -> None:
    with conn:
        conn.execute(
            """INSERT INTO stream_checkpoint
                   (stream_name, last_delivery_seq, deliveries_seen, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stream_name) DO UPDATE SET
                   last_delivery_seq = excluded.last_delivery_seq,
                   deliveries_seen   = stream_checkpoint.deliveries_seen
                                       + excluded.deliveries_seen,
                   updated_at        = excluded.updated_at""",
            (STREAM_NAME, delivery_seq, seen, iso(now)),
        )


def knowledge_watermark(conn: sqlite3.Connection) -> str | None:
    """The newest `ingested_at` in the log: the platform's "now" for as-of defaults."""
    row = conn.execute("SELECT MAX(ingested_at) AS w FROM events").fetchone()
    return row["w"] if row else None


def traveler_pii_values(conn: sqlite3.Connection, traveler_id: str) -> list[str]:
    """Exact PII strings for one traveler, for the scrubber to assert against."""
    row = conn.execute(
        "SELECT full_name, email, phone FROM travelers WHERE traveler_id = ?",
        (traveler_id,),
    ).fetchone()
    values = [row["full_name"], row["email"], row["phone"]] if row else []
    profile = conn.execute(
        "SELECT contact_email FROM profiles WHERE traveler_id = ?", (traveler_id,)
    ).fetchone()
    if profile and profile["contact_email"]:
        values.append(profile["contact_email"])
    return [v for v in values if v]


def all_pii_values(conn: sqlite3.Connection) -> list[str]:
    """Every PII string in the dataset, for scrubbing traveler-agnostic responses."""
    rows = conn.execute("SELECT full_name, email, phone FROM travelers").fetchall()
    return [value for row in rows for value in (row["full_name"], row["email"], row["phone"]) if value]


def traveler_exists(conn: sqlite3.Connection, traveler_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM travelers WHERE traveler_id = ?", (traveler_id,)
        ).fetchone()
        is not None
    )
