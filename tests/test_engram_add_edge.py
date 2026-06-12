"""Tests for engram_add_edge — non-cascade after-creation edge addition (sub-issue #529).

After-creation add-edge complement to engram_remove_edge. Whitelist-scoped to
non-cascade, non-structural-commitment, non-provenance relations
(about, exemplifies, serves, subtask_of, tensions).

Coverage:
  1. Happy path — add `about` edge, returns {"status": "created", ...}, audit-logged.
  2. Idempotency — adding the same edge twice returns no_op_already_exists, no
     duplicate edge in DB.
  3. Whitelist enforcement — `cites` blocked (returns "not addable" error).
  4. Whitelist enforcement — `derives_from` blocked (cascade-bearing).
  5. Whitelist enforcement — `supersedes` blocked (cascade-bearing).
  6. Source-node-missing — nonexistent source_id returns error.
  7. Target-node-missing — nonexistent target_id returns error.
  8. Payload validation — unknown field returns error listing allowed fields.
  9. Payload validation — empty relation returns error.
  10. Audit log — edit_history row with kind='edge_added', target_id + relation
      in metadata JSON.
  11. DAG guard fires for `subtask_of` when source is older than target (#1076 retarget).
  11b. Positive regression — `serves` allows source older than target (#1076 exemption).
  12. Happy path — add `serves` edge with source newer than target succeeds.
  13. is_current guard — retracted source_id rejected (#1017).
  14. is_current guard — retracted target_id rejected (#1017).
  15. is_current guard — superseded source_id rejected (#1017).
  16. is_current guard — superseded target_id rejected (#1017).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_add_edge_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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


def _seed_self_anchor_and_obs(server):
    """Create a self-anchor person + an observation; return their IDs."""
    p = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Test Self",
        "role": "agent",
        "description": "self-anchor for add-edge tests",
        "is_self": True,
    })))
    person_id = p.get("person_id") or p.get("node_id")

    obs_payload = {
        "url": "https://example.com/add-edge-test",
        "title": "Add Edge Test Obs",
        "quoted_text": "A says B is the case.",
        "interpretation": "A claims B holds.",
        "claim": "B is the case according to A.",
        "quote_type": "attributed_analysis",
    }
    o = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    obs_id = o["observation_id"]
    return person_id, obs_id


# ── 1. Happy path: add `about` edge ───────────────────────────────────────


def test_add_about_edge_created():
    """Adding an `about` edge to an existing node returns status='created'."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert r["source_id"] == obs_id
    assert r["target_id"] == person_id
    assert r["relation"] == "about"
    assert "created_at" in r


def test_add_about_edge_persisted_in_db():
    """Edge is actually present in the edges table after a successful add."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert r["status"] == "created"

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (obs_id, person_id, "about"),
        ).fetchone()
        assert row is not None, "Edge should be present in the edges table"
    finally:
        conn.close()


# ── 2. Idempotency: add same edge twice ────────────────────────────────────


def test_add_edge_idempotent_second_call():
    """Second call with same edge returns no_op_already_exists."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    payload = json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })
    r1 = json.loads(server.engram_add_edge(payload_json=payload))
    assert r1["status"] == "created"
    r2 = json.loads(server.engram_add_edge(payload_json=payload))
    assert "error" not in r2, r2
    assert r2["status"] == "no_op_already_exists", r2


def test_add_edge_idempotent_no_duplicate_in_db():
    """Two calls with same edge produce exactly one row in the edges table."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    payload = json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })
    server.engram_add_edge(payload_json=payload)
    server.engram_add_edge(payload_json=payload)

    conn = server._get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (obs_id, person_id, "about"),
        ).fetchone()["c"]
        assert count == 1, f"Expected 1 edge, found {count}"
    finally:
        conn.close()


# ── 3. Whitelist enforcement: `cites` blocked ──────────────────────────────


def test_add_cites_edge_rejected():
    """Relation 'cites' is not addable via engram_add_edge."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "cites",
    })))
    assert "error" in r, r
    assert "not addable via engram_add_edge" in r["error"], r


# ── 4. Whitelist enforcement: `derives_from` blocked (cascade-bearing) ─────


def test_add_derives_from_edge_rejected():
    """Relation 'derives_from' is cascade-bearing and must be blocked."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "derives_from",
    })))
    assert "error" in r, r
    assert "not addable via engram_add_edge" in r["error"], r


# ── 5. Whitelist enforcement: `supersedes` blocked (cascade-bearing) ────────


def test_add_supersedes_edge_rejected():
    """Relation 'supersedes' is cascade-bearing and must be blocked."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "supersedes",
    })))
    assert "error" in r, r
    assert "not addable via engram_add_edge" in r["error"], r


