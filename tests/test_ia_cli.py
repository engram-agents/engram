"""Tests for tools/ia.py — inter-agent CLI.

Test categories per DESIGN §8:
  - Smoke: each subcommand returns expected exit code on happy-path input
  - Validation: each frontmatter rule rejects bad input with the expected error
  - Cursor: monotonic advance across cursor --set / mark-read / read,
            atomic write, ISO format enforcement, --force override works
  - Filesystem: atomic write of letters (no .tmp remains after success)
  - Mode gate: single-agent mode returns clean error (exit 3)
  - Integration: write → list → read round-trip produces correct cursor advance

Follows the same tmp_path / subprocess / helper patterns as
tests/test_inter_agent_hook.py.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CLI_PATH = (
    Path(__file__).parent.parent / "tools" / "ia.py"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_ia(
    engram_home: Path,
    inter_agent_dir: Path,
    *args: str,
    stdin_text: str = "",
    env_overrides: dict | None = None,
) -> tuple:
    """Run the ia CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["INTER_AGENT_DIR"] = str(inter_agent_dir)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, mode: str, agent_name: str = "ariadne") -> None:
    """Write a minimal config.json into engram_home."""
    cfg = {"mode": mode, "agent_name": agent_name, "counterparts": []}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_read_cursor(engram_home: Path, ts: datetime) -> None:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    (engram_home / "inter-agent-read-cursor.txt").write_text(ts_str + "\n")


def _read_read_cursor(engram_home: Path) -> str:
    path = engram_home / "inter-agent-read-cursor.txt"
    if path.exists():
        return path.read_text().strip()
    return ""


def _write_letter(
    inter_agent_dir: Path,
    filename: str,
    from_agent: str,
    to_agent: str,
    timestamp: datetime,
    body: str = "Hello from a fellow agent.",
    re_field: str = "",
    heading: str = "",
) -> Path:
    """Write a properly-formatted inter-agent letter markdown file."""
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    fm_lines = [
        "---",
        f"from: {from_agent}",
        f"to: {to_agent}",
        f"timestamp: {ts_str}",
    ]
    if re_field:
        fm_lines.append(f"re: {re_field}")
    fm_lines.append("---")
    fm_lines.append("")
    if heading:
        fm_lines.append(f"# {heading}")
        fm_lines.append("")
    fm_lines.append(body)
    content = "\n".join(fm_lines) + "\n"
    path = inter_agent_dir / filename
    path.write_text(content)
    return path


def _ts(offset_minutes: int = -5) -> datetime:
    """Return a UTC datetime offset by the given minutes from now."""
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


@pytest.fixture
def inter_agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inter-agent"
    d.mkdir()
    return d


# ===========================================================================
# CATEGORY 1: Smoke tests — each subcommand happy-path
# ===========================================================================

class TestSmoke:
    """Each subcommand exits 0 on happy-path input."""

    def test_help_exits_0(self, engram_home, inter_agent_dir):
        """ia --help exits 0 and prints subcommand descriptions."""
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "--help")
        assert rc == 0
        assert "list" in stdout
        assert "read" in stdout
        assert "write" in stdout

    def test_status_multi_mode(self, engram_home, inter_agent_dir):
        """ia status exits 0 in multi mode and shows agent name."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status")
        assert rc == 0
        assert "ariadne" in stdout
        assert "multi" in stdout

    def test_list_empty_exits_0(self, engram_home, inter_agent_dir):
        """ia list with no letters exits 0."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 0

    def test_list_with_letter(self, engram_home, inter_agent_dir):
        """ia list shows a letter addressed to the agent."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="Hello from Borges",
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 0
        assert "borges" in stdout
        assert "Hello from Borges" in stdout

    def test_cursor_show_exits_0(self, engram_home, inter_agent_dir):
        """ia cursor --show exits 0 (no cursor file → shows '(none)')."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "cursor", "--show")
        assert rc == 0
        assert "none" in stdout

    def test_cursor_set_exits_0(self, engram_home, inter_agent_dir):
        """ia cursor --set exits 0 for a valid ISO timestamp."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--set", "2026-05-23T10:00:00Z",
        )
        assert rc == 0

    def test_mark_read_all_exits_0(self, engram_home, inter_agent_dir):
        """ia mark-read --all exits 0."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "mark-read", "--all")
        assert rc == 0

    def test_write_from_stdin_exits_0(self, engram_home, inter_agent_dir):
        """ia write --from-stdin exits 0 for valid letter."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Hello Borges, this is a test letter.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 0, f"write --from-stdin failed; stderr: {stderr}"
        assert "sent" in stdout


# ===========================================================================
# CATEGORY 2: Validation tests — frontmatter rules
# ===========================================================================

class TestValidation:
    """Frontmatter rules reject bad input with the expected error."""

    def _build_letter(
        self,
        from_val: str = "ariadne",
        to_val: str = "borges",
        ts_val: str = "2026-05-23T10:00:00Z",
        re_val: str = "",
        body: str = "Test body content.\n",
    ) -> str:
        fm = ["---", f"from: {from_val}", f"to: {to_val}", f"timestamp: {ts_val}"]
        if re_val:
            fm.append(f"re: {re_val}")
        fm.append("---")
        fm.append("")
        fm.append(body)
        return "\n".join(fm)

    def test_from_impersonation_rejected(self, engram_home, inter_agent_dir):
        """from: field not matching agent name → exit 1, impersonation error."""
        _write_config(engram_home, "multi", "ariadne")
        letter = self._build_letter(from_val="borges")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "impersonation" in stderr.lower() or "does not match" in stderr

    def test_to_empty_rejected(self, engram_home, inter_agent_dir):
        """to: value absent (line has only whitespace) → exit 1 with frontmatter error.

        Note: the regex parser (shared with the hook) treats an empty `to:` line
        followed by a newline as the value being merged with the next field, so
        the validation error may surface as a timestamp or to: complaint depending
        on parse outcome — the key invariant is rc == 1.
        """
        _write_config(engram_home, "multi", "ariadne")
        # Use a letter where 'to' is clearly absent (no 'to:' key at all)
        letter = (
            "---\n"
            "from: ariadne\n"
            "timestamp: 2026-05-23T10:00:00Z\n"
            "---\n\n"
            "Body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "to:" in stderr or "`to:`" in stderr

    def test_timestamp_filename_format_rejected(self, engram_home, inter_agent_dir):
        """timestamp in filename-format (dashes instead of colons) → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        letter = self._build_letter(ts_val="2026-05-23T10-00-00Z")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "filename-format" in stderr or "dashes" in stderr

    def test_timestamp_invalid_iso_rejected(self, engram_home, inter_agent_dir):
        """timestamp that isn't ISO-8601 at all → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        letter = self._build_letter(ts_val="not-a-timestamp")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "timestamp" in stderr

    def test_re_nonexistent_file_rejected(self, engram_home, inter_agent_dir):
        """re: pointing to a file that doesn't exist → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        letter = self._build_letter(re_val="nonexistent-letter.md")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "non-existent" in stderr or "nonexistent" in stderr or "not found" in stderr.lower()

    def test_re_existing_file_accepted(self, engram_home, inter_agent_dir):
        """re: pointing to an existing file → accepted (no re: error)."""
        _write_config(engram_home, "multi", "ariadne")
        # Create the referenced file
        ref_filename = "2026-05-23T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, ref_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60),
        )
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            f"re: {ref_filename}\n"
            "---\n\n"
            "Reply body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 0, f"re: existing file should be accepted; stderr: {stderr}"

    def test_missing_frontmatter_rejected(self, engram_home, inter_agent_dir):
        """No frontmatter block at all → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text="Just a plain body with no frontmatter.\n",
        )
        assert rc == 1
        assert "frontmatter" in stderr.lower()

    def test_to_empty_line_rejected_pre_parse(self, engram_home, inter_agent_dir):
        """Bare 'to:' line (newline immediately after colon) caught before parser merge-quirk."""
        _write_config(engram_home, "multi", "ariadne")
        # The frontmatter parser regex would merge `to:\n<next-field>` into
        # `to: <next-field-value>` — the pre-check catches this before parsing.
        letter = (
            "---\n"
            "from: ariadne\n"
            "to:\n"
            "timestamp: 2026-05-23T10:00:00Z\n"
            "---\n\n"
            "Body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 1
        assert "to" in stderr.lower() and ("empty" in stderr.lower() or "recipient" in stderr.lower())

    def test_write_no_to_no_reply_rejected(self, engram_home, inter_agent_dir):
        """Guard fires when neither --to nor --reply is passed."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 1
        assert "--to" in stderr and "--reply" in stderr


