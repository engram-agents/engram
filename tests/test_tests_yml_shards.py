"""Contract cells for tests.yml running the full suite on the shard runner (part of #4420).

Contract (stated before the code existed):

K1  every test id the old one-process step collected (``pytest tests/ src/forum/tests/``,
    no ``-m``) is collected by EXACTLY ONE shard; compared as id sets before any shard
    runs; a missing id, an extra id or an id in two shards fails and names the ids
    (list capped, counts given); a shard collection that exits non-zero fails and
    names the shard.
K2  red if any shard is red, every shard runs: tests.yml must not weaken the runner
    (no ``continue-on-error`` on the suite step).
K3  the "#3346" extra-files step stays and still names all its files and test_tripwire.py.
K4  with the new knobs unset the runner's shard argv is exactly today's (``-m "not timing"``,
    ``--cov=src --cov-branch``, ``COVERAGE_FILE=.coverage.<k>``).

Runner knobs: NIGHTLY_MARKER (unset = ``not timing``, empty = no -m), NIGHTLY_COVERAGE
(unset/1 = coverage, 0 = none), NIGHTLY_IDSET_REFERENCE (check before the first shard).
"""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from _shard_env import hermetic_env

REPO = Path(__file__).resolve().parent.parent
CI_DIR = REPO / ".github" / "ci"
RUN = CI_DIR / "nightly_run_shards.sh"
CHECK = CI_DIR / "shard_idset_check.py"
TESTS_YML = REPO / ".github" / "workflows" / "tests.yml"
TID_TOOL = REPO / "tools" / "test_id_set_check.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


idset = _load("tests_yml_shards_idset_check", CHECK)
tid = _load("tests_yml_shards_tid_tool", TID_TOOL)

FILES = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "src/forum/tests/test_d.py"]


def _ids(path, n=3):
    return [f"{path}::test_{i}" for i in range(n)]


ALL_IDS = [i for f in FILES for i in _ids(f)]
REFERENCE_TEXT = "\n".join(ALL_IDS) + f"\n\n{len(ALL_IDS)} tests collected in 0.5s\n"

# Stands in for `python` in `python -m pytest --collect-only ...`: prints node ids for every
# *.py argument, from a spec in $IDSET_SPEC, and logs its argv to $IDSET_LOG.
IDSET_PYTHON_SHIM = '''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["IDSET_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
with open(os.environ["IDSET_LOG"] + ".env", "a") as f:
    f.write(json.dumps(sorted(os.environ)) + "\\n")
spec = json.loads(os.environ.get("IDSET_SPEC", "{{}}"))
files = [a for a in args if a.endswith(".py")]
for fl in files:
    if fl in spec.get("fail_files", []):
        print("ERROR collecting " + fl)
        sys.exit(2)
    for i in range(3):
        nid = fl + "::test_" + str(i)
        if nid not in spec.get("drop", []):
            print(nid)
    for nid in spec.get("extra", {{}}).get(fl, []):
        print(nid)
print()
print("collected in 0.1s")
'''

PYTEST_SHIM = '''#!{python}
import json, os, sys
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cov": os.environ.get("COVERAGE_FILE", ""),
                        "env_keys": sorted(os.environ)}}) + "\\n")
print("shim pytest")
'''

TIMEOUT_SHIM = '''#!{python}
import os, subprocess, sys
args = sys.argv[1:]
assert args[0] == "-k", args
sys.exit(subprocess.call(args[3:]))
'''


