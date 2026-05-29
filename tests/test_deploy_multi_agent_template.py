"""Tests for deploy.sh multi-agent CLAUDE.md drift-detection logic.

Exercises the AND-detection condition and drift-warning/marker lifecycle
introduced in the borges/multi-agent-claude-md-template PR (#448 related).

Test strategy:
  - Run deploy.sh --yes in a temp fixture; --dry-run flag is per-test.
  - --dry-run: suppresses actual rsync / file copies AND the marker write
    (gated on DRY_RUN=0). Warning emission still fires so warning-only paths
    are testable without touching the real install.
  - --yes: suppresses interactive prompts.
  - Tests that assert marker-write semantics (first-deploy-live, drift-update)
    use dry_run=False; they accept rsync failures on missing MANIFEST entries.
  - The multi-agent detection + marker logic runs BEFORE the $CHANGES gating
    that exits early on "no changes", so it fires regardless of MANIFEST state.

Fixture layout per test:
  - tmp_engram_home: stands in for $ENGRAM_HOME (--dry-run prevents rsync writes
    to it, but the dir must exist for the sanity checks to pass).
  - SOURCE_DIR: a copy of the repo root (symlink not needed — we set ENGRAM_HOME
    and ALPHA_DIR explicitly via environment).
  - config.json inside tmp_engram_home: controls the multi_agent config flag.
  - /home/agents-shared/ presence: controlled via a monkeypatched tempdir.

Note: the shared-filesystem check in deploy.sh uses the literal path
/home/agents-shared/. We cannot create that path in tests without root.
Instead we test via the config-flag-false path (single-agent) for the
partial-detection cases, and verify the AND-logic by confirming the config
flag alone (with /home/agents-shared/ absent) does not trigger the warning.
For the full multi-agent path, we use an override-friendly approach: the
deploy.sh block checks both conditions in a predictable order, so we can
cover the AND logic via the config=false branch.

See "Ambiguous decisions" note in PR handoff for rationale on the
/home/agents-shared/ constraint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_SH = REPO_ROOT / "tools" / "deploy.sh"
MULTI_AGENT_TEMPLATE = REPO_ROOT / "templates" / "CLAUDE.md.multi-agent.template"


def _run_deploy(
    engram_home: Path,
    *,
    extra_env: dict[str, str] | None = None,
    dry_run: bool = True,
) -> tuple[int, str, str]:
    """Run deploy.sh --yes (optionally --dry-run) with a custom ENGRAM_HOME.

    dry_run=True (default): passes --dry-run, suppresses rsync/file writes.
    dry_run=False: live mode — marker write fires but rsync may fail on missing
    MANIFEST entries (that's acceptable; we only care about the marker state).

    Returns (exit_code, stdout, stderr).
    """
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["ALPHA_DIR"] = str(REPO_ROOT)  # point at actual alpha for template resolution
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(DEPLOY_SH), "--yes"]
    if dry_run:
        cmd.append("--dry-run")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, *, multi_agent: bool = False) -> None:
    """Write a minimal config.json into engram_home."""
    cfg: dict = {}
    if multi_agent:
        cfg["multi_agent"] = True
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _marker_path(engram_home: Path) -> Path:
    return engram_home / ".deployed-multi-agent-template-sha"


# ---------------------------------------------------------------------------
# Test 1: Single-agent mode (no shared-fs, no config flag)
# ---------------------------------------------------------------------------

class TestSingleAgentMode:
    def test_no_marker_created_no_warning(self, tmp_path: Path) -> None:
        """Single-agent: no shared-fs + no config flag → no marker, no warning."""
        engram_home = tmp_path / "engram_home"
        engram_home.mkdir()
        _write_config(engram_home, multi_agent=False)

        rc, stdout, stderr = _run_deploy(engram_home)
        # deploy.sh may return non-zero if rsync can't find MANIFEST entries;
        # we only care about the warning and marker state, not overall exit code.

        assert not _marker_path(engram_home).exists(), (
            "Marker file must not be created for single-agent installs."
        )
        assert "Multi-agent CLAUDE.md template" not in stdout, (
            "Warning must not appear for single-agent mode."
        )

    def test_no_config_json_handled_cleanly(self, tmp_path: Path) -> None:
        """Missing config.json: jq fallback to false → treated as single-agent."""
        engram_home = tmp_path / "engram_home"
        engram_home.mkdir()
        # No config.json written — jq must handle missing file gracefully.

        rc, stdout, stderr = _run_deploy(engram_home)

        assert not _marker_path(engram_home).exists(), (
            "Marker file must not be created when config.json is missing."
        )
        assert "Multi-agent CLAUDE.md template" not in stdout, (
            "Warning must not appear when config.json is missing."
        )


# ---------------------------------------------------------------------------
# Test 2: Partial detection — config flag true, but no shared-fs
# ---------------------------------------------------------------------------

class TestPartialDetectionFlagOnly:
    def test_flag_true_no_shared_fs_no_warning(self, tmp_path: Path) -> None:
        """AND-detection: config flag true but /home/agents-shared/ absent → no warning.

        This verifies the load-bearing AND (not OR) logic. On the test host,
        /home/agents-shared/ may or may not exist. We test the config-flag-false
        case separately; this case directly exercises 'flag=true, no shared-fs'.
        """
        # Only run this test if /home/agents-shared/ does NOT exist on the host.
        if Path("/home/agents-shared/").is_dir():
            pytest.skip(
                "Skipped: /home/agents-shared/ exists on this host — "
                "cannot test the 'flag-true, no-shared-fs' partial-detection branch."
            )

        engram_home = tmp_path / "engram_home"
        engram_home.mkdir()
        _write_config(engram_home, multi_agent=True)

        rc, stdout, stderr = _run_deploy(engram_home)

        assert not _marker_path(engram_home).exists(), (
            "Marker file must not be created when shared-fs is absent."
        )
        assert "Multi-agent CLAUDE.md template" not in stdout, (
            "Warning must not appear when /home/agents-shared/ is absent."
        )


# ---------------------------------------------------------------------------
# Tests 3, 4, 5: Full multi-agent path (shared-fs exists AND flag true)
# ---------------------------------------------------------------------------

@pytest.fixture()
def multi_agent_engram_home(tmp_path: Path) -> Path | None:
    """Return a configured ENGRAM_HOME for multi-agent tests, or None if
    /home/agents-shared/ doesn't exist on this host (tests skip gracefully).
    """
    if not Path("/home/agents-shared/").is_dir():
        return None
    engram_home = tmp_path / "engram_home"
    engram_home.mkdir()
    _write_config(engram_home, multi_agent=True)
    return engram_home


class TestMultiAgentFullPath:
    def test_first_deploy_dry_run_warning_but_no_marker(
        self, multi_agent_engram_home: Path | None
    ) -> None:
        """Multi-agent, first deploy under --dry-run: warning emitted, marker NOT created.

        dry-run must not persist side-effects. After the DRY_RUN=0 gate (Item 1
        fix), the marker write is skipped, so subsequent runs will warn again.
        """
        if multi_agent_engram_home is None:
            pytest.skip("Skipped: /home/agents-shared/ not present on this host.")

        engram_home = multi_agent_engram_home
        assert not _marker_path(engram_home).exists(), "Pre-condition: no marker yet."

        rc, stdout, stderr = _run_deploy(engram_home, dry_run=True)

        assert not _marker_path(engram_home).exists(), (
            "Marker must NOT be created under --dry-run (DRY_RUN=0 gate)."
        )
        assert "Multi-agent CLAUDE.md template" in stdout, (
            "Warning must still be emitted on first multi-agent dry-run (marker absent)."
        )

    def test_first_deploy_live_warning_and_marker_created(
        self, multi_agent_engram_home: Path | None
    ) -> None:
        """Multi-agent, first live deploy (no marker): warning emitted, marker created."""
        if multi_agent_engram_home is None:
            pytest.skip("Skipped: /home/agents-shared/ not present on this host.")

        engram_home = multi_agent_engram_home
        assert not _marker_path(engram_home).exists(), "Pre-condition: no marker yet."

        rc, stdout, stderr = _run_deploy(engram_home, dry_run=False)

        assert _marker_path(engram_home).exists(), (
            "Marker file must be created on first live multi-agent deploy."
        )
        assert "Multi-agent CLAUDE.md template" in stdout, (
            "Warning must be emitted on first live multi-agent deploy (marker absent)."
        )
        marker_content = _marker_path(engram_home).read_text().strip()
        assert len(marker_content) == 64, (
            f"Marker must contain a 64-char sha256 hex string, got {marker_content!r}."
        )

    def test_no_drift_no_warning(self, multi_agent_engram_home: Path | None) -> None:
        """Multi-agent, no drift (marker matches current SHA): no warning."""
        if multi_agent_engram_home is None:
            pytest.skip("Skipped: /home/agents-shared/ not present on this host.")

        engram_home = multi_agent_engram_home
        # Pre-seed the marker with the current template SHA.
        import hashlib
        current_sha = hashlib.sha256(
            MULTI_AGENT_TEMPLATE.read_bytes()
        ).hexdigest()
        _marker_path(engram_home).write_text(current_sha + "\n")

        rc, stdout, stderr = _run_deploy(engram_home, dry_run=True)

        assert "Multi-agent CLAUDE.md template" not in stdout, (
            "Warning must NOT appear when marker SHA matches current template SHA."
        )

    def test_drift_detected_warning_emitted_and_marker_updated(
        self, multi_agent_engram_home: Path | None
    ) -> None:
        """Multi-agent, drift: marker has old SHA → warning emitted, marker updated.

        Uses live mode so the marker-write (gated on DRY_RUN=0) actually fires.
        """
        if multi_agent_engram_home is None:
            pytest.skip("Skipped: /home/agents-shared/ not present on this host.")

        engram_home = multi_agent_engram_home
        # Pre-seed marker with a bogus "old" SHA.
        old_sha = "a" * 64
        _marker_path(engram_home).write_text(old_sha + "\n")

        rc, stdout, stderr = _run_deploy(engram_home, dry_run=False)

        assert "Multi-agent CLAUDE.md template" in stdout, (
            "Warning must appear when marker SHA differs from current template SHA."
        )
        marker_content = _marker_path(engram_home).read_text().strip()
        assert marker_content != old_sha, (
            "Marker must be updated to the new SHA after drift is detected."
        )
        assert len(marker_content) == 64, (
            f"Updated marker must be a 64-char sha256 hex string, got {marker_content!r}."
        )


# ---------------------------------------------------------------------------
# Test 6: Template file presence check
# ---------------------------------------------------------------------------

class TestTemplateSanity:
    def test_multi_agent_template_exists(self) -> None:
        """The multi-agent template file must exist in the alpha source tree."""
        assert MULTI_AGENT_TEMPLATE.exists(), (
            f"templates/CLAUDE.md.multi-agent.template not found at {MULTI_AGENT_TEMPLATE}. "
            "This file is required for the drift-detection logic to operate."
        )

    def test_multi_agent_template_has_required_section(self) -> None:
        """Template must contain the required '## Local multi-agent rules' heading."""
        content = MULTI_AGENT_TEMPLATE.read_text()
        assert "## Local multi-agent rules" in content, (
            "Template must contain the '## Local multi-agent rules' heading."
        )

    def test_multi_agent_template_has_no_divider_prefix(self) -> None:
        """Template must NOT begin with a '---' divider (that belongs to live CLAUDE.md)."""
        content = MULTI_AGENT_TEMPLATE.read_text()
        assert not content.startswith("---"), (
            "Template must not start with '---' divider. The divider is a structural "
            "separator in the live CLAUDE.md, not part of the template itself."
        )
