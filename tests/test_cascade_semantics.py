"""Tests for cascade semantics fix (PR A) — §5 of PR-A-SPEC.

Coverage:
  §5.1 Retract cascade — preserved correct behavior (non-cs/ls types still cascade)
  §5.2 Retract cascade — new correct behavior (cs/ls types skip taint + recursion)
  §5.3 Supersede cascade — mirror of §5.1/§5.2 for stale_by
  §5.4 Zero-support detection (retract and supersede paths; exemplifies edge)
  §5.5 Reflect surfacing (support_lost_nodes in engram_reflect output)

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision, engram_cornerstone, engram_derive,
    engram_epistemic, engram_lifecycle, engram_observation, and engram_core
    directly and contains no ``import server``. The module objects are passed
    to fresh_module() (R4) for per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
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
import engram_cornerstone
import engram_derive
import engram_epistemic
import engram_lifecycle
import engram_observation
import engram_revision


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cascade_semantics_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


# ---------------------------------------------------------------------------
# Module factory helpers
# ---------------------------------------------------------------------------

def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_revision) per R4: the module object is
    passed (not a string), so the ``import engram_revision`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (rev, obs, derive, cs, epistemic, lifecycle, core) — freshly
    reloaded module objects with engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    cs = importlib.import_module("engram_cornerstone")
    epistemic = importlib.import_module("engram_epistemic")
    lifecycle = importlib.import_module("engram_lifecycle")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, cs, epistemic, lifecycle, core


def _seed_ob(obs, claim="Test claim.", url="https://example.com/src",
             quote_type="attributed_analysis"):
    """Create an observation. All observations share the same evidence URL by
    default to avoid multiple DNS lookups in the test environment."""
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Source",
        quoted_text=claim,
        interpretation=f"Interpretation: {claim}",
        claim=claim,
        quote_type=quote_type,
    ))
    assert "error" not in r, f"Failed to create observation: {r}"
    return r["observation_id"]


def _seed_dv(derive, claim, supporting_ids):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=supporting_ids,
        logical_chain=f"Derivation: {claim}",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in r, r
    return r["derivation_id"]


def _seed_cs(cs, supporting_ids, tag="cs-tag"):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(cs._add_cornerstone_impl(
        tag=tag,
        title=f"Cornerstone: {tag}",
        new_frame=f"Principle for {tag}.",
        supporting_ids=supporting_ids,
    ))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _seed_ls(obs, epistemic, claim="Lesson pattern.", supporting_ids=None):
    """Create a lesson with the given incident observations as exemplifiers.

    engram_add_lesson requires incident_ids (comma-separated). Those observations
    are wired to the lesson via exemplifies edges by _add_lesson_impl.
    """
    if supporting_ids is None:
        # Lessons require at least one incident; create a placeholder.
        supporting_ids = _seed_ob(obs, f"Placeholder incident for: {claim}",
                                  url="https://example.com/placeholder")
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)

    r = json.loads(epistemic._add_lesson_impl(
        claim=claim,
        incident_ids=supporting_ids,
        logical_chain=f"Pattern derived from incidents: {claim}",
        scaffolding_nudge="Watch for this pattern.",
    ))
    assert "error" not in r, r
    return r["lesson_id"]


def _is_tainted(core, node_id, by_id):
    """node_id carries a PENDING cascade marker whose root is by_id and
    whose root_op is 'retract' (taint severity — see
    core._cascade_marker_severity). Migrated off metadata.tainted_by to the
    cascade_markers table (#2013/#2009 slice 1) — same observable meaning,
    new storage.
    """
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
            "AND root_op = 'retract' AND status = 'pending'",
            (node_id, by_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _is_stale(core, node_id, by_id):
    """node_id carries a PENDING cascade marker whose root is by_id and
    whose root_op is 'supersede' (stale severity). Migrated off
    metadata.stale_by to the cascade_markers table — same meaning, new
    storage.
    """
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
            "AND root_op = 'supersede' AND status = 'pending'",
            (node_id, by_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _get_meta(core, node_id):
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return json.loads(row["metadata"] or "{}") if row else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §5.1 Retract cascade — preserved correct behavior
# ---------------------------------------------------------------------------


def test_retract_ob_with_downstream_dv_propagates_taint():
    """Retracting ob_X; dv_Y with derives_from→ob_X gets tainted_by:[ob_X].

    Derivation chains still cascade — only cs/ls are skipped.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Premise A.")
    dv_id = _seed_dv(derive, "Derived from A.", ob_id)

    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Premise was fabricated.",
    ))
    assert "error" not in ret, ret

    assert _is_tainted(core, dv_id, ob_id), (
        f"dv_id {dv_id} should be tainted_by {ob_id} after retraction"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert dv_id in tainted_ids, f"{dv_id} should appear in tainted_downstream"


def test_retract_dv_with_downstream_dv_propagates_taint():
    """Retraction cascade walks derivation chains correctly."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Root premise.")
    dv1_id = _seed_dv(derive, "Intermediate derivation.", ob_id)
    dv2_id = _seed_dv(derive, "Downstream derivation.", dv1_id)

    ret = json.loads(rev._retract_impl(
        node_id=dv1_id,
        error_type="hallucinated_claim",
        reason="Intermediate derivation was wrong.",
    ))
    assert "error" not in ret, ret
    assert _is_tainted(core, dv2_id, dv1_id), (
        f"dv2_id {dv2_id} should be tainted_by {dv1_id}"
    )


# ---------------------------------------------------------------------------
# §5.2 Retract cascade — new correct behavior (cs/ls skip)
# ---------------------------------------------------------------------------


def test_retract_ob_with_supported_by_edge_from_cs_does_NOT_taint_cs():
    """Retracting ob_X must NOT taint cs_Y that has supported_by→ob_X.

    Cascade skip rule: cs accumulates instance-votes; one retracted instance
    does not invalidate the pattern.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob1 = _seed_ob(obs, "Instance vote 1.")
    ob2 = _seed_ob(obs, "Instance vote 2.")  # second supporter so not zero-support
    cs_id = _seed_cs(cs, [ob1, ob2], tag="multi-support-cs")

    ret = json.loads(rev._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 was fabricated.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(core, cs_id, ob1), (
        f"cs {cs_id} must NOT be tainted_by {ob1} — cascade skip rule applies"
    )
    # cs must not appear in tainted_downstream
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_id not in tainted_ids, f"cs {cs_id} must not be in tainted_downstream"


def test_retract_ob_with_exemplifies_edge_to_ls_does_NOT_taint_ls():
    """Retracting ob_X must NOT taint ls_Y with incoming exemplifies←ob_X.

    The exemplifies edge goes FROM the instance TO the lesson. The cascade
    skip rule applies to the lesson when ob_X is retracted.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob1 = _seed_ob(obs, "Incident A — exemplifies lesson.")
    ob2 = _seed_ob(obs, "Incident B — also exemplifies lesson.")
    ls_id = _seed_ls(obs, epistemic, "Pattern: X leads to Y.", supporting_ids=f"{ob1},{ob2}")

    ret = json.loads(rev._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 was fabricated.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(core, ls_id, ob1), (
        f"lesson {ls_id} must NOT be tainted_by {ob1} — cascade skip rule applies"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert ls_id not in tainted_ids, f"lesson {ls_id} must not be in tainted_downstream"


def test_retract_ob_does_not_recurse_through_cs():
    """Cascade stops at cs; does NOT mark other cs supporters as collateral taint.

    Setup:
      ob_A ←[supported_by]← cs
      ob_B ←[supported_by]← cs  (cs also supported_by ob_B — unrelated supporter)
      ob_dv_premise ←[derives_from]← dv_indirect  (dv that is unrelated to cs)

    Retract ob_A. The cascade skip rule prevents cs from being tainted.
    ob_B and dv_indirect must also NOT be tainted — cascade stops at cs and
    does not "pass through" to infect unrelated nodes that are only related
    via the cs's own supporting chain.

    Note: the cascade walks source_id→target_id in ('derives_from','supported_by').
    cs is SOURCE of supported_by→ob_A (target). Retracting ob_A finds cs as SOURCE.
    With skip: cs is not tainted AND we do NOT recurse from cs. Without skip, we
    would call _find_downstream(cs), which would look for nodes where cs is TARGET.
    There are none — so the non-recursion is implicitly verified by cs not being tainted.
    The test verifies the primary contract (cs not tainted) and that tainted_downstream
    does not include cs.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_a = _seed_ob(obs, "Retracted premise A.")
    ob_b = _seed_ob(obs, "Live premise B.")
    # cs supported_by both A and B
    cs_id = _seed_cs(cs, [ob_a, ob_b], tag="recursion-stop-test")

    # Create an unrelated derivation (derives from ob_b, not related to ob_a directly).
    # If cascade incorrectly recurses through cs and then looks for cs's targets,
    # it might find ob_b (cs→ob_b via supported_by) and taint ob_b.
    # Then from ob_b it might taint this derivation. This chain should NOT happen.
    dv_from_b = _seed_dv(derive, "Derivation from ob_b.", ob_b)

    ret = json.loads(rev._retract_impl(
        node_id=ob_a,
        error_type="hallucinated_claim",
        reason="ob_a was wrong.",
    ))
    assert "error" not in ret, ret

    # cs not tainted
    assert not _is_tainted(core, cs_id, ob_a), (
        f"cs {cs_id} must not be tainted"
    )
    # ob_b not tainted (cascade didn't recurse through cs)
    assert not _is_tainted(core, ob_b, ob_a), (
        f"ob_b {ob_b} must not be tainted — cascade did not recurse through cs"
    )
    # dv_from_b not tainted
    assert not _is_tainted(core, dv_from_b, ob_a), (
        f"dv_from_b {dv_from_b} must not be tainted — cascade stopped at cs"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_id not in tainted_ids, "cs must not appear in tainted_downstream"
    assert dv_from_b not in tainted_ids, "dv_from_b must not appear in tainted_downstream"


# ---------------------------------------------------------------------------
# §5.3 Supersede cascade — mirror of §5.1/5.2 for stale_by
# ---------------------------------------------------------------------------


def test_supersede_dv_with_downstream_dv_propagates_stale():
    """Superseding dv_old propagates stale_by to derivations downstream of it."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Premise.")
    dv_old = _seed_dv(derive, "Old derivation.", ob_id)
    dv_downstream = _seed_dv(derive, "Downstream of old.", dv_old)
    dv_new = _seed_dv(derive, "Refined old derivation.", ob_id)

    ret = json.loads(rev._supersede_impl(dv_old, dv_new))
    assert "superseded" in ret.get("status", ""), ret

    assert _is_stale(core, dv_downstream, dv_old), (
        f"dv {dv_downstream} should be stale_by {dv_old} after supersede"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert dv_downstream in stale_ids, f"{dv_downstream} should appear in stale_downstream"


def test_supersede_ob_with_supported_by_from_cs_does_NOT_stale_cs():
    """Superseding ob_old must NOT mark cs_Y stale when cs has supported_by→ob_old.

    Cascade skip rule for supersede: cornerstone accumulates instance-votes;
    one superseded instance does not stale the pattern.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_old = _seed_ob(obs, "Old instance A.")
    ob_extra = _seed_ob(obs, "Instance B — keeps cs live.")
    cs_id = _seed_cs(cs, [ob_old, ob_extra], tag="supersede-skip-cs")
    ob_new = _seed_ob(obs, "Refined instance A.")

    ret = json.loads(rev._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    assert not _is_stale(core, cs_id, ob_old), (
        f"cs {cs_id} must NOT be stale_by {ob_old} — cascade skip rule applies"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert cs_id not in stale_ids, f"cs {cs_id} must not be in stale_downstream"


def test_supersede_ob_with_exemplifies_to_ls_does_NOT_stale_ls():
    """Superseding ob_old must NOT stale ls_Y when ls has incoming exemplifies←ob_old."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_old = _seed_ob(obs, "Old incident exemplifying lesson.")
    ob_extra = _seed_ob(obs, "Second incident keeps lesson live.")
    ls_id = _seed_ls(obs, epistemic, "Lesson pattern.", supporting_ids=f"{ob_old},{ob_extra}")
    ob_new = _seed_ob(obs, "Refined old incident.")

    ret = json.loads(rev._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    assert not _is_stale(core, ls_id, ob_old), (
        f"lesson {ls_id} must NOT be stale_by {ob_old}"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert ls_id not in stale_ids, f"lesson {ls_id} must not be in stale_downstream"


# ---------------------------------------------------------------------------
# §5.4 Zero-support detection
# ---------------------------------------------------------------------------


def test_retract_last_supporter_of_cs_sets_support_lost():
    """Retracting the ONLY supporter of cs → cs flagged support_lost."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Sole supporter.")
    cs_id = _seed_cs(cs, ob_id, tag="sole-supporter-cs")

    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Sole supporter was fabricated.",
    ))
    assert "error" not in ret, ret

    # support_lost must be in return value
    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in support_lost list, got: {support_lost_ids}"
    )

    # metadata must be updated
    meta = _get_meta(core, cs_id)
    assert meta.get("support_lost") is True, (
        f"cs {cs_id} metadata.support_lost must be True, got: {meta}"
    )
    # still NOT tainted (cascade skip rule)
    assert not _is_tainted(core, cs_id, ob_id), (
        f"cs {cs_id} must NOT be tainted"
    )


def test_retract_one_of_many_supporters_does_NOT_set_support_lost():
    """Retracting one of 5 supporters does NOT flag support_lost (4 remain)."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    obs_list = [_seed_ob(obs, f"Supporter {i}.") for i in range(5)]
    cs_id = _seed_cs(cs, obs_list, tag="five-supporter-cs")

    ret = json.loads(rev._retract_impl(
        node_id=obs_list[0],
        error_type="hallucinated_claim",
        reason="First supporter was fabricated.",
    ))
    assert "error" not in ret, ret

    assert ret.get("support_lost_count", 0) == 0, (
        f"support_lost_count should be 0 (4 supporters remain), got: {ret}"
    )
    meta = _get_meta(core, cs_id)
    assert not meta.get("support_lost"), (
        f"cs {cs_id} must NOT have support_lost set: {meta}"
    )


def test_retract_last_supporter_via_exemplifies_sets_support_lost():
    """Lesson supported only by exemplifies edge; retract the instance → support_lost."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Sole incident exemplifying lesson.")
    ls_id = _seed_ls(obs, epistemic, "Lesson from sole incident.", supporting_ids=ob_id)

    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Incident was fabricated.",
    ))
    assert "error" not in ret, ret

    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1 for lesson, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert ls_id in support_lost_ids, (
        f"Expected {ls_id} in support_lost list, got: {support_lost_ids}"
    )
    meta = _get_meta(core, ls_id)
    assert meta.get("support_lost") is True, (
        f"lesson {ls_id} metadata.support_lost must be True"
    )
    # not tainted
    assert not _is_tainted(core, ls_id, ob_id), (
        f"lesson {ls_id} must NOT be tainted"
    )


