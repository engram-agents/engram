"""Tests for extended trust-tier tiers: self (rank 6) + primary_user (rank 5).

Coverage (9 tests):
  1.  test_set_tier_self_on_is_self_anchor_succeeds
      — pn_* with is_self=true accepts tier='self' without approval gate.
  2.  test_set_tier_self_on_non_self_node_rejected
      — pn_* without is_self=true is rejected when setting tier='self'.
  3.  test_set_tier_self_when_another_self_exists_rejected
      — A second pn_* (even with is_self=true) is rejected if another already
        holds tier='self'.
  4.  test_set_tier_primary_user_approval_required
      — First attempt without approval returns friction message naming
        'primary_user_approval_obtained'.
  5.  test_set_tier_primary_user_succeeds_with_approval_and_justification
      — With approval + justification, tier='primary_user' is set successfully.
  6.  test_set_tier_primary_user_multiple_allowed
      — Two distinct pn_* nodes may both hold tier='primary_user' simultaneously.
  7.  test_tier_rank_ordering_self_above_primary_user_above_family
      — TIER_RANK constants are in the expected order (6 > 5 > 4).
  8.  test_invalid_tier_error_lists_all_seven
      — Invalid tier error message mentions all 7 valid tier names without a
        'not assignable' trailer.
  9.  test_set_tier_self_succeeds_after_self_anchor_supersede
      — A superseded pn_X (is_current=0, trust_tier='self') must NOT block
        engram_set_trust_tier(tier='self') on the live successor pn_Y. Documents
        the supersede-time invariant: the supersede pathway does not clear
        trust_tier; only is_current flips. The live-tool gate must filter to
        is_current=1 only.
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
import uuid

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_trust_tier_v2_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TESTS_DIR = str(os.path.dirname(os.path.abspath(__file__)))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_observation
import engram_trust


def _fresh_mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_trust)
    importlib.import_module("engram_observation")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core


def _mods():
    tru = sys.modules["engram_trust"]
    obs = sys.modules["engram_observation"]
    core = sys.modules["engram_core"]
    return tru, obs, core


def _mk_person(tru, name: str = "TestPerson", is_self: bool = False) -> str:
    payload: dict = {"name": name, "role": "test subject"}
    if is_self:
        payload["is_self"] = True
    r = json.loads(tru._add_person_impl(**payload))
    assert "person_id" in r, r
    return r["person_id"]


def _mk_evidence_file(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="engram_tt2_ev_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _mk_obs(obs, claim: str = "Test claim.") -> str:
    ev_file = _mk_evidence_file(claim + "\n")
    r = json.loads(obs._add_observation_impl(
        url=f"file://{ev_file}",
        title="Trust-tier-v2 test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test: {claim}",
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"Failed to create obs: {r}"
    return r["observation_id"]


# ── Test 1: self tier on is_self anchor succeeds ──────────────────────────

def test_set_tier_self_on_is_self_anchor_succeeds():
    """pn_* with is_self=true accepts tier='self' without the standard approval gate."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_id = _mk_person(tru, "SelfAnchor", is_self=True)

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="self"))
    assert r.get("status") == "set", f"Expected status='set', got: {r}"
    assert r["to_tier"] == "self", r
    assert r["from_tier"] == "unknown", r
    assert r.get("edit_history_id") is not None, r

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    conn.close()
    assert row["trust_tier"] == "self", row


# ── Test 2: self tier on non-self node rejected ──────────────────────────

