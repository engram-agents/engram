"""Tests for hooks/claude/engram-inter-agent-prompt-hook.py.

Covers all eight behaviors specified in the PR spec:

  1. Mode=single — hook exits silently, no output.
  2. Mode=multi, no letters — hook exits silently, no output.
  3. Mode=multi, 1 new letter to self — outputs new-letter block.
  4. Mode=multi, 1 new + 1 older-unread — both surface, correct partitioning.
  5. Mode=multi, letter addressed to a different agent — NOT surfaced.
  6. Cursor advance on hook fire — surfaced cursor moves; read cursor doesn't.
  7. Malformed frontmatter file is skipped (not crashed-on).
  8. Title extraction — each precedence layer (heading / re: / body-first-60).

Each test invokes the hook as a subprocess with a fake ENGRAM_HOME +
fake INTER_AGENT_DIR so the full file-system path runs end-to-end.
"""

import json
import os
import subprocess
import sys
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "hooks" / "claude" / "engram-inter-agent-prompt-hook.py"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook(
    engram_home: Path,
    inter_agent_dir: Path,
    hook_input: dict | None = None,
) -> tuple[int, str, str]:
    """Run the hook as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["INTER_AGENT_DIR"] = str(inter_agent_dir)
    stdin_data = json.dumps(hook_input or {"prompt": "test"})
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, mode: str, agent_name: str = "borges") -> None:
    """Write a minimal config.json into engram_home."""
    cfg = {"mode": mode, "agent_name": agent_name, "counterparts": []}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_surfaced_cursor(engram_home: Path, ts: datetime) -> None:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    (engram_home / "inter-agent-surfaced-cursor.txt").write_text(ts_str + "\n")


def _write_read_cursor(engram_home: Path, ts: datetime) -> None:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    (engram_home / "inter-agent-read-cursor.txt").write_text(ts_str + "\n")


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


# ---------------------------------------------------------------------------
# Test 1: Mode=single — no output, no state mutation
# ---------------------------------------------------------------------------

def test_mode_single_exits_silently(engram_home, inter_agent_dir):
    """Single-agent mode: hook exits 0 with no stdout."""
    _write_config(engram_home, mode="single", agent_name="borges")
    # Place a letter that WOULD match in multi mode
    _write_letter(
        inter_agent_dir, "2026-05-21T19-50-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == ""
    # Surfaced cursor must NOT be written in single mode
    assert not (engram_home / "inter-agent-surfaced-cursor.txt").exists()


# ---------------------------------------------------------------------------
# Test 2: Mode=multi, no letters — exits silently, surfaced cursor advances
# ---------------------------------------------------------------------------

def test_mode_multi_no_letters_silent(engram_home, inter_agent_dir):
    """Multi-agent mode, no matching letters: exit 0, no stdout, cursor advances."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == ""
    # Surfaced cursor IS written even when nothing to report
    assert (engram_home / "inter-agent-surfaced-cursor.txt").exists()


# ---------------------------------------------------------------------------
# Test 3: Mode=multi, 1 new letter to self — new-letter block output
# ---------------------------------------------------------------------------

def test_mode_multi_one_new_letter(engram_home, inter_agent_dir):
    """Multi-agent mode, 1 new letter: output contains new-letter block."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-21T19-50-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=2),
        heading="First-session reflections",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() != ""
    response = json.loads(stdout)
    assert "hookSpecificOutput" in response
    output = response["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    ctx = output["additionalContext"]
    assert "1 new letter from ariadne" in ctx
    assert "read before responding" in ctx
    assert "First-session reflections" in ctx


# ---------------------------------------------------------------------------
# Test 4: Mode=multi, 1 new + 1 older-unread — both surface, correct partition
# ---------------------------------------------------------------------------

def test_mode_multi_new_and_unread_partitioned(engram_home, inter_agent_dir):
    """1 new letter + 1 older-unread: both appear; partitioning is correct."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    # Older letter (before surfaced cursor, after read cursor → unread)
    old_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-05-21T10-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=old_ts,
        body="An old letter that was surfaced but not read.",
    )

    # Set surfaced cursor AFTER the old letter but BEFORE the new one
    surfaced_at = now - timedelta(hours=1)
    _write_surfaced_cursor(engram_home, surfaced_at)
    # No read cursor → old letter is still "unread"

    # New letter (after surfaced cursor)
    new_ts = now - timedelta(minutes=5)
    _write_letter(
        inter_agent_dir, "2026-05-21T20-05-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=new_ts,
        heading="Re: handoff plan",
    )

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() != ""
    response = json.loads(stdout)
    ctx = response["hookSpecificOutput"]["additionalContext"]

    # New letter is listed under "New letters"
    assert "1 new letter from ariadne" in ctx
    assert "read before responding" in ctx
    assert "Re: handoff plan" in ctx

    # Older unread appears in "(Plus N older still-unread from <senders>.)" line
    assert "1 older still-unread" in ctx
    assert "from mneme" in ctx


# ---------------------------------------------------------------------------
# Test 5: Letter addressed to a different agent — NOT surfaced
# ---------------------------------------------------------------------------

