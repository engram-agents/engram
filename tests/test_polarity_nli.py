"""Tests for NLI polarity-dedup wiring (issue #56 v0).

Covers the *integration shape* — config gating, similarity floor, and
POLARITY_ALERT prefix formatting — using a stubbed NLI scorer. The actual
ModernCE-large-nli model integration is empirically validated upstream by
the 2026-05-10 NLI polarity bake-off (AUC 0.847, F1 0.889 at t=0.46).

Default test runs use the stub — no 1.5GB model download. There is no
real-model integration test in this file (would require a 1.5GB download
+ GPU). If you add one, gate it with
`@pytest.mark.skipif(not torch.cuda.is_available(), ...)` and register
the marker in pyproject.toml or conftest.py first.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_polarity_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server(*, polarity_enabled: bool, nli_score: float | None = None):
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    if polarity_enabled:
        os.environ.pop("ENGRAM_NO_POLARITY", None)
    else:
        os.environ["ENGRAM_NO_POLARITY"] = "1"
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
    import server
    import engram_core
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()

    # Stub the embedder gate so semantic-search branch fires. _embedder has
    # consumers in BOTH namespaces post-#872-wave-1 (_semantic_search reads
    # the server-module alias; core-resident _compute_and_store_embedding
    # reads engram_core._embedder) — stub both or the core-side readers
    # silently bypass the stub (review-caught on PR #894 round 1).
    class _StubEmb:
        def is_available(self_): return True
    server._embedder = _StubEmb()
    engram_core._embedder = server._embedder
    engram_core._get_embedding_config = lambda: {"enabled": True}

    # Patch polarity.enabled into config.json (read-modify-write) per the
    # fixture flag. The production default is `enabled=False` as of
    # 2026-05-15, so tests that want polarity on must opt-in explicitly
    # via the config file — same path real users follow. Read-modify-write
    # rather than overwrite so any other config sections that
    # _ensure_data_dir() wrote (thresholds, memory, embedding, cadence)
    # survive — keeps the fixture faithful to the real opt-in surface
    # users edit.
    if polarity_enabled:
        config_path = engram_core.CONFIG_PATH
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except json.JSONDecodeError:
                existing = {}
        existing["polarity"] = {
            "enabled": True,
            "model": "dleemiller/ModernCE-large-nli",
            "threshold": 0.46,
            "min_similarity_for_check": 0.30,
        }
        config_path.write_text(json.dumps(existing))

    # Stub the NLI classifier to return the chosen score (or None if not set).
    # Provides BOTH score() and score_batch() since the production code path
    # uses score_batch() (one batched call per write) and tests + back-compat
    # callers use score() (single-pair).
    if nli_score is not None:
        class _StubNLI:
            def __init__(self_): self_._loaded = True
            def available(self_): return True
            def score(self_, prem, hyp, model_name): return nli_score
            def score_batch(self_, pairs, model_name):
                return [nli_score] * len(pairs)
        server._nli_classifier = _StubNLI()

    return server


def _stub_semantic_with(server, similarity, target_id, target_claim):
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
    # _similar_existing_matches now lives in engram_core, so patch core.
    server.core._semantic_search = stub


def _seed_first_obs(server):
    payload = {
        "url": "https://example.com/source-A",
        "title": "Source A",
        "quoted_text": "first source claim text body",
        "interpretation": "interpretation A",
        "claim": "Topic Z is X.",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    return r["observation_id"]


def _add_second_obs(server, claim):
    payload = {
        "url": "https://example.com/source-B",
        "title": "Source B (different)",
        "quoted_text": "second source different content body",
        "interpretation": "interpretation B",
        "claim": claim,
        "quote_type": "attributed_analysis",
    }
    return json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))


# ── POLARITY_ALERT fires when NLI score >= threshold ───────────────────────


def test_polarity_alert_fires_above_threshold():
    """NLI score 0.60 (>= default 0.46) → POLARITY_ALERT prefix."""
    server = fresh_server(polarity_enabled=True, nli_score=0.60)
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("POLARITY_ALERT"), f"Expected POLARITY_ALERT, got: {hint}"
    assert "0.60" in hint, f"Expected p=0.60 in hint, got: {hint}"
    # Original tier hint must be preserved after the prefix
    assert "CORROBORATE" in hint, f"Expected CORROBORATE retained, got: {hint}"


@pytest.mark.parametrize("similarity,expected_tier", [
    (0.80, "CORROBORATE"),   # high-similarity tier
    (0.55, "RELATED"),       # mid-similarity tier
    (0.45, "WEAK_MATCH"),    # low-similarity tier (still > min_similarity_for_check 0.30)
])
def test_polarity_alert_overlays_all_cross_source_tiers(similarity, expected_tier):
    """POLARITY_ALERT prepends to ALL three cross-source tier hints (closes
    fairy suggestion #6 — coverage gap was that all earlier tests used
    CORROBORATE). The low-cosine-but-NLI-fires case is operationally the
    most surprising — a polarity dispute the embedder didn't catch."""
    server = fresh_server(polarity_enabled=True, nli_score=0.70)
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=similarity, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    assert hint.startswith("POLARITY_ALERT"), (
        f"Expected POLARITY_ALERT prefix at tier {expected_tier} (sim={similarity}), got: {hint}"
    )
    assert expected_tier in hint, (
        f"Expected tier {expected_tier} retained after prefix, got: {hint}"
    )


