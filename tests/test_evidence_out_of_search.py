"""Contract tests: evidence nodes are out of SEARCH and out of the tier-2 budget.

Ruling (Lei 2026-10-08, tracker #2005 section 4): evidence is reached by
traversal (cites edges, engram_inspect, engram_get_subgraph, engram_list), never
by search; and because the tier budget exists purely for search efficiency,
evidence no longer consumes tier-2 slots either.

The rule lives in ONE place: ``engram_core.SEARCH_EXCLUDED_TYPES`` (plus the
SQL helpers built from it). These tests pin the contract at each site so a
revert of any single filter reddens exactly that site's test:

  THRESHOLD   test_threshold_*                 (_get_tier_threshold count + cutoff)
  FTS         test_fts_site_*                  (_search_nodes keyword query)
  SEMANTIC    test_semantic_site_*             (_semantic_search, both callers)
  HINTS       test_hints_fts_site_*            (_similar_existing_matches FTS)
  MIN-IMP     test_min_queryable_*             (_get_min_queryable_importance)
  A/B         test_ab_block_*                  (engram_surface tier A/B block)
  KEEP        test_keep_*                      (traversal paths still see evidence)
"""

from __future__ import annotations

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
from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402,F401
import engram_query  # noqa: E402

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_evidence_out_of_search_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_NOW = "2026-01-01T00:00:00"
_EMB = json.dumps([1.0, 0.0, 0.0])


def _fresh():
    """Reload the module family on a clean DB; return (core, qry)."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_query)
    core = sys.modules["engram_core"]
    qry = importlib.import_module("engram_query")
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core, qry


def _insert(conn, nid, ntype, claim, importance, embedding=_EMB):
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, is_current, status, "
        "embedding, confidence, importance_score, quote_type, metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (nid, ntype, claim, _NOW, 1, None, embedding,
         None if ntype == "evidence" else 0.85, importance,
         None if ntype == "evidence" else "hard_data", "{}"),
    )


def _patch_memory(core, **mem):
    base = {"decay_base": 1.014, "current_turn": 0, "tier2_max_nodes": 1000}
    base.update(mem)
    core._get_memory_config = lambda: dict(base)


class _MockEmbedder:
    """is_available()=True, every text embeds to [1,0,0] (slow-path cosine)."""

    def is_available(self):
        return True

    def embed(self, text, model_name):
        return [1.0, 0.0, 0.0]

    @staticmethod
    def cosine_similarity(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


# ---------------------------------------------------------------------------
# THRESHOLD — tier-2 count and cutoff ranking exclude evidence
# ---------------------------------------------------------------------------

def _threshold_fixture(core):
    """3 searchable (0.9/0.8/0.7) + 3 evidence (0.95/0.85/0.5)."""
    conn = core._get_db()
    try:
        for nid, imp in (("ob_a", 0.9), ("ob_b", 0.8), ("ob_c", 0.7)):
            _insert(conn, nid, "observation_factual", f"searchable {nid}", imp)
        for nid, imp in (("ev_a", 0.95), ("ev_b", 0.85), ("ev_c", 0.5)):
            _insert(conn, nid, "evidence", f"evidence {nid}", imp)
        conn.commit()
    finally:
        conn.close()


def test_threshold_count_excludes_evidence():
    """cap between N and N+M: threshold is 0.0 -- evidence is not counted."""
    core, _ = _fresh()
    _threshold_fixture(core)
    _patch_memory(core, tier2_max_nodes=5)  # N=3 <= 5 < N+M=6
    conn = core._get_db()
    try:
        assert core._get_tier_threshold(conn, 2) == 0.0
    finally:
        conn.close()


def test_threshold_count_boundary_cap_equals_searchable():
    """cap == N searchable: still 0.0. Counting evidence would push the total
    over the cap and return a real cutoff (0.7) even though every searchable
    node fits -- this is the case that isolates the COUNT from the ranking."""
    core, _ = _fresh()
    _threshold_fixture(core)
    _patch_memory(core, tier2_max_nodes=3)
    conn = core._get_db()
    try:
        assert core._get_tier_threshold(conn, 2) == 0.0
    finally:
        conn.close()


def test_threshold_cutoff_ranks_without_evidence():
    """cap below N: cutoff is the cap-th SEARCHABLE score; a higher-scoring
    evidence node must not shift it (old behaviour would return 0.9)."""
    core, _ = _fresh()
    _threshold_fixture(core)
    _patch_memory(core, tier2_max_nodes=2)
    conn = core._get_db()
    try:
        assert core._get_tier_threshold(conn, 2) == 0.8
    finally:
        conn.close()


def test_threshold_max_nodes_param_overrides_config():
    """The optional max_nodes arg is the shared function's test-cap hook."""
    core, _ = _fresh()
    _threshold_fixture(core)
    _patch_memory(core, tier2_max_nodes=1000)
    conn = core._get_db()
    try:
        assert core._get_tier_threshold(conn, 2) == 0.0
        assert core._get_tier_threshold(conn, 2, max_nodes=2) == 0.8
        assert core._get_tier_threshold(conn, 2, max_nodes=1) == 0.9
        assert core._get_tier_threshold(conn, 2, max_nodes=3) == 0.0
        assert core._get_tier_threshold(conn, 3, max_nodes=1) == 0.0
    finally:
        conn.close()


