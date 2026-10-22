"""Contract tests for engram-alpha#3240 items 2 and 3 -- written BEFORE the
implementation (contract-first).

Item 2: the owner runs engram_walguard.assert_not_displaced as its own
self-check before store-touching methods (snapshot), answers
rejected(reason="substrate_displaced", remedy=...), never touches the store
then, and does NOT cleanly .close() its connections afterwards (a clean
close of a stale holder checkpoints an orphan WAL over newer data).

Item 3: pin the premise behind _handle_snapshot's os.access destination
check (owner's rights == caller's rights because the peer-credential gate
admits only the owner's uid).
"""

from __future__ import annotations

import gc
import hashlib
import os
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner
from _owner_store import stamp_current_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_walguard  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="displacement detection is Linux-only (/proc)"
    ),
    requires_linux_owner,
]


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="edd-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _seed_wal_store(home: Path) -> None:
    conn = sqlite3.connect(str(home / eo.DEFAULT_STORE_NAME))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO widgets (name) VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()
    stamp_current_schema(home / eo.DEFAULT_STORE_NAME)  # #3753 (D5 starts the owner in a child)


def _snap(owner, dest: Path):
    return ec.read(
        "snapshot", {"dest_path": str(dest)}, timeout=3.0, socket_path=owner.socket_path
    )


@pytest.fixture
def owner(home):
    _seed_wal_store(home)
    o = eo.EngramdOwner(home, request_deadline=0.6)
    o.start(install_signal_handlers=False)
    # Touch the store once so the owner's connections hold -wal/-shm fds.
    first = _snap(o, home / "warm.db")
    assert isinstance(first, ec.Ok)
    try:
        yield o
    finally:
        o.stop()


def _displace(home: Path, suffix: str) -> None:
    os.unlink(str(home / (eo.DEFAULT_STORE_NAME + suffix)))


# -- D1 --------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["-shm", "-wal"])
def test_D1_displaced_snapshot_rejected_and_store_untouched(owner, home, suffix):
    _displace(home, suffix)
    # Premise check (not a monkeypatch): the real detector reports it.
    verdict = engram_walguard.detect_shm_displacement(str(owner.store_path))
    assert verdict is not None and verdict.get("reason") != "detection_unavailable", verdict

    dest = home / "after.db"
    result = _snap(owner, dest)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "substrate_displaced"
    assert result.remedy
    assert not dest.exists()


# -- D2 --------------------------------------------------------------------


def test_D2_healthy_snapshot_ok(owner, home):
    dest = home / "ok.db"
    assert isinstance(_snap(owner, dest), ec.Ok)
    assert dest.exists()


# -- D3 --------------------------------------------------------------------


def test_D3_detection_unavailable_proceeds(owner, home, monkeypatch):
    monkeypatch.setattr(
        engram_walguard,
        "detect_shm_displacement",
        lambda db_path: {"reason": "detection_unavailable", "why": "test"},
    )
    assert isinstance(_snap(owner, home / "u.db"), ec.Ok)


# -- D4 --------------------------------------------------------------------


def test_D4_ping_unaffected_while_displaced(owner, home):
    _displace(home, "-shm")
    result = ec.read("ping", {}, timeout=2.0, socket_path=owner.socket_path)
    assert isinstance(result, ec.Ok)


# -- D5 (property, subprocess) -----------------------------------------------

