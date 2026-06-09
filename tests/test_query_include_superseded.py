"""Regression tests for engram_query(include_superseded=True) — issue #191.

Background (#191):
    The original bug was that engram_query(include_superseded=True) honored the
    flag in the FTS path but hardcoded is_current = 1 in the semantic path,
    silently dropping superseded matches on the semantic side.

What is fixed (as of PR #272):
    server.py _search_nodes (line 5174): accepts include_superseded: bool = False.
    server.py FTS post-filter (line 5314): skips the is_current check when
        include_superseded=True, so superseded nodes surface in FTS results.

What is fixed (as of PR #273 — this PR):
    server.py _semantic_search now accepts include_superseded: bool = False.
    Both the KNN fast path and Python slow path honor the flag — superseded
    nodes surface on the semantic side when include_superseded=True.
    Retracted nodes are always excluded on the semantic side regardless of the
    flag (defense-in-depth). Test 5 exercises the slow path directly.

Fixed in #274 (retracted-node FTS leak):
    Retracted nodes (status='retracted', is_current=0) previously surfaced when
    include_superseded=True because the FTS post-filter at line 5314 checked only
    is_current, not status. The nodes_retract_remove_from_fts trigger (server.py,
    #274) removes retracted nodes from nodes_fts at retraction time — so they are
    absent from the FTS index and never appear in results regardless of the flag.
    Test 2 below is updated to assert the corrected behavior.

Coverage (one item per test function):
    1. test_query_include_superseded_fts_path — core #191 regression:
       - include_superseded=False excludes superseded nodes from FTS hits.
       - include_superseded=True includes them via FTS path.
       - Current (non-superseded) replacement node surfaces in both modes.
    2. test_query_retracted_behavior_with_include_superseded_flag — #274 fix:
       - Retracted nodes do NOT surface with include_superseded=True (trigger
         removes them from nodes_fts at retraction time).
    3. test_query_include_superseded_false_default_excludes_superseded — sanity
       baseline: default call (no flag) excludes superseded, includes current.
    4. test_query_include_superseded_multiple_superseded_nodes — multiple
       superseded nodes in one query all surface with flag=True.
    5. test_query_include_superseded_semantic_path — semantic-side regression
       for issue #273: _semantic_search honors include_superseded + always
       excludes retracted. Calls _semantic_search directly (slow path) with a
       mock embedder and a tiny fake corpus to bypass ENGRAM_NO_EMBEDDINGS=1.

Import-pattern contract (#948 wave 1 — R1):
    This file imports engram_query, engram_observation, engram_revision, and
    engram_core directly and contains no ``import server``. The module objects
    are passed to fresh_module() (R4) for per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_observation
import engram_query
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_include_superseded_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_mods():
    """Reload all family modules, configure a clean test DB, and return core.

    Uses conftest.fresh_module(engram_query) per R4: the module object is
    passed (not a string), so ``import engram_query`` at the top is the
    AST-visible dependency edge for run_touched_tests.py.

    Returns the reloaded engram_core (shared state root).
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_query)  # purges server + engram_* and reloads engram_query
    obs = importlib.import_module("engram_observation")
    rev = importlib.import_module("engram_revision")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core, obs, rev


def _add_observation(obs, claim: str, quoted_text: str = None, url: str = "https://example.com/test") -> str:
    qt = quoted_text if quoted_text is not None else claim[:80]
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Test Source",
        quoted_text=qt,
        interpretation="Test interpretation.",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _query_ids(qry, query: str, include_superseded: bool = False, top_k: int = 20) -> set:
    """Run engram_query in debug mode and return the set of result IDs."""
    r = json.loads(qry._query_impl(
        query=query,
        include_superseded=include_superseded,
        top_k=top_k,
        return_debug=True,
    ))
    return {n["id"] for n in r.get("results", [])}


# ---------------------------------------------------------------------------
# Test 1: include_superseded=True surfaces superseded nodes via FTS path
# ---------------------------------------------------------------------------

