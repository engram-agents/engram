"""Tests for alpha#1825 — one gate for trust_tier (close the generic writers).

engram_update_person and engram_add_person used to let trust_tier through as
an ordinary mutable/creation field, bypassing engram_set_trust_tier's
primary-user approval gate (and the is_self singleton gate for tier='self').
This file locks down the closed gate: both generic writers now refuse any
trust_tier touch and name engram_set_trust_tier as the remedy, while
engram_set_trust_tier itself is UNCHANGED (positive control).

Coverage:
  1. update_person -> trust_tier='primary_user' refused (promotion attempt).
  2. update_person -> trust_tier='self' refused (promotion attempt, node has
     no is_self=true).
  3. update_person -> trust_tier='known_external' refused -- a DEMOTION from
     an already-approved 'our_side' tier. One gate for every change, up or
     down.
  4. update_person with {trust_tier, pronouns} in the same call changes
     NEITHER field (all-or-nothing).
  5. add_person at primary_user / user_family / our_side -> refused, no node
     created.
  6. add_person at trust_tier='self' (with or without is_self=True) ->
     refused, no node created; remedy names add_person(is_self=True) then
     engram_set_trust_tier(tier='self').
  7. add_person at known_external / unknown / suspect / empty -> created
     with that tier exactly as before.
  8. Positive control: engram_set_trust_tier still promotes with approval +
     justification, and still refuses without.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_trust_tier_one_gate_")
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


def _mk_person(tru, name: str = "TestPerson", is_self: bool = False,
                trust_tier: str = "") -> str:
    payload: dict = {"name": name, "role": "test subject"}
    if is_self:
        payload["is_self"] = True
    if trust_tier:
        payload["trust_tier"] = trust_tier
    r = json.loads(tru._add_person_impl(**payload))
    assert "person_id" in r, r
    return r["person_id"]


def _mk_evidence_file(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="engram_tt1g_ev_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _mk_obs(obs, claim: str = "Test claim.") -> str:
    ev_file = _mk_evidence_file(claim + "\n")
    r = json.loads(obs._add_observation_impl(
        url=f"file://{ev_file}",
        title="Trust-tier-one-gate test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test: {claim}",
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"Failed to create obs: {r}"
    return r["observation_id"]


def _row(core, node_id: str):
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT metadata, trust_tier FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    conn.close()
    return row


def _person_count_by_name(core, name: str) -> int:
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type = 'person' AND claim LIKE ?",
        (f"{name}%",),
    ).fetchone()["c"]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# §1 — engram_update_person: trust_tier refused (promotion + demotion)
# ---------------------------------------------------------------------------

def test_update_person_trust_tier_to_primary_user_refused():
    """update_person -> trust_tier='primary_user' is refused; DB unchanged."""
    _fresh_mods()
    tru, obs, core = _mods()
    pid = _mk_person(tru, "PrimaryUserCandidate")

    before = _row(core, pid)
    assert before["trust_tier"] == "unknown", before

    r = json.loads(tru._update_person_impl(
        node_id=pid, updates={"trust_tier": "primary_user"},
    ))
    assert "error" in r, f"Expected refusal, got: {r}"
    assert "engram_set_trust_tier" in r["error"], (
        f"Error must name engram_set_trust_tier as the remedy. Got:\n{r['error']}"
    )

    after = _row(core, pid)
    assert after["trust_tier"] == "unknown", after
    meta = json.loads(after["metadata"])
    assert "trust_tier" not in meta, meta


def test_update_person_trust_tier_to_self_refused():
    """update_person -> trust_tier='self' is refused on a non-self-anchor node."""
    _fresh_mods()
    tru, obs, core = _mods()
    pid = _mk_person(tru, "NotTheSelfAnchor", is_self=False)

    r = json.loads(tru._update_person_impl(
        node_id=pid, updates={"trust_tier": "self"},
    ))
    assert "error" in r, f"Expected refusal, got: {r}"
    assert "engram_set_trust_tier" in r["error"], r["error"]

    after = _row(core, pid)
    assert after["trust_tier"] == "unknown", after


def test_update_person_trust_tier_demotion_to_known_external_refused():
    """update_person -> trust_tier='known_external' (a DEMOTION from an
    already-approved 'our_side') is refused exactly like a promotion. One
    gate for every trust_tier change, up or down."""
    _fresh_mods()
    tru, obs, core = _mods()
    pid = _mk_person(tru, "AlreadyOurSide")
    claim = "Primary user approved AlreadyOurSide to our_side."
    ob_id = _mk_obs(obs, claim)

    promo = json.loads(tru._set_trust_tier_impl(
        target_pn=pid, tier="our_side",
        justification_obs_id=ob_id, primary_user_approval_obtained=True,
    ))
    assert promo.get("status") == "set", f"Setup promotion failed: {promo}"
    assert _row(core, pid)["trust_tier"] == "our_side"

    r = json.loads(tru._update_person_impl(
        node_id=pid, updates={"trust_tier": "known_external"},
    ))
    assert "error" in r, f"Expected refusal on demotion, got: {r}"
    assert "engram_set_trust_tier" in r["error"], r["error"]

    after = _row(core, pid)
    assert after["trust_tier"] == "our_side", (
        f"Demotion must not apply -- expected unchanged 'our_side', got: {after}"
    )


def test_update_person_trust_tier_with_other_field_changes_neither():
    """updates={trust_tier, pronouns} in one call: refusal applies to the
    WHOLE call -- pronouns is not applied either (all-or-nothing)."""
    _fresh_mods()
    tru, obs, core = _mods()
    pid = _mk_person(tru, "ComboFieldPerson")

    r = json.loads(tru._update_person_impl(
        node_id=pid,
        updates={"trust_tier": "known_external", "pronouns": "she/her"},
    ))
    assert "error" in r, f"Expected refusal, got: {r}"
    assert "engram_set_trust_tier" in r["error"], r["error"]

    after = _row(core, pid)
    assert after["trust_tier"] == "unknown", after
    meta = json.loads(after["metadata"])
    assert "pronouns" not in meta, (
        f"pronouns must NOT be applied when trust_tier is in the same call: {meta}"
    )
    assert meta["field_traces"] == {}, (
        f"No field_traces entry should be written on a refused call: {meta['field_traces']}"
    )


# ---------------------------------------------------------------------------
# §2 — engram_add_person: internal-threshold tiers refused at creation
# ---------------------------------------------------------------------------

def test_add_person_internal_tiers_refused_no_node_created():
    """add_person at our_side / user_family / primary_user is refused; no
    node is created for any of them."""
    _fresh_mods()
    tru, obs, core = _mods()

    for tier in ("our_side", "user_family", "primary_user"):
        name = f"Refused_{tier}"
        r = json.loads(tru._add_person_impl(name=name, trust_tier=tier))
        assert "error" in r, f"tier={tier} should be refused, got: {r}"
        assert "engram_set_trust_tier" in r["error"], (
            f"tier={tier} error must name engram_set_trust_tier. Got:\n{r['error']}"
        )
        assert "person_id" not in r, r
        assert _person_count_by_name(core, name) == 0, (
            f"No node should have been created for tier={tier}"
        )


def test_add_person_self_tier_refused_even_with_is_self_true():
    """add_person at trust_tier='self' is refused regardless of is_self --
    remedy is add_person(is_self=True) THEN engram_set_trust_tier(tier='self')."""
    _fresh_mods()
    tru, obs, core = _mods()

    # Without is_self at all.
    r1 = json.loads(tru._add_person_impl(name="SelfRefused1", trust_tier="self"))
    assert "error" in r1, r1
    assert "is_self=True" in r1["error"] or "is_self" in r1["error"], r1["error"]
    assert _person_count_by_name(core, "SelfRefused1") == 0

    # With is_self=True -- still refused; is_self alone does not grant the
    # tier at creation time.
    r2 = json.loads(tru._add_person_impl(
        name="SelfRefused2", trust_tier="self", is_self=True,
    ))
    assert "error" in r2, r2
    assert "engram_set_trust_tier" in r2["error"], r2["error"]
    assert _person_count_by_name(core, "SelfRefused2") == 0


def test_add_person_external_tiers_still_created():
    """known_external / unknown / suspect / empty all still work exactly as
    before -- not touched by the new gate."""
    _fresh_mods()
    tru, obs, core = _mods()

    for tier in ("known_external", "unknown", "suspect"):
        name = f"Allowed_{tier}"
        r = json.loads(tru._add_person_impl(name=name, trust_tier=tier))
        assert "error" not in r, f"tier={tier} should still be allowed, got: {r}"
        pid = r["person_id"]
        row = _row(core, pid)
        assert row["trust_tier"] == tier, row
        meta = json.loads(row["metadata"])
        assert meta["trust_tier"] == tier, meta

    # Empty trust_tier -> defaults to 'unknown', unchanged behaviour.
    r_empty = json.loads(tru._add_person_impl(name="NoTierGiven"))
    assert "error" not in r_empty, r_empty
    row_empty = _row(core, r_empty["person_id"])
    assert row_empty["trust_tier"] == "unknown", row_empty


def test_add_person_invalid_tier_name_keeps_today_error():
    """An invalid tier name at add_person time keeps today's allowlist
    error -- the new threshold check does not change this path."""
    _fresh_mods()
    tru, obs, core = _mods()

    r = json.loads(tru._add_person_impl(name="BogusTierPerson", trust_tier="not_a_real_tier"))
    assert "error" in r, r
    assert "Invalid trust_tier" in r["error"], r["error"]
    assert _person_count_by_name(core, "BogusTierPerson") == 0


# ---------------------------------------------------------------------------
# §3 — Positive control: engram_set_trust_tier is UNCHANGED
# ---------------------------------------------------------------------------

def test_set_trust_tier_still_promotes_with_approval_and_still_refuses_without():
    """engram_set_trust_tier is the one remaining gate: still refuses an
    internal-tier promotion without approval, still succeeds with
    approval + justification."""
    _fresh_mods()
    tru, obs, core = _mods()
    pid = _mk_person(tru, "SetTierControlPerson")

    without = json.loads(tru._set_trust_tier_impl(target_pn=pid, tier="our_side"))
    assert "error" in without, f"Expected approval-gate error, got: {without}"
    assert "primary_user_approval_obtained" in without["error"], without["error"]
    assert _row(core, pid)["trust_tier"] == "unknown"

    ob_id = _mk_obs(obs, "Primary user approved SetTierControlPerson to our_side.")
    withit = json.loads(tru._set_trust_tier_impl(
        target_pn=pid, tier="our_side",
        justification_obs_id=ob_id, primary_user_approval_obtained=True,
    ))
    assert withit.get("status") == "set", f"Expected status='set', got: {withit}"
    assert _row(core, pid)["trust_tier"] == "our_side"
