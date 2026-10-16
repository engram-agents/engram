"""#3458 half C: _surface_impl exposes per-candidate ranking signals additively.

Contract (stated by the dispatching spec):
  S1  every matched_meta / top_claims / special_nodes entry carries `signals`
      = {similarity, importance_score, utility, composite_score, match_type,
      recall_turn, ranked (#3551)}, each EQUAL to the ranked row's value (`utility` is read from
      the row's `utility_score`); top-level `signals_present` lists the names
      that were non-null on at least one candidate.
  S2  a row lacking a key -> that signal is null on that entry; the name is
      absent from `signals_present` when no candidate has it.
  S3  additive only: with `signals` / `signals_present` deleted, the response is
      byte-identical to the pre-change golden for the same fixture rows.
  S4  the surface daemon passes the fields through unchanged.

_search_nodes is stubbed with fixture rows so the signals under test are the
rows' own values (the equality cells compare against the rows, not constants).
"""

import atexit
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_surface_signals_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

SIGNAL_NAMES = ["similarity", "importance_score", "utility",
                "composite_score", "match_type", "recall_turn", "ranked"]
# signal name -> row key it is read from
_ROW_KEY = {"utility": "utility_score"}


def _fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    # Seed one real node so the DB file exists (_surface_impl opens it even
    # though _search_nodes is stubbed in these cells).
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/surface-signals-seed",
        "title": "Seed Source",
        "quoted_text": "Surface signals seed claim verbatim.",
        "interpretation": "Seed interpretation for the surface signals test.",
        "claim": "Surface signals seed claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, r
    return server


def _rows(drop=()):
    """Rows carrying all seven signals (distinct values per row/signal)."""
    base = [
        {"id": "ob_0001", "type": "observation_factual", "claim": "claim one",
         "confidence": 0.85, "created_at": "2026-10-01T00:00:00+00:00",
         "recall_summary": "s1", "recall_keywords": json.dumps(["k1"]),
         "similarity": 0.81, "importance_score": 2.5, "utility_score": 0.33,
         "composite_score": 1.7, "match_type": "semantic", "recall_turn": 7,
         "ranked": True},
        {"id": "ob_0002", "type": "observation_factual", "claim": "claim two",
         "confidence": 0.7, "created_at": "2026-10-02T00:00:00+00:00",
         "recall_summary": "s2", "recall_keywords": json.dumps(["k2"]),
         "similarity": 0.42, "importance_score": 1.25, "utility_score": 0.11,
         "composite_score": 0.9, "match_type": "keyword", "recall_turn": 3,
         "ranked": True},
        {"id": "ls_0001", "type": "lesson", "claim": "a lesson",
         "created_at": "2026-10-03T00:00:00+00:00",
         "recall_summary": "s3", "recall_keywords": json.dumps(["k3"]),
         "similarity": 0.66, "importance_score": 3.75, "utility_score": 0.55,
         "composite_score": 2.2, "match_type": "semantic", "recall_turn": 9,
         "ranked": False},
    ]
    for r in base:
        for k in drop:
            r.pop(k, None)
    return base


def _search_result(rows):
    return {"results": rows, "keyword_matches": 1, "semantic_matches": 2,
            "semantic_available": False}


def _surface(server, rows):
    """Run the real _surface_impl over stubbed ranked rows; clock frozen."""
    eq = sys.modules["engram_query"]
    with patch.object(eq, "_search_nodes", return_value=_search_result(copy.deepcopy(rows))), \
         patch.object(eq.core, "_humanized_ago", return_value="AGO"), \
         patch.object(eq.core, "_created_minutes_ago", return_value=1):
        return json.loads(server._surface_impl(query="q", top_k=5, semantic=False))


def _expected(row):
    return {n: row.get(_ROW_KEY.get(n, n)) for n in SIGNAL_NAMES}


def _entries(resp):
    return (resp["matched_meta"], resp["top_claims"], resp["special_nodes"])


