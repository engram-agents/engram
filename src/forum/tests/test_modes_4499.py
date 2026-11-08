"""#4499: the forum stops re-creating group-writable files (server, audit, DM dir, packs, backup).

Contract-first (#3045): this file is committed BEFORE the implementation. The contract
is Ariadne's spec 4499-forum-modes.md plus its "Rulings" section:

- S1  forum.db is created 0640 (``& 0o037 == 0``) WHICHEVER path creates it first: the
      startup probe (``probe_deps``), ``main()``'s own connect, or ``create_app``'s
      per-request connect. One cell per path, the file absent beforehand. An existing
      file's mode is left alone. The creator is ONE shared create-if-absent helper
      (``forum.fileperms.create_if_absent``: ``O_CREAT|O_EXCL``, 0o640).
- S2  SQLite gives the rollback journal and the WAL/SHM files the DB file's mode
      (measured, not assumed): a regression guard against an SQLite change.
- S3  the first DM write creates ``dm/`` owner-only; an EXISTING ``dm/`` is never
      chmod'd (the installer manages the live one, I3); ``projects/`` is unchanged.
- S4  forum-audit.jsonl is created 0640 whichever path creates it first (the probe,
      ``audit.write_audit``); an existing file's mode is left alone.
- B1  tools/forum_backup.py: ``forum.sql`` and ``.last-success.json`` come out 0640,
      and the TEMP is already at its final mode when ``os.replace`` runs (a chmod
      after the rename leaves a window on the new inode).
- P1  publishing a pack leaves NO group or other WRITE on the pack dir, the tarball
      or meta.json (the property is "not writable", NOT 0640: packs are content to read).

Mode checks are bit tests, never ``== 0o640``: a default ACL can change a raw mode, and
the bit test does not go vacuous under one. Every family varies the umask and the
starting state of the file or directory (the grid in the builder spec).
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forum import audit, db as forum_db, packs, server  # noqa: E402
from forum.coordination import FileStore  # noqa: E402

UMASKS = [0o002, 0o022, 0o077]
STATES = ["absent", "present-664", "present-600"]


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture
def umask():
    """Set the process umask for one cell; always restored."""
    old = os.umask(0o022)
    os.umask(old)
    applied = []

    def _set(value: int) -> None:
        applied.append(os.umask(value))

    yield _set
    if applied:
        os.umask(applied[0])


def _helper():
    """The shared create-if-absent helper. Imported lazily so a missing module is a
    failing CELL, not a collection error that hides every other cell in the file."""
    return importlib.import_module("forum.fileperms").create_if_absent


def _prepare(path: Path, state: str) -> None:
    """Put *path* in its starting state. A present db is a real, initialised database."""
    if state == "absent":
        assert not path.exists()
        return
    mode = {"present-664": 0o664, "present-600": 0o600}[state]
    conn = sqlite3.connect(str(path))
    forum_db.init_db(conn)
    conn.close()
    os.chmod(path, mode)


def _check_created_or_left_alone(path: Path, state: str, mask: int) -> None:
    assert path.exists(), f"{path.name} was not created"
    if state == "absent":
        # 0640 or narrower (umask 077 narrows the 0o640 request to 0o600): never g+w, never o-anything.
        assert _mode(path) & 0o037 == 0, oct(_mode(path))
        assert _mode(path) & 0o600 == 0o600, oct(_mode(path))  # the owner can still use it
        # EXACT: the group READ bit is part of the contract (co-admins read the db and the
        # audit log), so 0o640 under umask 002/022 and 0o600 under 077 -- not "anything narrower".
        assert _mode(path) == 0o640 & ~mask, f"{oct(_mode(path))} under umask {mask:04o}"
    else:
        want = {"present-664": 0o664, "present-600": 0o600}[state]
        assert _mode(path) == want, f"existing file's mode was changed: {oct(_mode(path))}"


# ---------------------------------------------------------------------------
# S1: forum.db, one cell per creation path
# ---------------------------------------------------------------------------

class _StopMain(Exception):
    pass


def _create_db_via_probe(db_path: Path, audit_path: Path) -> None:
    server.probe_deps(str(db_path), str(audit_path))


def _create_db_via_main(db_path: Path, audit_path: Path, monkeypatch) -> None:
    """Run main() with the probe stubbed out, so main()'s OWN connect is the creator."""
    monkeypatch.setattr(sys, "argv", ["forum", "--db", str(db_path), "--audit", str(audit_path)])
    monkeypatch.delenv("FORUM_GITHUB_INTEGRATION", raising=False)
    monkeypatch.setattr(
        server, "_run_boot_verify",
        lambda *a, **k: {"db": "ok", "audit_log": "ok", "engram_pkg": "ok", "embeddings": "ok"},
    )

    def _stop(*a, **k):
        raise _StopMain

    monkeypatch.setattr(server, "create_app", _stop)
    with pytest.raises(_StopMain):
        server.main()


