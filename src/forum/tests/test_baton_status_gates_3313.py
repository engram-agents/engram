"""Contract tests for engram-alpha#3313 -- status-write gates on a baton.

Written BEFORE the implementation (contract-first, Lei 2026-09-19).

Contract (see the issue + the dispatch spec):
  C1  close/reopen via POST /api/projects/<pid>/status take the requesting
      agent as ``invoker`` and refuse 403 when it is not a current participant;
      the gate lives in the coordination write-fn (a direct call cannot bypass).
  C2  a close-direction write on a baton ALREADY in a closed state is refused
      409 naming the current status; reopen of a closed baton stays legal.
  C3  both refusals write NOTHING (file bytes + seq counter unchanged).
  C4  missing project stays 404, checked FIRST (order: 404 -> 403 -> 409).
  C5  the baton CLI surfaces both refusals (non-zero exit, message printed).
  C6  happy paths unchanged.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forum.coordination import (
    FileStore,
    NotAParticipant,
    ProjectNotFound,
    SeqAllocator,
    close,
    init as _init,
    reopen,
)
from forum.coordination.store_file import CLOSED_STATUSES
from forum.db import init_db
from forum.server import create_app

# append (not insert(0)) -- see test_baton_thin_client.py (#2037/#2063).
_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed(store, allocator, pid="PR-42", status="in-progress"):
    _init(store, allocator, pid, title="t", status=status, turn="ariadne",
          participants=["ariadne", "luria"], turn_reason="start",
          transition_phase="fairy")


@pytest.fixture
def parts(tmp_path):
    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    _seed(store, allocator)
    return store, allocator


@pytest.fixture
def app(tmp_path, parts):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    store, allocator = parts
    application = create_app(db_path, str(tmp_path / "audit.jsonl"))
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = store
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _status(client, agent, new_status, pid="PR-42", reason="r"):
    return client.post(f"/api/projects/{pid}/status", json={
        "agent": agent, "new_status": new_status, "reason": reason})


def _snapshot(store, allocator, pid="PR-42"):
    return store.read_project(pid), allocator.current()


# ---------------------------------------------------------------------------
# Route level: C1
# ---------------------------------------------------------------------------

class TestRouteParticipantGate:
    def test_non_participant_close_is_403(self, client, parts):
        before = _snapshot(*parts)
        resp = _status(client, "mallory", "cancelled")
        assert resp.status_code == 403
        assert "mallory" in resp.get_json()["error"]
        assert "is not a participant of PR-42" in resp.get_json()["error"]
        assert _snapshot(*parts) == before

    def test_non_participant_reopen_is_403(self, client, parts):
        assert _status(client, "ariadne", "merged").status_code == 201
        before = _snapshot(*parts)
        resp = _status(client, "mallory", "in-progress")
        assert resp.status_code == 403
        assert "is not a participant of PR-42" in resp.get_json()["error"]
        assert _snapshot(*parts) == before

    def test_participant_name_is_case_insensitive(self, client):
        assert _status(client, "Ariadne", "cancelled").status_code == 201

    def test_participant_close_then_reopen_still_work_c6(self, client):
        r = _status(client, "luria", "merged")
        assert r.status_code == 201
        assert set(r.get_json()) == {"seq", "turn", "status"}
        assert r.get_json()["status"] == "merged"
        r2 = _status(client, "luria", "in-progress")
        assert r2.status_code == 201
        assert r2.get_json()["status"] == "in-progress"
        assert r2.get_json()["seq"] > r.get_json()["seq"]

    def test_turn_holder_who_is_not_a_participant_is_refused(self, app, client, parts):
        """Gate is the participants list, not the turn holder (mutation (a))."""
        store, _ = parts
        raw = store.read_project("PR-42")
        # turn holder 'borges' is not in participants [ariadne, luria]
        store.write_project("PR-42", raw.replace("turn: ariadne", "turn: borges"), seq=1)
        assert _status(client, "borges", "cancelled").status_code == 403

    def test_sentinel_or_maintainer_listed_as_participant_counts(self, client, parts):
        """The maintainer/sentinel is admitted exactly as any participant is
        (listed in `participants`); there is no separate server-side sentinel
        predicate, and none is added."""
        store, _ = parts
        raw = store.read_project("PR-42")
        store.write_project(
            "PR-42", raw.replace("participants: [ariadne, luria]",
                                 "participants: [ariadne, luria, lei]"), seq=1)
        assert _status(client, "lei", "merged").status_code == 201


# ---------------------------------------------------------------------------
# Route level: C2 + the originating-incident shape
# ---------------------------------------------------------------------------

class TestRouteClosedRefusal:
    def test_closed_statuses_are_read_not_hardcoded(self):
        assert {"merged", "cancelled"} <= set(CLOSED_STATUSES)

    @pytest.mark.parametrize("first", sorted(CLOSED_STATUSES))
    @pytest.mark.parametrize("second", sorted(CLOSED_STATUSES))
    def test_close_on_closed_baton_is_409_naming_status(self, client, parts, first, second):
        assert _status(client, "ariadne", first).status_code == 201
        before = _snapshot(*parts)
        resp = _status(client, "ariadne", second)
        assert resp.status_code == 409
        assert first in resp.get_json()["error"]
        assert _snapshot(*parts) == before

    def test_incident_non_participant_flips_merged_to_cancelled(self, client, parts):
        """ALPHA-PR-3306: a non-participant rewrote merged -> cancelled."""
        assert _status(client, "ariadne", "merged").status_code == 201
        raw_before = parts[0].read_project("PR-42")
        resp = _status(client, "ariadne-probe", "cancelled")
        assert resp.status_code in (403, 409)
        assert parts[0].read_project("PR-42") == raw_before  # byte-identical

    def test_participant_overwrite_of_closed_is_409(self, client, parts):
        assert _status(client, "ariadne", "merged").status_code == 201
        raw_before = parts[0].read_project("PR-42")
        assert _status(client, "ariadne", "cancelled").status_code == 409
        assert parts[0].read_project("PR-42") == raw_before

    def test_reopen_of_closed_is_allowed(self, client):
        _status(client, "ariadne", "cancelled")
        assert _status(client, "ariadne", "in-review").status_code == 201

    def test_reopen_on_already_active_baton_keeps_todays_behaviour(self, client):
        """Not the overwrite defect (nothing terminal is clobbered): unchanged."""
        r = _status(client, "ariadne", "in-review")
        assert r.status_code == 201
        assert r.get_json()["status"] == "in-review"

    def test_gc_route_also_refuses_double_close(self, client, parts):
        """/gc shares close(): the closed-state refusal (C2) applies there too."""
        assert _status(client, "ariadne", "merged").status_code == 201
        raw_before = parts[0].read_project("PR-42")
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "anyone", "new_status": "cancelled", "reason": "gc"})
        assert resp.status_code == 409
        assert parts[0].read_project("PR-42") == raw_before


# ---------------------------------------------------------------------------
# Route level: C4 ordering
# ---------------------------------------------------------------------------

class TestRouteOrdering:
    def test_missing_project_is_404_even_for_a_nobody(self, client):
        assert _status(client, "mallory", "merged", pid="NOPE").status_code == 404
        assert _status(client, "mallory", "in-progress", pid="NOPE").status_code == 404

    def test_participant_check_precedes_closed_check(self, client, parts):
        """non-participant + closed baton -> 403 (does not leak state via 409)."""
        _status(client, "ariadne", "merged")
        assert _status(client, "mallory", "cancelled").status_code == 403


# ---------------------------------------------------------------------------
# Write-fn level: C1 (cannot be bypassed by a direct call), C2, C3
# ---------------------------------------------------------------------------

class TestWriteFnGates:
    def test_close_requires_invoker_keyword(self, parts):
        store, allocator = parts
        with pytest.raises(TypeError):
            close(store, allocator, "PR-42", new_status="merged", reason="r")

    def test_close_non_participant_raises_and_writes_nothing(self, parts):
        store, allocator = parts
        raw, cur = store.read_project("PR-42"), allocator.current()
        with pytest.raises(NotAParticipant):
            close(store, allocator, "PR-42", invoker="mallory",
                  new_status="merged", reason="r")
        assert store.read_project("PR-42") == raw
        assert allocator.current() == cur
        # no seq consumed: the next legitimate write is exactly cur + 1
        assert close(store, allocator, "PR-42", invoker="ariadne",
                     new_status="merged", reason="r") == cur + 1

    def test_close_on_closed_raises_and_writes_nothing(self, parts):
        store, allocator = parts
        from forum.coordination import AlreadyClosed
        close(store, allocator, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        raw, cur = store.read_project("PR-42"), allocator.current()
        with pytest.raises(AlreadyClosed) as ei:
            close(store, allocator, "PR-42", invoker="ariadne",
                  new_status="cancelled", reason="r")
        assert "merged" in str(ei.value)
        assert store.read_project("PR-42") == raw
        assert allocator.current() == cur
        assert reopen(store, allocator, "PR-42", invoker="ariadne",
                      new_status="in-progress") == cur + 1

    def test_reopen_non_participant_raises_and_writes_nothing(self, parts):
        store, allocator = parts
        close(store, allocator, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        raw, cur = store.read_project("PR-42"), allocator.current()
        with pytest.raises(NotAParticipant):
            reopen(store, allocator, "PR-42", invoker="mallory", new_status="in-progress")
        assert store.read_project("PR-42") == raw
        assert allocator.current() == cur

    def test_invoker_match_is_case_insensitive_for_direct_calls(self, parts):
        """The route lowercases `agent` already, so only a direct call can
        distinguish a case-sensitive gate from the contract."""
        store, allocator = parts
        close(store, allocator, "PR-42", invoker="  Ariadne ", new_status="merged", reason="r")
        reopen(store, allocator, "PR-42", invoker="LURIA", new_status="in-progress")

    def test_missing_project_is_not_found_first(self, parts):
        store, allocator = parts
        with pytest.raises(ProjectNotFound):
            close(store, allocator, "NOPE", invoker="mallory", new_status="merged", reason="r")
        with pytest.raises(ProjectNotFound):
            reopen(store, allocator, "NOPE", invoker="mallory", new_status="in-progress")

    def test_participant_close_log_line_unchanged_c6(self, parts):
        store, allocator = parts
        close(store, allocator, "PR-42", invoker="ariadne", new_status="merged",
              reason="done", ts="2026-10-05T00:00:00Z")
        assert "- 2026-10-05T00:00:00Z close: merged — done" in store.read_project("PR-42")


# ---------------------------------------------------------------------------
# CLI: C5
# ---------------------------------------------------------------------------

CFG = {"mode": "multi", "forum": {"url": "http://test-forum:5002"}}


def _raw(status="in-progress"):
    return ("---\nproject: PR-50\ntitle: t\nstatus: %s\nturn: borges\n"
            "turn_since: 2026-06-26T12:00:00Z\nturn_reason: start\n"
            "participants: [borges, ariadne]\n---\nbody\n" % status)


def _cli_client(raw, exc):
    c = MagicMock()
    c.get.return_value = {"raw": raw}
    c.post.side_effect = exc
    return c


def _ns(**kw):
    d = {"force": False, "dry_run": False, "reason": "r", "status": "merged", "limit": 30}
    d.update(kw)
    return argparse.Namespace(**d)


class TestCliSurfacesRefusals:
    @pytest.mark.parametrize("code,msg", [
        (403, "agent 'mallory' is not a participant of PR-50"),
        (409, "project PR-50 is already merged"),
    ])
    def test_close_prints_server_message_and_exits_nonzero(self, capsys, code, msg):
        import json
        c = _cli_client(_raw(), ForumHttpError(code, json.dumps({"error": msg})))
        with pytest.raises(SystemExit) as ei:
            _baton.cmd_close(_ns(project_id="PR-50", status="merged"), CFG, "mallory", c)
        assert ei.value.code not in (0, None)
        err = capsys.readouterr().err
        assert msg in err                      # verbatim, not buried in a JSON blob
        assert f"HTTP {code}" in err or "refused" in err

    @pytest.mark.parametrize("code,msg", [
        (403, "agent 'mallory' is not a participant of PR-50"),
    ])
    def test_reopen_prints_server_message_and_exits_nonzero(self, capsys, code, msg):
        import json
        c = _cli_client(_raw("merged"), ForumHttpError(code, json.dumps({"error": msg})))
        with pytest.raises(SystemExit) as ei:
            _baton.cmd_reopen(_ns(project_id="PR-50", status="in-progress"), CFG, "mallory", c)
        assert ei.value.code not in (0, None)
        assert msg in capsys.readouterr().err

    def test_gc_does_not_abort_sweep_on_one_refused_baton(self, monkeypatch, capsys):
        projects = [
            {"project_id": "PR-1", "github": "pr/o/r/1"},
            {"project_id": "PR-2", "github": "pr/o/r/2"},
        ]
        c = MagicMock()
        c.get.return_value = {"projects": projects}
        calls = []

        def _post(url, body):
            calls.append(url)
            if url.endswith("PR-1/gc"):
                raise ForumHttpError(409, '{"error": "project PR-1 is already merged"}')
            return {"seq": 3}

        c.post.side_effect = _post
        monkeypatch.setattr(_baton.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(_baton.subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stdout="MERGED\n", stderr=""))
        with pytest.raises(SystemExit):          # non-zero: one write failed
            _baton.cmd_gc(_ns(dry_run=False), CFG, "ariadne", c)
        assert calls == ["/api/projects/PR-1/gc", "/api/projects/PR-2/gc"]
        out = capsys.readouterr()
        assert "PR-1" in out.err and "closed PR-2" in out.out


# ---------------------------------------------------------------------------
# Round 2: /gc exemption pin, read-outside-lock window, 'done' measurement
# ---------------------------------------------------------------------------

class TestGcRouteExemptionPinned:
    def test_non_participant_can_gc_close_an_active_baton(self, client, parts):
        """The /gc route deliberately has NO participant gate (open design
        question on #3313): a sweeper who is not a participant still closes."""
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "sweeper-not-listed", "new_status": "merged", "reason": "gc"})
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "merged"
        assert "status: merged" in parts[0].read_project("PR-42")


