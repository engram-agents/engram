"""Tests for src/forum/deploy/forum-drift-check.sh (issue #2824).

Read-only drift check: is the deployed forum app tree (<service-dir>/app/forum/)
current relative to src/forum/ at a given git ref? These tests invoke the
script via subprocess against synthetic fixtures — a throwaway git repo
standing in for the "src" checkout, and a plain directory standing in for the
"deployed" tree. No network access, no dependency on the real
/home/agents-shared/forum.

Covers: identical trees -> CURRENT + exit 0; a modified file -> DIFFERS +
exit 1; a ref-only file -> MISSING IN DEPLOYED; a deployed-only file ->
MISSING IN SOURCE; exclusion of non-source/runtime-state paths (tests/,
deploy/, spec.md, README.md, fairy-spec-*.md, forum.db, *.jsonl,
__pycache__, .venv).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_SCRIPT = _REPO_ROOT / "src" / "forum" / "deploy" / "forum-drift-check.sh"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    cmd = ["bash", str(_SCRIPT)] + args
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def src_repo(tmp_path) -> Path:
    """A throwaway git repo with a src/forum/ tree, standing in for --src."""
    repo = tmp_path / "srcrepo"
    (repo / "src" / "forum" / "tests").mkdir(parents=True)
    (repo / "src" / "forum" / "deploy").mkdir(parents=True)

    forum = repo / "src" / "forum"
    (forum / "server.py").write_text("print('hello')\n")
    (forum / "db.py").write_text("identical content\n")
    (forum / "README.md").write_text("# readme\n")
    (forum / "spec.md").write_text("spec\n")
    (forum / "fairy-spec-backend.md").write_text("fairy spec\n")
    (forum / "tests" / "test_x.py").write_text("def test(): pass\n")
    (forum / "deploy" / "install-forum-service.sh").write_text("#!/bin/bash\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def deployed_dir(tmp_path) -> Path:
    """A plain directory standing in for --service-dir's app/forum/."""
    d = tmp_path / "deployed" / "app" / "forum"
    d.mkdir(parents=True)
    return d.parent.parent  # returns --service-dir (parent of app/)


