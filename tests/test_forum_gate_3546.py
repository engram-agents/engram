"""Contract-first tests for the "forum configured" gate of the three forum-backed
prompt hooks (part of #3546).

The contract (G1-G5), for the forum, baton and inter-agent prompt hooks:

  G1  "forum configured" means config["forum"] is a dict whose "url" is a
      non-empty string after .strip(). agent_name alone is identity, NOT forum
      configuration. A non-string url, a non-dict forum, an empty/whitespace
      url and a missing forum key are all NOT configured; the gate never raises.
  G2  Not configured -> exit 0, EMPTY stdout, EMPTY stderr, NO file written,
      NO network connection.
  G3  Configured -> behaviour unchanged (unreachable lines, healthy path).
  G4  forum.url set but no agent_name stays configured.
  G5  FORUM_URL in the environment does NOT configure by itself.

Cell classes (the dev-code red/green record is part of the PR handoff):
  * (a) agent_name-only silent + zero connections  -- RED on unmodified dev
  * (e) non-configured shapes with agent_name set  -- RED on dev
  * (f) env FORUM_URL does not configure           -- RED on dev
  * (h) unit cells on the gate                     -- the agent_name-only and
        non-string-url rows are RED on dev
  * (b) {} silent, (c)/(d) configured-and-dead, (g) healthy -- controls,
        GREEN on dev.

Subprocess cells run the REAL hooks; (h) loads each hook as a module.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOKS = {
    "forum": "engram-forum-prompt-hook.py",
    "baton": "engram-baton-prompt-hook.py",
    "inter": "engram-inter-agent-prompt-hook.py",
}
# The line each hook injects when its forum is configured but unreachable.
UNREACHABLE = {
    "forum": "forum unreachable (connect)",
    "baton": "UCS unreachable",
    "inter": "DM unread check unavailable (UCS)",
}
STDIN = json.dumps({"session_id": "t1", "prompt": "hello",
                    "hook_event_name": "UserPromptSubmit", "cwd": "/tmp"})
AGENT = "testbot"


@pytest.fixture(params=list(HOOKS))
def hook(request):
    return request.param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _home(tmp_path: Path, cfg) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(cfg))
    return home


def _tree(home: Path) -> set:
    return {str(p.relative_to(home)) for p in home.rglob("*")}


def _run(hook: str, home: Path, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT", "ENGRAM_DATA_DIR")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    # Hermetic: a developer host may run a real forum on the default
    # localhost:5002; point the env fallback at a dead port so no cell can
    # reach it. (The env var does not configure a hook -- G5 -- so this only
    # changes where an UNGATED hook would have gone.)
    env["FORUM_URL"] = _DEAD_ENV_URL
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(HOOK_DIR / HOOKS[hook])], input=STDIN,
        capture_output=True, text=True, env=env, timeout=60,
    )


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


_DEAD_ENV_URL = _dead_url()


def _injected(r) -> str:
    out = r.stdout.strip()
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _assert_silent(r, home: Path):
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), (r.stdout, r.stderr)
    assert _tree(home) == {"config.json"}, _tree(home)


class _Listener:
    """A raw listening socket that counts accept()s and drops each connection."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.accepts = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepts += 1
            conn.close()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2)
        self.sock.close()


@pytest.fixture
def listener():
    lst = _Listener()
    yield lst
    lst.stop()


