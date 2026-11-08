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
import ast
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
# structural: one policy, every sender uses it; the monitors never follow (#4491)
#
# The sender set is DERIVED from the repo (every non-test .py that is tracked by git, or lies
# under src/ or tools/ on disk, and sends with the Authorization header), never listed, and the
# matcher resolves ``urlopen`` in every spelling from the file's own imports. Contract, stated
# before the cells:
#   1. D = non-test .py in the UNIVERSE (every file `git ls-files` lists at the repo root, plus an
#      os.walk of src/ and tools/ for untracked files) that (a) has a string literal naming
#      the Authorization header in any casing (HTTP header names are case-insensitive), or (b)
#      an Authorization= keyword, or (c) references the forum token source; realpath-deduped,
#      minus the receiver src/forum/server.py. A failing `git ls-files` raises.
#   2. D is non-empty and contains at least the four known senders (a floor, not the domain).
#   3. No F in D REFERENCES urllib.request.urlopen (a load: called, or bound to a variable and
#      called later) as attribute, bare name, or import alias, where the name RESOLVES to
#      urllib.request; no F but forum_api.py references build_opener, HTTPRedirectHandler or
#      OpenerDirector (a subclass base is such a reference).
#   4. Each F in D is (a) forum_api.py, or at least one of (b) references forum_urlopen,
#      (c) imports http.client AND imports nothing from urllib.request (the hook's shape).
#   5. The monitor scripts are DERIVED too: every non-test .sh in the universe that references the
#      forum token source and invokes curl; none follows a redirect.
# ---------------------------------------------------------------------------
_RECEIVER = (REPO / "src" / "forum" / "server.py").resolve()
_KNOWN_SENDERS = [TOOLS / "forum.py", TOOLS / "_status_derive.py", TOOLS / "forum_api.py", HOOK]


def _is_test_path(rel):
    parts = rel.split("/")
    name = parts[-1]
    return ("tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


_PRUNE_DIRS = {"__pycache__", ".git", "node_modules"}


def _has_header_literal(tree):
    """(a) a str/bytes constant whose header-name part (text before the first colon, stripped,
    lowercased) is ``authorization``: header-dict keys, add_header/putheader arguments, and the
    literal parts of f-strings (``ast.walk`` reaches the Constants inside a JoinedStr)."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes)):
            v = n.value.decode("latin-1") if isinstance(n.value, bytes) else n.value
            if v.strip().split(":")[0].strip().lower() == "authorization":
                return True
    return False


def _has_header_keyword(tree):
    """(b) a keyword argument named ``authorization`` in any casing: ``dict(h, Authorization=t)``."""
    return any(isinstance(n, ast.keyword) and n.arg and n.arg.lower() == "authorization"
               for n in ast.walk(tree))


def _has_token_reference(tree):
    """(c) a reference to the forum token source: the literal ``ENGRAM_FORUM_TOKEN`` or
    ``forum-token``, or a Name, Attribute or import of ``load_forum_token``."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and n.value in ("ENGRAM_FORUM_TOKEN", "forum-token"):
            return True
        if isinstance(n, ast.Name) and n.id == "load_forum_token":
            return True
        if isinstance(n, ast.Attribute) and n.attr == "load_forum_token":
            return True
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                a.name == "load_forum_token" or (a.asname == "load_forum_token") for a in n.names):
            return True
    return False


def _is_sender_source(source):
    """True when the file is a sender by ANY of (a) a header-name literal, (b) an
    ``Authorization=`` keyword, (c) a reference to the forum token source. Prose in a comment or
    docstring matches none. A file that fails to parse COUNTS as a member, so the downstream
    cells report it instead of D silently dropping it."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True
    return _has_header_literal(tree) or _has_header_keyword(tree) or _has_token_reference(tree)


def _tracked_files(root):
    """Every path ``git ls-files`` lists at ``root``. A failure RAISES (no git, not a repo, a
    nonzero exit): the collector must never fall back silently to the narrower walk."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [root / rel for rel in out.stdout.decode("utf-8", "surrogateescape").split("\0") if rel]