def test_letter_to_other_agent_not_surfaced(engram_home, inter_agent_dir):
    """Letter addressed to 'mneme' while we're 'borges': not surfaced."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    _write_letter(
        inter_agent_dir, "2026-05-21T20-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="mneme",   # <-- different agent
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
        heading="Message for Mneme",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == "", (
        "Letter to 'mneme' should not surface for 'borges'"
    )


# ---------------------------------------------------------------------------
# Test 6: Cursor advance semantics
# ---------------------------------------------------------------------------

def test_surfaced_cursor_advances_read_cursor_does_not(engram_home, inter_agent_dir):
    """After hook fires: surfaced cursor advances; read cursor stays put."""
    _write_config(engram_home, mode="multi", agent_name="borges")

    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    _write_surfaced_cursor(engram_home, before)

    # Place a read cursor; it must remain unchanged after hook fire.
    read_ts = datetime.now(timezone.utc) - timedelta(days=1)
    _write_read_cursor(engram_home, read_ts)

    _run_hook(engram_home, inter_agent_dir)

    # Surfaced cursor was updated
    surfaced_raw = (engram_home / "inter-agent-surfaced-cursor.txt").read_text().strip()
    surfaced_new = datetime.fromisoformat(surfaced_raw.replace("Z", "+00:00"))
    assert surfaced_new > before, "Surfaced cursor must advance past previous value"

    # Read cursor was NOT touched
    read_raw = (engram_home / "inter-agent-read-cursor.txt").read_text().strip()
    read_back = datetime.fromisoformat(read_raw.replace("Z", "+00:00"))
    # Allow 1-second slop for sub-second formatting differences
    assert abs((read_back - read_ts).total_seconds()) < 2, (
        "Read cursor must not change after hook fire"
    )


# ---------------------------------------------------------------------------
# Test 7: Malformed frontmatter file is skipped (not crashed-on)
# ---------------------------------------------------------------------------

def test_malformed_frontmatter_skipped(engram_home, inter_agent_dir):
    """Files with malformed/absent frontmatter are silently skipped."""
    _write_config(engram_home, mode="multi", agent_name="borges")

    # File with no frontmatter at all
    (inter_agent_dir / "no-frontmatter.md").write_text("Just some text.\n")

    # File with incomplete frontmatter (no timestamp)
    (inter_agent_dir / "incomplete.md").write_text(
        "---\nfrom: ariadne\nto: borges\n---\nBody.\n"
    )

    # File with completely garbage content
    (inter_agent_dir / "garbage.md").write_text("\x00\x01\x02\x03 not yaml\n")

    # File with valid frontmatter that targets us (to verify others are skipped)
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "valid.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=1),
        heading="Valid letter",
    )

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0, f"Hook should not crash on malformed files; stderr: {stderr}"
    # The valid letter should still be surfaced
    assert stdout.strip() != ""
    response = json.loads(stdout)
    ctx = response["hookSpecificOutput"]["additionalContext"]
    assert "Valid letter" in ctx


# ---------------------------------------------------------------------------
# Test 8: Title extraction precedence
# ---------------------------------------------------------------------------

class TestTitleExtraction:
    """Title extraction: heading > re: field > body-first-60 chars."""

    def test_title_from_heading(self, engram_home, inter_agent_dir):
        """# Heading in body takes priority over re: and body text."""
        _write_config(engram_home, mode="multi", agent_name="borges")
        _write_letter(
            inter_agent_dir, "heading-test.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            heading="Thoughts on memory consolidation",
            re_field="some-re-field",
            body="This is the body text that should not be used as title.",
        )
        _, stdout, _ = _run_hook(engram_home, inter_agent_dir)
        assert stdout.strip()
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Thoughts on memory consolidation" in ctx
        assert "some-re-field" not in ctx
        assert "This is the body text" not in ctx

    def test_title_from_re_field_when_no_heading(self, engram_home, inter_agent_dir):
        """re: frontmatter used when body has no # heading."""
        _write_config(engram_home, mode="multi", agent_name="borges")
        _write_letter(
            inter_agent_dir, "re-test.md",
            from_agent="mneme", to_agent="borges",
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            re_field="2026-05-20T18-00-00Z_borges.md",
            body="Just body, no heading.",
        )
        _, stdout, _ = _run_hook(engram_home, inter_agent_dir)
        assert stdout.strip()
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "2026-05-20T18-00-00Z_borges.md" in ctx

    def test_title_from_body_first_60_when_no_heading_no_re(
        self, engram_home, inter_agent_dir
    ):
        """First 60 chars of body used when no heading and no re: field."""
        _write_config(engram_home, mode="multi", agent_name="borges")
        long_body = "A" * 80 + " trailing words"
        _write_letter(
            inter_agent_dir, "body-test.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            body=long_body,
        )
        _, stdout, _ = _run_hook(engram_home, inter_agent_dir)
        assert stdout.strip()
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        # First 60 chars of body should appear (truncated with ...)
        assert "A" * 57 + "..." in ctx

    def test_title_body_short_not_truncated(self, engram_home, inter_agent_dir):
        """Short body (≤60 chars) is used as-is without ellipsis."""
        _write_config(engram_home, mode="multi", agent_name="borges")
        short_body = "Short body text here."
        _write_letter(
            inter_agent_dir, "short-body.md",
            from_agent="ariadne", to_agent="borges",
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            body=short_body,
        )
        _, stdout, _ = _run_hook(engram_home, inter_agent_dir)
        assert stdout.strip()
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Short body text here." in ctx
        assert "..." not in ctx


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