def test_supersede_last_supporter_of_cs_sets_support_lost():
    """Superseding the ONLY supporter of cs → cs flagged support_lost."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_old = _seed_ob(obs, "Sole supporter, will be superseded.")
    cs_id = _seed_cs(cs, ob_old, tag="supersede-sole-cs")
    ob_new = _seed_ob(obs, "Replacement supporter.")

    ret = json.loads(rev._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    # ob_old is now is_current=0; cs has no live supported_by edges (ob_new is
    # NOT automatically wired to cs — caller must link explicitly).
    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in support_lost list, got: {support_lost_ids}"
    )
    meta = _get_meta(core, cs_id)
    assert meta.get("support_lost") is True, (
        f"cs {cs_id} metadata.support_lost must be True after supersede"
    )
    # still NOT stale
    assert not _is_stale(core, cs_id, ob_old), (
        f"cs {cs_id} must NOT be stale — cascade skip rule applies"
    )


def test_support_lost_logged_in_edit_log():
    """support_lost event appears in edit_history with correct action."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Sole supporter — will be retracted.")
    cs_id = _seed_cs(cs, ob_id, tag="audit-cs")

    json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Fabricated.",
    ))

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT action, node_id, details FROM edit_history "
            "WHERE node_id = ? AND action = 'support_lost' ORDER BY id DESC LIMIT 1",
            (cs_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, (
        f"Expected a support_lost edit_history entry for {cs_id}, found none"
    )
    assert row["action"] == "support_lost"
    details = json.loads(row["details"])
    assert details.get("trigger") == ob_id, (
        f"Expected trigger={ob_id!r}, got: {details}"
    )


# ---------------------------------------------------------------------------
# §5.5 Surfacing — engram_reflect
# ---------------------------------------------------------------------------


def test_reflect_surfaces_support_lost_node_ids():
    """engram_reflect includes support_lost_nodes in its returned structure."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Sole supporter.")
    cs_id = _seed_cs(cs, ob_id, tag="reflect-support-lost-cs")

    # Retract the sole supporter → cs gets support_lost
    json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Fabricated.",
    ))

    reflect_result = json.loads(lifecycle._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    support_lost = reflect_result.get("support_lost_nodes", [])
    support_lost_ids = [n["id"] for n in support_lost]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in reflect support_lost_nodes, got: {support_lost_ids}"
    )

    # summary count should match
    summary = reflect_result.get("summary", {})
    assert summary.get("support_lost_nodes", 0) >= 1, (
        f"Expected summary.support_lost_nodes >= 1, got: {summary}"
    )


def test_reflect_does_not_surface_cs_ls_as_tainted_when_skip_rule_applies():
    """A cs/ls that was NOT tainted (due to skip rule) does not appear in tainted_nodes."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob1 = _seed_ob(obs, "Supporter A.", url="https://example.com/a")
    ob2 = _seed_ob(obs, "Supporter B.", url="https://example.com/b")
    cs_id = _seed_cs(cs, [ob1, ob2], tag="skip-rule-cs")

    # Retract ob1 — cs should NOT be tainted (skip rule)
    json.loads(rev._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 fabricated.",
    ))

    reflect_result = json.loads(lifecycle._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    tainted_ids = {n["id"] for n in reflect_result.get("tainted_nodes", [])}
    assert cs_id not in tainted_ids, (
        f"cs {cs_id} must NOT appear in tainted_nodes — cascade skip rule was applied"
    )


# ---------------------------------------------------------------------------
# §5.7 Skip rule works on cs→cs / cs→ls / ls→cs cascade paths
# ---------------------------------------------------------------------------


def test_retract_ls_with_supported_by_from_cs_does_NOT_taint_cs():
    """Retracting ls_Y must NOT taint cs_X that has supported_by → ls_Y.

    Live graphs contain cs→ls supported_by edges (e.g. cs_0003→ls_0001).
    The cascade skip rule must fire on the downstream node's type (cs_X),
    not on the retracted node's type, so cs_X is protected even when the
    retracted node is itself a lesson.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    # Create the lesson with a placeholder incident so engram_add_lesson is happy.
    ob_incident = _seed_ob(obs, "Incident exemplifying the lesson.")
    ls_y = _seed_ls(obs, epistemic, "Lesson: pattern Y.", supporting_ids=ob_incident)
    # Cornerstone whose supported_by edge targets ls_y.
    cs_x = _seed_cs(cs, [ls_y], tag="cs-supported-by-ls")

    ret = json.loads(rev._retract_impl(
        node_id=ls_y,
        error_type="hallucinated_claim",
        reason="Lesson was incorrect.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(core, cs_x, ls_y), (
        f"cs {cs_x} must NOT be tainted_by {ls_y} — downstream-type check fires on cs"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_x not in tainted_ids, (
        f"cs {cs_x} must not appear in tainted_downstream"
    )


def test_retract_cs_with_supported_by_from_other_cs_does_NOT_taint_source_cs():
    """Retracting cs_Y must NOT taint cs_X that has supported_by → cs_Y.

    Uncommon but schema-permitted: a cornerstone can cite another cornerstone
    as a support (engram_add_cornerstone accepts any existing node ID as a
    supporting_id). When the inner cs is retracted, the outer cs's skip rule
    must fire.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    # cs_y: the cornerstone that will be retracted.
    ob_for_y = _seed_ob(obs, "Evidence backing cs_y.")
    cs_y = _seed_cs(cs, [ob_for_y], tag="inner-cs")
    # cs_x: cornerstone with supported_by → cs_y.
    cs_x = _seed_cs(cs, [cs_y], tag="outer-cs-citing-inner")

    ret = json.loads(rev._retract_impl(
        node_id=cs_y,
        error_type="hallucinated_claim",
        reason="cs_y was wrong.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(core, cs_x, cs_y), (
        f"cs {cs_x} must NOT be tainted_by {cs_y} — cascade skip rule applies to cs type"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_x not in tainted_ids, (
        f"cs {cs_x} must not appear in tainted_downstream"
    )


def test_skip_rule_does_not_recurse_through_cs_or_ls():
    """Cascade walk terminates at cs/ls; nodes 'downstream of cs' in cascade terms
    must NOT receive taint via the cs hop.

    Graph shape:
      ob_a  ← (supported_by) ← cs_mid        # cs_mid is downstream of ob_a
      cs_mid ← (supported_by) ← cs_down       # cs_down is downstream of cs_mid

    When ob_a is retracted:
    - _find_downstream(ob_a) finds cs_mid; skip rule fires → cs_mid NOT tainted.
    - Because cs_mid is skipped, _find_downstream(cs_mid) is NOT called.
    - Therefore cs_down (which has supported_by → cs_mid) NEVER gets visited.
    - cs_down must NOT be tainted.

    Note: a derivation cannot directly cite cs as a premise (cornerstones are
    not in CLAIM_BEARING_TYPES, so engram_derive rejects them as supporting_ids).
    Instead we use a cs→cs supported_by chain, which is a schema-permitted shape
    already present in the live graph (e.g. cs_0002→cs_0001). This verifies the
    same invariant — cascade termination at the cs boundary — via a permitted
    graph topology.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_a = _seed_ob(obs, "Root observation A.")
    # cs_mid: mid-level cornerstone, downstream of ob_a in the cascade walk.
    cs_mid = _seed_cs(cs, [ob_a], tag="cascade-mid-cs")
    # cs_down: further cornerstone, downstream of cs_mid in the cascade walk.
    # If cascade recursed through cs_mid, it would call _find_downstream(cs_mid)
    # and find cs_down (because cs_down has supported_by → cs_mid, i.e.
    # source_id=cs_down, target_id=cs_mid).
    cs_down = _seed_cs(cs, [cs_mid], tag="cascade-down-cs")

    ret = json.loads(rev._retract_impl(
        node_id=ob_a,
        error_type="hallucinated_claim",
        reason="ob_a was fabricated.",
    ))
    assert "error" not in ret, ret

    # cs_mid: cascade skip rule fires — NOT tainted.
    assert not _is_tainted(core, cs_mid, ob_a), (
        f"cs_mid {cs_mid} must NOT be tainted_by {ob_a} — skip rule fires"
    )
    # cs_down: cascade did NOT recurse through cs_mid — NOT tainted.
    assert not _is_tainted(core, cs_down, ob_a), (
        f"cs_down {cs_down} must NOT be tainted — cascade terminates at cs_mid hop"
    )

    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_mid not in tainted_ids, "cs_mid must not appear in tainted_downstream"
    assert cs_down not in tainted_ids, (
        "cs_down must not appear in tainted_downstream — cascade did not recurse through cs_mid"
    )


# ---------------------------------------------------------------------------
# §6.2-6.4 PR-AA — cascade-walker SSoT regression tests
# ---------------------------------------------------------------------------


def test_diamond_cascade_retract_visits_each_node_exactly_once():
    """Diamond-shape: dv_Z reachable from ob_X via two paths; retract ob_X.

    Graph shape:
      ob_X → dv_A (derives_from → ob_X)
      ob_X → dv_B (derives_from → ob_X)
      dv_Z derives_from → dv_A  AND  dv_Z derives_from → dv_B

    Before the PR-AA fix, _find_downstream would append dv_Z to `tainted`
    twice (once via dv_A path, once via dv_B path). After the fix, the shared
    walker's visited-set deduplication ensures dv_Z appears exactly once.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_x = _seed_ob(obs, "Root observation X.")
    dv_a = _seed_dv(derive, "Derivation A — from X.", ob_x)
    dv_b = _seed_dv(derive, "Derivation B — from X.", ob_x)
    # dv_Z cites both dv_A and dv_B (diamond shape).
    dv_z = _seed_dv(derive, "Derivation Z — from A and B.", [dv_a, dv_b])

    ret = json.loads(rev._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x was fabricated.",
    ))
    assert "error" not in ret, ret

    # dv_Z should appear exactly once in tainted_downstream.
    tainted_ids = [t["id"] for t in ret.get("tainted_downstream", [])]
    dv_z_count = tainted_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once in tainted_downstream, "
        f"got {dv_z_count} (diamond-shape dedup failure). Full list: {tainted_ids}"
    )

    # Metadata write is still correct.
    assert _is_tainted(core, dv_z, ob_x), (
        f"dv_Z {dv_z} must be tainted_by {ob_x}"
    )


def test_diamond_cascade_supersede_visits_each_node_exactly_once():
    """Diamond-shape: dv_Z reachable from ob_old via two paths; supersede ob_old.

    Graph shape:
      ob_old → dv_A (derives_from → ob_old)
      ob_old → dv_B (derives_from → ob_old)
      dv_Z derives_from → dv_A  AND  dv_Z derives_from → dv_B

    Before the PR-AA fix, _flag_stale would append dv_Z to `stale` twice.
    After the fix, the shared walker's visited-set deduplication ensures
    dv_Z appears exactly once in stale_downstream.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_old = _seed_ob(obs, "Old root observation.")
    ob_new = _seed_ob(obs, "Refined root observation.")
    dv_a = _seed_dv(derive, "Derivation A — from old root.", ob_old)
    dv_b = _seed_dv(derive, "Derivation B — from old root.", ob_old)
    # dv_Z cites both dv_A and dv_B (diamond shape).
    dv_z = _seed_dv(derive, "Derivation Z — from A and B.", [dv_a, dv_b])

    ret = json.loads(rev._supersede_impl(ob_old, ob_new))
    assert "error" not in ret, ret

    # dv_Z should appear exactly once in stale_downstream.
    stale_ids = [s["id"] for s in ret.get("stale_downstream", [])]
    dv_z_count = stale_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once in stale_downstream, "
        f"got {dv_z_count} (diamond-shape dedup failure). Full list: {stale_ids}"
    )

    # Metadata write is still correct.
    assert _is_stale(core, dv_z, ob_old), (
        f"dv_Z {dv_z} must be stale_by {ob_old}"
    )


def test_three_path_cascade_visits_node_once():
    """Three-parent diamond: dv_Z reachable via 3 paths; retract ob_X.

    Graph shape:
      ob_X → dv_A, dv_B, dv_C  (all derives_from → ob_X)
      dv_Z derives_from → dv_A, dv_B, dv_C

    dv_Z must appear exactly once in tainted_downstream regardless of the
    path count.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_x = _seed_ob(obs, "Root observation for 3-parent test.")
    dv_a = _seed_dv(derive, "Derivation A (3-parent).", ob_x)
    dv_b = _seed_dv(derive, "Derivation B (3-parent).", ob_x)
    dv_c = _seed_dv(derive, "Derivation C (3-parent).", ob_x)
    dv_z = _seed_dv(derive, "Derivation Z — from A, B, and C.", [dv_a, dv_b, dv_c])

    ret = json.loads(rev._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x fabricated — 3-parent test.",
    ))
    assert "error" not in ret, ret

    tainted_ids = [t["id"] for t in ret.get("tainted_downstream", [])]
    dv_z_count = tainted_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once even with 3 parents, "
        f"got {dv_z_count}. Full list: {tainted_ids}"
    )
    assert _is_tainted(core, dv_z, ob_x), (
        f"dv_Z {dv_z} must still be tainted_by {ob_x}"
    )