def _universe(root, suffix):
    """Resolved paths of every non-test file ending in ``suffix`` in the universe: the UNION of
    (1) every file git tracks at ``root`` (what the repo ships, so a sender in a new top-level
    directory is seen) and (2) an os.walk of src/ and tools/ (pruning ``__pycache__``, ``.git``,
    ``node_modules``), which keeps untracked files in the code roots. Not "everything git does
    not ignore": an untracked, un-ignored ``.venv/`` would flood it. De-duplicated by realpath;
    a path that is not a file on disk is dropped. Test paths are excluded BY NAME (``test_*.py``,
    ``tests/``, ``conftest.py``), so a non-test tool named ``test_*.py`` is excluded: a stated bound."""
    candidates = list(_tracked_files(root))
    for top in ("src", "tools"):
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE_DIRS)
            candidates.extend(Path(dirpath) / fn for fn in sorted(filenames))
    found = set()
    for path in candidates:
        if not path.name.endswith(suffix) or _is_test_path(path.relative_to(root).as_posix()):
            continue
        real = Path(os.path.realpath(path))
        if real.is_file():
            found.add(real)
    return sorted(found)


def _sender_set(root=REPO):
    """D: resolved paths of every non-test .py in the universe (``_universe``: git-tracked files
    plus an on-disk walk of src/ and tools/) that ``_is_sender_source`` selects, minus the
    receiver. D therefore includes untracked files under src/ and tools/, which errs toward
    inclusion. The test-path exclusion is by name (``test_*.py``, ``tests/``, ``conftest.py``), so
    a non-test tool named ``test_*.py`` is excluded: a stated bound, not a caught case."""
    return [real for real in _universe(root, ".py")
            if real != _RECEIVER and _is_sender_source(real.read_text(errors="replace"))]


def _monitor_scripts(root=REPO):
    """Every non-test .sh in the universe that references the forum token source
    (``ENGRAM_FORUM_TOKEN`` or ``forum-token``) and invokes curl on a non-comment line."""
    out = []
    for real in _universe(root, ".sh"):
        text = real.read_text(errors="replace")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        if ("ENGRAM_FORUM_TOKEN" in text or "forum-token" in text) and any(
                re.search(r"(^|[|(\s])curl\b", ln) for ln in code):
            out.append(real)
    return out


def _rel(path):
    try:
        return str(Path(path).relative_to(REPO))
    except ValueError:
        return str(path)


def _urllib_request_bindings(tree):
    """Names that mean urllib.request / its urlopen / its build_opener in this file's imports."""
    import ast
    modules, urlopen_names, opener_names = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "urllib.request":
                    modules.add(a.asname or "urllib.request")
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            if n.module == "urllib":
                for a in n.names:
                    if a.name == "request":
                        modules.add(a.asname or "request")
            elif n.module == "urllib.request":
                for a in n.names:
                    if a.name in ("urlopen", "*"):
                        urlopen_names.add(a.asname or "urlopen")
                    if a.name in ("build_opener", "*"):
                        opener_names.add(a.asname or "build_opener")
    return modules, urlopen_names, opener_names


def _urllib_resolution(tree, attr):
    """Names that RESOLVE to urllib.request in this file: ``pkg`` (bound to the ``urllib``
    package by any ``import urllib[.x]`` without an alias, or ``import urllib as X``), ``req``
    (bound to the ``urllib.request`` module: ``import urllib.request as X``, ``from urllib import
    request [as X]``), and ``bare`` (bound to ``attr`` itself: ``from urllib.request import attr
    [as Z]`` or ``import *``)."""
    pkg, req, bare = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "urllib.request" and a.asname:
                    req.add(a.asname)
                elif a.name == "urllib" or a.name.startswith("urllib."):
                    if not a.asname:
                        pkg.add("urllib")           # `import urllib[.x]` binds the package
                    elif a.name == "urllib":
                        pkg.add(a.asname)           # `import urllib as X`
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            if n.module == "urllib":
                for a in n.names:
                    if a.name == "request":
                        req.add(a.asname or "request")
            elif n.module == "urllib.request":
                for a in n.names:
                    if a.name in (attr, "*"):
                        bare.add(a.asname or attr)
    return pkg, req, bare