# ===========================================================================
# CATEGORY 3: Cursor tests
# ===========================================================================

class TestCursor:
    """Cursor mechanics: monotonic, ISO format, --force, atomic write."""

    def test_cursor_show_none_when_no_file(self, engram_home, inter_agent_dir):
        """No cursor file → shows '(none)'."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "cursor", "--show")
        assert rc == 0
        assert "none" in stdout

    def test_cursor_set_valid_ts(self, engram_home, inter_agent_dir):
        """ia cursor --set writes the timestamp to the cursor file."""
        _write_config(engram_home, "multi", "ariadne")
        target = "2026-05-22T10:00:00Z"
        rc, _, _ = _run_ia(engram_home, inter_agent_dir, "cursor", "--set", target)
        assert rc == 0
        cursor_content = _read_read_cursor(engram_home)
        assert cursor_content == target

    def test_cursor_show_after_set(self, engram_home, inter_agent_dir):
        """ia cursor --show after --set returns the set value."""
        _write_config(engram_home, "multi", "ariadne")
        target = "2026-05-22T10:00:00Z"
        _run_ia(engram_home, inter_agent_dir, "cursor", "--set", target)
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "cursor", "--show")
        assert rc == 0
        assert target in stdout

    def test_cursor_set_refuses_backward(self, engram_home, inter_agent_dir):
        """cursor --set backward without --force → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        # Set cursor forward first
        _run_ia(engram_home, inter_agent_dir, "cursor", "--set", "2026-05-22T10:00:00Z")
        # Try to go backward
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--set", "2026-05-21T10:00:00Z",
        )
        assert rc == 1
        assert "backward" in stderr

    def test_cursor_set_backward_with_force(self, engram_home, inter_agent_dir):
        """cursor --set backward WITH --force → succeeds."""
        _write_config(engram_home, "multi", "ariadne")
        _run_ia(engram_home, inter_agent_dir, "cursor", "--set", "2026-05-22T10:00:00Z")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--set", "2026-05-21T10:00:00Z", "--force",
        )
        assert rc == 0
        cursor_content = _read_read_cursor(engram_home)
        assert cursor_content == "2026-05-21T10:00:00Z"

    def test_cursor_set_filename_format_rejected(self, engram_home, inter_agent_dir):
        """cursor --set with filename-format value → exit 1, clear error."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--set", "2026-05-22T07-15-30Z_borges.md",
        )
        assert rc == 1
        assert "filename-format" in stderr or "dashes" in stderr

    def test_cursor_type_surfaced_requires_advanced(self, engram_home, inter_agent_dir):
        """cursor --type surfaced without --advanced → exit 1 with hint."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--show", "--type", "surfaced",
        )
        assert rc == 1
        assert "--advanced" in stderr

    def test_cursor_type_surfaced_with_advanced(self, engram_home, inter_agent_dir):
        """cursor --advanced --type surfaced --show → exits 0."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "cursor", "--show", "--advanced", "--type", "surfaced",
        )
        assert rc == 0

    def test_mark_read_filename_advances_cursor(self, engram_home, inter_agent_dir):
        """ia mark-read <filename> advances cursor to letter's timestamp."""
        _write_config(engram_home, "multi", "ariadne")
        letter_ts = _ts(-30)
        letter_path = _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=letter_ts,
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "mark-read", "2026-05-23T10-00-00Z_borges.md",
        )
        assert rc == 0
        cursor_content = _read_read_cursor(engram_home)
        expected_ts = letter_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert cursor_content == expected_ts

    def test_mark_read_refuses_backward(self, engram_home, inter_agent_dir):
        """mark-read backward without --force → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        # Set cursor forward first
        _write_read_cursor(engram_home, _ts(-10))
        # Create an older letter
        old_letter = _write_letter(
            inter_agent_dir, "2026-05-20T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60 * 24 * 3),  # 3 days ago
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "mark-read", "2026-05-20T10-00-00Z_borges.md",
        )
        assert rc == 1
        assert "backward" in stderr

    def test_mark_read_backward_with_force(self, engram_home, inter_agent_dir):
        """mark-read backward WITH --force → succeeds."""
        _write_config(engram_home, "multi", "ariadne")
        _write_read_cursor(engram_home, _ts(-10))
        old_ts = _ts(-60 * 24 * 3)
        old_letter = _write_letter(
            inter_agent_dir, "2026-05-20T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=old_ts,
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "mark-read", "2026-05-20T10-00-00Z_borges.md", "--force",
        )
        assert rc == 0

    def test_mark_read_all_advances_to_now(self, engram_home, inter_agent_dir):
        """mark-read --all advances cursor to approximately now.

        The cursor is written with second precision (ISO format: HH:MM:SSZ),
        so we compare against a truncated 'before' to avoid sub-second races.
        """
        _write_config(engram_home, "multi", "ariadne")
        # Truncate to seconds: the cursor file only has second precision
        before = datetime.now(timezone.utc).replace(microsecond=0)
        rc, _, _ = _run_ia(engram_home, inter_agent_dir, "mark-read", "--all")
        assert rc == 0
        after = datetime.now(timezone.utc)
        cursor_str = _read_read_cursor(engram_home)
        cursor_dt = datetime.fromisoformat(cursor_str.replace("Z", "+00:00"))
        assert before <= cursor_dt <= after

    def test_mark_read_up_to(self, engram_home, inter_agent_dir):
        """mark-read --up-to <ts> sets cursor to that timestamp."""
        _write_config(engram_home, "multi", "ariadne")
        target = "2026-05-23T12:00:00Z"
        rc, _, _ = _run_ia(
            engram_home, inter_agent_dir, "mark-read", "--up-to", target,
        )
        assert rc == 0
        assert _read_read_cursor(engram_home) == target

    def test_read_advances_cursor(self, engram_home, inter_agent_dir):
        """ia read advances the read cursor to the letter's timestamp."""
        _write_config(engram_home, "multi", "ariadne")
        letter_ts = _ts(-5)
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=letter_ts,
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "read", "2026-05-23T10-00-00Z_borges.md",
        )
        assert rc == 0
        cursor_content = _read_read_cursor(engram_home)
        expected_ts = letter_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert cursor_content == expected_ts

    def test_read_force_backward_advance(self, engram_home, inter_agent_dir):
        """ia read backward cursor: hint on stderr without --force, advance with --force."""
        _write_config(engram_home, "multi", "ariadne")
        # Cursor at T2 (recent)
        cursor_ts = _ts(-10)
        _write_read_cursor(engram_home, cursor_ts)
        cursor_ts_str = cursor_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Old letter at T1 (older than cursor)
        old_ts = _ts(-60 * 24 * 3)  # 3 days ago
        old_ts_str = old_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_letter(
            inter_agent_dir, "2026-05-20T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=old_ts,
        )

        # Without --force: ia read exits 0, cursor stays at T2, stderr hint emitted
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "read", "2026-05-20T10-00-00Z_borges.md",
        )
        assert rc == 0
        assert _read_read_cursor(engram_home) == cursor_ts_str
        assert "cursor not advanced" in stderr or "older than current cursor" in stderr

        # With --force: ia read exits 0, cursor advances backward to T1
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "read", "2026-05-20T10-00-00Z_borges.md", "--force",
        )
        assert rc == 0
        assert _read_read_cursor(engram_home) == old_ts_str


