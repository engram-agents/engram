"""Contract tests for the owner's xattr-based run/ ACL strip and read-back (#4929, part
of #2652) -- the Linux arm no longer shells out to `setfacl` / `getfacl`.

The contract (written before the implementation):

  X1  the strip is `os.removexattr(fd, name)` for `system.posix_acl_access` then
      `system.posix_acl_default`, on the SAME open fd of run/. ENODATA and ENOTSUP per
      name are fine; any other OSError refuses with OwnerPermissionUnsupported naming the
      errno, the xattr and run_dir.
  X2  the read-back is `os.getxattr(fd, name)` for both names through the same fd. ENODATA
      or ENOTSUP = that ACL is absent; any other OSError refuses with OwnerAclNotStripped
      ("could not be verified"). A present blob is decoded into getfacl-style entry strings
      so `_acl_leftovers` stays the single judge. An absent access blob contributes the
      base triple from the mode bits.
  X3  a removexattr that returns success proves nothing (measured: a second remove on a
      clean dir raises nothing), so the read-back is the check. Cell (c) is the
      discriminating one.
  X4  when both names raise ENOTSUP at read-back, ONE line is logged naming the no-ACL-
      filesystem branch.
  X5  the decoder refuses (OwnerAclNotStripped) on a short blob, a wrong version, a length
      that is not 4 + 8k, or an unknown tag; it never raises struct.error / IndexError.
  X6  `platform_unsupported_reason()` (Linux arm) no longer asks for `setfacl` / `getfacl`;
      it asks for `os.getxattr` and `os.removexattr` and still asks for SO_PEERCRED.

Two kinds of cell, split on purpose: the REAL-XATTR cells (a)(b)(real-*) need a Linux
kernel and an ACL-capable tmp filesystem and skip elsewhere; the STAND-IN cells install
`os.getxattr` / `os.removexattr` stand-ins and patch `eo._owner_arm` to "linux", so they run
on every platform (including a Mac, whose `os` has neither function).
"""

from __future__ import annotations

import errno
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as eo  # noqa: E402

REQUEST_DEADLINE = 0.6
ACCESS = "system.posix_acl_access"
DEFAULT = "system.posix_acl_default"
_NOBODY = 65534  # numeric uid; never resolved by name
_UNDEF = 0xFFFFFFFF
RWX, RX, NONE = 7, 5, 0

_linux_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and hasattr(os, "getxattr") and hasattr(os, "setxattr")),
    reason="Linux xattr cells, run on a Linux seat",
)


# -- blob + stand-in helpers ------------------------------------------------------


def _blob(entries, version: int = 2) -> bytes:
    """A posix_acl xattr blob: u32 LE version, then (tag, perm, id) u16/u16/u32 LE entries."""
    return struct.pack("<I", version) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)


def _base(user=RWX, group=RX, other=NONE):
    return [(1, user, _UNDEF), (4, group, _UNDEF), (32, other, _UNDEF)]


def _with_named_user(uid=_NOBODY, perm=RWX):
    return [(1, RWX, _UNDEF), (2, perm, uid), (4, RX, _UNDEF), (16, RWX, _UNDEF), (32, NONE, _UNDEF)]


