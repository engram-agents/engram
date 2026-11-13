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
  A6  The same guarantee from the other side: replacing an existing file that had
      NO access ACL leaves the target with NO access ACL, even when the target's
      directory carries a DEFAULT ACL that the staged temp inherits. A brand-new
      target still inherits that default ACL by design (a plain ``open`` would
      give it the same), so the removal is confined to the replace path.

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
DEFAULT_ACL = "system.posix_acl_default"
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


def _nobody_uid():
    """The uid the default-ACL cells name: ``u:nobody:r``, as the defect was
    measured. Falls back to the conventional nobody uid where the account is
    absent, so the cell still names a user that is neither owner nor group."""
    try:
        import pwd
    except ImportError:  # Windows: the module fixture skips before any cell runs
        return 65534
    try:
        return pwd.getpwnam("nobody").pw_uid
    except KeyError:
        return 65534


def _set_default_acl(directory, uid):
    """Give ``directory`` a default access ACL carrying one named user entry.

    Every file created inside inherits it -- that is how the writer's staged
    temp picks up an ACL the file being REPLACED never had.
    """
    blob = _minimal_named_acl(uid)
    os.setxattr(directory, DEFAULT_ACL, blob, follow_symlinks=False)
    return blob


def _require_default_acl(directory, uid):
    """``_set_default_acl``, or a loud skip when this filesystem cannot carry a
    default ACL unprivileged. Never a weakened assertion."""
    cannot_carry = {
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.ENOSYS,
        errno.EPERM,
    }
    try:
        return _set_default_acl(directory, uid)
    except OSError as exc:
        if exc.errno in cannot_carry:
            name = errno.errorcode.get(exc.errno, str(exc.errno))
            pytest.skip(
                f"filesystem cannot carry system.posix_acl_default unprivileged "
                f"({name}: {exc})"
            )
        raise


def _acl_entries(blob):
    """Parse a POSIX ACL xattr blob into ``(tag, perm, id)`` entries."""
    version = struct.unpack_from("<I", blob, 0)[0]
    assert version == 2, f"unexpected ACL blob version {version}"
    return [struct.unpack_from("<HHI", blob, offset) for offset in range(4, len(blob), 8)]


def _has_named_user_entry(blob, uid):
    """True when the blob carries ``user:<uid>:r--`` (tag 0x02, perm 0o4)."""
    return any(
        tag == 0x02 and perm == 0o4 and ident == uid for tag, perm, ident in _acl_entries(blob)
    )


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


@pytest.mark.parametrize("mode", [0o640, 0o600])
def test_replacing_a_file_with_no_acl_does_not_install_the_directory_default(
    mod, tmp_path, mode
):
    """A6: the temp is created beside the target, so it inherits that directory's
    DEFAULT access ACL. The file being replaced had none, so publishing the
    temp's inherited ACL would GRANT access the old file never had -- the same
    metadata-survives-the-write guarantee as A1/A2, from the other side."""
    target = tmp_path / "state.json"
    target.write_text("old")
    os.chmod(target, mode)
    try:
        os.getxattr(target, ACL, follow_symlinks=False)
    except OSError as exc:
        assert exc.errno in (
            errno.ENODATA,
            getattr(errno, "ENOATTR", errno.ENODATA),
        ), f"setup: unexpected errno {exc.errno} on a file with no ACL"
    else:
        pytest.fail("setup: the target must start with no access ACL")

    nobody = _nobody_uid()
    _require_default_acl(tmp_path, nobody)
    # Instrument check: prove the hazard is live HERE -- a file created in this
    # directory now inherits the default entry. Without it the cell could pass
    # against a writer that never had an ACL to remove.
    probe = tmp_path / "probe"
    probe.write_text("x")
    try:
        inherited = os.getxattr(probe, ACL, follow_symlinks=False)
    except OSError as exc:
        pytest.fail(
            f"the default ACL did not reach a new file ({exc}); this cell would "
            "assert nothing"
        )
    assert _has_named_user_entry(inherited, nobody), inherited.hex()
    probe.unlink()

    mod.atomic_write_text(target, "new")

    assert target.read_text() == "new"
    try:
        installed = os.getxattr(target, ACL, follow_symlinks=False)
    except OSError as exc:
        assert exc.errno in (
            errno.ENODATA,
            getattr(errno, "ENOATTR", errno.ENODATA),
        ), f"unexpected errno {exc.errno} reading the target's ACL"
    else:
        pytest.fail(
            "the replaced file had no access ACL, but the write installed one "
            f"({installed.hex()}): it now grants access the old file never had"
        )
    after = stat.S_IMODE(os.stat(target).st_mode)
    assert after == mode, f"mode {oct(after)} != the source's {oct(mode)}"
    assert after & 0o007 == 0, (
        "with no access ACL the other bits decide for a non-owner: they must not "
        "grant a user outside the owning group the read the default entry gave it"
    )
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


