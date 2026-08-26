"""cascade ⇒ dag_check consistency invariant over EDGE_CLASSIFICATIONS.

This is declarative-consistency plus forward-looking groundwork, not a
live termination guard, and it's worth being precise about why. Today
`cascade` is an annotation with exactly one runtime consumer —
viz_server.py, which reads it to choose solid-vs-dashed edge rendering in
the graph visualiser. Nothing else reads the flag: every cascade walker
(three in engram_revision.py, one each in engram_core.py,
engram_lifecycle.py, and engram_stats.py) hardcodes its walked-relation
set as a SQL literal, ``relation IN ('derives_from', 'supported_by')``,
rather than deriving it from `cascade`. So a relation carrying
`cascade: True` without `dag_check: True` does not, today, cause any walk
to run over it that wouldn't otherwise.

Nor does a cascade walk's termination depend on acyclicity. Each walker
(e.g. `_walk_cascade_downstream` in engram_revision.py) carries its own
`visited` set, checked BEFORE recursing, so a cycle in the underlying
graph is visited once and then skipped — no hang, no RecursionError,
regardless of dag_check. And `dag_check`'s own init-time check
(engram_core.py) is warn-only: it runs once at startup, compares
`created_at` ordering (not actual cycle detection — a genuine cycle among
equal-timestamp nodes evades it), `LIMIT 5`s the result, calls
`logging.warning`, and the whole block is wrapped in
``try: ... except Exception: pass  # Don't block startup on check
failures``. It rejects nothing, at startup or at edge-creation time.

So why assert this hard? Two reasons, both about keeping the design
honest rather than preventing a today-hazard:
  - `cascade` and `dag_check` are meant to co-vary — a relation that
    propagates taint/stale state should also be guaranteed acyclic, even
    though nothing currently wires the first to depend on the second.
    cascade:True with dag_check:False is a flag combination that asserts
    something about the relation's design while nothing enforces the
    property it implies.
  - It's groundwork for engram-alpha#2013's planned cascade machine, which
    intends to make `cascade` load-bearing — driving the walked-relation
    set from the flag instead of the hardcoded SQL literal. A flag that
    has never been load-bearing is exactly the kind that drifts unnoticed
    before the day something starts depending on it; this test is what
    keeps that drift from accumulating silently in the meantime.

Contrast with test_edge_tier_audit.py, but precisely: that file is not
purely a non-blocking audit — its own docstring calls itself a
"completeness gate + soft warning", and it ships hard-failing
completeness tests (every relation has a tier and vice versa, every tier
value is valid) alongside one soft-warning test,
`test_dag_check_tier_drift_warn` (`warnings.warn` on divergence, always
passes). It's specifically that soft-warning test this file's check is
unlike: a tier's dag_check *expectation* is a design convention with a
documented escape hatch (EDGE_TIER_DIVERGENCE_ACK) for relations that
intentionally diverge from it. cascade⇒dag_check has no such escape
hatch — there is no legitimate use for cascade:True with dag_check:False,
so this is asserted HARD rather than warned.

Import-pattern contract (#948 wave C): imports engram_core directly;
contains no ``import server``.
"""

from engram_core import EDGE_CLASSIFICATIONS

# Change-detector: the current cascade-carrying relation set. Deliberately
# not derived from EDGE_CLASSIFICATIONS itself (a computed set could never
# catch anything) and deliberately not silently updated by test code —
# adding a cascade relation is a real, deliberate design decision and
# should require touching this test.
#
# #2252 Stage 1.0a corrected the declaration: `retracts` and `supersedes`
# were found to be mis-declared cascade:True (they mark revision history —
# correction of / supersession by — not evidential dependency; walking them
# is actively wrong, see engram_core.py's EDGE_CLASSIFICATIONS comments on
# those two relations). This is the authorising decision for the set
# narrowing from four members to two.
_EXPECTED_CASCADE_RELATIONS = frozenset(
    {"derives_from", "supported_by"}
)


def test_cascade_implies_dag_check():
    """Every cascade:True relation must also be dag_check:True.

    Today this is declarative consistency, not a live termination guard.
    The cascade walkers (engram_revision.py and friends) hardcode their
    walked relations as a SQL literal rather than reading `cascade`, and
    each terminates via its own visited-set regardless of acyclicity — a
    cycle would be re-visited once and skipped, not hang or recurse
    forever. `cascade`'s only current runtime consumer is viz_server.py's
    edge-rendering choice.

    What this guards is the declared relationship between the two flags,
    and groundwork for engram-alpha#2013's cascade machine, which intends
    to make `cascade` drive the walked-relation set directly. If that
    lands on top of a relation carrying cascade=True/dag_check=False, the
    walk it drives over that relation would have no acyclicity guarantee,
    and the failure mode (hang / RecursionError) would surface far from
    this line, in whichever cascade walk hit the cycle first. Catching the
    mismatch here, before that day, is the point.

    This does NOT require the converse. A relation may be dag_check=True
    without being cascade=True (e.g. 'cites', 'subtask_of') — those are
    acyclic for other reasons and never propagate anything. Only the
    cascade ⇒ dag_check direction is a correctness requirement.
    """
    violations = sorted(
        relation
        for relation, flags in EDGE_CLASSIFICATIONS.items()
        if flags.get("cascade") and not flags.get("dag_check")
    )
    assert not violations, (
        f"Relation(s) with cascade=True but dag_check=False: {violations}. "
        f"cascade and dag_check are declared to co-vary; nothing enforces "
        f"that today, but engram-alpha#2013's cascade machine will make "
        f"cascade drive the walked-relation set, turning this into a live "
        f"acyclicity gap. Fix by setting dag_check=True for each relation "
        f"listed in EDGE_CLASSIFICATIONS (src/engram/engram_core.py) — this "
        f"has no legitimate use and is not an exception to justify, it is a "
        f"bug to fix."
    )


def test_cascade_relation_set_is_known():
    """Change-detector: the current cascade=True relation set is exactly
    {derives_from, supported_by}.

    #2252 Stage 1.0a narrowed this from the prior four-member set
    {derives_from, retracts, supersedes, supported_by} — retracts and
    supersedes were mis-declared cascade:True; see the EDGE_CLASSIFICATIONS
    comments on those two relations in engram_core.py.

    THIS TEST IS A CHANGE-DETECTOR, NOT A DESIGN CONSTRAINT. If it fails
    because a relation's cascade flag was deliberately added or removed,
    the correct response is to UPDATE _EXPECTED_CASCADE_RELATIONS in this
    file to match the new intended set — do not delete this test out of
    frustration. Its job is to make sure that change is a deliberate,
    reviewed edit rather than something that slips in unnoticed. (The
    consistency half of the invariant — that every cascade relation is
    also dag_check'd — is enforced separately, by
    test_cascade_implies_dag_check above, and stays enforced regardless of
    how this set changes.)
    """
    actual_cascade_relations = frozenset(
        relation
        for relation, flags in EDGE_CLASSIFICATIONS.items()
        if flags.get("cascade")
    )
    assert actual_cascade_relations == _EXPECTED_CASCADE_RELATIONS, (
        f"The set of cascade=True relations in EDGE_CLASSIFICATIONS changed: "
        f"expected {sorted(_EXPECTED_CASCADE_RELATIONS)}, got "
        f"{sorted(actual_cascade_relations)}. This test is a change-detector, "
        f"not a hard design constraint — if this change is intentional, "
        f"update _EXPECTED_CASCADE_RELATIONS in this file (and confirm the "
        f"cascade⇒dag_check invariant still holds via "
        f"test_cascade_implies_dag_check) rather than removing this test."
    )
