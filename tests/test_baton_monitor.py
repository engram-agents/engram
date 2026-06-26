"""Tests for collab-baton-monitor.sh detection logic.

The poll loop itself runs indefinitely (with sleep 2), so tests use the
--once flag which does one poll iteration and exits immediately without
seeding or sleeping.  The COLLAB_BATON_MONITOR_SEEN_FILE env var lets each
test control the starting state of the seen-set.

Cases covered:
  1. Flip TO me by someone else           → emits exactly once.
  2. Same flip re-polled (de-dup)         → does NOT re-emit.
  3. Flip BY me to myself (self-flip)     → no emit.
  4. Flip to a DIFFERENT agent            → no emit.
  5. Missing/empty projects dir           → no crash, no emit.
  6. Empty agent_name in config           → exits with error, refuses to arm.
  7. No config.json at all               → exits with error, refuses to arm.
  8. Purpose key with metacharacter       → exits 1 with validation message.
  9. Purpose key with spaces              → exits 1 with validation message.
 10. Valid purpose key                    → passes validation (different exit).
 11. Self-init (FROM=initialized) latest  → does NOT emit (Change 1).
 12. Re-flip with new turn_since          → emits once more (not de-duped).
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "collab-baton-monitor.sh"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path: Path, agent_name: str) -> Path:
    """Create a minimal ~/.engram/config.json with the given agent_name."""
    engram_dir = tmp_path / ".engram"
    engram_dir.mkdir()
    (engram_dir / "config.json").write_text(
        json.dumps({"agent_name": agent_name}), encoding="utf-8"
    )
    return tmp_path


def _make_baton(projects_dir: Path, name: str, turn: str, turn_since: str,
                log_entries: list[tuple[str, str, str, str]]) -> Path:
    """Write a baton .md file with YAML frontmatter and turn log entries.

    log_entries: list of (timestamp, from_, to, reason).
    """
    lines = [
        "---",
        f"project: {name}",
        f"title: {name} test baton",
        "status: in-progress",
        f"turn: {turn}",
        f"turn_since: {turn_since}",
        f'turn_reason: "test"',
        "participants: [borges, ariadne, lei]",
        "---",
        f"# {name}",
        "",
        "## Turn log",
        "",
    ]
    for ts, from_, to, reason in log_entries:
        lines.append(f"- {ts} {from_} → {to}: {reason}")
    content = "\n".join(lines) + "\n"

    path = projects_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _run(
    tmp_home: Path,
    baton_dir: Path,
    seen_file: Path,
    extra_args: list[str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str, str]:
    """Run the baton monitor with --once against the given baton_dir.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    env["HOME"] = str(tmp_home)
    env["COLLAB_MONITOR_NO_REAP"] = "1"
    env["COLLAB_BATON_MONITOR_SEEN_FILE"] = str(seen_file)

    cmd = ["bash", str(_SCRIPT), "--once", str(baton_dir)]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -1, stdout, stderr


# ---------------------------------------------------------------------------
# Case 1: flip TO me by someone else → emits exactly once
# ---------------------------------------------------------------------------

