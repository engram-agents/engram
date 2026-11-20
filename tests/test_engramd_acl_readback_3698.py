"""Contract tests for the run/ ACL read-back (#3698, part of #2652) -- the
Linux half.

The contract (written before the implementation; ported to the xattr strip in #4929):

  A1  after the strip, _harden_run_dir() reads the ACL back through the SAME fd
      (`os.getxattr(fd, "system.posix_acl_access" / "system.posix_acl_default")`;
      #3698 had `getfacl -c -n /proc/self/fd/<fd>`), never the path, before the fd
      is closed and before the stale-socket check and the bind.
  A2  the only accepted entries are exactly one each of `user::`, `group::`,
      `other::`. Anything else (named user/group, `mask::`, any `default:` entry,
      an unrecognised line, a duplicated or missing base entry) is a leftover and
      refuses.
  A3  the refusal is a named RuntimeError, OwnerAclNotStripped, carrying the
      run_dir path and the leftover entries verbatim (listing capped at 10,
      with the count of the rest). A read-back failure (an OSError other than
      ENODATA/ENOTSUP, or an xattr blob that does not decode) also refuses, with its
      own reason. A missing `os.getxattr` is refused by _check_platform_support()
      like a missing `os.removexattr`. main() prints the message and returns 1.
  A4  healthy paths do not change: a real strip under a parent that carries an
      inheritable default ACL still starts and serves (Linux seat).
  A5  the read-back is the helper `_read_acl_entries(fd)`; the judge is
      `_acl_leftovers(entries)`.

A real strip never leaves an entry, so the refusal cells can only be shown by
making the strip a no-op (or by stubbing what the read-back sees); the docstrings
below say so where it applies.

#4929 port: the cells that stubbed `subprocess.run` for setfacl/getfacl now install
`os.getxattr` / `os.removexattr` stand-ins and patch `eo._owner_arm` to "linux", so
they run on every platform (a Mac's os has neither function). The cells that need a
real kernel ACL plant it with `os.setxattr` of a hand-built blob (no `setfacl`) and
skip with the reason "Linux xattr cells, run on a Linux seat" elsewhere. Two #3698
cells have no xattr referent and are dropped (named in the T5 and T4 sections).

Environment pinning: every test builds its own directory under a fresh
tempfile.mkdtemp() and passes it to EngramdOwner explicitly; the ambient
ENGRAM_HOME / HOME are never read (test_main_* sets ENGRAM_HOME itself).
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

from _owner_store import seed_current_store

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

REQUEST_DEADLINE = 0.6
_NOBODY = "65534"  # numeric uid; never resolved by name
ACCESS = "system.posix_acl_access"
DEFAULT = "system.posix_acl_default"
_UNDEF = 0xFFFFFFFF

_linux_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and hasattr(os, "getxattr") and hasattr(os, "setxattr")),
    reason="Linux xattr cells, run on a Linux seat",
)


@pytest.fixture
def tmp_base():
    d = tempfile.mkdtemp(prefix="acl-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _blob(entries, version: int = 2) -> bytes:
    """A posix_acl xattr blob: u32 LE version, then (tag, perm, id) u16/u16/u32 LE entries."""
    return struct.pack("<I", version) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)


_BASE = [(1, 7, _UNDEF), (4, 5, _UNDEF), (32, 5, _UNDEF)]


def _named_user(uid=int(_NOBODY)):
    return [(1, 7, _UNDEF), (2, 7, uid), (4, 5, _UNDEF), (16, 7, _UNDEF), (32, 5, _UNDEF)]


def _named_group(gid=int(_NOBODY)):
    return [(1, 7, _UNDEF), (4, 5, _UNDEF), (8, 5, gid), (16, 7, _UNDEF), (32, 5, _UNDEF)]


class XattrStandIn:
    """Stand-ins for os.getxattr / os.removexattr. `blobs`: name -> bytes getxattr returns
    (absent = ENODATA). `get_exc`: name -> OSError getxattr raises. `get_hook`, if set,
    answers getxattr instead (target, name). removexattr is a no-op returning None unless
    `rm_hook` is set (it is called after the no-op, with (target, name))."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.get_exc: dict[str, OSError] = {}
        self.get_hook = None
        self.rm_hook = None
        self.calls: list[tuple[str, object, str]] = []

    def getxattr(self, target, name, *a, **k):
        self.calls.append(("get", target, name))
        if self.get_hook is not None:
            return self.get_hook(target, name)
        if name in self.get_exc:
            raise self.get_exc[name]
        if name in self.blobs:
            return self.blobs[name]
        raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))

    def removexattr(self, target, name, *a, **k):
        self.calls.append(("rm", target, name))
        if self.rm_hook is not None:
            self.rm_hook(target, name)
        return None