# ===========================================================================
# CATEGORY 4: Filesystem tests — atomic write
# ===========================================================================

class TestFilesystem:
    """Atomic write of letters: no .tmp remains after success."""

    def test_write_no_tmp_remains(self, engram_home, inter_agent_dir):
        """After ia write --from-stdin, no .tmp file remains in inter-agent/."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Body.\n"
        )
        _run_ia(engram_home, inter_agent_dir, "write", "--to", "borges", "--from-stdin",
                stdin_text=letter)
        tmp_files = list(inter_agent_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain after successful write: {tmp_files}"

    def test_write_creates_md_file(self, engram_home, inter_agent_dir):
        """ia write --from-stdin creates a .md file in inter-agent/."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Test body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 0
        md_files = list(inter_agent_dir.glob("*.md"))
        assert len(md_files) == 1, f"Exactly one .md file expected; got {md_files}"

    def test_write_file_mode_is_644(self, engram_home, inter_agent_dir):
        """Written letter file has mode 644 (owner-only write)."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Body.\n"
        )
        _run_ia(engram_home, inter_agent_dir, "write", "--to", "borges", "--from-stdin",
                stdin_text=letter)
        md_files = list(inter_agent_dir.glob("*.md"))
        assert md_files, "Expected at least one .md file"
        mode = oct(md_files[0].stat().st_mode)[-3:]
        assert mode == "644", f"Expected mode 644, got {mode}"

    def test_cursor_write_no_tmp_remains(self, engram_home, inter_agent_dir):
        """After cursor --set, no .tmp file remains in engram_home."""
        _write_config(engram_home, "multi", "ariadne")
        _run_ia(engram_home, inter_agent_dir, "cursor", "--set", "2026-05-22T10:00:00Z")
        tmp_files = list(engram_home.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain after cursor write: {tmp_files}"

    def test_cursor_write_correct_content(self, engram_home, inter_agent_dir):
        """Cursor file written with correct ISO timestamp content."""
        _write_config(engram_home, "multi", "ariadne")
        target = "2026-05-22T10:00:00Z"
        _run_ia(engram_home, inter_agent_dir, "cursor", "--set", target)
        cursor_path = engram_home / "inter-agent-read-cursor.txt"
        assert cursor_path.exists()
        content = cursor_path.read_text().strip()
        assert content == target

    def test_scan_skipped_count_emitted_on_malformed(self, engram_home, inter_agent_dir):
        """ia list emits a stderr note when .md files with unparseable frontmatter exist."""
        _write_config(engram_home, "multi", "ariadne")
        # Write a valid letter (addressed to ariadne)
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="Valid letter",
        )
        # Write a malformed .md file (no frontmatter block at all)
        (inter_agent_dir / "malformed.md").write_text("No frontmatter here.\n")

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list", "--all")
        assert rc == 0
        assert "skipped" in stderr and "unparseable" in stderr


# ===========================================================================
# CATEGORY 5: Mode gate tests
# ===========================================================================

class TestModeGate:
    """Single-agent mode returns a clean error (exit 3) for all subcommands."""

    def test_list_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia list in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 3
        assert "single-agent mode" in stderr
        assert "mode='multi'" in stderr or "mode='single'" in stderr
        assert "inter-agent/README.md" in stderr

    def test_write_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia write in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir, "write", "--to", "borges", "--from-stdin",
            stdin_text="body",
        )
        assert rc == 3

    def test_cursor_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia cursor in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "cursor", "--show")
        assert rc == 3

    def test_mark_read_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia mark-read in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "mark-read", "--all")
        assert rc == 3

    def test_star_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia star in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "star", "some-letter.md")
        assert rc == 3

    def test_unstar_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia unstar in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "unstar", "some-letter.md")
        assert rc == 3

    def test_starred_single_mode_exits_3(self, engram_home, inter_agent_dir):
        """ia starred in single mode → exit 3."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred")
        assert rc == 3

    def test_status_single_mode_exits_0(self, engram_home, inter_agent_dir):
        """ia status works in single mode (diagnostic utility)."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status")
        assert rc == 0
        assert "single" in stdout

    def test_mode_gate_error_has_all_three_lines(self, engram_home, inter_agent_dir):
        """Mode gate error has all three actionable lines from DESIGN §9.4."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 3
        assert "single-agent mode" in stderr
        assert "mode='multi'" in stderr
        assert "inter-agent/README.md" in stderr

    def test_missing_config_treated_as_single(self, engram_home, inter_agent_dir):
        """No config.json → mode defaults to 'single' → exit 3."""
        # Don't write any config
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 3

    def test_mode_gate_error_invalid_config(self, engram_home, inter_agent_dir):
        """Corrupt config.json → exit 3 with 'invalid' + 'failed to parse' in stderr."""
        # Write invalid JSON to config.json
        (engram_home / "config.json").write_text("{not valid json")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 3
        assert "invalid" in stderr
        assert "failed to parse" in stderr