class TestCurrent:
    def test_identical_trees_report_current_and_exit_0(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert "CURRENT" in result.stdout
        assert "STALE" not in result.stdout

    def test_current_output_lists_identical_files(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert "IDENTICAL" in result.stdout
        assert "server.py" in result.stdout


class TestDiffers:
    def test_modified_file_reports_differs_and_exits_1(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text("print('DIFFERENT')\n")
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert "DIFFERS" in result.stdout
        assert "server.py" in result.stdout
        assert "STALE" in result.stdout

    def test_differs_line_carries_byte_or_line_delta(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text("print('DIFFERENT')\n")
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        # Grab the DIFFERS line and check it carries some count info.
        differs_lines = [l for l in result.stdout.splitlines() if l.startswith("DIFFERS")]
        assert differs_lines, f"no DIFFERS line found:\n{result.stdout}"
        assert "bytes" in differs_lines[0] or "lines" in differs_lines[0]

    def test_default_output_has_no_diff_body(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text("print('DIFFERENT')\n")
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        # Unified-diff markers should NOT appear without --verbose.
        assert "@@" not in result.stdout

    def test_verbose_includes_diff_body(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text("print('DIFFERENT')\n")
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        result = _run([
            "--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD", "--verbose",
        ])
        assert result.returncode == 1
        assert "@@" in result.stdout
        assert "DIFFERENT" in result.stdout


class TestMissing:
    def test_ref_only_file_reports_missing_in_deployed(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())
        # server.py exists at ref but not deployed at all.

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 1
        assert "MISSING IN DEPLOYED" in result.stdout
        assert "server.py" in result.stdout

    def test_deployed_only_file_reports_missing_in_source(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())
        (deployed_dir / "app" / "forum" / "oldmodule.py").write_text("leftover\n")

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 1
        assert "MISSING IN SOURCE" in result.stdout
        assert "oldmodule.py" in result.stdout


class TestExclusions:
    """Non-source and runtime-state paths must never appear in the report."""

    def test_excluded_paths_never_appear(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        # Deployed-side runtime junk that must be ignored, not reported as
        # MISSING IN SOURCE.
        (deployed_dir / "app" / "forum" / "__pycache__").mkdir()
        (deployed_dir / "app" / "forum" / "__pycache__" / "server.cpython.pyc").write_text("junk\n")
        (deployed_dir / "app" / "forum" / "forum.db").write_text("sqlite junk\n")
        (deployed_dir / "app" / "forum" / "some-log.jsonl").write_text("{}\n")
        venv_dir = deployed_dir / "app" / "forum" / ".venv" / "bin"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python").write_text("junk\n")

        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert "CURRENT" in result.stdout
        for excluded in ("__pycache__", ".pyc", "forum.db", "some-log.jsonl", ".venv"):
            assert excluded not in result.stdout, (
                f"{excluded!r} should be excluded from the report, but appeared:\n{result.stdout}"
            )

    def test_source_side_exclusions_not_flagged_missing_in_deployed(self, src_repo, deployed_dir):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())

        # src_repo fixture ships tests/, deploy/, README.md, spec.md,
        # fairy-spec-backend.md under src/forum/ — none copied to deployed/.
        # None of these should surface as MISSING IN DEPLOYED.
        result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        for excluded in ("test_x.py", "install-forum-service.sh", "README.md", "spec.md", "fairy-spec-backend.md"):
            assert excluded not in result.stdout, (
                f"{excluded!r} should be excluded from the report, but appeared:\n{result.stdout}"
            )


class TestUsageErrors:
    def test_missing_service_dir_exits_2(self, src_repo, tmp_path):
        result = _run([
            "--src", str(src_repo),
            "--service-dir", str(tmp_path / "does-not-exist"),
            "--ref", "HEAD",
        ])
        assert result.returncode == 2
        combined = result.stdout + result.stderr
        assert "not found" in combined.lower()

    def test_bad_src_exits_2(self, tmp_path, deployed_dir):
        result = _run([
            "--src", str(tmp_path / "not-a-checkout"),
            "--service-dir", str(deployed_dir),
            "--ref", "HEAD",
        ])
        assert result.returncode == 2

    def test_bad_ref_exits_2(self, src_repo, deployed_dir):
        result = _run([
            "--src", str(src_repo),
            "--service-dir", str(deployed_dir),
            "--ref", "this-ref-does-not-exist",
        ])
        assert result.returncode == 2
        combined = result.stdout + result.stderr
        assert "ref" in combined.lower()

    def test_help_exits_0(self):
        result = _run(["--help"])
        assert result.returncode == 0
        assert "Exit codes" in result.stdout

    def test_unreadable_deployed_dir_exits_2_not_false_stale(self, src_repo, deployed_dir):
        """Regression (#2824 live find): a permission-denied deployed dir must
        surface as a clear usage error (exit 2), never as a false STALE
        verdict. Before this test's fix, a `cd`-based enumeration failing
        inside a process substitution was silently swallowed by bash (a
        failure inside `<(...)` does not propagate under `set -e`), so the
        deployed side enumerated as empty and every source file printed as
        "MISSING IN DEPLOYED" — a real defect found by running this script
        against the actual live /home/agents-shared/forum/app/forum, which is
        group-owned in a way that denies read access outside its admin's own
        private group.
        """
        target = deployed_dir / "app" / "forum"
        target.chmod(0o000)
        try:
            result = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        finally:
            target.chmod(0o755)  # restore so tmp_path cleanup can remove it
        assert result.returncode == 2, (
            f"expected exit 2 (usage error) on unreadable deployed dir, got {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "permission" in combined.lower()
        assert "MISSING IN DEPLOYED" not in result.stdout


class TestExitCodeContract:
    def test_current_is_exit_0_stale_is_exit_1_never_confused_with_usage_error_2(
        self, src_repo, deployed_dir
    ):
        forum = src_repo / "src" / "forum"
        (deployed_dir / "app" / "forum" / "server.py").write_text((forum / "server.py").read_text())
        (deployed_dir / "app" / "forum" / "db.py").write_text((forum / "db.py").read_text())
        current = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert current.returncode == 0

        (deployed_dir / "app" / "forum" / "server.py").write_text("changed\n")
        stale = _run(["--src", str(src_repo), "--service-dir", str(deployed_dir), "--ref", "HEAD"])
        assert stale.returncode == 1
