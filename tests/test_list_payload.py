"""Tests for the engram_list payload_json wrapper (migrate/read-cluster-payload-json).

Coverage:
  1. Unknown-field rejection — payload with an unrecognised key returns {"error": ...}
     with "Unknown fields" in the message.
  2. _list_impl callable with named kwargs — internal function works without
     going through the JSON wrapper.
  3. Invalid JSON rejection — non-parseable payload_json string returns {"error": ...}
     with "Invalid JSON" in the message.
  4. Non-object JSON payload rejection — valid JSON but non-object (e.g. a list)
     returns {"error": ...} with "must be a JSON object" in the message.
  5. sort_by / node_type refuse-unrecognized-values (engram-alpha#1927) — an
     unrecognized sort_by or node_type must error loudly rather than silently
     falling back (sort_by) or silently returning zero rows (node_type). Also
     covers: every valid sort key still orders correctly, a valid node_type
     still filters correctly, and empty/absent sort_by + node_type keep
     meaning "default sort" / "no filter".
  6. sort_order (engram-alpha#3016) — sort_order="asc" flips the direction for
     each of the 4 sort_by values; sort_order omitted is byte-identical to
     pre-change behavior (regression guard); an unrecognized sort_order
     errors loudly rather than silently falling back; and the literal
     oldest-first regression this issue exists to fix (sort_by="created",
     sort_order="asc" surfaces the OLDEST node first).

  Note: engram_list has NO required fields (all have defaults), so the
  missing-required-field test (path 5 in the original numbering) is omitted.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_list_payload_")
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


# ---------------------------------------------------------------------------
# 1. Unknown-field rejection
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    """Payload containing an unrecognised field returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "node_type": "observation_factual",
        "bogus_field": "should be rejected",
    })))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "Unknown fields" in r["error"], (
        f"Expected 'Unknown fields' in error message, got: {r['error']}"
    )
    assert "bogus_field" in r["error"], (
        f"Expected rejected field name in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 2. _list_impl callable with named kwargs
# ---------------------------------------------------------------------------

def test_impl_callable_with_kwargs():
    """_list_impl is directly callable with named kwargs (in-server caller path)."""
    server = fresh_server()
    # Add an observation so the list has something to return.
    r_add = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/list-payload-test",
        "title": "Test Source",
        "quoted_text": "List impl kwargs test claim verbatim.",
        "interpretation": "Test interpretation for list kwargs callable check.",
        "claim": "List impl kwargs callable test claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r_add, f"add_observation failed: {r_add}"

    # Call _list_impl directly with named kwargs — must not raise, must return valid JSON.
    result_str = server._list_impl(node_type="observation_factual")
    r = json.loads(result_str)
    assert "error" not in r, f"_list_impl returned error: {r}"
    assert "nodes" in r or "status" in r, (
        f"_list_impl did not return expected shape: {r}"
    )


# ---------------------------------------------------------------------------
# 3. Invalid JSON rejection
# ---------------------------------------------------------------------------

def test_invalid_json_rejected():
    """A payload_json string that isn't valid JSON returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_list(payload_json='{"node_type": "x", not valid json'))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "Invalid JSON" in r["error"], (
        f"Expected 'Invalid JSON' in error message, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 4. Non-object JSON payload rejection
# ---------------------------------------------------------------------------

def test_non_object_payload_rejected():
    """A valid-JSON but non-object payload (e.g. a list) returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_list(payload_json=json.dumps([1, 2, 3])))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "must be a JSON object" in r["error"], (
        f"Expected 'must be a JSON object' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 5. sort_by / node_type refuse-unrecognized-values (engram-alpha#1927)
# ---------------------------------------------------------------------------
#
# Two prior silent-fallback defects:
#   (a) an unrecognized sort_by silently fell back to id-order while the
#       response echoed "sort_by": <the unrecognized value> back as though
#       it had been honoured.
#   (b) an unrecognized node_type silently returned total_matched: 0 —
#       indistinguishable from "you genuinely have none of these".
# Both must now refuse with an error naming the valid values.

def _add_observation(server, claim):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/list-sort-test",
        "title": "Test Source",
        "quoted_text": claim,
        "interpretation": "Test interpretation for sort/node_type refusal tests.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def test_unknown_sort_by_rejected():
    """An unrecognized sort_by errors loudly and does not return rows."""
    server = fresh_server()
    _add_observation(server, "Node present so a silent-fallback would have rows to return.")
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "confidence",  # not a valid sort_map key
    })))
    assert "nodes" not in r, f"Expected refusal (no 'nodes' key), got: {r}"
    assert "error" in r, f"Expected error key, got: {r}"
    for key in ("id", "created", "importance", "recalls"):
        assert key in r["error"], (
            f"Expected valid sort key {key!r} listed in error, got: {r['error']}"
        )


