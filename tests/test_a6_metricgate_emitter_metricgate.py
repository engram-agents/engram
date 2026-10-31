"""Contract tests for the A6 latency tool's metricgate hooks (A2 of the metricgate adoption PR). Written BEFORE
the implementation. The suffix `metricgate` stands in for an issue number (none was given at dispatch).

The contract (spec A2):

  * `--emit-runs DIR`: after a completed run, for the SEQUENTIAL arm of the gate cell (installed layout,
    real-store copy) append TWO records with the SAME run_id, one to `...-baseline.jsonl` and one to
    `...-owner.jsonl`, named `a6-<hook>-<sqlite_version>-excl<on|off>-<path>.jsonl`; stat = that path's
    sequential p50 in ms as read from the result rows (key `p50_ms`), ts = the run's end time,
    run_id = `<ts compact>-<first 8 hex of sha256 over build + hook + sqlite_version + exclusion>`;
  * nothing is emitted for the source layout, `--no-real-store`, or a run that did not complete, and one
    stderr line says so;
  * `--gate GATE.json` with the library absent prints ONE stderr line (the MetricgateMissing message) and
    leaves the stdout unchanged; with the library present the metricgate report is printed under the heading
    `metricgate verdict (R12)`; without `--gate` nothing changes;
  * the verdict line is relabelled "point ratio (not the gate; ...)", `passes` is kept, and a sibling key
    `gate_is_metricgate: true` is added.

The tool needs an owner and a built layout to run, so these cells unit-test the emitter and the epilogue on a
fake result dict and never run the tool itself.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"
ADAPTER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "metricgate_adapter.py"
POINT_RATIO = "point ratio (not the gate; the gate is the metricgate interval against gate.json)"
END = datetime.datetime(2026, 10, 31, 15, 30, 45, tzinfo=datetime.timezone.utc)
CTX = dict(host="seat-a", build="0123456789abcdef0123456789abcdef01234567", load="0.31")


@pytest.fixture(autouse=True, scope="module")
def _restore_sys_path_and_tools():
    """Same recipe as tests/test_engramd_a6_latency.py (#4023): put sys.path and the cached `tools` modules
    back when the module ends."""
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
    return _load("engramd_a6_latency_metricgate_cells", TOOL_PATH)


@pytest.fixture()
def adapter(tool, monkeypatch):
    """The tool's adapter handle, pinned to the file under test (no `tools` package resolution in the cell)."""
    mod = _load("metricgate_adapter_emitter_cells", ADAPTER_PATH)
    monkeypatch.setattr(tool, "_load_adapter", lambda: mod)
    return mod


def _rows(base_p50, owner_p50):
    out = []
    for arm, k in (("sequential", 1.0), ("contended", 1.7), ("cold-cache", 2.1)):
        for path, p50 in (("baseline", base_p50), ("owner", owner_p50)):
            out.append({"arm": arm, "path": path, "n": 300, "p50_ms": p50 * k, "p99_ms": p50 * k * 1.5,
                        "mean_ms": p50 * k, "min_ms": p50 * k * 0.9})
    return out


def _cell(layout="installed", store_cell="real-store", base=10.0, owner=9.5, sqlite="3.45.1", hook="surface",
          **extra):
    r = {"hook": hook, "layout": layout, "rows": _rows(base, owner), "answers_match": True, "n": 300,
         "env": {"platform": "x", "cpus": 4, "python": "3.12", "sqlite": sqlite}}
    if store_cell is not None:
        r["store_cell"] = store_cell
    r.update(extra)
    return r


def _emit(tool, results, out_dir, **over):
    kw = dict(hook="surface", exclusion=False, store_is_copy=True, out_dir=out_dir, end=END, **CTX)
    kw.update(over)
    return tool.emit_gate_runs(results, **kw)


def _no_records(out_dir):
    """No run RECORD was written. Since round 3 a refused run still leaves its attempts-ledger line in the
    directory, so 'nothing emitted' is read off the record files, not off the directory's absence."""
    d = Path(out_dir)
    assert not d.exists() or [p for p in d.glob("*.jsonl") if not p.name.endswith("-attempts.jsonl")] == []


def _lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


# --- the emitter ---------------------------------------------------------------------------------------------

def test_two_records_with_one_run_id_and_the_named_files(tool, tmp_path):
    """Mutation: write only one path, give the two records different run_ids, or misname the files -> red."""
    paths, why = _emit(tool, [_cell()], tmp_path / "runs")
    assert why is None
    names = sorted(p.name for p in paths)
    assert names == ["a6-surface-3.45.1-excloff-baseline.jsonl", "a6-surface-3.45.1-excloff-owner.jsonl"]
    base = _lines(tmp_path / "runs" / names[0])
    owner = _lines(tmp_path / "runs" / names[1])
    assert len(base) == len(owner) == 1
    assert base[0]["run_id"] == owner[0]["run_id"]


def test_stat_is_the_sequential_p50_of_each_path_read_from_the_rows(tool, tmp_path):
    """The contended and cold-cache rows carry other p50s (1.7x, 2.1x). Mutation: take the first p50 row of
    the path, the mean, the p99, or another arm -> red."""
    _emit(tool, [_cell(base=10.0, owner=9.5)], tmp_path)
    assert _lines(tmp_path / "a6-surface-3.45.1-excloff-baseline.jsonl")[0]["stat"] == 10.0
    assert _lines(tmp_path / "a6-surface-3.45.1-excloff-owner.jsonl")[0]["stat"] == 9.5


def test_record_fields_ts_host_build_load(tool, tmp_path):
    """Mutation: local time instead of UTC, drop a context key, or swap host and build -> red."""
    _emit(tool, [_cell()], tmp_path)
    rec = _lines(tmp_path / "a6-surface-3.45.1-excloff-owner.jsonl")[0]
    assert rec["ts"] == "2026-10-31T15:30:45Z"
    assert (rec["host"], rec["build"], rec["load"]) == (CTX["host"], CTX["build"], CTX["load"])


@pytest.mark.parametrize("hook,excl,sqlite", [("surface", False, "3.45.1"), ("tripwire", True, "3.53.0"),
                                              ("tripwire", False, "3.53.0")])
def test_run_id_is_the_compact_ts_and_eight_hex_of_the_cell_hash(tool, tmp_path, hook, excl, sqlite):
    """The hash covers build + hook + sqlite_version + exclusion. Mutation: leave one of them out -> two cells
    that differ only in it share a run_id suffix -> red."""
    store_cell = "real-store" if hook == "surface" else None
    _emit(tool, [_cell(store_cell=store_cell, hook=hook, sqlite=sqlite)], tmp_path, hook=hook, exclusion=excl)
    f = tmp_path / f"a6-{hook}-{sqlite}-excl{'on' if excl else 'off'}-owner.jsonl"
    rid = _lines(f)[0]["run_id"]
    want = hashlib.sha256("|".join([CTX["build"], hook, sqlite, "on" if excl else "off"]).encode()).hexdigest()[:8]
    assert rid == f"20261031T153045Z-{want}"
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", rid)


def test_the_hash_part_differs_when_only_the_exclusion_differs(tool, tmp_path):
    _emit(tool, [_cell(store_cell=None, hook="tripwire")], tmp_path / "off", hook="tripwire", exclusion=False)
    _emit(tool, [_cell(store_cell=None, hook="tripwire")], tmp_path / "on", hook="tripwire", exclusion=True)
    a = _lines(tmp_path / "off" / "a6-tripwire-3.45.1-excloff-owner.jsonl")[0]["run_id"]
    b = _lines(tmp_path / "on" / "a6-tripwire-3.45.1-exclon-owner.jsonl")[0]["run_id"]
    assert a.split("-")[1] != b.split("-")[1]


def test_the_gate_cell_is_the_installed_real_store_copy_not_its_neighbours(tool, tmp_path):
    """Other cells carry other p50s. Mutation: take the source layout, or the synthetic cell, or the last
    result -> red."""
    results = [_cell("source", "real-store", base=1.0, owner=1.0), _cell("installed", "synthetic", base=2.0, owner=2.0),
               _cell("installed", "real-store", base=10.0, owner=9.5), _cell("source", "synthetic", base=3.0, owner=3.0)]
    _emit(tool, results, tmp_path)
    assert _lines(tmp_path / "a6-surface-3.45.1-excloff-baseline.jsonl")[0]["stat"] == 10.0


@pytest.mark.parametrize("results", [
    [_cell("source", "real-store"), _cell("source", "synthetic")],      # --layout source
    [_cell("installed", "synthetic")],                                  # --no-real-store
    [],
], ids=["source-layout", "no-real-store", "nothing"])
def test_nothing_is_emitted_without_the_gate_cell_and_the_reason_is_returned(tool, tmp_path, results):
    """Mutation: emit from whatever cell exists -> red. The out dir is not even created."""
    paths, why = _emit(tool, results, tmp_path / "runs")
    assert paths == [] and isinstance(why, str) and why
    assert not (tmp_path / "runs").exists()


def test_a_non_surface_hook_emits_only_from_a_store_copy(tool, tmp_path):
    """The gate files register 'real-store copy': a synthetic store must not land in that cell's file.
    Mutation: ignore store_is_copy -> red."""
    r = _cell(store_cell=None, hook="stop")
    paths, why = _emit(tool, [r], tmp_path, hook="stop", store_is_copy=False)
    assert paths == [] and why
    paths, why = _emit(tool, [r], tmp_path, hook="stop", store_is_copy=True)
    assert why is None and len(paths) == 2


def test_rows_without_a_sequential_p50_emit_nothing_and_say_why(tool, tmp_path):
    """Mutation: crash with KeyError, or emit a zero -> red."""
    r = _cell()
    r["rows"] = [x for x in r["rows"] if x["arm"] != "sequential"]
    paths, why = _emit(tool, [r], tmp_path)
    assert paths == [] and "sequential" in why


def test_repeated_runs_append_one_pair_each_with_distinct_run_ids(tool, tmp_path):
    """Mutation: open the files for write -> the cell never accumulates the planned n -> red."""
    _emit(tool, [_cell()], tmp_path)
    _emit(tool, [_cell()], tmp_path, end=END + datetime.timedelta(minutes=5))
    base = _lines(tmp_path / "a6-surface-3.45.1-excloff-baseline.jsonl")
    assert len(base) == 2 and base[0]["run_id"] != base[1]["run_id"]


def _scell(**kw):
    """A stop-hook cell (no store_cell axis; the epilogue's contract check for stop is answers_match)."""
    return _cell(hook="stop", store_cell=None, **kw)


# --- the epilogue: --emit-runs / --gate ----------------------------------------------------------------------

def _epilogue(tool, results, **over):
    kw = dict(hook="stop", exclusion=False, store_is_copy=True, emit_dir=None, gate_path=None, end=END,
              context=(CTX["host"], CTX["build"], CTX["load"]))
    kw.update(over)
    return tool.metricgate_epilogue(results, **kw)


def test_without_the_new_flags_nothing_is_printed_or_written(tool, adapter, tmp_path, capsys):
    """Unchanged output. Mutation: print a header or a notice when neither flag is given -> red."""
    _epilogue(tool, [_scell()])
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_emit_runs_alone_writes_the_files_and_prints_nothing_on_stdout(tool, adapter, tmp_path, capsys):
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs")
    out = capsys.readouterr()
    assert out.out == ""
    assert (tmp_path / "runs" / "a6-stop-3.45.1-excloff-owner.jsonl").is_file()


def test_the_no_gate_cell_case_says_so_on_stderr_in_one_line(tool, adapter, tmp_path, capsys):
    """Mutation: stay silent, or print to stdout -> red."""
    _epilogue(tool, [_scell(layout="source")], emit_dir=tmp_path / "runs")
    out = capsys.readouterr()
    assert out.out == ""
    assert len(out.err.strip().splitlines()) == 1 and "emit" in out.err.lower()
    _no_records(tmp_path / "runs")


def test_a_run_with_contract_violations_is_not_emitted(tool, adapter, tmp_path, capsys):
    """A run whose answers do not match is not a latency sample (R11: correctness first). Mutation: emit
    anyway -> red."""
    bad = _scell(answers_match=False)
    _epilogue(tool, [bad], hook="stop", emit_dir=tmp_path / "runs")
    out = capsys.readouterr()
    _no_records(tmp_path / "runs")
    ledger = _lines(tmp_path / "runs" / "a6-stop-3.45.1-excloff-attempts.jsonl")
    assert len(ledger) == 1 and ledger[0]["emitted"] is False and ledger[0]["reason"].startswith("contract_violation")
    assert len(out.err.strip().splitlines()) == 1


def test_gate_with_the_library_absent_prints_one_named_stderr_line_and_leaves_stdout_alone(
        tool, adapter, tmp_path, capsys, monkeypatch):
    """Mutation: raise out of main, print the line to stdout, print it twice, or print a verdict anyway -> red."""
    monkeypatch.setattr(adapter, "have_metricgate", lambda: False)
    gate = tmp_path / "gate.json"
    gate.write_text("{}", encoding="utf-8")
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs", gate_path=gate)
    out = capsys.readouterr()
    assert out.out == ""
    lines = out.err.strip().splitlines()
    assert len(lines) == 1
    assert 'pip install "metricgate @ git+ssh://git@github.com/engram-agents/metricgate@v0.1.0"' in lines[0]
    assert "verdict is unavailable" in lines[0]


def test_gate_with_the_library_present_prints_the_report_under_its_heading(tool, adapter, tmp_path, capsys):
    """Six runs of a neutral cell, then the epilogue's own run is the sixth: the report appears under the
    heading. Mutation: omit the heading, or judge the wrong pair of files -> red."""
    mg = pytest.importorskip("metricgate")
    gate = mg.preregister({
        "metric": "sequential p50 per run (ms), owner path vs baseline path", "unit": "ms",
        "population": "installed layout, real-store copy", "instrument": "planted", "instrument_version": "t",
        "design": "paired interleaved", "unit_of_analysis": "run", "n_planned": 6, "margin": 0.03,
        "block_bound": 0.10, "scale": "log_ratio", "direction": "lower_is_better", "paired": True,
        "alpha": 0.05, "verdict_rule": "planted", "deciders": {"trade_off": "the design owner"}})
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    runs = tmp_path / "runs"
    for i, (b, o) in enumerate([(10.0, 10.02), (10.1, 10.08), (9.9, 9.93), (10.05, 10.04), (9.95, 9.97)]):
        _emit(tool, [_scell(base=b, owner=o)], runs, hook="stop", end=END - datetime.timedelta(hours=i + 1))
    _epilogue(tool, [_scell(base=10.02, owner=10.0)], emit_dir=runs, gate_path=gate_path)
    out = capsys.readouterr()
    assert "metricgate verdict (R12)" in out.out
    assert "| Metric |" in out.out and gate["gate_sha256"] in out.out
    assert "PASS" in out.out
    assert out.err == ""


def test_the_epilogue_reads_the_shared_contract_helper(tool, adapter, tmp_path, capsys, monkeypatch):
    """Round-2 item 1: ONE `hook_contract_violations` serves main's post-table checks and the epilogue's R11
    guard. A clean run plus a helper that reports a violation must be refused. Mutation: let the epilogue keep
    its own copy of the per-hook checks -> the planted violation is never seen, the files are written -> red."""
    monkeypatch.setattr(tool, "hook_contract_violations", lambda hook, results: ["planted violation"])
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs")
    out = capsys.readouterr()
    _no_records(tmp_path / "runs")
    lines = out.err.strip().splitlines()
    assert len(lines) == 1 and "planted violation" in lines[0]


def test_the_shared_helper_is_empty_for_a_clean_run_and_names_a_surface_run_without_its_gate_cell(tool):
    assert tool.hook_contract_violations("stop", [_scell()]) == []
    bad = tool.hook_contract_violations("surface", [_cell("installed", "synthetic")])
    assert any("real-store" in v for v in bad)


def test_an_unreadable_context_emits_nothing_and_says_so_in_one_stderr_line(tool, adapter, tmp_path, capsys,
                                                                            monkeypatch):
    """Round-2 item 6. Mutation: crash with the RuntimeError, or write a record with a made-up host -> red."""
    def boom():
        raise RuntimeError("cannot read the load average (planted)")
    monkeypatch.setattr(tool, "_run_context", boom)
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs", context=None)
    out = capsys.readouterr()
    assert out.out == ""
    _no_records(tmp_path / "runs")
    lines = out.err.strip().splitlines()
    assert len(lines) == 1 and "nothing emitted" in lines[0] and "planted" in lines[0]


def _scratch_repo(tmp_path):
    import subprocess
    repo = tmp_path / "tree"
    repo.mkdir()
    vcs = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-C", str(repo)]
    subprocess.run(vcs + ["init", "-q"], check=True)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(vcs + ["add", "tracked.txt"], check=True)
    subprocess.run(vcs + ["commit", "-q", "-m", "c"], check=True)
    sha = subprocess.run(vcs + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def test_run_context_build_is_the_commit_and_gains_dirty_only_when_a_tracked_file_differs(tool, tmp_path,
                                                                                         monkeypatch):
    """Round-2 item 6. Mutation: never add the suffix, always add it, or count an untracked file as dirt -> red."""
    repo, sha = _scratch_repo(tmp_path)
    monkeypatch.setattr(tool, "REPO_ROOT", repo)
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    assert tool._run_context()[1] == sha
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    assert tool._run_context()[1] == sha + "+dirty"


def test_run_context_outside_a_checkout_raises_runtime_error(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    with pytest.raises(RuntimeError):
        tool._run_context()


def test_a_malformed_gate_is_one_stderr_line_without_a_traceback(tool, adapter, tmp_path, capsys, monkeypatch):
    """Round-2 item 2: judge_cell raises ValueError for a gate missing a key; the epilogue turns it into its one
    line. Mutation: let it escape (no except) or print the traceback -> red."""
    def bad(gate_path, a, b, attempts_path=None):
        raise ValueError(f"gate {gate_path} is missing n_planned")
    monkeypatch.setattr(adapter, "judge_cell", bad)
    monkeypatch.setattr(adapter, "have_metricgate", lambda: True)
    gate = tmp_path / "gate.json"
    gate.write_text("{}", encoding="utf-8")
    _epilogue(tool, [_scell()], emit_dir=tmp_path / "runs", gate_path=gate)
    out = capsys.readouterr()
    assert out.out == ""
    lines = out.err.strip().splitlines()
    assert len(lines) == 1 and "n_planned" in lines[0] and "Traceback" not in out.err


# --- the relabelled verdict line and the sibling key -------------------------------------------------------

def _gate_cell_results(ratio):
    return [_cell("installed", "real-store", base=10.0, owner=10.0 * ratio)]


@pytest.mark.parametrize("ratio", [0.97, 1.04])
def test_the_surface_verdict_is_labelled_a_point_ratio_and_the_keys_are_kept(tool, ratio):
    """`passes` stays for compatibility; the sibling key is added. Mutation: drop either key, or leave the
    verdict line unlabelled -> red."""
    gate = tool.surface_gate(_gate_cell_results(ratio))
    assert POINT_RATIO in gate["verdict"]
    assert gate["gate_is_metricgate"] is True
    assert gate["passes"] is (ratio <= 1.0)


def test_the_no_gate_cell_result_also_carries_the_sibling_key(tool):
    gate = tool.surface_gate([])
    assert gate["gate_is_metricgate"] is True and gate["passes"] is False


def test_the_session_start_gate_line_is_labelled_a_point_ratio(tool):
    r = _cell(hook="session-start", store_cell=None)
    r.update({"opens_flag_on": 0, "calibration_opens_on": 0, "opens_flag_off": 6, "calibration_opens_off": 0,
              "open_violations": [], "stdout_match": True, "readers_reached_off": [], "readers_reached_on": []})
    line = "\n".join(tool._session_start_header_lines(r))
    assert POINT_RATIO in line


def test_gate_without_emit_runs_is_a_usage_error(tool, tmp_path, capsys):
    """The report judges the files the run just wrote: --gate has nothing to read without --emit-runs.
    Mutation: accept --gate alone -> red."""
    with pytest.raises(SystemExit) as err:
        tool.main(["--gate", str(tmp_path / "g.json")])
    assert err.value.code == 2
    assert "--emit-runs" in capsys.readouterr().err


# --- the real adapter handle ---------------------------------------------------------------------------------

def test_load_adapter_resolves_the_real_module(tool):
    """The un-monkeypatched handle resolves through the repo-root `tools` package (the symlink beside the
    other tools). Mutation: drop the tools/metricgate_adapter.py link -> ImportError -> red."""
    mod = tool._load_adapter()
    assert hasattr(mod, "have_metricgate") and hasattr(mod, "append_run_record")


def test_emit_runs_help_says_one_fresh_directory_per_announced_cell(tool, capsys):
    """Round-2 item 3. Mutation: drop the sentence from the help text -> red."""
    with pytest.raises(SystemExit):
        tool.main(["--help"])
    assert "one fresh directory per announced cell" in " ".join(capsys.readouterr().out.split())
