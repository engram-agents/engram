"""Contract-first tests: the route ledger (#3458 section A).

The surface hook (route `prompt_surface`) and the lesson-tripwire hook's
in-turn recall (route `tool_recall`) each append ONE JSON line per fire to
`<ENGRAM_HOME>/probe/mods-recall/<session_id>/routes/<route>.jsonl`, but only
when `<...>/<session_id>/` already exists (opt-in by presence: the probe is
live for that session). The ledger OBSERVES -- it never changes what a hook
renders, ranks or cuts, and a ledger failure never reaches stdout.

Cells: no probe dir -> nothing written; probe dir -> one parseable line whose
`shown` equals the ids parsed from the hook's own output; a response lacking a
signal -> null + missing_signals; unwritable routes -> stdout byte-identical +
one stderr line; tool_recall fire_key stability; file/dir modes.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))
from engram_idf import JUNK_STOPLIST as _REAL_JUNK_STOPLIST  # noqa: E402

HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
TRIPWIRE_PATH = HOOK_DIR / "engram-lesson-tripwire-hook.py"

SESSION = "sess-ledger-1"
SIGNALS = (
    "similarity", "importance_score", "utility", "composite_score",
    "match_type", "recall_turn", "created_at", "in_context",
)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored);
# module scope: later tests rely on the loaded engram_* modules staying imported (a fresh import re-prints the sqlite-vec notice).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")


def _ids(*nums):
    # built, never spelled: the repo's no-new-shipped-node-ids gate scans tests too
    return [f"ob_{n}" for n in nums]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "engram"
    eh.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    yield
    mod = sys.modules.get("engram_idf")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("engram_idf", None)


def _probe_root(tmp_path) -> Path:
    return tmp_path / "engram" / "probe" / "mods-recall"


def _live(tmp_path, session=SESSION) -> Path:
    d = _probe_root(tmp_path) / session
    d.mkdir(parents=True)
    return d


def _tree(tmp_path):
    root = _probe_root(tmp_path)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def _lines(tmp_path, route, session=SESSION):
    p = _probe_root(tmp_path) / session / "routes" / f"{route}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines()]


# ---------------------------------------------------------------- surface ---

def _load_surface(tag):
    spec = importlib.util.spec_from_file_location(f"surface_ledger_{tag}", SURFACE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surface_response(ids, *, extra=None, keywords=True, top_n=1):
    extra = extra or {}
    meta = []
    for nid in ids:
        m = {"id": nid, "type": "observation",
             "recall_keywords": ["kw"] if keywords else None,
             "created_at": "2026-10-01T00:00:00+00:00", "created_minutes_ago": 100}
        m.update(extra)
        meta.append(m)
    top = [
        {"id": nid, "type": "observation", "claim": f"claim {nid}", "confidence": 0.85,
         "created_at": "2026-10-01T00:00:00+00:00", "created_ago": "5d",
         "created_minutes_ago": 7200, "recall_summary": f"summary {nid}",
         "recall_keywords": ["kw"], **extra}
        for nid in ids[:top_n]
    ]
    return {"match_count": len(ids), "matched_ids": list(ids), "matched_meta": meta,
            "special_nodes": [], "top_claims": top, "age": {}, "issues": {}}


def _run_surface(mod, monkeypatch, response, *, prompt="how do we rank recall?", daemon=True,
                 session=SESSION):
    monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: response if daemon else None)
    monkeypatch.setattr(mod, "query_fts_fallback", lambda *a, **kw: response if not daemon else None)
    monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
        "idf_gate_enabled": False, "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0})
    monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
    monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))
    monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (True, "ok"))
    monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")
    monkeypatch.setitem(sys.modules, "context_tracker", types.SimpleNamespace(
        estimate_usage=lambda *a, **kw: None, format_drowsiness=lambda *a, **kw: ""))
    stdin = json.dumps({"prompt": prompt, "session_id": session, "transcript_path": ""})
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))), \
            patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit:
            pass
    return out.getvalue(), err.getvalue()


def _rendered_ids(stdout):
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    block = ctx[ctx.index("[ENGRAM Recall"):]
    return re.findall(r"\[(ob_\d+)\]", block)


def test_surface_no_probe_dir_writes_nothing(tmp_path, monkeypatch):
    mod = _load_surface("a")
    out, err = _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2)))
    assert not _probe_root(tmp_path).exists()
    assert "route-ledger" not in err


def test_surface_session_dir_absent_but_other_session_present(tmp_path, monkeypatch):
    _live(tmp_path, "some-other-session")
    mod = _load_surface("b")
    before = _tree(tmp_path)
    _, err = _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2)))
    assert not (_probe_root(tmp_path) / SESSION).exists()
    # absent path is silent (no log line) and touches nothing under the probe
    assert err == ""
    assert _tree(tmp_path) == before


def test_surface_probe_live_one_parseable_line_shown_matches_output(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _load_surface("c")
    prompt = "how do we rank recall?"
    out, err = _run_surface(mod, monkeypatch, _surface_response(
        _ids(1, 2, 3), extra={"similarity": 0.81, "utility": 0.2}), prompt=prompt)
    rows = _lines(tmp_path, "prompt_surface")
    assert len(rows) == 1
    row = rows[0]
    assert row["route"] == "prompt_surface" and row["session_id"] == SESSION
    assert isinstance(row["ts"], int) and row["ts"] > 10**12
    assert row["fire_key"] == hashlib.sha256(prompt.encode()).hexdigest()[:16]
    assert row["query"] == prompt
    assert row["source"] == "daemon"
    assert row["shown"] == _rendered_ids(out) and row["shown"]
    assert [c["id"] for c in row["candidates"]] == _ids(1, 2, 3)
    assert [c["rank"] for c in row["candidates"]] == [1, 2, 3]
    c0 = row["candidates"][0]
    # #3551: + `ranked` (producer flag) and `in_context_why` (hook-derived)
    assert set(c0) == {"id", "type", "rank", *SIGNALS, "ranked", "in_context_why"}
    assert c0["similarity"] == 0.81 and c0["utility"] == 0.2
    # every candidate is either shown or has a cut reason, never both
    for c in row["candidates"]:
        assert (c["id"] in row["shown"]) != (c["id"] in row["cut"])


def test_surface_missing_signal_is_null_and_named_once(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _load_surface("d")
    # response carries similarity but NOT utility (nor the rest)
    _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2), extra={"similarity": 0.5}))
    row = _lines(tmp_path, "prompt_surface")[0]
    assert all(c["utility"] is None for c in row["candidates"])
    assert row["missing_signals"].count("utility") == 1
    assert "similarity" not in row["missing_signals"]
    assert len(row["missing_signals"]) == len(set(row["missing_signals"]))
    assert row["candidates"][0]["similarity"] == 0.5


def test_surface_fallback_path_recorded(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _load_surface("e")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2)), daemon=False)
    row = _lines(tmp_path, "prompt_surface")[0]
    assert row["source"] == "fallback"
    assert "utility" in row["missing_signals"]


def test_surface_budget_cut_named(tmp_path, monkeypatch):
    _live(tmp_path)
    (Path(os.environ["ENGRAM_HOME"]) / "config.json").write_text(
        json.dumps({"recall_budget": {"total_chars": 1, "enabled": True}}))
    mod = _load_surface("f")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2, 3)))
    row = _lines(tmp_path, "prompt_surface")[0]
    assert row["shown"] == []
    assert row["cut"] == {nid: "budget" for nid in _ids(1, 2, 3)}


def test_surface_no_keyword_candidate_cut_named(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _load_surface("g")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1, 2), keywords=False, top_n=0))
    row = _lines(tmp_path, "prompt_surface")[0]
    assert row["shown"] == []
    assert set(row["cut"]) == set(_ids(1, 2))
    assert all(r for r in row["cut"].values())


def test_surface_stdout_identical_with_and_without_ledger(tmp_path, monkeypatch):
    resp = _surface_response(_ids(1, 2, 3))
    mod = _load_surface("h1")
    base, _ = _run_surface(mod, monkeypatch, resp)           # probe dir absent
    _live(tmp_path)
    mod2 = _load_surface("h2")
    with_ledger, _ = _run_surface(mod2, monkeypatch, resp)
    assert _lines(tmp_path, "prompt_surface")
    assert with_ledger == base


def test_surface_unwritable_routes_stdout_unchanged_one_stderr_line(tmp_path, monkeypatch):
    resp = _surface_response(_ids(1, 2))
    mod = _load_surface("i1")
    base, _ = _run_surface(mod, monkeypatch, resp)
    live = _live(tmp_path)
    (live / "routes").write_text("a file where the directory must go")
    mod2 = _load_surface("i2")
    out, err = _run_surface(mod2, monkeypatch, resp)
    assert out == base
    lines = [x for x in err.splitlines() if x.startswith("route-ledger:")]
    assert len(lines) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_surface_readonly_routes_dir(tmp_path, monkeypatch):
    resp = _surface_response(_ids(1, 2))
    mod = _load_surface("j1")
    base, _ = _run_surface(mod, monkeypatch, resp)
    live = _live(tmp_path)
    (live / "routes").mkdir(mode=0o500)
    mod2 = _load_surface("j2")
    out, err = _run_surface(mod2, monkeypatch, resp)
    (live / "routes").chmod(0o700)
    assert out == base
    assert len([x for x in err.splitlines() if x.startswith("route-ledger:")]) == 1


def test_surface_modes_0600_file_0700_dir(tmp_path, monkeypatch):
    live = _live(tmp_path)
    mod = _load_surface("k")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1)))
    assert (live / "routes").stat().st_mode & 0o777 == 0o700
    assert (live / "routes" / "prompt_surface.jsonl").stat().st_mode & 0o777 == 0o600


def test_surface_two_fires_append_two_lines(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _load_surface("l")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1)), prompt="first prompt here")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1)), prompt="second prompt here")
    rows = _lines(tmp_path, "prompt_surface")
    assert len(rows) == 2 and rows[0]["fire_key"] != rows[1]["fire_key"]


_BAD_SIDS = ["unknown", "../escape", "a/b", "", ".", ".."]


@pytest.mark.parametrize("bad", _BAD_SIDS)
def test_surface_unsafe_session_id_never_writes(tmp_path, monkeypatch, bad):
    _live(tmp_path)
    mod = _load_surface("m")
    _run_surface(mod, monkeypatch, _surface_response(_ids(1)), session=bad)
    assert not list(_probe_root(tmp_path).rglob("*.jsonl"))
    assert not (_probe_root(tmp_path) / "routes").exists()


# --------------------------------------------------------------- tool_recall ---

def _load_tripwire(tag):
    spec = importlib.util.spec_from_file_location(f"tripwire_ledger_{tag}", TRIPWIRE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_tripwire(tmp_path, tag, terms=("alphaterm", "betaterm")):
    import sqlite3
    eh = Path(os.environ["ENGRAM_HOME"])
    (eh / "config.json").write_text(json.dumps(
        {"auto_surface": {"in_turn_recall": {"enabled": True}}}))
    conn = sqlite3.connect(eh / "knowledge.db")
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT,
        claim TEXT, is_current INT, memory_status TEXT, metadata TEXT)""")
    conn.execute("INSERT OR REPLACE INTO nodes VALUES ('seed_1','observation','s',1,'active','{}')")
    conn.commit()
    conn.close()
    fake = types.ModuleType("engram_idf")
    fake.extract_keywords = lambda conn, text, min_idf=4.0, top_k=5, stopwords=None, n_docs=None: [
        (t, 7.5) for t in terms]
    fake.STOPWORDS = frozenset()
    fake.JUNK_STOPLIST = _REAL_JUNK_STOPLIST
    sys.modules["engram_idf"] = fake
    return _load_tripwire(tag)


