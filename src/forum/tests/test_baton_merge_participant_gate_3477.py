"""Contract tests for engram-alpha#3477 -- /merge is participant-gated on an UNANCHORED baton.

Written BEFORE the implementation (contract-first). Follow-up to #3416 / #3471.

Contract:
  M1  POST /merge on an UNANCHORED baton (no anchor, ``project/N``, unqualified
      ``pr/N``) by a non-participant: 403 with exactly /status's message
      (``agent 'x' is not a participant of PID``); NOTHING written -- same file
      bytes, no seq consumed, nothing archived.
  M2  A participant on an unanchored baton behaves as before (201, archived).
  M3  /merge on an ANCHORED baton: no participant gate; GitHub decides.
  M4  coordination.merge(): the same ``require_participant`` / ``NotAParticipant``
      path as close(); checked under the lock, after not-found and before the
      anchor binding and the seq; default keeps every existing caller unchanged.
  C1  client wording pins: ``baton gc``'s 403/409/503 refusal lines and
      ``baton merge``'s 409/503 "GitHub merge succeeded ... could not confirm" hint.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forum import board_projects
from forum.coordination import (
    AnchorChanged, FileStore, NotAParticipant, ProjectNotFound, SeqAllocator,
    init as _init, merge as _merge_fn,
)
from forum.db import init_db
from forum.server import create_app

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError, ForumNetworkError  # noqa: E402

ANCHOR = "pr/acme/widgets/42"
OUTSIDER = "sweeper-not-listed"


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
    _seed(store, allocator, "PR-44", "pr/44")            # unqualified
    _seed(store, allocator, "PR-45", "project/7")        # non-PR anchor
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
    box = {"state": "merged", "calls": []}

    def fake(pr_number, repo):
        box["calls"].append((pr_number, repo))
        return box["state"]

    monkeypatch.setattr(board_projects, "_gh_pr_state", fake)
    return box


def _snap(parts, pid):
    store, allocator = parts
    return store.read_project(pid), allocator.current()


def _merge(client, pid, agent):
    return client.post(f"/api/projects/{pid}/merge", json={"agent": agent})


def _status(client, pid, agent):
    return client.post(f"/api/projects/{pid}/status",
                       json={"agent": agent, "new_status": "merged", "reason": "r"})


UNANCHORED = ["PR-43", "PR-44", "PR-45"]


# ------------------------------------------------- M1/M2: route, unanchored

class TestMergeUnanchoredParticipantGate:
    @pytest.mark.parametrize("pid", UNANCHORED)
    def test_non_participant_is_403_nothing_written(self, client, parts, gh_state, pid):
        before = _snap(parts, pid)
        resp = _merge(client, pid, OUTSIDER)
        assert resp.status_code == 403
        # same file bytes, no seq consumed, nothing archived
        assert _snap(parts, pid) == before
        assert parts[0].read_project(pid) is not None
        assert gh_state["calls"] == []

    @pytest.mark.parametrize("pid", UNANCHORED)
    def test_403_message_is_exactly_status_message(self, client, parts, pid):
        merge_err = _merge(client, pid, OUTSIDER).get_json()["error"]
        status_resp = _status(client, pid, OUTSIDER)
        assert status_resp.status_code == 403
        assert merge_err == status_resp.get_json()["error"]
        assert merge_err == f"agent {OUTSIDER!r} is not a participant of {pid}"

    @pytest.mark.parametrize("pid", UNANCHORED)
    def test_participant_merges_and_archives_as_today(self, client, parts, gh_state, pid):
        resp = _merge(client, pid, "luria")
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "merged"
        assert parts[0].read_project(pid) is None  # archived
        assert gh_state["calls"] == []

    def test_participant_match_is_case_insensitive_like_close(self, client, parts):
        assert _merge(client, "PR-43", "LURIA").status_code == 201

    def test_missing_project_is_still_404_for_non_participant(self, client):
        assert _merge(client, "MISSING", OUTSIDER).status_code == 404


# ------------------------------------------------------ M3: route, anchored

class TestMergeAnchoredUnchanged:
    def test_non_participant_merges_when_github_says_merged(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        resp = _merge(client, "PR-42", OUTSIDER)
        assert resp.status_code == 201
        assert parts[0].read_project("PR-42") is None

    @pytest.mark.parametrize("state,want", [("open", 409), ("closed", 409), ("unknown", 503)])
    def test_github_still_decides_for_non_participant(self, client, parts, gh_state, state, want):
        gh_state["state"] = state
        before = _snap(parts, "PR-42")
        resp = _merge(client, "PR-42", OUTSIDER)
        assert resp.status_code == want
        assert _snap(parts, "PR-42") == before


# --------------------------------------------- M4: coordination.merge() directly

class TestCoordinationMergeGate:
    def test_require_participant_true_refuses_non_participant_nothing_written(self, parts):
        store, allocator = parts
        before = _snap(parts, "PR-43")
        with pytest.raises(NotAParticipant):
            _merge_fn(store, allocator, "PR-43", merged_by=OUTSIDER, require_participant=True)
        assert _snap(parts, "PR-43") == before

    def test_default_keeps_existing_callers_unchanged(self, parts):
        store, allocator = parts
        seq = _merge_fn(store, allocator, "PR-43", merged_by=OUTSIDER)  # no kwarg
        assert seq > 0
        assert store.read_project("PR-43") is None

    def test_participant_passes_with_require_participant_true(self, parts):
        store, allocator = parts
        assert _merge_fn(store, allocator, "PR-43", merged_by="Luria",
                         require_participant=True) > 0

    def test_not_found_is_checked_before_participant(self, parts):
        store, allocator = parts
        with pytest.raises(ProjectNotFound):
            _merge_fn(store, allocator, "MISSING", merged_by=OUTSIDER,
                      require_participant=True)

    def test_participant_gate_runs_before_anchor_binding(self, parts):
        # Same order as close(): a non-participant on a drifted anchor gets the
        # participant refusal, not AnchorChanged.
        store, allocator = parts
        with pytest.raises(NotAParticipant):
            _merge_fn(store, allocator, "PR-42", merged_by=OUTSIDER,
                      require_participant=True, expected_anchor="pr/other/repo/42")

    def test_expected_anchor_still_enforced_for_participant(self, parts):
        store, allocator = parts
        before = _snap(parts, "PR-42")
        with pytest.raises(AnchorChanged):
            _merge_fn(store, allocator, "PR-42", merged_by="luria",
                      require_participant=True, expected_anchor="pr/other/repo/42")
        assert _snap(parts, "PR-42") == before


# ------------------------------------------------ C1: client wording pins

class TestGcClientRefusalWording:
    @pytest.mark.parametrize("status,msg", [
        (403, "agent 'ariadne' is not a participant of PR-42"),
        (409, "PR acme/widgets#42 is still OPEN"),
        (503, "could not read the state of PR acme/widgets#42"),
    ])
    def test_refusal_line_shape(self, monkeypatch, capsys, status, msg):
        c = MagicMock()
        c.get.return_value = {"projects": [{"project_id": "PR-42", "github": ANCHOR}]}
        c.post.side_effect = ForumHttpError(status, '{"error": "%s"}' % msg)
        monkeypatch.setattr(_baton.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(_baton.subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stdout="MERGED\n", stderr=""))
        ns = type("NS", (), {"dry_run": False, "limit": 30})()
        with pytest.raises(SystemExit) as ei:
            _baton.cmd_gc(ns, {"mode": "multi", "forum": {"url": "http://test-forum:5002"}},
                          "ariadne", c)
        assert ei.value.code not in (0, None)
        err = capsys.readouterr().err
        assert f"baton gc: server refused PR-42 (HTTP {status}): {msg}" in err
        assert "write failed" not in err

    def test_other_http_status_is_a_write_failure_not_a_refusal(self, monkeypatch, capsys):
        c = MagicMock()
        c.get.return_value = {"projects": [{"project_id": "PR-42", "github": ANCHOR}]}
        c.post.side_effect = ForumHttpError(500, '{"error": "boom"}')
        monkeypatch.setattr(_baton.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(_baton.subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stdout="MERGED\n", stderr=""))
        ns = type("NS", (), {"dry_run": False, "limit": 30})()
        with pytest.raises(SystemExit):
            _baton.cmd_gc(ns, {"mode": "multi", "forum": {"url": "http://test-forum:5002"}},
                          "ariadne", c)
        err = capsys.readouterr().err
        assert "baton gc: write failed for PR-42" in err
        assert "server refused" not in err


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


class TestMergeClientRetryHintWording:
    @pytest.mark.parametrize("status", [409, 503])
    def test_hint_after_server_refusal(self, monkeypatch, capsys, status):
        code = _run_cmd_merge(monkeypatch, ForumHttpError(status, '{"error": "x"}'))
        assert code not in (0, None)
        err = capsys.readouterr().err
        assert f"baton merge: baton closure failed ({status}):" in err
        assert "GitHub merge succeeded; the server could not confirm it." in err
        assert "Retry later" in err
        assert "baton close PR-42 --status merged" in err

    @pytest.mark.parametrize("status", [403, 500])
    def test_no_could_not_confirm_hint_for_other_statuses(self, monkeypatch, capsys, status):
        _run_cmd_merge(monkeypatch, ForumHttpError(status, '{"error": "x"}'))
        err = capsys.readouterr().err
        assert f"baton merge: baton closure failed ({status}):" in err
        assert "could not confirm" not in err

    def test_network_error_has_manual_close_hint(self, monkeypatch, capsys):
        _run_cmd_merge(monkeypatch, ForumNetworkError("down"))
        err = capsys.readouterr().err
        assert "server unreachable writing closure" in err
        assert "GitHub merge succeeded; close baton manually: baton close PR-42 --status merged" in err
