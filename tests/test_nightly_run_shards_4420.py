"""Contract cells for .github/ci/nightly_run_shards.sh and nightly_shard_summary.py (#4420).

Contract (stated before the scripts existed):

C5a  every shard runs even when an earlier one failed; the script exits non-zero
     if any shard was non-zero.
C5b  each shard runs under ``timeout -k 60 <limit>``; rc 124 is reported as a
     NAMED timed-out shard ("shard k TIMED OUT after <limit>"), counts as red, and
     the loop continues with the next shard.
C5c  a shard with no files is skipped (a bare ``pytest`` would run the whole suite).
C1   shards are separate sequential pytest processes: no ``-n`` / ``--dist``.
C3   each shard writes its own ``COVERAGE_FILE=.coverage.<k>``; no ``--cov-append``,
     no per-shard ``--cov-report``.
C4   the sampler writes ``[sampler]`` lines, the script brackets each shard with
     ``[shard k start]`` / ``[shard k end]`` markers, and the pgrep pattern matches a
     shard whose first argument is under either ``tests/`` or ``src/forum/tests/``.
C4s  the summary prints one row per shard; a shard with no peak line is
     ``unmeasured`` (never 0), is named in a ``::error::`` annotation, and the
     script exits non-zero.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from _shard_env import hermetic_env

REPO = Path(__file__).resolve().parent.parent
CI_DIR = REPO / ".github" / "ci"
RUN = CI_DIR / "nightly_run_shards.sh"
SUMMARY = CI_DIR / "nightly_shard_summary.py"

COLLECT = (
    "tests/test_a.py: 10\n"
    "tests/test_b.py: 10\n"
    "tests/test_c.py: 10\n"
    "src/forum/tests/test_d.py: 10\n"
    "\n40/40 tests collected in 0.5s\n"
)

PYTEST_SHIM = '''#!{python}
import json, os, sys, time
cov = os.environ.get("COVERAGE_FILE", "")
k = cov.rsplit(".", 1)[-1]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cov": cov, "pythonpath": os.environ.get("PYTHONPATH", ""),
                        "rss_out": os.environ.get("NIGHTLY_RSS_OUT", ""),
                        "env_keys": sorted(os.environ)}}) + "\\n")
if k in os.environ.get("SHIM_SLEEP", "").split(","):
    time.sleep(float(os.environ.get("SHIM_SLEEP_SECS", "60")))
print("shim pytest shard " + k)
if k in os.environ.get("SHIM_KILL", "").split(","):
    sys.exit(137)
if k in os.environ.get("SHIM_FAIL", "").split(","):
    print("FAILED " + sys.argv[1] + "::test_y - boom")
    print("=== 1 failed, 2 passed in 0.1s ===")
    sys.exit(1)
print("=== 3 passed in 0.1s ===")
'''

TIMEOUT_SHIM = '''#!{python}
import os, signal, subprocess, sys
args = sys.argv[1:]
with open(os.environ["SHIM_TIMEOUT_LOG"], "a") as f:
    f.write(" ".join(args[:3]) + "\\n")
assert args[0] == "-k", args
dur = args[2]
secs = float(dur[:-1]) * (60 if dur.endswith("m") else 1)
p = subprocess.Popen(args[3:], start_new_session=True)
try:
    sys.exit(p.wait(timeout=secs))
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGTERM)
    p.wait()
    sys.exit(124)
'''


def _write_exe(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_exe(bindir / "pytest", PYTEST_SHIM.format(python=sys.executable))
    _write_exe(bindir / "timeout", TIMEOUT_SHIM.format(python=sys.executable))
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "collect.txt").write_text(COLLECT, encoding="utf-8")
    return tmp_path


def _env(sb, **extra):
    env = hermetic_env()
    env.pop("PYTEST_ADDOPTS", None)
    env.update(
        PATH=f"{sb / 'bin'}{os.pathsep}{env['PATH']}",
        SHIM_LOG=str(sb / "shim.jsonl"),
        SHIM_TIMEOUT_LOG=str(sb / "timeout.log"),
        NIGHTLY_SHARD_COUNT="4",
        NIGHTLY_COLLECT_FILE=str(sb / "collect.txt"),
        NIGHTLY_PYTHON=sys.executable,
        NIGHTLY_OUTPUT_LOG=str(sb / "nightly-output.log"),
        NIGHTLY_SHARD_DIR=str(sb),
        NIGHTLY_SAMPLER_LOG=str(sb / "sampler.log"),
        NIGHTLY_SAMPLE_SECS="1",
        NIGHTLY_SHARD_TIMEOUT_MIN="75",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run(sb, timeout=90, **extra):
    try:
        return subprocess.run(
            ["bash", str(RUN)], cwd=str(sb / "work"), env=_env(sb, **extra),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:  # a hang is a FAILURE of the cell, not a hang
        pytest.fail(f"run script did not finish within {timeout}s (hung): {e.stdout!r}")


def _invocations(sb):
    p = sb / "shim.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_all_green_runs_every_shard_once_and_exits_zero(sandbox):
    r = _run(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    inv = _invocations(sandbox)
    assert [i["cov"] for i in inv] == [f".coverage.{k}" for k in (1, 2, 3, 4)]
    files = [f for i in inv for f in i["argv"] if f.endswith(".py")]
    assert sorted(files) == sorted([
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "src/forum/tests/test_d.py"])


def test_c5a_a_failing_first_shard_does_not_stop_the_later_shards(sandbox):
    r = _run(sandbox, SHIM_FAIL="1")
    assert r.returncode != 0, "a red shard must make the step red"
    cov = [i["cov"] for i in _invocations(sandbox)]
    assert cov == [".coverage.1", ".coverage.2", ".coverage.3", ".coverage.4"], cov
    assert re.search(r"shard 1\b.*(failed|FAILED|rc=1)", r.stdout + r.stderr)


def test_c5a_a_failing_middle_shard_is_red_too(sandbox):
    r = _run(sandbox, SHIM_FAIL="3")
    assert r.returncode != 0
    assert len(_invocations(sandbox)) == 4


def test_c5a_output_log_holds_every_shard_and_shard_logs_exist(sandbox):
    _run(sandbox, SHIM_FAIL="2")
    out = (sandbox / "nightly-output.log").read_text()
    for k in (1, 2, 3, 4):
        assert f"shim pytest shard {k}" in out
        assert f"shim pytest shard {k}" in (sandbox / f"nightly-shard-{k}.log").read_text()


def test_c1_c3_each_shard_is_a_separate_plain_pytest_process_with_its_own_data_file(sandbox):
    _run(sandbox)
    inv = _invocations(sandbox)
    assert len(inv) == 4
    for k, i in enumerate(inv, start=1):
        argv = i["argv"]
        assert i["cov"] == f".coverage.{k}"
        for banned in ("-n", "--dist", "--cov-append"):
            assert banned not in argv
        assert not any(a.startswith("--cov-report") for a in argv)
        assert not any(a.startswith("-n") and a[2:].isdigit() for a in argv)
        assert "--cov=src" in argv and "--cov-branch" in argv
        assert "-v" in argv and "--tb=short" in argv
        assert "tmp_path_retention_policy=failed" in argv
        assert argv[argv.index("-m") + 1] == "not timing"
        assert argv[argv.index("-p") + 1] == "nightly_rss_plugin"
        assert str(CI_DIR) in i["pythonpath"].split(os.pathsep)
        assert i["rss_out"].endswith(f"nightly-rss-shard-{k}.md")


def test_c5b_default_limit_wraps_every_shard_in_timeout_dash_k_60_75m(sandbox):
    _run(sandbox)
    lines = (sandbox / "timeout.log").read_text().splitlines()
    assert lines == ["-k 60 75m"] * 4


def test_c5b_a_hung_shard_is_named_timed_out_red_and_later_shards_still_run(sandbox):
    r = _run(sandbox, timeout=60, SHIM_SLEEP="2", SHIM_SLEEP_SECS="45",
             NIGHTLY_SHARD_TIMEOUT_SECONDS="2")
    text = r.stdout + r.stderr
    assert r.returncode != 0
    assert re.search(r"shard 2 TIMED OUT after 2s", text), text
    assert "shard 1 TIMED OUT" not in text
    assert [i["cov"] for i in _invocations(sandbox)] == [
        ".coverage.1", ".coverage.2", ".coverage.3", ".coverage.4"]
    assert re.search(r"\[shard 2 end\].*rc=124", (sandbox / "sampler.log").read_text())


def test_c5b_a_sigkilled_shard_is_named_killed_not_timed_out_red_and_later_shards_run(sandbox):
    """rc 137 is SIGKILL: timeout's -k escalation OR the kernel OOM killer. It must be red and
    named, must not be labelled a timeout (it may be the memory failure this split avoids), and
    must not stop the later shards."""
    r = _run(sandbox, SHIM_KILL="2")
    text = r.stdout + r.stderr
    assert r.returncode != 0
    assert re.search(r"shard 2 was KILLED \(rc=137", text), text
    assert "TIMED OUT" not in text
    assert "shard-2(killed)" in text
    assert [i["cov"] for i in _invocations(sandbox)] == [
        ".coverage.1", ".coverage.2", ".coverage.3", ".coverage.4"]


def test_c5c_a_shard_with_no_files_is_skipped_not_run_bare(sandbox):
    r = _run(sandbox, NIGHTLY_SHARD_COUNT="6")
    assert r.returncode == 0, r.stdout + r.stderr
    inv = _invocations(sandbox)
    assert len(inv) == 4
    for i in inv:
        assert [a for a in i["argv"] if a.endswith(".py")], "bare pytest would run the whole suite"
    assert "shard 5" in r.stdout and "no files" in r.stdout


def test_missing_shard_count_fails_loudly(sandbox):
    env = _env(sandbox)
    env.pop("NIGHTLY_SHARD_COUNT")
    r = subprocess.run(["bash", str(RUN)], cwd=str(sandbox / "work"), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "NIGHTLY_SHARD_COUNT" in r.stdout + r.stderr
    assert _invocations(sandbox) == []


def test_script_is_bash_3_2_safe():
    text = RUN.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "mapfile" not in code and "readarray" not in code
    assert "declare -A" not in code
    assert not re.search(r"\$\{[A-Za-z_]+,,|\$\{[A-Za-z_]+\^\^", code)
    r = subprocess.run(["bash", "-n", str(RUN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- C4: the sampler -----------------------------------------------------------------


def test_c4_sampler_lines_and_shard_markers_land_in_the_log(sandbox):
    r = _run(sandbox, SHIM_SLEEP="1", SHIM_SLEEP_SECS="4")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (sandbox / "sampler.log").read_text()
    for k in (1, 2, 3, 4):
        assert re.search(rf"\[shard {k} start\].*files=1", log), log
        assert re.search(rf"\[shard {k} end\].*rc=0", log), log
    # A sample taken while shard 1 slept saw the pytest process.
    window = log.split("[shard 1 start]")[1].split("[shard 1 end]")[0]
    seen = [ln for ln in window.splitlines() if ln.startswith("[sampler]") and "pids=none" not in ln]
    assert seen, log
    assert re.search(r"pytest_rss=\d+M", seen[0])


def _pgrep_pattern():
    r = subprocess.run(["bash", str(RUN), "--print-pgrep-pattern"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    pat = r.stdout.strip()
    assert pat
    return pat


@pytest.mark.parametrize("cmdline", [
    "/opt/hostedtoolcache/Python/3.12.9/x64/bin/python /opt/hostedtoolcache/Python/3.12.9/x64/bin/pytest tests/test_a.py tests/test_b.py -v --tb=short",
    "/opt/hostedtoolcache/Python/3.12.9/x64/bin/python /opt/hostedtoolcache/Python/3.12.9/x64/bin/pytest src/forum/tests/test_d.py tests/test_z.py -v",
    "/usr/bin/python3 /usr/local/bin/pytest src/forum/tests/test_d.py -v",
])
def test_c4_pgrep_pattern_matches_a_shard_whatever_its_first_argument(cmdline):
    assert re.search(_pgrep_pattern(), cmdline), cmdline


@pytest.mark.parametrize("cmdline", [
    "timeout -k 60 75m pytest tests/test_a.py -v",
    "tail -n +1 -F /tmp/sampler.log",
    "bash .github/ci/nightly_run_shards.sh",
    "/usr/bin/python3 /usr/local/bin/pytest --collect-only -qq tests/ src/forum/tests/",
    "grep -r pytest tests/",
])
def test_c4_pgrep_pattern_ignores_wrappers_loggers_and_collection(cmdline):
    assert not re.search(_pgrep_pattern(), cmdline), cmdline


def test_c4_live_pgrep_finds_a_forum_first_shard_process(tmp_path):
    if shutil.which("pgrep") is None:
        pytest.skip("no pgrep")
    d = tmp_path / "bin"
    d.mkdir()
    _write_exe(d / "pytest", "#!/bin/sh\nsleep 20\n")
    p = subprocess.Popen([str(d / "pytest"), "src/forum/tests/test_d.py", "-v"])
    try:
        import time
        time.sleep(0.5)
        r = subprocess.run(["pgrep", "-f", _pgrep_pattern()], capture_output=True, text=True)
        assert str(p.pid) in r.stdout.split(), r.stdout
    finally:
        p.terminate()
        p.wait()


# --- C4: the summary -----------------------------------------------------------------


def _summary_dir(tmp_path, sampler_lines, logs=None, rss=None):
    d = tmp_path / "sum"
    d.mkdir()
    (d / "sampler.log").write_text("\n".join(sampler_lines) + "\n", encoding="utf-8")
    for k, text in (logs or {}).items():
        (d / f"nightly-shard-{k}.log").write_text(text, encoding="utf-8")
    for k, text in (rss or {}).items():
        (d / f"nightly-rss-shard-{k}.md").write_text(text, encoding="utf-8")
    return d


def _sample(rss, swap, pids="123"):
    return f"[sampler] 03:41:00Z used=3000M free=1000M avail=4000M swap_used={swap}M pytest_rss={rss}M pids={pids} load=1.0 1.0 1.0"


def _run_summary(d, n, summary_file=None):
    env = dict(os.environ)
    env.pop("GITHUB_STEP_SUMMARY", None)
    if summary_file:
        env["GITHUB_STEP_SUMMARY"] = str(summary_file)
    return subprocess.run(
        [sys.executable, str(SUMMARY), "--n", str(n), "--sampler-log", str(d / "sampler.log"), "--dir", str(d)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _rows(text):
    rows = {}
    for ln in text.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if ln.strip().startswith("|") and cells and cells[0].isdigit():
            rows[int(cells[0])] = cells
    return rows


GOOD_LOG = "...\nFAILED tests/test_x.py::test_y - boom\n=== 1 failed, 120 passed, 2 errors in 90.0s ===\n"


def test_c4s_one_row_per_shard_with_counts_and_peaks(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=7", _sample(1100, 0), _sample(1500, 300),
        "[shard 1 end] 03:50:00Z rc=1",
        "[shard 2 start] 03:50:01Z files=3", _sample(900, 5), "[shard 2 end] 04:00:00Z rc=0",
    ], logs={1: GOOD_LOG, 2: "=== 50 passed in 10.0s ===\n"})
    out = tmp_path / "gh_summary.md"
    r = _run_summary(d, 2, out)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = _rows(out.read_text())
    assert sorted(rows) == [1, 2]
    assert rows[1][1:7] == ["7", "120", "1", "2", "1500 MB", "300 MB"]
    assert rows[2][1:7] == ["3", "50", "0", "0", "900 MB", "5 MB"]
    assert "tests/test_x.py" in out.read_text()  # the failing file is named


def test_c4s_a_shard_without_a_sampler_line_is_unmeasured_named_and_loud(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=7", _sample(1100, 0), "[shard 1 end] 03:50:00Z rc=0",
        "[shard 2 start] 03:50:01Z files=3", "[shard 2 end] 03:50:30Z rc=0",
    ], logs={1: "=== 5 passed in 1.0s ===\n", 2: "=== 5 passed in 1.0s ===\n"})
    out = tmp_path / "gh_summary.md"
    r = _run_summary(d, 2, out)
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert re.search(r"::error::.*shard 2", combined), combined
    assert "shard 1" not in "".join(ln for ln in combined.splitlines() if ln.startswith("::error::"))
    row = _rows(out.read_text())[2]
    assert "unmeasured" in row[5] and "unmeasured" in row[6]
    assert "0 MB" not in row[5] and row[5] != "0"


def test_c4s_a_sample_with_no_pytest_pid_is_not_a_peak(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=1", _sample(0, 0, pids="none"), "[shard 1 end] 03:41:00Z rc=0",
    ], logs={1: "=== 1 passed in 1.0s ===\n"})
    r = _run_summary(d, 1, tmp_path / "s.md")
    assert r.returncode != 0
    assert "unmeasured" in (tmp_path / "s.md").read_text()


def test_c4s_a_timed_out_shard_is_marked_in_its_row(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=1", _sample(800, 0), "[shard 1 end] 04:55:00Z rc=124",
    ], logs={1: "partial output\n"})
    out = tmp_path / "s.md"
    r = _run_summary(d, 1, out)
    assert "TIMED OUT" in out.read_text()
    assert r.returncode == 0


def test_c7_summary_appends_per_shard_rss_reports_with_the_reading_caveat(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=1", _sample(800, 0), "[shard 1 end] 03:45:00Z rc=0",
    ], logs={1: "=== 1 passed in 1.0s ===\n"}, rss={1: "| `tests/test_big.py::test_x` | 77.0 |\n"})
    out = tmp_path / "s.md"
    assert _run_summary(d, 1, out).returncode == 0
    text = out.read_text()
    assert "tests/test_big.py::test_x" in text
    assert "where memory was first touched, not who holds it" in text


def test_c4s_summary_goes_to_stdout_when_no_summary_file(tmp_path):
    d = _summary_dir(tmp_path, [
        "[shard 1 start] 03:40:00Z files=1", _sample(800, 0), "[shard 1 end] 03:45:00Z rc=0",
    ], logs={1: "=== 1 passed in 1.0s ===\n"})
    r = _run_summary(d, 1)
    assert r.returncode == 0
    assert 1 in _rows(r.stdout)


# --- the runner's own settings must not leak into the tests it runs (part of #4420) ----------
# tests.yml sets these in the env of the shard pytest processes it runs; a test that runs the
# runner in a subprocess must not inherit them, and the runner must not hand its own settings
# on to the shards.

CI_RUNNER_ENV = {
    "NIGHTLY_SHARD_COUNT": "5",
    "NIGHTLY_SHARD_TIMEOUT_MIN": "60",
    "NIGHTLY_MARKER": "",
    "NIGHTLY_COVERAGE": "0",
}


def test_outer_ci_runner_settings_do_not_change_the_all_green_run(sandbox, monkeypatch):
    ref = sandbox / "one-test-reference.txt"
    ref.write_text("tests/test_a.py::test_one\n\n1 test collected in 0.1s\n", encoding="utf-8")
    for k, v in {**CI_RUNNER_ENV, "NIGHTLY_COLLECT_FILE": str(ref),
                 "NIGHTLY_IDSET_REFERENCE": str(ref)}.items():
        monkeypatch.setenv(k, v)
    r = _run(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    inv = _invocations(sandbox)
    assert [i["cov"] for i in inv] == [".coverage.1", ".coverage.2", ".coverage.3", ".coverage.4"]
    for i in inv:
        assert i["argv"][i["argv"].index("-m") + 1] == "not timing"


@pytest.mark.parametrize("knobs", [{}, {"NIGHTLY_MARKER": ""}],
                         ids=["default-knobs", "non-default-knobs"])
def test_a_shard_env_holds_no_runner_setting_but_the_rss_out_path(sandbox, knobs):
    r = _run(sandbox, NIGHTLY_FUTURE_KNOB="1", SHARD_IDSET_PYTHON="/nonexistent",
             NIGHTLY_SHARD_TIMEOUT_SECONDS="600", NIGHTLY_SAMPLER_DISABLE="1", **knobs)
    assert r.returncode == 0, r.stdout + r.stderr
    inv = _invocations(sandbox)
    assert len(inv) == 4
    for i in inv:
        leaked = [k for k in i["env_keys"]
                  if k.startswith(("NIGHTLY_", "SHARD_IDSET_")) and k != "NIGHTLY_RSS_OUT"]
        assert leaked == []
        assert "NIGHTLY_RSS_OUT" in i["env_keys"]