def test_searchable_rule_is_single_named_constant():
    """The rule is defined once: evidence is in the constant, observations are not."""
    core, _ = _fresh()
    assert "evidence" in core.SEARCH_EXCLUDED_TYPES
    assert "observation_factual" not in core.SEARCH_EXCLUDED_TYPES
    conn = core._get_db()
    try:
        _threshold_fixture(core)
        n = conn.execute(
            f"SELECT COUNT(*) FROM nodes WHERE {core._searchable_where_sql()}"
        ).fetchone()[0]
        assert n == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FTS site — _search_nodes keyword path
# ---------------------------------------------------------------------------

def test_fts_site_excludes_evidence():
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_fts", "observation_factual",
                "zorblax calibration reading from the lab", 0.5)
        _insert(conn, "ev_fts", "evidence",
                "zorblax calibration reading from the lab", 0.5)
        conn.commit()
        res = qry._search_nodes(conn, "zorblax calibration", use_semantic=False)
        ids = {n["id"] for n in res["results"]}
        assert "ob_fts" in ids
        assert "ev_fts" not in ids
    finally:
        conn.close()


def test_fts_site_explicit_evidence_type_filter_still_excluded():
    """Even an explicit types=evidence search returns no evidence (the
    listing path for that is engram_list)."""
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ev_fts2", "evidence", "quuxmarker evidence body", 0.5)
        conn.commit()
        res = qry._search_nodes(conn, "quuxmarker", types="evidence",
                                use_semantic=False)
        assert res["results"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SEMANTIC site — _semantic_search (and engram_query end to end)
# ---------------------------------------------------------------------------

def test_semantic_site_excludes_evidence():
    core, _ = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_sem", "observation_factual", "semantic body one", 0.5)
        _insert(conn, "ev_sem", "evidence", "semantic body two", 0.5)
        conn.commit()
    finally:
        conn.close()
    orig = core._embedder
    core._embedder = _MockEmbedder()
    try:
        conn = core._get_db()
        try:
            ids = {r["id"] for r in core._semantic_search(
                conn, "anything", top_k=10, min_similarity=0.5)}
            assert "ob_sem" in ids
            assert "ev_sem" not in ids
            # include_superseded=True takes the other SQL branch of the slow path.
            ids2 = {r["id"] for r in core._semantic_search(
                conn, "anything", top_k=10, min_similarity=0.5,
                include_superseded=True)}
            assert "ob_sem" in ids2
            assert "ev_sem" not in ids2
        finally:
            conn.close()
    finally:
        core._embedder = orig


def test_semantic_site_knn_fast_path_excludes_evidence():
    """The sqlite-vec KNN fast path (384-dim, the production path) excludes
    evidence too -- the 3-dim tests above only exercise the slow path."""
    import pytest
    core, _ = _fresh()
    if not core._VEC_BACKEND_AVAILABLE:
        pytest.skip("sqlite-vec not available")
    sqlite_vec = core._sqlite_vec
    vec = [1.0] + [0.0] * 383
    emb384 = json.dumps(vec)
    conn = core._get_db()
    try:
        _insert(conn, "ob_knn", "observation_factual", "knn body one", 0.5, emb384)
        _insert(conn, "ev_knn", "evidence", "knn body two", 0.5, emb384)
        for nid in ("ob_knn", "ev_knn"):
            conn.execute(
                "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                (nid, sqlite_vec.serialize_float32(vec)))
        conn.commit()
    except Exception as e:  # vec_nodes absent in this build
        conn.close()
        pytest.skip(f"vec_nodes unavailable: {e}")

    class _Emb384(_MockEmbedder):
        def embed(self, text, model_name):
            return vec

    orig = core._embedder
    core._embedder = _Emb384()
    try:
        ids = {r["id"] for r in core._semantic_search(
            conn, "anything", top_k=10, min_similarity=0.5)}
        assert "ob_knn" in ids  # proves the fast path actually ran and matched
        assert "ev_knn" not in ids
    finally:
        core._embedder = orig
        conn.close()


def test_semantic_site_engram_query_end_to_end():
    """engram_query: query shares no FTS token with either node (semantic-only
    path); the evidence node is never returned."""
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_e2e", "observation_factual", "apple orchard harvest", 0.5)
        _insert(conn, "ev_e2e", "evidence", "banana plantation yield", 0.5)
        conn.commit()
    finally:
        conn.close()
    orig = core._embedder
    core._embedder = _MockEmbedder()
    try:
        out = json.loads(qry._query_impl(query="qqqnomatch", top_k=10,
                                         return_debug=True))
        ids = {n["id"] for n in out.get("results", [])}
        assert "ob_e2e" in ids
        assert "ev_e2e" not in ids
    finally:
        core._embedder = orig


def test_engram_surface_never_surfaces_evidence():
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_srf", "observation_factual",
                "wibblefrop quarterly metric observed", 0.5)
        _insert(conn, "ev_srf", "evidence",
                "wibblefrop quarterly metric observed", 0.5)
        conn.commit()
    finally:
        conn.close()
    out = qry._surface_impl(query="wibblefrop quarterly metric", top_k=10,
                            semantic=False)
    assert "ob_srf" in out
    assert "ev_srf" not in out


# ---------------------------------------------------------------------------
# HINTS site — add_* similar_existing (FTS leg, embeddings off)
# ---------------------------------------------------------------------------

def test_hints_fts_site_never_lists_evidence():
    """The helper is handed a type_filter that even INCLUDES evidence; the
    FTS leg must still not list an evidence node."""
    core, _ = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_hnt", "observation_factual",
                "gloopnitz regression in nightly build", 0.5)
        _insert(conn, "ev_hnt", "evidence",
                "gloopnitz regression in nightly build", 0.5)
        conn.commit()
        matches = core._similar_existing_matches(
            conn, "gloopnitz regression nightly",
            type_filter={"observation_factual", "evidence"},
        )
        ids = {m["id"] for m in matches}
        assert "ob_hnt" in ids
        assert "ev_hnt" not in ids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MIN-IMP site — normalization anchor is over the searchable population
