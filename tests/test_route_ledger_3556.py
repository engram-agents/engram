"""Contract-first tests: route ledger `in_context` across a compaction (#3556).

Contract (stated by the dispatching spec):

  S1. SessionStart `write_marker`: the marker carries `compaction_count`.
      source == "compact" -> (prior value, missing = 0) + 1; startup / resume /
      clear carry it unchanged; a brand-new marker is 0 (a first SessionStart
      with source=compact and no prior marker is 1). `session_started_at` and
      `injected_through` are still carried forward.
  L1. Every ledger line carries top-level `compaction_count`: the marker's int,
      or null when the marker lacks it or is unreadable.
  L2. `shown` resets at a compaction: at current count N > 0 only lines whose
      `compaction_count == N` count (lower counts and lines lacking the key do
      not); at count 0 or unknown every earlier line counts as before.
  L3. New source `summary` (only when N > 0): ids appearing as text in the LAST
      record with top-level `isCompactSummary` true in the marker's transcript
      (content a string, or a list of text blocks). A record that merely
      mentions the phrase inside an escaped string never matches. A candidate
      not in the summary falls through to the other sources.
  L4. Summary unreadable (no transcript_path, file missing, no summary record
      although N > 0): nothing else fired -> in_context None and "in_context" in
      missing_signals; another source fired -> that source wins.
  L5. `in_context_why` precedence: summary, shown, created, focused.
  L6. N == 0 or None never opens the transcript (a nonexistent path adds no
      unknown-ness).
  L7. A naive `created_at` is UTC, never process-local time, whatever TZ is.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))

SESSION = "sess-3556"
STARTED = "2026-10-05T00:00:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"
NEW = "2026-10-06T00:00:00+00:00"


def _id(n):
    # built, never spelled: the repo's no-new-shipped-node-ids gate scans tests too
    return f"ob_{n:04d}"


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
    spec = importlib.util.spec_from_file_location("_hooklib_3556", HOOK_DIR / "_hooklib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ S1: marker ---

def _hook_module():
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook_3556", HOOK_DIR / "engram-session-start-hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_marker(**fields):
    eh = Path(os.environ["ENGRAM_HOME"])
    (eh / "sessions").mkdir(exist_ok=True)
    (eh / "sessions" / f"{SESSION}.json").write_text(
        json.dumps({"session_id": SESSION, "started_at": STARTED, **fields}))


def _hook_input(source):
    return {"session_id": SESSION, "transcript_path": "/tmp/t.jsonl", "source": source}


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_s1_brand_new_marker_is_zero_for_non_compact_sources(source):
    assert _hook_module().write_marker(_hook_input(source))["compaction_count"] == 0


def test_s1_first_ever_session_start_with_compact_and_no_marker_is_one():
    assert _hook_module().write_marker(_hook_input("compact"))["compaction_count"] == 1


def test_s1_compact_without_prior_count_becomes_one():
    _seed_marker()  # legacy marker: no compaction_count key
    assert _hook_module().write_marker(_hook_input("compact"))["compaction_count"] == 1


def test_s1_compact_increments_existing_count():
    _seed_marker(compaction_count=1)
    assert _hook_module().write_marker(_hook_input("compact"))["compaction_count"] == 2


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_s1_other_sources_carry_count_unchanged(source):
    _seed_marker(compaction_count=3)
    assert _hook_module().write_marker(_hook_input(source))["compaction_count"] == 3


def test_s1_count_is_persisted_in_the_marker_file():
    _seed_marker(compaction_count=1)
    _hook_module().write_marker(_hook_input("compact"))
    p = Path(os.environ["ENGRAM_HOME"]) / "sessions" / f"{SESSION}.json"
    assert json.loads(p.read_text())["compaction_count"] == 2


def test_s1_session_started_at_and_injected_through_still_carried():
    _seed_marker(compaction_count=1, session_started_at="2026-09-01T00:00:00+00:00",
                 injected_through="2026-09-02T00:00:00+00:00")
    m = _hook_module().write_marker(_hook_input("compact"))
    assert m["session_started_at"] == "2026-09-01T00:00:00+00:00"
    assert m["injected_through"] == "2026-09-02T00:00:00+00:00"


# ------------------------------------------------------ ledger test helpers ---

def _sig(**kw):
    base = {"similarity": 0.5, "importance_score": 1.0, "utility": 0.1,
            "composite_score": 1.5, "match_type": "keyword", "recall_turn": 1,
            "ranked": True}
    base.update(kw)
    return base


_ABSENT = object()


def _make_home(*, started=STARTED, count=_ABSENT, transcript=_ABSENT, focused=(),
               db=True, marker=True):
    """Marker (with optional compaction_count / transcript_path) + focus table."""
    eh = Path(os.environ["ENGRAM_HOME"])
    if marker:
        body = {"session_id": SESSION, "started_at": started}
        if count is not _ABSENT:
            body["compaction_count"] = count
        if transcript is not _ABSENT:
            body["transcript_path"] = str(transcript)
        (eh / "sessions").mkdir(exist_ok=True)
        (eh / "sessions" / f"{SESSION}.json").write_text(json.dumps(body))
    if db:
        conn = sqlite3.connect(eh / "knowledge.db")
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INT, focused_at TEXT)")
        for nid in focused:
            conn.execute("INSERT INTO nodes VALUES (?,?,?)", (nid, 1, "2026-10-06T00:00:00"))
        conn.commit()
        conn.close()


def _probe(tmp_path) -> Path:
    d = tmp_path / "home" / ".engram" / "probe" / "mods-recall" / SESSION
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prior_line(tmp_path, route, shown, **extra):
    routes = _probe(tmp_path) / "routes"
    routes.mkdir(exist_ok=True)
    with open(routes / f"{route}.jsonl", "a") as f:
        f.write(json.dumps({"route": route, "shown": shown, **extra}) + "\n")


def _write(tmp_path, ids_created, *, shown=(), route="prompt_surface"):
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


def _summary_rec(content, **kw):
    return {"type": "user", "isCompactSummary": True,
            "message": {"role": "user", "content": content}, **kw}


def _transcript(tmp_path, records, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return p


def _plain(text="hello"):
    return {"type": "user", "message": {"role": "user", "content": text}}


# --------------------------------------------------- L1: compaction_count ---

@pytest.mark.parametrize("count", [0, 1, 4])
def test_l1_line_records_marker_count(tmp_path, count):
    _make_home(count=count, transcript=_transcript(tmp_path, [_summary_rec("x")]))
    row = _write(tmp_path, {_id(1): OLD})
    assert row["compaction_count"] == count and type(row["compaction_count"]) is int


def test_l1_null_when_marker_lacks_count(tmp_path):
    _make_home()
    row = _write(tmp_path, {_id(1): OLD})
    assert "compaction_count" in row and row["compaction_count"] is None


def test_l1_null_when_marker_unreadable(tmp_path):
    _make_home(marker=False)
    row = _write(tmp_path, {_id(1): OLD})
    assert "compaction_count" in row and row["compaction_count"] is None


# ------------------------------------------------------- L2: shown resets ---

def test_l2_shown_in_earlier_window_is_not_shown_now(tmp_path):
    _make_home(count=2, transcript=_transcript(tmp_path, [_summary_rec("none")]))
    _prior_line(tmp_path, "prompt_surface", [_id(1)], compaction_count=1)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is False and c["in_context_why"] is None


def test_l2_shown_in_current_window_is_shown(tmp_path):
    _make_home(count=2, transcript=_transcript(tmp_path, [_summary_rec("none")]))
    _prior_line(tmp_path, "tool_recall", [_id(1)], compaction_count=2)  # other route too
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "shown"


def test_l2_line_lacking_the_key_is_excluded_when_count_positive(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("none")]))
    _prior_line(tmp_path, "prompt_surface", [_id(1)])  # old-format line
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is False


def test_l2_null_count_line_is_excluded_when_count_positive(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("none")]))
    _prior_line(tmp_path, "prompt_surface", [_id(1)], compaction_count=None)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is False


@pytest.mark.parametrize("count", [0, None, _ABSENT])
def test_l2_every_earlier_line_counts_at_zero_or_unknown(tmp_path, count):
    _make_home(count=count)
    _prior_line(tmp_path, "prompt_surface", [_id(1)])                      # no key
    _prior_line(tmp_path, "tool_recall", [_id(2)], compaction_count=7)     # other count
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD})
    assert _cand(row, _id(1))["in_context_why"] == "shown"
    assert _cand(row, _id(2))["in_context_why"] == "shown"


# ---------------------------------------------------------- L3: summary ---

def test_l3_id_in_summary_is_true_summary(tmp_path):
    t = _transcript(tmp_path, [_plain(), _summary_rec(f"we discussed {_id(1)} at length")])
    _make_home(count=1, transcript=t)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "summary"


def test_l3_candidate_not_in_summary_falls_through_to_other_sources(tmp_path):
    t = _transcript(tmp_path, [_summary_rec(f"only {_id(1)}")])
    _make_home(count=1, transcript=t, focused=[_id(3)])
    _prior_line(tmp_path, "prompt_surface", [_id(4)], compaction_count=1)
    row = _write(tmp_path, {_id(2): OLD, _id(3): OLD, _id(4): OLD, _id(5): NEW})
    assert _cand(row, _id(2))["in_context"] is False
    assert _cand(row, _id(2))["in_context_why"] is None
    assert _cand(row, _id(3))["in_context_why"] == "focused"
    assert _cand(row, _id(4))["in_context_why"] == "shown"
    assert _cand(row, _id(5))["in_context_why"] == "created"


def test_l3_only_the_last_summary_counts(tmp_path):
    t = _transcript(tmp_path, [
        _summary_rec(f"first {_id(1)}"), _plain(), _summary_rec(f"second {_id(2)}"), _plain()])
    _make_home(count=2, transcript=t)
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD})
    assert _cand(row, _id(1))["in_context"] is False  # only in the first summary
    assert _cand(row, _id(2))["in_context_why"] == "summary"


def test_l3_phrase_inside_an_escaped_string_never_matches(tmp_path):
    # a record whose TEXT quotes the marker; and it comes AFTER the real summary
    decoy = _plain('see "isCompactSummary":true and ' + _id(2))
    t = _transcript(tmp_path, [_summary_rec(f"real {_id(1)}"), decoy])
    _make_home(count=1, transcript=t)
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD})
    assert _cand(row, _id(1))["in_context_why"] == "summary"
    assert _cand(row, _id(2))["in_context"] is False


def test_l3_unparseable_or_non_true_hit_is_skipped_and_search_continues(tmp_path):
    p = tmp_path / "t.jsonl"
    good = json.dumps(_summary_rec(f"good {_id(1)}"), separators=(",", ":"))
    bad_json = '{"isCompactSummary":true, broken'
    not_true = json.dumps({"type": "user", "isCompactSummary": "true",
                           "x": '"isCompactSummary":true'}, separators=(",", ":"))
    no_message = json.dumps({"isCompactSummary": True, "message": 5}, separators=(",", ":"))
    p.write_text("\n".join([good, bad_json, no_message, not_true]) + "\n")
    _make_home(count=1, transcript=p)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context_why"] == "summary"


def test_l3_content_as_list_of_text_blocks(tmp_path):
    blocks = [{"type": "text", "text": "intro"}, {"type": "image"},
              {"type": "text", "text": f" see {_id(1)}"}]
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec(blocks)]))
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "summary"


def test_l3_suffixed_id_form_is_extracted(tmp_path):
    full = _id(1) + "_ab12"
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec(f"x {full}.")]))
    c = _cand(_write(tmp_path, {full: OLD}), full)
    assert c["in_context_why"] == "summary"


# --------------------------------------------------- L4: summary unreadable ---

def _unreadable_home(tmp_path, kind, **kw):
    if kind == "no_path":
        _make_home(count=1, **kw)
    elif kind == "missing_file":
        _make_home(count=1, transcript=tmp_path / "nope.jsonl", **kw)
    else:
        _make_home(count=1, transcript=_transcript(tmp_path, [_plain(_id(1))]), **kw)


@pytest.mark.parametrize("kind", ["no_path", "missing_file", "no_summary_record"])
def test_l4_unreadable_summary_nothing_else_fired_is_null_and_missing(tmp_path, kind):
    _unreadable_home(tmp_path, kind)
    row = _write(tmp_path, {_id(1): OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and c["in_context_why"] is None
    assert "in_context" in row["missing_signals"]


@pytest.mark.parametrize("kind", ["no_path", "missing_file", "no_summary_record"])
def test_l4_unreadable_summary_but_another_source_fired_wins(tmp_path, kind):
    _unreadable_home(tmp_path, kind, focused=[_id(1)])
    _prior_line(tmp_path, "prompt_surface", [_id(2)], compaction_count=1)
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD, _id(3): NEW})
    assert _cand(row, _id(1))["in_context_why"] == "focused"
    assert _cand(row, _id(2))["in_context_why"] == "shown"
    assert _cand(row, _id(3))["in_context_why"] == "created"
    assert "in_context" not in row["missing_signals"]


def test_l4_readable_summary_without_the_id_and_nothing_else_is_false_not_null(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("nothing here")]))
    row = _write(tmp_path, {_id(1): OLD})
    assert _cand(row, _id(1))["in_context"] is False
    assert "in_context" not in row["missing_signals"]


# ----------------------------------------------------------- L5: precedence ---

def test_l5_summary_beats_shown_and_focused(tmp_path):
    t = _transcript(tmp_path, [_summary_rec(_id(1))])
    _make_home(count=1, transcript=t, focused=[_id(1)])
    _prior_line(tmp_path, "prompt_surface", [_id(1)], compaction_count=1)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context_why"] == "summary"


def test_l5_shown_beats_focused(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("x")]), focused=[_id(1)])
    _prior_line(tmp_path, "prompt_surface", [_id(1)], compaction_count=1)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context_why"] == "shown"


def test_l5_shown_beats_created(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("x")]))
    _prior_line(tmp_path, "prompt_surface", [_id(1)], compaction_count=1)
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context_why"] == "shown"


def test_l5_created_beats_focused(tmp_path):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec("x")]), focused=[_id(1)])
    c = _cand(_write(tmp_path, {_id(1): NEW}), _id(1))
    assert c["in_context_why"] == "created"


# ------------------------------------------------------------- L6: no scan ---

@pytest.mark.parametrize("count", [0, None, _ABSENT])
def test_l6_no_transcript_scan_when_count_is_zero_or_unknown(tmp_path, count):
    gone = tmp_path / "does-not-exist.jsonl"
    _make_home(count=count, transcript=gone, focused=[_id(1)])
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD})
    assert _cand(row, _id(1))["in_context_why"] == "focused"
    # readable-and-empty everywhere else: exactly False, NOT unknown
    assert _cand(row, _id(2))["in_context"] is False
    assert "in_context" not in row["missing_signals"]


def test_l6_summary_ids_are_not_consulted_at_count_zero(tmp_path):
    t = _transcript(tmp_path, [_summary_rec(_id(1))])
    _make_home(count=0, transcript=t)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is False and c["in_context_why"] is None


# ------------------------------------------------- L7: naive created_at = UTC ---

@pytest.fixture
def _tz():
    saved = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()
    yield set_tz
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


@pytest.mark.parametrize("zone,created,expected", [
    # west of UTC: read as local this would be hours LATER than written -> True
    ("America/New_York", "2026-10-04T23:59:00", False),
    # east of UTC: read as local this would be hours EARLIER than written -> False
    ("Asia/Tokyo", "2026-10-05T00:01:00", True),
    ("UTC", "2026-10-05T00:01:00", True),
])
def test_l7_naive_created_at_is_utc_not_local(tmp_path, _tz, zone, created, expected):
    _tz(zone)
    _make_home(count=0)
    c = _cand(_write(tmp_path, {_id(1): created}), _id(1))
    if expected:
        assert c["in_context"] is True and c["in_context_why"] == "created"
    else:
        assert c["in_context"] is False and c["in_context_why"] is None


# ------------------------------------- round 2: pending-summary guard etc. ---

def _boundary():
    return {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"trigger": "auto"}}


def test_p1_boundary_then_its_summary_counts(tmp_path):
    t = _transcript(tmp_path, [_plain(), _boundary(), _summary_rec(f"one {_id(1)}")])
    _make_home(count=1, transcript=t)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "summary"


def test_p2_newer_boundary_without_its_summary_means_summary_not_counted(tmp_path):
    t = _transcript(tmp_path, [
        _boundary(), _summary_rec(f"one {_id(1)}"), _plain(), _boundary()])
    _make_home(count=2, transcript=t)
    row = _write(tmp_path, {_id(1): OLD})
    c = _cand(row, _id(1))
    assert c["in_context"] is None and c["in_context_why"] is None
    assert "in_context" in row["missing_signals"]


def test_p2b_pending_summary_still_lets_another_source_win(tmp_path):
    t = _transcript(tmp_path, [_boundary(), _summary_rec(f"one {_id(1)}"), _boundary()])
    _make_home(count=2, transcript=t, focused=[_id(1)])
    row = _write(tmp_path, {_id(1): OLD})
    assert _cand(row, _id(1))["in_context_why"] == "focused"
    assert "in_context" not in row["missing_signals"]


def test_p3_only_the_newest_summary_counts_once_it_has_landed(tmp_path):
    t = _transcript(tmp_path, [
        _boundary(), _summary_rec(f"one {_id(1)}"), _plain(),
        _boundary(), _summary_rec(f"two {_id(2)}")])
    _make_home(count=2, transcript=t)
    row = _write(tmp_path, {_id(1): OLD, _id(2): OLD})
    assert _cand(row, _id(1))["in_context"] is False
    assert _cand(row, _id(2))["in_context_why"] == "summary"


def test_p4_summary_without_any_boundary_record_still_counts(tmp_path):
    t = _transcript(tmp_path, [_plain(), _summary_rec(f"old format {_id(1)}"), _plain()])
    _make_home(count=1, transcript=t)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context_why"] == "summary"


def test_p5_boundary_phrase_only_inside_an_escaped_string_does_not_trigger(tmp_path):
    decoy = _plain('quoting "subtype":"compact_boundary" in prose')
    t = _transcript(tmp_path, [_boundary(), _summary_rec(f"one {_id(1)}"), decoy])
    assert b'"subtype":"compact_boundary"' not in json.dumps(
        decoy, separators=(",", ":")).encode()  # the decoy really is escaped
    _make_home(count=1, transcript=t)
    c = _cand(_write(tmp_path, {_id(1): OLD}), _id(1))
    assert c["in_context"] is True and c["in_context_why"] == "summary"


# ------------------------------------------------------ round 2: id regex ---

@pytest.mark.parametrize("nid", ["nd_" + str(10000), _id(1234), _id(1) + "_ab12",
                                 "nd_" + str(123456) + "_x9"])
def test_r1_node_id_regex_widths(tmp_path, nid):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec(f"x {nid} y")]))
    c = _cand(_write(tmp_path, {nid: OLD}), nid)
    assert c["in_context_why"] == "summary"


# ------------------------------------------ round 2: marker read once only ---

def test_m1_marker_is_read_once_per_ledger_write(tmp_path, monkeypatch):
    _make_home(count=1, transcript=_transcript(tmp_path, [_summary_rec(_id(1))]))
    hl = _hooklib()
    calls = []
    real = hl._read_session_marker

    def counting(sid):
        calls.append(sid)
        return real(sid)
    monkeypatch.setattr(hl, "_read_session_marker", counting)
    _probe(tmp_path)
    resp = {"matched_ids": [_id(1)], "matched_meta": [
        {"id": _id(1), "type": "observation_factual", "created_at": OLD, "signals": _sig()}]}
    hl.write_route_ledger("prompt_surface", SESSION, "fk", "q", resp, [], {}, source="daemon")
    assert calls == [SESSION]


def test_m2_resolver_uses_the_marker_it_was_given_not_the_file(tmp_path):
    # file on disk: count 0, started far in the future -> would give no summary,
    # every earlier line counting, and nothing "created"
    _make_home(count=0, started="2030-01-01T00:00:00+00:00")
    t = _transcript(tmp_path, [_boundary(), _summary_rec(f"s {_id(1)}")])
    _prior_line(tmp_path, "prompt_surface", [_id(2)], compaction_count=2)
    _prior_line(tmp_path, "prompt_surface", [_id(3)], compaction_count=1)
    given = {"session_id": SESSION, "started_at": STARTED, "compaction_count": 2,
             "transcript_path": str(t)}
    resolve = _hooklib().make_in_context_resolver(
        SESSION, _probe(tmp_path) / "routes", marker=given)
    assert resolve(_id(1), OLD) == (True, "summary")   # count 2 + passed transcript
    assert resolve(_id(2), OLD) == (True, "shown")     # only count-2 lines
    assert resolve(_id(3), OLD) == (False, None)       # count-1 line excluded
    assert resolve(_id(4), NEW) == (True, "created")   # passed started_at, not 2030


# ------------------------------------------------ round 2: source casing ---

def test_s1_source_compare_is_case_insensitive():
    _seed_marker(compaction_count=1)
    assert _hook_module().write_marker(_hook_input("COMPACT"))["compaction_count"] == 2


# ------------------------------------ round 3: a non-regular transcript path ---
# `open()` on a FIFO blocks until a writer appears, and the reader runs in a hook
# that fires on every prompt and tool call. Each cell below runs in a SUBPROCESS
# with a hard wall-clock guard, so a regression FAILS instead of hanging the suite.

_GUARD_S = 5

_CHILD_PRELUDE = f"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("_hooklib_3556_child", {str(HOOK_DIR / "_hooklib.py")!r})
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)
"""


