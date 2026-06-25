"""Edge tier audit: completeness gate + soft dag_check/tier drift warning.

Design (issue #1080, Lei 2026-06-10 decision):
  - The per-edge dag_check flags in EDGE_CLASSIFICATIONS are INDEPENDENT design
    dimensions. This test does NOT auto-couple dag_check to tier.
  - HARD assert (completeness gate): every relation in EDGE_CLASSIFICATIONS
    has an entry in EDGE_TIERS and vice-versa. Also asserts every tier value
    is one of the three valid strings. A relation added without a deliberate
    tier decision fails the build immediately — no silent drift.
  - SOFT warn (NON-blocking): for each relation, compares its dag_check flag
    against the tier's expectation (dependency→True; relational/classification→
    False). If they diverge AND the relation is NOT in EDGE_TIER_DIVERGENCE_ACK,
    emits warnings.warn for human review. The test still PASSES — divergence is
    a signal for human attention, not a failure. An intentional divergence is
    acknowledged in EDGE_TIER_DIVERGENCE_ACK to suppress the warning.

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

# Tier → expected dag_check value.
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


# ── SOFT dag_check / tier drift warning (NON-blocking) ───────────────────────

def test_dag_check_tier_drift_warn():
    """Emit warnings.warn for any unacknowledged dag_check/tier divergence.

    For each relation: compares its dag_check flag in EDGE_CLASSIFICATIONS
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
            actual_dag = EDGE_CLASSIFICATIONS[relation]["dag_check"]

            if actual_dag != expected_dag and relation not in EDGE_TIER_DIVERGENCE_ACK:
                warnings.warn(
                    f"Edge tier/dag_check drift for '{relation}': "
                    f"tier='{tier}' expects dag_check={expected_dag}, "
                    f"but EDGE_CLASSIFICATIONS has dag_check={actual_dag}. "
                    f"If intentional, add '{relation}' to EDGE_TIER_DIVERGENCE_ACK "
                    f"in engram_core.py with a reason. Otherwise, align dag_check "
                    f"or reconsider the tier assignment.",
                    stacklevel=2,
                )
    # Always passes — divergence is a warning for human review, not a failure.