def _create_db_via_create_app(db_path: Path, audit_path: Path) -> None:
    app = server.create_app(str(db_path), str(audit_path))
    client = app.test_client()
    try:
        client.get("/health")  # any request opens the per-request connection
    except Exception:  # noqa: BLE001 -- a 500 on an uninitialised db is fine; the file is what we read
        pass


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("mask", UMASKS)
@pytest.mark.parametrize("path_kind", ["probe", "main", "create_app"])
def test_s1_db_mode_whichever_path_creates_it_first(tmp_path, monkeypatch, umask, path_kind, mask, state):
    db_path, audit_path = tmp_path / "forum.db", tmp_path / "forum-audit.jsonl"
    _prepare(db_path, state)
    umask(mask)
    if path_kind == "probe":
        _create_db_via_probe(db_path, audit_path)
    elif path_kind == "main":
        _create_db_via_main(db_path, audit_path, monkeypatch)
    else:
        _create_db_via_create_app(db_path, audit_path)
    _check_created_or_left_alone(db_path, state, mask)


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_s1_a_symlink_at_the_db_path_to_an_existing_file_is_left_alone(tmp_path, umask, mask):
    target = tmp_path / "real.db"
    _prepare(target, "present-664")
    link = tmp_path / "forum.db"
    link.symlink_to(target)
    umask(mask)
    _create_db_via_probe(link, tmp_path / "forum-audit.jsonl")
    assert link.is_symlink()
    assert _mode(target) == 0o664


# --- the helper itself: race, symlink, error paths ---------------------------

@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_helper_a_dangling_symlink_is_neither_followed_nor_created_through(tmp_path, umask, mask):
    create_if_absent = _helper()
    target = tmp_path / "elsewhere"
    link = tmp_path / "forum.db"
    link.symlink_to(target)
    umask(mask)
    create_if_absent(str(link))  # must not raise: the path exists (as a link)
    assert link.is_symlink()
    assert not target.exists(), "the helper created a file THROUGH the symlink"


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_helper_tolerates_a_file_appearing_between_the_check_and_the_create(tmp_path, monkeypatch, umask, mask):
    """Another process creates the file after our existence check and before our O_EXCL
    open: FileExistsError must be swallowed, the other process's file left untouched."""
    create_if_absent = _helper()
    fileperms = importlib.import_module("forum.fileperms")
    path = tmp_path / "forum.db"
    real_open = os.open
    state = {"raced": False}

    def racing_open(p, flags, *a, **k):
        if not state["raced"] and str(p) == str(path):
            state["raced"] = True
            fd = real_open(p, os.O_WRONLY | os.O_CREAT, 0o664)  # the other process wins
            os.close(fd)
            os.chmod(p, 0o664)
        return real_open(p, flags, *a, **k)

    monkeypatch.setattr(fileperms.os, "open", racing_open)
    umask(mask)
    create_if_absent(str(path))  # no raise
    assert state["raced"], "the race was never staged: the helper does not os.open the path"
    assert _mode(path) == 0o664