# ===========================================================================
# CATEGORY 6: Integration round-trip
# ===========================================================================

class TestIntegration:
    """write → list → read round-trip with correct cursor advance."""

    def test_write_list_read_round_trip(self, engram_home, inter_agent_dir):
        """Write a letter from borges, then list and read it as ariadne."""
        _write_config(engram_home, "multi", "ariadne")

        # Simulate borges writing a letter to ariadne
        letter_ts = _ts(-5)
        letter_filename = _write_letter(
            inter_agent_dir, "2026-05-23T13-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=letter_ts,
            heading="Round-trip test",
            body="This letter tests the full round-trip.",
        )

        # Step 1: ia list should show the letter
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list")
        assert rc == 0
        assert "borges" in stdout
        assert "Round-trip test" in stdout

        # Verify cursor is not yet advanced (no read yet)
        assert _read_read_cursor(engram_home) == ""

        # Step 2: ia read should display the letter and advance cursor
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "read", "2026-05-23T13-00-00Z_borges.md",
        )
        assert rc == 0
        assert "Round-trip test" in stdout
        assert "This letter tests the full round-trip." in stdout

        # Cursor must now be set to the letter's timestamp
        cursor_content = _read_read_cursor(engram_home)
        expected_ts = letter_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert cursor_content == expected_ts

        # Step 3: ia list --unread should now show 0 letters
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list", "--unread")
        assert rc == 0
        assert "(0)" in stdout or "no letters found" in stdout.lower()

    def test_write_creates_readable_letter(self, engram_home, inter_agent_dir):
        """Letter written via 'ia write --from-stdin' can be read back."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        body = "This is the test letter body content."
        letter = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "# Test heading\n\n"
            f"{body}\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter,
        )
        assert rc == 0, f"write failed; stderr: {stderr}"

        # The written file should exist and be parseable
        md_files = list(inter_agent_dir.glob("*.md"))
        assert md_files, "Expected a .md file to be written"
        content = md_files[0].read_text()
        assert body in content
        assert "from: ariadne" in content
        assert "to: borges" in content

    def test_read_latest_reads_newest_unread(self, engram_home, inter_agent_dir):
        """ia read --latest reads the newest unread letter."""
        _write_config(engram_home, "multi", "ariadne")

        older_ts = _ts(-30)
        newer_ts = _ts(-5)

        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=older_ts, heading="Older letter",
        )
        _write_letter(
            inter_agent_dir, "2026-05-23T11-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=newer_ts, heading="Newer letter",
        )

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "read", "--latest")
        assert rc == 0
        assert "Newer letter" in stdout

    def test_list_format_json(self, engram_home, inter_agent_dir):
        """ia list --format json returns valid JSON with expected fields."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="JSON test",
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir, "list", "--format", "json",
        )
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        letter = data[0]
        assert "timestamp" in letter
        assert "from" in letter
        assert "title" in letter
        assert "path" in letter
        assert "filename" in letter

    def test_status_json_format(self, engram_home, inter_agent_dir):
        """ia status --format json returns valid JSON with expected fields."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status", "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert data["agent"] == "ariadne"
        assert data["mode"] == "multi"
        assert "unread_letters" in data
        assert "read_cursor" in data

    def test_list_from_filter(self, engram_home, inter_agent_dir):
        """ia list --from <agent> filters to only that sender."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="From Borges",
        )
        _write_letter(
            inter_agent_dir, "2026-05-23T11-00-00Z_mneme.md",
            from_agent="mneme", to_agent="ariadne",
            timestamp=_ts(-20), heading="From Mneme",
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir, "list", "--from", "borges",
        )
        assert rc == 0
        assert "From Borges" in stdout
        assert "From Mneme" not in stdout

    def test_read_latest_no_unread_message_on_stdout(self, engram_home, inter_agent_dir):
        """Nit 1: 'no unread' message lands on stdout (informational), not stderr."""
        _write_config(engram_home, "multi", "ariadne")

        # Write a letter, then advance the cursor past it so there are no unread
        letter_ts = _ts(-30)
        _write_letter(
            inter_agent_dir, "2026-05-23T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=letter_ts, heading="Already read",
        )
        # Cursor set to a time after the letter — no unread remain
        _write_read_cursor(engram_home, _ts(-5))

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "read", "--latest")
        assert rc == 0
        assert "no unread" in stdout
        assert "no unread" not in stderr


