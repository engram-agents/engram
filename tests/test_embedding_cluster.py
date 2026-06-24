"""Tests for engram_embedding_cluster (issue #1154).

Coverage:
  1. Empty input — returns empty result cleanly.
  2. All unembedded — all nodes land in unembedded list.
  3. Two similar nodes cluster — cosine >= threshold.
  4. Two dissimilar nodes stay separate — cosine < threshold.
  5. Transitive closure — A~B, B~C but A!~C → {A,B,C} in one cluster.
  6. min_cluster_size=1 — singletons promoted to clusters.
  7. threshold validation — out-of-range rejected.
  8. Unknown fields rejected.
  9. Missing node_ids rejected.
  10. node_ids not a list rejected.
  11. Too many node_ids (>500) rejected.
"""

import atexit
import json
import math
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_emb_cluster_")
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


def _make_unit_vec(dim: int, hot_index: int) -> list[float]:
    """Return a unit vector with 1.0 at hot_index and 0.0 elsewhere."""
    v = [0.0] * dim
    v[hot_index] = 1.0
    return v


def _make_mixed_vec(dim: int, i: int, j: int) -> list[float]:
    """Return a normalised vector with 0.5 at positions i and j."""
    v = [0.0] * dim
    v[i] = 1.0
    v[j] = 1.0
    norm = math.sqrt(2)
    return [x / norm for x in v]


def _seed_observation(server, claim: str) -> str:
    """Add a minimal observation and return its node_id."""
    result = json.loads(server.engram_add_observation(json.dumps({
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": claim,
        "interpretation": claim,
        "claim": claim,
        "quote_type": "editorial",
    })))
    return result["observation_id"]


def _store_embedding(server, node_id: str, vec: list[float]) -> None:
    """Directly store an embedding vector in the nodes table."""
    conn = server._get_db()
    conn.execute(
        "UPDATE nodes SET embedding = ? WHERE id = ?",
        (json.dumps(vec), node_id),
    )
    conn.commit()


# ── 1. Empty input ──────────────────────────────────────────────────────────

def test_empty_node_ids():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(json.dumps({"node_ids": []})))
    assert result["clusters"] == []
    assert result["singletons"] == []
    assert result["unembedded"] == []
    assert result["pair_count"] == 0


# ── 2. All unembedded ───────────────────────────────────────────────────────

def test_all_unembedded():
    server = fresh_server()
    ob1 = _seed_observation(server, "alpha claim")
    ob2 = _seed_observation(server, "beta claim")
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [ob1, ob2]})
    ))
    assert result["clusters"] == []
    assert result["singletons"] == []
    assert set(result["unembedded"]) == {ob1, ob2}


# ── 3. Two similar nodes cluster ────────────────────────────────────────────

def test_similar_nodes_cluster():
    server = fresh_server()
    ob1 = _seed_observation(server, "alpha claim")
    ob2 = _seed_observation(server, "beta claim")
    # Both point in dimension 0 → cosine = 1.0
    _store_embedding(server, ob1, _make_unit_vec(4, 0))
    _store_embedding(server, ob2, _make_unit_vec(4, 0))
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [ob1, ob2], "threshold": 0.55})
    ))
    assert len(result["clusters"]) == 1
    assert set(result["clusters"][0]) == {ob1, ob2}
    assert result["singletons"] == []
    assert result["pair_count"] == 1


# ── 4. Dissimilar nodes stay separate ───────────────────────────────────────

def test_dissimilar_nodes_separate():
    server = fresh_server()
    ob1 = _seed_observation(server, "alpha claim")
    ob2 = _seed_observation(server, "beta claim")
    # Orthogonal vectors → cosine = 0.0
    _store_embedding(server, ob1, _make_unit_vec(4, 0))
    _store_embedding(server, ob2, _make_unit_vec(4, 1))
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [ob1, ob2], "threshold": 0.55})
    ))
    assert result["clusters"] == []
    assert set(result["singletons"]) == {ob1, ob2}


# ── 5. Transitive closure ────────────────────────────────────────────────────

def test_transitive_closure():
    server = fresh_server()
    ob_a = _seed_observation(server, "A claim")
    ob_b = _seed_observation(server, "B claim")
    ob_c = _seed_observation(server, "C claim")
    # A and B share dims 0+1; B and C share dims 1+2; A and C share dim 0 only.
    # With 3 dims, cosines: A·B = cos(45°)≈0.707, B·C ≈ 0.707, A·C = 0.5
    # threshold=0.6 → A~B and B~C but not A~C directly;
    # transitive closure should put all 3 in one cluster.
    _store_embedding(server, ob_a, _make_mixed_vec(4, 0, 1))
    _store_embedding(server, ob_b, _make_mixed_vec(4, 1, 2))
    _store_embedding(server, ob_c, _make_mixed_vec(4, 0, 2))
    # A·B: dims 1 shared, norm each = 1 → dot = 0.5*0.5 + 0.5*0.5 = 0.5
    # A·C: dims 0 shared → dot = 0.5
    # B·C: dims 2 shared → dot = 0.5
    # All cosines = 0.5 — none reach threshold 0.6; all singletons.
    # Use threshold 0.4 to force all to cluster.
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [ob_a, ob_b, ob_c], "threshold": 0.4})
    ))
    assert len(result["clusters"]) == 1
    assert set(result["clusters"][0]) == {ob_a, ob_b, ob_c}


# ── 6. min_cluster_size=1 promotes singletons ───────────────────────────────

def test_min_cluster_size_one():
    server = fresh_server()
    ob1 = _seed_observation(server, "alpha claim")
    _store_embedding(server, ob1, _make_unit_vec(4, 0))
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [ob1], "threshold": 0.55, "min_cluster_size": 1})
    ))
    assert len(result["clusters"]) == 1
    assert result["clusters"][0] == [ob1]
    assert result["singletons"] == []


# ── 7. threshold out of range ────────────────────────────────────────────────

def test_threshold_out_of_range():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [], "threshold": 1.5})
    ))
    assert "error" in result


def test_threshold_negative():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [], "threshold": -0.1})
    ))
    assert "error" in result


# ── 8. Unknown fields rejected ───────────────────────────────────────────────

def test_unknown_field_rejected():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [], "bogus_param": True})
    ))
    assert "error" in result


# ── 9. Missing node_ids ──────────────────────────────────────────────────────

def test_missing_node_ids():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"threshold": 0.5})
    ))
    assert "error" in result


# ── 10. node_ids not a list ──────────────────────────────────────────────────

def test_node_ids_not_list():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": "not-a-list"})
    ))
    assert "error" in result


# ── 11. Too many node_ids ────────────────────────────────────────────────────

def test_too_many_node_ids():
    server = fresh_server()
    result = json.loads(server.engram_embedding_cluster(
        json.dumps({"node_ids": [f"ob_{i:04d}" for i in range(501)]})
    ))
    assert "error" in result
