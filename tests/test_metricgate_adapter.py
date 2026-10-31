"""Contract tests for src/engram/tools/metricgate_adapter.py (A1 of the metricgate adoption PR).

Written BEFORE the implementation. The contract is the spec's A1:

  * the ONE import site: no other file under src/ or tools/ imports `metricgate` directly;
  * have_metricgate() -> bool; require_metricgate() raises MetricgateMissing(RuntimeError) naming the install
    line and saying the verdict is unavailable without the library;
  * append_run_record(...) appends one JSON object per line in metricgate's run-record shape, creates the
    file, refuses a non-finite stat, and is pure file I/O (no metricgate import);
  * judge_cell(gate, a, b) -> (verdict_name, markdown_report) through the library, MetricgateMissing when
    the library is absent.

Cells that need the library use importorskip("metricgate"): CI has no metricgate, so they SKIP and the skip is
recorded; a seat with the library installed runs them.
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "metricgate_adapter.py"
INSTALL_LINE = 'pip install "metricgate @ git+ssh://git@github.com/engram-agents/metricgate@v0.1.0"'


def _load():
    spec = importlib.util.spec_from_file_location("metricgate_adapter_under_test", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def adapter():
    return _load()


@pytest.fixture()
def no_metricgate(monkeypatch):
    """Make `import metricgate` fail, whether or not the library is installed here."""
    monkeypatch.setitem(sys.modules, "metricgate", None)


# --- the one import site -------------------------------------------------------------------------------------

def test_only_the_adapter_imports_metricgate():
    """Mutation: add a direct metricgate import to any other file under src/ or tools/ (the A6 tool, the
    lock-wait report) -> that file is a second hit -> red."""
    pattern = re.compile(r"^\s*(import\s+metricgate\b|from\s+metricgate\b)", re.MULTILINE)
    hits = set()
    for root in (REPO_ROOT / "src", REPO_ROOT / "tools"):
        for path in root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if pattern.search(text):
                hits.add(path.resolve())   # tools/<name>.py symlinks resolve to the src copy: one file, one hit
    assert hits == {ADAPTER_PATH.resolve()}, sorted(str(h) for h in hits)


# --- have_metricgate / require_metricgate --------------------------------------------------------------------

def test_have_metricgate_is_false_when_the_import_fails(adapter, no_metricgate):
    """Mutation: cache the import result at module load (a module-level flag) -> the monkeypatched absence is
    not seen -> red."""
    assert adapter.have_metricgate() is False


def test_have_metricgate_is_true_when_the_library_imports(adapter):
    pytest.importorskip("metricgate")
    assert adapter.have_metricgate() is True


def test_require_metricgate_raises_a_runtime_error_naming_the_install_line(adapter, no_metricgate):
    """Mutation: raise a bare ImportError, or omit the install line or the 'unavailable' sentence -> red."""
    assert issubclass(adapter.MetricgateMissing, RuntimeError)
    with pytest.raises(adapter.MetricgateMissing) as err:
        adapter.require_metricgate()
    msg = str(err.value)
    assert INSTALL_LINE in msg
    assert "verdict is unavailable" in msg


def test_require_metricgate_returns_the_module_when_present(adapter):
    mg = pytest.importorskip("metricgate")
    assert adapter.require_metricgate() is mg


# --- append_run_record ---------------------------------------------------------------------------------------

def _rec(adapter, path, **over):
    kw = dict(run_id="20261031T120000Z-abcd1234", ts="2026-10-31T12:00:00Z", stat=1.25, host="h1",
              build="deadbeef", load="0.42")
    kw.update(over)
    adapter.append_run_record(path, **kw)


def test_append_creates_the_file_and_writes_one_json_object_per_line(adapter, tmp_path):
    """Mutation: write a JSON array / pretty-print across lines / drop a key -> red."""
    f = tmp_path / "cell.jsonl"
    assert not f.exists()
    _rec(adapter, f)
    _rec(adapter, f, run_id="20261031T130000Z-abcd1234", ts="2026-10-31T13:00:00Z", stat=1.5)
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(x) for x in lines)
    assert first == {"run_id": "20261031T120000Z-abcd1234", "ts": "2026-10-31T12:00:00Z", "stat": 1.25,
                     "host": "h1", "build": "deadbeef", "load": "0.42"}
    assert second["stat"] == 1.5 and second["run_id"].startswith("20261031T130000Z")
    assert f.read_text(encoding="utf-8").endswith("\n")


def test_append_appends_never_truncates(adapter, tmp_path):
    """Mutation: open with 'w' -> the first record is lost -> red."""
    f = tmp_path / "cell.jsonl"
    f.write_text('{"run_id": "old"}\n', encoding="utf-8")
    _rec(adapter, f)
    assert len(f.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_append_refuses_a_non_finite_stat_and_writes_nothing(adapter, tmp_path, bad):
    """Mutation: json.dumps a NaN through (it emits the non-JSON token NaN) or create the file before the
    check -> red."""
    f = tmp_path / "cell.jsonl"
    with pytest.raises(ValueError):
        _rec(adapter, f, stat=bad)
    assert not f.exists()


def test_append_needs_no_metricgate(adapter, tmp_path, no_metricgate):
    """Pure file I/O. Mutation: import the library inside append_run_record -> red."""
    _rec(adapter, tmp_path / "cell.jsonl")


def test_appended_records_are_readable_by_the_library(adapter, tmp_path):
    mg = pytest.importorskip("metricgate")
    f = tmp_path / "cell.jsonl"
    _rec(adapter, f)
    runs = mg.read_runs_jsonl(f)
    assert runs[0]["stat"] == 1.25 and runs[0]["host"] == "h1"


# --- judge_cell ----------------------------------------------------------------------------------------------

def _gate(mg):
    return mg.preregister({
        "metric": "sequential p50 per run (ms), owner path vs baseline path", "unit": "ms",
        "population": "installed layout, real-store copy", "instrument": "planted",
        "instrument_version": "t", "design": "paired interleaved", "unit_of_analysis": "run",
        "n_planned": 6, "margin": 0.03, "block_bound": 0.10, "scale": "log_ratio",
        "direction": "lower_is_better", "paired": True, "alpha": 0.05,
        "verdict_rule": "PASS: upper <= 1.03; TRADE_OFF: estimate <= 10%; BLOCK beyond",
        "deciders": {"trade_off": "the design owner"}})


def _write_cell(adapter, tmp_path, factor):
    base = [10.00, 10.05, 9.95, 10.02, 9.98, 10.03]
    jitter = [1.002, 0.998, 1.001, 0.999, 1.003, 0.997]
    a, b = tmp_path / "baseline.jsonl", tmp_path / "owner.jsonl"
    for i, (x, j) in enumerate(zip(base, jitter)):
        rid, ts = f"20261031T12000{i}Z-aaaaaaaa", f"2026-10-31T12:00:0{i}Z"
        _rec(adapter, a, run_id=rid, ts=ts, stat=x)
        _rec(adapter, b, run_id=rid, ts=ts, stat=x * factor * j)
    return a, b


@pytest.mark.parametrize("factor,expected", [(1.0, "PASS"), (1.5, "BLOCK")])
def test_judge_cell_returns_the_verdict_name_and_the_markdown_table(adapter, tmp_path, factor, expected):
    """Planted cells whose verdict is known by construction: a neutral pair PASSes, a 50 percent regression
    BLOCKs. Mutation: return the Verdict enum itself, or return the JSON table instead of markdown -> red."""
    mg = pytest.importorskip("metricgate")
    gate = _gate(mg)
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    a, b = _write_cell(adapter, tmp_path, factor)
    verdict, md = adapter.judge_cell(gate_path, a, b)
    assert verdict == expected
    assert isinstance(md, str) and md.startswith("| Metric |") and gate["gate_sha256"] in md


def test_judge_cell_raises_metricgate_missing_when_the_library_is_absent(adapter, tmp_path, no_metricgate):
    """Mutation: swallow the absence and return a made-up verdict -> red."""
    for n in ("g.json", "a.jsonl", "b.jsonl"):
        (tmp_path / n).write_text("{}", encoding="utf-8")
    with pytest.raises(adapter.MetricgateMissing):
        adapter.judge_cell(tmp_path / "g.json", tmp_path / "a.jsonl", tmp_path / "b.jsonl")


# --- round 2: a malformed gate is a ValueError naming the key ------------------------------------------------

def test_judge_cell_names_the_missing_gate_key_in_a_value_error(adapter, tmp_path, monkeypatch):
    """A fake library whose judge raises the KeyError the real one raises for gate["n_planned"]; runs without
    the library. Mutation: let the KeyError escape (or drop the key from the message) -> red."""
    import types
    fake = types.SimpleNamespace(read_runs_jsonl=lambda p: [], report=lambda g, j: ("", None))

    def judge(gate, a, b):
        return gate["n_planned"]
    fake.judge = judge
    monkeypatch.setitem(sys.modules, "metricgate", fake)
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps({"metric": "m"}), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        adapter.judge_cell(gate_path, tmp_path / "a.jsonl", tmp_path / "b.jsonl")
    assert not isinstance(err.value, KeyError)
    assert f"gate {gate_path} is missing n_planned" in str(err.value)


def test_judge_cell_on_a_real_gate_without_n_planned_is_a_value_error(adapter, tmp_path):
    mg = pytest.importorskip("metricgate")
    gate = _gate(mg)
    del gate["n_planned"]
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    a, b = _write_cell(adapter, tmp_path, 1.0)
    with pytest.raises(ValueError, match="n_planned"):
        adapter.judge_cell(gate_path, a, b)