def _run_guarded(body):
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD_PRELUDE + body],
                              capture_output=True, text=True, timeout=_GUARD_S,
                              env=dict(os.environ))
    except subprocess.TimeoutExpired:
        pytest.fail(f"blocked for more than {_GUARD_S}s on a non-regular transcript path")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _call_guarded(expr):
    """Evaluate `expr` (using `hl`) in the guarded child; sets come back sorted."""
    out = _run_guarded(
        f"r = {expr}\n"
        "print(json.dumps(r, default=lambda x: sorted(x) if isinstance(x, (set, frozenset)) else str(x)))")
    return json.loads(out)


def _summary_ids_of(path):
    return _call_guarded(f"hl._last_summary_ids({str(path)!r})")


_needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")


@_needs_fifo
def test_r3_fifo_transcript_path_does_not_block(tmp_path):
    fifo = tmp_path / "t.fifo"
    os.mkfifo(fifo)
    assert _summary_ids_of(fifo) == [[], False]


@_needs_fifo
def test_r3_ledger_line_is_written_with_null_in_context_for_a_fifo_transcript(tmp_path):
    fifo = tmp_path / "t.fifo"
    os.mkfifo(fifo)
    _make_home(count=1, transcript=fifo)
    _probe(tmp_path)
    _run_guarded(f"""
resp = {{"matched_ids": [{_id(1)!r}], "matched_meta": [
    {{"id": {_id(1)!r}, "type": "observation_factual", "created_at": {OLD!r},
      "signals": {_sig()!r}}}]}}
hl.write_route_ledger("prompt_surface", {SESSION!r}, "fk", "q", resp, [], {{}}, source="daemon")
""")
    p = _probe(tmp_path) / "routes" / "prompt_surface.jsonl"
    row = json.loads(p.read_text().splitlines()[-1])
    c = _cand(row, _id(1))
    assert row["compaction_count"] == 1
    assert c["in_context"] is None and c["in_context_why"] is None
    assert "in_context" in row["missing_signals"]