def test_cascade_stops_at_retracted_downstream_node():
    """Parity guard: walker stops at retracted mid-chain nodes (is_current=0).

    Graph shape:
      ob_X → dv_A (dv_A is already retracted, is_current=0)
      dv_A → dv_B (dv_B depends on dv_A)

    When ob_X is retracted:
    - dv_A has is_current=0 → walker stops there, dv_A NOT in tainted list.
    - dv_B is downstream of dv_A, which is not visited → dv_B NOT in tainted list.

    Before the PR-AA fix, _find_downstream would recurse through dv_A even
    when dv_A was retracted (the recursive call was outside the `if d_node:`
    guard). The shared walker's stop-at-retracted semantics fix this asymmetry.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_x = _seed_ob(obs, "Root observation for parity guard test.")
    dv_a = _seed_dv(derive, "Mid-chain derivation A.", ob_x)
    dv_b = _seed_dv(derive, "Terminal derivation B — derives from A.", dv_a)

    # Pre-retract dv_A so it has is_current=0 before ob_X is retracted.
    pre = json.loads(rev._retract_impl(
        node_id=dv_a,
        error_type="hallucinated_claim",
        reason="dv_a pre-retracted to set up parity test.",
    ))
    assert "error" not in pre, pre

    # Now retract ob_X. The cascade should not include dv_A (already retracted)
    # or dv_B (unreachable because walker stops at dv_A).
    ret = json.loads(rev._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x retracted — parity guard test.",
    ))
    assert "error" not in ret, ret

    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert dv_a not in tainted_ids, (
        f"dv_A {dv_a} must NOT be in tainted_downstream — is_current=0, walker stops here"
    )
    # Note: dv_b may already be tainted_by dv_a from the step-1 pre-retract; this asserts only that it
    #  does not appear in ob_x's cascade (walker stopped at retracted dv_a).
    assert dv_b not in tainted_ids, (
        f"dv_B {dv_b} must NOT be in tainted_downstream — cascade must not recurse through "
        f"retracted dv_A"
    )


def test_supersede_stops_at_superseded_downstream_node():
    """Parity guard: walker stops at superseded mid-chain nodes (is_current=0).

    Graph shape:
      ob_X → dv_A (dv_A is already superseded, is_current=0)
      dv_A → dv_B (dv_B depends on dv_A)

    When ob_X is superseded:
    - dv_A has is_current=0 → walker stops there, dv_A NOT in stale_downstream.
    - dv_B is downstream of dv_A, which is not visited → dv_B NOT in stale_downstream.

    Mirror of test_cascade_stops_at_retracted_downstream_node for the supersede path.
    The shared walker's stop-at-non-current semantics apply symmetrically to both
    retract (is_current=0 via retraction) and supersede (is_current=0 via supersession).
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_x = _seed_ob(obs, "Root observation for supersede parity guard test.")
    dv_a = _seed_dv(derive, "Mid-chain derivation A.", ob_x)
    dv_b = _seed_dv(derive, "Terminal derivation B — derives from A.", dv_a)

    # Pre-supersede dv_A so it has is_current=0 before ob_X is superseded.
    dv_a_replacement = _seed_dv(derive, "Replacement for mid-chain derivation A.", ob_x)
    pre = json.loads(rev._supersede_impl(dv_a, dv_a_replacement))
    assert "superseded" in pre.get("status", ""), pre

    # Now supersede ob_X. The cascade should not include dv_A (already superseded)
    # or dv_B (unreachable because walker stops at dv_A).
    ob_x_replacement = _seed_ob(obs, "Replacement for root observation.")
    ret = json.loads(rev._supersede_impl(ob_x, ob_x_replacement))
    assert "superseded" in ret.get("status", ""), ret

    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    # Positive guard against a vacuous pass: dv_a_replacement is a live dependent of ob_x,
    # so it MUST land in ob_x's cascade — confirms stale_downstream is non-empty and the
    # dv_a/dv_b absences below are real stops, not an empty result.
    assert dv_a_replacement in stale_ids, (
        f"dv_A_replacement {dv_a_replacement} SHOULD be in stale_downstream — live dependent of ob_x"
    )
    assert dv_a not in stale_ids, (
        f"dv_A {dv_a} must NOT be in stale_downstream — is_current=0, walker stops here"
    )
    # Note: dv_b may already be stale_by dv_a from the step-1 pre-supersede; this asserts only that it
    #  does not appear in ob_x's cascade (walker stopped at superseded dv_a).
    assert dv_b not in stale_ids, (
        f"dv_B {dv_b} must NOT be in stale_downstream — cascade must not recurse through "
        f"superseded dv_A"
    )


