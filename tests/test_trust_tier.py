"""Tests for Layer-1 trust-tier mechanism (PR-Layer1-V1).

Coverage (15 tests; test 4b extracted to test_trust_tier_payload.py):
  1.  engram_set_trust_tier — below threshold, no approval required.
  2.  engram_set_trust_tier — at/above threshold without approval → error contains
      literal "primary_user_approval_obtained".
  3.  engram_set_trust_tier — at/above threshold with approval → succeeds, writes
      edit_history row.
  4.  engram_set_trust_tier — primary_user tier without approval → approval-gate error
      (primary_user is now assignable; this test verifies the approval gate fires, not
      a structural rejection).
  5.  engram_set_trust_tier — invalid tier → allowlist error (7-tier list, no trailer).
  6.  engram_set_trust_tier — invalid justification_obs_id (non-existent, wrong type,
      retracted).
  7.  engram_set_trust_tier — same-tier no-op → no edit_history row.
  8.  engram_add_person — new pn_ has trust_tier='unknown'.
  9.  engram_add_trust_signal — happy path: ts_ + 2 edges in single transaction.
  10. engram_add_trust_signal — invalid subject_pn → rejected.
  11. engram_add_trust_signal — invalid source_obs_id → rejected.
  12. engram_add_trust_signal — polarity out of range → rejected.
  13. engram_derive — rejects ts_ as supporting_id (CLAIM_BEARING_TYPES enforcement).
  14. Cascade — retract source ob_ → ts_ is tainted.
  15. Cascade — supersede source ob_ → ts_ is staled.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_trust_tier_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TESTS_DIR = str(os.path.dirname(os.path.abspath(__file__)))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_observation
import engram_query
import engram_revision
import engram_trust


def _fresh_mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_trust)
    for name in [
        "engram_observation",
        "engram_revision",
        "engram_derive",
        "engram_query",
    ]:
        importlib.import_module(name)
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core


def _mods():
    tru = sys.modules["engram_trust"]
    obs = sys.modules["engram_observation"]
    rev = sys.modules["engram_revision"]
    drv = sys.modules["engram_derive"]
    qry = sys.modules["engram_query"]
    core = sys.modules["engram_core"]
    return tru, obs, rev, drv, qry, core


def _mk_person(tru, name: str = "TestPerson") -> str:
    r = json.loads(tru._add_person_impl(name=name, role="test subject"))
    assert "person_id" in r, r
    return r["person_id"]


def _mk_evidence_file(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="engram_tt_ev_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _mk_obs(obs, claim: str = "Test claim.") -> str:
    ev_file = _mk_evidence_file(claim + "\n")
    r = json.loads(obs._add_observation_impl(
        url=f"file://{ev_file}",
        title="Trust-tier test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test: {claim}",
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"Failed to create obs: {r}"
    return r["observation_id"]


# ── Test 3: set_trust_tier — below threshold ───────────────────────────────

def test_set_trust_tier_below_threshold():
    """known_external / unknown / suspect require no approval."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "ExternalPerson")

    for tier in ("known_external", "suspect", "unknown"):
        r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier=tier))
        assert r.get("status") in ("set", "no_op"), f"Expected set or no_op for tier '{tier}', got: {r}"
        if r["status"] == "set":
            assert r["to_tier"] == tier, r


# ── Test 4: set_trust_tier — at/above threshold without approval ───────────