def test_flip_to_me_emits(tmp_path):
    """A baton flipped to the local agent by another agent is emitted."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    _make_baton(
        projects, "PR-999",
        turn="borges", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T11:00:00Z", "initialized", "borges", "project initialized by borges"),
            ("2026-06-26T12:00:00Z", "ariadne",    "borges", "fairy-converged; colleague please"),
        ],
    )

    rc, stdout, stderr = _run(home, projects, seen)

    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr!r}"
    assert "📌 baton flipped to you" in stdout, f"Expected emit. stdout: {stdout!r}"
    assert "PR-999" in stdout, f"Expected baton name in output. stdout: {stdout!r}"
    assert "ariadne" in stdout, f"Expected FROM agent in output. stdout: {stdout!r}"
    assert "fairy-converged" in stdout, f"Expected reason in output. stdout: {stdout!r}"


# ---------------------------------------------------------------------------
# Case 2: same flip re-polled → does NOT re-emit (de-dup)
# ---------------------------------------------------------------------------

def test_flip_to_me_no_reemit_on_repoll(tmp_path):
    """A seen flip is not re-emitted on a subsequent poll iteration."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    _make_baton(
        projects, "PR-999",
        turn="borges", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T12:00:00Z", "ariadne", "borges", "fairy-converged; colleague please"),
        ],
    )

    # First poll — should emit and populate seen-file.
    rc1, stdout1, _ = _run(home, projects, seen)
    assert rc1 == 0
    assert "📌 baton flipped to you" in stdout1, (
        f"First poll should have emitted. stdout: {stdout1!r}"
    )
    assert seen.exists(), "Seen-file should exist after first poll."

    # Second poll with same seen-file — should NOT re-emit.
    rc2, stdout2, _ = _run(home, projects, seen)
    assert rc2 == 0
    assert "📌" not in stdout2, (
        f"Second poll should not re-emit. stdout: {stdout2!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: self-flip (borges → borges) → no emit
# ---------------------------------------------------------------------------

def test_self_flip_no_emit(tmp_path):
    """A baton whose latest flip was a self-flip does not emit."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    _make_baton(
        projects, "PR-999",
        turn="borges", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T12:00:00Z", "borges", "borges", "self-assigning for now"),
        ],
    )

    rc, stdout, stderr = _run(home, projects, seen)
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr!r}"
    assert "📌" not in stdout, f"Self-flip should not emit. stdout: {stdout!r}"


# ---------------------------------------------------------------------------
# Case 4: flip to a different agent → no emit
# ---------------------------------------------------------------------------

def test_flip_to_other_agent_no_emit(tmp_path):
    """A baton flipped to another agent (not SELF) does not emit."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    # Baton is turned to ariadne, not borges.
    _make_baton(
        projects, "PR-999",
        turn="ariadne", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T12:00:00Z", "borges", "ariadne", "colleague please"),
        ],
    )

    rc, stdout, stderr = _run(home, projects, seen)
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr!r}"
    assert "📌" not in stdout, (
        f"Flip to a different agent should not emit. stdout: {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case 5: missing / empty projects dir → no crash, no emit
# ---------------------------------------------------------------------------

def test_missing_projects_dir_no_crash(tmp_path):
    """Monitor handles a non-existent baton directory gracefully."""
    home = _make_home(tmp_path, "borges")
    missing_dir = tmp_path / "no-such-dir"
    seen = tmp_path / "seen"

    rc, stdout, stderr = _run(home, missing_dir, seen)
    assert rc == 0, f"Expected exit 0 for missing dir, got {rc}. stderr: {stderr!r}"
    assert "📌" not in stdout, f"Missing dir should not emit. stdout: {stdout!r}"


def test_empty_projects_dir_no_emit(tmp_path):
    """Monitor handles an empty baton directory without emitting."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    rc, stdout, stderr = _run(home, projects, seen)
    assert rc == 0, f"Expected exit 0 for empty dir, got {rc}. stderr: {stderr!r}"
    assert "📌" not in stdout, f"Empty dir should not emit. stdout: {stdout!r}"


# ---------------------------------------------------------------------------
# Case 6 + 7: identity gate — refuse to arm without a valid agent_name
# ---------------------------------------------------------------------------

def test_empty_agent_name_refuses_to_arm(tmp_path):
    """Monitor exits 1 with a clear message when agent_name is empty string."""
    engram_dir = tmp_path / ".engram"
    engram_dir.mkdir()
    (engram_dir / "config.json").write_text(
        json.dumps({"agent_name": ""}), encoding="utf-8"
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    rc, stdout, stderr = _run(tmp_path, projects, seen)
    assert rc == 1, f"Expected exit 1 for empty agent_name, got {rc}"
    assert "refusing to arm" in stderr.lower() or "agent_name" in stderr.lower(), (
        f"Expected refusal message in stderr. Got: {stderr!r}"
    )


def test_missing_config_refuses_to_arm(tmp_path):
    """Monitor exits 1 when config.json is absent."""
    # No .engram dir / config.json at all.
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["COLLAB_MONITOR_NO_REAP"] = "1"
    env["COLLAB_BATON_MONITOR_SEEN_FILE"] = str(seen)

    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--once", str(projects)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert proc.returncode == 1, (
        f"Expected exit 1 when config missing, got {proc.returncode}. "
        f"stderr: {proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Cases 8–10: purpose key validation (mirrors test_monitor_purpose_validation)
# ---------------------------------------------------------------------------

def test_rejects_metachar_in_purpose(tmp_path):
    """Purpose key containing '.' → exit 1, validation message."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["COLLAB_MONITOR_NO_REAP"] = "1"
    env["COLLAB_BATON_MONITOR_SEEN_FILE"] = str(seen)

    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--purpose", "bad.key", "--once", str(projects)],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert proc.returncode == 1, f"Expected exit 1. stderr: {proc.stderr!r}"
    assert "[a-zA-Z0-9_-]" in proc.stderr or "must contain" in proc.stderr, (
        f"Expected validation error. Got: {proc.stderr!r}"
    )


def test_rejects_space_in_purpose(tmp_path):
    """Purpose key containing a space → exit 1, validation message."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["COLLAB_MONITOR_NO_REAP"] = "1"
    env["COLLAB_BATON_MONITOR_SEEN_FILE"] = str(seen)

    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--purpose", "key with spaces", "--once", str(projects)],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert proc.returncode == 1, f"Expected exit 1. stderr: {proc.stderr!r}"
    assert "[a-zA-Z0-9_-]" in proc.stderr or "must contain" in proc.stderr, (
        f"Expected validation error. Got: {proc.stderr!r}"
    )


def test_accepts_valid_purpose_key(tmp_path):
    """Valid purpose key passes validation; --once exits 0 from the poll path."""
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    rc, stdout, stderr = _run(home, projects, seen, extra_args=["--purpose", "valid-key_123"])
    # Exit 0 from the --once poll path, not from the validation failure path.
    assert rc == 0, f"Expected exit 0 for valid purpose key. stderr: {stderr!r}"
    assert "[a-zA-Z0-9_-]" not in stderr and "must contain" not in stderr, (
        f"Got purpose-validation error for a valid key. stderr: {stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 11: self-init (FROM=initialized, latest entry) → no emit  [Change 1]
# ---------------------------------------------------------------------------

def test_init_flip_no_emit(tmp_path):
    """A baton whose LATEST turn-log entry is 'initialized → SELF' does not emit.

    This is the self-init false-positive: the baton was created with turn=SELF
    but no genuine hand-off from another agent has occurred yet.  The monitor
    must not wake on baton creation.
    """
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    _make_baton(
        projects, "PR-999",
        turn="borges", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T12:00:00Z", "initialized", "borges", "project initialized by borges"),
        ],
    )

    rc, stdout, stderr = _run(home, projects, seen)
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr!r}"
    assert "📌" not in stdout, (
        f"Self-init (FROM=initialized) should not emit. stdout: {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case 12: re-flip with new turn_since → emits once more (not de-duped)
# ---------------------------------------------------------------------------

def test_reflip_new_turn_since_emits(tmp_path):
    """A re-flip of the same baton with a new turn_since emits once more.

    De-dup key is basename:turn_since.  When the baton is flipped away and
    then flipped back to this agent with a DIFFERENT turn_since, the new key
    is not in the seen-set and must emit — verifying that de-dup is keyed
    on turn_since, not just on basename.
    """
    home = _make_home(tmp_path, "borges")
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = tmp_path / "seen"

    # First flip: ariadne → borges at T1.
    _make_baton(
        projects, "PR-998",
        turn="borges", turn_since="2026-06-26T12:00:00Z",
        log_entries=[
            ("2026-06-26T12:00:00Z", "ariadne", "borges", "fairy-converged; colleague please"),
        ],
    )

    # Poll 1 — should emit (seen is empty, flip is new).
    rc1, stdout1, _ = _run(home, projects, seen)
    assert rc1 == 0, f"Poll 1 expected exit 0, got {rc1}"
    assert "📌 baton flipped to you" in stdout1, (
        f"Poll 1 should emit for first flip. stdout: {stdout1!r}"
    )
    assert seen.exists(), "Seen-file should exist after Poll 1."

    # Baton flipped away (borges → lei) then back (lei → borges) with new T2.
    _make_baton(
        projects, "PR-998",
        turn="borges", turn_since="2026-06-26T14:00:00Z",   # NEW turn_since
        log_entries=[
            ("2026-06-26T12:00:00Z", "ariadne", "borges", "fairy-converged; colleague please"),
            ("2026-06-26T13:00:00Z", "borges",  "lei",    "ready for merge"),
            ("2026-06-26T14:00:00Z", "lei",      "borges", "needs a small tweak"),
        ],
    )

    # Poll 2 — should emit again; the new key (PR-998.md:T2) is not in the
    # seen-file which still holds (PR-998.md:T1) from Poll 1.
    rc2, stdout2, _ = _run(home, projects, seen)
    assert rc2 == 0, f"Poll 2 expected exit 0, got {rc2}"
    assert "📌 baton flipped to you" in stdout2, (
        f"Poll 2 should emit for re-flip (new turn_since). stdout: {stdout2!r}"
    )
    assert "PR-998" in stdout2, f"Expected baton name in output. stdout: {stdout2!r}"
    assert "lei" in stdout2, f"Expected FROM agent (lei) in Poll 2 output. stdout: {stdout2!r}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
