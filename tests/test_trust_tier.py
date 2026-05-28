"""Tests for Layer-1 trust-tier mechanism (PR-Layer1-V1).

Coverage (17 tests):
  1.  Migration idempotency — run twice, clean no-op second time.
  2.  Migration data backfill — pn_* get 'unknown'; non-person nodes unaffected.
  3.  engram_set_trust_tier — below threshold, no approval required.
  4.  engram_set_trust_tier — at/above threshold without approval → error contains
      literal "primary_user_approval_obtained".
  5.  engram_set_trust_tier — at/above threshold with approval → succeeds, writes
      edit_history row.
  6.  engram_set_trust_tier — primary_user tier → structural error.
  7.  engram_set_trust_tier — invalid tier → allowlist error.
  8.  engram_set_trust_tier — invalid justification_obs_id (non-existent, wrong type,
      retracted).
  9.  engram_set_trust_tier — same-tier no-op → no edit_history row.
  10. engram_add_person — new pn_ has trust_tier='unknown'.
  11. engram_add_trust_signal — happy path: ts_ + 2 edges in single transaction.
  12. engram_add_trust_signal — invalid subject_pn → rejected.
  13. engram_add_trust_signal — invalid source_obs_id → rejected.
  14. engram_add_trust_signal — polarity out of range → rejected.
  15. engram_derive — rejects ts_ as supporting_id (CLAIM_BEARING_TYPES enforcement).
  16. Cascade — retract source ob_ → ts_ is tainted.
  17. Cascade — supersede source ob_ → ts_ is staled.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tools/ to path so we can import the migration script.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_trust_tier")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _mk_person(server, name: str = "TestPerson") -> str:
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": name,
        "role": "test subject",
    })))
    assert "person_id" in r, r
    return r["person_id"]


def _mk_evidence_file(content: str) -> str:
    """Write content to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="engram_tt_ev_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _mk_obs(server, claim: str = "Test claim.") -> str:
    # Write a temp evidence file containing the exact quoted_text so the
    # quote-verification guard passes. file:// bypasses DNS resolution.
    ev_file = _mk_evidence_file(claim + "\n")
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": f"file://{ev_file}",
        "title": "Trust-tier test source",
        "claim": claim,
        "quoted_text": claim,
        "interpretation": f"Test: {claim}",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"Failed to create obs: {r}"
    return r["observation_id"]


# ── Test 1: Migration idempotency ──────────────────────────────────────────

def test_migration_idempotent():
    """Run migration twice; second run is a clean no-op."""
    from migrate_db_trust_tier import run_migration, NEW_COLUMNS
    from pathlib import Path

    server = fresh_server()
    # Trigger DB initialization by making a server call
    _mk_person(server, "MigrationIdempotentSeed")
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    assert db_path.exists(), f"DB not found at {db_path}"

    # First run
    result1 = run_migration(db_path, dry_run=False)
    assert result1 == 0, f"First migration run failed with exit code {result1}"

    # Second run — must succeed cleanly (no crash on duplicate column)
    result2 = run_migration(db_path, dry_run=False)
    assert result2 == 0, f"Second migration run failed with exit code {result2}"

    # Verify all 4 columns present
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    conn.close()
    for col, _ in NEW_COLUMNS:
        assert col in cols, f"Column '{col}' missing after double migration"


# ── Test 2: Migration data backfill ────────────────────────────────────────