def _refs_of(source, attr):
    """Line numbers of every LOAD reference to ``attr`` that RESOLVES to urllib.request, not only
    call sites: ``urllib.request.attr`` (called or not, e.g. ``f = urllib.request.urlopen``), ``X.attr``
    for an ``X`` bound to the module, or a bare name bound by ``from urllib.request import attr
    [as Z]`` / ``import *``. The import statement itself is not a load. A parameter, a local, or
    ``self.attr`` does not resolve to urllib.request and does not match. STATIC references only.
    Bounds, matched by NO cell: ``getattr(urllib.request, "urlopen")``,
    ``importlib.import_module("urllib.request")``, ``__import__("urllib.request")``, and a module
    rebound by assignment (``u = urllib.request``; ``u.urlopen`` is matched only where ``u`` is
    bound by an import). A file that reaches urllib.request through importlib imports nothing the
    classification sees and passes every cell; the cells do not catch it. A bare-name binding is
    also file-wide, not scoped."""
    tree = ast.parse(source)
    pkg, req, bare = _urllib_resolution(tree, attr)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == attr and isinstance(n.ctx, ast.Load):
            v = n.value
            if (isinstance(v, ast.Name) and v.id in req) or (
                    isinstance(v, ast.Attribute) and v.attr == "request"
                    and isinstance(v.value, ast.Name) and v.value.id in pkg):
                hits.append(n.lineno)
        elif isinstance(n, ast.Name) and n.id in bare and isinstance(n.ctx, ast.Load):
            hits.append(n.lineno)
    return sorted(hits)


def _imports_urllib_request(source):
    import ast
    modules, urlopen_names, opener_names = _urllib_request_bindings(ast.parse(source))
    return bool(modules or urlopen_names or opener_names)


def _references_name(source, name):
    import ast
    for n in ast.walk(ast.parse(source)):
        if (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name):
            return True
        if isinstance(n, (ast.ImportFrom, ast.Import)) and any(a.name == name for a in n.names):
            return True
    return False


