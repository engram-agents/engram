"""Restore must rebuild the derived data the backup strips (engram-alpha#868).

Contract (written before the fix):

  1. tools/forum_backup.py strips posts_fts, vec_* and the embedding columns from
     the dump, so a DB restored from forum.sql and started has NO search index
     and NO embeddings: search returns nothing until they are rebuilt.
  2. The README's ``## Restore`` section therefore runs tools/forum_regen_derived.py
     AFTER the sqlite3 restore and BEFORE the service start.
  3. The installer snapshots forum_regen_derived.py and forum_backfill_embeddings.py
     into <service-dir>/app/tools/ (real files, like forum_backup.py), so the
     README command works from the deployed layout, never from a checkout.
  4. Round trip: dump -> restore -> init_db leaves FTS empty (the defect); the
     regen tool, run from the deployed layout, makes FTS match again.  The
     embedding phase is switched off (FORUM_NO_EMBEDDINGS) so the cell needs no
     model; FTS is rebuilt in an earlier step than the backfill.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forum.tests.test_deploy_shape_3568 import _install

_REPO = Path(__file__).resolve().parents[3]
README = _REPO / "src" / "forum" / "deploy" / "README.md"
INSTALLER = _REPO / "src" / "forum" / "deploy" / "install-forum-service.sh"
TOOLS = ("forum_regen_derived.py", "forum_backfill_embeddings.py")


# ------------------------------------------------------------ installer ----

class TestInstallerSnapshotsRegenTools:

    @pytest.mark.parametrize("name", TOOLS)
    def test_tool_is_snapshotted_as_a_real_file_identical_to_the_source(self, tmp_path, name):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        snap = ctx.svc / "app" / "tools" / name
        assert snap.is_file() and not snap.is_symlink()
        assert snap.read_bytes() == (_REPO / "tools" / name).read_bytes()

    @pytest.mark.parametrize("name", TOOLS)
    def test_dry_run_names_the_snapshot_and_writes_nothing(self, tmp_path, name):
        ctx = _install(tmp_path, dry_run=True)
        assert ctx.r.returncode == 0, ctx.both
        assert f"{ctx.svc}/app/tools/{name}" in ctx.out
        assert not (ctx.svc / "app" / "tools" / name).exists()

    @pytest.mark.parametrize("name", TOOLS)
    def test_the_installer_checks_the_source_tool_exists_up_front(self, name):
        assert f'[ -f "$SRC/tools/{name}" ]' in INSTALLER.read_text()


# --------------------------------------------------------------- README ----

def _restore_section() -> str:
    m = re.search(r"^## Restore\n(.*?)(?=^## )", README.read_text(), re.S | re.M)
    assert m, "README has no '## Restore' section"
    return m.group(1)


class TestReadmeRestoreRunsRegen:

    def test_regen_sits_between_the_sqlite3_restore_and_the_service_start(self):
        sec = _restore_section()
        restore = sec.index('sqlite3 "$D/forum.db"')
        regen = sec.index('"$SVC/.venv/bin/python" "$SVC/app/tools/forum_regen_derived.py"')
        start = sec.index("systemctl --user start")
        assert restore < regen < start

    def test_regen_runs_under_the_forum_venv_from_the_snapshot(self):
        sec = re.sub(r"\s*\\\n\s*", " ", _restore_section())
        assert re.search(
            r'"\$SVC/\.venv/bin/python" "\$SVC/app/tools/forum_regen_derived\.py"'
            r' --db \S+/forum\.db', sec), sec
        assert "SVC=<service-dir>" in sec, sec

    def test_the_file_table_lists_both_snapshots(self):
        text = README.read_text()
        for name in TOOLS:
            assert f"`tools/{name}`" in text


# ------------------------------------------------------------ round trip ----

def _deployed_layout(root: Path) -> Path:
    """$APP/forum + $APP/tools, as the installer lays them out."""
    app = root / "app"
    shutil.copytree(_REPO / "src" / "forum", app / "forum",
                    ignore=shutil.ignore_patterns("tests", "deploy", "__pycache__"))
    (app / "tools").mkdir()
    for name in TOOLS:
        shutil.copy(_REPO / "tools" / name, app / "tools" / name)
    return app


def test_restored_dump_has_no_search_index_until_regen_runs(tmp_path):
    sys.path.insert(0, str(_REPO / "src"))
    sys.path.append(str(_REPO / "tools"))
    try:
        from forum import db as fdb
        import forum_backup
    finally:
        sys.path.remove(str(_REPO / "src"))
    src = tmp_path / "src.db"
    c = sqlite3.connect(src)
    fdb.init_db(c)
    agent = fdb.upsert_agent(c, "tester")
    slug = c.execute("SELECT slug FROM categories LIMIT 1").fetchone()[0]
    fdb.create_thread(c, agent, slug, "Quokka title", "the quokka sat on a wombat")
    c.commit()
    match = "SELECT count(*) FROM posts_fts WHERE posts_fts MATCH 'quokka'"
    assert c.execute(match).fetchone()[0] == 1  # the live DB searches fine
    c.close()

    sql = forum_backup._dump_to_sql(str(src))
    restored = tmp_path / "restored.db"
    r = sqlite3.connect(restored)
    r.executescript(sql)  # `sqlite3 forum.db < forum.sql`
    fdb.init_db(r)        # what the server does on start
    r.commit()
    assert r.execute(match).fetchone()[0] == 0, "defect gone? the dump now keeps FTS"
    r.close()

    app = _deployed_layout(tmp_path)
    env = {**os.environ, "FORUM_NO_EMBEDDINGS": "1"}
    subprocess.run([sys.executable, str(app / "tools" / "forum_regen_derived.py"),
                    "--db", str(restored)], capture_output=True, text=True,
                   env=env, cwd=str(tmp_path), timeout=120)  # exit code ignored: no model here
    r = sqlite3.connect(restored)
    assert r.execute(match).fetchone()[0] == 1
    r.close()


# ------------------------------------------------ the Restore block, RUN ----
# The block's own commands are executed with bash against temp paths.  A restore
# that fails must never be followed by a service start that looks like success.

_MATCH = "SELECT count(*) FROM posts_fts WHERE posts_fts MATCH 'quokka'"
_STUB_BACKFILL = (
    "def run_backfill(db, dry_run=False):\n"
    "    return {'posts_embedded': 0, 'threads_updated': 0, 'fts_rows_after_rebuild': 0}\n"
)


def _restore_block() -> str:
    m = re.search(r"```bash\n(.*?)```", _restore_section(), re.S)
    assert m, "no fenced bash block under '## Restore'"
    return m.group(1)


def _make_dump(tmp_path: Path) -> Path:
    """A live forum.db with a post, and backup/forum.sql dumped from it."""
    sys.path.insert(0, str(_REPO / "src"))
    sys.path.append(str(_REPO / "tools"))
    try:
        from forum import db as fdb
        import forum_backup
    finally:
        sys.path.remove(str(_REPO / "src"))
    live = tmp_path / "forum"
    (live / "backup").mkdir(parents=True)
    db = live / "forum.db"
    c = sqlite3.connect(db)
    fdb.init_db(c)
    agent = fdb.upsert_agent(c, "tester")
    slug = c.execute("SELECT slug FROM categories LIMIT 1").fetchone()[0]
    fdb.create_thread(c, agent, slug, "Quokka title", "the quokka sat on a wombat")
    c.commit()
    (live / "backup" / "forum.sql").write_text(forum_backup._dump_to_sql(str(db)))
    c.close()
    return live


def _run_block(tmp_path: Path, *, model: bool, corrupt: bool = False,
               block: str | None = None):
    live = _make_dump(tmp_path)
    # Reopen the live DB in WAL mode with an open writer, so -wal/-shm exist.
    keep = sqlite3.connect(live / "forum.db")
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("PRAGMA wal_autocheckpoint=0")
    keep.execute("CREATE TABLE IF NOT EXISTS marker(x)")
    keep.execute("INSERT INTO marker VALUES ('old-db')")
    keep.commit()
    assert (live / "forum.db-wal").exists() and (live / "forum.db-shm").exists()
    if corrupt:
        (live / "backup" / "forum.sql").write_text("CREATE TABLE ((( not sql at all;\n")

    ctx = _install(tmp_path)
    assert ctx.r.returncode == 0, ctx.both
    svc = ctx.svc
    if model:  # the model is available: the embedding phase is a stand-in that succeeds
        (svc / "app" / "tools" / "forum_backfill_embeddings.py").write_text(_STUB_BACKFILL)
    venv_py = svc / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    venv_py.chmod(0o755)

    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    calls = tmp_path / "systemctl.calls"
    sc = bindir / "systemctl"
    sc.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    sc.chmod(0o755)

    script = (block or _restore_block()).replace(
        "/home/agents-shared/forum", str(live)).replace("<service-dir>", str(svc))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("FORUM_NO_EMBEDDINGS", None)
    if not model:
        env["FORUM_NO_EMBEDDINGS"] = "1"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, cwd=str(tmp_path), timeout=120)
    keep.close()
    log = calls.read_text().splitlines() if calls.exists() else []
    return SimpleNamespace(r=r, live=live, log=log, both=r.stdout + r.stderr)


def _pre(live: Path, name: str) -> list[Path]:
    return sorted(live.glob(f"{name}.pre-restore-*"))


class TestRestoreBlockRuns:

    def test_a_good_restore_moves_the_old_db_aside_restores_and_starts_last(self, tmp_path):
        x = _run_block(tmp_path, model=True)
        assert x.r.returncode == 0, x.both
        # the old DB is kept, with its side files, under the timestamped name
        for name in ("forum.db", "forum.db-wal", "forum.db-shm"):
            assert len(_pre(x.live, name)) == 1, (name, sorted(p.name for p in x.live.iterdir()))
        # the pre-restore files hold the old data: reassemble them under the original
        # names (the -wal carries the marker row) in a scratch dir and read it
        scratch = tmp_path / "reassembled"
        scratch.mkdir()
        for name in ("forum.db", "forum.db-wal", "forum.db-shm"):
            shutil.copy(_pre(x.live, name)[0], scratch / name)
        old = sqlite3.connect(scratch / "forum.db")
        assert old.execute("SELECT x FROM marker").fetchone()[0] == "old-db"
        old.close()
        # the new DB has the post, is searchable, and is 0640
        new = sqlite3.connect(x.live / "forum.db")
        assert new.execute(_MATCH).fetchone()[0] == 1
        assert new.execute("SELECT count(*) FROM sqlite_master WHERE name='marker'").fetchone()[0] == 0
        new.close()
        assert (x.live / "forum.db").stat().st_mode & 0o777 == 0o640
        # stop first, start last
        assert x.log == ["--user stop engram-forum.service", "--user start engram-forum.service"]

    def test_a_failed_restore_does_not_start_the_service_and_keeps_the_old_db(self, tmp_path):
        x = _run_block(tmp_path, model=True, corrupt=True)
        assert x.r.returncode != 0, x.both
        assert x.log == ["--user stop engram-forum.service"], x.log
        assert len(_pre(x.live, "forum.db")) == 1
        # the README's own put-it-back block restores the old DB, side files included
        stamp = _pre(x.live, "forum.db")[0].name.split(".pre-restore-")[1]
        rec = re.findall(r"```bash\n(.*?)```", _restore_section(), re.S)[1]
        rec = rec.replace("/home/agents-shared/forum", str(x.live)).replace("<stamp>", stamp)
        r = subprocess.run(["bash", "-c", rec], capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{tmp_path / 'stubbin'}:{os.environ['PATH']}"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert not _pre(x.live, "forum.db")
        c = sqlite3.connect(x.live / "forum.db")
        assert c.execute("SELECT x FROM marker").fetchone()[0] == "old-db"
        c.close()
        assert list(x.live.glob("forum.db*.failed-restore-*"))

    def test_model_off_regen_exit_stops_the_block_after_the_text_index_is_rebuilt(self, tmp_path):
        x = _run_block(tmp_path, model=False)
        assert x.r.returncode != 0, x.both
        assert x.log == ["--user stop engram-forum.service"], x.log
        new = sqlite3.connect(x.live / "forum.db")
        assert new.execute(_MATCH).fetchone()[0] == 1  # FTS was rebuilt before the exit
        new.close()
        assert len(_pre(x.live, "forum.db")) == 1


def test_a_wrong_service_dir_stops_the_block_before_anything_is_touched(tmp_path):
    """The precondition `test` lines run before `systemctl stop`: an unedited or
    wrong SVC leaves the forum running and its DB files where they were."""
    block = _restore_block().replace("SVC=<service-dir>", "SVC=/nonexistent/service-dir")
    x = _run_block(tmp_path, model=True, block=block)
    assert x.r.returncode != 0, x.both
    assert x.log == [], x.log  # neither stop nor start
    assert (x.live / "forum.db").exists()
    assert not _pre(x.live, "forum.db"), list(x.live.iterdir())