def test_r3_directory_transcript_path_is_unreadable(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    assert _summary_ids_of(d) == [[], False]


# --- round 3b: every ledger read of a path another process controls ---------

def _eh():
    return Path(os.environ["ENGRAM_HOME"])


def _marker_path():
    (_eh() / "sessions").mkdir(exist_ok=True)
    return _eh() / "sessions" / f"{SESSION}.json"


def _routes_dir(tmp_path):
    d = _probe(tmp_path) / "routes"
    d.mkdir(exist_ok=True)
    return d


def test_r3b_positive_control_regular_marker_reads():
    _marker_path().write_text(json.dumps({"session_id": SESSION, "started_at": STARTED}))
    assert _call_guarded(f"hl._read_session_marker({SESSION!r})")["session_id"] == SESSION


@_needs_fifo
def test_r3b_fifo_marker_is_unreadable_not_a_hang():
    os.mkfifo(_marker_path())
    assert _call_guarded(f"hl._read_session_marker({SESSION!r})") is None


def test_r3b_positive_control_regular_routes_file_reads(tmp_path):
    (_routes_dir(tmp_path) / "a.jsonl").write_text(
        json.dumps({"route": "x", "shown": [_id(1)]}) + "\n")
    assert _call_guarded(f"hl._earlier_shown_ids(__import__('pathlib').Path({str(_routes_dir(tmp_path))!r}))") \
        == [[_id(1)], True]


@_needs_fifo
def test_r3b_fifo_in_routes_dir_is_unreadable_not_a_hang(tmp_path):
    d = _routes_dir(tmp_path)
    (d / "a.jsonl").write_text(json.dumps({"route": "x", "shown": [_id(1)]}) + "\n")
    os.mkfifo(d / "x.jsonl")
    assert _call_guarded(
        f"hl._earlier_shown_ids(__import__('pathlib').Path({str(d)!r}))") == [[_id(1)], False]


@_needs_fifo
def test_r3b_fifo_knowledge_db_is_unreadable_not_a_hang():
    os.mkfifo(_eh() / "knowledge.db")
    assert _call_guarded("hl._focused_ids()") == [[], False]


_WRITE_BODY = f"""
resp = {{"matched_ids": [{_id(1)!r}], "matched_meta": [
    {{"id": {_id(1)!r}, "type": "observation_factual", "created_at": {OLD!r},
      "signals": {_sig()!r}}}]}}
hl.write_route_ledger("prompt_surface", {SESSION!r}, "fk", "q", resp, [], {{}}, source="daemon")
"""


def _last_row(tmp_path):
    p = _probe(tmp_path) / "routes" / "prompt_surface.jsonl"
    return json.loads(p.read_text().splitlines()[-1])


@_needs_fifo
def test_r3b_end_to_end_fifo_marker_still_writes_the_line(tmp_path):
    _make_home(marker=False)
    os.mkfifo(_marker_path())
    _probe(tmp_path)
    _run_guarded(_WRITE_BODY)
    row = _last_row(tmp_path)
    assert row["compaction_count"] is None
    assert _cand(row, _id(1))["in_context"] is not None or "in_context" in row["missing_signals"]


@_needs_fifo
def test_r3b_end_to_end_fifo_in_routes_dir_still_writes_the_line(tmp_path):
    _make_home(count=0)
    os.mkfifo(_routes_dir(tmp_path) / "x.jsonl")
    _run_guarded(_WRITE_BODY)
    row = _last_row(tmp_path)
    assert row["compaction_count"] == 0
    assert _cand(row, _id(1))["in_context"] is None
    assert "in_context" in row["missing_signals"]


# --- round 3c: cap the line, not the file -----------------------------------

# fixed, not derived from the constant: a cap raised past this must turn the cells red
_OVER_CAP_PAD = 1_100_000


def _big_summary(nid, extra):
    return _summary_rec(f"s {nid} " + "x" * extra)


def test_r3c_positive_control_large_line_under_the_cap_reads(tmp_path):
    hl = _hooklib()
    t = _transcript(tmp_path, [_big_summary(_id(1), 100_000)])
    assert hl._last_summary_ids(str(t)) == ({_id(1)}, True)


def test_r3c_over_cap_last_summary_is_skipped_and_an_earlier_one_is_returned(tmp_path):
    hl = _hooklib()
    t = _transcript(tmp_path, [_summary_rec(f"early {_id(1)}"),
                               _big_summary(_id(2), _OVER_CAP_PAD)])
    assert hl._last_summary_ids(str(t)) == ({_id(1)}, True)


def test_r3c_only_summary_line_over_the_cap_is_unreadable(tmp_path):
    hl = _hooklib()
    t = _transcript(tmp_path, [_plain(), _big_summary(_id(2), _OVER_CAP_PAD)])
    assert hl._last_summary_ids(str(t)) == (set(), False)


# --- round 3d: the ledger APPEND must not block on a FIFO -------------------

def _ledger_rows(tmp_path):
    p = _probe(tmp_path) / "routes" / "tool_recall.jsonl"
    return p.read_text().splitlines() if p.is_file() else []


_WRITE_TOOL_BODY = _WRITE_BODY.replace('"prompt_surface"', '"tool_recall"')


def test_r3d_positive_control_regular_file_at_the_same_place_gets_its_line(tmp_path):
    _make_home(count=0)
    _routes_dir(tmp_path)
    _run_guarded(_WRITE_TOOL_BODY)
    assert len(_ledger_rows(tmp_path)) == 1


@_needs_fifo
def test_r3d_fifo_with_no_reader_returns_and_writes_nothing(tmp_path):
    _make_home(count=0)
    fifo = _routes_dir(tmp_path) / "tool_recall.jsonl"
    os.mkfifo(fifo)
    _run_guarded(_WRITE_TOOL_BODY)
    assert fifo.is_fifo()


@_needs_fifo
def test_r3d_fifo_with_a_reader_returns_and_no_line_goes_into_it(tmp_path):
    _make_home(count=0)
    fifo = _routes_dir(tmp_path) / "tool_recall.jsonl"
    os.mkfifo(fifo)
    # a reader holds the FIFO open for read (O_NONBLOCK so this does not wait
    # for the writer), so a plain write-open would succeed and the write land
    rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        _run_guarded(_WRITE_TOOL_BODY)
        try:
            leaked = os.read(rfd, 65536)
        except BlockingIOError:
            leaked = b""
    finally:
        os.close(rfd)
    assert leaked == b""
