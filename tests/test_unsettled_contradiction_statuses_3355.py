"""#3355 -- one shared unsettled-contradiction status set.

Contradiction nodes use ``active`` as their live status (``open`` is question
vocabulary) and move to ``partially_resolved`` / ``resolved`` through
``engram_resolve``. Three call sites each hand-wrote that set and disagreed:
the dream-fairy Category 2 inline fallback filtered ``('open',
'partially_resolved')`` (0 rows on a live graph) and
``_pattern_contradiction_obsolescence_ready`` filtered ``'active'`` only (skips a
``partially_resolved`` one). ``engram_core.UNSETTLED_CONTRADICTION_STATUSES`` is the
single source; these cells pin every consumer to it.

Cells:
  C1  partially_resolved contradiction with a superseded side -> returned
  C2  active contradiction with a superseded side -> returned (guard)
  C3  resolved contradiction with a superseded side -> NOT returned
  C4  dream-fairy Category 2 fallback status set == the constant
  C5  health ``m_contradictions_open`` status set == the constant (it is a
      hash-versioned measurement and is NOT edited; this pins it)
  C6  health ``open_contradiction_rate`` signal agrees with the constant
  C7  reflect's unresolved_contradictions holds active + partially_resolved, not resolved (behavioural)
"""

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core as core
import engram_query as qry
from health import measurements, metrics

REPO = Path(__file__).resolve().parent.parent
DREAM_FAIRY_MD = REPO / "src" / "engram" / "agents" / "claude" / "engram-dream-fairy.md"
LIFECYCLE_PY = REPO / "src" / "engram" / "engram_lifecycle.py"

# Statuses a fixture sweeps to prove membership is exactly the constant's.
_SWEEP = ("active", "partially_resolved", "resolved", "open", "retracted")


def _live():
    return tuple(core.UNSETTLED_CONTRADICTION_STATUSES)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, "
        "status TEXT, is_current INTEGER DEFAULT 1)"
    )
    c.execute(
        "CREATE TABLE edges (source_id TEXT, target_id TEXT, relation TEXT)"
    )
    yield c
    c.close()


def _contradiction(c, ct_id, status, *, a_current=0, b_current=1):
    """A contradiction with two sides; side A non-current by default."""
    a, b = f"{ct_id}_a", f"{ct_id}_b"
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?)",
              (a, "observation_factual", "side a", "active", a_current))
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?)",
              (b, "observation_factual", "side b", "active", b_current))
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?)",
              (ct_id, "contradiction", "a vs b", status, 1))
    for side in (a, b):
        c.execute("INSERT INTO edges VALUES (?,?,'contradicts')", (ct_id, side))
    c.commit()


def _ready_ids(c):
    params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
    return [x["id"] for x in qry._pattern_contradiction_obsolescence_ready(c, params)]


def test_c1_partially_resolved_with_obsolete_side_is_returned(conn):
    _contradiction(conn, "ct_1", "partially_resolved")
    assert _ready_ids(conn) == ["ct_1"]


def test_c2_active_with_obsolete_side_is_returned(conn):
    _contradiction(conn, "ct_1", "active")
    assert _ready_ids(conn) == ["ct_1"]


def test_c3_resolved_with_obsolete_side_is_not_returned(conn):
    _contradiction(conn, "ct_1", "resolved")
    assert _ready_ids(conn) == []


def test_c3b_open_is_not_a_contradiction_status(conn):
    """`open` is question vocabulary: a contradiction row carrying it is not live."""
    _contradiction(conn, "ct_1", "open")
    assert _ready_ids(conn) == []


def test_constant_shape():
    assert _live() == ("active", "partially_resolved")


def _fairy_category2_statuses() -> tuple:
    text = DREAM_FAIRY_MD.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines()
                if l.startswith("2. **Contradictions ripe for resolution.**"))
    m = re.search(r"filtered by `status IN \(([^)]*)\)`", line)
    assert m, "Category 2 inline fallback no longer states its status filter"
    return tuple(s.strip().strip("'\"") for s in m.group(1).split(","))


def test_c4_dream_fairy_category2_fallback_matches_constant():
    assert _fairy_category2_statuses() == _live()


def test_c4b_dream_fairy_says_open_is_not_a_contradiction_status():
    line = next(l for l in DREAM_FAIRY_MD.read_text(encoding="utf-8").splitlines()
                if l.startswith("2. **Contradictions ripe for resolution.**"))
    assert "question vocabulary" in line


def test_c5_m_contradictions_open_matches_constant(conn):
    for i, st in enumerate(_SWEEP):
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,1)",
                     (f"ct_{i}", "contradiction", "x", st))
    conn.commit()
    assert measurements.m_contradictions_open(conn) == sum(
        1 for s in _SWEEP if s in _live())


def test_c6_open_contradiction_rate_matches_constant(conn):
    for i, st in enumerate(_SWEEP):
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,1)",
                     (f"ct_{i}", "contradiction", "x", st))
    conn.commit()
    live = sum(1 for s in _SWEEP if s in _live())
    assert metrics.open_contradiction_rate(conn) == live / len(_SWEEP)


def test_c7_reflect_lists_active_and_partially_resolved_not_resolved(tmp_path):
    """Behavioural: reflect's ``unresolved_contradictions`` holds exactly the
    unsettled statuses -- an active and a partially_resolved contradiction appear,
    a resolved one does not. Drives the real ``_reflect_impl`` on a temp graph."""
    import json
    import shutil
    from conftest import fresh_module
    import engram_lifecycle
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    lc = fresh_module(engram_lifecycle)
    lcore = sys.modules["engram_core"]
    data = str(tmp_path / "reflect_data")
    lcore._configure_paths(data)
    lcore._ensure_data_dir()
    c = lcore._get_db()
    try:
        for ct_id, st in (("ct_9001", "active"), ("ct_9002", "partially_resolved"),
                          ("ct_9003", "resolved")):
            c.execute(
                "INSERT INTO nodes (id, type, claim, created_at, status, is_current) "
                "VALUES (?, 'contradiction', ?, 't0', ?, 1)",
                (ct_id, f"claim for {ct_id}", st),
            )
        c.commit()
    finally:
        c.close()
    report = json.loads(lc._reflect_impl())
    got = {x["id"]: x["status"] for x in report["unresolved_contradictions"]}
    assert got == {"ct_9001": "active", "ct_9002": "partially_resolved"}
    shutil.rmtree(data, ignore_errors=True)
