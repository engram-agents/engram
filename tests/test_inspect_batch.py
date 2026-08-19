"""Batch inspect — engram_inspect accepting a list of node IDs (#1956).

Motivation: inspecting N nodes cost N round-trips, which is the friction that
made a raw sqlite3 script look cheaper than the tool. The tool losing to a
workaround on ergonomics is how a whole capability goes unused.

The contract these tests pin, in priority order:
  1. A batch result is IDENTICAL to N single inspects — no quiet shape drift.
  2. Over-cap is REFUSED, never truncated. A partial result nobody asked for is
     indistinguishable from a complete one, and batch reads feed correctness
     checks.
  3. One bad ID does not destroy the batch — the other nodes were legitimately
     requested.
  4. Duplicates are collapsed, because inspecting a node twice would refresh its
     memory twice: a side effect the caller neither asked for nor can see.
"""

import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_inspect_batch_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _seed(client, n):
    """Create n axioms and return their IDs. Axioms need no evidence."""
    ids = []
    for i in range(n):
        r = client.call("engram_add_axiom", {
            "claim": f"Seeded axiom number {i} for batch-inspect coverage",
            "basis": f"Test fixture {i}.",
        })
        ids.append(r["axiom_id"])
    return ids


# ── happy path ───────────────────────────────────────────────────────────

def test_batch_returns_every_requested_node_in_order():
    client = fresh_client()
    ids = _seed(client, 3)
    r = client.call("engram_inspect", {"node_ids": ids})
    assert r["count"] == 3
    assert [n["node"]["id"] for n in r["nodes"]] == ids


def test_batch_entry_is_identical_to_a_single_inspect():
    """The whole point: batching must not change what you get back."""
    client = fresh_client()
    ids = _seed(client, 2)
    single = client.call("engram_inspect", {"node_id": ids[0], "dream_mode": True})
    batch = client.call("engram_inspect", {"node_ids": ids, "dream_mode": True})
    assert batch["nodes"][0] == single


def test_view_and_dream_mode_apply_to_every_entry():
    client = fresh_client()
    ids = _seed(client, 2)
    r = client.call("engram_inspect", {"node_ids": ids, "view": "edges", "dream_mode": True})
    for entry in r["nodes"]:
        assert entry["view"] == "edges"


# ── the refusals that matter ─────────────────────────────────────────────

def test_over_cap_is_refused_not_truncated():
    client = fresh_client()
    ids = _seed(client, 3)
    r = client.call("engram_inspect", {"node_ids": ids * 20})  # 60 > cap
    assert "error" in r
    assert "cap" in r["error"].lower()
    assert "nodes" not in r, "over-cap must refuse outright, never return a partial batch"


def test_node_id_and_node_ids_together_is_an_error():
    client = fresh_client()
    ids = _seed(client, 2)
    r = client.call("engram_inspect", {"node_id": ids[0], "node_ids": ids})
    assert "error" in r and "not both" in r["error"]


def test_neither_node_id_nor_node_ids_is_an_error():
    client = fresh_client()
    r = client.call("engram_inspect", {"view": "recall"})
    assert "error" in r


def test_empty_list_is_an_error():
    client = fresh_client()
    r = client.call("engram_inspect", {"node_ids": []})
    assert "error" in r


def test_non_list_and_non_string_entries_are_errors():
    client = fresh_client()
    ids = _seed(client, 1)
    assert "error" in client.call("engram_inspect", {"node_ids": ids[0]})
    assert "error" in client.call("engram_inspect", {"node_ids": [ids[0], 42]})


# ── partial-failure and dedupe behaviour ─────────────────────────────────

def test_one_missing_id_does_not_destroy_the_batch():
    client = fresh_client()
    ids = _seed(client, 2)
    r = client.call("engram_inspect", {"node_ids": [ids[0], "ob_9999999", ids[1]]})
    assert r["count"] == 3
    assert r["nodes"][0]["node"]["id"] == ids[0]
    assert "error" in r["nodes"][1]
    assert r["nodes"][2]["node"]["id"] == ids[1], "a bad ID must not drop later good ones"


def test_duplicates_are_collapsed_and_reported():
    client = fresh_client()
    ids = _seed(client, 2)
    r = client.call("engram_inspect", {"node_ids": [ids[0], ids[1], ids[0]]})
    assert r["count"] == 2
    assert [n["node"]["id"] for n in r["nodes"]] == ids
    assert r["deduped"] == 1


def test_single_node_path_is_unchanged():
    """Regression guard — the batch addition must not disturb existing callers."""
    client = fresh_client()
    ids = _seed(client, 1)
    r = client.call("engram_inspect", {"node_id": ids[0]})
    assert r["node"]["id"] == ids[0]
    assert "nodes" not in r and "count" not in r