def test_migration_backfill():
    """Existing pn_* get trust_tier='unknown'; non-person nodes unaffected."""
    from migrate_db_trust_tier import run_migration
    from pathlib import Path

    server = fresh_server()
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))

    # Create a person node and an observation node before migration
    pn_id = _mk_person(server, "BackfillTest")
    ob_id = _mk_obs(server, "Non-person node — trust_tier should remain NULL.")

    # Manually null out trust_tier on the pn_ to simulate a pre-migration DB
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE nodes SET trust_tier = NULL WHERE id = ?", (pn_id,))
    conn.commit()
    conn.close()

    # Run migration
    result = run_migration(db_path, dry_run=False)
    assert result == 0

    # Verify
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    pn_row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    ob_row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (ob_id,)).fetchone()
    conn.close()

    assert pn_row["trust_tier"] == "unknown", f"pn_ trust_tier should be 'unknown', got {pn_row['trust_tier']}"
    assert ob_row["trust_tier"] is None, f"ob_ trust_tier should be NULL, got {ob_row['trust_tier']}"


# ── Test 3: set_trust_tier — below threshold ───────────────────────────────

def test_set_trust_tier_below_threshold():
    """known_external / unknown / suspect require no approval."""
    server = fresh_server()
    pn_id = _mk_person(server, "ExternalPerson")

    for tier in ("known_external", "suspect", "unknown"):
        r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
            "target_pn": pn_id,
            "tier": tier,
        })))
        # unknown→unknown may be a no_op from prior iteration
        assert r.get("status") in ("set", "no_op"), f"Expected set or no_op for tier '{tier}', got: {r}"
        if r["status"] == "set":
            assert r["to_tier"] == tier, r


# ── Test 4: set_trust_tier — at/above threshold without approval ───────────

def test_set_trust_tier_above_threshold_without_approval_contains_hidden_param():
    """Attempting to set our_side/user_family without approval returns an error
    that explicitly names 'primary_user_approval_obtained'."""
    server = fresh_server()
    pn_id = _mk_person(server, "InternalAgent")

    for tier in ("our_side", "user_family"):
        r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
            "target_pn": pn_id,
            "tier": tier,
        })))
        assert "error" in r, f"Expected error for tier '{tier}' without approval, got: {r}"
        assert "primary_user_approval_obtained" in r["error"], (
            f"Error message for tier '{tier}' must contain literal "
            f"'primary_user_approval_obtained'. Got:\n{r['error']}"
        )


# ── Test 4b: set_trust_tier — unknown-field error does not leak sealed param ─

def test_set_trust_tier_unknown_field_error_does_not_leak_hidden_param():
    """Unknown-field validator error must NOT reveal 'primary_user_approval_obtained'.

    The sealed param name is the teaching surface that appears only in the
    friction-warning error (Test 4). Leaking it via the unknown-field validator
    echo would let an agent probe the param name without ever hitting the
    friction-warning path — bypassing the structural-honesty stake.
    """
    server = fresh_server()
    pn_id = _mk_person(server, "SealLeakProbe")

    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "foo_typo": "bar",
    })))
    assert "error" in r, f"Expected error for unknown field, got: {r}"
    error_str = r["error"]
    assert "Unknown fields" in error_str, (
        f"Expected 'Unknown fields' in error, got:\n{error_str}"
    )
    assert "primary_user_approval_obtained" not in error_str, (
        f"Seal leak: unknown-field error must NOT contain "
        f"'primary_user_approval_obtained'. Got:\n{error_str}"
    )


# ── Test 5: set_trust_tier — at/above threshold with approval ─────────────

def test_set_trust_tier_above_threshold_with_approval():
    """our_side with justification + approval flag succeeds and writes edit_history."""
    server = fresh_server()
    pn_id = _mk_person(server, "OurAgent")
    ob_id = _mk_obs(server, "Primary user approved OurAgent to our_side tier.")

    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "justification_obs_id": ob_id,
        "primary_user_approval_obtained": True,
    })))
    assert r.get("status") == "set", r
    assert r["to_tier"] == "our_side", r
    assert r["from_tier"] == "unknown", r
    assert r.get("edit_history_id") is not None, r

    # Verify edit_history row content
    hist = json.loads(server.engram_history(payload_json=json.dumps({
        "mode": "edits",
        "action": "trust_tier_set",
        "node_id": pn_id,
    })))
    assert hist["shown"] >= 1, f"Expected edit_history row, got: {hist}"
    entry = hist["entries"][0]
    assert entry["action"] == "trust_tier_set"
    assert entry["node_id"] == pn_id
    details = entry["details"]
    assert details["from_tier"] == "unknown"
    assert details["to_tier"] == "our_side"
    assert details["justification_obs_id"] == ob_id
    assert details["primary_user_approval_obtained"] is True

    # Verify db state
    import sqlite3
    from pathlib import Path
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    conn.close()
    assert row["trust_tier"] == "our_side", row