# ---------------------------------------------------------------------------

def test_min_queryable_importance_ignores_evidence():
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_mi", "observation_factual", "min importance obs", 0.4)
        _insert(conn, "ev_mi", "evidence", "min importance ev", 0.01)
        conn.commit()
        assert qry._get_min_queryable_importance(conn, tier2_threshold=0.0) == 0.4
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A/B block — must agree with the shared threshold function
# ---------------------------------------------------------------------------

def test_ab_block_threshold_equals_shared_function():
    """Fixture where evidence WOULD change the answer: test cap 2 -> shared
    function gives 0.8 (searchable only); a duplicated all-types computation
    would give 0.9."""
    core, qry = _fresh()
    _threshold_fixture(core)
    conn = core._get_db()
    try:
        # One more matching searchable node so the surface query has results.
        _insert(conn, "ob_ab", "observation_factual",
                "ababmarker findable observation", 0.6)
        conn.commit()
    finally:
        conn.close()
    _patch_memory(core, tier2_max_nodes=1000,
                  test={"enabled": True, "tier2_max_nodes": 2})
    out = json.loads(qry._surface_impl(query="ababmarker findable",
                                       top_k=10, semantic=False))
    ab = out["tier_ab"]
    conn = core._get_db()
    try:
        expected = core._get_tier_threshold(conn, 2, max_nodes=2)
    finally:
        conn.close()
    assert expected == 0.8  # 4 searchable now: 0.9/0.8/0.7/0.6 -> 2nd = 0.8
    assert ab["test"]["threshold"] == round(expected, 4)
    # total_searchable reports the searchable population (evidence excluded).
    assert ab["base"]["total_searchable"] == 4