def test_a_new_target_keeps_the_directory_default_acl(mod, tmp_path):
    """A6's control: nothing is being replaced, so the new inode keeps the
    directory's default ACL, exactly as a plain ``open`` would give it. Stripping
    it here would be a behaviour change, not a fix."""
    target = tmp_path / "brand-new.json"
    nobody = _nobody_uid()
    _require_default_acl(tmp_path, nobody)

    mod.atomic_write_text(target, "new")

    assert target.read_text() == "new"
    inherited = os.getxattr(target, ACL, follow_symlinks=False)
    assert _has_named_user_entry(inherited, nobody), (
        "a brand-new target lost the directory's default ACL; that is what a "
        f"plain open() would give it (blob {inherited.hex()})"
    )


def test_acl_read_unsupported_is_tolerated_and_applies_no_acl(mod, tmp_path, monkeypatch):
    """A filesystem that cannot carry xattrs at all is "no ACL to copy", not an
    error: EOPNOTSUPP/ENOTSUP/ENOSYS on the explicit ACL ``getxattr`` is in the
    absent-or-unsupported set, so the write succeeds and no ACL is applied."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.keep", b"keep-me", follow_symlinks=False)
    real_getxattr = os.getxattr
    real_setxattr = os.setxattr
    set_calls = []

    def unsupported(path, name, **kwargs):
        if name == ACL:
            raise OSError(errno.EOPNOTSUPP, "ACLs are not supported on this filesystem")
        return real_getxattr(path, name, **kwargs)

    def spy_set(path, name, value, **kwargs):
        set_calls.append(name)
        return real_setxattr(path, name, value, **kwargs)

    monkeypatch.setattr(os, "getxattr", unsupported)
    monkeypatch.setattr(os, "setxattr", spy_set)

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert ACL not in set_calls, (
        "an unsupported ACL read must not be followed by an ACL copy"
    )
    assert os.getxattr(p, "user.keep", follow_symlinks=False) == b"keep-me", (
        "tolerating the ACL read must not skip the user.* copy"
    )
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


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


def test_only_the_user_namespace_is_copied_from_listxattr(mod, tmp_path, monkeypatch):
    """The copy is bounded to ``user.*`` even when ``listxattr`` reports other
    namespaces.  ``security.*``/``trusted.*`` are frequently unsettable by an
    unprivileged writer, so attempting them turns a normal write into an SELinux
    host failure; only the ``user.*`` names may reach ``setxattr``."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.setxattr(p, "user.keep", b"keep-me", follow_symlinks=False)
    foreign = {"security.selinux": b"ctx", "trusted.overlay": b"opaque"}
    real_getxattr = os.getxattr
    real_setxattr = os.setxattr
    set_calls = []

    def listed(path, **kwargs):
        # The ACL name is included on purpose: the ACL is read explicitly, never
        # inferred from this list, so a filesystem that reports it must not make
        # the user.* loop copy it either.
        return [ACL, "user.keep", "security.selinux", "trusted.overlay"]

    def reading(path, name, **kwargs):
        if name in foreign:
            return foreign[name]  # readable on the simulated host, hence tempting
        return real_getxattr(path, name, **kwargs)

    def writing(path, name, value, **kwargs):
        set_calls.append(name)
        if name in foreign:
            return None  # recorded, not really applied: unprivileged here
        return real_setxattr(path, name, value, **kwargs)

    monkeypatch.setattr(os, "listxattr", listed)
    monkeypatch.setattr(os, "getxattr", reading)
    monkeypatch.setattr(os, "setxattr", writing)

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert set_calls == ["user.keep"], (
        "only the user.* namespace may be copied; the writer attempted "
        f"{set_calls!r}"
    )


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


