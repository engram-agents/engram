"""Contract cells C1-C8 for tools/seat-run.sh, the Mac-safe test runner for
colleague reads [engram-alpha#3974].

Contract-first: each cell below was written and watched to fail before the
script existed. Every cell runs the tool under /bin/bash (3.2 on a Mac) in a
scratch git repo built in tmp_path, with a fake HOME, a private TMPDIR and
SEAT_RUN_ROOT pointed at a scratch dir: no network, no `gh`, nothing under a
live ENGRAM_HOME.

Cells (the spec's C1-C8):
  C1 never touches the working tree; scratch worktree removed on exit/failure/INT
  C2 exit 2 before any pytest run: missing / <3.10 / no-pytest interpreter
  C3 header block first; one `arg[i]:` line per pytest argv element
  C4 flag read-back (`ini k=v (effective)`); a fused `-o k=v` element -> exit 6
  C5 zero-test file -> exit 5 naming it
  C6 per-file FILE lines, skip reasons, SUMMARY; skipped never folded into passed
  C7 exit codes 0/1/2/5/6 only; a crash is 1 with the raw tail
  C8 no network / gh / launchctl / writes outside SEAT_RUN_ROOT + worktree
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "engram" / "tools" / "seat-run.sh"
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(
    not Path(BASH).exists() or not (REPO / "src").is_dir(),
    reason="needs /bin/bash and a source checkout",
)

OK_TEST = "def test_one():\n    assert True\n\n\ndef test_two():\n    assert 1 + 1 == 2\n"
OPT = "tmp_path_retention_policy"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n")
    for name, body in files.items():
        (repo / name).write_text(body)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


class Env:
    """Isolated environment for one tool run."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.root = tmp_path / "root"
        self.tmpdir = tmp_path / "tmpdir"
        for d in (self.home, self.root, self.tmpdir):
            d.mkdir()

    def env(self, python=None, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VIRTUAL_ENV"))}
        e.update(
            HOME=str(self.home), TMPDIR=str(self.tmpdir), SEAT_RUN_ROOT=str(self.root),
            ENGRAM_PY=str(python or sys.executable),
            # Floor off by default [#4507]: cells that do not test the floor must not go
            # red on a host with little free space. The C9 cells set or remove it (None).
            SEAT_RUN_DISK_FLOOR="0",
        )
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def scratch(self):
        return sorted(self.root.glob("seat-run.*"))


def run_tool(repo, env, args, **extra):
    return subprocess.run(
        [BASH, str(SCRIPT), *args], cwd=repo, env=env.env(**extra),
        capture_output=True, text=True, timeout=240,
    )


def out(r):
    return r.stdout + r.stderr


@pytest.fixture
def world(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_ok.py": OK_TEST})
    return repo, Env(tmp_path), tmp_path


def stub_python(tmp_path, body, name="stubpy"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------- C1 --------
def test_c1_working_tree_untouched_and_scratch_removed(world):
    repo, env, _ = world
    (repo / "untracked.txt").write_text("x")
    before = _git(repo, "status", "--porcelain")
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"])
    assert r.returncode == 0, out(r)
    assert _git(repo, "status", "--porcelain") == before
    assert env.scratch() == [], "scratch dir must be gone after exit"
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


def test_c1_scratch_removed_on_failure_too(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_bad.py": "def test_bad():\n    assert False\n"})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_bad.py"])
    assert r.returncode == 1, out(r)
    assert env.scratch() == []
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


def test_c1_keep_retains_scratch(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--keep", "--", "tests/test_ok.py"])
    assert r.returncode == 0, out(r)
    kept = env.scratch()
    assert len(kept) == 1
    subprocess.run(["git", "worktree", "remove", "--force", str(kept[0] / "wt")],
                   cwd=repo, capture_output=True)


def test_c1_scratch_removed_on_sigint(tmp_path):
    marker = tmp_path / "started"
    body = (
        "import os, time\n"
        "def test_sleepy():\n"
        f"    open({str(marker)!r}, 'w').write('x')\n"
        "    time.sleep(60)\n"
    )
    repo = make_repo(tmp_path, {"tests/test_sleepy.py": body})
    env = Env(tmp_path)
    proc = subprocess.Popen(
        [BASH, str(SCRIPT), "--ref", "HEAD", "--", "tests/test_sleepy.py"],
        cwd=repo, env=env.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        deadline = time.time() + 90
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert marker.exists(), "sleepy test never started"
        os.killpg(proc.pid, signal.SIGINT)  # what a terminal ^C does
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert env.scratch() == []


# ---------------------------------------------------------------- C2 --------
def test_c2_old_python_refused_before_any_run(world):
    repo, env, tmp_path = world
    py = stub_python(tmp_path, "echo 3.9.6\n")
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(py))
    assert r.returncode == 2, out(r)
    assert "3.9" in out(r)
    assert "FILE " not in out(r)
    assert env.scratch() == [], "refusal happens before any worktree is made"


def test_c2_missing_python_refused(world):
    repo, env, tmp_path = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"],
                 ENGRAM_PY=str(tmp_path / "nope" / "python"))
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r)


def test_c2_no_pytest_refused(world):
    repo, env, tmp_path = world
    py = stub_python(tmp_path, 'case "$*" in *pytest*) exit 1;; esac\necho 3.12.0\n')
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(py))
    assert r.returncode == 2, out(r)
    assert "pytest" in out(r).lower()
    assert "FILE " not in out(r)


