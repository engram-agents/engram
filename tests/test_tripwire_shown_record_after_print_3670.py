"""Contract-first tests: the tripwire's in-turn recall writes its shown record
only AFTER the response was written out (#3670, part of #3613).

Before: `check_in_turn_recall`'s `_route_ledger` appended the shown record
(`append_shown_record("tool_recall", ...)`) while the recall was still being
built, long before main() printed the response. An exception in between, or a
hook timeout, recorded ids the model never saw.

Contract (main() drives `check_in_turn_recall(target, hook_input, record_out)`):
  T1  the append happens AFTER print(json.dumps(response)) (+ flush) succeeded;
      a print that raises records nothing.
  T2  recall rendered and printed: exactly ONE line, route "tool_recall", the
      rendered ids; prompt_index continues the session's sequence.
  T3  gate-passed fire with no matches / all candidates suppressed: an EMPTY-ids
      line lands (prompt_index + compaction window stay continuous), even when
      the hook then has nothing to print and takes the `if not parts` exit.
  T4  check_in_turn_recall RAISES after _route_ledger ran: the pending record
      is DISCARDED (the model saw no recall text).
  T5  gate not passed / disabled / empty ENGRAM marker: no line, as before.
  T6  other callers (positional, no out-param): same return string, no shown
      record appended any more; the route ledger is still written.
  T7  the route ledger call (arguments and position) is unchanged.

Labels: [RED-ON-BASE] cells fail on the pre-change hook; [GUARD] cells are
green on base and pin behaviour that must not move.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
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
TRIPWIRE_PATH = HOOK_DIR / "engram-lesson-tripwire-hook.py"
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))
import _hooklib  # noqa: E402

SESSION = "sess-3670-1"
STARTED = "2026-10-09T00:00:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"


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


def _record(session=SESSION):
    p = _eh() / "sessions" / f"{session}.shown.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _seed_record(ids, *, route="prompt_surface", idx=0, session=SESSION):
    row = {"ts": 1, "route": route, "compaction_count": None, "prompt_index": idx,
           "ids": list(ids)}
    with open(_eh() / "sessions" / f"{session}.shown.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def _probe_lines(tmp_path, route, session=SESSION):
    p = tmp_path / "home" / ".engram" / "probe" / "mods-recall" / session / "routes" / f"{route}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _live_probe(tmp_path, session=SESSION):
    (tmp_path / "home" / ".engram" / "probe" / "mods-recall" / session).mkdir(parents=True)


def _load(tag, terms=("alphaterm", "betaterm"), enabled=True):
    import sqlite3
    (_eh() / "config.json").write_text(json.dumps(
        {"auto_surface": {"in_turn_recall": {"enabled": enabled}}}))
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
    spec = importlib.util.spec_from_file_location(f"tripwire_3670_{tag}", TRIPWIRE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _response(ids):
    top = [{"id": n, "type": "observation", "created_at": OLD,
            "recall_summary": f"summary of {n}", "recall_keywords": ["kw"]} for n in ids]
    return {"match_count": len(ids), "special_nodes": [], "top_claims": top,
            "matched_ids": list(ids)}


class _RaisingOut(io.StringIO):
    def write(self, s):
        raise OSError("stdout closed")


def _hook_input(command="alphaterm betaterm", session=SESSION):
    return {"session_id": session, "transcript_path": "", "tool_name": "Bash",
            "tool_input": {"command": command}}


def _main(mod, monkeypatch, response, *, stdout=None, recent=None, command="alphaterm betaterm"):
    """Run mod.main() in-process. Returns (stdout text, raised exception or None)."""
    monkeypatch.setattr(mod, "_query_recall_daemon", lambda q, top_k=5: response)
    state = _eh() / "in-turn-recall-state.json"
    if state.exists():
        state.unlink()
    if recent:
        state.write_text(json.dumps({"recent_ids": list(recent)}))
    out, err = (stdout if stdout is not None else io.StringIO()), io.StringIO()
    stdin = json.dumps(_hook_input(command))
    raised = None
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))), \
            patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            raised = e
    return (out.getvalue() if not isinstance(out, _RaisingOut) else ""), raised


# ----------------------------------------------------------------------- T1

def test_t1_a_print_that_raises_records_nothing(monkeypatch):
    """[RED-ON-BASE] The model saw nothing, so no line may land.
    Mutant: append before the print -> red."""
    mod = _load("t1a")
    _main(mod, monkeypatch, _response(_ids(1, 2)), stdout=_RaisingOut())
    assert _record() == []


def test_t1_the_record_is_written_after_the_print(monkeypatch):
    """[RED-ON-BASE] Order of events: print first, then append.
    Mutant: append before the print -> red."""
    mod = _load("t1b")
    order = []
    real = mod.append_shown_record
    monkeypatch.setattr(mod, "append_shown_record",
                        lambda *a, **kw: (order.append("append"), real(*a, **kw)))

    class _Out(io.StringIO):
        def write(self, s):
            if s.strip():
                order.append("print")
            return super().write(s)

    _main(mod, monkeypatch, _response(_ids(1)), stdout=_Out())
    assert order == ["print", "append"]


def test_t1_a_failed_flush_records_nothing(monkeypatch):
    """[RED-ON-BASE] print succeeds into the buffer but the flush fails: the line
    may not have reached the reader, so no record.
    Mutant: append right after print, before flush -> red."""
    mod = _load("t1c")

    class _FlushFail(io.StringIO):
        def flush(self):
            raise OSError("broken pipe")

    _main(mod, monkeypatch, _response(_ids(1)), stdout=_FlushFail())
    assert _record() == []


# ----------------------------------------------------------------------- T2

def test_t2_rendered_and_printed_lands_exactly_one_line(monkeypatch):
    """[GUARD] one line, route tool_recall, the rendered ids; prompt_index
    continues the sequence started by an earlier surface line."""
    mod = _load("t2")
    _seed_record(_ids(9), idx=0)
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert "[ob_1]" in out and "[ob_2]" in out
    rows = _record()
    assert len(rows) == 2
    assert rows[1]["route"] == "tool_recall"
    assert rows[1]["ids"] == _ids(1, 2)
    assert rows[1]["prompt_index"] == 1


def test_t2_only_rendered_ids_not_cap_cut_ones(monkeypatch):
    """[GUARD] max_lines 3: the fourth candidate is cut and is not recorded."""
    mod = _load("t2b")
    _main(mod, monkeypatch, _response(_ids(1, 2, 3, 4)))
    assert [r["ids"] for r in _record()] == [_ids(1, 2, 3)]


# ----------------------------------------------------------------------- T3

def test_t3_daemon_returned_nothing_lands_an_empty_line_though_nothing_printed(monkeypatch):
    """[GUARD on base; the after-exit half is the new path] nothing is printed
    (`if not parts` exit); the empty-ids line still lands.
    Mutant: skip the record when nothing is printed -> red."""
    mod = _load("t3a")
    out, _ = _main(mod, monkeypatch, None)
    assert out == ""
    rows = _record()
    assert len(rows) == 1 and rows[0]["route"] == "tool_recall" and rows[0]["ids"] == []


def test_t3_zero_match_count_lands_an_empty_line(monkeypatch):
    """[GUARD] match_count 0 -> same."""
    mod = _load("t3b")
    _main(mod, monkeypatch, {"match_count": 0, "special_nodes": [], "top_claims": []})
    assert [r["ids"] for r in _record()] == [[]]


def test_t3_all_candidates_suppressed_lands_an_empty_line(monkeypatch):
    """[GUARD] every candidate deduped -> nothing rendered, empty line.
    Mutant: skip the record when nothing is printed -> red."""
    mod = _load("t3c")
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)), recent=_ids(1, 2))
    assert out == ""
    assert [r["ids"] for r in _record()] == [[]]


def test_t3_prompt_index_stays_continuous_across_an_empty_fire(monkeypatch):
    """[GUARD] rendered, empty, rendered -> indexes 0, 1, 2."""
    mod = _load("t3d")
    _main(mod, monkeypatch, _response(_ids(1)))
    _main(mod, monkeypatch, None)
    _main(mod, monkeypatch, _response(_ids(3)))
    rows = _record()
    assert [r["prompt_index"] for r in rows] == [0, 1, 2]
    assert [r["ids"] for r in rows] == [_ids(1), [], _ids(3)]


def test_t3_empty_line_is_written_after_the_print_when_other_parts_print(monkeypatch):
    """[RED-ON-BASE] a fire whose recall is empty but whose response still prints
    (another part present): the empty line lands, after the print."""
    mod = _load("t3e")
    order = []
    real = mod.append_shown_record
    monkeypatch.setattr(mod, "append_shown_record",
                        lambda *a, **kw: (order.append("append"), real(*a, **kw)))

    class _Out(io.StringIO):
        def write(self, s):
            if s.strip():
                order.append("print")
            return super().write(s)

    # a loader-failure notice is a printable part independent of recall
    def _failing_loader(*a, **kw):
        mod.LOADER_FAILURES.append({"loader": "load_tripwires", "exc_type": "OSError"})
        return []
    monkeypatch.setattr(mod, "load_tripwires", _failing_loader)
    out, _ = _main(mod, monkeypatch, None, stdout=_Out())
    assert "additionalContext" in out and "load_tripwires failed" in out
    assert order == ["print", "append"]
    assert [r["ids"] for r in _record()] == [[]]


# ----------------------------------------------------------------------- T4

def test_t4_an_exception_after_the_ledger_discards_the_pending_record_rendered(monkeypatch):
    """[RED-ON-BASE] check_in_turn_recall raises AFTER _route_ledger ran (the
    module-level _append_ledger it calls next raises). main() sets recall = "";
    the model saw no recall text, so nothing may be recorded.
    Mutant: record whatever record_out holds even when the call raised -> red."""
    mod = _load("t4a")

    def _boom(*a, **kw):
        raise RuntimeError("ledger boom")

    monkeypatch.setattr(mod, "_append_ledger", _boom)
    out, raised = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert raised is None and out == ""
    assert _record() == []


def test_t4_an_exception_after_the_ledger_discards_the_pending_record_no_matches(monkeypatch):
    """[RED-ON-BASE] same, on the daemon-returned-nothing path (ledger call at the
    other site).  Mutant: record whatever record_out holds even when raised -> red."""
    mod = _load("t4b")

    def _boom(*a, **kw):
        raise RuntimeError("ledger boom")

    monkeypatch.setattr(mod, "_append_ledger", _boom)
    _main(mod, monkeypatch, None)
    assert _record() == []


# ----------------------------------------------------------------------- T5

def test_t5_gate_not_passed_records_nothing(monkeypatch):
    """[GUARD] no novel terms -> the gate is not passed, no line.
    Mutant: always record -> red."""
    mod = _load("t5a", terms=())
    _main(mod, monkeypatch, _response(_ids(1)))
    assert _record() == []


def test_t5_disabled_records_nothing(monkeypatch):
    """[GUARD] in_turn_recall disabled -> no line.  Mutant: always record -> red."""
    mod = _load("t5b", enabled=False)
    _main(mod, monkeypatch, _response(_ids(1)))
    assert _record() == []


def test_t5_empty_engram_marker_records_nothing(monkeypatch):
    """[GUARD] markers present but nothing inside (_recall_state == "empty") ->
    the recall is skipped, no line.  Mutant: always record -> red."""
    mod = _load("t5c")
    cmd = f"{_hooklib.ENGRAM_MARKER_OPEN}{_hooklib.ENGRAM_MARKER_CLOSE} alphaterm"
    _main(mod, monkeypatch, _response(_ids(1)), command=cmd)
    assert _record() == []


# ----------------------------------------------------------------------- T6

@pytest.mark.parametrize("with_input", [False, True])
def test_t6_other_callers_same_string_and_no_shown_record(tmp_path, monkeypatch, with_input):
    """[RED-ON-BASE for with_input=True] positional, no out-param: unchanged
    return string, no shown-record append; the route ledger is still written."""
    mod = _load(f"t6{with_input}")
    _live_probe(tmp_path)
    monkeypatch.setattr(mod, "_query_recall_daemon", lambda q, top_k=5: _response(_ids(1, 2)))
    hi = _hook_input() if with_input else None
    out = mod.check_in_turn_recall("Bash alphaterm betaterm", hi)
    assert out.startswith("[ENGRAM in-turn recall") and "[ob_1]" in out and "[ob_2]" in out
    assert _record() == []
    if with_input:
        rows = _probe_lines(tmp_path, "tool_recall")
        assert len(rows) == 1 and rows[0]["shown"] == _ids(1, 2)


def test_t6_one_positional_argument_still_works(monkeypatch):
    """[GUARD] the perf bench calls check_in_turn_recall(target) only."""
    mod = _load("t6c")
    monkeypatch.setattr(mod, "_query_recall_daemon", lambda q, top_k=5: _response(_ids(1)))
    assert "[ob_1]" in mod.check_in_turn_recall("Bash alphaterm betaterm")
    assert _record() == []


# ----------------------------------------------------------------------- T7

def test_t7_route_ledger_call_arguments_and_position_unchanged(monkeypatch):
    """[GUARD] write_route_ledger is called once, before _append_ledger, with the
    route's own render (shown ids, cut names) -- on both ledger sites."""
    mod = _load("t7")
    calls = []
    monkeypatch.setattr(mod, "write_route_ledger", lambda *a, **kw: calls.append(("route", a, kw)))
    monkeypatch.setattr(mod, "_append_ledger", lambda *a, **kw: calls.append(("fire",)))
    _main(mod, monkeypatch, _response(_ids(1, 2, 3, 4)))
    assert [c[0] for c in calls] == ["route", "fire"]
    _, a, kw = calls[0]
    assert a[0] == "tool_recall" and a[1] == SESSION
    assert a[3] == "alphaterm betaterm" and a[5] == _ids(1, 2, 3)
    assert a[6] == {"ob_4": "max_lines"}
    assert kw["source"] == "daemon" and kw["default_cut"] == "not_in_pool"
    assert kw["extra"] == {"novel_terms": ["alphaterm", "betaterm"]}

    calls.clear()
    _main(mod, monkeypatch, None)
    assert [c[0] for c in calls] == ["route", "fire"]
    _, a, _ = calls[0]
    assert a[4] is None and a[5] == [] and a[6] == {}


