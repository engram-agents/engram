"""Unit tests for _check_backup_staleness in the SessionStart hook.

Issue #723: independent stale-knowledge.sql check so the agent is nudged to run
engram-nap/engram-sleep when the git backup has fallen behind the live DB.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(monkeypatch, engram_home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)

    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", _HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sql_absent_with_nodes_returns_false(tmp_path, monkeypatch):
    """knowledge.sql absent AND DB has nodes → (False, '...not found...')."""
    import sqlite3 as _sqlite3
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # Create a real SQLite DB with at least one node.
    db_path = str(engram_home / "knowledge.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'test claim')")
    conn.commit()
    conn.close()
    # No knowledge.sql.

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is False
    assert "not found" in reason


def test_sql_absent_empty_schema_returns_ok(tmp_path, monkeypatch):
    """knowledge.sql absent AND DB has 0 nodes (freshly bootstrapped) → (True, None).

    A freshly bootstrapped DB (full schema, no actual user nodes) is a normal
    state at first install and must not fire the warning.
    """
    import sqlite3 as _sqlite3
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # Create a DB with the nodes table but no rows (empty schema).
    db_path = str(engram_home / "knowledge.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.commit()
    conn.close()
    # No knowledge.sql.

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_sql_newer_than_db_returns_ok(tmp_path, monkeypatch):
    """knowledge.sql newer than knowledge.db → (True, None)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    db_path = engram_home / "knowledge.db"
    sql_path = engram_home / "knowledge.sql"
    db_path.write_bytes(b"SQLite")
    sql_path.write_text("-- dump\n")

    # Make sql newer: db_mtime < sql_mtime → stale_seconds negative → ok.
    base = 1_700_000_000.0
    os.utime(str(db_path), (base, base))
    os.utime(str(sql_path), (base + 3600, base + 3600))

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_db_newer_by_1h_within_default_threshold_returns_ok(tmp_path, monkeypatch):
    """knowledge.db newer by 1h (< 2h default threshold) → (True, None)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    db_path = engram_home / "knowledge.db"
    sql_path = engram_home / "knowledge.sql"
    db_path.write_bytes(b"SQLite")
    sql_path.write_text("-- dump\n")

    base = 1_700_000_000.0
    os.utime(str(sql_path), (base, base))
    os.utime(str(db_path), (base + 3600, base + 3600))  # 1h newer

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_db_newer_by_3h_exceeds_default_threshold_returns_false(tmp_path, monkeypatch):
    """knowledge.db newer by 3h (> 2h default threshold) → (False, '...stale...')."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    db_path = engram_home / "knowledge.db"
    sql_path = engram_home / "knowledge.sql"
    db_path.write_bytes(b"SQLite")
    sql_path.write_text("-- dump\n")

    base = 1_700_000_000.0
    os.utime(str(sql_path), (base, base))
    os.utime(str(db_path), (base + 3 * 3600, base + 3 * 3600))  # 3h newer

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is False
    assert "stale" in reason
    assert "3h" in reason


def test_custom_threshold_from_config_is_respected(tmp_path, monkeypatch):
    """Custom backup.stale_threshold_hours in config.json is respected."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    db_path = engram_home / "knowledge.db"
    sql_path = engram_home / "knowledge.sql"
    db_path.write_bytes(b"SQLite")
    sql_path.write_text("-- dump\n")

    base = 1_700_000_000.0
    os.utime(str(sql_path), (base, base))
    # 1.5h newer — within 2h default but exceeds 1h custom threshold
    os.utime(str(db_path), (base + 1.5 * 3600, base + 1.5 * 3600))

    # Write config with a 1h threshold.
    config = {"backup": {"stale_threshold_hours": 1.0}}
    (engram_home / "config.json").write_text(json.dumps(config))

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is False
    assert "stale" in reason


def test_oserror_returns_ok_fail_open(tmp_path, monkeypatch):
    """OSError (e.g. permission denied) → (True, None) — fail-open, never block session start."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # Patch os.path.exists to raise OSError to simulate permission denial.
    original_getmtime = os.path.getmtime

    def _raise_oserror(path):
        raise OSError("Permission denied")

    monkeypatch.setattr(mod.os.path, "getmtime", _raise_oserror)

    # Both files must exist so we get past the exists() checks.
    (engram_home / "knowledge.db").write_bytes(b"SQLite")
    (engram_home / "knowledge.sql").write_text("-- dump\n")

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_db_absent_returns_ok_nothing_to_backup(tmp_path, monkeypatch):
    """knowledge.db absent (fresh install) → (True, None) — nothing to back up."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # No files at all.
    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_db_empty_shell_returns_ok(tmp_path, monkeypatch):
    """knowledge.db is 0 bytes (empty shell left by sqlite3.connect without writes)
    → (True, None).  The sleep-debt banner check calls sqlite3.connect(ENGRAM_DB_PATH)
    during main(), which creates a 0-byte file — we must not fire the staleness warning
    for an empty placeholder.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # Create a 0-byte knowledge.db (no knowledge.sql alongside it).
    (engram_home / "knowledge.db").write_bytes(b"")

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None


def test_sql_absent_no_nodes_table_returns_ok(tmp_path, monkeypatch):
    """knowledge.db exists with no 'nodes' table (corrupt / partial schema)
    → (True, None) — fail-open, never block session start.

    The SELECT count(*) FROM nodes raises OperationalError; the except branch
    sets _count=0 → treated as "nothing to back up yet".
    """
    import sqlite3 as _sqlite3
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)

    # Create a DB with a different table — no 'nodes' table.
    db_path = str(engram_home / "knowledge.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    # No knowledge.sql.

    ok, reason = mod._check_backup_staleness(str(engram_home))
    assert ok is True
    assert reason is None
