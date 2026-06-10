"""Tests for stale_replacement dict-keyed migration (issue #231).

Covers:
  1. Writers emit dict form after supersede.
  2. Legacy scalar on disk is upgraded to dict on next write.
  3. Multiple cascades preserve all replacement entries (the #231 load-bearing bug).
  4. Reader (engram_reflect) handles dict-form, scalar-form, and absent correctly.
  5. Contradiction cascade (site 2 from PR #230) also emits dict form.

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision, engram_derive, engram_lifecycle,
    engram_observation, engram_query, and engram_core directly and contains
    no ``import server``. The module objects are passed to fresh_module() (R4)
    for per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_lifecycle
import engram_observation
import engram_query
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_stale_replacement_dict_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_revision) per R4: the module object is
    passed (not a string), so the ``import engram_revision`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (rev, obs, derive, lifecycle, qry, core) — freshly reloaded module
    objects with engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    lifecycle = importlib.import_module("engram_lifecycle")
    qry = importlib.import_module("engram_query")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, lifecycle, qry, core


def _mk_obs(obs, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/test",
        title="Test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test: {claim}",
        quote_type=quote_type,
    ))
    assert "observation_id" in r, f"obs creation failed: {r}"
    return r["observation_id"]


def _mk_dv(derive, claim: str, supporting_ids: list, reasoning_type: str = "deductive_modus_ponens") -> str:
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(supporting_ids),
        logical_chain=f"Reasoning: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _mk_ct(derive, node_a: str, node_b: str, description: str) -> str:
    r = json.loads(derive._contradict_impl(
        node_id_a=node_a,
        node_id_b=node_b,
        description=description,
    ))
    assert "contradiction_id" in r, f"contradict failed: {r}"
    return r["contradiction_id"]


def _get_meta(qry, node_id: str) -> dict:
    """Fetch a node's metadata dict via _inspect_impl."""
    r = json.loads(qry._inspect_impl(node_id=node_id, view="deep", dream_mode=True))
    assert "node" in r, f"_inspect_impl response missing 'node' key — contract changed: {r}"
    raw = r["node"].get("metadata")
    return json.loads(raw) if raw else {}


# ── Test 1: writers emit dict form ─────────────────────────────────────────


def test_stale_replacement_writes_dict_form():
    """Supersede A→B: downstream node's stale_replacement is dict {A_id: B_id}."""
    rev, obs, derive, lifecycle, qry, core = _make_fresh_mods()
    # Build: ob_base → dv_downstream. Supersede ob_base → ob_refined.
    ob_base = _mk_obs(obs, "Base premise.")
    ob_base2 = _mk_obs(obs, "Supporting premise.")
    dv_downstream = _mk_dv(derive, "Downstream claim.", [ob_base, ob_base2])
    ob_refined = _mk_obs(obs, "Refined base premise.")

    r = json.loads(rev._supersede_impl(
        old_node_id=ob_base,
        new_node_id=ob_refined,
    ))
    assert r.get("status") == "superseded", r
    assert dv_downstream in {s["id"] for s in r["stale_downstream"]}

    meta = _get_meta(qry, dv_downstream)
    stale_repl = meta.get("stale_replacement")
    assert isinstance(stale_repl, dict), (
        f"stale_replacement should be dict, got {type(stale_repl).__name__}: {stale_repl!r}"
    )
    assert stale_repl.get(ob_base) == ob_refined, (
        f"expected {{{ob_base}: {ob_refined}}}, got {stale_repl}"
    )


# ── Test 2: legacy scalar is upgraded on next write ────────────────────────


