"""Edge tier audit: completeness gate + soft temporal_causal/tier drift warning
+ hard dag/tier divergence-acknowledgment gate.

Design (issue #1080, Lei 2026-06-10 decision):
  - The per-edge temporal_causal flags in EDGE_CLASSIFICATIONS are INDEPENDENT design
    dimensions. This test does NOT auto-couple temporal_causal to tier.
  - HARD assert (completeness gate): every relation in EDGE_CLASSIFICATIONS
    has an entry in EDGE_TIERS and vice-versa. Also asserts every tier value
    is one of the three valid strings. A relation added without a deliberate
    tier decision fails the build immediately — no silent drift.
  - SOFT warn (NON-blocking): for each relation, compares its temporal_causal flag
    against the tier's expectation (dependency→True; relational/classification→
    False). If they diverge AND the relation is NOT in EDGE_TIER_DIVERGENCE_ACK,
    emits warnings.warn for human review. The test still PASSES — divergence is
    a signal for human attention, not a failure. An intentional divergence is
    acknowledged in EDGE_TIER_DIVERGENCE_ACK to suppress the warning.
  - HARD assert (#2753 epic WS1, R2): a SEPARATE divergence axis — the `dag`
    flag (real structural acyclicity, #2357) against the SAME tier
    expectation table — is NOT covered by the soft warn above (which checks
    temporal_causal only). `generalizes` is classification-tier in the
    abstraction acyclicity family, diverging from every other
    classification-tier relation (dag_family None) — and the soft warn above would NEVER catch its ack entry
    going missing, because it never looks at `dag` at all. This is exactly
    the gap R2 names: "the existing guard will NOT catch its absence."
    test_generalizes_dag_tier_divergence_must_be_acknowledged below is a
    HARD FAILURE (not a warning) if any relation's `dag` flag diverges from
    its tier's expectation while absent from EDGE_TIER_DIVERGENCE_ACK.

Import-pattern contract (#948 wave C):
    Imports engram_core directly; contains no ``import server``.
"""

import warnings

from engram_core import (
    EDGE_CLASSIFICATIONS,
    EDGE_TIERS,
    EDGE_TIER_DIVERGENCE_ACK,
)

# The three valid tier strings (the three-tier edge taxonomy).
_VALID_TIERS = {"dependency", "relational", "classification"}

# Tier → expected temporal_causal value.
_TIER_DAG_EXPECTATION: dict[str, bool] = {
    "dependency":     True,
    "relational":     False,
    "classification": False,
}


# ── HARD completeness gate ────────────────────────────────────────────────────

def test_every_classification_has_a_tier():
    """Every relation in EDGE_CLASSIFICATIONS must have an entry in EDGE_TIERS.

    Guards against adding a new relation to EDGE_CLASSIFICATIONS without making
    a deliberate tier decision. Failing this test means the new relation is
    untiered — fix by adding it to EDGE_TIERS in engram_core.py.
    """
    missing = set(EDGE_CLASSIFICATIONS.keys()) - set(EDGE_TIERS.keys())
    assert not missing, (
        f"Relations in EDGE_CLASSIFICATIONS with no tier assignment in EDGE_TIERS: "
        f"{sorted(missing)}. Add each to EDGE_TIERS with a deliberate tier choice."
    )


def test_every_tier_entry_has_a_classification():
    """Every relation in EDGE_TIERS must appear in EDGE_CLASSIFICATIONS.

    Guards against EDGE_TIERS accumulating stale entries for relations that
    have been removed from EDGE_CLASSIFICATIONS.
    """
    extra = set(EDGE_TIERS.keys()) - set(EDGE_CLASSIFICATIONS.keys())
    assert not extra, (
        f"Relations in EDGE_TIERS with no matching EDGE_CLASSIFICATIONS entry: "
        f"{sorted(extra)}. Remove stale entries from EDGE_TIERS."
    )


def test_all_tier_values_are_valid():
    """Every tier value must be one of the three valid tier strings.

    Valid values: 'dependency', 'relational', 'classification'.
    """
    invalid = {
        relation: tier
        for relation, tier in EDGE_TIERS.items()
        if tier not in _VALID_TIERS
    }
    assert not invalid, (
        f"Invalid tier values in EDGE_TIERS (must be one of {sorted(_VALID_TIERS)}): "
        f"{invalid}"
    )


# ── SOFT temporal_causal / tier drift warning (NON-blocking) ───────────────────────

def test_temporal_causal_tier_drift_warn():
    """Emit warnings.warn for any unacknowledged temporal_causal/tier divergence.

    For each relation: compares its temporal_causal flag in EDGE_CLASSIFICATIONS
    against the expected value for its tier (dependency→True; relational/
    classification→False). If they diverge AND the relation is not in
    EDGE_TIER_DIVERGENCE_ACK, emits a warnings.warn describing the drift.

    This test ALWAYS PASSES — divergence is a human-review signal, not a
    failure. With the current mapping there are zero divergences, so no
    warnings fire. To acknowledge an intentional divergence, add the relation
    to EDGE_TIER_DIVERGENCE_ACK in engram_core.py with a reason string.
    """
    # Force warnings to always emit (never elevated to errors by a global
    # filter), so this test stays non-failing regardless of any future
    # warnings-as-errors CI config — the airtight form of "audit, not enforce".
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        for relation, tier in EDGE_TIERS.items():
            if tier not in _TIER_DAG_EXPECTATION:
                # Unknown tier — the completeness test above catches this; skip here.
                continue
            if relation not in EDGE_CLASSIFICATIONS:
                # Stale tier entry — the completeness test above catches this; skip here.
                continue

            expected_dag = _TIER_DAG_EXPECTATION[tier]
            actual_dag = EDGE_CLASSIFICATIONS[relation]["temporal_causal"]

            if actual_dag != expected_dag and relation not in EDGE_TIER_DIVERGENCE_ACK:
                warnings.warn(
                    f"Edge tier/temporal_causal drift for '{relation}': "
                    f"tier='{tier}' expects temporal_causal={expected_dag}, "
                    f"but EDGE_CLASSIFICATIONS has temporal_causal={actual_dag}. "
                    f"If intentional, add '{relation}' to EDGE_TIER_DIVERGENCE_ACK "
                    f"in engram_core.py with a reason. Otherwise, align temporal_causal "
                    f"or reconsider the tier assignment.",
                    stacklevel=2,
                )
    # Always passes — divergence is a warning for human review, not a failure.