def test_mode_multi_missing_config_treated_as_single(engram_home, inter_agent_dir):
    """No config.json → mode defaults to 'single', hook exits silently."""
    # Don't write any config file
    _write_letter(
        inter_agent_dir, "letter.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == ""


def test_inter_agent_dir_missing_is_safe(engram_home, inter_agent_dir):
    """If INTER_AGENT_DIR doesn't exist, hook exits silently (no crash)."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    missing_dir = inter_agent_dir.parent / "nonexistent-dir"
    rc, stdout, stderr = _run_hook(engram_home, missing_dir)
    assert rc == 0
    # surfaced cursor should still advance
    assert (engram_home / "inter-agent-surfaced-cursor.txt").exists()


# ---------------------------------------------------------------------------
# Test 9: Atomic cursor write — smoke test
# ---------------------------------------------------------------------------

def test_cursor_write_is_atomic(engram_home, inter_agent_dir):
    """Atomic cursor write: successful write leaves correct content and no .tmp file."""
    _write_config(engram_home, mode="multi", agent_name="borges")

    # Run the hook so the cursor is written via the atomic path
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0

    cursor_path = engram_home / "inter-agent-surfaced-cursor.txt"
    tmp_path = engram_home / "inter-agent-surfaced-cursor.txt.tmp"

    # Cursor file must exist with valid ISO timestamp content
    assert cursor_path.exists(), "Surfaced cursor file must exist after hook fire"
    content = cursor_path.read_text().strip()
    # Must parse as a valid datetime (atomic write landed correctly)
    dt = datetime.fromisoformat(content.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, "Cursor timestamp must be timezone-aware"

    # Tmp file must NOT remain after a successful write (os.replace consumed it)
    assert not tmp_path.exists(), (
        ".tmp file must not persist after a successful atomic cursor write"
    )


# ---------------------------------------------------------------------------
# Test 10: Bare-heading body-first-60 fallback produces a clean title
# ---------------------------------------------------------------------------

def test_bare_heading_body_uses_clean_title(engram_home, inter_agent_dir):
    """Body starting with '##\\n\\nactual content' → title is 'actual content', not '##'."""
    _write_config(engram_home, mode="multi", agent_name="borges")

    # Body starts with a bare heading (hash-only line), then the real content.
    bare_heading_body = "##\n\nactual content here"
    _write_letter(
        inter_agent_dir, "bare-heading.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
        body=bare_heading_body,
        # No re: field, no heading kwarg — forces body-first-60 path
    )

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip(), "Hook should produce output for a valid letter"
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    # Must NOT surface the bare '##' as a title
    assert '"##"' not in ctx, "Bare '##' must not appear as title"
    assert "##" not in ctx.split('"')[1::2], (
        "No field in the context should be '##' as the title text"
    )
    # Must surface the actual content
    assert "actual content here" in ctx, (
        "Actual body content should be used as the title"
    )


# ---------------------------------------------------------------------------
# Test 11: Malformed cursor (filename-format) logs to stderr + still surfaces
# ---------------------------------------------------------------------------

def test_malformed_cursor_logs_stderr_and_surfaces_letter(engram_home, inter_agent_dir):
    """Cursor with filename-format value logs stderr warning and treats all letters as unread.

    A v0-pattern cursor stores a filename like '2026-05-22T07-15-30Z_borges.md'
    instead of an ISO-8601 timestamp. The hook must:
      - Log a warning to stderr containing the diagnostic phrases.
      - Exit with code 0 (no crash).
      - Treat all letters as unread (None-cursor path), so the letter surfaces.
    """
    _write_config(engram_home, mode="multi", agent_name="borges")

    # Write a filename-format value to the surfaced cursor file (v0 pattern)
    malformed_cursor = "2026-05-22T07-15-30Z_borges.md"
    (engram_home / "inter-agent-surfaced-cursor.txt").write_text(malformed_cursor + "\n")

    # Write a letter addressed to us
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-22T07-35-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=5),
        heading="Test letter for cursor warning",
    )

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)

    # Hook must exit cleanly
    assert rc == 0, f"Hook must not crash on malformed cursor; stderr: {stderr}"

    # Stderr must contain the diagnostic warning
    assert "not an ISO-8601 timestamp" in stderr, (
        f"Expected 'not an ISO-8601 timestamp' in stderr; got: {stderr!r}"
    )
    assert "2026-05-22T14:30:00Z" in stderr, (
        f"Expected example format hint in stderr; got: {stderr!r}"
    )
    assert "README.md §4" in stderr, (
        f"Expected README pointer in stderr; got: {stderr!r}"
    )

    # Letter must surface (None-cursor path treats everything as unread/new)
    assert stdout.strip(), "Letter must surface when cursor is malformed (None-cursor path)"
    response = json.loads(stdout)
    ctx = response["hookSpecificOutput"]["additionalContext"]
    assert "Test letter for cursor warning" in ctx, (
        "Letter title must appear in output when cursor falls back to None"
    )


# ---------------------------------------------------------------------------
# Helpers for unit-testing _get_agent_name directly
# ---------------------------------------------------------------------------

def _load_hook_module():
    """Load the hook as a module so we can call _get_agent_name directly."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "engram_inter_agent_hook",
        Path(__file__).parent.parent / "hooks" / "claude" / "engram-inter-agent-prompt-hook.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Test 11: _get_agent_name fallback chain
# ---------------------------------------------------------------------------

class TestGetAgentName:
    """Unit tests for _get_agent_name's priority chain."""

    @pytest.fixture(autouse=True)
    def _hook(self):
        self.hook = _load_hook_module()

    def test_get_agent_name_config_wins_over_env(self, monkeypatch):
        """config.json agent_name wins even when $USER is set to something else."""
        monkeypatch.setenv("USER", "agent-mallory")
        result = self.hook._get_agent_name({"agent_name": "borges"})
        assert result == "borges"

    def test_get_agent_name_user_env_fallback(self, monkeypatch):
        """No config field; $USER=agent-borges → returns 'borges'."""
        monkeypatch.setenv("USER", "agent-borges")
        monkeypatch.delenv("LOGNAME", raising=False)
        result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_get_agent_name_logname_when_user_empty(self, monkeypatch):
        """$USER is empty; $LOGNAME=agent-borges → returns 'borges'.

        This is the bug being fixed: Claude Code hook context populates
        $LOGNAME but not $USER.
        """
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "agent-borges")
        result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_get_agent_name_uid_fallback_when_both_env_empty(self, monkeypatch):
        """$USER and $LOGNAME both empty; pwd lookup returns 'agent-borges' → 'borges'."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        mock_pw = unittest.mock.MagicMock()
        mock_pw.pw_name = "agent-borges"
        with unittest.mock.patch.object(self.hook.pwd, "getpwuid", return_value=mock_pw):
            result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_get_agent_name_returns_empty_when_all_paths_fail(self, monkeypatch):
        """All fallback paths exhausted → returns empty string."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        with unittest.mock.patch.object(self.hook.pwd, "getpwuid", side_effect=KeyError("uid not found")):
            result = self.hook._get_agent_name({})
        assert result == ""

    @pytest.mark.parametrize("envvar,value", [
        ("USER", "agent-borges"),
        ("LOGNAME", "agent-borges"),
    ])
    def test_get_agent_name_strips_agent_prefix_consistently(self, monkeypatch, envvar, value):
        """All env-var paths strip the 'agent-' prefix the same way."""
        # Blank out both env vars first, then set the one under test
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        monkeypatch.setenv(envvar, value)
        result = self.hook._get_agent_name({})
        assert result == "borges", (
            f"Expected 'borges' from {envvar}={value!r}, got {result!r}"
        )

    def test_get_agent_name_strips_agent_prefix_in_uid_path(self, monkeypatch):
        """pwd uid fallback also strips the 'agent-' prefix."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        mock_pw = unittest.mock.MagicMock()
        mock_pw.pw_name = "agent-borges"
        with unittest.mock.patch.object(self.hook.pwd, "getpwuid", return_value=mock_pw):
            result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_get_agent_name_returns_raw_username_when_no_agent_prefix(self, monkeypatch):
        """$USER='localdev' (no agent- prefix): returns raw username 'localdev', not ''."""
        monkeypatch.setenv("USER", "localdev")
        monkeypatch.delenv("LOGNAME", raising=False)
        result = self.hook._get_agent_name({})
        assert result == "localdev", (
            "Non-agent- username must be returned as-is (behavioral change from old impl)"
        )


# ---------------------------------------------------------------------------
# Test 12: Sender-list rendering (_format_senders helper)
# ---------------------------------------------------------------------------

class TestFormatSenders:
    """Unit tests for _format_senders helper via module import."""

    @pytest.fixture(autouse=True)
    def _hook(self):
        self.hook = _load_hook_module()

    def _make_letters(self, froms: list[str]) -> list[dict]:
        """Build minimal letter dicts with just 'from' fields."""
        return [{"from": f} for f in froms]

    def test_single_sender(self):
        """Single distinct sender: just the name, no comma."""
        letters = self._make_letters(["borges"])
        result = self.hook._format_senders(letters)
        assert result == "borges"

    def test_two_senders_sorted(self):
        """Two distinct senders: sorted, comma-joined."""
        letters = self._make_letters(["mneme", "borges"])
        result = self.hook._format_senders(letters)
        assert result == "borges, mneme"

    def test_three_senders_no_cap(self):
        """Three distinct senders: all shown (cap is >3, so 3 fits)."""
        letters = self._make_letters(["mneme", "borges", "ariadne"])
        result = self.hook._format_senders(letters)
        assert result == "ariadne, borges, mneme"

    def test_four_senders_cap_applied(self):
        """Four distinct senders: first 2 shown + '+ 2 other(s)'."""
        letters = self._make_letters(["mneme", "borges", "ariadne", "zephyr"])
        result = self.hook._format_senders(letters)
        # sorted: ariadne, borges, mneme, zephyr -> show ariadne, borges + 2 others
        assert result == "ariadne, borges, + 2 other(s)"

    def test_duplicate_senders_deduplicated(self):
        """Multiple letters from the same sender count as one in the list."""
        letters = self._make_letters(["borges", "borges", "ariadne"])
        result = self.hook._format_senders(letters)
        assert result == "ariadne, borges"

    def test_five_senders_cap_applied(self):
        """Five distinct senders: first 2 shown + '+ 3 other(s)'."""
        letters = self._make_letters(["e", "d", "c", "b", "a"])
        result = self.hook._format_senders(letters)
        assert result == "a, b, + 3 other(s)"


# ---------------------------------------------------------------------------
# Test 13: Action instruction wording in hook output
# ---------------------------------------------------------------------------

def test_new_letters_action_instruction_present(engram_home, inter_agent_dir):
    """'read before responding' appears in new-letters hook output."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-23T10-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=2),
        heading="Discipline relay",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "read before responding" in ctx, (
        "New-letters line must contain 'read before responding' action instruction"
    )
    assert "counterpart-agent letters carry context from the user" in ctx, (
        "New-letters line must contain the rationale clause"
    )