def test_query_include_superseded_fts_path():
    """FTS path: superseded node appears with flag=True, absent with flag=False.

    This is the core regression test for #191 (FTS side). A node B is superseded
    by D. With include_superseded=False, only D surfaces. With include_superseded=True,
    both B and D surface.

    Note: ENGRAM_NO_EMBEDDINGS=1 disables semantic search — this test exclusively
    exercises the FTS path. The semantic path is not yet fixed (see module docstring).
    """
    core, obs, rev = _fresh_mods()
    qry = sys.modules["engram_query"]

    # B: will be superseded. Uses a highly unique token so FTS finds it reliably.
    b_id = _add_observation(
        obs,
        claim="superseded claim about fts191regression uniquemarker",
        quoted_text="fts191regression uniquemarker original quote",
    )

    # D: the replacement for B.
    d_id = _add_observation(
        obs,
        claim="replacement claim about fts191regression uniquemarker refined",
        quoted_text="fts191regression uniquemarker refined quote",
    )

    # Supersede B with D.
    sup = json.loads(rev._supersede_impl(
        b_id, d_id, supersede_reason="regression test for issue #191"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    query = "fts191regression uniquemarker"

    # With flag=False: only D (the current node) should appear.
    ids_false = _query_ids(qry, query, include_superseded=False)
    assert d_id in ids_false, (
        f"Replacement node D ({d_id}) missing from include_superseded=False results: {ids_false}"
    )
    assert b_id not in ids_false, (
        f"Superseded node B ({b_id}) should not appear with include_superseded=False. "
        f"Results: {ids_false}"
    )

    # With flag=True: both B (superseded) and D (current) should appear.
    ids_true = _query_ids(qry, query, include_superseded=True)
    assert d_id in ids_true, (
        f"Replacement node D ({d_id}) missing from include_superseded=True results: {ids_true}"
    )
    assert b_id in ids_true, (
        f"Superseded node B ({b_id}) should appear with include_superseded=True (FTS path). "
        f"This is the regression fix from issue #191. Results: {ids_true}"
    )


# ---------------------------------------------------------------------------
# Test 2: Retracted node behavior with include_superseded flag (gap documentation)
# ---------------------------------------------------------------------------

def test_query_retracted_behavior_with_include_superseded_flag():
    """Retracted nodes must NOT surface even with include_superseded=True (#274 fix).

    The nodes_retract_remove_from_fts trigger (server.py) removes retracted nodes
    from nodes_fts at retraction time. This ensures retracted nodes are absent from
    FTS results regardless of include_superseded — the trigger removes them from the
    index entirely, so the post-filter at line 5314 never sees them.

    Superseded nodes (is_current=0, status=NULL/unchanged) are intentionally kept in
    nodes_fts per design intent: "they're like past memories that still exist,
    just the information is not accurate anymore." They continue to surface with
    include_superseded=True (see test 1 and test 4).
    """
    core, obs, rev = _fresh_mods()
    qry = sys.modules["engram_query"]

    x_id = _add_observation(
        obs,
        claim="retracted claim retract191regression uniquemarkerX",
        quoted_text="retract191regression uniquemarkerX quote",
    )

    ret = json.loads(rev._retract_impl(
        node_id=x_id,
        error_type="other",
        reason="test retraction for issue #191 regression suite",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    query = "retract191regression uniquemarkerX"

    # With flag=False: retracted node absent (expected and correct).
    ids_false = _query_ids(qry, query, include_superseded=False)
    assert x_id not in ids_false, (
        f"Retracted node ({x_id}) should not appear with include_superseded=False. "
        f"Results: {ids_false}"
    )

    # With flag=True: retracted node must NOT surface — #274 fix.
    # The nodes_retract_remove_from_fts trigger (added in #274) removes retracted
    # nodes from nodes_fts at retraction time, so they never appear in FTS results
    # regardless of the include_superseded flag.
    ids_true = _query_ids(qry, query, include_superseded=True)
    assert x_id not in ids_true, (
        f"Retracted node ({x_id}) must not appear with include_superseded=True "
        "after #274 fix (trigger removes retracted nodes from nodes_fts). "
        f"Results: {ids_true}"
    )


# ---------------------------------------------------------------------------
# Test 3: Default behavior (flag absent) excludes superseded — sanity baseline
# ---------------------------------------------------------------------------

def test_query_include_superseded_false_default_excludes_superseded():
    """Sanity baseline: default engram_query excludes superseded nodes.

    Files an observation, supersedes it, queries without the flag. The superseded
    node must not appear. This makes the default-False behavior explicit so any
    regression that flips the default or breaks the is_current filter fires here.
    """
    core, obs, rev = _fresh_mods()
    qry = sys.modules["engram_query"]

    old_id = _add_observation(
        obs,
        claim="old version baseline191regression uniquemarkerZ",
        quoted_text="baseline191regression uniquemarkerZ old quote",
    )
    new_id = _add_observation(
        obs,
        claim="new version baseline191regression uniquemarkerZ refined",
        quoted_text="baseline191regression uniquemarkerZ new quote",
    )

    sup = json.loads(rev._supersede_impl(
        old_id, new_id, supersede_reason="baseline regression test"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    query = "baseline191regression uniquemarkerZ"

    # Default call (no include_superseded kwarg at all) — same as False.
    r_default = json.loads(qry._query_impl(
        query=query, top_k=20, return_debug=True
    ))
    ids_default = {n["id"] for n in r_default.get("results", [])}

    assert old_id not in ids_default, (
        f"Superseded node ({old_id}) must not appear in default (include_superseded=False) query. "
        f"Results: {ids_default}"
    )
    assert new_id in ids_default, (
        f"Current replacement node ({new_id}) must appear in default query. "
        f"Results: {ids_default}"
    )


# ---------------------------------------------------------------------------
# Test 4: Multiple superseded nodes — all surface with flag=True
# ---------------------------------------------------------------------------

def test_query_include_superseded_multiple_superseded_nodes():
    """All superseded nodes in a chain surface when include_superseded=True.

    Files C (current), supersedes B with C, and checks B surfaces.
    Also files E (current), supersedes D with E, and checks both B and D surface.
    This catches edge cases in the FTS post-filter loop where only one of multiple
    superseded nodes might surface.
    """
    core, obs, rev = _fresh_mods()
    qry = sys.modules["engram_query"]

    unique = "multi191regression uniquemarkerQ"

    b_id = _add_observation(obs, claim=f"B superseded {unique}", quoted_text=f"B {unique}")
    c_id = _add_observation(obs, claim=f"C replacement {unique}", quoted_text=f"C {unique}")
    d_id = _add_observation(obs, claim=f"D superseded {unique}", quoted_text=f"D {unique}")
    e_id = _add_observation(obs, claim=f"E replacement {unique}", quoted_text=f"E {unique}")

    sup1 = json.loads(rev._supersede_impl(b_id, c_id))
    assert sup1.get("status") == "superseded", f"B→C supersede failed: {sup1}"

    sup2 = json.loads(rev._supersede_impl(d_id, e_id))
    assert sup2.get("status") == "superseded", f"D→E supersede failed: {sup2}"

    query = "multi191regression uniquemarkerQ"

    # With flag=False: only current nodes (C, E) surface.
    ids_false = _query_ids(qry, query, include_superseded=False)
    assert b_id not in ids_false, f"B (superseded) should not appear with flag=False: {ids_false}"
    assert d_id not in ids_false, f"D (superseded) should not appear with flag=False: {ids_false}"
    assert c_id in ids_false, f"C (current) should appear with flag=False: {ids_false}"
    assert e_id in ids_false, f"E (current) should appear with flag=False: {ids_false}"

    # With flag=True: all four nodes surface (B, C, D, E).
    ids_true = _query_ids(qry, query, include_superseded=True)
    assert b_id in ids_true, f"B (superseded) should appear with flag=True: {ids_true}"
    assert c_id in ids_true, f"C (current) should appear with flag=True: {ids_true}"
    assert d_id in ids_true, f"D (superseded) should appear with flag=True: {ids_true}"
    assert e_id in ids_true, f"E (current) should appear with flag=True: {ids_true}"


# ---------------------------------------------------------------------------
# Test 5: _semantic_search honors include_superseded + always excludes retracted
# ---------------------------------------------------------------------------

def test_query_include_superseded_semantic_path():
    """Semantic path: _semantic_search honors include_superseded; retracted always excluded.

    Issue #273 regression test: _semantic_search hardcoded is_current=1 in both
    KNN fast-path and Python slow-path, silently dropping superseded nodes even
    when include_superseded=True.

    This test calls _semantic_search directly with a mock embedder (bypassing
    ENGRAM_NO_EMBEDDINGS=1 which disables the real embedder) and a hand-built
    fake corpus inserted directly into the DB. It exercises the pure-Python slow
    path (the KNN fast-path requires sqlite-vec extension, not loaded in tests).

    Fake corpus (3-dimensional unit vectors; cosine similarity is exact):
      - current_id:     is_current=1, status=NULL,       emb=[1, 0, 0]
      - superseded_id:  is_current=0, status=NULL,       emb=[1, 0, 0]  (superseded)
      - retracted_id:   is_current=0, status='retracted', emb=[1, 0, 0]  (retracted)

    Query vector: [1, 0, 0] — similarity 1.0 with all three nodes.

    Expected behavior:
      include_superseded=False: only current_id surfaces.
      include_superseded=True:  current_id + superseded_id surface; retracted_id never does.
    """
    core, obs, rev = _fresh_mods()

    conn = core._get_db()
    now = "2026-01-01T00:00:00"
    fake_emb = json.dumps([1.0, 0.0, 0.0])

    try:
        # Insert three nodes directly — bypassing the full API to avoid
        # triggering embedding computation (which is disabled via ENGRAM_NO_EMBEDDINGS=1).
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, status, embedding, "
            "confidence, importance_score, quote_type, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("sem_cur_273", "observation_factual",
             "current node sem273regression uniquemarkersem", now,
             1, None, fake_emb, 0.85, 0.5, "hard_data", "{}"),
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, status, embedding, "
            "confidence, importance_score, quote_type, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("sem_sup_273", "observation_factual",
             "superseded node sem273regression uniquemarkersem", now,
             0, None, fake_emb, 0.85, 0.5, "hard_data", "{}"),
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, status, embedding, "
            "confidence, importance_score, quote_type, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("sem_ret_273", "observation_factual",
             "retracted node sem273regression uniquemarkersem", now,
             0, "retracted", fake_emb, 0.85, 0.5, "hard_data", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # Mock embedder: is_available()=True, embed()=fake query vector, cosine_similarity=real.
    class _MockEmbedder:
        def is_available(self):
            return True

        def embed(self, text, model_name):
            return [1.0, 0.0, 0.0]

        @staticmethod
        def cosine_similarity(a, b):
            # Reuse real implementation — works on plain lists.
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(x * x for x in b) ** 0.5
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

    original_embedder = core._embedder
    mock_embedder = _MockEmbedder()
    core._embedder = mock_embedder
    try:
        conn2 = core._get_db()
        try:
            # Default (include_superseded=False): only current node.
            results_false = core._semantic_search(
                conn2, "sem273regression uniquemarkersem",
                top_k=10, min_similarity=0.5,
                include_superseded=False,
            )
            ids_false = {r["id"] for r in results_false}
            assert "sem_cur_273" in ids_false, (
                f"Current node missing from include_superseded=False semantic results: {ids_false}"
            )
            assert "sem_sup_273" not in ids_false, (
                f"Superseded node should not appear with include_superseded=False: {ids_false}"
            )
            assert "sem_ret_273" not in ids_false, (
                f"Retracted node must never appear (include_superseded=False): {ids_false}"
            )

            # include_superseded=True: current + superseded; retracted still absent.
            results_true = core._semantic_search(
                conn2, "sem273regression uniquemarkersem",
                top_k=10, min_similarity=0.5,
                include_superseded=True,
            )
            ids_true = {r["id"] for r in results_true}
            assert "sem_cur_273" in ids_true, (
                f"Current node missing from include_superseded=True semantic results: {ids_true}"
            )
            assert "sem_sup_273" in ids_true, (
                f"Superseded node should appear with include_superseded=True (semantic path). "
                f"This is the PR #273 regression fix. Results: {ids_true}"
            )
            assert "sem_ret_273" not in ids_true, (
                f"Retracted node must never appear regardless of include_superseded. "
                f"(Defense-in-depth: retracted nodes were never valid.) Results: {ids_true}"
            )
        finally:
            conn2.close()
    finally:
        core._embedder = original_embedder


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