def test_polarity_alert_fires_on_same_source():
    """Per Lei 2026-05-10: 'same source still worth marking contradiction'.
    Polarity overlay applies to BOTH same-source (DUPLICATE/POSSIBLE_DUPLICATE/
    DISTINCT_FROM_SAME_SOURCE) and cross-source tier branches.

    Setup: file the first obs, then file second citing SAME evidence_id so
    same_ev=True triggers the same-source branch. Stub semantic to surface
    the first as a high-sim candidate."""
    server = fresh_server(polarity_enabled=True, nli_score=0.70)

    payload1 = {
        "url": "https://example.com/source-A",
        "title": "Source A",
        "quoted_text": "first source claim text body",
        "interpretation": "interpretation A",
        "claim": "Topic Z is X.",
        "quote_type": "attributed_analysis",
    }
    r1 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload1)))
    first_id = r1["observation_id"]
    first_ev = r1["evidence_id"]

    def stub_with_ev(conn, query_text, **kwargs):
        return [{
            "id": first_id,
            "type": "observation_factual",
            "claim": "Topic Z is X.",
            "confidence": 0.8,
            "similarity": 0.90,
            "quote_type": "attributed_analysis",
            "importance_score": 1.0,
            "evidence_id": first_ev,
        }]
    server.core._semantic_search = stub_with_ev

    payload2 = {
        "evidence_id": first_ev,
        "quoted_text": "second-claim text within same source body",
        "interpretation": "interpretation B",
        "claim": "Topic Z is NOT X.",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload2)))
    hint = r["similar_existing"][0]["action_hint"]
    same_source_labels = ("DUPLICATE", "POSSIBLE_DUPLICATE", "DISTINCT_FROM_SAME_SOURCE")
    assert any(label in hint for label in same_source_labels), (
        f"Expected a same-source tier label, got: {hint}"
    )
    assert hint.startswith("POLARITY_ALERT"), (
        f"POLARITY_ALERT should overlay same-source per Lei's call: {hint}"
    )


def test_no_polarity_alert_below_threshold():
    """NLI score 0.30 (< default 0.46) → no POLARITY_ALERT prefix."""
    server = fresh_server(polarity_enabled=True, nli_score=0.30)
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is X with extra detail.")
    hint = r["similar_existing"][0]["action_hint"]
    assert not hint.startswith("POLARITY_ALERT"), f"Should not fire below threshold: {hint}"
    assert hint.startswith("CORROBORATE"), f"Expected plain CORROBORATE, got: {hint}"


def test_polarity_disabled_via_env_var():
    """ENGRAM_NO_POLARITY=1 disables the check even at high NLI score."""
    server = fresh_server(polarity_enabled=False, nli_score=0.95)
    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    assert not hint.startswith("POLARITY_ALERT"), (
        f"ENGRAM_NO_POLARITY should suppress: {hint}"
    )


def test_polarity_disabled_via_config():
    """config.polarity.enabled=False disables the check."""
    server = fresh_server(polarity_enabled=True, nli_score=0.95)
    # Write polarity-disabled config
    cfg_path = server.core.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["polarity"] = {
        "enabled": False,
        "model": "dleemiller/ModernCE-large-nli",
        "threshold": 0.46,
        "min_similarity_for_check": 0.30,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))

    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    assert not hint.startswith("POLARITY_ALERT"), (
        f"config.polarity.enabled=False should suppress: {hint}"
    )


