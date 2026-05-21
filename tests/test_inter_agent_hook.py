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
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    (engram_home / "inter-agent-surfaced-cursor.txt").write_text(ts_str + "\n")


def _write_read_cursor(engram_home: Path, ts: datetime) -> None:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
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
    assert "New letters since last prompt: 1" in ctx
    assert "ariadne" in ctx
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
    assert "New letters since last prompt: 1" in ctx
    assert "ariadne" in ctx
    assert "Re: handoff plan" in ctx

    # Older unread appears in "(Plus N older still-unread.)" line
    assert "1 older still-unread" in ctx


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
