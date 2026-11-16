"""#4727: engram_core._walk_cascade_from_root, the one helper that owns the cascade walk
from a root. Cells for what the four live callers rely on and no existing test pinned:

  * the first hop reads a DEAD root's edges (live_only=False), and the walk continues
    past the direct dependents;
  * ``extra_visited`` (supersede passes its replacement) is never processed, and the
    walk does not go through it.

Graph (hand-built, raw rows): R is retracted. X and Z derive from R; Y derives from X.
"""

import os
import shutil
import sys
import tempfile

import pytest

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    core = fresh_module(engram_core)
    d = tempfile.mkdtemp(prefix="engram_test_4727_walk_")
    core._configure_paths(d)
    core._ensure_data_dir()
    c = core._get_db()
    now = core._now()
    for nid, status, cur in (("root_r", "retracted", 0), ("dep_x", "active", 1),
                             ("dep_y", "active", 1), ("dep_z", "active", 1)):
        c.execute("INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                  "VALUES (?, 'derivation', ?, ?, ?, ?)", (nid, now, f"claim {nid}", status, cur))
    for src, tgt in (("dep_x", "root_r"), ("dep_z", "root_r"), ("dep_y", "dep_x")):
        c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                  "VALUES (?, ?, 'derives_from', ?)", (src, tgt, now))
    c.commit()
    try:
        yield core, c
    finally:
        c.close()
        shutil.rmtree(d, ignore_errors=True)


def _walk(core, c, **kw):
    got = []
    core._walk_cascade_from_root(c, "root_r", on_live_node=lambda row: got.append(row["id"]), **kw)
    return got


def test_walks_a_dead_roots_dependents_and_past_them(conn):
    core, c = conn
    got = _walk(core, c)
    assert sorted(got) == ["dep_x", "dep_y", "dep_z"], got
    assert len(got) == len(set(got)), "each node handed over once"


def test_extra_visited_is_neither_processed_nor_walked_through(conn):
    core, c = conn
    got = _walk(core, c, extra_visited=("dep_x",))
    # dep_x is seeded, so it is skipped AND dep_y (reachable only through dep_x) is not reached.
    assert sorted(got) == ["dep_z"], got