# ---------------------------------------------------------------------------
# stale_by → superseded_by invariant (slice 1 tripwire)
# ---------------------------------------------------------------------------
#
# Today, a node lands in someone's stale_by list ONLY because the cascade
# walker (_on_live_for_supersede, engram_revision.py) marks the ROOT
# superseded node's id in stale_by for every downstream node it walks — not
# just the direct premise. So an id appearing in a stale_by list is *always*
# a node that was itself superseded, and therefore always carries a
# non-NULL superseded_by on that same node.
#
# Slice 1 will change the write path to mark the DIRECT premise instead of
# the superseded root. Under a chain dv_A -> dv_B -> ob_1 (dv_B derives_from
# ob_1, dv_A derives_from dv_B), if ob_1 is superseded, dv_A's stale_by would
# become [dv_B] — but dv_B itself was never superseded, so its superseded_by
# is NULL. A viz layer reading stale_by + superseded_by together would then
# render a false sentence ("stale — premise dv_B superseded by ?"). This
# test pins the invariant that makes that impossible TODAY, so it fails
# loudly the moment the write path changes without preserving the invariant.


def _find_stale_by_invariant_violations(conn):
    """For every PENDING supersede-severity cascade marker, its root_node
    must name a node with a non-NULL superseded_by. Returns a list of
    (offending_node_id, root_node_id) violation tuples; empty means the
    invariant holds.

    Migrated off metadata.stale_by to cascade_markers (#2013/#2009 slice 1
    — Ariadne: "keep the tripwire commit... it just now passes for a
    different reason"). The invariant's SHAPE survives unchanged: slice 1
    reports root_node uniformly (not a direct-premise refinement — that's
    #1969's territory layered on top later), and root_node is always the
    literal node passed to _supersede_impl, whose own superseded_by is set
    atomically in the same call before the walker marks anything — so this
    should hold by construction, same as the pre-marker code held it by
    construction. A checker that always finds nothing proves nothing; the
    negative-control test below fabricates a marker row directly (bypassing
    the writer) to prove this checker can actually fail.
    """
    violations = []
    rows = conn.execute(
        "SELECT node_id, root_node FROM cascade_markers "
        "WHERE status = 'pending' AND root_op = 'supersede'"
    ).fetchall()
    for row in rows:
        node_id, root_node = row["node_id"], row["root_node"]
        prow = conn.execute(
            "SELECT superseded_by FROM nodes WHERE id = ?", (root_node,)
        ).fetchone()
        if prow is None or prow["superseded_by"] is None:
            violations.append((node_id, root_node))
    return violations


