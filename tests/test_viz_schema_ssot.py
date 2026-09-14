"""CI drift gate: viz.data.schema.get_schema_data() must stay in sync with
the schema SSoT (engram-alpha#2555 PR A retarget -- was viz_server).

Tests import get_schema_data() directly (no HTTP) so CI catches any divergence
between the /api/schema endpoint and the engram_core / engram_query / engram_stats
constants immediately — before it can silently drift again.

Import pattern mirrors test_viz_server_stats.py: conftest.py already inserts
src/engram into sys.path, so both viz.data.schema and the engram_* modules
resolve.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Belt-and-suspenders path insertion (mirrors test_viz_server_stats.py pattern).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz.data import schema as viz_schema_data
import engram_core


class TestGetSchemaDataNodeTypes(unittest.TestCase):
    """Every node type in the schema SSoT must appear in get_schema_data()."""

    def test_node_types_match_ssot(self):
        data = viz_schema_data.get_schema_data()
        self.assertEqual(
            data["node_types"],
            sorted(engram_core.VALID_NODE_TYPES),
            "get_schema_data()['node_types'] must equal sorted(VALID_NODE_TYPES); "
            "a schema addition that is not exposed here fails this gate.",
        )

    def test_node_types_count(self):
        data = viz_schema_data.get_schema_data()
        # Concrete count check: catches accidental deletion from VALID_NODE_TYPES.
        self.assertEqual(
            len(data["node_types"]),
            len(engram_core.VALID_NODE_TYPES),
        )

    def test_cornerstone_is_present(self):
        """Regression: cornerstone was missing from the old hardcoded JS map."""
        data = viz_schema_data.get_schema_data()
        self.assertIn(
            "cornerstone",
            data["node_types"],
            "cornerstone must appear in node_types — it was missing from the "
            "original hardcoded JS NODE_TYPES const (the bug this PR addresses).",
        )

    def test_claim_bearing_types_match_ssot(self):
        data = viz_schema_data.get_schema_data()
        self.assertEqual(
            data["claim_bearing_types"],
            sorted(engram_core.CLAIM_BEARING_TYPES),
        )


class TestGetSchemaDataRelations(unittest.TestCase):
    """Every relation in the schema SSoT must appear in get_schema_data()."""

    def test_all_valid_relations_present(self):
        data = viz_schema_data.get_schema_data()
        for rel in engram_core.VALID_RELATIONS:
            self.assertIn(
                rel,
                data["relations"],
                f"Relation {rel!r} from VALID_RELATIONS is missing from "
                f"get_schema_data()['relations'] — the drift gate would not have "
                f"caught a future schema addition without this check.",
            )

    def test_no_extra_relations(self):
        data = viz_schema_data.get_schema_data()
        for rel in data["relations"]:
            self.assertIn(
                rel,
                engram_core.VALID_RELATIONS,
                f"Relation {rel!r} in get_schema_data()['relations'] is not in "
                f"VALID_RELATIONS — stale entry in the schema response.",
            )

    def test_cascade_provenance_bools_match_edge_classifications(self):
        data = viz_schema_data.get_schema_data()
        for rel, info in data["relations"].items():
            expected_cascade = bool(
                engram_core.EDGE_CLASSIFICATIONS.get(rel, {}).get("cascade", False)
            )
            expected_provenance = bool(
                engram_core.EDGE_CLASSIFICATIONS.get(rel, {}).get("provenance", False)
            )
            self.assertEqual(
                info["cascade"],
                expected_cascade,
                f"relations[{rel!r}]['cascade'] mismatch: "
                f"got {info['cascade']!r}, expected {expected_cascade!r}",
            )
            self.assertEqual(
                info["provenance"],
                expected_provenance,
                f"relations[{rel!r}]['provenance'] mismatch: "
                f"got {info['provenance']!r}, expected {expected_provenance!r}",
            )

    def test_instantiates_present(self):
        """Regression: instantiates was in VALID_RELATIONS but unclassified."""
        data = viz_schema_data.get_schema_data()
        self.assertIn(
            "instantiates",
            data["relations"],
            "instantiates must be present in relations",
        )
        self.assertIn(
            "instantiates",
            data["logical_substrate_relations"],
            "instantiates must be classified as logical_substrate (it is in neither "
            "_LOGICAL_SUBSTRATE_RELATIONS nor _CONTEXTUAL_RELATIONS in engram_query; "
            "the spec places it in logical_substrate for the graph spine).",
        )


class TestGetSchemaDataConfidenceBearingTypes(unittest.TestCase):
    """confidence_bearing_types must match the engram_stats SSoT.

    engram_stats._CONFIDENCE_BEARING_TYPES is now a module-level constant
    (promoted in #1225); viz_server imports it. This test compares the endpoint
    output against the CANONICAL engram_stats set — so a future drift in either
    place (or a broken import that empties viz's copy) fails the gate loudly.
    """

    def test_confidence_bearing_types_match_ssot(self):
        import engram_stats
        data = viz_schema_data.get_schema_data()
        self.assertEqual(
            data["confidence_bearing_types"],
            sorted(engram_stats._CONFIDENCE_BEARING_TYPES),
        )

    def test_confidence_bearing_types_nonempty(self):
        data = viz_schema_data.get_schema_data()
        self.assertGreater(
            len(data["confidence_bearing_types"]),
            0,
            "confidence_bearing_types must not be empty",
        )

    def test_confidence_bearing_types_are_valid_node_types(self):
        """Every confidence-bearing type must be a valid node type."""
        data = viz_schema_data.get_schema_data()
        for t in data["confidence_bearing_types"]:
            self.assertIn(
                t,
                engram_core.VALID_NODE_TYPES,
                f"confidence_bearing_type {t!r} is not in VALID_NODE_TYPES",
            )


class TestGetSchemaDataRelationClassification(unittest.TestCase):
    """Every VALID_RELATIONS member must be in exactly one of the two spine sets.

    The union of logical_substrate_relations + contextual_relations must equal
    VALID_RELATIONS.  Any relation that is in neither (like instantiates before
    this PR) or in both would fail this gate.

    This is the mechanical constraint: a future schema addition that is not
    classified into either set fails CI immediately.
    """

    def test_union_equals_valid_relations(self):
        data = viz_schema_data.get_schema_data()
        logical = set(data["logical_substrate_relations"])
        contextual = set(data["contextual_relations"])
        combined = logical | contextual
        expected = set(engram_core.VALID_RELATIONS)
        self.assertEqual(
            combined,
            expected,
            f"logical_substrate_relations ∪ contextual_relations must equal "
            f"VALID_RELATIONS.\n"
            f"  In combined but not in VALID_RELATIONS: {combined - expected!r}\n"
            f"  In VALID_RELATIONS but not classified:  {expected - combined!r}",
        )

    def test_no_overlap_between_sets(self):
        data = viz_schema_data.get_schema_data()
        logical = set(data["logical_substrate_relations"])
        contextual = set(data["contextual_relations"])
        overlap = logical & contextual
        self.assertEqual(
            overlap,
            set(),
            f"Relations must be classified into exactly one set; "
            f"overlap found: {overlap!r}",
        )


if __name__ == "__main__":
    unittest.main()
