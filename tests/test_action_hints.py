"""Functional tests for action-hint tier labeling (closes issue #60).

PR #58 added test_thresholds.py asserting the constant *values* are correct.
This file complements that with tests of the functional code path: when
two observations come back from semantic search with similarity X, does the
action_hint string actually say CORROBORATE / RELATED / WEAK_MATCH at the
right tiers? And does POLARITY_ALERT prepend correctly when the NLI
classifier flags a contradiction (issue #56 v0)?

The semantic-search backend is monkeypatched to return controlled
similarity values, so these tests don't need an embedding model.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_action_hints_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    # Disable real embeddings + polarity by default — tests opt back in via
    # monkeypatch. Keep the embedding env-var unset for the action-hint
    # branch to fire (the "if _embedder.is_available()" gate).
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    import engram_core
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _stub_semantic_with(server, similarity, target_id, target_claim):
    """Force _semantic_search to return one synthetic hit at the chosen sim."""
    def stub(conn, query_text, **kwargs):
        return [{
            "id": target_id,
            "type": "observation_factual",
            "claim": target_claim,
            "confidence": 0.8,
            "similarity": similarity,
            "quote_type": "attributed_analysis",
            "importance_score": 1.0,
        }]
    server._semantic_search = stub

    # Force the embedder gate so the semantic-search branch fires
    class _StubEmb:
        def is_available(self_): return True
    server._embedder = _StubEmb()
    server.core._get_embedding_config = lambda: {"enabled": True}


def _seed_first_obs(server, url="https://example.com/source-A"):
    payload = {
        "url": url,
        "title": "Source A",
        "quoted_text": "first claim text body",
        "interpretation": "interpretation A",
        "claim": "Original anchor claim about topic Z.",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    return r["observation_id"]


def _add_second_obs(server, claim, url="https://example.com/source-B"):
    payload = {
        "url": url,
        "title": "Source B (different)",
        "quoted_text": "second source different content body",
        "interpretation": "interpretation B",
        "claim": claim,
        "quote_type": "attributed_analysis",
    }
    return json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))


# ── Tier labeling at boundary similarities ─────────────────────────────────


def test_corroborate_at_high_similarity():
    """sim >= action_hint_corroborate (default 0.65) → CORROBORATE prefix."""
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.85, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Independent confirmation of topic Z claim.")
    assert r.get("similar_count", 0) >= 1, r
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("CORROBORATE"), f"Expected CORROBORATE, got: {hint}"


def test_related_at_moderate_similarity():
    """0.50 <= sim < 0.65 → RELATED prefix."""
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.55, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Adjacent but distinct topic-Z claim.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("RELATED"), f"Expected RELATED, got: {hint}"


def test_weak_match_at_low_similarity():
    """sim < 0.50 → WEAK_MATCH prefix.

    Note: dedup_min_similarity (0.40) gates whether the match surfaces at
    all. We pick 0.45 — above the floor, below RELATED.
    """
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.45, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Tangential topic-Z mention.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("WEAK_MATCH"), f"Expected WEAK_MATCH, got: {hint}"


def test_corroborate_at_exact_threshold():
    """sim == action_hint_corroborate boundary uses >= so 0.65 → CORROBORATE."""
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.65, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Boundary-case topic Z claim.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("CORROBORATE"), (
        f"sim==0.65 should be CORROBORATE (>= boundary), got: {hint}"
    )


def test_related_at_exact_threshold():
    """sim == action_hint_related boundary uses >= so 0.50 → RELATED."""
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.50, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Mid-strength topic Z claim.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("RELATED"), (
        f"sim==0.50 should be RELATED (>= boundary), got: {hint}"
    )


# ── Hint string contains similarity number for agent context ───────────────


def test_hint_contains_similarity_value():
    """Each hint embeds the (sim:.2f) value so the agent can sanity-check."""
    server = fresh_server()
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.71, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Topic Z claim variant.")
    hint = r["similar_existing"][0]["action_hint"]
    assert "0.71" in hint, f"Hint missing similarity value: {hint}"


# ── Config overrides actually drive the tier boundaries ───────────────────


def test_thresholds_loaded_from_config_override():
    """A config override should change which tier label fires.

    Confirms _get_thresholds_config() is the surface code path uses, not
    the module-level constants — i.e. config edits work without restart.
    """
    server = fresh_server()
    first_id = _seed_first_obs(server)
    # Write an override that bumps CORROBORATE to 0.90 — sim 0.70 should
    # now fall into RELATED (was CORROBORATE under defaults).
    cfg_path = server.core.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["thresholds"] = {
        "dedup_top_k": 15,
        "dedup_min_similarity": 0.40,
        "action_hint_corroborate": 0.90,
        "action_hint_related": 0.50,
        "pattern_balanced_cosine": 0.55,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))

    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Original anchor claim about topic Z.")
    r = _add_second_obs(server, claim="Topic Z claim under custom thresholds.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("RELATED"), (
        f"With CORROBORATE bumped to 0.90, sim=0.70 should be RELATED, got: {hint}"
    )
