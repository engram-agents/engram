"""Contract tests for engram-alpha#3416 -- /gc and /merge derive PR state server-side.

Written BEFORE the implementation (contract-first). Design ruling: Kepler,
2026-10-06 14:26Z, "option B, with /merge in the same change".

Contract:
  K1  POST /gc on a baton with a repo-qualified PR anchor: the server reads the PR
      state itself (board_projects._gh_pr_state); the client's new_status is IGNORED
      for the decision. merged -> close ``merged``; closed -> close ``cancelled``;
      open -> 409. No participant gate on the anchored path.
  K2  POST /merge on an anchored baton: requires GitHub ``merged``, else 409.
  K3  state ``unknown`` (gh unreachable / non-zero / unparseable) -> 503, NOTHING
      written, never a fallback to the client's state.
  K4  an unanchored baton (no anchor, ``project/N``, or an unqualified ``pr/N``)
      keeps the pre-#3416 behaviour on both routes.
  K5  cells per route over merged / closed / open / error / garbage.
  K6  (round 2) a NON-EMPTY github anchor that does not parse (full URL, trailing
      slash, owner/repo#N, overlong number, ...) is NOT "unanchored": both routes
      answer 503, write nothing, never call gh; the message says the anchor could
      not be parsed, quotes the stored value and names /status. The gate module
      being unavailable fails closed the same way.

Fake gh: two instruments. (a) monkeypatching ``forum.board_projects._gh_pr_state``
(the decision-table cells); (b) a fake ``gh`` executable first on PATH, so the REAL
``_gh_pr_state`` parses real subprocess output (the error / garbage / merged cells).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forum import board_projects
from forum import server as _server
from forum.coordination import FileStore, SeqAllocator, init as _init
from forum.db import init_db
from forum.server import create_app

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError  # noqa: E402

ANCHOR = "pr/acme/widgets/42"


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
    """Set the state the fake gh reports; records the (pr, repo) it was asked."""
    box = {"state": "open", "calls": []}

    def fake(pr_number, repo):
        box["calls"].append((pr_number, repo))
        return box["state"]

    monkeypatch.setattr(board_projects, "_gh_pr_state", fake)
    return box


def _snap(parts, pid="PR-42"):
    store, allocator = parts
    return store.read_project(pid), allocator.current()


def _gc(client, new_status, agent="sweeper-not-listed", pid="PR-42"):
    return client.post(f"/api/projects/{pid}/gc",
                       json={"agent": agent, "new_status": new_status, "reason": "gc"})


def _merge(client, pid="PR-42"):
    return client.post(f"/api/projects/{pid}/merge", json={"agent": "luria"})


# ---------------------------------------------------------------- /gc, anchored

class TestGcAnchored:
    def test_client_cancelled_github_open_is_409_nothing_written(self, client, parts, gh_state):
        gh_state["state"] = "open"
        before = _snap(parts)
        resp = _gc(client, "cancelled")
        assert resp.status_code == 409
        err = resp.get_json()["error"].lower()
        assert "close the pr first" in err and "/status" in err
        assert _snap(parts) == before

    def test_client_merged_github_open_is_409(self, client, parts, gh_state):
        gh_state["state"] = "open"
        before = _snap(parts)
        assert _gc(client, "merged").status_code == 409
        assert _snap(parts) == before

    def test_client_merged_github_closed_closes_cancelled(self, client, parts, gh_state):
        gh_state["state"] = "closed"
        resp = _gc(client, "merged")
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "cancelled"
        assert "status: cancelled" in parts[0].read_project("PR-42")

    def test_client_cancelled_github_merged_closes_merged(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        resp = _gc(client, "cancelled")
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "merged"

    def test_census_non_participant_gc_on_merged_pr_succeeds(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        resp = _gc(client, "merged", agent="sweeper-not-listed")
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "merged"

    def test_server_asks_the_anchored_repo_and_number(self, client, gh_state):
        gh_state["state"] = "merged"
        _gc(client, "merged")
        assert gh_state["calls"] == [("42", "acme/widgets")]

    def test_disagreement_is_logged(self, client, gh_state, caplog):
        gh_state["state"] = "closed"
        with caplog.at_level(logging.WARNING):
            _gc(client, "merged")
        assert any("3416" in r.getMessage() and "disagree" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.parametrize("claimed", ["merged", "cancelled"])
    def test_unknown_is_503_nothing_written_never_client_fallback(
            self, client, parts, gh_state, claimed):
        gh_state["state"] = "unknown"
        before = _snap(parts)
        assert _gc(client, claimed).status_code == 503
        assert _snap(parts) == before

    def test_missing_project_still_404(self, client, gh_state):
        assert _gc(client, "merged", pid="MISSING").status_code == 404

    def test_already_closed_is_409_without_asking_github(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        assert _gc(client, "merged").status_code == 201
        gh_state["calls"].clear()
        assert _gc(client, "merged").status_code == 409
        assert gh_state["calls"] == []


# --------------------------------------------------------------- /merge, anchored

class TestMergeAnchored:
    def test_github_merged_archives(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        resp = _merge(client)
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "merged"
        assert parts[0].read_project("PR-42") is None  # moved to archive/

    @pytest.mark.parametrize("state", ["open", "closed"])
    def test_github_not_merged_is_409_nothing_written(self, client, parts, gh_state, state):
        gh_state["state"] = state
        before = _snap(parts)
        assert _merge(client).status_code == 409
        assert _snap(parts) == before

    def test_unknown_is_503_nothing_written(self, client, parts, gh_state):
        gh_state["state"] = "unknown"
        before = _snap(parts)
        assert _merge(client).status_code == 503
        assert _snap(parts) == before

    def test_forced_flag_does_not_bypass(self, client, parts, gh_state):
        gh_state["state"] = "open"
        before = _snap(parts)
        resp = client.post("/api/projects/PR-42/merge",
                           json={"agent": "luria", "forced": True})
        assert resp.status_code == 409
        assert _snap(parts) == before

    def test_missing_project_still_404(self, client, gh_state):
        assert _merge(client, pid="MISSING").status_code == 404


# ------------------------------------------------------------------- unanchored

class TestUnanchoredUnchanged:
    @pytest.mark.parametrize("pid", ["PR-43", "PR-44", "PR-45"])
    def test_gc_trusts_client_and_never_asks_github(self, client, parts, gh_state, pid):
        # Participant caller: unanchored /gc is participant-gated (ruling (a)).
        gh_state["state"] = "unknown"          # would be 503 if consulted
        resp = _gc(client, "cancelled", agent="luria", pid=pid)
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "cancelled"
        assert gh_state["calls"] == []

    @pytest.mark.parametrize("pid", ["PR-43", "PR-44", "PR-45"])
    def test_gc_non_participant_is_403_nothing_written(self, client, parts, gh_state, pid):
        before = _snap(parts, pid)
        resp = _gc(client, "cancelled", pid=pid)       # default agent: not a participant
        assert resp.status_code == 403
        assert _snap(parts, pid) == before
        assert gh_state["calls"] == []

    @pytest.mark.parametrize("pid", ["PR-43", "PR-44", "PR-45"])
    def test_merge_unchanged_and_never_asks_github(self, client, gh_state, pid):
        gh_state["state"] = "unknown"
        assert _merge(client, pid=pid).status_code == 201
        assert gh_state["calls"] == []


# ------------------------------------ round 2: present-but-unparseable anchor (K6)

UNPARSEABLE = [
    "https://github.com/o/r/pull/5",
    "pr/o/r/5/",
    "pr/o/r/5#x",
    "owner/repo#5",
    "pr/o/r/1234567890123456",      # 16 digits: over the parser's 15-digit cap
    "garbage",
]


def _seed_unparseable(parts, value):
    _seed(*parts, "PR-99", value)
    return "PR-99"


class TestUnparseableAnchorFailsClosed:
    @pytest.mark.parametrize("value", UNPARSEABLE)
    @pytest.mark.parametrize("claimed", ["merged", "cancelled"])
    def test_gc_503_nothing_written_no_gh(self, client, parts, gh_state, value, claimed):
        pid = _seed_unparseable(parts, value)
        before = _snap(parts, pid)
        resp = _gc(client, claimed, agent="luria", pid=pid)
        assert resp.status_code == 503
        err = resp.get_json()["error"]
        assert "could not be parsed" in err and repr(value) in err and "/status" in err
        assert _snap(parts, pid) == before
        assert gh_state["calls"] == []

    @pytest.mark.parametrize("value", UNPARSEABLE)
    def test_merge_503_nothing_written_no_gh(self, client, parts, gh_state, value):
        pid = _seed_unparseable(parts, value)
        before = _snap(parts, pid)
        resp = _merge(client, pid=pid)
        assert resp.status_code == 503
        err = resp.get_json()["error"]
        assert "could not be parsed" in err and repr(value) in err and "/status" in err
        assert _snap(parts, pid) == before
        assert gh_state["calls"] == []

    def test_message_does_not_print_a_fake_repo_number(self, client, parts, gh_state):
        pid = _seed_unparseable(parts, "owner/repo#5")
        assert "PR #" not in _merge(client, pid=pid).get_json()["error"]
        assert "PR #" not in _gc(client, "merged", agent="luria",
                                 pid=pid).get_json()["error"]

    def test_whitespace_and_case_are_normalised_like_the_other_readers(
            self, client, parts, gh_state):
        # An otherwise-valid anchor with stray case is NOT unparseable (the same
        # strip+lower the readers apply), so it reaches gh.
        _seed(*parts, "PR-98", "  PR/Acme/Widgets/42 ")
        gh_state["state"] = "merged"
        assert _merge(client, pid="PR-98").status_code == 201
        assert gh_state["calls"] == [("42", "acme/widgets")]

    @pytest.mark.parametrize("route", ["gc", "merge"])
    def test_gate_module_unavailable_is_503_nothing_written(
            self, client, parts, gh_state, monkeypatch, route):
        monkeypatch.setattr(_server, "_load_baton_gate", lambda: None)
        before = _snap(parts)                      # PR-42, a valid anchor
        if route == "gc":
            resp = _gc(client, "merged")
        else:
            resp = _merge(client)
        assert resp.status_code == 503
        assert _snap(parts) == before
        assert gh_state["calls"] == []


# ----------------------------------------- real _gh_pr_state through a fake `gh`

def _fake_gh(tmp_path, monkeypatch, *, stdout="", rc=0):
    d = tmp_path / "fakebin"
    d.mkdir()
    script = d / "gh"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {rc}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")


GH_CELLS = {
    "merged": ('{"state": "MERGED"}', 0),
    "closed": ('{"state": "CLOSED"}', 0),
    "open": ('{"state": "OPEN"}', 0),
    "error": ("boom", 1),
    "garbage": ("<html>not json</html>", 0),
}


class TestRealGhParsing:
    @pytest.mark.parametrize("cell,want", [("merged", 201), ("closed", 201), ("open", 409),
                                           ("error", 503), ("garbage", 503)])
    def test_gc_cells(self, client, parts, tmp_path, monkeypatch, cell, want):
        out, rc = GH_CELLS[cell]
        _fake_gh(tmp_path, monkeypatch, stdout=out, rc=rc)
        before = _snap(parts)
        resp = _gc(client, "merged")
        assert resp.status_code == want
        if want != 201:
            assert _snap(parts) == before
        elif cell == "closed":
            assert resp.get_json()["status"] == "cancelled"

    @pytest.mark.parametrize("cell,want", [("merged", 201), ("closed", 409), ("open", 409),
                                           ("error", 503), ("garbage", 503)])
    def test_merge_cells(self, client, parts, tmp_path, monkeypatch, cell, want):
        out, rc = GH_CELLS[cell]
        _fake_gh(tmp_path, monkeypatch, stdout=out, rc=rc)
        before = _snap(parts)
        resp = _merge(client)
        assert resp.status_code == want
        if want != 201:
            assert _snap(parts) == before


# ------------------------------------------- thin client: 409 / 503 are readable

class TestThinClientMessages:
    @pytest.mark.parametrize("status,msg", [(409, "PR acme/widgets#42 is still OPEN"),
                                            (503, "could not read the state"),
                                            (403, "is not a participant of PR-42")])
    def test_gc_prints_server_message_and_counts_failure(self, monkeypatch, capsys, status, msg):
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
        assert f"HTTP {status}" in err and msg in err


# ------------------------------------------------- round 3: anchor TOCTOU (Mira)
#
# The gh read is made OUTSIDE the allocator lock (it blocks up to 10s), so the baton
# can be re-anchored while it is outstanding. The write must be bound to the anchor
# the server actually asked GitHub about (the (repo, number) it queried, as the
# normalised stored anchor), compared under the same lock that re-checks status,
# before a seq is claimed. Drift -> 409, nothing written, no seq consumed.
#
# Two race shapes (coordinator, round 3):
#   coord -- different NUMBER through coordination.anchor (Mira's reproduction)
#   route -- SAME number, different REPO through the HTTP /anchor route (the
#            realistic one: the route refuses a number that differs from the id's)
# Each cell reads the anchor back INSIDE the stub and asserts the re-anchor really
# took, so "refused on drift" can't be confused with "drift never happened".

import threading  # noqa: E402

from forum import coordination as _coord  # noqa: E402

NEW_ANCHOR = "pr/acme/widgets/777"
OTHER_REPO_ANCHOR = "pr/other/repo/42"

_SHAPES = {
    "coord": ("coord", NEW_ANCHOR),
    "route": ("route", OTHER_REPO_ANCHOR),
}


def _stored_anchor(store, pid):
    from forum.coordination import markdown as md
    return md.parse_frontmatter(store.read_project(pid))[0].get("github", "").strip()


def _reanchoring_gh(monkeypatch, client, store, allocator, shape, pid="PR-42"):
    """_gh_pr_state that re-anchors ``pid`` while 'running' (via ``shape``), reads the
    anchor back to prove it changed, then reports ``merged`` for the ORIGINAL number."""
    how, new_anchor = _SHAPES[shape]
    box = {"asked": [], "text": None, "seq": None, "new": new_anchor, "after": None}

    def fake(pr_number, repo):
        box["asked"].append((str(pr_number), repo))
        if how == "coord":
            _coord.anchor(store, allocator, pid, github_anchor=new_anchor)
        else:
            r = client.application.test_client().post(
                f"/api/projects/{pid}/anchor", json={"agent": "luria", "github": new_anchor})
            assert r.status_code == 201, r.get_json()
        box["after"] = _stored_anchor(store, pid)
        assert box["after"] == new_anchor, "re-anchor inside the stub did not take"
        box["text"], box["seq"] = store.read_project(pid), allocator.current()
        return "merged"

    monkeypatch.setattr(board_projects, "_gh_pr_state", fake)
    return box


def _assert_drift_refusal(resp, store, allocator, box, pid="PR-42"):
    assert box["after"] == box["new"]                      # the drift really happened
    assert resp.status_code == 409
    err = resp.get_json()["error"].lower()
    assert "anchor changed" in err and "retry" in err and "nothing" in err
    assert ANCHOR in err and box["new"] in err             # old -> new, both named
    assert store.read_project(pid) == box["text"]          # byte-identical to post-re-anchor
    assert allocator.current() == box["seq"]               # no seq claimed
    assert "status: in-progress" in box["text"]
    assert store.read_project(pid) is not None             # still on the active board


@pytest.mark.parametrize("shape", ["coord", "route"])
class TestAnchorDriftDuringGhRead:
    def test_gc_refuses_when_anchor_changes_during_gh_read(self, client, parts, monkeypatch, shape):
        store, allocator = parts
        box = _reanchoring_gh(monkeypatch, client, store, allocator, shape)
        resp = _gc(client, "merged")
        assert box["asked"] == [("42", "acme/widgets")]     # asked about the ORIGINAL only
        _assert_drift_refusal(resp, store, allocator, box)

    def test_merge_refuses_when_anchor_changes_during_gh_read(self, client, parts, monkeypatch, shape):
        store, allocator = parts
        box = _reanchoring_gh(monkeypatch, client, store, allocator, shape)
        resp = _merge(client)
        assert box["asked"] == [("42", "acme/widgets")]
        _assert_drift_refusal(resp, store, allocator, box)

    def test_merge_forced_is_also_bound_to_the_anchor(self, client, parts, monkeypatch, shape):
        store, allocator = parts
        box = _reanchoring_gh(monkeypatch, client, store, allocator, shape)
        resp = client.post("/api/projects/PR-42/merge", json={"agent": "luria", "forced": True})
        _assert_drift_refusal(resp, store, allocator, box)


class TestUnanchoredDrift:
    @pytest.mark.parametrize("route", ["gc", "merge"])
    def test_unanchored_then_anchored_is_refused(self, client, parts, monkeypatch, route):
        """PR-43 is unanchored (no gh call at all); the route reads 'no anchor', then an
        anchor is ADDED before the write: drift, refused."""
        store, allocator = parts
        real = _server._server_pr_read
        seen = {}

        def racing(st, pid):
            out = real(st, pid)
            _coord.anchor(store, allocator, "PR-43", github_anchor=NEW_ANCHOR)
            seen["after"] = _stored_anchor(store, "PR-43")
            seen["text"], seen["seq"] = store.read_project("PR-43"), allocator.current()
            return out

        monkeypatch.setattr(_server, "_server_pr_read", racing)
        if route == "gc":
            resp = _gc(client, "merged", agent="luria", pid="PR-43")
        else:
            resp = _merge(client, pid="PR-43")
        assert seen["after"] == NEW_ANCHOR                  # the anchor really was added
        assert resp.status_code == 409
        err = resp.get_json()["error"].lower()
        assert "anchor changed" in err and NEW_ANCHOR in err
        assert store.read_project("PR-43") == seen["text"]
        assert allocator.current() == seen["seq"]
        assert "status: in-progress" in seen["text"]


class TestMissingAtReadDrift:
    @pytest.mark.parametrize("route", ["gc", "merge"])
    def test_baton_created_anchored_between_read_and_write_is_refused(
            self, client, parts, monkeypatch, route):
        """PR-99 does not exist at read time; it is created WITH an anchor (the real
        init path) before the write. The write must refuse (409), not close it."""
        store, allocator = parts
        assert store.read_project("PR-99") is None
        real = _server._server_pr_read
        seen = {}

        def racing(st, pid):
            out = real(st, pid)
            _seed(store, allocator, "PR-99", ANCHOR)
            seen["after"] = _stored_anchor(store, "PR-99")
            seen["text"], seen["seq"] = store.read_project("PR-99"), allocator.current()
            return out

        monkeypatch.setattr(_server, "_server_pr_read", racing)
        if route == "gc":
            resp = _gc(client, "merged", agent="luria", pid="PR-99")
        else:
            resp = _merge(client, pid="PR-99")
        assert seen["after"] == ANCHOR                      # the baton really appeared
        assert resp.status_code == 409
        err = resp.get_json()["error"].lower()
        assert "anchor changed" in err and ANCHOR in err and "nothing" in err
        assert store.read_project("PR-99") == seen["text"]  # byte-identical
        assert allocator.current() == seen["seq"]           # no seq claimed
        assert "status: in-progress" in seen["text"]


# ------------------------------------------------- round 3: gh admission bound
#
# At most _GH_ADMISSION_LIMIT gh reads in flight across /gc and /merge; the next is
# refused 503 BEFORE any gh call, nothing written. Non-blocking acquire, released in
# a finally; no PR-state cache anywhere.

class TestGhAdmissionBound:
    def test_limit_is_four(self):
        assert _server._GH_ADMISSION_LIMIT == 4

    def test_fifth_concurrent_anchored_request_is_503_without_gh_call(
            self, client, parts, monkeypatch):
        store, allocator = parts
        for i in range(50, 54):
            _seed(store, allocator, f"PR-{i}", f"pr/acme/widgets/{i}")
        release = threading.Event()
        lock = threading.Lock()
        calls = []

        def blocking_gh(pr_number, repo):
            with lock:
                calls.append(str(pr_number))
            release.wait(timeout=20)
            return "merged"

        monkeypatch.setattr(board_projects, "_gh_pr_state", blocking_gh)
        app = client.application
        results = {}

        def fire(i):
            results[i] = app.test_client().post(
                f"/api/projects/PR-{i}/merge", json={"agent": "luria"}).status_code

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(50, 54)]
        for t in threads:
            t.start()
        try:
            for _ in range(400):
                with lock:
                    if len(calls) == 4:
                        break
                threading.Event().wait(0.01)
            assert len(calls) == 4

            before = _snap(parts)
            resp = _gc(client, "merged")                    # the 5th, anchored
            assert resp.status_code == 503
            assert "busy" in resp.get_json()["error"].lower()
            assert "retry" in resp.get_json()["error"].lower()
            with lock:
                assert len(calls) == 4                      # NO gh call for the 5th
            assert _snap(parts) == before
        finally:
            release.set()
            for t in threads:
                t.join(timeout=20)
        assert sorted(results.values()) == [201] * 4

        # slots were released: a follow-up request succeeds and DOES call gh
        resp = _merge(client)
        assert resp.status_code == 201
        assert len(calls) == 5

    def test_acquire_is_non_blocking(self, client, parts, monkeypatch):
        """Pins ``blocking=False``: a blocking acquire (even with a timeout) would
        hold a request thread when the bound is full."""
        acquires = []
        real = _server._GH_ADMISSION

        class Spy:
            def acquire(self, *args, **kwargs):
                acquires.append((args, kwargs))
                blocking = kwargs.get("blocking", args[0] if args else True)
                assert blocking is False, f"blocking acquire: {args} {kwargs}"
                assert "timeout" not in kwargs and len(args) <= 1, (args, kwargs)
                return real.acquire(*args, **kwargs)

            def release(self):
                real.release()

        monkeypatch.setattr(_server, "_GH_ADMISSION", Spy())
        monkeypatch.setattr(board_projects, "_gh_pr_state", lambda n, r: "merged")
        assert _merge(client).status_code == 201
        assert len(acquires) == 1

    def test_slot_released_after_gh_raises(self, client, parts, monkeypatch):
        """finally-release: a gh read that blows up must not leak a slot."""
        def boom(pr_number, repo):
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(board_projects, "_gh_pr_state", boom)
        client.application.config["PROPAGATE_EXCEPTIONS"] = False
        for _ in range(_server._GH_ADMISSION_LIMIT + 2):
            assert _merge(client).status_code == 500
        monkeypatch.setattr(board_projects, "_gh_pr_state", lambda n, r: "merged")
        assert _merge(client).status_code == 201

    def test_unanchored_and_unparseable_never_touch_the_semaphore(self, client, parts, monkeypatch):
        store, allocator = parts
        _seed(store, allocator, "PR-60", "https://github.com/acme/widgets/pull/60")
        taken = []

        class Spy:
            def acquire(self, blocking=True, timeout=None):
                taken.append(1)
                return True

            def release(self):
                pass

        monkeypatch.setattr(_server, "_GH_ADMISSION", Spy())
        assert _gc(client, "cancelled", agent="luria", pid="PR-43").status_code == 201
        assert _merge(client, pid="PR-45").status_code == 201
        assert _merge(client, pid="PR-60").status_code == 503      # unparseable anchor
        assert taken == []


class TestNoDriftControl:
    def test_gc_still_closes_without_drift(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        assert _gc(client, "merged").status_code == 201
        assert "status: merged" in parts[0].read_project("PR-42")

    def test_merge_still_closes_without_drift(self, client, parts, gh_state):
        gh_state["state"] = "merged"
        assert _merge(client).status_code == 201

    def test_unanchored_gc_and_merge_still_work(self, client, parts):
        assert _gc(client, "cancelled", agent="luria", pid="PR-43").status_code == 201
        assert _merge(client, pid="PR-45").status_code == 201

    def test_write_fns_default_is_no_check(self, parts):
        """close() called without expected_anchor (e.g. /status) never checks."""
        store, allocator = parts
        _coord.anchor(store, allocator, "PR-42", github_anchor=NEW_ANCHOR)
        _coord.close(store, allocator, "PR-42", invoker="luria", new_status="cancelled",
                     reason="x")
        assert "status: cancelled" in store.read_project("PR-42")

    def test_write_fns_expected_anchor_matches_normalised(self, parts):
        store, allocator = parts
        _coord.close(store, allocator, "PR-42", invoker="luria", new_status="cancelled",
                     reason="x", expected_anchor="PR/ACME/widgets/42 ")
        assert "status: cancelled" in store.read_project("PR-42")

    def test_write_fns_raise_anchor_changed_without_writing(self, parts):
        store, allocator = parts
        before = _snap(parts)
        with pytest.raises(_coord.AnchorChanged) as ei:
            _coord.close(store, allocator, "PR-42", invoker="luria", new_status="merged",
                         reason="x", expected_anchor="pr/acme/widgets/41")
        assert ei.value.expected == "pr/acme/widgets/41"
        assert ei.value.actual == ANCHOR
        with pytest.raises(_coord.AnchorChanged):
            _coord.merge(store, allocator, "PR-42", merged_by="luria",
                         expected_anchor="")        # "" = expect unanchored
        assert _snap(parts) == before
