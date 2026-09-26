"""Regression coverage for viz_server's cascade-marker attachment
(#2013/#2009 slice 1 — the cascade machine).

get_graph() and get_health_data() used to read tainted_by/stale_by
straight off a node's metadata JSON blob. That field is retired —
cascade state now lives in the cascade_markers table — and viz_server.py
was migrated to attach tainted_by/stale_by/stale_replacement as
top-level node fields (get_graph) and to count pending markers by
severity (get_health_data), both via bulk queries against the real
table. Reviewer-fairy flagged this migration as having zero test
coverage on #2009's first pass (2026-08-16): a metadata-shape change
here would previously have broken the JS render silently, which is
exactly the drift class the PR claims to structurally close. These
tests exercise the actual queries against a real cascade_markers table
built through the ordinary public API (retract/supersede), not a
metadata fabrication — so a regression in the SQL itself, not just the
shape, would be caught.

Import-pattern contract (#948 wave B — R1):
    This file imports engram_core, engram_observation, and
    engram_revision directly and contains no ``import server``.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import importlib

import engram_core
import engram_derive
import engram_observation
import engram_revision
from viz.data import graph as viz_graph_data  # noqa: E402
from viz.data import health as viz_health_data  # noqa: E402


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_viz_cascade_markers_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """fresh_module() purges ALL engram_* modules from sys.modules before
    reimporting the one it's called with — calling it a SECOND time (e.g.
    fresh_module(engram_derive) after fresh_module(engram_revision)) purges
    the first call's engram_core along with everything else, leaving any
    module imported before either call (like a plain top-of-file `import
    engram_observation`) holding a STALE core reference that never got
    _configure_paths(TEST_DIR). Call fresh_module ONCE (it purges
    everything needed), then importlib.import_module the rest under the
    same purge — the pattern test_cascade_semantics.py's _make_fresh_mods
    already uses correctly.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


def _seed_ob(obs, claim="Test claim.", url="https://example.com/src"):
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Source",
        quoted_text=claim,
        interpretation=f"Interpretation: {claim}",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "error" not in r, f"Failed to create observation: {r}"
    return r["observation_id"]


def test_get_graph_attaches_tainted_by_and_stale_by():
    """get_graph() attaches top-level tainted_by/stale_by fields sourced
    from cascade_markers, not from metadata — a retracted-cascade node
    and a superseded-cascade node each show up correctly, and an
    unaffected node carries neither key at all (matches the
    warnings-omission convention used elsewhere in the migration).
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_root_retract = _seed_ob(obs, "Will be retracted.")
    ob_dep_tainted = _seed_ob(obs, "Depends on the retracted node — filler, "
                                    "wired via a derivation below.")
    ob_root_supersede = _seed_ob(obs, "Will be superseded.")
    ob_clean = _seed_ob(obs, "Untouched node.")

    # Wire a real derivation so the retract cascade has something to mark.
    dv = json.loads(derive._derive_impl(
        claim="Derived from the soon-to-be-retracted node.",
        supporting_ids=ob_root_retract,
        logical_chain="Test chain.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in dv, dv
    dv_id = dv["derivation_id"]

    ret = json.loads(rev._retract_impl(
        node_id=ob_root_retract,
        error_type="fabricated_quote",
        reason="Test retract for viz coverage.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert ret.get("status") == "retracted", ret

    ob_new = _seed_ob(obs, "Replacement for the superseded node.")
    sup = json.loads(rev._supersede_impl(
        old_node_id=ob_root_supersede,
        new_node_id=ob_new,
    ))
    assert sup.get("status") == "superseded", sup

    del ob_dep_tainted  # not directly used; dv_id is the tainted node

    db_path = str(core.DB_PATH)
    graph = viz_graph_data.get_graph(db_path)
    assert "error" not in graph, graph
    by_id = {n["id"]: n for n in graph["nodes"]}

    assert by_id[dv_id].get("tainted_by") == [ob_root_retract], (
        f"tainted node's tainted_by wrong: {by_id[dv_id].get('tainted_by')}"
    )
    assert "stale_by" not in by_id[dv_id], "tainted node must not also carry stale_by"

    assert "tainted_by" not in by_id[ob_clean], "clean node must omit tainted_by"
    assert "stale_by" not in by_id[ob_clean], "clean node must omit stale_by"

    # The retract/supersede ROOTS themselves never carry a marker for
    # their own event (the walker seeds them into `visited` before
    # recursing) — confirms markers land only on downstream dependents.
    assert "tainted_by" not in by_id[ob_root_retract]
    assert "stale_by" not in by_id[ob_root_retract]


def test_get_graph_stale_replacement_reads_roots_own_superseded_by():
    """stale_replacement on a marked node maps each stale root to that
    root's OWN superseded_by column — the mechanism that replaces the
    retired per-downstream metadata.stale_replacement cache.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_old = _seed_ob(obs, "Original claim.")
    dv = json.loads(derive._derive_impl(
        claim="Derived from the original claim.",
        supporting_ids=ob_old,
        logical_chain="Test chain.",
        reasoning_type="deductive_modus_ponens",
    ))
    dv_id = dv["derivation_id"]

    ob_new = _seed_ob(obs, "Refined claim.")
    sup = json.loads(rev._supersede_impl(old_node_id=ob_old, new_node_id=ob_new))
    assert sup.get("status") == "superseded", sup

    db_path = str(core.DB_PATH)
    graph = viz_graph_data.get_graph(db_path)
    by_id = {n["id"]: n for n in graph["nodes"]}

    assert by_id[dv_id]["stale_by"] == [ob_old]
    assert by_id[dv_id]["stale_replacement"] == {ob_old: ob_new}