def _write_exe(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def sb(tmp_path):
    (tmp_path / "bin").mkdir()
    _write_exe(tmp_path / "bin" / "pytest", PYTEST_SHIM.format(python=sys.executable))
    _write_exe(tmp_path / "bin" / "timeout", TIMEOUT_SHIM.format(python=sys.executable))
    _write_exe(tmp_path / "idset-python", IDSET_PYTHON_SHIM.format(python=sys.executable))
    (tmp_path / "work").mkdir()
    (tmp_path / "reference.txt").write_text(REFERENCE_TEXT, encoding="utf-8")
    return tmp_path


def _env(sb, **extra):
    env = hermetic_env()
    for k in ("PYTEST_ADDOPTS", "COVERAGE_FILE", "IDSET_SPEC"):
        env.pop(k, None)
    env.update(
        PATH=f"{sb / 'bin'}{os.pathsep}{env['PATH']}",
        SHIM_LOG=str(sb / "shim.jsonl"),
        IDSET_LOG=str(sb / "idset.jsonl"),
        SHARD_IDSET_PYTHON=str(sb / "idset-python"),
        NIGHTLY_SHARD_COUNT="4",
        NIGHTLY_COLLECT_FILE=str(sb / "reference.txt"),
        NIGHTLY_PYTHON=sys.executable,
        NIGHTLY_OUTPUT_LOG=str(sb / "out.log"),
        NIGHTLY_SHARD_DIR=str(sb),
        NIGHTLY_SAMPLER_LOG=str(sb / "sampler.log"),
        NIGHTLY_SAMPLER_DISABLE="1",
        NIGHTLY_SHARD_TIMEOUT_MIN="75",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run(sb, **extra):
    return subprocess.run(["bash", str(RUN)], cwd=str(sb / "work"), env=_env(sb, **extra),
                          capture_output=True, text=True, timeout=90)


def _jsonl(p):
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _marker_of(argv):
    """The -m expression of a `python -m pytest ...` argv, or None (the leading -m is the module flag)."""
    rest = argv[argv.index("pytest") + 1:]
    return rest[rest.index("-m") + 1] if "-m" in rest else None


def _shards(sb):
    return _jsonl(sb / "shim.jsonl")


def _check_main(sb, capsys, marker=None, spec=None, reference=None, monkeypatch=None):
    plan = sb / "plan.json"
    counts = [(f, 3) for f in FILES]
    plan.write_text(json.dumps(_plan(counts, 4)), encoding="utf-8")
    ref = reference if reference is not None else sb / "reference.txt"
    argv = ["--plan", str(plan), "--reference", str(ref), "--python", str(sb / "idset-python")]
    if marker:
        argv += ["--marker", marker]
    os.environ["IDSET_LOG"] = str(sb / "idset.jsonl")
    os.environ["IDSET_SPEC"] = json.dumps(spec or {})
    try:
        rc = idset.main(argv)
    finally:
        os.environ.pop("IDSET_SPEC", None)
        os.environ.pop("IDSET_LOG", None)
    return rc, capsys.readouterr().out


def _plan(counts, n):
    shards = [{"index": i + 1, "files": [], "tests": 0} for i in range(n)]
    for i, (f, c) in enumerate(counts):
        shards[i % n]["files"].append(f)
        shards[i % n]["tests"] += c
    return {"n": n, "files": len(counts), "tests": sum(c for _, c in counts), "shards": shards}


# --- K1: the id-set check ---------------------------------------------------------------


def test_k1_equal_and_disjoint_passes_with_a_counted_line(sb, capsys):
    rc, out = _check_main(sb, capsys)
    assert rc == 0, out
    assert f"idset: 4 shards, {len(ALL_IDS)} ids, union == reference, disjoint" in out
    assert "::error::" not in out


def test_k1_an_id_missing_from_every_shard_fails_and_names_it(sb, capsys):
    gone = "tests/test_b.py::test_1"
    rc, out = _check_main(sb, capsys, spec={"drop": [gone]})
    assert rc == 1
    assert gone in out and "NO shard" in out and "1 id(s)" in out


def test_k1_an_id_in_two_shards_fails_and_names_it(sb, capsys):
    dup = "tests/test_a.py::test_0"
    # shard 2 (tests/test_b.py) also collects an id the reference puts in tests/test_a.py
    rc, out = _check_main(sb, capsys, spec={"extra": {"tests/test_b.py": [dup]}})
    assert rc == 1
    assert dup in out and "two shards" in out


def test_k1_an_extra_id_not_in_the_reference_fails_and_names_it(sb, capsys):
    extra = "tests/test_c.py::test_brand_new"
    rc, out = _check_main(sb, capsys, spec={"extra": {"tests/test_c.py": [extra]}})
    assert rc == 1
    assert extra in out and "NOT in the reference" in out


def test_k1_a_shard_collection_with_a_nonzero_exit_fails_and_names_the_shard(sb, capsys):
    rc, out = _check_main(sb, capsys, spec={"fail_files": ["tests/test_c.py"]})
    assert rc == 1
    assert "shard 3 collection failed" in out and "exit 2" in out


def test_k1_a_long_list_is_capped_with_the_full_count(sb, capsys):
    ref = sb / "big-reference.txt"
    big = [f"tests/test_a.py::test_{i}" for i in range(3)] + [f"tests/test_zz.py::test_{i}" for i in range(30)]
    others = [i for i in ALL_IDS if not i.startswith("tests/test_a.py")]
    all_ids = big + others
    # a complete reference carries its summary line (a file without one is refused: round 2)
    ref.write_text("\n".join(all_ids) + f"\n\n{len(all_ids)} tests collected in 0.5s\n", encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 1
    assert "30 id(s)" in out and "and 10 more" in out
    assert out.count("tests/test_zz.py::test_") == 20


def test_k1_marker_is_passed_to_the_shard_collections_only_when_given(sb, capsys):
    _check_main(sb, capsys)
    argvs = _jsonl(sb / "idset.jsonl")
    assert argvs and all(_marker_of(a) is None for a in argvs)
    (sb / "idset.jsonl").unlink()
    _check_main(sb, capsys, marker="not timing")
    argvs = _jsonl(sb / "idset.jsonl")
    assert argvs and all(_marker_of(a) == "not timing" for a in argvs)


def test_k1_an_empty_reference_is_refused_not_passed(sb, capsys):
    ref = sb / "empty.txt"
    ref.write_text("\n3 tests collected\n", encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 2 and "no test ids" in out


# --- K1 (round 2): a PARTIAL reference is refused, exit 2, before any shard collection -------
#
# A file that fails to collect yields no ids, so it vanishes from the plan and the shards
# alike and "union == reference" would still hold. The shapes below are pytest 9's real
# `--collect-only -q` outputs (observed on a scratch dir: ok / import error / empty run).

_IDS_TEXT = "\n".join(ALL_IDS) + "\n"
_N = len(ALL_IDS)
_ERR_BODY = (
    "\n==================================== ERRORS ====================================\n"
    "_________________________ ERROR collecting tests/test_bad.py _________________________\n"
    "E   ModuleNotFoundError: No module named 'flask'\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_bad.py\n"
)
_BANNER = "!!!!!!!!!!!!!!!!!!!! Interrupted: 72 errors during collection !!!!!!!!!!!!!!!!!!!!\n"

PARTIAL_REFERENCES = {
    # summary line carries `N errors` (the measured case: `... collected, 72 errors in 16.12s`)
    "summary-errors": (_IDS_TEXT + f"\n{_N} tests collected, 72 errors in 16.12s\n", "collection errors"),
    "summary-one-error": (_IDS_TEXT + f"\n{_N} tests collected, 1 error in 0.04s\n", "collection errors"),
    # only the Interrupted banner (summary line cut off)
    "banner-only": (_IDS_TEXT + "\n" + _BANNER, "collection errors"),
    # only an `ERROR collecting` block
    "error-collecting-only": (_IDS_TEXT + _ERR_BODY, "collection errors"),
    # no summary line at all: a truncated file
    "truncated-no-summary": (_IDS_TEXT, "no pytest summary"),
    "truncated-mid-line": ("\n".join(ALL_IDS[:-3]) + "\ntests/test_c.p", "no pytest summary"),
    # the summary count differs from the ids parsed
    "count-mismatch-more": (_IDS_TEXT + f"\n{_N + 5} tests collected in 0.5s\n", "were parsed"),
    "count-mismatch-fewer": (_IDS_TEXT + f"\n{_N - 1} tests collected in 0.5s\n", "were parsed"),
    # the deselected form: the SELECTED count (K of K/M) is the one to compare
    "deselected-mismatch": (_IDS_TEXT + f"\n{_N + 2}/{_N + 9} tests collected (7 deselected) in 0.5s\n", "were parsed"),
    # `no tests collected` / `no tests ran` / `0 tests collected` with ids present (a corrupt count of 0)
    "no-tests-collected-with-ids": (_IDS_TEXT + "\nno tests collected in 0.00s\n", "were parsed"),
    "no-tests-ran-with-ids": (_IDS_TEXT + "\nno tests ran in 0.01s\n", "were parsed"),
    "zero-tests-collected-with-ids": (_IDS_TEXT + "\n0 tests collected in 0.00s\n", "were parsed"),
}


@pytest.mark.parametrize("name", sorted(PARTIAL_REFERENCES))
def test_k1_a_partial_reference_is_refused_with_exit_2_and_no_shard_collection(sb, capsys, name):
    text, needle = PARTIAL_REFERENCES[name]
    ref = sb / "partial.txt"
    ref.write_text(text, encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 2, out
    assert "::error::idset: " in out and needle in out, out
    assert "union == reference" not in out
    assert not (sb / "idset.jsonl").exists(), "a refused reference must not reach any shard collection"


def test_k1_the_collection_errors_message_names_the_error_count(sb, capsys):
    ref = sb / "partial.txt"
    ref.write_text(PARTIAL_REFERENCES["summary-errors"][0], encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 2 and "72 error(s)" in out


def test_k1_the_count_mismatch_message_names_both_numbers(sb, capsys):
    ref = sb / "partial.txt"
    ref.write_text(_IDS_TEXT + f"\n{_N + 5} tests collected in 0.5s\n", encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 2 and f"says {_N + 5} test(s)" in out and f"{_N} id(s) were parsed" in out


@pytest.mark.parametrize("summary", [
    "{n} tests collected in 16.12s",
    "{n} tests collected in 16.12s (0:00:16)",
    "{n} tests collected, 1 warning in 0.5s",
    "{n}/{m} tests collected ({d} deselected) in 0.5s",
])
def test_k1_clean_reference_shapes_still_pass(sb, capsys, summary):
    ref = sb / "clean.txt"
    ref.write_text(_IDS_TEXT + "\n" + summary.format(n=_N, m=_N + 4, d=4) + "\n", encoding="utf-8")
    rc, out = _check_main(sb, capsys, reference=ref)
    assert rc == 0, out


def test_k1_summary_parser_reads_the_observed_pytest_9_shapes():
    ps = idset.parse_summary
    assert ps("a.py::t\n\n33375 tests collected, 72 errors in 16.12s\n")[:2] == (33375, 72)
    assert ps("1 test collected in 0.00s\n")[:2] == (1, 0)
    assert ps("no tests collected, 1 error in 0.04s\n")[:2] == (0, 1)
    assert ps("no tests collected in 0.00s\n")[:2] == (0, 0)
    assert ps("1/2 tests collected (1 deselected) in 0.00s\n")[:2] == (1, 0)
    assert ps("no tests collected (2 deselected) in 0.00s\n")[:2] == (0, 0)
    assert ps("a.py::t\n") is None


# --- runner knobs -----------------------------------------------------------------------

TODAY_TAIL = ["-v", "--tb=short", "-o", "tmp_path_retention_policy=failed", "-m", "not timing",
              "--cov=src", "--cov-branch", "-p", "nightly_rss_plugin"]


def test_k4_unset_knobs_give_exactly_todays_argv_and_coverage_file(sb):
    r = _run(sb)
    assert r.returncode == 0, r.stdout + r.stderr
    shards = _shards(sb)
    assert len(shards) == 4
    for k, s in enumerate(shards, start=1):
        assert s["argv"][-len(TODAY_TAIL):] == TODAY_TAIL
        assert s["cov"] == f".coverage.{k}"
    assert not (sb / "idset.jsonl").exists(), "no reference set: the check must not run"


def test_marker_empty_means_no_dash_m_in_any_shard(sb):
    r = _run(sb, NIGHTLY_MARKER="")
    assert r.returncode == 0, r.stdout + r.stderr
    shards = _shards(sb)
    assert len(shards) == 4
    assert all("-m" not in s["argv"] and "not timing" not in s["argv"] for s in shards)


def test_marker_set_is_passed_through_verbatim(sb):
    _run(sb, NIGHTLY_MARKER="slow or timing")
    assert all(s["argv"][s["argv"].index("-m") + 1] == "slow or timing" for s in _shards(sb))


def test_coverage_zero_means_no_cov_flags_and_no_coverage_file(sb):
    r = _run(sb, NIGHTLY_COVERAGE="0", COVERAGE_FILE="inherited.cov")
    assert r.returncode == 0, r.stdout + r.stderr
    shards = _shards(sb)
    assert len(shards) == 4
    for s in shards:
        assert not [a for a in s["argv"] if a.startswith("--cov")]
        assert s["cov"] == ""


def test_coverage_one_is_todays_coverage(sb):
    _run(sb, NIGHTLY_COVERAGE="1")
    assert [s["cov"] for s in _shards(sb)] == [f".coverage.{k}" for k in range(1, 5)]


def test_coverage_other_value_is_refused(sb):
    r = _run(sb, NIGHTLY_COVERAGE="yes")
    assert r.returncode == 2 and "NIGHTLY_COVERAGE" in r.stdout
    assert _shards(sb) == []


def test_idset_reference_failing_check_exits_1_with_zero_shard_runs(sb):
    r = _run(sb, NIGHTLY_IDSET_REFERENCE=sb / "reference.txt", NIGHTLY_MARKER="",
             IDSET_SPEC=json.dumps({"drop": ["tests/test_b.py::test_2"]}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tests/test_b.py::test_2" in r.stdout and "::error::" in r.stdout
    assert _shards(sb) == []
    assert "all 4 shards green" not in r.stdout


def test_idset_reference_passing_check_runs_every_shard(sb):
    r = _run(sb, NIGHTLY_IDSET_REFERENCE=sb / "reference.txt", NIGHTLY_MARKER="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "union == reference, disjoint" in r.stdout
    assert len(_shards(sb)) == 4
    # the check ran with the runner's marker (none here), before the shards
    argvs = _jsonl(sb / "idset.jsonl")
    assert argvs and all(_marker_of(a) is None for a in argvs)


def test_idset_check_gets_the_runners_marker_when_it_has_one(sb):
    _run(sb, NIGHTLY_IDSET_REFERENCE=sb / "reference.txt")
    argvs = _jsonl(sb / "idset.jsonl")
    assert argvs and all(_marker_of(a) == "not timing" for a in argvs)


def test_runner_text_keeps_the_unset_vs_empty_distinction():
    code = "\n".join(ln for ln in RUN.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("#"))
    assert "${NIGHTLY_MARKER-not timing}" in code


# --- tests.yml structure ----------------------------------------------------------------


@pytest.fixture(scope="module")
def yml_text():
    return TESTS_YML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def job(yml_text):
    return yaml.safe_load(yml_text)["jobs"]["pytest"]


def _step(job, needle):
    hits = [s for s in job["steps"] if needle in s.get("name", "")]
    assert len(hits) == 1, f"{needle!r}: {len(hits)} steps"
    return hits[0]


def _suite(job):
    hits = [s for s in job["steps"] if "nightly_run_shards.sh" in str(s.get("run", ""))]
    assert len(hits) == 1
    return hits[0]


def test_suite_step_runs_the_shard_runner_with_the_contract_env(job):
    s = _suite(job)
    assert s["run"].strip() == "bash .github/ci/nightly_run_shards.sh"
    env = s["env"]
    assert env["NIGHTLY_SHARD_COUNT"] == "5"
    assert env["NIGHTLY_SHARD_TIMEOUT_MIN"] == "60"
    assert env["NIGHTLY_MARKER"] == ""
    assert env["NIGHTLY_COVERAGE"] == "0"
    ref = "${{ runner.temp }}/reference-collect.txt"
    assert env["NIGHTLY_COLLECT_FILE"] == ref
    assert env["NIGHTLY_IDSET_REFERENCE"] == ref


def test_k2_suite_step_cannot_go_green_over_a_red_shard(job):
    s = _suite(job)
    assert "continue-on-error" not in s
    assert "|| true" not in s["run"]
    assert "if" not in s


def test_suite_step_timeout_is_shards_times_limit_plus_15_and_the_job_sits_above(job):
    s = _suite(job)
    want = int(s["env"]["NIGHTLY_SHARD_COUNT"]) * int(s["env"]["NIGHTLY_SHARD_TIMEOUT_MIN"]) + 15
    assert s["timeout-minutes"] == want == 315
    assert job["timeout-minutes"] == want + 15 <= 360


def test_job_env_and_install_steps_are_kept(job):
    assert job["env"]["ENGRAM_NO_EMBEDDINGS"] == "1"
    install = _step(job, "Install dependencies")["run"]
    assert "tools/check_no_cuda_wheels.py" in install and "src/forum/requirements.txt" in install


def test_reference_collect_line_is_the_old_roots_without_a_marker(yml_text):
    lines = [ln for ln in yml_text.splitlines() if re.match(r"^\s*run:\s*pytest\s", ln)]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "--collect-only" in line and " -q " in line and "-p no:cacheprovider" in line
    assert not re.search(r"\s-m\s", line)
    assert '> "$RUNNER_TEMP/reference-collect.txt"' in line
    assert tuple(tid._parse_workflow_scan_roots(yml_text)) == tuple(tid.TEST_SCAN_ROOTS)


def test_the_old_one_process_step_is_gone(yml_text):
    assert not re.search(r"run:\s*pytest tests/ src/forum/tests/ -v", yml_text)


def test_steps_are_ordered_reference_then_suite_then_extra_files(job):
    names = [s.get("name", "") for s in job["steps"]]
    i_ref = next(i for i, n in enumerate(names) if "reference" in n)
    i_suite = next(i for i, s in enumerate(job["steps"]) if "nightly_run_shards.sh" in str(s.get("run", "")))
    i_extra = next(i for i, n in enumerate(names) if "#3346" in n)
    assert i_ref < i_suite < i_extra


def test_k3_the_3346_step_still_names_all_its_files_and_the_tripwire(job):
    s = _step(job, "#3346")
    assert s["env"] == {"PYTHONPATH": "src"}
    run = s["run"]
    for name in ("test_backup_claude_projects", "test_backup_session_logs", "test_forum_backup",
                 "test_forum_backup_alarm", "test_forum_backup_stamp", "test_forum_cli",
                 "test_forum_status_backup", "test_pre_dispatch_check"):
        assert f"tools/{name}.py" in run, name
    assert "pytest src/engram/tools/test_tripwire.py -v --tb=short || rc=1" in run
    assert "exit $rc" in run


# --- the runner's own settings must not leak into the tests it runs (part of #4420) ----------


def _leaked(keys, allow=()):
    return [k for k in keys if k.startswith(("NIGHTLY_", "SHARD_IDSET_")) and k not in allow]


def test_outer_ci_runner_settings_do_not_change_the_all_green_run(sb, monkeypatch):
    ref = sb / "one-test-reference.txt"
    ref.write_text("tests/test_a.py::test_one\n\n1 test collected in 0.1s\n", encoding="utf-8")
    for k, v in {"NIGHTLY_SHARD_COUNT": "5", "NIGHTLY_SHARD_TIMEOUT_MIN": "60", "NIGHTLY_MARKER": "",
                 "NIGHTLY_COVERAGE": "0", "NIGHTLY_COLLECT_FILE": str(ref),
                 "NIGHTLY_IDSET_REFERENCE": str(ref)}.items():
        monkeypatch.setenv(k, v)
    r = _run(sb)
    assert r.returncode == 0, r.stdout + r.stderr
    shards = _shards(sb)
    assert len(shards) == 4
    for k, s in enumerate(shards, start=1):
        assert s["argv"][-len(TODAY_TAIL):] == TODAY_TAIL
        assert s["cov"] == f".coverage.{k}"
    assert not (sb / "idset.jsonl").exists(), "the outer reference must not switch the check on"


def test_the_runner_hands_no_setting_of_its_own_to_the_shards_or_the_idset_check(sb):
    r = _run(sb, NIGHTLY_IDSET_REFERENCE=sb / "reference.txt", NIGHTLY_MARKER="", NIGHTLY_COVERAGE="0",
             NIGHTLY_FUTURE_KNOB="1", SHARD_IDSET_X="1")
    assert r.returncode == 0, r.stdout + r.stderr
    shards = _shards(sb)
    assert len(shards) == 4
    for s in shards:
        assert _leaked(s["env_keys"], allow=("NIGHTLY_RSS_OUT",)) == []
        assert "NIGHTLY_RSS_OUT" in s["env_keys"]
    env_log = sb / "idset.jsonl.env"
    check_envs = [json.loads(ln) for ln in env_log.read_text().splitlines() if ln.strip()]
    assert len(check_envs) == 4, "one collection per shard"
    for keys in check_envs:
        assert _leaked(keys) == []


def test_the_idset_check_collects_each_shard_without_any_runner_setting(sb, capsys, monkeypatch):
    for k in ("NIGHTLY_SHARD_COUNT", "NIGHTLY_IDSET_REFERENCE", "NIGHTLY_FUTURE_KNOB",
              "SHARD_IDSET_PYTHON", "SHARD_IDSET_X"):
        monkeypatch.setenv(k, "1")
    rc, out = _check_main(sb, capsys)
    assert rc == 0, out
    check_envs = [json.loads(ln) for ln in (sb / "idset.jsonl.env").read_text().splitlines() if ln.strip()]
    assert len(check_envs) == 4
    for keys in check_envs:
        assert _leaked(keys) == []
        assert "IDSET_LOG" in keys, "the scrub removes the runner's settings only"
