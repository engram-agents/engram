"""Auth report mode: a required ``--auth-mode {report,enforce}`` and a recording report.

Contract (written before the implementation; cell numbers follow the spec):

* ``--auth-mode`` is REQUIRED where the process starts (``main()``): unset, empty or any
  other value (case matters) exits 78 with one stderr line, nothing bound, no env fallback.
  ``create_app(..., auth_mode="enforce")`` keeps ``enforce`` as its Python-API default.
* In ``report`` mode a request that carries NO token is served as its declared (validated)
  identity and recorded; a PRESENTED token that is unknown/revoked is still 401, and a valid
  token for another agent than the one named is still 403, in BOTH modes. Every guarded
  request gets exactly one report line, written before any refusal is returned, with
  exactly the fields ``ts, method, route, kind, named, verdict, token_agent``.
* The report never holds a token, a header, the body, the query string beyond validated
  identities, the source address, or the raw text of a malformed identity.
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

FIELDS = {"ts", "method", "route", "kind", "named", "verdict", "token_agent"}
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
    r = app.test_client().get("/api/updates")
    assert r.status_code == 200, r.get_data(as_text=True)
    (rec,) = _lines(owned_tmp)
    assert (rec["kind"], rec["verdict"], rec["named"], rec["route"], rec["token_agent"]) == (
        "read", "absent", [], "GET /api/updates", None)


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