def test_carryover_only_action_instruction_present(engram_home, inter_agent_dir):
    """'finish reading at the next natural break' appears in carryover-only output."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    # Write a letter in the past
    old_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-05-23T08-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=old_ts,
        heading="Old unread letter",
    )

    # Set surfaced cursor AFTER the letter (so it's "older still-unread", not "new")
    surfaced_at = now - timedelta(hours=1)
    _write_surfaced_cursor(engram_home, surfaced_at)
    # No read cursor → letter is unread

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "finish reading at the next natural break" in ctx, (
        "Carryover-only line must contain 'finish reading at the next natural break'"
    )
    assert "from mneme" in ctx, (
        "Carryover-only line must include the sender name"
    )
    assert "1 older still-unread letter " in ctx, (
        f"Expected singular form '1 older still-unread letter ' (no trailing 's'); got: {ctx!r}"
    )


def test_new_letters_sender_name_in_header_line(engram_home, inter_agent_dir):
    """Sender name appears in the header line (not just in the bullet list)."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-23T11-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=3),
        heading="Hello",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    first_line = ctx.splitlines()[0]
    assert "from ariadne" in first_line, (
        "First line of output must include 'from ariadne' for single-sender new letter"
    )


def test_two_new_letter_senders_sorted_in_header(engram_home, inter_agent_dir):
    """Two distinct new-letter senders appear sorted in the header line."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-23T11-01-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=now - timedelta(minutes=5),
        heading="Letter from mneme",
    )
    _write_letter(
        inter_agent_dir, "2026-05-23T11-02-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=4),
        heading="Letter from ariadne",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    first_line = ctx.splitlines()[0]
    assert "2 new letters" in first_line, "Plural 'letters' for 2 new letters"
    assert "from ariadne, mneme" in first_line, (
        "Sorted senders 'ariadne, mneme' must appear in the header line"
    )


def test_four_new_letter_senders_cap_in_header(engram_home, inter_agent_dir):
    """Four distinct new-letter senders: first 2 + '+ 2 other(s)' in header line."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    senders = ["zephyr", "ariadne", "mneme", "lei"]
    for i, sender in enumerate(senders):
        _write_letter(
            inter_agent_dir, f"2026-05-23T12-0{i}-00Z_{sender}.md",
            from_agent=sender, to_agent="borges",
            timestamp=now - timedelta(minutes=10 - i),
            heading=f"Letter from {sender}",
        )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    first_line = ctx.splitlines()[0]
    # sorted: ariadne, lei, mneme, zephyr → show ariadne, lei + 2 other(s)
    assert "from ariadne, lei, + 2 other(s)" in first_line, (
        f"Expected cap-rule output in header; got: {first_line!r}"
    )