# ── Test 6: set_trust_tier — primary_user tier ────────────────────────────

def test_set_trust_tier_primary_user_rejected():
    """'primary_user' tier must be rejected with a structural error."""
    server = fresh_server()
    pn_id = _mk_person(server, "StructuralTest")

    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "primary_user",
    })))
    assert "error" in r, r
    assert "structural" in r["error"].lower() or "primary_user" in r["error"], r


# ── Test 7: set_trust_tier — invalid tier value ────────────────────────────

def test_set_trust_tier_invalid_tier():
    """Unrecognized tier values are rejected with an allowlist error."""
    server = fresh_server()
    pn_id = _mk_person(server, "TierTest")

    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "totally_made_up_tier",
    })))
    assert "error" in r, r
    # Error should mention the invalid tier or the allowlist
    assert "totally_made_up_tier" in r["error"] or "tier" in r["error"].lower(), r


# ── Test 8: set_trust_tier — invalid justification_obs_id ─────────────────

def test_set_trust_tier_invalid_justification_obs_id():
    """Three sub-cases: non-existent, wrong type, retracted."""
    server = fresh_server()
    pn_id = _mk_person(server, "ApprovalValidation")

    # 8a: non-existent obs
    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "justification_obs_id": "ob_9999",
        "primary_user_approval_obtained": True,
    })))
    assert "error" in r and "ob_9999" in r["error"], r

    # 8b: wrong type (create a person node and use its id as justification)
    pn2_id = _mk_person(server, "WrongTypeNode")
    r2 = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "justification_obs_id": pn2_id,
        "primary_user_approval_obtained": True,
    })))
    assert "error" in r2, r2
    assert "observation" in r2["error"].lower() or pn2_id in r2["error"], r2

    # 8c: retracted obs
    ob_id = _mk_obs(server, "Approval obs to be retracted.")
    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id,
        "error_type": "fabricated_quote",
        "reason": "Test retract for validation check.",
    })))
    assert ret.get("status") == "retracted", ret

    r3 = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "justification_obs_id": ob_id,
        "primary_user_approval_obtained": True,
    })))
    assert "error" in r3, r3
    assert "retracted" in r3["error"].lower(), r3


# ── Test 9: set_trust_tier — same-tier no-op ──────────────────────────────

def test_set_trust_tier_same_tier_noop():
    """Setting to the current tier is a no-op; no edit_history row is written."""
    server = fresh_server()
    pn_id = _mk_person(server, "NoOpPerson")

    # pn_ starts at 'unknown'
    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "unknown",
    })))
    assert r.get("status") == "no_op", r

    # No edit_history row for trust_tier_set
    hist = json.loads(server.engram_history(payload_json=json.dumps({
        "mode": "edits",
        "action": "trust_tier_set",
        "node_id": pn_id,
    })))
    assert hist["shown"] == 0, f"Expected 0 edit_history rows for no-op, got {hist['shown']}"


# ── Test 10: engram_add_person — default tier ─────────────────────────────

def test_add_person_default_trust_tier():
    """New pn_ created via engram_add_person gets trust_tier='unknown'."""
    server = fresh_server()
    pn_id = _mk_person(server, "DefaultTierTest")

    import sqlite3
    from pathlib import Path
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    conn.close()
    assert row is not None, f"Person node {pn_id} not found in DB"
    assert row["trust_tier"] == "unknown", (
        f"New person should have trust_tier='unknown', got '{row['trust_tier']}'"
    )


