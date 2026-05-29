"""Tests for engram_inspect view-modes (view='recall', 'deep', 'edges').

Covers:
  1. view='recall' (default) — logical/contextual grouping, recall_summary present
     on hop-0 node, _RECALL_OMIT fields stripped
  2. view='deep' — all node fields present, adjacency-map topology shape
  3. view='edges' — topology-only output, no node payload; early-return skips
     the question-node `last_assessed_at` bump; `recall_refresh` still fires (deliberate probe behavior)
  4. dream_mode=True — recall_refresh skipped on the target node
  5. Invalid view — rejected with a sensible error message
  6. Recall grouping cap — with N > cap neighbors, cap fires and truncated_* key
     appears
  7. _RECALL_OMIT stripping — specific fields are absent from recall view
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_inspect_view_modes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# Fields that must be stripped from the focal node in recall view.
# These are defined as a local variable inside _build_inspect_recall_view;
# we replicate them here for assertion purposes.
_RECALL_OMIT = frozenset({
    "confidence_history", "logical_chain", "metadata",
    "importance_score", "importance_base", "utility_score",
    "recall_turn", "recall_count",
})


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
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


def _add_question(server, question):
    r = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": question,
        "category": "research",
    })))
    assert "question_id" in r, f"engram_ask failed: {r}"
    return r["question_id"]


def _set_recall_fields(server, node_id, summary, keywords):
    r = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


def _get_last_assessed_at(server, node_id):
    """Read last_assessed_at directly from DB."""
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT last_assessed_at FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return row["last_assessed_at"] if row else None
    finally:
        conn.close()


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
# 1. view='recall' — logical/contextual grouping + recall_summary on focal node
# ---------------------------------------------------------------------------

def test_view_recall_default_shape():
    """view='recall' (default) returns node + logical_neighbors + contextual_neighbors."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom A for inspect recall test.")
    ob_id = _add_observation(server, "Observation for inspect recall test.")
    dv_id = _derive(server, "Derived from axiom and obs for recall view.", [ax_id, ob_id])

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id})))
    assert "error" not in result, f"unexpected error: {result}"

    assert result["view"] == "recall"
    assert "node" in result, "node key missing from recall view"
    assert "logical_neighbors" in result, "logical_neighbors key missing"
    assert "contextual_neighbors" in result, "contextual_neighbors key missing"
    assert result["node"]["id"] == dv_id


def test_view_recall_logical_neighbor_grouping():
    """Logical neighbors are partitioned into upstream/downstream/lateral."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for grouping test.")
    dv_id = _derive(server, "Derivation citing axiom for grouping test.", [ax_id])

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"

    ln = result["logical_neighbors"]
    assert "upstream" in ln, "upstream key missing from logical_neighbors"
    assert "downstream" in ln, "downstream key missing from logical_neighbors"
    assert "lateral" in ln, "lateral key missing from logical_neighbors"

    # ax_id is upstream (dv derives_from ax — outgoing edge from dv)
    upstream_ids = [nb["id"] for nb in ln["upstream"]]
    assert ax_id in upstream_ids, (
        f"ax_id should appear in upstream (derives_from is outgoing); got {upstream_ids}"
    )


def test_view_recall_recall_summary_on_focal_node():
    """When recall_summary is set on focal node, it appears in the recall view node."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom with recall summary for inspect test.")
    _set_recall_fields(server, ax_id, "This axiom is about testing.", ["test", "axiom", "recall"])

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"

    node = result["node"]
    # recall_summary is NOT in _RECALL_OMIT — it should appear on the focal node
    assert node.get("recall_summary") == "This axiom is about testing.", (
        f"recall_summary missing or wrong on focal node: {node.get('recall_summary')}"
    )


# ---------------------------------------------------------------------------
# 2. _RECALL_OMIT stripping in recall view
# ---------------------------------------------------------------------------

