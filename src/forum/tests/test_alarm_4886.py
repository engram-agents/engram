"""Contract cells for the forum backup alarm (engram-alpha#4886): it could never post.

Written BEFORE the implementation (contract-first, #3045). Two defects: the installed
tools/ tree lacked forum_api.py (the alarm imports ForumClient lazily, so it died on
ImportError only when a backup had already failed), and the alarm posted under the name
``forum-backup`` with the forum owner's token (the server answers 403 identity mismatch).

The contract (C1-C7):

  C1  the installer ships every module the shipped tools import, lazy imports included.
  C2  every installer run rotates the alarm's own token: alarm home <service-dir>/alarm-home
      (0700), revoke any live ``forum-backup`` token, remove the old token file, issue a
      fresh one into <alarm-home>/forum-token; the unit sets ENGRAM_HOME to the alarm home
      and keeps FORUM_BACKUP_ALARM_AGENT=forum-backup.
  C3  the installed token's identity is verified by behaviour, never by file existence:
      GET /api/agent/forum-backup/mentions with the token read from the file answers 200,
      and the same GET with a deliberately invalid token answers 401. Anything else fails
      the install, naming both status codes. With no live server (--no-start, --dry-run)
      the file properties are still asserted and one line says identity was NOT verified.
  C4  a token file / alarm home wider than 0600 / 0700 is refused, naming path and mode.
  C5  an empty FORUM_BACKUP_ALARM_MENTION is refused by the installer; the alarm itself
      exits nonzero (and posts nothing) when it would post with zero mentions, and on any
      non-2xx answer.
  C6  the alarm thread body's first line starts ``forum-backup:``.
  C7  revocation is written down in the unit template and the installer.

HERMETIC: the tree comes from running the REAL install-forum-service.sh (the helpers of
test_deploy_shape_3568), the forum is the REAL server (create_app on a temp DB, on a
loopback port, in a thread) so the name/token rule under test is the server's own, and
the token is issued by the real ``forum.admin`` into that same DB.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_deploy_shape_3568 import (ALARM, ALARM_TEMPLATE, DEPLOY, INSTALLER, _hermetic_env,
                                     _install)

_REPO = Path(__file__).resolve().parents[3]
MENTIONS = "/api/agent/forum-backup/mentions"


# ---------------------------------------------------------------- helpers ----

def _prep_live(tmp_path: Path) -> Path:
    """A service dir whose venv python is a REAL interpreter (a wrapper: the installer's
    `-m forum.server --verify-only` probe is answered 0, everything else runs for real)
    and whose forum.db is a real, initialised forum DB."""
    svc = tmp_path / "forum"
    (svc / "app").mkdir(parents=True)
    py = svc / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        '#!/bin/sh\ncase "$*" in *forum.server*) exit 0;; esac\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n')
    py.chmod(0o755)
    import sqlite3
    from forum.db import init_db
    conn = sqlite3.connect(str(svc / "forum.db"))
    init_db(conn)
    conn.close()
    return svc


class _Server:
    """The REAL forum server on a loopback port, in a thread."""

    def __init__(self, svc: Path, mode: str = "enforce"):
        from werkzeug.serving import make_server
        from forum.server import create_app
        self.report = svc / "auth-report.jsonl"
        kw = {}
        if mode == "report":
            until = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            kw = {"auth_report": str(self.report), "auth_report_until": until}
        app = create_app(str(svc / "forum.db"), str(svc / "forum-audit.jsonl"),
                         auth_mode=mode, **kw)
        self.srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self.srv.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    svc = _prep_live(tmp_path)
    made = []

    def start(mode="enforce"):
        s = _Server(svc, mode)
        made.append(s)
        return s
    yield SimpleNamespace(svc=svc, start=start)
    for s in made:
        s.stop()


def _install_live(tmp_path: Path, port: int, *, no_start: bool = False, **kw):
    kw.setdefault("env_extra", {}).setdefault("FORUM_BACKUP_ALARM_MENTION", "someone")
    return _install(tmp_path, prep=False, no_start=no_start,
                    extra_args=("--port", str(port)) + tuple(kw.pop("extra_args", ())), **kw)


def _get(url: str, token: str | None) -> int:
    req = urllib.request.Request(url)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class _Recorder:
    """A recording HTTP stub: answers ``status`` to everything and keeps what it saw."""

    def __init__(self, status: int = 200):
        self.seen: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                outer.seen.append({"method": self.command, "path": self.path,
                                   "auth": self.headers.get("Authorization"), "body": body})
                data = b'{"ok": true, "mentions": []}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = _do

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def recorder():
    made = []

    def make(status=200):
        r = _Recorder(status)
        made.append(r)
        return r
    yield make
    for r in made:
        r.stop()


def _unit_env(ctx, *, drop: tuple = ()) -> dict:
    """The Environment= the INSTALLED alarm unit (and its mention drop-in) declares."""
    env: dict = {}
    for f in (ctx.unit_dir / ALARM, ctx.unit_dir / f"{ALARM}.d" / "mention.conf"):
        for line in f.read_text().splitlines():
            if line.startswith("Environment="):
                for tok in shlex.split(line[len("Environment="):]):
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        env[k] = v
    for k in drop:
        env.pop(k, None)
    return env


def _run_alarm(ctx, tmp_path: Path, url: str, *, env_over: dict | None = None,
               drop: tuple = ()) -> subprocess.CompletedProcess:
    """Run the INSTALLED forum_backup.py --alarm, isolated from the checkout: no
    PYTHONPATH, -s, a clean cwd, the unit's own Environment=, a stub journalctl."""
    stub = tmp_path / "alarm-stubs"
    stub.mkdir(exist_ok=True)
    j = stub / "journalctl"
    j.write_text("#!/bin/sh\necho 'line one'\necho 'line two'\n")
    j.chmod(0o755)
    clean = tmp_path / "clean-cwd"
    clean.mkdir(exist_ok=True)
    env = {"PATH": f"{stub}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path / "home"),
           "FORUM_URL": url}
    env.update(_unit_env(ctx, drop=drop))
    env.update(env_over or {})
    return subprocess.run(
        [sys.executable, "-s", str(ctx.svc / "app" / "tools" / "forum_backup.py"),
         "--alarm", "--unit", "engram-forum-backup.service"],
        capture_output=True, text=True, env=env, cwd=clean, timeout=60)