def _tool_response(ids, *, extra=None, matched=True):
    extra = extra or {}
    top = [{"id": n, "type": "observation", "recall_summary": f"summary of {n}",
            "recall_keywords": ["kw"], **extra} for n in ids]
    r = {"match_count": len(ids), "special_nodes": [], "top_claims": top}
    if matched:
        r["matched_ids"] = list(ids)
    return r


def _hook_input(tool_input, session=SESSION):
    return {"session_id": session, "transcript_path": "", "tool_name": "Bash",
            "tool_input": tool_input}


def _fire(mod, monkeypatch, response, tool_input, session=SESSION):
    monkeypatch.setattr(mod, "_query_recall_daemon", lambda q, top_k=5: response)
    err = io.StringIO()
    with patch("sys.stderr", err):
        out = mod.check_in_turn_recall("Bash alphaterm betaterm", _hook_input(tool_input, session))
    return out, err.getvalue()


def _tool_rendered_ids(out):
    return re.findall(r"^\s+- \[(ob_\d+)\]", out, flags=re.M)


def test_tool_no_probe_dir_writes_nothing(tmp_path, monkeypatch):
    mod = _setup_tripwire(tmp_path, "a")
    out, err = _fire(mod, monkeypatch, _tool_response(_ids(1, 2)), {"command": "ls"})
    assert out
    assert not _probe_root(tmp_path).exists()
    assert "route-ledger" not in err


