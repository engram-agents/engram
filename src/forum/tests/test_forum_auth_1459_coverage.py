"""#1459 PR A, second half: coverage enumerated from the url_map, the structural
"the guard is the only identity reader" cell, team-internal reads, the board/DM
viewers, token delivery and the installer's refusal.

Opts out of the suite-wide auto-auth plumbing (``raw_auth``) like its sibling.
"""

import ast
import os
import sqlite3
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from forum import db
from forum.db import init_db
from forum.server import _parse_args

from .test_forum_auth_1459 import (  # noqa: F401 (owned_tmp is a fixture)
    _auth, _count, _fill, _issue, _make_app, _post, owned_tmp)

pytestmark = pytest.mark.raw_auth


# 9 (explicit halves; enumerated from app.url_map so a future route is covered) ----
def _api_rules(app):
    return [r for r in app.url_map.iter_rules() if r.rule.startswith("/api/")]


def test_every_write_route_is_401_without_a_token(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    writes = [(r, m) for r in app.url_map.iter_rules()
              for m in sorted(r.methods & {"POST", "PUT", "PATCH", "DELETE"})]
    assert len(writes) >= 20  # 21 write routes at the time of writing; the map is really walked
    for rule, method in writes:
        resp = client.open(_fill(rule), method=method, json={"agent": "probe"})
        assert resp.status_code == 401, (method, rule.rule, resp.status_code)


def test_every_api_route_naming_an_agent_is_401_without_a_token(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    seen = 0
    for rule in _api_rules(app):
        if "GET" not in rule.methods:
            continue
        seen += 1
        resp = client.get(_fill(rule), query_string={"agent": "probe"})
        assert resp.status_code == 401, (rule.rule, resp.status_code)
    # rule strings / arguments that name an agent are guarded even with no query
    for rule in _api_rules(app):
        if "GET" in rule.methods and ("agent" in rule.rule or "name" in rule.arguments):
            seen += 1
            assert client.get(_fill(rule)).status_code == 401, rule.rule
    assert seen > 20


# (1) structural: the guard is the only reader of a caller identity from the request ----
SERVER_SRC = Path(__file__).resolve().parents[1] / "server.py"
ALLOWED_READERS = {"_auth_guard", "_body_identities"}

# R7 (round 2): any key that could carry "who is calling", not only the literal "agent".
IDENTITY_KEYS = frozenset({"agent", "sender", "from", "from_agent", "as_agent", "actor",
                           "user", "username", "poster"})

# Record roles that are NOT the caller's identity: a handler may read these from the request.
# Each is a role a record carries (who is named, not who is asking); identity is the token's.
NON_IDENTITY_ROLES = {
    "author": "baton-init record role (like colleague); declared, not the caller; the merge "
              "gate still needs a GitHub approval",
    "to_agent": "the flip/advance TARGET: who the turn goes to, not who asks",
    "counterpart": "the other party of a DM, a path segment: a target, not the caller",
    "colleague": "the named reviewer of a baton: a role, not the caller",
    "participants": "the baton's participant list: a record field, not the caller",
    "turn": "whose move it is on a baton: record state, not the caller",
}


def _identity_readers(src, keys=IDENTITY_KEYS):
    """(innermost enclosing function name, line) of every node that reads one of `keys`.

    BOUND: this is a DENY-LIST over STRING-LITERAL keys. It sees a call argument or a subscript
    that is a literal `ast.Constant` naming one of `keys`. A key built or held at runtime (for
    example `request.json[k]` with `k` computed, `K = "from"; body[K]`, or a loop over keys) is
    outside what it can see, as are identity-like keys not in `keys`. A tripwire, not a proof."""
    found = []

    def is_key(node):
        return isinstance(node, ast.Constant) and node.value in keys

    def walk(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        hit = False
        if isinstance(node, ast.Call) and any(is_key(a) for a in node.args):
            hit = True  # .get("agent"), .getlist("sender"), _require_str(.., "agent", ..)
        elif isinstance(node, ast.Subscript) and is_key(node.slice):
            hit = True  # body["agent"], f["from"]
        elif isinstance(node, ast.Dict):  # a field-spec dict naming an identity key
            hit = any(is_key(k) and isinstance(v, ast.Constant) and v.value in ("id", "idl", "text")
                      for k, v in zip(node.keys, node.values))
        if hit and fn is not None:
            found.append((fn, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, fn)

    walk(ast.parse(src), None)
    return found


def test_no_handler_reads_an_identity_key_from_the_request_outside_the_guard():
    offenders = sorted({(n, ln) for n, ln in _identity_readers(SERVER_SRC.read_text())
                        if n not in ALLOWED_READERS})
    assert offenders == [], offenders


def test_structural_cell_has_teeth():
    """Positive control: the walker sees the guard's own reads, and flags a handler that reads."""
    readers = {n for n, _ in _identity_readers(SERVER_SRC.read_text())}
    assert ALLOWED_READERS <= readers
    bad = "def h():\n    return request.args.get('agent')\n"
    assert _identity_readers(bad.replace("'agent'", '"agent"')) == [("h", 2)]
    bad2 = 'def h(data):\n    return data["agent"]\n'
    assert _identity_readers(bad2) == [("h", 2)]


@pytest.mark.parametrize("key", sorted(IDENTITY_KEYS))
@pytest.mark.parametrize("shape", ["get", "subscript", "getlist", "spec"])
def test_every_identity_key_is_caught_in_every_read_shape(key, shape):
    """R7: a handler inventing `sender` / `from` / ... is flagged, not trusted."""
    snippets = {
        "get": f'def h(data):\n    return data.get("{key}")\n',
        "subscript": f'def h(data):\n    return data["{key}"]\n',
        "getlist": f'def h():\n    return request.args.getlist("{key}")\n',
        "spec": f'def h(data):\n    return _fields(data, {{"{key}": "id"}})\n',
    }
    assert _identity_readers(snippets[shape]) == [("h", 2)], (key, shape)


def test_non_identity_roles_are_named_with_a_reason_and_disjoint_from_identity_keys():
    assert set(NON_IDENTITY_ROLES).isdisjoint(IDENTITY_KEYS)
    assert all(isinstance(r, str) and len(r) > 10 for r in NON_IDENTITY_ROLES.values())
    # a role read in a handler is invisible to the identity walker by design
    assert _identity_readers('def h(f):\n    return f["to_agent"], f.get("colleague")\n') == []



# (3) team-internal reads need a token for ANY agent ------------------------------------
INTERNAL_READS = ["/api/board/projects", "/api/board/updates", "/api/agents/online",
                  "/api/agents/board", "/api/projects", "/api/projects/x-1", "/api/dm"]


@pytest.mark.parametrize("path", INTERNAL_READS)
def test_internal_reads_need_a_token_for_any_agent(owned_tmp, path):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    assert client.get(path).status_code == 401
    tok = _issue(app, "carol")
    resp = client.get(path, headers=_auth(tok))
    assert resp.status_code in (200, 404), (path, resp.status_code)  # 404: the dummy baton is absent


def test_updates_with_a_token_and_no_agent_param_uses_the_token_agent(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    tok = _issue(app, "carol")
    assert app.test_client().get("/api/updates", headers=_auth(tok)).status_code == 200


def test_html_board_is_404_by_default_and_served_when_enabled(owned_tmp):
    off = _make_app(owned_tmp)
    assert off.test_client().get("/board").status_code == 404
    on_dir = owned_tmp / "on"
    on_dir.mkdir()
    on = _make_app(on_dir, enable_board_viewer=True)
    assert on.test_client().get("/board").status_code == 200
    assert 'href="/board"' not in off.test_client().get("/").get_data(as_text=True)
    assert 'href="/board"' in on.test_client().get("/").get_data(as_text=True)


def test_startup_discloses_board_viewer(owned_tmp, capsys):
    from forum import server

    conn = sqlite3.connect(str(owned_tmp / "f.db"))
    init_db(conn)
    db.issue_token(conn, "alice")
    server._report_auth_startup(conn, enable_dm_viewer=False, enable_board_viewer=True)
    assert "--enable-board-viewer" in capsys.readouterr().err
    assert _parse_args(["--enable-board-viewer"]).enable_board_viewer is True
    assert _parse_args([]).enable_board_viewer is False


# non-string identity stays a 422 after auth (#3436), never coerced -------------------------
def test_non_string_agent_is_422_for_an_authenticated_caller(owned_tmp):
    app = _make_app(owned_tmp)
    tok = _issue(app, "alice")
    resp = app.test_client().post(
        "/api/post", json={"agent": 5, "category_slug": "inter-agent", "title": "t", "body_md": "b"},
        headers=_auth(tok))
    assert resp.status_code == 422


def test_body_agent_may_be_omitted_by_an_authenticated_caller(owned_tmp):
    app = _make_app(owned_tmp)
    tok = _issue(app, "alice")
    body = {"category_slug": "inter-agent", "title": "t", "body_md": "b"}
    resp = app.test_client().post("/api/post", json=body, headers=_auth(tok))
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    conn = sqlite3.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT 1 FROM agents WHERE name='alice'").fetchone()
    conn.close()


# (4) token delivery ----------------------------------------------------------------
def test_token_issue_write_to_is_0600_and_stdout_has_no_token(owned_tmp, capsys):
    from forum import admin

    p = str(owned_tmp / "f.db")
    target = owned_tmp / "forum-token"
    rc = admin.main(["--db", p, "token", "issue", "alice", "--write-to", str(target)])
    out, err = capsys.readouterr()
    assert rc == 0
    raw = target.read_text().strip()
    assert len(raw) >= 43 and raw not in out and raw not in err
    assert out.split() == [str(target), "alice"]
    assert (target.stat().st_mode & 0o777) == 0o600
    conn = sqlite3.connect(p)
    assert db.resolve_token(conn, raw) == "alice"
    conn.close()


def test_token_issue_write_to_refuses_existing_path_and_mints_nothing(owned_tmp, capsys):
    from forum import admin

    p = str(owned_tmp / "f.db")
    target = owned_tmp / "forum-token"
    target.write_text("keep me\n")
    rc = admin.main(["--db", p, "token", "issue", "alice", "--write-to", str(target)])
    capsys.readouterr()
    assert rc != 0
    assert target.read_text() == "keep me\n"
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0] == 0
    conn.close()
    assert admin.main(["--db", p, "token", "issue", "alice", "--write-to", str(target), "--force"]) == 0
    assert target.read_text().strip() != "keep me"


def test_token_issue_write_to_refuses_world_writable_parent(owned_tmp, capsys):
    from forum import admin

    d = owned_tmp / "open"
    d.mkdir()
    os.chmod(d, 0o777)
    rc = admin.main(["--db", str(owned_tmp / "f.db"), "token", "issue", "alice",
                     "--write-to", str(d / "t")])
    capsys.readouterr()
    assert rc != 0 and not (d / "t").exists()


def test_token_issue_stdout_form_warns_about_history(owned_tmp, capsys):
    from forum import admin

    admin.main(["--db", str(owned_tmp / "f.db"), "token", "issue", "alice"])
    _, err = capsys.readouterr()
    assert "shell history" in err


def test_token_count(owned_tmp, capsys):
    from forum import admin

    p = str(owned_tmp / "f.db")
    admin.main(["--db", p, "token", "count"])
    assert capsys.readouterr().out.strip() == "0"
    admin.main(["--db", p, "token", "issue", "alice", "--write-to", str(owned_tmp / "t")])
    capsys.readouterr()
    admin.main(["--db", p, "token", "count"])
    assert capsys.readouterr().out.strip() == "1"


# (5) install-time refusal -----------------------------------------------------------
INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "install-forum-service.sh"


def _install(owned_tmp, *extra, db_tokens=0, revoke=False, path_prefix=None, unit=None):
    svc = owned_tmp / "svc"
    (svc / "app").mkdir(parents=True, exist_ok=True)  # the installer refuses a foreign non-empty dir
    conn = sqlite3.connect(str(svc / "forum.db"))
    init_db(conn)
    for i in range(db_tokens):
        db.issue_token(conn, f"agent{i}")
        if revoke:
            db.revoke_tokens(conn, f"agent{i}")
    conn.close()
    home = owned_tmp / "home"
    home.mkdir(exist_ok=True)
    if unit is not None:  # an already-installed unit, as the installer's re-run guards read it
        up = home / ".config" / "systemd" / "user" / "engram-forum.service"
        up.parent.mkdir(parents=True, exist_ok=True)
        up.write_text(unit)
    env = dict(os.environ, HOME=str(home))
    env.pop("ENGRAM_HOME", None)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{os.environ['PATH']}"
    repo = Path(__file__).resolve().parents[3]
    return subprocess.run(
        ["bash", str(INSTALLER), "--dry-run", "--src", str(repo), "--service-dir", str(svc),
         "--group", "agents", *extra],
        capture_output=True, text=True, env=env)


def _exec_hosts(unit):
    """The --host value(s) on ExecStart argument lines (comments excluded)."""
    return [ln.split()[1] for ln in unit.splitlines()
            if ln.strip().startswith("--host ")]


def test_installer_default_host_is_loopback_in_the_rendered_unit(owned_tmp):
    r = _install(owned_tmp)
    assert r.returncode == 0, r.stderr
    unit = r.stdout.split("rendered unit begin")[1]
    assert _exec_hosts(unit) == ["127.0.0.1"]


def test_installer_refuses_non_loopback_host_with_no_live_token(owned_tmp):
    r = _install(owned_tmp, "--host", "0.0.0.0")
    assert r.returncode != 0
    assert "no live agent token" in r.stderr and r.stderr.count("ERROR") == 1
    assert "rendered unit begin" not in r.stdout


def test_installer_proceeds_non_loopback_with_a_live_token(owned_tmp):
    r = _install(owned_tmp, "--host", "0.0.0.0", db_tokens=1)
    assert r.returncode == 0, r.stderr
    assert _exec_hosts(r.stdout.split("rendered unit begin")[1]) == ["0.0.0.0"]


def test_installer_revoked_only_tokens_still_refuse(owned_tmp):
    r = _install(owned_tmp, "--host", "0.0.0.0", db_tokens=1, revoke=True)
    assert r.returncode != 0


def test_installer_rejects_a_malformed_host(owned_tmp):
    assert _install(owned_tmp, "--host", "0.0.0.0;rm").returncode != 0


# R1 (round 2): loopback is PARSED, never prefix-matched. Zephyr measured that the old
# `127.*` glob let `127.evil.example.com` and `127.0.0.1.nip.io` skip the zero-token refusal.
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_installer_loopback_hosts_proceed_with_zero_tokens(owned_tmp, host):
    r = _install(owned_tmp, "--host", host)
    assert r.returncode == 0, r.stderr
    assert _exec_hosts(r.stdout.split("rendered unit begin")[1]) == [host]


@pytest.mark.parametrize("host", ["127.evil.example.com", "127.0.0.1.nip.io", "127.internal.lan"])
def test_installer_refuses_a_127_prefixed_hostname_with_zero_tokens(owned_tmp, host):
    r = _install(owned_tmp, "--host", host)
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr and "rendered unit begin" not in r.stdout


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5"])
def test_installer_refuses_non_loopback_addresses_with_zero_tokens(owned_tmp, host):
    r = _install(owned_tmp, "--host", host)
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr and "rendered unit begin" not in r.stdout


def test_installer_refuses_ipv4_shorthand_127_1_as_a_chosen_fail_safe(owned_tmp):
    """`127.1` is loopback to a resolver but `ipaddress` does not parse IPv4 shorthand, so the
    installer treats it as NON-loopback and demands a live token. That is a CHOSEN fail-safe,
    not an accident: an unparsed host takes the token-check arm, never the loopback arm."""
    r = _install(owned_tmp, "--host", "127.1")
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr and "rendered unit begin" not in r.stdout


@pytest.mark.parametrize("code", [1, 127])
def test_installer_with_a_broken_python3_refuses_even_127_0_0_1(owned_tmp, code):
    """The loopback test shells out to python3. A python3 that exits non-zero (or is a stub
    that prints nothing) cannot prove loopback, so the host takes the token-check arm."""
    stub = owned_tmp / "stub"
    stub.mkdir()
    py = stub / "python3"
    py.write_text(f"#!/bin/sh\nexit {code}\n")
    py.chmod(0o755)
    r = _install(owned_tmp, "--host", "127.0.0.1", path_prefix=str(stub))
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr and "rendered unit begin" not in r.stdout


def _stub_python3(owned_tmp, body):
    stub = owned_tmp / "stub"
    stub.mkdir()
    py = stub / "python3"
    py.write_text(f"#!/bin/sh\n{body}\n")
    py.chmod(0o755)
    return str(stub)


@pytest.mark.parametrize("host", ["10.0.0.1", "0.0.0.0", "127.0.0.1"])
def test_installer_python3_that_exits_0_and_prints_nothing_never_proves_loopback(owned_tmp, host):
    """Round 3 (S3): the shell needs exit 0 AND the exact sentinel. A stub or broken shim that
    exits 0 silently used to make every host loopback (10.0.0.1 and 0.0.0.0 skipped the refusal).

    Why the rows discriminate: under the OLD exit-status-only probe, a python3 that exits 0 made
    EVERY host read as loopback (round-2 review measured 10.0.0.1, 0.0.0.0 and 127.0.0.1), so all
    three PROCEEDED; all three must now be REFUSED. 127.0.0.1 is the sharpest row: a real loopback
    host goes to the refusal arm when the instrument cannot prove itself.

    The refusal is gated on zero live tokens, so the cell uses its own fresh DB (owned_tmp is
    per-test) and asserts that precondition BEFORE the run; with a live token the refusal arm
    could not fire and a correct sentinel would read as broken."""
    dbp = owned_tmp / "svc" / "forum.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dbp))
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM agent_tokens WHERE revoked_at IS NULL"
                        ).fetchone()[0] == 0
    conn.close()
    r = _install(owned_tmp, "--host", host, path_prefix=_stub_python3(owned_tmp, "exit 0"))
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr and "rendered unit begin" not in r.stdout


@pytest.mark.parametrize("host", ["10.0.0.1", "127.0.0.1"])
def test_installer_python3_that_prints_the_wrong_token_never_proves_loopback(owned_tmp, host):
    r = _install(owned_tmp, "--host", host,
                 path_prefix=_stub_python3(owned_tmp, "echo NOT-LOOPBACK; exit 0"))
    assert r.returncode != 0, r.stdout
    assert "no live agent token" in r.stderr


def test_installer_python3_that_prints_the_sentinel_for_everything_is_the_honest_bound(owned_tmp):
    """OUT OF SCOPE, documented as the bound: a python3 on PATH that prints LOOPBACK for every
    host makes every host loopback. Whoever controls python3 on the installer's PATH controls the
    installer (they could replace any binary it runs), so this is not a defence the installer
    claims. The cell pins that the bound is exactly this and nothing wider."""
    r = _install(owned_tmp, "--host", "10.0.0.1",
                 path_prefix=_stub_python3(owned_tmp, "echo LOOPBACK"))
    assert r.returncode == 0, r.stderr


# R4 (round 2): a re-run never silently changes an existing unit's bind.
_UNIT_0000 = "[Service]\nExecStart=/x \\\n    --port 5002 \\\n    --host 0.0.0.0\n"
_UNIT_LOOPBACK = "[Service]\nExecStart=/x \\\n    --port 5002 \\\n    --host 127.0.0.1\n"


def test_installer_rerun_without_host_refuses_to_narrow_an_existing_nonloopback_unit(owned_tmp):
    r = _install(owned_tmp, db_tokens=1, unit=_UNIT_0000)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "bound to --host 0.0.0.0" in r.stderr and "Nothing was written" in r.stderr
    assert "rendered unit begin" not in r.stdout


def test_installer_rerun_with_the_same_explicit_host_keeps_it(owned_tmp):
    r = _install(owned_tmp, "--host", "0.0.0.0", db_tokens=1, unit=_UNIT_0000)
    assert r.returncode == 0, r.stderr
    assert _exec_hosts(r.stdout.split("rendered unit begin")[1]) == ["0.0.0.0"]


def test_installer_rerun_with_an_explicit_different_host_is_the_deliberate_narrowing(owned_tmp):
    r = _install(owned_tmp, "--host", "127.0.0.1", db_tokens=1, unit=_UNIT_0000)
    assert r.returncode == 0, r.stderr
    assert _exec_hosts(r.stdout.split("rendered unit begin")[1]) == ["127.0.0.1"]


def test_installer_rerun_without_host_on_a_loopback_unit_is_not_refused(owned_tmp):
    r = _install(owned_tmp, unit=_UNIT_LOOPBACK)
    assert r.returncode == 0, r.stderr


# F2 (round 1b): presence follows the board-viewer flag ------------------------------------------
def _publish_presence(app):
    tok = _issue(app, "presence-bot")
    r = app.test_client().post("/api/agents/status", headers=_auth(tok), json={
        "agent": "presence-bot", "state": "working", "activity": "SECRET-ACTIVITY-XYZ"})
    assert r.status_code == 200, r.data


def test_index_renders_no_presence_when_board_viewer_is_off(owned_tmp):
    app = _make_app(owned_tmp)
    _publish_presence(app)
    html = app.test_client().get("/").get_data(as_text=True)
    assert "presence-bot" not in html and "SECRET-ACTIVITY-XYZ" not in html
    assert "Online now" not in html and "Agents registered" not in html
    assert 'id="status-board"' not in html and 'id="board-rows"' not in html


def test_index_does_not_poll_the_board_api_when_viewer_is_off(owned_tmp):
    app = _make_app(owned_tmp)
    _publish_presence(app)
    html = app.test_client().get("/").get_data(as_text=True)
    assert "/api/agents/board" not in html
    assert "board-refresh" not in html


def test_index_renders_presence_as_of_page_load_when_board_viewer_is_on(owned_tmp):
    app = _make_app(owned_tmp, enable_board_viewer=True)
    _publish_presence(app)
    html = app.test_client().get("/").get_data(as_text=True)
    assert "presence-bot" in html and "SECRET-ACTIVITY-XYZ" in html
    assert "Online now" in html and 'id="status-board"' in html
    assert "as of page load" in html


def test_public_html_pages_render_no_presence_when_viewer_off(owned_tmp):
    app = _make_app(owned_tmp)
    _publish_presence(app)
    tok = _issue(app, "alice")
    r = app.test_client().post("/api/post", headers=_auth(tok), json={
        "agent": "alice", "category_slug": "inter-agent", "title": "t", "body_md": "b"})
    tid = r.get_json()["thread_id"]
    for path in (f"/thread/{tid}", "/search?q=t", "/packs"):
        html = app.test_client().get(path).get_data(as_text=True)
        assert "presence-bot" not in html and "SECRET-ACTIVITY-XYZ" not in html, path


# round 3 (S1): presence on the public pages needs the SAME Host check as the viewers -----------
def _presence_pages(app):
    """The three public pages that render presence, with a real thread for /thread/<id>."""
    tok = _issue(app, "alice")
    r = app.test_client().post("/api/post", headers=_auth(tok), json={
        "agent": "alice", "category_slug": "inter-agent", "title": "t", "body_md": "b"})
    return ["/", f"/thread/{r.get_json()['thread_id']}", "/search?q=t"]


@pytest.mark.parametrize("host", ["evil.example:5002", "evil.example", "10.0.0.5:5002"])
def test_public_pages_render_no_presence_to_a_foreign_host_with_viewer_on(owned_tmp, host):
    """Over the same page, a foreign Host is served (200, never 403: these are public pages)
    but without presence, the data /api/agents/board guards with a token."""
    app = _make_app(owned_tmp, enable_board_viewer=True)
    _publish_presence(app)
    for path in _presence_pages(app):
        resp = app.test_client().get(path, headers={"Host": host})
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200, (path, host, resp.status_code)
        assert "presence-bot" not in html and "SECRET-ACTIVITY-XYZ" not in html, (path, host)


@pytest.mark.parametrize("host", ["localhost:5002", "127.0.0.1", "[::1]:5002"])
def test_public_pages_render_presence_to_a_loopback_host_with_viewer_on(owned_tmp, host):
    app = _make_app(owned_tmp, enable_board_viewer=True)
    _publish_presence(app)
    for path in _presence_pages(app):
        html = app.test_client().get(path, headers={"Host": host}).get_data(as_text=True)
        assert "presence-bot" in html, (path, host)


@pytest.mark.parametrize("host", ["localhost:5002", "evil.example:5002"])
def test_public_pages_never_render_presence_with_viewer_off_whatever_the_host(owned_tmp, host):
    app = _make_app(owned_tmp)
    _publish_presence(app)
    for path in _presence_pages(app):
        html = app.test_client().get(path, headers={"Host": host}).get_data(as_text=True)
        assert "presence-bot" not in html and "SECRET-ACTIVITY-XYZ" not in html, (path, host)


# round 3 (S1/S2 amendment): shapes a test client cannot send -- duplicated Host, X-Forwarded-Host.
# Driven as RAW BYTES over a real socket to a short-lived werkzeug server on 127.0.0.1 (an
# ephemeral port, torn down in `finally`). Not curl: curl collapses two `-H Host` flags into one.
_RAW_SHAPES = {
    "dup-loopback-first": b"Host: localhost:5002\r\nHost: evil.example\r\n",
    "dup-evil-first": b"Host: evil.example\r\nHost: localhost:5002\r\n",
    "x-forwarded-host": b"Host: evil.example\r\nX-Forwarded-Host: localhost\r\n",
}


def _raw_get(app, path, header_bytes):
    """(status line, body) of one raw HTTP/1.1 GET against a live server for `app`."""
    import socket
    import threading
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        sock = socket.create_connection(("127.0.0.1", srv.server_port), timeout=10)
        try:
            sock.sendall(b"GET " + path.encode() + b" HTTP/1.1\r\n" + header_bytes
                         + b"Connection: close\r\n\r\n")
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=10)
    head, _, body = buf.partition(b"\r\n\r\n")
    return head.split(b"\r\n", 1)[0].decode(), body.decode("utf-8", "replace")


def test_raw_socket_driver_can_fail_a_loopback_host_is_served(owned_tmp):
    """Control for the raw cells below: the SAME driver with a plain loopback Host gets 200 and
    presence, so a 403 / no-presence result is the Host check, not a broken driver."""
    app = _viewer_app(owned_tmp)
    _publish_presence(app)
    status, _ = _raw_get(app, "/board", b"Host: localhost:5002\r\n")
    assert " 200 " in status, status
    status, body = _raw_get(app, "/", b"Host: localhost:5002\r\n")
    assert " 200 " in status and "presence-bot" in body, status


@pytest.mark.parametrize("shape", sorted(_RAW_SHAPES))
@pytest.mark.parametrize("path", ["/board", "/dm"])
def test_viewers_refuse_duplicate_host_and_x_forwarded_host_over_a_real_socket(
        owned_tmp, path, shape):
    status, _ = _raw_get(_viewer_app(owned_tmp), path, _RAW_SHAPES[shape])
    assert " 403 " in status, (path, shape, status)


@pytest.mark.parametrize("shape", sorted(_RAW_SHAPES))
def test_public_pages_render_no_presence_for_duplicate_host_or_x_forwarded_host(owned_tmp, shape):
    app = _make_app(owned_tmp, enable_board_viewer=True)
    _publish_presence(app)
    for path in _presence_pages(app):
        status, body = _raw_get(app, path, _RAW_SHAPES[shape])
        assert " 200 " in status, (path, shape, status)  # public page: served, never a 403
        assert "presence-bot" not in body and "SECRET-ACTIVITY-XYZ" not in body, (path, shape)


# F4 (round 1b): the shared validator refuses a trailing newline -----------------------------------
def test_is_valid_agent_name_refuses_a_trailing_newline():
    from forum.db import is_valid_agent_name

    assert is_valid_agent_name("alice")
    assert not is_valid_agent_name("alice\n")
    assert not is_valid_agent_name("alice\n\n")
    assert not is_valid_agent_name("\nalice")
    assert not is_valid_agent_name("")
    assert not is_valid_agent_name("a" * 64) and is_valid_agent_name("a" * 63)


# Round 1d: no browser-side reference to a guarded route -----------------------------------------
def test_index_has_no_board_poll_when_viewer_is_on(owned_tmp):
    app = _make_app(owned_tmp, enable_board_viewer=True)
    _publish_presence(app)
    html = app.test_client().get("/").get_data(as_text=True)
    assert "presence-bot" in html  # presence is still server-rendered
    assert "/api/agents/board" not in html
    assert "setInterval" not in html and "fetch(" not in html
    assert "board-refresh" not in html
    assert "as of page load" in html


def _route_regex(rule):
    """Regex for how a page could spell ``rule``: converters match one path segment, and the
    spelling must not continue into a longer path."""
    import re as _re

    parts = _re.split(r"(<[^>]+>)", rule.rule)
    body = "".join("[^/\"'\\s?#<>]+" if p.startswith("<") else _re.escape(p) for p in parts)
    return _re.compile(body + r"(?![A-Za-z0-9_/<{-])")


def test_no_rendered_page_references_a_guarded_route(owned_tmp):
    import re as _re

    from forum.server import _TOKEN_REQUIRED_READ_PREFIXES

    rendered = 0
    for flags in ({}, {"enable_board_viewer": True, "enable_dm_viewer": True}):
        sub = owned_tmp / ("on" if flags else "off")
        sub.mkdir()
        app = _make_app(sub, coord_root=str(sub / "coord"), **flags)
        tok = _issue(app, "alice")
        r = app.test_client().post("/api/post", headers=_auth(tok), json={
            "agent": "alice", "category_slug": "inter-agent", "title": "t", "body_md": "b"})
        assert r.status_code == 201, r.data
        _publish_presence(app)
        write_rules = [rule for rule in app.url_map.iter_rules()
                       if rule.methods & {"POST", "PUT", "PATCH", "DELETE"}]
        assert write_rules
        pages = [rule for rule in app.url_map.iter_rules()
                 if "GET" in rule.methods and not rule.rule.startswith("/api/")
                 and rule.endpoint != "static"]
        for rule in pages:
            path = _fill(rule)
            resp = app.test_client().get(path)
            if resp.status_code != 200 or "text/html" not in resp.content_type:
                continue
            rendered += 1
            text = resp.get_data(as_text=True)
            for prefix in _TOKEN_REQUIRED_READ_PREFIXES:
                assert prefix not in text, (path, flags, "references guarded read", prefix)
            for w in write_rules:
                assert not _route_regex(w).search(text), (path, flags, "references write route", w.rule)
    assert rendered > 0
    assert rendered >= 8  # index, thread, search, packs, and (flag on) board + dm pages, twice


# R2 (round 2): names that PREFIX one another must not pass an identity comparison -------------
@pytest.mark.parametrize("token_agent,named", [("ali", "alice"), ("alice", "ali"), ("alice", "alice2")])
def test_prefix_related_names_are_a_403_and_write_nothing(owned_tmp, token_agent, named):
    app = _make_app(owned_tmp)
    tok = _issue(app, token_agent)
    before = (_count(app, "threads"), _count(app, "posts"), _count(app, "agents"))
    resp = _post(app.test_client(), named, _auth(tok))
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json() == {"error": "identity mismatch",
                               "token_agent": token_agent, "named_agent": named}
    assert (_count(app, "threads"), _count(app, "posts"), _count(app, "agents")) == before


# R3 (round 2): PUT / DELETE / PATCH on ANY route are refused before routing --------------------
@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["/api/post", "/api/x", "/"])
def test_write_methods_without_a_token_are_401_on_any_route(owned_tmp, method, path):
    """No route uses PUT/DELETE, so only the method set itself can make these 401 (never a 404
    or 405 from routing, which would run before auth if the set lost a method). No body on
    purpose: a body naming an agent would 401 an /api/ path through the identity rule instead
    and hide a method dropped from the set."""
    app = _make_app(owned_tmp)
    resp = app.test_client().open(path, method=method)
    assert resp.status_code == 401, (method, path, resp.status_code)
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


# R8 (round 2): the opt-in viewers carry no token, so they check the Host header ---------------
_VIEWERS = ["/dm", "/dm/alice/bob", "/board"]


def _viewer_app(owned_tmp, **kw):
    return _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"),
                     enable_dm_viewer=True, enable_board_viewer=True, **kw)


