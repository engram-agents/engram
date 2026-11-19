"""#2652 unit 04 follow-up (part of #2652) -- name a healthy store the reader cannot open, and the step-6 owner note.

A WAL store whose folder the reader cannot write fails closed (a `mode=ro` open needs the -wal/-shm sidecars beside
the store, and cannot create them). That is a folder problem; the store is healthy. Saying "does not read as a
store ... restore it from backup" over a healthy store invites the one harmful move, so it gets its own exit.

Contract (each cell written before the change, red first):

  A   direct path, WAL store, no sidecars, folder not writable, a real SQLITE_READONLY error: node-count / ids /
      axiom-count exit 4, stdout empty, ONE stderr line saying the folder cannot be written, a WAL store needs its
      -wal/-shm beside it, and the store is not shown to be damaged
  B   a real code-14 error ("unable to open database file") on a store whose header says WAL with no -shm beside
      it: exit 4, the line says a read-only open could not create the -shm, and does NOT say "permissions" (14 also
      fires in writable folders on some builds). Any other code-14 case stays exit 1 with today's wording
  C   every other sqlite3.Error stays exit 1 with today's wording (garbage bytes pinned)
  D   install.sh step 6: exit 4 has its own FAIL (store not shown to be damaged, do not restore from backup yet, fix the folder or
      start the owner); exit 3 and other non-zero keep their messages
  E   the step-6 header comment says an owner can serve a zero-byte store, so that case stops at the owner refusal
  F   the docstring lists exit 4

Every sqlite error here is PROVOKED (a real WAL store under a chmod-ed folder, a real open of a missing directory),
never constructed: a hand-built sqlite3.OperationalError has no `sqlite_errorcode`. The one copy made without the
attribute is made FROM a real error, to stand in for Python 3.10, which does not have it.

Mutation controls (run by hand, recorded in the PR):
  M1  exit 4 mapped back to 1                         -> A and D red
  M2  code 14 treated as the folder problem always    -> B's writable-folder / non-WAL controls red
  M3  getattr(...) replaced by exc.sqlite_errorcode   -> the 3.10 stand-in cell red (AttributeError)
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_install_sh_4097 import Sandbox, sb  # noqa: E402,F401  (`sb` is a fixture)
from test_store_read_2652_unit02 import (  # noqa: E402,F401  (`home` is a fixture)
    TOOL,
    build_store,
    home,
    load_tool,
    run,
    stderr_lines,
)

INSTALL_SH = TOOL.parent / "install.sh"
BUILD = "Python %s, SQLite %s" % (sys.version.split()[0], sqlite3.sqlite_version)
COMMANDS = [["node-count"], ["axiom-count"], ["ids", "--since", ""]]


# ------------------------------------------------------------------------------------------ helpers

def _make_wal_store(home: Path) -> Path:
    """A real, clean-closed WAL store with no -wal / -shm beside it."""
    db = build_store(home)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL").fetchall()
    con.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(str(db) + suffix)
        except FileNotFoundError:
            pass
    assert db.read_bytes()[18:20] == b"\x02\x02", "the fixture must be a WAL store"
    assert not (Path(str(db) + "-shm")).exists()
    return db


def _read_error(db: Path):
    """The error a `mode=ro` open then SELECT really raises here, or None if it reads."""
    try:
        con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            con.execute("SELECT COUNT(*) FROM nodes").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return exc
    return None


def _is_readonly_shape(exc) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) == 8
    return str(exc) == "attempt to write a readonly database"


def _is_cantopen_shape(exc) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == 14
    return str(exc) == "unable to open database file"


@pytest.fixture
def locked_wal(home):
    """(db path, the real error its read raises): a WAL store, no sidecars, the folder chmod 0555. Skips, naming the
    build, when this host does not produce the measured read-only shape. The folder is restored on teardown."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: a chmod does not bind")
    db = _make_wal_store(home)
    os.chmod(home, 0o555)
    try:
        if os.access(home, os.W_OK):
            pytest.skip("this filesystem ignores the folder chmod")
        err = _read_error(db)
        if err is None or not _is_readonly_shape(err):
            pytest.skip("not the measured read-only shape on this build (%s): got %r" % (BUILD, err))
        yield db, err
    finally:
        os.chmod(home, 0o755)


def _missing_dir_error(tmp: Path):
    """A REAL code-14 error: a read-only open of a path whose folder does not exist."""
    err = _read_error(tmp / "no-such-folder" / "knowledge.db")
    if err is None or not _is_cantopen_shape(err):
        pytest.skip("this build does not raise 'unable to open database file' here (%s): got %r" % (BUILD, err))
    return err


# ------------------------------------------------------------------------------------------ A

@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: a[0])
def test_a_a_wal_store_in_an_unwritable_folder_exits_4_and_names_the_folder(locked_wal, home, capsys, argv):
    rc, out, err, _ = run(home, argv, capsys)
    assert rc == 4, (rc, out, err)
    assert out == "", out
    lines = stderr_lines(err)
    bad = [ln for ln in lines if not ln.startswith("store_read: path=")]
    assert len(bad) == 1, err
    line = bad[0]
    assert "cannot be written" in line, line
    assert "-wal" in line and "-shm" in line, line
    assert "not shown to be damaged" in line, line
    assert "does not read as a store" not in line, line


def test_a_the_classifier_reads_the_real_code_not_only_the_message(locked_wal, home):
    _, real = locked_wal
    mod = load_tool()
    assert mod.unreadable_but_healthy(real, locked_wal[0]) is not None
    if hasattr(real, "sqlite_errorcode"):  # Python 3.11+: the real error carries the code
        assert (real.sqlite_errorcode & 0xFF) == 8