def _strip(resp):
    r = copy.deepcopy(resp)
    r.pop("signals_present", None)
    for section in _entries(r):
        for e in section:
            e.pop("signals", None)
    return r


def test_signals_equal_row_values_on_every_entry():
    server = _fresh_server()
    rows = _rows()
    by_id = {r["id"]: r for r in rows}
    resp = _surface(server, rows)
    seen = 0
    for section in _entries(resp):
        assert section, "section unexpectedly empty"
        for e in section:
            sig = e["signals"]
            assert set(sig) == set(SIGNAL_NAMES)
            assert sig == _expected(by_id[e["id"]])
            assert all(v is not None for v in sig.values())
            seen += 1
    assert seen == 3 + 2 + 1  # 3 meta, 2 top_claims, 1 special
    assert sorted(resp["signals_present"]) == sorted(SIGNAL_NAMES)


def test_missing_recall_turn_is_null_and_not_present():
    server = _fresh_server()
    resp = _surface(server, _rows(drop=("recall_turn",)))
    for section in _entries(resp):
        for e in section:
            assert e["signals"]["recall_turn"] is None
            assert e["signals"]["similarity"] is not None
    assert "recall_turn" not in resp["signals_present"]
    assert "similarity" in resp["signals_present"]


def test_signal_present_if_non_null_on_at_least_one_candidate():
    server = _fresh_server()
    rows = _rows()
    del rows[0]["recall_turn"]
    resp = _surface(server, rows)
    assert "recall_turn" in resp["signals_present"]
    meta = {e["id"]: e for e in resp["matched_meta"]}
    assert meta["ob_0001"]["signals"]["recall_turn"] is None
    assert meta["ob_0002"]["signals"]["recall_turn"] == 3


def test_existing_keys_byte_identical_to_pre_change_golden():
    server = _fresh_server()
    resp = _surface(server, _rows())
    assert json.dumps(_strip(resp), sort_keys=True) == json.dumps(GOLDEN, sort_keys=True)


def test_daemon_reply_carries_signals():
    server = _fresh_server()
    daemon_path = _ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
    sys.path.insert(0, str(daemon_path.parent))
    spec = importlib.util.spec_from_file_location("engram_surface_daemon_3458", str(daemon_path))
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)

    rows = _rows()
    by_id = {r["id"]: r for r in rows}
    resp_direct = _surface(server, rows)

    class _FakeClient:
        def call(self, tool, args):
            assert tool == "engram_surface"
            return resp_direct

    class _Conn:
        def __init__(self):
            self.sent = b""
            self._req = (json.dumps({"query": "q", "top_k": 5}) + "\n").encode()

        def recv(self, n):
            d, self._req = self._req, b""
            return d

        def sendall(self, b):
            self.sent += b

        def close(self):
            pass

    conn = _Conn()
    with patch.object(daemon, "_client", _FakeClient(), create=True):
        daemon.handle_client(conn, None)
    reply = json.loads(conn.sent.decode().strip())
    assert reply["status"] == "ok"
    result = reply["result"]
    assert sorted(result["signals_present"]) == sorted(SIGNAL_NAMES)
    for e in result["matched_meta"]:
        assert e["signals"] == _expected(by_id[e["id"]])


def _real_server_two_nodes():
    """Fresh server whose DB holds two keyword-matchable observations."""
    server = _fresh_server()
    for i, claim in enumerate(("Zymurgy fermentation yields ethanol.",
                               "Zymurgy brewing needs yeast cultures.")):
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": f"https://example.com/zymurgy-{i}",
            "title": f"Zymurgy Source {i}",
            "quoted_text": claim,
            "interpretation": "Zymurgy interpretation text for ranking.",
            "claim": claim,
            "quote_type": "hard_data",
        })))
        assert "observation_id" in r, r
    return server


def _debug_search(server):
    conn = server._get_db()
    try:
        return server._search_nodes(conn, "zymurgy", use_semantic=False,
                                    top_k=5, return_debug=True)
    finally:
        conn.close()