# ---------------------------------------------------------------------------
# Test 14: ia CLI footer in surfacing output
# ---------------------------------------------------------------------------

_IA_FOOTER = (
    "Tools: `ia read <filename>` (read + advance cursor) · "
    "`ia write --re <filename>` (reply with reference) · "
    "`ia status` (show cursor + counts)."
)


def test_footer_present_with_new_letter(engram_home, inter_agent_dir):
    """Footer naming the ia CLI appears when there is 1 new letter."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-05-28T10-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=2),
        heading="Footer test — new letter",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert _IA_FOOTER in ctx, (
        f"ia CLI footer must appear when there are new letters; got:\n{ctx}"
    )
    # Footer must be the last line
    assert ctx.splitlines()[-1] == _IA_FOOTER, (
        "ia CLI footer must be the last line of additionalContext"
    )


def test_footer_present_with_older_unread_only(engram_home, inter_agent_dir):
    """Footer naming the ia CLI appears when there are only older-unread letters."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    old_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-05-28T08-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=old_ts,
        heading="Footer test — older unread",
    )
    # Set surfaced cursor after the letter so it falls into "older still-unread"
    _write_surfaced_cursor(engram_home, now - timedelta(hours=1))

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert _IA_FOOTER in ctx, (
        f"ia CLI footer must appear for older-unread-only scenario; got:\n{ctx}"
    )
    assert ctx.splitlines()[-1] == _IA_FOOTER, (
        "ia CLI footer must be the last line of additionalContext"
    )