def test_stale_by_premises_have_superseded_by_across_graph():
    """Invariant: every stale_by entry names a node with non-NULL superseded_by.

    Builds several independent supersede cascades — a simple chain, a
    transitive (two-hop) chain, and a diamond-of-supersedes shape where one
    downstream node accumulates stale_by entries from two separate supersede
    calls — through the ordinary public API (create/derive/supersede), then
    checks the invariant against EVERY node in the resulting graph, not a
    hand-picked one.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()

    # Cascade 1: simple chain. ob_1 -> dv_1 (derives_from ob_1). Supersede ob_1.
    ob_1 = _seed_ob(obs, "Premise 1.")
    dv_1 = _seed_dv(derive, "Derived from premise 1.", ob_1)
    ob_1_new = _seed_ob(obs, "Refined premise 1.")
    r1 = json.loads(rev._supersede_impl(ob_1, ob_1_new))
    assert "superseded" in r1.get("status", ""), r1
    assert _is_stale(core, dv_1, ob_1), (
        f"setup check: {dv_1} should be stale_by {ob_1}"
    )

    # Cascade 2: transitive (two-hop) chain, to exercise the walker's
    # recursion beyond a direct premise.
    # ob_2 -> dv_2a (derives_from ob_2) -> dv_2b (derives_from dv_2a).
    ob_2 = _seed_ob(obs, "Premise 2.")
    dv_2a = _seed_dv(derive, "First-level derivation from premise 2.", ob_2)
    dv_2b = _seed_dv(derive, "Second-level derivation from premise 2.", dv_2a)
    ob_2_new = _seed_ob(obs, "Refined premise 2.")
    r2 = json.loads(rev._supersede_impl(ob_2, ob_2_new))
    assert "superseded" in r2.get("status", ""), r2
    # Sanity: the transitive node really did pick up a stale_by entry, so the
    # invariant check below has more than one shape to look at.
    assert _is_stale(core, dv_2b, ob_2), (
        f"setup check: {dv_2b} should be stale_by {ob_2} (transitive cascade)"
    )

    # Cascade 3: shared downstream node accumulates stale markers from two
    # separate supersede calls (diamond-of-supersedes shape, the #231
    # scenario — test_stale_replacement_dict.py, which used to carry it, was
    # deleted by this slice along with the metadata.stale_replacement field
    # it covered).
    ob_3a = _seed_ob(obs, "Premise 3a.")
    ob_3b = _seed_ob(obs, "Premise 3b.")
    dv_shared = _seed_dv(derive, "Derived from both 3a and 3b.", [ob_3a, ob_3b])
    ob_3a_new = _seed_ob(obs, "Refined premise 3a.")
    r3a = json.loads(rev._supersede_impl(ob_3a, ob_3a_new))
    assert "superseded" in r3a.get("status", ""), r3a
    ob_3b_new = _seed_ob(obs, "Refined premise 3b.")
    r3b = json.loads(rev._supersede_impl(ob_3b, ob_3b_new))
    assert "superseded" in r3b.get("status", ""), r3b

    conn = core._get_db()
    try:
        violations = _find_stale_by_invariant_violations(conn)
    finally:
        conn.close()

    assert violations == [], (
        "stale_by invariant violated — the following (node, premise) pairs "
        "name a premise with a NULL superseded_by: "
        + ", ".join(
            f"{node_id} names premise {premise_id}"
            for node_id, premise_id in violations
        )
    )


def test_stale_by_invariant_checker_flags_never_superseded_premise():
    """Negative control: prove the checker above can actually fail.

    Directly fabricates (via a raw cascade_markers INSERT, deliberately NOT
    through the public API — the point is to construct the exact violation
    shape a buggy write-path could introduce) a pending supersede marker
    whose root_node was never superseded, then calls the checker on that
    state and asserts it flags the fabricated pair. If this test cannot
    fail, the positive test above proves nothing.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()

    # never_superseded: an ordinary live node — is_current=1, superseded_by NULL.
    never_superseded = _seed_ob(obs, "Never superseded premise.")
    # victim: a node that will be (falsely, for this test only) marked stale
    # by never_superseded — the shape a buggy write-path would produce.
    victim = _seed_ob(obs, "Falsely-marked-stale node.")

    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO cascade_markers "
            "(node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES (?, 'cev_fabricated_test', ?, 'supersede', 'pending', ?)",
            (victim, never_superseded, core._now()),
        )
        conn.commit()

        violations = _find_stale_by_invariant_violations(conn)
    finally:
        conn.close()

    assert (victim, never_superseded) in violations, (
        f"checker failed to flag the fabricated violation: expected "
        f"({victim!r}, {never_superseded!r}) in violations, got {violations}"
    )


