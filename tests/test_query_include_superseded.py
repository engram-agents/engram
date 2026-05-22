"""Regression tests for engram_query(include_superseded=True) — issue #191.

Background (#191):
    The original bug was that engram_query(include_superseded=True) honored the
    flag in the FTS path but hardcoded is_current = 1 in the semantic path,
    silently dropping superseded matches on the semantic side.

What is fixed (as of this PR):
    server.py _search_nodes (line 5174): accepts include_superseded: bool = False.
    server.py FTS post-filter (line 5314): skips the is_current check when
        include_superseded=True, so superseded nodes surface in FTS results.

What is NOT yet fixed (documented here for completeness):
    The _semantic_search function (line 2283) does not accept include_superseded
    and hardcodes is_current = 1 in both its KNN fast path and Python slow path.
    The semantic path therefore still silently drops superseded nodes regardless
    of the flag. Tests run with ENGRAM_NO_EMBEDDINGS=1 (semantic disabled) so
    they only exercise the FTS path. A follow-up issue should fix _semantic_search
    and add semantic-path coverage once that fix is in.

Known behavior gap documented by these tests:
    Retracted nodes (status='retracted', is_current=0) also surface when
    include_superseded=True. The FTS post-filter at line 5314 checks is_current
    but not status, so retracted nodes pass through alongside superseded ones.
    Test 2 below documents this gap explicitly so any future fix that correctly
    excludes retracted nodes will need to update that test.

Coverage (one item per test function):
    1. test_query_include_superseded_fts_path — core #191 regression:
       - include_superseded=False excludes superseded nodes from FTS hits.
       - include_superseded=True includes them via FTS path.
       - Current (non-superseded) replacement node surfaces in both modes.
    2. test_query_retracted_behavior_with_include_superseded_flag — known gap:
       - include_superseded=True also surfaces retracted nodes (the FTS filter
         checks is_current but not status='retracted'); test is written against
         actual behavior with flip-instruction comment for when #274 is fixed.
    3. test_query_include_superseded_false_default_excludes_superseded — sanity
       baseline: default call (no flag) excludes superseded, includes current.
    4. test_query_include_superseded_multiple_superseded_nodes — multiple
       superseded nodes in one query all surface with flag=True.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_query_include_superseded")


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in list(sys.modules.keys()):
        if mod in ("server", "engram_client"):
            del sys.modules[mod]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, quoted_text: str = None, url: str = "https://example.com/test") -> str:
    qt = quoted_text if quoted_text is not None else claim[:80]
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": qt,
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _query_ids(server, query: str, include_superseded: bool = False, top_k: int = 20) -> set:
    """Run engram_query in debug mode and return the set of result IDs."""
    r = json.loads(server.engram_query(
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
    server = fresh_server()

    # B: will be superseded. Uses a highly unique token so FTS finds it reliably.
    b_id = _add_observation(
        server,
        claim="superseded claim about fts191regression uniquemarker",
        quoted_text="fts191regression uniquemarker original quote",
    )

    # D: the replacement for B.
    d_id = _add_observation(
        server,
        claim="replacement claim about fts191regression uniquemarker refined",
        quoted_text="fts191regression uniquemarker refined quote",
    )

    # Supersede B with D.
    sup = json.loads(server._supersede_impl(
        b_id, d_id, supersede_reason="regression test for issue #191"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    query = "fts191regression uniquemarker"

    # With flag=False: only D (the current node) should appear.
    ids_false = _query_ids(server, query, include_superseded=False)
    assert d_id in ids_false, (
        f"Replacement node D ({d_id}) missing from include_superseded=False results: {ids_false}"
    )
    assert b_id not in ids_false, (
        f"Superseded node B ({b_id}) should not appear with include_superseded=False. "
        f"Results: {ids_false}"
    )

    # With flag=True: both B (superseded) and D (current) should appear.
    ids_true = _query_ids(server, query, include_superseded=True)
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
    """Document retracted-node behavior: currently surfaces with include_superseded=True.

    IMPORTANT: This test documents a KNOWN GAP in the current implementation.
    The FTS post-filter at server.py:5314 checks is_current but not status,
    so retracted nodes (is_current=0, status='retracted') also pass through
    when include_superseded=True.

    The flag is named include_superseded but currently behaves as include_non_current.
    A correct implementation would:
      - include_superseded=True: surface superseded nodes, NOT retracted nodes.
      - include_superseded=False: surface only current nodes (existing behavior).

    When the retracted-exclusion fix lands, update this test to assert:
        assert retracted_id not in ids_true  (instead of the current assertion)

    This test is intentionally written against actual current behavior so it
    passes as-is and documents the gap for the fix PR to correct.
    """
    server = fresh_server()

    x_id = _add_observation(
        server,
        claim="retracted claim retract191regression uniquemarkerX",
        quoted_text="retract191regression uniquemarkerX quote",
    )

    ret = json.loads(server._retract_impl(
        node_id=x_id,
        error_type="other",
        reason="test retraction for issue #191 regression suite",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    query = "retract191regression uniquemarkerX"

    # With flag=False: retracted node absent (expected and correct).
    ids_false = _query_ids(server, query, include_superseded=False)
    assert x_id not in ids_false, (
        f"Retracted node ({x_id}) should not appear with include_superseded=False. "
        f"Results: {ids_false}"
    )

    # With flag=True: retracted node CURRENTLY surfaces (documented gap).
    # This assertion reflects actual behavior as of 2026-05-22.
    # When the fix lands to properly exclude retracted nodes regardless of the flag,
    # change this assertion to: assert x_id not in ids_true
    ids_true = _query_ids(server, query, include_superseded=True)
    assert x_id in ids_true, (
        f"KNOWN GAP: Retracted node ({x_id}) expected to appear with include_superseded=True "
        "(FTS post-filter does not distinguish retracted from superseded). "
        "If this assertion fails, the gap has been fixed — update to assert NOT in. "
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
    server = fresh_server()

    old_id = _add_observation(
        server,
        claim="old version baseline191regression uniquemarkerZ",
        quoted_text="baseline191regression uniquemarkerZ old quote",
    )
    new_id = _add_observation(
        server,
        claim="new version baseline191regression uniquemarkerZ refined",
        quoted_text="baseline191regression uniquemarkerZ new quote",
    )

    sup = json.loads(server._supersede_impl(
        old_id, new_id, supersede_reason="baseline regression test"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    query = "baseline191regression uniquemarkerZ"

    # Default call (no include_superseded kwarg at all) — same as False.
    r_default = json.loads(server.engram_query(
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
    server = fresh_server()

    unique = "multi191regression uniquemarkerQ"

    b_id = _add_observation(server, claim=f"B superseded {unique}", quoted_text=f"B {unique}")
    c_id = _add_observation(server, claim=f"C replacement {unique}", quoted_text=f"C {unique}")
    d_id = _add_observation(server, claim=f"D superseded {unique}", quoted_text=f"D {unique}")
    e_id = _add_observation(server, claim=f"E replacement {unique}", quoted_text=f"E {unique}")

    sup1 = json.loads(server._supersede_impl(b_id, c_id))
    assert sup1.get("status") == "superseded", f"B→C supersede failed: {sup1}"

    sup2 = json.loads(server._supersede_impl(d_id, e_id))
    assert sup2.get("status") == "superseded", f"D→E supersede failed: {sup2}"

    query = "multi191regression uniquemarkerQ"

    # With flag=False: only current nodes (C, E) surface.
    ids_false = _query_ids(server, query, include_superseded=False)
    assert b_id not in ids_false, f"B (superseded) should not appear with flag=False: {ids_false}"
    assert d_id not in ids_false, f"D (superseded) should not appear with flag=False: {ids_false}"
    assert c_id in ids_false, f"C (current) should appear with flag=False: {ids_false}"
    assert e_id in ids_false, f"E (current) should appear with flag=False: {ids_false}"

    # With flag=True: all four nodes surface (B, C, D, E).
    ids_true = _query_ids(server, query, include_superseded=True)
    assert b_id in ids_true, f"B (superseded) should appear with flag=True: {ids_true}"
    assert c_id in ids_true, f"C (current) should appear with flag=True: {ids_true}"
    assert d_id in ids_true, f"D (superseded) should appear with flag=True: {ids_true}"
    assert e_id in ids_true, f"E (current) should appear with flag=True: {ids_true}"


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