def test_set_trust_tier_above_threshold_without_approval_contains_hidden_param():
    """Attempting to set our_side/user_family without approval returns an error
    that explicitly names 'primary_user_approval_obtained'."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "InternalAgent")

    for tier in ("our_side", "user_family"):
        r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier=tier))
        assert "error" in r, f"Expected error for tier '{tier}' without approval, got: {r}"
        assert "primary_user_approval_obtained" in r["error"], (
            f"Error message for tier '{tier}' must contain literal "
            f"'primary_user_approval_obtained'. Got:\n{r['error']}"
        )


# ── Test 5: set_trust_tier — at/above threshold with approval ─────────────

def test_set_trust_tier_above_threshold_with_approval():
    """our_side with justification + approval flag succeeds and writes edit_history."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "OurAgent")
    ob_id = _mk_obs(obs, "Primary user approved OurAgent to our_side tier.")

    r = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_id,
        tier="our_side",
        justification_obs_id=ob_id,
        primary_user_approval_obtained=True,
    ))
    assert r.get("status") == "set", r
    assert r["to_tier"] == "our_side", r
    assert r["from_tier"] == "unknown", r
    assert r.get("edit_history_id") is not None, r

    hist = json.loads(qry._history_impl(mode="edits", action="trust_tier_set", node_id=pn_id))
    assert hist["shown"] >= 1, f"Expected edit_history row, got: {hist}"
    entry = hist["entries"][0]
    assert entry["action"] == "trust_tier_set"
    assert entry["node_id"] == pn_id
    details = entry["details"]
    assert details["from_tier"] == "unknown"
    assert details["to_tier"] == "our_side"
    assert details["justification_obs_id"] == ob_id
    assert details["primary_user_approval_obtained"] is True

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    conn.close()
    assert row["trust_tier"] == "our_side", row


# ── Test 6: set_trust_tier — primary_user tier (approval-gated) ───────────

def test_set_trust_tier_primary_user_approval_gate():
    """'primary_user' is now an assignable tier (rank 5). Without approval it must
    hit the approval gate error, NOT a structural rejection."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "PrimaryUserGateTest")

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="primary_user"))
    assert "error" in r, r
    assert "primary_user_approval_obtained" in r["error"], (
        f"primary_user without approval should hit approval-gate error naming "
        f"'primary_user_approval_obtained'. Got:\n{r['error']}"
    )
    assert "structural" not in r["error"].lower() or "structural-honesty" in r["error"].lower(), (
        f"primary_user is now assignable; error must not claim it is structural. "
        f"Got:\n{r['error']}"
    )


# ── Test 7: set_trust_tier — invalid tier value ────────────────────────────

def test_set_trust_tier_invalid_tier():
    """Unrecognized tier values are rejected with an allowlist error listing all 7 tiers."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "TierTest")

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="totally_made_up_tier"))
    assert "error" in r, r
    assert "totally_made_up_tier" in r["error"] or "tier" in r["error"].lower(), r
    assert "primary_user' is structural" not in r["error"], (
        f"Invalid-tier error must not claim primary_user is structural. Got:\n{r['error']}"
    )


# ── Test 8: set_trust_tier — invalid justification_obs_id ─────────────────

def test_set_trust_tier_invalid_justification_obs_id():
    """Three sub-cases: non-existent, wrong type, retracted."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "ApprovalValidation")

    # 8a: non-existent obs
    r = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_id,
        tier="our_side",
        justification_obs_id="ob_9999",
        primary_user_approval_obtained=True,
    ))
    assert "error" in r and "ob_9999" in r["error"], r

    # 8b: wrong type (create a person node and use its id as justification)
    pn2_id = _mk_person(tru, "WrongTypeNode")
    r2 = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_id,
        tier="our_side",
        justification_obs_id=pn2_id,
        primary_user_approval_obtained=True,
    ))
    assert "error" in r2, r2
    assert "observation" in r2["error"].lower() or pn2_id in r2["error"], r2

    # 8c: retracted obs
    ob_id = _mk_obs(obs, "Approval obs to be retracted.")
    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="fabricated_quote",
        reason="Test retract for validation check.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert ret.get("status") == "retracted", ret

    r3 = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_id,
        tier="our_side",
        justification_obs_id=ob_id,
        primary_user_approval_obtained=True,
    ))
    assert "error" in r3, r3
    assert "retracted" in r3["error"].lower(), r3


# ── Test 9: set_trust_tier — same-tier no-op ──────────────────────────────

def test_set_trust_tier_same_tier_noop():
    """Setting to the current tier is a no-op; no edit_history row is written."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "NoOpPerson")

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="unknown"))
    assert r.get("status") == "no_op", r

    hist = json.loads(qry._history_impl(mode="edits", action="trust_tier_set", node_id=pn_id))
    assert hist["shown"] == 0, f"Expected 0 edit_history rows for no-op, got {hist['shown']}"