# ── 6. Source-node-missing ─────────────────────────────────────────────────


def test_missing_source_node_rejected():
    """Nonexistent source_id returns an error referencing 'Source node'."""
    server = fresh_server()
    person_id, _ = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": "ob_9999_nonexistent",
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" in r and "Source node" in r["error"], r


# ── 7. Target-node-missing ─────────────────────────────────────────────────


def test_missing_target_node_rejected():
    """Nonexistent target_id returns an error referencing 'Target node'."""
    server = fresh_server()
    _, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": "pn_9999_nonexistent",
        "relation": "about",
    })))
    assert "error" in r and "Target node" in r["error"], r


# ── 8. Payload validation: unknown field ───────────────────────────────────


def test_unknown_field_rejected():
    """Extra field in payload_json returns error listing allowed fields."""
    server = fresh_server()
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": "ob_0001",
        "target_id": "pn_0001",
        "relation": "about",
        "bogus_field": "should be rejected",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 9. Payload validation: missing / empty relation ────────────────────────


def test_missing_relation_rejected():
    """Payload without 'relation' key returns an error referencing 'relation'."""
    server = fresh_server()
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": "ob_0001",
        "target_id": "pn_0001",
    })))
    assert "error" in r and "relation" in r["error"], r


def test_empty_relation_rejected():
    """Payload with empty 'relation' string returns an error."""
    server = fresh_server()
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": "ob_0001",
        "target_id": "pn_0001",
        "relation": "",
    })))
    assert "error" in r and "relation" in r["error"], r


def test_invalid_json_rejected():
    """Malformed JSON in payload_json returns an 'Invalid JSON' error."""
    server = fresh_server()
    r = json.loads(server.engram_add_edge(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_non_object_payload_rejected():
    """Non-dict payload_json (array) returns a 'must be a JSON object' error."""
    server = fresh_server()
    r = json.loads(server.engram_add_edge(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


# ── 10. Audit log written ──────────────────────────────────────────────────


def test_edge_add_logged_to_edit_history():
    """Successful add writes an 'edge_added' row to edit_history."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert r["status"] == "created"

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT action, node_id, node_type, details FROM edit_history "
            "WHERE action = 'edge_added' AND node_id = ?",
            (obs_id,),
        ).fetchone()
        assert row is not None, "edge_added entry should be in edit_history"
        details = json.loads(row["details"])
        assert details["target_id"] == person_id, details
        assert details["relation"] == "about", details
    finally:
        conn.close()


def test_edge_add_log_includes_target_type():
    """edit_history row for edge_added includes target_type in details JSON."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT details FROM edit_history WHERE action = 'edge_added' AND node_id = ?",
            (obs_id,),
        ).fetchone()
        assert row is not None
        details = json.loads(row["details"])
        assert "target_type" in details, details
        assert details["target_type"] == "person", details
    finally:
        conn.close()


# ── Additional: internal helper callable with kwargs ───────────────────────


def test_impl_callable_with_kwargs():
    """_add_edge_impl is callable with named kwargs (for in-server callers)."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server._add_edge_impl(
        source_id=obs_id, target_id=person_id, relation="about",
    ))
    assert "error" not in r, r
    assert r["status"] == "created"


# ── exemplifies side-effect: cache rebuild (mirrors remove-side coverage) ──
#
# `exemplifies` is the only whitelist relation with filesystem side-effects:
# error_incidents.json is the hot-path cache the surface-hook scans every
# prompt for lesson tripwires. engram_remove_edge rebuilds the cache on
# removal of an exemplifies edge; engram_add_edge must do the same on
# addition or a newly-linked incident won't appear in the cache until the
# next engram_register_exemplar call or manual rebuild. Mirrors the
# tests/test_remove_edge_payload.py exemplifies block.


def _seed_lesson_and_dangling_incident(server, error_incidents_tmp_path):
    """Create one incident already linked to a lesson, plus a second dangling
    incident not yet linked. Returns (lesson_id, linked_incident_id,
    dangling_incident_id). error_incidents.json rebuilt to reflect linked-only
    state on disk."""
    server.core.ERROR_INCIDENTS_PATH = str(error_incidents_tmp_path)

    linked = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "Skipped reading stderr.",
        "interpretation": "Hypothesized cause before reading the trace.",
        "claim": "Skipped error-output before forming a hypothesis (incident A).",
        "quote_type": "personal_communication",
        "url": "https://example.com/add-inc-a",
        "title": "Incident A (linked)",
    })))["observation_id"]

    dangling = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "Guessed at root cause.",
        "interpretation": "Same skip-output pattern, different occasion.",
        "claim": "Skipped error-output before forming a hypothesis (incident B).",
        "quote_type": "personal_communication",
        "url": "https://example.com/add-inc-b",
        "title": "Incident B (dangling — to be linked via engram_add_edge)",
    })))["observation_id"]

    lesson_id = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": linked,
        "scaffolding_nudge": "Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "One incident → action-focused tripwire pattern.",
    })))["lesson_id"]

    server._rebuild_incidents_cache()
    return lesson_id, linked, dangling


def test_add_exemplifies_returns_cache_rebuild(tmp_path):
    """Adding an exemplifies edge returns cache_rebuild info in the response."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, _linked, dangling = _seed_lesson_and_dangling_incident(server, cache_path)

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": dangling,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert "cache_rebuild" in r, r
    assert r["cache_rebuild"]["status"] != "error", r["cache_rebuild"]


def test_add_exemplifies_rebuilds_error_incidents_cache(tmp_path):
    """error_incidents.json gains the newly-linked incident on disk after add."""
    server = fresh_server()
    cache_path = tmp_path / "error_incidents.json"
    lesson_id, linked, dangling = _seed_lesson_and_dangling_incident(server, cache_path)

    with open(cache_path) as f:
        cache_before = json.load(f)
    assert linked in cache_before, cache_before
    assert dangling not in cache_before, "dangling incident should not be in cache before edge add"

    json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": dangling,
        "target_id": lesson_id,
        "relation": "exemplifies",
    })))

    with open(cache_path) as f:
        cache_after = json.load(f)
    assert dangling in cache_after, "newly-linked incident must appear in cache after add"
    assert linked in cache_after, "originally-linked incident should remain in cache"


