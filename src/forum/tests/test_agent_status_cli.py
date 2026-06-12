"""Tests for agent-status CLI (slice 2 of #956).

Covers:
1. status-publish: builds the correct payload and POSTs it.
2. status-publish: argparse rejects invalid --state (SystemExit).
3. board text render: mixed board (working/sleeping/offline) renders correct cells;
   offline shows '—' activity; stale online shows '(stale)'.
4. board --format json: dumps the raw payload.
5. online enriched line includes state + activity.
6. Repeatable --queue collects into a list.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Import the CLI module — mirror test_discovery.py style
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent.parent  # repo root

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import tools.forum as forum_cli
    # _status_derive is a sibling of tools/forum.py; forum.py adds tools/ to
    # sys.path at import time, so _status_derive is importable after forum_cli
    # loads and is registered in sys.modules as '_status_derive'.
    import _status_derive
    _CLI_AVAILABLE = True
except ImportError:
    _status_derive = None  # type: ignore[assignment]
    _CLI_AVAILABLE = False

SKIP_CLI = pytest.mark.skipif(
    not _CLI_AVAILABLE,
    reason="tools.forum CLI not importable",
)

# ---------------------------------------------------------------------------
# Import forum app for live Flask round-trips
# ---------------------------------------------------------------------------

from forum.db import (
    init_db,
    set_agent_status,
    upsert_agent,
)
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Flask app backed by a temp file DB."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    c = sqlite3.connect(db_path)
    init_db(c)
    c.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def engram_home(tmp_path):
    """Temp ENGRAM_HOME with a minimal config.json pointing at testserver."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "alice", "forum": {"url": "http://testserver"}}
    (home / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture(autouse=True)
def reset_forum_cli_cache(engram_home, monkeypatch):
    """Clear the forum URL cache + remap ENGRAM_HOME before each test."""
    if not _CLI_AVAILABLE:
        yield
        return
    forum_cli._FORUM_URL_CACHE = None
    monkeypatch.setattr(forum_cli, "ENGRAM_HOME", str(engram_home))
    monkeypatch.setattr(
        forum_cli, "READ_CURSOR_PATH", str(engram_home / "forum-read-cursor.txt")
    )
    yield
    forum_cli._FORUM_URL_CACHE = None


# ---------------------------------------------------------------------------
# Wire-CLI helper — routes HTTP calls through a Flask test client
# ---------------------------------------------------------------------------

def _make_wired_cli(flask_client):
    """Build a run_cli helper that routes HTTP through the Flask test client.

    Patches forum_cli._do_request to forward GET/POST to the Flask client.
    Returns run_cli(argv, stdin_text=None) -> (stdout, stderr, exit_code).
    """
    base_url = "http://testserver"

    def _patched_do_request(req, url):
        path = url[len(base_url):] if url.startswith(base_url) else url
        method = req.get_method()
        if method == "POST":
            data = req.data
            ct = dict(req.headers).get("Content-Type", "application/json")
            resp = flask_client.post(path, data=data, content_type=ct)
        else:
            resp = flask_client.get(path)

        if resp.status_code == 404:
            body = resp.data.decode("utf-8", errors="replace")
            print(f"forum: not found (404): {body}", file=sys.stderr)
            sys.exit(forum_cli.EXIT_NOT_FOUND)
        elif resp.status_code == 400:
            body = resp.data.decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", body)
            except (json.JSONDecodeError, ValueError):
                err_msg = body
            print(f"forum: validation error (400): {err_msg}", file=sys.stderr)
            sys.exit(forum_cli.EXIT_VALIDATION)
        elif resp.status_code >= 500:
            body = resp.data.decode("utf-8", errors="replace")
            print(f"forum: server error ({resp.status_code}): {body}", file=sys.stderr)
            sys.exit(forum_cli.EXIT_VALIDATION)

        return json.loads(resp.data.decode("utf-8"))

    def run_cli(argv, stdin_text=None):
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
# 1. status-publish: correct payload is POSTed
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestStatusPublishPayload:
    """status-publish builds the correct payload and sends it as a POST."""

    def test_publish_working_with_activity_and_queue(self, client):
        """Full publish call — state/activity/queue all land in the POST."""
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli([
            "status-publish",
            "--state", "working",
            "--activity", "reviewing PR #1005",
            "--queue", "#994",
            "--queue", "#1005 review",
        ])
        assert code == 0, f"stderr: {err}"
        # Confirmation line must mention the agent and state.
        assert "alice" in out
        assert "working" in out

    def test_publish_idle_no_activity_no_queue(self, client):
        """Minimal publish with only --state."""
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-publish", "--state", "idle"])
        assert code == 0, f"stderr: {err}"
        assert "alice" in out
        assert "idle" in out

    def test_publish_sleeping(self, client):
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-publish", "--state", "sleeping"])
        assert code == 0, f"stderr: {err}"
        assert "sleeping" in out

    def test_publish_payload_stub(self):
        """Stub _api_post to assert the exact payload dict delivered."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["url"] = url
            captured["payload"] = payload
            return {"status": "published", "agent": "alice", "state": "working"}

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", [
                 "forum", "status-publish",
                 "--state", "working",
                 "--activity", "deep work",
                 "--queue", "item-a",
                 "--queue", "item-b",
             ]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        # The POST target itself is externally-visible contract: a typo in the
        # endpoint path would otherwise pass every payload assertion silently.
        assert captured.get("url", "").endswith("/api/agents/status"), (
            f"unexpected publish URL: {captured.get('url')!r}"
        )
        assert captured.get("payload") == {
            "agent": "alice",
            "state": "working",
            "activity": "deep work",
            "queue": ["item-a", "item-b"],
        }, f"unexpected payload: {captured.get('payload')}"

    def test_publish_payload_empty_queue_when_no_queue_flag(self):
        """When --queue is not passed, queue in payload is []."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["payload"] = payload
            return {"status": "published", "agent": "alice", "state": "idle"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", ["forum", "status-publish", "--state", "idle"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["payload"]["queue"] == []

    def test_publish_activity_none_when_not_given(self):
        """When --activity is not passed, activity in payload is None."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["payload"] = payload
            return {"status": "published", "agent": "alice", "state": "idle"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", ["forum", "status-publish", "--state", "idle"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["payload"]["activity"] is None


# ---------------------------------------------------------------------------
# 2. status-publish: argparse rejects invalid --state
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestStatusPublishStateValidation:
    """Argparse must enforce the enum and exit non-zero on bad --state."""

    def _run_local(self, argv):
        """Run with no HTTP wiring needed (argparse exits before any request)."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum"] + argv), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

    def test_offline_state_rejected_by_argparse(self):
        """'offline' is not in the choices; argparse must exit non-zero."""
        _, err, code = self._run_local(["status-publish", "--state", "offline"])
        assert code != 0, "expected non-zero exit for invalid state 'offline'"

    def test_bogus_state_rejected_by_argparse(self):
        """Arbitrary strings are rejected by argparse."""
        _, err, code = self._run_local(["status-publish", "--state", "flying"])
        assert code != 0

    def test_missing_state_rejected(self):
        """--state is required; omitting it must exit non-zero."""
        _, err, code = self._run_local(["status-publish"])
        assert code != 0

    def test_valid_idle_passes_argparse(self):
        """'idle' must NOT be rejected by argparse (only rejected by the server
        if there is a server-side reason, but argparse itself should accept it)."""
        # We just test argparse acceptance — stub the post to avoid network.
        captured = {}

        def _fake_post(url, payload):
            captured["called"] = True
            return {"status": "published", "agent": "alice", "state": "idle"}

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", ["forum", "status-publish", "--state", "idle"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        assert code == 0
        assert captured.get("called"), "argparse should accept 'idle'"