def test_helper_a_missing_parent_raises_and_creates_nothing(tmp_path):
    create_if_absent = _helper()
    with pytest.raises(OSError):
        create_if_absent(str(tmp_path / "no-such-dir" / "forum.db"))
    assert not (tmp_path / "no-such-dir").exists()


def test_probe_reports_missing_and_leaves_no_file_when_the_create_is_refused(tmp_path, monkeypatch):
    fileperms = importlib.import_module("forum.fileperms")

    def refuse(*a, **k):
        raise PermissionError(13, "refused")

    monkeypatch.setattr(fileperms.os, "open", refuse)
    db_path = tmp_path / "forum.db"
    results = server.probe_deps(str(db_path), str(tmp_path / "forum-audit.jsonl"))
    assert results["db"] == "missing"
    assert not db_path.exists()


# ---------------------------------------------------------------------------
# S2: the journal / WAL follow the db file's mode (measured on this SQLite)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_s2_rollback_journal_and_wal_follow_the_db_file_mode(tmp_path, umask, mask):
    umask(mask)
    path = tmp_path / "s2.db"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    os.close(fd)
    assert _mode(path) & 0o037 == 0

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (1)")
    journal = Path(str(path) + "-journal")
    assert journal.exists(), "no rollback journal while a write transaction is open"
    assert _mode(journal) & 0o037 == 0, oct(_mode(journal))
    conn.execute("COMMIT")

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (2)")
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        assert side.exists(), f"no {suffix} file in WAL mode"
        assert _mode(side) & 0o037 == 0, f"{suffix}: {oct(_mode(side))}"
    conn.execute("COMMIT")
    conn.close()


# ---------------------------------------------------------------------------
# S4: forum-audit.jsonl, one cell per creation path
# ---------------------------------------------------------------------------

def _write_one_audit_line(audit_path: Path) -> None:
    audit.write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "hello", path=str(audit_path))


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("mask", UMASKS)
@pytest.mark.parametrize("path_kind", ["probe", "write_audit"])
def test_s4_audit_mode_whichever_path_creates_it_first(tmp_path, umask, path_kind, mask, state):
    audit_path = tmp_path / "forum-audit.jsonl"
    if state != "absent":
        audit_path.write_text('{"old": 1}\n')
        os.chmod(audit_path, {"present-664": 0o664, "present-600": 0o600}[state])
    umask(mask)
    if path_kind == "probe":
        _create_db_via_probe(tmp_path / "forum.db", audit_path)
    else:
        _write_one_audit_line(audit_path)
    _check_created_or_left_alone(audit_path, state, mask)


def test_s4_an_append_keeps_the_existing_content(tmp_path, umask):
    audit_path = tmp_path / "forum-audit.jsonl"
    audit_path.write_text('{"old": 1}\n')
    os.chmod(audit_path, 0o664)
    umask(0o002)
    _write_one_audit_line(audit_path)
    lines = audit_path.read_text().splitlines()
    assert lines[0] == '{"old": 1}' and len(lines) == 2
    assert _mode(audit_path) == 0o664


# ---------------------------------------------------------------------------
# S3: dm/ is created owner-only, an existing dm/ is never chmod'd, projects/ unchanged
# ---------------------------------------------------------------------------

def _append(store) -> None:
    store.append_dm("ariadne", "sol", "hello", seq=1, ts="2026-11-08T00:00:00Z")


def _baton(project_id: str) -> str:
    return (
        "---\n"
        f"project_id: {project_id}\ntitle: {project_id} title\nstatus: in-progress\n"
        "turn: ariadne\nturn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "handed off"\nparticipants: [ariadne, sol]\n'
        "---\n"
        f"# {project_id}\n\nbody\n"
    )


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_s3_first_dm_write_creates_dm_owner_only(tmp_path, umask, mask):
    store = FileStore(tmp_path)
    umask(mask)
    _append(store)
    assert _mode(tmp_path / "dm") & 0o077 == 0, oct(_mode(tmp_path / "dm"))


