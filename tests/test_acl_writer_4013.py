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
COPIES = {
    "core": SRC / "engram_config_io.py",
    "hooks": SRC / "hooks" / "claude" / "_hooklib.py",
}
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


def _load_fresh(name, path):
    """A freshly imported copy, for cells that change the platform API state the
    module decides its guard from at import."""
    sys.path.insert(0, str(SRC))
    try:
        return _load(name, path)
    finally:
        sys.path.remove(str(SRC))


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
    before_acl = _set_named_acl(p)
    assert os.getxattr(p, ACL, follow_symlinks=False) == before_acl, "setup: the ACL was not set"
    before = os.stat(p)

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert os.getxattr(p, ACL, follow_symlinks=False) == before_acl, (
        "the named POSIX ACL entry was dropped by the replacement; the new inode "
        "must carry the source's system.posix_acl_access blob"
    )
    assert stat.S_IMODE(os.stat(p).st_mode) == stat.S_IMODE(before.st_mode), (
        "the mode changed; a step applied after the ACL copy (fchmod rewrites the "
        "ACL mask, the final word on the group bits) must not run after it"
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


def test_acl_applied_then_a_user_xattr_set_fails(mod, tmp_path, monkeypatch):
    """A2/A5: the ACL is applied, THEN the ``user.*`` copy fails.

    A5 monkeypatches every ``os.setxattr`` call, so it fails on the ACL set and
    never reaches the per-name copy; this cell pins the cleanup and the
    target's byte-identity on that second path.
    """
    p = tmp_path / "state.json"
    p.write_text("old")
    _set_named_acl(p)
    os.setxattr(p, "user.test", b"keep-me", follow_symlinks=False)
    before = p.read_bytes()
    real_setxattr = os.setxattr

    def spy(path, name, value, **kwargs):
        if name == "user.test":
            raise OSError(errno.EPERM, "cannot set the user xattr on the temp")
        return real_setxattr(path, name, value, **kwargs)

    monkeypatch.setattr(os, "setxattr", spy)
    with pytest.raises(OSError):
        mod.atomic_write_text(p, "new")

    assert p.read_bytes() == before, "the target must be untouched when the user.* copy fails"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"], (
        "the temp file must be removed when the user.* copy fails"
    )


def test_acl_and_user_xattr_both_survive_one_write(mod, tmp_path):
    """A1+A2 in one write: the ACL is set first, then named attributes are listed."""
    p = tmp_path / "state.json"
    p.write_text("old")
    acl = _set_named_acl(p)
    os.setxattr(p, "user.test", b"keep-me", follow_symlinks=False)

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert os.getxattr(p, ACL, follow_symlinks=False) == acl, "the named ACL was dropped"
    assert os.getxattr(p, "user.test", follow_symlinks=False) == b"keep-me", (
        "the user.* xattr was dropped"
    )


def test_listxattr_unsupported_is_tolerated_and_copies_no_names(mod, tmp_path, monkeypatch):
    """A filesystem without xattr listing cannot enumerate names: that is "nothing
    to copy", not an error (ENOTSUP/EOPNOTSUPP/ENOSYS are in the no-attribute set)."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.test", b"unseen", follow_symlinks=False)

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "listxattr is not supported here")

    monkeypatch.setattr(os, "listxattr", unsupported)
    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    with pytest.raises(OSError):
        os.getxattr(p, "user.test", follow_symlinks=False)


def test_user_xattr_removed_between_listxattr_and_getxattr_is_skipped(mod, tmp_path, monkeypatch):
    """ENODATA/ENOATTR on the per-name read is a benign race: the attribute was
    listed and then vanished. It is genuinely absent, so skipping it is not a
    silent loss and the write must succeed."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.test", b"gone", follow_symlinks=False)
    real_getxattr = os.getxattr

    def racing(path, name, **kwargs):
        if name == "user.test":
            raise OSError(errno.ENODATA, "removed between listxattr and getxattr")
        return real_getxattr(path, name, **kwargs)

    monkeypatch.setattr(os, "getxattr", racing)
    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


def test_user_xattr_unreadable_raises_and_keeps_the_target(mod, tmp_path, monkeypatch):
    """EACCES/EPERM on the per-name read is NOT tolerated. A file the caller may
    write but not read would otherwise lose the attribute silently, which is the
    defect this writer exists to fix; "we cannot tell" must be loud. This is a
    bounded behaviour change versus the writer before #4641."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.test", b"unreadable", follow_symlinks=False)
    before = p.read_bytes()
    real_getxattr = os.getxattr

    def denied(path, name, **kwargs):
        if name == "user.test":
            raise OSError(errno.EACCES, "write-only file: cannot read the xattr")
        return real_getxattr(path, name, **kwargs)

    monkeypatch.setattr(os, "getxattr", denied)
    with pytest.raises(OSError):
        mod.atomic_write_text(p, "new")

    assert p.read_bytes() == before, "the target must be untouched when a user.* read fails"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


@pytest.mark.parametrize("which", ["core", "hooks"])
def test_no_xattr_api_is_a_no_op_not_an_error(which, tmp_path, monkeypatch):
    """CPython exposes ``os.getxattr``/``os.setxattr``/``os.listxattr`` on
    Linux/FreeBSD only; macOS has none of the three, so an unconditional call
    raises ``AttributeError`` (not ``OSError``) -- and some callers catch only
    ``OSError``. The writer must treat an absent API as "nothing to copy".

    The guard is decided at import, so this cell imports a fresh copy with the
    attributes deleted: that is the macOS import-time state. Against a writer
    that calls the API unconditionally it raises ``AttributeError``.
    """
    p = tmp_path / "state.json"
    p.write_text("old")

    monkeypatch.delattr(os, "getxattr")
    monkeypatch.delattr(os, "setxattr")
    monkeypatch.delattr(os, "listxattr")
    mod = _load_fresh(f"_writer_no_xattr_{which}", COPIES[which])

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]
