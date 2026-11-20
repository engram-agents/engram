"""One helper decides the engramd owner's permission arm (#3708).

Before: the Darwin arm was chosen by `platform.system() == "Darwin"` at three
sites (the platform gate, the TCC check, `_harden_run_dir`) and by
`not hasattr(socket, "SO_PEERCRED")` at a fourth (the peer check), so a seat
where the two disagreed could take the Linux strip with the Darwin peer check
or the reverse. Now `_owner_arm()` -- keyed on `platform.system()` only -- is
used at all four, and the peer check REFUSES rather than fall through when its
arm's primitive is absent.

These cells simulate a platform by patching `platform.system` and the socket
attributes: they test the DISPATCH, not the primitive. The primitive stays
covered by the real-socket cells in test_engramd_owner_darwin_3666.py (Mac) and
by test_engramd_owner.py (Linux CI). No cell here carries a platform skip: each
runs on every seat, because each patches what it depends on.

Cell map: (a)(b)(c) peer-check dispatch; (d) one behavioural cell per site,
`_owner_arm` patched to the OTHER arm than `platform.system()` says -- the
discriminating control, no source grep; (e) main() exits cleanly on an
unsupported seat; (f) a failing Linux xattr strip refuses by name (#4929: was a hung
`setfacl -b`).
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as eo  # noqa: E402

_SENTINEL = {"status": "rejected", "reason": "sentinel"}


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="a38-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _owner(h: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(h, request_deadline=0.6)


def _system(monkeypatch, name: str) -> None:
    monkeypatch.setattr("platform.system", lambda: name)


def _arm(monkeypatch, arm) -> None:
    monkeypatch.setattr(eo, "_owner_arm", lambda: arm)


def _so_peercred(monkeypatch, present: bool) -> None:
    if present:
        monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    else:
        monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)


class _LinuxConn:
    """Answers SO_PEERCRED with this process's own uid."""

    def getsockopt(self, level, optname, buflen):
        return struct.pack(eo.PEERCRED_STRUCT_FORMAT, 1, os.getuid(), os.getgid())


def _spy_darwin_peer(monkeypatch, owner):
    calls = []

    def spy(conn):
        calls.append(conn)
        return _SENTINEL

    monkeypatch.setattr(owner, "_check_peer_credentials_darwin", spy)
    return calls


# ---------------------------------------------------------------------------
# (a)(b)(c) -- the peer check dispatches on the arm
# ---------------------------------------------------------------------------


def test_a_darwin_takes_the_darwin_peer_check_even_with_so_peercred_present(home, monkeypatch):
    _system(monkeypatch, "Darwin")
    _so_peercred(monkeypatch, True)
    owner = _owner(home)
    calls = _spy_darwin_peer(monkeypatch, owner)
    conn = _LinuxConn()
    assert owner._check_peer_credentials(conn) is _SENTINEL
    assert calls == [conn], "the Darwin check must run on a Darwin seat"


def test_b_linux_without_so_peercred_refuses_and_never_runs_the_darwin_check(home, monkeypatch):
    _system(monkeypatch, "Linux")
    _so_peercred(monkeypatch, False)
    owner = _owner(home)
    calls = _spy_darwin_peer(monkeypatch, owner)
    with pytest.raises(eo.OwnerPermissionUnsupported):
        owner._check_peer_credentials(_LinuxConn())
    assert calls == [], "Linux must never fall through to the Darwin check"


def test_c_an_unknown_platform_refuses_the_peer_check(home, monkeypatch):
    _system(monkeypatch, "FreeBSD")
    _so_peercred(monkeypatch, True)  # even with the primitive present
    owner = _owner(home)
    calls = _spy_darwin_peer(monkeypatch, owner)
    with pytest.raises(eo.OwnerPermissionUnsupported) as exc:
        owner._check_peer_credentials(_LinuxConn())
    assert "FreeBSD" in str(exc.value)
    assert calls == []


@pytest.mark.parametrize(
    "system,arm", [("Linux", "linux"), ("Darwin", "darwin"), ("FreeBSD", None), ("", None)]
)
def test_owner_arm_is_keyed_on_platform_system(monkeypatch, system, arm):
    _system(monkeypatch, system)
    assert eo._owner_arm() == arm