def test_tool_probe_live_line_shown_matches_output(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "b")
    tool_input = {"command": "ls -la"}
    out, _ = _fire(mod, monkeypatch, _tool_response(
        _ids(1, 2, 3), extra={"similarity": 0.7}), tool_input)
    rows = _lines(tmp_path, "tool_recall")
    assert len(rows) == 1
    row = rows[0]
    canon = json.dumps(tool_input, sort_keys=True)
    assert row["fire_key"] == "Bash:" + hashlib.sha256(canon.encode()).hexdigest()[:16]
    assert row["route"] == "tool_recall" and row["session_id"] == SESSION
    assert row["source"] == "daemon"
    assert row["query"] == "alphaterm betaterm"
    assert row["novel_terms"] == ["alphaterm", "betaterm"]
    assert row["shown"] == _tool_rendered_ids(out) and row["shown"]
    assert [c["id"] for c in row["candidates"]] == _ids(1, 2, 3)
    assert row["candidates"][0]["similarity"] == 0.7
    assert "utility" in row["missing_signals"] and "similarity" not in row["missing_signals"]
    for c in row["candidates"]:
        assert (c["id"] in row["shown"]) != (c["id"] in row["cut"])


def test_tool_dedup_and_max_lines_cuts_named(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "c")
    ids = _ids(1, 2, 3, 4, 5, 6, 7)
    st = {"recent_ids": [ids[0]]}
    Path(os.environ["ENGRAM_HOME"], "in-turn-recall-state.json").write_text(json.dumps(st))
    out, _ = _fire(mod, monkeypatch, _tool_response(ids), {"command": "x"})
    row = _lines(tmp_path, "tool_recall")[0]
    assert row["cut"][ids[0]] == "dedup"
    assert row["shown"] == _tool_rendered_ids(out)
    cut_other = {k: v for k, v in row["cut"].items() if v != "dedup"}
    assert cut_other and all(v == "max_lines" for v in cut_other.values())