# ── Test 11: engram_add_trust_signal — happy path ─────────────────────────

def test_add_trust_signal_happy_path():
    """Creates ts_ + about edge + derives_from edge in a single transaction."""
    server = fresh_server()
    pn_id = _mk_person(server, "SignalSubject")
    ob_id = _mk_obs(server, "Observed alignment behavior.")

    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": ob_id,
        "kind": "alignment",
        "polarity": 0.7,
        "weight": 0.5,
        "claim": "Subject showed alignment signal — mild positive, moderate weight.",
    })))
    assert r.get("status") == "created", r
    ts_id = r.get("trust_signal_id")
    assert ts_id is not None and ts_id.startswith("ts_"), r

    # Verify DB: ts_ row + 2 edges
    import sqlite3
    from pathlib import Path
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    ts_row = conn.execute("SELECT * FROM nodes WHERE id = ?", (ts_id,)).fetchone()
    assert ts_row is not None, f"ts_ node not found in DB"
    assert ts_row["type"] == "trust_signal"
    assert ts_row["trust_signal_kind"] == "alignment"
    assert abs(ts_row["trust_signal_polarity"] - 0.7) < 1e-6
    assert abs(ts_row["trust_signal_weight"] - 0.5) < 1e-6

    about_edge = conn.execute(
        "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
        (ts_id, pn_id),
    ).fetchone()
    assert about_edge is not None, f"about edge missing for {ts_id} → {pn_id}"

    derives_edge = conn.execute(
        "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'derives_from'",
        (ts_id, ob_id),
    ).fetchone()
    assert derives_edge is not None, f"derives_from edge missing for {ts_id} → {ob_id}"

    conn.close()


# ── Test 12: engram_add_trust_signal — invalid subject_pn ─────────────────

def test_add_trust_signal_invalid_subject_pn():
    """Non-existent or non-person subject_pn is rejected."""
    server = fresh_server()
    ob_id = _mk_obs(server, "Valid obs.")

    # Non-existent
    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": "pn_9999",
        "source_obs_id": ob_id,
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Test signal.",
    })))
    assert "error" in r and "pn_9999" in r["error"], r

    # Wrong type: use the obs id as subject_pn
    r2 = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": ob_id,
        "source_obs_id": ob_id,
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Test signal.",
    })))
    assert "error" in r2 and "person" in r2["error"].lower(), r2


# ── Test 13: engram_add_trust_signal — invalid source_obs_id ──────────────

def test_add_trust_signal_invalid_source_obs_id():
    """Non-existent, wrong-type, or retracted source_obs_id is rejected."""
    server = fresh_server()
    pn_id = _mk_person(server, "SignalSubject2")

    # Non-existent
    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": "ob_9999",
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Test.",
    })))
    assert "error" in r and "ob_9999" in r["error"], r

    # Wrong type (person node as source)
    r2 = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": pn_id,
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Test.",
    })))
    assert "error" in r2 and "observation" in r2["error"].lower(), r2

    # Retracted obs
    ob_id = _mk_obs(server, "Obs to retract.")
    json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id,
        "error_type": "fabricated_quote",
        "reason": "Test.",
    })))
    r3 = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": ob_id,
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Test.",
    })))
    assert "error" in r3 and "retracted" in r3["error"].lower(), r3


# ── Test 14: engram_add_trust_signal — polarity out of range ──────────────

def test_add_trust_signal_polarity_out_of_range():
    """polarity outside [-1.0, 1.0] is rejected."""
    server = fresh_server()
    pn_id = _mk_person(server, "PolarityTest")
    ob_id = _mk_obs(server, "Obs for polarity test.")

    for bad_polarity in (1.1, -1.1, 2.0, -99.0):
        r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
            "subject_pn": pn_id,
            "source_obs_id": ob_id,
            "kind": "alignment",
            "polarity": bad_polarity,
            "weight": 0.5,
            "claim": "Out of range polarity test.",
        })))
        assert "error" in r, f"Expected error for polarity={bad_polarity}, got: {r}"
        assert "polarity" in r["error"].lower(), r