def test_set_tier_self_on_non_self_node_rejected():
    """pn_* without is_self=true in metadata is rejected when setting tier='self'."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_id = _mk_person(tru, "NormalPerson", is_self=False)

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="self"))
    assert "error" in r, f"Expected error for non-self node, got: {r}"
    assert "is_self" in r["error"], (
        f"Error must mention 'is_self'. Got:\n{r['error']}"
    )


# ── Test 3: self tier when another self exists rejected ───────────────────

def test_set_tier_self_when_another_self_exists_rejected():
    """Setting tier='self' is rejected if any other pn_* already holds tier='self'."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_first = _mk_person(tru, "FirstSelf", is_self=True)
    r1 = json.loads(tru._set_trust_tier_impl(target_pn=pn_first, tier="self"))
    assert r1.get("status") == "set", f"First self-tier set failed: {r1}"

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO nodes (id, type, claim, created_at, status, metadata, trust_tier, is_current)
           VALUES ('pn_9999', 'person', 'Second self — test only', datetime('now'),
                   'active', ?, 'unknown', 1)""",
        (json.dumps({"name": "SecondSelf", "role": "test", "is_self": True}),),
    )
    conn.commit()
    conn.close()

    r2 = json.loads(tru._set_trust_tier_impl(target_pn="pn_9999", tier="self"))
    assert "error" in r2, f"Expected error for second self-tier, got: {r2}"
    assert pn_first in r2["error"] or "already" in r2["error"].lower(), (
        f"Error should mention existing self-tier holder '{pn_first}'. Got:\n{r2['error']}"
    )


# ── Test 4: primary_user approval required ────────────────────────────────

def test_set_tier_primary_user_approval_required():
    """Setting tier='primary_user' without approval returns the friction message
    naming 'primary_user_approval_obtained'."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_id = _mk_person(tru, "PrimaryUserCandidate")

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="primary_user"))
    assert "error" in r, f"Expected approval-gate error, got: {r}"
    assert "primary_user_approval_obtained" in r["error"], (
        f"Friction message must name 'primary_user_approval_obtained'. Got:\n{r['error']}"
    )


# ── Test 5: primary_user succeeds with approval + justification ───────────

def test_set_tier_primary_user_succeeds_with_approval_and_justification():
    """tier='primary_user' with approval flag + valid justification obs is accepted."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_id = _mk_person(tru, "PrimaryUser")
    claim = "Primary user approval granted for PrimaryUser."
    ob_id = _mk_obs(obs, claim)

    r = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_id,
        tier="primary_user",
        justification_obs_id=ob_id,
        primary_user_approval_obtained=True,
    ))
    assert r.get("status") == "set", f"Expected status='set', got: {r}"
    assert r["to_tier"] == "primary_user", r
    assert r["from_tier"] == "unknown", r
    assert r.get("edit_history_id") is not None, r

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pn_id,)).fetchone()
    conn.close()
    assert row["trust_tier"] == "primary_user", row


# ── Test 6: primary_user multiple allowed ─────────────────────────────────

def test_set_tier_primary_user_multiple_allowed():
    """Two distinct pn_* nodes may simultaneously hold tier='primary_user'."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_a = _mk_person(tru, "PrimaryUserA")
    pn_b = _mk_person(tru, "PrimaryUserB")
    ob_a = _mk_obs(obs, "Primary user approval granted for PrimaryUserA.")
    ob_b = _mk_obs(obs, "Primary user approval granted for PrimaryUserB.")

    r_a = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_a,
        tier="primary_user",
        justification_obs_id=ob_a,
        primary_user_approval_obtained=True,
    ))
    assert r_a.get("status") == "set", f"First primary_user failed: {r_a}"

    r_b = json.loads(tru._set_trust_tier_impl(
        target_pn=pn_b,
        tier="primary_user",
        justification_obs_id=ob_b,
        primary_user_approval_obtained=True,
    ))
    assert r_b.get("status") == "set", (
        f"Second primary_user should succeed (multiple allowed). Got: {r_b}"
    )

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE trust_tier = 'primary_user'"
    ).fetchone()["c"]
    conn.close()
    assert count == 2, f"Expected 2 nodes with tier='primary_user', got {count}"


# ── Test 7: tier rank ordering ────────────────────────────────────────────

def test_tier_rank_ordering_self_above_primary_user_above_family():
    """TIER_RANK has self > primary_user > user_family (6 > 5 > 4).
    Wave 3: TIER_RANK moved to engram_trust; test migrated to family-module namespace.
    """
    _fresh_mods()
    tru, obs, core = _mods()
    assert tru.TIER_RANK["self"] == 6, tru.TIER_RANK
    assert tru.TIER_RANK["primary_user"] == 5, tru.TIER_RANK
    assert tru.TIER_RANK["user_family"] == 4, tru.TIER_RANK
    assert tru.TIER_RANK["self"] > tru.TIER_RANK["primary_user"] > tru.TIER_RANK["user_family"], (
        f"Expected self > primary_user > user_family. TIER_RANK: {tru.TIER_RANK}"
    )


