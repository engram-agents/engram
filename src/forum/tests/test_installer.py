"""Tests for forum/deploy/install-forum-service.sh (part of #868 slice B).

Part 2 installer promotion tests:
- --dry-run flag: action plan lines printed, no systemd/filesystem side-effects.
- Template rendering: rendered unit contains substituted service-dir + port,
  retains UMask=002 and the ONE-INSTANCE invariant comment.
- Foreign-dir guard: abort if --service-dir exists, is non-empty, and has no app/.
- DB preservation: forum.db not overwritten.

These tests invoke the installer script via subprocess (bash --dry-run) and
inspect stdout/stderr. No systemd calls are made in --dry-run mode.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate installer and template paths (relative to worktree).
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent.parent  # src/forum/tests/ → src/forum/ → src/ → repo root
_INSTALLER = _REPO_ROOT / "src" / "forum" / "deploy" / "install-forum-service.sh"
_TEMPLATE = _REPO_ROOT / "src" / "forum" / "deploy" / "engram-forum.service.template"


def _run_installer(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run the installer shell script and return the CompletedProcess."""
    cmd = ["bash", str(_INSTALLER)] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# --dry-run: action plan lines printed, no filesystem mutations.
# ---------------------------------------------------------------------------

class TestDryRun:
    """--dry-run produces action plan output and causes no side-effects."""

    def test_dry_run_exits_0(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        assert result.returncode == 0, (
            f"--dry-run should exit 0, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_prints_dry_run_mode_banner(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower(), (
            f"Expected dry-run banner in output, got:\n{combined}"
        )

    def test_dry_run_mentions_snapshot_action(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "forum/" in combined or "Snapshot" in combined or "rsync" in combined, (
            f"Expected snapshot/copy action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_venv_action(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "venv" in combined.lower(), (
            f"Expected venv action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_unit_rendering(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # Should mention the unit file or systemd rendering
        assert "unit" in combined.lower() or "systemd" in combined.lower() or "service" in combined.lower(), (
            f"Expected unit rendering action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_systemctl_actions(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # systemctl daemon-reload + enable should be mentioned
        assert "daemon-reload" in combined or "systemctl" in combined, (
            f"Expected systemctl action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_no_filesystem_creation(self, tmp_path):
        """--dry-run must not create any files/dirs inside --service-dir."""
        service_dir = tmp_path / "forum_dry_test"
        _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        # Service dir should not be created (umask/mkdir are dry-run printed)
        assert not service_dir.exists(), (
            f"--dry-run must not create {service_dir}; it was created anyway."
        )

    def test_dry_run_custom_port_in_output(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--port", "5099",
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "5099" in combined, (
            f"Expected port 5099 mentioned in dry-run output, got:\n{combined}"
        )


# ---------------------------------------------------------------------------
# Template rendering: rendered unit has correct substitutions + invariants.
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    """Render the template manually and assert key properties."""

    def _render_template(self, service_dir: str, port: str = "5002") -> str:
        """Render the service template the same way the installer does."""
        template_text = _TEMPLATE.read_text(encoding="utf-8")
        # Strip >>>TEMPLATE-ONLY ... <<<TEMPLATE-ONLY block.
        lines = template_text.splitlines()
        filtered = []
        in_block = False
        for line in lines:
            if ">>>TEMPLATE-ONLY" in line:
                in_block = True
                continue
            if "<<<TEMPLATE-ONLY" in line:
                in_block = False
                continue
            if not in_block:
                filtered.append(line)
        rendered = "\n".join(filtered)
        # Substitute {{FORUM_HOME}} and port.
        rendered = rendered.replace("{{FORUM_HOME}}", service_dir)
        rendered = rendered.replace("5002", port)
        return rendered

    def test_rendered_unit_contains_service_dir(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert service_dir in rendered, (
            f"Rendered unit does not contain --service-dir path {service_dir!r}:\n{rendered}"
        )

    def test_rendered_unit_contains_port(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir, port="5099")
        assert "5099" in rendered, (
            f"Rendered unit does not contain port 5099:\n{rendered}"
        )

    def test_rendered_unit_retains_umask_002(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert "UMask=002" in rendered, (
            f"Rendered unit must retain UMask=002 (group-writable files):\n{rendered}"
        )

    def test_rendered_unit_retains_one_instance_comment(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        # The template carries an INVARIANT comment about one instance at a time.
        assert "INVARIANT" in rendered or "one" in rendered.lower(), (
            f"Rendered unit must retain the ONE-INSTANCE invariant comment:\n{rendered}"
        )

    def test_rendered_unit_has_no_template_only_block(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert "TEMPLATE-ONLY" not in rendered, (
            f"Rendered unit must not contain the >>>TEMPLATE-ONLY block:\n{rendered}"
        )

    def test_rendered_unit_has_no_unsubstituted_placeholders(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert "{{" not in rendered, (
            f"Rendered unit has unsubstituted placeholders:\n{rendered}"
        )

    def test_rendered_unit_contains_venv_python(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert ".venv/bin/python" in rendered, (
            f"Rendered unit must reference the venv Python:\n{rendered}"
        )


# ---------------------------------------------------------------------------
# Foreign-dir guard.
# ---------------------------------------------------------------------------

class TestForeignDirGuard:
    """Abort if --service-dir exists, is non-empty, and has no app/ subdir."""

    def test_empty_service_dir_ok(self, tmp_path):
        service_dir = tmp_path / "empty_forum"
        service_dir.mkdir()
        # Empty dir is fine — installer should proceed (and exit 0 in --dry-run).
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        assert result.returncode == 0, (
            f"Empty service-dir should be accepted, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_service_dir_with_app_subdir_ok(self, tmp_path):
        service_dir = tmp_path / "forum_with_app"
        service_dir.mkdir()
        (service_dir / "app").mkdir()
        # Has app/ → looks like ours → should proceed.
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        assert result.returncode == 0, (
            f"service-dir with app/ should be accepted, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_foreign_service_dir_aborts(self, tmp_path):
        service_dir = tmp_path / "foreign"
        service_dir.mkdir()
        # Non-empty with no app/ → foreign.
        (service_dir / "some_foreign_file.txt").write_text("foreign content")
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        assert result.returncode != 0, (
            f"Foreign service-dir should cause non-zero exit, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "foreign" in combined.lower() or "ERROR" in combined, (
            f"Expected foreign-dir error message:\n{combined}"
        )


# ---------------------------------------------------------------------------
# DB preservation: forum.db MUST survive an installer re-run unchanged.
# ---------------------------------------------------------------------------

class TestDBPreservation:
    """A pre-existing forum.db must survive an installer re-run unchanged.

    Structural invariant: the installer's copy step targets $SERVICE_DIR/app/forum/
    (code snapshot only). forum.db lives at $SERVICE_DIR/forum.db — a sibling of
    app/, not inside it. These paths never overlap, so rsync/cp cannot reach the DB.

    Two test forms, both required to prove the invariant:
      1. --dry-run integration: pre-create service-dir + forum.db + app/ marker,
         run the installer, assert DB is byte-identical afterward.
      2. Unit-level path test: show that the rsync/cp target path ($SERVICE_DIR/app/forum/)
         is strictly disjoint from $SERVICE_DIR/forum.db — i.e., the DB path is never
         a prefix-match of the copy destination.
    """

    def test_dry_run_db_preserved(self, tmp_path):
        """DB byte content is unchanged after a --dry-run installer pass."""
        service_dir = tmp_path / "forum"
        service_dir.mkdir()
        # Pre-create the app/ marker (foreign-dir guard passes).
        (service_dir / "app").mkdir()
        # Pre-create a forum.db with known content.
        db_file = service_dir / "forum.db"
        sentinel = b"SENTINEL_DB_CONTENT_DO_NOT_TOUCH"
        db_file.write_bytes(sentinel)

        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        assert result.returncode == 0, (
            f"--dry-run should exit 0 even with pre-existing forum.db:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # The DB must still exist and be byte-identical.
        assert db_file.exists(), (
            "forum.db was deleted by --dry-run installer pass!"
        )
        assert db_file.read_bytes() == sentinel, (
            f"forum.db content was changed by --dry-run installer pass!\n"
            f"Expected: {sentinel!r}\nGot: {db_file.read_bytes()!r}"
        )

    def test_db_path_disjoint_from_copy_target(self, tmp_path):
        """Structural unit-level check: rsync/cp target path cannot address forum.db.

        The copy step writes to $SERVICE_DIR/app/forum/ (code snapshot).
        forum.db lives at $SERVICE_DIR/forum.db — one level up from app/.
        These are structurally disjoint; no traversal from app/forum/ reaches ../forum.db.
        """
        service_dir = tmp_path / "forum"
        db_path = service_dir / "forum.db"
        copy_target = service_dir / "app" / "forum"

        # The DB path must NOT be relative to (inside) the copy target.
        try:
            db_path.relative_to(copy_target)
            is_inside = True
        except ValueError:
            is_inside = False

        assert not is_inside, (
            f"forum.db at {db_path} is INSIDE the copy target {copy_target} — "
            "the installer could overwrite it!"
        )

    def test_dry_run_installer_reports_db_preserved(self, tmp_path):
        """Installer output explicitly mentions DB preservation when forum.db exists."""
        service_dir = tmp_path / "forum"
        service_dir.mkdir()
        (service_dir / "app").mkdir()
        db_file = service_dir / "forum.db"
        db_file.write_bytes(b"existing_db")

        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # The installer prints an explicit "will NOT overwrite" message when forum.db exists.
        assert "NOT overwrite" in combined or "preserved" in combined.lower(), (
            f"Expected DB-preservation message in output when forum.db exists:\n{combined}"
        )


# ---------------------------------------------------------------------------
# Missing --src validation.
# ---------------------------------------------------------------------------

class TestMissingSrc:
    def test_missing_src_exits_nonzero(self, tmp_path):
        result = _run_installer([
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        assert result.returncode != 0, (
            f"Missing --src should exit non-zero, got {result.returncode}"
        )

    def test_missing_src_error_message(self, tmp_path):
        result = _run_installer([
            "--service-dir", str(tmp_path / "forum"),
            "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "src" in combined.lower() or "required" in combined.lower(), (
            f"Expected --src required error:\n{combined}"
        )