@pytest.mark.parametrize("path", _VIEWERS)
@pytest.mark.parametrize("host", ["localhost:5002", "localhost", "127.0.0.1", "127.0.0.1:5002",
                                  "[::1]", "[::1]:5002", "LOCALHOST:5002"])
def test_viewers_serve_a_loopback_host_header(owned_tmp, path, host):
    resp = _viewer_app(owned_tmp).test_client().get(path, headers={"Host": host})
    assert resp.status_code == 200, (path, host, resp.status_code)


@pytest.mark.parametrize("path", _VIEWERS)
@pytest.mark.parametrize("host", ["evil.example:5002", "evil.example", "127.0.0.1.evil.example",
                                  "localhost.evil.example", "10.0.0.5:5002", "0.0.0.0:5002",
                                  # round 3 (S2): shapes an endswith or a userinfo-accepting check lets through
                                  "evil.localhost", "xlocalhost", "user@localhost",
                                  "localhost@evil.example", "[::1].evil.example",
                                  # round 3b: malformed port / trailing dot (the port-parse branch)
                                  "localhost.", "localhost:", "localhost:80:80", "localhost:5002x",
                                  "[::1]x", "[::1]:", "[::1]:80:80"])
def test_viewers_refuse_a_foreign_host_header(owned_tmp, path, host):
    resp = _viewer_app(owned_tmp).test_client().get(path, headers={"Host": host})
    assert resp.status_code == 403, (path, host, resp.status_code)