class _Forum:
    """A healthy local forum answering the three hooks' read endpoints."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/threads"):
                    return self._send({"threads": [
                        {"id": "1", "title": "t1"}, {"id": "2", "title": "t2"}]})
                if "/inbox" in self.path:
                    return self._send({"inbox": [], "unread_all": 0})
                if self.path.startswith("/api/projects"):
                    return self._send({"projects": [{
                        "project_id": "pool-x", "title": "x", "status": "in-progress",
                        "turn": AGENT, "turn_since": "2026-01-01T12:00:00Z",
                        "turn_reason": "your move", "github": ""}]})
                if self.path.startswith("/api/updates"):
                    return self._send({"updates": [{
                        "seq": 7, "sender": "luria", "body": "# hello\nbody"}],
                        "as_of": 7, "ts": "2026-01-01T12:00:00Z"})
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                return self._send({})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._t.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def forum():
    f = _Forum()
    yield f
    f.stop()


# ---------------------------------------------------------------------------
# (a) agent_name alone: silent, writes nothing, connects to nothing (G1, G2)
# ---------------------------------------------------------------------------

def test_a_agent_name_alone_is_silent_and_writes_nothing(hook, tmp_path):
    home = _home(tmp_path, {"agent_name": "x"})
    _assert_silent(_run(hook, home), home)


def test_a_agent_name_alone_makes_no_connection(hook, tmp_path, listener):
    """FORUM_URL points at a socket we count; with no forum.url the hook must
    not even look at it (and, per G5, the env var does not configure)."""
    home = _home(tmp_path, {"agent_name": "x"})
    r = _run(hook, home, {"FORUM_URL": listener.url})
    _assert_silent(r, home)
    assert listener.accepts == 0


def test_a_control_forum_url_set_does_connect(hook, tmp_path, listener):
    home = _home(tmp_path, {"agent_name": "x", "forum": {"url": listener.url}})
    _run(hook, home)
    assert listener.accepts >= 1


# ---------------------------------------------------------------------------
# (b) empty config: silent (control)
# ---------------------------------------------------------------------------

def test_b_empty_config_is_silent(hook, tmp_path):
    home = _home(tmp_path, {})
    _assert_silent(_run(hook, home), home)


# ---------------------------------------------------------------------------
# (c)/(d) configured + nothing listening: the hook's unreachable output
# ---------------------------------------------------------------------------

def test_c_configured_unreachable_speaks(hook, tmp_path):
    home = _home(tmp_path, {"agent_name": "x", "forum": {"url": _dead_url()}})
    r = _run(hook, home)
    assert r.returncode == 0
    assert UNREACHABLE[hook] in _injected(r), (r.stdout, r.stderr)


def test_d_url_without_agent_name_is_still_configured(hook, tmp_path):
    url = _dead_url()
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    h1 = _home(tmp_path / "with", {"agent_name": "x", "forum": {"url": url}})
    h2 = _home(tmp_path / "without", {"forum": {"url": url}})
    r1, r2 = _run(hook, h1), _run(hook, h2)
    assert r2.returncode == 0
    assert UNREACHABLE[hook] in _injected(r2), (r2.stdout, r2.stderr)
    assert UNREACHABLE[hook] in _injected(r1)


# ---------------------------------------------------------------------------
# (e) shapes of `forum` that are NOT configured, with agent_name set
# ---------------------------------------------------------------------------

NOT_CONFIGURED_FORUM = [
    {}, {"url": ""}, {"url": "   "}, {"url": 5}, {"url": None},
    {"url": ["http://x"]}, "http://x", None, [],
]


@pytest.mark.parametrize("forum_val", NOT_CONFIGURED_FORUM,
                         ids=[repr(v) for v in NOT_CONFIGURED_FORUM])
def test_e_not_configured_shapes_are_silent_no_traceback(hook, tmp_path, forum_val):
    home = _home(tmp_path, {"agent_name": "x", "forum": forum_val})
    r = _run(hook, home)
    assert "Traceback" not in r.stderr, r.stderr
    _assert_silent(r, home)


# ---------------------------------------------------------------------------
# (f) FORUM_URL in the environment does not configure by itself (G5)
# ---------------------------------------------------------------------------

def test_f_env_forum_url_alone_does_not_configure(hook, tmp_path):
    home = _home(tmp_path, {"agent_name": "x"})
    _assert_silent(_run(hook, home, {"FORUM_URL": _dead_url()}), home)


# ---------------------------------------------------------------------------
# (g) healthy forum with forum.url set: the hook's normal output (control)
# ---------------------------------------------------------------------------

HEALTHY_MARKER = {
    "forum": "2 new forum posts since last read",
    "baton": "1 baton in your court",
    "inter": "1 new message from luria",
}


def test_g_healthy_forum_speaks_as_on_dev(hook, tmp_path, forum):
    home = _home(tmp_path, {"agent_name": AGENT, "forum": {"url": forum.url}})
    r = _run(hook, home)
    assert r.returncode == 0, r.stderr
    ctx = _injected(r)
    assert HEALTHY_MARKER[hook] in ctx, (r.stdout, r.stderr)
    assert UNREACHABLE[hook] not in ctx


# ---------------------------------------------------------------------------
# (h) unit cells on each hook's _is_forum_configured over the G1 table
# ---------------------------------------------------------------------------

def _load(hook: str, tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        f"gate3546_{hook}", HOOK_DIR / HOOKS[hook])
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(tmp_path)}):
        spec.loader.exec_module(mod)
    return mod


CONFIGURED = [
    {"forum": {"url": "http://x:5002"}},
    {"forum": {"url": "  http://x:5002  "}},
    {"agent_name": "a", "forum": {"url": "http://x:5002"}},
    {"agent_name": "", "forum": {"url": "http://x:5002"}},
]
UNCONFIGURED = [
    {}, {"agent_name": "a"}, {"agent_name": "a", "forum": {}},
    {"agent_name": "a", "forum": {"url": ""}},
    {"agent_name": "a", "forum": {"url": "   "}},
    {"agent_name": "a", "forum": {"url": 5}},
    {"agent_name": "a", "forum": {"url": None}},
    {"agent_name": "a", "forum": {"url": ["http://x"]}},
    {"agent_name": "a", "forum": "http://x"},
    {"agent_name": "a", "forum": None},
    {"agent_name": "a", "forum": []},
    {"agent_name": 5}, {"agent_name": None}, {"mode": "multi"},
]


@pytest.mark.parametrize("cfg", CONFIGURED, ids=[json.dumps(c) for c in CONFIGURED])
def test_h_gate_true_over_g1_table(hook, tmp_path, cfg):
    assert _load(hook, tmp_path)._is_forum_configured(cfg) is True


@pytest.mark.parametrize("cfg", UNCONFIGURED, ids=[json.dumps(c) for c in UNCONFIGURED])
def test_h_gate_false_and_total_over_g1_table(hook, tmp_path, cfg):
    assert _load(hook, tmp_path)._is_forum_configured(cfg) is False