def test_tool_candidates_fall_back_to_pool_without_matched_ids(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "d")
    _fire(mod, monkeypatch, _tool_response(_ids(1, 2), matched=False), {"command": "x"})
    row = _lines(tmp_path, "tool_recall")[0]
    assert [c["id"] for c in row["candidates"]] == _ids(1, 2)


def test_tool_fire_key_stable_and_input_sensitive(tmp_path, monkeypatch):
    _live(tmp_path)
    keys = []
    for i, ti in enumerate([{"command": "ls", "b": 1}, {"b": 1, "command": "ls"}, {"command": "pwd"}]):
        mod = _setup_tripwire(tmp_path, f"e{i}")
        Path(os.environ["ENGRAM_HOME"], "in-turn-recall-state.json").unlink(missing_ok=True)
        _fire(mod, monkeypatch, _tool_response(_ids(1)), ti)
        keys.append(_lines(tmp_path, "tool_recall")[-1]["fire_key"])
    assert keys[0] == keys[1]          # key order does not matter (sort_keys)
    assert keys[0] != keys[2]


def test_tool_stdout_identical_and_unwritable_one_stderr_line(tmp_path, monkeypatch):
    resp = _tool_response(_ids(1, 2))
    mod = _setup_tripwire(tmp_path, "f1")
    live = _live(tmp_path)  # probe live BEFORE fire 1: fire 1 is a SUCCESSFUL write
    cap1 = io.StringIO()
    with contextlib.redirect_stdout(cap1):
        base, _ = _fire(mod, monkeypatch, resp, {"command": "ls"})
    assert (live / "routes" / "tool_recall.jsonl").is_file()
    Path(os.environ["ENGRAM_HOME"], "in-turn-recall-state.json").unlink(missing_ok=True)
    shutil.rmtree(live / "routes")
    (live / "routes").write_text("blocker")
    mod2 = _setup_tripwire(tmp_path, "f2")
    cap2 = io.StringIO()
    with contextlib.redirect_stdout(cap2):
        out, err = _fire(mod2, monkeypatch, resp, {"command": "ls"})
    assert out == base
    # The tool route returns its text and never prints: any stdout here is a
    # leak. Both captures are asserted (the ledger code runs in both fires,
    # so equality alone would let a print in it pass).
    assert cap1.getvalue() == "" and cap2.getvalue() == ""
    assert len([x for x in err.splitlines() if x.startswith("route-ledger:")]) == 1


def test_tool_modes(tmp_path, monkeypatch):
    live = _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "g")
    _fire(mod, monkeypatch, _tool_response(_ids(1)), {"command": "ls"})
    assert (live / "routes").stat().st_mode & 0o777 == 0o700
    assert (live / "routes" / "tool_recall.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", _BAD_SIDS)
def test_tool_unsafe_session_id_never_writes(tmp_path, monkeypatch, bad):
    _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "m")
    _fire(mod, monkeypatch, _tool_response(_ids(1)), {"command": "ls"}, session=bad)
    assert not list(_probe_root(tmp_path).rglob("*.jsonl"))
    assert not (_probe_root(tmp_path) / "routes").exists()


