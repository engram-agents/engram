"""Tests for .githooks/pre-push's local test gate (stdin parsing + changed-file
classification).

.githooks/pre-push has no .py extension (it's a git hook, invoked directly by
git via its shebang), so it's loaded via importlib.machinery.SourceFileLoader
rather than a normal import -- mirrors the pattern other extensionless/private
hook-lib modules in this suite use for module loading, adapted because
spec_from_file_location alone can't infer a loader for an extensionless path.

Scope: the classification/parsing logic (parse_stdin, changed_files_for_update,
touches_test_relevant_py, _resolve_test_python) is unit-tested here with an
injectable git-runner so no real git subprocess is needed for the git-plumbing
calls. Per the spec, this suite does NOT shell out to a real `git push` --
run_test_suite (which actually invokes pytest) is monkeypatched out wherever
a test only cares about the skip/run DECISION, not the suite's own result.
"""

import importlib.machinery
import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parent.parent / ".githooks" / "pre-push"

ZERO40 = "0" * 40


def _load_module():
    """Load .githooks/pre-push as a fresh module instance, isolated per test."""
    loader = importlib.machinery.SourceFileLoader("prepush_test_instance", str(MODULE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


def _fake_completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# parse_stdin
# ---------------------------------------------------------------------------


def test_parse_stdin_single_line(mod):
    text = "refs/heads/topic aaaa refs/heads/topic bbbb\n"
    updates = mod.parse_stdin(text)
    assert len(updates) == 1
    u = updates[0]
    assert u.local_ref == "refs/heads/topic"
    assert u.local_sha == "aaaa"
    assert u.remote_ref == "refs/heads/topic"
    assert u.remote_sha == "bbbb"


def test_parse_stdin_multi_line_and_blank_and_malformed(mod):
    text = (
        "refs/heads/a sha1 refs/heads/a sha2\n"
        "\n"
        "malformed line with too many fields here\n"
        "refs/heads/b sha3 refs/heads/b sha4\n"
    )
    updates = mod.parse_stdin(text)
    assert len(updates) == 2
    assert updates[0].local_ref == "refs/heads/a"
    assert updates[1].local_ref == "refs/heads/b"


def test_parse_stdin_empty(mod):
    assert mod.parse_stdin("") == []


# ---------------------------------------------------------------------------
# _is_zero_sha
# ---------------------------------------------------------------------------


def test_is_zero_sha(mod):
    assert mod._is_zero_sha(ZERO40) is True
    assert mod._is_zero_sha("0" * 64) is True  # sha256-repo length, still all zero
    assert mod._is_zero_sha("a" * 40) is False
    assert mod._is_zero_sha("0" * 39 + "1") is False


# ---------------------------------------------------------------------------
# changed_files_for_update -- the four stdin-driven cases from the spec
# ---------------------------------------------------------------------------


def test_branch_delete_case_returns_none(mod):
    """Case 1: local sha all-zero == branch deletion. No git calls should even
    be needed; the function must short-circuit before touching git_runner."""
    update = mod.RefUpdate(
        local_ref="(delete)", local_sha=ZERO40,
        remote_ref="refs/heads/topic", remote_sha="a" * 40,
    )
    calls = []

    def spy_runner(args):
        calls.append(args)
        raise AssertionError("git_runner should not be called for a branch deletion")

    assert mod.changed_files_for_update(update, git_runner=spy_runner) is None
    assert calls == []


def test_new_branch_case_diffs_against_merge_base(mod):
    """Case 2: remote sha all-zero == new branch. Falls back to merge-base
    with origin/dev."""
    update = mod.RefUpdate(
        local_ref="refs/heads/new-topic", local_sha="deadbeef" * 5,
        remote_ref="refs/heads/new-topic", remote_sha=ZERO40,
    )
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[0] == "merge-base":
            return _fake_completed(stdout="basesha1234\n")
        if args[0] == "diff":
            assert args == ["diff", "--name-only", "basesha1234..deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"]
            return _fake_completed(stdout="src/engram/server.py\ndocs/README.md\n")
        raise AssertionError(f"unexpected git call: {args}")

    files = mod.changed_files_for_update(update, git_runner=fake_runner)
    assert files == ["src/engram/server.py", "docs/README.md"]
    assert calls[0][0] == "merge-base"
    assert calls[1][0] == "diff"


def test_new_branch_case_merge_base_failure_falls_back_to_empty_tree(mod, capsys):
    update = mod.RefUpdate(
        local_ref="refs/heads/new-topic", local_sha="c" * 40,
        remote_ref="refs/heads/new-topic", remote_sha=ZERO40,
    )

    def fake_runner(args):
        if args[0] == "merge-base":
            return _fake_completed(returncode=1, stderr="fatal: no such branch")
        if args[0] == "diff":
            assert args[1] == "--name-only"
            assert args[2].startswith(mod._EMPTY_TREE_SHA)
            return _fake_completed(stdout="src/engram/x.py\n")
        raise AssertionError(f"unexpected git call: {args}")

    files = mod.changed_files_for_update(update, git_runner=fake_runner)
    assert files == ["src/engram/x.py"]
    err = capsys.readouterr().err
    assert "could not compute a merge-base" in err


def test_docs_only_push_case(mod):
    """Case 3: ordinary update, only non-py/non-src-or-tests files changed."""
    update = mod.RefUpdate(
        local_ref="refs/heads/docs-fix", local_sha="1" * 40,
        remote_ref="refs/heads/docs-fix", remote_sha="2" * 40,
    )

    def fake_runner(args):
        assert args == ["diff", "--name-only", ("2" * 40) + ".." + ("1" * 40)]
        return _fake_completed(stdout="README.md\ndocs/DEVELOPMENT.md\n")

    files = mod.changed_files_for_update(update, git_runner=fake_runner)
    assert files == ["README.md", "docs/DEVELOPMENT.md"]
    assert mod.touches_test_relevant_py(files) is False


def test_src_py_push_case(mod):
    """Case 4: ordinary update touching a src/**.py file -- must trigger the gate."""
    update = mod.RefUpdate(
        local_ref="refs/heads/feature", local_sha="3" * 40,
        remote_ref="refs/heads/feature", remote_sha="4" * 40,
    )

    def fake_runner(args):
        return _fake_completed(stdout="src/engram/server.py\n")

    files = mod.changed_files_for_update(update, git_runner=fake_runner)
    assert files == ["src/engram/server.py"]
    assert mod.touches_test_relevant_py(files) is True


def test_tests_py_push_also_counts(mod):
    assert mod.touches_test_relevant_py(["tests/test_foo.py"]) is True


def test_non_py_under_src_does_not_count(mod):
    assert mod.touches_test_relevant_py(["src/engram/README.md"]) is False


def test_git_diff_failure_defaults_to_changed_not_silent(mod, capsys):
    update = mod.RefUpdate(
        local_ref="refs/heads/x", local_sha="5" * 40,
        remote_ref="refs/heads/x", remote_sha="6" * 40,
    )

    def fake_runner(args):
        return _fake_completed(returncode=1, stderr="fatal: bad revision")

    files = mod.changed_files_for_update(update, git_runner=fake_runner)
    assert files == ["<diff-unavailable>"]
    err = capsys.readouterr().err
    assert "git diff' failed" in err


# ---------------------------------------------------------------------------
# run_test_gate -- end-to-end decision, with run_test_suite stubbed out
# ---------------------------------------------------------------------------


def test_run_test_gate_all_deletions_skips(mod, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(mod, "run_test_suite", lambda **kw: called.append(kw) or 0)

    stdin_text = f"refs/heads/gone {ZERO40} refs/heads/gone {'a' * 40}\n"
    exit_code = mod.run_test_gate(stdin_text)
    assert exit_code == 0
    assert called == []
    assert "deletions" in capsys.readouterr().err


def test_run_test_gate_docs_only_skips(mod, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(mod, "run_test_suite", lambda **kw: called.append(kw) or 0)

    def fake_runner(args):
        return _fake_completed(stdout="README.md\n")

    stdin_text = f"refs/heads/docs {'1' * 40} refs/heads/docs {'2' * 40}\n"
    exit_code = mod.run_test_gate(stdin_text, git_runner=fake_runner)
    assert exit_code == 0
    assert called == []
    assert "skipping the test gate" in capsys.readouterr().err


def test_run_test_gate_src_py_runs_suite(mod, monkeypatch):
    called = []
    monkeypatch.setattr(mod, "run_test_suite", lambda **kw: called.append(kw) or 0)

    def fake_runner(args):
        return _fake_completed(stdout="src/engram/server.py\n")

    stdin_text = f"refs/heads/feat {'3' * 40} refs/heads/feat {'4' * 40}\n"
    exit_code = mod.run_test_gate(stdin_text, git_runner=fake_runner)
    assert exit_code == 0
    assert len(called) == 1


def test_run_test_gate_suite_failure_propagates(mod, monkeypatch):
    monkeypatch.setattr(mod, "run_test_suite", lambda **kw: 1)

    def fake_runner(args):
        return _fake_completed(stdout="src/engram/server.py\n")

    stdin_text = f"refs/heads/feat {'3' * 40} refs/heads/feat {'4' * 40}\n"
    exit_code = mod.run_test_gate(stdin_text, git_runner=fake_runner)
    assert exit_code == 1


# ---------------------------------------------------------------------------
# interpreter resolution order
# ---------------------------------------------------------------------------


def test_resolve_test_python_prefers_engram_python_env(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_PYTHON", "/some/explicit/python")
    path, label = mod._resolve_test_python(str(tmp_path))
    assert path == "/some/explicit/python"
    assert label == "$ENGRAM_PYTHON"


def test_resolve_test_python_prefers_repo_venv_over_home_venv(mod, monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_PYTHON", raising=False)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")

    # Make the home venv resolve to a real file too, to prove repo .venv wins.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".engram" / "venv" / "bin").mkdir(parents=True)
    (fake_home / ".engram" / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    path, label = mod._resolve_test_python(str(tmp_path))
    assert path == str(venv_bin / "python")
    assert label == ".venv/bin/python"


def test_resolve_test_python_falls_back_to_home_venv(mod, monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_PYTHON", raising=False)
    fake_home = tmp_path / "fake_home"
    (fake_home / ".engram" / "venv" / "bin").mkdir(parents=True)
    (fake_home / ".engram" / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    path, label = mod._resolve_test_python(str(tmp_path))
    assert path == str(fake_home / ".engram" / "venv" / "bin" / "python")
    assert label == "~/.engram/venv/bin/python"


def test_resolve_test_python_last_resort_bare_python3_warns(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ENGRAM_PYTHON", raising=False)
    fake_home = tmp_path / "fake_home_empty"
    fake_home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    path, label = mod._resolve_test_python(str(tmp_path))
    assert path == "python3"
    assert "python3" in label
    err = capsys.readouterr().err
    assert "coin-flip" in err


# ---------------------------------------------------------------------------
# run_test_suite -- pytest exit-code classification (only exit 1 blocks)
#
# Real repro for the bug this pins: 2026-08-18, a resolved interpreter that
# had pytest importable but not this repo's own deps (~/.engram/venv, no
# flask) made `pytest` exit 2 (collection errors) on a clean tree, and the
# old code treated any non-zero as "tests failed" -> blocked every push.
# These tests stub _resolve_test_python/_pytest_importable/subprocess.run so
# no real 4-minute suite runs; only the classification branch is exercised.
# ---------------------------------------------------------------------------


def _stub_for_exit_code(mod, monkeypatch, tmp_path, code):
    """Wire run_test_suite to resolve a fake interpreter, treat pytest as
    importable, and have the real pytest subprocess 'exit' with `code`."""
    monkeypatch.setattr(mod, "_resolve_test_python",
                         lambda repo_root: ("/fake/python", "fake-source-label"))
    monkeypatch.setattr(mod, "_pytest_importable", lambda python_path: True)
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=code),
    )
    return tmp_path


def test_run_test_suite_exit_1_blocks(mod, monkeypatch, tmp_path, capsys):
    """(1) exit 1 -> gate returns 1 (blocks). Pins the ONE legitimate block --
    without this, the fail-open fix above could regress to never blocking."""
    _stub_for_exit_code(mod, monkeypatch, tmp_path, 1)
    exit_code = mod.run_test_suite(repo_root=str(tmp_path))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err


def test_run_test_suite_exit_2_fails_open_with_actionable_message(mod, monkeypatch, tmp_path, capsys):
    """(2) exit 2 (interrupted/collection error -- the real-world shape measured
    2026-08-18) -> gate returns 0 (fail open), message names the resolved
    interpreter and the venv remedy. This is also the practical stand-in for
    "pytest importable but project deps missing" (item 5 in the spec's list):
    a deps-missing interpreter is exactly what makes pytest's own collection
    fail and exit 2 in the first place, so this test covers that real-world
    shape without needing an actual broken venv fixture."""
    _stub_for_exit_code(mod, monkeypatch, tmp_path, 2)
    exit_code = mod.run_test_suite(repo_root=str(tmp_path))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "/fake/python" in err  # (a) resolved interpreter
    assert "NOT blocking" in err  # (b) gate did not check anything
    assert ".venv/bin/pip install -r requirements.txt pytest" in err  # (c) remedy
    assert "$ENGRAM_PYTHON" in err  # (c) override


def test_run_test_suite_exit_5_fails_open_loud(mod, monkeypatch, tmp_path, capsys):
    """(3) exit 5 (no tests collected) -> gate returns 0, loud."""
    _stub_for_exit_code(mod, monkeypatch, tmp_path, 5)
    exit_code = mod.run_test_suite(repo_root=str(tmp_path))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "⚠" in err
    assert "no tests were collected" in err


def test_run_test_suite_exit_0_passes_green(mod, monkeypatch, tmp_path, capsys):
    """(4) exit 0 -> gate returns 0, green message."""
    _stub_for_exit_code(mod, monkeypatch, tmp_path, 0)
    exit_code = mod.run_test_suite(repo_root=str(tmp_path))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "✓ pre-push: local test suite green." in err


def test_run_test_suite_unrecognized_exit_code_also_fails_open(mod, monkeypatch, tmp_path, capsys):
    """Belt-and-suspenders: an exit code pytest itself doesn't document (not in
    0/1/2/3/4/5) must still fail open rather than block -- only exit 1 blocks,
    by construction, not by enumerating every other known code."""
    _stub_for_exit_code(mod, monkeypatch, tmp_path, 42)
    exit_code = mod.run_test_suite(repo_root=str(tmp_path))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "unrecognized pytest exit code 42" in err


# ---------------------------------------------------------------------------
# main() -- entry-point-level fail-open guarantee (structural, not per-check)
#
# The reviewer-found defect: _check_close_keywords called `which gh` via
# subprocess, and on a host with no `which` binary that raises
# FileNotFoundError uncaught anywhere -- the hook's traceback becomes a
# non-zero exit, blocking the push for an environment reason. The fix has two
# parts: (1) shutil.which replaces the `which` subprocess (tested directly
# below), and (2) main() itself wraps both checks in try/except so this class
# of bug -- wherever it next turns up -- fails open by construction rather than
# by each call site remembering to catch it. These tests pin (2); the
# `_check_close_keywords` sections above/below pin (1).
# ---------------------------------------------------------------------------


def test_main_exception_in_close_keyword_check_fails_open(mod, monkeypatch, capsys):
    """An unhandled exception raised inside the close-keyword check must not
    propagate: main() returns 0, and the traceback lands on stderr so the
    operator can see and report it."""
    def boom():
        raise FileNotFoundError("simulated: no 'which' binary on this host")

    monkeypatch.setattr(mod, "_check_close_keywords", boom)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(""))

    exit_code = mod.main()

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "FileNotFoundError" in err
    assert "the hook itself failed" in err
    assert "NOT blocking this push" in err


def test_main_exception_in_test_gate_fails_open(mod, monkeypatch, capsys):
    """Same guarantee, other check: an exception inside the test gate must
    also fail open with a traceback on stderr, not propagate to sys.exit."""
    monkeypatch.setattr(mod, "_check_close_keywords", lambda: 0)
    monkeypatch.setattr(mod, "run_test_gate", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("simulated: test gate blew up")
    ))
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(""))

    exit_code = mod.main()

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError" in err
    assert "the hook itself failed" in err
    assert "NOT blocking this push" in err


def test_main_negative_control_legitimate_block_still_blocks(mod, monkeypatch, capsys):
    """The load-bearing negative control: when both checks run CLEANLY and the
    test gate legitimately returns 1 (a genuine test failure), main() must
    still return 1. Without this, a wrapper broad enough to swallow every
    exception could just as easily swallow a real block -- this proves the
    try/except catches exceptions, not return values."""
    monkeypatch.setattr(mod, "_check_close_keywords", lambda: 0)
    monkeypatch.setattr(mod, "run_test_gate", lambda *a, **kw: 1)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(""))

    exit_code = mod.main()

    assert exit_code == 1


def test_check_close_keywords_no_gh_skips_cleanly_no_exception(mod, monkeypatch, capsys):
    """shutil.which returning None (no 'gh' on PATH) must skip the check
    cleanly via the standard-library call -- no subprocess, no exception
    possible from a missing 'which' binary."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    exit_code = mod._check_close_keywords()

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "'gh' CLI not found" in err