@pytest.mark.parametrize("mask", [0o002, 0o022])
@pytest.mark.parametrize("existing", [0o700, 0o755])
def test_s3_an_existing_dm_dir_is_never_chmodded(tmp_path, umask, mask, existing):
    (tmp_path / "dm").mkdir()
    os.chmod(tmp_path / "dm", existing)
    store = FileStore(tmp_path)
    umask(mask)
    _append(store)
    assert _mode(tmp_path / "dm") == existing
    assert len(list((tmp_path / "dm").glob("*.md"))) == 1


@pytest.mark.parametrize("mask", [0o002, 0o022])
@pytest.mark.parametrize("dm_state", ["absent", "present-755"])
def test_s3_projects_dir_keeps_todays_mode(tmp_path, umask, mask, dm_state):
    if dm_state == "present-755":
        (tmp_path / "dm").mkdir()
        os.chmod(tmp_path / "dm", 0o755)
    store = FileStore(tmp_path)
    umask(mask)
    _append(store)
    store.write_project("PR-1", _baton("PR-1"), seq=2)
    assert _mode(tmp_path / "projects") == 0o777 & ~mask


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_s3_a_failing_chmod_of_the_new_dm_dir_is_loud_and_leaves_nothing_wider(tmp_path, monkeypatch, umask, mask):
    store = FileStore(tmp_path)
    dm = tmp_path / "dm"
    real_chmod = os.chmod

    def failing(p, m, *a, **k):
        if Path(p) == dm:
            raise PermissionError(1, "chmod refused")
        return real_chmod(p, m, *a, **k)

    from forum.coordination import store_file
    monkeypatch.setattr(store_file.os, "chmod", failing)
    umask(mask)
    with pytest.raises(OSError):
        _append(store)
    assert _mode(dm) & 0o077 == 0, "a refused chmod left a dm/ wider than owner-only"
    assert list(dm.glob("*.md")) == [], "a thread file was written into a dm/ we could not secure"


# ---------------------------------------------------------------------------
# B1: tools/forum_backup.py writers
# ---------------------------------------------------------------------------

def _backup_module():
    return importlib.import_module("tools.forum_backup")


def _spy_replace(fb, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst, *a, **k):
        seen.append(_mode(src))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(fb.os, "replace", spy)
    return seen


def _write_forum_sql(fb, dest: Path) -> None:
    fb._write_atomic("-- dump\n", dest)


def _write_stamp(fb, backup_dir: Path) -> None:
    fb._write_success_stamp(backup_dir, committed=False)


WRITERS = {
    "forum.sql": (_write_forum_sql, "forum.sql"),
    "stamp": (_write_stamp, ".last-success.json"),
}


def _call_writer(fb, kind: str, backup_dir: Path) -> Path:
    fn, name = WRITERS[kind]
    dest = backup_dir / name
    if kind == "forum.sql":
        fn(fb, dest)
    else:
        fn(fb, backup_dir)
    return dest


@pytest.mark.parametrize("dest_state", ["absent", "present-664"])
@pytest.mark.parametrize("mask", [0o002, 0o022])
@pytest.mark.parametrize("kind", list(WRITERS))
def test_b1_output_is_0640_and_the_temp_is_at_its_final_mode_at_replace_time(
    tmp_path, monkeypatch, umask, kind, mask, dest_state
):
    fb = _backup_module()
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    dest = backup_dir / WRITERS[kind][1]
    if dest_state == "present-664":
        dest.write_text("old\n")
        os.chmod(dest, 0o664)
    seen = _spy_replace(fb, monkeypatch)
    umask(mask)
    _call_writer(fb, kind, backup_dir)
    assert seen, "os.replace was never reached"
    assert all(m & 0o037 == 0 for m in seen), f"temp was wider than 0640 at rename time: {[oct(m) for m in seen]}"
    assert _mode(dest) & 0o037 == 0, oct(_mode(dest))
    assert _mode(dest) & 0o600 == 0o600
    # EXACT 0o640: the group read bit (co-admins read the backup) at rename time AND on the final file.
    assert all(m == 0o640 for m in seen), f"temp at rename time: {[oct(m) for m in seen]}"
    assert _mode(dest) == 0o640, oct(_mode(dest))


