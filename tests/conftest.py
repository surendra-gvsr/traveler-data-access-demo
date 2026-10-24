"""Shared fixtures.

The dataset is generated once per session and the built database once per session,
because generation is deterministic and building it is the slow part. Tests that need
to mutate the store get `fresh_db` instead, which is empty and per-test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traveler_access import events as events_mod, generate, profiles, store


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> dict:
    """The generated stream on disk, plus its report."""
    out_dir = tmp_path_factory.mktemp("dataset")
    produced = generate.generate()
    generate.write(out_dir, produced)
    return {
        "dir": out_dir,
        "report": json.loads((out_dir / "generation_report.json").read_text(encoding="utf-8")),
        "events": list(events_mod.read_events(out_dir / "events.jsonl")),
        "travelers": events_mod.read_travelers(out_dir / "travelers.json"),
    }


@pytest.fixture(scope="session")
def built_db(tmp_path_factory, dataset) -> Path:
    """The dataset loaded and fully built: the state the demo and MCP server see."""
    db_path = tmp_path_factory.mktemp("built") / "profiles.db"
    conn = store.connect(db_path)
    store.load_travelers(conn, dataset["travelers"])
    store.append_deliveries(conn, dataset["events"])
    profiles.build_profiles(conn)
    conn.close()
    return db_path


@pytest.fixture
def built_conn(built_db):
    """A read-only-by-convention connection to the built database."""
    conn = store.connect(built_db)
    yield conn
    conn.close()


@pytest.fixture
def fresh_db(tmp_path) -> Path:
    return tmp_path / "fresh.db"


@pytest.fixture
def fresh_conn(fresh_db):
    conn = store.connect(fresh_db)
    yield conn
    conn.close()


@pytest.fixture
def audit_path(tmp_path) -> Path:
    return tmp_path / "audit.jsonl"
