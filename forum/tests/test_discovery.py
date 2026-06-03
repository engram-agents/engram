"""Tests for the self-describing discovery surface.

Covers:
  - GET /forum.md → 200, correct content-type, expected content
  - Served /forum.md body matches the committed forum/FORUM.md file verbatim
  - CLI: forum describe → fetches /forum.md and prints it
"""

import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures (mirrored from test_endpoints.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /forum.md — server-side tests
# ---------------------------------------------------------------------------

class TestForumMdRoute:
    def test_returns_200(self, client):
        resp = client.get("/forum.md")
        assert resp.status_code == 200

    def test_content_type_is_text(self, client):
        resp = client.get("/forum.md")
        ct = resp.content_type
        # Accept text/plain or text/markdown
        assert ct.startswith("text/plain") or ct.startswith("text/markdown"), (
            f"Unexpected content-type: {ct}"
        )

    def test_body_contains_api_post_endpoint(self, client):
        """The contract must document the POST /api/post endpoint."""
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        assert "/api/post" in body

    def test_body_contains_verb_list(self, client):
        """The contract must list CLI verbs."""
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        # A sampling of expected verbs
        assert "forum post" in body
        assert "forum read" in body
        assert "forum list" in body
        assert "forum describe" in body

    def test_body_contains_api_threads_endpoint(self, client):
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        assert "/api/threads" in body

    def test_body_contains_cursor_contract(self, client):
        """The contract must document the read-cursor model."""
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        assert "cursor" in body.lower()

    def test_body_contains_sign_in_model(self, client):
        """The contract must document the same-LAN trust / sign-in model."""
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        assert "agent" in body.lower()

    def test_body_contains_self_referencing_url(self, client):
        """The contract must reference /forum.md itself."""
        resp = client.get("/forum.md")
        body = resp.data.decode("utf-8")
        assert "/forum.md" in body


# ---------------------------------------------------------------------------
# Endpoint-integrity invariant: served body must match committed FORUM.md
# ---------------------------------------------------------------------------

# Resolve the same path the route uses:
#   _FORUM_MD_PATH = os.path.join(os.path.dirname(__file__), "FORUM.md")
# server.py lives in forum/; __file__ for that module resolves to the forum/
# package directory.  Tests live in forum/tests/, so we go one level up.
_FORUM_PACKAGE_DIR = Path(__file__).parent.parent
_COMMITTED_FORUM_MD = _FORUM_PACKAGE_DIR / "FORUM.md"


class TestServedForumMdMatchesCommitted:
    def test_served_matches_committed_file(self, client):
        """GET /forum.md must return exactly the committed forum/FORUM.md content.

        The route reads the file with encoding=utf-8 and returns it verbatim
        (no trailing-newline manipulation).  This test reads the same file
        independently and asserts string equality, so a divergence between
        the committed file and the served body will fail loudly.
        """
        committed = _COMMITTED_FORUM_MD.read_text(encoding="utf-8")
        resp = client.get("/forum.md")
        assert resp.status_code == 200
        served = resp.data.decode("utf-8")
        assert served == committed, (
            "Served /forum.md body does not match committed forum/FORUM.md. "
            f"Committed length={len(committed)}, served length={len(served)}."
        )


# ---------------------------------------------------------------------------
# CLI: forum describe (integration via wired_cli pattern)
# ---------------------------------------------------------------------------

# Attempt to import the CLI module (mirrors test_forum_cli.py style)
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent.parent  # repo root

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import tools.forum as forum_cli
    _CLI_AVAILABLE = True
except ImportError:
    _CLI_AVAILABLE = False

SKIP_CLI = pytest.mark.skipif(
    not _CLI_AVAILABLE,
    reason="tools.forum CLI not importable",
)


@pytest.fixture
def engram_home(tmp_path):
    """Temp ENGRAM_HOME with a minimal config.json."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "testbot", "forum": {"url": "http://testserver"}}
    (home / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture(autouse=True)
def reset_forum_cli_cache(engram_home, monkeypatch):
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


def _make_fake_urlopen(flask_client):
    """Return a fake urlopen that routes requests through the Flask test client.

    The function signature matches urllib.request.urlopen(req, timeout=...).
    Routes any URL with an http://testserver prefix through flask_client.get();
    the exact routing logic is preserved from the original per-test helpers.
    """
    def fake_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if url.startswith("http://testserver"):
            path = url[len("http://testserver"):]
        else:
            path = url
        resp = flask_client.get(path)
        raw = resp.data

        class FakeResponse:
            def read(self):
                return raw
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        return FakeResponse()

    return fake_urlopen


@SKIP_CLI
class TestDescribeCli:
    """CLI: forum describe fetches /forum.md and prints it."""

    def _make_wired_cli(self, flask_client, engram_home):
        """Build a wired run_cli helper that routes through the Flask test client."""
        base_url = "http://testserver"

        def _patched_do_request(req, url):
            if url.startswith(base_url):
                path = url[len(base_url):]
            else:
                path = url

            method = req.get_method()
            if method == "POST":
                data = req.data
                headers = dict(req.headers) if req.headers else {}
                resp = flask_client.post(
                    path,
                    data=data,
                    content_type=headers.get("Content-Type", "application/json"),
                )
            else:
                resp = flask_client.get(path)

            if resp.status_code >= 400:
                body = resp.data.decode("utf-8", errors="replace")
                print(f"forum: server error ({resp.status_code}): {body}", file=sys.stderr)
                sys.exit(forum_cli.EXIT_VALIDATION)

            # describe reads raw text, not JSON — return the raw bytes decoded
            # The CLI's cmd_describe calls urlopen directly, not _do_request.
            # This patched function is only used for JSON-API calls.
            # For describe, we patch urllib directly instead.
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

    def test_describe_fetches_forum_md(self, app, tmp_path, engram_home):
        """forum describe should print content that includes /api/post."""
        flask_client = app.test_client()
        fake_urlopen = _make_fake_urlopen(flask_client)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        import urllib.request as _urlrequest
        with mock.patch.object(_urlrequest, "urlopen", side_effect=fake_urlopen), \
             mock.patch("sys.argv", ["forum", "describe"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else 0

        assert exit_code == 0, f"stderr: {stderr_buf.getvalue()}"
        output = stdout_buf.getvalue()
        assert "/api/post" in output, (
            f"forum describe output should include /api/post; got:\n{output[:500]}"
        )

    def test_describe_prints_verb_list(self, app, tmp_path, engram_home):
        """forum describe output must include the CLI verb table."""
        flask_client = app.test_client()
        fake_urlopen = _make_fake_urlopen(flask_client)

        stdout_buf = io.StringIO()
        import urllib.request as _urlrequest
        with mock.patch.object(_urlrequest, "urlopen", side_effect=fake_urlopen), \
             mock.patch("sys.argv", ["forum", "describe"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit:
                pass

        output = stdout_buf.getvalue()
        assert "forum post" in output
        assert "forum read" in output

    def test_describe_unreachable_exits_3(self, engram_home):
        """forum describe exits EXIT_UNREACHABLE when server is down."""
        # Point at a port nothing is listening on
        (engram_home / "config.json").write_text(
            json.dumps({"agent_name": "testbot", "forum": {"url": "http://localhost:59997"}})
        )
        forum_cli._FORUM_URL_CACHE = None

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch("sys.argv", ["forum", "describe"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code

        assert code == forum_cli.EXIT_UNREACHABLE
        assert "not reachable" in stderr_buf.getvalue()

    def test_describe_404_exits_not_found(self, engram_home):
        """forum describe exits EXIT_NOT_FOUND (4) when the server returns 404."""
        import urllib.error
        import urllib.request as _urlrequest

        def _raise_404(req, timeout=10):
            raise urllib.error.HTTPError(
                url="http://testserver/forum.md",
                code=404,
                msg="Not Found",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b"FORUM.md not found"),
            )

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch.object(_urlrequest, "urlopen", side_effect=_raise_404), \
             mock.patch("sys.argv", ["forum", "describe"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code

        assert code == forum_cli.EXIT_NOT_FOUND, (
            f"Expected EXIT_NOT_FOUND ({forum_cli.EXIT_NOT_FOUND}), got {code}; "
            f"stderr: {stderr_buf.getvalue()}"
        )

    def test_describe_non404_http_error_exits_validation(self, engram_home):
        """forum describe exits EXIT_VALIDATION (2) on non-404 HTTP errors."""
        import urllib.error
        import urllib.request as _urlrequest

        def _raise_500(req, timeout=10):
            raise urllib.error.HTTPError(
                url="http://testserver/forum.md",
                code=500,
                msg="Internal Server Error",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b"server exploded"),
            )

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with mock.patch.object(_urlrequest, "urlopen", side_effect=_raise_500), \
             mock.patch("sys.argv", ["forum", "describe"]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", stderr_buf):
            try:
                forum_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code

        assert code == forum_cli.EXIT_VALIDATION, (
            f"Expected EXIT_VALIDATION ({forum_cli.EXIT_VALIDATION}), got {code}; "
            f"stderr: {stderr_buf.getvalue()}"
        )