# ---------------------------------------------------------------------------
# immediate_cause / dead_premises — direct unit tests (#2013/#2009 slice 1)
# ---------------------------------------------------------------------------
#
# These derived-read primitives (engram_revision._immediate_cause,
# _dead_premises) have no callers yet in this slice — they're built ahead
# of the resolve/acknowledge write path that will consume them (a later
# slice) and the frontier/mode-C machinery (Borges's proposed "slice 3",
# Cascade-machine room 2026-08-16). Reviewer-fairy flagged the zero-caller
# gap on #2009's first pass: correct on inspection is not the same as
# tested, and a latent join bug here wouldn't surface until a later slice
# starts depending on them. These tests exercise both functions directly
# against a fabricated diamond — Lei's exact case from #2013 (A→B, A→C,
# B+C→D) — since that's the shape that killed the naive singular
# immediate_cause/dead_premises fields in the first place.


def test_immediate_cause_is_one_hop_not_transitive():
    """Chain dv_A -> dv_B -> ob_1 (dv_B derives_from ob_1, dv_A derives_from
    dv_B). A cascade_event marks both ob_1 and dv_B (as a real cascade
    would — ob_1 is the root, dv_B is a visited downstream node).
    immediate_cause(dv_A, event) must return dv_B (dv_A's DIRECT premise
    that carries the marker) — never ob_1, which is two hops out and not
    among dv_A's own direct_premise_ids. This is #1969's direct-premise
    refinement, exercised as a unit test rather than only end-to-end.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()

    ob_1 = _seed_ob(obs, "Root premise.")
    dv_b = _seed_dv(derive, "Direct derivation from the root.", ob_1)
    dv_a = _seed_dv(derive, "Transitive derivation, two hops from the root.", dv_b)

    conn = core._get_db()
    try:
        event = "cev_immediate_cause_test"
        now = core._now()
        # Simulate a real cascade's write pattern: root_node=ob_1 on every
        # marker (root is never discarded), root_op arbitrary for this test.
        for marked_node in (ob_1, dv_b):
            conn.execute(
                "INSERT INTO cascade_markers "
                "(node_id, cascade_event, root_node, root_op, status, created_at) "
                "VALUES (?, ?, ?, 'supersede', 'pending', ?)",
                (marked_node, event, ob_1, now),
            )
        conn.commit()

        cause = rev._immediate_cause(conn, dv_a, event)
    finally:
        conn.close()

    assert cause == [dv_b], (
        f"immediate_cause(dv_a) should be [{dv_b!r}] (the direct premise), "
        f"got {cause}"
    )


def test_dead_premises_excludes_acknowledged_includes_superseded():
    """Lei's diamond: A -> B, A -> C, B + C -> D. B's marker for the event
    resolves 'superseded' (B genuinely changed); C's marker resolves
    'acknowledged' (C's claim survived unchanged — the delta didn't touch
    it). dead_premises(D, event) must include B and EXCLUDE C — this is
    the load-bearing exclusion from Lei's diamond argument (#2013 comment
    2026-08-14T21:23:31Z / Kepler's world crossing seq 1911): a marker
    that merely resolved is not the same as a premise that actually died.
    An unresolved (pending) premise must also be excluded — it hasn't been
    adjudicated yet, so nothing is known to have died.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()

    ob_a = _seed_ob(obs, "Root premise A.")
    dv_b = _seed_dv(derive, "B, derived from A.", ob_a)
    ob_c_base = _seed_ob(obs, "Placeholder base for C.")
    dv_c = _seed_dv(derive, "C, derived from a placeholder (not A directly).", ob_c_base)
    dv_d = _seed_dv(derive, "D, derived from both B and C.", [dv_b, dv_c])

    conn = core._get_db()
    try:
        event = "cev_dead_premises_test"
        now = core._now()

        def _marker(node_id, status="pending", resolution_kind=None):
            conn.execute(
                "INSERT INTO cascade_markers "
                "(node_id, cascade_event, root_node, root_op, status, "
                "resolution_kind, created_at, resolved_at) "
                "VALUES (?, ?, ?, 'supersede', ?, ?, ?, ?)",
                (node_id, event, ob_a, status, resolution_kind, now,
                 now if status == "resolved" else None),
            )

        _marker(dv_b, status="resolved", resolution_kind="superseded")
        _marker(dv_c, status="resolved", resolution_kind="acknowledged")
        conn.commit()

        dead = rev._dead_premises(conn, dv_d, event)
    finally:
        conn.close()

    assert dead == [dv_b], (
        f"dead_premises(dv_d) should be [{dv_b!r}] (superseded, genuinely "
        f"dead) and must EXCLUDE {dv_c!r} (acknowledged — never really "
        f"moved); got {dead}"
    )

    # Negative control: a PENDING (unresolved) marker on a direct premise
    # must not appear in dead_premises either — nothing is known to have
    # died until adjudication resolves it.
    conn2 = core._get_db()
    try:
        conn2.execute(
            "INSERT INTO cascade_markers "
            "(node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES (?, 'cev_dead_premises_pending_test', ?, 'retract', 'pending', ?)",
            (dv_b, ob_a, core._now()),
        )
        conn2.commit()
        dead_with_pending = rev._dead_premises(conn2, dv_d, "cev_dead_premises_pending_test")
    finally:
        conn2.close()

    assert dead_with_pending == [], (
        f"a PENDING marker must not count as a dead premise; got {dead_with_pending}"
    )