# ── Test 15: engram_derive rejects ts_ as supporting_id ───────────────────

def test_derive_rejects_ts_as_supporting_id():
    """CLAIM_BEARING_TYPES auto-rejects trust_signal as a derivation premise."""
    server = fresh_server()
    pn_id = _mk_person(server, "SignalForDerive")
    ob_id = _mk_obs(server, "Source for trust signal.")

    ts_r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": ob_id,
        "kind": "alignment",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Alignment signal.",
    })))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    # Attempt to use ts_ as a supporting_id in a derivation
    ob2_id = _mk_obs(server, "Second premise for derive.")
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Derived claim using ts_ as premise.",
        "supporting_ids": f"{ts_id},{ob2_id}",
        "logical_chain": "ts_ is a premise (should be rejected).",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "error" in r, f"Expected error when using ts_ as derive premise, got: {r}"
    assert ts_id in r["error"], r


# ── Test 16: Cascade — retract source ob_ taints ts_ ─────────────────────

def test_retract_source_obs_taints_trust_signal():
    """Retracting the source ob_ taints the derived ts_ node."""
    server = fresh_server()
    pn_id = _mk_person(server, "CascadeSubject")
    ob_id = _mk_obs(server, "Observation that will be retracted.")

    ts_r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": ob_id,
        "kind": "friction",
        "polarity": -0.5,
        "weight": 0.8,
        "claim": "Friction signal derived from the obs.",
    })))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    # Retract the source obs
    ret_r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id,
        "error_type": "fabricated_quote",
        "reason": "Test retract to verify cascade to ts_.",
    })))
    assert ret_r.get("status") == "retracted", ret_r

    # Check that ts_ was tainted
    import sqlite3
    from pathlib import Path
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ts_row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ts_id,)).fetchone()
    conn.close()
    assert ts_row is not None
    meta = json.loads(ts_row["metadata"] or "{}")
    assert "tainted_by" in meta, f"ts_ should have tainted_by in metadata. Got: {meta}"
    assert ob_id in meta["tainted_by"], (
        f"ts_ should be tainted by {ob_id}. tainted_by: {meta['tainted_by']}"
    )


# ── Test 17: Cascade — supersede source ob_ stales ts_ ───────────────────

def test_supersede_source_obs_stales_trust_signal():
    """Superseding the source ob_ marks the derived ts_ as stale."""
    server = fresh_server()
    pn_id = _mk_person(server, "SupersedeSubject")
    ob_old = _mk_obs(server, "Original observation about subject.")
    ob_new = _mk_obs(server, "Refined observation replacing the original.")

    ts_r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps({
        "subject_pn": pn_id,
        "source_obs_id": ob_old,
        "kind": "care",
        "polarity": 0.9,
        "weight": 0.6,
        "claim": "Care signal from original observation.",
    })))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    # Supersede ob_old with ob_new
    sup_r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_old,
        "new_node_id": ob_new,
        "supersede_reason": "Refined claim — test supersede cascade to ts_.",
    })))
    assert sup_r.get("status") == "superseded", sup_r

    # Check that ts_ has stale_by in metadata
    import sqlite3
    from pathlib import Path
    db_path = Path(os.path.join(TEST_DIR, "knowledge.db"))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ts_row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ts_id,)).fetchone()
    conn.close()
    assert ts_row is not None
    meta = json.loads(ts_row["metadata"] or "{}")
    assert "stale_by" in meta, f"ts_ should have stale_by in metadata. Got: {meta}"
    assert ob_old in meta["stale_by"], (
        f"ts_ should be stale_by {ob_old}. stale_by: {meta['stale_by']}"
    )