# ---------------------------------------------------------------- C3 --------
def test_c3_header_block_first_and_args_split(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o", f"{OPT}=failed"])
    assert r.returncode == 0, out(r)
    lines = r.stdout.splitlines()
    keys = [ln.split(":", 1)[0] for ln in lines[:6]]
    assert keys == ["host", "bash", "python", "pytest", "ref", "head"], lines[:8]
    kv = dict(ln.split(": ", 1) for ln in lines[:6])
    assert kv["host"] == subprocess.run(["uname", "-sm"], capture_output=True, text=True).stdout.strip()
    assert kv["bash"]
    assert kv["python"].startswith(sys.executable)
    assert "." in kv["python"].split()[-1]
    assert re.search(r"\d+\.\d+", kv["pytest"])
    assert kv["ref"] == "HEAD"
    assert kv["head"] == _git(repo, "rev-parse", "HEAD")
    assert "arg[0]: tests/test_ok.py" in lines
    assert "arg[1]: -o" in lines
    assert f"arg[2]: {OPT}=failed" in lines
    assert not any(ln.startswith("arg[") and "-o " in ln for ln in lines)


# ---------------------------------------------------------------- C4 --------
def test_c4_flag_lands_positive(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o", f"{OPT}=failed"])
    assert r.returncode == 0, out(r)
    assert f"ini {OPT}=failed (effective)" in r.stdout


def test_c4_fused_o_pair_exits_6_negative(world):
    """Must be able to FAIL: one fused argv element, as zsh hands it over."""
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", f"-o {OPT}=failed"])
    assert r.returncode == 6, out(r)
    assert "FILE " not in out(r), "no tests may run once the flag did not land"
    assert OPT in out(r)


def test_c4_unknown_ini_key_exits_6(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o", "no_such_seat_key=1"])
    assert r.returncode == 6, out(r)
    assert "FILE " not in out(r)


# ---------------------------------------------------------------- C5 --------
def test_c5_zero_test_file_exits_5(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_ok.py": OK_TEST, "tests/test_empty.py": "X = 1\n"})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "tests/test_empty.py"])
    assert r.returncode == 5, out(r)
    assert "tests/test_empty.py" in out(r)
    assert "FILE " not in out(r), "the guard runs before any real run"
    assert env.scratch() == []


# ---------------------------------------------------------------- C6 --------
MIX = '''import pytest

@pytest.fixture
def boom():
    raise RuntimeError("fixture boom")

def test_p1(): assert True
def test_p2(): assert True
def test_f1(): assert False
def test_e1(boom): pass

@pytest.mark.skip(reason="because-reason-A")
def test_s1(): pass

def test_s2():
    pytest.skip("because-reason-B")
'''


def test_c6_per_file_lines_skips_and_summary(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_mix.py": MIX, "tests/test_ok.py": OK_TEST})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_mix.py", "tests/test_ok.py"])
    assert r.returncode == 1, out(r)
    lines = r.stdout.splitlines()
    assert "FILE tests/test_mix.py passed=2 failed=1 skipped=2 errors=1" in lines
    assert "FILE tests/test_ok.py passed=2 failed=0 skipped=0 errors=0" in lines
    assert "SUMMARY files=2 passed=4 failed=1 skipped=2 errors=1" in lines
    assert lines[-1].startswith("SUMMARY ")
    i = lines.index("FILE tests/test_mix.py passed=2 failed=1 skipped=2 errors=1")
    block = "\n".join(lines[i + 1 : i + 8])
    assert "because-reason-A" in block and "because-reason-B" in block


def test_c6_all_skipped_is_not_a_pass(tmp_path):
    body = 'import pytest\n\n@pytest.mark.skip(reason="r")\ndef test_s(): pass\n'
    repo = make_repo(tmp_path, {"tests/test_skip.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_skip.py"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_skip.py passed=0 failed=0 skipped=1 errors=0" in r.stdout
    assert "SUMMARY files=1 passed=0 failed=0 skipped=1 errors=0" in r.stdout


# ---------------------------------------------------------------- C7 --------
def test_c7_green_is_0_and_usage_is_2(world):
    repo, env, _ = world
    assert run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"]).returncode == 0
    r = run_tool(repo, env, [])  # no --pr / --ref
    assert r.returncode == 2, out(r)
    r = run_tool(repo, env, ["--ref", "no-such-ref", "--", "tests/test_ok.py"])
    assert r.returncode == 2, out(r)


def test_c7_crash_is_1_with_raw_tail(tmp_path):
    body = "import os\n\ndef test_crash():\n    os._exit(7)\n"
    repo = make_repo(tmp_path, {"tests/test_crash.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_crash.py"])
    assert r.returncode == 1, out(r)
    assert "RAW-TAIL" in r.stdout
    assert "test_crash.py" in r.stdout.split("RAW-TAIL", 1)[1]
    assert env.scratch() == []


def test_c7_unreachable_pr_fetch_is_2(world):
    repo, env, tmp_path = world
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    r = run_tool(repo, env, ["--pr", "999", "--", "tests/test_ok.py"])
    assert r.returncode == 2, out(r)
    assert env.scratch() == []


# ---------------------------------------------------------------- C8 --------
def test_c8_nothing_written_outside_root_and_worktree(tmp_path):
    body = "def test_tmp(tmp_path):\n    (tmp_path / 'f').write_text('x')\n"
    repo = make_repo(tmp_path, {"tests/test_tmp.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_tmp.py"])
    assert r.returncode == 0, out(r)
    assert list(env.home.iterdir()) == [], "HOME must stay empty"
    assert list(env.tmpdir.iterdir()) == [], "the child TMPDIR is redirected into the scratch"
    assert env.scratch() == []


def test_c8_script_text_has_no_network_gh_launchctl_or_engram_writes():
    text = SCRIPT.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for banned in (r"\bgh\b", r"\blaunchctl\b", r"\bcurl\b", r"\bwget\b", r"\bssh\b"):
        assert not re.search(banned, code), f"{banned} must not appear in the tool"
    for ln in code.splitlines():
        if ".engram" in ln:
            assert ln.strip().startswith("ENGRAM_PY=") and "${ENGRAM_PY:-" in ln, ln
    # the only network verb is the fetch behind --pr
    fetches = [ln for ln in code.splitlines() if re.search(r"\bgit\b.*\bfetch\b", ln)]
    assert len(fetches) == 1


# ------------------------------------------------- bash 3.2 portability -----
FORBIDDEN = [
    r"\bmapfile\b", r"\breadarray\b", r"declare\s+-A", r"typeset\s+-A",
    r"\$\{[A-Za-z_][A-Za-z_0-9]*(,,|\^\^)", r"\blocal\s+-n\b", r"\bdeclare\s+-n\b",
    r"\btimeout\b", r"stat\s+-c", r"readlink\s+-f", r"sed\s+-i", r"\bgrep\s+-P\b",
    r"\bdate\s+-d\b", r"\bset\s+-[a-z]*u",
]


def test_script_exists_executable_and_syntax_clean():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_script_avoids_bash4_and_gnu_only_constructs():
    code = "\n".join(ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for pat in FORBIDDEN:
        assert not re.search(pat, code), f"forbidden construct {pat!r}"


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only: /bin/bash is 3.2 there")
def test_runs_under_bash_3_2_on_a_mac(world):
    v = subprocess.run([BASH, "-c", "echo $BASH_VERSION"], capture_output=True, text=True).stdout
    assert v.startswith("3.2"), v
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"])
    assert r.returncode == 0, out(r)
    assert f"bash: {v.strip()}" in r.stdout


# ======================================================================
# Round 2 (reviewer r1 on a10760ac): ONE classifier decides what every argv
# element of the pytest args is (option, option value, named file, other
# positional); the -o parser, the named-file split, the zero-test guard and
# the per-file run all read its answer.  Each cell below was written and
# watched to fail against a10760ac before the code changed, except the
# cells marked "mutation-killer", which were already green there and exist
# to be killed by a named mutant.
# ======================================================================
CONFTEST = '''def pytest_addoption(parser):
    parser.addoption("--seat-val", action="store", default=None)
    parser.addoption("--seat-multi", nargs="*", default=[])
    parser.addoption("--seat-flag", action="store_true", default=False)
'''
TEST_A = "def test_a1(): assert True\n\n\ndef test_a2(): assert True\n"
TEST_B = "def test_b1(): assert True\n\n\ndef test_b2(): assert True\n\n\ndef test_b3(): assert True\n"


@pytest.fixture
def ab(tmp_path):
    repo = make_repo(
        tmp_path,
        {"tests/test_a.py": TEST_A, "tests/test_b.py": TEST_B, "conftest.py": CONFTEST},
    )
    return repo, Env(tmp_path), tmp_path


def file_lines(r):
    return [ln for ln in r.stdout.splitlines() if ln.startswith("FILE ")]


def plain_passed(repo, *args):
    """What plain pytest reports as `passed` for the same args, in the same repo."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VIRTUAL_ENV"))}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", *args], cwd=repo, env=e,
                       capture_output=True, text=True)
    m = re.search(r"(\d+) passed", r.stdout)
    return int(m.group(1)) if m else 0


@pytest.mark.parametrize(
    "opt",
    [
        ["--ignore", "tests/test_a.py"],
        ["--ignore-glob", "tests/test_a.py"],
        ["--deselect", "tests/test_a.py::test_a1"],
        ["--junitxml", "tests/test_a.py"],
    ],
    ids=["ignore", "ignore-glob", "deselect", "junitxml"],
)
def test_r3_option_value_is_never_a_named_file(ab, opt):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", *opt, "tests/test_b.py"])
    assert r.returncode == 0, out(r)
    assert file_lines(r) == ["FILE tests/test_b.py passed=3 failed=0 skipped=0 errors=0"], out(r)
    assert "SUMMARY files=1 passed=3 failed=0 skipped=0 errors=0" in r.stdout
    assert plain_passed(repo, *opt, "tests/test_b.py") == 3  # plain pytest agrees for b


@pytest.mark.parametrize(
    "opt",
    [["--ignore=tests/test_a.py"], ["--deselect=tests/test_a.py::test_a1"]],
    ids=["ignore=", "deselect="],
)
def test_r3_attached_option_forms_are_self_contained(ab, opt):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", *opt, "tests/test_b.py"])
    assert r.returncode == 0, out(r)
    assert file_lines(r) == ["FILE tests/test_b.py passed=3 failed=0 skipped=0 errors=0"], out(r)


def test_r3_directory_after_a_file_is_refused_not_mislabelled(ab):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_a.py", "tests/"])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r)
    assert "tests/" in r.stderr
    assert env.scratch() == []


def test_r2_unknown_option_before_a_file_is_refused(ab):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "--seat-val", "tests/test_a.py", "tests/test_b.py"])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r)
    reason = [ln for ln in r.stderr.splitlines() if ln.startswith("seat-run:")]
    assert len(reason) == 1, r.stderr
    assert "--seat-val" in reason[0] and "tests/test_a.py" in reason[0]
    assert "ambiguous" in reason[0]
    assert env.scratch() == []


def test_r2_unknown_option_before_a_node_id_is_refused(ab):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "--seat-val", "x::y"])
    assert r.returncode == 2, out(r)
    assert "ambiguous" in r.stderr and "x::y" in r.stderr


def test_r2_unknown_option_with_equals_is_self_contained(ab):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "--seat-val=zzz", "tests/test_b.py"])
    assert r.returncode == 0, out(r)
    assert file_lines(r) == ["FILE tests/test_b.py passed=3 failed=0 skipped=0 errors=0"], out(r)


def test_r3_file_stays_before_a_trailing_unknown_option(ab):
    """The tool reorders argv; a trailing option of unknown arity must not
    end up directly in front of the named file and swallow it."""
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_a.py", "--seat-multi"])
    assert r.returncode == 0, out(r)
    assert file_lines(r) == ["FILE tests/test_a.py passed=2 failed=0 skipped=0 errors=0"], out(r)
    assert "SUMMARY files=1 passed=2 failed=0 skipped=0 errors=0" in r.stdout


def test_r3_double_dash_inside_the_pytest_args_still_works(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "--", "tests/test_ok.py"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_ok.py passed=2 failed=0 skipped=0 errors=0" in r.stdout


# ---- S1: xfailed / xpassed / deselected / -q -q are not a crash ------------
def test_s1_xfail_only_file_is_not_a_crash(tmp_path):
    body = "import pytest\n\n@pytest.mark.xfail\ndef test_xf(): assert False\n"
    repo = make_repo(tmp_path, {"tests/test_xf.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_xf.py"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_xf.py passed=0 failed=0 skipped=0 errors=0" in r.stdout
    assert "RAW-TAIL" not in r.stdout


def test_s1_xpass_and_deselected_only_are_not_a_crash(tmp_path):
    body = ("import pytest\n\n@pytest.mark.xfail\ndef test_xpassing(): pass\n\n"
            "def test_p(): assert True\n")
    repo = make_repo(tmp_path, {"tests/test_xp.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_xp.py", "-k", "xpassing"])
    assert r.returncode == 0, out(r)  # `1 deselected, 1 xpassed`: no passed/failed token at all
    assert "FILE tests/test_xp.py passed=0 failed=0 skipped=0 errors=0" in r.stdout
    assert "RAW-TAIL" not in r.stdout


def test_s1_double_quiet_is_not_a_crash(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-q", "-q"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_ok.py passed=2 failed=0 skipped=0 errors=0" in r.stdout
    assert "RAW-TAIL" not in r.stdout


# ---- S2: read-back compares what pytest normalized, not the raw string -----
@pytest.mark.parametrize(
    "kv, shown",
    [("xfail_strict=1", "xfail_strict=true"), ("pythonpath=.", None), ("markers=a", None)],
    ids=["bool", "path", "list"],
)
def test_s2_normalized_values_land(world, kv, shown):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o", kv])
    assert r.returncode == 0, out(r)
    key = kv.split("=")[0]
    assert re.search(rf"^ini {key}=.* \(effective\)$", r.stdout, re.M), out(r)
    if shown:
        assert f"ini {shown} (effective)" in r.stdout


def test_s2_fused_element_still_exits_6_with_requested_and_effective(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o xfail_strict=1"])
    assert r.returncode == 6, out(r)
    assert "requested=1" in r.stderr and "effective=" in r.stderr, r.stderr
    assert "FILE " not in out(r)


def test_s2_unregistered_key_reports_requested_and_effective(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o", "no_such_seat_key=1"])
    assert r.returncode == 6, out(r)
    assert "requested=1" in r.stderr and "effective=" in r.stderr


# ---- S3: a named file that ran nothing but skips ----------------------------
def test_s3_all_skipped_named_file_warns_but_stays_exit_0(tmp_path):
    body = 'import pytest\n\n@pytest.mark.skip(reason="r")\ndef test_s(): pass\n'
    repo = make_repo(tmp_path, {"tests/test_skip.py": body})
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_skip.py"])
    assert r.returncode == 0, out(r)
    assert "WARN named file ran 0 tests (all skipped): tests/test_skip.py" in r.stdout


def test_s3_no_warn_when_something_passed(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"])
    assert "WARN" not in r.stdout


# ---- S4 / S5: mutation-killers (green at a10760ac by design) ----------------
def test_s4_flags_reach_the_real_per_file_run(world):
    """Mutation-killer: dropping OTHER from the real run turns passed=1 into 2."""
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-k", "test_one"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_ok.py passed=1 failed=0 skipped=0 errors=0" in r.stdout


def test_s5_attached_short_o_is_read_back(world):
    """Mutation-killer for the `-oK=V` arm."""
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", f"-o{OPT}=failed"])
    assert r.returncode == 0, out(r)
    assert f"ini {OPT}=failed (effective)" in r.stdout


def test_s5_override_ini_equals_is_read_back(world):
    """Mutation-killer for the `--override-ini=K=V` arm."""
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", f"--override-ini={OPT}=failed"])
    assert r.returncode == 0, out(r)
    assert f"ini {OPT}=failed (effective)" in r.stdout


# ---- S6: TERM kills the child promptly -------------------------------------
def test_s6_term_kills_the_pytest_child_and_cleans_up(tmp_path):
    marker = tmp_path / "started"
    body = (
        "import os, time\n"
        "def test_sleepy():\n"
        f"    open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(120)\n"
    )
    repo = make_repo(tmp_path, {"tests/test_sleepy.py": body})
    env = Env(tmp_path)
    proc = subprocess.Popen(
        [BASH, str(SCRIPT), "--ref", "HEAD", "--", "tests/test_sleepy.py"],
        cwd=repo, env=env.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    child = None
    try:
        deadline = time.time() + 90
        while not (marker.exists() and marker.read_text()) and time.time() < deadline:
            time.sleep(0.2)
        assert marker.exists() and marker.read_text(), "sleepy test never started"
        child = int(marker.read_text())
        os.kill(proc.pid, signal.SIGTERM)  # the tool only, not its children
        t0 = time.time()
        proc.communicate(timeout=20)
        assert time.time() - t0 < 15, "the trap must fire while the child is still running"
    finally:
        if proc.poll() is None:
            proc.kill()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass
    assert proc.returncode == 1
    assert env.scratch() == []
    assert len(_git(repo, "worktree", "list").splitlines()) == 1
    assert child is not None
    for _ in range(50):
        try:
            os.kill(child, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the pytest child outlived the tool")


# ---- S8: an argv element cannot fake a header line -------------------------
def test_s8_newline_in_an_argv_element_cannot_fake_a_header_line(world):
    repo, env, _ = world
    nasty = "tests/test_ok.py::test_two\narg[9]: fake"
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "--deselect", nasty])
    lines = r.stdout.splitlines()
    assert "arg[9]: fake" not in lines, out(r)
    assert "arg[2]: tests/test_ok.py::test_two\\narg[9]: fake" in lines, lines[:12]


# ======================================================================
# Round 3 (reviewer r2 on ee915be5).  B1/B1b: every user-supplied PATH that
# reaches pytest or the worktree must live inside the ref or be flagged.  The
# cells use files that REALLY EXIST on disk outside the ref, so a tool that
# drops the check runs them green (the failure the check exists to close).
# ======================================================================
OUTSIDE_TEST = "def test_abs():\n    assert True\n"


@pytest.fixture
def outside(tmp_path):
    """A passing test file that is not in any ref: next to the scratch ROOT
    (so `../../x/...` from the scratch worktree reaches it) and absolute."""
    d = tmp_path / "root" / "x"
    d.mkdir(parents=True)
    f = d / "test_abs.py"
    f.write_text(OUTSIDE_TEST)
    return f


def _reason(r):
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("seat-run:")]
    assert len(lines) == 1, r.stderr
    return lines[0]


def test_b1_absolute_path_outside_the_ref_is_refused(world, outside):
    repo, env, _ = world
    assert outside.is_file()
    r = run_tool(repo, env, ["--ref", "HEAD", "--", str(outside)])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r) and "SUMMARY" not in out(r)
    assert "escapes the ref" in _reason(r) and str(outside) in _reason(r)
    assert env.scratch() == []


def test_b1_absolute_path_to_an_untracked_file_in_the_working_tree_is_refused(world):
    repo, env, _ = world
    untracked = repo / "tests" / "test_untracked.py"
    untracked.write_text(OUTSIDE_TEST)  # on disk, not committed: not in the ref
    r = run_tool(repo, env, ["--ref", "HEAD", "--", str(untracked)])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r)
    assert "escapes the ref" in _reason(r)


def test_b1_absolute_directory_and_node_id_are_refused_too(world, outside):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", str(repo / "tests")])
    assert r.returncode == 2 and "escapes the ref" in r.stderr, out(r)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", f"{outside}::test_abs"])
    assert r.returncode == 2 and "escapes the ref" in r.stderr, out(r)


@pytest.mark.parametrize(
    "tok",
    ["../../x/test_abs.py", "../../x/test_abs.py::test_abs", "tests/../../../x/test_abs.py", ".."],
    ids=["dotdot", "dotdot-node", "dotdot-middle", "bare-dotdot"],
)
def test_b1_dotdot_escape_is_refused(world, outside, tok):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", tok])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r) and "SUMMARY" not in out(r)
    assert "escapes the ref" in _reason(r) and tok in _reason(r)


def test_b1_relative_path_that_stays_inside_is_unaffected(world):
    repo, env, _ = world
    for tok in ("tests/test_ok.py", "./tests/test_ok.py", "tests/test_ok.py::test_one"):
        r = run_tool(repo, env, ["--ref", "HEAD", "--", tok])
        assert r.returncode == 0, out(r)
        assert "escapes" not in out(r)
        assert f"FILE {tok} passed=" in r.stdout


def test_b1_a_dotted_filename_is_not_a_dotdot_segment(tmp_path):
    repo = make_repo(tmp_path, {})
    (repo / "tests" / "a..b").mkdir()
    (repo / "tests" / "a..b" / "test_x.py").write_text("def test_x(): assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dotted dir")
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/a..b/test_x.py"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/a..b/test_x.py passed=1 " in r.stdout


@pytest.mark.parametrize(
    "opt, flag",
    [
        ("--rootdir", "sep"), ("--rootdir=", "eq"), ("-c", "sep"), ("-c", "attached"),
        ("--confcutdir", "sep"), ("--junitxml", "sep"), ("--basetemp", "dotdot"),
        ("-o", "pythonpath"),
    ],
    ids=["rootdir", "rootdir=", "c", "c-attached", "confcutdir", "junitxml", "basetemp-dotdot", "pythonpath"],
)
def test_b1b_path_valued_option_outside_the_ref_warns_once_and_still_runs(world, opt, flag):
    repo, env, tmp_path = world
    d = tmp_path / "elsewhere"
    d.mkdir()
    (d / "pytest.ini").write_text("[pytest]\n")
    if flag == "dotdot":
        value = "../bt"
    elif opt == "--junitxml":
        value = str(d / "out.xml")
    elif opt == "-c":
        value = str(d / "pytest.ini")
    else:
        value = str(d)
    if flag == "eq":
        args, shown = [f"--rootdir={value}"], f"--rootdir={value}"
    elif flag == "attached":
        args, shown = [f"-c{value}"], f"-c={value}"
    elif flag == "pythonpath":
        args, shown = ["-o", f"pythonpath={value}"], f"pythonpath={value}"
    else:
        args, shown = [opt, value], f"{opt}={value}"
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", *args])
    assert r.returncode == 0, out(r)
    warn = f"WARN option value points outside the ref: {shown}"
    assert r.stdout.splitlines().count(warn) == 1, out(r)
    assert "FILE tests/test_ok.py passed=2 failed=0 skipped=0 errors=0" in r.stdout


def test_b1b_inside_values_do_not_warn(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py",
                             "--junitxml", "out.xml", "--rootdir", ".", "-o", "pythonpath=."])
    assert r.returncode == 0, out(r)
    assert "WARN" not in r.stdout


# ---- B2: a fused `-o k=v` element exits 6 even when the value would match ---
def test_b2_fused_o_with_a_value_equal_to_the_ini_exits_6(tmp_path):
    repo = make_repo(tmp_path, {"tests/test_ok.py": OK_TEST})
    (repo / "pytest.ini").write_text("[pytest]\nxfail_strict = true\n")
    _git(repo, "commit", "-q", "-am", "strict")
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-o xfail_strict=1"])
    assert r.returncode == 6, out(r)
    assert "FILE " not in out(r)
    assert "xfail_strict" in r.stderr


# ---- B3: a request lands only if it IS the effective value, not a substring --
@pytest.mark.parametrize(
    "key, first, last",
    [
        ("junit_suite_name", "a", "ab"),
        ("filterwarnings", "error", "error::DeprecationWarning"),
    ],
    ids=["string", "list-member"],
)
def test_b3_requested_value_that_is_only_a_substring_of_the_effective_exits_6(world, key, first, last):
    """Mutation-killer (green at ee915be5: landed() was already exact)."""
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py",
                             "-o", f"{key}={first}", "-o", f"{key}={last}"])
    assert r.returncode == 6, out(r)
    assert "FILE " not in out(r)
    assert f"{key}(requested={first} effective=" in r.stderr, r.stderr


# ---- B4: rc 0 with no parsable summary line fails closed --------------------
@pytest.mark.parametrize(
    "extra",
    [["-q", "-q", "-q"], ["-o", "addopts=-qqq"]],
    ids=["qqq", "addopts-q"],
)
def test_b4_named_file_with_no_parsable_summary_fails_closed(world, extra):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", *extra])
    assert r.returncode == 1, out(r)
    assert "RAW-TAIL tests/test_ok.py" in r.stdout and "no parsable summary" in r.stdout
    assert "FILE tests/test_ok.py passed=0 failed=0 skipped=0 errors=1" in r.stdout


def test_b4_a_parsable_quiet_run_is_still_green(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", "-q", "-q"])
    assert r.returncode == 0, out(r)
    assert "RAW-TAIL" not in r.stdout


# ---- B5: no table option may have the wrong arity for the installed pytest --
def _table_words(func):
    text = SCRIPT.read_text()
    body = re.search(rf"^{func}\(\) \{{.*?^\}}", text, re.S | re.M).group(0)
    case = re.search(r"case \"\$1\" in(.*?)\) return 0", body, re.S).group(1)
    return [w for w in re.split(r"[\s|\\]+", case) if w.startswith("-")]


def test_b5_no_table_option_has_the_wrong_arity_for_the_installed_pytest():
    from _pytest.config import _prepareconfig

    ap = _prepareconfig([], None)._parser.optparser
    arity = {}
    for act in ap._actions:
        for s in act.option_strings:
            arity[s] = act.nargs
    value, flag = _table_words("value_opt"), _table_words("flag_opt")
    assert value and flag and "-o" in value and "-q" in flag
    wrong = []
    for name in value:
        if name in arity and arity[name] == 0:
            wrong.append(f"{name} is in value_opt but pytest takes no value")
    for name in flag:
        if name in arity and arity[name] != 0:
            wrong.append(f"{name} is in flag_opt but pytest takes a value")
    assert not wrong, wrong


def test_b5_ambiguity_refusal_names_the_option_before_the_token(ab):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "--seat-val", "tests/test_a.py"])
    assert r.returncode == 2, out(r)
    reason = _reason(r)
    assert "'--seat-val'" in reason and "'tests/test_a.py'" in reason


# ---- B7: --ignore-glob with a real glob pattern ------------------------------
@pytest.mark.parametrize(
    "opt", [["--ignore-glob", "tests/test_a*.py"], ["--ignore-glob=tests/test_a*.py"]],
    ids=["sep", "eq"],
)
def test_b7_ignore_glob_pattern_is_not_a_named_file(ab, opt):
    repo, env, _ = ab
    r = run_tool(repo, env, ["--ref", "HEAD", "--", *opt, "tests/test_b.py"])
    assert r.returncode == 0, out(r)
    assert file_lines(r) == ["FILE tests/test_b.py passed=3 failed=0 skipped=0 errors=0"], out(r)


# ======================================================================
# Round 4 (F1-F5): pytest also receives arguments or paths through channels
# the tool never classifies -- `@argsfile`, `-o addopts=`, PYTEST_ADDOPTS.
# Each cell uses a passing test file that REALLY EXISTS outside the ref, so a
# tool that drops the check runs it green.
# ======================================================================
def _plain_pytest_passes(repo, *args, **env_extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VIRTUAL_ENV"))}
    e.update(env_extra)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", *args], cwd=repo, env=e,
                       capture_output=True, text=True)
    return r.returncode == 0 and "1 passed" in r.stdout


# ---- F1: an @argsfile positional cannot be classified -> refuse -------------
def test_f1_argsfile_positional_naming_an_outside_file_is_refused(world, outside, tmp_path):
    repo, env, _ = world
    argsfile = tmp_path / "args.txt"
    argsfile.write_text(f"{outside}\n")
    assert outside.is_file()
    assert _plain_pytest_passes(repo, f"@{argsfile}"), "precondition: pytest really reads the file"
    r = run_tool(repo, env, ["--ref", "HEAD", "--", f"@{argsfile}"])
    assert r.returncode == 2, out(r)
    assert "FILE " not in out(r) and "SUMMARY" not in out(r)
    reason = _reason(r)
    assert f"positional '@{argsfile}' is an argsfile" in reason, reason
    assert "name the files on the command line" in reason
    assert env.scratch() == []


def test_f1_argsfile_next_to_a_named_file_is_refused_too(world, outside, tmp_path):
    repo, env, _ = world
    argsfile = tmp_path / "args.txt"
    argsfile.write_text(f"{outside}\n")
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", f"@{argsfile}"])
    assert r.returncode == 2 and "is an argsfile" in r.stderr, out(r)
    assert "FILE " not in out(r)


def test_f1_an_at_sign_inside_a_path_is_not_an_argsfile(tmp_path):
    """Plausible-wrong check: refuse any token that merely CONTAINS `@`."""
    repo = make_repo(tmp_path, {})
    (repo / "tests" / "a@b").mkdir()
    (repo / "tests" / "a@b" / "test_x.py").write_text("def test_x(): assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "at dir")
    env = Env(tmp_path)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/a@b/test_x.py"])
    assert r.returncode == 0, out(r)
    assert "FILE tests/a@b/test_x.py passed=1 " in r.stdout
    assert "argsfile" not in out(r)


# ---- F2: -o addopts= and PYTEST_ADDOPTS carry paths too: WARN, never refuse --
def test_f2_o_addopts_naming_an_outside_file_warns_and_still_runs(world, outside):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py",
                             "-o", f"addopts={outside}"])
    assert r.returncode == 0, out(r)
    assert f"WARN addopts word points outside the ref: {outside}" in r.stdout.splitlines(), out(r)
    # the channel is real: the outside file ran as part of the same pytest run
    assert "FILE tests/test_ok.py passed=3 failed=0 skipped=0 errors=0" in r.stdout


def test_f2_addopts_warns_per_escaping_word_only(world, outside):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py",
                             "-o", f"addopts=-p no:cacheprovider {outside}"])
    assert r.returncode == 0, out(r)
    warns = [ln for ln in r.stdout.splitlines() if ln.startswith("WARN addopts word")]
    assert warns == [f"WARN addopts word points outside the ref: {outside}"], warns


def test_f2_pytest_addopts_env_prints_header_and_warns(world, outside):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], PYTEST_ADDOPTS=str(outside))
    assert r.returncode == 0, out(r)
    lines = r.stdout.splitlines()
    assert f"env PYTEST_ADDOPTS={outside}" in lines, out(r)
    assert f"WARN addopts word points outside the ref: {outside}" in lines, out(r)
    # the channel is real: the outside file ran as part of the same pytest run
    assert "FILE tests/test_ok.py passed=3 failed=0 skipped=0 errors=0" in r.stdout


def test_f2_pytest_addopts_set_but_harmless_prints_header_without_warn(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"],
                 PYTEST_ADDOPTS="-p no:cacheprovider")
    assert r.returncode == 0, out(r)
    assert "env PYTEST_ADDOPTS=-p no:cacheprovider" in r.stdout.splitlines()
    assert "WARN addopts" not in r.stdout


def test_f2_pytest_plugins_env_is_named_in_the_header(world, tmp_path):
    repo, env, _ = world
    plug = tmp_path / "plugdir"
    plug.mkdir()
    (plug / "seat_noop_plugin.py").write_text("")
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"],
                 PYTEST_PLUGINS="seat_noop_plugin", PYTHONPATH=str(plug))
    assert r.returncode == 0, out(r)
    assert "env PYTEST_PLUGINS=seat_noop_plugin" in r.stdout.splitlines(), out(r)


def test_f2_neither_set_prints_no_env_line_and_no_addopts_warn(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"])
    assert r.returncode == 0, out(r)
    assert not [ln for ln in r.stdout.splitlines() if ln.startswith("env ")], out(r)
    assert "WARN addopts" not in r.stdout


# ---- F3: --co / --collect-only is neither a pass nor a crash -----------------
@pytest.mark.parametrize(
    "extra", [["--co"], ["--collect-only", "-q"]], ids=["co", "collect-only-q"],
)
def test_f3_collect_only_named_file_is_neither_false_red_nor_false_pass(world, extra):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py", *extra])
    assert r.returncode == 0, out(r)
    assert "FILE tests/test_ok.py collect-only rc=0" in r.stdout.splitlines(), out(r)
    assert "RAW-TAIL" not in r.stdout
    assert not [ln for ln in file_lines(r) if "passed=" in ln], file_lines(r)


# ---- F4: an (all) run whose summary cannot be parsed says so -----------------
def test_f4_all_run_without_parsable_summary_says_unparsed(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "-q", "-q", "-q"])
    assert r.returncode == 0, out(r)
    summary = [ln for ln in r.stdout.splitlines() if ln.startswith("SUMMARY ")]
    assert len(summary) == 1 and "passed=unparsed" in summary[0], summary
    assert "WARN no pytest summary line for (all)" in r.stdout


def test_f4_all_run_with_a_summary_keeps_numeric_counts(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--"])
    assert r.returncode == 0, out(r)
    assert "SUMMARY files=1 passed=2 failed=0 skipped=0 errors=0" in r.stdout


# ---- F5: header documents the trust boundary (comment only) ------------------
def test_f5_header_documents_the_ref_trust_boundary():
    head = SCRIPT.read_text().split("ENGRAM_PY=${ENGRAM_PY", 1)[0]
    for needle in ("symlink", "-xc/abs", "--cov-config", "--opt=value"):
        assert needle in head, needle


# ------------------------------------------- ENGRAM_HOME guard (alpha #3990) ---
def test_g3_relative_engram_home_is_refused_with_no_side_effect(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_HOME="rel/home")
    assert r.returncode == 2, out(r)
    assert r.stderr == "ENGRAM_HOME must be an absolute path, got 'rel/home'\n", repr(r.stderr)
    assert r.stdout == "", r.stdout
    assert list(env.root.iterdir()) == [], "the refusal created something under SEAT_RUN_ROOT"
    assert env.scratch() == []


def test_g3_absolute_engram_home_reaches_the_tools_own_usage_refusal(world, tmp_path):
    repo, env, _ = world
    r = run_tool(repo, env, [], ENGRAM_HOME=str(tmp_path / "abs-home"))
    assert r.returncode == 2, out(r)
    assert r.stderr.startswith("usage: seat-run.sh"), repr(r.stderr)
    assert "must be an absolute path" not in out(r)


# ---- C9 disk floor [engram-alpha#4507] ---------------------------------------
# The tool refuses to start when free space on SEAT_RUN_ROOT is below
# max(5% of the volume, 20 GiB) (the same arithmetic as memrun), prints the
# free / total / floor figures on every run, and states the floor in --help.
# Written before the implementation; D1, D3 and D5 were watched red on dev.
GIB = 2**30
FLOOR_ENV = "SEAT_RUN_DISK_FLOOR"
DISK_LINE = re.compile(r"^disk: (.+) free ([0-9.]+) GiB of ([0-9.]+) GiB, floor ([0-9.]+) GiB$")


def _default_floor_gib(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    return f"{max(total // 20, 20 * GIB) / GIB:.1f}", f"{total / GIB:.1f}"


def test_d1_floor_above_free_space_refuses_before_any_write(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], **{FLOOR_ENV: "999T"})
    assert r.returncode == 2, out(r)
    o = out(r)
    assert "not started" in o and "floor" in o, o
    assert str(env.root) in o, o
    assert re.search(r"is [0-9.]+ GiB, below the floor 1022976\.0 GiB", o), o
    assert "(overridden by SEAT_RUN_DISK_FLOOR)" in o, o
    assert env.scratch() == []
    assert list(env.root.iterdir()) == [], "nothing may be written under the root before the check"
    assert "FILE " not in o and "SUMMARY" not in o


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_d1b_floor_is_checked_before_the_root_is_written_to(world):
    # The exit trap removes a scratch dir, so an empty root after a refusal cannot show
    # that nothing was created first. A root nobody can write to can: if the check ran
    # after mktemp, the refusal would be "cannot create a scratch dir", not the floor.
    repo, env, _ = world
    env.root.chmod(0o555)
    try:
        r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], **{FLOOR_ENV: "999T"})
    finally:
        env.root.chmod(0o755)
    assert r.returncode == 2, out(r)
    assert "not started" in out(r), out(r)
    assert "cannot create a scratch dir" not in out(r), out(r)


def test_d2_zero_floor_lets_a_normal_run_through(world):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], **{FLOOR_ENV: "0"})
    assert r.returncode == 0, out(r)
    assert "SUMMARY files=1 passed=2" in r.stdout


def test_d3_every_run_prints_one_disk_line_with_the_default_floor(world):
    # No override (None removes the Env default): the real default floor applies, so the
    # outcome depends on this host's free space. Both outcomes print the disk line first.
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], **{FLOOR_ENV: None})
    disk = [ln for ln in r.stdout.splitlines() if ln.startswith("disk:")]
    assert len(disk) == 1, out(r)
    m = DISK_LINE.match(disk[0])
    assert m, disk[0]
    floor, total = _default_floor_gib(env.root)
    assert m.group(1) == str(env.root)
    assert m.group(3) == total, (m.group(3), total)
    assert m.group(4) == floor, (m.group(4), floor)
    st = os.statvfs(env.root)
    if st.f_bavail * st.f_frsize >= max(st.f_blocks * st.f_frsize // 20, 20 * GIB) + GIB:
        assert r.returncode == 0, out(r)
    elif st.f_bavail * st.f_frsize < max(st.f_blocks * st.f_frsize // 20, 20 * GIB) - GIB:
        assert r.returncode == 2 and "not started" in out(r), out(r)
        assert "max(5% of" in out(r), out(r)


@pytest.mark.parametrize("bad, named", [("lots", "lots"), ("9" * 5000, "5000 digits")], ids=["word", "5000-digits"])
def test_d4_bad_override_is_refused_naming_variable_and_value(world, bad, named):
    repo, env, _ = world
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], **{FLOOR_ENV: bad})
    assert r.returncode == 2, out(r)
    assert FLOOR_ENV in out(r) and named in out(r) and "not a size" in out(r), out(r)
    assert env.scratch() == []
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_d5_help_states_the_floor_and_exits_zero_without_preflight(world, flag):
    _, env, tmp_path = world
    # not a git repo, and an interpreter that does not exist: help must touch neither
    r = run_tool(env.home, env, [flag], ENGRAM_PY=str(tmp_path / "nope" / "python"))
    assert r.returncode == 0, out(r)
    assert "usage: seat-run.sh" in r.stdout, out(r)
    assert "max(5% of the volume, 20 GiB)" in r.stdout, out(r)
    assert FLOOR_ENV in r.stdout, out(r)
    assert list(env.root.iterdir()) == []


def test_d6_unreadable_free_space_is_refused(world):
    repo, env, tmp_path = world
    py = stub_python(
        tmp_path,
        # statvfs first: the root path contains "pytest-of-", so a *pytest* arm listed
        # first would answer the statvfs call and the junk branch would never run (Sol).
        'case "$*" in *statvfs*) echo junk;; *pytest*) echo 9.0.0;; *) echo 3.12.0;; esac\n',
    )
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(py))
    assert r.returncode == 2, out(r)
    assert "cannot read free space" in out(r), out(r)
    assert "got 'junk'" in out(r), out(r)  # the junk branch ran, not the pytest probe
    assert env.scratch() == []
    assert "FILE " not in out(r)


# D7: the script's own arithmetic on a SIMULATED small volume. ENGRAM_PY is a wrapper
# around the real interpreter that replaces os.statvfs inside the script's statvfs
# call only (every other call is exec'd unchanged), so this pins what a large-disk host
# cannot: the 20 GiB minimum arm, that free is f_bavail (not f_bfree), and the refusal
# branch of the real default floor.
FAKE_PY = '''#!{real}
import os, sys
if len(sys.argv) > 2 and sys.argv[1] == "-c" and "statvfs" in sys.argv[2]:
    blocks, bfree, bavail = (int(x) for x in os.environ["FAKE_STATVFS"].split(","))
    fake = os.statvfs_result((4096, 4096, blocks, bfree, bavail, 0, 0, 0, 0, 255))
    os.statvfs = lambda path: fake
    code = sys.argv[2]
    sys.argv = ["-c"] + sys.argv[3:]
    exec(compile(code, "<string>", "exec"), {{"__name__": "__main__"}})
else:
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
'''


def _fake_py(tmp_path):
    p = tmp_path / "fakepy"
    p.write_text(FAKE_PY.format(real=sys.executable))
    p.chmod(0o755)
    return p


def _blocks(gib):  # 4096-byte blocks in <gib> GiB
    return gib * GIB // 4096


@pytest.mark.parametrize(
    "total, bfree, bavail, rc",
    [
        (100, 40, 30, 0),  # 5% of 100 GiB is 5 GiB: the 20 GiB arm wins; 30 free -> runs
        (100, 25, 15, 2),  # 15 available < 20 floor -> refused, although bfree (25) is above it
    ],
    ids=["min-arm-passes", "bavail-not-bfree-refuses"],
)
def test_d7_default_floor_on_a_simulated_small_volume(world, total, bfree, bavail, rc):
    repo, env, tmp_path = world
    r = run_tool(
        repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"],
        ENGRAM_PY=str(_fake_py(tmp_path)), SEAT_RUN_DISK_FLOOR=None,
        FAKE_STATVFS=f"{_blocks(total)},{_blocks(bfree)},{_blocks(bavail)}",
    )
    o = out(r)
    assert f"free {bavail:.1f} GiB of {total:.1f} GiB, floor 20.0 GiB" in r.stdout, o
    assert r.returncode == rc, o
    if rc == 2:
        assert "not started" in o and "max(5% of 100.0 GiB volume, 20 GiB)" in o, o
        assert list(env.root.iterdir()) == [], o
    else:
        assert "SUMMARY files=1 passed=2" in r.stdout, o


# Sol's colleague-pass cells (review on c93d616c): each kills a mutant that every cell above
# let through. d6b: empty / failing interpreter output must refuse, never fail open (a
# dropped empty-string case would run with no floor). d7b: free exactly at the floor runs
# (-lt, not -le). d7c: the 5% arm on a volume where it beats 20 GiB (D7's 100 GiB cannot
# tell total//20 from total//10). d4b: the G unit is GiB.
@pytest.mark.parametrize("body", [":", "exit 1"], ids=["empty-output", "nonzero-exit"])
def test_d6b_empty_or_failing_statvfs_is_refused(world, body):
    repo, env, tmp_path = world
    py = stub_python(tmp_path, 'case "$*" in *statvfs*) %s;; *pytest*) echo 9.0.0;; *) echo 3.12.0;; esac\n' % body)
    r = run_tool(repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(py), SEAT_RUN_DISK_FLOOR=None)
    assert r.returncode == 2, out(r)
    assert "cannot read free space" in out(r), out(r)
    assert list(env.root.iterdir()) == [], out(r)


@pytest.mark.parametrize("bavail, rc", [(20, 0), (19, 2)], ids=["free-equals-floor-runs", "one-gib-below-refuses"])
def test_d7b_boundary(world, bavail, rc):
    repo, env, tmp_path = world
    r = run_tool(
        repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(_fake_py(tmp_path)),
        SEAT_RUN_DISK_FLOOR=None, FAKE_STATVFS=f"{_blocks(100)},{_blocks(40)},{_blocks(bavail)}",
    )
    assert r.returncode == rc, out(r)


def test_d7c_five_percent_arm_on_a_1000_gib_volume(world):
    repo, env, tmp_path = world
    r = run_tool(
        repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(_fake_py(tmp_path)),
        SEAT_RUN_DISK_FLOOR=None, FAKE_STATVFS=f"{_blocks(1000)},{_blocks(60)},{_blocks(60)}",
    )
    assert "free 60.0 GiB of 1000.0 GiB, floor 50.0 GiB" in r.stdout, out(r)
    assert r.returncode == 0, out(r)


@pytest.mark.parametrize("floor, rc", [("15G", 0), ("40G", 2)])
def test_d4b_override_units(world, floor, rc):
    repo, env, tmp_path = world  # 30 GiB available on a simulated 100 GiB volume
    r = run_tool(
        repo, env, ["--ref", "HEAD", "--", "tests/test_ok.py"], ENGRAM_PY=str(_fake_py(tmp_path)),
        SEAT_RUN_DISK_FLOOR=floor, FAKE_STATVFS=f"{_blocks(100)},{_blocks(40)},{_blocks(30)}",
    )
    assert r.returncode == rc, (floor, out(r))
    assert f"floor {float(floor[:-1]):.1f} GiB" in r.stdout, (floor, out(r))
