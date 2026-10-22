"""Contract-first tests: live in-context exclusion (#3613 Part 2).

Both LIVE recall routes -- the prompt route (engram-surface-hook.py) and the
tool route (the in-turn recall in engram-lesson-tripwire-hook.py) -- drop a
candidate whose id is already in the model's context window, BEFORE their count
caps so the next candidates backfill.

S1  both hooks append one JSONL line per fire to a per-session record OUTSIDE
    the probe dir (`<ENGRAM_HOME>/sessions/<sid>.shown.jsonl`); the in-context
    resolver's `shown` set is the union of that record (windowed by
    compaction_count exactly as the probe-ledger reader is) and the probe ledger.
C1  in_context True (any why) -> dropped.   C2  None / False -> kept.
C3  resolver build/call raises -> nothing dropped, output == switch-off output.
C4  dropped ids stay in the probe ledger with cut `in_context`, rank intact.
C5  exclusion runs BEFORE the caps (backfill).
C6  `recall_in_context_exclusion.enabled`, default OFF: output byte-identical to
    today's; the S1 record is still written.
C7  tool route only: lesson / cornerstone are never excluded.
C8  everything excluded -> no recall block; the header keeps the daemon's count.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
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
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))
import _hooklib  # noqa: E402

SESSION = "sess-incontext-1"
STARTED = "2026-10-09T00:00:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"  # created before STARTED: never "created" source

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _ids(*nums, prefix="ob"):
    # built, never spelled: the repo's no-new-shipped-node-ids gate scans tests too
    return [f"{prefix}_{n}" for n in nums]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "engram"
    (eh / "sessions").mkdir(parents=True)
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    yield
    mod = sys.modules.get("engram_idf")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("engram_idf", None)


def _eh() -> Path:
    return Path(os.environ["ENGRAM_HOME"])


def _marker(count=None, session=SESSION):
    m = {"session_id": session, "started_at": STARTED, "transcript_path": ""}
    if count is not None:
        m["compaction_count"] = count
    (_eh() / "sessions" / f"{session}.json").write_text(json.dumps(m))


def _record_path(session=SESSION) -> Path:
    return _eh() / "sessions" / f"{session}.shown.jsonl"


def _record(session=SESSION):
    p = _record_path(session)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _seed_record(ids, *, count=None, route="prompt_surface", idx=0, session=SESSION):
    row = {"ts": 1, "route": route, "compaction_count": count, "prompt_index": idx,
           "ids": list(ids)}
    with open(_record_path(session), "a") as f:
        f.write(json.dumps(row) + "\n")


def _cfg(*, enabled=None, extra=None):
    cfg = {}
    p = _eh() / "config.json"
    if p.exists():
        cfg = json.loads(p.read_text())
    if enabled is not None:
        cfg["recall_in_context_exclusion"] = {"enabled": enabled}
    if extra:
        cfg.update(extra)
    p.write_text(json.dumps(cfg))


def _probe_root(tmp_path) -> Path:
    return tmp_path / "engram" / "probe" / "mods-recall"


def _live(tmp_path, session=SESSION) -> Path:
    d = _probe_root(tmp_path) / session
    d.mkdir(parents=True)
    return d


def _probe_lines(tmp_path, route, session=SESSION):
    p = _probe_root(tmp_path) / session / "routes" / f"{route}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _give_db_focused_column():
    """A knowledge.db whose `focused_at` query succeeds and returns nothing, so
    the focused source is READABLE (and not a hit)."""
    import sqlite3
    conn = sqlite3.connect(_eh() / "knowledge.db")
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT,
        claim TEXT, is_current INT, memory_status TEXT, metadata TEXT)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)")]
    if "focused_at" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN focused_at TEXT")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- surface ---

def _load_surface(tag):
    spec = importlib.util.spec_from_file_location(f"surface_incontext_{tag}", SURFACE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entry(nid, *, typ="observation", kw=True):
    return {"id": nid, "type": typ, "claim": f"claim {nid}", "confidence": 0.85,
            "created_at": OLD, "created_ago": "5d", "created_minutes_ago": 7200,
            "recall_summary": f"summary {nid}",
            "recall_keywords": ["kw"] if kw else None}


def _surface_response(*, specials=(), tops=(), others=(), match_count=None, types=None):
    """specials/tops: id lists rendered in full; others: matched_meta-only ids."""
    types = types or {}
    sp = [_entry(i, typ=types.get(i, "goal")) for i in specials]
    tc = [_entry(i, typ=types.get(i, "observation")) for i in tops]
    meta = [_entry(i, typ=types.get(i, "observation")) for i in [*specials, *tops, *others]]
    matched = [*specials, *tops, *others]
    return {"match_count": len(matched) if match_count is None else match_count,
            "matched_ids": matched, "matched_meta": meta,
            "special_nodes": sp, "top_claims": tc, "age": {}, "issues": {}}


def _run_surface(mod, monkeypatch, response, *, prompt="how do we rank recall?",
                 session=SESSION):
    monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: response)
    monkeypatch.setattr(mod, "query_fts_fallback", lambda *a, **kw: None)
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


_ID_RE = re.compile(r"\[([a-z]{2}_\d+)\]")


def _surface_block(stdout):
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    return ctx[ctx.index("[ENGRAM Recall"):]


def _surface_ids(stdout):
    return _ID_RE.findall(_surface_block(stdout))


def _section_ids(block, header, stop_headers):
    """Ids rendered under `header` (e.g. 'Specials:'), up to the next section."""
    out, on = [], False
    for ln in block.splitlines():
        s = ln.strip()
        if s == header:
            on = True
            continue
        if on and (s in stop_headers or not ln.startswith("    ")):
            break
        if on:
            out += _ID_RE.findall(ln)
    return out


_SECTIONS = {"Specials:", "Top claims:", "Others:", "Worth revisiting (not recalled recently):"}


def _prompt_no_block(stdout):
    return "[ENGRAM Recall" not in stdout


# ----------------------------------------------------------------- tool ---

def _load_tripwire(tag):
    spec = importlib.util.spec_from_file_location(f"tripwire_incontext_{tag}", TRIPWIRE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_tripwire(tag, terms=("alphaterm", "betaterm")):
    import sqlite3
    (_eh() / "config.json").write_text(json.dumps(
        {"auto_surface": {"in_turn_recall": {"enabled": True}}}))
    conn = sqlite3.connect(_eh() / "knowledge.db")
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


def _tool_response(ids, *, types=None, match_count=None):
    types = types or {}
    top = [{"id": n, "type": types.get(n, "observation"), "created_at": OLD,
            "recall_summary": f"summary of {n}", "recall_keywords": ["kw"]} for n in ids]
    return {"match_count": len(ids) if match_count is None else match_count,
            "special_nodes": [], "top_claims": top, "matched_ids": list(ids)}


def _fire(mod, monkeypatch, response, tool_input=None, session=SESSION):
    monkeypatch.setattr(mod, "_query_recall_daemon", lambda q, top_k=5: response)
    hook_input = {"session_id": session, "transcript_path": "", "tool_name": "Bash",
                  "tool_input": tool_input or {"command": "ls"}}
    Path(os.environ["ENGRAM_HOME"], "in-turn-recall-state.json").unlink(missing_ok=True)
    err = io.StringIO()
    with patch("sys.stderr", err):
        pending = {}
        out = mod.check_in_turn_recall("Bash alphaterm betaterm", hook_input, pending)
        if "ids" in pending:
            mod.append_shown_record("tool_recall", session, pending["ids"])
    return out, err.getvalue()


def _tool_ids(out):
    return _ID_RE.findall(out)


# ======================================================================
# S1 -- the shown record
# ======================================================================

def test_s1_surface_writes_record_without_probe(tmp_path, monkeypatch):
    _marker(count=2)
    mod = _load_surface("s1a")
    out, _ = _run_surface(mod, monkeypatch, _surface_response(tops=_ids(1, 2, 3)))
    rows = _record()
    assert len(rows) == 1 and not _probe_root(tmp_path).exists()
    row = rows[0]
    assert set(row) == {"ts", "route", "compaction_count", "prompt_index", "ids"}
    assert row["route"] == "prompt_surface"
    assert isinstance(row["ts"], int) and row["ts"] > 10**12
    assert row["compaction_count"] == 2
    assert isinstance(row["prompt_index"], int)
    assert row["ids"] == _surface_ids(out) and row["ids"]


def test_s1_tool_writes_record_without_probe(tmp_path, monkeypatch):
    _marker(count=0)
    mod = _setup_tripwire("s1b")
    out, _ = _fire(mod, monkeypatch, _tool_response(_ids(1, 2)))
    rows = _record()
    assert len(rows) == 1 and not _probe_root(tmp_path).exists()
    assert rows[0]["route"] == "tool_recall"
    assert rows[0]["compaction_count"] == 0
    assert rows[0]["ids"] == _tool_ids(out) == _ids(1, 2)


def test_s1_prompt_index_strictly_increasing_across_routes(monkeypatch):
    _marker()
    sm = _load_surface("s1c")
    tm = _setup_tripwire("s1c")
    _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1)), prompt="first prompt here")
    _fire(tm, monkeypatch, _tool_response(_ids(2)))
    _run_surface(sm, monkeypatch, _surface_response(tops=_ids(3)), prompt="third prompt here")
    idx = [r["prompt_index"] for r in _record()]
    assert [r["route"] for r in _record()] == ["prompt_surface", "tool_recall", "prompt_surface"]
    assert all(isinstance(i, int) for i in idx)
    assert idx[0] < idx[1] < idx[2]


def test_s1_only_rendered_ids_recorded_not_cap_cut_ones(monkeypatch):
    _marker()
    tm = _setup_tripwire("s1d")
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3, 4, 5, 6, 7)))
    assert len(_tool_ids(out)) == 3                      # max_lines default
    assert _record()[-1]["ids"] == _ids(1, 2, 3)
    # surface: a budget that renders nothing records nothing
    _cfg(extra={"recall_budget": {"total_chars": 1, "enabled": True}})
    sm = _load_surface("s1d")
    _run_surface(sm, monkeypatch, _surface_response(tops=_ids(11, 12)))
    assert _record()[-1]["route"] == "prompt_surface" and _record()[-1]["ids"] == []


@pytest.mark.parametrize("switch", [None, False, True])
def test_s1_written_regardless_of_kill_switch(switch, monkeypatch):
    _marker()
    _cfg(enabled=switch)
    sm = _load_surface("s1e")
    _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1, 2)))
    assert len(_record()) == 1 and _record()[0]["ids"] == _ids(1, 2)


def test_s1_failure_is_fail_open_one_stderr_line_output_unchanged(tmp_path, monkeypatch):
    _marker()
    resp = _surface_response(tops=_ids(1, 2))
    sm = _load_surface("s1f1")
    base, _ = _run_surface(sm, monkeypatch, resp)
    _record_path().unlink()
    _record_path().mkdir()                                # a directory where the file goes
    sm2 = _load_surface("s1f2")
    out, err = _run_surface(sm2, monkeypatch, resp)
    assert out == base
    assert len([x for x in err.splitlines() if x.strip()]) <= 1
    tm = _setup_tripwire("s1f")
    _record_path().rmdir()
    clean, _ = _fire(tm, monkeypatch, _tool_response(_ids(5, 6)))
    _record_path().unlink()
    _record_path().mkdir()
    out2, err2 = _fire(tm, monkeypatch, _tool_response(_ids(5, 6)))
    assert out2 == clean
    assert len([x for x in err2.splitlines() if x.strip()]) <= 1


def test_s1_record_is_size_bounded_and_index_stays_monotonic():
    _marker()
    for i in range(6000):
        _seed_record(_ids(i, i + 1, i + 2), idx=i)
    _hooklib.append_shown_record("prompt_surface", SESSION, _ids(1))
    assert _record_path().stat().st_size <= _hooklib.SHOWN_RECORD_MAX_BYTES
    rows = _record()
    assert rows[-1]["prompt_index"] > 5999               # continues past the trimmed lines
    assert rows[-1]["ids"] == _ids(1)
    ix = [r["prompt_index"] for r in rows]
    assert ix == sorted(ix) and len(set(ix)) == len(ix)


def _fill_record_to_trim():
    for i in range(6000):
        _seed_record(_ids(i, i + 1, i + 2), idx=i)


def _trim_tmp_path() -> Path:
    return _record_path().with_name(_record_path().name + f".{os.getpid()}.tmp")


def test_s1_trim_survives_a_stale_tmp_file():
    _marker()
    _fill_record_to_trim()
    _trim_tmp_path().write_text("stale garbage from a crashed trim\n")
    _hooklib.append_shown_record("prompt_surface", SESSION, _ids(1))
    assert _record_path().stat().st_size <= _hooklib.SHOWN_RECORD_MAX_BYTES
    rows = _record()
    assert rows[-1]["ids"] == _ids(1) and rows[-1]["prompt_index"] > 5999
    assert not _trim_tmp_path().exists()


def test_s1_trim_does_not_follow_a_symlinked_tmp(tmp_path):
    _marker()
    _fill_record_to_trim()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    _trim_tmp_path().symlink_to(victim)
    _hooklib.append_shown_record("prompt_surface", SESSION, _ids(1))
    assert victim.read_text() == "do not touch"
    assert _record_path().stat().st_size <= _hooklib.SHOWN_RECORD_MAX_BYTES
    assert _record()[-1]["ids"] == _ids(1)


def test_s1_ids_per_line_bounded():
    _marker()
    _hooklib.append_shown_record("tool_recall", SESSION, _ids(*range(500)))
    ids = _record()[0]["ids"]
    assert 0 < len(ids) <= _hooklib.SHOWN_RECORD_MAX_IDS < 500
    assert ids == _ids(*range(len(ids)))                 # the head is kept, in order


@pytest.mark.parametrize("bad", ["unknown", "../escape", "a/b", "", ".", ".."])
def test_s1_unsafe_session_id_never_writes(bad, tmp_path):
    _hooklib.append_shown_record("prompt_surface", bad, _ids(1))
    assert not list((_eh() / "sessions").glob("*.shown.jsonl"))
    assert not list(tmp_path.rglob("*.shown.jsonl"))


def _resolver(count_marker=None, routes=None):
    _marker(count=count_marker)
    return _hooklib.make_in_context_resolver(
        SESSION, routes or (_eh() / "no-routes"), marker=_hooklib._read_session_marker(SESSION))


def test_s1_resolver_reads_the_record_as_shown():
    _seed_record(_ids(1))
    assert _resolver()(_ids(1)[0], OLD) == (True, "shown")


def test_s1_resolver_window_matches_probe_reader_exactly(tmp_path):
    a, b, c, d = _ids(1, 2, 3, 4)
    _seed_record([a], count=1)
    _seed_record([b], count=2)
    _seed_record([c], count=None)                        # count-less line (older code)
    _seed_record([d], count=3)
    # the probe reader's own answer is the reference for "exactly"
    routes = tmp_path / "ref" / "routes"
    routes.mkdir(parents=True)
    with open(routes / "prompt_surface.jsonl", "w") as f:
        for ids, cc in (([a], 1), ([b], 2), ([c], None), ([d], 3)):
            row = {"shown": ids}
            if cc is not None:
                row["compaction_count"] = cc
            f.write(json.dumps(row) + "\n")
    for marker_count in (None, 0, 2):
        ref, _ = _hooklib._earlier_shown_ids(routes, marker_count)
        res = _resolver(marker_count)
        got = {i for i in (a, b, c, d) if res(i, OLD)[0] is True}
        assert got == ref, (marker_count, got, ref)
    assert {i for i in (a, b, c, d) if _resolver(2)(i, OLD)[1] == "shown"} == {b}


def test_s1_resolver_unions_probe_ledger_and_record(tmp_path):
    a, b = _ids(1, 2)
    live = _live(tmp_path)
    (live / "routes").mkdir()
    (live / "routes" / "prompt_surface.jsonl").write_text(json.dumps({"shown": [a]}) + "\n")
    _seed_record([b])
    res = _resolver(routes=live / "routes")
    assert res(a, OLD) == (True, "shown") and res(b, OLD) == (True, "shown")


def test_s1_unreadable_record_makes_unknown_not_false():
    _give_db_focused_column()       # every OTHER source readable: only the record can make it unknown
    _record_path().mkdir()                               # unreadable as a regular file
    assert _resolver()(_ids(9)[0], OLD) == (None, None)


def test_s1_not_shown_by_the_fire_being_written(monkeypatch):
    """Resolver is built before this fire's line is appended: fire 1 renders X
    with the switch ON and an empty record; only fire 2 excludes it."""
    _marker()
    _cfg(enabled=True)
    sm = _load_surface("s1g")
    x = _ids(1)[0]
    out1, _ = _run_surface(sm, monkeypatch, _surface_response(tops=[x], others=_ids(2)),
                           prompt="first prompt here")
    assert x in _surface_ids(out1)
    out2, _ = _run_surface(sm, monkeypatch, _surface_response(tops=[x], others=_ids(3)),
                           prompt="second prompt here")
    assert x not in _surface_ids(out2)


def _assert_probe_ordering(lines, first_ids, second_ids):
    """Two consecutive probe-ledger lines: fire 1's own ids read in_context
    False, fire 2's ids that fire 1 rendered read True/'shown', and fire 2's
    own new ids read False (never 'shown' by the fire being written)."""
    assert len(lines) == 2
    c1 = {c["id"]: c for c in lines[0]["candidates"]}
    for nid in first_ids:
        assert lines[0]["shown"].count(nid) == 1
        assert c1[nid]["in_context"] is False and c1[nid]["in_context_why"] is None, nid
    c2 = {c["id"]: c for c in lines[1]["candidates"]}
    for nid in second_ids:
        assert nid in lines[1]["shown"]
        if nid in first_ids:
            assert c2[nid]["in_context"] is True and c2[nid]["in_context_why"] == "shown", nid
        else:
            assert c2[nid]["in_context"] is False and c2[nid]["in_context_why"] is None, nid


def test_s1_ordering_surface_probe_line_never_shown_by_its_own_fire(tmp_path, monkeypatch):
    _marker()
    _give_db_focused_column()
    _live(tmp_path)
    sm = _load_surface("s1ord")
    a, b, c = _ids(1, 2, 3)
    _run_surface(sm, monkeypatch, _surface_response(tops=[a, b]), prompt="first prompt here")
    _run_surface(sm, monkeypatch, _surface_response(tops=[b, c]), prompt="second prompt here")
    _assert_probe_ordering(_probe_lines(tmp_path, "prompt_surface"), [a, b], [b, c])


def test_s1_ordering_tool_probe_line_never_shown_by_its_own_fire(tmp_path, monkeypatch):
    _marker()
    tm = _setup_tripwire("s1ordt")
    _give_db_focused_column()
    _live(tmp_path)
    a, b, c = _ids(1, 2, 3)
    _fire(tm, monkeypatch, _tool_response([a, b]))
    _fire(tm, monkeypatch, _tool_response([b, c]))
    _assert_probe_ordering(_probe_lines(tmp_path, "tool_recall"), [a, b], [b, c])


# ======================================================================
# C1 -- exclusion, both routes
# ======================================================================

def test_c1_surface_drops_in_context_ids(monkeypatch):
    _marker()
    _cfg(enabled=True)
    _seed_record(_ids(1, 3))
    sm = _load_surface("c1a")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1, 2), others=_ids(3, 4)))
    assert _surface_ids(out) == _ids(2, 4) or set(_surface_ids(out)) == set(_ids(2, 4))
    assert not (set(_surface_ids(out)) & set(_ids(1, 3)))


def test_c1_any_why_drops_not_only_shown(monkeypatch, tmp_path):
    """`created` (node filed since session start) is an in-context source too."""
    _marker()
    _cfg(enabled=True)
    resp = _surface_response(tops=_ids(1), others=_ids(2))
    for e in resp["top_claims"] + resp["matched_meta"]:
        if e["id"] == _ids(1)[0]:
            e["created_at"] = "2026-10-09T05:00:00+00:00"   # after STARTED
            e["created_minutes_ago"] = 100000               # keep the echo guard out
    sm = _load_surface("c1b")
    out, _ = _run_surface(sm, monkeypatch, resp)
    assert _ids(1)[0] not in _surface_ids(out) and _ids(2)[0] in _surface_ids(out)


def test_c1_tool_drops_in_context_ids(monkeypatch):
    _marker()
    tm = _setup_tripwire("c1c")
    _cfg(enabled=True)
    _seed_record(_ids(1), route="tool_recall")
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2)))
    assert _tool_ids(out) == _ids(2)


# ======================================================================
# C2 -- unknown is not true
# ======================================================================

def test_c2_unknown_none_is_not_dropped_surface(monkeypatch):
    _marker()                                            # no knowledge.db -> focused unreadable
    resp = _surface_response(tops=_ids(1, 2), others=_ids(3))
    assert _hooklib.make_in_context_resolver(
        SESSION, _eh() / "r", marker=_hooklib._read_session_marker(SESSION))(
            _ids(1)[0], OLD) == (None, None)
    sm = _load_surface("c2a0")
    off, _ = _run_surface(sm, monkeypatch, resp)
    _cfg(enabled=True)
    _record_path().unlink()          # the off run's own record must not make these "shown"
    sm2 = _load_surface("c2a")
    on, _ = _run_surface(sm2, monkeypatch, resp)
    assert _surface_ids(on) == _surface_ids(off)


def test_c2_false_is_not_dropped_surface(monkeypatch):
    _marker()
    _give_db_focused_column()
    resp = _surface_response(tops=_ids(1, 2), others=_ids(3))
    assert _hooklib.make_in_context_resolver(
        SESSION, _eh() / "r", marker=_hooklib._read_session_marker(SESSION))(
            _ids(1)[0], OLD) == (False, None)
    sm = _load_surface("c2b0")
    off, _ = _run_surface(sm, monkeypatch, resp)
    _cfg(enabled=True)
    _record_path().unlink()          # the off run's own record must not make these "shown"
    sm2 = _load_surface("c2b")
    on, _ = _run_surface(sm2, monkeypatch, resp)
    assert _surface_ids(on) == _surface_ids(off) and _surface_ids(on)


def test_c2_unknown_and_false_not_dropped_tool(monkeypatch):
    _marker()
    tm = _setup_tripwire("c2c")
    _cfg(enabled=True)
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2)))          # unknown (None)
    assert _tool_ids(out) == _ids(1, 2)
    _give_db_focused_column()
    _record_path().unlink()          # fire 1's own record must not make these "shown"
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2)))          # False
    assert _tool_ids(out) == _ids(1, 2)


# ======================================================================
# C3 -- fail-open
# ======================================================================

def _boom(*a, **kw):
    raise RuntimeError("resolver build failed")


def _boom_resolver(*a, **kw):
    def resolve(node_id, created_at):
        raise RuntimeError("resolver call failed")
    return resolve


@pytest.mark.parametrize("failing", [_boom, _boom_resolver], ids=["build-raises", "call-raises"])
def test_c3_surface_fail_open_output_identical_to_switch_off(failing, monkeypatch):
    _marker()
    _seed_record(_ids(1, 3))
    resp = _surface_response(tops=_ids(1, 2), others=_ids(3, 4))
    sm = _load_surface("c3a0")
    off, _ = _run_surface(sm, monkeypatch, resp)         # switch absent == off
    _cfg(enabled=True)
    monkeypatch.setattr(_hooklib, "make_in_context_resolver", failing)
    sm2 = _load_surface("c3a")
    on, _ = _run_surface(sm2, monkeypatch, resp)
    assert on == off


@pytest.mark.parametrize("failing", [_boom, _boom_resolver], ids=["build-raises", "call-raises"])
def test_c3_tool_fail_open_output_identical_to_switch_off(failing, monkeypatch):
    _marker()
    _seed_record(_ids(1), route="tool_recall")
    tm = _setup_tripwire("c3b")
    off, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3)))
    _cfg(enabled=True)
    monkeypatch.setattr(_hooklib, "make_in_context_resolver", failing)
    on, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3)))
    assert on == off and _tool_ids(on) == _ids(1, 2, 3)


# ======================================================================
# C4 -- the probe ledger still names the dropped ids
# ======================================================================

def test_c4_surface_dropped_ids_in_probe_ledger_with_cut_and_rank(tmp_path, monkeypatch):
    _marker()
    _cfg(enabled=True)
    _live(tmp_path)
    _seed_record(_ids(1))
    sm = _load_surface("c4a")
    _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1, 2), others=_ids(3)))
    row = _probe_lines(tmp_path, "prompt_surface")[0]
    cand = {c["id"]: c for c in row["candidates"]}
    x = _ids(1)[0]
    assert x in cand and cand[x]["rank"] == 1
    assert row["cut"][x] == "in_context"
    assert x not in row["shown"]
    assert cand[_ids(3)[0]]["rank"] == 3


def test_c4_tool_dropped_ids_in_probe_ledger_with_cut_and_rank(tmp_path, monkeypatch):
    _marker()
    tm = _setup_tripwire("c4b")
    _cfg(enabled=True)
    _live(tmp_path)
    _seed_record(_ids(2), route="tool_recall")
    _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3)))
    row = _probe_lines(tmp_path, "tool_recall")[0]
    cand = {c["id"]: c for c in row["candidates"]}
    x = _ids(2)[0]
    assert cand[x]["rank"] == 2 and row["cut"][x] == "in_context"
    assert row["shown"] == _ids(1, 3)


# ======================================================================
# C5 -- before the caps: backfill
# ======================================================================

def test_c5_specials_backfill_surface(monkeypatch):
    _marker()
    _cfg(enabled=True)
    _seed_record(_ids(1, 2))
    sm = _load_surface("c5a")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(specials=_ids(1, 2, 3, 4, 5)))
    block = _surface_block(out)
    assert _section_ids(block, "Specials:", _SECTIONS) == _ids(3, 4, 5)


def test_c5_top_claims_backfill_surface(monkeypatch):
    _marker()
    _cfg(enabled=True)
    _seed_record(_ids(1, 2))
    sm = _load_surface("c5b")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1, 2, 3, 4, 5)))
    assert _section_ids(_surface_block(out), "Top claims:", _SECTIONS) == _ids(3, 4, 5)


def test_c5_others_backfill_surface(monkeypatch):
    _marker()
    _cfg(enabled=True)
    ids = _ids(*range(1, 21))
    _seed_record(ids[:5])
    sm = _load_surface("c5c")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(others=ids))
    got = _section_ids(_surface_block(out), "Others:", _SECTIONS)
    assert got == ids[5:20] and len(got) == 15


def test_c5_max_lines_backfill_tool(monkeypatch):
    _marker()
    tm = _setup_tripwire("c5d")
    _cfg(enabled=True)
    _seed_record(_ids(1, 2), route="tool_recall")
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3, 4, 5, 6)))
    assert _tool_ids(out) == _ids(3, 4, 5)


# ======================================================================
# C6 -- kill switch default OFF, byte-identical
# ======================================================================

def test_c6_default_off_surface_equals_pre_change_golden(monkeypatch):
    _marker()
    _seed_record(_ids(1, 3))                             # in context, but the switch is off
    sm = _load_surface("c6a")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(
        specials=_ids(7), tops=_ids(1, 2), others=_ids(3, 4)))
    assert out == GOLDEN_SURFACE


def test_c6_default_off_tool_equals_pre_change_golden(monkeypatch):
    _marker()
    _seed_record(_ids(1), route="tool_recall")
    tm = _setup_tripwire("c6b")
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2, 3, 4)))
    assert out == GOLDEN_TOOL


def test_c6_explicit_false_same_as_absent(monkeypatch):
    _marker()
    _seed_record(_ids(1, 3))
    resp = _surface_response(tops=_ids(1, 2), others=_ids(3, 4))
    sm = _load_surface("c6c0")
    absent, _ = _run_surface(sm, monkeypatch, resp)
    _cfg(enabled=False)
    sm2 = _load_surface("c6c")
    off, _ = _run_surface(sm2, monkeypatch, resp)
    assert off == absent and _ids(1)[0] in _surface_ids(off)


@pytest.mark.parametrize("junk", ["yes", 1, None, "false"])
def test_c6_only_a_true_boolean_enables(junk, monkeypatch):
    _marker()
    _seed_record(_ids(1))
    resp = _surface_response(tops=_ids(1, 2))
    sm = _load_surface("c6d0")
    off, _ = _run_surface(sm, monkeypatch, resp)
    (_eh() / "config.json").write_text(json.dumps({"recall_in_context_exclusion": {"enabled": junk}}))
    sm2 = _load_surface("c6d")
    out, _ = _run_surface(sm2, monkeypatch, resp)
    assert out == off


def test_c6_a_fifo_at_config_json_reads_as_off_and_never_blocks():
    """A hook must never hang on config.json (#3628): a FIFO there reads as
    "switch off" at once. A plain open() of a reader-less FIFO waits for a
    writer, so the call runs in a thread with a bounded join."""
    import threading
    fifo = _eh() / "config.json"
    os.mkfifo(fifo)
    result = []
    t = threading.Thread(
        target=lambda: result.append(_hooklib.in_context_exclusion_enabled()), daemon=True)
    t.start()
    t.join(3)
    try:
        assert not t.is_alive(), "in_context_exclusion_enabled blocked on a FIFO config.json"
        assert result == [False]
    finally:
        if t.is_alive():  # unblock the stuck reader so the thread can end
            fd = os.open(fifo, os.O_RDWR)
            t.join(3)
            os.close(fd)


# ======================================================================
# C7 -- tool route exempts lesson / cornerstone; prompt route does not
# ======================================================================

def test_c7_tool_route_never_excludes_lesson_or_cornerstone(monkeypatch):
    _marker()
    tm = _setup_tripwire("c7a")
    _cfg(enabled=True)
    les, cor, obs = "ls_101", "cs_102", "ob_103"
    _seed_record([les, cor, obs], route="tool_recall")
    out, _ = _fire(tm, monkeypatch, _tool_response(
        [les, cor, obs, "ob_104"], types={les: "lesson", cor: "cornerstone"}))
    assert _tool_ids(out) == [les, cor, "ob_104"]


def test_c7_prompt_route_has_no_exemption(monkeypatch):
    _marker()
    _cfg(enabled=True)
    les, cor = "ls_101", "cs_102"
    _seed_record([les, cor])
    sm = _load_surface("c7b")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(
        tops=[les, cor, "ob_104"], types={les: "lesson", cor: "cornerstone"}))
    assert _surface_ids(out) == ["ob_104"]


# ======================================================================
# C8 -- everything excluded
# ======================================================================

def test_c8_surface_everything_excluded_emits_no_block(monkeypatch):
    _marker()
    _cfg(enabled=True)
    ids = _ids(1, 2, 3)
    _seed_record(ids)
    sm = _load_surface("c8a")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(specials=ids[:1], tops=ids[1:2],
                                                              others=ids[2:]))
    assert _prompt_no_block(out)
    assert not (set(_ID_RE.findall(out)) & set(ids))


def test_c8_tool_everything_excluded_emits_nothing(monkeypatch):
    _marker()
    tm = _setup_tripwire("c8b")
    _cfg(enabled=True)
    _seed_record(_ids(1, 2), route="tool_recall")
    out, _ = _fire(tm, monkeypatch, _tool_response(_ids(1, 2)))
    assert out == ""


def test_c8_header_keeps_the_daemon_match_count(monkeypatch):
    _marker()
    _cfg(enabled=True)
    _seed_record(_ids(1, 2))
    sm = _load_surface("c8c")
    out, _ = _run_surface(sm, monkeypatch, _surface_response(tops=_ids(1, 2, 3), others=_ids(4)))
    assert "[ENGRAM Recall: 4 nodes match your query]" in _surface_block(out)
    assert _surface_ids(out) and not (set(_surface_ids(out)) & set(_ids(1, 2)))


# Captured from the pre-change hooks (origin/dev bde0e6e8 + #3617) with the same inputs.
GOLDEN_SURFACE = '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\\u25b8 1 shown \\u00b7 0 cut\\n[ENGRAM Recall: 5 nodes match your query]\\n  Noteworthy: 1 goal\\n  Specials:\\n    - [ob_7] [goal] (conf 0.85) \\u00b7 5d  `kw` \\u2014 summary ob_7\\n  Top claims:\\n    - [ob_1] (conf 0.85) \\u00b7 5d  `kw` \\u2014 summary ob_1\\n    - [ob_2] (conf 0.85) \\u00b7 5d  `kw` \\u2014 summary ob_2\\n  Others:\\n    - [ob_3] `kw`\\n    - [ob_4] `kw`\\n  Use engram_inspect(node_id) for details, engram_get_subgraph for full chains."}}\n'
GOLDEN_TOOL = '[ENGRAM in-turn recall — novel terms: alphaterm, betaterm]\n  - [ob_1] `kw` — summary of ob_1\n  - [ob_2] `kw` — summary of ob_2\n  - [ob_3] `kw` — summary of ob_3\n  (ambient action-moment recall; engram_inspect(id) for detail)'
