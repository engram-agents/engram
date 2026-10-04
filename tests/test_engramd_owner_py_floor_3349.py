"""#3349 -- the engramd owner states and checks its Python 3.11 floor.

`Connection.setlimit` / `sqlite3.SQLITE_LIMIT_*` exist only from Python
3.11. The owner bounds client-read result size with them, so on an older
interpreter it must refuse to start, loudly and BEFORE any side effect --
never fall back to an unbounded read connection.

Contract (spec of #3349):
  1. A connection class lacking `setlimit` (or a sqlite3 lacking
     SQLITE_LIMIT_LENGTH) -> start() raises OwnerPythonTooOld whose
     message names "Python 3.11" and the running version; no socket file,
     no pidfile, no listener thread exist afterwards.
  2. On the real interpreter start() is unchanged (covered by the
     unmodified existing owner tests + the control below).
  3. Feature check (hasattr), not sys.version_info; injectable seam.
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as eo  # noqa: E402


@pytest.fixture
def engram_home():
    d = tempfile.mkdtemp(prefix="edo-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _NoSetlimitConn:
    """A Connection stand-in lacking setlimit (the 3.10 shape)."""


class _HasSetlimitConn:
    def setlimit(self, category, limit):  # pragma: no cover - never called
        return 0


def _fake_sqlite3(*, conn_cls, with_limit_const=True):
    # `connect` is the real one: a misplaced check must fail on the CONTRACT
    # (no OwnerPythonTooOld / a side effect exists), not on a missing attribute.
    ns = types.SimpleNamespace(Connection=conn_cls, connect=sqlite3.connect)
    if with_limit_const:
        ns.SQLITE_LIMIT_LENGTH = 0
    return ns


def test_helper_refuses_connection_class_without_setlimit():
    with pytest.raises(eo.OwnerPythonTooOld) as ei:
        eo._require_sqlite_setlimit(
            conn_cls=_NoSetlimitConn, module=_fake_sqlite3(conn_cls=_NoSetlimitConn)
        )
    msg = str(ei.value)
    assert "Python 3.11" in msg
    assert sys.version.split()[0] in msg


def test_helper_refuses_module_without_limit_constant():
    with pytest.raises(eo.OwnerPythonTooOld) as ei:
        eo._require_sqlite_setlimit(
            conn_cls=_HasSetlimitConn,
            module=_fake_sqlite3(conn_cls=_HasSetlimitConn, with_limit_const=False),
        )
    assert "Python 3.11" in str(ei.value)


def test_helper_accepts_feature_present():
    eo._require_sqlite_setlimit(
        conn_cls=_HasSetlimitConn, module=_fake_sqlite3(conn_cls=_HasSetlimitConn)
    )


def test_helper_is_feature_check_not_version_check(monkeypatch):
    """A fake interpreter version must not change the verdict either way."""
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    eo._require_sqlite_setlimit(
        conn_cls=_HasSetlimitConn, module=_fake_sqlite3(conn_cls=_HasSetlimitConn)
    )
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    with pytest.raises(eo.OwnerPythonTooOld):
        eo._require_sqlite_setlimit(
            conn_cls=_NoSetlimitConn, module=_fake_sqlite3(conn_cls=_NoSetlimitConn)
        )


def test_start_refuses_before_any_side_effect(engram_home, monkeypatch):
    """Cell 1: start() on a no-setlimit sqlite3 raises the named error and
    creates nothing at all -- not even the (not-yet-existing) home dir, its
    run/ dir, the store file, a socket, a pidfile or a listener thread."""
    monkeypatch.setattr(eo, "sqlite3", _fake_sqlite3(conn_cls=_NoSetlimitConn))
    fresh_home = engram_home / "not-yet-created"
    assert not fresh_home.exists()
    threads_before = set(threading.enumerate())
    owner = eo.EngramdOwner(fresh_home)
    with pytest.raises(eo.OwnerPythonTooOld) as ei:
        owner.start(install_signal_handlers=False)
    msg = str(ei.value)
    assert "Python 3.11" in msg
    assert sys.version.split()[0] in msg
    assert not fresh_home.exists()
    assert not owner.socket_path.exists()
    assert not owner.pid_path.exists()
    assert not owner.store_path.exists()
    assert owner._accept_thread is None
    assert set(threading.enumerate()) == threads_before


def test_start_refusal_leaves_stale_socket_untouched(engram_home, monkeypatch):
    """Cell 2: a stale (dead-owner) socket at the socket path is what
    _check_stale_socket_or_raise would unlink; the refusal must come first,
    so the file is still there afterwards."""
    owner = eo.EngramdOwner(engram_home)
    run_dir = owner.socket_path.parent
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(owner.socket_path))
    stale.close()  # the socket file stays behind, as after a crashed owner
    before = owner.socket_path.lstat()
    monkeypatch.setattr(eo, "sqlite3", _fake_sqlite3(conn_cls=_NoSetlimitConn))
    with pytest.raises(eo.OwnerPythonTooOld):
        owner.start(install_signal_handlers=False)
    after = owner.socket_path.lstat()
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert not owner.pid_path.exists()
    assert not owner.store_path.exists()


def test_start_proceeds_on_real_interpreter(engram_home):
    """Control: the real sqlite3 passes the check and start() works."""
    owner = eo.EngramdOwner(engram_home)
    owner.start(install_signal_handlers=False)
    try:
        assert owner.socket_path.exists()
        assert owner.pid_path.exists()
    finally:
        owner.stop()