@pytest.fixture
def standin(monkeypatch):
    """Linux arm on any host: arm helper says linux, SO_PEERCRED exists, os has the xattr
    functions (stand-ins). The strip is a no-op that returns None by default."""
    s = XattrStandIn()
    monkeypatch.setattr(eo, "_owner_arm", lambda: "linux")
    monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(eo.os, "getxattr", s.getxattr, raising=False)
    monkeypatch.setattr(eo.os, "removexattr", s.removexattr, raising=False)
    return s


def _plant(path, name: str, blob: bytes) -> None:
    """Plant an ACL xattr. Only 'this filesystem has no ACLs' skips; any other error (EINVAL from a
    malformed blob, a permission error) is the fixture's fault and FAILS, never a quiet skip."""
    try:
        os.setxattr(str(path), name, blob)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            pytest.skip(f"fixture filesystem does not support ACLs: {exc}")
        pytest.fail(f"planting {name} on {path} failed (the fixture's blob or setup, not the filesystem): {exc}")


def _skip_if_socket_path_too_long(home: Path) -> None:
    """start()/main() refuse an over-long socket path first (#3696). With a very long TMPDIR
    that is the tmp base's fault, not this file's subject, so skip rather than fail."""
    n = len(os.fsencode(home / "run" / "engramd.sock"))
    if n > ec.SOCKET_PATH_MAX_BYTES:
        pytest.skip(f"tmp base too long for start(): socket path is {n} bytes, limit {ec.SOCKET_PATH_MAX_BYTES}")