_OWNER_SCRIPT = (
    "import sys; sys.path.insert(0, %r); "
    "import engramd_owner; sys.exit(engramd_owner.main())"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_ping(sock: Path, proc, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"owner subprocess exited early: rc={proc.returncode}")
        if isinstance(ec.read("ping", {}, timeout=1.0, socket_path=sock), ec.Ok):
            return
        time.sleep(0.1)
    raise AssertionError("owner subprocess never answered ping")


def test_D5_displaced_owner_process_exits_without_clean_close(home):
    """PROPERTY: after a displaced shutdown the process exits with
    EXIT_DISPLACED and the store's -wal is byte-identical -- no clean close
    (no checkpoint/truncate/delete) happened, neither at drop time, at GC,
    nor at interpreter finalization."""
    _seed_wal_store(home)
    env = dict(os.environ, ENGRAM_HOME=str(home))
    proc = subprocess.Popen(
        [sys.executable, "-c", _OWNER_SCRIPT % _SRC_ENGRAM],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        sock = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
        _wait_for_ping(sock, proc)
        first = ec.read("snapshot", {"dest_path": str(home / "w.db")}, timeout=5.0, socket_path=sock)
        assert isinstance(first, ec.Ok), first
        _displace(home, "-shm")
        second = ec.read("snapshot", {"dest_path": str(home / "x.db")}, timeout=5.0, socket_path=sock)
        assert isinstance(second, ec.Rejected) and second.reason == "substrate_displaced", second

        wal = home / (eo.DEFAULT_STORE_NAME + "-wal")
        assert wal.exists(), "premise: owner must hold a -wal before SIGTERM"
        before = (wal.stat().st_size, _sha(wal))

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stderr is not None:
            proc.stderr.close()

    assert wal.exists(), "-wal was removed: a clean close (checkpoint) happened"
    assert (wal.stat().st_size, _sha(wal)) == before
    assert rc == 3
    assert eo.EXIT_DISPLACED == 3


# -- D5b (in-process keepalive) ------------------------------------------------


def _store_fd_count(home: Path) -> int:
    base = str(home / eo.DEFAULT_STORE_NAME)
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(base):
            n += 1
    return n


def test_D5b_displaced_connections_stay_open_after_stop_and_gc(owner, home):
    _displace(home, "-shm")
    result = _snap(owner, home / "d5b.db")
    assert isinstance(result, ec.Rejected) and result.reason == "substrate_displaced"
    before = _store_fd_count(home)
    assert before > 0
    owner.stop()
    gc.collect()
    assert _store_fd_count(home) >= before


# -- D6 ----------------------------------------------------------------------


def test_D6_healthy_stop_closes_both_connections(owner, home):
    oc, cc = owner._owner_conn, owner._client_read_conn
    owner.stop()
    for conn in (oc, cc):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("select 1")


# -- D7 ----------------------------------------------------------------------


def test_D7_unrequested_displacement_caught_at_shutdown(owner, home):
    oc, cc = owner._owner_conn, owner._client_read_conn
    _displace(home, "-shm")  # NO further request after this
    owner.stop()
    assert owner._displaced is True
    for conn in (oc, cc):
        conn.execute("select 1")  # not closed
    keep = eo._DISPLACED_CONNECTIONS_KEEPALIVE
    assert any(c is oc for c in keep) and any(c is cc for c in keep)


# -- D8 ----------------------------------------------------------------------


def test_D8a_a_displaced_stopped_object_refuses_a_second_start_and_touches_nothing(owner, home, monkeypatch):
    """One owner object, one lifetime (#4068): a displaced, stopped owner is never restarted. The refusal
    comes before anything is touched: the parked connections are the SAME objects and still open, the
    maintenance lock is neither re-taken nor released, and no socket or pidfile appears."""
    import engram_core as core

    oc, cc = owner._owner_conn, owner._client_read_conn
    _displace(home, "-shm")
    result = _snap(owner, home / "d8.db")
    assert isinstance(result, ec.Rejected) and result.reason == "substrate_displaced"
    owner.stop()
    assert owner._displaced is True
    keep = eo._DISPLACED_CONNECTIONS_KEEPALIVE
    assert any(c is oc for c in keep) and any(c is cc for c in keep)
    fd_before = owner._maint_lock_fd
    run_dir = owner.socket_path.parent
    run_before = sorted(p.name for p in run_dir.iterdir())
    acquires = []
    real_acquire = core.acquire_store_maint_lock

    def spy(*a, **kw):
        acquires.append(a)
        return real_acquire(*a, **kw)

    try:
        # A scoped context, not monkeypatch.undo(): undo() would also revert the conftest autouse patches.
        with monkeypatch.context() as mp:
            mp.setattr(core, "acquire_store_maint_lock", spy)
            with pytest.raises(eo.OwnerRestartRefused):
                owner.start(install_signal_handlers=False)
            assert acquires == [], f"the refused start took the lock again: {acquires}"
            # The lock is exactly as the displaced stop left it: still held, by the same fd (a displaced
            # owner's lock lives as long as its parked connections), and not touched by the refusal.
            assert owner._maint_lock_fd == fd_before
        assert "_2126_no_real_global_gitconfig" in os.environ.get("GIT_CONFIG_GLOBAL", ""), \
            "an early monkeypatch.undo() reverted the conftest autouse patches"
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(str(owner.store_path), "x")
        # The parked connections are the same objects and still open.
        assert any(c is oc for c in keep) and any(c is cc for c in keep)
        for conn in (oc, cc):
            conn.execute("select 1")  # not closed
        assert owner._owner_conn is None and owner._client_read_conn is None
        assert not os.path.lexists(owner.socket_path), "a socket was bound"
        assert not os.path.lexists(owner.pid_path), "a pidfile was written"
        assert sorted(p.name for p in run_dir.iterdir()) == run_before
    finally:
        owner._release_maint_lock()  # the displaced stop left it held; free it for the next test


def test_D8b_a_fresh_owner_after_a_displaced_one_starts_clean(owner, home):
    """What D8 protected: displacement state does not leak into the NEXT owner. A new EngramdOwner on a
    fresh store (a new path: this process still holds the old, unlinked fds, and the fd scan keys on
    path, an artifact of in-process testing that a real exit never has) starts with _displaced False,
    serves a working snapshot, and closes both connections on stop()."""
    _displace(home, "-shm")
    result = _snap(owner, home / "d8.db")
    assert isinstance(result, ec.Rejected) and result.reason == "substrate_displaced"
    owner.stop()
    assert owner._displaced is True
    try:
        fresh_home = home / "fresh"
        fresh_home.mkdir()
        _seed_wal_store(fresh_home)
        fresh = eo.EngramdOwner(fresh_home, request_deadline=0.6)
        fresh.start(install_signal_handlers=False)
        try:
            assert fresh._displaced is False
            oc, cc = fresh._owner_conn, fresh._client_read_conn
            assert isinstance(_snap(fresh, fresh_home / "d8b.db"), ec.Ok)
        finally:
            fresh.stop()
        for conn in (oc, cc):
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("select 1")
    finally:
        owner._release_maint_lock()  # the displaced stop left it held


# -- D9 ----------------------------------------------------------------------


def test_D9_every_known_method_is_guarded_except_no_store_set(owner, home):
    assert eo.NO_STORE_METHODS == frozenset({"ping"})
    # A hypothetical future store method, registered WITHOUT any guard code.
    owner._methods["probe_store"] = lambda params, deadline: {"status": "ok", "result": {}}
    owner._method_kinds["probe_store"] = "read"  # #3822: an unclassified method is default-deny at dispatch
    _displace(home, "-shm")
    probe = ec.read("probe_store", {}, timeout=3.0, socket_path=owner.socket_path)
    assert isinstance(probe, ec.Rejected) and probe.reason == "substrate_displaced", probe
    assert isinstance(ec.read("ping", {}, timeout=3.0, socket_path=owner.socket_path), ec.Ok)
    unknown = ec.read("no_such_method", {}, timeout=3.0, socket_path=owner.socket_path)
    assert isinstance(unknown, ec.Rejected) and unknown.reason == "unknown_operation", unknown


# -- Item 3 pin (expected GREEN on old code) --------------------------------


class _FakeConnCred:
    def __init__(self, uid):
        self._raw = struct.pack(eo.PEERCRED_STRUCT_FORMAT, 4242, uid, 4242)

    def getsockopt(self, level, optname, buflen):
        return self._raw


def test_snapshot_dest_rights_premise_peer_uid_equals_owner(home):
    """Protects `_handle_snapshot`'s `os.access(parent, os.W_OK)` check,
    which tests the OWNER's rights and is correct only because the peer-
    credential gate admits exactly the owner's uid. If a cross-uid caller is
    ever admitted, that check must become a check against the CALLER's
    rights -- and this test must be revisited."""
    o = eo.EngramdOwner(home)
    own = os.getuid()
    assert o._check_peer_credentials(_FakeConnCred(own)) is None
    refused = o._check_peer_credentials(_FakeConnCred(own + 1))
    assert refused is not None and refused["reason"] == "peer_identity"
