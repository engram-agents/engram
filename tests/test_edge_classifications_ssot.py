"""SSoT equivalence: derived constants match the historical literal sets.

Closes the regression channel where edits to EDGE_CLASSIFICATIONS could
silently drift DAG_EXEMPT_RELATIONS or _REMOVABLE_EDGE_RELATIONS away
from their pre-refactor values.
"""
from server import (
    EDGE_CLASSIFICATIONS,
    DAG_EXEMPT_RELATIONS,
    _REMOVABLE_EDGE_RELATIONS,
)

def test_dag_exempt_matches_pre_refactor_set():
    # Frozen value matches the literal at server.py:505 pre-refactor.
    # If this test fails, EDGE_CLASSIFICATIONS has drifted; either
    # update this expected set (with a justifying observation) or
    # fix the dict.
    expected = frozenset({"contradicts", "about", "exemplifies"})
    # Allow the new constant to add `cites` ONLY if a separate ob/dv
    # cites the design call that allowed it.
    assert DAG_EXEMPT_RELATIONS == expected, (
        f"DAG_EXEMPT_RELATIONS drift: got {sorted(DAG_EXEMPT_RELATIONS)}, "
        f"expected {sorted(expected)}. If intentional, update test + cite ob."
    )

def test_removable_matches_pre_refactor_set():
    expected = frozenset({"about", "tensions", "subtask_of", "serves", "exemplifies"})
    assert _REMOVABLE_EDGE_RELATIONS == expected, (
        f"_REMOVABLE_EDGE_RELATIONS drift: got {sorted(_REMOVABLE_EDGE_RELATIONS)}, "
        f"expected {sorted(expected)}."
    )

def test_all_relations_have_full_field_set():
    """Every entry has all 5 fields set (catches typos / partial edits)."""
    expected_fields = {"cascade", "dag_check", "removable",
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
    known_relations = {
        "about", "cites", "contradicts", "derives_from", "exemplifies",
        "resolves", "retracts", "serves", "subtask_of", "supersedes",
        "supported_by", "tensions",
    }
    assert set(EDGE_CLASSIFICATIONS.keys()) == known_relations, (
        f"Drift between EDGE_CLASSIFICATIONS and known relations: "
        f"missing={known_relations - set(EDGE_CLASSIFICATIONS.keys())}, "
        f"extra={set(EDGE_CLASSIFICATIONS.keys()) - known_relations}"
    )