class _RacingAllocator:
    """Runs ``hook`` once, just BEFORE the caller takes the allocator lock
    (via allocate() or locked()), i.e. after any read the caller did outside
    the lock. Deterministic stand-in for a competing writer landing in the
    read-to-lock window."""

    def __init__(self, inner, hook):
        self._inner, self._hook, self._fired = inner, hook, False

    def _fire(self):
        if not self._fired:
            self._fired = True
            self._hook()

    def allocate(self):
        self._fire()
        return self._inner.allocate()

    def locked(self):
        self._fire()
        return self._inner.locked()

    def current(self):
        return self._inner.current()


def _drop_ariadne(store, allocator):
    raw = store.read_project("PR-42")
    store.write_project(
        "PR-42", raw.replace("participants: [ariadne, luria]", "participants: [luria]"),
        seq=allocator.current())


class TestGatesAreRecheckedInsideTheLock:
    def test_concurrent_close_second_gets_already_closed(self, parts):
        store, allocator = parts
        start = allocator.current()

        def competitor():
            close(store, allocator, "PR-42", invoker="luria",
                  new_status="merged", reason="first closer")

        from forum.coordination import AlreadyClosed
        racing = _RacingAllocator(allocator, competitor)
        with pytest.raises(AlreadyClosed):
            close(store, racing, "PR-42", invoker="ariadne",
                  new_status="cancelled", reason="second closer")
        raw = store.read_project("PR-42")
        assert "status: merged" in raw and "status: cancelled" not in raw
        assert raw.count(" close: ") == 1 and "first closer" in raw
        assert allocator.current() == start + 1  # seq consumed exactly once

    def test_participant_removed_before_lock_is_refused_on_close(self, parts):
        store, allocator = parts
        racing = _RacingAllocator(allocator, lambda: _drop_ariadne(store, allocator))
        with pytest.raises(NotAParticipant):
            close(store, racing, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        assert "status: in-progress" in store.read_project("PR-42")

    def test_participant_removed_before_lock_is_refused_on_reopen(self, parts):
        store, allocator = parts
        close(store, allocator, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        racing = _RacingAllocator(allocator, lambda: _drop_ariadne(store, allocator))
        before_seq = allocator.current()
        with pytest.raises(NotAParticipant):
            reopen(store, racing, "PR-42", invoker="ariadne", new_status="in-progress")
        assert "status: merged" in store.read_project("PR-42")
        assert allocator.current() == before_seq


class _ObservedAllocator:
    """Wraps an allocator and records whether a locked()/allocate() context is
    currently open, using only enter/exit of the public context managers."""

    def __init__(self, inner):
        self._inner, self.held = inner, False

    def _wrap(self, cm):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            with cm as v:
                self.held = True
                try:
                    yield v
                finally:
                    self.held = False
        return _cm()

    def allocate(self):
        return self._wrap(self._inner.allocate())

    def locked(self):
        return self._wrap(self._inner.locked())

    def current(self):
        return self._inner.current()


class _AssertHeldStore:
    """Delegating store whose write_project fails unless the allocator lock is
    held at that moment (seq/write co-atomicity, fork-4)."""

    def __init__(self, inner, allocator):
        self._inner, self._alloc, self.writes = inner, allocator, 0

    def read_project(self, pid):
        return self._inner.read_project(pid)

    def write_project(self, pid, content, *, seq):
        assert self._alloc.held, "write_project ran OUTSIDE the allocator lock"
        self.writes += 1
        return self._inner.write_project(pid, content, seq=seq)


class TestWriteIsInsideTheLock:
    def test_close_writes_while_lock_held(self, parts):
        store, allocator = parts
        obs = _ObservedAllocator(allocator)
        stub = _AssertHeldStore(store, obs)
        close(stub, obs, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        assert stub.writes == 1

    def test_reopen_writes_while_lock_held(self, parts):
        store, allocator = parts
        close(store, allocator, "PR-42", invoker="ariadne", new_status="merged", reason="r")
        obs = _ObservedAllocator(allocator)
        stub = _AssertHeldStore(store, obs)
        reopen(stub, obs, "PR-42", invoker="ariadne", new_status="in-progress")
        assert stub.writes == 1


class TestArbitraryStatusMeasured:
    """Pre-existing behaviour, unchanged by #3313 (measured, not read off code):
    a participant posting a status outside the active/closed sets goes down
    the CLOSE path (the route only dispatches active -> reopen, else close)."""

    def test_participant_posting_done_on_active_baton(self, client, parts):
        r = _status(client, "ariadne", "done")
        assert r.status_code == 201
        assert r.get_json()["status"] == "done"
        raw = parts[0].read_project("PR-42")
        assert "status: done" in raw
        assert " close: done — " in raw      # logged by close(), not reopen()
        # 'done' is not in CLOSED_STATUSES, so the baton is NOT treated as
        # closed afterwards: a second write is not refused with 409.
        assert _status(client, "ariadne", "merged").status_code == 201
