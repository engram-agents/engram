"""Contract-first tests: route ledger `ranked` flag + hook-produced `in_context` (#3551).

Contract (stated by the dispatching spec):

  A. `ranked` (producer, engram_query): every candidate row in the surface
     response carries a boolean `ranked` inside its `signals` dict: True for
     rows that went through composite ranking, False for special-type bypass
     rows (core.SPECIAL_TYPES_BYPASS). Never null, never derived from
     `composite_score is None`. Ranked rows have a non-null composite_score.
  A2. build_route_candidates: a candidate with `ranked is False` does NOT list
     its null `composite_score` in `missing_signals`; a `ranked is True` row
     with null composite IS still listed (the defect the contract guards).
  B. `in_context` + `in_context_why` (hook, at ledger-write time): True when ANY
     of `shown` (id in the `shown` list of an EARLIER line of this session's
     routes/*.jsonl), `created` (created_at >= session started_at, no minutes
     window), `focused` (focused_at not null and is_current) holds; `why` names
     the FIRST that fired in that order. None of the three -> exactly False,
     why None. A source that cannot be read and no readable source fired ->
     None (unknown, not false) + "in_context" in `missing_signals`; a readable
     source that fired wins (True + its why).
  C4. "earlier means earlier": an id only in the line being written is not
     `shown`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))

SESSION = "sess-3551"
STARTED = "2026-10-05T00:00:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"
NEW = "2026-10-06T00:00:00+00:00"


def _id(n):
    # built, never spelled: the repo's no-new-shipped-node-ids gate scans tests too
    return f"ob_{n}"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "engram"
    eh.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    yield


def _hooklib():
    spec = importlib.util.spec_from_file_location("_hooklib_3551", HOOK_DIR / "_hooklib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- A: ranked ---

def _real_mixed_server():
    """Fresh server whose DB holds two observations (ranked path) and one
    definition (a SPECIAL_TYPES_BYPASS type), all keyword-matchable."""
    tests_dir = str(Path(__file__).resolve().parent)
    sys.path.insert(0, tests_dir)
    try:
        import test_surface_signals as tss
    finally:
        sys.path.remove(tests_dir)
    server = tss._real_server_two_nodes()
    r = json.loads(server.engram_add_definition(payload_json=json.dumps({
        "term": "Zymurgy",
        "definition": "Zymurgy is the study of fermentation in brewing.",
    })))
    assert "error" not in r, r
    return server


def _entries(resp):
    return [e for sec in (resp["matched_meta"], resp["top_claims"], resp["special_nodes"])
            for e in sec]


def test_c1_ranked_flag_true_for_ranked_false_for_bypass_real_surface():
    server = _real_mixed_server()
    resp = json.loads(server._surface_impl(query="zymurgy", top_k=5, semantic=False))
    meta = {e["id"]: e for e in resp["matched_meta"]}
    types = {i: e["type"] for i, e in meta.items()}
    assert "definition" in types.values() and "observation_factual" in types.values(), types
    for e in _entries(resp):
        ranked = e["signals"]["ranked"]
        assert type(ranked) is bool, (e["id"], ranked)  # boolean, never null
        assert ranked is (e["type"] not in ("definition",)), (e["type"], ranked)
    assert "ranked" in resp["signals_present"]


def test_c1_null_count_of_composite_among_ranked_rows_is_zero():
    server = _real_mixed_server()
    resp = json.loads(server._surface_impl(query="zymurgy", top_k=5, semantic=False))
    ranked_rows = [e for e in resp["matched_meta"] if e["signals"]["ranked"] is True]
    bypass_rows = [e for e in resp["matched_meta"] if e["signals"]["ranked"] is False]
    assert ranked_rows and bypass_rows
    null_ranked = sum(1 for e in ranked_rows if e["signals"]["composite_score"] is None)
    assert null_ranked == 0
    # by design (not part of the fix): bypass rows are never composite-ranked
    assert all(e["signals"]["composite_score"] is None for e in bypass_rows)


# ------------------------------------------------- A2: missing_signals ---

def _sig(**kw):
    base = {"similarity": 0.5, "importance_score": 1.0, "utility": 0.1,
            "composite_score": 1.5, "match_type": "keyword", "recall_turn": 1,
            "ranked": True}
    base.update(kw)
    return base


def _meta(nid, **sig):
    return {"id": nid, "type": "observation_factual", "created_at": OLD,
            "signals": _sig(**sig)}


def test_c2_null_composite_on_unranked_rows_not_listed_missing():
    hl = _hooklib()
    resp = {"matched_ids": [_id(1), _id(2)], "matched_meta": [
        _meta(_id(1)),
        _meta(_id(2), composite_score=None, ranked=False, match_type="semantic"),
    ]}
    cands, missing = hl.build_route_candidates(resp)
    by = {c["id"]: c for c in cands}
    assert by[_id(2)]["composite_score"] is None
    assert by[_id(2)]["ranked"] is False  # False survives _signal_from
    assert by[_id(1)]["ranked"] is True
    assert "composite_score" not in missing


def test_c2_null_composite_on_ranked_row_is_still_missing():
    hl = _hooklib()
    resp = {"matched_ids": [_id(1), _id(2)], "matched_meta": [
        _meta(_id(1), composite_score=None, ranked=True),
        _meta(_id(2), composite_score=None, ranked=False),
    ]}
    _, missing = hl.build_route_candidates(resp)
    assert "composite_score" in missing


# ------------------------------------------------ B: in_context sources ---

def _make_home(*, started=STARTED, focused=(), db=True, marker=True):
    eh = Path(os.environ["ENGRAM_HOME"])
    if marker:
        (eh / "sessions").mkdir(exist_ok=True)
        (eh / "sessions" / f"{SESSION}.json").write_text(
            json.dumps({"session_id": SESSION, "started_at": started}))
    if db:
        conn = sqlite3.connect(eh / "knowledge.db")
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INT, focused_at TEXT)")
        conn.execute("INSERT INTO nodes VALUES (?,?,?)", (_id(900), 1, None))
        conn.execute("INSERT INTO nodes VALUES (?,?,?)", (_id(901), 0, "2026-10-06T00:00:00"))
        for nid in focused:
            conn.execute("INSERT INTO nodes VALUES (?,?,?)", (nid, 1, "2026-10-06T00:00:00"))
        conn.commit()
        conn.close()


def _probe(tmp_path) -> Path:
    d = tmp_path / "home" / ".engram" / "probe" / "mods-recall" / SESSION
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prior_line(tmp_path, route, shown):
    routes = _probe(tmp_path) / "routes"
    routes.mkdir(exist_ok=True)
    with open(routes / f"{route}.jsonl", "a") as f:
        f.write(json.dumps({"route": route, "shown": shown}) + "\n")


def _write(tmp_path, ids_created, *, shown=(), route="prompt_surface"):
    """Write ONE ledger line over candidates {id: created_at}; return its row."""
    hl = _hooklib()
    _probe(tmp_path)
    resp = {"matched_ids": list(ids_created), "matched_meta": [
        {"id": i, "type": "observation_factual", "created_at": c, "signals": _sig()}
        for i, c in ids_created.items()]}
    hl.write_route_ledger(route, SESSION, "fk", "q", resp, list(shown), {}, source="daemon")
    p = _probe(tmp_path) / "routes" / f"{route}.jsonl"
    return json.loads(p.read_text().splitlines()[-1])


def _cand(row, nid):
    return next(c for c in row["candidates"] if c["id"] == nid)


def test_c3_shown_only(tmp_path):
    _make_home()
    _prior_line(tmp_path, "tool_recall", [_id(1)])  # the OTHER route counts too
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "shown"


def test_c3_created_only(tmp_path):
    _make_home()
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "created"


def test_c3_created_at_exactly_equal_to_session_start_counts_as_created(tmp_path):
    """Boundary pin: the comparison is >=, so created_at == started_at fires."""
    _make_home()
    c = _cand(_write(tmp_path, {_id(1): STARTED}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "created"


def test_c3_focused_only(tmp_path):
    _make_home(focused=[_id(1)])
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "focused"


def test_c3_none_of_three_is_exactly_false_with_null_why(tmp_path):
    _make_home(focused=[_id(2)])
    _prior_line(tmp_path, "prompt_surface", [_id(3)])
    row = _write(tmp_path, {_id(1): OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is False  # not None, not merely falsy
    assert c["in_context_why"] is None
    assert "in_context" not in row["missing_signals"]


def test_c3_precedence_shown_beats_focused_and_created(tmp_path):
    _make_home(focused=[_id(1)])
    _prior_line(tmp_path, "prompt_surface", [_id(1)])
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "shown"


def test_c3_precedence_created_beats_focused(tmp_path):
    _make_home(focused=[_id(1)])
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context_why"] == "created"


def test_c3_non_current_node_with_focused_at_is_not_focused(tmp_path):
    _make_home()  # _id(901) has focused_at set but is_current = 0
    c = _cand(_write(tmp_path, {_id(901): OLD}), _id(901))
    assert c["in_context"] is False


def test_c4_id_only_in_the_line_being_written_is_not_shown(tmp_path):
    _make_home()
    row = _write(tmp_path, {_id(1): OLD}, shown=[_id(1)])
    assert row["shown"] == [_id(1)]
    c = _cand(row, _id(1))
    assert c["in_context"] is False and c["in_context_why"] is None
    # ...but the NEXT fire does see it as shown
    c2 = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c2["in_context_why"] == "shown"


def test_c4_malformed_earlier_line_is_skipped_not_raised(tmp_path, capsys):
    _make_home()
    routes = _probe(tmp_path) / "routes"
    routes.mkdir()
    (routes / "tool_recall.jsonl").write_text(
        "not json\n" + json.dumps({"shown": [_id(1)]}) + "\n" + json.dumps(["x"]) + "\n"
        + json.dumps({"shown": "oops"}) + "\n")
    row = _write(tmp_path, {_id(1): OLD})
    assert _cand(row, _id(1))["in_context_why"] == "shown"
    assert "route-ledger" not in capsys.readouterr().err


# ------------------------------------------------------ C5: unreadable ---

def test_c5_db_unreadable_and_nothing_else_fired_is_null_and_missing(tmp_path):
    _make_home(db=False)
    row = _write(tmp_path, {_id(1): OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and c["in_context_why"] is None
    assert "in_context" in row["missing_signals"]
    assert row["missing_signals"].count("in_context") == 1


def test_c5_db_unreadable_but_created_fired_is_true_created(tmp_path):
    _make_home(db=False)
    row = _write(tmp_path, {_id(1): NEW})
    c = _cand(row, _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "created"
    assert "in_context" not in row["missing_signals"]


def test_c5_marker_missing_and_nothing_else_fired_is_null(tmp_path):
    _make_home(marker=False)
    row = _write(tmp_path, {_id(1): NEW})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and "in_context" in row["missing_signals"]


def test_c5_marker_missing_but_focused_fired_is_true_focused(tmp_path):
    _make_home(marker=False, focused=[_id(1)])
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "focused"


def test_c5_unreadable_ledger_file_and_nothing_else_fired_is_null(tmp_path):
    _make_home()
    routes = _probe(tmp_path) / "routes"
    routes.mkdir()
    (routes / "tool_recall.jsonl").mkdir()  # a directory where a file must be: unreadable
    row = _write(tmp_path, {_id(1): OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and "in_context" in row["missing_signals"]


# ------------------------------- naive / malformed created_at (round 3) ---

NAIVE_NEW = "2026-10-06T00:00:00"  # no Z, no offset: naive means UTC
NAIVE_OLD = "2026-10-01T00:00:00"


def test_r3_naive_created_at_after_start_is_created_and_line_is_written(tmp_path):
    """A naive created_at vs the aware session start must not raise a TypeError
    (write_route_ledger's try/except would swallow it and drop the line)."""
    _make_home()
    row = _write(tmp_path, {_id(1): NAIVE_NEW})  # reads the written line back
    c = _cand(row, _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "created"
    assert (_probe(tmp_path) / "routes" / "prompt_surface.jsonl").read_text().strip()


def test_r3_naive_created_at_before_start_is_not_created(tmp_path):
    _make_home()
    row = _write(tmp_path, {_id(1): NAIVE_OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is False and c["in_context_why"] is None
    assert "in_context" not in row["missing_signals"]


@pytest.mark.parametrize("bad", ["not-a-date", None])
def test_r3_unparseable_created_at_and_nothing_else_fired_is_null_and_missing(tmp_path, bad):
    _make_home()
    row = _write(tmp_path, {_id(1): bad})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and c["in_context_why"] is None
    assert "in_context" in row["missing_signals"]


@pytest.mark.parametrize("bad", ["not-a-date", None])
def test_r3_unparseable_created_at_but_focused_is_true_focused(tmp_path, bad):
    _make_home(focused=[_id(1)])
    c = _cand(_write(tmp_path, {_id(1): bad}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "focused"


def test_r3_build_route_candidates_without_resolver_carries_in_context_why_none():
    hl = _hooklib()
    resp = {"matched_ids": [_id(1), _id(2)],
            "matched_meta": [_meta(_id(1)), _meta(_id(2))]}
    cands, missing = hl.build_route_candidates(resp)  # no resolver
    assert cands
    for c in cands:
        assert "in_context_why" in c, c
        assert c["in_context_why"] is None
    assert "in_context" in missing
