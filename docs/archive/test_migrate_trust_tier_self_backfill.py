"""Tests for migrate_trust_tier_self_backfill migration script.

Coverage (4 tests mirroring the migrate_db_trust_tier test pattern):
  1.  test_dry_run_fresh_db — dry run on a fresh DB with is_self node shows the
      planned change but does NOT write anything to the DB.
  2.  test_live_fresh_db — live run on a fresh DB backfills trust_tier='self'
      for all pn_* with is_self=true; non-is_self and non-person nodes unaffected.
  3.  test_plan_excludes_superseded_self_anchors — plan() only returns is_current=1
      self-anchors; superseded (is_current=0) nodes are excluded.
  4.  test_idempotent_rerun — re-running live on an already-migrated DB is a no-op
      (returns 0, no edit_history rows added on the second run).
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

# Add src/engram/ (for `import server`) and docs/archive/migration/ (for
# `from migrate_trust_tier_self_backfill import plan`) to sys.path.
# File is at docs/archive/ — repo root is two dirname() levels up.
_ARCHIVE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_ARCHIVE_DIR))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src", "engram"))
sys.path.insert(0, os.path.join(_ARCHIVE_DIR, "migration"))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_migrate_tt_self_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly-initialized server module pointed at TEST_DIR."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _mk_person(server, name: str, is_self: bool = False) -> str:
    payload: dict = {"name": name, "role": "test subject"}
    if is_self:
        payload["is_self"] = True
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "person_id" in r, r
    return r["person_id"]


def _mk_obs(server, claim: str = "Test claim.") -> str:
    fd, ev_file = tempfile.mkstemp(suffix=".txt", prefix="engram_ttv2_ev_")
    with os.fdopen(fd, "w") as f:
        f.write(claim + "\n")
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": f"file://{ev_file}",
        "title": "Migration test source",
        "claim": claim,
        "quoted_text": claim,
        "interpretation": f"Test: {claim}",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, r
    return r["observation_id"]


# ── Test 1: dry run on a fresh DB ─────────────────────────────────────────

def test_dry_run_fresh_db():
    """Dry run identifies the is_self node as needing backfill but writes nothing."""
    from pathlib import Path
    from migrate_trust_tier_self_backfill import plan

    server = fresh_server()
    pn_self = _mk_person(server, "SelfAnchor", is_self=True)
    pn_normal = _mk_person(server, "NormalPerson", is_self=False)
    _mk_obs(server, "Observation — should not appear in plan.")

    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    assert db_path.exists(), f"DB not found at {db_path}"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    migration_plan = plan(conn)
    conn.close()

    # Should find exactly the is_self node
    ids_in_plan = {entry["id"] for entry in migration_plan}
    assert pn_self in ids_in_plan, (
        f"Self-anchor node '{pn_self}' should appear in migration plan. Got: {ids_in_plan}"
    )
    assert pn_normal not in ids_in_plan, (
        f"Non-self node '{pn_normal}' should NOT appear in plan. Got: {ids_in_plan}"
    )

    # Verify DB was NOT modified (dry run only calls plan(), no writes)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_self,)).fetchone()
    conn.close()
    assert row["trust_tier"] != "self", (
        f"Dry run must not write to DB; trust_tier should still be 'unknown', "
        f"got '{row['trust_tier']}'"
    )


# ── Test 2: live run on a fresh DB ────────────────────────────────────────

def test_live_fresh_db():
    """Live run backfills trust_tier='self' for is_self=true nodes; others unaffected."""
    from pathlib import Path
    from migrate_trust_tier_self_backfill import plan, apply_migration

    server = fresh_server()
    pn_self = _mk_person(server, "SelfAnchor", is_self=True)
    pn_normal = _mk_person(server, "NormalPerson", is_self=False)
    ob_id = _mk_obs(server, "Observation — trust_tier should stay NULL.")

    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    migration_plan = plan(conn)
    result = apply_migration(conn, migration_plan)
    conn.commit()

    assert result["updated"] == 1, (
        f"Expected 1 updated node, got {result['updated']}. Details: {result['details']}"
    )

    # Verify self node was updated
    row_self = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (pn_self,)
    ).fetchone()
    assert row_self["trust_tier"] == "self", (
        f"Self node should have trust_tier='self', got '{row_self['trust_tier']}'"
    )

    # Verify normal person node was NOT updated
    row_normal = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (pn_normal,)
    ).fetchone()
    assert row_normal["trust_tier"] == "unknown", (
        f"Normal person node should remain 'unknown', got '{row_normal['trust_tier']}'"
    )

    # Verify observation node was NOT updated (trust_tier should be NULL)
    row_ob = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (ob_id,)
    ).fetchone()
    assert row_ob["trust_tier"] is None, (
        f"Observation trust_tier should be NULL, got '{row_ob['trust_tier']}'"
    )

    # Verify edit_history row was written
    hist = conn.execute(
        "SELECT * FROM edit_history WHERE node_id = ? AND action = 'trust_tier_set_migration_self'",
        (pn_self,),
    ).fetchall()
    assert len(hist) == 1, f"Expected 1 edit_history row for migration, got {len(hist)}"
    details = json.loads(hist[0]["details"])
    assert details.get("to_tier") == "self", details
    assert details.get("from_tier") == "unknown", details

    conn.close()


# ── Test 3: plan() excludes superseded self-anchors ───────────────────────

def test_plan_excludes_superseded_self_anchors():
    """plan() returns only is_current=1 self-anchors; superseded (is_current=0) excluded."""
    from pathlib import Path
    from migrate_trust_tier_self_backfill import plan, apply_migration

    server = fresh_server()
    pn_current = _mk_person(server, "CurrentSelf", is_self=True)

    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))

    # Manually INSERT a superseded self-anchor (is_self=true, is_current=0).
    # This simulates an old pn_* that was superseded by the current one but still
    # retains is_self=true in metadata. The singleton guard must not fire on it.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    import uuid
    superseded_id = "pn_" + uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO nodes (id, type, is_current, trust_tier, metadata, created_at)
           VALUES (?, 'person', 0, 'unknown',
                   '{"is_self": true, "name": "OldSelf"}',
                   datetime('now', '-1 day'))""",
        (superseded_id,),
    )
    conn.commit()

    # plan() must return only the current self-anchor, not the superseded one.
    migration_plan = plan(conn)
    ids_in_plan = {entry["id"] for entry in migration_plan}

    assert pn_current in ids_in_plan, (
        f"Current self-anchor '{pn_current}' must appear in plan. Got: {ids_in_plan}"
    )
    assert superseded_id not in ids_in_plan, (
        f"Superseded self-anchor '{superseded_id}' must NOT appear in plan. Got: {ids_in_plan}"
    )

    # Apply the migration and confirm only the current node gets trust_tier='self'.
    result = apply_migration(conn, migration_plan)
    conn.commit()

    assert result["updated"] == 1, (
        f"Expected exactly 1 update (current self only). Got: {result}"
    )

    row_current = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (pn_current,)
    ).fetchone()
    assert row_current["trust_tier"] == "self", (
        f"Current self-anchor should have trust_tier='self', got '{row_current['trust_tier']}'"
    )

    row_superseded = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (superseded_id,)
    ).fetchone()
    assert row_superseded["trust_tier"] == "unknown", (
        f"Superseded self-anchor trust_tier should remain 'unknown', "
        f"got '{row_superseded['trust_tier']}'"
    )

    conn.close()
