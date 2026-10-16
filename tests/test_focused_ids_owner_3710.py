"""Contract tests for U8 (#3710, part of #2652): `_hooklib._focused_ids` reads through
the caller's connection, closing U6b's flag-ON zero-opens hole when in-context exclusion
is ON.

Written BEFORE the implementation (docs/CONTRACT-FIRST-TESTS.md). Design seat: Borges.

The contract:

  (a) tripwire flag ON + `recall_in_context_exclusion.enabled` ON + the route probe live
      for the session: a full fire opens the store ZERO times, and the owner is asked the
      `_focused_ids` statement (so the exclusion source was read, through the owner);
  (b) `_focused_ids(read_conn=<owner shim>)` returns the same set as the direct path on
      one store;
  (c) a shim that raises the client's typed failure (Unavailable / Rejected) -> (set(),
      False), and NO direct open follows;
  (d) a caller-owned connection is never closed by `_focused_ids`;
  (e) no connection passed: the direct path, unchanged (one open of knowledge.db, the same
      set).
"""
from __future__ import annotations

import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from _fake_owner import FakeOwner
from test_tripwire_owner_read_3665 import (  # noqa: F401 -- fixtures and helpers reused
    DAEMON_RESULT,
    FULL_CMD,
    OWNER_KINDS,
    Opens,
    _restore_imports,
    home,
    load_hook,
    make_store,
    owner_of,
    run_main,
    set_config,
    spying_read,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
_HOOKS_SRC = str(REPO_ROOT / "src" / "engram" / "hooks" / "claude")
for _p in (_SRC_ENGRAM, _HOOKS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engramd_client as ec  # noqa: E402

FOCUSED_SQL = "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 ORDER BY focused_at"
SESSION = "s-3665"  # the session id test_tripwire_owner_read_3665.payload() sends
EXCLUSION_ON = {"recall_in_context_exclusion": {"enabled": True}}


def _focus(db: Path, *ids: str) -> None:
    conn = sqlite3.connect(str(db))
    conn.executemany("UPDATE nodes SET focused_at = '2026-10-12T00:00:00' WHERE id = ?",
                     [(i,) for i in ids])
    conn.commit()
    conn.close()


def _hooklib():
    import _hooklib
    return _hooklib


def _probe_live(user_home: Path) -> Path:
    """The route probe is live for SESSION when its dir exists under $HOME (route_probe_dir)."""
    d = user_home.joinpath(".engram", "probe", "mods-recall", SESSION)
    d.mkdir(parents=True, exist_ok=True)
    return d


# =========================================================================================
# (a) ZERO OPENS with exclusion ON and the probe live
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_a_flag_on_exclusion_on_probe_live_a_full_fire_opens_the_store_zero_times(
        owner_kind, home, tmp_path, monkeypatch):
    """U6b's cell (a) on the switches it did not cover: exclusion ON (the in-turn recall
    path builds the exclusion resolver) and the route probe live (the route ledger builds
    the same resolver). Every direct open RAISES AND RECORDS; the record is empty, and the
    owner received the `_focused_ids` statement, so the focused source was read through it.
    Mutation: drop the pass-through in build_exclusion_resolver OR in write_route_ledger
    -> a direct open is recorded -> red."""
    db = make_store(home)
    _focus(db, "ob_0001")
    set_config(home, flag=True, extra=EXCLUSION_ON)
    user_home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(user_home))
    probe = _probe_live(user_home)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        seen = spying_read(monkeypatch)
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert opens.record == [], opens.record
    assert FOCUSED_SQL in {s for s, _ in seen}, "the focused source never reached the owner"
    ledger = list(probe.rglob("*.jsonl"))
    assert ledger, "the route probe did not write a ledger line (the probe path did not run)"


def test_a_control_exclusion_and_probe_off_never_ask_for_focused_ids(home, tmp_path, monkeypatch):
    """Positive control for (a)'s FOCUSED_SQL assertion: with exclusion OFF and no probe the
    focused statement is never sent, so (a)'s 'reached the owner' check is not vacuous."""
    make_store(home)
    set_config(home, flag=True)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        seen = spying_read(monkeypatch)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert FOCUSED_SQL not in {s for s, _ in seen}


# =========================================================================================
# (b) owner and direct agree
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_b_focused_ids_via_the_shim_equals_the_direct_path(owner_kind, home, monkeypatch):
    """Same store, both paths, same (set, True). Two focused nodes plus one focused but
    NOT current (must be excluded by both). Mutation: drop `is_current = 1` handling or
    read a different column on the shim path -> the sets differ -> red."""
    db = make_store(home)
    _focus(db, "ob_0003", "ls_0001", "ls_old")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    direct = hl._focused_ids()
    with owner_of(owner_kind, home):
        conn = ec.OwnerReadConnection(total_budget=2.0, per_call_timeout=1.0)
        via = hl._focused_ids(read_conn=conn)
    assert direct == ({"ob_0003", "ls_0001"}, True)
    assert via == direct


# =========================================================================================
# (c) typed failure -> unreadable, no direct open
# =========================================================================================

class _FailingConn:
    def __init__(self, exc):
        self.exc = exc
        self.closed = False

    def execute(self, sql, params=()):
        raise self.exc

    def close(self):
        self.closed = True


def _owner_error(kind):
    """The client's typed failure: one OwnerReadError carrying `.kind`."""
    return ec.OwnerReadError(kind)


@pytest.mark.parametrize("kind", ["unavailable", "rejected"])
def test_c_shim_failure_reads_unreadable_and_never_opens_the_store(kind, home, monkeypatch):
    """The shim raises the client's typed failure -> (set(), False), and no direct open
    follows (a fallback to connect_regular_ro would be a store open on a flag-ON seat).
    Mutation: catch the failure and retry on the direct path -> an open is recorded -> red."""
    make_store(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    opened = []

    def guard(*a, **k):
        opened.append(a)
        raise AssertionError("direct open")
    monkeypatch.setattr(hl, "connect_regular_ro", guard)
    monkeypatch.setattr(sqlite3, "connect", guard)
    assert hl._focused_ids(read_conn=_FailingConn(_owner_error(kind))) == (set(), False)
    assert opened == []


# =========================================================================================
# (d) the caller owns the connection
# =========================================================================================

def test_d_a_caller_owned_connection_is_not_closed(home, monkeypatch):
    """`_focused_ids` must not close a connection it was handed (the tripwire hook shares
    ONE owner connection across the whole fire). Checked on a real sqlite connection: still
    usable afterwards. Mutation: close read_conn in the finally -> the next execute raises
    ProgrammingError -> red."""
    db = make_store(home)
    _focus(db, "ob_0002")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    conn = sqlite3.connect(str(db))
    try:
        assert hl._focused_ids(read_conn=conn) == ({"ob_0002"}, True)
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_d_a_failing_caller_connection_is_not_closed_either(home, monkeypatch):
    """The error path must not close it either. Mutation: close in the except -> red."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    c = _FailingConn(RuntimeError("boom"))
    assert hl._focused_ids(read_conn=c) == (set(), False)
    assert c.closed is False


# =========================================================================================
# (e) no connection: the direct path, unchanged
# =========================================================================================

def test_e_no_connection_opens_knowledge_db_once_and_closes_it(home, monkeypatch):
    """Direct path: exactly one open, of $ENGRAM_HOME/knowledge.db, closed afterwards, same
    result as before U8. Mutation: route the default through anything but connect_regular_ro
    on knowledge.db, or skip the close -> red."""
    db = make_store(home)
    _focus(db, "ob_0004")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    real = hl.connect_regular_ro
    calls, conns = [], []

    def spy(path, *a, **k):
        calls.append(Path(path))
        c = real(path, *a, **k)
        conns.append(c)
        return c
    monkeypatch.setattr(hl, "connect_regular_ro", spy)
    assert hl._focused_ids() == ({"ob_0004"}, True)
    assert calls == [db]
    with pytest.raises(sqlite3.ProgrammingError):
        conns[0].execute("SELECT 1")


# =========================================================================================
# plumbing: the resolver builders pass the connection through
# =========================================================================================

def test_build_exclusion_resolver_passes_the_connection_through(home, tmp_path, monkeypatch):
    """build_exclusion_resolver(session_id, read_conn=c) must hand `c` to _focused_ids.
    Mutation: drop it at either hop (build_exclusion_resolver -> make_in_context_resolver
    -> _focused_ids) -> `got` is [None] -> red."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    hl = _hooklib()
    got = []
    monkeypatch.setattr(hl, "_focused_ids", lambda read_conn=None: (got.append(read_conn), (set(), True))[1])
    sentinel = object()
    assert hl.build_exclusion_resolver(SESSION, read_conn=sentinel) is not None
    assert got == [sentinel]


def test_write_route_ledger_passes_the_connection_through(home, tmp_path, monkeypatch):
    """write_route_ledger(..., read_conn=c) with the probe live must hand `c` to
    _focused_ids. Mutation: drop it -> `got` is [None] -> red."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    user_home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(user_home))
    _probe_live(user_home)
    hl = _hooklib()
    got = []
    monkeypatch.setattr(hl, "_focused_ids", lambda read_conn=None: (got.append(read_conn), (set(), True))[1])
    sentinel = object()
    hl.write_route_ledger("tool_recall", SESSION, "k", "q", DAEMON_RESULT, [], {},
                          source="daemon", read_conn=sentinel)
    assert got == [sentinel]



def test_f_flag_on_client_import_failure_means_build_nothing(home, monkeypatch):
    """`_focused_read_conn` with the flag ON and the engramd client failing to import must
    answer (False, None) -- build NO resolver -- and record the failure (one loud notice),
    never (True, None), which would send the resolver to a direct open on a flag-ON fire.
    Tested on the helper itself, deliberately: in a real fire the hook's earlier read sites
    hit the same import failure first and the in-turn recall path returns before either
    resolver is built (a full-fire version of this cell stayed green under the mutation
    below, measured), so this branch is defence in depth. Mutation: return (True, None) on
    the failure -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)

    def _no_client():
        raise ImportError("engramd_client unavailable (test)")
    monkeypatch.setattr(mod, "_import_engramd_client", _no_client)
    monkeypatch.setattr(mod, "_OWNER_RUN", {"on": True, "conn": None, "failure": None})
    assert mod._focused_read_conn() == (False, None)
    assert mod._OWNER_RUN["failure"]["site"] == "focused_ids"
    assert mod._OWNER_RUN["failure"]["kind"] == "client_import"


def test_f_flag_off_means_the_direct_path(home, monkeypatch):
    """Flag OFF: (True, None), the resolver reads directly as before U8, and the client is
    never touched. Mutation: return the shim regardless of the flag -> red."""
    make_store(home)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_import_engramd_client",
                        lambda: (_ for _ in ()).throw(AssertionError("client touched")))
    monkeypatch.setattr(mod, "_OWNER_RUN", {"on": False, "conn": None, "failure": None})
    assert mod._focused_read_conn() == (True, None)