def _imports_http_client(source):
    """True for ``import http.client [as X]``, ``from http import client [as X]`` and
    ``from http.client import ...``."""
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Import) and any(a.name == "http.client" for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and n.level == 0 and (
                n.module == "http.client"
                or (n.module == "http" and any(a.name == "client" for a in n.names))):
            return True
    return False


def _is_classified(source):
    """A non-forum_api sender is classified when it (b) references forum_urlopen OR (c) imports
    http.client (the hook's shape) AND imports nothing from urllib.request. A member that imports
    urllib.request must go through the policy; a sender with no http.client (``requests``,
    ``httpx``) is refused. Bound: a file importing http.client AND a third-party client is
    accepted through (c); the cells do not look past the import of http.client."""
    return _references_name(source, "forum_urlopen") or (
        _imports_http_client(source) and not _imports_urllib_request(source))


def test_sender_set_is_derived_nonempty_and_covers_the_known_senders():
    d = _sender_set()
    assert d, "the derived sender set is EMPTY: the collector found no Authorization sender"
    missing = [_rel(p) for p in _KNOWN_SENDERS if Path(os.path.realpath(p)) not in d]
    assert not missing, f"known senders absent from the derived set: {missing}"
    assert _RECEIVER not in d and not any(_is_test_path(_rel(p)) for p in d)


def test_matcher_sees_every_urlopen_spelling():
    """The matcher itself, on synthetic sources: a spelling it cannot see is a bypass it cannot catch."""
    spellings = {
        "attribute": "import urllib.request\nurllib.request.urlopen(r)\n",
        "attribute-alias": "import urllib.request as ur\nur.urlopen(r)\n",
        "from-request": "from urllib import request\nrequest.urlopen(r)\n",
        "bare": "from urllib.request import urlopen\nurlopen(r)\n",
        "aliased": "from urllib.request import urlopen as _uo\n_uo(r)\n",
    }
    spellings["package-bound-by-another-import"] = "import urllib.parse\nurllib.request.urlopen(r)\n"
    spellings["star-import"] = "from urllib.request import *\nurlopen(r)\n"
    for label, src in spellings.items():
        assert _refs_of(src, "urlopen"), f"reference matcher misses the {label} spelling"
    # a name that does not resolve to urllib.request is not a reference to it
    not_urllib = {
        "parameter named urlopen": "def f(urlopen):\n    urlopen(r)\n",
        "self.urlopen": "class C:\n    def g(self):\n        self.urlopen(r)\n",
        "local urlopen, no urllib import": "urlopen = make()\nurlopen(r)\n",
        "attribute on a non-urllib module": "import requests\nrequests.urlopen(r)\n",
    }
    for label, src in not_urllib.items():
        assert not _refs_of(src, "urlopen"), f"{label}: must NOT match"
    # a REFERENCE is a bypass even when it is not the call site: bound to a variable, called later
    reference_only = {
        "attribute-to-variable": "import urllib.request\nf = urllib.request.urlopen\nf(r)\n",
        "attribute-never-called": "import urllib.request\nf = urllib.request.urlopen\n",
        "aliased-to-variable": "from urllib.request import urlopen as _uo\nf = _uo\nf(r)\n",
        "bare-to-variable": "from urllib.request import urlopen\nf = urlopen\nf(r)\n",
    }
    for label, src in reference_only.items():
        assert _refs_of(src, "urlopen"), f"reference matcher misses the {label} spelling"
    # the import statement is not a load, and forum_urlopen is not urlopen
    assert not _refs_of("from urllib.request import urlopen\n", "urlopen")
    assert not _refs_of("import urllib.request\nforum_urlopen(r)\nr.opener.open(x)\n", "urlopen")
    assert _refs_of("from urllib.request import build_opener as bo\nbo()\n", "build_opener")
    assert _refs_of("import urllib.request\nurllib.request.build_opener()\n", "build_opener")
    assert not _refs_of("def f(build_opener):\n    build_opener()\n", "build_opener")
    assert _imports_urllib_request("from urllib import request\n")
    assert not _imports_urllib_request("import urllib.parse\nimport http.client\n")


def test_sender_predicate_is_the_union_of_header_keyword_and_token_reference():
    """D is the union of (a) a string literal naming the Authorization header in any casing,
    (b) a keyword argument named authorization (``dict(h, Authorization=t)``), (c) a reference to
    the forum token source (``ENGRAM_FORUM_TOKEN``, ``forum-token``, ``load_forum_token``).
    Never the English word in a comment or docstring."""
    in_rows = {
        "keyword argument": "h = dict(h, Authorization=t)\n",
        "keyword argument, lowercase": "h = dict(h, authorization=t)\n",
        "dict unpack plus lowercase key": 'h = {**h, "authorization": t}\n',
        "load_forum_token call": "tok = load_forum_token()\n",
        "load_forum_token attribute": "tok = forum_api.load_forum_token()\n",
        "load_forum_token import": "from forum_api import load_forum_token\n",
        "token env literal": 'name = "ENGRAM_FORUM_TOKEN"\n',
        "token file literal": 'name = "forum-token"\n',
        "dict key": 'h = {"Authorization": t}\n',
        "dict key, lowercase": 'h = {"authorization": t}\n',
        "add_header, uppercase": 'req.add_header("AUTHORIZATION", t)\n',
        "f-string literal part": 'line = f"Authorization: Bearer {t}"\n',
        "bytes literal": 'line = b"Authorization: Bearer x"\n',
        "unparseable file still counts": "def broken(:\n    'Authorization'\n",
    }
    out_rows = {
        "comment": "x = 1  # server-side authorization check\n",
        "docstring": 'def f():\n    """Server-side authorization guard"""\n',
        "variable name": "authorization_ok = True\n",
        "no mention": "import http.client\n",
        "unrelated keyword": "h = dict(Accept='x')\n",
        "token-like prose": "x = 1  # reads the forum token\n",
    }
    for label, src in in_rows.items():
        assert _is_sender_source(src), f"{label}: expected IN D"
    for label, src in out_rows.items():
        assert not _is_sender_source(src), f"{label}: expected OUT of D"


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_sender_set_is_walked_on_disk_and_includes_untracked_files(tmp_path):
    """D unions git-tracked files with an os.walk of src/ and tools/, so an untracked file in a
    code root counts. (A throwaway repo under tmp_path; nothing is added here.)"""
    root = _git_repo(tmp_path / "repo")
    (root / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "src" / "pkg" / "tests").mkdir()
    (root / "tools").mkdir()
    (root / "src" / "pkg" / "sender.py").write_text('h = {"Authorization": t}\n')
    (root / "src" / "pkg" / "__pycache__" / "skipme.py").write_text('h = {"Authorization": t}\n')
    (root / "src" / "pkg" / "tests" / "test_x.py").write_text('h = {"Authorization": t}\n')
    (root / "tools" / "plain.py").write_text("x = 1\n")
    found = [str(p.relative_to(root.resolve())) for p in _sender_set(root.resolve())]
    assert found == ["src/pkg/sender.py"], found


def test_sender_set_includes_tracked_files_outside_src_and_tools(tmp_path):
    """The tracked half of the universe: a tracked sender in a new top-level directory is in D,
    an UNTRACKED one there is not (no flood from an un-ignored .venv/), and a tracked test path
    is still excluded. The git add is in the throwaway repo only."""
    root = _git_repo(tmp_path / "repo")
    (root / "scripts").mkdir()
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "scripts" / "tracked.py").write_text('h = {"Authorization": t}\n')
    (root / "scripts" / "test_tracked.py").write_text('h = {"Authorization": t}\n')
    (root / "scripts" / "untracked.py").write_text('h = {"Authorization": t}\n')
    (root / ".venv" / "lib" / "site.py").write_text('h = {"Authorization": t}\n')
    (root / "scripts" / "mon.sh").write_text("curl -s -H \"Authorization: Bearer $ENGRAM_FORUM_TOKEN\" u\n")
    subprocess.run(["git", "add", "scripts/tracked.py", "scripts/test_tracked.py", "scripts/mon.sh"],
                   cwd=root, check=True)
    real = root.resolve()
    assert [str(p.relative_to(real)) for p in _sender_set(real)] == ["scripts/tracked.py"]
    assert [str(p.relative_to(real)) for p in _monitor_scripts(real)] == ["scripts/mon.sh"]


