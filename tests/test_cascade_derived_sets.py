"""#2252 Stage 1.0b — cascade/provenance relation sets are DERIVED, and the
derivation is load-bearing (not just declaratively true).

Stage 1.0a corrected EDGE_CLASSIFICATIONS (retracts/supersedes lose
cascade:True; derives_from/supported_by gain provenance:True — see
tests/test_cascade_dag_check_audit.py). Stage 1.0b (this file, plus the
engram_core.py / engram_revision.py / engram_lifecycle.py / engram_stats.py /
engram_query.py edits it covers) makes the nine previously-hardcoded SQL
relation literals DERIVE from that declaration instead of re-stating it.

Coverage:
  1. CASCADE_RELATIONS / PROVENANCE_RELATIONS equal the sets Stage 1.0a
     declared.
  1b. Source-hop check (round-2 review fix, #2255; STRENGTHENED round-4):
     the cascade one (engram_revision._CASCADE_RELATIONS_SORTED) is now a
     genuine sourcing proof — it crosses the engram_core -> engram_revision
     module boundary, so a sentinel patched onto the upstream constant
     before the consumer reloads is observable, and a mis-wiring to a
     currently-equal decoy (e.g. CITATION_SUPPORT_RELATIONS) now fails it.
     The provenance twin (engram_core._PROVENANCE_RELATIONS_SORTED) is
     derived from PROVENANCE_RELATIONS by two sequential statements INSIDE
     THE SAME MODULE, with no module boundary to intercept via reload — it
     remains a value-equality check, not a sourcing proof; see that test's
     own docstring for why and what gap that leaves open.
  1c. Guard for the residual gap 1b's provenance twin discloses (#2255
     colleague-review fix): what makes that value-equality check safe today
     is that no OTHER module-level relation frozenset in engram_core is
     value-equal to PROVENANCE_RELATIONS. This test asserts that fact
     directly instead of leaving it as an unmeasured claim in a docstring.
     When it fails, the disclosed gap has gone from theoretical to live.
  2. The Layer-0 check that is the point of this stage: flipping the derived
     cascade-relation constants that engram_revision.py's three walkers share
     changes which edges the retract/supersede cascade actually traverses.
     This is checked to be able to FAIL (see the test's own docstring) —
     temporarily re-hardcoding the old SQL literal at those sites makes it
     go red.
  2b. Same Layer-0 shape (round-2 review fix, #2255) for the provenance-walk
     site, engram_core.py's _trace_evidence_roots: flipping the derived
     provenance-relation constants changes which edges the evidence-root
     trace actually walks. Also checked to be able to FAIL.
  3. CITATION_SUPPORT_RELATIONS is NOT derived from CASCADE_RELATIONS — a
     regression guard for the Stage 1.3 hazard the constant's own comment
     documents (removing cascade from supported_by must not silently change
     the citation-coverage metric). CASCADE_RELATIONS and
     CITATION_SUPPORT_RELATIONS are value-equal TODAY, which is exactly why
     tests 1b/2b matter: a constant mis-wired to the wrong upstream set would
     pass every other test here and only break silently once Stage 1.3
     diverges the two sets.
  4. cascade ⇒ dag still holds (#2357 renamed dag_check -> temporal_causal;
     this invariant now follows `dag`, re-confirmed here; the authoritative
     test is test_cascade_dag_check_audit.py).
  6. (#2298, 2026-08-28) _trace_observation_leaves's premise-spine walk now
     DERIVES from CASCADE_RELATIONS via the shared _walk_cascade_upstream
     primitive — superseding, for this walk specifically, the #2255
     "leave enumerated" doctrine tests 1b/1c/2/2b/3 exist to protect for the
     OTHER two sites. Same Layer-0 can-it-fail shape as test 2/2b, plus a
     second test proving "one traversal, two filters" shares one function.

Import-pattern contract (#948 wave B — R1): imports engram_core and
engram_revision (plus engram_observation / engram_derive for fixture
seeding) directly; contains no ``import server``.
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

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_observation
import engram_revision

from engram_core import (
    CASCADE_RELATIONS,
    CITATION_SUPPORT_RELATIONS,
    EDGE_CLASSIFICATIONS,
    PROVENANCE_RELATIONS,
)


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cascade_derived_sets_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


# ---------------------------------------------------------------------------
# 1 & 3 — static set-equality assertions (no DB needed)
# ---------------------------------------------------------------------------


def test_cascade_relations_equals_the_stage_1_0a_set():
    """CASCADE_RELATIONS == {derives_from, acknowledged_derives_from} — the
    set #2357 produces (Stage 1.0a narrowed the declaration to
    {derives_from, supported_by}; Stage 1.3 narrowed it again by removing
    cascade from `supported_by`, which post-1.3 is NCB accumulation with no
    claim to re-adjudicate; #2357 widened it back to two by adding
    `acknowledged_derives_from`, which must cascade — that's the entire
    reason it exists). See tests/test_cascade_dag_check_audit.py's
    _EXPECTED_CASCADE_RELATIONS for the change-detector on the declaration
    itself; this test asserts the DERIVED constant tracks it."""
    assert CASCADE_RELATIONS == frozenset({"derives_from", "acknowledged_derives_from"})


def test_provenance_relations_equals_the_stage_1_0a_set():
    """PROVENANCE_RELATIONS == {cites, derives_from, supported_by,
    predicted_by, acknowledged_derives_from} — matches what
    engram_core.py's _trace_evidence_roots (the provenance walk) actually
    traces.

    #2252 W6 added `predicted_by`, provenance:True, matching the
    `supported_by` pairing edge it was split out of — a trace starting from a
    prediction node must still walk through to the paired observation's own
    evidence roots, exactly as it did when the pairing edge was still named
    `supported_by`. #2357 (2026-08-29) added `acknowledged_derives_from`,
    provenance:True — an acknowledgement is a provenance-chain edge, mirroring
    `derives_from`.
    """
    assert PROVENANCE_RELATIONS == frozenset(
        {"cites", "derives_from", "supported_by", "predicted_by",
         "acknowledged_derives_from"}
    )


def test_cascade_relations_sorted_sourced_from_engram_core_CASCADE_RELATIONS():
    """Round-2 review fix (#2255); STRENGTHENED round-4 review (#2255 round
    4): a plain value-equality assertion (``_CASCADE_RELATIONS_SORTED ==
    sorted(CASCADE_RELATIONS)``) does NOT prove sourcing — it cannot tell a
    correctly-wired constant apart from one mis-wired to a DIFFERENT,
    currently-equal upstream frozenset. Proven live by the round-4 reviewer,
    by mutation: temporarily rewiring engram_revision.py's
    ``_CASCADE_RELATIONS_SORTED = sorted(core.CASCADE_RELATIONS)`` to read
    ``sorted(core.CITATION_SUPPORT_RELATIONS)`` instead (value-equal to
    CASCADE_RELATIONS today — see
    test_citation_support_relations_not_derived_from_cascade below) left
    every test in this file, including this one, green.

    So this test now proves the source hop directly. It patches
    ``engram_core.CASCADE_RELATIONS`` to a sentinel value that cannot
    coincide with any other frozenset in the module (today or after any
    future stage), then reloads ``engram_revision`` fresh so its
    module-level ``_CASCADE_RELATIONS_SORTED = sorted(core.CASCADE_RELATIONS)``
    line re-executes against the patched upstream (the patched
    ``engram_core`` module object stays in ``sys.modules`` across the
    reload, so ``engram_revision``'s ``import engram_core as core`` binds
    to the patched object rather than re-importing a clean one). A
    constant sourced from the right upstream set then tracks the sentinel;
    one sourced from anything else does not, regardless of whether that
    anything-else happens to be value-equal today.

    Can-it-fail check performed by hand (per the #2255 round-4 review
    brief): temporarily rewired engram_revision.py's assignment to read
    ``sorted(core.CITATION_SUPPORT_RELATIONS)`` — the exact decoy the
    round-4 reviewer demonstrated — ran this test, and watched it go RED
    (``_CASCADE_RELATIONS_SORTED`` stayed pinned to
    CITATION_SUPPORT_RELATIONS's value instead of tracking the sentinel,
    since patching ``engram_core.CASCADE_RELATIONS`` no longer has any
    effect on it), then reverted.
    """
    sentinel = frozenset({"__sentinel_a__", "__sentinel_b__"})
    try:
        core = fresh_module(engram_core)
        core.CASCADE_RELATIONS = sentinel
        rev = importlib.import_module("engram_revision")
        assert rev._CASCADE_RELATIONS_SORTED == sorted(sentinel), (
            "engram_revision._CASCADE_RELATIONS_SORTED did not track a "
            "sentinel patched onto engram_core.CASCADE_RELATIONS — the "
            "module constant is not genuinely sourced from it (source-hop "
            "broken, or re-hardcoded to a different set)"
        )
        assert rev._CASCADE_RELATION_PLACEHOLDERS == ",".join(
            "?" * len(rev._CASCADE_RELATIONS_SORTED)
        )
    finally:
        # Restore sys.modules to a clean, unpatched engram_core so later
        # tests in this file (and any later fresh_module()/import_module()
        # call elsewhere in the suite) never observe the sentinel. This
        # file's own top-level `engram_core` / `engram_revision` names were
        # never rebound by the block above, so they were never at risk —
        # this is purely about sys.modules hygiene for what runs after.
        fresh_module(engram_core)


def test_provenance_relations_sorted_sourced_from_engram_core_PROVENANCE_RELATIONS():
    """Round-2 review fix (#2255). NOT a sourcing proof, despite the name —
    round-4 review (#2255 round 4) found this test's docstring made the
    same "sourced from the right upstream set, not merely equal to it by
    coincidence" claim as the CASCADE test above, but only that test could
    actually be strengthened into one (see its docstring for the fix and
    the can-it-fail check that confirms it).

    Why this one can't: the CASCADE test's sourcing proof works by patching
    the upstream constant (engram_core.CASCADE_RELATIONS) and reloading the
    CONSUMING module (engram_revision) fresh, so the consumer's
    module-level assignment re-executes against the patched value — that
    only works because the assignment being tested lives on the far side of
    a real module boundary. ``_PROVENANCE_RELATIONS_SORTED`` has no such
    boundary: it is derived from ``PROVENANCE_RELATIONS`` by two sequential
    top-level statements INSIDE engram_core.py itself, executed by the same
    single ``import``/``reload`` of that one module. Verified empirically
    (round-4, by hand): setting ``engram_core.PROVENANCE_RELATIONS`` to a
    sentinel value after import leaves the already-computed
    ``_PROVENANCE_RELATIONS_SORTED`` untouched (nothing re-runs the
    assignment), and ``importlib.reload(engram_core)`` re-executes BOTH
    statements from source in one pass and silently discards the patch —
    there is no reload-based interception point between two statements in
    the same module body the way there is across a module boundary.

    RESIDUAL GAP, left open rather than papered over: this test is a
    value-equality check, not a sourcing proof. A future edit that
    mis-wires ``_PROVENANCE_RELATIONS_SORTED`` to some OTHER upstream
    frozenset that happens to be value-equal to ``PROVENANCE_RELATIONS`` at
    the time would pass this test undetected — the same shape of gap the
    CASCADE test used to have. No such decoy exists in engram_core.py today
    (no other frozenset there equals {cites, derives_from, supported_by}),
    but nothing about this test would catch one being introduced later.
    """
    assert engram_core._PROVENANCE_RELATIONS_SORTED == sorted(
        engram_core.PROVENANCE_RELATIONS
    )
    assert engram_core._PROVENANCE_RELATION_PLACEHOLDERS == ",".join(
        "?" * len(engram_core._PROVENANCE_RELATIONS_SORTED)
    )
    # See docstring: value-equality check only, not a sourcing proof — no
    # reload-based interception point exists for a same-module derivation
    # the way there is for the cascade source-hop test above.


def test_no_other_module_frozenset_equals_provenance_relations():
    """Guards the residual gap the sibling test above discloses (#2255
    colleague-review fix, following up on the round-4 reviewer's finding):
    ``test_provenance_relations_sorted_sourced_from_engram_core_PROVENANCE_RELATIONS``
    is a value-equality check, not a sourcing proof, and is only safe today
    because no OTHER module-level relation frozenset in ``engram_core`` is
    value-equal to ``PROVENANCE_RELATIONS``. That "no such decoy exists
    today" claim was previously just a sentence in a docstring; this test
    measures it directly, so a future mis-wire to a same-valued decoy is
    caught even where the value-equality check structurally cannot see it.

    Deciding what counts as a "relation frozenset": a module-level
    ``frozenset`` whose elements are ALL relation names appearing in
    ``EDGE_CLASSIFICATIONS``. This is what excludes e.g.
    ``SPECIAL_TYPES_BYPASS`` (elements are node *types* like "axiom" /
    "person", not edge relations) — it is not the kind of set that could
    collide with ``PROVENANCE_RELATIONS`` in the first place, so it is not
    a candidate decoy and skipping it is not weakening the guard.

    When this test fails, the disclosed gap has gone from theoretical to
    live: a second module-level frozenset now matches PROVENANCE_RELATIONS
    by value, and the sibling test's residual gap can silently mis-wire
    without either test catching it. Strengthen the sourcing proof (or
    reconsider the new constant) rather than adjusting this assertion to
    tolerate it.

    Can-it-fail check performed by hand (per the #2255 colleague-review
    follow-up): temporarily added
    ``_DECOY_PROVENANCE_RELATIONS = frozenset({"cites", "derives_from",
    "supported_by"})`` at module level in engram_core.py, ran this test,
    and watched it go RED (``matches == ["_DECOY_PROVENANCE_RELATIONS"]``
    instead of ``[]``), then reverted and confirmed green again.
    """
    relation_names = set(EDGE_CLASSIFICATIONS.keys())
    matches = []
    for name, value in vars(engram_core).items():
        if name == "PROVENANCE_RELATIONS":
            continue
        if not isinstance(value, frozenset):
            continue
        if not value or not value.issubset(relation_names):
            continue  # not a relation-name frozenset, e.g. SPECIAL_TYPES_BYPASS
        if value == PROVENANCE_RELATIONS:
            matches.append(name)
    assert matches == [], (
        "module-level frozenset(s) other than PROVENANCE_RELATIONS are now "
        f"value-equal to it: {matches}. See "
        "test_provenance_relations_sorted_sourced_from_engram_core_PROVENANCE_RELATIONS's "
        "docstring — its value-equality check can no longer distinguish "
        "correct sourcing from a mis-wire to one of these decoys. "
        "Strengthen that test (or investigate the new constant) rather "
        "than silencing this failure."
    )


def test_citation_support_relations_not_derived_from_cascade():
    """Regression guard for the Stage 1.3 hazard: CITATION_SUPPORT_RELATIONS
    is a NEW ENUMERATED CONSTANT, deliberately not derived from
    CASCADE_RELATIONS, per #2252 Stage 1.0b table row 2. At Stage 1.0b's
    authoring the two sets happened to be equal by VALUE (both
    {derives_from, supported_by}) — that coincidence is exactly what this
    test guards against silently breaking: when Stage 1.3 removed cascade
    from supported_by, CASCADE_RELATIONS shrank to {derives_from} while
    CITATION_SUPPORT_RELATIONS did NOT, or the uncited-observation metric
    (engram_lifecycle.py, engram_stats.py) would have silently changed
    meaning with no test to catch it.

    Both sets have since grown independently, in the SAME direction this
    test exists to police: #2013 §12.5 added `acknowledged_derives_from` to
    CASCADE_RELATIONS (cascade must reach an acknowledging node when its
    replacement is later superseded); #2349 blocker 2 separately added it to
    CITATION_SUPPORT_RELATIONS (a node that has acknowledged holds a real
    one-hop premise the citation-coverage metric must be able to see). The
    two constants are NOT value-equal today — CASCADE_RELATIONS is
    {derives_from, acknowledged_derives_from}, CITATION_SUPPORT_RELATIONS is
    {derives_from, supported_by, acknowledged_derives_from} — which is
    itself evidence the two questions really are independent, not that one
    silently tracks the other.

    This test asserts the thing that actually matters regardless of the
    two sets' current value relationship: CITATION_SUPPORT_RELATIONS is its
    own independent constant, not an alias/derivation of CASCADE_RELATIONS,
    so a future narrowing of one cannot silently narrow the other. Checked
    by identity-independence: the two names must not be the SAME frozenset
    object bound by construction to depend on `cascade`.
    """
    assert CITATION_SUPPORT_RELATIONS == frozenset(
        {"derives_from", "supported_by", "acknowledged_derives_from"}
    )
    # The guard is that CITATION_SUPPORT_RELATIONS is NOT computed
    # from EDGE_CLASSIFICATIONS' cascade flag the way CASCADE_RELATIONS is:
    assert CITATION_SUPPORT_RELATIONS is not CASCADE_RELATIONS
    computed_from_cascade_flag = frozenset(
        r for r, c in EDGE_CLASSIFICATIONS.items() if c["cascade"]
    )
    assert computed_from_cascade_flag == CASCADE_RELATIONS
    # The real guard is source-level: engram_lifecycle.py / engram_stats.py
    # must reference core.CITATION_SUPPORT_RELATIONS-derived placeholders,
    # not core.CASCADE_RELATIONS. Verified by test 5 below via the DB-level
    # citation-coverage query itself.


def test_cascade_implies_dag_check_still_holds():
    """cascade ⇒ dag still holds after Stage 1.0a's correction.

    #2357 renamed dag_check -> temporal_causal and added the new `dag` (real
    acyclicity) flag; the cascade⇒X invariant follows `dag`, not
    `temporal_causal` — a cascade relation deliberately backwards in time
    (acknowledged_derives_from) is exactly the case that must still be
    dag-safe without being temporal_causal. The authoritative test is
    test_cascade_dag_check_audit.py's test_cascade_implies_dag_check; this is
    a light re-confirmation scoped to the two relations Stage 1.0a actually
    touched, so a regression here is attributable to this stage without
    needing to cross-reference the other file.
    """
    for relation in CASCADE_RELATIONS:
        assert EDGE_CLASSIFICATIONS[relation]["dag"], (
            f"{relation} carries cascade=True but dag=False"
        )
    # The two relations this stage specifically corrected must both still
    # carry dag=True even though cascade dropped to False — dag (like the
    # temporal_causal it was split from) is a separate, unrelated design
    # dimension (retracts/supersedes are legitimately acyclic edges
    # regardless of whether they cascade).
    assert EDGE_CLASSIFICATIONS["retracts"]["dag"] is True
    assert EDGE_CLASSIFICATIONS["supersedes"]["dag"] is True


# ---------------------------------------------------------------------------
# 2 — Layer-0: declaration change ⇒ behaviour change
# ---------------------------------------------------------------------------


def _make_fresh_mods():
    """Reload engram_revision + friends against a clean temp DB.

    Minimal variant of test_cascade_semantics.py's helper of the same name —
    duplicated rather than cross-imported (no precedent in this repo for
    importing across test_*.py files, and the two files are independently
    selectable by run_touched_tests.py's import graph this way).
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
        quote_type="attributed_analysis",
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


def _is_tainted(core, node_id, by_id):
    """Migrated off metadata.tainted_by to cascade_markers (#2013/#2009
    slice 1). Test INTENT is unchanged — "did by_id's retract cascade reach
    node_id" — only the store moved. Written as a marker read because
    metadata is no longer where cascade state lives.
    """
    return _has_pending_marker(core, node_id, by_id, "retract")


def _has_pending_marker(core, node_id, by_id, root_op):
    conn = core._get_db()
    try:
        return conn.execute(
            "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
            "AND root_op = ? AND status = 'pending'",
            (node_id, by_id, root_op),
        ).fetchone() is not None
    finally:
        conn.close()


def _is_stale(core, node_id, by_id):
    """Marker-store twin of _is_tainted above; same intent, same migration."""
    return _has_pending_marker(core, node_id, by_id, "supersede")


def test_retract_cascade_derives_from_CASCADE_RELATIONS_not_hardcoded(monkeypatch):
    """The retract cascade (engram_revision.py's _retract_impl first-hop
    query, #2252 Stage 1.0b table row 1's site, plus the shared recursive
    walker _walk_cascade_downstream) walks exactly the
    relations named by the module's derived
    _CASCADE_RELATIONS_SORTED / _CASCADE_RELATION_PLACEHOLDERS constants —
    not a re-hardcoded SQL literal.

    Proof structure: with the REAL derived constants (today equal to
    CASCADE_RELATIONS == {derives_from, supported_by}), retracting an
    observation taints a downstream derivation linked via derives_from —
    ordinary, expected cascade behaviour. Then, in a fresh scenario, the
    SAME module-level constants are monkeypatched to a relation set that
    does NOT include derives_from. If the walk is genuinely derived, the
    retract must now leave the downstream derivation UNtainted, because the
    derives_from edge no longer matches the (patched) walked-relation set.

    This can fail: if either query — in _walk_cascade_downstream or in
    _retract_impl's first-hop query — were re-hardcoded back
    to the literal ``relation IN ('derives_from', 'supported_by')``,
    monkeypatching the module constants would have no effect and the
    downstream derivation would still get tainted — the assertion below
    would fail. (Verified by hand during authoring: temporarily reverting
    the derivation to the old literal turns this test red.)
    """
    rev, obs, derive, core = _make_fresh_mods()

    # Sanity leg — REAL derived constants, ordinary cascade behaviour.
    ob_id = _seed_ob(obs, "Retract-path premise (real constants).")
    dv_id = _seed_dv(derive, "Retract-path derivation.", ob_id)
    ret = json.loads(rev._retract_impl(
        node_id=ob_id, error_type="hallucinated_claim", reason="wrong",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret, ret
    assert _is_tainted(core, dv_id, ob_id), (
        "sanity leg: with the real derived cascade set, derives_from must "
        "still cascade taint"
    )

    # Flip leg — patch the derived constants away from derives_from/
    # supported_by, on a fresh DB/node pair.
    rev, obs, derive, core = _make_fresh_mods()
    monkeypatch.setattr(rev, "_CASCADE_RELATIONS_SORTED", ["cites"])
    monkeypatch.setattr(rev, "_CASCADE_RELATION_PLACEHOLDERS", "?")

    ob_id2 = _seed_ob(obs, "Retract-path premise (patched constants).")
    dv_id2 = _seed_dv(derive, "Retract-path derivation, patched.", ob_id2)
    ret2 = json.loads(rev._retract_impl(
        node_id=ob_id2, error_type="hallucinated_claim", reason="wrong",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret2, ret2
    assert not _is_tainted(core, dv_id2, ob_id2), (
        "flip leg: with the walked-relation constants patched away from "
        "derives_from, the retract cascade must NOT taint the downstream "
        "derivation. A taint here means the walk is not actually reading "
        "the module's derived constants (i.e. it has drifted back to a "
        "hardcoded literal)."
    )


def test_supersede_cascade_derives_from_CASCADE_RELATIONS_not_hardcoded(monkeypatch):
    """Same proof as the retract test above, for the supersede cascade
    (_supersede_impl's first-hop query, #2252 Stage 1.0b table row 1's site,
    plus the shared recursive walker _walk_cascade_downstream)."""
    rev, obs, derive, core = _make_fresh_mods()

    # Sanity leg — REAL derived constants.
    ob_id = _seed_ob(obs, "Supersede-path premise (real constants).")
    dv_old = _seed_dv(derive, "Old derivation.", ob_id)
    dv_downstream = _seed_dv(derive, "Downstream of old.", dv_old)
    dv_new = _seed_dv(derive, "Refined derivation.", ob_id)

    ret = json.loads(rev._supersede_impl(dv_old, dv_new))
    assert "superseded" in ret.get("status", ""), ret
    assert _is_stale(core, dv_downstream, dv_old), (
        "sanity leg: with the real derived cascade set, derives_from must "
        "still cascade stale"
    )

    # Flip leg — patch away from derives_from/supported_by.
    rev, obs, derive, core = _make_fresh_mods()
    monkeypatch.setattr(rev, "_CASCADE_RELATIONS_SORTED", ["cites"])
    monkeypatch.setattr(rev, "_CASCADE_RELATION_PLACEHOLDERS", "?")

    ob_id2 = _seed_ob(obs, "Supersede-path premise (patched constants).")
    dv_old2 = _seed_dv(derive, "Old derivation, patched.", ob_id2)
    dv_downstream2 = _seed_dv(derive, "Downstream of old, patched.", dv_old2)
    dv_new2 = _seed_dv(derive, "Refined derivation, patched.", ob_id2)

    ret2 = json.loads(rev._supersede_impl(dv_old2, dv_new2))
    assert "superseded" in ret2.get("status", ""), ret2
    assert not _is_stale(core, dv_downstream2, dv_old2), (
        "flip leg: with the walked-relation constants patched away from "
        "derives_from, the supersede cascade must NOT mark the downstream "
        "derivation stale. A stale mark here means the walk has drifted "
        "back to a hardcoded literal."
    )


# ---------------------------------------------------------------------------
# 2b — Layer-0, provenance-walk site: same shape as the cascade tests above,
#      for _trace_evidence_roots (engram_core.py). Round-2 review fix (#2255).
# ---------------------------------------------------------------------------


def test_trace_evidence_roots_derives_from_PROVENANCE_RELATIONS_not_hardcoded(
    monkeypatch,
):
    """The provenance walk (``_trace_evidence_roots`` in engram_core.py)
    walks exactly the relations named by the module's derived
    ``_PROVENANCE_RELATIONS_SORTED`` / ``_PROVENANCE_RELATION_PLACEHOLDERS``
    constants — not a re-hardcoded SQL literal. Same proof shape as the
    retract/supersede cascade tests above, applied to the provenance site.

    Proof structure: with the REAL derived constants (today equal to
    PROVENANCE_RELATIONS == {cites, derives_from, supported_by}), tracing
    an observation back through its 'cites' edge finds its evidence root —
    ordinary, expected behaviour. Then, on a fresh observation, the SAME
    module-level constants are monkeypatched to a relation set that
    excludes 'cites'. If the walk is genuinely derived, the same trace must
    now find NO root, because the cites edge no longer matches the
    (patched) walked-relation set.

    This can fail: if the query in ``_trace_evidence_roots`` were
    re-hardcoded back to a literal ``relation IN (...)`` that still
    includes 'cites', monkeypatching the module constants would have no
    effect and the root would still be found — the flip-leg assertion
    below would fail.

    Can-it-fail check performed by hand before this test was committed
    (per the #2255 round-2 review brief): temporarily re-hardcoded
    ``_trace_evidence_roots``'s edge query back to the literal
    ``relation IN ('cites', 'derives_from', 'supported_by')``, ran this
    test, and watched the flip-leg assertion go red (roots2 was non-empty
    instead of empty) — then reverted the hardcoding. Confirms the test
    genuinely exercises the derivation rather than passing vacuously.
    """
    rev, obs, derive, core = _make_fresh_mods()

    # Sanity leg — REAL derived constants: ordinary evidence-root trace.
    ob_id = _seed_ob(obs, "Provenance-path premise (real constants).")
    conn = core._get_db()
    try:
        roots = core._trace_evidence_roots(conn, ob_id)
    finally:
        conn.close()
    assert len(roots) == 1, (
        "sanity leg: with the real derived provenance set, tracing an "
        "observation's cites edge must find its evidence root"
    )

    # Flip leg — patch the derived constants away from 'cites'.
    monkeypatch.setattr(core, "_PROVENANCE_RELATIONS_SORTED", ["derives_from"])
    monkeypatch.setattr(core, "_PROVENANCE_RELATION_PLACEHOLDERS", "?")

    conn = core._get_db()
    try:
        roots2 = core._trace_evidence_roots(conn, ob_id)
    finally:
        conn.close()
    assert roots2 == set(), (
        "flip leg: with the walked-relation constants patched away from "
        "'cites', tracing the same observation must find NO evidence "
        "root. A non-empty result here means the walk is not actually "
        "reading the module's derived constants (i.e. it has drifted "
        "back to a hardcoded literal)."
    )


# ---------------------------------------------------------------------------
# 5 — citation-coverage query genuinely uses CITATION_SUPPORT_RELATIONS
#     (DB-level check that engram_lifecycle.py / engram_stats.py did not
#     silently keep the old hardcoded literal instead of wiring the new
#     constant)
# ---------------------------------------------------------------------------


def test_citation_coverage_uses_citation_support_relations_placeholders():
    """engram_lifecycle.py and engram_stats.py's uncited-observation checks
    must reference the SAME sorted placeholder set as
    core.CITATION_SUPPORT_RELATIONS (not a re-hardcoded SQL literal and not
    core.CASCADE_RELATIONS). A source-level check: both modules'
    module-level derived constants must equal
    sorted(engram_core.CITATION_SUPPORT_RELATIONS).
    """
    import engram_lifecycle
    import engram_stats

    expected = sorted(CITATION_SUPPORT_RELATIONS)
    assert engram_lifecycle._CITATION_SUPPORT_RELATIONS_SORTED == expected
    assert engram_stats._CITATION_SUPPORT_RELATIONS_SORTED == expected


# ---------------------------------------------------------------------------
# 6 — #2298: _trace_observation_leaves's premise-spine walk now DERIVES from
#     CASCADE_RELATIONS (superseding #2255's "leave enumerated" doctrine for
#     this specific walk — see engram_core._trace_observation_leaves's
#     docstring for the ruling). Same Layer-0 shape as tests 2/2b above,
#     applied to the shared upstream walker, plus a second test proving the
#     "one traversal, two filters" unification actually shares one function
#     rather than two similarly-shaped ones.
# ---------------------------------------------------------------------------


def test_trace_observation_leaves_derives_from_CASCADE_RELATIONS_not_hardcoded(
    monkeypatch,
):
    """``_trace_observation_leaves`` (via the shared ``_walk_cascade_upstream``)
    walks exactly the relations named by ``engram_core``'s derived
    ``_CASCADE_RELATIONS_SORTED`` / ``_CASCADE_RELATION_PLACEHOLDERS``
    constants — not a re-hardcoded ``('derives_from', 'supported_by')``
    literal. Same proof shape as
    ``test_retract_cascade_derives_from_CASCADE_RELATIONS_not_hardcoded`` and
    ``test_trace_evidence_roots_derives_from_PROVENANCE_RELATIONS_not_hardcoded``
    above, applied to the premise-spine site.

    Proof structure: with the REAL derived constants (today
    ``CASCADE_RELATIONS == {derives_from}``), tracing a derivation back
    through its ``derives_from`` edge finds its observation premise —
    ordinary, expected behaviour. Then, on a fresh derivation, the SAME
    module-level constants are monkeypatched to a relation set that excludes
    ``derives_from``. If the walk is genuinely derived, the same trace must
    now find NO leaves, because the ``derives_from`` edge no longer matches
    the (patched) walked-relation set.

    This can fail: if ``_walk_cascade_upstream``'s edge query were
    re-hardcoded back to a literal
    ``relation IN ('derives_from', 'supported_by')``, monkeypatching the
    module constants would have no effect and the leaf would still be
    found — the flip-leg assertion below would fail.

    Can-it-fail check performed by hand before this test was committed:
    temporarily re-hardcoded ``_walk_cascade_upstream``'s edge query back to
    that literal, ran this test, and watched the flip-leg assertion go red
    (leaves2 was non-empty instead of empty) — then reverted the hardcoding.
    """
    rev, obs, derive, core = _make_fresh_mods()

    # Sanity leg — REAL derived constants: ordinary leaf-trace.
    ob_id = _seed_ob(obs, "Premise-spine premise (real constants).")
    dv_id = _seed_dv(derive, "Derived from the premise (real constants).", [ob_id])
    conn = core._get_db()
    try:
        leaves = core._trace_observation_leaves(conn, dv_id)
    finally:
        conn.close()
    assert leaves == {ob_id}, (
        "sanity leg: with the real derived cascade set, tracing a "
        f"derivation's derives_from edge must find its observation leaf, got {leaves}"
    )

    # Flip leg — patch the derived constants away from 'derives_from'.
    monkeypatch.setattr(core, "_CASCADE_RELATIONS_SORTED", ["retracts"])
    monkeypatch.setattr(core, "_CASCADE_RELATION_PLACEHOLDERS", "?")

    conn = core._get_db()
    try:
        leaves2 = core._trace_observation_leaves(conn, dv_id)
    finally:
        conn.close()
    assert leaves2 == set(), (
        "flip leg: with the walked-relation constants patched away from "
        "'derives_from', tracing the same derivation must find NO "
        "observation leaves. A non-empty result here means the walk is not "
        "actually reading the module's derived constants (i.e. it has "
        "drifted back to a hardcoded literal)."
    )


def test_walk_cascade_upstream_full_ancestor_mode():
    """``_walk_cascade_upstream`` is ONE shared traversal serving two
    filters (#2298 §2's "one traversal, two filters" unification), not two
    independently-shaped walks that happen to look similar. Proven by
    exercising the SAME function with different ``collect_if``/``descend_if``
    predicates over the SAME two-hop chain (``ob -> dv1 -> dv2``) and getting
    genuinely different result shapes:

      - leaf-trace mode (what ``_trace_observation_leaves`` uses): stops AT
        the observation, returning only ``{ob_id}`` — dv1 is descended past,
        never collected.
      - full-ancestor mode (``collect_if=descend_if=lambda row: True``): the
        raw transitive closure via ``CASCADE_RELATIONS`` — every node on the
        premise spine, ``{dv2_id, dv1_id, ob_id}``. This is the ``ancestors(X)``
        primitive the #2013 cascade machine's mode-C travel filters down to
        ``ancestors(X) ∩ pending`` once the marker table lands (slice 1) —
        NOT built here (that intersection is the #2013 slice-3 row), but the
        shared traversal it will filter is.

    If these two modes required separate walk functions instead of one
    parameterised by predicates, this test would need to import two
    differently-named functions; it imports (and calls) exactly one.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_id = _seed_ob(obs, "Full-ancestor-mode premise.")
    dv1_id = _seed_dv(derive, "Intermediate derivation.", [ob_id])
    dv2_id = _seed_dv(derive, "Final derivation.", [dv1_id])

    conn = core._get_db()
    try:
        leaf_mode = core._trace_observation_leaves(conn, dv2_id)
        full_mode = core._walk_cascade_upstream(
            conn, dv2_id,
            collect_if=lambda row: True,
            descend_if=lambda row: True,
        )
    finally:
        conn.close()

    assert leaf_mode == {ob_id}, (
        f"leaf-trace mode must terminate AT the observation, got {leaf_mode}"
    )
    assert full_mode == {dv2_id, dv1_id, ob_id}, (
        "full-ancestor mode must collect every node on the premise spine "
        f"(the raw ancestors(X) set), got {full_mode}"
    )
    assert leaf_mode != full_mode, (
        "the two filters must produce genuinely different results over the "
        "same chain, or this test cannot distinguish 'one traversal, two "
        "filters' from 'two hardcoded near-duplicate walks'"
    )
