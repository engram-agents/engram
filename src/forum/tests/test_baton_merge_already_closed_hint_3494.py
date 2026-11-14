"""Contract tests for engram-alpha#3494 -- `baton merge` already-closed 409 hint.

Written BEFORE the implementation (contract-first).

Contract:
  S1  Both already-closed 409 sites in POST /merge (the pre-gh status check and
      the under-lock ``except AlreadyClosed``) add ``"reason": "already_closed"``
      and ``"status": "<current closed status>"``; ``error`` text unchanged.
  S2  Route-level pin for the under-lock site: the pre-check passes, ``merge``
      raises AlreadyClosed.
  C1  `baton merge` on a 409 with reason == already_closed prints a hint that the
      GitHub merge succeeded and the baton was already closed (reopen + close
      advice), NOT the "could not confirm ... Retry later" hint. An older server
      (no reason field) is matched on the error text.
  C2  Every other 409/503 keeps the old hint exactly; exit code is unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forum import board_projects
from forum import coordination as _coord
from forum.coordination import (
    AlreadyClosed, FileStore, SeqAllocator, init as _init,
)
from forum.coordination import markdown as md
from forum.db import init_db
from forum.server import create_app

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError  # noqa: E402

ANCHOR = "pr/acme/widgets/42"
OLD_HINT = "GitHub merge succeeded; the server could not confirm it."


def _seed(store, allocator, pid, github):
    _init(store, allocator, pid, title="t", status="in-progress", turn="ariadne",
          participants=["ariadne", "luria"], turn_reason="start",
          transition_phase="fairy", github=github)


@pytest.fixture
def parts(tmp_path):
    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    _seed(store, allocator, "PR-42", ANCHOR)
    _seed(store, allocator, "PR-43", None)
    return store, allocator


@pytest.fixture
def client(tmp_path, parts):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"))
    app.config["TESTING"] = True
    app.config["COORD_STORE"], app.config["COORD_ALLOCATOR"] = parts
    return app.test_client()


@pytest.fixture
def gh_state(monkeypatch):
    box = {"state": "merged"}
    monkeypatch.setattr(board_projects, "_gh_pr_state", lambda n, r: box["state"])
    return box


# ------------------------------------------------------------ server: S1 / S2

class TestServerReasonFields:
    @pytest.mark.parametrize("status", ["merged", "cancelled"])
    def test_pre_check_site_carries_reason_and_status(self, client, parts, gh_state, status):
        store, allocator = parts
        raw = store.read_project("PR-43")
        store.write_project("PR-43", md.update_frontmatter(raw, {"status": status}),
                            seq=allocator.current())
        resp = client.post("/api/projects/PR-43/merge", json={"agent": "luria"})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == (f"project PR-43 is already {status}; "
                                 "refusing to overwrite a closed status (reopen first)")
        assert body["reason"] == "already_closed"
        assert body["status"] == status

    def test_under_lock_site_carries_reason_and_status(self, client, parts, gh_state,
                                                       monkeypatch):
        # The pre-check passes (baton is active); a close "lands" before the lock.
        def racing_merge(*a, **k):
            raise AlreadyClosed("PR-43", "cancelled")
        monkeypatch.setattr(_coord, "merge", racing_merge)
        resp = client.post("/api/projects/PR-43/merge", json={"agent": "luria"})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == ("project PR-43 is already cancelled; "
                                 "refusing to overwrite a closed status (reopen first)")
        assert body["reason"] == "already_closed"
        assert body["status"] == "cancelled"

    def test_other_409_and_503_bodies_have_no_reason(self, client, parts, gh_state):
        gh_state["state"] = "open"
        r409 = client.post("/api/projects/PR-42/merge", json={"agent": "luria"})
        assert r409.status_code == 409
        assert "reason" not in r409.get_json()
        gh_state["state"] = "unknown"
        r503 = client.post("/api/projects/PR-42/merge", json={"agent": "luria"})
        assert r503.status_code == 503
        assert "reason" not in r503.get_json()


# ------------------------------------------------------------ client: C1 / C2

_BATON_RAW = (
    "---\n"
    "project: PR-42\n"
    "title: t\n"
    "status: in-progress\n"
    "turn: casey\n"
    "turn_since: 2026-06-26T12:00:00Z\n"
    "turn_reason: start\n"
    "participants: [borges, ariadne]\n"
    f"github: {ANCHOR}\n"
    "---\n\n## Turn log\n"
)


def _run_cmd_merge(monkeypatch, post_error):
    c = MagicMock()
    c.get.return_value = {"raw": _BATON_RAW}
    c.post.side_effect = post_error
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
    monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))
    monkeypatch.setattr(_baton.subprocess, "run",
                        lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
    cfg = {"mode": "multi", "agent_name": "borges", "primary_user": "casey",
           "forum": {"url": "http://test-forum:5002"}}
    ns = type("NS", (), {"pr": "PR-42", "force": False, "dry_run": False})()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_merge(ns, cfg, "borges", c)
    return ei.value.code


def _assert_old_hint(err):
    assert OLD_HINT in err
    assert "Retry later" in err
    assert "baton close PR-42 --status merged" in err


def _assert_new_hint(err, status):
    assert OLD_HINT not in err
    assert "retry later" not in err.lower()
    assert "GitHub merge succeeded" in err
    assert f"already closed as {status}" in err
    assert "someone closed it after this command read it" in err
    assert "nothing more is needed" in err
    assert "baton reopen PR-42" in err
    assert "baton close PR-42 --status merged" in err


ERR_TEXT = ("project PR-42 is already cancelled; "
            "refusing to overwrite a closed status (reopen first)")


class TestMergeClientAlreadyClosedHint:
    def test_a_reason_field_409_gets_new_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": ERR_TEXT, "reason": "already_closed",
                           "status": "cancelled"})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        err = capsys.readouterr().err
        assert "baton merge: baton closure failed (409):" in err
        _assert_new_hint(err, "cancelled")

    def test_a_reason_field_alone_is_enough_without_the_error_text(self, monkeypatch, capsys):
        # Proves the machine field is read on its own, not via the text fallback.
        body = json.dumps({"error": "x", "reason": "already_closed", "status": "cancelled"})
        _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        _assert_new_hint(capsys.readouterr().err, "cancelled")

    def test_a_reason_field_status_is_taken_from_the_field(self, monkeypatch, capsys):
        # error text says one thing, the machine field another: the field wins.
        body = json.dumps({"error": ERR_TEXT, "reason": "already_closed",
                           "status": "merged"})
        _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        _assert_new_hint(capsys.readouterr().err, "merged")

    def test_b_text_only_409_old_server_gets_new_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": ERR_TEXT})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_new_hint(capsys.readouterr().err, "cancelled")

    def test_c_pr_not_merged_409_keeps_old_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": "PR acme/widgets#42 is OPEN on GitHub, not MERGED; "
                                    "refusing to close PR-42 as merged."})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_old_hint(capsys.readouterr().err)

    def test_d_anchor_changed_409_keeps_old_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": "the github anchor changed during the GitHub check on "
                                    "PR-42 (a -> b); the PR state read was for the old "
                                    "anchor, so nothing was written for PR-42. Retry."})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_old_hint(capsys.readouterr().err)

    def test_e_503_keeps_old_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": "could not read the state of PR acme/widgets#42 "
                                    "from GitHub; nothing written for PR-42. Retry later."})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(503, body))
        assert code == _baton.EXIT_IO
        _assert_old_hint(capsys.readouterr().err)

    @pytest.mark.parametrize("body", ["<html>conflict</html>", "", "[1, 2]", "null"])
    def test_f_non_json_or_non_dict_409_keeps_old_hint_no_crash(self, monkeypatch, capsys, body):
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_old_hint(capsys.readouterr().err)

    def test_reason_on_a_503_does_not_trigger_new_hint(self, monkeypatch, capsys):
        body = json.dumps({"error": "x", "reason": "already_closed", "status": "merged"})
        _run_cmd_merge(monkeypatch, ForumHttpError(503, body))
        _assert_old_hint(capsys.readouterr().err)


# ------------------------------------------- unknown status: no "closed as closed"

def _assert_unknown_status_hint(err):
    assert OLD_HINT not in err
    assert "retry later" not in err.lower()
    assert "GitHub merge succeeded" in err
    assert "the baton was already closed when the closure was written" in err
    assert "closed as" not in err
    assert "someone closed it after this command read it" in err
    assert "Check the baton's status" in err
    assert "nothing more is needed" in err
    assert "baton reopen PR-42" in err
    assert "baton close PR-42 --status merged" in err


class TestUnknownStatusWording:
    @pytest.mark.parametrize("extra", [{}, {"status": None}, {"status": ""}, {"status": "  "}])
    def test_reason_present_status_missing(self, monkeypatch, capsys, extra):
        body = json.dumps({"error": "x", "reason": "already_closed", **extra})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_unknown_status_hint(capsys.readouterr().err)

    @pytest.mark.parametrize("err_text", [
        "refusing to overwrite a closed status",             # fragment, no "is already X;"
        "project PR-42 is already cancelled refusing to overwrite a closed status",  # no ';'
    ])
    def test_text_fallback_without_a_status_match(self, monkeypatch, capsys, err_text):
        body = json.dumps({"error": err_text})
        code = _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        assert code == _baton.EXIT_IO
        _assert_unknown_status_hint(capsys.readouterr().err)

    def test_known_status_still_names_it(self, monkeypatch, capsys):
        body = json.dumps({"error": "x", "reason": "already_closed", "status": "cancelled"})
        _run_cmd_merge(monkeypatch, ForumHttpError(409, body))
        err = capsys.readouterr().err
        assert "already closed as cancelled when the closure was written" in err
        assert "Check the baton's status" not in err


# ------------------------- joint: REAL route <-> REAL cmd_merge (client/route contract)

class _InProcessTransport:
    """Routes ForumClient-style get/post to the real Flask app, raising the REAL
    ForumHttpError (status + body text) as forum_api would over HTTP. ``mangle``
    may rewrite an error body in transit (to hide the text fallback)."""

    def __init__(self, tc, mangle=None):
        self.tc, self.mangle, self.bodies = tc, mangle, []

    def _wrap(self, r):
        if r.status_code >= 300:
            text = r.get_data(as_text=True)
            self.bodies.append((r.status_code, json.loads(text)))
            if self.mangle:
                text = self.mangle(text)
            raise ForumHttpError(r.status_code, text)
        return r.get_json()

    def get(self, path, **k):
        return self._wrap(self.tc.get(path))

    def post(self, path, body, **k):
        return self._wrap(self.tc.post(path, json=body))


def _drop_error_text(text):
    d = json.loads(text)
    d.pop("error", None)
    return json.dumps(d)


def _joint_cfg(monkeypatch, tc, closer):
    """Stub only gh/CI/approval; `gh pr merge` closes the baton mid-flight via the
    real status route."""
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
    monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))

    def fake_run(*a, **k):
        r = tc.post("/api/projects/PR-42/status",
                    json={"agent": "ariadne", "new_status": closer, "reason": "race"})
        assert r.status_code == 201, r.get_data()
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(_baton.subprocess, "run", fake_run)
    return {"mode": "multi", "agent_name": "luria", "primary_user": "casey",
            "forum": {"url": "http://test-forum:5002"}}


def _status_line(tc):
    raw = tc.get("/api/projects/PR-42").get_json()["raw"]
    return [ln for ln in raw.splitlines() if ln.startswith("status:")]


@pytest.mark.parametrize("closer", ["cancelled", "merged"])
@pytest.mark.parametrize("mangle", [None, _drop_error_text], ids=["text-intact", "text-dropped"])
def test_joint_race_close_prints_new_hint_with_real_status(
        client, parts, gh_state, monkeypatch, capsys, closer, mangle):
    """Real route + real cmd_merge. With the error text dropped in transit only the
    machine fields (reason/status) can carry the hint, so a one-sided rename of
    either the server's keys or the client's reads goes red."""
    store, allocator = parts
    raw = store.read_project("PR-42")
    store.write_project("PR-42", md.update_frontmatter(raw, {"turn": "casey"}),
                        seq=allocator.current())  # Gate 2: turn held by the pool sentinel
    cfg = _joint_cfg(monkeypatch, client, closer)
    tr = _InProcessTransport(client, mangle)
    ns = type("NS", (), {"pr": "PR-42", "force": False, "dry_run": False})()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_merge(ns, cfg, "luria", tr)
    assert ei.value.code == _baton.EXIT_IO
    err = capsys.readouterr().err
    # server side of the contract, as actually emitted by the real route
    assert tr.bodies[-1][0] == 409
    assert tr.bodies[-1][1]["reason"] == "already_closed"
    assert tr.bodies[-1][1]["status"] == closer
    # client side: new hint, right status, and NOT the #3416 hint
    _assert_new_hint(err, closer)
    assert f"already closed as {closer} when the closure was written" in err
    assert "could not confirm" not in err

    # follow the hint: reopen, then close as merged -> final status merged
    def _go(fn, a):
        try:
            fn(a, cfg, "luria", _InProcessTransport(client))
        except SystemExit as e:
            assert e.code in (0, None), e.code
    _go(_baton.cmd_reopen, type("NS", (), {"project_id": "PR-42", "status": "in-progress"})())
    assert _status_line(client) == ["status: in-progress"]
    _go(_baton.cmd_close, type("NS", (), {"project_id": "PR-42", "status": "merged"})())
    assert _status_line(client) == ["status: merged"]