def test_sender_set_raises_when_git_ls_files_fails(tmp_path, monkeypatch):
    """No silent fallback to the narrower walk: a non-repo root raises."""
    root = tmp_path / "norepo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "sender.py").write_text('h = {"Authorization": t}\n')
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises((subprocess.CalledProcessError, OSError)):
        _sender_set(root.resolve())


def test_classifier_accepts_and_refuses_the_right_sources():
    """The classification rule itself, on synthetic sources (no files in D)."""
    rows = [
        ("forum_urlopen via forum_api, no urllib.request import",
         "from forum_api import forum_urlopen\nforum_urlopen(req)\n", True),
        ("urllib.request import AND forum_urlopen call",
         "import urllib.request\nfrom forum_api import forum_urlopen\nforum_urlopen(urllib.request.Request(u))\n", True),
        ("http.client sender: no urllib import, no forum_urlopen",
         "import http.client\nhttp.client.HTTPConnection(h).request('GET', '/')\n", True),
        ("urllib.request import that never references forum_urlopen",
         "import urllib.request\nurllib.request.Request(u)\n", False),
        ("requests sender with no http.client: refused",
         "import requests\nrequests.get(u, allow_redirects=True)\n", False),
        ("http.client AND urllib.request, no forum_urlopen: refused",
         "import http.client\nimport urllib.request\nurllib.request.Request(u)\n", False),
        ("from http import client sender: accepted",
         "from http import client\nclient.HTTPConnection(h)\n", True),
    ]
    for label, src, expected in rows:
        assert _is_classified(src) is expected, f"{label}: expected classified={expected}"


def test_no_sender_references_urlopen_in_any_spelling():
    """No F in D references urllib.request.urlopen (call, or a load bound to a variable); the
    senders call ``forum_urlopen``. Static references only: the ``getattr``, ``importlib``,
    ``__import__`` and rebound-module forms are matched by no cell (see ``_refs_of``)."""
    bad = [f"{_rel(p)}:{ln} references urlopen directly, bypassing the redirect policy"
           for p in _sender_set() for ln in _refs_of(p.read_text(), "urlopen")]
    assert not bad, "\n".join(bad)


def test_only_forum_api_calls_build_opener():
    bad = [f"{_rel(p)}:{ln} references build_opener outside forum_api.py"
           for p in _sender_set() if p.name != "forum_api.py"
           for ln in _refs_of(p.read_text(), "build_opener")]
    assert not bad, "\n".join(bad)


