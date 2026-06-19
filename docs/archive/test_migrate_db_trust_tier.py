"""Regression tests for tools/migrate_db_trust_tier.py — issue #440.

Bug: on a fresh DB (one that doesn't yet have the 4 trust-tier-family columns),
the migration crashed with sqlite3.OperationalError: no such column: trust_tier
because the pn_count query ran before ALTER TABLE added the column.

Fix: gate the pn_count query on whether 'trust_tier' is already in
existing_cols.  When absent, fall back to a plain COUNT of person nodes
(every one will be backfilled after ALTER TABLE runs).

Coverage (4 tests):
  1. test_dry_run_on_fresh_db_doesnt_crash   — fresh DB, --dry-run, no crash.
  2. test_live_on_fresh_db_completes         — fresh DB, --live, all 4 columns
                                               created, pn_* backfilled,
                                               backup written.
  3. test_live_idempotent_re_run             — after a successful live run,
                                               re-run is a clean no-op.
  4. test_dry_run_on_already_migrated_db    — after a live run, --dry-run
                                               reports all columns present
                                               and no backfill needed.
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

# Add the repo root and tools/ to sys.path so we can import both server and the
# migration script independently.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from pathlib import Path

from migrate_db_trust_tier import NEW_COLUMNS, run_migration


# ── Schema fixture ────────────────────────────────────────────────────────────

# Canonical nodes table schema MINUS the 4 trust-tier-family columns.  This
# mirrors the state of an existing DB that predates the trust-tier migration.
_NODES_SCHEMA_NO_TRUST_TIER = """
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    claim           TEXT,
    created_at      TEXT NOT NULL,

    source_url      TEXT,
    source_title    TEXT,
    source_domain   TEXT,
    source_date     TEXT,
    source_accessed TEXT,
    content_snippet TEXT,

    evidence_id     TEXT,
    quoted_text     TEXT,
    interpretation  TEXT,
    quote_type      TEXT,

    predicted_event      TEXT,
    resolution_timeframe TEXT,
    status          TEXT DEFAULT 'active',
    resolved_by     TEXT,

    logical_chain   TEXT,

    confidence          REAL,
    confidence_history  TEXT DEFAULT '[]',
    supersedes          TEXT,
    superseded_by       TEXT,
    is_current          INTEGER DEFAULT 1,
    metadata            TEXT DEFAULT '{}',

    importance_base     REAL DEFAULT 0.5,
    importance_score    REAL DEFAULT 0.5,
    recall_turn         INTEGER DEFAULT 0,
    recall_count        INTEGER DEFAULT 0,
    memory_status       TEXT DEFAULT 'active',

    utility_score       REAL DEFAULT 0.0,
    embedding           TEXT
);
"""

_TRUST_TIER_COLUMNS = {col for col, _ in NEW_COLUMNS}


def _make_fresh_db(tmp_dir: str, num_persons: int = 3) -> Path:
    """Create a fresh DB at tmp_dir/knowledge.db without trust-tier columns.

    Inserts num_persons person nodes so the migration has something to
    backfill.  Returns the Path to the DB.
    """
    db_path = Path(tmp_dir) / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_NODES_SCHEMA_NO_TRUST_TIER)
    now = "2026-01-01T00:00:00Z"
    for i in range(num_persons):
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at) VALUES (?, 'person', ?, ?)",
            (f"pn_{1000 + i:04d}", f"Test person {i}", now),
        )
    conn.commit()
    conn.close()
    return db_path


def _get_columns(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    conn.close()
    return cols


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_dry_run_on_fresh_db_doesnt_crash(tmp_path, capsys):
    """--dry-run on a DB without trust-tier columns must not raise."""
    tmp_dir = tempfile.mkdtemp(prefix="trust_tier_test_", dir=str(tmp_path))
    db_path = _make_fresh_db(tmp_dir, num_persons=3)

    rc = run_migration(db_path, dry_run=True)

    assert rc == 0, f"run_migration returned {rc}, expected 0"

    captured = capsys.readouterr()
    out = captured.out

    # Dry-run should announce the planned ALTER TABLEs.
    assert "would add column" in out.lower() or "alter table" in out.lower(), (
        f"Expected dry-run output to mention column additions. Got:\n{out}"
    )

    # Confirm the columns were NOT actually added (dry-run must not write).
    cols_after = _get_columns(db_path)
    for col in _TRUST_TIER_COLUMNS:
        assert col not in cols_after, (
            f"Dry-run must not add column '{col}' but it appeared after the run."
        )


def test_live_on_fresh_db_completes(tmp_path, capsys):
    """--live on a fresh DB: 4 columns added, pn_* backfilled, backup written."""
    tmp_dir = tempfile.mkdtemp(prefix="trust_tier_test_", dir=str(tmp_path))
    db_path = _make_fresh_db(tmp_dir, num_persons=3)

    rc = run_migration(db_path, dry_run=False)

    assert rc == 0, f"run_migration returned {rc}, expected 0"

    # All 4 columns must now exist.
    cols_after = _get_columns(db_path)
    for col in _TRUST_TIER_COLUMNS:
        assert col in cols_after, f"Column '{col}' missing after live migration."

    # All 3 pn_* rows must have trust_tier='unknown'.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, trust_tier FROM nodes WHERE type = 'person'"
    ).fetchall()
    conn.close()
    assert len(rows) == 3, f"Expected 3 person rows, found {len(rows)}."
    for row in rows:
        assert row["trust_tier"] == "unknown", (
            f"Row {row['id']}: expected trust_tier='unknown', got '{row['trust_tier']}'"
        )

    # A backup file must have been written next to the DB.
    backup_files = list(Path(tmp_dir).glob("knowledge.db.pre-migration-*.bak"))
    assert len(backup_files) >= 1, (
        f"Expected at least one backup file in {tmp_dir}, found none."
    )


def test_live_idempotent_re_run(tmp_path, capsys):
    """Re-running --live on an already-migrated DB returns 0 without errors."""
    tmp_dir = tempfile.mkdtemp(prefix="trust_tier_test_", dir=str(tmp_path))
    db_path = _make_fresh_db(tmp_dir, num_persons=2)

    # First run.
    rc1 = run_migration(db_path, dry_run=False)
    assert rc1 == 0, f"First run returned {rc1}."

    # Read trust_tier values after first run.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    before = {r["id"]: r["trust_tier"] for r in conn.execute(
        "SELECT id, trust_tier FROM nodes WHERE type = 'person'"
    ).fetchall()}
    conn.close()

    # Second run — must also succeed.
    rc2 = run_migration(db_path, dry_run=False)
    assert rc2 == 0, f"Second (idempotent) run returned {rc2}."

    # trust_tier values must be unchanged.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    after = {r["id"]: r["trust_tier"] for r in conn.execute(
        "SELECT id, trust_tier FROM nodes WHERE type = 'person'"
    ).fetchall()}
    conn.close()

    assert before == after, (
        f"Re-run altered trust_tier values. Before: {before} / After: {after}"
    )


def test_dry_run_on_already_migrated_db(tmp_path, capsys):
    """--dry-run after a live run reports all columns present + no backfill needed."""
    tmp_dir = tempfile.mkdtemp(prefix="trust_tier_test_", dir=str(tmp_path))
    db_path = _make_fresh_db(tmp_dir, num_persons=2)

    # Live run first.
    rc_live = run_migration(db_path, dry_run=False)
    assert rc_live == 0

    # Discard live output before the dry-run check.
    capsys.readouterr()

    rc_dry = run_migration(db_path, dry_run=True)
    assert rc_dry == 0, f"Dry-run on migrated DB returned {rc_dry}."

    captured = capsys.readouterr()
    out = captured.out

    # Should report all columns already present.
    assert "all 4 columns already exist" in out, (
        f"Expected 'all 4 columns already exist' in dry-run output. Got:\n{out}"
    )

    # Should report no backfill needed.
    assert "all pn_* already have a non-null tier" in out, (
        f"Expected no-backfill message in dry-run output. Got:\n{out}"
    )