# ---------------------------------------------------------------------------
# 3. board text render
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestBoardTextRender:
    """board text render: mixed board (working/sleeping/offline) — correct cells."""

    def _board_data_mixed(self):
        """Synthetic board response for a mixed working/sleeping/offline scenario."""
        return {
            "board": [
                {
                    "name": "alice",
                    "avatar_seed": "alice",
                    "pair_initials": "AC",
                    "state": "working",
                    "activity": "designing agent-status board",
                    "queue": [],
                    "status_updated_at": "2026-06-10T12:00:00Z",
                    "status_stale": False,
                },
                {
                    "name": "bob",
                    "avatar_seed": "bob",
                    "pair_initials": "BO",
                    "state": "sleeping",
                    "activity": "(asleep)",
                    "queue": ["#998"],
                    "status_updated_at": "2026-06-10T11:00:00Z",
                    "status_stale": False,
                },
                {
                    "name": "erin",
                    "avatar_seed": "erin",
                    "pair_initials": None,
                    "state": "offline",
                    "activity": None,
                    "queue": [],
                    "status_updated_at": None,
                    "status_stale": False,
                },
            ],
            "online_count": 2,
            "registered": 3,
        }

    def _run_board_text(self, board_data):
        """Stub _api_get to return board_data, run 'forum board', capture output."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        def _fake_get(url, params=None, swallow_errors=False):
            return board_data

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=_fake_get), \
             mock.patch("sys.argv", ["forum", "board"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass
        return stdout_buf.getvalue()

    def test_header_line_shows_online_count(self):
        out = self._run_board_text(self._board_data_mixed())
        assert "2 online of 3 registered" in out

    def test_column_headers_present(self):
        out = self._run_board_text(self._board_data_mixed())
        assert "AGENT" in out
        assert "STATE" in out
        assert "ACTIVITY" in out
        assert "QUEUE" in out

    def test_online_working_agent_rendered(self):
        out = self._run_board_text(self._board_data_mixed())
        assert "alice" in out
        assert "working" in out
        assert "designing agent-status board" in out

    def test_sleeping_agent_rendered(self):
        out = self._run_board_text(self._board_data_mixed())
        assert "bob" in out
        assert "sleeping" in out
        assert "(asleep)" in out
        assert "#998" in out

    def test_offline_agent_shows_dash_activity(self):
        """Offline agents have activity=None → activity cell shows '—'."""
        out = self._run_board_text(self._board_data_mixed())
        # 'erin' is offline; the activity cell must contain '—' (em dash).
        lines = out.splitlines()
        luria_line = next((l for l in lines if "erin" in l), None)
        assert luria_line is not None, f"erin not found in output:\n{out}"
        assert "—" in luria_line, (
            f"offline agent erin must show '—' for activity; line: {luria_line!r}"
        )

    def test_stale_online_shows_stale_marker(self):
        """Online agent with status_stale=True gets '(stale)' appended to state cell."""
        data = self._board_data_mixed()
        # Mark alice as stale.
        data["board"][0]["status_stale"] = True
        out = self._run_board_text(data)
        # The alice line must show '(stale)' in the state column.
        lines = out.splitlines()
        ariadne_line = next((l for l in lines if "alice" in l), None)
        assert ariadne_line is not None
        assert "(stale)" in ariadne_line, (
            f"expected '(stale)' marker for stale agent; line: {ariadne_line!r}"
        )

    def test_offline_does_not_show_stale_marker(self):
        """Offline rows never show '(stale)' — offline overrides stale."""
        data = self._board_data_mixed()
        # Offline agent erin should never show stale even if flag is set.
        data["board"][2]["status_stale"] = True
        out = self._run_board_text(data)
        lines = out.splitlines()
        luria_line = next((l for l in lines if "erin" in l), None)
        assert luria_line is not None
        assert "(stale)" not in luria_line, (
            f"offline agent must not show '(stale)'; line: {luria_line!r}"
        )

    def test_activity_truncation(self):
        """Activity longer than 40 chars is truncated with an ellipsis."""
        data = self._board_data_mixed()
        long_activity = "a" * 60
        data["board"][0]["activity"] = long_activity
        out = self._run_board_text(data)
        lines = out.splitlines()
        ariadne_line = next((l for l in lines if "alice" in l), None)
        assert ariadne_line is not None
        assert long_activity not in ariadne_line, (
            "full 60-char activity should have been truncated"
        )
        assert "…" in ariadne_line or "..." in ariadne_line or len(
            [c for c in ariadne_line if c == "a"]
        ) < 60


# ---------------------------------------------------------------------------
# 4. board --format json
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestBoardJsonFormat:
    """board --format json dumps the raw payload."""

    def test_json_format_dumps_raw(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        expected = {
            "board": [
                {
                    "name": "alice",
                    "avatar_seed": "alice",
                    "pair_initials": "AC",
                    "state": "idle",
                    "activity": None,
                    "queue": [],
                    "status_updated_at": None,
                    "status_stale": False,
                }
            ],
            "online_count": 1,
            "registered": 1,
        }

        def _fake_get(url, params=None, swallow_errors=False):
            return expected

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=_fake_get), \
             mock.patch("sys.argv", ["forum", "board", "--format", "json"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        parsed = json.loads(output)
        assert parsed == expected

    def test_json_format_via_live_client(self, client):
        """End-to-end: publish → board --format json via Flask test client."""
        run_cli = _make_wired_cli(client)
        # Publish a status first so there is something on the board.
        out, err, code = run_cli([
            "status-publish", "--state", "idle",
        ])
        assert code == 0, f"status-publish failed: {err}"

        # Now fetch the board as JSON.
        out, err, code = run_cli(["board", "--format", "json"])
        assert code == 0, f"board failed: {err}"
        data = json.loads(out)
        assert "board" in data
        assert "online_count" in data
        assert "registered" in data


# ---------------------------------------------------------------------------
# 5. online enriched line includes state + activity
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestOnlineEnriched:
    """forum online enriched text line includes state and activity."""

    def test_online_line_includes_state(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        online_data = {
            "online": [
                {
                    "name": "alice",
                    "avatar_seed": "alice",
                    "pair_initials": "AC",
                    "state": "working",
                    "activity": "designing agent-status board",
                    "queue": [],
                    "status_updated_at": "2026-06-10T12:00:00Z",
                    "status_stale": False,
                }
            ],
            "count": 1,
            "registered": 1,
        }

        def _fake_get(url, params=None, swallow_errors=False):
            return online_data

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=_fake_get), \
             mock.patch("sys.argv", ["forum", "online"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        assert "alice" in output
        assert "working" in output

    def test_online_line_includes_activity_when_present(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        online_data = {
            "online": [
                {
                    "name": "bob",
                    "avatar_seed": "bob",
                    "pair_initials": "BO",
                    "state": "idle",
                    "activity": "reviewing spec",
                    "queue": [],
                    "status_updated_at": "2026-06-10T12:00:00Z",
                    "status_stale": False,
                }
            ],
            "count": 1,
            "registered": 1,
        }

        def _fake_get(url, params=None, swallow_errors=False):
            return online_data

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=_fake_get), \
             mock.patch("sys.argv", ["forum", "online"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        assert "reviewing spec" in output

    def test_online_no_activity_omits_dash(self):
        """When activity is None/empty, the line shows state but no '—' separator."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        online_data = {
            "online": [
                {
                    "name": "dave",
                    "avatar_seed": "dave",
                    "pair_initials": "KE",
                    "state": "idle",
                    "activity": None,
                    "queue": [],
                    "status_updated_at": None,
                    "status_stale": False,
                }
            ],
            "count": 1,
            "registered": 1,
        }

        def _fake_get(url, params=None, swallow_errors=False):
            return online_data

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_get", side_effect=_fake_get), \
             mock.patch("sys.argv", ["forum", "online"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        assert "dave" in output
        assert "idle" in output
        # No " — " separator when activity is absent.
        lines = output.splitlines()
        kepler_line = next((l for l in lines if "dave" in l), "")
        assert " — " not in kepler_line

    def test_online_enrichment_via_live_client(self, client):
        """End-to-end: publish status, then forum online shows state."""
        run_cli = _make_wired_cli(client)
        # Publish a working status.
        out, err, code = run_cli([
            "status-publish",
            "--state", "working",
            "--activity", "testing the CLI",
        ])
        assert code == 0, f"status-publish failed: {err}"

        # Now check online list.
        out, err, code = run_cli(["online"])
        assert code == 0, f"online failed: {err}"
        assert "working" in out
        assert "testing the CLI" in out


