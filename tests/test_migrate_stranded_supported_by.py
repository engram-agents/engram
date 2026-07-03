"""Tests for tools/migrate_stranded_supported_by.py (part of #1587).

Coverage:
  1. Dry-run is the default (no --apply-equivalent) — reports but does not write.
  2. dry_run=False actually re-roots the stranded edge to the current successor.
  3. The old (stranded) edge is preserved as audit trail, not deleted.
  4. Idempotency: a second run finds nothing left to migrate.

Regression anchor for two bugs caught while reviewing PR #1604:
  - The CLI's dry-run/write default was inverted (SAFETY docstring said
    dry-run by default; code wrote by default). Fixed by replacing --dry-run
    with --apply (write only when passed).
  - find_stranded_edges()'s "already migrated" guard compared against
    sup.target_id (== old_pn.id, i.e. the row `e` itself) instead of
    sup.source_id (the successor) — a self-referential NOT EXISTS that was
    always false, making the script silently find zero stranded edges no
    matter what was actually in the graph. Fixed to compare against the
    successor id.
"""

import atexit
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_migrate_stranded_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "tools", "migrate_stranded_supported_by.py",
)


def _load_migration_module():
    """Load the script by path — it lives under src/engram/tools/, which is
    shadowed by the top-level tools/ package on sys.path (see conftest.py),
    so a plain `import tools.migrate_stranded_supported_by` resolves to the
    wrong package entirely."""
    spec = importlib.util.spec_from_file_location(
        "migrate_stranded_supported_by", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_server():
    """Provision a clean server instance backed by a temporary directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _seed_stranded_edge(server, db_path: str):
    """Directly construct a pre-forward-fix stranded supported_by edge:
    a superseded person, a current successor, and a cornerstone whose
    supported_by edge still points at the superseded (old) person."""
    server.core._get_db().close()  # first call creates the schema
    conn = sqlite3.connect(db_path)
    now = "2026-07-02T12:00:00Z"
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at) VALUES (?, ?, ?, ?)",
        ("pn_old", "person", 0, now),
    )
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at) VALUES (?, ?, ?, ?)",
        ("pn_new", "person", 1, now),
    )
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at) VALUES (?, ?, ?, ?)",
        ("cs_test", "cornerstone", 1, now),
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        ("pn_new", "pn_old", "supersedes", now),
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        ("cs_test", "pn_old", "supported_by", now),
    )
    conn.commit()
    conn.close()


def test_dry_run_default_does_not_write():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_stranded_edge(server, db_path)

    msb = _load_migration_module()
    found = msb.find_stranded_edges(msb._get_db(db_path))
    assert len(found) == 1, f"expected exactly 1 stranded edge, got: {found}"

    msb.run_migration(db_path, dry_run=True)

    conn = sqlite3.connect(db_path)
    rerooted = conn.execute(
        "SELECT * FROM edges WHERE source_id='cs_test' AND target_id='pn_new'"
    ).fetchall()
    assert rerooted == [], f"dry_run=True must not write, found: {rerooted}"


def test_apply_reroots_and_preserves_old_edge():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_stranded_edge(server, db_path)

    msb = _load_migration_module()
    msb.run_migration(db_path, dry_run=False)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source_id, target_id, relation FROM edges WHERE source_id='cs_test'"
    ).fetchall()
    assert ("cs_test", "pn_new", "supported_by") in rows, (
        f"expected a re-rooted edge to the successor, got: {rows}"
    )
    assert ("cs_test", "pn_old", "supported_by") in rows, (
        f"old edge must be preserved as audit trail, got: {rows}"
    )


def test_apply_is_idempotent():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_stranded_edge(server, db_path)

    msb = _load_migration_module()
    msb.run_migration(db_path, dry_run=False)

    # Second run should find nothing left to migrate.
    remaining = msb.find_stranded_edges(msb._get_db(db_path))
    assert remaining == [], f"expected no stranded edges left after migration, got: {remaining}"