# ---------------------------------------------------------------------------
# Stats / reflect / continuity report the searchable population
# ---------------------------------------------------------------------------

def test_recall_set_continuity_set_excludes_evidence():
    core, _ = _fresh()
    _threshold_fixture(core)
    _patch_memory(core, tier2_max_nodes=1000)
    conn = core._get_db()
    try:
        res = core._compute_recall_set_continuity(conn, "nap")
        assert res["set_size"] == 3
        assert not any(i.startswith("ev_") for i in res["tier2_ids"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# KEEP — traversal paths still see evidence (and still restamp)
# ---------------------------------------------------------------------------

def _evidence_fixture(core):
    conn = core._get_db()
    try:
        _insert(conn, "ob_keep", "observation_factual", "keep observation", 0.5)
        _insert(conn, "ev_keep", "evidence", "keep evidence body", 0.5)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?,?,?,?)", ("ob_keep", "ev_keep", "cites", _NOW))
        conn.commit()
    finally:
        conn.close()


def test_keep_inspect_evidence_works_and_restamps():
    core, qry = _fresh()
    _evidence_fixture(core)
    out = json.loads(qry._inspect_impl("ev_keep"))
    assert "error" not in out, out
    conn = core._get_db()
    try:
        rc = conn.execute(
            "SELECT recall_count FROM nodes WHERE id='ev_keep'").fetchone()[0]
        assert (rc or 0) >= 1
    finally:
        conn.close()


def test_keep_subgraph_reaches_evidence_and_restamps():
    core, qry = _fresh()
    _evidence_fixture(core)
    out = qry._subgraph_impl("ob_keep", depth=1)
    assert "ev_keep" in out
    conn = core._get_db()
    try:
        rc = conn.execute(
            "SELECT recall_count FROM nodes WHERE id='ev_keep'").fetchone()[0]
        assert (rc or 0) >= 1
    finally:
        conn.close()


def test_keep_list_types_evidence_still_lists_evidence():
    core, qry = _fresh()
    _evidence_fixture(core)
    out = json.loads(qry._list_impl(node_type="evidence"))
    assert "ev_keep" in json.dumps(out)


# ---------------------------------------------------------------------------
# KNN FILL (round 2) -- evidence neighbours must not take KNN slots away from
# searchable nodes: the semantic leg still returns top_k when that many exist.
# ---------------------------------------------------------------------------

def _knn_fill_fixture(core, n_evidence, n_obs, obs_base=0.3):
    """n_evidence evidence rows NEARER the query than n_obs observations.

    Evidence sit at offsets 0.001..0.001*n_evidence; the default obs_base=0.3
    interleaves observations among a large evidence block, so a fixture that
    needs EVERY evidence row nearer than every observation passes obs_base
    above 0.001*n_evidence."""
    import pytest
    if not core._VEC_BACKEND_AVAILABLE:
        pytest.skip("sqlite-vec not available")
    sqlite_vec = core._sqlite_vec
    conn = core._get_db()

    def vec_at(offset):
        return [1.0, offset] + [0.0] * 382

    try:
        for i in range(n_evidence):
            v = vec_at(0.001 * (i + 1))  # very close to the query [1, 0, ...]
            _insert(conn, f"ev_fill{i:03d}", "evidence", f"fill ev {i}", 0.5, json.dumps(v))
            conn.execute("INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                         (f"ev_fill{i:03d}", sqlite_vec.serialize_float32(v)))
        for i in range(n_obs):
            v = vec_at(obs_base + 0.01 * i)  # further out, still well above min_similarity
            _insert(conn, f"ob_fill{i:03d}", "observation_factual", f"fill ob {i}", 0.5, json.dumps(v))
            conn.execute("INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                         (f"ob_fill{i:03d}", sqlite_vec.serialize_float32(v)))
        conn.commit()
    except Exception as e:  # vec_nodes absent in this build
        conn.close()
        pytest.skip(f"vec_nodes unavailable: {e}")
    return conn, vec_at(0.0)


def _run_knn(core, conn, qvec, **kw):
    class _Emb384(_MockEmbedder):
        def embed(self, text, model_name):
            return qvec

    orig = core._embedder
    core._embedder = _Emb384()
    try:
        return core._semantic_search(conn, "anything", min_similarity=0.5, **kw)
    finally:
        core._embedder = orig


def test_semantic_knn_fill_returns_top_k_when_nearest_are_evidence():
    """CONTRACT: nearest 60 neighbours are evidence (> the 40-row overfetch);
    the result must still hold top_k=5 searchable nodes (8 exist)."""
    core, _ = _fresh()
    conn, qvec = _knn_fill_fixture(core, n_evidence=60, n_obs=8)
    try:
        res = _run_knn(core, conn, qvec, top_k=5)
        ids = [r["id"] for r in res]
        assert len(ids) == 5, ids
        assert all(i.startswith("ob_fill") for i in ids), ids
        sims = [r["similarity"] for r in res]
        assert sims == sorted(sims, reverse=True)
    finally:
        conn.close()


def test_semantic_knn_fill_returns_all_when_fewer_than_top_k_exist():
    """Candidates run out: 3 searchable exist, top_k=5 -> exactly those 3, no error."""
    core, _ = _fresh()
    conn, qvec = _knn_fill_fixture(core, n_evidence=60, n_obs=3)
    try:
        res = _run_knn(core, conn, qvec, top_k=5)
        assert sorted(r["id"] for r in res) == ["ob_fill000", "ob_fill001", "ob_fill002"]
    finally:
        conn.close()


class _KnnCountingConn:
    """Delegating proxy: counts the vec_nodes KNN rounds the search runs."""

    def __init__(self, conn):
        self._conn = conn
        self.knn_rounds = 0

    def execute(self, sql, *a, **k):
        if "vec_nodes" in sql:
            self.knn_rounds += 1
        return self._conn.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_semantic_knn_fill_stops_at_min_similarity():
    """Widening stops at min_similarity. The fixture makes `past_floor` the
    ONLY stopping reason: the first page (k=40) is FULL (52 rows exist, so not
    exhausted) and its farthest row is already below the floor, while fewer
    than top_k searchable rows were found. A far searchable node never leaks in
    and exactly ONE KNN round runs; with the past_floor break disabled the
    search widens a second round (k=160, now exhausted)."""
    core, _ = _fresh()
    conn, qvec = _knn_fill_fixture(core, n_evidence=30, n_obs=2)
    try:
        for i in range(20):
            far = [0.0, 0.0, 1.0, 0.001 * i] + [0.0] * 380  # ~orthogonal to the query
            _insert(conn, f"ob_far{i:02d}", "observation_factual", f"far node {i}", 0.5,
                    json.dumps(far))
            conn.execute("INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                         (f"ob_far{i:02d}", core._sqlite_vec.serialize_float32(far)))
        conn.commit()
        counting = _KnnCountingConn(conn)
        res = _run_knn(core, counting, qvec, top_k=5)
        assert [r["id"] for r in res] == ["ob_fill000", "ob_fill001"]
        assert counting.knn_rounds == 1
    finally:
        conn.close()


def test_semantic_knn_fill_widens_through_rounds_three_and_four():
    """Rounds 3-4 of the widening are pinned. 700 evidence rows are all nearer
    than the 8 observations: with top_k=5 the k=40 and k=160 rounds find no
    searchable row, the k=640 round (640 < 700 evidence) still finds none, and
    only the k=2560 round reaches the observations. A `_KNN_MAX_K` of 160 (stop
    after round 2) or a `_KNN_MAX_ROUNDS` of 2 leaves the result short."""
    core, _ = _fresh()
    conn, qvec = _knn_fill_fixture(core, n_evidence=700, n_obs=8, obs_base=0.9)
    try:
        counting = _KnnCountingConn(conn)
        res = _run_knn(core, counting, qvec, top_k=5)
        assert len(res) == 5, [r["id"] for r in res]
        assert all(r["id"].startswith("ob_fill") for r in res)
        assert counting.knn_rounds >= 3, counting.knn_rounds
    finally:
        conn.close()


# --- engram_query: an all-excluded / partly-excluded `types` says so (round 4) ---

def _query_note_fixture():
    core, qry = _fresh()
    conn = core._get_db()
    try:
        _insert(conn, "ob_note", "observation_factual", "apple orchard harvest", 0.5)
        _insert(conn, "ev_note", "evidence", "apple orchard ledger", 0.5)
        conn.commit()
    finally:
        conn.close()
    return core, qry


def test_query_types_all_excluded_returns_empty_with_note():
    """CONTRACT: types made up ONLY of SEARCH_EXCLUDED_TYPES -> no results and a
    `note` that says evidence is never a search result and points at
    engram_list(node_type=evidence). Same for the return_debug shape."""
    core, qry = _query_note_fixture()
    for debug in (False, True):
        out = json.loads(qry._query_impl(query="apple orchard", types="evidence",
                                         top_k=10, return_debug=debug))
        assert out["results"] == []
        note = out.get("note", "")
        assert "evidence" in note, out
        assert "engram_list" in note and "node_type=evidence" in note, note


def test_query_types_mixed_searches_the_rest_and_names_ignored_types():
    """CONTRACT: excluded + searchable types -> the searchable ones are searched
    normally AND a note names the excluded type(s) that were ignored."""
    core, qry = _query_note_fixture()
    for debug in (False, True):
        out = json.loads(qry._query_impl(
            query="apple orchard", types="evidence, observation_factual",
            top_k=10, return_debug=debug))
        ids = [n["id"] for n in out["results"]]
        assert ids == ["ob_note"], ids
        note = out.get("note", "")
        assert "evidence" in note and "ignored" in note, out


def test_query_ordinary_types_has_no_note_key():
    """CONTRACT: no note without an excluded type -- neither for an empty
    `types` nor for a searchable-only one."""
    core, qry = _query_note_fixture()
    for types in ("", "observation_factual"):
        out = json.loads(qry._query_impl(query="apple orchard", types=types, top_k=10))
        assert [n["id"] for n in out["results"]] == ["ob_note"]
        assert "note" not in out, out


def test_query_note_is_built_from_search_excluded_types():
    """The note names the types from SEARCH_EXCLUDED_TYPES, not a literal: with
    a second excluded type patched in, it appears in the note."""
    core, qry = _query_note_fixture()
    core.SEARCH_EXCLUDED_TYPES = frozenset({"evidence", "zzz_probe_type"})
    out = json.loads(qry._query_impl(query="apple orchard",
                                     types="evidence,zzz_probe_type", top_k=10))
    assert out["results"] == []
    assert "zzz_probe_type" in out.get("note", ""), out