def _owner(home: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(home, request_deadline=REQUEST_DEADLINE)


def _home_with_run(base: Path) -> tuple[Path, Path]:
    home = base / "h"
    run = home / "run"
    run.mkdir(parents=True)
    seed_current_store(home)  # #3753: start()/main() callers pass the schema gate first
    return home, run


# What the stand-in shows for each leftover shape, and the entry the refusal must name.
_LEFTOVER_CASES = [
    pytest.param(ACCESS, _named_user(), f"user:{_NOBODY}:", id="named-user"),
    # A named entry needs a mask entry (and the kernel's entry order) or setxattr refuses it with EINVAL.
    pytest.param(DEFAULT, _named_user(), f"default:user:{_NOBODY}:", id="default-only"),
    pytest.param(ACCESS, _named_group(), f"group:{_NOBODY}:", id="named-group"),
]


# -- A5: the judge (pure) -------------------------------------------------------


def test_judge_accepts_exactly_the_base_triple_any_bits():
    assert eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x"]) == []
    assert eo._acl_leftovers(["user::---", "group::---", "other::---"]) == []


@pytest.mark.parametrize(
    "extra",
    [
        "user:65534:rwx",
        "group:65534:r-x",
        "mask::rwx",
        "default:user::rwx",
        "default:user:65534:rwx",
        "default:mask::rwx",
        "bogus line",
    ],
)
def test_judge_flags_each_leftover_shape(extra):
    left = eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x", extra])
    assert left == [extra]


def test_judge_mask_only_line_is_a_leftover():
    # a read-back whose only extra is mask:: -- must refuse
    assert eo._acl_leftovers(["user::rwx", "mask::rwx", "group::r-x", "other::r-x"]) == [
        "mask::rwx"
    ]


def test_judge_missing_and_duplicate_base_entries_are_leftovers():
    assert eo._acl_leftovers(["user::rwx", "group::r-x"]) != []
    assert eo._acl_leftovers(["user::rwx", "user::rwx", "group::r-x", "other::r-x"]) != []
    assert eo._acl_leftovers([]) != []


def test_judge_a_named_entry_is_never_mistaken_for_the_base_entry():
    # Real ACLs list the base triple next to every named entry, so the duplicate-count check
    # would catch a named entry even if the base pattern wrongly accepted it. The pattern is
    # still pinned on its own: a named entry with no base line is a leftover AND leaves the
    # base entry missing.
    left = eo._acl_leftovers(["user:65534:rwx", "group::r-x", "other::r-x"])
    assert "user:65534:rwx" in left
    assert any("missing base entry user" in x for x in left)


def test_judge_single_leftover_is_not_forgiven():
    # off-by-one guard: ONE stray entry must be reported, not only two or more
    left = eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x", "default:other::r-x"])
    assert len(left) == 1


# -- T1: no-op strip over an entry -> named refusal -------------------------------


@pytest.mark.parametrize("xname,entries,needle", _LEFTOVER_CASES)
def test_noop_strip_over_an_entry_is_refused_by_name(tmp_base, standin, xname, entries, needle):
    """MUTATION-SHAPED BY NECESSITY: a real strip never leaves an entry, so the leftover can
    only be produced by making the strip a no-op that returns success. Drives start() so the
    socket-never-bound half is pinned too."""
    home, run = _home_with_run(tmp_base)
    _skip_if_socket_path_too_long(home)
    standin.blobs[xname] = _blob(entries)
    owner = _owner(home)
    try:
        with pytest.raises(eo.OwnerAclNotStripped) as ei:
            owner.start(install_signal_handlers=False)
        bound = owner.socket_path.exists()
    finally:
        owner.stop()  # a pre-fix start() succeeds; never leave its threads running
    msg = str(ei.value)
    assert isinstance(ei.value, RuntimeError)
    assert str(run) in msg
    assert needle in msg
    assert not bound, "socket was bound despite the refusal"


@_linux_only
@pytest.mark.parametrize("xname,entries,needle", _LEFTOVER_CASES)
def test_real_noop_strip_over_a_planted_entry_is_refused_by_name(tmp_base, monkeypatch, xname, entries, needle):
    """The same refusal against a REAL kernel ACL: plant with os.setxattr, make the strip a
    no-op (real getxattr stays)."""
    home, run = _home_with_run(tmp_base)
    _skip_if_socket_path_too_long(home)
    _plant(run, xname, _blob(entries))
    monkeypatch.setattr(eo.os, "removexattr", lambda *a, **k: None)
    owner = _owner(home)
    try:
        with pytest.raises(eo.OwnerAclNotStripped) as ei:
            owner.start(install_signal_handlers=False)
        bound = owner.socket_path.exists()
    finally:
        owner.stop()
    assert str(run) in str(ei.value) and needle in str(ei.value)
    assert not bound, "socket was bound despite the refusal"


def test_refusal_lists_at_most_ten_leftovers_and_counts_the_rest(tmp_base, standin):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    extra = [(2, 7, 1000 + i) for i in range(14)]
    standin.blobs[ACCESS] = _blob(_BASE[:1] + extra + _BASE[1:])
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    msg = str(ei.value)
    shown = [f"user:{1000 + i}:rwx" for i in range(14)]
    for e in shown[:10]:
        assert e in msg
    for e in shown[10:]:
        assert e not in msg
    assert "4 more" in msg


@pytest.mark.parametrize(
    "xname,entry,needle",
    [
        pytest.param(DEFAULT, [(32, 5, _UNDEF)], "default:other::r-x", id="default"),
        pytest.param(ACCESS, _BASE + [(16, 7, _UNDEF)], "mask::rwx", id="mask"),
        pytest.param(ACCESS, _BASE + [(2, 7, 1000)], "user:1000:rwx", id="named-user"),
    ],
)
def test_exactly_one_leftover_entry_still_refuses(tmp_base, standin, xname, entry, needle):
    """Off-by-one guard at the refusal site: a SINGLE stray entry refuses."""
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    standin.blobs[xname] = _blob(entry)
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert needle in str(ei.value)
    assert "more)" not in str(ei.value)


# -- T2: healthy path, parent with an inheritable default ACL -------------------


def test_noop_strip_on_a_freshly_created_run_dir_that_inherited_entries_is_refused(tmp_base, standin):
    """The #3698 scenario itself: run/ does not exist, so it is created fresh and (on a real
    seat) INHERITS its parent's default ACL; with the strip a no-op the entries remain and
    must be caught. Every other refusal cell pre-creates run/, which would not notice a check
    skipped on fresh dirs."""
    home = tmp_base / "h"
    home.mkdir()
    standin.blobs[ACCESS] = _blob(_named_user())
    assert not (home / "run").exists()
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert (home / "run").is_dir()
    assert f"user:{_NOBODY}:" in str(ei.value)


@_linux_only
def test_real_noop_strip_on_a_fresh_run_dir_under_a_default_acl_parent_is_refused(tmp_base, monkeypatch):
    """The same, against the kernel: a default ACL on the parent is inherited by run/."""
    home = tmp_base / "h"
    home.mkdir()
    _plant(home, DEFAULT, _blob(_named_user()))
    probe = home / "probe"
    probe.mkdir()
    try:
        os.getxattr(str(probe), ACCESS)
    except OSError:
        pytest.skip("parent default ACL is not inherited on this filesystem")
    monkeypatch.setattr(eo.os, "removexattr", lambda *a, **k: None)
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert f"user:{_NOBODY}:" in str(ei.value)


def _open_fd_count() -> int:
    return len(os.listdir("/dev/fd"))  # /proc/self/fd on Linux, the fd table on a Mac


@pytest.mark.parametrize(
    "setup,exc_type",
    [
        pytest.param(lambda s: s.get_exc.update({ACCESS: OSError(errno.EIO, "boom")}), eo.OwnerAclNotStripped, id="getxattr-eio"),
        pytest.param(lambda s: s.get_exc.update({DEFAULT: OSError(errno.EACCES, "no")}), eo.OwnerAclNotStripped, id="getxattr-eacces"),
        pytest.param(lambda s: s.blobs.update({ACCESS: b"\x02\x00\x00\x00\x01"}), eo.OwnerAclNotStripped, id="malformed-blob"),
        pytest.param(lambda s: s.blobs.update({ACCESS: _blob(_BASE, version=1)}), eo.OwnerAclNotStripped, id="wrong-version"),
        pytest.param(lambda s: s.blobs.update({ACCESS: _blob(_BASE + [(16, 7, _UNDEF)])}), eo.OwnerAclNotStripped, id="leftover"),
        pytest.param(lambda s: setattr(s, "rm_hook", _raise_eio), eo.OwnerPermissionUnsupported, id="removexattr-eio"),
    ],
)
def test_every_refusal_closes_the_run_dir_fd(tmp_base, standin, setup, exc_type):
    """A refused start must not leak the run/ fd the strip and the read-back used."""
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    setup(standin)
    before = _open_fd_count()
    with pytest.raises(exc_type):
        _owner(home)._harden_run_dir()
    assert _open_fd_count() == before, "the run/ directory fd was not closed after a refusal"


def _raise_eio(target, name):
    raise OSError(errno.EIO, "boom")


@_linux_only
def test_healthy_real_strip_under_default_acl_parent_starts_and_serves(tmp_base):
    home = tmp_base / "h"
    home.mkdir()
    _skip_if_socket_path_too_long(home)
    seed_current_store(home)  # #3753
    _plant(home, DEFAULT, _blob(_named_user()))
    probe = home / "probe"
    probe.mkdir()
    try:
        os.getxattr(str(probe), DEFAULT)
    except OSError:
        pytest.skip("parent default ACL is not inherited on this filesystem")
    owner = _owner(home)
    owner.start(install_signal_handlers=False)
    try:
        run = home / "run"
        for name in (ACCESS, DEFAULT):
            with pytest.raises(OSError) as ei:
                os.getxattr(str(run), name)
            assert ei.value.errno in (errno.ENODATA, errno.ENOTSUP)
        assert isinstance(ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path), ec.Ok)
    finally:
        owner.stop()