def test_every_sender_is_classified_by_property():
    """(a) forum_api.py, the policy's home; else at least one of (b) references forum_urlopen,
    (c) imports http.client AND nothing from urllib.request (the hook's shape). A member that
    imports urllib.request must go through forum_urlopen; one with no http.client is refused."""
    bad = []
    for p in _sender_set():
        if p.name == "forum_api.py":
            continue
        src = p.read_text()
        if not _is_classified(src):
            bad.append(f"{_rel(p)}: is not classified (imports urllib.request without forum_urlopen, or no http.client); "
                       "a sender must be forum_api.py itself, or reference forum_urlopen, "
                       "or import http.client and nothing from urllib.request (at least one of these)")
    assert not bad, "\n".join(bad)


_REDIRECT_NAMES = ("HTTPRedirectHandler", "OpenerDirector")


def _redirect_refs(source):
    """(name, line) of every LOAD reference to urllib.request's HTTPRedirectHandler or
    OpenerDirector, through ``_refs_of``. A class base is a load, so a subclass
    (``class H(urllib.request.HTTPRedirectHandler)``, or one whose base is a from-import or alias
    that resolves to the name) is a hit. Bound: a handler built in a spelling resolution cannot
    see (``getattr``, ``importlib``, ``__import__``, a rebound module) is matched by no cell."""
    return sorted((name, ln) for name in _REDIRECT_NAMES for ln in _refs_of(source, name))


def test_matcher_sees_every_redirect_handler_spelling():
    """Synthetic rows for both names, as for urlopen, including subclass bases."""
    for name in _REDIRECT_NAMES:
        rows = {
            "attribute": f"import urllib.request\nurllib.request.{name}\n",
            "attribute-alias": f"import urllib.request as ur\nur.{name}\n",
            "from-request": f"from urllib import request\nrequest.{name}\n",
            "bare": f"from urllib.request import {name}\n{name}\n",
            "aliased": f"from urllib.request import {name} as H\nH\n",
            "star-import": f"from urllib.request import *\n{name}\n",
            "package-bound-by-another-import": f"import urllib.parse\nurllib.request.{name}\n",
            "uncalled reference": f"import urllib.request\nX = urllib.request.{name}\n",
            "subclass, attribute base": f"import urllib.request\nclass H(urllib.request.{name}):\n    pass\n",
            "subclass, aliased base": f"from urllib.request import {name} as B\nclass H(B):\n    pass\n",
        }
        for label, src in rows.items():
            assert _redirect_refs(src), f"{name}: redirect matcher misses the {label} spelling"
        for label, src in {
            "parameter": f"def f({name}):\n    {name}()\n",
            "non-urllib module": f"import requests\nrequests.{name}\n",
            "import statement alone": f"from urllib.request import {name}\n",
        }.items():
            assert not _redirect_refs(src), f"{name}, {label}: must NOT match"


def test_only_forum_api_defines_a_redirect_handler():
    """No member of D but forum_api.py references urllib.request's HTTPRedirectHandler or
    OpenerDirector (so none subclasses or builds one); forum_api.py itself does (a floor, so the
    matcher is not vacuous). The domain is D, derived, not a glob. Bound: see ``_redirect_refs``."""
    d = _sender_set()
    bad = [f"{_rel(p)}:{ln} references {name} outside forum_api.py"
           for p in d if p.name != "forum_api.py"
           for name, ln in _redirect_refs(p.read_text())]
    assert not bad, "\n".join(bad)
    api = [p for p in d if p.name == "forum_api.py"]
    assert api and {n for n, _ in _redirect_refs(api[0].read_text())} == set(_REDIRECT_NAMES)


def test_monitor_scripts_are_derived_nonempty_and_cover_the_known_monitors():
    names = {p.name for p in _monitor_scripts()}
    assert names >= {"forum-updates-monitor.sh", "forum-mention-monitor.sh"}, names


@pytest.mark.parametrize("script", _monitor_scripts(), ids=lambda p: p.name)
def test_monitor_curl_never_follows_redirects(script):
    name = script.name
    code = [ln for ln in script.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    invocations = [ln for ln in code if re.search(r"(^|[|(\s])curl\s+-", ln)]
    assert len(invocations) == 2, invocations   # the token arm and the no-token arm; not vacuous
    follow = re.compile(r"\s-[A-Za-z]*L[A-Za-z]*(\s|$)")
    for ln in invocations:      # `[ -L file ]` (a symlink test) elsewhere is not a curl flag
        assert not follow.search(ln), f"{name}: {ln.strip()}"
    for ln in code:
        assert "--location" not in ln, f"{name}: {ln.strip()}"
