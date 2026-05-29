"""Tests for the SSoT live-exemplar count helper _count_live_exemplars.

Closes issue #442 — replaces the cached exemplar_count/incident_count metadata
field with a live COUNT query that always reflects graph state.

Coverage:
  §1 Lesson with N exemplifies-incoming edges (all is_current=1) → returns N.
  §2 Lesson with mixed is_current edges → returns count of is_current=1 only.
  §3 Cornerstone with M exemplifies + K supported_by → returns M+K.
  §4 Cornerstone with only supported_by (pre-#431 pattern) → returns K.
  §5 Edge cases: target_id doesn't exist → 0. Empty graph → 0.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_count_live_exemplars")


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


def _seed_axiom(server, claim, basis="test basis"):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": basis,
    })))
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_obs(server, claim, url="https://example.com/obs"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": f"Evidence: {claim}",
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": url,
        "title": f"Obs: {claim[:30]}",
    })))
    assert "error" not in r, r
    return r["observation_id"]


def _seed_lesson(server, claim, incident_ids):
    if isinstance(incident_ids, (list, tuple)):
        incident_ids = ",".join(incident_ids)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": claim,
        "incident_ids": incident_ids,
        "logical_chain": f"Pattern from incidents.",
        "scaffolding_nudge": "Watch for this.",
    })))
    assert "error" not in r, r
    return r["lesson_id"]


def _seed_cornerstone(server, supporting_ids, tag="test-cs"):
    if isinstance(supporting_ids, (list, tuple)):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": tag,
        "title": f"Cornerstone {tag}",
        "new_frame": f"Principle for {tag}.",
        "supporting_ids": supporting_ids,
    })))
    assert "error" not in r, r
    return r["cornerstone_id"]


# ---------------------------------------------------------------------------
# §1 Lesson with N exemplifies edges (all is_current=1)
# ---------------------------------------------------------------------------


def test_lesson_n_live_exemplars():
    """Lesson with 3 live exemplifies sources → count == 3."""
    server = fresh_server()
    inc_a = _seed_axiom(server, "Incident A.")
    inc_b = _seed_axiom(server, "Incident B.")
    inc_c = _seed_axiom(server, "Incident C.")
    ls_id = _seed_lesson(server, "Three-incident lesson.", [inc_a, inc_b, inc_c])

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()

    assert count == 3, f"Expected 3, got {count}"


def test_lesson_single_exemplar():
    """Lesson with exactly 1 live exemplifies source → count == 1."""
    server = fresh_server()
    inc = _seed_axiom(server, "Single incident.")
    ls_id = _seed_lesson(server, "Single-incident lesson.", [inc])

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()

    assert count == 1, f"Expected 1, got {count}"


# ---------------------------------------------------------------------------
# §2 Lesson with mixed is_current edges (some source nodes superseded)
# ---------------------------------------------------------------------------


def test_lesson_mixed_is_current():
    """Only is_current=1 source nodes are counted — superseded sources excluded."""
    server = fresh_server()
    inc_live = _seed_axiom(server, "Live incident.")
    inc_to_supersede = _seed_axiom(server, "Incident that will be superseded.")
    ls_id = _seed_lesson(server, "Mixed-is_current lesson.", [inc_live, inc_to_supersede])

    # Supersede the second incident — sets is_current=0 on it.
    inc_replacement = _seed_axiom(server, "Replacement for superseded incident.")
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": inc_to_supersede,
        "new_node_id": inc_replacement,
        "supersede_reason": "Corrected understanding.",
    })))
    assert "error" not in r, r

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()

    # inc_to_supersede is now is_current=0, so only inc_live counts.
    assert count == 1, f"Expected 1 (only live source), got {count}"


# ---------------------------------------------------------------------------
# §3 Cornerstone with M exemplifies + K supported_by
# ---------------------------------------------------------------------------


def test_cornerstone_exemplifies_and_supported_by():
    """Cornerstone with M exemplifies + K supported_by → count == M+K."""
    server = fresh_server()
    sup_a = _seed_axiom(server, "Supported-by premise A.")
    sup_b = _seed_axiom(server, "Supported-by premise B.")
    cs_id = _seed_cornerstone(server, [sup_a, sup_b], tag="mixed-support-cs")

    # Register additional exemplifies edges (M=2).
    ex_a = _seed_axiom(server, "Exemplifies instance A.")
    ex_b = _seed_axiom(server, "Exemplifies instance B.")
    for ex in (ex_a, ex_b):
        r = json.loads(server.engram_register_exemplar(payload_json=json.dumps({
            "target_id": cs_id,
            "exemplar_id": ex,
        })))
        assert "error" not in r, r

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, cs_id, "cornerstone")
    finally:
        conn.close()

    # K=2 supported_by (sup_a, sup_b) + M=2 exemplifies (ex_a, ex_b) = 4.
    assert count == 4, f"Expected 4 (2 supported_by + 2 exemplifies), got {count}"


# ---------------------------------------------------------------------------
# §4 Cornerstone with only supported_by (pre-#431 pattern)
# ---------------------------------------------------------------------------


def test_cornerstone_only_supported_by():
    """Cornerstone with only supported_by edges (no exemplifies) → count == K."""
    server = fresh_server()
    sup_a = _seed_axiom(server, "Only-supported-by premise A.")
    sup_b = _seed_axiom(server, "Only-supported-by premise B.")
    sup_c = _seed_axiom(server, "Only-supported-by premise C.")
    cs_id = _seed_cornerstone(server, [sup_a, sup_b, sup_c], tag="only-sup-by-cs")

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, cs_id, "cornerstone")
    finally:
        conn.close()

    assert count == 3, f"Expected 3 (all supported_by), got {count}"


# ---------------------------------------------------------------------------
# §5 Edge cases
# ---------------------------------------------------------------------------


def test_target_not_exist_returns_zero():
    """Non-existent target_id → count == 0 (no matching edges)."""
    server = fresh_server()
    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, "ls_99999", "lesson")
    finally:
        conn.close()
    assert count == 0, f"Expected 0 for non-existent target, got {count}"


def test_empty_graph_returns_zero():
    """Empty graph (no edges) → count == 0 for any plausible target_id."""
    server = fresh_server()
    conn = server._get_db()
    try:
        # Both lesson and cornerstone types.
        assert server._count_live_exemplars(conn, "ls_0001", "lesson") == 0
        assert server._count_live_exemplars(conn, "cs_0001", "cornerstone") == 0
    finally:
        conn.close()


def test_cornerstone_supported_by_is_current_filter():
    """supported_by targets that are is_current=0 are excluded."""
    server = fresh_server()
    sup_live = _seed_axiom(server, "Live supported-by premise.")
    sup_to_supersede = _seed_axiom(server, "Supported-by premise to be superseded.")
    cs_id = _seed_cornerstone(server, [sup_live, sup_to_supersede], tag="filtered-sup-cs")

    # Supersede the second supported-by premise.
    sup_replacement = _seed_axiom(server, "Replacement premise.")
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": sup_to_supersede,
        "new_node_id": sup_replacement,
        "supersede_reason": "Corrected.",
    })))
    assert "error" not in r, r

    conn = server._get_db()
    try:
        count = server._count_live_exemplars(conn, cs_id, "cornerstone")
    finally:
        conn.close()

    # Only sup_live remains is_current=1 as a supported_by target.
    assert count == 1, f"Expected 1 (only live supported_by target), got {count}"
