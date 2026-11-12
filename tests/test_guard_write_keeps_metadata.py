"""A file's metadata survives a write to it (engram_file_guard's write path).

The guard rewrites an EXISTING file in place, in the same inode, so its mode, owner, group,
hard links, ACL and extended attributes stay as they were (what v0.5.6 did). It creates a NEW
file exclusively and all at once. The stamped backup is taken before any rewrite, and a write
that fails part-way puts the old bytes back and names the backup.

Two rules keep these checks from passing blind (engram#4629):
  * PLANT what no directory default supplies: the ACL entry asserted on is set on the file
    itself, and one cell runs in a directory whose default (inherited) ACL grants something
    else, so a file recreated there would carry the default and still lose the planted entry.
  * FORCE the rewrite: every cell makes the writer really change the file (a differing render
    with consent, a changed key), and checks the bytes did change.

ACL cells use setfacl/getfacl on Linux and chmod +a / ls -le on macOS; xattr cells use
os.setxattr on Linux and /usr/bin/xattr on macOS. A cell whose tool or filesystem support is
absent is skipped with the reason. The inode cells run everywhere.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bootstrap_no_clobber_4440 import SRC, _run  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("engram_file_guard_metadata_under_test", SRC / "engram_file_guard.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)

STAMP = "20261112T000000Z"
XATTR = "user.engram_probe"
DARWIN = sys.platform == "darwin"


def _backups(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".engram-backup."))


# --- planting and reading ACL entries and xattrs ---------------------------------------------

def _acl_entries(path: Path) -> "list[str]":
    """The file's own ACL entries, one string each (index numbers dropped on macOS)."""
    if DARWIN:
        out = subprocess.run(["/bin/ls", "-led", str(path)], capture_output=True, text=True, check=True).stdout
        return [line.split(":", 1)[1].strip() for line in out.splitlines()[1:] if ":" in line]
    out = subprocess.run(["getfacl", "--omit-header", "--absolute-names", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("#")[0].strip() for line in out.splitlines() if line.strip()]


def _plant_acl(path: Path) -> str:
    """Give `path` a named-user read entry for `nobody`; return the text that names it."""
    if DARWIN:
        subprocess.run(["/bin/chmod", "+a", "nobody allow read", str(path)], check=True)
        entries = [e for e in _acl_entries(path) if e.endswith("allow read")]
        assert entries, _acl_entries(path)
        return entries[0]
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("setfacl/getfacl not installed; the inode cells cover this host")
    r = subprocess.run(["setfacl", "-m", "user:nobody:r--", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"this filesystem refused a POSIX ACL: {r.stderr.strip()}")
    return "user:nobody:r--"


def _plant_default_acl(directory: Path) -> str:
    """Give `directory` an inherited ACL entry that differs from the one _plant_acl sets."""
    if DARWIN:
        subprocess.run(["/bin/chmod", "+a", "daemon allow read,file_inherit", str(directory)], check=True)
        return "daemon"
    if shutil.which("setfacl") is None:
        pytest.skip("setfacl not installed")
    r = subprocess.run(["setfacl", "-d", "-m", "user:daemon:r--", str(directory)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"this filesystem refused a default ACL: {r.stderr.strip()}")
    return "user:daemon:r--"


def _set_xattr(path: Path) -> None:
    if hasattr(os, "setxattr"):
        try:
            os.setxattr(path, XATTR, b"kept")
        except OSError as e:
            if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
                pytest.skip(f"this filesystem refused a user xattr ({e})")
            raise
    elif DARWIN and Path("/usr/bin/xattr").exists():
        subprocess.run(["/usr/bin/xattr", "-w", XATTR, "kept", str(path)], check=True)
    else:
        pytest.skip("no way to set an extended attribute on this host")


def _get_xattr(path: Path) -> "bytes | None":
    if hasattr(os, "getxattr"):
        try:
            return os.getxattr(path, XATTR)
        except OSError:
            return None
    r = subprocess.run(["/usr/bin/xattr", "-p", XATTR, str(path)], capture_output=True)
    return r.stdout.rstrip(b"\n") if r.returncode == 0 else None


def _meta(path: Path) -> dict:
    st = os.stat(path)
    return {"ino": (st.st_dev, st.st_ino), "mode": st.st_mode & 0o7777, "uid": st.st_uid,
            "gid": st.st_gid, "nlink": st.st_nlink}


# --- the writers, each forced to really change an existing file -------------------------------

def _rewrite_by_apply(path: Path) -> None:
    """A protected file that differs, replaced on consent (how bootstrap renders CLAUDE.md)."""
    p = fg.plan([fg.Item(path, "new text\n", first_identity=True)], STAMP)
    assert p.entries[0].state == "differs"
    fg.apply(p, fg.consent_for(True, []))


def _rewrite_owned(path: Path) -> None:
    """An ENGRAM-owned bookkeeping file in the home, rewritten with no consent and no backup."""
    p = fg.plan([fg.Item(path, "new text\n", owned=True)], STAMP, home=path.parent)
    assert p.entries[0].state == "owned"
    fg.apply(p)


def _rewrite_by_merge(path: Path) -> None:
    """config.json with an owned key changed (how both installers and set_forum_url write it)."""
    assert fg.apply_merge(path, {"install_tier": "dev"}, filename="config.json", stamp=STAMP) == "merged"


WRITERS = {
    "apply": ("CLAUDE.md", "mine\n", _rewrite_by_apply),
    "owned": ("seed-manifest.json", "{}\n", _rewrite_owned),
    "merge": ("config.json", json.dumps({"install_tier": "convenience", "mine": 1}) + "\n", _rewrite_by_merge),
}


@pytest.fixture(params=sorted(WRITERS))
def written(request, tmp_path):
    """(path, before-bytes, writer) for an existing file, mode 0640, in its own folder."""
    name, text, writer = WRITERS[request.param]
    folder = tmp_path / "home"
    folder.mkdir()
    path = folder / name
    path.write_text(text)
    os.chmod(path, 0o640)
    return path, text.encode(), writer


def test_an_existing_file_keeps_its_inode_mode_owner_and_links(written):
    """The portable cell: same inode, so nothing attached to the inode can be lost."""
    path, before_bytes, writer = written
    other_name = path.parent / "second-name"
    os.link(path, other_name)
    before = _meta(path)
    assert before["nlink"] == 2
    writer(path)
    assert path.read_bytes() != before_bytes, "the writer must really have rewritten the file"
    assert _meta(path) == before
    assert other_name.read_bytes() == path.read_bytes(), "one file, two names, as before the write"


def test_an_existing_file_keeps_a_planted_acl_entry_and_xattr(written):
    path, before_bytes, writer = written
    entry = _plant_acl(path)
    _set_xattr(path)
    assert entry in _acl_entries(path) and _get_xattr(path) == b"kept"
    writer(path)
    assert path.read_bytes() != before_bytes
    assert entry in _acl_entries(path), _acl_entries(path)
    assert _get_xattr(path) == b"kept"


def test_a_planted_entry_survives_in_a_folder_whose_default_acl_differs(written):
    """A file recreated in this folder would inherit the folder's default and look as if it had
    an ACL; only the entry planted on the file itself tells the two apart."""
    path, before_bytes, writer = written
    default = _plant_default_acl(path.parent)
    entry = _plant_acl(path)
    assert entry != default and default not in entry
    before = _acl_entries(path)
    writer(path)
    assert path.read_bytes() != before_bytes
    assert _acl_entries(path) == before


def test_backup_is_taken_before_the_rewrite_and_holds_the_old_bytes(tmp_path):
    for name in ("CLAUDE.md", "config.json"):
        _, text, writer = WRITERS["apply" if name == "CLAUDE.md" else "merge"]
        folder = tmp_path / name.replace(".", "-")
        folder.mkdir()
        path = folder / name
        path.write_text(text)
        writer(path)
        backups = _backups(path)
        assert len(backups) == 1 and backups[0].read_text() == text


# --- a NEW file: still exclusive and all at once ----------------------------------------------

def test_a_new_file_is_created_with_the_umask_mode(tmp_path):
    target = tmp_path / "sub" / "a.txt"
    old = os.umask(0o027)
    try:
        assert fg.apply(fg.plan([fg.Item(target, "new\n")], STAMP)) == [target]
    finally:
        os.umask(old)
    assert target.read_text() == "new\n"
    assert os.stat(target).st_mode & 0o777 == 0o640
    assert sorted(p.name for p in target.parent.iterdir()) == ["a.txt"], "no temp file left behind"


def test_create_exclusive_never_touches_something_already_there(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("someone else's\n")
    with pytest.raises(FileExistsError):
        fg.create_exclusive(target, "new\n", 0o600)
    assert target.read_text() == "someone else's\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


def test_create_exclusive_does_not_write_through_a_symlink(tmp_path):
    elsewhere = tmp_path / "elsewhere.txt"
    target = tmp_path / "a.txt"
    target.symlink_to(elsewhere)
    with pytest.raises(FileExistsError):
        fg.create_exclusive(target, "new\n", 0o600)
    assert not elsewhere.exists()


def test_a_new_file_that_fails_mid_write_leaves_nothing(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"

    def boom(fd):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(fg.os, "fsync", boom)
    with pytest.raises(OSError):
        fg.create_exclusive(target, "new\n", 0o600)
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_appears_after_the_check_is_refused_not_overwritten(tmp_path, monkeypatch):
    """The plan found nothing at the path; by the time of the write someone has put a file
    there. It is not ENGRAM's and has no backup, so it is refused and left as it is."""
    target = tmp_path / "CLAUDE.md"
    real_plan = fg.plan

    def plan_then_someone_writes(items, *a, **kw):
        p = real_plan(items, *a, **kw)
        if not target.exists():
            target.write_text("someone else's\n")
        return p

    monkeypatch.setattr(fg, "plan", plan_then_someone_writes)
    with pytest.raises(SystemExit) as got:
        fg.apply(real_plan([fg.Item(target, "new\n")], STAMP))
    assert "appeared" in str(got.value) and "owns" not in str(got.value), str(got.value)
    assert target.read_text() == "someone else's\n" and _backups(target) == []


def test_ctrl_c_mid_write_puts_the_old_bytes_back_and_stops(tmp_path, monkeypatch):
    name, text, writer = WRITERS["apply"]
    path = tmp_path / name
    path.write_text(text)
    before = _meta(path)
    real = fg._write_all
    calls = []

    def interrupted(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            os.write(fd, data[: len(data) // 2])
            raise KeyboardInterrupt
        real(fd, data)

    monkeypatch.setattr(fg, "_write_all", interrupted)
    with pytest.raises(KeyboardInterrupt):
        writer(path)
    assert path.read_text() == text and _meta(path) == before
    assert len(_backups(path)) == 1


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write a read-only file")
def test_a_read_only_file_is_refused_unchanged_and_the_line_names_the_backup(tmp_path):
    """A rename could replace a read-only file; an in-place write cannot (as at v0.5.6). The run
    refuses with the file and its mode untouched and the backup named."""
    name, text, writer = WRITERS["apply"]
    path = tmp_path / name
    path.write_text(text)
    os.chmod(path, 0o444)
    with pytest.raises(SystemExit) as got:
        writer(path)
    backups = _backups(path)
    assert path.read_text() == text and os.stat(path).st_mode & 0o777 == 0o444
    assert len(backups) == 1 and str(backups[0]) in str(got.value) and "not changed" in str(got.value)


def test_apply_merge_creates_a_new_config_exclusively(tmp_path):
    path = tmp_path / "config.json"
    assert fg.apply_merge(path, {"install_tier": "dev"}, filename="config.json", new_mode=0o600) == "created"
    assert json.loads(path.read_text()) == {"install_tier": "dev"}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


# --- a write that fails part-way --------------------------------------------------------------

def _fail_first_write(monkeypatch, then_fail_again: bool):
    """The first _write_all writes half its bytes and fails (a full disk); later calls (putting
    the old bytes back) succeed, or fail too when `then_fail_again`."""
    real = fg._write_all
    calls = []

    def flaky(fd, data):
        calls.append(len(data))
        if len(calls) == 1 or then_fail_again:
            os.write(fd, data[: len(data) // 2])
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        real(fd, data)

    monkeypatch.setattr(fg, "_write_all", flaky)
    return calls


@pytest.mark.parametrize("which", ["apply", "merge"])
def test_a_failed_rewrite_puts_the_old_bytes_back_and_names_the_backup(tmp_path, monkeypatch, which):
    name, text, writer = WRITERS[which]
    path = tmp_path / name
    path.write_text(text)
    before = _meta(path)
    calls = _fail_first_write(monkeypatch, then_fail_again=False)
    with pytest.raises(SystemExit) as got:
        writer(path)
    msg = str(got.value)
    backups = _backups(path)
    assert len(backups) == 1 and backups[0].read_text() == text
    assert path.read_text() == text, "the old bytes are back"
    assert _meta(path) == before, "and in the same inode"
    assert msg.startswith("[bootstrap] REFUSED:") and str(backups[0]) in msg and "put back" in msg, msg
    assert len(calls) == 2


def test_a_failed_rewrite_whose_put_back_also_fails_says_so_and_names_the_backup(tmp_path, monkeypatch):
    name, text, writer = WRITERS["apply"]
    path = tmp_path / name
    path.write_text(text)
    _fail_first_write(monkeypatch, then_fail_again=True)
    with pytest.raises(SystemExit) as got:
        writer(path)
    msg = str(got.value)
    backups = _backups(path)
    assert len(backups) == 1 and backups[0].read_text() == text, "the backup is intact"
    assert "may be incomplete" in msg and str(backups[0]) in msg and "Restore it with: " in msg, msg


def test_the_restore_command_in_the_message_keeps_the_acl_and_xattr(tmp_path, monkeypatch):
    """The backup carries bytes, mode and mtime, not the ACL or xattrs, so the command the
    message gives must restore INTO the file (same inode), not replace its metadata with the
    backup's. Runs the printed command as written."""
    name, text, writer = WRITERS["apply"]
    path = tmp_path / name
    path.write_text(text)
    entry = _plant_acl(path)
    _set_xattr(path)
    before = _meta(path)
    _fail_first_write(monkeypatch, then_fail_again=True)
    with pytest.raises(SystemExit) as got:
        writer(path)
    monkeypatch.undo()
    command = str(got.value).split("Restore it with: ", 1)[1].strip()
    subprocess.run(["/bin/sh", "-c", command], check=True)
    assert path.read_text() == text
    assert _meta(path) == before
    assert entry in _acl_entries(path) and _get_xattr(path) == b"kept"


def test_a_failed_rewrite_of_an_owned_file_says_there_is_no_backup(tmp_path, monkeypatch):
    path = tmp_path / "seed-manifest.json"
    path.write_text("{}\n")
    _fail_first_write(monkeypatch, then_fail_again=False)
    with pytest.raises(SystemExit) as got:
        _rewrite_owned(path)
    assert path.read_text() == "{}\n"
    assert "no backup" in str(got.value) and _backups(path) == []


# --- the symlink decision is unchanged --------------------------------------------------------

def test_a_symlinked_file_stays_a_link_and_its_target_keeps_its_inode(tmp_path):
    real = tmp_path / "real" / "CLAUDE.md"
    real.parent.mkdir()
    real.write_text("mine\n")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real)
    before = _meta(real)
    _rewrite_by_apply(link)
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert real.read_text() == "new text\n" and _meta(real) == before


def test_a_symlink_to_a_fifo_is_refused_and_nothing_waits(tmp_path):
    """The link is resolved first, then the open is O_NOFOLLOW|O_NONBLOCK on what it resolved
    to: a FIFO there is refused (by the plan, and by the final open if one appears later),
    never opened in a way that waits for a reader."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(fifo)
    p = fg.plan([fg.Item(link, "new text\n", first_identity=True)], STAMP)
    assert p.entries[0].state == "blocked"
    with pytest.raises(fg.Refused):
        fg.apply(p, fg.consent_for(True, []))
    with pytest.raises(SystemExit) as got:
        fg.write_in_place(fifo, "new text\n", None)
    assert "not a regular file" in str(got.value)
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(fifo)


def test_write_in_place_refuses_a_link_put_there_after_the_check(tmp_path):
    """The final open does not follow a link: the decision was made about a regular file, so a
    link that took its place meanwhile is refused, and what it points at is not written."""
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("not ours\n")
    path = tmp_path / "CLAUDE.md"
    path.symlink_to(elsewhere)
    with pytest.raises(SystemExit):
        fg.write_in_place(path, "new\n", None)
    assert elsewhere.read_text() == "not ours\n"


# --- end to end: bootstrap rendering over an existing CLAUDE.md -------------------------------

def test_bootstrap_force_over_an_existing_claude_md_keeps_its_inode_acl_and_xattr(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"mine\n")
    os.chmod(md, 0o640)
    entry = _plant_acl(md)
    _set_xattr(md)
    before = _meta(md)
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert md.read_bytes() != b"mine\n"
    assert (tmp_path / ".claude" / "CLAUDE.md.pre-engram").read_bytes() == b"mine\n"
    assert _meta(md) == before
    assert entry in _acl_entries(md) and _get_xattr(md) == b"kept"
