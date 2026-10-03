"""Tests for the _similar_existing_matches helper (#143 §3.1 refactor).

The helper was extracted from inline blocks in engram_add_observation and
engram_add_lesson. Two divergences existed before the extraction:

  - Observation FTS WHERE filtered by `COALESCE(n.importance_score, 0) >= ?`
  - Lesson FTS WHERE did NOT filter by importance (only the semantic
    path did, via the importance_threshold arg to _semantic_search)

Per 2026-05-14 audit Q2 ("that's a drift, let's unify it, filter both
in FTS"), the helper applies the tier-2 importance floor uniformly to FTS
for both type filters.

These tests lock in the unified behavior and verify the helper pulls
extra metadata keys correctly per caller.
"""

import atexit
import json
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_similar_existing_helper_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"  # FTS-only path for deterministic tests
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _seed_lesson(server, claim: str, scaffolding_nudge: str = "test nudge") -> str:
    """Create an incident observation + lesson citing it. Returns lesson_id."""
    obs_payload = {
        "quoted_text": "incident statement",
        "interpretation": "incident interpretation",
        "claim": "incident: " + claim[:40],
        "quote_type": "personal_communication",
        "url": "https://example.com/incident",
        "title": "Test incident",
    }
    obs_r = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    assert "error" not in obs_r, obs_r
    lesson_payload = {
        "claim": claim,
        "incident_ids": obs_r["observation_id"],
        "scaffolding_nudge": scaffolding_nudge,
        "logical_chain": "Single-incident test pattern.",
    }
    lesson_r = json.loads(server.engram_add_lesson(payload_json=json.dumps(lesson_payload)))
    assert "error" not in lesson_r, lesson_r
    return lesson_r["lesson_id"]


def test_helper_fts_applies_importance_floor_for_lessons():
    """#143 §3.1 drift cleanup regression-lock.

    Pre-cleanup: engram_add_lesson's inline FTS WHERE clause didn't
    filter by importance_score (only the semantic path did via
    _semantic_search's importance_threshold). A new lesson sharing
    keywords with a low-importance existing lesson would surface the
    existing one as a similar_existing nudge even when other low-
    importance noise should have been suppressed at tier-2.

    After cleanup: the helper applies the tier-2 importance floor in
    BOTH the FTS WHERE clause AND the semantic path's importance_threshold,
    uniformly for {"observation_factual", "observation_predictive"} and
    {"lesson"} type filters.
    """
    server = fresh_server()
    lesson_id = _seed_lesson(server, "Read error output before hypothesizing root cause.")

    # Force the seeded lesson's importance_score below our test threshold
    conn = server._get_db()
    conn.execute("UPDATE nodes SET importance_score = 0.1 WHERE id = ?", (lesson_id,))
    conn.commit()

    # Monkey-patch tier-2 threshold to 0.5 — above the seeded lesson's 0.1
    # Target: server.core._get_tier_threshold — _similar_existing_matches was
    # promoted to engram_core in #872 wave 4, so it resolves _get_tier_threshold
    # from core's namespace, not server's.
    original_get_tier = server.core._get_tier_threshold
    server.core._get_tier_threshold = (
        lambda c, t: 0.5 if t == 2 else original_get_tier(c, t)
    )
    try:
        matches = server._similar_existing_matches(
            conn, "read error output before",
            type_filter={"lesson"},
            extra_metadata_keys=("scaffolding_nudge",),
        )
        # Semantic disabled via ENGRAM_NO_EMBEDDINGS, so only the FTS path
        # contributes. The seeded lesson must be filtered out by the new
        # importance-floor WHERE clause; otherwise the drift is still present.
        assert all(m["id"] != lesson_id for m in matches), (
            f"#143 §3.1 drift regression: lesson {lesson_id} "
            f"(importance 0.1) should have been filtered out by the "
            f"FTS importance floor 0.5, but appeared in matches: {matches}"
        )
    finally:
        server.core._get_tier_threshold = original_get_tier


