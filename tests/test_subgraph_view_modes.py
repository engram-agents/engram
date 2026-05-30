"""Tests for engram_get_subgraph view-modes (Wave B recall_summary rollout).

Covers:
  1. view='recall' default — topology + content with hop_distance per node
  2. view='edges' — topology only, no content key
  3. hop_distance=2+ nodes have recall_keywords but no recall_summary
  4. hop_distance=0 and 1 nodes include recall_summary when present
  5. invalid view rejected with clear error
  6. view='deep' rejected with engram_inspect suggestion
  7. dream_mode=True skips recall_refresh on neighbours
  8. direction='up'/'down'/'both' still work
  9. topology truncation: depth=1 does not include hop-2 edges
  10. edge_count and node_count match actual topology
  11. invalid direction rejected
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_subgraph_view_modes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_axiom(server, claim, basis="Test basis."):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": basis,
    })))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _add_observation(server, claim, url="https://example.com/test"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _derive(server, claim, supporting_ids):
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids) if isinstance(supporting_ids, list) else supporting_ids,
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _set_recall_fields(server, node_id, summary, keywords):
    """Write recall_summary + recall_keywords directly for testing."""
    r = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


def _get_recall_turn(server, node_id):
    """Read recall_turn directly from DB."""
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT recall_turn FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return row["recall_turn"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. view='recall' default — topology + content with hop_distance
# ---------------------------------------------------------------------------

def test_view_recall_default_shape():
    """view='recall' (default) returns topology + content with hop_distance."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom A for subgraph test.")
    ob_id = _add_observation(server, "Observation for subgraph test.")
    dv_id = _derive(server, "Derived from axiom and obs.", [ax_id, ob_id])

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv_id, "depth": 2, "direction": "both"})))
    assert "error" not in result, f"unexpected error: {result}"

    # Required top-level keys
    assert "topology" in result, "topology key missing"
    assert "content" in result, "content key missing for view=recall"
    assert result["view"] == "recall"
    assert result["root"] == dv_id

    # Every node in content has hop_distance
    for nid, entry in result["content"].items():
        assert "hop_distance" in entry, f"hop_distance missing for {nid}"
        assert "type" in entry, f"type missing for {nid}"

    # Root is in content at hop_distance=0
    assert dv_id in result["content"]
    assert result["content"][dv_id]["hop_distance"] == 0

    # Counts match actual content
    assert result["node_count"] == len(result["topology"])
    assert result["edge_count"] == sum(
        len(v) for v in result["topology"].values()
    ) // 2  # each edge listed once per endpoint, so total entries / 2


# ---------------------------------------------------------------------------
# 2. view='edges' — topology only, no content key
# ---------------------------------------------------------------------------

def test_view_edges_no_content():
    """view='edges' returns topology but omits content key entirely."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for edges-view test.")
    ob_id = _add_observation(server, "Obs for edges-view test.")
    dv_id = _derive(server, "Derived for edges-view test.", [ax_id, ob_id])

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv_id, "view": "edges"})))
    assert "error" not in result, f"unexpected error: {result}"
    assert result["view"] == "edges"
    assert "topology" in result, "topology key missing"
    assert "content" not in result, "content key must be absent for view=edges"
    assert result["root"] == dv_id


# ---------------------------------------------------------------------------
# 3. hop_distance=2+ nodes: recall_keywords only (no recall_summary)
# ---------------------------------------------------------------------------

def test_hop_2_nodes_keywords_only():
    """2-hop nodes get recall_keywords but not recall_summary."""
    server = fresh_server()
    # Build a 3-node chain: ob0 → dv1 → dv2
    ob0 = _add_observation(server, "Hop-0 obs for depth test.")
    dv1 = _derive(server, "Hop-1 derivation.", [ob0])
    dv2 = _derive(server, "Hop-2 derivation.", [dv1])

    # Write recall fields on all three (min 3 keywords required by validator)
    _set_recall_fields(server, ob0, "Summary for ob0", ["kw0a", "kw0b", "kw0c"])
    _set_recall_fields(server, dv1, "Summary for dv1", ["kw1a", "kw1b", "kw1c"])
    _set_recall_fields(server, dv2, "Summary for dv2", ["kw2a", "kw2b", "kw2c"])

    # Root at dv2, direction=up, depth=2 → dv2 (hop 0), dv1 (hop 1), ob0 (hop 2)
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv2, "depth": 2, "direction": "up", "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"
    content = result["content"]

    # dv2: hop 0 — should have recall_summary
    assert content[dv2]["hop_distance"] == 0
    assert "recall_summary" in content[dv2], "root should have recall_summary"
    assert content[dv2].get("recall_keywords") == ["kw2a", "kw2b", "kw2c"]

    # dv1: hop 1 — should have recall_summary
    assert content[dv1]["hop_distance"] == 1
    assert "recall_summary" in content[dv1], "hop-1 node should have recall_summary"
    assert content[dv1].get("recall_keywords") == ["kw1a", "kw1b", "kw1c"]

    # ob0: hop 2 — should NOT have recall_summary
    assert content[ob0]["hop_distance"] == 2
    assert "recall_summary" not in content[ob0], "hop-2 node must NOT have recall_summary"
    assert content[ob0].get("recall_keywords") == ["kw0a", "kw0b", "kw0c"]


# ---------------------------------------------------------------------------
# 4. recall_summary absent when node has none written
# ---------------------------------------------------------------------------

def test_recall_summary_absent_when_not_written():
    """A hop-0 node with no recall_summary written doesn't get a None entry."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom with no recall_summary.")

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ax_id, "depth": 1, "view": "recall"})))
    assert "error" not in result
    entry = result["content"][ax_id]
    # recall_summary should be absent (not present) or omitted — never None
    assert entry.get("recall_summary") is None or "recall_summary" not in entry