def test_polarity_skipped_when_sim_below_min_check():
    """sim < min_similarity_for_check (0.30 default) skips the NLI call entirely."""
    # Use a NLI score that WOULD fire if asked — but we shouldn't be asked
    # because sim is too low.
    call_count = {"n": 0}

    server = fresh_server(polarity_enabled=True)
    class _CountingNLI:
        def __init__(self_): self_._loaded = True
        def available(self_): return True
        def score(self_, prem, hyp, model_name):
            call_count["n"] += 1
            return 0.99
        def score_batch(self_, pairs, model_name):
            call_count["n"] += len(pairs)
            return [0.99] * len(pairs)
    server._nli_classifier = _CountingNLI()

    first_id = _seed_first_obs(server)
    # sim 0.25 < default min_similarity_for_check 0.30, but we also need it
    # above DEDUP_MIN_SIMILARITY (0.40) for the match to surface in the
    # results at all. Use min_similarity_for_check=0.45 via config so we
    # can test the gate without needing two semantic-search hits.
    cfg_path = server.core.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg.setdefault("polarity", {})
    cfg["polarity"]["enabled"] = True
    cfg["polarity"]["threshold"] = 0.46
    cfg["polarity"]["min_similarity_for_check"] = 0.65
    cfg_path.write_text(json.dumps(cfg, indent=2))

    _stub_semantic_with(server, similarity=0.55, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Different topic Y is something else.")
    hint = r["similar_existing"][0]["action_hint"]
    assert call_count["n"] == 0, f"NLI should not be called below min_sim_for_check; was called {call_count['n']}x"
    assert not hint.startswith("POLARITY_ALERT"), hint


def test_polarity_threshold_configurable():
    """Setting config.polarity.threshold changes the firing point."""
    server = fresh_server(polarity_enabled=True, nli_score=0.50)
    cfg_path = server.core.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["polarity"] = {
        "enabled": True,
        "model": "dleemiller/ModernCE-large-nli",
        "threshold": 0.80,        # raised — NLI score 0.50 should NOT fire
        "min_similarity_for_check": 0.30,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))

    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    assert not hint.startswith("POLARITY_ALERT"), (
        f"threshold=0.80, score=0.50 should not fire: {hint}"
    )


def test_polarity_nli_returning_none_is_safe():
    """NLI scorer returning None (model-load failure) doesn't break the path."""
    server = fresh_server(polarity_enabled=True)
    class _FailingNLI:
        def __init__(self_): self_._loaded = False
        def available(self_): return False
        def score(self_, prem, hyp, model_name): return None
        def score_batch(self_, pairs, model_name): return None
    server._nli_classifier = _FailingNLI()

    first_id = _seed_first_obs(server)
    _stub_semantic_with(server, similarity=0.70, target_id=first_id,
                        target_claim="Topic Z is X.")
    r = _add_second_obs(server, claim="Topic Z is NOT X.")
    hint = r["similar_existing"][0]["action_hint"]
    # No POLARITY_ALERT, but the underlying tier hint must still work
    assert not hint.startswith("POLARITY_ALERT"), hint
    assert hint.startswith("CORROBORATE"), f"Tier hint must still work: {hint}"


