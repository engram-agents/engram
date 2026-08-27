"""Tests for tools/migrate_prediction_pairing_edges.py (#2252 W6).

Coverage:
  1. Dry-run is the default — reports but does not write.
  2. --apply adds the parallel predicted_by edge.
  3. The old supported_by edge is preserved as audit trail, not deleted.
  4. Idempotency: a second --apply run finds nothing left to migrate.

Mirrors the sibling test_migrate_stranded_supported_by.py pattern (same
migration-tool shape, different target relation/node-type set).
"""

import atexit
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_migrate_prediction_pairing_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "tools", "migrate_prediction_pairing_edges.py",
)


def _load_migration_module():
    """Load the script by path — it lives under src/engram/tools/, which is
    shadowed by the top-level tools/ package on sys.path (see conftest.py),
    so a plain `import tools.migrate_prediction_pairing_edges` resolves to
    the wrong package entirely."""
    spec = importlib.util.spec_from_file_location(
        "migrate_prediction_pairing_edges", _SCRIPT_PATH
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


def _seed_old_style_pairing(server, db_path: str):
    """Directly construct a pre-W6 prediction<->observation pairing edge
    still on `supported_by` (the shape every prediction had before W6's
    forward fix started minting `predicted_by`)."""
    server.core._get_db().close()  # first call creates the schema
    conn = sqlite3.connect(db_path)
    now = "2026-07-02T12:00:00Z"
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at, status) VALUES (?, ?, ?, ?, ?)",
        ("pr_test", "prediction", 1, now, "open"),
    )
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at) VALUES (?, ?, ?, ?)",
        ("ob_test", "observation_predictive", 1, now),
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        ("pr_test", "ob_test", "supported_by", now),
    )
    conn.commit()
    conn.close()


def test_dry_run_default_does_not_write():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_old_style_pairing(server, db_path)

    mig = _load_migration_module()
    found = mig.find_old_style_pairing_edges(mig._get_db(db_path))
    assert len(found) == 1, f"expected exactly 1 old-style pairing edge, got: {found}"

    mig.run_migration(db_path, dry_run=True)

    conn = sqlite3.connect(db_path)
    added = conn.execute(
        "SELECT * FROM edges WHERE source_id='pr_test' AND target_id='ob_test' "
        "AND relation='predicted_by'"
    ).fetchall()
    assert added == [], f"dry_run=True must not write, found: {added}"


def test_apply_adds_parallel_edge_and_preserves_old_edge():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_old_style_pairing(server, db_path)

    mig = _load_migration_module()
    mig.run_migration(db_path, dry_run=False)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source_id, target_id, relation FROM edges WHERE source_id='pr_test'"
    ).fetchall()
    assert ("pr_test", "ob_test", "predicted_by") in rows, (
        f"expected a parallel predicted_by edge, got: {rows}"
    )
    assert ("pr_test", "ob_test", "supported_by") in rows, (
        f"old supported_by edge must be preserved as audit trail, got: {rows}"
    )


def test_apply_is_idempotent():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_old_style_pairing(server, db_path)

    mig = _load_migration_module()
    mig.run_migration(db_path, dry_run=False)

    # Second run should find nothing left to migrate.
    remaining = mig.find_old_style_pairing_edges(mig._get_db(db_path))
    assert remaining == [], f"expected no old-style pairing edges left after migration, got: {remaining}"

    # And a second --apply run must not error or duplicate the edge.
    mig.run_migration(db_path, dry_run=False)
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source_id, target_id, relation FROM edges "
        "WHERE source_id='pr_test' AND target_id='ob_test' AND relation='predicted_by'"
    ).fetchall()
    assert len(rows) == 1, f"second --apply must not duplicate the edge, got: {rows}"