def test_unknown_node_type_rejected():
    """An unrecognized node_type errors loudly rather than returning a false empty success."""
    server = fresh_server()
    _add_observation(server, "Node present so a silent-empty-filter would look plausible.")
    # 'observation' (singular, no _factual/_predictive suffix) is the exact
    # near-miss that produced the live false-zero this issue cites.
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "node_type": "observation",
    })))
    assert r.get("status") != "ok", (
        f"Expected refusal, got a success payload (the exact defect this guards against): {r}"
    )
    assert "error" in r, f"Expected error key, got: {r}"
    assert "observation_factual" in r["error"], (
        f"Expected a valid node type listed in error, got: {r['error']}"
    )


def test_valid_node_type_still_filters():
    """A recognized node_type still filters correctly (mixed types in the graph)."""
    server = fresh_server()
    obs_id = _add_observation(server, "Observation node for node_type filter test.")
    task_r = json.loads(server.engram_add_task(payload_json=json.dumps({
        "description": "Task node for node_type filter test.",
    })))
    assert "error" not in task_r, f"add_task failed: {task_r}"

    r = json.loads(server.engram_list(payload_json=json.dumps({
        "node_type": "observation_factual",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    ids = {n["id"] for n in r["nodes"]}
    assert obs_id in ids, f"Expected {obs_id} in filtered results, got ids: {ids}"
    for n in r["nodes"]:
        assert n["type"] == "observation_factual", f"Unexpected type leaked through filter: {n}"


def test_empty_node_type_means_no_filter():
    """Empty-string node_type (default) still means 'no filter', not a refusal."""
    server = fresh_server()
    obs_id = _add_observation(server, "Observation node for empty node_type default test.")
    r = json.loads(server.engram_list(payload_json=json.dumps({"node_type": ""})))
    assert r["status"] == "ok", f"Expected ok status (no filter), got: {r}"
    ids = {n["id"] for n in r["nodes"]}
    assert obs_id in ids, f"Expected {obs_id} present with no node_type filter, got: {ids}"


def test_empty_sort_by_means_default_sort():
    """Empty-string sort_by still means 'default (id) order', not a refusal."""
    server = fresh_server()
    _add_observation(server, "Observation node for empty sort_by default test.")
    r = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": ""})))
    assert r["status"] == "ok", f"Expected ok status (default sort), got: {r}"
    assert r["sort_by"] == "", f"Expected sort_by echoed as-is (empty), got: {r['sort_by']}"


def _seed_three_for_sort(server):
    """Seed three observations with distinct importance_score, recall_count,
    and created_at so each valid sort_by key produces a distinguishable order."""
    ids = [
        _add_observation(server, "Sort-order seed node A (low importance/recall, oldest)."),
        _add_observation(server, "Sort-order seed node B (mid importance/recall, mid-age)."),
        _add_observation(server, "Sort-order seed node C (high importance/recall, newest)."),
    ]
    conn = server.core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=0.1, recall_count=1, "
            "created_at='2020-01-01T00:00:00Z' WHERE id=?", (ids[0],)
        )
        conn.execute(
            "UPDATE nodes SET importance_score=0.5, recall_count=5, "
            "created_at='2021-01-01T00:00:00Z' WHERE id=?", (ids[1],)
        )
        conn.execute(
            "UPDATE nodes SET importance_score=0.9, recall_count=9, "
            "created_at='2022-01-01T00:00:00Z' WHERE id=?", (ids[2],)
        )
        conn.commit()
    finally:
        conn.close()
    return ids


def test_valid_sort_by_id_orders_correctly():
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "id"})))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == sorted(ids), f"Expected ascending id order, got: {returned}"


def test_valid_sort_by_created_orders_correctly():
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "created"})))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == list(reversed(ids)), (
        f"Expected newest-first order (reverse of seed order {ids}), got: {returned}"
    )


def test_valid_sort_by_importance_orders_correctly():
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "importance"})))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == list(reversed(ids)), (
        f"Expected highest-importance-first order (reverse of seed order {ids}), got: {returned}"
    )


def test_valid_sort_by_recalls_orders_correctly():
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "recalls"})))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == list(reversed(ids)), (
        f"Expected most-recalled-first order (reverse of seed order {ids}), got: {returned}"
    )


# ---------------------------------------------------------------------------
# 6. sort_order (engram-alpha#3016)
# ---------------------------------------------------------------------------
#
# _seed_three_for_sort seeds ids = [A, B, C] with A oldest/lowest-importance/
# lowest-recall-count, B mid, C newest/highest-importance/highest-recall-count.
# So the seed order [A, B, C] IS the ascending order for created/importance/
# recalls, and sorted(ids) is the ascending order for id.

def test_sort_order_asc_flips_created():
    """sort_order='asc' reverses 'created' from its default (newest-first)."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "created", "sort_order": "asc",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == ids, (
        f"Expected oldest-first order (seed order {ids}) with sort_order=asc, got: {returned}"
    )
    assert r["sort_order"] == "asc", f"Expected resolved sort_order 'asc', got: {r['sort_order']}"


def test_sort_order_asc_flips_importance():
    """sort_order='asc' reverses 'importance' from its default (highest-first)."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "importance", "sort_order": "asc",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == ids, (
        f"Expected lowest-importance-first order (seed order {ids}) with sort_order=asc, got: {returned}"
    )
    assert r["sort_order"] == "asc", f"Expected resolved sort_order 'asc', got: {r['sort_order']}"