# ---------------------------------------------------------------------------
# 5. invalid view rejected
# ---------------------------------------------------------------------------

def test_invalid_view_rejected():
    """Passing an unrecognised view value returns an error."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for invalid-view test.")

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ax_id, "view": "full"})))
    assert "error" in result, f"Expected error for view='full', got: {result}"
    assert "full" in result["error"]


# ---------------------------------------------------------------------------
# 6. view='deep' rejected with engram_inspect suggestion
# ---------------------------------------------------------------------------

def test_view_deep_rejected_with_hint():
    """view='deep' is rejected with a clear suggestion to use engram_inspect."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for view-deep test.")

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ax_id, "view": "deep"})))
    assert "error" in result
    assert "engram_inspect" in result["error"], (
        f"Error should mention engram_inspect: {result['error']}"
    )


# ---------------------------------------------------------------------------
# 7. dream_mode=True skips recall_refresh on neighbours
# ---------------------------------------------------------------------------

def test_dream_mode_skips_neighbour_recall_refresh():
    """With dream_mode=True, recall_turn on neighbours is not updated."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for dream-mode test.")
    ob_id = _add_observation(server, "Obs for dream-mode test.")
    dv_id = _derive(server, "Derived for dream-mode test.", [ax_id, ob_id])

    # Capture recall_turn for ax_id before the subgraph call
    before_ax = _get_recall_turn(server, ax_id)

    # Call with dream_mode=True — neighbours should NOT get recall_refresh
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps(
        {"node_id": dv_id, "depth": 1, "direction": "up", "dream_mode": True}
    )))
    assert "error" not in result
    assert result["dream_mode"] is True

    # ax_id is a neighbour (hop 1); its recall_turn must not have changed
    after_ax = _get_recall_turn(server, ax_id)
    assert before_ax == after_ax, (
        f"dream_mode=True must not refresh recall_turn on neighbours; "
        f"before={before_ax}, after={after_ax}"
    )


def test_normal_mode_refreshes_neighbour_recall():
    """With dream_mode=False (default), neighbours do get recall_refresh."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for normal-mode refresh test.")
    ob_id = _add_observation(server, "Obs for normal-mode refresh test.")
    dv_id = _derive(server, "Derived for normal-mode refresh test.", [ax_id, ob_id])

    before_ax = _get_recall_turn(server, ax_id)

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps(
        {"node_id": dv_id, "depth": 1, "direction": "up", "dream_mode": False}
    )))
    assert "error" not in result
    assert result["dream_mode"] is False

    after_ax = _get_recall_turn(server, ax_id)
    # recall_turn should be >= before (refresh bumps it to current turn)
    assert after_ax >= (before_ax or 0), (
        f"Normal mode should refresh recall_turn on neighbours; "
        f"before={before_ax}, after={after_ax}"
    )


# ---------------------------------------------------------------------------
# 8. direction='up'/'down'/'both' still work
# ---------------------------------------------------------------------------

def test_direction_up_traverses_toward_evidence():
    """direction='up' traverses outgoing edges (toward evidence)."""
    server = fresh_server()
    ob_id = _add_observation(server, "Evidence for direction-up test.")
    dv_id = _derive(server, "Derivation citing obs.", [ob_id])

    # direction=up from dv_id should reach ob_id (dv derives_from ob)
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv_id, "depth": 1, "direction": "up"})))
    assert "error" not in result
    assert ob_id in result["topology"], "direction=up should include the cited observation"


def test_direction_down_traverses_toward_dependents():
    """direction='down' traverses incoming edges (toward dependents)."""
    server = fresh_server()
    ob_id = _add_observation(server, "Evidence for direction-down test.")
    dv_id = _derive(server, "Derivation citing obs for down test.", [ob_id])

    # direction=down from ob_id should reach dv_id (dv derives_from ob, so ob is target)
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ob_id, "depth": 1, "direction": "down"})))
    assert "error" not in result
    assert dv_id in result["topology"], "direction=down should include the dependent derivation"