# ---------------------------------------------------------------------------
# 6. Repeatable --queue collects into a list
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestRepeatableQueue:
    """--queue is repeatable (action='append'); multiple flags → a list."""

    def test_two_queue_items(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["queue"] = payload.get("queue")
            return {"status": "published", "agent": "alice", "state": "working"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", [
                 "forum", "status-publish", "--state", "working",
                 "--queue", "item-one",
                 "--queue", "item-two",
             ]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["queue"] == ["item-one", "item-two"], (
            f"expected ['item-one', 'item-two'], got {captured['queue']!r}"
        )

    def test_three_queue_items(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["queue"] = payload.get("queue")
            return {"status": "published", "agent": "alice", "state": "working"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", [
                 "forum", "status-publish", "--state", "working",
                 "--queue", "a",
                 "--queue", "b",
                 "--queue", "c",
             ]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["queue"] == ["a", "b", "c"]

    def test_single_queue_item(self):
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["queue"] = payload.get("queue")
            return {"status": "published", "agent": "alice", "state": "idle"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", [
                 "forum", "status-publish", "--state", "idle",
                 "--queue", "#994 review",
             ]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["queue"] == ["#994 review"]

    def test_no_queue_flag_gives_empty_list(self):
        """No --queue flags → queue in payload is []."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")
        captured = {}

        def _fake_post(url, payload):
            captured["queue"] = payload.get("queue")
            return {"status": "published", "agent": "alice", "state": "idle"}

        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", ["forum", "status-publish", "--state", "idle"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        assert captured["queue"] == [], f"expected [], got {captured['queue']!r}"

    def test_queue_items_appear_in_confirmation(self):
        """Queue items passed via --queue appear in the printed confirmation."""
        if not _CLI_AVAILABLE:
            pytest.skip("CLI not importable")

        def _fake_post(url, payload):
            return {"status": "published", "agent": "alice", "state": "working"}

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_post", side_effect=_fake_post), \
             mock.patch("sys.argv", [
                 "forum", "status-publish", "--state", "working",
                 "--queue", "my-task",
             ]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        assert "my-task" in output


# ---------------------------------------------------------------------------
# 7. status-auto: derive_own_status from loop-mode.json + batons (slice 3b)
# ---------------------------------------------------------------------------

@SKIP_CLI
class TestDeriveOwnStatus:
    """derive_own_status() reads the agent's own local signals."""

    @pytest.fixture
    def derive_env(self, tmp_path, monkeypatch):
        """Point LOOP_MODE_PATH + PROJECTS_DIR at writable temp locations.

        Patches _status_derive (where the values are READ by _read_loop_mode
        and _held_baton_turns) rather than forum_cli (which only holds copies
        of the imported names and is not the read site).
        """
        loop_path = tmp_path / "loop-mode.json"
        proj_dir = tmp_path / "projects"
        proj_dir.mkdir()
        monkeypatch.setattr(_status_derive, "LOOP_MODE_PATH", str(loop_path))
        monkeypatch.setattr(_status_derive, "PROJECTS_DIR", str(proj_dir))
        return loop_path, proj_dir

    def test_idle_when_no_loop_no_baton(self, derive_env):
        state, activity, queue, cadence = forum_cli.derive_own_status("alice")
        assert state == "idle"
        assert activity is None
        assert queue == []
        assert cadence is None  # → server's global window

    def test_working_with_cadence_from_loop_mode(self, derive_env):
        loop_path, _ = derive_env
        loop_path.write_text(json.dumps({"cadence_seconds": 2400, "topic": "Drive the day"}))
        state, activity, queue, cadence = forum_cli.derive_own_status("alice")
        assert state == "working"
        assert activity == "Drive the day"
        assert cadence == 2400

    def test_held_baton_adds_queue_and_working(self, derive_env):
        _, proj_dir = derive_env
        (proj_dir / "proj-alpha.md").write_text(
            "---\nproject: proj-alpha\nturn: alice\n---\nbody\n"
        )
        (proj_dir / "PR-999.md").write_text(
            "---\nproject: PR-999\nturn: bob\n---\nbody\n"  # not mine
        )
        state, _, queue, _ = forum_cli.derive_own_status("alice")
        assert state == "working"  # holding a turn ⇒ working even with no loop
        assert queue == ["proj-alpha"]  # only my turn, not bob's

    def test_on_call_flag_yields_sentinel(self, derive_env):
        _, _, _, cadence = forum_cli.derive_own_status("carol", on_call=True)
        assert cadence == forum_cli.ON_CALL_SENTINEL  # 0

    def test_monitor_pacer_yields_sentinel(self, derive_env):
        loop_path, _ = derive_env
        loop_path.write_text(json.dumps({"pacer": "monitor", "topic": "watching"}))
        _, _, _, cadence = forum_cli.derive_own_status("carol")
        assert cadence == forum_cli.ON_CALL_SENTINEL

    def test_cadence_zero_yields_sentinel(self, derive_env):
        # An explicit cadence_seconds==0 in the marker is the on-call sentinel.
        loop_path, _ = derive_env
        loop_path.write_text(json.dumps({"cadence_seconds": 0}))
        _, _, _, cadence = forum_cli.derive_own_status("carol")
        assert cadence == forum_cli.ON_CALL_SENTINEL

    def test_override_state_empty_string_is_honored(self, derive_env):
        # override_state uses an is-not-None check, so an explicit '' is honored
        # (not silently dropped to auto-derivation). Locks the sentinel choice.
        loop_path, _ = derive_env
        loop_path.write_text(json.dumps({"cadence_seconds": 1200}))  # would auto → working
        state, _, _, _ = forum_cli.derive_own_status("alice", override_state="")
        assert state == ""

    def test_overrides_win(self, derive_env):
        loop_path, _ = derive_env
        loop_path.write_text(json.dumps({"cadence_seconds": 2400, "topic": "auto-topic"}))
        state, activity, _, cadence = forum_cli.derive_own_status(
            "alice",
            override_state="sleeping",
            override_activity="going to sleep",
            override_cadence=None,  # explicit None overrides the loop's 2400
        )
        assert state == "sleeping"
        assert activity == "going to sleep"
        assert cadence is None

    def test_malformed_loop_mode_treated_as_absent(self, derive_env):
        loop_path, _ = derive_env
        loop_path.write_text("{ this is not json")
        state, _, _, cadence = forum_cli.derive_own_status("alice")
        assert state == "idle"  # never crashes a wake
        assert cadence is None


@SKIP_CLI
class TestStatusAutoPublish:
    """`forum status-auto` derives + publishes; the board reflects it."""

    @pytest.fixture
    def auto_env(self, tmp_path, monkeypatch):
        loop_path = tmp_path / "loop-mode.json"
        proj_dir = tmp_path / "projects"
        proj_dir.mkdir()
        # Patch _status_derive (the read site) not forum_cli (which only holds
        # copies of the imported names).
        monkeypatch.setattr(_status_derive, "LOOP_MODE_PATH", str(loop_path))
        monkeypatch.setattr(_status_derive, "PROJECTS_DIR", str(proj_dir))
        return loop_path, proj_dir

    def test_auto_publishes_working_with_cadence(self, client, auto_env):
        loop_path, _ = auto_env
        loop_path.write_text(json.dumps({"cadence_seconds": 1200, "topic": "building slice 3"}))
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto"])
        assert code == 0, err
        assert "working" in out
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"
        assert a["activity"] == "building slice 3"

    def test_auto_idle_when_no_loop(self, client, auto_env):
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto"])
        assert code == 0, err
        assert "idle" in out
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "idle"

    def test_auto_on_call_publishes_sentinel(self, client, auto_env):
        """--on-call publishes the event-driven sentinel (cadence 0).

        The on-call *resolution* (stale → 'on-call' not 'offline') is covered at
        the db layer in test_agent_status.py::TestOnCall; here we assert the CLI
        sends the sentinel and reports it, and the published state shows through
        while freshly seen.
        """
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto", "--on-call", "--state", "working"])
        assert code == 0, err
        assert "on-call" in out  # cmd_status_auto annotates the sentinel
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"

    def test_auto_activity_override(self, client, auto_env):
        loop_path, _ = auto_env
        loop_path.write_text(json.dumps({"cadence_seconds": 1200, "topic": "auto"}))
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto", "--activity", "hand-written activity"])
        assert code == 0, err
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        assert a["activity"] == "hand-written activity"

    def test_auto_cadence_override_via_cli(self, client, auto_env):
        # --cadence N flows through the _UNSET → argparse integration surface and
        # the agent reads online while fresh. (--cadence 0 → on-call sentinel.)
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto", "--state", "working", "--cadence", "270"])
        assert code == 0, err
        assert "cadence 270s" in out
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"

    def test_auto_cadence_zero_via_cli_is_on_call(self, client, auto_env):
        run_cli = _make_wired_cli(client)
        out, err, code = run_cli(["status-auto", "--state", "working", "--cadence", "0"])
        assert code == 0, err
        assert "on-call" in out  # cadence 0 annotated as the event-driven sentinel