# ===========================================================================
# CATEGORY 7: REPLIED marker tests
# ===========================================================================

class TestRepliedMarker:
    """REPLIED marker: _scan_replied_filenames, cmd_list display, --reply convenience,
    cmd_status unreplied count."""

    # -----------------------------------------------------------------------
    # _scan_replied_filenames unit tests
    # -----------------------------------------------------------------------

    def test_scan_replied_empty_when_no_outgoing(self, engram_home, inter_agent_dir):
        """_scan_replied_filenames returns empty set when no outgoing letters exist."""
        from tools.ia import _scan_replied_filenames
        # Only incoming letters (to ariadne, from borges) — none outgoing from ariadne
        _write_letter(
            inter_agent_dir, "2026-05-28T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30),
        )
        result = _scan_replied_filenames("ariadne", inter_agent_dir)
        assert result == set()

    def test_scan_replied_collects_re_values(self, engram_home, inter_agent_dir):
        """_scan_replied_filenames collects re: values from multiple outgoing letters."""
        from tools.ia import _scan_replied_filenames
        incoming1 = "2026-05-28T09-00-00Z_borges.md"
        incoming2 = "2026-05-28T10-00-00Z_borges.md"
        # Outgoing letter referencing incoming1
        _write_letter(
            inter_agent_dir, "2026-05-28T11-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-10), re_field=incoming1,
        )
        # Outgoing letter referencing incoming2
        _write_letter(
            inter_agent_dir, "2026-05-28T12-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-5), re_field=incoming2,
        )
        result = _scan_replied_filenames("ariadne", inter_agent_dir)
        assert result == {incoming1, incoming2}

    def test_scan_replied_skips_malformed_letters(self, engram_home, inter_agent_dir):
        """_scan_replied_filenames skips malformed letters gracefully."""
        from tools.ia import _scan_replied_filenames
        # Write a malformed .md file alongside a valid outgoing letter
        (inter_agent_dir / "malformed-outgoing.md").write_text("no frontmatter here\n")
        incoming1 = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, "2026-05-28T11-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-10), re_field=incoming1,
        )
        # Should not raise; should still collect re: from the valid outgoing letter
        result = _scan_replied_filenames("ariadne", inter_agent_dir)
        assert result == {incoming1}

    def test_scan_replied_ignores_outgoing_without_re(self, engram_home, inter_agent_dir):
        """_scan_replied_filenames ignores outgoing letters with no re: field."""
        from tools.ia import _scan_replied_filenames
        # Outgoing letter without re: (cold start letter, not a reply)
        _write_letter(
            inter_agent_dir, "2026-05-28T11-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-10),
        )
        result = _scan_replied_filenames("ariadne", inter_agent_dir)
        assert result == set()

    # -----------------------------------------------------------------------
    # cmd_list REPLIED display tests
    # -----------------------------------------------------------------------

    def test_list_shows_replied_marker(self, engram_home, inter_agent_dir):
        """cmd_list shows [REPLIED] on a letter when an outgoing re: points at it."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Original question",
        )
        # Outgoing reply that references the incoming letter
        _write_letter(
            inter_agent_dir, "2026-05-28T10-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-30), re_field=incoming_filename,
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list", "--all")
        assert rc == 0
        assert "[REPLIED]" in stdout
        assert "Original question" in stdout

    def test_list_no_replied_marker_without_re(self, engram_home, inter_agent_dir):
        """cmd_list does NOT show [REPLIED] when no outgoing letter references the incoming."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Unreplied letter",
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list", "--all")
        assert rc == 0
        assert "[REPLIED]" not in stdout
        assert "Unreplied letter" in stdout

    def test_list_replied_marker_only_on_correct_letter(self, engram_home, inter_agent_dir):
        """[REPLIED] appears only on the specific letter referenced, not others."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_replied = "2026-05-28T08-00-00Z_borges.md"
        incoming_unreplied = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_replied,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-90), heading="Replied letter",
        )
        _write_letter(
            inter_agent_dir, incoming_unreplied,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Unreplied letter",
        )
        # Outgoing letter replies to incoming_replied only
        _write_letter(
            inter_agent_dir, "2026-05-28T10-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-30), re_field=incoming_replied,
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "list", "--all")
        assert rc == 0
        # Exactly one [REPLIED] marker in output
        assert stdout.count("[REPLIED]") == 1
        # "Replied letter" line has the marker
        for line in stdout.splitlines():
            if "Replied letter" in line:
                assert "[REPLIED]" in line
            if "Unreplied letter" in line:
                assert "[REPLIED]" not in line

    # -----------------------------------------------------------------------
    # ia write --reply FILENAME tests
    # -----------------------------------------------------------------------

    def test_write_reply_autofills_to_and_re(self, engram_home, inter_agent_dir):
        """ia write --reply FILENAME auto-fills to: from source's from: and sets re:."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="Please reply to this",
        )
        # Write a reply via --reply (body provided via stdin)
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            f"re: {incoming_filename}\n"
            "---\n\n"
            "# Reply\n\n"
            "Here is my reply.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--reply", incoming_filename, "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 0, f"write --reply failed; stderr: {stderr}"
        # Find the written letter (the outgoing one, from ariadne)
        md_files = [f for f in inter_agent_dir.glob("*.md") if f.name != incoming_filename]
        assert md_files, "Expected an outgoing letter to be written"
        content = md_files[0].read_text()
        assert "to: borges" in content
        assert f"re: {incoming_filename}" in content

    def test_write_reply_to_conflict_rejected(self, engram_home, inter_agent_dir):
        """ia write --reply FILENAME --to OTHER (OTHER != source's from) is rejected."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="From borges",
        )
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: mneme\n"
            f"timestamp: {ts_str}\n"
            f"re: {incoming_filename}\n"
            "---\n\n"
            "Body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--reply", incoming_filename, "--to", "mneme", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 1, "Expected rejection when --to conflicts with source's from:"
        assert "conflict" in stderr.lower() and "borges" in stderr and "mneme" in stderr

    def test_write_reply_to_matching_accepted(self, engram_home, inter_agent_dir):
        """ia write --reply FILENAME --to borges (matches source's from:) is accepted."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="From borges",
        )
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            f"re: {incoming_filename}\n"
            "---\n\n"
            "Body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--reply", incoming_filename, "--to", "borges", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 0, f"Expected acceptance when --to matches source's from:; stderr: {stderr}"

    def test_write_reply_nonexistent_source_rejected(self, engram_home, inter_agent_dir):
        """ia write --reply NONEXISTENT → exit 1 with clear error."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\nBody.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--reply", "nonexistent-letter.md", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 1
        assert "not found" in stderr.lower() or "nonexistent" in stderr.lower()

    def test_write_reply_results_in_re_and_to_in_file(self, engram_home, inter_agent_dir):
        """Written reply letter has correct to: and re: fields in the file."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-30), heading="Source letter",
        )
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            f"re: {incoming_filename}\n"
            "---\n\n"
            "# Reply\n\nReply body here.\n"
        )
        rc, _, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--reply", incoming_filename, "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 0, f"stderr: {stderr}"
        # Check the written file
        written = [f for f in inter_agent_dir.glob("*.md") if f.name != incoming_filename]
        assert written, "Expected a written reply file"
        content = written[0].read_text()
        from tools.ia import _parse_frontmatter
        fields, _ = _parse_frontmatter(content)
        assert fields.get("to") == "borges"
        assert fields.get("re") == incoming_filename

    # -----------------------------------------------------------------------
    # cmd_status unreplied count tests
    # -----------------------------------------------------------------------

    def test_status_shows_unreplied_when_present(self, engram_home, inter_agent_dir):
        """cmd_status shows unreplied count when there are unresponded letters."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-28T09-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Needs a reply",
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status")
        assert rc == 0
        assert "unreplied" in stdout
        assert "1" in stdout

    def test_status_omits_unreplied_when_zero(self, engram_home, inter_agent_dir):
        """cmd_status omits unreplied line when all incoming letters have been replied to."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Already replied",
        )
        # Outgoing reply
        _write_letter(
            inter_agent_dir, "2026-05-28T10-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-30), re_field=incoming_filename,
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status")
        assert rc == 0
        # Check that no "unreplied:" label line is emitted (the label is only shown > 0).
        # Note: the inter-agent-dir path in the output may contain "unreplied" if the
        # pytest tmp dir name includes it — check for the specific label pattern instead.
        output_lines = stdout.splitlines()
        unreplied_label_lines = [ln for ln in output_lines if ln.strip().startswith("unreplied:")]
        assert unreplied_label_lines == [], f"Expected no unreplied: line; got: {unreplied_label_lines}"

    def test_status_json_includes_unreplied_field(self, engram_home, inter_agent_dir):
        """cmd_status --format json includes unreplied_letters field."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-28T09-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60),
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status", "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert "unreplied_letters" in data
        assert data["unreplied_letters"] == 1

    def test_status_json_unreplied_zero_when_all_replied(self, engram_home, inter_agent_dir):
        """cmd_status --format json shows unreplied_letters=0 when all replied."""
        _write_config(engram_home, "multi", "ariadne")
        incoming_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, incoming_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60),
        )
        _write_letter(
            inter_agent_dir, "2026-05-28T10-00-00Z_ariadne.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=_ts(-30), re_field=incoming_filename,
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "status", "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert data["unreplied_letters"] == 0


# ===========================================================================
# CATEGORY 8: --body-file tests
# ===========================================================================

class TestBodyFile:
    """ia write --body-file: auto-assembled frontmatter, verbatim body, error cases."""

    def test_body_file_exits_0_and_sends(self, engram_home, inter_agent_dir, tmp_path):
        """ia write --body-file exits 0 and creates a letter."""
        _write_config(engram_home, "multi", "ariadne")
        body_file = tmp_path / "body.md"
        body_file.write_text("Hello Borges, this is the body.\n", encoding="utf-8")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--body-file", str(body_file),
        )
        assert rc == 0, f"write --body-file failed; stderr: {stderr}"
        assert "sent" in stdout

    def test_body_file_frontmatter_auto_filled(self, engram_home, inter_agent_dir, tmp_path):
        """Written letter has correct auto-filled from, to, and valid timestamp."""
        _write_config(engram_home, "multi", "ariadne")
        body_file = tmp_path / "body.md"
        body_file.write_text("Body content here.\n", encoding="utf-8")
        before = datetime.now(timezone.utc).replace(microsecond=0)
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--body-file", str(body_file),
        )
        after = datetime.now(timezone.utc)
        assert rc == 0, f"stderr: {stderr}"
        md_files = list(inter_agent_dir.glob("*.md"))
        assert md_files, "Expected a written .md file"
        from tools.ia import _parse_frontmatter
        content = md_files[0].read_text()
        fields, _ = _parse_frontmatter(content)
        assert fields.get("from") == "ariadne", f"from: should be agent name; got {fields.get('from')!r}"
        assert fields.get("to") == "borges", f"to: should be borges; got {fields.get('to')!r}"
        ts_str = fields.get("timestamp", "")
        assert ts_str, "timestamp: should be present"
        ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        assert before <= ts_dt <= after, f"timestamp {ts_str!r} not in expected range"

    def test_body_file_re_and_title_honored(self, engram_home, inter_agent_dir, tmp_path):
        """--re and --title flags are honored when using --body-file."""
        _write_config(engram_home, "multi", "ariadne")
        # Create the referenced letter so re: validation passes
        ref_filename = "2026-05-28T09-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, ref_filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60),
        )
        body_file = tmp_path / "body.md"
        body_file.write_text("Reply body content.\n", encoding="utf-8")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges",
            "--re", ref_filename,
            "--title", "My Reply",
            "--body-file", str(body_file),
        )
        assert rc == 0, f"stderr: {stderr}"
        md_files = [f for f in inter_agent_dir.glob("*.md") if f.name != ref_filename]
        assert md_files, "Expected an outgoing letter"
        from tools.ia import _parse_frontmatter
        content = md_files[0].read_text()
        fields, body_text = _parse_frontmatter(content)
        assert fields.get("re") == ref_filename, f"re: field should be set; got {fields.get('re')!r}"
        assert "# My Reply" in content, "Title heading should appear in letter"

    def test_body_file_backtick_roundtrip(self, engram_home, inter_agent_dir, tmp_path):
        """Body with backticks/$vars/angle-brackets/YAML-special chars survives verbatim."""
        _write_config(engram_home, "multi", "ariadne")
        # This is the load-bearing test: all the problem chars that shell heredocs mangle
        tricky_body = (
            "Here is a shell command: `gh project copy <template>`\n"
            "A variable: $TS\n"
            "Angle brackets: <angle-brackets>\n"
            "YAML-special: key: value: with colons\n"
            "Another YAML-special: [list, of, items]\n"
            "Backtick in context: run `echo $HOME` to see your home dir.\n"
        )
        body_file = tmp_path / "tricky_body.md"
        body_file.write_text(tricky_body, encoding="utf-8")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--body-file", str(body_file),
        )
        assert rc == 0, f"write with tricky body failed; stderr: {stderr}"
        md_files = list(inter_agent_dir.glob("*.md"))
        assert md_files, "Expected a written .md file"
        content = md_files[0].read_text()
        # Every tricky substring must appear verbatim in the written letter
        assert "`gh project copy <template>`" in content, \
            "Backtick command with angle-brackets did not survive verbatim"
        assert "$TS" in content, "$TS variable did not survive verbatim"
        assert "<angle-brackets>" in content, "Angle brackets did not survive verbatim"
        assert "key: value: with colons" in content, "YAML colon content did not survive verbatim"
        assert "[list, of, items]" in content, "YAML list syntax did not survive verbatim"
        assert "`echo $HOME`" in content, "Nested backtick+var did not survive verbatim"

    def test_from_stdin_back_compat(self, engram_home, inter_agent_dir):
        """--from-stdin still works (back-compat retained)."""
        _write_config(engram_home, "multi", "ariadne")
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        letter_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            "---\n\n"
            "Back-compat stdin body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=letter_content,
        )
        assert rc == 0, f"--from-stdin back-compat failed; stderr: {stderr}"
        assert "sent" in stdout

    def test_body_file_and_from_stdin_mutually_exclusive(self, engram_home, inter_agent_dir, tmp_path):
        """--body-file and --from-stdin together → clear error, non-zero exit."""
        _write_config(engram_home, "multi", "ariadne")
        body_file = tmp_path / "body.md"
        body_file.write_text("Some body.\n", encoding="utf-8")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges",
            "--body-file", str(body_file),
            "--from-stdin",
            stdin_text="stdin content",
        )
        assert rc != 0, "Expected non-zero exit when both --body-file and --from-stdin are passed"
        assert "mutually exclusive" in stderr.lower() or "body-file" in stderr

    def test_body_file_nonexistent_path(self, engram_home, inter_agent_dir):
        """--body-file pointing at a nonexistent path → clear error, non-zero exit."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges",
            "--body-file", "/tmp/definitely-does-not-exist-ia-body-file-test.md",
        )
        assert rc != 0, "Expected non-zero exit for missing body-file"
        assert "not found" in stderr or "body-file" in stderr

    def test_body_file_unreadable_permission_denied(self, engram_home, inter_agent_dir, tmp_path):
        """--body-file pointing at an unreadable file (permission denied) → clear error, non-zero exit."""
        if os.geteuid() == 0:
            pytest.skip("Running as root — file permission checks are bypassed")
        _write_config(engram_home, "multi", "ariadne")
        body_file = tmp_path / "unreadable_body.md"
        body_file.write_text("Some body content.\n", encoding="utf-8")
        os.chmod(body_file, 0o000)
        try:
            rc, stdout, stderr = _run_ia(
                engram_home, inter_agent_dir,
                "write", "--to", "borges",
                "--body-file", str(body_file),
            )
            assert rc != 0, "Expected non-zero exit for unreadable body-file"
            assert "cannot read" in stderr or "body-file" in stderr
        finally:
            os.chmod(body_file, 0o644)


