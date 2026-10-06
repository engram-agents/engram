"""cascade ⇒ dag consistency invariant over EDGE_CLASSIFICATIONS.

#2357 renamed the old `dag_check` flag to `temporal_causal` (pure rename,
same creation-order semantics) and added a new, independent flag (real
edge-walk acyclicity; since #3210 it is the `dag_family` field, and "has a
dag_family" is the old `dag` truth). This file's invariant follows
acyclicity-family membership, not
`temporal_causal` — see EDGE_CLASSIFICATIONS's header comment in
engram_core.py for why: `acknowledged_derives_from` is a cascade relation
that is deliberately `temporal_causal: False` (it points backwards in
creation time by construction) while still needing an acyclicity family (a real
cycle through it would be genuinely bad). Coupling this invariant to
`temporal_causal` instead would make that relation's very existence a
declared violation of a design goal it's actually satisfying.

This is a LIVE correctness guard today, not forward-looking groundwork —
correction, PR #2360 reviewer-fairy round 2, after round 1's own first
correction attempt was itself incomplete. Verified directly against
source (grep + read, not inferred): every cascade-taint walker already
derives its walked-relation set from `CASCADE_RELATIONS` at call time,
none hardcodes a SQL literal —
  - `engram_core.py`'s `_walk_cascade_upstream` (used by
    `_trace_observation_leaves` and #2013's full-ancestor-set primitive)
    reads its own `_CASCADE_RELATIONS_SORTED`, derived from
    `core.CASCADE_RELATIONS`.
  - All three cascade-relevant queries in `engram_revision.py`
    (`_walk_cascade_downstream` itself, plus the "direct dependents"
    queries in `_supersede_impl` and `_retract_impl` that seed it) share
    one module-level `_CASCADE_RELATIONS_SORTED = sorted(core.CASCADE_RELATIONS)`
    (that file's line 51), added by commit `4431e89` (2026-08-26) —
    predating even this branch's base commit, so this was already true
    before `#2357` touched anything here.
So the moment `acknowledged_derives_from` edges exist (once #2013 slice
3/4 lands), all four of these walkers start traversing them automatically,
with zero code changes needed at any of the four call sites — `cascade`
already IS the load-bearing flag driving cascade-taint propagation today,
not a future intention.
`engram_lifecycle.py` and `engram_stats.py`'s citation-coverage queries
are NOT part of this family at all, and grouping them with the cascade
walkers (as an earlier draft of this docstring did) was a category error,
not merely stale text: they derive live too, but from a DIFFERENT,
deliberately-separate constant, `CITATION_SUPPORT_RELATIONS` (see that
constant's own extensive comment in `engram_core.py` for why it is
hand-enumerated rather than derived FROM `CASCADE_RELATIONS` — the two
sets answer different questions and have differed in practice since
#2252 Stage 1.3). They check citation coverage ("is this observation
cited by anything"), not cascade-taint propagation, and have no bearing
on this file's `cascade ⇒ dag` invariant either way.
Today `cascade` also has one purely cosmetic runtime consumer —
viz_server.py, which reads it to choose solid-vs-dashed edge rendering in
the graph visualiser.

Nor does a cascade walk's termination depend on acyclicity. Each walker
(e.g. `_walk_cascade_downstream` in engram_revision.py) carries its own
`visited` set, checked BEFORE recursing, so a cycle in the underlying
graph is visited once and then skipped — no hang, no RecursionError,
regardless of acyclicity. And `temporal_causal`'s own init-time check
(engram_core.py) is warn-only: it runs once at startup, compares
`created_at` ordering (not actual cycle detection — a genuine cycle among
equal-timestamp nodes evades it), `LIMIT 5`s the result, calls
`logging.warning`, and the whole block is wrapped in
``try: ... except Exception: pass  # Don't block startup on check
failures``. It rejects nothing, at startup or at edge-creation time. The
REAL cycle-detection walk lives in engram_dag_conformance.py (#2357),
walking the relations that carry a `dag_family` specifically.

So why assert this hard? Because it is a live correctness dependency
today, not a someday one: `cascade` and acyclicity are meant to co-vary — a
relation that propagates taint/stale state should also be guaranteed
acyclic. All four cascade-taint walkers listed above already depend on
`CASCADE_RELATIONS` being right; cascade:True with dag_family None is a flag
combination that would hand every one of them an unprotected relation to
traverse, with no acyclicity guarantee behind it. There is no "the rest
still hardcodes it, so this is only groundwork" hedge left to make — that
was true once, is not true of any cascade-taint walker as of commit
`4431e89`, and restating it as still-pending groundwork (as two earlier
drafts of this docstring did) is exactly the kind of claim that outlives
its own truth silently if nobody re-checks it against source.

Contrast with test_edge_tier_audit.py, but precisely: that file is not
purely a non-blocking audit — its own docstring calls itself a
"completeness gate + soft warning", and it ships hard-failing
completeness tests (every relation has a tier and vice versa, every tier
value is valid) alongside one soft-warning test,
`test_dag_check_tier_drift_warn` (`warnings.warn` on divergence, always
passes, and checks `temporal_causal` against tier expectation — a
DIFFERENT invariant from this file's acyclicity-family check). It's specifically that
soft-warning test this file's check is unlike: a tier's temporal_causal
*expectation* is a design convention with a documented escape hatch
(EDGE_TIER_DIVERGENCE_ACK) for relations that intentionally diverge from
it. cascade⇒dag has no such escape hatch — there is no legitimate use for
cascade:True with dag_family None, so this is asserted HARD rather than warned.

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
#
# #2252 Stage 1.3 narrowed it again, from two members to one: `supported_by`
# no longer carries cascade — post-1.3 it is NCB accumulation (the W0 doc:
# 99.2% of live instances are cornerstone-sourced vote edges), and an NCB
# target has no claim to re-adjudicate. See the `supported_by` entry's
# comment in EDGE_CLASSIFICATIONS.
#
# #2357 (2026-08-29) widened it again, from one member to two:
# `acknowledged_derives_from` is a new relation that must cascade (that's
# the entire reason it exists — see its EDGE_CLASSIFICATIONS entry).
_EXPECTED_CASCADE_RELATIONS = frozenset(
    {"derives_from", "acknowledged_derives_from"}
)


def test_cascade_implies_dag_check():
    """Every cascade:True relation must also be in an acyclicity family.

    This IS a live correctness dependency, not merely declarative
    consistency (module docstring above has the full, source-verified
    account — corrected in PR #2360 round 2 after an earlier draft here
    overstated it as still-pending groundwork). Every cascade-taint walker
    (engram_core.py's `_walk_cascade_upstream`; engram_revision.py's
    `_walk_cascade_downstream` and its two callers) already derives its
    walked relations from `CASCADE_RELATIONS` at call time, not a
    hardcoded SQL literal, and each terminates via its own visited-set
    regardless of acyclicity — a cycle would be re-visited once and
    skipped, not hang or recurse forever. `cascade` ALSO has one purely
    cosmetic runtime consumer, viz_server.py's edge-rendering choice.

    What this guards: a relation carrying cascade=True and no dag_family would
    hand every one of those walkers an unprotected relation to traverse
    RIGHT NOW, not on some future day — the failure mode (hang /
    RecursionError, or in the new engram_dag_conformance.py checker's own
    case, a wrong "clean" reading) would surface far from this line, in
    whichever walk or check hit the cycle first. Catching the mismatch
    here is the point.

    This does NOT require the converse. A relation may be in an acyclicity family
    without being cascade=True (e.g. 'cites', 'subtask_of') — those are
    acyclic for other reasons and never propagate anything. Only the
    cascade ⇒ dag direction is a correctness requirement.

    #2357: this checks acyclicity-family membership, not `temporal_causal`
    (the renamed old `dag_check`) — a cascade relation MAY legitimately be
    temporal_causal=False (acknowledged_derives_from is), as long as it's
    still in a family. Coupling this test to temporal_causal instead would make
    that relation's existence a declared bug.
    """
    violations = sorted(
        relation
        for relation, flags in EDGE_CLASSIFICATIONS.items()
        if flags.get("cascade") and flags.get("dag_family") is None
    )
    assert not violations, (
        f"Relation(s) with cascade=True but no dag_family: {violations}. "
        f"cascade and acyclicity are declared to co-vary; nothing enforces "
        f"that today, but engram-alpha#2013's cascade machine will make "
        f"cascade drive the walked-relation set, turning this into a live "
        f"acyclicity gap. Fix by giving each relation a dag_family "
        f"listed in EDGE_CLASSIFICATIONS (src/engram/engram_core.py) — this "
        f"has no legitimate use and is not an exception to justify, it is a "
        f"bug to fix."
    )


def test_cascade_relation_set_is_known():
    """Change-detector: the current cascade=True relation set is exactly
    {derives_from, acknowledged_derives_from}.

    #2252 Stage 1.0a narrowed this from the prior four-member set
    {derives_from, retracts, supersedes, supported_by} — retracts and
    supersedes were mis-declared cascade:True; see the EDGE_CLASSIFICATIONS
    comments on those two relations in engram_core.py. #2357 (2026-08-29)
    widened it back to two, adding `acknowledged_derives_from`.

    THIS TEST IS A CHANGE-DETECTOR, NOT A DESIGN CONSTRAINT. If it fails
    because a relation's cascade flag was deliberately added or removed,
    the correct response is to UPDATE _EXPECTED_CASCADE_RELATIONS in this
    file to match the new intended set — do not delete this test out of
    frustration. Its job is to make sure that change is a deliberate,
    reviewed edit rather than something that slips in unnoticed. (The
    consistency half of the invariant — that every cascade relation is
    also dag-safe — is enforced separately, by
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
        f"cascade⇒dag invariant still holds via "
        f"test_cascade_implies_dag_check) rather than removing this test."
    )
