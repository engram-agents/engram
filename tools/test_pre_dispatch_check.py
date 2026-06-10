"""Tests for tools/pre-dispatch-check.sh.

Coverage:
  1. exact-token match: #100 does NOT hit when searching for #1006
  2. exact-token match: #1006 in title IS a hit for #1006
  3. PR hit → exit 1 with DUPLICATE RISK message
  4. clear (no hit) → exit 0 with CLEAR message
  5. gh failure → exit 3 with UNKNOWN message
  6. no argument → exit 2 (usage)
  7. invalid argument → exit 2
  8. '#NN' prefix stripped correctly (leading '#' accepted)
  9. baton dir absent → single-agent note in CLEAR output
 10. baton file hit → exit 1 with DUPLICATE RISK + baton filename
 11. baton dir present but no matching files → exit 0 (clear)

All tests are hermetic: gh is stubbed via a shim on PATH; baton dir is a
tmpdir.  No network calls, no real baton files outside tmp_path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

# Locate the script relative to this test file
_TOOLS_DIR = Path(__file__).parent
_SCRIPT = _TOOLS_DIR / "pre-dispatch-check.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gh_shim(tmp_path: Path, payload: list[dict] | None = None, exit_code: int = 0) -> Path:
    """Create a fake 'gh' shim that returns the given JSON payload (or error).

    The shim is placed in a tmpdir/bin/ directory; callers add that dir to PATH.
    The JSON payload is written to a data file to avoid shell-quoting problems.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "gh"

    if exit_code != 0:
        shim.write_text(
            f'#!/usr/bin/env bash\necho "gh: simulated failure" >&2\nexit {exit_code}\n'
        )
    else:
        # Write JSON to a sidecar file so the shim can `cat` it without
        # any shell quoting issues (the JSON may contain chars that break
        # inline shell string literals).
        data_file = bin_dir / "gh-payload.json"
        data_file.write_text(json.dumps(payload or []))
        shim.write_text(
            f'#!/usr/bin/env bash\ncat {str(data_file)!r}\nexit 0\n'
        )

    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run(
    args: list[str],
    gh_bin_dir: Path | None = None,
    baton_dir: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if gh_bin_dir is not None:
        env["PATH"] = str(gh_bin_dir) + ":" + env.get("PATH", "")
    if baton_dir is not None:
        env["BATON_PROJECTS_DIR"] = str(baton_dir)
    elif "BATON_PROJECTS_DIR" in env:
        del env["BATON_PROJECTS_DIR"]
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(_SCRIPT)] + args,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Tests: argument validation
# ---------------------------------------------------------------------------

def test_no_arg_exits_2(tmp_path):
    """No argument → usage message on stderr, exit 2.

    BATON_PROJECTS_DIR is injected even though arg-validation exits first, so
    isolation from the live baton dir doesn't depend on check ordering."""
    env = os.environ.copy()
    env["BATON_PROJECTS_DIR"] = str(tmp_path / "no-projects")
    result = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "Usage" in result.stderr or "usage" in result.stderr.lower()


def test_invalid_arg_exits_2(tmp_path):
    """Non-numeric argument → exit 2. (Baton-dir isolated; see test_no_arg_exits_2.)"""
    env = os.environ.copy()
    env["BATON_PROJECTS_DIR"] = str(tmp_path / "no-projects")
    result = subprocess.run(["bash", str(_SCRIPT), "abc"], capture_output=True, text=True, env=env)
    assert result.returncode == 2