# ── HARD dag/tier divergence-acknowledgment gate (#2753 epic WS1, R2) ──────
#
# A SEPARATE axis from the soft warn above: this checks the `dag` flag
# (real structural acyclicity, #2357), not `temporal_causal`. The soft warn
# above NEVER inspects `dag` at all, so an unacknowledged `dag`/tier
# divergence would ship silently through that check with no signal
# whatsoever — R2's exact concern. `generalizes` (classification tier,
# abstraction family) is the first relation to diverge on this axis; every other
# classification-tier relation today (instantiates/serves/exemplifies) has
# dag_family None, matching the tier's usual (though not name-encoded) pattern.
#
# Reuses `_TIER_DAG_EXPECTATION` for the expected-per-tier value: despite
# its name (predating #2357's dag/temporal_causal split), its values
# (dependency=True, relational=False, classification=False) are the same
# per-tier expectation this dag-axis check needs — verified empirically for
# every relation EXISTING before `generalizes` was added (dag ==
# temporal_causal's tier-expectation for all of them; `generalizes` is the
# first to diverge on `dag` while matching on `temporal_causal`, the
# opposite of `acknowledged_derives_from`'s divergence, which is on
# `temporal_causal` while matching on `dag`).

def _unacknowledged_dag_tier_divergences(
    classifications: dict, tiers: dict, ack: dict
) -> "list[str]":
    """Relations whose `dag` flag diverges from its tier's expectation
    while ABSENT from `ack`. Empty list == fully acknowledged (the healthy
    state). Extracted as a plain function (not inlined in the pytest test
    body) specifically so the mutation-check test below can call it
    directly against a locally modified `ack` dict, rather than needing to
    trigger pytest's own pass/fail machinery to prove the detection logic
    works.
    """
    unacknowledged = []
    for relation, tier in tiers.items():
        if tier not in _TIER_DAG_EXPECTATION:
            continue  # unknown tier — the completeness test above catches this
        if relation not in classifications:
            continue  # stale tier entry — the completeness test above catches this

        expected_dag = _TIER_DAG_EXPECTATION[tier]
        actual_dag = classifications[relation]["dag_family"] is not None
        if actual_dag != expected_dag and relation not in ack:
            unacknowledged.append(relation)
    return unacknowledged


def test_generalizes_dag_tier_divergence_must_be_acknowledged():
    """HARD FAILURE (not a warning) if any relation's `dag` flag diverges
    from its tier's expectation while absent from EDGE_TIER_DIVERGENCE_ACK.

    #2753 epic WS1, R2: "the existing guard will NOT catch its absence" —
    the soft warn above checks temporal_causal only, and would stay silent
    forever on a `dag`-only divergence going unacknowledged. This test is
    the one that actually fails the build. Covers `generalizes` today (and
    any future dag-divergent relation, since the check is general, not
    hand-keyed to 'generalizes').
    """
    unacknowledged = _unacknowledged_dag_tier_divergences(
        EDGE_CLASSIFICATIONS, EDGE_TIERS, EDGE_TIER_DIVERGENCE_ACK
    )
    assert not unacknowledged, (
        f"Relation(s) with an unacknowledged dag/tier divergence: "
        f"{unacknowledged}. Add each to EDGE_TIER_DIVERGENCE_ACK in "
        f"engram_core.py with a reason string (see the 'generalizes' entry "
        f"for the pattern)."
    )


def test_mutation_check_dag_tier_gate_actually_fails_without_ack():
    """Mutation-verify test_generalizes_dag_tier_divergence_must_be_acknowledged:
    call the SAME detection helper with a locally modified ack dict that
    omits 'generalizes' (simulating the omission R2 warns ships silently
    otherwise) and confirm it IS reported — proving the hard-failing test
    above actually depends on the ack entry being present, not merely
    passing by construction. The real EDGE_TIER_DIVERGENCE_ACK imported
    from engram_core is never mutated (a plain local dict copy is used
    instead) — no monkeypatch/restore needed for this one.
    """
    assert "generalizes" in EDGE_TIER_DIVERGENCE_ACK, (
        "precondition: the real table must currently acknowledge "
        "'generalizes' for this mutation check to mean anything"
    )
    ack_without_generalizes = {
        k: v for k, v in EDGE_TIER_DIVERGENCE_ACK.items() if k != "generalizes"
    }
    unacknowledged = _unacknowledged_dag_tier_divergences(
        EDGE_CLASSIFICATIONS, EDGE_TIERS, ack_without_generalizes
    )
    assert "generalizes" in unacknowledged, (
        "mutation check did not reproduce the unacknowledged-divergence "
        f"shape -- the detection helper may not be the actual gate: {unacknowledged}"
    )
