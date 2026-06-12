"""Tests for tools/forum.py — agent-first forum CLI.

Integration strategy: spin up the real forum server via Flask's test_client()
(from forum.server.create_app + a tmp SQLite DB) and wire the CLI's _api_get /
_api_post to call through it. This avoids contract-drift between mock responses
and the real API — the CLI talks to the actual Flask routes.

The wiring is done by monkeypatching the forum module's _do_request() function
so that urllib.request calls are intercepted and forwarded to the Flask test
client. This keeps the CLI code unmodified while giving real HTTP semantics
(status codes, JSON bodies, error shapes).

Test coverage (per spec):
  - post new thread (returns thread_id + post_id)
  - reply to existing thread (returns thread_id + post_id)
  - list with category filter
  - list with sort filter
  - list --limit
  - read advances cursor to thread's last_activity_at
  - read --format json
  - status tally (new_since_cursor count)
  - online command
  - cursor --set monotonic enforcement
  - cursor --set --force override
  - cursor --show
  - server-unreachable → exit 3
  - unknown-category → exit 2
  - missing agent_name → exit 2
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Discover the worktree root so we can import the CLI module
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent  # repo root (worktree root)

# Ensure tools/ and forum/ are on the path
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.forum as forum_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Try to import the real forum server for integration tests.
# If the forum package isn't importable (e.g. missing Flask), fall back to
# mock-only mode and skip integration fixtures gracefully.
# ---------------------------------------------------------------------------
try:
    from forum.db import init_db
    from forum.server import create_app
    _FORUM_AVAILABLE = True
except ImportError:
    _FORUM_AVAILABLE = False

SKIP_INTEGRATION = pytest.mark.skipif(
    not _FORUM_AVAILABLE,
    reason="forum server package not importable (Flask/deps missing)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path):
    """Temp ENGRAM_HOME with a minimal config.json."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "testbot", "forum": {"url": "http://localhost:59999"}}
    (home / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture(autouse=True)
def reset_forum_cli_cache(engram_home, monkeypatch):
    """Reset the CLI's lazy-cached forum URL and patch ENGRAM_HOME on each test."""
    # Reset the module-level URL cache
    forum_cli._FORUM_URL_CACHE = None

    # Patch ENGRAM_HOME + cursor paths in the module
    monkeypatch.setattr(forum_cli, "ENGRAM_HOME", str(engram_home))
    monkeypatch.setattr(
        forum_cli, "READ_CURSOR_PATH", str(engram_home / "forum-read-cursor.txt")
    )
    yield
    # Reset cache again after each test
    forum_cli._FORUM_URL_CACHE = None


@pytest.fixture
def flask_app(tmp_path):
    """Real Flask forum app with a tmp SQLite DB (integration tests only)."""
    if not _FORUM_AVAILABLE:
        pytest.skip("forum server not available")
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit_path)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def flask_client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def wired_cli(flask_client, engram_home):
    """Wire the CLI's HTTP calls through the Flask test client.

    Returns a helper that calls forum_cli with the given argv + optional stdin,
    captures stdout/stderr, and returns (stdout, stderr, exit_code).

    The Flask test client is patched onto forum_cli._do_request so that any
    urllib.request.Request → flask_client response. We also set FORUM_URL to
    a local URL so that _resolve_forum_url doesn't try to connect for real.
    """
    base_url = "http://testserver"

    # Override config to point at our fake URL
    config = {"agent_name": "testbot", "forum": {"url": base_url}}
    (engram_home / "config.json").write_text(json.dumps(config))
    forum_cli._FORUM_URL_CACHE = None

    def _patched_do_request(req, url):
        """Translate urllib.request.Request into a flask test_client call.

        Mirrors the error-handling contract of the real _do_request:
        exits with appropriate exit codes for HTTP errors.
        """
        # Extract relative path (strip base_url prefix)
        if url.startswith(base_url):
            path = url[len(base_url):]
        else:
            path = url

        method = req.get_method()
        headers = dict(req.headers) if req.headers else {}

        if method == "POST":
            data = req.data
            resp = flask_client.post(
                path,
                data=data,
                content_type=headers.get("Content-Type", "application/json"),
            )
        else:
            resp = flask_client.get(path)

        # Mirror _do_request error handling for non-2xx responses
        if resp.status_code >= 400:
            body = resp.data.decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", body)
            except (json.JSONDecodeError, ValueError):
                err_msg = body
            if resp.status_code == 404:
                print(f"forum: not found (404): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_NOT_FOUND)
            elif resp.status_code == 400:
                print(f"forum: validation error (400): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)
            else:
                print(f"forum: server error ({resp.status_code}): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)

        return json.loads(resp.data.decode("utf-8"))

    def run_cli(argv, stdin_text=None):
        """Run the CLI with given argv; return (stdout, stderr, exit_code)."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        stdin_str = stdin_text if stdin_text is not None else ""

        with mock.patch.object(forum_cli, "_do_request", side_effect=_patched_do_request), \
             mock.patch("sys.argv", ["forum"] + argv), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf), \
             mock.patch("sys.stdin", io.StringIO(stdin_str)):
            try:
                forum_cli.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else 0

        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

    return run_cli


# ---------------------------------------------------------------------------
# Integration tests (require flask_app)
# ---------------------------------------------------------------------------

@SKIP_INTEGRATION
class TestPostNewThread:
    def test_returns_thread_and_post_ids(self, wired_cli):
        stdout, stderr, code = wired_cli(
            ["post", "--category", "inter-agent", "--title", "Hello forum"],
            stdin_text="This is the first post.",
        )
        assert code == 0, f"stderr: {stderr}"
        assert "thread_id=" in stdout
        assert "post_id=" in stdout

    def test_thread_id_is_integer(self, wired_cli):
        stdout, stderr, code = wired_cli(
            ["post", "--category", "cold-start", "--title", "Cold start test"],
            stdin_text="Body text.",
        )
        assert code == 0, f"stderr: {stderr}"
        # Extract thread_id value
        import re
        m = re.search(r"thread_id=(\d+)", stdout)
        assert m is not None
        assert int(m.group(1)) > 0

    def test_unknown_category_exits_2(self, wired_cli):
        stdout, stderr, code = wired_cli(
            ["post", "--category", "does-not-exist", "--title", "Bad category"],
            stdin_text="Body.",
        )
        assert code == forum_cli.EXIT_VALIDATION, f"stdout: {stdout}, stderr: {stderr}"

    def test_empty_stdin_exits_2(self, wired_cli):
        stdout, stderr, code = wired_cli(
            ["post", "--category", "inter-agent", "--title", "No body"],
            stdin_text="",
        )
        assert code == forum_cli.EXIT_VALIDATION


@SKIP_INTEGRATION
class TestReply:
    def _make_thread(self, wired_cli):
        stdout, _, _ = wired_cli(
            ["post", "--category", "inter-agent", "--title", "OP thread"],
            stdin_text="Opening post body.",
        )
        import re
        m = re.search(r"thread_id=(\d+)", stdout)
        return int(m.group(1))

    def test_reply_returns_thread_and_post_ids(self, wired_cli):
        tid = self._make_thread(wired_cli)
        stdout, stderr, code = wired_cli(
            ["reply", str(tid)],
            stdin_text="This is a reply.",
        )
        assert code == 0, f"stderr: {stderr}"
        assert f"thread_id={tid}" in stdout
        assert "post_id=" in stdout

    def test_reply_to_nonexistent_thread_exits_4(self, wired_cli):
        stdout, stderr, code = wired_cli(
            ["reply", "99999"],
            stdin_text="Reply body.",
        )
        assert code == forum_cli.EXIT_NOT_FOUND


@SKIP_INTEGRATION
class TestList:
    def _post_thread(self, wired_cli, category="inter-agent", title="Test thread"):
        wired_cli(
            ["post", "--category", category, "--title", title],
            stdin_text="Thread body.",
        )

    def test_list_returns_threads(self, wired_cli):
        self._post_thread(wired_cli)
        stdout, stderr, code = wired_cli(["list"])
        assert code == 0, f"stderr: {stderr}"
        assert "THREADS" in stdout

    def test_list_category_filter(self, wired_cli):
        self._post_thread(wired_cli, category="cold-start", title="CS thread")
        self._post_thread(wired_cli, category="inter-agent", title="IA thread")

        stdout, _, code = wired_cli(["list", "--category", "cold-start"])
        assert code == 0
        assert "cold-start" in stdout
        # The inter-agent thread should not appear
        assert "IA thread" not in stdout

    def test_list_sort_new(self, wired_cli):
        self._post_thread(wired_cli)
        stdout, _, code = wired_cli(["list", "--sort", "new"])
        assert code == 0
        assert "THREADS" in stdout

    def test_list_limit(self, wired_cli):
        for i in range(3):
            self._post_thread(wired_cli, title=f"Thread {i}")
        stdout, _, code = wired_cli(["list", "--limit", "1"])
        assert code == 0
        # With --limit 1, exactly one thread entry
        import re
        entries = re.findall(r"#\d+", stdout)
        assert len(entries) == 1

    def test_list_json_format(self, wired_cli):
        self._post_thread(wired_cli)
        stdout, _, code = wired_cli(["list", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert "threads" in data
        assert isinstance(data["threads"], list)

    def test_list_does_not_advance_cursor(self, wired_cli, engram_home):
        """list must not touch the read cursor."""
        cursor_path = engram_home / "forum-read-cursor.txt"
        self._post_thread(wired_cli)
        before_exists = cursor_path.exists()
        wired_cli(["list"])
        after_exists = cursor_path.exists()
        # The cursor should not have been created by list
        assert not after_exists or (
            before_exists and cursor_path.read_text() == ""
        ), "list must not advance the read cursor"


@SKIP_INTEGRATION
class TestRead:
    def _make_thread(self, wired_cli, category="tools-hooks", title="Test thread"):
        stdout, _, _ = wired_cli(
            ["post", "--category", category, "--title", title],
            stdin_text="Initial post body.",
        )
        import re
        m = re.search(r"thread_id=(\d+)", stdout)
        return int(m.group(1))

    def test_read_displays_thread(self, wired_cli):
        tid = self._make_thread(wired_cli)
        stdout, stderr, code = wired_cli(["read", str(tid)])
        assert code == 0, f"stderr: {stderr}"
        assert "Thread" in stdout or str(tid) in stdout
        # Should display body_md content
        assert "Initial post body" in stdout

    def test_read_advances_cursor(self, wired_cli, engram_home):
        """read must post a server-side read watermark (v2 read-state).

        In v2, cmd_read posts the max post_id to /api/thread/<id>/read rather
        than writing a local cursor file.  A reply from another agent makes the
        thread appear unread; after testbot reads it the watermark clears it
        from the inbox (unread_total drops back to 0).
        """
        tid = self._make_thread(wired_cli)

        # Have otherbot reply so testbot has an unread post in the thread.
        with mock.patch.object(forum_cli, "_get_agent_name", return_value="otherbot"):
            wired_cli(["reply", str(tid)], stdin_text="Reply from otherbot.")

        # Before read: testbot sees the reply as unread
        stdout, _, _ = wired_cli(["status", "--format", "json"])
        before = json.loads(stdout)
        assert before["unread_total"] >= 1, (
            "reply from otherbot must appear unread before testbot reads the thread; "
            f"got unread_total={before['unread_total']!r}"
        )

        stdout, stderr, code = wired_cli(["read", str(tid)])
        assert code == 0, f"stderr: {stderr}"

        # After read: the watermark must clear the thread from testbot's inbox
        stdout, _, _ = wired_cli(["status", "--format", "json"])
        after = json.loads(stdout)
        assert after["unread_total"] == 0, (
            "read must post the server-side watermark so the thread leaves the inbox; "
            f"got unread_total={after['unread_total']!r}"
        )

    def test_read_cursor_is_monotonic(self, wired_cli, engram_home):
        """Reading multiple threads in any order clears each from the server-side inbox.

        In v2, read state is tracked server-side via per-thread watermarks.
        Reading thread2 (newer) before thread1 (older) must clear both — the
        watermark is per-thread, so reading an older thread does not undo the
        newer thread's watermark.
        """
        tid1 = self._make_thread(wired_cli, title="First thread")
        tid2 = self._make_thread(wired_cli, title="Second thread")

        # Have otherbot reply to both threads so testbot has unread content.
        with mock.patch.object(forum_cli, "_get_agent_name", return_value="otherbot"):
            wired_cli(["reply", str(tid1)], stdin_text="Reply to first.")
            wired_cli(["reply", str(tid2)], stdin_text="Reply to second.")

        # Read thread2 first (newer), then thread1 (older)
        _, _, code2 = wired_cli(["read", str(tid2)])
        assert code2 == 0, "read of thread2 must succeed"

        _, _, code1 = wired_cli(["read", str(tid1)])
        assert code1 == 0, "read of thread1 must succeed"

        # Both threads must be cleared from the inbox after reading each
        stdout, _, _ = wired_cli(["status", "--format", "json"])
        after = json.loads(stdout)
        assert after["unread_total"] == 0, (
            "reading both threads must clear them from the server-side inbox "
            "(per-thread watermarks are cumulative, not regressive); "
            f"got unread_total={after['unread_total']!r}"
        )

    def test_read_json_format(self, wired_cli):
        tid = self._make_thread(wired_cli)
        stdout, _, code = wired_cli(["read", str(tid), "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert "thread" in data
        assert "posts" in data
        # posts must have body_md (not body_html)
        for post in data["posts"]:
            assert "body_md" in post

    def test_read_nonexistent_thread_exits_4(self, wired_cli):
        stdout, stderr, code = wired_cli(["read", "99999"])
        assert code == forum_cli.EXIT_NOT_FOUND


@SKIP_INTEGRATION
class TestStatus:
    def test_status_shows_agent(self, wired_cli):
        stdout, stderr, code = wired_cli(["status"])
        assert code == 0, f"stderr: {stderr}"
        assert "testbot" in stdout

    def test_status_shows_url(self, wired_cli):
        stdout, _, _ = wired_cli(["status"])
        assert "testserver" in stdout or "http" in stdout

    def test_status_json_format(self, wired_cli):
        """status --format json emits the v2 read-state shape."""
        stdout, _, code = wired_cli(["status", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert "agent" in data
        assert "online_count" in data
        # v2 read-state keys (replaces old new_since_cursor)
        assert "unread_total" in data
        assert "unread_on_my_threads" in data
        assert "mention_count" in data
        assert "inbox" in data

    def test_status_new_count_increases_after_post(self, wired_cli, engram_home):
        """After another agent replies, status shows unread_total > 0 (v2 read-state).

        In v2, unread counts come from the server-side inbox endpoint
        (per-thread watermarks), not from a local time cursor.  testbot posts a
        thread; otherbot replies; status for testbot must then report
        unread_total >= 1.  After testbot reads the thread, unread_total drops
        back to 0.
        """
        # Fresh DB — no threads → 0 unread
        stdout, _, _ = wired_cli(["status", "--format", "json"])
        data = json.loads(stdout)
        assert data["unread_total"] == 0

        # testbot posts a thread; otherbot replies
        stdout, _, _ = wired_cli(
            ["post", "--category", "inter-agent", "--title", "New thread"],
            stdin_text="Body.",
        )
        import re as _re
        m = _re.search(r"thread_id=(\d+)", stdout)
        tid = int(m.group(1))

        with mock.patch.object(forum_cli, "_get_agent_name", return_value="otherbot"):
            wired_cli(["reply", str(tid)], stdin_text="Reply from otherbot.")

        # Status: otherbot's reply must appear unread for testbot
        stdout, _, _ = wired_cli(["status", "--format", "json"])
        data = json.loads(stdout)
        assert data["unread_total"] >= 1, (
            "otherbot's reply must be counted as unread by server-side watermark; "
            f"got unread_total={data['unread_total']!r}"
        )

    def test_status_mention_bell_end_to_end(self, wired_cli):
        """End-to-end: reply @-mentioning testbot appears as a bell line in status.

        This exercises the full CLI→mentions-endpoint→render wire-path through
        the wired_cli test fixture (i.e. _do_request is intercepted, not raw urlopen).
        """
        # testbot posts a thread
        stdout, _, _ = wired_cli(
            ["post", "--category", "inter-agent", "--title", "Mention target"],
            stdin_text="OP body.",
        )
        import re
        m = re.search(r"thread_id=(\d+)", stdout)
        tid = int(m.group(1))

        # A different agent (we simulate by making a direct API call via wired_cli
        # with a different agent_name) replies with @testbot.
        # We temporarily swap the agent name in config to "otherbot" for this post.
        import json as _json
        from pathlib import Path as _Path

        # Build a second wired_cli run as "otherbot" by temporarily patching config
        # We reuse the same flask_client underneath via the fixture's closure.
        # Simplest: call the reply endpoint directly through wired_cli with a
        # monkeypatched _get_agent_name.
        with mock.patch.object(forum_cli, "_get_agent_name", return_value="otherbot"):
            wired_cli(
                ["reply", str(tid)],
                stdin_text="Hey @testbot, great thread!",
            )

        # Now run status as testbot — the bell line must appear
        stdout, stderr, code = wired_cli(["status"])
        assert code == 0, f"stderr: {stderr}"
        assert "\U0001f514" in stdout, (
            "bell emoji must appear in status output when @testbot is mentioned; "
            f"got:\n{stdout}"
        )

    def test_status_json_includes_mentions_array(self, wired_cli):
        """status --format json emits mention_count and inbox (v2 shape, no top-level mentions array)."""
        stdout, _, code = wired_cli(["status", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        # v2 shape: mention info is in mention_count (int) and inbox items with kind=="at_mention"
        assert "mention_count" in data, "'mention_count' key must appear in status JSON output"
        assert isinstance(data["mention_count"], int), "'mention_count' must be an int"
        assert "inbox" in data, "'inbox' key must appear in status JSON output"
        assert isinstance(data["inbox"], list), "'inbox' must be a list"
        # When no mentions, mention_count must be 0
        assert data["mention_count"] == 0

    def test_status_json_includes_mentions_when_present(self, wired_cli):
        """status --format json populates mention_count and inbox when @testbot is mentioned."""
        # testbot posts a thread
        stdout, _, _ = wired_cli(
            ["post", "--category", "cold-start", "--title", "JSON mention test"],
            stdin_text="OP body.",
        )
        import re
        m = re.search(r"thread_id=(\d+)", stdout)
        tid = int(m.group(1))

        # otherbot replies with @testbot
        with mock.patch.object(forum_cli, "_get_agent_name", return_value="otherbot"):
            wired_cli(
                ["reply", str(tid)],
                stdin_text="Pinging @testbot about this.",
            )

        stdout, _, code = wired_cli(["status", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert data["mention_count"] >= 1, (
            "mention_count must be >= 1 after @testbot mention; "
            f"got mention_count={data['mention_count']!r}"
        )
        at_mention_items = [
            item for item in data["inbox"]
            if item.get("kind") == "at_mention"
        ]
        assert len(at_mention_items) >= 1, (
            "inbox must contain at least one at_mention item after @testbot mention; "
            f"inbox={data['inbox']!r}"
        )


@SKIP_INTEGRATION
class TestOnline:
    def test_online_shows_registered(self, wired_cli):
        # Posting as testbot registers it
        wired_cli(
            ["post", "--category", "inter-agent", "--title", "Online test"],
            stdin_text="Body.",
        )
        stdout, _, code = wired_cli(["online"])
        assert code == 0
        # Should mention at least 1 online (testbot just made a request)
        assert "registered" in stdout.lower() or "ONLINE" in stdout

    def test_online_json_format(self, wired_cli):
        stdout, _, code = wired_cli(["online", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert "online" in data
        assert "count" in data
        assert "registered" in data


# ---------------------------------------------------------------------------
# Cursor tests (local only — no server needed)
# ---------------------------------------------------------------------------

class TestCursor:
    def _set_cursor(self, engram_home, ts_str):
        cursor_path = engram_home / "forum-read-cursor.txt"
        cursor_path.write_text(ts_str + "\n")

    def test_cursor_show_when_none(self, engram_home):
        """cursor --show when no cursor exists prints '(none)'."""
        stdout_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "cursor", "--show"]), \
             mock.patch("sys.stdout", stdout_buf):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        assert "(none)" in stdout_buf.getvalue()

    def test_cursor_set_advances_forward(self, engram_home):
        """cursor --set with a forward timestamp succeeds."""
        ts = "2026-06-01T12:00:00Z"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "cursor", "--set", ts]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        assert code == 0, stderr_buf.getvalue()
        cursor_path = Path(engram_home / "forum-read-cursor.txt")
        assert cursor_path.exists()
        # _write_cursor now emits fixed-width 6-digit microseconds; check the
        # date+time prefix (without the trailing Z) so the assertion is
        # format-agnostic for the microseconds field.
        assert "2026-06-01T12:00:00" in cursor_path.read_text()

    def test_cursor_set_monotonic_refuses_backward(self, engram_home):
        """cursor --set refuses to move cursor backward without --force."""
        self._set_cursor(engram_home, "2026-06-01T12:00:00Z")
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "cursor", "--set", "2026-01-01T00:00:00Z"]), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        assert code == forum_cli.EXIT_VALIDATION
        assert "backward" in stderr_buf.getvalue()

    def test_cursor_set_force_allows_backward(self, engram_home):
        """cursor --set --force allows cursor to move backward."""
        self._set_cursor(engram_home, "2026-06-01T12:00:00Z")
        old_ts = "2026-01-01T00:00:00Z"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "cursor", "--set", old_ts, "--force"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        assert code == 0, stderr_buf.getvalue()
        cursor_path = Path(engram_home / "forum-read-cursor.txt")
        # _write_cursor now emits fixed-width 6-digit microseconds; check the
        # date+time prefix (without the trailing Z) so the assertion is
        # format-agnostic for the microseconds field.
        assert "2026-01-01T00:00:00" in cursor_path.read_text()

    def test_cursor_invalid_timestamp_exits_2(self, engram_home):
        """cursor --set with a non-ISO value exits 2."""
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "cursor", "--set", "not-a-date"]), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        assert code == forum_cli.EXIT_VALIDATION

    # --- regression tests for issue #663: cursor precision ---

    def test_cursor_roundtrip_preserves_microseconds(self, engram_home):
        """_write_cursor then _read_cursor round-trips microseconds exactly.

        Regression for #663: the old strftime("%Y-%m-%dT%H:%M:%SZ") truncated
        microseconds, so a sub-second timestamp was silently coarsened.
        """
        cursor_path = str(engram_home / "forum-read-cursor.txt")
        ts = datetime(2026, 6, 2, 12, 0, 0, 123456, tzinfo=timezone.utc)
        forum_cli._write_cursor(cursor_path, ts)
        recovered = forum_cli._read_cursor(cursor_path)
        assert recovered is not None
        assert recovered == ts, (
            f"round-trip must preserve microseconds; wrote {ts!r}, got {recovered!r}"
        )

    def test_cursor_str_microseconds_not_excluded_by_since_filter(self, engram_home):
        """The fixed cursor format preserves microseconds so the since-filter works correctly.

        The core bug in #663: the cursor was formatted as seconds-only
        '2026-06-02T12:00:00Z' (strftime truncation), while DB timestamps carry
        microseconds like '2026-06-02T12:00:00.500000Z'.  Lexicographically
        '.' (0x2E) < 'Z' (0x5A), so '...00.500000Z' sorts BEFORE '...00Z',
        meaning a DB `WHERE created_at > '...00Z'` silently EXCLUDES a post at
        '...00.500000Z' even though it is temporally later.

        After the fix, the cursor stores its microseconds (e.g. '...00.123456Z').
        A post at '...00.500000Z' satisfies '...00.500000Z' > '...00.123456Z'
        because after the common '...00.' prefix the digit comparison is correct.

        This test constructs the bug directly: the cursor was advanced to a
        datetime with non-zero microseconds (e.g. 123456 µs), which the OLD code
        would truncate to '...00Z', losing the sub-second component.  We verify
        that the FIXED _cursor_str keeps the microseconds so that a subsequent
        post at 500000 µs correctly sorts AFTER the cursor.
        """
        # Simulate cursor advanced to a datetime with non-zero microseconds
        # (e.g. the thread's last_activity_at was at 123456 µs into the second).
        cursor_ts = datetime(2026, 6, 2, 12, 0, 0, 123456, tzinfo=timezone.utc)
        cursor_str = forum_cli._cursor_str(cursor_ts)

        # Fixed: microseconds are preserved in the output
        assert cursor_str == "2026-06-02T12:00:00.123456Z", (
            f"_cursor_str must preserve microseconds after fix; got {cursor_str!r}"
        )

        # A post at 500000 µs in the same second — must sort AFTER the cursor
        # so the since-filter `WHERE created_at > cursor_str` includes it.
        post_ts_str = "2026-06-02T12:00:00.500000Z"
        assert cursor_str < post_ts_str, (
            f"Post at {post_ts_str!r} must sort AFTER cursor {cursor_str!r} "
            f"(since-filter includes it); '.' prefix comparison must work correctly"
        )

        # Also verify the pre-fix behavior: old seconds-only cursor string would
        # have caused the post to be EXCLUDED (sorts before the cursor).
        old_cursor_str = "2026-06-02T12:00:00Z"
        post_same_second_str = "2026-06-02T12:00:00.000123Z"
        # '.' (0x2E) < 'Z' (0x5A) → sub-second post sorts BEFORE seconds-only cursor
        assert post_same_second_str < old_cursor_str, (
            "Pre-fix demonstration: a sub-second post sorts BEFORE a seconds-only cursor; "
            "this is the lexicographic ordering bug that the fix eliminates by ensuring "
            "the cursor carries microseconds matching the DB timestamp format"
        )

    def test_cursor_backward_compat_seconds_only_string_parses(self, engram_home):
        """A legacy seconds-only cursor file ('...T12:00:00Z') still parses correctly.

        _read_cursor uses datetime.fromisoformat which handles both formats,
        so old cursor files written before this fix remain readable.
        """
        cursor_path = engram_home / "forum-read-cursor.txt"
        # Write old-format seconds-only string directly (no microseconds)
        cursor_path.write_text("2026-05-01T10:30:00Z\n")
        recovered = forum_cli._read_cursor(str(cursor_path))
        assert recovered is not None
        expected = datetime(2026, 5, 1, 10, 30, 0, 0, tzinfo=timezone.utc)
        assert recovered == expected, (
            f"seconds-only legacy cursor must parse correctly; got {recovered!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests: error handling + agent-name gate (no server needed)
# ---------------------------------------------------------------------------

class TestAgentNameGate:
    def test_missing_agent_name_exits_2(self, engram_home, monkeypatch):
        """Commands that need an agent fail-loud with EXIT_VALIDATION."""
        # Remove agent_name from config
        (engram_home / "config.json").write_text(
            json.dumps({"forum": {"url": "http://localhost:59999"}})
        )
        forum_cli._FORUM_URL_CACHE = None

        # Patch env vars to ensure no username fallback
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        # Also patch _get_agent_name to ensure no pwd fallback
        monkeypatch.setattr(forum_cli, "_get_agent_name", lambda *a, **kw: "")

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "online"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        assert code == forum_cli.EXIT_VALIDATION
        assert "agent_name" in stderr_buf.getvalue()


class TestServerUnreachable:
    def test_unreachable_exits_3(self, engram_home, monkeypatch):
        """When the server is down, any network command exits EXIT_UNREACHABLE."""
        # Point at a port nothing is listening on
        (engram_home / "config.json").write_text(
            json.dumps({"agent_name": "testbot", "forum": {"url": "http://localhost:59999"}})
        )
        forum_cli._FORUM_URL_CACHE = None

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "online"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code

        assert code == forum_cli.EXIT_UNREACHABLE
        err = stderr_buf.getvalue()
        assert "not reachable" in err
        assert "localhost:59999" in err


class TestUrlResolution:
    """Unit tests for _resolve_forum_url resolution order."""

    def test_config_wins_over_env(self, engram_home, monkeypatch):
        forum_cli._FORUM_URL_CACHE = None
        config = {"agent_name": "x", "forum": {"url": "http://from-config:5002"}}
        (engram_home / "config.json").write_text(json.dumps(config))
        monkeypatch.setenv("FORUM_URL", "http://from-env:5002")
        url = forum_cli._resolve_forum_url()
        assert url == "http://from-config:5002"
        forum_cli._FORUM_URL_CACHE = None

    def test_env_wins_over_default(self, engram_home, monkeypatch):
        forum_cli._FORUM_URL_CACHE = None
        config = {"agent_name": "x"}
        (engram_home / "config.json").write_text(json.dumps(config))
        monkeypatch.setenv("FORUM_URL", "http://from-env:5002")
        url = forum_cli._resolve_forum_url()
        assert url == "http://from-env:5002"
        forum_cli._FORUM_URL_CACHE = None

    def test_default_fallback(self, engram_home, monkeypatch):
        forum_cli._FORUM_URL_CACHE = None
        config = {"agent_name": "x"}
        (engram_home / "config.json").write_text(json.dumps(config))
        monkeypatch.delenv("FORUM_URL", raising=False)
        url = forum_cli._resolve_forum_url()
        assert url == "http://localhost:5002"
        forum_cli._FORUM_URL_CACHE = None


# ---------------------------------------------------------------------------
# Unit tests: fix regressions (no server needed)
# ---------------------------------------------------------------------------

class TestStatusHintWhenCursorUnset:
    """status --since hint must not emit '--since (none)' when cursor is unset."""

    def _run_status(self, engram_home, monkeypatch):
        """Run `forum status` with a mocked _api_get that returns empty data."""
        # Patch _api_get to return minimal stub data so status doesn't need a server
        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/threads" in url:
                return {"threads": []}
            if "/api/agents/online" in url:
                return {"count": 0, "registered": 0}
            return {}

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        return stdout_buf.getvalue()

    def test_no_since_none_hint_when_cursor_unset(self, engram_home, monkeypatch):
        """When read cursor is unset, status must not suggest --since (none)."""
        # Cursor file does not exist — cursor is None
        cursor_path = engram_home / "forum-read-cursor.txt"
        assert not cursor_path.exists(), "cursor must not exist for this test"

        # Patch _api_get to return one thread so the hint branch fires
        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/threads" in url:
                return {"threads": [{"id": 1, "title": "x", "last_activity_at": "2026-05-31T10:00:00Z"}]}
            if "/api/agents/online" in url:
                return {"count": 1, "registered": 1}
            return {}

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        output = stdout_buf.getvalue()
        assert "--since (none)" not in output, (
            f"status must not emit '--since (none)' when cursor is unset; got:\n{output}"
        )

    def test_since_hint_present_when_cursor_is_set(self, engram_home):
        """status exits 0 and renders v2 unread fields regardless of local cursor state.

        In v2, forum status reads state from the server-side inbox endpoint;
        the local forum-read-cursor.txt is not used by cmd_status.  This test
        verifies that having a cursor file does not break status and that the
        v2 output shape (agent, unread, @mentions) is present.
        """
        # Write a cursor file — v2 status must not error on it
        ts = "2026-05-01T00:00:00Z"
        (engram_home / "forum-read-cursor.txt").write_text(ts + "\n")

        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/agents/online" in url:
                return {"count": 1, "registered": 1}
            if "/inbox" in url:
                return {"inbox": [], "unread_all": 0}
            return {}

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        output = stdout_buf.getvalue()
        assert "testbot" in output, (
            f"status must render agent name in v2 output; got:\n{output}"
        )
        assert "unread" in output, (
            f"status must render unread count in v2 output; got:\n{output}"
        )
        assert "@mentions" in output, (
            f"status must render @mentions count in v2 output; got:\n{output}"
        )


class TestStatusMentions:
    """cmd_status mention-line rendering (patches _api_get + _fetch_inbox for v2)."""

    def _run_status(self, engram_home, monkeypatch, inbox_items, inbox_fetch_fails=False,
                    cursor_ts=None):
        """Run `forum status` with stubbed API helpers; return (stdout, stderr).

        Patches _fetch_inbox directly (v2 read-state path) so the mention-line
        rendering can be exercised without a live server.

        inbox_items  -- list of inbox item dicts to return from _fetch_inbox
                        (must have 'kind' field: 'at_mention' or 'reply_on_my_thread').
        inbox_fetch_fails -- if True, _fetch_inbox returns None (simulates server failure).
        cursor_ts    -- optional local cursor timestamp (written to cursor file;
                        v2 status does not use it, but should not error on it).
        """
        if cursor_ts:
            (engram_home / "forum-read-cursor.txt").write_text(cursor_ts + "\n")

        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/agents/online" in url:
                return {"count": 1, "registered": 1}
            return {}

        if inbox_fetch_fails:
            fake_inbox = None
        else:
            fake_inbox = {"inbox": inbox_items, "unread_all": len(inbox_items)}

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch.object(forum_cli, "_fetch_inbox",
                               return_value=fake_inbox), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        return stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_mention_line_rendered_when_mentions_present(self, engram_home, monkeypatch):
        """When at_mention inbox items are present, a bell summary line is printed.

        In v2, cmd_status calls _format_mention_line with items whose kind=='at_mention'.
        Items with kind=='reply_on_my_thread' are counted separately (unread_on_my_threads)
        and do not appear in the bell line.
        """
        inbox_items = [
            {
                "thread_id": 3,
                "thread_title": "debugging ground-rules",
                "post_id": 7,
                "author": "luria",
                "kind": "at_mention",
                "created_at": "2026-06-01T10:00:00Z",
            },
            {
                "thread_id": 1,
                "thread_title": "first-retractions",
                "post_id": 2,
                "author": "borges",
                "kind": "at_mention",
                "created_at": "2026-06-01T10:01:00Z",
            },
        ]
        stdout, _ = self._run_status(engram_home, monkeypatch, inbox_items=inbox_items)
        assert "\U0001f514" in stdout, "bell emoji must appear in mention line"
        assert "2 posts waiting on you" in stdout
        assert "debugging ground-rules" in stdout
        assert "@mention by luria" in stdout
        assert "@mention by borges" in stdout

    def test_no_mention_line_when_zero_mentions(self, engram_home, monkeypatch):
        """Zero inbox items → no bell mention line in output."""
        stdout, _ = self._run_status(engram_home, monkeypatch, inbox_items=[])
        assert "\U0001f514" not in stdout

    def test_no_mention_line_when_fetch_fails(self, engram_home, monkeypatch):
        """When _fetch_inbox returns None (server unreachable), no mention line."""
        stdout, _ = self._run_status(engram_home, monkeypatch, inbox_items=[],
                                     inbox_fetch_fails=True)
        assert "\U0001f514" not in stdout

    def test_status_exit_code_0_when_mention_fetch_fails(self, engram_home, monkeypatch):
        """An inbox-fetch failure must not change the status exit code."""
        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/agents/online" in url:
                return {"count": 0, "registered": 0}
            return {}

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch.object(forum_cli, "_fetch_inbox", return_value=None), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        assert code == 0, f"status must exit 0 even when inbox fetch fails; stderr: {stderr_buf.getvalue()}"

    def test_fetch_inbox_endpoint_failure_returns_none(self, engram_home, monkeypatch):
        """_fetch_inbox returns None (not sys.exit) when the inbox endpoint fails.

        This tests the advisory-failure contract at the _fetch_inbox level:
        a 4xx/5xx from the inbox endpoint must not propagate as sys.exit().
        """
        calls: list = []

        def fake_do_request(req, url):
            calls.append(url)
            if "/api/agent/" in url and "/inbox" in url:
                sys.exit(forum_cli.EXIT_NOT_FOUND)
            return {}

        result = None
        with mock.patch.object(forum_cli, "_do_request", side_effect=fake_do_request):
            result = forum_cli._fetch_inbox(
                "http://testserver", "testbot"
            )

        assert result is None, (
            "_fetch_inbox must return None when the endpoint returns an error, "
            f"not propagate sys.exit; got: {result!r}"
        )

    def test_status_json_mentions_empty_on_fetch_failure(self, engram_home, monkeypatch):
        """status --format json emits mention_count=0 and inbox=[] when inbox fetch fails."""
        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/agents/online" in url:
                return {"count": 0, "registered": 0}
            return {}

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch.object(forum_cli, "_fetch_inbox", return_value=None), \
             mock.patch("sys.argv", ["forum", "status", "--format", "json"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0

        assert code == 0
        data = json.loads(stdout_buf.getvalue())
        assert "mention_count" in data, "JSON output must include 'mention_count' key"
        assert data["mention_count"] == 0, (
            "mention_count must be 0 when inbox fetch failed; got: {!r}".format(data["mention_count"])
        )
        assert "inbox" in data, "JSON output must include 'inbox' key"
        assert data["inbox"] == [], (
            "inbox must be [] when fetch failed; got: {!r}".format(data["inbox"])
        )

    def test_mention_truncation_at_5(self, engram_home, monkeypatch):
        """More than 5 at_mention inbox items are truncated with '+N more' suffix."""
        inbox_items = [
            {
                "thread_id": i,
                "thread_title": f"Thread {i}",
                "post_id": i + 100,
                "author": "borges",
                "kind": "at_mention",
                "created_at": f"2026-06-01T10:0{i}:00Z",
            }
            for i in range(7)
        ]
        stdout, _ = self._run_status(engram_home, monkeypatch, inbox_items=inbox_items)
        assert "+2 more" in stdout

    def test_fetch_inbox_called_with_agent_name(self, engram_home, monkeypatch):
        """_fetch_inbox is called with the agent name (v2 read-state; no client cursor).

        In v2, cmd_status calls _fetch_inbox(forum_url, agent_name) — the server
        is responsible for determining what is unread.  The local read cursor is
        not passed to the inbox endpoint.
        """
        captured = {}

        def fake_api_get(url, params=None, swallow_errors=False):
            if "/api/agents/online" in url:
                return {"count": 0, "registered": 0}
            return {}

        def fake_fetch_inbox(forum_url, agent_name):
            captured["agent_name"] = agent_name
            return {"inbox": [], "unread_all": 0}

        cursor_ts = "2026-05-01T00:00:00Z"
        (engram_home / "forum-read-cursor.txt").write_text(cursor_ts + "\n")

        with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
             mock.patch.object(forum_cli, "_fetch_inbox",
                               side_effect=fake_fetch_inbox), \
             mock.patch("sys.argv", ["forum", "status"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        assert captured.get("agent_name") == "testbot", (
            f"_fetch_inbox must be called with the agent name; "
            f"got agent_name={captured.get('agent_name')!r}"
        )


class TestHookCursorNotAdvancedOnUnreachableNoCache:
    """Hook must not advance the surfaced cursor when server is unreachable and no cache exists."""

    @classmethod
    def _load_hook_module(cls):
        """Load engram-forum-prompt-hook.py via importlib (hyphenated filename).

        The hook moved from hooks/claude/ to src/engram/hooks/claude/ in the
        Phase 3/4 restructure (#1093/#1116).
        """
        import importlib.util
        hook_path = (
            Path(__file__).parent.parent
            / "src" / "engram" / "hooks" / "claude" / "engram-forum-prompt-hook.py"
        )
        spec = importlib.util.spec_from_file_location("engram_forum_prompt_hook", hook_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cursor_not_advanced_on_unreachable_no_cache(self, tmp_path, monkeypatch):
        """When server is unreachable and no cache exists, surfaced cursor must not advance."""
        hook = self._load_hook_module()

        # Set up a temp ENGRAM_HOME so the hook reads config from it
        engram_home = tmp_path / "hook-engram"
        engram_home.mkdir()
        config = {"agent_name": "testbot", "forum": {"url": "http://localhost:59998"}}
        (engram_home / "config.json").write_text(json.dumps(config))

        surfaced_cursor_path = str(engram_home / "forum-surfaced-cursor.txt")
        hook_cache_path = str(engram_home / "forum-hook-cache.json")

        # Patch module-level paths
        monkeypatch.setattr(hook, "ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", surfaced_cursor_path)
        monkeypatch.setattr(hook, "HOOK_CACHE_PATH", hook_cache_path)

        # No cache file exists; _fetch_threads returns None (server unreachable)
        monkeypatch.setattr(hook, "_fetch_threads", lambda *a, **kw: None)
        # _load_cache returns None (no fresh cache)
        monkeypatch.setattr(hook, "_load_cache", lambda: None)

        # Run the hook's main — should exit silently
        try:
            hook.main()
        except SystemExit:
            pass

        # Surfaced cursor must NOT have been written
        assert not Path(surfaced_cursor_path).exists(), (
            "hook must not advance the surfaced cursor when server is unreachable "
            "and no cache exists (threads is None)"
        )

    def test_cursor_advanced_on_successful_fetch(self, tmp_path, monkeypatch):
        """When a successful fetch returns threads (even empty list), cursor IS advanced."""
        hook = self._load_hook_module()

        engram_home = tmp_path / "engram2"
        engram_home.mkdir()
        config = {"agent_name": "testbot", "forum": {"url": "http://localhost:59998"}}
        (engram_home / "config.json").write_text(json.dumps(config))

        surfaced_cursor_path = str(engram_home / "forum-surfaced-cursor.txt")
        hook_cache_path = str(engram_home / "forum-hook-cache.json")

        monkeypatch.setattr(hook, "ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", surfaced_cursor_path)
        monkeypatch.setattr(hook, "HOOK_CACHE_PATH", hook_cache_path)

        # Successful fetch returns empty list (no new threads — hook exits silently)
        monkeypatch.setattr(hook, "_fetch_threads", lambda *a, **kw: [])
        monkeypatch.setattr(hook, "_load_cache", lambda: None)
        # _save_cache is a no-op for this test
        monkeypatch.setattr(hook, "_save_cache", lambda threads, mentions: None)

        try:
            hook.main()
        except SystemExit:
            pass

        # Surfaced cursor SHOULD have been written (threads is not None — empty list is fine)
        assert Path(surfaced_cursor_path).exists(), (
            "hook must advance the surfaced cursor when fetch succeeds (even with 0 threads)"
        )


# ---------------------------------------------------------------------------
# Pack CLI tests
# ---------------------------------------------------------------------------

import io as _io_module
import tarfile as _tarfile_module

# Minimal knowledge.sql — empty graph, trivially closure-complete.
_PACK_MINIMAL_SQL = """\
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT,
    claim TEXT,
    confidence REAL,
    evidence_id TEXT,
    source_class TEXT,
    quote_type TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    reasoning_type TEXT,
    logical_chain TEXT,
    tags TEXT,
    author TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT
);
"""


def _make_test_pack_dir(tmp_path, sql_content=None):
    """Create a minimal engram-package directory in tmp_path/pkg.

    Returns the path to the package directory.
    """
    pkg = tmp_path / "test-pack"
    pkg.mkdir()
    (pkg / "knowledge.sql").write_text(
        sql_content if sql_content is not None else _PACK_MINIMAL_SQL,
        encoding="utf-8",
    )
    scripts = pkg / "scripts"
    scripts.mkdir()
    (scripts / "build.sh").write_text("#!/bin/bash\n# stub\n")
    (scripts / "dump.sh").write_text("#!/bin/bash\n# stub\n")
    (pkg / "README.md").write_text("# Test Pack\n")
    return pkg


@pytest.fixture
def flask_app_with_packs(tmp_path):
    """Real Flask forum app with a temp DB and packs_dir (pack integration tests)."""
    if not _FORUM_AVAILABLE:
        pytest.skip("forum server not available")
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    packs_dir = str(tmp_path / "packs")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit_path, packs_dir=packs_dir)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def wired_cli_packs(flask_app_with_packs, engram_home):
    """wired_cli variant that supports multipart uploads for pack tests.

    Extends the standard _patched_do_request to also handle multipart POST
    (used by forum pack publish).  The multipart body is passed as-is to the
    Flask test client which handles it natively.
    """
    flask_client_packs = flask_app_with_packs.test_client()
    base_url = "http://testserver"

    config = {"agent_name": "testbot", "forum": {"url": base_url}}
    (engram_home / "config.json").write_text(json.dumps(config))
    forum_cli._FORUM_URL_CACHE = None

    def _patched_do_request(req, url):
        if url.startswith(base_url):
            path = url[len(base_url):]
        else:
            path = url

        method = req.get_method()
        # urllib.request lowercases the 't' in Content-Type → "Content-type".
        # Use case-insensitive lookup to avoid falling back to application/json.
        headers_raw = dict(req.headers) if req.headers else {}
        headers_lower = {k.lower(): v for k, v in headers_raw.items()}
        content_type = headers_lower.get("content-type", "application/json")

        if method == "POST":
            data = req.data
            resp = flask_client_packs.post(
                path,
                data=data,
                content_type=content_type,
            )
        else:
            resp = flask_client_packs.get(path)

        if resp.status_code >= 400:
            body = resp.data.decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", body)
            except (json.JSONDecodeError, ValueError):
                err_msg = body
            if resp.status_code == 404:
                print(f"forum: not found (404): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_NOT_FOUND)
            elif resp.status_code == 400:
                print(f"forum: validation error (400): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)
            else:
                print(f"forum: server error ({resp.status_code}): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)

        return json.loads(resp.data.decode("utf-8"))

    def _patched_do_binary_request(req, url):
        """Handle binary downloads (pack get) via the test client."""
        if url.startswith(base_url):
            path = url[len(base_url):]
        else:
            path = url

        resp = flask_client_packs.get(path)

        if resp.status_code >= 400:
            body = resp.data.decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", body)
            except (json.JSONDecodeError, ValueError):
                err_msg = body
            if resp.status_code == 404:
                print(f"forum: not found (404): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_NOT_FOUND)
            else:
                print(f"forum: server error ({resp.status_code}): {err_msg}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)

        return resp.data

    def run_cli(argv, stdin_text=None):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        stdin_str = stdin_text if stdin_text is not None else ""

        with mock.patch.object(forum_cli, "_do_request", side_effect=_patched_do_request), \
             mock.patch.object(forum_cli, "_do_binary_request",
                               side_effect=_patched_do_binary_request), \
             mock.patch("sys.argv", ["forum"] + argv), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf), \
             mock.patch("sys.stdin", io.StringIO(stdin_str)):
            try:
                forum_cli.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else 0

        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

    return run_cli


@SKIP_INTEGRATION
class TestPackCLIPublish:
    def test_pack_publish_exits_0(self, wired_cli_packs, tmp_path):
        pkg_dir = _make_test_pack_dir(tmp_path)
        stdout, stderr, code = wired_cli_packs(["pack", "publish", str(pkg_dir)])
        assert code == 0, f"stderr: {stderr}"

    def test_pack_publish_prints_pack_id(self, wired_cli_packs, tmp_path):
        pkg_dir = _make_test_pack_dir(tmp_path)
        stdout, stderr, code = wired_cli_packs(["pack", "publish", str(pkg_dir)])
        assert code == 0, f"stderr: {stderr}"
        assert "pack_id=" in stdout

    def test_pack_publish_missing_dir_exits_2(self, wired_cli_packs, tmp_path):
        stdout, stderr, code = wired_cli_packs(
            ["pack", "publish", str(tmp_path / "does-not-exist")]
        )
        assert code == forum_cli.EXIT_VALIDATION


@SKIP_INTEGRATION
class TestPackCLIList:
    def test_pack_list_exits_0(self, wired_cli_packs):
        stdout, stderr, code = wired_cli_packs(["pack", "list"])
        assert code == 0, f"stderr: {stderr}"

    def test_pack_list_empty_initially(self, wired_cli_packs):
        stdout, stderr, code = wired_cli_packs(["pack", "list"])
        assert code == 0
        assert "no packs" in stdout.lower()

    def test_pack_list_shows_pack_after_publish(self, wired_cli_packs, tmp_path):
        pkg_dir = _make_test_pack_dir(tmp_path)
        wired_cli_packs(["pack", "publish", str(pkg_dir)])
        stdout, stderr, code = wired_cli_packs(["pack", "list"])
        assert code == 0, f"stderr: {stderr}"
        assert "PACKS" in stdout

    def test_pack_list_json_format(self, wired_cli_packs):
        stdout, stderr, code = wired_cli_packs(["pack", "list", "--format", "json"])
        assert code == 0
        data = json.loads(stdout)
        assert "packs" in data


@SKIP_INTEGRATION
class TestPackCLIGet:
    def test_pack_get_after_publish(self, wired_cli_packs, tmp_path):
        # Publish first.
        pkg_dir = _make_test_pack_dir(tmp_path)
        pub_stdout, _, pub_code = wired_cli_packs(["pack", "publish", str(pkg_dir)])
        assert pub_code == 0, f"publish failed: {pub_stdout}"

        # Extract pack_id from output.
        import re
        m = re.search(r"pack_id=(\S+)", pub_stdout)
        assert m is not None, f"pack_id not in output: {pub_stdout}"
        pack_id = m.group(1)

        # Download and extract the pack.
        out_dir = str(tmp_path / "extracted")
        stdout, stderr, code = wired_cli_packs(
            ["pack", "get", pack_id, "--out", out_dir]
        )
        assert code == 0, f"stderr: {stderr}"
        assert "extracted" in stdout

    def test_pack_get_unknown_exits_4(self, wired_cli_packs, tmp_path):
        out_dir = str(tmp_path / "out")
        stdout, stderr, code = wired_cli_packs(
            ["pack", "get", "does-not-exist-v99", "--out", out_dir]
        )
        assert code == forum_cli.EXIT_NOT_FOUND