# ── Test 10: engram_add_person — default tier ─────────────────────────────

def test_add_person_default_trust_tier():
    """New pn_ created via engram_add_person gets trust_tier='unknown'."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "DefaultTierTest")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
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
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "SignalSubject")
    ob_id = _mk_obs(obs, "Observed alignment behavior.")

    r = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=ob_id,
        kind="alignment",
        polarity=0.7,
        weight=0.5,
        claim="Subject showed alignment signal — mild positive, moderate weight.",
    ))
    assert r.get("status") == "created", r
    ts_id = r.get("trust_signal_id")
    assert ts_id is not None and ts_id.startswith("ts_"), r

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    ts_row = conn.execute("SELECT * FROM nodes WHERE id = ?", (ts_id,)).fetchone()
    assert ts_row is not None, "ts_ node not found in DB"
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
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    ob_id = _mk_obs(obs, "Valid obs.")

    r = json.loads(tru._add_trust_signal_impl(
        subject_pn="pn_9999",
        source_obs_id=ob_id,
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Test signal.",
    ))
    assert "error" in r and "pn_9999" in r["error"], r

    r2 = json.loads(tru._add_trust_signal_impl(
        subject_pn=ob_id,
        source_obs_id=ob_id,
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Test signal.",
    ))
    assert "error" in r2 and "person" in r2["error"].lower(), r2


# ── Test 13: engram_add_trust_signal — invalid source_obs_id ──────────────

def test_add_trust_signal_invalid_source_obs_id():
    """Non-existent, wrong-type, or retracted source_obs_id is rejected."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "SignalSubject2")

    r = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id="ob_9999",
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Test.",
    ))
    assert "error" in r and "ob_9999" in r["error"], r

    r2 = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=pn_id,
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Test.",
    ))
    assert "error" in r2 and "observation" in r2["error"].lower(), r2

    ob_id = _mk_obs(obs, "Obs to retract.")
    json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="fabricated_quote",
        reason="Test.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    r3 = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=ob_id,
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Test.",
    ))
    assert "error" in r3 and "retracted" in r3["error"].lower(), r3


# ── Test 14: engram_add_trust_signal — polarity out of range ──────────────

