"""`--preflight` refuses a non-regular STORE path (#3785, part of #3753).

launchd has no per-exit-code restart exclusion, so an owner that starts and exits 4 on a planted
<ENGRAM_HOME>/knowledge.db (FIFO, directory, socket) would respawn every throttle interval
forever. The installer's `--preflight` must therefore refuse it up front, from the SAME predicate
`_connect_store_ro` uses (one helper, `_refuse_non_regular_store`), exiting 4 like `start()` does.

Real-process cells: each runs `engramd_owner.py --preflight` under a kill timeout and never opens
the planted FIFO.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_store import seed_current_store  # stamp discipline: the regular-store cell is stamped

_SRC_ENGRAM = Path(__file__).resolve().parent.parent / "src" / "engram"
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))

import engramd_owner as eo  # noqa: E402

OWNER_PY = _SRC_ENGRAM / "engramd_owner.py"
TIMEOUT = 15.0
HELPER = "_refuse_non_regular_store"


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="p85-")  # short: AF_UNIX path limits
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _preflight(home: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM_")}
    env.update({"ENGRAM_HOME": str(home), "ENGRAM_NO_EMBEDDINGS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        r = subprocess.run([sys.executable, str(OWNER_PY), "--preflight"], capture_output=True,
                           text=True, timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail(f"--preflight did not return within {TIMEOUT}s (it blocked on the store path)")
    return r.returncode, r.stdout, r.stderr


def _store(home: Path) -> Path:
    return home / "knowledge.db"


def _plant_fifo(p: Path):
    os.mkfifo(str(p))


def _plant_dir(p: Path):
    p.mkdir()


_socks = []


def _plant_socket(p: Path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(p))
    _socks.append(s)


def _plant_symlink_to_fifo(p: Path):
    target = p.parent / "elsewhere.fifo"
    os.mkfifo(str(target))
    os.symlink(str(target), str(p))


@pytest.fixture(autouse=True)
def _close_socks():
    yield
    while _socks:
        _socks.pop().close()


@pytest.mark.parametrize("plant,kind", [
    (_plant_fifo, "fifo"),
    (_plant_dir, "directory"),
    (_plant_socket, "socket"),
    (_plant_symlink_to_fifo, "fifo"),
], ids=["fifo", "directory", "socket", "symlink-to-fifo"])
def test_preflight_refuses_a_non_regular_store_with_exit_4(home, plant, kind):
    plant(_store(home))
    rc, out, err = _preflight(home)
    assert rc == eo.EXIT_PATH_NOT_REGULAR == 4, (rc, out, err)
    assert out == "" and "not a regular file" in err and kind in err, (out, err)
    assert len(err.splitlines()) == 1, err


def test_preflight_passes_for_an_absent_store(home):
    assert _preflight(home) == (0, "", "")


def test_preflight_passes_for_a_regular_store(home):
    seed_current_store(home)
    assert _preflight(home) == (0, "", "")


def test_preflight_passes_for_a_symlink_to_a_regular_store(home):
    real = home / "real.db"
    sqlite3.connect(str(real)).close()
    os.symlink(str(real), str(_store(home)))
    assert _preflight(home) == (0, "", "")


def test_absent_pidfile_falls_through_to_the_store_check(home):
    """The pidfile check used to `return` on FileNotFoundError, which skipped any later check."""
    _plant_fifo(_store(home))
    assert not (home / eo.DEFAULT_PID_NAME).exists()
    rc, _out, err = _preflight(home)
    assert rc == 4 and "fifo" in err, (rc, err)


def test_regular_pidfile_then_store_check_still_runs(home):
    (home / eo.DEFAULT_PID_NAME).write_text("1\n")
    _plant_fifo(_store(home))
    rc, _out, err = _preflight(home)
    assert rc == 4 and "fifo" in err, (rc, err)


def test_connect_store_ro_and_check_start_refusals_share_one_helper(home, monkeypatch):
    calls = []
    monkeypatch.setattr(eo, HELPER, lambda p: calls.append(("helper", str(p))))
    with pytest.raises(sqlite3.OperationalError):  # absent store: sqlite refuses after the helper
        eo._connect_store_ro(_store(home))
    assert calls == [("helper", str(_store(home)))], calls

    calls.clear()
    owner = eo.EngramdOwner(home)
    monkeypatch.setattr(owner, "_check_platform_support", lambda: None)
    monkeypatch.setattr(owner, "_check_darwin_home_not_tcc_protected", lambda: None)
    owner.check_start_refusals()
    assert len(calls) == 1 and calls[0][0] == "helper", calls
    assert Path(calls[0][1]) == Path(owner.store_path), calls