# ---------------------------------------------------------------------------
# (d) -- one behavioural cell per site: `_owner_arm` says the OTHER arm than
# `platform.system()`, and the site follows the helper. No source grep.
# ---------------------------------------------------------------------------


def test_d1_platform_gate_follows_the_helper(monkeypatch):
    _system(monkeypatch, "Darwin")
    _arm(monkeypatch, "linux")
    # #4929: the Linux checks are now os.getxattr / os.removexattr (then SO_PEERCRED),
    # not setfacl/getfacl on PATH. Delete the xattr functions so the cell fails the
    # same way on a Linux host (where they exist) and on a Mac (where they never do).
    monkeypatch.delattr(os, "getxattr", raising=False)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and "getxattr" in reason, (
        "system says Darwin but the helper says linux: the gate must run the Linux checks"
    )


def test_d2_tcc_check_follows_the_helper(home, monkeypatch):
    _system(monkeypatch, "Linux")
    _arm(monkeypatch, "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(eo.OwnerHomeTccProtected):
        _owner(home / "Documents" / "e")._check_darwin_home_not_tcc_protected()


def test_d3_harden_run_dir_follows_the_helper(home, monkeypatch):
    _system(monkeypatch, "Linux")
    _arm(monkeypatch, "darwin")
    owner = _owner(home)
    darwin_strips = []
    monkeypatch.setattr(
        owner, "_strip_run_dir_acl_darwin", lambda fd, run_dir: darwin_strips.append(run_dir)
    )

    def no_linux_strip(*a, **k):
        raise AssertionError("the Linux xattr strip ran although the helper says darwin")

    # #4929: the Linux strip is os.removexattr / os.getxattr (it no longer runs a
    # subprocess), so guard those; the subprocess guard stays for the Darwin-vs-Linux
    # mix-up the cell was written for.
    monkeypatch.setattr(os, "removexattr", no_linux_strip, raising=False)
    monkeypatch.setattr(os, "getxattr", no_linux_strip, raising=False)
    monkeypatch.setattr(subprocess, "run", no_linux_strip)
    owner._harden_run_dir()
    assert len(darwin_strips) == 1


def test_d4_peer_check_follows_the_helper(home, monkeypatch):
    _system(monkeypatch, "Darwin")
    _arm(monkeypatch, "linux")
    _so_peercred(monkeypatch, True)
    owner = _owner(home)
    calls = _spy_darwin_peer(monkeypatch, owner)
    assert owner._check_peer_credentials(_LinuxConn()) is None  # own uid, Linux parse
    assert calls == [], "system says Darwin but the helper says linux: no Darwin check"


# ---------------------------------------------------------------------------
# (e) -- main() on an unsupported seat: one stderr line, exit 1, no traceback
# ---------------------------------------------------------------------------


def test_e_main_exits_cleanly_when_the_seat_is_unsupported(home, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setattr(
        eo, "platform_unsupported_reason", lambda: "refusing to start: no such tool on this seat"
    )
    assert eo.main() == 1
    err = capsys.readouterr().err
    assert err.splitlines() == ["[engramd] refusing to start: no such tool on this seat"]
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# (f) -- a failing Linux strip refuses by name (#4929: was a hung `setfacl -b`)
# ---------------------------------------------------------------------------
#
# The #3708 pair pinned two properties of the SUBPROCESS strip: a hang refuses by name
# (a timeout), and stderr is decoded with errors="replace". #4929 removed the subprocess:
# `os.removexattr` on the fd cannot hang on a timer and has no stderr, so neither property
# has a referent any more. What survives is the one both guarded: a strip that fails
# refuses with a named OwnerPermissionUnsupported, never a bare traceback.


def test_f_a_failing_xattr_strip_refuses_with_a_named_reason(home, monkeypatch):
    # Drives the real Linux branch of _harden_run_dir on any seat: the arm helper says
    # linux and os.removexattr is a stand-in that fails (a Mac's os has none).
    _arm(monkeypatch, "linux")
    owner = _owner(home)

    def failing(fd, name, *a, **kw):
        raise OSError(5, "boom")  # EIO

    monkeypatch.setattr(os, "removexattr", failing, raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"", raising=False)
    with pytest.raises(eo.OwnerPermissionUnsupported) as exc:
        owner._harden_run_dir()
    msg = str(exc.value)
    assert msg.startswith("refusing to start: `os.removexattr` of system.posix_acl_access on ")
    assert "EIO" in msg