def test_sort_order_asc_flips_recalls():
    """sort_order='asc' reverses 'recalls' from its default (most-recalled-first)."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "recalls", "sort_order": "asc",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == ids, (
        f"Expected least-recalled-first order (seed order {ids}) with sort_order=asc, got: {returned}"
    )
    assert r["sort_order"] == "asc", f"Expected resolved sort_order 'asc', got: {r['sort_order']}"


def test_sort_order_desc_flips_id():
    """sort_order='desc' reverses 'id' from its default (ascending)."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "id", "sort_order": "desc",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == list(reversed(sorted(ids))), (
        f"Expected descending id order with sort_order=desc, got: {returned}"
    )
    assert r["sort_order"] == "desc", f"Expected resolved sort_order 'desc', got: {r['sort_order']}"


def test_sort_order_omitted_matches_pre_change_behavior():
    """Omitting sort_order (the field absent entirely, not empty string) is a
    byte-identical regression guard: ordering + resolved sort_order both match
    what sort_by's own default direction always produced before sort_order
    existed."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)

    r_created = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "created"})))
    assert r_created["status"] == "ok", f"Expected ok status, got: {r_created}"
    returned = [n["id"] for n in r_created["nodes"] if n["id"] in ids]
    assert returned == list(reversed(ids)), (
        f"Expected unchanged newest-first default order, got: {returned}"
    )
    assert r_created["sort_order"] == "desc", (
        f"Expected resolved sort_order to reflect 'created' default direction 'desc', "
        f"got: {r_created['sort_order']}"
    )

    r_id = json.loads(server.engram_list(payload_json=json.dumps({"sort_by": "id"})))
    assert r_id["status"] == "ok", f"Expected ok status, got: {r_id}"
    returned_id = [n["id"] for n in r_id["nodes"] if n["id"] in ids]
    assert returned_id == sorted(ids), f"Expected unchanged ascending id default order, got: {returned_id}"
    assert r_id["sort_order"] == "asc", (
        f"Expected resolved sort_order to reflect 'id' default direction 'asc', "
        f"got: {r_id['sort_order']}"
    )


def test_invalid_sort_order_rejected():
    """An unrecognized sort_order errors loudly and does not return rows."""
    server = fresh_server()
    _add_observation(server, "Node present so a silent-fallback would have rows to return.")
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "created", "sort_order": "sideways",
    })))
    assert "nodes" not in r, f"Expected refusal (no 'nodes' key), got: {r}"
    assert "error" in r, f"Expected error key, got: {r}"
    for key in ("asc", "desc"):
        assert key in r["error"], (
            f"Expected valid sort_order value {key!r} listed in error, got: {r['error']}"
        )


def test_sort_order_case_insensitive():
    """sort_order accepts mixed-case values ('ASC' as well as 'asc')."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "created", "sort_order": "ASC",
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == ids, f"Expected oldest-first order with sort_order='ASC', got: {returned}"
    assert r["sort_order"] == "asc", f"Expected resolved sort_order lowercased to 'asc', got: {r['sort_order']}"


def test_empty_sort_order_means_default_direction():
    """Empty-string sort_order (explicit, not omitted) still means 'use
    sort_by's own default direction', not a refusal."""
    server = fresh_server()
    ids = _seed_three_for_sort(server)
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "created", "sort_order": "",
    })))
    assert r["status"] == "ok", f"Expected ok status (default direction), got: {r}"
    returned = [n["id"] for n in r["nodes"] if n["id"] in ids]
    assert returned == list(reversed(ids)), (
        f"Expected unchanged newest-first default order with sort_order='', got: {returned}"
    )
    assert r["sort_order"] == "desc", f"Expected resolved sort_order 'desc', got: {r['sort_order']}"


def test_sort_by_created_asc_surfaces_oldest_node_first():
    """The literal regression this issue exists to fix (engram-alpha#3016):
    with sort_by='created', sort_order='asc', a node created BEFORE others
    actually appears FIRST in the results — the oldest-first case the sleep
    skill's backfill needs and the tool could not previously express."""
    server = fresh_server()
    oldest_id = _add_observation(server, "Oldest node — created first, chronologically earliest.")
    conn = server.core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET created_at='2019-01-01T00:00:00Z' WHERE id=?", (oldest_id,)
        )
        conn.commit()
    finally:
        conn.close()
    _add_observation(server, "Middle node — created second.")
    _add_observation(server, "Newest node — created third, chronologically latest.")

    r = json.loads(server.engram_list(payload_json=json.dumps({
        "sort_by": "created", "sort_order": "asc", "limit": 1,
    })))
    assert r["status"] == "ok", f"Expected ok status, got: {r}"
    assert len(r["nodes"]) == 1, f"Expected exactly 1 node (limit=1), got: {r['nodes']}"
    assert r["nodes"][0]["id"] == oldest_id, (
        f"Expected the oldest node {oldest_id!r} to surface first under "
        f"sort_by=created, sort_order=asc — got {r['nodes'][0]['id']!r} instead"
    )