def test_stale_replacement_legacy_scalar_upgrades_on_write():
    """Node with legacy scalar stale_replacement gets upgraded to dict on next cascade write."""
    rev, obs, derive, lifecycle, qry, core = _make_fresh_mods()
    ob_base = _mk_obs(obs, "Premise with legacy data.")
    ob_base2 = _mk_obs(obs, "Second premise.")
    dv_downstream = _mk_dv(derive, "Downstream claim.", [ob_base, ob_base2])

    # Inject a legacy scalar directly into the node's metadata (simulates pre-PR data).
    legacy_scalar = "ob_0099"  # a fake prior replacement ID
    conn = core._get_db()
    try:
        raw = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (dv_downstream,)
        ).fetchone()["metadata"]
        meta = json.loads(raw) if raw else {}
        meta["stale_replacement"] = legacy_scalar  # inject legacy scalar form
        meta["stale_by"] = [ob_base]               # seed stale_by so cascade can append
        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), dv_downstream),
        )
        conn.commit()
    finally:
        conn.close()

    # Trigger a fresh cascade: create a third node that is also downstream.
    ob_base3 = _mk_obs(obs, "Third upstream premise.")
    dv_downstream2 = _mk_dv(derive, "Third downstream claim.", [ob_base, ob_base3])

    # Now supersede ob_base with a new node; this cascade will touch dv_downstream again.
    ob_refined = _mk_obs(obs, "Refined premise after legacy data.")
    r = json.loads(rev._supersede_impl(
        old_node_id=ob_base,
        new_node_id=ob_refined,
    ))
    assert r.get("status") == "superseded", r

    # Verify upgrade happened: scalar → dict with "_legacy" key + new entry.
    meta = _get_meta(qry, dv_downstream)
    stale_repl = meta.get("stale_replacement")
    assert isinstance(stale_repl, dict), (
        f"stale_replacement should be dict after upgrade, got {type(stale_repl).__name__}: {stale_repl!r}"
    )
    assert "_legacy" in stale_repl, (
        f"legacy sentinel key '_legacy' missing; got {stale_repl}"
    )
    assert stale_repl["_legacy"] == legacy_scalar, (
        f"legacy scalar preserved wrong; expected {legacy_scalar!r}, got {stale_repl['_legacy']!r}"
    )
    assert stale_repl.get(ob_base) == ob_refined, (
        f"new entry {ob_base} → {ob_refined} missing; got {stale_repl}"
    )


# ── Test 3: multiple cascades preserve all entries ─────────────────────────


def test_stale_replacement_multiple_cascades_preserve_all():
    """Load-bearing test for issue #231 bug.

    Setup: dv_shared derives from ob_x. ob_x is superseded by ob_y (cascade 1).
    Then we add a second supersede on a new ob_x2 (also a premise of dv_shared)
    by ob_z (cascade 2). Both {ob_x: ob_y} and {ob_x2: ob_z} must survive.
    """
    rev, obs, derive, lifecycle, qry, core = _make_fresh_mods()
    ob_x = _mk_obs(obs, "First premise X.")
    ob_x2 = _mk_obs(obs, "Second premise X2.")
    dv_shared = _mk_dv(derive, "Claim from both premises.", [ob_x, ob_x2])

    # Cascade 1: supersede ob_x → ob_y
    ob_y = _mk_obs(obs, "Refined first premise Y.")
    r1 = json.loads(rev._supersede_impl(
        old_node_id=ob_x,
        new_node_id=ob_y,
    ))
    assert r1.get("status") == "superseded", r1
    assert dv_shared in {s["id"] for s in r1["stale_downstream"]}

    # After cascade 1
    meta1 = _get_meta(qry, dv_shared)
    sr1 = meta1.get("stale_replacement")
    assert isinstance(sr1, dict), f"After cascade 1: expected dict, got {type(sr1)}: {sr1!r}"
    assert sr1.get(ob_x) == ob_y, f"After cascade 1: entry {ob_x}→{ob_y} missing: {sr1}"

    # Cascade 2: supersede ob_x2 → ob_z
    ob_z = _mk_obs(obs, "Refined second premise Z.")
    r2 = json.loads(rev._supersede_impl(
        old_node_id=ob_x2,
        new_node_id=ob_z,
    ))
    assert r2.get("status") == "superseded", r2
    assert dv_shared in {s["id"] for s in r2["stale_downstream"]}

    # After cascade 2: BOTH entries must survive (this is the #231 bug).
    meta2 = _get_meta(qry, dv_shared)
    sr2 = meta2.get("stale_replacement")
    assert isinstance(sr2, dict), f"After cascade 2: expected dict, got {type(sr2)}: {sr2!r}"
    assert sr2.get(ob_x) == ob_y, (
        f"After cascade 2: first entry {ob_x}→{ob_y} was clobbered; got {sr2}"
    )
    assert sr2.get(ob_x2) == ob_z, (
        f"After cascade 2: second entry {ob_x2}→{ob_z} missing; got {sr2}"
    )


# ── Test 4: reader handles all three meta forms ────────────────────────────


