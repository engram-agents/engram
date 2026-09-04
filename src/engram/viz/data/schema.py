"""viz.data.schema — the schema-SSoT data assembly for /api/schema
(engram-alpha#2555 PR A, pure relocation of viz_server.py's
``get_schema_data``, byte-identical function body).

No HTTP, no HTML — unit-testable in isolation, per the design's own
constraint on this package.
"""

from __future__ import annotations

try:
    from engram_stats import _CONFIDENCE_BEARING_TYPES  # noqa: E402
except ImportError:
    _CONFIDENCE_BEARING_TYPES = ()  # type: ignore[assignment]

try:
    from engram_core import (  # noqa: E402
        VALID_NODE_TYPES,
        CLAIM_BEARING_TYPES,
        VALID_RELATIONS,
        EDGE_CLASSIFICATIONS,
    )
    _SCHEMA_CORE_AVAILABLE = True
except ImportError:
    _SCHEMA_CORE_AVAILABLE = False
    VALID_NODE_TYPES = set()  # type: ignore[assignment]
    CLAIM_BEARING_TYPES = set()  # type: ignore[assignment]
    VALID_RELATIONS = set()  # type: ignore[assignment]
    EDGE_CLASSIFICATIONS = {}  # type: ignore[assignment]

try:
    from engram_query import (  # noqa: E402
        _CONTEXTUAL_RELATIONS,
    )
    _SCHEMA_QUERY_AVAILABLE = True
except ImportError:
    _SCHEMA_QUERY_AVAILABLE = False
    _CONTEXTUAL_RELATIONS = frozenset()  # type: ignore[assignment]


def get_schema_data() -> dict:
    """Return the schema SSoT as a JSON-serialisable dict.

    Builds from engram_core / engram_query / engram_stats constants.
    No DB access required — the schema is static.  Called by the
    /api/schema endpoint and directly by the CI drift gate.
    """
    # Logical-substrate set: DERIVED as the residual of VALID_RELATIONS after
    # the contextual set, so the two groups partition VALID_RELATIONS BY
    # CONSTRUCTION and a new relation lands in one of them automatically.
    #
    # This replaces a hand-union of {"instantiates"} — a relation that sits in
    # VALID_RELATIONS but in neither underlying constant, and was patched in by
    # name. That patch was correct and unmaintainable in the same breath: the
    # next such relation needed a second hand-edit, and #2013 §12.5's
    # `acknowledged_derives_from` is exactly that next one. Deriving the
    # residual makes the union-equals-VALID_RELATIONS property true by
    # construction rather than by remembering (§12.6 — the edge declaration is
    # the SSoT at every call site).
    #
    # Equivalent to the old expression today: the test this replaces asserted
    # that the hand-union plus the contextual set already covered
    # VALID_RELATIONS exactly, so the residual is the same set — it just stops
    # needing an editor.
    contextual = sorted(_CONTEXTUAL_RELATIONS)
    logical_substrate = sorted(set(VALID_RELATIONS) - set(_CONTEXTUAL_RELATIONS))

    relations_detail = {
        rel: {
            "cascade": bool(EDGE_CLASSIFICATIONS.get(rel, {}).get("cascade", False)),
            "provenance": bool(EDGE_CLASSIFICATIONS.get(rel, {}).get("provenance", False)),
        }
        for rel in VALID_RELATIONS
    }

    return {
        "node_types": sorted(VALID_NODE_TYPES),
        "claim_bearing_types": sorted(CLAIM_BEARING_TYPES),
        "confidence_bearing_types": sorted(_CONFIDENCE_BEARING_TYPES),
        "relations": relations_detail,
        "logical_substrate_relations": logical_substrate,
        "contextual_relations": contextual,
    }