def test_helper_pulls_extra_metadata_keys_for_lessons():
    """Lesson caller passes extra_metadata_keys=("scaffolding_nudge",);
    the helper pulls the key from each match's metadata JSON blob and
    includes it in the returned record. Verifies the metadata-pull path
    on the FTS branch (semantic disabled via env var).
    """
    server = fresh_server()
    lesson_id = _seed_lesson(
        server,
        "Always read error output before hypothesizing root cause.",
        scaffolding_nudge="Stop. Read the actual error output first.",
    )
    conn = server._get_db()
    # FTS5 with the codebase's sanitizer wraps each token as a phrase
    # match, so the search query must use tokens that EXACTLY appear in
    # the seeded lesson's claim (no stemming). "hypothesizing" stems
    # nowhere — keep the search vocabulary tight to the seeded text.
    matches = server._similar_existing_matches(
        conn, "read error output before",
        type_filter={"lesson"},
        extra_metadata_keys=("scaffolding_nudge",),
    )
    seeded = [m for m in matches if m["id"] == lesson_id]
    assert seeded, f"Expected seeded lesson {lesson_id} in helper output: {matches}"
    assert seeded[0]["scaffolding_nudge"] == "Stop. Read the actual error output first.", (
        f"scaffolding_nudge not pulled correctly: {seeded[0]}"
    )
    # Standard record shape preserved
    assert seeded[0]["match_type"] == "keyword"
    assert "claim" in seeded[0]
    assert "confidence" in seeded[0]


def test_helper_empty_type_filter_returns_empty():
    """Defensive: empty type_filter is a degenerate input. The helper
    short-circuits to an empty list rather than running an FTS query
    with an empty IN clause (which would either error or return all
    rows, depending on SQLite's mood).
    """
    server = fresh_server()
    conn = server._get_db()
    matches = server._similar_existing_matches(
        conn, "anything",
        type_filter=set(),
    )
    assert matches == []


def test_helper_exception_logs_warning_and_returns_empty(caplog):
    """#307 regression: an injected failure in _similar_existing_matches
    must log a warning (not swallow silently) and still return [].

    Guards the fix in server.py: `except Exception as e:
    logging.warning(...)` replacing the bare `except Exception: return []`.
    """
    server = fresh_server()
    conn = server._get_db()

    def _boom(conn, tier):
        raise RuntimeError("injected failure for test")

    # _similar_existing_matches now lives in engram_core — patch core namespace.
    original = server.core._get_tier_threshold
    server.core._get_tier_threshold = _boom
    try:
        with caplog.at_level(logging.WARNING, logger="root"):
            matches = server._similar_existing_matches(
                conn, "any query",
                type_filter={"observation_factual"},
            )
    finally:
        server.core._get_tier_threshold = original

    assert matches == [], f"Expected [] on failure, got: {matches}"
    assert any(
        "_similar_existing_matches failed" in r.message
        for r in caplog.records
    ), f"Expected warning log, got records: {[r.message for r in caplog.records]}"


def test_helper_semantic_complement_ignores_enabled_false():
    """(#345 PR2, A2) The dedup semantic complement (engram_core.py ~8072,
    inside _similar_existing_matches) no longer consults the retired
    `enabled` key: with `enabled: False` mocked, the semantic complement
    must still run (is_available()=True) and contribute a match that FTS
    alone would never find (a synonym with zero shared FTS tokens).

    Mutation target: restoring the
    `_embedder.is_available() and _get_embedding_config().get("enabled", True)`
    check here turns this RED (the semantic-only match disappears).
    """
    server = fresh_server()
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)  # this test wants the semantic branch live
    conn = server._get_db()

    class _StubEmbedder:
        def is_available(self):
            return True

    original_embedder = server.core._embedder
    original_get_config = server.core._get_embedding_config

    def _stub_semantic_search(conn, claim, top_k=10, min_similarity=0.3,
                               type_filter=None, importance_threshold=0.0,
                               include_superseded=False):
        return [{
            "id": "sem_only_345", "claim": "semantic-only synonym match",
            "confidence": 0.8, "similarity": 0.9,
        }]

    server.core._embedder = _StubEmbedder()
    server.core._get_embedding_config = lambda: {"enabled": False, "model": "x"}
    original_semantic_search = server.core._semantic_search
    server.core._semantic_search = _stub_semantic_search
    try:
        matches = server._similar_existing_matches(
            conn, "anything unrelated to the fts tokens",
            type_filter={"observation_factual"},
        )
    finally:
        server.core._embedder = original_embedder
        server.core._get_embedding_config = original_get_config
        server.core._semantic_search = original_semantic_search

    ids = {m["id"] for m in matches}
    assert "sem_only_345" in ids, (
        f"expected the semantic-only match despite enabled=False, got: {matches}"
    )
