"""`main()` maps every owner refusal to ONE exit code, whichever entry point raised it
(#3785, part of #3753).

Borges's ruling on PR #3776: `--preflight` and `start()` used to carry two hand-written
exception -> exit-code tables, and they disagreed (`--preflight` gave OwnerPidfileNotRegular 1,
`start()` gave 4). Both branches now look up one module-level table, `_REFUSAL_EXIT`.

Contract:
 1. For every refusal `check_start_refusals()` can raise, `main(["--preflight"])` and `main([])`
    return the SAME code on the same condition.
 2. Every start() code is pinned (the refactor is behaviour-neutral for start()).
 3. Every refusal class the module defines has a row in the table, or is named below as one
    `main()` deliberately does not map (it propagates).
 4. An exception not in the table is not swallowed.

Cells 1/2/4 run `main()` in-process with the check / start patched to raise the class, so no real
owner starts (portable: Mac and Linux). The real-process cells plant a FIFO (portable) and run the
owner script under a kill timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_store import seed_current_store  # stamp discipline: the real-process cells' store

_SRC_ENGRAM = Path(__file__).resolve().parent.parent / "src" / "engram"
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))

import engramd_owner as eo  # noqa: E402

OWNER_PY = _SRC_ENGRAM / "engramd_owner.py"
TIMEOUT = 15.0

# start()'s codes as of the pre-refactor main(), spelled out so the table cannot drift unseen.
START_CODES = {
    "OwnerAlreadyRunning": 1,
    "OwnerSocketPathTooLong": 1,
    "OwnerHomeTccProtected": 1,
    "OwnerAclNotStripped": 1,
    "OwnerPermissionUnsupported": 1,
    "OwnerPidfileNotRegular": 4,
    "OwnerStorePathNotRegular": 4,
    "OwnerStoreBehindSchema": 5,
    "OwnerStoreMaintenance": 6,  # #3882: the store-maintenance lock is held by a repair tool
    "OwnerStoreLockDirUnusable": 4,  # #3882: the lock directory is a planted path; not retried
}
# What check_start_refusals() (so --preflight) can raise: read off the method and the two
# helpers it calls (_check_platform_support, _check_darwin_home_not_tcc_protected).
PREFLIGHT_RAISES = [
    "OwnerSocketPathTooLong",
    "OwnerHomeTccProtected",
    "OwnerPermissionUnsupported",
    "OwnerPidfileNotRegular",
    "OwnerStorePathNotRegular",
]
# Owner* RuntimeErrors that are NOT start() refusals main() maps: they propagate today.
# OwnerRestartRefused (#4068) is a caller error (a second start() on one object), deliberately unmapped.
UNMAPPED = {"OwnerStalePathUnsafe", "OwnerPeerCredentialsMalformed", "OwnerPythonTooOld", "OwnerRestartRefused"}


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="p86-")  # short: AF_UNIX path limits
    monkeypatch.setenv("ENGRAM_HOME", d)
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _raise(cls):
    def f(*_a, **_k):
        raise cls(f"boom-{cls.__name__}")
    return f


def _run_main(monkeypatch, capsys, argv, cls):
    monkeypatch.setattr(eo.EngramdOwner, "check_start_refusals", _raise(cls))
    monkeypatch.setattr(eo.EngramdOwner, "start", _raise(cls))
    rc = eo.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.mark.parametrize("name", PREFLIGHT_RAISES)
def test_preflight_and_start_return_the_same_code_for_the_same_refusal(name, home, monkeypatch, capsys):
    cls = getattr(eo, name)
    pre = _run_main(monkeypatch, capsys, ["--preflight"], cls)
    start = _run_main(monkeypatch, capsys, [], cls)
    assert pre == start == (START_CODES[name], "", f"[engramd] boom-{name}\n"), (pre, start)


@pytest.mark.parametrize("name", sorted(START_CODES))
def test_start_code_is_pinned(name, home, monkeypatch, capsys):
    rc, out, err = _run_main(monkeypatch, capsys, [], getattr(eo, name))
    assert (rc, out, err) == (START_CODES[name], "", f"[engramd] boom-{name}\n")


def test_exit_constants_are_the_values_the_pins_assume():
    assert (eo.EXIT_PATH_NOT_REGULAR, eo.EXIT_STORE_BEHIND_SCHEMA, eo.EXIT_STORE_MAINTENANCE) == (4, 5, 6)


def _table():
    return {cls.__name__: code for cls, code in eo._REFUSAL_EXIT}


def test_table_has_exactly_the_start_codes():
    assert _table() == START_CODES


def test_every_refusal_class_in_the_module_has_a_table_row():
    """Discovery: every Owner* RuntimeError the module defines is either in the table or named in
    UNMAPPED above. A new refusal class that is neither fails here, so the author must decide."""
    defined = {n for n, v in vars(eo).items()
               if n.startswith("Owner") and isinstance(v, type) and issubclass(v, RuntimeError)}
    assert defined - UNMAPPED == set(_table()), (defined, UNMAPPED, set(_table()))
    assert UNMAPPED <= defined, "UNMAPPED names a class the module no longer defines"


def test_refusal_exit_code_helper():
    for name, code in START_CODES.items():
        assert eo._refusal_exit_code(getattr(eo, name)("x")) == code
    assert eo._refusal_exit_code(RuntimeError("x")) is None
    assert eo._refusal_exit_code(eo.OwnerPythonTooOld("x")) is None


@pytest.mark.parametrize("argv", [["--preflight"], []], ids=["preflight", "start"])
def test_an_exception_not_in_the_table_is_not_swallowed(argv, home, monkeypatch, capsys):
    with pytest.raises(eo.OwnerPythonTooOld):
        _run_main(monkeypatch, capsys, argv, eo.OwnerPythonTooOld)


# ---- real-process cells: a planted FIFO, the same code from --preflight and from the owner ----

def _real(argv, home: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM_")}
    env.update({"ENGRAM_HOME": str(home), "ENGRAM_NO_EMBEDDINGS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        r = subprocess.run([sys.executable, str(OWNER_PY), *argv], capture_output=True, text=True,
                           timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{argv} did not return within {TIMEOUT}s")
    return r.returncode, r.stderr


@pytest.mark.parametrize("planted", ["pidfile", "store"])
def test_real_planted_fifo_gives_the_same_code_from_preflight_and_start(planted, home):
    if planted == "pidfile":
        seed_current_store(home)  # a current store, so the store-schema gate is not what refuses
        os.mkfifo(str(home / eo.DEFAULT_PID_NAME))
    else:
        os.mkfifo(str(home / "knowledge.db"))
    pre_rc, pre_err = _real(["--preflight"], home)
    start_rc, start_err = _real([], home)
    assert pre_rc == start_rc == eo.EXIT_PATH_NOT_REGULAR == 4, (pre_rc, pre_err, start_rc, start_err)
    assert "fifo" in pre_err.lower() and "fifo" in start_err.lower(), (pre_err, start_err)
