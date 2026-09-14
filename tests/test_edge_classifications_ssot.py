"""SSoT equivalence: derived constants match the historical literal sets.

Closes the regression channel where edits to EDGE_CLASSIFICATIONS could
silently drift DAG_EXEMPT_RELATIONS or _REMOVABLE_EDGE_RELATIONS away
from their pre-refactor values.

Import-pattern contract (#948 wave C — R1):
    This file imports engram_core directly and contains no ``import server``.
    EDGE_CLASSIFICATIONS, DAG_EXEMPT_RELATIONS, and _REMOVABLE_EDGE_RELATIONS
    are defined in engram_core (server.py re-exports them via the core alias).
"""
import engram_core
from engram_core import (
    EDGE_CLASSIFICATIONS,
    DAG_EXEMPT_RELATIONS,
    _REMOVABLE_EDGE_RELATIONS,
)

def test_dag_exempt_matches_pre_refactor_set():
    # Frozen value matches the literal at server.py:505 pre-refactor.
    # If this test fails, EDGE_CLASSIFICATIONS has drifted; either
    # update this expected set (with a justifying observation) or
    # fix the dict.
    # `instantiates` added 2026-06-07 (#530 c1, forum #44 three-graph
    # convergence; the cite is the design-record observation in the
    # implementing agent's graph + issue #530's consolidated spec):
    # realization is not a temporal dependency — a goal may be articulated
    # after the work that realizes it (the gl-0008-shaped first customer).
    # `resolves` added 2026-06-10 (issue #1076): back-reference edge where
    # the resolver legitimately pre-dates the question — a question asked
    # retroactively about a pre-existing answer must not fire a DAG warning.
    # `tensions` added 2026-06-10 (issue #1076, three-tier edge taxonomy):
    # relational tier, symmetric/cross-temporal — same class as contradicts.
    # `serves` added 2026-06-10 (issue #1076, three-tier edge taxonomy):
    # classification tier — a goal can be articulated after the node that serves
    # it, same shape as instantiates (already exempt).
    # `acknowledged_derives_from` added 2026-08-29 (#2357): the dag_check flag
    # was renamed temporal_causal (this constant's semantics are unchanged —
    # it names the CREATION-ORDER exemption set) and this new relation is
    # deliberately temporal_causal=False despite carrying dag=True (real
    # acyclicity, checked separately — see EDGE_TIER_DIVERGENCE_ACK).
    # `generalizes` added 2026-09-10 (#2753 epic WS1, D1): temporal_causal=
    # False by design (a later concrete cornerstone can still generalize to
    # an older principle), same shape as instantiates/serves — but ALSO
    # carries dag=True (real acyclicity matters here, unlike
    # instantiates/serves/exemplifies), the intentional divergence
    # acknowledged in EDGE_TIER_DIVERGENCE_ACK.
    expected = frozenset({"contradicts", "about", "exemplifies", "instantiates",
                          "resolves", "tensions", "serves",
                          "acknowledged_derives_from", "generalizes"})
    # Allow the new constant to add `cites` ONLY if a separate ob/dv
    # cites the design call that allowed it.
    assert DAG_EXEMPT_RELATIONS == expected, (
        f"DAG_EXEMPT_RELATIONS drift: got {sorted(DAG_EXEMPT_RELATIONS)}, "
        f"expected {sorted(expected)}. If intentional, update test + cite ob."
    )

def test_removable_matches_pre_refactor_set():
    # `instantiates` removable per #530 spec (pure relevance marker —
    # over-applied wiring must be correctable without surgical tools).
    # `generalizes` removable per #2753 epic WS1, D1 — same shape (a pure
    # routing/classification edge, over-applied wiring must be correctable).
    expected = frozenset({"about", "tensions", "subtask_of", "serves", "exemplifies",
                          "instantiates", "generalizes"})
    assert _REMOVABLE_EDGE_RELATIONS == expected, (
        f"_REMOVABLE_EDGE_RELATIONS drift: got {sorted(_REMOVABLE_EDGE_RELATIONS)}, "
        f"expected {sorted(expected)}."
    )

def test_all_relations_have_full_field_set():
    """Every entry has all 6 fields set (catches typos / partial edits)."""
    expected_fields = {"cascade", "temporal_causal", "dag", "removable",
                       "addable_after_creation", "provenance"}
    for relation, classification in EDGE_CLASSIFICATIONS.items():
        missing = expected_fields - set(classification.keys())
        extra = set(classification.keys()) - expected_fields
        assert not missing, f"{relation}: missing fields {missing}"
        assert not extra, f"{relation}: extra fields {extra}"

def test_no_unclassified_relations_in_schema():
    """Every edge relation used in the DB schema appears in EDGE_CLASSIFICATIONS.

    Guards against the next PR adding a new relation to a new tool but
    forgetting to add the classification — would mean its add-edge /
    remove-edge / DAG behavior is undefined.
    """
    # Hardcoded — extend when adding a relation.
    # `acknowledged_derives_from` added 2026-08-29 (#2013 §12.5) — a premise
    # edge established by adjudication rather than at authorship.
    known_relations = {
        "acknowledged_derives_from",
        "about", "cites", "contradicts", "derives_from", "exemplifies",
        "instantiates",  # #530 c1 (2026-06-07)
        "predicted_by",  # #2252 W6 (2026-08-27) — prediction pairing edge,
                         # split out of supported_by.
        "acknowledged_derives_from",  # #2357 (2026-08-29) — cascade-frontier
                         # acknowledgement edge, part of #2013.
        "resolves", "retracts", "serves", "subtask_of", "supersedes",
        "supported_by", "tensions",
        "generalizes",  # #2753 (2026-09-10) — epic WS1, D1: general ->
                         # specific abstraction/classification edge.
    }
    assert set(EDGE_CLASSIFICATIONS.keys()) == known_relations, (
        f"Drift between EDGE_CLASSIFICATIONS and known relations: "
        f"missing={known_relations - set(EDGE_CLASSIFICATIONS.keys())}, "
        f"extra={set(EDGE_CLASSIFICATIONS.keys()) - known_relations}"
    )
