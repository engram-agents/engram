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
        restore = sec.index("sqlite3 ")
        regen = sec.index("forum_regen_derived.py")
        start = sec.index("systemctl --user start")
        assert restore < regen < start

    def test_regen_runs_under_the_forum_venv_from_the_snapshot(self):
        sec = re.sub(r"\s*\\\n\s*", " ", _restore_section())
        assert re.search(
            r"<service-dir>/\.venv/bin/python <service-dir>/app/tools/forum_regen_derived\.py"
            r" --db \S+/forum\.db", sec), sec

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