@pytest.mark.parametrize("path", _VIEWERS)
def test_viewers_refuse_a_request_with_no_host_header(owned_tmp, path):
    resp = _viewer_app(owned_tmp).test_client().get(path, environ_overrides={"HTTP_HOST": ""})
    assert resp.status_code == 403, (path, resp.status_code)


@pytest.mark.parametrize("path", _VIEWERS)
def test_viewers_serve_the_host_the_server_was_started_with(owned_tmp, path):
    app = _viewer_app(owned_tmp, bind_host="192.168.7.9")
    c = app.test_client()
    assert c.get(path, headers={"Host": "192.168.7.9:5002"}).status_code == 200
    assert c.get(path, headers={"Host": "192.168.7.9"}).status_code == 200
    assert c.get(path, headers={"Host": "192.168.7.10:5002"}).status_code == 403
    assert c.get(path, headers={"Host": "evil.example"}).status_code == 403


def test_viewers_off_stay_404_whatever_the_host(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    for host in ("localhost", "evil.example"):
        for path in _VIEWERS:
            assert app.test_client().get(path, headers={"Host": host}).status_code == 404


def test_the_host_check_is_for_the_viewers_only_public_pages_and_the_api_are_unchanged(owned_tmp):
    app = _viewer_app(owned_tmp)
    c = app.test_client()
    tok = _issue(app, "alice")
    assert c.get("/health", headers={"Host": "evil.example"}).status_code == 200
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 200
    r = c.get("/api/updates", headers={**_auth(tok), "Host": "evil.example"})
    assert r.status_code == 200
    assert c.get("/api/updates", headers={"Host": "evil.example"}).status_code == 401


def test_startup_disclosure_states_the_host_defence_and_its_bound(owned_tmp, capsys):
    from forum import server

    conn = sqlite3.connect(str(owned_tmp / "f.db"))
    init_db(conn)
    db.issue_token(conn, "alice")
    server._report_auth_startup(conn, enable_dm_viewer=True, enable_board_viewer=True)
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "--enable-" in ln]
    assert len(lines) == 2, lines
    for ln in lines:
        assert "Host header" in ln and "DNS rebinding" in ln, ln
        # N1 (round 3): it says what is ACCEPTED, and the bound: a non-browser client is not stopped
        assert "localhost" in ln and "127.0.0.1" in ln and "[::1]" in ln and "--host value" in ln, ln
        assert "non-browser client" in ln and "allowed Host" in ln, ln