def test_batched_polarity_one_call_per_write():
    """The hot path makes exactly ONE score_batch() call per write, not K calls.

    Validates the batch optimization: even with 5 candidates returned by
    semantic search, score_batch is called once with all 5 pairs (filtered
    by min_similarity_for_check) — not 5 sequential single-pair calls.
    """
    server = fresh_server(polarity_enabled=True)

    batch_calls = {"n": 0, "max_pair_count": 0}

    class _BatchCountingNLI:
        def __init__(self_): self_._loaded = True
        def available(self_): return True
        def score(self_, prem, hyp, model_name):
            # Should NOT be called by the hot path.
            raise AssertionError("score() called — hot path should use score_batch()")
        def score_batch(self_, pairs, model_name):
            batch_calls["n"] += 1
            batch_calls["max_pair_count"] = max(
                batch_calls["max_pair_count"], len(pairs)
            )
            return [0.70] * len(pairs)

    server._nli_classifier = _BatchCountingNLI()

    first_id = _seed_first_obs(server)

    # Stub semantic search to return MULTIPLE candidates (5).
    def stub_multi(conn, query_text, **kwargs):
        return [
            {
                "id": f"ob_900{i}",
                "type": "observation_factual",
                "claim": f"Stub claim {i} about topic Z.",
                "confidence": 0.8,
                "similarity": 0.65,  # above min_sim_for_check (0.30) → all eligible
                "quote_type": "attributed_analysis",
                "importance_score": 1.0,
            }
            for i in range(5)
        ]
    server.core._semantic_search = stub_multi

    r = _add_second_obs(server, claim="Topic Z disputed claim.")

    # Hot path should have made exactly ONE batched call covering all 5 candidates
    assert batch_calls["n"] == 1, (
        f"Expected exactly 1 score_batch call (the batch optimization), "
        f"got {batch_calls['n']}"
    )
    assert batch_calls["max_pair_count"] == 5, (
        f"Expected the batched call to include all 5 candidates, got "
        f"{batch_calls['max_pair_count']} pairs"
    )
    # All 5 should have POLARITY_ALERT prepended (stub returns 0.70 ≥ 0.46)
    for m in r["similar_existing"]:
        assert m["action_hint"].startswith("POLARITY_ALERT"), (
            f"Expected POLARITY_ALERT on candidate {m['id']}, got: {m['action_hint']}"
        )


def test_batched_polarity_filters_below_min_sim():
    """Candidates below min_similarity_for_check must NOT enter the batch."""
    server = fresh_server(polarity_enabled=True)
    batch_calls = {"pairs_seen": 0}

    class _PairCountNLI:
        def __init__(self_): self_._loaded = True
        def available(self_): return True
        def score(self_, prem, hyp, model_name): return 0.70
        def score_batch(self_, pairs, model_name):
            batch_calls["pairs_seen"] = len(pairs)
            return [0.70] * len(pairs)

    server._nli_classifier = _PairCountNLI()

    first_id = _seed_first_obs(server)

    # Mix: 2 candidates below floor (sim=0.20), 3 above (sim=0.65)
    def stub_mixed(conn, query_text, **kwargs):
        rows = []
        for i in range(2):
            rows.append({
                "id": f"ob_low_{i}", "type": "observation_factual",
                "claim": f"Low-sim stub {i}", "confidence": 0.5,
                "similarity": 0.20,  # below min_sim_for_check (0.30)
                "quote_type": "attributed_analysis", "importance_score": 1.0,
            })
        for i in range(3):
            rows.append({
                "id": f"ob_high_{i}", "type": "observation_factual",
                "claim": f"High-sim stub {i}", "confidence": 0.8,
                "similarity": 0.65,
                "quote_type": "attributed_analysis", "importance_score": 1.0,
            })
        return rows
    server.core._semantic_search = stub_mixed

    # min_similarity_for_check is 0.30 by default; with sim=0.20 both
    # the dedup floor (0.40 default) AND the polarity floor (0.30) are below
    # — so the low-sim ones may not surface in similar_existing at all
    # (the add_* inline auto-similarity-hint uses dedup_min_similarity, not
    # stub-bypassed here because we're stubbing _semantic_search). The KEY
    # assertion: the batched call must NOT contain low-sim pairs even if
    # they DID surface.
    _add_second_obs(server, claim="Topic Z disputed claim.")
    assert batch_calls["pairs_seen"] <= 3, (
        f"Batch should exclude pairs below min_sim_for_check (0.30); "
        f"got {batch_calls['pairs_seen']} pairs in the batch"
    )


# ── Config defaults match v0 recommendation (issue #56 bake-off) ───────────


def test_polarity_defaults():
    """Defaults: enabled=False (per Lei 2026-05-15 — opt-in for fresh installs),
    model=ModernCE-large-nli, threshold=0.46."""
    if "ENGRAM_NO_POLARITY" in os.environ:
        del os.environ["ENGRAM_NO_POLARITY"]
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    cfg = server._get_polarity_config()
    assert cfg["enabled"] is False
    assert cfg["model"] == "dleemiller/ModernCE-large-nli"
    assert cfg["threshold"] == 0.46
    assert cfg["min_similarity_for_check"] == 0.30