def test_leading_hash_stripped(tmp_path):
    """'#1006' should be treated the same as '1006' — strip the leading '#'."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    absent_baton = tmp_path / "no-projects"
    result = _run(["#1006"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 0
    assert "CLEAR" in result.stdout
    assert "1006" in result.stdout


# ---------------------------------------------------------------------------
# Tests: exact-token match logic
# ---------------------------------------------------------------------------

def test_exact_token_no_collision(tmp_path):
    """PR with '#100' in title must NOT be reported when searching for #1006."""
    prs = [{"number": 42, "title": "fix for #100 — the real issue", "author": {"login": "borges"}}]
    bin_dir = _make_gh_shim(tmp_path, payload=prs)
    absent_baton = tmp_path / "no-projects"
    result = _run(["1006"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 0, f"expected CLEAR; got: {result.stdout!r} err={result.stderr!r}"
    assert "CLEAR" in result.stdout


def test_exact_token_hit(tmp_path):
    """PR with '#1006' in title IS reported when searching for #1006."""
    prs = [{"number": 99, "title": "implement #1006 pre-dispatch check", "author": {"login": "ariadne"}}]
    bin_dir = _make_gh_shim(tmp_path, payload=prs)
    absent_baton = tmp_path / "no-projects"
    result = _run(["1006"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 1
    assert "DUPLICATE RISK" in result.stdout
    assert "#99" in result.stdout


def test_exact_token_with_trailing_text(tmp_path):
    """PR title '#1006: description' (colon follows) IS a hit for #1006."""
    prs = [{"number": 7, "title": "#1006: add pre-dispatch check", "author": {"login": "borges"}}]
    bin_dir = _make_gh_shim(tmp_path, payload=prs)
    absent_baton = tmp_path / "no-projects"
    result = _run(["1006"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 1
    assert "DUPLICATE RISK" in result.stdout


# ---------------------------------------------------------------------------
# Tests: exit codes and output content
# ---------------------------------------------------------------------------

def test_clear_exit_0(tmp_path):
    """No matching PRs, no baton hits → exit 0, CLEAR in stdout."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    absent_baton = tmp_path / "no-projects"
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 0
    assert "CLEAR" in result.stdout


def test_pr_hit_exit_1(tmp_path):
    """Matching PR → exit 1, DUPLICATE RISK."""
    prs = [{"number": 517, "title": "closes #500 baton fix", "author": {"login": "ariadne"}}]
    bin_dir = _make_gh_shim(tmp_path, payload=prs)
    absent_baton = tmp_path / "no-projects"
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 1
    assert "DUPLICATE RISK" in result.stdout
    assert "#517" in result.stdout
    assert "ariadne" in result.stdout


def test_gh_failure_exit_3(tmp_path):
    """gh non-zero exit → exit 3, UNKNOWN in stderr."""
    bin_dir = _make_gh_shim(tmp_path, exit_code=1)
    absent_baton = tmp_path / "no-projects"
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=absent_baton)
    assert result.returncode == 3
    assert "UNKNOWN" in result.stderr


# ---------------------------------------------------------------------------
# Tests: baton dir handling
# ---------------------------------------------------------------------------

def test_baton_dir_absent_note(tmp_path):
    """Non-existent baton dir → CLEAR with single-agent note."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    absent_dir = tmp_path / "does-not-exist"
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=absent_dir)
    assert result.returncode == 0
    assert "CLEAR" in result.stdout
    assert "single-agent" in result.stdout


def test_baton_hit_exit_1(tmp_path):
    """Baton file containing '#500' → exit 1, baton filename in output."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    baton_dir = tmp_path / "projects"
    baton_dir.mkdir()
    baton_file = baton_dir / "pool-cadence.md"
    baton_file.write_text(
        "---\nproject: pool-cadence\nturn: borges\n---\n\nFixes issue #500 in this bunch.\n"
    )
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=baton_dir)
    assert result.returncode == 1
    assert "DUPLICATE RISK" in result.stdout
    assert "pool-cadence.md" in result.stdout


def test_baton_dir_present_no_match_exit_0(tmp_path):
    """Baton dir present but no files match #500 → exit 0 (clear)."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    baton_dir = tmp_path / "projects"
    baton_dir.mkdir()
    # Write a baton that references a different issue
    (baton_dir / "unrelated.md").write_text(
        "---\nproject: unrelated\nturn: borges\n---\n\nRelates to #5000 not #500xx.\n"
    )
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=baton_dir)
    assert result.returncode == 0
    assert "CLEAR" in result.stdout


def test_baton_exact_token_no_collision(tmp_path):
    """Baton file with '#5001' must NOT be hit when searching for #500."""
    bin_dir = _make_gh_shim(tmp_path, payload=[])
    baton_dir = tmp_path / "projects"
    baton_dir.mkdir()
    (baton_dir / "wide.md").write_text(
        "---\nproject: wide\nturn: ariadne\n---\n\nCovers #5001 refactor.\n"
    )
    result = _run(["500"], gh_bin_dir=bin_dir, baton_dir=baton_dir)
    assert result.returncode == 0
    assert "CLEAR" in result.stdout


def test_corrupt_gh_stdout_is_unknown_not_clear(tmp_path):
    """Round 2: gh stdout that isn't JSON (e.g. a warning merged via 2>&1) must
    exit 3 (UNKNOWN), never 0 — a false CLEAR is the one forbidden direction."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text('#!/usr/bin/env bash\necho "! warning: something" \necho "[]"\nexit 0\n')
    shim.chmod(0o755)
    empty_baton = tmp_path / "no-batons"
    empty_baton.mkdir()
    result = _run(["1006"], gh_bin_dir=bin_dir, baton_dir=empty_baton)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNKNOWN" in (result.stdout + result.stderr)
