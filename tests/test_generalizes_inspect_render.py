"""#2753 epic WS2, point 3: engram_inspect renders `generalizes` edges.

WS1 (PR #2812) added the `generalizes` edge relation. `_build_topology_entries`
in engram_query.py (the shared adjacency-map builder for engram_inspect's
view="deep" and view="edges") is relation-agnostic -- it returns
{src, dst, relation} for every edge touching a node, both directions,
regardless of relation name. This test is the empirical verification the WS2
spec required BEFORE writing any new render code for engram_inspect: it
confirms `generalizes` edges already show up correctly, both directions, on
both view modes, with zero code change.

Point 3 required NO code change -- this test is the confirmation, not a
regression guard for a fix.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_generalizes_inspect_")
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
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _seed_two_cornerstones(server):
    """Mint two fixture cornerstones and a `generalizes` edge between them
    (general -> specific), using engram_add_edge's whitelist (WS1)."""
    r1 = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "GeneralCS", "new_frame": "general frame",
    })))
    assert "error" not in r1, r1
    general_id = r1["cornerstone_id"]

    r2 = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "SpecificCS", "new_frame": "specific frame",
    })))
    assert "error" not in r2, r2
    specific_id = r2["cornerstone_id"]

    edge = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": general_id, "target_id": specific_id, "relation": "generalizes",
    })))
    assert edge.get("status") == "created", edge

    return general_id, specific_id


def test_inspect_edges_view_renders_generalizes_both_ends():
    """view='edges' on BOTH the general and specific end shows the same
    edge, correctly labeled relation='generalizes', with src/dst preserving
    the general->specific direction regardless of which end is inspected."""
    server = fresh_server()
    general_id, specific_id = _seed_two_cornerstones(server)

    general_view = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": general_id, "view": "edges",
    })))
    specific_view = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": specific_id, "view": "edges",
    })))

    general_edges = general_view["topology"][general_id]
    specific_edges = specific_view["topology"][specific_id]

    assert len(general_edges) == 1, general_edges
    assert len(specific_edges) == 1, specific_edges

    expected = {"src": general_id, "dst": specific_id, "relation": "generalizes"}
    assert general_edges[0] == expected, general_edges
    assert specific_edges[0] == expected, specific_edges


def test_inspect_deep_view_renders_generalizes_both_ends():
    """Same confirmation on view='deep' -- a second consumer of the same
    _build_topology_entries helper, per the spec's "both view=edges and
    view=deep" requirement."""
    server = fresh_server()
    general_id, specific_id = _seed_two_cornerstones(server)

    general_view = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": general_id, "view": "deep",
    })))
    specific_view = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": specific_id, "view": "deep",
    })))

    expected = {"src": general_id, "dst": specific_id, "relation": "generalizes"}
    assert general_view["topology"][general_id] == [expected], general_view
    assert specific_view["topology"][specific_id] == [expected], specific_view