@pytest.mark.parametrize("kind", list(WRITERS))
def test_b1_a_failing_chmod_is_loud_leaves_no_temp_and_keeps_the_old_file(tmp_path, monkeypatch, umask, kind):
    fb = _backup_module()
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    dest = backup_dir / WRITERS[kind][1]
    dest.write_text("old\n")
    os.chmod(dest, 0o664)

    def refuse(*a, **k):
        raise PermissionError(1, "chmod refused")

    monkeypatch.setattr(fb.os, "fchmod", refuse)
    monkeypatch.setattr(fb.os, "chmod", refuse)
    umask(0o002)
    with pytest.raises(OSError):
        _call_writer(fb, kind, backup_dir)
    assert dest.read_text() == "old\n"
    assert [p.name for p in backup_dir.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("kind", list(WRITERS))
def test_b1_a_failing_replace_propagates_and_leaves_no_temp(tmp_path, monkeypatch, umask, kind):
    fb = _backup_module()
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(fb.os, "replace", boom)
    umask(0o002)
    with pytest.raises(OSError, match="replace failed"):
        _call_writer(fb, kind, backup_dir)
    assert [p.name for p in backup_dir.iterdir() if p.name.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# P1: published packs are not group- or other-writable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mask", [0o002, 0o022])
@pytest.mark.parametrize("source_mode", [0o664, 0o600])
def test_p1_a_published_pack_has_no_group_or_other_write(tmp_path, umask, mask, source_mode):
    tarball = tmp_path / "upload.tar.gz"
    tarball.write_bytes(b"not really a tarball, store_pack only copies it")
    os.chmod(tarball, source_mode)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    os.chmod(packs_dir, 0o755)
    umask(mask)
    pack_dir = packs.store_pack(packs_dir, "alice__demo__v1", tarball)
    packs.write_pack_meta(
        pack_dir, pack_id="alice__demo__v1", author="alice", name="demo", version=1,
        uploaded_at="2026-11-08T00:00:00Z", root_count=0, node_count=0, edge_count=0,
    )
    for p in (pack_dir, pack_dir / "package.tar.gz", pack_dir / "meta.json"):
        assert _mode(p) & 0o022 == 0, f"{p.name}: {oct(_mode(p))}"
    assert (pack_dir / "package.tar.gz").read_bytes() == tarball.read_bytes()
    assert os.access(pack_dir / "meta.json", os.R_OK)
    # Packs are content to be READ: group AND other keep the read bit on both files, and
    # the (newly created) pack dir keeps read+search for both.
    for p in (pack_dir / "package.tar.gz", pack_dir / "meta.json"):
        assert _mode(p) & 0o044 == 0o044, f"{p.name}: {oct(_mode(p))}"
    assert _mode(pack_dir) & 0o055 == 0o055, oct(_mode(pack_dir))


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_p1_an_existing_pack_dir_is_left_alone(tmp_path, umask, mask):
    tarball = tmp_path / "upload.tar.gz"
    tarball.write_bytes(b"x")
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    os.chmod(packs_dir, 0o755)
    pre = packs_dir / "alice__demo__v1"
    pre.mkdir()
    os.chmod(pre, 0o750)
    umask(mask)
    pack_dir = packs.store_pack(packs_dir, "alice__demo__v1", tarball)
    assert pack_dir == pre
    assert _mode(pre) == 0o750, oct(_mode(pre))


def test_p1_the_source_upload_is_not_modified_by_publishing(tmp_path, umask):
    tarball = tmp_path / "upload.tar.gz"
    tarball.write_bytes(b"x")
    os.chmod(tarball, 0o664)
    (tmp_path / "packs").mkdir()
    umask(0o002)
    packs.store_pack(tmp_path / "packs", "a__b__v1", tarball)
    assert _mode(tarball) == 0o664