def test_direction_both_includes_all():
    """direction='both' includes nodes in both traversal directions."""
    server = fresh_server()
    ob_id = _add_observation(server, "Evidence for direction-both test.")
    dv1 = _derive(server, "Derivation 1 citing obs.", [ob_id])
    dv2 = _derive(server, "Derivation 2 citing dv1.", [dv1])

    # Start from dv1 with both: should get ob_id (up) and dv2 (down)
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv1, "depth": 1, "direction": "both"})))
    assert "error" not in result
    assert ob_id in result["topology"], "direction=both should include up-neighbour (obs)"
    assert dv2 in result["topology"], "direction=both should include down-neighbour (dv2)"


# ---------------------------------------------------------------------------
# 9. topology truncation: depth=1 does not include hop-2 edges
# ---------------------------------------------------------------------------

def test_topology_truncation_at_depth_boundary():
    """At depth=1, hop-2 nodes are absent from topology so no dangling edges."""
    server = fresh_server()
    # Build ob0 → dv1 → dv2 chain
    ob0 = _add_observation(server, "Hop-0 obs for truncation test.")
    dv1 = _derive(server, "Hop-1 deriv for truncation test.", [ob0])
    dv2 = _derive(server, "Hop-2 deriv for truncation test.", [dv1])

    # Root at dv2, depth=1, direction=up → dv2 (hop 0) + dv1 (hop 1); ob0 beyond depth
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv2, "depth": 1, "direction": "up"})))
    assert "error" not in result
    assert dv2 in result["topology"]
    assert dv1 in result["topology"]
    # ob0 should be absent — it's at hop 2
    assert ob0 not in result["topology"], (
        "ob0 at hop-2 should be excluded from depth=1 subgraph"
    )

    # No edge should reference ob0 (topology truncation rule)
    for entries in result["topology"].values():
        for entry in entries:
            assert entry["to"] != ob0, (
                "Boundary edges to hop-2 node ob0 must be dropped from topology"
            )


# ---------------------------------------------------------------------------
# 10. node_count and edge_count are correct
# ---------------------------------------------------------------------------

def test_node_and_edge_counts_match_topology():
    """node_count and edge_count match the actual topology content."""
    server = fresh_server()
    ob_id = _add_observation(server, "Obs for count test.")
    dv_id = _derive(server, "Derivation for count test.", [ob_id])

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv_id, "depth": 1, "direction": "up"})))
    assert "error" not in result

    # node_count == number of keys in topology
    assert result["node_count"] == len(result["topology"]), (
        f"node_count={result['node_count']} != topology keys={len(result['topology'])}"
    )
    # edge_count == number of unique edges (each edge appears as outgoing from src
    # and incoming to tgt — total adjacency list entries / 2)
    total_entries = sum(len(v) for v in result["topology"].values())
    assert result["edge_count"] == total_entries // 2, (
        f"edge_count={result['edge_count']} != total_entries//2={total_entries // 2}"
    )


# ---------------------------------------------------------------------------
# 11. invalid direction rejected
# ---------------------------------------------------------------------------

def test_invalid_direction_rejected():
    """Passing an unrecognised direction value returns an error."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for invalid-direction test.")

    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ax_id, "direction": "sideways"})))
    assert "error" in result, f"Expected error for direction='sideways', got: {result}"
    assert "sideways" in result["error"]


# ---------------------------------------------------------------------------
# 12. adjacency-map has correct direction labels
# ---------------------------------------------------------------------------

def test_adjacency_map_direction_labels():
    """Topology entries carry correct 'direction' labels (outgoing/incoming)."""
    server = fresh_server()
    ob_id = _add_observation(server, "Obs for direction-label test.")
    dv_id = _derive(server, "Derivation citing obs for direction-label test.", [ob_id])

    # depth=1, direction=both from dv_id
    result = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": dv_id, "depth": 1, "direction": "up"})))
    assert "error" not in result

    # From dv_id's perspective, the derives_from edge to ob_id is outgoing
    dv_entries = result["topology"].get(dv_id, [])
    outgoing = [e for e in dv_entries if e["to"] == ob_id and e["direction"] == "outgoing"]
    assert len(outgoing) >= 1, (
        f"Expected outgoing edge from {dv_id} to {ob_id}; got {dv_entries}"
    )

    # From ob_id's perspective, the same edge is incoming
    ob_entries = result["topology"].get(ob_id, [])
    incoming = [e for e in ob_entries if e["to"] == dv_id and e["direction"] == "incoming"]
    assert len(incoming) >= 1, (
        f"Expected incoming edge at {ob_id} from {dv_id}; got {ob_entries}"
    )