# ── Test 8: invalid tier error lists all seven ────────────────────────────

def test_invalid_tier_error_lists_all_seven():
    """The invalid-tier error message lists all 7 valid tiers and does NOT
    carry a 'primary_user is structural / not assignable' trailer."""
    _fresh_mods()
    tru, obs, core = _mods()
    pn_id = _mk_person(tru, "InvalidTierTest")

    r = json.loads(tru._set_trust_tier_impl(target_pn=pn_id, tier="bogus_tier_xyz"))
    assert "error" in r, r

    err = r["error"]
    all_seven = ["self", "primary_user", "user_family", "our_side",
                 "known_external", "unknown", "suspect"]
    for t in all_seven:
        assert t in err, (
            f"Invalid-tier error must list all 7 tiers. Missing '{t}'. Got:\n{err}"
        )

    assert "primary_user' is structural" not in err, (
        f"Invalid-tier error must not claim primary_user is structural. Got:\n{err}"
    )
    assert "not assignable" not in err, (
        f"Invalid-tier error must not carry 'not assignable' trailer. Got:\n{err}"
    )


# ── Test 9: self-tier succeeds after self-anchor supersede ────────────────────

def test_set_tier_self_succeeds_after_self_anchor_supersede():
    """Superseded pn_X (is_current=0, trust_tier='self') must NOT block
    engram_set_trust_tier(tier='self') on its live successor pn_Y.

    Context: the supersede pathway flips is_current=0 but does NOT clear the
    trust_tier column. Without the is_current=1 guard in the singleton gate,
    the dead pn_X row is found and the call to assign tier='self' to pn_Y is
    falsely rejected. This is the live-tool sibling of the migration plan()
    bug fixed in round-2. (PR #444 Ari blocker)

    Invariant documented here: the supersede pathway does NOT clear trust_tier.
    That is a separate concern for a future PR; this PR makes the live tool
    resilient by querying only is_current=1 rows.
    """
    _fresh_mods()
    tru, obs, core = _mods()

    # Step 1: create pn_X as the self-anchor and assign tier='self'
    pn_x = _mk_person(tru, "OldSelfAnchor", is_self=True)
    r_x = json.loads(tru._set_trust_tier_impl(target_pn=pn_x, tier="self"))
    assert r_x.get("status") == "set", f"Initial self-tier set on pn_X failed: {r_x}"

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Step 2: manually INSERT pn_Y — new live self-anchor (is_self=true, is_current=1).
    # Direct SQL bypasses the API singleton guard in engram_add_person which would
    # reject a second is_self=true node while pn_X is still is_current=1.
    pn_y_id = "pn_" + uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO nodes (id, type, claim, created_at, status, metadata,
                              trust_tier, is_current)
           VALUES (?, 'person', 'New self-anchor — test only', datetime('now'),
                   'active', ?, 'unknown', 1)""",
        (pn_y_id, json.dumps({"name": "NewSelfAnchor", "role": "test", "is_self": True})),
    )
    conn.commit()

    # Step 3: mark pn_X as superseded — flip is_current=0 but intentionally do
    # NOT clear trust_tier (mirrors actual supersede pathway behaviour).
    conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (pn_x,))
    conn.commit()
    conn.close()

    # Step 4: call engram_set_trust_tier on pn_Y — must succeed.
    # The dead pn_X row (is_current=0, trust_tier='self') must not block this.
    r_y = json.loads(tru._set_trust_tier_impl(target_pn=pn_y_id, tier="self"))
    assert r_y.get("status") == "set", (
        f"Expected tier='self' to succeed on pn_Y after pn_X superseded. Got: {r_y}"
    )
    assert r_y["to_tier"] == "self", r_y

    # Step 5: verify pn_Y holds tier='self' in DB
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row_y = conn2.execute(
        "SELECT trust_tier, is_current FROM nodes WHERE id = ?", (pn_y_id,)
    ).fetchone()
    conn2.close()
    assert row_y["trust_tier"] == "self", (
        f"pn_Y should have trust_tier='self', got '{row_y['trust_tier']}'"
    )
    assert row_y["is_current"] == 1, "pn_Y should still be is_current=1"