@pytest.fixture
def tmp_base():
    d = tempfile.mkdtemp(prefix="x49-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _owner(home: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(home, request_deadline=REQUEST_DEADLINE)


class XattrStandIn:
    """Stand-ins for os.getxattr / os.removexattr. `blobs` maps xattr name -> bytes returned
    by getxattr (absent = ENODATA); `get_exc` / `rm_exc` map name -> OSError to raise.
    `rm_clears` makes a removexattr really drop the blob (a working strip); the default is
    the no-op that returns None, the shape of a strip that did nothing."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.get_exc: dict[str, OSError] = {}
        self.rm_exc: dict[str, OSError] = {}
        self.rm_clears = False
        self.calls: list[tuple[str, object, str]] = []

    def getxattr(self, target, name, *a, **k):
        self.calls.append(("get", target, name))
        if name in self.get_exc:
            raise self.get_exc[name]
        if name in self.blobs:
            return self.blobs[name]
        raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))

    def removexattr(self, target, name, *a, **k):
        self.calls.append(("rm", target, name))
        if name in self.rm_exc:
            raise self.rm_exc[name]
        if self.rm_clears:
            self.blobs.pop(name, None)
        return None

    def names(self, kind):
        return [n for (k, _t, n) in self.calls if k == kind]

    def targets(self):
        return [t for (_k, t, _n) in self.calls]


@pytest.fixture
def standin(monkeypatch):
    """Linux arm on any host: the arm helper says linux, SO_PEERCRED exists, os has the
    xattr functions (stand-ins)."""
    s = XattrStandIn()
    monkeypatch.setattr(eo, "_owner_arm", lambda: "linux")
    monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(eo.os, "getxattr", s.getxattr, raising=False)
    monkeypatch.setattr(eo.os, "removexattr", s.removexattr, raising=False)
    return s


def _enotsup():
    return OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))


# -- (a)(b) REAL xattr cells (Linux seat) ----------------------------------------


def _plant(path, name: str, blob: bytes) -> None:
    try:
        os.setxattr(str(path), name, blob)
    except OSError as exc:
        pytest.skip(f"fixture filesystem does not support ACLs: {exc}")


def _xattr_absent(path, name: str) -> bool:
    try:
        os.getxattr(str(path), name)
    except OSError as exc:
        return exc.errno in (errno.ENODATA, errno.ENOTSUP)
    return False


@_linux_only
def test_a_real_no_setfacl_no_getfacl_on_path_owner_hardens_run_dir(tmp_base, monkeypatch):
    """PATH holds neither tool (shutil.which -> None for both AND PATH is an empty dir, so a
    leftover subprocess call fails). RED on base: base refuses on the PATH check."""
    empty = tmp_base / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    real_which = shutil.which
    monkeypatch.setattr(
        eo.shutil, "which", lambda name, *a, **k: None if name in ("setfacl", "getfacl") else real_which(name, *a, **k)
    )
    assert eo.platform_unsupported_reason() is None
    home = tmp_base / "h"
    owner = _owner(home)
    owner._harden_run_dir()
    run = home / "run"
    assert _xattr_absent(run, ACCESS) and _xattr_absent(run, DEFAULT)


@_linux_only
def test_b_real_planted_access_and_default_acl_are_stripped(tmp_base):
    home = tmp_base / "h"
    run = home / "run"
    run.mkdir(parents=True)
    _plant(run, ACCESS, _blob(_with_named_user()))
    _plant(run, DEFAULT, _blob(_with_named_user()))
    assert not _xattr_absent(run, ACCESS) and not _xattr_absent(run, DEFAULT), "plant did not take"
    _owner(home)._harden_run_dir()
    assert _xattr_absent(run, ACCESS), "access ACL survived the strip"
    assert _xattr_absent(run, DEFAULT), "default ACL survived the strip"


# -- (c) THE discriminating stand-in cell -------------------------------------------


def test_c_noop_remove_with_a_named_user_still_present_is_refused(tmp_base, standin):
    """removexattr returns None and does nothing; getxattr still shows `user:65534:rwx`.
    Fails if the read-back is dropped or trusts the remove."""
    home = tmp_base / "h"
    standin.blobs[ACCESS] = _blob(_with_named_user())
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    msg = str(ei.value)
    assert f"user:{_NOBODY}:rwx" in msg
    assert str(home / "run") in msg
    # the read-back really ran after the remove
    assert standin.names("rm") == [ACCESS, DEFAULT]
    assert standin.names("get") == [ACCESS, DEFAULT]


def test_c2_a_strip_that_really_clears_is_accepted(tmp_base, standin):
    """The control for (c): the same blob, but the remove works -> start is allowed."""
    home = tmp_base / "h"
    standin.blobs[ACCESS] = _blob(_with_named_user())
    standin.blobs[DEFAULT] = _blob(_with_named_user())
    standin.rm_clears = True
    _owner(home)._harden_run_dir()  # must not raise


def test_c3_strip_and_readback_use_one_int_fd_that_is_the_run_dir(tmp_base, standin, monkeypatch):
    home = tmp_base / "h"
    seen = {}

    real_get = standin.getxattr

    def spy_get(target, name, *a, **k):
        seen.setdefault("ino", os.fstat(target).st_ino)  # target must be an int fd
        return real_get(target, name, *a, **k)

    monkeypatch.setattr(eo.os, "getxattr", spy_get, raising=False)
    _owner(home)._harden_run_dir()
    targets = standin.targets()
    assert len(targets) == 4 and all(isinstance(t, int) for t in targets), targets
    assert len(set(targets)) == 1, "strip and read-back must use the same fd"
    assert seen["ino"] == os.stat(home / "run").st_ino


@pytest.mark.parametrize("default_only", [False, True], ids=["access", "default"])
def test_c4_each_name_is_read_back(tmp_base, standin, default_only):
    """A leftover under EITHER name refuses (a read-back of only one name is not enough)."""
    home = tmp_base / "h"
    if default_only:
        standin.blobs[DEFAULT] = _blob(_base())
        needle = "default:user::"
    else:
        standin.blobs[ACCESS] = _blob(_with_named_user())
        needle = f"user:{_NOBODY}:"
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert needle in str(ei.value)


# -- (d) failures ---------------------------------------------------------------------


def test_d_getxattr_eio_refuses_could_not_be_verified(tmp_base, standin):
    home = tmp_base / "h"
    standin.get_exc[ACCESS] = OSError(errno.EIO, "boom")
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    msg = str(ei.value)
    assert "could not be verified" in msg
    assert "EIO" in msg and ACCESS in msg and str(home / "run") in msg


def test_d_getxattr_eio_on_the_default_name_also_refuses(tmp_base, standin):
    home = tmp_base / "h"
    standin.get_exc[DEFAULT] = OSError(errno.EIO, "boom")
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert "could not be verified" in str(ei.value) and DEFAULT in str(ei.value)


@pytest.mark.parametrize("name", [ACCESS, DEFAULT])
def test_d_removexattr_eio_refuses_naming_errno_and_xattr(tmp_base, standin, name):
    home = tmp_base / "h"
    standin.rm_exc[name] = OSError(errno.EIO, "boom")
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        _owner(home)._harden_run_dir()
    msg = str(ei.value)
    assert "EIO" in msg and name in msg and str(home / "run") in msg
    assert "via its open fd" in msg


@pytest.mark.parametrize("code", [errno.ENODATA, errno.ENOTSUP])
def test_d_removexattr_enodata_or_enotsup_is_not_an_error(tmp_base, standin, code):
    home = tmp_base / "h"
    standin.rm_exc[ACCESS] = OSError(code, "x")
    standin.rm_exc[DEFAULT] = OSError(code, "x")
    _owner(home)._harden_run_dir()  # read-back says clean (nothing planted)
    assert standin.names("rm") == [ACCESS, DEFAULT], "the default name must still be tried"


# -- (e) no-ACL filesystem --------------------------------------------------------------


def test_e_enotsup_everywhere_starts_and_logs_the_no_acl_branch_once(tmp_base, standin, caplog):
    import logging

    home = tmp_base / "h"
    for n in (ACCESS, DEFAULT):
        standin.get_exc[n] = _enotsup()
        standin.rm_exc[n] = _enotsup()
    with caplog.at_level(logging.DEBUG, logger="engramd_owner"):
        _owner(home)._harden_run_dir()  # must not raise
    hits = [r for r in caplog.records if "no POSIX ACL support" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    assert "mode bits" in hits[0].getMessage()


def test_e2_enodata_everywhere_is_clean_and_logs_no_no_acl_line(tmp_base, standin, caplog):
    import logging

    home = tmp_base / "h"
    with caplog.at_level(logging.DEBUG, logger="engramd_owner"):
        _owner(home)._harden_run_dir()
    assert not [r for r in caplog.records if "no POSIX ACL support" in r.getMessage()]


def test_e3_enotsup_on_only_one_name_does_not_claim_a_no_acl_filesystem(tmp_base, standin, caplog):
    import logging

    home = tmp_base / "h"
    standin.get_exc[ACCESS] = _enotsup()
    with caplog.at_level(logging.DEBUG, logger="engramd_owner"):
        _owner(home)._harden_run_dir()
    assert not [r for r in caplog.records if "no POSIX ACL support" in r.getMessage()]


# -- the read-back's base triple comes from the mode bits ------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        (0o700, ["user::rwx", "group::---", "other::---"]),
        (0o750, ["user::rwx", "group::r-x", "other::---"]),
        (0o641, ["user::rw-", "group::r--", "other::--x"]),
    ],
)
def test_absent_access_acl_reports_the_base_triple_from_the_mode(tmp_base, standin, mode, expected):
    d = tmp_base / "d"
    d.mkdir()
    os.chmod(d, mode)
    fd = os.open(str(d), os.O_DIRECTORY)
    try:
        entries = eo._read_acl_entries(fd, d)
    finally:
        os.close(fd)
    assert entries == expected
    assert eo._acl_leftovers(entries) == []


def test_present_access_blob_is_decoded_not_mixed_with_mode_bits(tmp_base, standin):
    d = tmp_base / "d"
    d.mkdir()
    standin.blobs[ACCESS] = _blob(_with_named_user())
    fd = os.open(str(d), os.O_DIRECTORY)
    try:
        entries = eo._read_acl_entries(fd, d)
    finally:
        os.close(fd)
    assert entries == ["user::rwx", f"user:{_NOBODY}:rwx", "group::r-x", "mask::rwx", "other::---"]


# -- (f) the decoder (pure) -------------------------------------------------------------


def test_f_base_only_blob_is_clean():
    entries = eo._decode_posix_acl_xattr(_blob(_base()), default=False)
    assert entries == ["user::rwx", "group::r-x", "other::---"]
    assert eo._acl_leftovers(entries) == []


@pytest.mark.parametrize(
    "extra,needle",
    [
        ((16, RWX, _UNDEF), "mask::rwx"),
        ((2, RWX, _NOBODY), f"user:{_NOBODY}:rwx"),
        ((8, RX, 1234), "group:1234:r-x"),
    ],
    ids=["mask", "named-user", "named-group"],
)
def test_f_each_extra_access_entry_is_a_leftover_naming_it(extra, needle):
    entries = eo._decode_posix_acl_xattr(_blob(_base() + [extra]), default=False)
    assert eo._acl_leftovers(entries) == [needle]


def test_f_any_default_entry_is_a_default_prefixed_leftover():
    entries = eo._decode_posix_acl_xattr(_blob(_base()), default=True)
    assert entries == ["default:user::rwx", "default:group::r-x", "default:other::---"]
    assert eo._acl_leftovers(entries) != []
    one = eo._decode_posix_acl_xattr(_blob([(32, RX, _UNDEF)]), default=True)
    assert one == ["default:other::r-x"]


@pytest.mark.parametrize(
    "blob,reason",
    [
        pytest.param(b"\x02\x00\x00\x00\x01", "short", id="truncated-5-bytes"),
        pytest.param(b"", "empty", id="empty"),
        pytest.param(b"\x02\x00", "tiny", id="2-bytes"),
        pytest.param(_blob(_base(), version=1), "version", id="wrong-version-1"),
        pytest.param(_blob(_base(), version=3), "version", id="wrong-version-3"),
        pytest.param(_blob(_base() + [(64, RWX, _UNDEF)]), "tag", id="unknown-tag-64"),
        pytest.param(_blob([(0, RWX, _UNDEF)]), "tag", id="unknown-tag-0"),
        pytest.param(_blob(_base())[:-3], "length", id="length-not-4+8k"),
    ],
)
@pytest.mark.parametrize("default", [False, True])
def test_f_malformed_blobs_refuse_with_owner_acl_not_stripped_only(blob, reason, default):
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        eo._decode_posix_acl_xattr(blob, default=default)
    assert "could not be verified" in str(ei.value)


def test_f_malformed_blob_through_the_read_back_refuses_with_the_run_dir(tmp_base, standin):
    home = tmp_base / "h"
    standin.blobs[ACCESS] = b"\x02\x00\x00\x00\x01"
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert str(home / "run") in str(ei.value)
    assert "could not be verified" in str(ei.value)


# -- platform cells (pure, any OS) -------------------------------------------------------


def _linux_platform(monkeypatch):
    monkeypatch.setattr(eo, "_owner_arm", lambda: "linux")
    monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(eo.os, "getxattr", lambda *a, **k: b"", raising=False)
    monkeypatch.setattr(eo.os, "removexattr", lambda *a, **k: None, raising=False)


def test_platform_reason_is_none_without_setfacl_and_getfacl_on_path(monkeypatch):
    """RED on base: base refuses when setfacl is not on PATH."""
    _linux_platform(monkeypatch)
    monkeypatch.setattr(eo.shutil, "which", lambda name, *a, **k: None)
    assert eo.platform_unsupported_reason() is None


@pytest.mark.parametrize("missing", ["getxattr", "removexattr"])
def test_platform_reason_names_a_missing_xattr_function(monkeypatch, missing):
    _linux_platform(monkeypatch)
    monkeypatch.delattr(eo.os, missing, raising=False)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and missing in reason
    assert "setfacl" not in reason and "getfacl" not in reason


def test_platform_reason_still_asks_for_so_peercred(monkeypatch):
    _linux_platform(monkeypatch)
    monkeypatch.delattr(eo.socket, "SO_PEERCRED", raising=False)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and "SO_PEERCRED" in reason