def test_add_non_exemplifies_omits_cache_rebuild():
    """Adding a non-exemplifies edge (e.g., `about`) does NOT include cache_rebuild
    in the response — the cache-rebuild side-effect is exemplifies-only."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert "cache_rebuild" not in r, (
        "cache_rebuild key should only appear in exemplifies response"
    )


# ── 11. DAG guard fires for `subtask_of` (dag_check=True) ─────────────────


def test_dag_guard_fires_for_subtask_of_when_source_older():
    """DAG guard blocks `subtask_of` when source.created_at < target.created_at.

    `subtask_of` has dag_check=True + addable_after_creation=True — so the
    guard fires today, not only for hypothetical future relations.

    NOTE: `serves` was originally tested here but gained a dag_check=False
    exemption in #1076 (a goal can be articulated after the work serving it).
    `subtask_of` remains dag_check=True and is now the canonical test case.
    """
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)

    # Create a second observation to serve as the 'target' in our test edge.
    obs2_payload = {
        "url": "https://example.com/add-edge-dag-target",
        "title": "DAG Target Obs",
        "quoted_text": "Target claim text.",
        "interpretation": "Target interpretation.",
        "claim": "Target claim.",
        "quote_type": "attributed_analysis",
    }
    obs2 = json.loads(server.engram_add_observation(payload_json=json.dumps(obs2_payload)))
    obs2_id = obs2["observation_id"]

    # Patch obs_id (source) to be OLDER than obs2_id (target) — subtask_of is NOT dag-exempt, so the guard must fire.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (obs_id,),
        )
        conn.execute(
            "UPDATE nodes SET created_at = '2026-06-01T00:00:00+00:00' WHERE id = ?",
            (obs2_id,),
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": obs2_id,
        "relation": "subtask_of",
    })))
    assert "error" in r, r
    assert "DAG" in r["error"] or "invariant" in r["error"], r


# ── 11b. Positive regression: `serves` allows source older than target (#1076) ──


def test_serves_allows_source_older_than_target():
    """serves.source can legitimately pre-date serves.target — #1076 exemption.

    A goal can be articulated after the work that serves it, so `serves` has
    dag_check=False (exempted in #1076, three-tier edge taxonomy). This test
    guards that exemption against future regression: back-dating the source
    older than the target must NOT produce a DAG error.
    """
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)

    obs2_payload = {
        "url": "https://example.com/add-edge-serves-older-source",
        "title": "Serves Older Source Obs",
        "quoted_text": "Source claim text — older than target.",
        "interpretation": "Source interpretation.",
        "claim": "Source claim.",
        "quote_type": "attributed_analysis",
    }
    obs2 = json.loads(server.engram_add_observation(payload_json=json.dumps(obs2_payload)))
    obs2_id = obs2["observation_id"]

    # Back-date obs_id (source) so it is older than obs2_id (target).
    # For dag_check=False relations this must succeed, not raise a DAG error.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (obs_id,),
        )
        conn.execute(
            "UPDATE nodes SET created_at = '2026-06-01T00:00:00+00:00' WHERE id = ?",
            (obs2_id,),
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": obs2_id,
        "relation": "serves",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert r["relation"] == "serves"


# ── 12. Happy path for `serves` (dag_check=False, any timestamp order) ──


def test_add_serves_edge_correct_order_succeeds():
    """Adding a `serves` edge returns status='created' regardless of timestamp order.

    Confirms the dag_check=False path works end-to-end for `serves`.
    Source newer than target also works (just not the only valid ordering).
    """
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)

    # Create a second observation to be the target (older).
    obs2_payload = {
        "url": "https://example.com/add-edge-serves-target",
        "title": "Serves Target Obs",
        "quoted_text": "Older target claim text.",
        "interpretation": "Older target interpretation.",
        "claim": "Older target claim.",
        "quote_type": "attributed_analysis",
    }
    obs2 = json.loads(server.engram_add_observation(payload_json=json.dumps(obs2_payload)))
    obs2_id = obs2["observation_id"]

    # Patch: obs2 (target) is older, obs_id (source) is newer.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (obs2_id,),
        )
        conn.execute(
            "UPDATE nodes SET created_at = '2026-06-01T00:00:00+00:00' WHERE id = ?",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": obs2_id,
        "relation": "serves",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert r["relation"] == "serves"


# ── 13-14. is_current guard — retracted source/target (#1017) ─────────────


def _retract_node(server, node_id):
    """Retract a node so it becomes is_current=0."""
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": node_id,
        "error_type": "hallucinated_claim",
        "reason": "Test retraction — is_current guard test.",
    })))
    assert "error" not in r, f"retract failed: {r}"


def _supersede_node(server, old_id, new_id):
    """Supersede old_id with new_id so old_id becomes is_current=0."""
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Test supersede — is_current guard test.",
    })))
    assert "error" not in r, f"supersede failed: {r}"


def test_retracted_source_rejected_by_is_current_guard():
    """_add_edge_impl rejects a retracted source_id (#1017)."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    _retract_node(server, obs_id)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" in r, r
    assert "not current" in r["error"], r


def test_retracted_target_rejected_by_is_current_guard():
    """_add_edge_impl rejects a retracted target_id (#1017)."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    # Create a second observation to use as source (person can't be retracted easily)
    obs2_id = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/b", "title": "B",
        "quoted_text": "B says C.", "interpretation": "B claims C.", "claim": "C.",
        "quote_type": "attributed_analysis",
    })))["observation_id"]
    _retract_node(server, obs_id)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs2_id,
        "target_id": obs_id,
        "relation": "about",
    })))
    assert "error" in r, r
    assert "not current" in r["error"], r


def test_superseded_source_rejected_by_is_current_guard():
    """_add_edge_impl rejects a superseded source_id (#1017)."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    obs2_id = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/b", "title": "B",
        "quoted_text": "B says C.", "interpretation": "B claims C.", "claim": "C.",
        "quote_type": "attributed_analysis",
    })))["observation_id"]
    _supersede_node(server, obs_id, obs2_id)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": person_id,
        "relation": "about",
    })))
    assert "error" in r, r
    assert "not current" in r["error"], r


def test_superseded_target_rejected_by_is_current_guard():
    """_add_edge_impl rejects a superseded target_id (#1017)."""
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    obs2_id = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/b", "title": "B",
        "quoted_text": "B says C.", "interpretation": "B claims C.", "claim": "C.",
        "quote_type": "attributed_analysis",
    })))["observation_id"]
    obs3_id = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/c", "title": "C",
        "quoted_text": "C says D.", "interpretation": "C claims D.", "claim": "D.",
        "quote_type": "attributed_analysis",
    })))["observation_id"]
    _supersede_node(server, obs_id, obs2_id)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs3_id,
        "target_id": obs_id,
        "relation": "about",
    })))
    assert "error" in r, r
    assert "not current" in r["error"], r