# §5.6 #2252 Stage 1.1 — support_lost discharge (state-based, reflect-time)
# ---------------------------------------------------------------------------


def _set_support_lost(core, node_id, value=True):
    """Directly stamp a support_lost marker via SQL, bypassing any hook.

    Used to simulate a marker whose setting event predates this fix (the
    shape of the graph's real specimen: a lesson re-wired before any
    discharge code existed) — no retract/supersede/register_exemplar call
    is made, only a direct metadata write, matching how a marker set before
    this code existed would look today.
    """
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        meta = json.loads(row["metadata"] or "{}")
        if value:
            meta["support_lost"] = True
        else:
            meta.pop("support_lost", None)
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(meta), node_id))
        conn.commit()
    finally:
        conn.close()


def test_reflect_discharges_backlog_marker_with_no_triggering_event():
    """Part 1 — the backlog test that matters.

    A node carries support_lost:true in metadata AND has live exemplars,
    with NO retract/supersede/register_exemplar event anywhere in this test.
    This is the shape of the graph's one real specimen: a lesson whose
    marker's setting event is already in the past by the time any discharge
    code exists.

    A hook keyed on retract/supersede/register-exemplar events CANNOT pass
    this test — there is no event here for a hook to fire on. Only a
    state-based recount at reflect time (Part 1) can discharge it.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Live supporter — never touched by any mutation.")
    cs_id = _seed_cs(cs, ob_id, tag="backlog-marker-cs")

    # Stamp a false-positive marker with no triggering event.
    _set_support_lost(core, cs_id, True)
    assert _get_meta(core, cs_id).get("support_lost") is True, "precondition: marker set"

    reflect_result = json.loads(lifecycle._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    support_lost_ids = [n["id"] for n in reflect_result.get("support_lost_nodes", [])]
    assert cs_id not in support_lost_ids, (
        f"{cs_id} must be discharged (has live exemplars via ob_id) — a "
        f"hook-only fix would leave it marked forever because no event ever "
        f"fires on it. Got support_lost_nodes: {support_lost_ids}"
    )

    cleared_ids = [n["id"] for n in reflect_result.get("support_lost_cleared", [])]
    assert cs_id in cleared_ids, (
        f"Expected {cs_id} in support_lost_cleared, got: {cleared_ids}"
    )

    meta = _get_meta(core, cs_id)
    assert not meta.get("support_lost"), f"metadata marker should be cleared, got: {meta}"

    summary = reflect_result.get("summary", {})
    assert summary.get("support_lost_cleared", 0) >= 1, (
        f"Expected summary.support_lost_cleared >= 1, got: {summary}"
    )


def test_support_lost_mark_restore_remark_round_trip_idempotent():
    """Part 2 — mark -> restore -> re-mark via the write-time hook.

    Also asserts idempotency: exactly one edit_history row per real state
    transition, no duplicates from repeated _detect_zero_support triggers.
    """
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob1 = _seed_ob(obs, "Supporter 1 — retracted first.", url="https://example.com/rt1")
    cs_id = _seed_cs(cs, ob1, tag="round-trip-cs")

    # 1. Mark: retract the sole supporter.
    ret1 = json.loads(rev._retract_impl(
        node_id=ob1, error_type="hallucinated_claim", reason="fabricated 1",
    ))
    assert ret1.get("support_lost_count", 0) == 1, ret1
    assert _get_meta(core, cs_id).get("support_lost") is True

    # 2. Wire two more supporters directly (bypasses any hook on cs itself):
    # ob2 stays live, ob3 will be retracted to re-trigger _detect_zero_support
    # against cs's neighborhood.
    ob2 = _seed_ob(obs, "Supporter 2 — added post-hoc, stays live.", url="https://example.com/rt2")
    ob3 = _seed_ob(obs, "Supporter 3 — added then retracted to re-trigger.", url="https://example.com/rt3")
    conn = core._get_db()
    try:
        now = core._now()
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supported_by', ?)",
            (cs_id, ob2, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supported_by', ?)",
            (cs_id, ob3, now),
        )
        conn.commit()
    finally:
        conn.close()

    # 3. Restore: retracting ob3 re-triggers _detect_zero_support against
    # cs; ob2 is still live, so this should CLEAR support_lost (Part 2).
    ret2 = json.loads(rev._retract_impl(
        node_id=ob3, error_type="hallucinated_claim", reason="fabricated 3",
    ))
    assert "error" not in ret2, ret2
    assert _get_meta(core, cs_id).get("support_lost") is not True, (
        f"cs {cs_id} should be restored (ob2 still live), meta: {_get_meta(core, cs_id)}"
    )
    restored_ids = [n["id"] for n in ret2.get("support_restored", [])]
    assert cs_id in restored_ids, f"Expected {cs_id} in support_restored, got: {restored_ids}"

    # 4. Re-mark: retract the last live supporter ob2.
    ret3 = json.loads(rev._retract_impl(
        node_id=ob2, error_type="hallucinated_claim", reason="fabricated 2",
    ))
    assert ret3.get("support_lost_count", 0) == 1, ret3
    assert _get_meta(core, cs_id).get("support_lost") is True

    # 5. Idempotency: exactly 2 'support_lost' rows (steps 1 and 4) and
    # exactly 1 'support_lost_cleared' row (step 3) for cs_id.
    conn = core._get_db()
    try:
        lost_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM edit_history WHERE node_id = ? AND action = 'support_lost'",
            (cs_id,),
        ).fetchone()["c"]
        cleared_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM edit_history WHERE node_id = ? AND action = 'support_lost_cleared'",
            (cs_id,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert lost_rows == 2, f"Expected exactly 2 support_lost rows, got {lost_rows}"
    assert cleared_rows == 1, f"Expected exactly 1 support_lost_cleared row, got {cleared_rows}"


def test_register_exemplar_clears_support_lost_marker():
    """Part 3 — registering a new exemplar on a marked lesson clears it."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_incident = _seed_ob(obs, "Sole incident — will be retracted.")
    ls_id = _seed_ls(obs, epistemic, "Lesson losing its only incident.", supporting_ids=ob_incident)

    ret = json.loads(rev._retract_impl(
        node_id=ob_incident, error_type="hallucinated_claim", reason="fabricated incident",
    ))
    assert ret.get("support_lost_count", 0) == 1, ret
    assert _get_meta(core, ls_id).get("support_lost") is True

    ob_new = _seed_ob(obs, "New incident registered post-hoc.", url="https://example.com/new-incident")
    reg = json.loads(epistemic._register_exemplar_impl(target_id=ls_id, exemplar_id=ob_new))
    assert "error" not in reg, reg

    meta = _get_meta(core, ls_id)
    assert not meta.get("support_lost"), (
        f"lesson {ls_id} support_lost should clear on new exemplar registration, got: {meta}"
    )


