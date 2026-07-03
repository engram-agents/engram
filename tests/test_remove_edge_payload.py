"""Tests for engram_remove_edge — non-cascade edge removal (issue #117).

Closes the substrate gap where over-applied `about` (and other relational)
edges previously required engram-surgical to correct. Companion to
test_link_about_payload.py.

Coverage:
  1. Happy path — well-formed payload removes an existing `about` edge.
  2. Idempotency — removing a non-existent edge returns no_op_not_found.
  3. Whitelist enforcement — cascade-bearing / structural / provenance
     relations are rejected (derives_from, supports, supersedes, retracts,
     contradicts, resolves, cites).
  4. Schema enforcement — invalid JSON, non-object, unknown fields.
  5. Required-field validation — missing source_id / target_id / relation.
  6. Node-existence checks — missing source or target node rejected.
  7. Audit-trail — edit_history records the edge_removed action.
  8. Internal helper — _remove_edge_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_remove_edge_")
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


def _seed_self_anchor_and_obs(server):
    """Create a self-anchor person + an observation; return their IDs."""
    p = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Test Self",
        "role": "agent",
        # description removed: no longer in typed person-node schema
        "is_self": True,
    })))
    person_id = p.get("person_id") or p.get("node_id")

    obs_payload = {
        "url": "https://example.com/a",
        "title": "A",
        "quoted_text": "X says Y is the case.",
        "interpretation": "X claims Y holds.",
        "claim": "Y is the case according to X.",
        "quote_type": "attributed_analysis",
    }
    o = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    obs_id = o["observation_id"]
    return person_id, obs_id


def _seed_about_edge(server):
    """Seed self-anchor + obs + an `about` edge. Returns (person_id, obs_id)."""
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_id,
        "person_id": person_id,
    })))
    assert "error" not in r, r
    return person_id, obs_id


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_remove_existing_about_edge():
    server = fresh_server()
    person_id, obs_id = _seed_about_edge(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" not in r, r
    assert r["status"] == "removed", r
    assert r["source_id"] == obs_id
    assert r["target_id"] == person_id
    assert r["relation"] == "about"


def test_remove_actually_deletes_edge():
    """Subsequent removal of same edge returns no_op_not_found."""
    server = fresh_server()
    person_id, obs_id = _seed_about_edge(server)
    payload = json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })
    r1 = json.loads(server.engram_remove_edge(payload_json=payload))
    assert r1["status"] == "removed"
    r2 = json.loads(server.engram_remove_edge(payload_json=payload))
    assert r2["status"] == "no_op_not_found"


# ── 2. Idempotency ─────────────────────────────────────────────────────────


def test_remove_nonexistent_edge_is_no_op():
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    # No about edge was created. Removal should succeed as no_op.
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" not in r, r
    assert r["status"] == "no_op_not_found", r


# ── 3. Whitelist enforcement ───────────────────────────────────────────────


def test_blocked_relations_rejected():
    """All cascade / structural / provenance relations must be rejected."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    blocked = ["derives_from", "supports", "supersedes", "retracts",
               "contradicts", "resolves", "cites"]
    for relation in blocked:
        r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
            "source_id": obs_id,
            "target_id": person_id,
            "relation": relation,
        })))
        assert "error" in r, f"Relation {relation} should be rejected: {r}"
        assert "not removable" in r["error"], r


def test_unknown_relation_rejected():
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "totally_fake_relation",
    })))
    assert "error" in r and "not removable" in r["error"], r


# ── 4. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": "ob_0001",
        "target_id": "pn_0001",
        "relation": "about",
        "bogus_field": "should be rejected",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 5. Required-field validation ───────────────────────────────────────────


def test_missing_source_id_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "target_id": "pn_0001", "relation": "about",
    })))
    assert "error" in r and "source_id" in r["error"], r


def test_missing_target_id_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": "ob_0001", "relation": "about",
    })))
    assert "error" in r and "target_id" in r["error"], r


def test_missing_relation_rejected():
    server = fresh_server()
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": "ob_0001", "target_id": "pn_0001",
    })))
    assert "error" in r and "relation" in r["error"], r


# ── 6. Node-existence checks ───────────────────────────────────────────────


def test_missing_source_node_rejected():
    server = fresh_server()
    person_id, _ = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": "ob_9999_nonexistent",
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" in r and "Source node" in r["error"], r


def test_missing_target_node_rejected():
    server = fresh_server()
    _, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": "pn_9999_nonexistent",
        "relation": "about",
    })))
    assert "error" in r and "Target node" in r["error"], r


# ── 7. Audit-trail ─────────────────────────────────────────────────────────


def test_edge_removal_logged_to_edit_history():
    server = fresh_server()
    person_id, obs_id = _seed_about_edge(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert r["status"] == "removed"

    # Check edit_history for the edge_removed entry.
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT action, node_id, node_type, details FROM edit_history "
            "WHERE action = 'edge_removed' AND node_id = ?",
            (obs_id,),
        ).fetchone()
        assert row is not None, "edge_removed entry should be in edit_history"
        details = json.loads(row["details"])
        assert details["target_id"] == person_id
        assert details["target_type"] == "person"
        assert details["relation"] == "about"
    finally:
        conn.close()


