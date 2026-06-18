"""Tests for tools/check-claude-md-drift.py — rendered CLAUDE.md drift detector CLI.

Test categories:
  - Exit 0 (no drift): two refs rendering identically produce returncode 0, empty stdout.
  - Exit 1 (drift detected): a head commit that changes a template source produces
    returncode 1 and a unified diff on stdout. This is the mutation guard — if the
    script's drift-detection logic were broken, this test would fail.
  - Exit 2 (usage / argument errors): missing required args and --repo pointing at a
    non-git directory both produce returncode 2 via argparse parser.error().

Follows the tmp_path + subprocess.run([sys.executable, str(CLI_PATH)] + args,
capture_output=True, text=True, timeout=...) idiom used in test_ia_cli.py.

The fixture builds a minimal throwaway git repo in tmp_path containing only the two
template source files that check-claude-md-drift.py reads:
  src/engram/templates/template.CLAUDE.md
  src/engram/templates/compact-instructions.md

The script imports template_render from its own source tree (resolved from __file__),
so the fixture only needs the template content at the git refs — the render logic
comes from the installed worktree.
"""

import subprocess
import sys
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).parent.parent / "tools" / "check-claude-md-drift.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATE_CONTENT = """\
# I'm {{AGENT_NAME}}, an AI agent with ENGRAM.

<!-- Source of truth: compact-instructions.md — edit there, not in this template. This comment and the marker below are both replaced at install-time. -->
{{COMPACT_INSTRUCTIONS}}
"""

_COMPACT_CONTENT = """\
When summarizing this conversation, write in first person.
"""


def _run_drift(repo: Path, base_ref: str, head_ref: str) -> tuple[int, str, str]:
    """Invoke the drift CLI as a subprocess; return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--repo", str(repo),
            "--base", base_ref,
            "--head", head_ref,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the repo; raise on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n{result.stderr}"
        )
    return result


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one base commit (both template files present).

    Returns the repo path. The HEAD after this call is the base commit; callers can
    mutate files and commit again to produce a head commit that differs from base.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()

    # git init
    _git(repo, "init")
    # Configure local identity so git commit doesn't fail
    _git(repo, "config", "user.email", "test@test.invalid")
    _git(repo, "config", "user.name", "Test Runner")

    # Create src/engram/templates/
    templates_dir = repo / "src" / "engram" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "template.CLAUDE.md").write_text(_TEMPLATE_CONTENT, encoding="utf-8")
    (templates_dir / "compact-instructions.md").write_text(_COMPACT_CONTENT, encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base commit")

    return repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo with one base commit containing both template files."""
    return _make_git_repo(tmp_path)


# ---------------------------------------------------------------------------
# Tests: exit 0 — no drift
# ---------------------------------------------------------------------------

class TestNoDrift:
    """Comparing a ref to itself (or two commits with identical renders) exits 0."""

    def test_same_ref_exits_0(self, git_repo: Path):
        """--base HEAD --head HEAD: identical renders → exit 0, empty stdout."""
        rc, stdout, stderr = _run_drift(git_repo, "HEAD", "HEAD")
        assert rc == 0, f"Expected exit 0 (no drift); stderr: {stderr}"
        assert stdout == "", f"Expected empty stdout when no drift; got: {stdout!r}"

    def test_two_identical_commits_exits_0(self, git_repo: Path, tmp_path: Path):
        """Two commits whose template content renders identically → exit 0."""
        # Record base SHA
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Make a second commit that changes something UNRELATED to the templates
        (git_repo / "unrelated.txt").write_text("not a template file\n", encoding="utf-8")
        _git(git_repo, "add", "unrelated.txt")
        _git(git_repo, "commit", "-m", "unrelated change")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 0, f"Expected exit 0 (no drift); stderr: {stderr}"
        assert stdout == "", f"Expected empty stdout; got: {stdout!r}"


# ---------------------------------------------------------------------------
# Tests: exit 1 — drift detected (this is the mutation guard)
# ---------------------------------------------------------------------------

class TestDriftDetected:
    """When a head commit changes a template source, the CLI exits 1 with a diff."""

    def test_template_change_exits_1(self, git_repo: Path, tmp_path: Path):
        """Changing template.CLAUDE.md in the head commit → exit 1, diff on stdout."""
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Mutate the template file
        templates_dir = git_repo / "src" / "engram" / "templates"
        new_content = _TEMPLATE_CONTENT + "\n## A new section added in head\n"
        (templates_dir / "template.CLAUDE.md").write_text(new_content, encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "add section to template")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 1, (
            f"Expected exit 1 (drift detected); got rc={rc}. "
            f"stdout: {stdout!r}, stderr: {stderr!r}"
        )
        # stdout must contain a unified diff
        assert "---" in stdout or "+++" in stdout, (
            f"Expected unified diff markers in stdout; got: {stdout!r}"
        )
        # The changed text must appear in the diff
        assert "A new section added in head" in stdout, (
            f"Expected changed text in diff; got: {stdout!r}"
        )

    def test_compact_instructions_change_exits_1(self, git_repo: Path, tmp_path: Path):
        """Changing compact-instructions.md in the head commit → exit 1, diff on stdout."""
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Mutate the compact-instructions file (this is folded into the render)
        templates_dir = git_repo / "src" / "engram" / "templates"
        new_compact = _COMPACT_CONTENT + "\nAn additional compact instruction.\n"
        (templates_dir / "compact-instructions.md").write_text(new_compact, encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "add compact instruction")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 1, (
            f"Expected exit 1 (drift detected from compact change); got rc={rc}. "
            f"stdout: {stdout!r}, stderr: {stderr!r}"
        )
        assert "---" in stdout or "+++" in stdout, (
            f"Expected unified diff in stdout; got: {stdout!r}"
        )
        assert "An additional compact instruction" in stdout, (
            f"Expected changed compact text in diff; got: {stdout!r}"
        )


# ---------------------------------------------------------------------------
# Tests: exit 2 — usage / argument errors
# ---------------------------------------------------------------------------

class TestUsageErrors:
    """Missing required args and invalid --repo produce exit 2 via parser.error()."""

    def test_missing_head_arg_exits_2(self, git_repo: Path):
        """Omitting --head (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--repo", str(git_repo), "--base", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --head; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_missing_base_arg_exits_2(self, git_repo: Path):
        """Omitting --base (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--repo", str(git_repo), "--head", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --base; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_missing_repo_arg_exits_2(self):
        """Omitting --repo (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--base", "HEAD", "--head", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --repo; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_repo_not_a_git_dir_exits_2(self, tmp_path: Path):
        """--repo pointing at a plain directory (no .git) → exit 2 via parser.error()."""
        plain_dir = tmp_path / "not_a_git_repo"
        plain_dir.mkdir()
        rc, stdout, stderr = _run_drift(plain_dir, "HEAD", "HEAD")
        assert rc == 2, (
            f"Expected exit 2 for non-git --repo; got rc={rc}. "
            f"stderr: {stderr!r}"
        )
