"""Tests for hooks/claude/engram-inter-agent-prompt-hook.py (DM-API shape).

The hook was converted from local-filesystem letter scans to pure forum DM
API reads (UCS PR3b, stacked on PR3a). Tests are restructured accordingly:

- No INTER_AGENT_DIR / letter-file scaffolding.
- Unit tests call _extract_dm_subject(), _format_senders() as pure functions.
- Integration tests monkeypatch mod._fetch_dm_updates to control what the
  hook "sees" from the forum, and capture stdout/stderr via
  redirect_stdout / redirect_stderr.

Behaviors covered:

  Unit -- _extract_dm_subject(body):
    1.  **Subject:** line wins over heading and body-first-60.
    2.  # Heading used when no **Subject:** line.
    3.  First 60 chars used as fallback when no heading.
    4.  Body ≤60 chars returned as-is (no ellipsis).
    5.  Bare-heading body uses non-hash content, not the hash itself.
    6.  Empty body returns '(no subject)'.

  Unit -- _format_senders(updates):
    7.  Single sender: just the name.
    8.  Two distinct senders: sorted, comma-joined.
    9.  Three senders: all shown.
    10. Four senders: first 2 + '+ 2 other(s)'.
    11. Duplicate senders deduplicated.

  Integration -- main() rendering:
    12. No-forum-configured guard (no forum.url, no agent_name) -> silent
        exit 0, no output. (mode='single' WITH forum.url/agent_name set
        succeeds identically to mode='multi' — #2857 step 1.)
    13. forum_api gate (_FORUM_API_OK=False) -> silent exit 0, no output.
    14. No DMs -> no output; surfaced cursor advances.
    15. 1 new DM -> new-message block emitted.
    16. New + unread -> both surface, correct partitioning.
    17. Surfaced cursor advances to as_of on live fetch.
    18. Surfaced cursor does NOT advance on outage (banner path).
    19. Read cursor never advanced by the hook (only surfaced cursor moves).
    20. ForumNetworkError -> '⚠️ DM unread check unavailable' banner, no crash.
    21. ForumHttpError -> same banner, no crash.
    22. Group-summarize cap: >LIST_CAP new DMs -> listed cap + '+ N older' line.
    23. Read-cursor-ahead edge: already-read DM is invisible (not re-surfaced).
    24. First-run (both cursors 0): all DMs appear as new.
    25. Two new DMs from different senders: sorted senders in header.
    26. Still-unread-only (no new): 'older still-unread' line, correct wording.
    27. Footer 'ia read/write/mr/status' present when DMs surface.
    28. Footer absent when no DMs (no stdout).
    29. Missing config -> mode defaults to single -> silent exit.
"""

import importlib
import importlib.util
import io
import json
import os
import sys
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-inter-agent-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _make_dm_update(
    seq: int,
    sender: str,
    body: str = "Hello from a fellow agent.",
    kind: str = "dm",
    wake: str = "act-now",
) -> dict:
    """Build a DM update dict matching the shape returned by GET /api/updates."""
    return {
        "kind": kind,
        "seq": seq,
        "wake": wake,
        "sender": sender,
        "body": body,
        "event_ts": f"2026-01-01T00:00:{seq:02d}Z",
    }