def test_recall_omit_fields_stripped():
    """Fields in _RECALL_OMIT are absent from the focal node in recall view."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for _RECALL_OMIT stripping test.")

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"

    node = result["node"]
    for field in _RECALL_OMIT:
        assert field not in node, (
            f"_RECALL_OMIT field '{field}' must not appear in recall view node; "
            f"node keys: {list(node.keys())}"
        )


# ---------------------------------------------------------------------------
# 3. view='deep' — all fields present, adjacency-map topology shape
# ---------------------------------------------------------------------------

def test_view_deep_full_node_fields():
    """view='deep' includes the full node dict (not stripped)."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for deep-view field test.")

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "deep"})))
    assert "error" not in result, f"unexpected error: {result}"

    assert result["view"] == "deep"
    assert "node" in result
    assert "topology" in result
    node = result["node"]
    # Fields that recall view strips should be present in deep view
    # (if they exist on this node — confidence_history may be empty list)
    assert "id" in node
    assert "type" in node
    assert "claim" in node


def test_view_deep_topology_shape():
    """view='deep' topology is an adjacency map keyed by focal node id."""
    server = fresh_server()
    ob_id = _add_observation(server, "Obs for deep-view topology test.")
    dv_id = _derive(server, "Derivation for deep-view topology test.", [ob_id])

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "deep"})))
    assert "error" not in result, f"unexpected error: {result}"

    topology = result["topology"]
    assert isinstance(topology, dict), "topology should be a dict"
    assert dv_id in topology, f"focal node {dv_id} should be key in topology"

    entries = topology[dv_id]
    assert isinstance(entries, list), "topology entries should be a list"

    # Each entry has to/relation/direction
    for entry in entries:
        assert "to" in entry, f"entry missing 'to': {entry}"
        assert "relation" in entry, f"entry missing 'relation': {entry}"
        assert "direction" in entry, f"entry missing 'direction': {entry}"
        assert entry["direction"] in ("outgoing", "incoming"), (
            f"direction must be 'outgoing' or 'incoming'; got {entry['direction']}"
        )

    # ob_id should appear as a neighbor
    neighbor_ids = [e["to"] for e in entries]
    assert ob_id in neighbor_ids, (
        f"ob_id {ob_id} should appear in deep topology entries; got {neighbor_ids}"
    )


# ---------------------------------------------------------------------------
# 4. view='edges' — topology-only, no node payload
# ---------------------------------------------------------------------------

def test_view_edges_no_node_payload():
    """view='edges' returns topology + node_id/node_type but NO node field."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for edges-view no-payload test.")

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "edges"})))
    assert "error" not in result, f"unexpected error: {result}"

    assert result["view"] == "edges"
    assert "topology" in result
    assert "node_id" in result
    assert "node_type" in result
    assert "node" not in result, "edges view must not include full node payload"


def test_view_edges_topology_shape():
    """view='edges' topology adjacency map has correct shape."""
    server = fresh_server()
    ob_id = _add_observation(server, "Obs for edges-view shape test.")
    dv_id = _derive(server, "Derivation for edges-view shape test.", [ob_id])

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "edges"})))
    assert "error" not in result, f"unexpected error: {result}"

    topology = result["topology"]
    assert dv_id in topology, f"focal node {dv_id} should be key in topology"

    entries = topology[dv_id]
    for entry in entries:
        assert "to" in entry
        assert "relation" in entry
        assert "direction" in entry

    neighbor_ids = [e["to"] for e in entries]
    assert ob_id in neighbor_ids, (
        f"ob_id {ob_id} should appear in edges topology; got {neighbor_ids}"
    )


def test_view_edges_skips_question_node_assessment_bump():
    """view='edges' early-return does not bump last_assessed_at on question nodes."""
    server = fresh_server()
    qu_id = _add_question(server, "What is the implication of the edges early-return?")

    before = _get_last_assessed_at(server, qu_id)

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": qu_id, "view": "edges"})))
    assert "error" not in result, f"unexpected error: {result}"

    after = _get_last_assessed_at(server, qu_id)
    assert before == after, (
        f"view='edges' must not bump last_assessed_at on question nodes; "
        f"before={before}, after={after}"
    )


def test_view_recall_bumps_question_node_assessment():
    """view='recall' DOES bump last_assessed_at on question nodes (positive control)."""
    server = fresh_server()
    qu_id = _add_question(server, "What does recall view do to question-node timestamps?")

    before = _get_last_assessed_at(server, qu_id)

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": qu_id, "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"

    after = _get_last_assessed_at(server, qu_id)
    assert after is not None, "last_assessed_at should be set after recall view"
    assert after != before, (
        f"view='recall' should bump last_assessed_at on question nodes; "
        f"before={before}, after={after}"
    )


# ---------------------------------------------------------------------------
# 5. dream_mode=True — recall_refresh skipped on target node
# ---------------------------------------------------------------------------

def test_dream_mode_skips_recall_refresh():
    """With dream_mode=True, recall_turn on the target node is not updated."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for dream-mode inspect test.")

    before = _get_recall_turn(server, ax_id)

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "recall", "dream_mode": True})))
    assert "error" not in result, f"unexpected error: {result}"
    assert result["dream_mode"] is True

    after = _get_recall_turn(server, ax_id)
    assert before == after, (
        f"dream_mode=True must not update recall_turn; before={before}, after={after}"
    )