def test_search_rows_composite_equals_debug_composite():
    """Each row's composite_score == the debug per_result composite for that id."""
    res = _debug_search(_real_server_two_nodes())
    dbg = {d["id"]: d["composite_score"] for d in res["debug"]["per_result"]}
    generic = [r for r in res["results"] if r["id"] in dbg]
    assert len(generic) >= 2, res["results"]
    for row in generic:
        assert isinstance(row["composite_score"], float)
        assert row["composite_score"] == dbg[row["id"]]


def test_search_rows_composite_set_and_order_match_debug():
    """Row composites are the debug list's set; top-1 is the max; order non-increasing."""
    res = _debug_search(_real_server_two_nodes())
    dbg = [d["composite_score"] for d in res["debug"]["per_result"]]
    got = [r["composite_score"] for r in res["results"]
           if r.get("composite_score") is not None]
    assert sorted(got) == sorted(dbg)
    assert got[0] == max(dbg)
    # no embeddings -> MMR reduces to composite order
    assert got == sorted(got, reverse=True)


def test_signals_present_includes_composite_on_stubbed_and_real_runs():
    resp = _surface(_fresh_server(), _rows())
    assert "composite_score" in resp["signals_present"]

    server = _real_server_two_nodes()
    real = json.loads(server._surface_impl(query="zymurgy", top_k=5, semantic=False))
    assert real["matched_meta"], real
    assert "composite_score" in real["signals_present"]
    for e in real["matched_meta"]:
        assert isinstance(e["signals"]["composite_score"], float)


# Pre-change response for _rows() with the clock frozen, `signals` absent,
# captured from the code at base ae2b69a7 before this change was implemented.
GOLDEN = json.loads(r"""
{
 "age": {
  "current_turn": 0,
  "not_recalled_recently": [],
  "recall_turn_range": [
   3,
   9
  ],
  "wall_clock_range": {
   "newest_ago": "AGO",
   "oldest_ago": "AGO"
  }
 },
 "hint": "Use engram_inspect(node_id) for full detail on a specific node, or engram_get_subgraph(node_id) for full evidence chain.",
 "issues": {},
 "match_count": 3,
 "matched_ids": [
  "ob_0001",
  "ob_0002",
  "ls_0001"
 ],
 "matched_meta": [
  {
   "created_at": "2026-10-01T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ob_0001",
   "recall_keywords": [
    "k1"
   ],
   "type": "observation_factual"
  },
  {
   "created_at": "2026-10-02T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ob_0002",
   "recall_keywords": [
    "k2"
   ],
   "type": "observation_factual"
  },
  {
   "created_at": "2026-10-03T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ls_0001",
   "recall_keywords": [
    "k3"
   ],
   "type": "lesson"
  }
 ],
 "query": "q",
 "search_methods": {
  "keyword_matches": 1,
  "semantic_available": false,
  "semantic_matches": 2
 },
 "special_nodes": [
  {
   "claim": "a lesson",
   "created_ago": "AGO",
   "created_at": "2026-10-03T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ls_0001",
   "recall_keywords": [
    "k3"
   ],
   "recall_summary": "s3",
   "type": "lesson"
  }
 ],
 "top_claims": [
  {
   "claim": "claim one",
   "confidence": 0.85,
   "created_ago": "AGO",
   "created_at": "2026-10-01T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ob_0001",
   "recall_keywords": [
    "k1"
   ],
   "recall_summary": "s1",
   "type": "observation_factual"
  },
  {
   "claim": "claim two",
   "confidence": 0.7,
   "created_ago": "AGO",
   "created_at": "2026-10-02T00:00:00+00:00",
   "created_minutes_ago": 1,
   "id": "ob_0002",
   "recall_keywords": [
    "k2"
   ],
   "recall_summary": "s2",
   "type": "observation_factual"
  }
 ],
 "type_counts": {
  "lesson": 1,
  "observation_factual": 2
 }
}
""")