def test_add_trust_signal_polarity_out_of_range():
    """polarity outside [-1.0, 1.0] is rejected."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "PolarityTest")
    ob_id = _mk_obs(obs, "Obs for polarity test.")

    for bad_polarity in (1.1, -1.1, 2.0, -99.0):
        r = json.loads(tru._add_trust_signal_impl(
            subject_pn=pn_id,
            source_obs_id=ob_id,
            kind="alignment",
            polarity=bad_polarity,
            weight=0.5,
            claim="Out of range polarity test.",
        ))
        assert "error" in r, f"Expected error for polarity={bad_polarity}, got: {r}"
        assert "polarity" in r["error"].lower(), r


# ── Test 15: engram_derive rejects ts_ as supporting_id ───────────────────

def test_derive_rejects_ts_as_supporting_id():
    """CLAIM_BEARING_TYPES auto-rejects trust_signal as a derivation premise."""
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "SignalForDerive")
    ob_id = _mk_obs(obs, "Source for trust signal.")

    ts_r = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=ob_id,
        kind="alignment",
        polarity=0.5,
        weight=0.5,
        claim="Alignment signal.",
    ))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    ob2_id = _mk_obs(obs, "Second premise for derive.")
    r = json.loads(drv._derive_impl(
        claim="Derived claim using ts_ as premise.",
        supporting_ids=f"{ts_id},{ob2_id}",
        logical_chain="ts_ is a premise (should be rejected).",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" in r, f"Expected error when using ts_ as derive premise, got: {r}"
    assert ts_id in r["error"], r


# ── Test 16: Cascade — retract source ob_ does NOT taint ts_ (#2252 Stage 1.3) ──

def test_retract_source_obs_does_not_taint_trust_signal():
    """Retracting the source ob_ does NOT taint the derived ts_ node.

    #2252 Stage 1.3: `trust_signal` is NCB, and `_walk_cascade_downstream`'s
    skip rule is now derived from the CB/NCB partition (`core._is_claim_bearing`)
    rather than a hand-written {cornerstone, lesson} tuple — the walk stops
    at the trust_signal by partition membership before writing tainted_by,
    the same way it stops at a cornerstone or lesson. This inverts this
    test's pre-1.3 assertion, which is exactly the W0 doc's flagged
    trust_signal cascade tension that Stage 1.3 resolves (Kepler's
    corrected #2281 ruling, point 1, 2026-08-27T00:46:05Z).
    """
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "CascadeSubject")
    ob_id = _mk_obs(obs, "Observation that will be retracted.")

    ts_r = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=ob_id,
        kind="friction",
        polarity=-0.5,
        weight=0.8,
        claim="Friction signal derived from the obs.",
    ))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    ret_r = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="fabricated_quote",
        reason="Test retract to verify the walk stops at ts_.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert ret_r.get("status") == "retracted", ret_r

    # Migrated off metadata.tainted_by to cascade_markers (#2013/#2009
    # slice 1) — ts_ should carry a pending retract-severity marker rooted
    # at ob_id.
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    marker = conn.execute(
        "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
        "AND root_op = 'retract' AND status = 'pending'",
        (ts_id, ob_id),
    ).fetchone()
    conn.close()
    # TEST INTENT (decided 2026-08-28, #2009 slice-1 integration): dev's
    # SEMANTICS win, this branch's STORAGE wins. The pre-Stage-1.3 version
    # of this branch asserted a marker was PRESENT; #2252 Stage 1.3 then
    # shipped the derived-skip, so _walk_cascade_downstream now returns at
    # the NCB trust_signal before any marker is written. Asserting presence
    # would re-assert behaviour the merged code no longer has — and would
    # contradict this test's own name and docstring. So: absence, checked
    # against cascade_markers rather than metadata, because metadata is no
    # longer where cascade state lives.
    assert marker is None, (
        f"ts_ {ts_id} should carry NO cascade marker — trust_signal is NCB "
        f"and the derived skip must stop the walk there before marking."
    )


# ── Test 17: Cascade — supersede source ob_ does NOT stale ts_ (#2252 Stage 1.3) ──

def test_supersede_source_obs_does_not_stale_trust_signal():
    """Superseding the source ob_ does NOT mark the derived ts_ as stale.

    #2252 Stage 1.3: same derived-skip reasoning as
    test_retract_source_obs_does_not_taint_trust_signal above — the walk
    stops at the NCB trust_signal node before writing stale_by.
    """
    _fresh_mods()
    tru, obs, rev, drv, qry, core = _mods()
    pn_id = _mk_person(tru, "SupersedeSubject")
    ob_old = _mk_obs(obs, "Original observation about subject.")
    ob_new = _mk_obs(obs, "Refined observation replacing the original.")

    ts_r = json.loads(tru._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=ob_old,
        kind="care",
        polarity=0.9,
        weight=0.6,
        claim="Care signal from original observation.",
    ))
    assert ts_r.get("status") == "created", ts_r
    ts_id = ts_r["trust_signal_id"]

    sup_r = json.loads(rev._supersede_impl(
        old_node_id=ob_old,
        new_node_id=ob_new,
        supersede_reason="Refined claim — test supersede stops at ts_.",
    ))
    assert sup_r.get("status") == "superseded", sup_r

    # Migrated off metadata.stale_by to cascade_markers (#2013/#2009
    # slice 1) — ts_ should carry a pending supersede-severity marker
    # rooted at ob_old.
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    marker = conn.execute(
        "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
        "AND root_op = 'supersede' AND status = 'pending'",
        (ts_id, ob_old),
    ).fetchone()
    conn.close()
    # TEST INTENT (decided 2026-08-28): same call as the retract twin above
    # — dev's absence semantics (#2252 Stage 1.3 derived-skip) over this
    # branch's presence assertion, expressed through the cascade_markers
    # store this slice migrates to.
    assert marker is None, (
        f"ts_ {ts_id} should carry NO cascade marker — trust_signal is NCB "
        f"and the derived skip must stop the walk there before marking."
    )