# ----------------------------------------------------------------------- B (round 2)
# The HARNESS cuts the TAIL of an oversized additionalContext, and the recall
# block is appended LAST, outside emit_context's bound. main() therefore records
# the recall ids only when the WHOLE joined string fits the harness budget;
# otherwise it records an EMPTY-ids line (under-record, never over-record).

def _ctx(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _with_lesson(mod, monkeypatch, nudge_chars):
    """One matching lesson whose nudge is `nudge_chars` long, rendered through the
    real emit_context tripwire path (the pattern matches the command)."""
    nudge = ("nudge " * (nudge_chars // 6 + 1))[:nudge_chars]
    monkeypatch.setattr(mod, "load_tripwires",
                        lambda *a, **kw: [("alphaterm", nudge, "ls_9999", "normal")])


def _budget(monkeypatch, mod, n):
    monkeypatch.setattr(mod, "get_harness_budgets",
                        lambda h: {"budget": n, "head_budget": n, "measured": True})


def _bypass_total_budget_pass(mod, monkeypatch):
    """#3677: the recall is not trimmed (max_chars/cut_reserve ignored) and the
    chain is never dropped, so an over-budget joined string can still be built."""
    real = mod.check_in_turn_recall
    monkeypatch.setattr(mod, "check_in_turn_recall",
                        lambda mt, hi=None, ro=None, **kw: real(mt, hi, ro))
    monkeypatch.setattr(mod, "_chain_fits", lambda parts, block, budget: True)


def test_b1_near_budget_lesson_plus_recall_records_empty_ids(monkeypatch):
    """[RED-ON-BASE] the harness budget as detected here (the unmeasured floor,
    4000, when no harness env is set; 9500 on claude-code), one ~9,000-char lesson
    nudge through the real emit_context path (the lesson block alone already
    fills the budget, the recall appended after it pushes the joined string past
    it). Printed, and an EMPTY line lands.
    Mutant: record the pending ids regardless of size -> red.
    the total-budget pass (#3677) normally prevents this state; the pass is bypassed here to keep the #3674 guard pinned."""
    mod = _load("b1")
    _bypass_total_budget_pass(mod, monkeypatch)
    _with_lesson(mod, monkeypatch, 9000)
    budget = mod.get_harness_budgets(mod.detect_harness())["budget"]
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert len(_ctx(out)) > budget  # precondition: the total really is over
    rows = _record()
    assert len(rows) == 1 and rows[0]["route"] == "tool_recall"
    assert rows[0]["ids"] == []


def test_b2_small_lesson_plus_recall_within_budget_records_full_ids(monkeypatch):
    """[GUARD] round-1 behaviour: total within budget -> the rendered ids land.
    Mutant: always record empty -> red."""
    mod = _load("b2")
    _with_lesson(mod, monkeypatch, 200)
    budget = mod.get_harness_budgets(mod.detect_harness())["budget"]
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert len(_ctx(out)) <= budget
    assert [r["ids"] for r in _record()] == [_ids(1, 2)]


def test_b3_boundary_exactly_budget_records_budget_plus_one_does_not(monkeypatch):
    """Exactly the budget -> ids recorded [GUARD]; budget + 1 -> empty ids
    [RED-ON-BASE].  Mutant: `>=` instead of `>` -> the exactly-budget half red.
    the total-budget pass (#3677) normally prevents this state; the pass is bypassed here to keep the #3674 guard pinned."""
    mod = _load("b3")
    _bypass_total_budget_pass(mod, monkeypatch)
    _budget(monkeypatch, mod, 10**6)
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    length = len(_ctx(out))
    assert [r["ids"] for r in _record()] == [_ids(1, 2)]

    session_file = _eh() / "sessions" / f"{SESSION}.shown.jsonl"
    session_file.unlink()
    _budget(monkeypatch, mod, length)  # exactly the budget
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert len(_ctx(out)) == length
    assert [r["ids"] for r in _record()] == [_ids(1, 2)]

    session_file.unlink()
    _budget(monkeypatch, mod, length - 1)  # one over
    out, _ = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert len(_ctx(out)) == length
    assert [r["ids"] for r in _record()] == [[]]


def test_b4_small_recall_after_a_huge_block_still_records_empty(monkeypatch):
    """[RED-ON-BASE] the recall block alone is far under the budget, the TOTAL is
    over: compare the total.  Mutant: compare only the recall block's length to
    the budget -> red.
    the total-budget pass (#3677) normally prevents this state; the pass is bypassed here to keep the #3674 guard pinned."""
    mod = _load("b4")
    _bypass_total_budget_pass(mod, monkeypatch)
    _budget(monkeypatch, mod, 1500)
    _with_lesson(mod, monkeypatch, 1400)
    recall_len = len(mod.check_in_turn_recall("Bash alphaterm betaterm"))
    assert recall_len < 1500  # the recall alone would fit
    out, _ = _main(mod, monkeypatch, _response(_ids(1)))
    assert len(_ctx(out)) > 1500
    assert [r["ids"] for r in _record()] == [[]]


def test_b5_budget_computation_raising_fails_open_to_empty_ids(monkeypatch):
    """[RED-ON-BASE] the budget lookup raises: the response is still printed,
    nothing is raised, and an EMPTY line lands (under-record).
    Mutant: let the exception propagate, or record the full ids on failure -> red."""
    mod = _load("b5")

    def _boom(h):
        raise RuntimeError("budget boom")

    monkeypatch.setattr(mod, "get_harness_budgets", _boom)
    out, raised = _main(mod, monkeypatch, _response(_ids(1, 2)))
    assert raised is None
    assert "[ob_1]" in _ctx(out)  # still printed
    assert [r["ids"] for r in _record()] == [[]]