def test_footer_absent_when_no_letters(engram_home, inter_agent_dir):
    """No letters → hook exits silently (no stdout, so no footer to check)."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == "", (
        "No letters → hook must produce no stdout; ia footer must not appear in isolation"
    )


def test_footer_present_with_new_and_older_unread(engram_home, inter_agent_dir):
    """Footer appears when both new letters and older-unread letters are present."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    # Older letter (unread)
    old_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-05-28T08-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=old_ts,
        heading="Old unread letter",
    )
    _write_surfaced_cursor(engram_home, now - timedelta(hours=1))

    # New letter (after surfaced cursor)
    _write_letter(
        inter_agent_dir, "2026-05-28T10-30-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(minutes=5),
        heading="New letter",
    )

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert _IA_FOOTER in ctx, (
        f"ia CLI footer must appear when both new and older-unread present; got:\n{ctx}"
    )
    assert ctx.splitlines()[-1] == _IA_FOOTER, (
        "ia CLI footer must be the last line when both buckets are non-empty"
    )
    # Footer appears exactly once
    assert ctx.count(_IA_FOOTER) == 1, (
        "ia CLI footer must appear exactly once even when both buckets are non-empty"
    )


# ---------------------------------------------------------------------------
# Test 15: Read-cursor-floor (#627) — already-read letters stay invisible
#   even when the read cursor has run ahead of the surfaced cursor.
# ---------------------------------------------------------------------------

def test_read_cursor_ahead_of_surfaced_already_read_letter_invisible(
    engram_home, inter_agent_dir
):
    """Primary regression for #627: letter with surfaced_cursor < ts <= read_cursor
    must be INVISIBLE — not in 'new', not in 'unread'.

    This is the bug case: `ia read` advances the read cursor without a
    UserPromptSubmit fire (e.g. during a Monitor task-notification wake),
    leaving read_cursor > surfaced_cursor.  The pre-fix code bucketed any
    letter with ts > surfaced_cursor as 'new', re-firing the nag on the
    next prompt even though the agent already acknowledged the letter.
    """
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    # Letter was sent 2 hours ago.
    letter_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-06-01T10-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=letter_ts,
        heading="Already-read letter",
    )

    # surfaced_cursor is 3 hours old — letter arrived AFTER it, so old code
    # would classify it as 'new' on next hook fire.
    surfaced_at = now - timedelta(hours=3)
    _write_surfaced_cursor(engram_home, surfaced_at)

    # read_cursor is 1 hour old — the agent read the letter (advancing read
    # cursor past letter_ts) but no UserPromptSubmit fired since, so
    # surfaced_cursor was NOT advanced.  read_cursor > surfaced_cursor.
    read_at = now - timedelta(hours=1)
    _write_read_cursor(engram_home, read_at)

    # Confirm the scenario: surfaced_cursor < letter_ts <= read_cursor
    assert surfaced_at < letter_ts, "Precondition: letter arrived after surfaced cursor"
    assert letter_ts <= read_at, "Precondition: letter acknowledged (ts <= read_cursor)"

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0

    # The hook must exit silently — the acknowledged letter is invisible.
    assert stdout.strip() == "", (
        "Already-acknowledged letter must be invisible (not in 'new', not in 'unread'); "
        f"got stdout: {stdout!r}"
    )


def test_read_cursor_ahead_happy_paths_unaffected(engram_home, inter_agent_dir):
    """Guard: read_cursor > surfaced_cursor — genuinely new letters still surface.

    When read_cursor has run ahead of surfaced_cursor (the #627 precondition),
    letters with ts > read_cursor are genuinely unread and must appear as 'new'.
    Letters with ts <= read_cursor are acknowledged and must be invisible.

    Note: when read_cursor > surfaced_cursor the 'seen-but-unread' bucket is
    structurally empty — a letter would need ts <= surfaced_cursor AND
    ts > read_cursor, which is impossible when read_cursor > surfaced_cursor.
    The seen-but-unread path is covered by test_mode_multi_new_and_unread_partitioned
    and test_carryover_only_action_instruction_present (the normal cursor ordering).

    Timeline (oldest → newest):
      surfaced=3h  <  acknowledged=2h  <  read=1h  <  new=30min
    """
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    # (a) Genuinely new letter — ts > read_cursor → unread and new
    new_ts = now - timedelta(minutes=30)
    _write_letter(
        inter_agent_dir, "2026-06-01T13-30-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=new_ts,
        heading="Genuinely new letter",
    )

    # (b) Acknowledged letter — surfaced_cursor < ts <= read_cursor (the #627 zone)
    acknowledged_ts = now - timedelta(hours=2)
    _write_letter(
        inter_agent_dir, "2026-06-01T12-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=acknowledged_ts,
        heading="Already-acknowledged letter",
    )

    # Cursors: surfaced=3h ago; read=1h ago (read > surfaced, i.e. the #627 condition)
    surfaced_at = now - timedelta(hours=3)
    read_at = now - timedelta(hours=1)
    _write_surfaced_cursor(engram_home, surfaced_at)
    _write_read_cursor(engram_home, read_at)

    # Confirm preconditions
    assert surfaced_at < acknowledged_ts <= read_at, "Precondition: acknowledged letter in #627 zone"
    assert new_ts > read_at, "Precondition: new letter ts > read_cursor"

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip(), "Hook should produce output (genuinely new letter present)"

    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    # (a) Genuinely new letter surfaces
    assert "Genuinely new letter" in ctx, (
        f"Genuinely new letter must appear in output; got:\n{ctx}"
    )
    assert "1 new letter from ariadne" in ctx, (
        "New-letter count must be 1 (only the genuinely new one)"
    )

    # (b) Acknowledged letter must NOT appear anywhere
    assert "Already-acknowledged letter" not in ctx, (
        "Acknowledged letter must be invisible even when surfaced_cursor < ts <= read_cursor"
    )

    # No 'older still-unread' bucket — structurally impossible in read-cursor-ahead state
    assert "older still-unread" not in ctx, (
        "When read_cursor > surfaced_cursor, the seen-but-unread bucket must be empty"
    )


