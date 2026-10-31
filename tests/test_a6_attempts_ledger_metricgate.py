"""Contract tests for the attempts ledger (Addendum B, contract A6 of the metricgate adoption PR, round 3).
Written BEFORE the implementation. The suffix `metricgate` stands in for an issue number.

The blocker being closed (the colleague's read on #4296): a run that breaks its hook contract is refused by
`--emit-runs` and leaves NO trace in the cell, so one more clean run completes n=6 and the cell can PASS with
nothing saying that a run on it broke its contract. Two rules the fix satisfies: every recorded run on the cell
counts and none is dropped (B1 on #3740); a contract signal is the cell's verdict, not a missing latency sample
(the A6 pre-registration on #2652).

The contract:

  * every `--emit-runs DIR` invocation that reaches the epilogue appends ONE line to
    `DIR/a6-<hook>-<sqlite>-excl<on|off>-attempts.jsonl`:
    {"run_id", "ts", "emitted", "reason", "host", "build"}; `reason` is `contract_violation: <first violation>`,
    `source_layout`, `no_real_store_cell`, `synthetic_store`, `unreadable_context`, or null when emitted;
  * the ledger write happens BEFORE the record emission and regardless of it; a ledger that cannot be written
    -> one stderr line and NOTHING emitted;
  * `read_attempts(path)` parses the ledger (a malformed line is a ValueError naming it);
  * `judge_cell(gate, a, b, attempts_path=)`: any attempt whose reason starts with `contract_violation` ->
    verdict NO_VERDICT, reason `contract_violation_in_cell`, a report whose Verdict column says so and lists
    the run_ids; otherwise the normal verdict plus one line per non-emitted attempt:
    `attempt <run_id> at <ts>: not emitted (<reason>)`;
  * a run killed by the layout timeout writes a `layout_timeout` ledger line from the timeout path;
  * the `--gate` epilogue always passes the ledger path.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"
ADAPTER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "metricgate_adapter.py"
END = datetime.datetime(2026, 10, 31, 15, 30, 45, tzinfo=datetime.timezone.utc)
CTX = ("seat-a", "0123456789abcdef0123456789abcdef01234567", "0.31")
SQLITE = "3.45.1"
LEDGER = f"a6-stop-{SQLITE}-excloff-attempts.jsonl"


@pytest.fixture(autouse=True, scope="module")
def _restore_sys_path_and_tools():
    saved_path = list(sys.path)
    saved_tools = {k: v for k, v in sys.modules.items() if k == "tools" or k.startswith("tools.")}
    yield
    sys.path[:] = saved_path
    for key in list(sys.modules):
        if (key == "tools" or key.startswith("tools.")) and key not in saved_tools:
            del sys.modules[key]
    sys.modules.update(saved_tools)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load("engramd_a6_latency_ledger_cells", TOOL_PATH)


@pytest.fixture()
def adapter(tool, monkeypatch):
    mod = _load("metricgate_adapter_ledger_cells", ADAPTER_PATH)
    monkeypatch.setattr(tool, "_load_adapter", lambda: mod)
    return mod


@pytest.fixture()
def no_metricgate(monkeypatch):
    monkeypatch.setitem(sys.modules, "metricgate", None)


def _scell(base=10.0, owner=10.0, sqlite=SQLITE, **extra):
    rows = []
    for arm, k in (("sequential", 1.0), ("contended", 1.7), ("cold-cache", 2.1)):
        for path, p50 in (("baseline", base), ("owner", owner)):
            rows.append({"arm": arm, "path": path, "n": 300, "p50_ms": p50 * k})
    r = {"hook": "stop", "layout": "installed", "rows": rows, "answers_match": True, "n": 300,
         "env": {"platform": "x", "cpus": 4, "python": "3.12", "sqlite": sqlite}}
    r.update(extra)
    return r


def _epilogue(tool, results, i=0, **over):
    """One epilogue call; run i ends i+1 hours before END so every run_id is distinct."""
    kw = dict(hook="stop", exclusion=False, store_is_copy=True, emit_dir=None, gate_path=None,
              end=END - datetime.timedelta(hours=i + 1), context=CTX)
    kw.update(over)
    return tool.metricgate_epilogue(results, **kw)


def _ledger(d, name=LEDGER):
    return [json.loads(x) for x in (Path(d) / name).read_text(encoding="utf-8").splitlines()]


def _record_files(d):
    return sorted(p.name for p in Path(d).glob("*.jsonl") if not p.name.endswith("-attempts.jsonl"))


def _plain_gate(tmp_path):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"metric": "sequential p50 per run (ms)", "population": "installed layout",
                             "gate_sha256": "f" * 64}), encoding="utf-8")
    return p


def _real_gate(tmp_path):
    mg = pytest.importorskip("metricgate")
    gate = mg.preregister({
        "metric": "sequential p50 per run (ms), owner path vs baseline path", "unit": "ms",
        "population": "installed layout, real-store copy", "instrument": "planted", "instrument_version": "t",
        "design": "paired interleaved", "unit_of_analysis": "run", "n_planned": 6, "margin": 0.03,
        "block_bound": 0.10, "scale": "log_ratio", "direction": "lower_is_better", "paired": True,
        "alpha": 0.05, "verdict_rule": "planted", "deciders": {"trade_off": "the design owner"}})
    p = tmp_path / "gate-real.json"
    p.write_text(json.dumps(gate), encoding="utf-8")
    return p


NEUTRAL = [(10.0, 10.02), (10.1, 10.08), (9.9, 9.93), (10.05, 10.04), (9.95, 9.97), (10.02, 10.0)]


# --- the adapter: append_attempt / read_attempts ------------------------------------------------------------

def test_append_attempt_writes_one_line_with_exactly_the_six_keys(adapter, tmp_path):
    """Mutation: add or drop a key, write a pretty-printed object, or truncate instead of append -> red."""
    f = tmp_path / "x-attempts.jsonl"
    adapter.append_attempt(f, run_id="r1", ts="2026-10-31T15:00:00Z", emitted=False,
                           reason="source_layout", host="h", build="b")
    adapter.append_attempt(f, run_id="r2", ts="2026-10-31T16:00:00Z", emitted=True, reason=None,
                           host="h", build="b")
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {"run_id", "ts", "emitted", "reason", "host", "build"}
    assert first["emitted"] is False and first["reason"] == "source_layout"
    assert json.loads(lines[1])["reason"] is None and json.loads(lines[1])["emitted"] is True


def test_read_attempts_round_trips(adapter, tmp_path):
    f = tmp_path / "x-attempts.jsonl"
    adapter.append_attempt(f, run_id="r1", ts="t1", emitted=True, reason=None, host="h", build="b")
    adapter.append_attempt(f, run_id="r2", ts="t2", emitted=False, reason="layout_timeout", host="h", build="b")
    got = adapter.read_attempts(f)
    assert [a["run_id"] for a in got] == ["r1", "r2"]
    assert got[1]["reason"] == "layout_timeout"


@pytest.mark.parametrize("bad", ["not json at all", "[1, 2]", '{"run_id": "r2"}'],
                         ids=["not-json", "not-an-object", "missing-keys"])
def test_read_attempts_names_the_malformed_line(adapter, tmp_path, bad):
    """Mutation: skip the bad line silently (a dropped attempt), or raise without the line number -> red."""
    f = tmp_path / "x-attempts.jsonl"
    adapter.append_attempt(f, run_id="r1", ts="t1", emitted=True, reason=None, host="h", build="b")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(bad + "\n")
    with pytest.raises(ValueError) as err:
        adapter.read_attempts(f)
    assert "line 2" in str(err.value)


# --- judge_cell with a ledger --------------------------------------------------------------------------------

def test_a_contract_violation_in_the_ledger_is_a_no_verdict_naming_the_run_ids(adapter, tmp_path, no_metricgate):
    """The cell's verdict, not a missing sample: no record files are needed and no library either.
    Mutation: ignore the ledger, or judge the records anyway -> red."""
    f = tmp_path / "x-attempts.jsonl"
    adapter.append_attempt(f, run_id="RUN-BAD", ts="2026-10-31T10:00:00Z", emitted=False,
                           reason="contract_violation: [installed layout] answers_match is not true",
                           host="h", build="b")
    adapter.append_attempt(f, run_id="RUN-OK", ts="2026-10-31T11:00:00Z", emitted=True, reason=None,
                           host="h", build="b")
    verdict, md = adapter.judge_cell(_plain_gate(tmp_path), tmp_path / "a.jsonl", tmp_path / "b.jsonl",
                                     attempts_path=f)
    assert verdict == "NO_VERDICT"
    assert "contract_violation_in_cell" in md and "RUN-BAD" in md and "RUN-OK" not in md
    assert "Verdict" in md.splitlines()[0]


def test_non_violation_attempts_leave_the_verdict_and_add_one_tagged_line_each(adapter, tmp_path):
    """Six clean runs plus a timeout and an unreadable context: the normal verdict, and one tagged line per
    non-emitted attempt, in the stated shape. Mutation: drop the lines, tag emitted attempts too, or turn a
    timeout into NO_VERDICT -> red."""
    gate = _real_gate(tmp_path)
    a, b, led = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "x-attempts.jsonl"
    for i, (x, y) in enumerate(NEUTRAL):
        rid = f"2026103{i}"
        adapter.append_run_record(a, run_id=rid, ts=f"2026-10-31T0{i}:00:00Z", stat=x, host="h", build="b", load="0.1")
        adapter.append_run_record(b, run_id=rid, ts=f"2026-10-31T0{i}:00:00Z", stat=y, host="h", build="b", load="0.1")
        adapter.append_attempt(led, run_id=rid, ts=f"2026-10-31T0{i}:00:00Z", emitted=True, reason=None,
                               host="h", build="b")
    adapter.append_attempt(led, run_id="T-1", ts="2026-10-31T09:00:00Z", emitted=False,
                           reason="layout_timeout", host="h", build="b")
    adapter.append_attempt(led, run_id="U-1", ts="2026-10-31T09:30:00Z", emitted=False,
                           reason="unreadable_context", host="h", build="b")
    plain_v, plain_md = adapter.judge_cell(gate, a, b)
    verdict, md = adapter.judge_cell(gate, a, b, attempts_path=led)
    assert verdict == plain_v == "PASS"
    assert "attempt T-1 at 2026-10-31T09:00:00Z: not emitted (layout_timeout)" in md
    assert "attempt U-1 at 2026-10-31T09:30:00Z: not emitted (unreadable_context)" in md
    assert md.count("attempt ") == 2
    assert "attempt " not in plain_md   # without a ledger the report is exactly as before


# --- the epilogue writes the ledger -------------------------------------------------------------------------

CASES = {
    "contract_violation": dict(results=[_scell(answers_match=False)], over={}),
    "source_layout": dict(results=[_scell(layout="source")], over={}),
    "synthetic_store": dict(results=[_scell()], over=dict(store_is_copy=False)),
    "no_real_store_cell": dict(results=[_scell(hook="surface", layout="installed", store_cell="synthetic")],
                               over=dict(hook="surface")),
}


@pytest.mark.parametrize("reason", list(CASES))
def test_every_refusal_reason_leaves_a_ledger_line_and_no_record(tool, adapter, tmp_path, capsys, monkeypatch,
                                                                 reason):
    """The refusal is no longer a silent hole in the cell. Mutation: skip the ledger write, write the wrong
    reason, or emit anyway -> red."""
    c = CASES[reason]
    monkeypatch.setattr(tool, "surface_contract_violations", lambda r: [])   # the planted surface cell is bare
    hook = c["over"].get("hook", "stop")
    _epilogue(tool, c["results"], emit_dir=tmp_path / "runs", **c["over"])
    capsys.readouterr()
    led = _ledger(tmp_path / "runs", f"a6-{hook}-{SQLITE}-excloff-attempts.jsonl")
    assert len(led) == 1
    line = led[0]
    assert set(line) == {"run_id", "ts", "emitted", "reason", "host", "build"}
    assert line["emitted"] is False
    assert line["reason"].startswith(reason)
    assert (line["host"], line["build"]) == (CTX[0], CTX[1])
    assert line["ts"] == "2026-10-31T14:30:45Z"
    assert _record_files(tmp_path / "runs") == []


def test_the_contract_violation_reason_carries_the_first_violation_text(tool, adapter, tmp_path):
    _epilogue(tool, [_scell(answers_match=False)], emit_dir=tmp_path / "runs")
    reason = _ledger(tmp_path / "runs")[0]["reason"]
    assert reason.startswith("contract_violation: ") and "answers_match is not true" in reason


def test_an_unreadable_context_leaves_a_ledger_line_with_the_reason_and_no_record(tool, adapter, tmp_path,
                                                                                  capsys, monkeypatch):
    """No made-up values in a RECORD; the ledger still says a run happened. Mutation: no ledger line -> red."""
    def boom():
        raise RuntimeError("cannot read the load average (planted)")
    monkeypatch.setattr(tool, "_run_context", boom)
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs", context=None)
    capsys.readouterr()
    line = _ledger(tmp_path / "runs")[0]
    assert line["reason"] == "unreadable_context" and line["emitted"] is False
    assert _record_files(tmp_path / "runs") == []


def test_an_emitted_run_has_a_ledger_line_with_a_null_reason_and_the_records_run_id(tool, adapter, tmp_path):
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs")
    line = _ledger(tmp_path / "runs")[0]
    assert line["emitted"] is True and line["reason"] is None
    rec = json.loads((tmp_path / "runs" / f"a6-stop-{SQLITE}-excloff-owner.jsonl").read_text().splitlines()[0])
    assert line["run_id"] == rec["run_id"]


def test_the_ledger_is_written_before_the_emission(tool, adapter, tmp_path, monkeypatch):
    """Mutation: write the ledger after emit_gate_runs -> an emission that blows up leaves no line -> red."""
    def boom(*a, **k):
        raise RuntimeError("planted emission failure")
    monkeypatch.setattr(tool, "emit_gate_runs", boom)
    with pytest.raises(RuntimeError, match="planted"):
        _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs")
    assert len(_ledger(tmp_path / "runs")) == 1


@pytest.mark.parametrize("results", [[_scell()], [_scell(answers_match=False)]], ids=["clean", "violating"])
def test_an_unwritable_ledger_emits_nothing_and_prints_one_line(tool, adapter, tmp_path, capsys, results):
    """A cell without a ledger has no record. Mutation: emit the records anyway, print nothing, or crash -> red."""
    runs = tmp_path / "runs"
    (runs / LEDGER).mkdir(parents=True)          # the ledger path is a directory: open() fails
    gate = _plain_gate(tmp_path)
    _epilogue(tool, results, emit_dir=runs, gate_path=gate)
    out = capsys.readouterr()
    assert out.out == ""
    lines = out.err.strip().splitlines()
    assert len(lines) == 1 and "ledger" in lines[0]
    assert _record_files(runs) == []


# --- the cell verdicts through the epilogue -----------------------------------------------------------------

def test_a_violation_then_six_clean_runs_is_a_no_verdict_naming_the_run(tool, adapter, tmp_path, capsys):
    """THE blocker. The cell that cannot PASS after a contract break. Library-free: the NO_VERDICT stands on the
    ledger. Mutation: skip the ledger write -> the six clean runs complete the cell and it is judged -> red."""
    runs, gate = tmp_path / "runs", _plain_gate(tmp_path)
    _epilogue(tool, [_scell(answers_match=False)], i=0, emit_dir=runs)
    bad_id = _ledger(runs)[0]["run_id"]
    capsys.readouterr()
    for i, (x, y) in enumerate(NEUTRAL, start=1):
        _epilogue(tool, [_scell(base=x, owner=y)], i=i, emit_dir=runs, gate_path=gate if i == 6 else None)
    out = capsys.readouterr()
    assert "NO_VERDICT" in out.out and "contract_violation_in_cell" in out.out and bad_id in out.out
    assert len(_record_files(runs)) == 2 and len(_ledger(runs)) == 7


def test_the_refused_run_itself_reports_the_no_verdict_when_gate_is_given(tool, adapter, tmp_path, capsys):
    """The run that broke its contract, with --gate: the cell's verdict is printed then, not later.
    Mutation: return at the refusal before the gate step -> red."""
    _epilogue(tool, [_scell(answers_match=False)], emit_dir=tmp_path / "runs", gate_path=_plain_gate(tmp_path))
    out = capsys.readouterr()
    assert "metricgate verdict (R12): NO_VERDICT" in out.out and "contract_violation_in_cell" in out.out


def test_a_timeout_then_six_clean_runs_keeps_the_verdict_and_shows_one_tagged_line(tool, adapter, tmp_path, capsys):
    """Mutation: do not pass the ledger path in the --gate epilogue -> no tagged line -> red."""
    runs, gate = tmp_path / "runs", _real_gate(tmp_path)
    tool.record_layout_timeout(runs, hook="stop", exclusion=False, end=END - datetime.timedelta(hours=9),
                               context=CTX, sqlite_version=SQLITE)
    tid = _ledger(runs)[0]["run_id"]
    for i, (x, y) in enumerate(NEUTRAL):
        _epilogue(tool, [_scell(base=x, owner=y)], i=i, emit_dir=runs, gate_path=gate if i == 5 else None)
    out = capsys.readouterr().out
    assert "PASS" in out and "contract_violation_in_cell" not in out
    assert f"attempt {tid} at 2026-10-31T06:30:45Z: not emitted (layout_timeout)" in out


# --- the layout-timeout path --------------------------------------------------------------------------------

def test_record_layout_timeout_writes_one_not_emitted_line(tool, adapter, tmp_path):
    tool.record_layout_timeout(tmp_path / "runs", hook="stop", exclusion=True, end=END, context=CTX,
                               sqlite_version=SQLITE)
    line = _ledger(tmp_path / "runs", f"a6-stop-{SQLITE}-excl" "on-attempts.jsonl")[0]
    assert line["emitted"] is False and line["reason"] == "layout_timeout"
    assert line["ts"] == "2026-10-31T15:30:45Z" and line["host"] == CTX[0] and line["build"] == CTX[1]


def test_main_writes_the_timeout_line_when_the_layout_child_is_killed(tool, adapter, tmp_path, capsys, monkeypatch):
    """The path main() takes when the child outran --layout-timeout: it never reaches the epilogue, so the
    handler writes the ledger line. Mutation: leave the handler as it was -> no ledger -> red."""
    monkeypatch.setattr(tool, "source_layout", lambda: object())

    def timeout(layout, layout_timeout=0, **kw):
        raise subprocess.TimeoutExpired("child", layout_timeout)
    monkeypatch.setattr(tool, "measure_in_layout", timeout)
    monkeypatch.setattr(tool, "_run_context", lambda: CTX)
    runs = tmp_path / "runs"
    rc = tool.main(["--hook", "stop", "--layout", "source", "--emit-runs", str(runs)])
    assert rc == tool.EXIT_LAYOUT_TIMEOUT
    line = _ledger(runs, f"a6-stop-{sqlite3.sqlite_version}-excloff-attempts.jsonl")[0]
    assert line["reason"] == "layout_timeout" and line["emitted"] is False
    assert _record_files(runs) == []
    assert "exceeded --layout-timeout" in capsys.readouterr().err


def test_main_without_emit_runs_writes_no_ledger_on_timeout(tool, adapter, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(tool, "source_layout", lambda: object())

    def timeout(layout, layout_timeout=0, **kw):
        raise subprocess.TimeoutExpired("child", layout_timeout)
    monkeypatch.setattr(tool, "measure_in_layout", timeout)
    monkeypatch.chdir(tmp_path)
    assert tool.main(["--hook", "stop", "--layout", "source"]) == tool.EXIT_LAYOUT_TIMEOUT
    assert list(tmp_path.iterdir()) == []