@pytest.mark.parametrize("which", ["core", "hooks"])
def test_absent_removexattr_is_a_no_op_not_an_error(which, tmp_path, monkeypatch):
    """The ACL REMOVAL needs the same import-time guard as the copy: a platform
    where ``os.removexattr`` is absent must never attempt the call (it raises
    ``AttributeError``, not ``OSError``).

    Deleting ONLY ``removexattr`` leaves every other guard attribute present, so
    this cell discriminates a guarded removal from an unconditional one -- unlike
    deleting all four, which the pre-existing cell above already exercises.
    """
    p = tmp_path / "state.json"
    p.write_text("old")

    monkeypatch.delattr(os, "removexattr")
    mod = _load_fresh(f"_writer_no_removexattr_{which}", COPIES[which])

    mod.atomic_write_text(p, "new")

    assert p.read_text() == "new"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


def test_dir_fd_write_replacing_an_existing_file_skips_the_copy(mod, tmp_path):
    """Under dir_fd the metadata copy is skipped, and it must not raise.

    This is the composition cell: without the ``dir_fd is None`` conjunct the
    read resolves ``a.json`` against the process CWD and raises ENOENT, which
    is NOT in the tolerance set -- so a registration write would succeed the
    first time (no target yet) and fail every session after.
    """
    d = tmp_path / "reg"
    d.mkdir()
    target = d / "a.json"
    target.write_text("old")
    os.setxattr(target, "user.test", b"source-value", follow_symlinks=False)

    dir_fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mod.atomic_write_text("a.json", "new", dir_fd=dir_fd, mode=0o644)
    finally:
        os.close(dir_fd)

    assert target.read_text() == "new"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    with pytest.raises(OSError) as exc_info:
        os.getxattr(target, "user.test", follow_symlinks=False)
    assert exc_info.value.errno in (
        errno.ENODATA,
        getattr(errno, "ENOATTR", errno.ENODATA),
    ), f"the copy was not skipped under dir_fd: user.test survived ({exc_info.value})"


def test_mode_applied_before_the_acl_so_the_preserved_mask_survives(mod, tmp_path):
    """``mode=`` must never run after the ACL copy: fchmod rewrites the ACL
    mask, which is the final word on the group bits (#4682 measured it)."""
    p = tmp_path / "state.json"
    p.write_text("old")
    before_acl = _set_named_acl(p)

    mod.atomic_write_text(p, "new", mode=0o600)

    assert p.read_text() == "new"
    # The file's mode here is whatever the preserved ACL encodes (0o644 for
    # _minimal_named_acl), NOT mode=0o600: the ACL mask is the final word on
    # the group bits, by design.  Asserting the blob, not the mode, is the
    # point of this cell.
    assert os.getxattr(p, ACL, follow_symlinks=False) == before_acl, (
        "the preserved ACL was altered by the mode= application; fchmod after "
        "setxattr rewrites mask::, so mode= must come first"
    )


def test_dir_fd_write_in_a_default_acl_directory_leaves_the_inherited_acl(mod, tmp_path):
    """Pins a KNOWN difference between the two modes: the path-based writer
    REMOVES the temp's inherited access ACL when the replaced file had none
    (cell A6), while the ``dir_fd`` writer installs it.

    This is a deliberate scope choice for this composition, not an API
    limitation (#4682: the xattr calls take no ``dir_fd`` keyword, but they DO
    accept an open file descriptor, so an fd-relative copy is available), and
    not a property to preserve.  The cell exists so that a future change to
    either mode must be deliberate rather than silent.
    """
    nobody = _nobody_uid()
    _require_default_acl(tmp_path, nobody)
    # Created AFTER the default ACL, so the target inherited one; remove it so
    # the file being replaced genuinely has NO access ACL (the A6 shape).
    target = tmp_path / "state.json"
    target.write_text("old")
    os.removexattr(target, ACL, follow_symlinks=False)
    try:
        os.getxattr(target, ACL, follow_symlinks=False)
    except OSError as exc:
        assert exc.errno in (
            errno.ENODATA,
            getattr(errno, "ENOATTR", errno.ENODATA),
        ), f"setup: unexpected errno {exc.errno} reading the target's ACL"
    else:
        pytest.fail("setup: the target must start with no access ACL")

    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mod.atomic_write_text("state.json", "new", dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    assert target.read_text() == "new"
    inherited = os.getxattr(target, ACL, follow_symlinks=False)
    assert _has_named_user_entry(inherited, nobody), (
        "the dir_fd writer was expected to leave the directory default's named "
        f"entry installed, the accepted cost of the skip; got {inherited.hex()}"
    )
