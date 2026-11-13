"""#4641: the shared atomic writer must carry named ACLs and user.* xattrs across the replace.

``atomic_write_text`` publishes with ``os.replace``, which installs a NEW inode: the
replaced file's named POSIX ACL entries and ``user.*`` extended attributes do not
follow, so both are silently lost (the #4013 mode/group work stopped at the two
attributes ``fchmod``/``fchown`` can restore).

Contract (written before the implementation; the cells below are the design):

  A1  A named POSIX ACL entry on the target survives a write byte-for-byte.
  A2  A ``user.*`` xattr on the target survives a write byte-for-byte.
  A3  Control: the pre-existing mode/group preservation still holds (a fix that
      breaks this is worse than the bug).
  A4  A target with no ACL is written normally: ENODATA is "no ACL", not an error.
  A5  If the source HAS an ACL and applying it to the temp fails, the write RAISES,
      the target is byte-identical, and no temp file is left behind. Silently
      dropping it is the defect being fixed.

The whole file skips when this filesystem cannot carry ``system.posix_acl_access``
unprivileged; the skip reason names the errno and the platform, so a Linux-with-ACL
run and a macOS run are distinguishable in the report.
"""

import errno
import importlib.util
import os
import stat
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "engram"
ACL = "system.posix_acl_access"
_NONE = 0xFFFFFFFF


def _minimal_named_acl(uid):
    """A valid ``system.posix_acl_access`` blob with one NAMED user entry.

    Format (linux/posix_acl_xattr.h): u32 version, then (u16 tag, u16 perm, u32 id)
    entries.  The named entry is ``user:<uid>:r--``; the mask is ``r--``, so the
    kernel rewrites the group mode bits to 4 when the blob is installed.
    """
    return (
        struct.pack("<I", 2)
        + struct.pack("<HHI", 0x01, 0o6, _NONE)  # user::rw-
        + struct.pack("<HHI", 0x02, 0o4, uid)  # user:<uid>:r--  (the named entry)
        + struct.pack("<HHI", 0x04, 0o4, _NONE)  # group::r--
        + struct.pack("<HHI", 0x10, 0o4, _NONE)  # mask::r--
        + struct.pack("<HHI", 0x20, 0o4, _NONE)  # other::r--
    )


def _set_named_acl(path, uid=None):
    blob = _minimal_named_acl(os.getuid() if uid is None else uid)
    os.setxattr(path, ACL, blob, follow_symlinks=False)
    return blob


@pytest.fixture(scope="module", autouse=True)
def _posix_acl_filesystem(tmp_path_factory):
    """Skip the whole module where a real setxattr of system.posix_acl_access fails.

    The probe is a real round-trip, never a capability guess: a file is created in a
    real tmp_path and the blob is written and read back.  Only "cannot carry it"
    errnos skip (unsupported / unprivileged); any other failure is a broken
    instrument and is raised, so the cells can never pass vacuously.
    """
    if not (hasattr(os, "setxattr") and hasattr(os, "getxattr")):
        pytest.skip(
            "platform does not expose os.setxattr/os.getxattr (macOS/Windows): "
            "POSIX ACLs are not readable through the xattr API here"
        )
    probe = tmp_path_factory.mktemp("acl-probe") / "probe"
    probe.write_text("x")
    cannot_carry = {
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.ENOSYS,
        errno.EPERM,
    }
    try:
        blob = _set_named_acl(probe)
        got = os.getxattr(probe, ACL, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in cannot_carry:
            name = errno.errorcode.get(exc.errno, str(exc.errno))
            pytest.skip(
                f"filesystem cannot carry system.posix_acl_access unprivileged "
                f"({name}: {exc}); macOS ACLs are not this xattr at all"
            )
        raise
    if got != blob:
        pytest.skip(
            "filesystem accepted the ACL blob but did not return it verbatim "
            f"(got {got.hex()}); ACL preservation is not observable here"
        )


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def writer_mods():
    sys.path.insert(0, str(SRC))
    try:
        import engram_config_io
    finally:
        sys.path.remove(str(SRC))
    return {
        "core": engram_config_io,
        "hooks": _load("_hooklib_4641", SRC / "hooks" / "claude" / "_hooklib.py"),
    }


@pytest.fixture(params=["core", "hooks"])
def mod(request, writer_mods):
    return writer_mods[request.param]


def test_named_acl_entry_survives_an_atomic_write(mod, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("old")
    before = _set_named_acl(p)
    assert os.getxattr(p, ACL, follow_symlinks=False) == before, "setup: the ACL was not set"

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert os.getxattr(p, ACL, follow_symlinks=False) == before, (
        "the named POSIX ACL entry was dropped by the replacement; the new inode "
        "must carry the source's system.posix_acl_access blob"
    )


def test_user_xattr_survives_an_atomic_write(mod, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.test", b"keep-me", follow_symlinks=False)

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert os.getxattr(p, "user.test", follow_symlinks=False) == b"keep-me", (
        "the user.* extended attribute was dropped by the replacement"
    )


def test_plain_mode_and_group_still_preserved_control(mod, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("old")
    os.chmod(p, 0o640)
    before = os.stat(p)

    mod.atomic_write_text(p, "new")

    after = os.stat(p)
    assert stat.S_IMODE(after.st_mode) == 0o640, oct(stat.S_IMODE(after.st_mode))
    assert after.st_gid == before.st_gid


def test_file_without_acl_is_written_normally(mod, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("old")
    try:
        os.getxattr(p, ACL, follow_symlinks=False)
    except OSError as exc:
        assert exc.errno in (
            errno.ENODATA,
            getattr(errno, "ENOATTR", errno.ENODATA),
        ), f"setup: unexpected errno {exc.errno} on a file with no ACL"

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"


def test_acl_apply_failure_raises_and_leaves_the_target_intact(mod, tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("old")
    _set_named_acl(p)  # the source HAS an ACL, so applying it must be attempted
    before = p.read_bytes()

    def boom(*args, **kwargs):
        raise OSError(errno.EPERM, "cannot set ACL on the temp")

    monkeypatch.setattr(os, "setxattr", boom)
    with pytest.raises(OSError):
        mod.atomic_write_text(p, "new")

    assert p.read_bytes() == before, "the target must be untouched when the ACL cannot be kept"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"], (
        "the temp file must be removed when applying the ACL fails"
    )