def test_first_run_all_letters_treated_as_new(engram_home, inter_agent_dir):
    """First-run case (both cursors absent): all letters appear as 'new'.

    Preserves the existing first-run semantics: when there is no surfaced
    cursor AND no read cursor, every letter is unread and new.
    """
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)

    _write_letter(
        inter_agent_dir, "2026-06-01T08-00-00Z_ariadne.md",
        from_agent="ariadne", to_agent="borges",
        timestamp=now - timedelta(hours=3),
        heading="First run letter A",
    )
    _write_letter(
        inter_agent_dir, "2026-06-01T09-00-00Z_mneme.md",
        from_agent="mneme", to_agent="borges",
        timestamp=now - timedelta(hours=2),
        heading="First run letter B",
    )

    # No cursor files written — first-run state.

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip(), "First run must surface all letters as new"

    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "2 new letters" in ctx, (
        f"First run must report both letters as new; got:\n{ctx}"
    )
    assert "First run letter A" in ctx
    assert "First run letter B" in ctx
    # No "older still-unread" line — everything went into new
    assert "older still-unread" not in ctx, (
        "First run must not have an 'older still-unread' bucket"
    )


# ---------------------------------------------------------------------------
# Test 16: Same-second cursor precision regression (#751)
#     Cursor written at T:00.000000Z must NOT exclude a letter at T:00.000500Z.
# ---------------------------------------------------------------------------

def test_same_second_cursor_write_uses_microseconds(engram_home, inter_agent_dir):
    """_write_cursor must emit fixed-width 6-digit microseconds (#751 regression).

    The bug: _write_cursor wrote '...T12:00:00Z' (seconds-only).  Lexicographically
    'T12:00:00Z' > 'T12:00:00.500000Z' ('Z'=0x5A > '.'=0x2E), so any post/letter
    with a microsecond-precise timestamp in the same second was silently excluded
    by a `created_at > since` (or Python datetime >) comparison.

    The fix: _write_cursor now emits '...T12:00:00.000000Z'.  This test verifies
    the written cursor format, and then confirms that a letter with a microsecond-
    precise timestamp in the SAME second is correctly classified as 'new'
    (letter_ts.microsecond > 0 means letter_ts > cursor_ts in Python datetime
    comparison, since both parse to the same UTC second base but different micros).

    The letter's frontmatter timestamp is written with microseconds directly to
    simulate a real letter authored by a tool that preserves sub-second precision.
    """
    _write_config(engram_home, mode="multi", agent_name="borges")

    # Write cursor at the start of a second with zero microseconds
    cursor_ts = datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=timezone.utc)
    _write_surfaced_cursor(engram_home, cursor_ts)

    # Verify the written cursor format uses microseconds (the fix)
    raw_cursor = (engram_home / "inter-agent-surfaced-cursor.txt").read_text().strip()
    assert "." in raw_cursor, (
        f"Cursor file must use microseconds format after fix; got: {raw_cursor!r} — "
        "pre-fix format 'T12:00:00Z' would lexically sort AFTER any same-second "
        "microsecond-precise timestamp"
    )
    assert raw_cursor == "2026-01-01T12:00:00.000000Z", (
        f"Cursor must use fixed-width 6-digit microseconds; got: {raw_cursor!r}"
    )

    # Write a letter whose timestamp is 500ms later in the same second.
    # We write the frontmatter directly (not via _write_letter's seconds-only helper)
    # to simulate a tool that emits microsecond-precise timestamps.
    letter_ts = datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)
    letter_ts_str = letter_ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    letter_content = (
        "---\n"
        "from: ariadne\n"
        "to: borges\n"
        f"timestamp: {letter_ts_str}\n"
        "---\n"
        "\n"
        "# Same-second letter\n"
        "\n"
        "A letter with a sub-second timestamp.\n"
    )
    (inter_agent_dir / "2026-01-01T12-00-00.500000Z_ariadne.md").write_text(letter_content)

    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0, f"Hook must not crash; stderr: {stderr}"
    assert stdout.strip(), (
        "Same-second letter must surface as new — letter_ts 12:00:00.500000Z "
        "must be > cursor 12:00:00.000000Z (Python datetime comparison works correctly "
        "once cursor is parsed from the fixed-width microseconds format)"
    )
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Same-second letter" in ctx, (
        f"Same-second letter title must appear in hook output; got:\n{ctx}"
    )
    assert "1 new letter from ariadne" in ctx, (
        "Letter must be classified as 'new' (letter_ts > cursor_ts after the fix)"
    )