def test_a_python_310_stand_in_without_the_code_attribute_classifies_by_message(locked_wal, home):
    """M3: a 3.10 error has no `sqlite_errorcode`. The stand-in is built FROM a real raised error."""
    db, real = locked_wal
    stripped = sqlite3.OperationalError(str(real))
    assert not hasattr(stripped, "sqlite_errorcode"), "the stand-in must lack the attribute"
    mod = load_tool()
    line = mod.unreadable_but_healthy(stripped, db)
    assert line is not None and "cannot be written" in line


# ------------------------------------------------------------------------------------------ B

def test_b_code_14_on_a_wal_store_with_no_shm_is_named_and_says_nothing_of_permissions(home):
    real = _missing_dir_error(home)  # a real code-14 error
    db = _make_wal_store(home)       # WAL header, no -shm, writable folder
    line = load_tool().unreadable_but_healthy(real, db)
    assert line is not None, "code 14 on a WAL store with no -shm is the could-not-create-the--shm case"
    assert "-shm" in line and "read-only open" in line, line
    assert "permission" not in line.lower(), line


def test_b_code_14_by_message_alone_classifies_the_same(home):
    real = _missing_dir_error(home)
    stripped = sqlite3.OperationalError(str(real))
    assert not hasattr(stripped, "sqlite_errorcode")
    db = _make_wal_store(home)
    assert load_tool().unreadable_but_healthy(stripped, db) is not None


def test_b_control_code_14_on_a_non_wal_store_is_not_classified(home):
    real = _missing_dir_error(home)
    db = build_store(home)  # delete-mode journal: header bytes are 1, 1
    assert db.read_bytes()[18:20] != b"\x02\x02"
    assert load_tool().unreadable_but_healthy(real, db) is None


def test_b_control_code_14_with_a_shm_beside_the_store_is_not_classified(home):
    real = _missing_dir_error(home)
    db = _make_wal_store(home)
    Path(str(db) + "-shm").write_bytes(b"\0" * 32768)
    assert load_tool().unreadable_but_healthy(real, db) is None


def test_b_control_code_14_on_a_missing_store_is_not_classified(home):
    real = _missing_dir_error(home)
    assert load_tool().unreadable_but_healthy(real, home / "knowledge.db") is None


def test_b_end_to_end_where_this_build_really_raises_code_14_in_a_writable_folder(home, capsys):
    """Build-conditional: SQLite 3.51 on Apple's Python raises code 14 for a WAL store with no sidecars even in a
    writable folder. Where this build does not, there is nothing to assert."""
    db = _make_wal_store(home)
    err = _read_error(db)
    if err is None or not _is_cantopen_shape(err):
        pytest.skip("this build does not raise code 14 on a read-only open of a sidecar-less WAL store (%s): got %r"
                    % (BUILD, err))
    rc, out, e, _ = run(home, ["node-count"], capsys)
    assert rc == 4 and out == "", (rc, out, e)
    assert "-shm" in e and "permission" not in e.lower(), e


# ------------------------------------------------------------------------------------------ C

def test_c_garbage_bytes_keep_exit_1_and_todays_wording(home, capsys):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database at all" * 200)
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 1 and out == "", (rc, out, err)
    assert "does not read as a store" in err, err


def test_c_an_unrelated_real_error_is_not_classified(home):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database at all" * 200)
    real = _read_error(home / "knowledge.db")
    assert real is not None
    assert load_tool().unreadable_but_healthy(real, home / "knowledge.db") is None


# ------------------------------------------------------------------------------------------ D

def _stub_exit(sb, code):
    (sb.tools_src / "store_read.py").write_text(
        "import sys\nsys.stderr.write('store_read: stub\\n')\nsys.exit(%d)\n" % code)


def _step6_fail(sb):
    r = sb.run("--yes")
    assert r.returncode != 0, r.all
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("FAIL: [step 6]")]
    assert lines, r.all
    return r, lines[0], sb.calls_starting("bootstrap ")


def test_d_installer_exit_4_has_its_own_fail_and_never_says_restore_from_backup(sb):
    _stub_exit(sb, 4)
    sb.make_db(3)
    r, fail_line, boot = _step6_fail(sb)
    assert "not shown to be damaged" in fail_line and "restore it from backup yet" in fail_line, fail_line
    assert "do not restore" in fail_line.lower(), fail_line
    assert "permissions" in fail_line and str(sb.engram) in fail_line, fail_line
    assert "owner" in fail_line, fail_line
    assert "release any lock" not in fail_line, fail_line
    assert boot == [], "a healthy store the reader cannot open must never be re-seeded"


def test_d_exit_3_keeps_its_owner_refusal_message(sb):
    _stub_exit(sb, 3)
    sb.make_db(3)
    r, fail_line, boot = _step6_fail(sb)
    assert "owner refused" in fail_line and "not damaged" not in fail_line, fail_line
    assert boot == []


def test_d_any_other_non_zero_keeps_the_lock_or_backup_message(sb):
    _stub_exit(sb, 1)
    sb.make_db(3)
    r, fail_line, boot = _step6_fail(sb)
    assert "release any lock" in fail_line and "not damaged" not in fail_line, fail_line
    assert boot == []


# ------------------------------------------------------------------------------------------ E, F

def test_e_the_step_6_header_says_an_owner_can_serve_a_zero_byte_store():
    text = INSTALL_SH.read_text(encoding="utf-8")
    header = text[text.index("# ── Step 6: bootstrap"):text.index('begin_step 6 "bootstrap"')]
    assert "zero-byte" in header, header
    assert "owner" in header and "exit 3" in header, header


def test_f_the_docstring_lists_exit_4():
    doc = load_tool().__doc__
    tail = doc[doc.index("Exit codes:"):]
    assert re.search(r"\b4 ", tail), tail