def test_tool_skipped_recent_counts_dedup_before_cap_only(tmp_path, monkeypatch):
    _live(tmp_path)
    mod = _setup_tripwire(tmp_path, "h")
    ids = _ids(1, 2, 3, 4, 5, 6, 7)  # max_lines is 3: ids 5..7 are past the cap
    st = {"recent_ids": [ids[0], ids[6]]}  # one dedup before the cap, one after
    Path(os.environ["ENGRAM_HOME"], "in-turn-recall-state.json").write_text(json.dumps(st))
    _fire(mod, monkeypatch, _tool_response(ids), {"command": "x"})
    entries = [json.loads(x) for x in
               Path(os.environ["ENGRAM_HOME"], "in-turn-recall-ledger.jsonl").read_text().splitlines()]
    assert len(entries) == 1 and entries[0]["outcome"] == "rendered"
    assert entries[0]["rendered_ids"] == ids[1:4]
    assert entries[0]["skipped_recent"] == 1  # the post-cap recent id is not counted


# ---- #3458 fold: nested `signals` (merged #3462 response shape) ----------

NESTED_SIGNALS = (
    "similarity", "importance_score", "utility", "composite_score",
    "match_type", "recall_turn",
)


def _load_hooklib(tag):
    spec = importlib.util.spec_from_file_location(f"_hooklib_nested_{tag}", HOOK_DIR / "_hooklib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_composition_real_surface_impl_feeds_ledger_signals():
    """REAL _surface_impl (real fixture DB) -> REAL build_route_candidates.

    For every entry carrying a `signals` dict, the ledger candidate's six
    signals EQUAL the response's own nested values (nothing defaulted, nothing
    dropped), and missing_signals is exactly `in_context` plus the signals the
    response itself reports null. The fixture DB has no embeddings, so
    keyword-only rows honestly carry `similarity: null`; the other five are
    non-null on every entry."""
    tests_dir = str(Path(__file__).resolve().parent)
    sys.path.insert(0, tests_dir)
    try:
        import test_surface_signals as tss
    finally:
        sys.path.remove(tests_dir)
    server = tss._real_server_two_nodes()
    resp = json.loads(server._surface_impl(query="zymurgy", top_k=5, semantic=False))
    entries = [e for sec in (resp["matched_meta"], resp["top_claims"], resp["special_nodes"])
               for e in sec if isinstance(e.get("signals"), dict)]
    assert entries, resp
    hooklib = _load_hooklib("comp")
    for e in entries:
        cands, missing = hooklib.build_route_candidates(
            {"matched_ids": [e["id"]], "matched_meta": [e]})
        assert len(cands) == 1
        for name in NESTED_SIGNALS:
            assert cands[0][name] == e["signals"][name], (name, e["signals"])
            if name != "similarity":
                assert cands[0][name] is not None, (name, e["signals"])
        expected_missing = [n for n in hooklib.ROUTE_SIGNALS
                            if n in ("in_context",) or
                            (n in NESTED_SIGNALS and e["signals"][n] is None)]
        assert missing == expected_missing, (missing, e["signals"])
        assert set(missing) <= {"similarity", "in_context"}, missing


def test_signal_nested_first_flat_fallback_none_with_missing():
    """Sol's real shape: nested entry, flat legacy entry, entry with neither."""
    hooklib = _load_hooklib("fixture")
    nested = {"id": "ob_0001", "similarity": 0.1,  # stale flat value must lose
              "signals": {"similarity": 0.81, "importance_score": 2.5, "utility": 0.33,
                          "composite_score": 1.7, "match_type": "semantic", "recall_turn": 7}}
    flat = {"id": "ob_0002", "similarity": 0.42, "importance_score": 1.25,
            "utility": 0.11, "composite_score": 0.9, "match_type": "keyword", "recall_turn": 3}
    neither = {"id": "ob_0003", "type": "observation_factual"}
    cands, missing = hooklib.build_route_candidates({
        "matched_ids": ["ob_0001", "ob_0002", "ob_0003"],
        "matched_meta": [nested, flat, neither],
    })
    by = {c["id"]: c for c in cands}
    assert by["ob_0001"]["similarity"] == 0.81  # nested first
    assert by["ob_0001"]["recall_turn"] == 7
    assert by["ob_0002"]["similarity"] == 0.42 and by["ob_0002"]["match_type"] == "keyword"  # flat fallback
    assert all(by["ob_0003"][n] is None for n in NESTED_SIGNALS)  # None, no default
    for n in NESTED_SIGNALS:  # still missing on the neither entry -> named once
        assert missing.count(n) == 1