# ===========================================================================
# CATEGORY 9: Star/unstar/starred tests
# ===========================================================================

class TestStar:
    """ia star / ia unstar / ia starred commands and the starred-block surface helper."""

    # -----------------------------------------------------------------------
    # ia star tests
    # -----------------------------------------------------------------------

    def test_star_new_letter(self, engram_home, inter_agent_dir):
        """ia star <filename> adds a new entry to the starred list."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-30T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Key agreement",
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "star", "2026-05-30T10-00-00Z_borges.md",
        )
        assert rc == 0, f"star failed; stderr: {stderr}"
        assert "starred" in stdout

        # Verify starred JSON file was created with correct entry
        starred_path = engram_home / "inter-agent-starred.json"
        assert starred_path.exists(), "starred JSON file should be created"
        entries = json.loads(starred_path.read_text())
        assert isinstance(entries, list)
        assert len(entries) == 1
        assert entries[0]["filename"] == "2026-05-30T10-00-00Z_borges.md"
        assert "starred_at" in entries[0]

    def test_star_with_note(self, engram_home, inter_agent_dir):
        """ia star <filename> --note <text> stores the note in the entry."""
        _write_config(engram_home, "multi", "ariadne")
        _write_letter(
            inter_agent_dir, "2026-05-30T10-00-00Z_borges.md",
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Key agreement",
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "star", "2026-05-30T10-00-00Z_borges.md",
            "--note", "load-bearing work split agreed here",
        )
        assert rc == 0, f"star --note failed; stderr: {stderr}"
        starred_path = engram_home / "inter-agent-starred.json"
        entries = json.loads(starred_path.read_text())
        assert entries[0]["note"] == "load-bearing work split agreed here"

    def test_star_snapshots_from_and_title(self, engram_home, inter_agent_dir):
        """ia star snapshots from/title at star-time into the stored entry."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Cross-session pact",
        )
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "star", filename)
        assert rc == 0, f"star failed; stderr: {stderr}"

        starred_path = engram_home / "inter-agent-starred.json"
        entries = json.loads(starred_path.read_text())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["from"] == "borges", f"expected from=borges; got {entry.get('from')!r}"
        assert entry["title"] == "Cross-session pact", f"expected title; got {entry.get('title')!r}"

    def test_starred_renders_without_rereading_letter(self, engram_home, inter_agent_dir):
        """ia starred shows from/title from snapshot without re-opening the source letter."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Agreement details",
        )
        # Star it to create snapshot
        _run_ia(engram_home, inter_agent_dir, "star", filename)

        # Delete the source letter to prove ia starred reads from snapshot only
        (inter_agent_dir / filename).unlink()

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred")
        assert rc == 0, f"starred should not fail after letter deleted; stderr: {stderr}"
        assert "borges" in stdout, "from-agent from snapshot should appear"
        assert "Agreement details" in stdout, "title from snapshot should appear"

    def test_star_idempotent_no_duplicate(self, engram_home, inter_agent_dir):
        """Starring an already-starred letter does not add a duplicate entry."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Key agreement",
        )
        # Star it twice
        _run_ia(engram_home, inter_agent_dir, "star", filename)
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "star", filename)
        assert rc == 0, f"second star call failed; stderr: {stderr}"
        starred_path = engram_home / "inter-agent-starred.json"
        entries = json.loads(starred_path.read_text())
        # Only one entry, not two
        matching = [e for e in entries if e.get("filename") == filename]
        assert len(matching) == 1, f"Expected 1 entry, got {len(matching)}"

    def test_star_idempotent_updates_note(self, engram_home, inter_agent_dir):
        """Re-starring with a new --note updates the note without duplicating."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Key agreement",
        )
        _run_ia(engram_home, inter_agent_dir, "star", filename, "--note", "first note")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir, "star", filename, "--note", "updated note",
        )
        assert rc == 0
        entries = json.loads((engram_home / "inter-agent-starred.json").read_text())
        matching = [e for e in entries if e.get("filename") == filename]
        assert len(matching) == 1
        assert matching[0]["note"] == "updated note"

    def test_star_nonexistent_letter_rejected(self, engram_home, inter_agent_dir):
        """ia star for a filename that doesn't exist in inter-agent/ → exit 1."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "star", "nonexistent-letter.md",
        )
        assert rc == 1, "Expected validation failure for nonexistent letter"
        assert "not found" in stderr.lower() or "nonexistent" in stderr.lower()

    # -----------------------------------------------------------------------
    # ia unstar tests
    # -----------------------------------------------------------------------

    def test_unstar_removes_entry(self, engram_home, inter_agent_dir):
        """ia unstar <filename> removes the entry from the starred list."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Key agreement",
        )
        _run_ia(engram_home, inter_agent_dir, "star", filename)
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "unstar", filename)
        assert rc == 0, f"unstar failed; stderr: {stderr}"
        entries = json.loads((engram_home / "inter-agent-starred.json").read_text())
        assert all(e.get("filename") != filename for e in entries)

    def test_unstar_absent_is_noop(self, engram_home, inter_agent_dir):
        """ia unstar for a filename not in the starred list → exit 0, no-op."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "unstar", "never-starred.md",
        )
        assert rc == 0, f"unstar no-op should exit 0; stderr: {stderr}"
        assert "no-op" in stdout.lower() or "not starred" in stdout.lower()

    # -----------------------------------------------------------------------
    # ia starred list tests
    # -----------------------------------------------------------------------

    def test_starred_empty_list(self, engram_home, inter_agent_dir):
        """ia starred with no starred letters shows 0 count."""
        _write_config(engram_home, "multi", "ariadne")
        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred")
        assert rc == 0
        assert "0" in stdout or "no starred" in stdout.lower()

    def test_starred_shows_from_title_filename(self, engram_home, inter_agent_dir):
        """ia starred shows from · title · filename for each starred letter."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Cross-session agreement",
        )
        _run_ia(engram_home, inter_agent_dir, "star", filename, "--note", "work split")

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred")
        assert rc == 0
        assert "borges" in stdout
        assert "Cross-session agreement" in stdout
        assert filename in stdout

    def test_starred_missing_letter_skips_with_warning(self, engram_home, inter_agent_dir):
        """ia starred warns on stderr when a starred letter's file no longer exists."""
        _write_config(engram_home, "multi", "ariadne")
        # Manually inject a starred entry for a non-existent file
        missing_filename = "2026-05-01T10-00-00Z_borges.md"
        entries = [{"filename": missing_filename, "starred_at": "2026-05-01T10:00:00Z", "note": ""}]
        (engram_home / "inter-agent-starred.json").write_text(json.dumps(entries))

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred")
        assert rc == 0, "starred should not fail when a letter is missing"
        # Warning about missing letter on stderr
        assert "missing" in stderr.lower() or "no longer" in stderr.lower() or "not exist" in stderr.lower()

    def test_starred_json_format(self, engram_home, inter_agent_dir):
        """ia starred --format json returns valid JSON list."""
        _write_config(engram_home, "multi", "ariadne")
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(
            inter_agent_dir, filename,
            from_agent="borges", to_agent="ariadne",
            timestamp=_ts(-60), heading="Agreement",
        )
        _run_ia(engram_home, inter_agent_dir, "star", filename, "--note", "important")

        rc, stdout, stderr = _run_ia(engram_home, inter_agent_dir, "starred", "--format", "json")
        assert rc == 0
        data = json.loads(stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["filename"] == filename
        assert data[0]["note"] == "important"