class _FalsyConn:
    """A valid connection that is falsy (defines __len__ == 0), like some shims."""
    def __init__(self, conn):
        self._c = conn
        self.closed = False

    def __len__(self):
        return 0

    def execute(self, sql, params=()):
        return self._c.execute(sql, params)

    def close(self):
        self.closed = True


def test_d_a_falsy_caller_connection_is_still_the_callers(home, monkeypatch):
    """Ownership is `read_conn is None`, not truthiness: a falsy-but-valid caller connection
    is used and not closed, and no direct open happens. Mutation: `owned = not read_conn`
    -> it opens directly and the record is non-empty -> red (reviewer-fairy round 1, M8)."""
    db = make_store(home)
    _focus(db, "ob_0005")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    real = sqlite3.connect(str(db))
    opened = []
    monkeypatch.setattr(hl, "connect_regular_ro", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("direct open")))
    c = _FalsyConn(real)
    try:
        assert hl._focused_ids(read_conn=c) == ({"ob_0005"}, True)
        assert c.closed is False and opened == []
    finally:
        real.close()


# =========================================================================================
# (g) a failed focused read is REPORTED to the caller (Borges, #3827), fail-open unchanged
# =========================================================================================

def test_g_on_failure_hears_why_a_read_failed_and_the_contract_is_unchanged(home, monkeypatch):
    """`_focused_ids(read_conn=..., on_failure=cb)`: a failing read calls cb(exc) ONCE with the
    very exception and still returns (set(), False); a good read never calls it; with no cb the
    behaviour is exactly as before; a cb that raises changes nothing (fail-open).
    Mutation: no call -> `heard == []`; calling on success; letting the cb's raise escape."""
    make_store(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    hl = _hooklib()
    boom = ec.OwnerReadError("rejected")
    heard = []
    assert hl._focused_ids(read_conn=_FailingConn(boom), on_failure=heard.append) == (set(), False)
    assert heard == [boom] and heard[0] is boom
    assert hl._focused_ids(read_conn=_FailingConn(boom)) == (set(), False)
    assert hl._focused_ids(read_conn=_FailingConn(boom), on_failure=lambda e: 1 / 0) == (set(), False)
    ok = []
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        assert hl._focused_ids(read_conn=conn, on_failure=ok.append)[1] is True
    finally:
        conn.close()
    assert ok == []


def test_g_on_failure_is_threaded_through_both_resolver_builders(home, tmp_path, monkeypatch):
    """build_exclusion_resolver(..., on_failure=cb) and write_route_ledger(..., on_failure=cb) (the
    probe live) both hand cb to the focused read: a failing connection reaches it from each.
    The resolver is still built (fail-open). Mutation: drop the kwarg at any hop -> `heard` empty."""
    make_store(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    user_home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(user_home))
    _probe_live(user_home)
    hl = _hooklib()
    boom = ec.OwnerReadError("rejected")
    heard = []
    assert hl.build_exclusion_resolver(SESSION, read_conn=_FailingConn(boom), on_failure=heard.append) is not None
    assert heard == [boom]
    hl.write_route_ledger("tool_recall", SESSION, "k", "q", DAEMON_RESULT, [], {}, source="daemon",
                          read_conn=_FailingConn(boom), on_failure=heard.append)
    assert heard == [boom, boom]


def test_g_without_on_failure_the_plumbing_passes_no_extra_kwarg(home, tmp_path, monkeypatch):
    """Callers that never heard of on_failure see `_focused_ids` called exactly as before (the
    existing plumbing cells replace it with a one-kwarg lambda). Mutation: always passing
    on_failure=None -> TypeError inside the resolver -> red."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    hl = _hooklib()
    monkeypatch.setattr(hl, "_focused_ids", lambda read_conn=None: (set(), True))
    assert hl.build_exclusion_resolver(SESSION, read_conn=object()) is not None


@pytest.mark.parametrize("probe_live", [False, True], ids=["no-probe", "probe-live"])
@pytest.mark.parametrize("reason", ["refused_at_load", "unregistered_template", "result_too_large"])
def test_g_tripwire_flag_on_a_focused_template_refusal_is_recorded_once_and_the_fire_works(
        reason, probe_live, home, tmp_path, monkeypatch):
    """Tripwire hook, flag ON, exclusion ON, the owner refuses ONLY the focused-ids template:
    exactly ONE owner-failure line at site `focused_ids` (the hook's own spelling, shared with
    its build-failure record), kind rejected; the tripwire nudge is still delivered and the fire
    exits 0 (fail-open unchanged). Mutation: no recording in the tripwire hook -> zero lines."""
    make_store(home)
    set_config(home, flag=True, extra=EXCLUSION_ON)
    user_home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(user_home))
    if probe_live:
        _probe_live(user_home)
    mod = load_hook(home, monkeypatch)
    real = ec.read

    def fake(method, params, **k):
        if method == "read" and (params or {}).get("sql") == FOCUSED_SQL:
            return ec.Rejected(reason=reason, detail="template-scoped", remedy="n/a")
        return real(method, params, **k)
    monkeypatch.setattr(ec, "read", fake)
    with FakeOwner(home):
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    lines = re.findall(r"^engram-lesson-tripwire-hook: owner read failed: (\w+) at (\w+)$", run.stderr, re.M)
    assert lines == [("rejected", "focused_ids")], (lines, run.stderr)
    assert "Nudge for tripwire one" in run.ctx, run.ctx


def test_g_tripwire_flag_off_a_failing_focused_read_records_nothing(home, tmp_path, monkeypatch):
    """Tripwire hook, flag OFF: a failing direct focused read stays silent as before (no owner
    line). Mutation: wiring the recorder on the flag-OFF path."""
    make_store(home)
    set_config(home, flag=False, extra=EXCLUSION_ON)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    mod = load_hook(home, monkeypatch)
    hl = _hooklib()
    real = hl.connect_regular_ro

    def selective(path, *a, **k):
        if k.get("timeout") == 0.2:
            raise sqlite3.OperationalError("focused read down (test)")
        return real(path, *a, **k)
    monkeypatch.setattr(hl, "connect_regular_ro", selective)
    run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "owner read failed" not in run.stderr
    assert "Nudge for tripwire one" in run.ctx