# R9 (round 2): the docs state the plain-HTTP bound ---------------------------------------------
@pytest.mark.parametrize("doc", ["README.md", "FORUM.md"])
def test_docs_state_that_bearer_tokens_travel_in_plain_http(doc):
    text = (Path(__file__).resolve().parents[1] / doc).read_text()
    flat = " ".join(text.split())
    assert "plain HTTP" in flat, doc
    assert "never leave the host" in flat, doc
    assert "trusted network" in flat, doc
    assert "TLS termination is out of scope" in flat, doc


# R4-B1 (round 4): no python3 the installer runs under --dry-run may write bytecode -----------
# Apple's /usr/bin/python3 writes a bytecode cache under ~/Library/Caches/com.apple.python for
# what it imports (Luria measured 44 files with an empty HOME, 0 with `python3 -B`), which breaks
# the dry-run "writes nothing under HOME" invariant on a Mac only. Linux python3 writes nothing
# there, so no Linux cell can see the files; the CLASS is checked on the argv instead: every
# python3 the installer runs under --dry-run is invoked `python3 -B -c ...`.
def _logging_python3(owned_tmp):
    """A python3 first on PATH that logs one OK/BAD record per call OUTSIDE HOME, then execs the real
    one. OK = argv starts `-B -c` (the -c body can span lines, so only the flags are recorded)."""
    real = shutil.which("python3")
    assert real, "no python3 on PATH to exec"
    stub = owned_tmp / "logstub"
    stub.mkdir()
    log = owned_tmp / "python3-argv.log"  # HOME is owned_tmp/home: the log is not under it
    py = stub / "python3"
    py.write_text(
        '#!/bin/sh\n'
        f'if [ "$1" = "-B" ] && [ "$2" = "-c" ]; then echo OK >> "{log}"; else echo BAD >> "{log}"; fi\n'
        f'exec "{real}" "$@"\n')
    py.chmod(0o755)
    return str(stub), log


@pytest.mark.parametrize("host,min_calls,expect_rc", [
    ("127.0.0.1", 1, 0),   # the loopback probe runs
    ("10.0.0.1", 2, 2),    # the loopback probe, then the live-token count; refused with zero tokens
])
def test_installer_dry_run_python3_probes_all_run_with_dash_B(owned_tmp, host, min_calls, expect_rc):
    stub, log = _logging_python3(owned_tmp)
    r = _install(owned_tmp, "--host", host, path_prefix=stub)
    assert r.returncode == expect_rc, (r.stdout, r.stderr)
    calls = log.read_text().splitlines() if log.exists() else []
    # a stub that never ran would make the -B assertion below vacuous
    assert len(calls) >= min_calls, (host, calls)
    assert set(calls) == {"OK"}, (host, calls)  # every invocation is `python3 -B -c ...`
