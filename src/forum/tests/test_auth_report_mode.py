"""Auth report mode: a required ``--auth-mode {report,enforce}`` and a recording report.

Contract (written before the implementation; cell numbers follow the spec):

* ``--auth-mode`` is REQUIRED where the process starts (``main()``): unset, empty or any
  other value (case matters) exits 78 with one stderr line, nothing bound, no env fallback.
  ``create_app(..., auth_mode="enforce")`` keeps ``enforce`` as its Python-API default.
* In ``report`` mode a request that carries NO token is served as its declared (validated)
  identity and recorded; a PRESENTED token that is unknown/revoked is still 401, and a valid
  token for another agent than the one named is still 403, in BOTH modes. Every guarded
  request gets exactly one report line, written before any refusal is returned, with
  exactly the fields ``ts, method, route, kind, named, verdict, token_agent, outcome``.
* The report holds no request text outside its ``named`` field: no token, header, body,
  query string, source address or path (an unmatched route is the fixed ``<METHOD>
  <unmatched>``). ``named`` holds only validated identities of at most 128 characters; a
  malformed one is ``<invalid-name> len=N`` and a longer one ``<too-long> len=N``. (The
  method is written as the server read it.)
* A report that cannot be opened exits 78; a failed append is counted (``/health``
  ``auth_report_failures``), logged, and the request proceeds.
* ``--auth-report-until`` (at most 72 h ahead) is required with ``report``; past it the
  server keeps serving and ``/health`` shows ``auth_report_overdue: true``.

These tests opt OUT of the suite-wide auto-auth plumbing (``raw_auth``) so they exercise
the real guard.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forum import db
from forum import server as server_mod
from forum.db import init_db
from forum.server import create_app

pytestmark = pytest.mark.raw_auth

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
_INSTALLER = _REPO / "src" / "forum" / "deploy" / "install-forum-service.sh"
_TEMPLATE = _REPO / "src" / "forum" / "deploy" / "engram-forum.service.template"

FIELDS = {"ts", "method", "route", "kind", "named", "verdict", "token_agent", "outcome"}
POST_BODY = {"agent": "alice", "category_slug": "inter-agent", "title": "t", "body_md": "hello"}
HOSTILE = "evil\u2028x|y"          # a `|` and a line-break character
HOSTILE_Q = "evil%E2%80%A8x%7Cy"    # the same text, URL-encoded
HOSTILE_LEN = len(HOSTILE)


def invalid_name(n: int) -> str:
    return f"<invalid-name> len={n}"


def _ts_until(hours: float = 1.0) -> str:
    t = datetime.now(timezone.utc) + timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _reset_globals():
    """main() puts the baton gate and the board in service-identity mode (process-wide
    state); the cells that run main() must not leak that into the rest of the suite."""
    from forum import board_projects

    gate = server_mod._load_baton_gate()
    assert gate is not None
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)
    yield
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)


@pytest.fixture
def owned_tmp():
    d = Path(tempfile.mkdtemp(prefix="forumreport-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_app(tmp: Path, mode: str = "report", *, until=None, coord=True, **kw):
    db_path = str(tmp / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    if mode == "report":
        kw.setdefault("auth_report", str(tmp / "auth-report.jsonl"))
        kw.setdefault("auth_report_until", until or _ts_until(1))
    if coord:
        kw.setdefault("coord_root", str(tmp / "coord"))
    app = create_app(db_path, str(tmp / "audit.jsonl"), auth_mode=mode, **kw)
    app.config["TESTING"] = True
    return app


def _issue(app, agent):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return db.issue_token(conn, agent)
    finally:
        conn.close()


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _report_path(tmp: Path) -> Path:
    return tmp / "auth-report.jsonl"


def _bytes(tmp: Path) -> bytes:
    p = _report_path(tmp)
    return p.read_bytes() if p.exists() else b""


def _lines(tmp: Path) -> list:
    out = []
    for ln in _bytes(tmp).decode("utf-8").splitlines():
        rec = json.loads(ln)
        assert set(rec) == FIELDS, rec
        out.append(rec)
    return out


def _agent_row(app, name):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()


def _thread_authors(app) -> list:
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return [r[0] for r in conn.execute(
            "SELECT a.name FROM threads t JOIN agents a ON a.id = t.author_agent_id "
            "WHERE t.title = 't'")]
    finally:
        conn.close()


def _post(client, body=None, query="", headers=None):
    return client.post("/api/post" + query, json=(dict(POST_BODY) if body is None else body),
                       headers=headers or {})


# 1 ------------------------------------------------------------------
def test_cell1_report_tokenless_post_naming_alice_is_authored_by_alice(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client())
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    assert _thread_authors(app) == ["alice"]
    (rec,) = _lines(owned_tmp)
    assert (rec["method"], rec["kind"], rec["named"], rec["verdict"], rec["token_agent"]) == (
        "POST", "write", ["alice"], "absent", None)
    assert rec["route"] == "POST /api/post"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["ts"])


# 2 ------------------------------------------------------------------
def test_cell2_wrong_token_is_401_in_report_mode_and_recorded_without_the_header(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), headers=_auth("garbage"))
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"
    assert _thread_authors(app) == []
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["named"], rec["token_agent"], rec["kind"]) == (
        "invalid", ["alice"], None, "write")
    data = _bytes(owned_tmp)
    assert b"garbage" not in data and b"Bearer" not in data and b"bearer" not in data
    assert b"Authorization" not in data


# 3 ------------------------------------------------------------------
def test_cell3_match_in_report_equals_enforce(owned_tmp):
    rep_dir, enf_dir = owned_tmp / "r", owned_tmp / "e"
    rep_dir.mkdir()
    enf_dir.mkdir()
    out = {}
    for label, tmp, mode in (("report", rep_dir, "report"), ("enforce", enf_dir, "enforce")):
        app = _make_app(tmp, mode)
        tok = _issue(app, "alice")
        c = app.test_client()
        w = _post(c, headers=_auth(tok))
        rd = c.get("/api/agent/alice/inbox", headers=_auth(tok))
        out[label] = (w.status_code, w.get_json(), rd.status_code, rd.get_data())
    assert out["report"] == out["enforce"]
    recs = _lines(rep_dir)
    assert [(r["verdict"], r["token_agent"], r["named"]) for r in recs] == [
        ("match", "alice", ["alice"])] * 2
    assert [r["kind"] for r in recs] == ["write", "read"]


# 4 ------------------------------------------------------------------
def test_cell4_valid_token_for_bob_naming_alice_is_403_and_recorded(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), headers=_auth(_issue(app, "bob")))
    assert r.status_code == 403
    assert r.get_json() == {"error": "identity mismatch", "token_agent": "bob",
                            "named_agent": "alice"}
    assert _thread_authors(app) == []
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["token_agent"], rec["named"]) == ("mismatch", "bob", ["alice"])


# 5 ------------------------------------------------------------------
def test_cell5_tokenless_updates_read_is_served_and_recorded(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().get("/api/updates?agent=alice")
    assert r.status_code == 200, r.get_data(as_text=True)
    (rec,) = _lines(owned_tmp)
    assert (rec["kind"], rec["verdict"], rec["named"], rec["route"], rec["token_agent"]) == (
        "read", "absent", ["alice"], "GET /api/updates", None)


def test_cell5_tokenless_updates_read_naming_nobody_is_recorded_and_left_to_the_handler(owned_tmp):
    """/api/updates is a token-required read that need not name an agent: the guard lets a
    tokenless one through and records it; the handler's own 'agent is required' (400) is
    what answers it, exactly as it does for a valid token that names nobody."""
    app = _make_app(owned_tmp)
    r = app.test_client().get("/api/updates")
    assert r.status_code == 400 and r.get_json() == {"error": "agent is required"}
    (rec,) = _lines(owned_tmp)
    assert (rec["kind"], rec["verdict"], rec["named"]) == ("read", "absent", [])


# 6 ------------------------------------------------------------------
def test_cell6_public_read_naming_no_agent_has_no_line(owned_tmp):
    app = _make_app(owned_tmp)
    c = app.test_client()
    assert c.get("/api/threads").status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/forum.md").status_code == 200
    assert _bytes(owned_tmp) == b""


# main() cells --------------------------------------------------------
def _argv(tmp_path, monkeypatch, extra, env=None):
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("FORUM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("FORUM_GITHUB_INTEGRATION", raising=False)
    monkeypatch.delenv("FORUM_AUTH_MODE", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", [
        "forum.server", "--db", str(tmp_path / "svc" / "forum.db"),
        "--audit", str(tmp_path / "svc" / "audit.jsonl"), *extra])
    booted = []
    monkeypatch.setattr(server_mod, "_run_boot_verify",
                        lambda *a, **k: booted.append(1) or {})
    ran = []
    monkeypatch.setattr(server_mod.Flask, "run", lambda self, *a, **k: ran.append(self))
    return booted, ran


def _refuses(tmp_path, monkeypatch, capsys, extra, env=None):
    booted, ran = _argv(tmp_path, monkeypatch, extra, env)
    with pytest.raises(SystemExit) as ei:
        server_mod.main()
    assert ei.value.code == 78
    assert ran == [] and booted == []
    assert not (tmp_path / "svc").exists(), "a refusal must precede any data-dir write"
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    return lines[0]


# 7 ------------------------------------------------------------------
@pytest.mark.parametrize("extra", [
    [], ["--auth-mode", "banana"], ["--auth-mode", ""], ["--auth-mode", "Report"],
    ["--auth-mode", "ENFORCE"], ["--auth-mode", " enforce"],
], ids=["unset", "banana", "empty", "Report-case", "ENFORCE-case", "padded"])
def test_cell7_main_requires_a_valid_auth_mode(tmp_path, monkeypatch, capsys, extra):
    line = _refuses(tmp_path, monkeypatch, capsys, extra)
    assert "--auth-mode" in line and "report" in line and "enforce" in line


def test_cell7_no_env_fallback(tmp_path, monkeypatch, capsys):
    line = _refuses(tmp_path, monkeypatch, capsys, [], env={"FORUM_AUTH_MODE": "report"})
    assert "--auth-mode" in line


def test_cell7_subprocess_without_the_flag_exits_78_and_binds_nothing(owned_tmp):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(_SRC), FORUM_NO_EMBEDDINGS="1")
    env.pop("FORUM_AUTH_MODE", None)
    r = subprocess.run(
        [sys.executable, "-m", "forum.server", "--port", str(port), "--db",
         str(owned_tmp / "svc" / "forum.db"), "--audit", str(owned_tmp / "svc" / "a.jsonl")],
        cwd=str(_SRC), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 78, (r.stdout, r.stderr)
    assert len([ln for ln in r.stderr.splitlines() if ln.strip()]) == 1, r.stderr
    assert not (owned_tmp / "svc").exists()
    with socket.socket() as c:
        c.settimeout(2)
        assert c.connect_ex(("127.0.0.1", port)) != 0


# 8 ------------------------------------------------------------------
def test_cell8_report_requires_a_destination_and_enforce_refuses_one(tmp_path, monkeypatch, capsys):
    line = _refuses(tmp_path, monkeypatch, capsys,
                    ["--auth-mode", "report", "--auth-report-until", _ts_until(1)])
    assert "--auth-report" in line


def test_cell8_enforce_with_a_report_path_is_refused(tmp_path, monkeypatch, capsys):
    line = _refuses(tmp_path, monkeypatch, capsys,
                    ["--auth-mode", "enforce", "--auth-report", str(tmp_path / "x.jsonl")])
    assert "--auth-report" in line


def test_cell8_enforce_with_an_until_is_refused(tmp_path, monkeypatch, capsys):
    line = _refuses(tmp_path, monkeypatch, capsys,
                    ["--auth-mode", "enforce", "--auth-report-until", _ts_until(1)])
    assert "--auth-report-until" in line


def test_cell8_main_serves_in_each_valid_shape(tmp_path, monkeypatch, capsys):
    booted, ran = _argv(tmp_path, monkeypatch, ["--auth-mode", "enforce"])
    server_mod.main()
    assert len(ran) == 1 and ran[0].config["AUTH_MODE"] == "enforce"
    out = capsys.readouterr().out
    assert "forum auth: mode=enforce" in out.splitlines()


def test_cell8_main_report_prints_the_startup_line_and_serves(tmp_path, monkeypatch, capsys):
    rp = tmp_path / "rep.jsonl"
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--auth-mode", "report", "--auth-report", str(rp), "--auth-report-until", _ts_until(1)])
    server_mod.main()
    assert len(ran) == 1 and ran[0].config["AUTH_MODE"] == "report"
    out = capsys.readouterr().out
    assert f"forum auth: mode=report (refusing nothing; recording to {rp})" in out.splitlines()
    assert rp.exists()


# 9 ------------------------------------------------------------------
def test_cell9_health_reports_the_mode_in_both_modes(owned_tmp):
    rep_dir, enf_dir = owned_tmp / "r", owned_tmp / "e"
    rep_dir.mkdir()
    enf_dir.mkdir()
    rep = _make_app(rep_dir).test_client().get("/health").get_json()
    enf = _make_app(enf_dir, "enforce").test_client().get("/health").get_json()
    assert rep["auth_mode"] == "report"
    assert rep["auth_report_failures"] == 0 and rep["auth_report_overdue"] is False
    assert enf["auth_mode"] == "enforce"
    assert "auth_report_failures" not in enf and "auth_report_overdue" not in enf
    assert rep["status"] == "ok" and enf["status"] == "ok"


# 10 -----------------------------------------------------------------
def test_cell10_report_file_is_created_0640_and_an_existing_one_keeps_its_mode(owned_tmp):
    old = os.umask(0o022)
    try:
        a, b = owned_tmp / "a", owned_tmp / "b"
        a.mkdir()
        b.mkdir()
        _make_app(a)
        assert (_report_path(a).stat().st_mode & 0o777) == 0o640
        for mode in (0o600, 0o664):
            _report_path(b).write_bytes(b"")
            os.chmod(_report_path(b), mode)
            _make_app(b)
            assert (_report_path(b).stat().st_mode & 0o777) == mode
    finally:
        os.umask(old)


# 13 -----------------------------------------------------------------
def test_cell13_a_b_a_control(owned_tmp):
    """Same request, same build, three instances: enforce 401, report serves + records,
    enforce 401 again. The third leg tells 'did not refuse' from 'never reached the guard'."""
    dirs = []
    for n in ("one", "two", "three"):
        d = owned_tmp / n
        d.mkdir()
        dirs.append(d)
    codes = []
    for d, mode in zip(dirs, ("enforce", "report", "enforce")):
        app = _make_app(d, mode)
        codes.append(_post(app.test_client()).status_code)
    assert codes[0] == 401 and codes[2] == 401
    assert codes[1] in (200, 201)
    assert _lines(dirs[1])[0]["verdict"] == "absent"
    assert _bytes(dirs[0]) == b"" and not _report_path(dirs[2]).exists()


# 14 -----------------------------------------------------------------
def test_cell14_nothing_a_request_or_the_environment_supplies_changes_the_mode(owned_tmp, monkeypatch):
    rep_dir, enf_dir = owned_tmp / "r", owned_tmp / "e"
    rep_dir.mkdir()
    enf_dir.mkdir()
    rep, enf = _make_app(rep_dir), _make_app(enf_dir, "enforce")
    monkeypatch.setenv("FORUM_AUTH_MODE", "report")
    monkeypatch.setenv("AUTH_MODE", "report")
    ce, cr = enf.test_client(), rep.test_client()
    sneaky = dict(POST_BODY, auth_mode="report", mode="report")
    hdrs = {"X-Auth-Mode": "report", "X-Forum-Auth-Mode": "report"}
    r = ce.post("/api/post?auth_mode=report&mode=report", json=sneaky, headers=hdrs)
    assert r.status_code == 401
    assert ce.get("/health?auth_mode=report", headers=hdrs).get_json()["auth_mode"] == "enforce"
    monkeypatch.setenv("FORUM_AUTH_MODE", "enforce")
    r = cr.post("/api/post?auth_mode=enforce", json=dict(sneaky, auth_mode="enforce"),
                headers={"X-Auth-Mode": "enforce"})
    assert r.status_code in (200, 201)
    assert cr.get("/health?auth_mode=enforce").get_json()["auth_mode"] == "report"


# 15 -----------------------------------------------------------------
@pytest.mark.parametrize("how", ["path", "query"])
def test_cell15_hostile_name_never_reaches_the_file_nor_proceeds(owned_tmp, how):
    app = _make_app(owned_tmp)
    c = app.test_client()
    if how == "path":
        r = c.get(f"/api/agent/{HOSTILE_Q}/inbox")
    else:
        r = c.get(f"/api/updates?agent={HOSTILE_Q}")
    assert r.status_code == 422, r.get_data(as_text=True)
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == [invalid_name(HOSTILE_LEN)]
    assert rec["verdict"] == "absent" and rec["kind"] == "read"
    data = _bytes(owned_tmp)
    for frag in (b"evil", "\u2028".encode(), b"\\u2028", b"x|y", b"|"):
        assert frag not in data, frag
    assert _agent_row(app, HOSTILE.strip().lower()) is None


def test_cell15_pipe_only_name_lands_as_the_normalised_text(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().get("/api/updates?agent=%20Al%7CICE%20")
    assert r.status_code != 401
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == ["al|ice"]


@pytest.mark.parametrize("how", ["path", "query"])
def test_cell15_hostile_name_with_a_wrong_or_valid_token_is_not_written_raw(owned_tmp, how):
    app = _make_app(owned_tmp)
    c = app.test_client()
    for tok in ("garbage", _issue(app, "bob")):
        if how == "path":
            c.get(f"/api/agent/{HOSTILE_Q}/inbox", headers=_auth(tok))
        else:
            c.get(f"/api/updates?agent={HOSTILE_Q}", headers=_auth(tok))
    recs = _lines(owned_tmp)
    assert [r["verdict"] for r in recs] == ["invalid", "mismatch"]
    assert all(r["named"] == [invalid_name(HOSTILE_LEN)] for r in recs)
    data = _bytes(owned_tmp)
    assert b"evil" not in data and b"|" not in data and b"\\u2028" not in data


# 16 -----------------------------------------------------------------
def test_cell16_unwritable_report_path_exits_78_at_startup(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "no-such-dir" / "rep.jsonl"
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--auth-mode", "report", "--auth-report", str(bad), "--auth-report-until", _ts_until(1)])
    with pytest.raises(SystemExit) as ei:
        server_mod.main()
    assert ei.value.code == 78
    assert ran == []
    err = capsys.readouterr().err
    assert "--auth-report" in err and not bad.exists()


def test_cell16_report_path_that_is_a_directory_exits_78(tmp_path, monkeypatch, capsys):
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--auth-mode", "report", "--auth-report", str(tmp_path), "--auth-report-until", _ts_until(1)])
    with pytest.raises(SystemExit) as ei:
        server_mod.main()
    assert ei.value.code == 78 and ran == []


def test_cell16_append_failure_is_counted_logged_and_the_request_still_served(
        owned_tmp, monkeypatch, capsys):
    app = _make_app(owned_tmp)
    report_fd = app.config["AUTH_REPORT"].fd
    real_write = os.write

    def failing_write(fd, data):
        if fd == report_fd:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(server_mod.os, "write", failing_write)
    c = app.test_client()
    for _ in range(101):
        assert c.get("/api/updates?agent=alice").status_code == 200
    # the write path is shared with the audit log: restore before asking /health
    monkeypatch.setattr(server_mod.os, "write", real_write)
    h = c.get("/health").get_json()
    assert h["auth_report_failures"] == 101
    err = capsys.readouterr().err
    marks = [ln for ln in err.splitlines() if "auth report" in ln]
    assert len(marks) == 2, err          # the first failure and the 100th
    assert "No space left" in marks[0]
    # and a later good write works and is the only line in the file
    assert c.get("/api/updates?agent=alice").status_code == 200
    assert len(_lines(owned_tmp)) == 1


# 17 -----------------------------------------------------------------
@pytest.mark.parametrize("until", [
    None, "", "tomorrow", "2026-13-45T99:99:99Z", "2099-01-01T00:00:00",
    "2099-01-01T00:00:00+00:00",
], ids=["missing", "empty", "word", "bad-date", "no-Z", "offset-not-Z"])
def test_cell17_missing_or_unparseable_until_exits_78(tmp_path, monkeypatch, capsys, until):
    extra = ["--auth-mode", "report", "--auth-report", str(tmp_path / "r.jsonl")]
    if until is not None:
        extra += ["--auth-report-until", until]
    line = _refuses(tmp_path, monkeypatch, capsys, extra)
    assert "--auth-report-until" in line


def test_cell17_more_than_72h_ahead_exits_78_and_72h_or_less_starts(tmp_path, monkeypatch, capsys):
    rp = str(tmp_path / "r.jsonl")
    line = _refuses(tmp_path, monkeypatch, capsys, [
        "--auth-mode", "report", "--auth-report", rp, "--auth-report-until", _ts_until(73)])
    assert "72" in line
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--auth-mode", "report", "--auth-report", rp, "--auth-report-until", _ts_until(71)])
    server_mod.main()
    assert len(ran) == 1


def test_cell17_past_the_time_serves_flags_overdue_and_logs_once_an_hour(owned_tmp, capsys):
    app = _make_app(owned_tmp, until=_ts_until(-1))
    c = app.test_client()
    h = c.get("/health").get_json()
    assert h["auth_report_overdue"] is True and h["auth_mode"] == "report"
    assert _post(c).status_code in (200, 201)         # still serving, still report mode
    c.get("/api/updates?agent=alice")
    err = capsys.readouterr().err
    over = [ln for ln in err.splitlines() if "overdue" in ln.lower()]
    assert len(over) == 1, err                        # one loud line, not one per request


# 18 -----------------------------------------------------------------
def test_cell18_read_with_a_mismatched_token_is_403_like_a_write(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().get("/api/agent/alice/inbox", headers=_auth(_issue(app, "bob")))
    assert r.status_code == 403
    (rec,) = _lines(owned_tmp)
    assert (rec["kind"], rec["verdict"], rec["token_agent"], rec["named"]) == (
        "read", "mismatch", "bob", ["alice"])


# 19 -----------------------------------------------------------------
def test_cell19_create_app_without_auth_mode_enforces(owned_tmp):
    db_path = str(owned_tmp / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(owned_tmp / "audit.jsonl"))
    assert _post(app.test_client()).status_code == 401
    assert app.config["AUTH_MODE"] == "enforce"
    assert app.test_client().get("/health").get_json()["auth_mode"] == "enforce"


def test_create_app_refuses_a_bad_or_inconsistent_mode(owned_tmp):
    db_path, audit = str(owned_tmp / "f.db"), str(owned_tmp / "a.jsonl")
    for kw in ({"auth_mode": "Report"}, {"auth_mode": ""}, {"auth_mode": "report"},
               {"auth_mode": "enforce", "auth_report": str(owned_tmp / "r")},
               {"auth_mode": "report", "auth_report": str(owned_tmp / "r"),
                "auth_report_until": _ts_until(100)}):
        with pytest.raises(ValueError):
            create_app(db_path, audit, **kw)


# Extras E1-E3 ---------------------------------------------------------
def test_E1_tokenless_write_naming_nobody_is_recorded_and_left_to_the_handler(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().post("/api/thread/1/accept", json={"post_id": 1})
    assert r.status_code == 400 and r.get_json() == {"error": "agent is required"}
    (rec,) = _lines(owned_tmp)
    assert (rec["named"], rec["verdict"], rec["kind"], rec["token_agent"]) == (
        [], "absent", "write", None)


@pytest.mark.parametrize("bad,n", [("a\nb", 3), (5, 1), ("a\u2028b", 3)],
                         ids=["newline", "int", "line-separator"])
def test_E2_malformed_body_identity_is_one_line_then_a_422(owned_tmp, bad, n):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), body=dict(POST_BODY, agent=bad))
    assert r.status_code == 422, r.get_data(as_text=True)
    assert _thread_authors(app) == []
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == [invalid_name(n)] and rec["verdict"] == "absent"
    data = _bytes(owned_tmp)
    assert data.count(b"\n") == 1 and data.endswith(b"\n")
    assert "\u2028".encode() not in data and b"\\u2028" not in data


def test_E2_malformed_body_identity_under_a_valid_token_is_recorded_too(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), body=dict(POST_BODY, agent="a\nb"), headers=_auth(_issue(app, "alice")))
    assert r.status_code == 422
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == [invalid_name(3)] and rec["token_agent"] == "alice"
    assert b"a\\nb" not in _bytes(owned_tmp)


def test_two_differing_identities_without_a_token_are_403_and_recorded(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), body=dict(POST_BODY, agent="bob"), query="?agent=alice")
    assert r.status_code == 403
    assert _thread_authors(app) == []
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["named"], rec["token_agent"]) == ("absent", ["alice", "bob"], None)


def test_the_same_identity_named_twice_proceeds_as_that_identity(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), body=dict(POST_BODY, agent=" ALICE "), query="?agent=alice")
    assert r.status_code in (200, 201)
    assert _thread_authors(app) == ["alice"]
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == ["alice"]


# Verdict table: 4 verdicts x write/read x path/query/body --------------
_ROWS = [
    # (id, method, url, body-key, token, status-ok, verdict, kind)
    ("absent-write-query", "POST", "/api/post?agent=alice", "nobody", "none", {200, 201}, "absent", "write"),
    ("absent-write-body", "POST", "/api/post", "alice", "none", {200, 201}, "absent", "write"),
    ("absent-read-path", "GET", "/api/agent/alice/inbox", None, "none", {200}, "absent", "read"),
    ("absent-read-query", "GET", "/api/updates?agent=alice", None, "none", {200}, "absent", "read"),
    ("absent-read-body", "GET", "/api/updates", "alice", "none", {200}, "absent", "read"),
    ("invalid-write-query", "POST", "/api/post?agent=alice", "nobody", "bad", {401}, "invalid", "write"),
    ("invalid-write-body", "POST", "/api/post", "alice", "bad", {401}, "invalid", "write"),
    ("invalid-read-path", "GET", "/api/agent/alice/inbox", None, "bad", {401}, "invalid", "read"),
    ("invalid-read-query", "GET", "/api/updates?agent=alice", None, "bad", {401}, "invalid", "read"),
    ("match-write-body", "POST", "/api/post", "alice", "alice", {200, 201}, "match", "write"),
    ("match-write-query", "POST", "/api/post?agent=alice", "nobody", "alice", {200, 201}, "match", "write"),
    ("match-read-path", "GET", "/api/agent/alice/inbox", None, "alice", {200}, "match", "read"),
    ("match-read-query", "GET", "/api/updates?agent=alice", None, "alice", {200}, "match", "read"),
    ("mismatch-write-body", "POST", "/api/post", "alice", "bob", {403}, "mismatch", "write"),
    ("mismatch-write-query", "POST", "/api/post?agent=alice", "nobody", "bob", {403}, "mismatch", "write"),
    ("mismatch-read-path", "GET", "/api/agent/alice/inbox", None, "bob", {403}, "mismatch", "read"),
    ("mismatch-read-query", "GET", "/api/updates?agent=alice", None, "bob", {403}, "mismatch", "read"),
    ("mismatch-read-body", "GET", "/api/updates", "alice", "bob", {403}, "mismatch", "read"),
]


@pytest.mark.parametrize("row", _ROWS, ids=[r[0] for r in _ROWS])
def test_verdict_table(owned_tmp, row):
    _id, method, url, body_agent, token, ok, verdict, kind = row
    app = _make_app(owned_tmp)
    headers = {}
    if token == "bad":
        headers = _auth("not-a-real-token")
    elif token in ("alice", "bob"):
        headers = _auth(_issue(app, token))
    kwargs: dict = {"headers": headers}
    if method == "POST":
        body = dict(POST_BODY)
        if body_agent == "nobody":
            body.pop("agent")
        kwargs["json"] = body
    elif body_agent:
        kwargs["json"] = {"agent": body_agent}
    r = app.test_client().open(url, method=method, **kwargs)
    assert r.status_code in ok, (r.status_code, r.get_data(as_text=True))
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["kind"], rec["named"]) == (verdict, kind, ["alice"])
    assert rec["token_agent"] == (token if verdict in ("match", "mismatch") else None)
    assert rec["method"] == method


# One line per request ------------------------------------------------
def test_one_line_per_request_whether_refused_or_served(owned_tmp):
    app = _make_app(owned_tmp)
    c = app.test_client()
    assert _post(c, headers=_auth("bad")).status_code == 401
    assert len(_lines(owned_tmp)) == 1
    assert _post(c).status_code in (200, 201)
    assert [r["verdict"] for r in _lines(owned_tmp)] == ["invalid", "absent"]
    assert _post(c, headers=_auth(_issue(app, "bob"))).status_code == 403
    assert _post(c, headers=_auth(_issue(app, "alice"))).status_code in (200, 201)
    assert [r["verdict"] for r in _lines(owned_tmp)] == ["invalid", "absent", "mismatch", "match"]


# Never recorded --------------------------------------------------------
def test_the_report_never_holds_headers_body_query_or_source_address(owned_tmp):
    app = _make_app(owned_tmp)
    c = app.test_client()
    body = dict(POST_BODY, body_md="BODYMARK-9f2c", title="t")
    ip = {"REMOTE_ADDR": "203.0.113.77"}
    hdrs = {"X-Forwarded-For": "198.51.100.9", "Cookie": "COOKIEMARK=1",
            "User-Agent": "UAMARK-7"}
    _post(c, body=body, query="?agent=alice&secretqs=QSMARK-3", headers=hdrs)
    _post(c, body=body, query="?secretqs=QSMARK-3",
          headers=dict(hdrs, Authorization="Basic HDRMARK-5"))
    _post(c, body=body, headers=dict(hdrs, Authorization="Bearer TOKMARK-1"))
    _post(c, body=body, headers=dict(hdrs, Authorization="Bearer " + _issue(app, "bob")))
    tok = _issue(app, "alice")
    r = c.open("/api/post", method="POST", json=body, headers=dict(hdrs, **_auth(tok)),
               environ_overrides=ip)
    assert r.status_code in (200, 201)
    data = _bytes(owned_tmp)
    assert len(_lines(owned_tmp)) == 5
    for frag in (b"BODYMARK", b"COOKIEMARK", b"UAMARK", b"QSMARK", b"HDRMARK", b"TOKMARK",
                 b"203.0.113", b"198.51.100", b"X-Forwarded", b"Cookie", b"Basic", b"Bearer",
                 tok.encode(), b"hello", b"secretqs"):
        assert frag not in data, frag


def test_a_malformed_authorization_header_counts_as_a_presented_credential(owned_tmp):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401
    assert _lines(owned_tmp)[0]["verdict"] == "invalid"


# Concurrency -------------------------------------------------------------
def test_concurrent_requests_never_tear_a_line(owned_tmp):
    app = _make_app(owned_tmp)
    errors = []

    def worker():
        try:
            c = app.test_client()
            for _ in range(25):
                assert c.get("/api/updates?agent=alice").status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    recs = _lines(owned_tmp)      # json.loads of every line: a torn line raises
    assert len(recs) == 200
    assert {(r["verdict"], r["kind"]) for r in recs} == {("absent", "read")}


# Installer (cell 12) and the unit template -------------------------------
def _primary_group() -> str:
    return subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()


def _install(tmp: Path, extra: list, *, home: Path | None = None):
    home = home or (tmp / "home")
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    env.pop("ENGRAM_HOME", None)
    return subprocess.run(
        ["bash", str(_INSTALLER), "--src", str(_REPO), "--service-dir", str(tmp / "forum"),
         "--group", _primary_group(), "--dry-run", *extra],
        capture_output=True, text=True, env=env, timeout=120)


def _exec_start(stdout: str) -> list:
    text = stdout.split("--- rendered unit begin ---", 1)[1].split("--- rendered unit end ---", 1)[0]
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("ExecStart="))
    joined = []
    while True:
        ln = lines[i]
        joined.append(ln.rstrip("\\").strip())
        if not ln.rstrip().endswith("\\"):
            break
        i += 1
    return shlex.split(" ".join(joined)[len("ExecStart="):])


def test_cell12_installer_without_auth_mode_refuses_and_writes_nothing(owned_tmp):
    r = _install(owned_tmp, [])
    assert r.returncode != 0
    assert "--auth-mode" in r.stderr and "unknown arg" not in r.stderr
    assert not (owned_tmp / "forum").exists()
    assert "rendered unit begin" not in r.stdout


@pytest.mark.parametrize("extra", [
    ["--auth-mode", ""], ["--auth-mode", "Report"], ["--auth-mode", "banana"],
    ["--auth-mode", "report"],
    ["--auth-mode", "report", "--auth-report", "/x/r.jsonl"],
    ["--auth-mode", "report", "--auth-report-until", "2026-11-16T00:00:00Z"],
    ["--auth-mode", "report", "--auth-report", "rel/r.jsonl",
     "--auth-report-until", "2026-11-16T00:00:00Z"],
    ["--auth-mode", "report", "--auth-report", "/x/r.jsonl", "--auth-report-until", "tomorrow"],
    ["--auth-mode", "enforce", "--auth-report", "/x/r.jsonl"],
    ["--auth-mode", "enforce", "--auth-report-until", "2026-11-16T00:00:00Z"],
], ids=["empty", "Report-case", "banana", "report-bare", "report-no-until", "report-no-path",
        "relative-path", "bad-until", "enforce-with-path", "enforce-with-until"])
def test_cell12_installer_refuses_bad_auth_shapes(owned_tmp, extra):
    r = _install(owned_tmp, extra)
    assert r.returncode != 0, r.stdout
    assert "--auth-" in r.stderr
    assert "unknown arg" not in r.stderr, "the flag must be KNOWN to the installer, then refused"
    assert "rendered unit begin" not in r.stdout
    assert not (owned_tmp / "forum").exists()


def test_cell12_installer_renders_report_flags_into_execstart(owned_tmp):
    until = "2026-11-16T00:00:00Z"
    r = _install(owned_tmp, ["--auth-mode", "report", "--auth-report", "/srv/forum/auth-report.jsonl",
                             "--auth-report-until", until])
    assert r.returncode == 0, r.stderr
    argv = _exec_start(r.stdout)
    assert argv[argv.index("--auth-mode") + 1] == "report"
    assert argv[argv.index("--auth-report") + 1] == "/srv/forum/auth-report.jsonl"
    assert argv[argv.index("--auth-report-until") + 1] == until
    assert argv.count("--auth-mode") == 1


def test_cell12_installer_renders_enforce_with_no_report_flags(owned_tmp):
    r = _install(owned_tmp, ["--auth-mode", "enforce"])
    assert r.returncode == 0, r.stderr
    argv = _exec_start(r.stdout)
    assert argv[argv.index("--auth-mode") + 1] == "enforce"
    assert "--auth-report" not in argv and "--auth-report-until" not in argv


def test_template_carries_the_flags_as_a_placeholder_and_no_default_mode_text():
    text = _TEMPLATE.read_text(encoding="utf-8")
    assert "{{AUTH_MODE_ARGS}}" in text
    text = text.replace("/health report backup age", "")  # pre-existing prose about /health
    assert not re.search(r"\b(report|enforce)\b", text, re.IGNORECASE), \
        "the template must hold no mode text; the installer renders the operator's choice"


def test_a_unit_started_without_the_flag_fails_to_start(owned_tmp):
    """The server's own exit 78 is the backstop for a hand-written or stale unit: take a
    rendered ExecStart, drop the auth flags, run it. It must refuse, not serve in some mode."""
    r = _install(owned_tmp, ["--auth-mode", "report", "--auth-report", "/srv/r.jsonl",
                             "--auth-report-until", _ts_until(1)])
    assert r.returncode == 0, r.stderr
    argv = _exec_start(r.stdout)
    kept, skip = [], 0
    for tok in argv:
        if skip:
            skip -= 1
        elif tok in ("--auth-mode", "--auth-report", "--auth-report-until"):
            skip = 1
        else:
            kept.append(tok)
    assert "-m" in kept and "forum.server" in kept and "--auth-mode" not in kept
    args = kept[kept.index("forum.server") + 1:]
    swap = {"--db": str(owned_tmp / "svc" / "forum.db"), "--audit": str(owned_tmp / "svc" / "a.jsonl")}
    for flag, val in swap.items():
        args[args.index(flag) + 1] = val
    env = dict(os.environ, PYTHONPATH=str(_SRC), FORUM_NO_EMBEDDINGS="1")
    p = subprocess.run([sys.executable, "-m", "forum.server", *args], cwd=str(_SRC), env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 78, (p.stdout, p.stderr)
    assert not (owned_tmp / "svc").exists()


# ---- delta 1: tokenless reads, the PATCH verb, header shapes, path-named writes ----

def _mk_thread(app, who="bob"):
    r = _post(app.test_client(), body={**POST_BODY, "agent": who}, headers=_auth(_issue(app, who)))
    assert r.status_code in (200, 201), r.get_data(as_text=True)


def _mk_batons(app):
    c = app.test_client()
    tok = _issue(app, "bob")
    for pid, turn, parts in (("pool-a", "alice", ["alice", "bob"]), ("pool-b", "bob", ["bob", "carol"])):
        r = c.post("/api/projects", headers=_auth(tok), json={
            "agent": "bob", "project_id": pid, "title": "x", "status": "in-progress",
            "turn": turn, "participants": parts, "turn_reason": "r"})
        assert r.status_code == 201, r.get_data(as_text=True)


def _setup_thread_read(app):
    _mk_thread(app)
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.mark_thread_read(conn, db.upsert_agent(conn, "alice"), 1, 1)
    finally:
        conn.close()


def _obs_agent_row(app, r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return _agent_row(app, "alice") is not None


def _obs_watermark(app, r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["thread"]["last_read_post_id"]


def _obs_registered(app, r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["registered"]


def _obs_board_updates(app, r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return sorted(u["project"] for u in r.get_json()["updates"])


def _obs_projects(app, r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return sorted(p["project_id"] for p in r.get_json()["projects"])


# (id, route, setup, observable): the observable that differs between a named and an
# un-named caller of that handler (the six readers of g.names_identity).
_D1 = [
    ("threads", "/api/threads", None, _obs_agent_row),
    ("thread", "/api/thread/1", _setup_thread_read, _obs_watermark),
    ("agents-online", "/api/agents/online", None, _obs_registered),
    ("agents-board", "/api/agents/board", None, _obs_registered),
    ("board-updates", "/api/board/updates", _mk_batons, _obs_board_updates),
    ("projects", "/api/projects", _mk_batons, _obs_projects),
]


def _d1_call(tmp, route, setup, obs, how):
    app = _make_app(tmp)
    if setup:
        setup(app)
    headers = _auth(_issue(app, "alice")) if how == "token" else {}
    url = route if how == "control" else route + "?agent=alice"
    return obs(app, app.test_client().get(url, headers=headers))


@pytest.mark.parametrize("row", _D1, ids=[r[0] for r in _D1])
def test_d1_tokenless_named_read_reaches_the_handler_as_that_agent(owned_tmp, row):
    _id, route, setup, obs = row
    vals = {}
    for how in ("control", "named", "token"):
        d = owned_tmp / how
        d.mkdir()
        vals[how] = _d1_call(d, route, setup, obs, how)
        if how == "named":
            (rec,) = [r for r in _lines(d) if r["named"] == ["alice"]]
            assert (rec["verdict"], rec["kind"], rec["token_agent"]) == ("absent", "read", None)
    assert vals["named"] == vals["token"], vals
    assert vals["named"] != vals["control"], vals


def test_d1_tokenless_read_naming_alice_in_the_path_gets_the_same_answer_as_her_token(owned_tmp):
    out = {}
    for how in ("named", "token"):
        d = owned_tmp / how
        d.mkdir()
        app = _make_app(d)
        _mk_thread(app, "bob")
        headers = _auth(_issue(app, "alice")) if how == "token" else {}
        out[how] = []
        for leaf in ("inbox", "mentions"):
            r = app.test_client().get(f"/api/agent/alice/{leaf}", headers=headers)
            assert r.status_code == 200, r.get_data(as_text=True)
            out[how].append(r.get_json())
    assert out["named"] == out["token"]
    recs = [r for r in _lines(owned_tmp / "named") if r["named"] == ["alice"]]
    assert [(r["route"], r["verdict"]) for r in recs] == [
        ("GET /api/agent/<name>/inbox", "absent"),
        ("GET /api/agent/<name>/mentions", "absent")]


# D2: the PATCH verb (the only PUT/PATCH/DELETE route) ---------------------
_PATCH_BODY = {"agent": "alice", "pair_initials": "AB"}


def _patch(app, headers=None):
    return app.test_client().patch("/api/agent/me", json=dict(_PATCH_BODY), headers=headers or {})


def test_d2_tokenless_patch_naming_alice_is_served_as_alice_and_recorded_once(owned_tmp):
    out = {}
    for how in ("named", "token"):
        d = owned_tmp / how
        d.mkdir()
        app = _make_app(d)
        r = _patch(app, _auth(_issue(app, "alice")) if how == "token" else None)
        out[how] = (r.status_code, r.get_json())
    assert out["named"] == out["token"] == (200, {"agent": "alice", "pair_initials": "AB"})
    (rec,) = _lines(owned_tmp / "named")
    assert (rec["method"], rec["route"], rec["kind"], rec["verdict"], rec["named"],
            rec["token_agent"]) == ("PATCH", "PATCH /api/agent/me", "write", "absent",
                                    ["alice"], None)


def test_d2_tokenless_patch_is_401_under_enforce(owned_tmp):
    app = _make_app(owned_tmp, "enforce")
    assert _patch(app).status_code == 401
    assert _agent_row(app, "alice") is None


def test_d2_patch_with_a_bad_token_is_401_and_recorded_invalid(owned_tmp):
    app = _make_app(owned_tmp)
    assert _patch(app, _auth("not-a-real-token")).status_code == 401
    (rec,) = _lines(owned_tmp)
    assert (rec["method"], rec["kind"], rec["verdict"], rec["token_agent"]) == (
        "PATCH", "write", "invalid", None)
    assert _agent_row(app, "alice") is None


def test_d2_patch_with_bobs_token_naming_alice_is_403_and_recorded_mismatch(owned_tmp):
    app = _make_app(owned_tmp)
    r = _patch(app, _auth(_issue(app, "bob")))
    assert r.status_code == 403
    assert r.get_json() == {"error": "identity mismatch", "token_agent": "bob",
                            "named_agent": "alice"}
    (rec,) = _lines(owned_tmp)
    assert (rec["method"], rec["kind"], rec["verdict"], rec["token_agent"]) == (
        "PATCH", "write", "mismatch", "bob")
    assert _agent_row(app, "alice") is None


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_d2_guard_treats_put_and_delete_like_the_other_writes(owned_tmp, method):
    """No route takes PUT or DELETE, so the handler answers 405; what is under test is
    the guard: in report mode it reaches routing (not 401) with one line, in enforce 401."""
    (owned_tmp / "rep").mkdir()
    rep = _make_app(owned_tmp / "rep")
    assert rep.test_client().open("/api/agent/alice/inbox", method=method).status_code == 405
    (rec,) = _lines(owned_tmp / "rep")
    assert (rec["method"], rec["kind"], rec["verdict"], rec["named"]) == (
        method, "write", "absent", ["alice"])
    (owned_tmp / "enf").mkdir()
    enf = _make_app(owned_tmp / "enf", "enforce")
    assert enf.test_client().open("/api/agent/alice/inbox", method=method).status_code == 401


# D3: Authorization header shapes ------------------------------------------
@pytest.mark.parametrize("header,needle,status,verdict", [
    ("", None, {200, 201}, "absent"),
    ("   ", None, {200, 201}, "absent"),
    ("Bearer", b"Bearer", {401}, "invalid"),
    ("Bearer ", b"Bearer", {401}, "invalid"),
    ("Basic x", b"Basic", {401}, "invalid"),
], ids=["empty", "whitespace", "scheme-only", "scheme-space", "basic"])
def test_d3_header_shapes(owned_tmp, header, needle, status, verdict):
    app = _make_app(owned_tmp)
    r = _post(app.test_client(), headers={"Authorization": header})
    assert r.status_code in status, (r.status_code, r.get_data(as_text=True))
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["kind"], rec["named"], rec["token_agent"]) == (
        verdict, "write", ["alice"], None)
    assert _thread_authors(app) == (["alice"] if verdict == "absent" else [])
    data = _bytes(owned_tmp)
    assert b"Authorization" not in data and b"authorization" not in data
    if needle:
        assert needle not in data and needle.lower() not in data.lower()
    if header.strip():
        assert header.encode() not in data


# ---- delta 2: outcome ----
# (id, method, url, body, token, verdict, outcome)
_BAD_BODY = dict(POST_BODY, agent="a\nb")
_O_ROWS = [
    ("invalid-401", "POST", "/api/post", None, "bad", "invalid", 401),
    ("mismatch-403", "POST", "/api/post", None, "bob", "mismatch", 403),
    ("absent-two-names-403", "POST", "/api/post?agent=alice", dict(POST_BODY, agent="bob"), "none",
     "absent", 403),
    ("absent-malformed-path-422", "GET", f"/api/agent/{HOSTILE_Q}/inbox", False, "none", "absent", 422),
    ("absent-malformed-query-422", "GET", f"/api/updates?agent={HOSTILE_Q}", False, "none", "absent", 422),
    ("absent-malformed-body-422", "POST", "/api/post", _BAD_BODY, "none", "absent", 422),
    ("valid-token-malformed-body-422", "POST", "/api/post", _BAD_BODY, "alice", "mismatch", 422),
    ("valid-token-malformed-body-mismatch-422", "POST", "/api/post", _BAD_BODY, "bob", "mismatch", 422),
    ("match-proceeded", "POST", "/api/post", None, "alice", "match", "proceeded"),
    ("match-names-nobody-proceeded", "GET", "/api/updates", False, "alice", "match", "proceeded"),
    ("absent-one-name-proceeded", "POST", "/api/post", None, "none", "absent", "proceeded"),
    ("absent-names-nobody-proceeded", "POST", "/api/thread/1/accept", {"post_id": 1}, "none",
     "absent", "proceeded"),
]


def _o_run(tmp, row):
    _id, method, url, body, token, verdict, outcome = row
    app = _make_app(tmp)
    headers = {}
    if token == "bad":
        headers = _auth("not-a-real-token")
    elif token in ("alice", "bob"):
        headers = _auth(_issue(app, token))
    kwargs: dict = {"headers": headers}
    if method == "POST":
        kwargs["json"] = dict(POST_BODY) if body is None else body
    r = app.test_client().open(url, method=method, **kwargs)
    return r, _lines(tmp)


@pytest.mark.parametrize("row", _O_ROWS, ids=[r[0] for r in _O_ROWS])
def test_o1_outcome_table(owned_tmp, row):
    r, recs = _o_run(owned_tmp, row)
    (rec,) = recs
    assert rec["verdict"] == row[5]
    assert rec["outcome"] == row[6] and type(rec["outcome"]) is type(row[6])
    if isinstance(row[6], int):
        assert r.status_code == row[6], (r.status_code, r.get_data(as_text=True))
    else:
        assert r.status_code not in (401, 403, 422), r.status_code


def test_o2_outcome_type_pin(owned_tmp):
    seen = []
    for i, row in enumerate(_O_ROWS):
        tmp = owned_tmp / f"o2-{i}"
        tmp.mkdir()
        _r, (rec,) = _o_run(tmp, row)
        seen.append(rec["outcome"])
    for o in seen:
        assert o == "proceeded" or (isinstance(o, int) and not isinstance(o, bool)), o
        assert o in ("proceeded", 401, 403, 422), o
    assert not [o for o in seen if isinstance(o, str) and o != "proceeded"]
    assert {"proceeded", 401, 403, 422} <= set(seen)


def test_o3_guard_outcome_not_the_handlers(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().post("/api/thread/1/accept", json={"post_id": 1})
    assert r.status_code == 400 and r.get_json() == {"error": "agent is required"}
    (rec,) = _lines(owned_tmp)
    assert rec["outcome"] == "proceeded"


# ---- delta 4: the report holds no request text outside `named`; hardening ----

def _unmatched_line(tmp, method, url, status, *, headers=None, json_body=None):
    """One request that routing does not match; assert the status it has always had and that
    exactly one line was written, with the fixed unmatched route."""
    app = _make_app(tmp)
    kw = {"headers": headers or {}}
    if json_body is not None:
        kw["json"] = json_body
    r = app.test_client().open(url, method=method, **kw)
    assert r.status_code == status, (r.status_code, r.get_data(as_text=True)[:200])
    (rec,) = _lines(tmp)
    assert _bytes(tmp).count(b"\n") == 1
    assert rec["route"] == f"{method} <unmatched>", rec
    return app, rec, _bytes(tmp)


def test_c1_unmatched_path_with_a_hostile_name_is_one_line_with_the_fixed_route(owned_tmp):
    _app, rec, data = _unmatched_line(
        owned_tmp, "POST", f"/api/agent/{HOSTILE_Q}/nosuchleaf", 422, json_body={})
    assert rec["named"] == [invalid_name(HOSTILE_LEN)]
    for frag in (b"evil", b"x|y", b"|", b"\\u2028", b"nosuchleaf", b"/api"):
        assert frag not in data, frag


def test_c1_a_path_carrying_a_valid_token_text_never_reaches_the_file(owned_tmp):
    app = _make_app(owned_tmp)
    tok = _issue(app, "alice")
    r = app.test_client().post(f"/api/nosuch/Bearer%20{tok}/x", json={})
    assert r.status_code == 404
    (rec,) = _lines(owned_tmp)
    data = _bytes(owned_tmp)
    assert rec["route"] == "POST <unmatched>" and data.count(b"\n") == 1
    for frag in (tok.encode(), b"Bearer", b"nosuch"):
        assert frag not in data, frag


def test_c1_a_wrong_method_on_a_variable_route_is_405_and_holds_no_path_text(owned_tmp):
    _app, rec, data = _unmatched_line(owned_tmp, "PUT", "/api/thread/7777", 405, json_body={})
    assert rec["named"] == []
    for frag in (b"7777", b"/api/thread", b"thread"):
        assert frag not in data, frag


def test_c1_a_wrong_method_with_a_hostile_path_segment_is_422_and_holds_no_path_text(owned_tmp):
    _app, rec, data = _unmatched_line(
        owned_tmp, "PUT", f"/api/agent/{HOSTILE_Q}/inbox", 422)
    assert rec["named"] == [invalid_name(HOSTILE_LEN)]
    for frag in (b"evil", b"x|y", b"|", b"\\u2028", b"inbox"):
        assert frag not in data, frag


def test_c1_a_wrong_method_naming_a_valid_agent_records_only_the_name(owned_tmp):
    _app, rec, data = _unmatched_line(owned_tmp, "PUT", "/api/agent/zzmarkname/inbox", 405)
    assert rec["named"] == ["zzmarkname"]
    assert data.count(b"zzmarkname") == 1 and b"inbox" not in data


def test_c1_a_3000_character_path_is_one_short_line_without_the_path(owned_tmp):
    _app, rec, data = _unmatched_line(
        owned_tmp, "POST", "/api/" + "q" * 3000, 404, json_body={})
    assert b"qqqq" not in data and len(data) < 1024


def test_c1_a_matched_route_keeps_its_rule_text_in_the_report(owned_tmp):
    app = _make_app(owned_tmp)
    app.test_client().get("/api/agent/alice/inbox")
    (rec,) = _lines(owned_tmp)
    assert rec["route"] == "GET /api/agent/<name>/inbox"


# C2: a malformed identity wins over two differing identities (the order enforce uses) ----
_C2_ROWS = [
    ("absent-malformed-body-plus-query-name-422", "POST", "/api/post?agent=alice",
     dict(POST_BODY, agent="bo\nb"), "none", "absent", 422),
    ("absent-malformed-query-plus-body-name-422", "POST", f"/api/post?agent={HOSTILE_Q}",
     dict(POST_BODY, agent="bob"), "none", "absent", 422),
]


def _no_threads(tmp) -> bool:
    conn = sqlite3.connect(str(tmp / "forum.db"))
    try:
        return conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("row", _C2_ROWS, ids=[r[0] for r in _C2_ROWS])
def test_c2_malformed_wins_over_two_differing_identities(owned_tmp, row):
    r, recs = _o_run(owned_tmp, row)
    (rec,) = recs
    assert (rec["verdict"], rec["outcome"]) == ("absent", 422)
    assert r.status_code == 422, (r.status_code, r.get_data(as_text=True))
    assert _no_threads(owned_tmp)


# C3: a name's length is capped in the report ----------------------------------
def test_c3_the_cap_boundary_128_is_written_129_is_a_marker(owned_tmp):
    (owned_tmp / "a").mkdir()
    (owned_tmp / "b").mkdir()
    n128, n129 = "a" * 128, "a" * 129
    app = _make_app(owned_tmp / "a")
    app.test_client().get(f"/api/updates?agent={n128}")
    assert _lines(owned_tmp / "a")[0]["named"] == [n128]
    app = _make_app(owned_tmp / "b")
    app.test_client().get(f"/api/updates?agent={n129}")
    rec = _lines(owned_tmp / "b")[0]
    assert rec["named"] == ["<too-long> len=129"]
    assert n129.encode() not in _bytes(owned_tmp / "b")


def test_c3_a_3mb_body_name_and_a_60kb_query_name_leave_short_lines(owned_tmp):
    (owned_tmp / "a").mkdir()
    (owned_tmp / "b").mkdir()
    app = _make_app(owned_tmp / "a")
    big = "z" * (3 * 1024 * 1024)
    app.test_client().post("/api/post", json=dict(POST_BODY, agent=big))
    (rec,) = _lines(owned_tmp / "a")
    assert rec["named"] == [f"<too-long> len={len(big)}"]
    assert len(_bytes(owned_tmp / "a")) < 1024
    app = _make_app(owned_tmp / "b")
    app.test_client().get("/api/updates?agent=" + "y" * 60000)
    (rec,) = _lines(owned_tmp / "b")
    assert rec["named"] == ["<too-long> len=60000"]
    assert len(_bytes(owned_tmp / "b")) < 1024


# C4: the report is appended to, never truncated -------------------------------
def test_c4a_a_recreated_app_on_the_same_path_appends(owned_tmp):
    app = _make_app(owned_tmp)
    app.test_client().get("/api/updates?agent=alice")
    first = _bytes(owned_tmp)
    assert first.count(b"\n") == 1
    app2 = _make_app(owned_tmp)
    app2.test_client().get("/api/updates?agent=bob")
    data = _bytes(owned_tmp)
    assert data.startswith(first) and data.count(b"\n") == 2
    assert [r["named"] for r in _lines(owned_tmp)] == [["alice"], ["bob"]]


def test_c4b_two_instances_on_one_path_and_an_external_truncate_leave_no_hole(owned_tmp):
    p = _report_path(owned_tmp)
    r1, r2 = server_mod._AuthReport(p), server_mod._AuthReport(p)
    r1.record({"tag": "A"})
    r2.record({"tag": "B"})
    assert [json.loads(ln)["tag"] for ln in p.read_bytes().splitlines()] == ["A", "B"]
    os.truncate(p, 0)
    r1.record({"tag": "C"})
    r2.record({"tag": "D"})
    data = p.read_bytes()
    assert b"\x00" not in data
    assert [json.loads(ln)["tag"] for ln in data.splitlines()] == ["C", "D"]
    assert r1.failures == 0 and r2.failures == 0


# C5: no symlink, and a removed file is a failure ------------------------------
@pytest.mark.parametrize("kind", ["to-a-file", "dangling"])
def test_c5a_a_symlink_report_path_exits_78_at_startup(tmp_path, monkeypatch, capsys, kind):
    target = tmp_path / "real.jsonl"
    if kind == "to-a-file":
        target.write_bytes(b"")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--auth-mode", "report", "--auth-report", str(link), "--auth-report-until", _ts_until(1)])
    with pytest.raises(SystemExit) as ei:
        server_mod.main()
    assert ei.value.code == 78 and ran == [] and booted == []
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if ln.strip()]) == 1, err
    assert "--auth-report" in err
    assert (target.stat().st_size == 0) if kind == "to-a-file" else (not target.exists())


def test_c5b_a_report_file_removed_after_start_counts_every_record_as_a_failure(
        owned_tmp, capsys):
    app = _make_app(owned_tmp)
    c = app.test_client()
    assert c.get("/api/updates?agent=alice").status_code == 200
    _report_path(owned_tmp).unlink()
    for _ in range(3):
        assert c.get("/api/updates?agent=alice").status_code == 200     # still proceeds
    h = c.get("/health").get_json()
    assert h["auth_report_failures"] == 3
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if "auth report" in ln]) == 1, err
    assert not _report_path(owned_tmp).exists()


# C6: a malformed name under a valid token is `mismatch` by a fixed rule --------
@pytest.mark.parametrize("token_agent,bad,n", [("5", 5, 1), ("alice", None, 4), ("a", ["a"], 5)],
                         ids=["int-equal-to-agent", "null", "list-equal-to-agent"])
def test_c6_malformed_name_under_a_valid_token_is_mismatch_by_a_fixed_rule(
        owned_tmp, token_agent, bad, n):
    app = _make_app(owned_tmp)
    tok = _issue(app, token_agent)
    r = _post(app.test_client(), body=dict(POST_BODY, agent=bad), headers=_auth(tok))
    assert r.status_code == 422, (r.status_code, r.get_data(as_text=True))
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["outcome"], rec["token_agent"]) == ("mismatch", 422, token_agent)
    assert rec["named"] == [invalid_name(n)]


# C7: the installer validates --auth-report-until -------------------------------
def _install_until(tmp, until):
    return _install(tmp, ["--auth-mode", "report", "--auth-report", "/srv/forum/auth-report.jsonl",
                          "--auth-report-until", until])


@pytest.mark.parametrize("until", [
    "2026-99-99T99:99:99Z", "2026-02-30T00:00:00Z", "2026-00-10T00:00:00Z", "2026-13-01T00:00:00Z",
    "2026-11-00T00:00:00Z", "2026-11-16T24:00:00Z", "2026-11-16T00:60:00Z", "2026-11-16T00:00:60Z",
], ids=["all-99", "feb-30", "month-0", "month-13", "day-0", "hour-24", "minute-60", "second-60"])
def test_c7_installer_refuses_an_impossible_until_with_one_line_and_writes_nothing(owned_tmp, until):
    r = _install_until(owned_tmp, until)
    assert r.returncode != 0, r.stdout
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "--auth-report-until" in lines[0], r.stderr
    assert "rendered unit begin" not in r.stdout
    assert not (owned_tmp / "forum").exists()


@pytest.mark.parametrize("hours", [73, 72 + 5 / 60, 24 * 365])
def test_c7_installer_refuses_an_until_more_than_72_hours_ahead(owned_tmp, hours):
    r = _install_until(owned_tmp, _ts_until(hours))
    assert r.returncode != 0, r.stdout
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "--auth-report-until" in lines[0] and "72" in lines[0], r.stderr
    assert "rendered unit begin" not in r.stdout
    assert not (owned_tmp / "forum").exists()


@pytest.mark.parametrize("hours", [1, 72])
def test_c7_installer_accepts_one_hour_and_exactly_72_hours_ahead(owned_tmp, hours):
    # The until is computed BEFORE the installer reads its clock, so "now + 72h" here is
    # never later than the installer's own now + 72h: the exact boundary is accepted.
    until = _ts_until(hours)
    r = _install_until(owned_tmp, until)
    assert r.returncode == 0, r.stderr
    argv = _exec_start(r.stdout)
    assert argv[argv.index("--auth-report-until") + 1] == until


# ---- delta 5: the peer-read items ---------------------------------------------------
# I1: a request whose body is over MAX_CONTENT_LENGTH gets one line, then the unchanged 413.
_SMALL_MAX = 300
_BIG_MARK = "OVERSIZE-MARKER"
_BIG_BODY = dict(POST_BODY, body_md=_BIG_MARK + "x" * 2000)


def _small_app(tmp, mode="report"):
    app = _make_app(tmp, mode)
    app.config["MAX_CONTENT_LENGTH"] = _SMALL_MAX
    return app


def _oversize(client, *, url="/api/post", headers=None):
    return client.post(url, json=_BIG_BODY, headers=headers or {})


def _control_then(tmp, app, headers=None):
    """Positive control: a normal write under the cap gives exactly one line (so a zero-line
    result for the oversize one cannot be a broken reader), then return the line count."""
    r = app.test_client().post("/api/post", json=POST_BODY, headers=headers or {})
    assert r.status_code not in (401, 403, 413, 422), r.status_code
    assert len(_lines(tmp)) == 1
    return 1


def test_i1_tokenless_oversize_write_gets_one_413_line_and_the_response_is_413(owned_tmp):
    app = _small_app(owned_tmp)
    n = _control_then(owned_tmp, app)
    r = _oversize(app.test_client())
    assert r.status_code == 413
    recs = _lines(owned_tmp)
    assert len(recs) == n + 1
    rec = recs[-1]
    assert (rec["verdict"], rec["outcome"], rec["token_agent"]) == ("absent", 413, None)
    assert type(rec["outcome"]) is int
    assert rec["named"] == [] and rec["kind"] == "write"
    assert _BIG_MARK.encode() not in _bytes(owned_tmp)


def test_i1_tokenless_oversize_names_come_from_the_path_and_query_only(owned_tmp):
    app = _small_app(owned_tmp)
    r = app.test_client().post("/api/post?agent=carol", json=_BIG_BODY)
    assert r.status_code == 413
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["outcome"], rec["named"]) == ("absent", 413, ["carol"])
    assert b"alice" not in _bytes(owned_tmp)  # the body's agent was never read


def test_i1_bad_token_oversize_is_invalid_413(owned_tmp):
    app = _small_app(owned_tmp)
    n = 0
    r = _oversize(app.test_client(), headers=_auth("not-a-real-token"))
    assert r.status_code == 413
    recs = _lines(owned_tmp)
    assert len(recs) == n + 1
    rec = recs[-1]
    assert (rec["verdict"], rec["outcome"], rec["token_agent"], rec["named"]) == (
        "invalid", 413, None, [])
    assert b"not-a-real-token" not in _bytes(owned_tmp)
    assert _BIG_MARK.encode() not in _bytes(owned_tmp)


def test_i1_valid_token_oversize_is_match_413(owned_tmp):
    app = _small_app(owned_tmp)
    tok = _issue(app, "alice")
    n = _control_then(owned_tmp, app, _auth(tok))
    r = _oversize(app.test_client(), headers=_auth(tok))
    assert r.status_code == 413
    recs = _lines(owned_tmp)
    assert len(recs) == n + 1
    rec = recs[-1]
    assert (rec["verdict"], rec["outcome"], rec["token_agent"], rec["named"]) == (
        "match", 413, "alice", [])
    assert _BIG_MARK.encode() not in _bytes(owned_tmp)


def test_i1_valid_token_oversize_with_another_name_in_the_path_is_mismatch_413(owned_tmp):
    app = _small_app(owned_tmp)
    tok = _issue(app, "bob")
    r = _oversize(app.test_client(), url="/api/agent/alice/nosuchleaf", headers=_auth(tok))
    assert r.status_code == 413
    (rec,) = _lines(owned_tmp)
    assert (rec["verdict"], rec["outcome"], rec["token_agent"], rec["named"]) == (
        "mismatch", 413, "bob", ["alice"])
    assert _BIG_MARK.encode() not in _bytes(owned_tmp)


def test_i1_enforce_answers_are_unchanged(owned_tmp):
    app = _small_app(owned_tmp, "enforce")
    assert _oversize(app.test_client()).status_code == 401
    tok = _issue(app, "alice")
    assert _oversize(app.test_client(), headers=_auth(tok)).status_code == 413
    assert not _report_path(owned_tmp).exists()


# I2: `named` holds at most 8 entries; the rest is one "<more> n=K" entry.
def _names_line(tmp, names):
    app = _make_app(tmp)
    app.test_client().get("/api/updates?" + "&".join(f"agent={n}" for n in names))
    (rec,) = _lines(tmp)
    return rec["named"], len(_bytes(tmp))


def test_i2_exactly_eight_names_are_all_written(owned_tmp):
    names = [f"n{i}" for i in range(8)]
    got, _ = _names_line(owned_tmp, names)
    assert got == names


def test_i2_nine_names_are_eight_and_a_more_marker(owned_tmp):
    names = [f"n{i}" for i in range(9)]
    got, _ = _names_line(owned_tmp, names)
    assert got == names[:8] + ["<more> n=1"]


def test_i2_forty_names_are_eight_and_n_32(owned_tmp):
    names = [f"n{i}" for i in range(40)]
    got, _ = _names_line(owned_tmp, names)
    assert got == names[:8] + ["<more> n=32"]


def test_i2_the_first_eight_keep_request_order_not_sorted_order(owned_tmp):
    names = [f"n{i:02d}" for i in range(20, 0, -1)]  # descending: sorted order would differ
    got, _ = _names_line(owned_tmp, names)
    assert got == names[:8] + ["<more> n=12"]


def test_i2_duplicates_do_not_count_toward_the_cap(owned_tmp):
    names = [f"n{i}" for i in range(8)] * 3
    got, _ = _names_line(owned_tmp, names)
    assert got == [f"n{i}" for i in range(8)]


def test_i2_forty_maximum_length_names_stay_under_2kb(owned_tmp):
    names = [("a" * 125) + f"{i:03d}" for i in range(40)]
    assert all(len(n) == 128 for n in names)
    got, size = _names_line(owned_tmp, names)
    assert got == names[:8] + ["<more> n=32"]
    assert size < 2048, size


# I3: the installer refuses an --auth-report-until at or before now (exit 78).
@pytest.mark.parametrize("until", [
    _ts_until(-1 / 3600), _ts_until(-1), "2025-01-01T00:00:00Z",
], ids=["one-second-ago", "one-hour-ago", "year-2025"])
def test_i3_installer_refuses_a_past_until_with_exit_78_one_line_and_writes_nothing(owned_tmp, until):
    r = _install_until(owned_tmp, until)
    assert r.returncode == 78, (r.returncode, r.stdout, r.stderr)
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "--auth-report-until" in lines[0] and "past" in lines[0], r.stderr
    assert "rendered unit begin" not in r.stdout
    assert not (owned_tmp / "forum").exists()


# ---- delta 5: caller-supplied names in the report column -----------------------------------
# A name a caller supplies is written with `<` as `&lt;` and `"` as `&quot;`, so it can never
# equal or imitate one of the server's own markers; the request is served exactly as before.
_MARKER_INPUTS = ["<more> n=3", "<invalid-name> len=5", "<too-long> len=9"]
_MARKER_IDS = ["more", "invalid-name", "too-long"]
_SERVER_MARKERS = (b'"<more> n=', b'"<invalid-name> len=', b'"<too-long> len=')


def _esc(name: str) -> str:
    return name.replace("<", "&lt;").replace('"', "&quot;")


def _q(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")


@pytest.mark.parametrize("how", ["read", "write"])
@pytest.mark.parametrize("name", _MARKER_INPUTS, ids=_MARKER_IDS)
def test_f1_a_marker_shaped_caller_name_is_written_escaped_and_the_request_is_unchanged(
        owned_tmp, name, how):
    app = _make_app(owned_tmp)
    c = app.test_client()

    def call(n):
        if how == "read":
            return c.get(f"/api/updates?agent={_q(n)}")
        return c.post("/api/post", json=dict(POST_BODY, agent=n))

    r_marker = call(name)
    r_plain = call("zed")  # positive control: a plain name is written as it always was
    recs = _lines(owned_tmp)
    assert len(recs) == 2
    marker_rec, plain_rec = recs
    # (1) the written entry is the escaped form, byte-checked in the file
    assert marker_rec["named"] == [_esc(name)] and _esc(name) != name
    data = _bytes(owned_tmp)
    assert _esc(name).encode("ascii") in data
    for m in _SERVER_MARKERS:
        assert m not in data, m
    # (2) the outcome AND the status are what a plain name gets
    assert (marker_rec["verdict"], marker_rec["outcome"], marker_rec["kind"]) == (
        "absent", "proceeded", plain_rec["kind"])
    assert (plain_rec["verdict"], plain_rec["outcome"]) == ("absent", "proceeded")
    assert r_marker.status_code == r_plain.status_code, (
        r_marker.status_code, r_plain.status_code)
    # (3) a plain name is byte-identical to today
    assert plain_rec["named"] == ["zed"]


@pytest.mark.parametrize("name", _MARKER_INPUTS, ids=_MARKER_IDS)
def test_f1_a_marker_shaped_name_counts_as_one_entry_toward_the_cap(owned_tmp, name):
    seven = [f"n{i}" for i in range(7)]
    got, _ = _names_line(owned_tmp, seven + [_q(name)])
    assert got == seven + [_esc(name)]
    (owned_tmp / "b").mkdir()
    eight = [f"n{i}" for i in range(8)]
    got, _ = _names_line(owned_tmp / "b", eight + [_q(name)])
    assert got == eight + ["<more> n=1"]


def test_f1_a_genuine_server_marker_is_still_written_unescaped(owned_tmp):
    forged = "<more> n=3"
    names = [_q(forged)] + [f"n{i}" for i in range(9)]  # 10 distinct: 8 kept, 2 over
    got, _ = _names_line(owned_tmp, names)
    assert got == [_esc(forged)] + [f"n{i}" for i in range(7)] + ["<more> n=2"]
    assert b'"<more> n=2"' in _bytes(owned_tmp)
    (owned_tmp / "b").mkdir()
    app = _make_app(owned_tmp / "b")
    app.test_client().get(f"/api/updates?agent={HOSTILE_Q}")
    (rec,) = _lines(owned_tmp / "b")
    assert rec["named"] == [invalid_name(HOSTILE_LEN)]
    assert b'"<invalid-name> len=' in _bytes(owned_tmp / "b")
    (owned_tmp / "c").mkdir()
    app = _make_app(owned_tmp / "c")
    app.test_client().get(f"/api/updates?agent={'a' * 129}")
    assert _lines(owned_tmp / "c")[0]["named"] == ["<too-long> len=129"]
    assert b'"<too-long> len=129"' in _bytes(owned_tmp / "c")


def test_f1_a_name_with_a_double_quote_only_is_escaped(owned_tmp):
    app = _make_app(owned_tmp)
    r = app.test_client().get(f"/api/updates?agent={_q('a\"b')}")
    assert r.status_code != 401
    (rec,) = _lines(owned_tmp)
    assert rec["named"] == ["a&quot;b"]
    data = _bytes(owned_tmp)
    assert b"a&quot;b" in data and b'a\\"b' not in data


def test_f1_every_name_of_a_line_is_escaped_not_only_the_first(owned_tmp):
    got, _ = _names_line(owned_tmp, ["plain", _q("<a>"), _q("x<y"), _q('q"')])
    assert got == ["plain", "&lt;a>", "x&lt;y", "q&quot;"]


def test_f1_the_cap_counts_the_written_form_so_a_128_name_with_a_bracket_is_a_marker(owned_tmp):
    name = "<" + "a" * 127  # 128 characters as typed, 131 as written
    got, _ = _names_line(owned_tmp, [_q(name)])
    assert got == ["<too-long> len=131"]
    assert b"&lt;aaaa" not in _bytes(owned_tmp)


def test_f1_a_plain_name_with_an_ampersand_is_unchanged(owned_tmp):
    got, _ = _names_line(owned_tmp, [_q("a&b")])
    assert got == ["a&b"]


# ---- delta 5: --verify-only binds nothing, so it takes the serving flags and an until ----

def test_f3_verify_only_with_the_serving_flags_and_an_until_is_not_refused(
        tmp_path, monkeypatch, capsys):
    booted, ran = _argv(tmp_path, monkeypatch, [
        "--verify-only", "--host", "127.0.0.1", "--port", "5099",
        "--auth-mode", "report", "--auth-report", str(tmp_path / "r.jsonl"),
        "--auth-report-until", _ts_until(1)])
    server_mod.main()
    assert booted == [1] and len(ran) == 1
    assert ran[0].config["AUTH_MODE"] == "enforce"
    assert ran[0].config["AUTH_REPORT_UNTIL"] is None
    err = capsys.readouterr().err
    assert "--auth-report" not in err and "refusing" not in err, err


def test_f3_a_serving_run_with_a_bad_until_is_still_refused(tmp_path, monkeypatch, capsys):
    line = _refuses(tmp_path, monkeypatch, capsys, [
        "--host", "127.0.0.1", "--port", "5099",
        "--auth-mode", "report", "--auth-report", str(tmp_path / "r.jsonl"),
        "--auth-report-until", _ts_until(73)])
    assert "--auth-report-until" in line and "72" in line
