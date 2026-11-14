"""Admin CLI creators decide file/dir modes themselves, not via the caller's umask (#4530).

Follow-up to #4499: ``_open_db`` and ``categories export --out`` used to create files at
whatever umask the invoking shell had (0664 / 0775 under umask 002).
"""

from __future__ import annotations

import os
import stat

import pytest

from forum.admin import _open_db
from forum.admin import main as admin_main


@pytest.fixture
def umask002():
    old = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(old)


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_absent_db_created_private(tmp_path, umask002):
    db = tmp_path / "forum.db"
    _open_db(str(db)).close()
    assert db.exists()
    assert _mode(db) & 0o037 == 0, oct(_mode(db))


def test_dangling_symlink_db_target_private(tmp_path, umask002):
    target = tmp_path / "real.db"
    link = tmp_path / "forum.db"
    os.symlink(target, link)
    conn = _open_db(str(link))
    # init ran: the db is usable through the link
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.close()
    assert target.exists()
    assert _mode(target) & 0o037 == 0, oct(_mode(target))


def test_absent_db_dir_created_0750(tmp_path, umask002):
    db = tmp_path / "newdir" / "forum.db"
    _open_db(str(db)).close()
    assert _mode(tmp_path / "newdir") == 0o750, oct(_mode(tmp_path / "newdir"))


def test_absent_nested_leaf_dir_0750(tmp_path, umask002):
    # os.makedirs' mode reaches the leaf only; intermediates follow the umask.
    db = tmp_path / "a" / "b" / "forum.db"
    _open_db(str(db)).close()
    assert _mode(tmp_path / "a" / "b") == 0o750


def test_existing_dir_mode_untouched(tmp_path, umask002):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o775)
    _open_db(str(d / "forum.db")).close()
    assert _mode(d) == 0o775


def test_existing_db_mode_untouched(tmp_path, umask002):
    db = tmp_path / "forum.db"
    _open_db(str(db)).close()
    os.chmod(db, 0o660)
    _open_db(str(db)).close()
    assert _mode(db) == 0o660


def test_export_absent_file_created_private(tmp_path, umask002):
    db = tmp_path / "forum.db"
    out = tmp_path / "cats.json"
    rc = _run_export(db, out)
    assert rc == 0
    assert out.read_text().strip().startswith("[")
    assert _mode(out) & 0o037 == 0, oct(_mode(out))


def test_export_existing_file_overwritten_mode_kept(tmp_path, umask002):
    db = tmp_path / "forum.db"
    out = tmp_path / "cats.json"
    out.write_text("old")
    os.chmod(out, 0o660)
    assert _run_export(db, out) == 0
    assert out.read_text().strip().startswith("[")
    assert _mode(out) == 0o660


def test_export_dangling_symlink_target_private(tmp_path, umask002):
    db = tmp_path / "forum.db"
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    os.symlink(target, link)
    assert _run_export(db, link) == 0
    assert target.exists()
    assert _mode(target) & 0o037 == 0, oct(_mode(target))


def test_export_missing_parent_raises_nothing_created(tmp_path, umask002):
    db = tmp_path / "forum.db"
    out = tmp_path / "nope" / "cats.json"
    with pytest.raises(OSError):
        _run_export(db, out)
    assert not (tmp_path / "nope").exists()


def _run_export(db, out) -> int:
    return admin_main(["--db", str(db), "export", "--out", str(out)])


def test_memory_db_creates_nothing_in_cwd(tmp_path, monkeypatch):
    # ":memory:" is sqlite's in-memory db, not a file name: no empty ':memory:' file
    # may be left behind (dev creates nothing).
    monkeypatch.chdir(tmp_path)
    conn = _open_db(":memory:")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()
    assert os.listdir(tmp_path) == []


def test_export_to_dev_stdout_pipe(tmp_path):
    # /dev/stdout under a pipe realpaths to a nonexistent /proc/<pid>/fd/pipe:[n]; the
    # export must not try to create that (os.path.exists follows the link, so skip).
    import json
    import subprocess
    import sys

    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    code = (
        "from forum.admin import main; "
        "raise SystemExit(main(['--db', 'f.db', 'export', '--out', '/dev/stdout']))"
    )
    env = dict(os.environ, PYTHONPATH=src)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    text = proc.stdout.decode()
    # main() prints a trailing "exported N categories to ..." line after the JSON
    decoded, _ = json.JSONDecoder().raw_decode(text)
    assert isinstance(decoded, list)