def test_stale_replacement_reader_handles_both_forms():
    """engram_reflect reader surfaces correct replacement_hint for dict, scalar, absent."""
    rev, obs, derive, lifecycle, qry, core = _make_fresh_mods()

    # Create three downstream nodes; we'll manually set their metadata to test three forms.
    ob_base = _mk_obs(obs, "Reader test base premise.")
    ob_base2 = _mk_obs(obs, "Reader test supporting premise.")
    dv_dict_form = _mk_dv(derive, "Dict-form downstream.", [ob_base, ob_base2])
    dv_scalar_form = _mk_dv(derive, "Scalar-form (legacy) downstream.", [ob_base, ob_base2])
    dv_absent_form = _mk_dv(derive, "No stale_replacement downstream.", [ob_base, ob_base2])

    sentinel_new_id = "ob_8001"  # synthetic replacement target IDs
    sentinel_old_id = "ob_8000"
    scalar_replacement = "ob_9000"

    conn = core._get_db()
    try:
        def _write_meta(node_id, metadata_dict):
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(metadata_dict), node_id),
            )

        # (a) dict form: {sentinel_old_id: sentinel_new_id}
        _write_meta(dv_dict_form, {
            "stale_by": [sentinel_old_id],
            "stale_replacement": {sentinel_old_id: sentinel_new_id},
        })
        # (b) scalar (legacy) form
        _write_meta(dv_scalar_form, {
            "stale_by": [sentinel_old_id],
            "stale_replacement": scalar_replacement,
        })
        # (c) absent form — just stale_by so the node appears in the stale query
        _write_meta(dv_absent_form, {
            "stale_by": [sentinel_old_id],
        })
        conn.commit()
    finally:
        conn.close()

    # Call _reflect_impl to exercise the reader.
    reflect_r = json.loads(lifecycle._reflect_impl())
    stale_nodes = {sn["id"]: sn for sn in reflect_r.get("stale_nodes", [])}

    # (a) dict form: replacement_hint should be the last value in the dict
    assert dv_dict_form in stale_nodes, f"{dv_dict_form} not in stale_nodes"
    entry_a = stale_nodes[dv_dict_form]
    assert entry_a["replacement_hint"] == sentinel_new_id, (
        f"dict-form: expected hint={sentinel_new_id!r}, got {entry_a['replacement_hint']!r}"
    )
    assert entry_a["replacement_hints_all"] == {sentinel_old_id: sentinel_new_id}, (
        f"dict-form hints_all wrong: {entry_a['replacement_hints_all']}"
    )

    # (b) scalar form: replacement_hint should be the scalar value
    assert dv_scalar_form in stale_nodes, f"{dv_scalar_form} not in stale_nodes"
    entry_b = stale_nodes[dv_scalar_form]
    assert entry_b["replacement_hint"] == scalar_replacement, (
        f"scalar-form: expected hint={scalar_replacement!r}, got {entry_b['replacement_hint']!r}"
    )
    assert entry_b["replacement_hints_all"] == {"_legacy": scalar_replacement}, (
        f"scalar-form hints_all wrong: {entry_b['replacement_hints_all']}"
    )

    # (c) absent form: replacement_hint should be None, hints_all should be empty dict
    assert dv_absent_form in stale_nodes, f"{dv_absent_form} not in stale_nodes"
    entry_c = stale_nodes[dv_absent_form]
    assert entry_c["replacement_hint"] is None, (
        f"absent-form: expected hint=None, got {entry_c['replacement_hint']!r}"
    )
    assert entry_c["replacement_hints_all"] == {}, (
        f"absent-form hints_all wrong: {entry_c['replacement_hints_all']}"
    )


# ── Test 5: contradiction cascade (site 2) emits dict form ────────────────


def test_stale_replacement_contradiction_cascade_uses_dict():
    """Supersede a node in a contradiction: the ct's stale_replacement is dict-keyed (site 2)."""
    rev, obs, derive, lifecycle, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A is true.")
    ob_b = _mk_obs(obs, "Claim A is false.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Direct contradiction on A.")

    ob_a_refined = _mk_obs(obs, "Claim A holds with caveat.")
    r = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=ob_a_refined,
        supersede_reason="narrowed claim",
    ))
    assert r.get("status") == "superseded", r
    assert r["stale_contradiction_count"] == 1
    assert r["stale_contradictions"][0]["id"] == ct_id

    ct_meta = _get_meta(qry, ct_id)
    stale_repl = ct_meta.get("stale_replacement")
    assert isinstance(stale_repl, dict), (
        f"ct stale_replacement should be dict (site 2), got {type(stale_repl).__name__}: {stale_repl!r}"
    )
    assert stale_repl.get(ob_a) == ob_a_refined, (
        f"ct stale_replacement dict wrong: expected {{{ob_a}: {ob_a_refined}}}, got {stale_repl}"
    )