def test_get_health_data_tainted_stale_counts_match_pending_markers():
    """get_health_data()'s 'tainted'/'stale' counts are DISTINCT-node
    counts against pending cascade_markers, not a metadata string-match
    — verified against a hand-built scenario with a known count.
    """
    rev, obs, derive, core = _make_fresh_mods()

    # Two independent retract cascades, each tainting one derivation.
    tainted_ids = []
    for i in range(2):
        ob = _seed_ob(obs, f"Retract-root {i}.")
        dv = json.loads(derive._derive_impl(
            claim=f"Derived from retract-root {i}.",
            supporting_ids=ob,
            logical_chain="Test chain.",
            reasoning_type="deductive_modus_ponens",
        ))
        tainted_ids.append(dv["derivation_id"])
        ret = json.loads(rev._retract_impl(
            node_id=ob, error_type="fabricated_quote", reason="Test.",
         replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
        assert ret.get("status") == "retracted", ret

    db_path = str(core.DB_PATH)
    health = viz_health_data.get_health_data(db_path)
    assert "error" not in health, health
    assert health["tainted"] == 2, f"expected 2 tainted nodes, got {health['tainted']}"
    assert health["stale"] == 0, f"expected 0 stale nodes, got {health['stale']}"


def test_get_health_data_health_score_available_reports_a_number_and_no_error():
    """When a real health-v2 checkpoint has been written, health_score is a
    real number and health_score_error is None -- the companion test to the
    #2940 no-snapshot-yet fallback test below, confirming the happy path
    wasn't disturbed by re-pointing health_score at the health-v2 reader
    (engram-alpha#2940)."""
    rev, obs, derive, core = _make_fresh_mods()
    _seed_ob(obs)  # the DB file/schema is created on first write, not at _ensure_data_dir()
    db_path = str(core.DB_PATH)

    from health import engine as health_engine

    conn = core._get_db()
    try:
        health_engine.write_snapshot(conn, "nap")
    finally:
        conn.close()

    health = viz_health_data.get_health_data(db_path)
    assert "error" not in health, health
    assert health["health_score"] is not None
    assert isinstance(health["health_score"], (int, float))
    assert health["health_score_error"] is None


def test_get_health_data_health_score_no_snapshot_yet_reports_null_not_zero():
    """engram-alpha#2940: retiring the old pre-v2 formula moved the "must
    not fabricate a fake number" guard from an ImportError fallback
    (engram-alpha#2524's _HEALTH_SCORE_AVAILABLE flag, now retired along
    with the now-retired penalty function) to a no-snapshot-yet fallback -- same
    principle, new trigger. A genuinely fresh temp dir (via
    _make_fresh_mods(), no nap/sleep checkpoint ever run against it) has no
    health-snapshots/ directory at all, so
    health.engine.read_latest_composed_summary() reports has_snapshot=False
    and get_health_data must render null + a reason, never a fabricated
    0.0 'maximally unhealthy' reading indistinguishable from a real one."""
    rev, obs, derive, core = _make_fresh_mods()
    _seed_ob(obs)  # the DB file/schema is created on first write, not at _ensure_data_dir()
    db_path = str(core.DB_PATH)

    health = viz_health_data.get_health_data(db_path)
    assert "error" not in health, health
    assert health["health_score"] is None, (
        f"expected None with no checkpoint written yet, got a fabricated "
        f"number: {health['health_score']!r}"
    )
    assert health["health_score_error"] is not None


def test_get_health_data_reads_the_requested_agents_own_checkpoint_not_the_process_default():
    """engram-alpha#2940 round 2 (reviewer-fairy blocker 2): get_health_data(db_path)
    is called by viz/server.py for potentially ANY agent's dashboard
    (multi-tenant viz) -- it must report THAT agent's own health-v2
    checkpoint, never silently substitute the calling process's own
    default agent's checkpoint under the requested agent's identity. This
    is the exact regression reviewer-fairy round 1 reproduced directly:
    read_latest_composed_summary() with no argument always resolved its
    own process's DATA_DIR, ignoring db_path entirely.

    Sets up two SEPARATE agent trees ("A" and "B", each with their own
    knowledge.db + health-snapshots dir + distinct checkpoint), makes the
    calling PROCESS's own default agent B (the live engram_core module
    state _make_fresh_mods() leaves behind), then calls
    get_health_data(<agent A's db_path>) and asserts the result reflects
    A's checkpoint (via snapshot_id, not just a possibly-coincidental
    score/turn match) -- not B's.
    """
    from health import engine as health_engine

    # ── Agent A: seed, checkpoint, capture its OWN expected summary while
    # the process still points at A (self-scoped read, the ground truth).
    # Force A onto a distinct turn number (write_snapshot's own filename is
    # f"{turn}-{mode}-{timestamp}.json" -- two fresh graphs both checkpoint
    # at turn 0 by default, and a fast test can collide on the
    # second-resolution timestamp too, making snapshot_id an unreliable
    # discriminator unless turn itself is forced to differ). ──
    rev, obs, derive, core = _make_fresh_mods()
    _seed_ob(obs, "Agent A's own observation.")
    core._set_current_turn(101)
    conn_a = core._get_db()
    try:
        health_engine.write_snapshot(conn_a, "nap")
    finally:
        conn_a.close()
    a_expected = health_engine.read_latest_composed_summary()
    assert a_expected["has_snapshot"], a_expected
    a_db_path = str(core.DB_PATH)

    # TEST_DIR is a single shared module-level path that _make_fresh_mods()
    # rmtree's on every call -- copy agent A's tree out before reusing it
    # for agent B below, or A's own files would be wiped from under us.
    a_dir_copy = tempfile.mkdtemp(prefix="engram_test_viz_cascade_markers_agent_a_")
    shutil.copytree(os.path.dirname(a_db_path), a_dir_copy, dirs_exist_ok=True)
    a_db_path_copy = os.path.join(a_dir_copy, os.path.basename(a_db_path))

    # ── Agent B: a completely different tree, seeded differently, its own
    # checkpoint -- and this call leaves the PROCESS's own engram_core
    # module state pointed at B, simulating "the viz process's own live
    # agent is B" while we're about to ask for A's dashboard. ──
    rev, obs, derive, core = _make_fresh_mods()
    _seed_ob(obs, "Agent B's own, DIFFERENT observation -- must not leak into A's read.")
    core._set_current_turn(202)
    conn_b = core._get_db()
    try:
        health_engine.write_snapshot(conn_b, "nap")
    finally:
        conn_b.close()
    b_expected = health_engine.read_latest_composed_summary()
    assert b_expected["has_snapshot"], b_expected

    assert a_expected["snapshot_id"] != b_expected["snapshot_id"], (
        "test precondition failed -- agent A's and agent B's checkpoints "
        "produced the same snapshot_id, so this test cannot discriminate "
        f"'read A' from 'silently read B instead': {a_expected['snapshot_id']!r}"
    )

    try:
        # Process default is now B. Request A's dashboard by db_path.
        result = viz_health_data.get_health_data(a_db_path_copy)
        assert "error" not in result, result
        assert result["health_score_detail"]["snapshot_id"] == a_expected["snapshot_id"], (
            f"get_health_data(agent A's db_path) must read AGENT A's own "
            f"checkpoint ({a_expected['snapshot_id']!r}), not the calling "
            f"process's own default agent B's "
            f"({result['health_score_detail']['snapshot_id']!r} returned, "
            f"vs B's actual {b_expected['snapshot_id']!r}) -- multi-tenant "
            f"wrong-identity-attribution regression"
        )
    finally:
        shutil.rmtree(a_dir_copy, ignore_errors=True)