def _make_updates_response(
    updates: list,
    as_of: int,
) -> dict:
    """Build an /api/updates response dict."""
    return {
        "updates": updates,
        "as_of": as_of,
        "ts": "2026-01-01T00:00:01Z",
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _write_config(
    engram_home: Path,
    agent_name="borges",
    mode: str = "multi",
    forum_url=None,
) -> None:
    """Write a minimal config.json into engram_home.

    agent_name=None omits the agent_name field entirely (for the
    no-forum-configured gate tests); forum_url, if given, sets forum.url.
    #3546: agent_name alone no longer configures a forum, so a config that
    names an agent also gets a (never-contacted: the fetch is stubbed) forum.url.
    """
    cfg = {"mode": mode, "counterparts": []}
    if agent_name is not None:
        cfg["agent_name"] = agent_name
        if forum_url is None:
            forum_url = "http://127.0.0.1:5002"
    if forum_url is not None:
        cfg["forum"] = {"url": forum_url}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_surfaced_cursor(engram_home: Path, value: int) -> None:
    (engram_home / "inter-agent-surfaced-cursor.txt").write_text(str(value) + "\n")


def _write_read_cursor(engram_home: Path, value: int) -> None:
    (engram_home / "inter-agent-read-cursor.txt").write_text(str(value) + "\n")


def _read_surfaced_cursor(engram_home: Path) -> int:
    path = engram_home / "inter-agent-surfaced-cursor.txt"
    if not path.exists():
        return -1  # sentinel: "not written"
    return int(path.read_text().strip())


def _read_read_cursor(engram_home: Path) -> int:
    path = engram_home / "inter-agent-read-cursor.txt"
    if not path.exists():
        return -1  # sentinel: "not written"
    return int(path.read_text().strip())


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook_module(engram_home: Path):
    """Import the hook as a live module with ENGRAM_HOME set to engram_home."""
    spec = importlib.util.spec_from_file_location(
        "engram_inter_agent_hook", HOOK_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Integration runner helper
# ---------------------------------------------------------------------------

def _call_main(
    mod,
    updates=None,
    as_of: int = 100,
    raise_exc=None,
) -> tuple:
    """Call mod.main() with _fetch_dm_updates and stdin patched.

    Returns (stdout_str, stderr_str, exit_code).

    Parameters
    ----------
    updates:
        List of DM update dicts returned by the fake fetch. None treated as [].
    as_of:
        The as_of watermark returned by the fake fetch.
    raise_exc:
        If set, _fetch_dm_updates raises this exception instead.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0

    def _fake_fetch(config, agent_name, since):
        if raise_exc is not None:
            raise raise_exc
        items = updates if updates is not None else []
        return _make_updates_response(items, as_of)

    fake_stdin = io.StringIO(json.dumps({"session_id": "test-session"}))

    with patch.object(mod, "_fetch_dm_updates", side_effect=_fake_fetch):
        with patch.object(sys, "stdin", fake_stdin):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    mod.main()
            except SystemExit as e:
                exit_code = int(e.code) if e.code is not None else 0

    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


def _parse_ctx(stdout_str: str) -> str:
    """Parse the additionalContext string from hook stdout JSON."""
    return json.loads(stdout_str)["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Unit tests -- _extract_dm_subject()
# ---------------------------------------------------------------------------

class TestExtractDmSubject:
    """Pure-function unit tests for _extract_dm_subject(body)."""

    @pytest.fixture(autouse=True)
    def _hook(self, engram_home):
        _write_config(engram_home)
        self.mod = _load_hook_module(engram_home)

    # 1. **Subject:** line wins
    def test_subject_line_wins_over_heading(self):
        body = "**Subject:** The real subject\n\n# A heading\n\nBody text."
        result = self.mod._extract_dm_subject(body)
        assert result == "The real subject"

    def test_subject_line_wins_over_body_first_60(self):
        body = "**Subject:** Main topic\n\n" + "A" * 80
        result = self.mod._extract_dm_subject(body)
        assert result == "Main topic"

    # 2. # Heading used when no **Subject:** line
    def test_heading_used_when_no_subject_line(self):
        body = "# My Heading\n\nBody text here."
        result = self.mod._extract_dm_subject(body)
        assert result == "My Heading"

    def test_heading_strips_hash(self):
        body = "## Section heading\n\nContent."
        result = self.mod._extract_dm_subject(body)
        assert result == "Section heading"

    # 3. First 60 chars as fallback
    def test_body_first_60_fallback(self):
        body = "A" * 80 + " trailing"
        result = self.mod._extract_dm_subject(body)
        assert result == "A" * 57 + "..."

    # 4. Body ≤60 chars returned as-is
    def test_short_body_no_ellipsis(self):
        body = "Short message body."
        result = self.mod._extract_dm_subject(body)
        assert result == "Short message body."
        assert "..." not in result

    # 5. Bare-heading body uses non-hash content
    def test_bare_heading_skipped(self):
        body = "##\n\nactual content here"
        result = self.mod._extract_dm_subject(body)
        assert result == "actual content here"
        assert "##" not in result

    # 6. Empty body returns '(no subject)'
    def test_empty_body(self):
        assert self.mod._extract_dm_subject("") == "(no subject)"
        assert self.mod._extract_dm_subject(None) == "(no subject)"


# ---------------------------------------------------------------------------
# Unit tests -- _format_senders()
# ---------------------------------------------------------------------------

class TestFormatSenders:
    """Pure-function unit tests for _format_senders(updates)."""

    @pytest.fixture(autouse=True)
    def _hook(self, engram_home):
        _write_config(engram_home)
        self.mod = _load_hook_module(engram_home)

    def _make(self, senders: list) -> list:
        return [{"sender": s} for s in senders]

    # 7. Single sender
    def test_single_sender(self):
        assert self.mod._format_senders(self._make(["borges"])) == "borges"

    # 8. Two distinct senders: sorted, comma-joined
    def test_two_senders_sorted(self):
        result = self.mod._format_senders(self._make(["mneme", "borges"]))
        assert result == "borges, mneme"

    # 9. Three senders: all shown
    def test_three_senders_all_shown(self):
        result = self.mod._format_senders(self._make(["mneme", "borges", "ariadne"]))
        assert result == "ariadne, borges, mneme"

    # 10. Four senders: cap applied
    def test_four_senders_cap(self):
        result = self.mod._format_senders(self._make(["mneme", "borges", "ariadne", "zephyr"]))
        # sorted: ariadne, borges, mneme, zephyr → show ariadne, borges + 2 others
        assert result == "ariadne, borges, + 2 other(s)"

    # 11. Duplicates deduplicated
    def test_duplicates_deduplicated(self):
        result = self.mod._format_senders(self._make(["borges", "borges", "ariadne"]))
        assert result == "ariadne, borges"


# ---------------------------------------------------------------------------
# Integration tests -- main() rendering
# ---------------------------------------------------------------------------

class TestMainRendering:
    """Integration tests for main(): DM surfacing behavior."""

    # 12. No-forum-configured gate
    def test_no_forum_configured_gate_silent_exit(self, engram_home):
        """No forum.url, no agent_name in config -> hook exits 0 with no output."""
        _write_config(engram_home, mode="single", agent_name=None)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, updates=[
            _make_dm_update(1, "ariadne", "Hello"),
        ])
        assert rc == 0
        assert stdout.strip() == "", "no forum configured must produce no output"
        # Surfaced cursor must NOT be written when no forum is configured
        assert _read_surfaced_cursor(engram_home) == -1

    # 12b. Control: mode='single' WITH a forum configured succeeds
    def test_single_mode_with_forum_configured_succeeds(self, engram_home):
        """mode='single' but agent_name set -> hook proceeds (not held at the gate)."""
        _write_config(engram_home, mode="single", agent_name="borges")
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, updates=[
            _make_dm_update(1, "ariadne", "Hello"),
        ])
        assert rc == 0
        assert stdout.strip() != ""
        assert _read_surfaced_cursor(engram_home) != -1

    # 13. forum_api gate (_FORUM_API_OK=False)
    def test_forum_api_gate_silent_exit(self, engram_home):
        """_FORUM_API_OK=False -> hook exits 0 with no output."""
        _write_config(engram_home, mode="multi")
        mod = _load_hook_module(engram_home)
        mod._FORUM_API_OK = False
        stdout, stderr, rc = _call_main(mod, updates=[])
        assert rc == 0
        assert stdout.strip() == "", "forum_api import failure must be silent"

    # 14. No DMs -> no output; surfaced cursor advances
    def test_no_dms_silent_cursor_advances(self, engram_home):
        """No DMs from API -> exit 0, no stdout, surfaced cursor advances."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, updates=[], as_of=42)
        assert rc == 0
        assert stdout.strip() == ""
        # Surfaced cursor advances to as_of even when nothing to surface
        assert _read_surfaced_cursor(engram_home) == 42

    # 15. 1 new DM -> new-message block emitted
    def test_one_new_dm_surfaces(self, engram_home):
        """1 new DM: output contains new-message block with sender and subject."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        # surfaced_cursor=0 (absent), so seq=5 > 0 → new
        update = _make_dm_update(5, "ariadne", "**Subject:** Review request\n\nBody.")
        stdout, stderr, rc = _call_main(mod, updates=[update], as_of=5)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "1 new message from ariadne" in ctx
        assert "read before responding" in ctx
        assert "Review request" in ctx

    # 16. New + unread -> both surface, correct partitioning
    def test_new_and_unread_partitioned(self, engram_home):
        """1 new DM + 1 older still-unread: both appear, correctly partitioned."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        # surfaced cursor at seq=10, read cursor at 0
        # seq=5 → still-unread (5 > read_cursor=0 AND 5 <= surfaced=10)
        # seq=15 → new (15 > surfaced_cursor=10)
        _write_surfaced_cursor(engram_home, 10)
        _write_read_cursor(engram_home, 0)

        older = _make_dm_update(5, "mneme", "**Subject:** Old unread")
        newer = _make_dm_update(15, "ariadne", "**Subject:** New arrival")
        stdout, stderr, rc = _call_main(mod, updates=[older, newer], as_of=20)
        assert rc == 0
        ctx = _parse_ctx(stdout)

        # New DM surfaces
        assert "1 new message from ariadne" in ctx
        assert "New arrival" in ctx

        # Older unread appears as "(Plus N older still-unread from …)"
        assert "1 older still-unread" in ctx
        assert "from mneme" in ctx

    # 16b. since = min(surfaced, read) — the fetch window must cover BOTH buckets
    def test_fetch_since_is_min_of_both_cursors(self, engram_home):
        """`_fetch_dm_updates` must be called with `since = min(surfaced, read)` so
        one call covers both the 'new' (>surfaced) and 'still-unread' (>read) buckets.
        The mock-returns-all path can't exercise this, so assert the `since` value
        directly — from BOTH cursor orderings, so a since=surfaced-only or
        since=read-only off-by-one is caught (each ordering rules out one)."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)

        captured = {}

        def _capture_fetch(config, agent_name, since):
            captured["since"] = since
            return _make_updates_response([], as_of=99)

        def _run():
            with patch.object(mod, "_fetch_dm_updates", side_effect=_capture_fetch):
                with patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "t"}))):
                    try:
                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            mod.main()
                    except SystemExit:
                        pass

        # read ahead of surfaced (e.g. `ia mr` during a Monitor wake) → min = surfaced = 5
        _write_surfaced_cursor(engram_home, 5)
        _write_read_cursor(engram_home, 15)
        _run()
        assert captured["since"] == 5, f"min(surfaced=5, read=15) should be 5, got {captured['since']}"

        # surfaced ahead of read → min = read = 5 (rules out 'always surfaced')
        _write_surfaced_cursor(engram_home, 15)
        _write_read_cursor(engram_home, 5)
        _run()
        assert captured["since"] == 5, f"min(surfaced=15, read=5) should be 5, got {captured['since']}"

    # 17. Surfaced cursor advances to as_of on live fetch
    def test_surfaced_cursor_advances_to_as_of(self, engram_home):
        """After successful fetch: surfaced cursor = as_of from response."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_surfaced_cursor(engram_home, 5)
        _call_main(mod, updates=[], as_of=99)
        assert _read_surfaced_cursor(engram_home) == 99

    # 18. Surfaced cursor does NOT advance on outage (banner path)
    def test_surfaced_cursor_no_advance_on_outage(self, engram_home):
        """On ForumNetworkError: surfaced cursor stays at old value (no step-over)."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_surfaced_cursor(engram_home, 7)

        # Import ForumNetworkError from the loaded hook module
        from forum_api import ForumNetworkError
        stdout, stderr, rc = _call_main(
            mod,
            raise_exc=ForumNetworkError("connection refused"),
        )
        assert rc == 0
        # Banner emitted
        ctx = _parse_ctx(stdout)
        assert "DM unread check unavailable" in ctx
        # Surfaced cursor UNCHANGED
        assert _read_surfaced_cursor(engram_home) == 7

    # 19. Read cursor never advanced by the hook
    def test_read_cursor_never_advanced(self, engram_home):
        """Hook never advances the read cursor — only surfaced cursor moves."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_read_cursor(engram_home, 3)
        _write_surfaced_cursor(engram_home, 3)

        update = _make_dm_update(10, "ariadne", "New message")
        _call_main(mod, updates=[update], as_of=10)

        # Surfaced cursor advanced
        assert _read_surfaced_cursor(engram_home) == 10
        # Read cursor UNCHANGED
        assert _read_read_cursor(engram_home) == 3

    # 20. ForumNetworkError -> banner
    def test_forum_network_error_banner(self, engram_home):
        """ForumNetworkError -> banner on stdout, exit 0, no crash."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        from forum_api import ForumNetworkError
        stdout, stderr, rc = _call_main(
            mod,
            raise_exc=ForumNetworkError("timeout"),
        )
        assert rc == 0
        assert stdout.strip() != "", "Must emit banner on network error"
        ctx = _parse_ctx(stdout)
        assert "DM unread check unavailable" in ctx
        assert "UCS" in ctx

    # 21. ForumHttpError -> banner
    def test_forum_http_error_banner(self, engram_home):
        """ForumHttpError -> banner on stdout, exit 0, no crash."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        from forum_api import ForumHttpError
        stdout, stderr, rc = _call_main(
            mod,
            raise_exc=ForumHttpError(503, "service unavailable"),
        )
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "DM unread check unavailable" in ctx

    # 22. Group-summarize cap
    def test_group_summarize_cap(self, engram_home):
        """More than LIST_CAP new DMs: first LIST_CAP listed + '+ N older' line."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        cap = mod.LIST_CAP  # typically 10

        # Create cap+3 new DMs from 2 different senders
        updates = [
            _make_dm_update(i + 1, "ariadne" if i < cap else "mneme", f"Msg {i}")
            for i in range(cap + 3)
        ]
        # surfaced_cursor=0 so all are new
        stdout, stderr, rc = _call_main(mod, updates=updates, as_of=cap + 3)
        assert rc == 0
        ctx = _parse_ctx(stdout)

        # Cap+3 new total; lines after header should show LIST_CAP individual + 1 group line
        assert f"{cap + 3} new messages" in ctx
        assert "+ 3 older from" in ctx

    # 23. Read-cursor-ahead edge: already-read DM invisible
    def test_read_cursor_ahead_already_read_invisible(self, engram_home):
        """When read_cursor > surfaced_cursor, acknowledged DMs are invisible.

        Edge case: `ia mr` advanced the read cursor (seq=15) without an
        intervening UserPromptSubmit, leaving read_cursor > surfaced_cursor.
        A DM with surfaced_cursor < seq <= read_cursor was acknowledged and
        must NOT re-appear as 'new'.
        """
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        # surfaced at 5, read at 15 (read ahead of surfaced)
        _write_surfaced_cursor(engram_home, 5)
        _write_read_cursor(engram_home, 15)

        # DM at seq=10 — acknowledged (10 <= read_cursor=15) even though
        # it arrived after surfaced_cursor=5
        acknowledged = _make_dm_update(10, "ariadne", "Already read")
        # Genuinely new DM at seq=20 (20 > read_cursor=15)
        new_dm = _make_dm_update(20, "borges", "Genuinely new")

        stdout, stderr, rc = _call_main(
            mod, updates=[acknowledged, new_dm], as_of=20
        )
        assert rc == 0
        ctx = _parse_ctx(stdout)

        assert "Genuinely new" in ctx, "Genuinely new DM must surface"
        assert "Already read" not in ctx, "Acknowledged DM must NOT re-appear"
        assert "older still-unread" not in ctx, "No unread bucket when read_cursor > surfaced_cursor"

    # 24. First run (both cursors 0): all DMs appear as new
    def test_first_run_all_dms_as_new(self, engram_home):
        """First run (no cursor files): all DMs appear as new."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        # No cursor files written — first-run state
        updates = [
            _make_dm_update(1, "ariadne", "First message"),
            _make_dm_update(2, "mneme", "Second message"),
        ]
        stdout, stderr, rc = _call_main(mod, updates=updates, as_of=2)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "2 new messages" in ctx
        assert "First message" in ctx
        assert "Second message" in ctx
        assert "older still-unread" not in ctx

    # 25. Two new DMs from different senders: sorted senders in header
    def test_two_new_senders_sorted_in_header(self, engram_home):
        """Two distinct new-DM senders appear sorted in the header line."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        updates = [
            _make_dm_update(1, "mneme", "From mneme"),
            _make_dm_update(2, "ariadne", "From ariadne"),
        ]
        stdout, stderr, rc = _call_main(mod, updates=updates, as_of=2)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        first_line = ctx.splitlines()[0]
        assert "2 new messages" in first_line
        assert "from ariadne, mneme" in first_line, (
            f"Sorted senders 'ariadne, mneme' must appear in header; got: {first_line!r}"
        )

    # 26. Still-unread-only (no new): 'older still-unread' line and wording
    def test_still_unread_only(self, engram_home):
        """Only older-unread DMs (no new): 'older still-unread' line surfaced."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        # surfaced=10, read=0, DM at seq=5 → older still-unread
        _write_surfaced_cursor(engram_home, 10)
        _write_read_cursor(engram_home, 0)

        update = _make_dm_update(5, "mneme", "Old unread")
        stdout, stderr, rc = _call_main(mod, updates=[update], as_of=10)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "1 older still-unread message" in ctx, (
            f"Singular 'message' wording expected; got: {ctx!r}"
        )
        assert "from mneme" in ctx
        assert "finish reading at the next natural break" in ctx

    # 27. Footer present when DMs surface
    def test_footer_present_with_new_dms(self, engram_home):
        """'ia read/write/mr/status' footer appears when there are new DMs."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        update = _make_dm_update(1, "ariadne", "Hello")
        stdout, _, rc = _call_main(mod, updates=[update], as_of=1)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "`ia read <counterpart>`" in ctx
        assert "`ia mr`" in ctx
        assert "`ia status`" in ctx

    def test_footer_present_with_unread_only(self, engram_home):
        """Footer appears when there are only older-unread DMs."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_surfaced_cursor(engram_home, 10)
        _write_read_cursor(engram_home, 0)
        update = _make_dm_update(5, "mneme", "Old")
        stdout, _, rc = _call_main(mod, updates=[update], as_of=10)
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "`ia read <counterpart>`" in ctx

    # 28. Footer absent when no DMs
    def test_footer_absent_when_no_dms(self, engram_home):
        """No DMs -> hook exits silently, no footer."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, _, rc = _call_main(mod, updates=[], as_of=1)
        assert rc == 0
        assert stdout.strip() == "", "No DMs -> no stdout; footer must not appear"

    # 29. Missing config -> no forum configured -> silent exit
    def test_missing_config_treated_as_single(self, engram_home):
        """No config.json -> no forum.url/agent_name, hook exits silently."""
        mod = _load_hook_module(engram_home)  # no config written
        stdout, stderr, rc = _call_main(mod, updates=[
            _make_dm_update(1, "ariadne", "Hello"),
        ])
        assert rc == 0
        assert stdout.strip() == ""


# ---------------------------------------------------------------------------
# Unit tests -- _get_agent_name() (unchanged function, preserved tests)
# ---------------------------------------------------------------------------

def _load_hook_module_direct(engram_home: Path):
    """Load the hook module directly (for unit-testing internal functions)."""
    spec = importlib.util.spec_from_file_location(
        "engram_inter_agent_hook",
        HOOK_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}):
        spec.loader.exec_module(mod)
    return mod


class TestGetAgentName:
    """Unit tests for _get_agent_name's priority chain."""

    @pytest.fixture(autouse=True)
    def _hook(self, engram_home):
        self.hook = _load_hook_module_direct(engram_home)

    def test_config_wins_over_env(self, monkeypatch):
        """config.json agent_name wins even when $USER is set to something else."""
        monkeypatch.setenv("USER", "agent-mallory")
        result = self.hook._get_agent_name({"agent_name": "borges"})
        assert result == "borges"

    def test_user_env_fallback(self, monkeypatch):
        """No config field; $USER=agent-borges -> returns 'borges'."""
        monkeypatch.setenv("USER", "agent-borges")
        monkeypatch.delenv("LOGNAME", raising=False)
        result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_logname_when_user_empty(self, monkeypatch):
        """$USER is empty; $LOGNAME=agent-borges -> returns 'borges'."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "agent-borges")
        result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_uid_fallback_when_both_env_empty(self, monkeypatch):
        """$USER and $LOGNAME both empty; pwd lookup returns 'agent-borges' -> 'borges'."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        mock_pw = unittest.mock.MagicMock()
        mock_pw.pw_name = "agent-borges"
        with unittest.mock.patch.object(self.hook.pwd, "getpwuid", return_value=mock_pw):
            result = self.hook._get_agent_name({})
        assert result == "borges"

    def test_returns_empty_when_all_paths_fail(self, monkeypatch):
        """All fallback paths exhausted -> returns empty string."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        with unittest.mock.patch.object(self.hook.pwd, "getpwuid", side_effect=KeyError("uid not found")):
            result = self.hook._get_agent_name({})
        assert result == ""

    @pytest.mark.parametrize("envvar,value", [
        ("USER", "agent-borges"),
        ("LOGNAME", "agent-borges"),
    ])
    def test_strips_agent_prefix_consistently(self, monkeypatch, envvar, value):
        """All env-var paths strip the 'agent-' prefix the same way."""
        monkeypatch.setenv("USER", "")
        monkeypatch.setenv("LOGNAME", "")
        monkeypatch.setenv(envvar, value)
        result = self.hook._get_agent_name({})
        assert result == "borges", (
            f"Expected 'borges' from {envvar}={value!r}, got {result!r}"
        )

    def test_returns_raw_username_when_no_agent_prefix(self, monkeypatch):
        """$USER='localdev' (no agent- prefix): returns raw username 'localdev', not ''."""
        monkeypatch.setenv("USER", "localdev")
        monkeypatch.delenv("LOGNAME", raising=False)
        result = self.hook._get_agent_name({})
        assert result == "localdev"



# ---------------------------------------------------------------------------
# Flattened-topology regression (gh#1657 -- _hooklib extraction, slice 2)
# ---------------------------------------------------------------------------

import subprocess


def test_hooklib_resolution_in_flattened_deployed_topology(tmp_path):
    """The plugin build FLATTENS hooks/claude/ -> hooks/, so the deployed
    inter-agent hook sits one level shallower than source, AND _hooklib.py
    (its sibling since gh#1657 slice 2) AND _prompthooklib.py (its sibling
    since gh#1680 slice 1) must both be found alongside it in that same
    flattened layout. Mirrors the baton hook's own regression test (added
    in slice 1), per Aleph's review note on PR #1726: keep the per-hook
    flattened-topology test as each hook is re-pointed, so a future
    build-layout change can't silently break resolution for one hook while
    the others still pass. Extended for gh#1680 slice 1 to also confirm
    (b) config loads, (c) agent name resolves, (d) emit_context prints the
    identical additionalContext envelope -- all now delegated to
    _prompthooklib rather than defined locally.
    """
    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()
    hooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
    )
    prompthooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_prompthooklib.py"
    )
    (root / "hooks" / "engram-inter-agent-prompt-hook.py").write_text(HOOK_PATH.read_text())
    (root / "hooks" / "_hooklib.py").write_text(hooklib_src.read_text())
    (root / "hooks" / "_prompthooklib.py").write_text(prompthooklib_src.read_text())
    # Stub forum_api.py so the import can succeed.
    (root / "tools" / "forum_api.py").write_text(
        "class ForumClient:\n"
        "    def __init__(self, *a, **k): pass\n"
        "class ForumNetworkError(Exception): pass\n"
        "class ForumHttpError(Exception):\n"
        "    def __init__(self, status=0, body=''): self.status=status; self.body=body\n"
        "def forum_url_from_config(config): return 'http://localhost:5002'\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "luria", "mode": "multi"}))

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    probe_script = (
        "import importlib.util, sys, io, json, contextlib\n"
        "spec = importlib.util.spec_from_file_location('hook', "
        f"  r'{root}/hooks/engram-inter-agent-prompt-hook.py')\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "print('resolved' if mod._FORUM_API_OK and mod._TOOLS_DIR is not None else 'broken')\n"
        "cfg = mod._load_config()\n"
        "print('config_ok' if cfg.get('agent_name') == 'luria' else f'config_broken:{cfg!r}')\n"
        "name = mod._get_agent_name(cfg)\n"
        "print('name_ok' if name == 'luria' else f'name_broken:{name!r}')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    mod._emit_context('hello world')\n"
        "envelope = json.loads(buf.getvalue())\n"
        "expected = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'hello world'}}\n"
        "print('emit_ok' if envelope == expected else f'emit_broken:{envelope!r}')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe_script],
        capture_output=True, text=True, env=env,
    )
    assert "resolved" in r.stdout, (
        "deployed-topology hook failed to resolve _hooklib/forum_api via "
        f"walk-parents (stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "config_ok" in r.stdout, (
        f"deployed-topology hook failed to load config via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "name_ok" in r.stdout, (
        f"deployed-topology hook failed to resolve agent name via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "emit_ok" in r.stdout, (
        f"deployed-topology hook's emit_context envelope diverged "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