def _token(ctx) -> str:
    return (ctx.svc / "alarm-home" / "forum-token").read_text().strip()


# ------------------------------------------------------------------- K1 ------

def _requirement_names(text: str) -> set:
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.-]+)", line)
        if m:
            out.add(m.group(1).lower().replace("-", "_"))
    return out


_ALIASES = {"bs4": "beautifulsoup4", "markdown_it": "markdown_it_py"}


def _unresolved_imports(svc: Path) -> list:
    app = svc / "app"
    tools = app / "tools"
    dists = _requirement_names((_REPO / "src" / "forum" / "requirements.txt").read_text())
    bad = []
    for py in sorted(tools.glob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [(a.name, node.lineno) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = py.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    mods = [(("." * node.level) + (node.module or ""), node.lineno)]
                    ok = (base / ((node.module or "").replace(".", "/") + ".py")).exists() or \
                         (base / (node.module or "").replace(".", "/")).is_dir()
                    if not ok:
                        bad.append((py.name, mods[0][0], node.lineno))
                    continue
                mods = [(node.module, node.lineno)]
            else:
                continue
            for mod, line in mods:
                top = mod.split(".")[0]
                if top in sys.stdlib_module_names:
                    continue
                rel = Path(*mod.split("."))
                if any((root / rel).with_suffix(".py").is_file() or (root / rel).is_dir()
                       for root in (tools, app)):
                    continue
                if _ALIASES.get(top, top).lower().replace("-", "_") in dists:
                    continue
                bad.append((py.name, mod, line))
    return bad


def test_K1_every_import_of_every_installed_tool_resolves(tmp_path):
    ctx = _install(tmp_path)
    assert ctx.r.returncode == 0, ctx.both
    assert _unresolved_imports(ctx.svc) == []


# ------------------------------------------------------------------- K2 ------

def test_K2_alarm_posts_as_forum_backup_in_enforce_mode(tmp_path, live):
    server = live.start("enforce")
    ctx = _install_live(tmp_path, server.port)
    assert ctx.r.returncode == 0, ctx.both
    # The production situation: the forum owner's OWN token sits in the default home the
    # alarm would fall back to. Without the unit's ENGRAM_HOME the alarm would send it
    # under the name forum-backup, and the server answers 403 identity mismatch.
    import sqlite3
    from forum.db import issue_token
    conn = sqlite3.connect(str(live.svc / "forum.db"))
    owner_tok = issue_token(conn, "owner-seat")
    conn.close()
    owner_home = tmp_path / "home" / ".engram"
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "forum-token").write_text(owner_tok + "\n")
    (owner_home / "forum-token").chmod(0o600)
    r = _run_alarm(ctx, tmp_path, server.url)
    assert r.returncode == 0, r.stdout + r.stderr
    conn = sqlite3.connect(str(live.svc / "forum.db"))
    posts = conn.execute("SELECT body_md FROM posts").fetchall()
    conn.close()
    assert len(posts) == 1
    assert posts[0][0].splitlines()[0].startswith("forum-backup:"), posts[0][0]


# ------------------------------------------------------------------- K3 ------

def test_K3_token_path_follows_engram_home(tmp_path, recorder):
    rec = recorder(200)
    home = tmp_path / "some-home"
    home.mkdir(mode=0o700)
    (home / "forum-token").write_text("tok-from-home\n")
    (home / "forum-token").chmod(0o600)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from forum_api import ForumClient\n"
            "ForumClient(%r).get('/api/agent/forum-backup/mentions')\n"
            % (str(_REPO / "tools"), rec.url))
    r = subprocess.run([sys.executable, "-s", "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "ENGRAM_HOME": str(home), "HOME": str(tmp_path)},
                       cwd=tmp_path, timeout=60)
    assert r.returncode == 0, r.stderr
    assert [s["auth"] for s in rec.seen] == ["Bearer tok-from-home"]


# ------------------------------------------------------------------- K4 ------

def test_K4_installer_refuses_an_empty_mention(tmp_path):
    ctx = _install(tmp_path, env_extra={"FORUM_BACKUP_ALARM_MENTION": ""})
    assert ctx.r.returncode != 0
    assert "FORUM_BACKUP_ALARM_MENTION" in ctx.err and "empty" in ctx.err.lower(), ctx.both
    assert not (ctx.unit_dir / ALARM).exists()


def test_K4_the_alarm_exits_nonzero_and_posts_nothing_with_zero_mentions(tmp_path, recorder):
    ctx = _install(tmp_path, env_extra={"FORUM_BACKUP_ALARM_MENTION": "someone"})
    assert ctx.r.returncode == 0, ctx.both
    rec = recorder(200)
    r = _run_alarm(ctx, tmp_path, rec.url, env_over={"FORUM_BACKUP_ALARM_MENTION": ""})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "mention" in r.stderr.lower()
    assert rec.seen == []


# ------------------------------------------------------------------- K5 ------

def test_K5_alarm_home_wider_than_0700_is_refused(tmp_path, live):
    home = live.svc / "alarm-home"
    home.mkdir()
    home.chmod(0o755)
    ctx = _install_live(tmp_path, 1)
    assert ctx.r.returncode != 0
    assert str(home) in ctx.err and "0755" in ctx.err, ctx.both


def test_K5_token_file_wider_than_0600_is_refused(tmp_path, live):
    home = live.svc / "alarm-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    (home / "forum-token").write_text("x\n")
    (home / "forum-token").chmod(0o644)
    ctx = _install_live(tmp_path, 1)
    assert ctx.r.returncode != 0
    assert str(home / "forum-token") in ctx.err and "0644" in ctx.err, ctx.both


# ------------------------------------------------------------------- K6 ------

def test_K6_reinstall_rotates_the_token(tmp_path, live):
    server = live.start("enforce")
    first = _install_live(tmp_path, server.port)
    assert first.r.returncode == 0, first.both
    tok1 = _token(first)
    assert (live.svc / "alarm-home").stat().st_mode & 0o777 == 0o700
    assert (live.svc / "alarm-home" / "forum-token").stat().st_mode & 0o777 == 0o600
    assert _get(server.url + MENTIONS, tok1) == 200
    second = _install_live(tmp_path, server.port)
    assert second.r.returncode == 0, second.both
    tok2 = _token(second)
    assert tok1 != tok2
    assert _get(server.url + MENTIONS, tok1) == 401, "the first token must be revoked"
    assert _get(server.url + MENTIONS, tok2) == 200


# ------------------------------------------------------------------- K7 ------

def test_K7_in_report_mode_the_check_sends_a_real_token_and_a_bad_one(tmp_path, live):
    """Report mode serves a request with NO Authorization header as the named identity, so a
    check that forgot its header would pass there. The server's own report file shows what the
    installer really sent: one `match` for forum-backup, one `invalid`, and no `absent`."""
    server = live.start("report")
    ctx = _install_live(tmp_path, server.port, extra_args=(
        "--auth-mode", "report", "--auth-report", str(server.report),
        "--auth-report-until", (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    assert ctx.r.returncode == 0, ctx.both
    lines = [json.loads(x) for x in server.report.read_text().splitlines() if x.strip()]
    mine = [x for x in lines if x["method"] == "GET" and x["named"] == ["forum-backup"]]
    verdicts = sorted(x["verdict"] for x in mine)
    assert verdicts == ["invalid", "match"], lines
    assert next(x for x in mine if x["verdict"] == "match")["token_agent"] == "forum-backup"


# ------------------------------------------------------------------- K8 ------

def test_K8_a_server_that_accepts_a_bad_token_fails_the_install(tmp_path, live, recorder):
    rec = recorder(200)  # answers 200 to everything, including the bad-token control
    ctx = _install_live(tmp_path, rec.port)
    assert ctx.r.returncode != 0, ctx.both
    assert "real token answered HTTP 200" in ctx.err, ctx.err
    assert "invalid token answered HTTP 200 (want 401)" in ctx.err, ctx.err
    assert any(s["auth"] and s["auth"].startswith("Bearer ") for s in rec.seen)


# ------------------------------------------------------------------- K9 ------

def test_K9_no_start_asserts_the_file_and_says_identity_was_not_verified(tmp_path, live):
    ctx = _install_live(tmp_path, 1, no_start=True)
    assert ctx.r.returncode == 0, ctx.both
    tok = live.svc / "alarm-home" / "forum-token"
    assert tok.stat().st_mode & 0o777 == 0o600 and tok.read_text().strip()
    lines = [x for x in ctx.out.splitlines() if "NOT verified" in x]
    assert len(lines) == 1 and "no live server" in lines[0], ctx.out


def test_K9_dry_run_asserts_an_existing_token_file_and_says_not_verified(tmp_path, live):
    home = live.svc / "alarm-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    (home / "forum-token").write_text("abc\n")
    (home / "forum-token").chmod(0o600)
    ctx = _install_live(tmp_path, 1, dry_run=True, no_start=True)
    assert ctx.r.returncode == 0, ctx.both
    assert len([x for x in ctx.out.splitlines() if "NOT verified" in x]) == 1, ctx.out
    assert (home / "forum-token").read_text() == "abc\n", "a dry run must not rotate"


def test_K9_dry_run_refuses_an_empty_or_wide_token_file(tmp_path, live):
    home = live.svc / "alarm-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    (home / "forum-token").write_text("")
    (home / "forum-token").chmod(0o600)
    ctx = _install_live(tmp_path, 1, dry_run=True, no_start=True)
    assert ctx.r.returncode != 0 and "empty" in ctx.err, ctx.both
    (home / "forum-token").write_text("abc\n")
    (home / "forum-token").chmod(0o640)
    ctx = _install_live(tmp_path, 1, dry_run=True, no_start=True)
    assert ctx.r.returncode != 0 and "0640" in ctx.err, ctx.both


# ------------------------------------------------------------------ K10 ------

@pytest.mark.parametrize("status", [403, 500])
def test_K10_the_alarm_exits_nonzero_with_a_log_line_on_any_non_2xx(tmp_path, recorder, status):
    ctx = _install(tmp_path, env_extra={"FORUM_BACKUP_ALARM_MENTION": "someone"})
    assert ctx.r.returncode == 0, ctx.both
    (ctx.svc / "alarm-home").mkdir(exist_ok=True, mode=0o700)
    rec = recorder(status)
    r = _run_alarm(ctx, tmp_path, rec.url, env_over={"ENGRAM_FORUM_TOKEN": "t"})
    assert r.returncode != 0
    assert "alarm post failed" in r.stderr and str(status) in r.stderr, r.stderr
    assert len(rec.seen) == 1


# ------------------------------------------------------------------ K11 ------

def test_K11_revocation_is_written_down_and_the_unit_is_wired():
    line = "the alarm's token is revoked by name: `forum.admin token revoke forum-backup`"
    assert line in ALARM_TEMPLATE.read_text()
    assert line in INSTALLER.read_text()
    unit = ALARM_TEMPLATE.read_text()
    assert re.search(r"^Environment=ENGRAM_HOME=\{\{FORUM_HOME\}\}/alarm-home$", unit, re.M)
    assert re.search(r"^Environment=FORUM_BACKUP_ALARM_AGENT=forum-backup$", unit, re.M)
