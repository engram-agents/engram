"""#1459 PR A round 4 (S2): the bearer token never follows a redirect to a different origin.

Contract (stated before the implementation):

* A request carrying ``Authorization: Bearer`` follows a redirect ONLY when the target has the
  same scheme, host and port as the original request.
* A cross-origin redirect is NOT followed. The client raises ``ForumRedirectError`` naming the
  target origin, and the target never receives the header. It is a ``ForumNetworkError``
  subclass, so every existing ``except ForumNetworkError`` caller (baton, ia) already handles it.
* A same-origin redirect (a werkzeug 308 for a doubled slash, say) keeps working, and the
  header arrives at the final URL.
* ONE policy, in ``forum_api`` (``forum_urlopen``); every urllib sender uses it. The prompt
  hook drives ``http.client`` and treats any non-2xx as a failure, so it follows no redirect.
* The monitor scripts run curl without ``-L`` / ``--location`` / ``--location-trusted``, so
  curl follows no redirect at all.

Each sender is exercised in its own subprocess (the three tool modules and the hook resolve
``forum_api`` by different import routes, and a subprocess is the layout they ship in).
Every cross-origin cell also asserts the FIRST server DID receive the header: otherwise "the
second server never saw it" could be true only because nothing was sent.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "src" / "engram" / "tools"
HOOK = REPO / "src" / "engram" / "hooks" / "claude" / "engram-forum-prompt-hook.py"
TOKEN = "tok-redirect-1459-secret"


class _Server:
    """A local HTTP server on an ephemeral port that records what it was sent.

    mode "cross":  every request -> 302 to ``target`` + the same path.
    mode "same":   every path but /landed -> 308 to the RELATIVE /landed; /landed -> 200 JSON.
    mode "ok":     200 JSON for everything.
    """

    def __init__(self, mode="ok", target=None):
        self.mode, self.target, self.seen = mode, target, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                if outer.mode == "cross":
                    self.send_response(302)
                    self.send_header("Location", f"{outer.target}{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif outer.mode == "same" and not self.path.startswith("/landed"):
                    self.send_response(308)
                    self.send_header("Location", "/landed")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b'{"ok": true, "projects": []}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def auth_seen(self):
        return [a for _, a in self.seen if a]


@pytest.fixture
def owned_tmp():
    d = Path(tempfile.mkdtemp(prefix="forum1459redir-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def servers(owned_tmp):
    """(A, B): A answers 302 to B, a different origin (a different port)."""
    with _Server("ok") as b, _Server("cross", target=None) as a:
        a.target = b.url
        yield a, b


def _run(owned_tmp, code, forum_url, timeout=60):
    home = owned_tmp / "home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps({"agent_name": "alice"}))
    env = dict(os.environ, ENGRAM_HOME=str(home), FORUM_URL=forum_url, USER="alice",
               ENGRAM_FORUM_TOKEN=TOKEN)
    env.pop("ENGRAM_NO_EMBEDDINGS", None)
    prelude = f"import sys; sys.path.insert(0, {str(TOOLS)!r})\n"
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _bearer():
    return f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# the policy itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,same", [
    ("http://h:5002/x", "http://h:5002/y?q=1", True),
    ("http://h/x", "http://h:80/y", True),            # the default port is the same origin
    ("https://h/x", "https://h:443/y", True),
    ("HTTP://H:5002/x", "http://h:5002/y", True),     # scheme and host compare case-folded
    ("http://h:5002/x", "http://h:5003/x", False),    # port
    ("http://h:5002/x", "http://other:5002/x", False),  # host
    ("http://localhost:5002/x", "http://127.0.0.1:5002/x", False),  # names are not resolved
    ("http://h:5002/x", "https://h:5002/x", False),   # scheme
    ("http://h/x", "https://h/x", False),             # scheme (and implied port)
])
def test_same_origin_compares_scheme_host_and_port(a, b, same):
    from tools.forum_api import same_origin
    assert same_origin(a, b) is same


def test_policy_refuses_a_cross_origin_redirect_and_names_the_target(servers, owned_tmp):
    import urllib.request
    from tools.forum_api import ForumNetworkError, ForumRedirectError, forum_urlopen
    a, b = servers
    req = urllib.request.Request(a.url + "/x", headers={"Authorization": _bearer()})
    with pytest.raises(ForumRedirectError) as ei:
        forum_urlopen(req, timeout=10)
    assert isinstance(ei.value, ForumNetworkError)
    assert b.url in str(ei.value), str(ei.value)       # names the target ORIGIN
    assert TOKEN not in str(ei.value)                    # never the secret
    assert ei.value.target_origin == b.url
    assert a.auth_seen() == [_bearer()] and b.seen == []


def test_policy_follows_a_same_origin_308_and_the_header_arrives(owned_tmp):
    import urllib.request
    from tools.forum_api import forum_urlopen
    with _Server("same") as a:
        req = urllib.request.Request(a.url + "/doubled", headers={"Authorization": _bearer()})
        with forum_urlopen(req, timeout=10) as resp:
            assert json.loads(resp.read())["ok"] is True
        assert [p for p, _ in a.seen] == ["/doubled", "/landed"]
        assert a.auth_seen() == [_bearer(), _bearer()]   # re-sent to the SAME origin


def test_a_request_with_no_authorization_header_is_not_restricted(servers):
    """The policy protects the bearer token; a request that carries none follows as before."""
    import urllib.request
    from tools.forum_api import forum_urlopen
    a, b = servers
    with forum_urlopen(urllib.request.Request(a.url + "/x"), timeout=10) as resp:
        assert json.loads(resp.read())["ok"] is True
    assert [p for p, _ in b.seen] == ["/x"] and b.auth_seen() == []


# ---------------------------------------------------------------------------
# every sender: cross-origin refused, the target never sees the header
# ---------------------------------------------------------------------------
def test_forum_client_refuses_a_cross_origin_redirect(servers, owned_tmp):
    a, b = servers
    r = _run(owned_tmp, f"""
        from forum_api import ForumClient, ForumRedirectError
        try:
            ForumClient({a.url!r}).get("/api/x")
        except ForumRedirectError as e:
            print("REDIRECT-ERR", e)
        """, a.url)
    assert "REDIRECT-ERR" in r.stdout and b.url in r.stdout, (r.stdout, r.stderr)
    assert a.auth_seen() == [_bearer()] and b.seen == []


def test_forum_client_post_refuses_a_cross_origin_redirect(servers, owned_tmp):
    a, b = servers
    r = _run(owned_tmp, f"""
        from forum_api import ForumClient, ForumRedirectError
        try:
            ForumClient({a.url!r}).post("/api/x", {{"k": 1}})
        except ForumRedirectError as e:
            print("REDIRECT-ERR", e)
        """, a.url)
    assert "REDIRECT-ERR" in r.stdout and b.url in r.stdout, (r.stdout, r.stderr)
    assert a.auth_seen() == [_bearer()] and b.seen == []


def test_forum_client_follows_a_same_origin_308(owned_tmp):
    with _Server("same") as a:
        r = _run(owned_tmp, f"""
            from forum_api import ForumClient
            print("GOT", ForumClient({a.url!r}).get("/api/x"))
            """, a.url)
        assert "GOT {'ok': True" in r.stdout, (r.stdout, r.stderr)
        assert a.auth_seen() == [_bearer(), _bearer()]


def test_forum_cli_refuses_a_cross_origin_redirect_loudly(servers, owned_tmp):
    a, b = servers
    r = _run(owned_tmp, f"""
        import urllib.request, forum
        req = urllib.request.Request({a.url!r} + "/api/x", headers={{"Accept": "application/json"}})
        try:
            forum._do_request(req, {a.url!r} + "/api/x")
        except SystemExit as e:
            print("EXIT", e.code)
        """, a.url)
    assert "EXIT 3" in r.stdout, (r.stdout, r.stderr)           # EXIT_UNREACHABLE, no traceback
    assert "Traceback" not in r.stderr and b.url in r.stderr, r.stderr
    assert a.auth_seen() == [_bearer()] and b.seen == []


def test_forum_cli_follows_a_same_origin_308(owned_tmp):
    with _Server("same") as a:
        r = _run(owned_tmp, f"""
            import urllib.request, forum
            req = urllib.request.Request({a.url!r} + "/api/x", headers={{"Accept": "application/json"}})
            print("GOT", forum._do_request(req, {a.url!r} + "/api/x"))
            """, a.url)
        assert "GOT {'ok': True" in r.stdout, (r.stdout, r.stderr)
        assert a.auth_seen() == [_bearer(), _bearer()]


def test_status_derive_refuses_a_cross_origin_redirect_loudly(servers, owned_tmp):
    a, b = servers
    r = _run(owned_tmp, """
        import _status_derive
        print("HELD", _status_derive._fetch_held_batons("alice"))
        """, a.url)
    assert "HELD []" in r.stdout, (r.stdout, r.stderr)           # still fails soft ...
    assert b.url in r.stderr and "Traceback" not in r.stderr, r.stderr  # ... but not silently
    assert a.auth_seen() == [_bearer()] and b.seen == []


def test_status_derive_follows_a_same_origin_308(owned_tmp):
    with _Server("same") as a:
        r = _run(owned_tmp, """
            import _status_derive
            print("HELD", _status_derive._fetch_held_batons("alice"))
            """, a.url)
        assert "HELD []" in r.stdout, (r.stdout, r.stderr)
        assert [p for p, _ in a.seen][-1] == "/landed" and a.auth_seen() == [_bearer(), _bearer()]


def test_prompt_hook_follows_no_redirect_at_all(servers, owned_tmp):
    """The hook drives http.client and treats any non-2xx as a failure: a 302 is a failure line,
    never a follow, so the target never sees the header it WAS sent to the original origin."""
    a, b = servers
    r = _run(owned_tmp, f"""
        import importlib.util, time
        spec = importlib.util.spec_from_file_location("hook", {str(HOOK)!r})
        hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
        print("RESULT", hook._fetch_classified({a.url!r} + "/api/x", time.monotonic() + 5, 2.0))
        """, a.url)
    assert "RESULT (None, 'http-302" in r.stdout, (r.stdout, r.stderr)
    assert a.auth_seen() == [_bearer()] and b.seen == []


# ---------------------------------------------------------------------------
# structural: one policy, every urllib sender uses it; the monitors never follow
# ---------------------------------------------------------------------------
def _calls(path, attr):
    import ast
    tree = ast.parse(path.read_text())
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


@pytest.mark.parametrize("name", ["forum.py", "_status_derive.py", "forum_api.py"])
def test_urllib_senders_open_through_the_one_policy(name):
    """No bare ``urllib.request.urlopen(`` call in a sender; they call ``forum_urlopen``."""
    src = (TOOLS / name).read_text()
    assert "forum_urlopen" in src, name
    for n in _calls(TOOLS / name, "urlopen"):
        # none allowed: the policy's own call is `opener.open(`, the senders call `forum_urlopen(`
        pytest.fail(f"{name}:{n.lineno} calls .urlopen( directly, bypassing the redirect policy")


def test_only_forum_api_defines_a_redirect_handler():
    for p in list((TOOLS).glob("*.py")) + [HOOK]:
        txt = p.read_text()
        if p.name == "forum_api.py":
            assert txt.count("HTTPRedirectHandler") >= 1
        else:
            assert "HTTPRedirectHandler" not in txt and "build_opener" not in txt, p.name


@pytest.mark.parametrize("script", ["forum-updates-monitor.sh", "forum-mention-monitor.sh"])
def test_monitor_curl_never_follows_redirects(script):
    code = [ln for ln in (TOOLS / script).read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    invocations = [ln for ln in code if re.search(r"(^|[|(\s])curl\s+-", ln)]
    assert len(invocations) == 2, invocations   # the token arm and the no-token arm; not vacuous
    follow = re.compile(r"\s-[A-Za-z]*L[A-Za-z]*(\s|$)")
    for ln in invocations:      # `[ -L file ]` (a symlink test) elsewhere is not a curl flag
        assert not follow.search(ln), f"{script}: {ln.strip()}"
    for ln in code:
        assert "--location" not in ln, f"{script}: {ln.strip()}"
