"""The one-time sweep scope guards are retired permanently [part of #3863].

tests/test_prose_home_sweep_3863.py pinned the file list of the home-path prose sweep so
that two sibling PRs could not touch the same files while the sweep was in flight. The
sweep merged as caebaf3c, which ended that job. The guards compare a FROZEN fact (the
sweep commit's own diff) with the test file's MUTABLE constants (FILES and the PR 3a/3b
sets), so any later PR that deletes or renames a swept file would turn them red, in the
full suite only. They therefore stay in the file under their names, skipped
unconditionally, and are to be deleted when the #3863 epic closes.

CONTRACT (each clause is a test below; written before the change):
  R1  `_our_diff_files()` is the one retirement point and always skips, with the module's
      RETIRED_REASON ("sweep merged as ..."). The reason is identical with and without an
      origin/dev ref, with and without branch edits to swept files, in a repo that holds
      a sweep commit and in a repo that holds none: the skip depends on nothing the
      repo holds.
  R2  A branch that edits a swept file and a file owned by a sibling PR sees the three
      guard tests SKIPPED, not failed.
  R4  The four retired tests (the three scope guards plus the c5 diff-from-dev test) keep
      their names and, run through an inner pytest in the scratch repo, are all SKIPPED
      with a reason naming "sweep merged as", exit code 0, no FAILED.
  R3  The three guard tests keep their names and each still calls `_our_diff_files()`,
      so a rename or a deletion is a deliberate act and the skip reaches all three.
  R5  The inner pytest output is plain whatever colour variables the caller exports.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SWEEP_TEST = "tests/test_prose_home_sweep_3863.py"
GUARDS = (
    "test_scope_only_the_23_the_twin_the_baseline_and_this_test_changed",
    "test_disjoint_from_pr_3a_none_of_its_files_is_in_this_diff",
    "test_disjoint_from_pr_3b_none_of_its_files_is_in_this_diff",
)
# The c5 diff-from-dev test is retired by its own pytest.skip(RETIRED_REASON), not by
# _our_diff_files(); only R4 runs it, so GUARDS (used by R2, R3, R5) is unchanged.
C5_DIFF = "test_c5_baseline_diff_from_dev_is_exactly_our_23"
RETIRED = GUARDS + (C5_DIFF,)
# Files the copied sweep test loads at import time.
COPIED = (
    SWEEP_TEST,
    "tests/test_skill_home_paths_3863.py",
    "tests/_skill_block_runner.py",
    "tools/home_path_gate.py",
)


def _run(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _load(path):
    spec = importlib.util.spec_from_file_location("sweep_copy_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


_COLOUR_VARS = ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE")


def _inner_pytest(repo, ids, *flags):
    """Run pytest on ids inside the scratch repo, with plain output whatever colour
    variables the caller exports (the Claude Code harness sets FORCE_COLOR=3)."""
    env = {k: v for k, v in os.environ.items() if k not in _COLOUR_VARS}
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no", *flags, *ids],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )


def _scratch_repo(tmp_path, with_sweep=False, dev_ref=True):
    """A scratch repo with a base commit, an optional sweep commit, and a branch that
    edits one swept file and one file owned by a sibling PR.

    with_sweep=False: no sweep commit exists (the real SWEEP sha is absent), the shape
    of a checkout that never fetched it. with_sweep=True: a commit that edits both files
    is made, and the copied test's BASE and SWEEP are rewritten to this repo's own shas
    (the COPY only, never the real file). dev_ref adds an origin/dev ref at the base."""
    repo = tmp_path / "scratch"
    for rel in COPIED:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, repo / rel)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.invalid")
    _run(repo, "config", "user.name", "t")
    mod = _load(repo / SWEEP_TEST)
    swept = mod.SRC + mod.SKILL_FILES[0]
    sibling = mod.SRC + sorted(mod.COVERED_BY_PR_3A)[0]
    for rel in (swept, sibling):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("one\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "base")
    base = _run(repo, "rev-parse", "HEAD").stdout.strip()
    if dev_ref:
        _run(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    if with_sweep:
        for rel in (swept, sibling):
            (repo / rel).write_text("one\ntwo\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "sweep edits a swept file and a sibling's file")
        sweep = _run(repo, "rev-parse", "HEAD").stdout.strip()
        copy = repo / SWEEP_TEST
        text = copy.read_text()
        text, n1 = re.subn(r'(?m)^BASE = "[0-9a-f]{40}"$', f'BASE = "{base}"', text)
        text, n2 = re.subn(r'(?m)^SWEEP = "[0-9a-f]{40}"$', f'SWEEP = "{sweep}"', text)
        assert (n1, n2) == (1, 1), "BASE/SWEEP constants not rewritable by line match"
        copy.write_text(text)
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "point the copied test at this repo's shas")
    _run(repo, "checkout", "-q", "-b", "later-pr")
    for rel in (swept, sibling):
        (repo / rel).write_text("one\ntwo\nthree\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "later pr edits swept files")
    return repo


def test_r1_diff_files_skips_when_dev_already_carries_the_sweep(tmp_path):
    reasons = set()
    n = 0
    for with_sweep in (False, True):
        for dev_ref in (True, False):
            n += 1
            repo = _scratch_repo(tmp_path / f"c{n}", with_sweep=with_sweep, dev_ref=dev_ref)
            mod = _load(repo / SWEEP_TEST)
            with pytest.raises(pytest.skip.Exception) as e:
                mod._our_diff_files()  # with branch edits to swept files
            reasons.add(str(e.value))
            # Without branch edits to swept files: same answer.
            _run(repo, "checkout", "-q", "main")
            with pytest.raises(pytest.skip.Exception) as e:
                mod._our_diff_files()
            reasons.add(str(e.value))
    # The with_sweep copies carry a rewritten SWEEP, so only the sha differs.
    assert {re.sub(r"[0-9a-f]{8}", "<sha>", r) for r in reasons} == {
        "sweep merged as <sha>; concurrent-edit guard no longer applies"
    }, reasons
    assert all("sweep merged as" in r for r in reasons), reasons


def test_r2_a_later_branch_sees_the_three_guards_skipped_not_failed(tmp_path):
    repo = _scratch_repo(tmp_path)
    ids = [f"{SWEEP_TEST}::{n}" for n in GUARDS]
    r = _inner_pytest(repo, ids)
    tail = r.stdout[-600:]
    assert re.search(r"\b3 skipped\b", tail), r.stdout[-1500:]
    assert "failed" not in tail, r.stdout[-1500:]
    assert r.returncode == 0, r.stdout[-1500:]


def test_r4_the_retired_guards_keep_their_names_and_say_why_they_skip(tmp_path):
    for with_sweep in (False, True):
        repo = _scratch_repo(tmp_path / str(with_sweep), with_sweep=with_sweep)
        ids = [f"{SWEEP_TEST}::{n}" for n in RETIRED]
        r = _inner_pytest(repo, ids, "-rs")
        out = r.stdout
        assert re.search(r"\b4 skipped\b", out[-600:]), out[-1500:]
        assert "passed" not in out[-600:], out[-1500:]
        # Every SKIPPED line (-rs groups equal reasons) must carry the reason, and they
        # must cover all four tests: one test with another reason cannot hide behind the rest.
        lines = re.findall(r"(?m)^SKIPPED \[(\d+)\] .*$", out)
        skipped = re.findall(r"(?m)^SKIPPED \[\d+\] .*$", out)
        assert sum(map(int, lines)) == len(RETIRED), out[-1500:]
        assert all("sweep merged as" in ln for ln in skipped), out[-1500:]
        assert "FAILED" not in out and "failed" not in out[-600:], out[-1500:]
        assert r.returncode == 0, out[-1500:]


def test_r5_the_inner_pytest_output_is_plain_whatever_colour_the_caller_forces(tmp_path, monkeypatch):
    for k in _COLOUR_VARS:
        monkeypatch.setenv(k, "3" if k == "FORCE_COLOR" else "1")
    repo = _scratch_repo(tmp_path)
    ids = [f"{SWEEP_TEST}::{n}" for n in GUARDS]
    r = _inner_pytest(repo, ids, "-rfs")
    assert r.stdout.strip(), r.stderr
    assert "\x1b" not in r.stdout + r.stderr


def test_r3_the_three_guards_keep_their_names_and_all_call_diff_files():
    tree = ast.parse((REPO / SWEEP_TEST).read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in GUARDS:
        assert name in funcs, f"{name} was renamed or deleted; retire it deliberately"
        called = {
            c.func.id for c in ast.walk(funcs[name])
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        assert "_our_diff_files" in called, f"{name} no longer inherits the skip"