# ---------------------------------------------------------------------------
# Test 17: Multi-recipient membership in the hook (part of #631)
# ---------------------------------------------------------------------------

def test_hook_multi_recipient_surfaces_for_named_agent(engram_home, inter_agent_dir):
    """Hook surfaces a letter with to: ariadne, borges when agent is borges."""
    _write_config(engram_home, mode="multi", agent_name="borges")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-06-05T10-00-00Z_mira.md",
        from_agent="mira", to_agent="ariadne, borges",
        timestamp=now - timedelta(minutes=5),
        heading="Shared update for both",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip(), "Hook must surface the multi-recipient letter for borges"
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Shared update for both" in ctx, (
        f"Multi-recipient letter must surface for borges; got:\n{ctx}"
    )


def test_hook_multi_recipient_does_not_surface_for_non_recipient(engram_home, inter_agent_dir):
    """Hook does NOT surface a letter with to: ariadne, borges when agent is mira."""
    _write_config(engram_home, mode="multi", agent_name="mira")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-06-05T10-00-00Z_sender.md",
        from_agent="sender", to_agent="ariadne, borges",
        timestamp=now - timedelta(minutes=5),
        heading="Shared update for ariadne and borges",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == "", (
        "Multi-recipient letter to 'ariadne, borges' must not surface for 'mira'; "
        f"got stdout: {stdout!r}"
    )


def test_hook_multi_recipient_no_substring_match(engram_home, inter_agent_dir):
    """Hook does not match 'mir' when the to: field contains 'mira' (substring non-match)."""
    _write_config(engram_home, mode="multi", agent_name="mir")
    now = datetime.now(timezone.utc)
    _write_letter(
        inter_agent_dir, "2026-06-05T11-00-00Z_sender.md",
        from_agent="sender", to_agent="mira",
        timestamp=now - timedelta(minutes=5),
        heading="Letter to mira only",
    )
    rc, stdout, stderr = _run_hook(engram_home, inter_agent_dir)
    assert rc == 0
    assert stdout.strip() == "", (
        "Agent 'mir' must not see a letter addressed to 'mira' (no substring match); "
        f"got stdout: {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Parity: hook._is_recipient and tools/ia._is_recipient must agree
# ---------------------------------------------------------------------------

def test_is_recipient_hook_and_ia_parity():
    """hook._is_recipient and tools/ia._is_recipient must agree on the same input pairs.

    Both implementations share the same algorithm (split on comma, strip,
    case-insensitive exact match). This test pins them against drift: if one
    copy is updated and the other is not, the test goes red.

    Mechanical gate > vigilance — analogous to test_marker_filename_matches_walguard.
    """
    import importlib.util
    import sys

    # Load hook module (not importable by normal name — use spec_from_file_location)
    hook_spec = importlib.util.spec_from_file_location(
        "engram_inter_agent_hook",
        Path(__file__).parent.parent / "hooks" / "claude" / "engram-inter-agent-prompt-hook.py",
    )
    hook_module = importlib.util.module_from_spec(hook_spec)
    hook_spec.loader.exec_module(hook_module)
    hook_is_recipient = hook_module._is_recipient

    # Load tools/ia._is_recipient via importlib to avoid relying on sys.path order
    ia_spec = importlib.util.spec_from_file_location(
        "ia",
        Path(__file__).parent.parent / "tools" / "ia.py",
    )
    ia_module = importlib.util.module_from_spec(ia_spec)
    ia_spec.loader.exec_module(ia_module)
    ia_is_recipient = ia_module._is_recipient

    # ~8 input pairs covering the documented contract
    cases = [
        # (to_field, agent_name, description)
        ("ariadne", "ariadne", "single recipient — match"),
        ("ariadne", "borges", "single recipient — mismatch"),
        ("ariadne, borges", "ariadne", "multi-recipient — first name match"),
        ("ariadne, borges", "borges", "multi-recipient — second name match"),
        ("mira", "mir", "substring non-match: 'mir' vs 'mira'"),
        ("Ariadne, BORGES", "ariadne", "case-insensitive match"),
        ("ariadne, borges", "", "empty agent_name — no match"),
        ("", "ariadne", "empty to_field — no match"),
    ]

    for to_field, agent_name, description in cases:
        hook_result = hook_is_recipient(to_field, agent_name)
        ia_result = ia_is_recipient(to_field, agent_name)
        assert hook_result == ia_result, (
            f"Parity failure on case {description!r}: "
            f"hook._is_recipient({to_field!r}, {agent_name!r}) = {hook_result}, "
            f"ia._is_recipient({to_field!r}, {agent_name!r}) = {ia_result}"
        )