def test_genuinely_unsupported_node_stays_marked_through_all_paths():
    """A node with zero live support must NOT be discharged by any path —
    the discharge machinery must not become a way to lose the warning."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob_id = _seed_ob(obs, "Sole supporter — retracted, nothing replaces it.")
    cs_id = _seed_cs(cs, ob_id, tag="genuinely-unsupported-cs")

    ret = json.loads(rev._retract_impl(
        node_id=ob_id, error_type="hallucinated_claim", reason="fabricated",
    ))
    assert ret.get("support_lost_count", 0) == 1, ret
    assert _get_meta(core, cs_id).get("support_lost") is True

    # Part 1: reflect must NOT discharge a genuinely-unsupported node.
    reflect_result = json.loads(lifecycle._reflect_impl())
    support_lost_ids = [n["id"] for n in reflect_result.get("support_lost_nodes", [])]
    cleared_ids = [n["id"] for n in reflect_result.get("support_lost_cleared", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} to remain in support_lost_nodes, got: {support_lost_ids}"
    )
    assert cs_id not in cleared_ids, (
        f"{cs_id} has zero live support and must NOT appear in support_lost_cleared, "
        f"got: {cleared_ids}"
    )
    assert _get_meta(core, cs_id).get("support_lost") is True


def test_reflect_summary_reports_support_lost_cleared_count():
    """support_lost_cleared appears in the reflect report with a correct count."""
    rev, obs, derive, cs, epistemic, lifecycle, core = _make_fresh_mods()
    ob1 = _seed_ob(obs, "Live supporter for backlog cs 1.", url="https://example.com/sc1")
    cs1 = _seed_cs(cs, ob1, tag="summary-cleared-cs-1")
    ob2 = _seed_ob(obs, "Live supporter for backlog cs 2.", url="https://example.com/sc2")
    cs2 = _seed_cs(cs, ob2, tag="summary-cleared-cs-2")

    _set_support_lost(core, cs1, True)
    _set_support_lost(core, cs2, True)

    reflect_result = json.loads(lifecycle._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    cleared_ids = {n["id"] for n in reflect_result.get("support_lost_cleared", [])}
    assert {cs1, cs2} <= cleared_ids, (
        f"Expected both {cs1} and {cs2} in support_lost_cleared, got: {cleared_ids}"
    )
    summary = reflect_result.get("summary", {})
    assert summary.get("support_lost_cleared", 0) == len(reflect_result.get("support_lost_cleared", [])), (
        f"summary.support_lost_cleared must match list length, got: {summary}"
    )