# -- T3: swap control -----------------------------------------------------------


def _ino_hook(standin, original_ino: int, decoy_blob: bytes):
    """getxattr answers by WHICH directory the target is: the original run/ is clean
    (ENODATA), anything else carries `decoy_blob`. An int target is an fd (resolved through
    fstat); a str/PathLike target is a path (resolved through stat) -- so a read-back by path
    lands on the decoy and a read-back by fd lands on the original."""

    def hook(target, name):
        ino = os.fstat(target).st_ino if isinstance(target, int) else os.stat(target).st_ino
        if ino == original_ino or name != ACCESS:
            raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))
        return decoy_blob

    standin.get_hook = hook


def test_readback_judges_the_fds_directory_not_the_path(tmp_base, standin):
    run = tmp_base / "run"
    run.mkdir()
    fd = os.open(str(run), os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        original_ino = os.fstat(fd).st_ino
        run.rename(tmp_base / "run.moved")
        run.mkdir()
        _ino_hook(standin, original_ino, _blob(_named_user()))
        # the decoy at the path carries an entry (read by PATH it is a leftover) ...
        assert eo._acl_leftovers(eo._read_acl_entries(str(run))) != [], "decoy carries no entry"
        # ... but the fd still points at the original, clean directory
        assert eo._acl_leftovers(eo._read_acl_entries(fd)) == []
    finally:
        os.close(fd)


@_linux_only
def test_real_readback_judges_the_fds_directory_not_the_path(tmp_base):
    run = tmp_base / "run"
    run.mkdir()
    fd = os.open(str(run), os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        run.rename(tmp_base / "run.moved")
        run.mkdir()
        _plant(run, ACCESS, _blob(_named_user()))
        assert eo._acl_leftovers(eo._read_acl_entries(str(run))) != [], "decoy carries no entry"
        assert eo._acl_leftovers(eo._read_acl_entries(fd)) == []
    finally:
        os.close(fd)


def test_swap_between_strip_and_readback_inside_harden_is_judged_by_fd(tmp_base, standin):
    """Inside _harden_run_dir(): right after the strip returns, swap run/ for a decoy
    carrying an entry. Reading by fd judges the stripped original (passes); reading by path
    would judge the decoy (would refuse)."""
    home = tmp_base / "h"
    run = home / "run"
    run.mkdir(parents=True)
    original_ino = os.stat(run).st_ino
    _ino_hook(standin, original_ino, _blob(_named_user()))
    swapped = []

    def swap_after_strip(target, name):
        if name == DEFAULT and not swapped:  # after the last strip call
            run.rename(home / "run.moved")
            run.mkdir()
            swapped.append(True)

    standin.rm_hook = swap_after_strip
    _owner(home)._harden_run_dir()  # must not raise
    assert swapped, "the swap never ran"


# -- T4: read-back failures refuse ----------------------------------------------


@pytest.mark.parametrize(
    "kw,needle",
    [
        pytest.param({"exc": OSError(errno.EIO, "boom")}, "EIO", id="eio"),
        pytest.param({"exc": OSError(errno.EACCES, "no")}, "EACCES", id="eacces"),
        pytest.param({"exc": OSError(errno.ERANGE, "big")}, "ERANGE", id="erange"),
        pytest.param({"blob": b"\x00\x01 not an acl"}, "malformed", id="garbage"),
        pytest.param({"blob": b""}, "malformed", id="empty"),
    ],
)
def test_readback_failure_refuses_with_its_own_reason(tmp_base, standin, kw, needle):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    if "exc" in kw:
        standin.get_exc[ACCESS] = kw["exc"]
    else:
        standin.blobs[ACCESS] = kw["blob"]
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert needle in str(ei.value)
    assert "could not be verified" in str(ei.value)
    assert str(home / "run") in str(ei.value)


def test_getxattr_is_called_with_the_open_fd_not_a_path(tmp_base, standin):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    _owner(home)._harden_run_dir()
    gets = [(t, n) for (k, t, n) in standin.calls if k == "get"]
    assert [n for _t, n in gets] == [ACCESS, DEFAULT]
    assert all(isinstance(t, int) for t, _n in gets), "read-back must use the fd, never a path"


# -- T5: no stderr in the xattr read-back ---------------------------------------
#
# DROPPED (#4929): `test_stderr_warning_with_clean_stdout_does_not_refuse`. It pinned that a
# `getfacl` stderr warning ("Removing leading '/' from absolute path names") is not a failure
# signal. `os.getxattr` has no stderr and no exit code; its only failure signal is an OSError,
# which T4 pins. The property has no referent in the xattr read-back.


# -- T6: a missing xattr function is a platform refusal -------------------------


@pytest.mark.parametrize("missing", ["getxattr", "removexattr"])
def test_platform_reason_names_a_missing_xattr_function(monkeypatch, standin, missing):
    monkeypatch.delattr(eo.os, missing, raising=False)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and missing in reason


@pytest.mark.parametrize("missing", ["getxattr", "removexattr"])
def test_start_refuses_a_missing_xattr_function_before_creating_anything(tmp_base, monkeypatch, standin, missing):
    monkeypatch.delattr(eo.os, missing, raising=False)
    home = tmp_base / "fresh"
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        _owner(home).start(install_signal_handlers=False)
    assert missing in str(ei.value)
    assert not home.exists()


# -- T7: main() ------------------------------------------------------------------


def test_main_exits_nonzero_with_message_on_leftover_acl(tmp_base, monkeypatch, standin, capsys):
    home, run = _home_with_run(tmp_base)
    _skip_if_socket_path_too_long(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    standin.blobs[ACCESS] = _blob(_named_user())

    def _no_serve(self):  # a pre-fix start() succeeds; never block forever
        self.stop()
        raise AssertionError("owner started despite the leftover ACL")

    monkeypatch.setattr(eo.EngramdOwner, "wait_forever", _no_serve)
    assert eo.main() == 1
    err = capsys.readouterr().err
    assert err.startswith("[engramd] ")
    assert str(run) in err and f"user:{_NOBODY}:" in err