# ── 8. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    person_id, obs_id = _seed_about_edge(server)
    r = json.loads(server._remove_edge_impl(
        source_id=obs_id, target_id=person_id, relation="about",
    ))
    assert "error" not in r, r
    assert r["status"] == "removed"


# ── 9. `exemplifies` side-effects (fairy round-1 PR #126 blockers) ─────────
#
# `exemplifies` is the only whitelist relation with filesystem side-effects:
# it appends to error_incidents.json (rebuilt on add/remove). Removing such an
# edge must rebuild the cache or the lesson's scaffolding tripwire keeps firing
# for the removed incident. The metadata counter (incident_count) was retired
# in favour of a live-count SSoT helper (closes #442).


def _seed_lesson_with_two_incidents(server, error_incidents_tmp_path):
    """Create two incidents and a lesson, both incidents linked via exemplifies.
    Returns (lesson_id, incident_a_id, incident_b_id).

    error_incidents.json is freshly rebuilt to reflect both incidents on disk.
    """
    # Redirect the cache file to a test path so we don't pollute ~/.engram/.
    server.core.ERROR_INCIDENTS_PATH = str(error_incidents_tmp_path)

    inc_a = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "Skipped reading stderr.",
        "interpretation": "Hypothesized cause before reading the trace.",
        "claim": "Skipped error-output before forming a hypothesis (incident A).",
        "quote_type": "personal_communication",
        "url": "https://example.com/inc-a",
        "title": "Incident A",
    })))["observation_id"]

    inc_b = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "Guessed at root cause.",
        "interpretation": "Same skip-output pattern, different occasion.",
        "claim": "Skipped error-output before forming a hypothesis (incident B).",
        "quote_type": "personal_communication",
        "url": "https://example.com/inc-b",
        "title": "Incident B",
    })))["observation_id"]

    lesson_id = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": f"{inc_a},{inc_b}",
        "scaffolding_nudge": "Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Two incidents → action-focused tripwire pattern.",
    })))["lesson_id"]

    # Rebuild the cache so error_incidents.json is on the redirected path.
    server._rebuild_incidents_cache()
    return lesson_id, inc_a, inc_b


def test_remove_exemplifies_happy_path(tmp_path):
    """Removing an exemplifies edge succeeds and returns cache_rebuild info."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, inc_a, inc_b = _seed_lesson_with_two_incidents(server, cache_path)

    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": inc_a,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))
    assert "error" not in r, r
    assert r["status"] == "removed"
    # Cache-rebuild is reported in the response for the exemplifies case.
    assert "cache_rebuild" in r, r
    assert r["cache_rebuild"]["status"] != "error", r["cache_rebuild"]


def test_remove_exemplifies_decrements_live_count(tmp_path):
    """Live exemplar count goes from 2 → 1 after edge removal (SSoT helper, closes #442)."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, inc_a, _ = _seed_lesson_with_two_incidents(server, cache_path)

    # Verify starting state — 2 live exemplars.
    conn = server._get_db()
    try:
        assert server._count_live_exemplars(conn, lesson_id, "lesson") == 2
    finally:
        conn.close()

    json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": inc_a,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))

    conn = server._get_db()
    try:
        assert server._count_live_exemplars(conn, lesson_id, "lesson") == 1
    finally:
        conn.close()


def test_remove_exemplifies_rebuilds_error_incidents_cache(tmp_path):
    """error_incidents.json no longer contains the removed incident on disk."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, inc_a, inc_b = _seed_lesson_with_two_incidents(server, cache_path)

    # Verify pre-state — both incidents present in cache (flat dict keyed by incident_id).
    with open(cache_path) as f:
        cache_before = json.load(f)
    assert inc_a in cache_before, cache_before
    assert inc_b in cache_before, cache_before

    json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": inc_a,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))

    # Cache should be rebuilt without inc_a, keeping inc_b.
    with open(cache_path) as f:
        cache_after = json.load(f)
    assert inc_a not in cache_after, cache_after
    assert inc_b in cache_after, cache_after


def test_remove_exemplifies_live_count_after_all_removed(tmp_path):
    """Live exemplar count reaches 0 after all exemplifies edges are removed (no floor needed)."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, inc_a, inc_b = _seed_lesson_with_two_incidents(server, cache_path)

    # Remove both edges — live count should reach exactly 0.
    json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": inc_a,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))
    json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": inc_b,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))

    conn = server._get_db()
    try:
        assert server._count_live_exemplars(conn, lesson_id, "lesson") == 0
    finally:
        conn.close()


def test_non_exemplifies_does_not_trigger_cache_rebuild(tmp_path):
    """Removing an `about` edge should NOT include cache_rebuild in response."""
    server = fresh_server()
    person_id, obs_id = _seed_about_edge(server)
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert r["status"] == "removed"
    assert "cache_rebuild" not in r, "cache_rebuild should only fire for exemplifies"