# ── Test 4: idempotent rerun ──────────────────────────────────────────────

def test_idempotent_rerun():
    """Re-running the live migration on an already-migrated DB is a no-op."""
    from pathlib import Path
    from migrate_trust_tier_self_backfill import plan, apply_migration

    server = fresh_server()
    pn_self = _mk_person(server, "SelfAnchorIdem", is_self=True)

    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # First run — should update the node
    migration_plan_1 = plan(conn)
    result_1 = apply_migration(conn, migration_plan_1)
    conn.commit()
    assert result_1["updated"] == 1, f"First run should update 1 node, got: {result_1}"

    # Second run — should find nothing to do
    migration_plan_2 = plan(conn)
    assert len(migration_plan_2) == 0, (
        f"Second plan should be empty (already migrated). Got: {migration_plan_2}"
    )
    result_2 = apply_migration(conn, migration_plan_2)
    conn.commit()
    assert result_2["updated"] == 0, (
        f"Second run should be a no-op (0 updated). Got: {result_2}"
    )

    # Verify DB state is still correct after double run
    row = conn.execute(
        "SELECT trust_tier FROM nodes WHERE id = ?", (pn_self,)
    ).fetchone()
    assert row["trust_tier"] == "self", (
        f"After idempotent re-run, trust_tier should still be 'self'. Got '{row['trust_tier']}'"
    )

    # Verify only 1 edit_history row (not 2)
    hist = conn.execute(
        "SELECT * FROM edit_history WHERE node_id = ? AND action = 'trust_tier_set_migration_self'",
        (pn_self,),
    ).fetchall()
    assert len(hist) == 1, (
        f"Idempotent re-run should not add a second edit_history row. Got {len(hist)} rows."
    )

    conn.close()