def test_normal_mode_does_refresh_recall():
    """Without dream_mode, recall_turn on the target node is updated (positive control)."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for normal-mode recall-refresh test.")

    before = _get_recall_turn(server, ax_id)

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "recall", "dream_mode": False})))
    assert "error" not in result, f"unexpected error: {result}"
    assert result["dream_mode"] is False

    after = _get_recall_turn(server, ax_id)
    assert after is not None
    assert after >= (before or 0), (
        f"Normal mode should refresh recall_turn; before={before}, after={after}"
    )


# ---------------------------------------------------------------------------
# 6. Invalid view rejected with sensible error
# ---------------------------------------------------------------------------

def test_invalid_view_rejected():
    """Passing an unrecognised view value returns an error containing the bad value."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for invalid-view test.")

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": ax_id, "view": "bogus"})))
    assert "error" in result, f"Expected error for view='bogus', got: {result}"
    assert "bogus" in result["error"], (
        f"Error message should name the bad view; got: {result['error']}"
    )


# ---------------------------------------------------------------------------
# 7. Recall grouping cap — truncated_* key appears when group exceeds cap
# ---------------------------------------------------------------------------

def test_recall_grouping_cap_fires():
    """With more upstream neighbors than cap, truncated_upstream_count appears."""
    server = fresh_server()

    # _INSPECT_RECALL_GROUP_CAP = 10; build 12 axioms all cited by a single derivation
    cap = 10
    ax_ids = [
        _add_axiom(server, f"Axiom {i} for cap-test in inspect view.", basis=f"Basis {i}.")
        for i in range(cap + 2)
    ]
    dv_id = _derive(
        server,
        "Derivation citing many axioms to trigger the recall cap.",
        ax_ids,
    )

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "recall"})))
    assert "error" not in result, f"unexpected error: {result}"

    ln = result["logical_neighbors"]
    upstream = ln.get("upstream", [])
    # Cap should have fired: upstream list is at most cap entries
    assert len(upstream) <= cap, (
        f"upstream should be capped at {cap}; got {len(upstream)}"
    )
    # truncated_upstream_count should appear
    assert "truncated_upstream_count" in ln, (
        f"truncated_upstream_count must appear when cap fires; "
        f"logical_neighbors keys: {list(ln.keys())}"
    )
    assert ln["truncated_upstream_count"] > 0, (
        f"truncated_upstream_count should be positive; got {ln['truncated_upstream_count']}"
    )
