"""Unit tests for engram-pkg's pure closure checker (_find_dangling_edges)
and its thin sys.exit wrapper (_assert_closure_completeness).

Import pattern mirrors tests/test_scope_export.py's _import_scope_export_helpers().
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import helpers from the extensionless engram-pkg script via importlib.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_engram_pkg_helpers():
    """Import engram-pkg CLI as a module using the same importlib pattern
    as tests/test_scope_export.py's _import_scope_export_helpers().
    """
    candidates = [
        os.path.join(REPO_ROOT, "tools", "engram-pkg", "engram-pkg"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "engram-pkg", "engram-pkg",
        ),
    ]
    cli_path = None
    for c in candidates:
        if os.path.exists(c):
            cli_path = c
            break

    if cli_path is None:
        raise FileNotFoundError(
            f"engram-pkg CLI not found. Tried: {candidates}"
        )

    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli",
        importlib.machinery.SourceFileLoader("engram_pkg_cli", cli_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CLI = _import_engram_pkg_helpers()


# ---------------------------------------------------------------------------
# Tests for _find_dangling_edges (pure checker)
# ---------------------------------------------------------------------------

class TestFindDanglingEdges:
    """Unit tests for the pure _find_dangling_edges checker."""

    def test_clean_closure_returns_empty_list(self):
        """No dangling edges when all edge endpoints are in nodes."""
        nodes = {"ob_0001": {}, "ev_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert result == []

    def test_empty_nodes_and_edges_returns_empty_list(self):
        """Empty graph is trivially closure-complete."""
        result = _CLI._find_dangling_edges({}, [])
        assert result == []

    def test_empty_edges_returns_empty_list(self):
        """Nodes with no edges: no dangling possible."""
        nodes = {"ob_0001": {}, "ob_0002": {}}
        result = _CLI._find_dangling_edges(nodes, [])
        assert result == []

    def test_dangling_target_listed(self):
        """Edge whose target is missing from nodes is returned."""
        nodes = {"ob_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "dv_9999", "relation": "derives_from"},
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert len(result) == 1
        assert result[0] == ("ob_0001", "dv_9999", "derives_from")

    def test_dangling_source_listed(self):
        """Edge whose source is missing from nodes is returned."""
        nodes = {"ev_0001": {}}
        edges = [
            {"source_id": "ob_9999", "target_id": "ev_0001", "relation": "cites"},
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert len(result) == 1
        assert result[0] == ("ob_9999", "ev_0001", "cites")

    def test_both_endpoints_missing_listed(self):
        """Edge where both endpoints are absent is still listed once."""
        nodes = {}
        edges = [
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert len(result) == 1
        assert result[0] == ("ob_0001", "ev_0001", "cites")

    def test_multiple_dangling_all_listed(self):
        """All dangling edges are returned, not just the first."""
        nodes = {"ob_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "dv_9991", "relation": "derives_from"},
            {"source_id": "ob_0001", "target_id": "dv_9992", "relation": "derives_from"},
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},  # valid
        ]
        # Only "ev_0001" is missing (ob_0001 is in nodes but ev_0001 is not)
        # Wait: ev_0001 is NOT in nodes either.
        # ob_0001 IS in nodes. dv_9991, dv_9992, ev_0001 are NOT.
        result = _CLI._find_dangling_edges(nodes, edges)
        assert len(result) == 3

    def test_valid_and_dangling_mixed(self):
        """Only dangling edges are returned; valid edges are not."""
        nodes = {"ob_0001": {}, "ev_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},   # valid
            {"source_id": "ob_0001", "target_id": "dv_9999", "relation": "derives_from"},  # dangling
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert len(result) == 1
        assert result[0] == ("ob_0001", "dv_9999", "derives_from")

    def test_returns_list_of_tuples(self):
        """Return type is a list of (src, tgt, relation) 3-tuples."""
        nodes = {"ob_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "missing_0001", "relation": "test_rel"},
        ]
        result = _CLI._find_dangling_edges(nodes, edges)
        assert isinstance(result, list)
        assert len(result) == 1
        src, tgt, rel = result[0]
        assert src == "ob_0001"
        assert tgt == "missing_0001"
        assert rel == "test_rel"

    def test_no_side_effects_on_inputs(self):
        """Calling the checker does not mutate nodes or edges."""
        nodes = {"ob_0001": {"id": "ob_0001"}}
        edges = [{"source_id": "ob_0001", "target_id": "ev_missing", "relation": "cites"}]
        nodes_before = dict(nodes)
        edges_before = [dict(e) for e in edges]
        _CLI._find_dangling_edges(nodes, edges)
        assert nodes == nodes_before
        assert edges == edges_before


# ---------------------------------------------------------------------------
# Tests for _assert_closure_completeness (thin wrapper)
# ---------------------------------------------------------------------------

class TestAssertClosureCompleteness:
    """The wrapper must still SystemExit on violation with the right message."""

    def test_clean_closure_does_not_exit(self):
        """No SystemExit when the closure is complete."""
        nodes = {"ob_0001": {}, "ev_0001": {}}
        edges = [{"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"}]
        # Should return normally (None) — no exception.
        result = _CLI._assert_closure_completeness(nodes, edges)
        assert result is None

    def test_dangling_edge_raises_system_exit(self):
        """Violation must raise SystemExit (CLI contract unchanged)."""
        nodes = {"ob_0001": {}, "ev_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},
            {"source_id": "ob_0001", "target_id": "dv_9999", "relation": "derives_from"},
        ]
        with pytest.raises(SystemExit) as exc_info:
            _CLI._assert_closure_completeness(nodes, edges)
        msg = str(exc_info.value.code)
        assert "ERROR: scope-export: closure-completeness invariant violated" in msg

    def test_exit_message_contains_count(self):
        """Exit message mentions the number of dangling edges."""
        nodes = {"ob_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "dv_0001", "relation": "derives_from"},
            {"source_id": "ob_0001", "target_id": "dv_0002", "relation": "derives_from"},
        ]
        with pytest.raises(SystemExit) as exc_info:
            _CLI._assert_closure_completeness(nodes, edges)
        msg = str(exc_info.value.code)
        assert "2 edge(s)" in msg

    def test_exit_message_contains_examples(self):
        """Exit message includes the arrow-notation examples."""
        nodes = {"ob_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "dv_9999", "relation": "derives_from"},
        ]
        with pytest.raises(SystemExit) as exc_info:
            _CLI._assert_closure_completeness(nodes, edges)
        msg = str(exc_info.value.code)
        assert "ob_0001" in msg
        assert "dv_9999" in msg
        assert "derives_from" in msg
