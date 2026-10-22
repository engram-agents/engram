"""#3425 follow-up (3): `nodes.metadata` must be valid JSON or NULL.

Background: a non-JSON blob in `nodes.metadata` makes SQL
`json_extract(metadata, ...)` abort the WHOLE query ("malformed JSON"), at
six read sites. Design call (Kepler, issue #3425): the defect is a WRITE-path
defect, so the fix is write-side; the read sites get defence in depth only.

Contract (written before the implementation; every cell below is a stated
behaviour, not an inferred one):

  C1  INSERT of invalid JSON ('not json', '', '{') is refused with the exact
      message 'nodes.metadata must be valid JSON or NULL'; the row is absent.
  C2  UPDATE OF metadata to invalid JSON is refused with the same message and
      the old value is intact.
  C3  (control) NULL, '{}', '[]', '"x"', '3' and a normal object all pass on
      INSERT and UPDATE: valid JSON of ANY shape is allowed.
  C4  trigger creation is idempotent: setup run twice -> still exactly one
      INSERT guard and one UPDATE guard trigger.
  C5  migration rung 7 quarantines existing invalid rows: 3 invalid + 3 valid
      rows at user_version 6 -> after the rung the 3 invalid rows have
      metadata NULL, `metadata_quarantine` holds exactly those 3 (raw
      byte-for-byte, node_id correct), valid rows untouched, every other
      column of the invalid nodes unchanged.
  C6  the rung twice: second run adds 0 rows, version stays 8 (the top rung), no error.
  C7  fault injection: if the quarantine step raises, user_version stays 6,
      nothing is half-applied, and the next start completes the rung.
  C8  engram_diagnose names the finding `metadata_quarantine` with the table's
      row count: 0 on a clean graph AND when the table is missing (never
      omitted), the count when rows were quarantined; non-zero is `flare`.
  C9  one end-to-end cell per read site (six), through the real call path,
      with the guard triggers dropped and an invalid blob present: the call
      returns normally, the invalid row's field reads as NULL, a valid row
      still works.
  C10 writers: see the module-level note at the bottom (grep + full-suite
      evidence lives in the PR report; the cell here pins the one invariant
      that is checkable mechanically: the real writers' output passes the
      guard).
"""

import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402

MSG = "nodes.metadata must be valid JSON or NULL"
GUARD_NEEDLE = "%nodes.metadata must be valid JSON%"
INVALID_BLOBS = ["not json", "", "{", "{'a': 1}", "undefined"]


# --------------------------------------------------------------- helpers


@pytest.fixture
def core(monkeypatch):
    """A fresh engram_core against a temp data dir, DB bootstrapped to the
    latest rung. Never touches ~/.engram."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    mod = fresh_module(engram_core)
    d = tempfile.mkdtemp(prefix="engram_test_3425_")
    mod._configure_paths(d)
    mod._ensure_data_dir()
    conn = mod._get_db()
    conn.close()
    try:
        yield mod
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _raw(core):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _guard_triggers(c):
    return [
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes' "
            "AND sql LIKE ?",
            (GUARD_NEEDLE,),
        ).fetchall()
    ]


def _drop_guard_triggers(c):
    for n in _guard_triggers(c):
        c.execute(f'DROP TRIGGER IF EXISTS "{n}"')
    c.commit()


def _ins(c, nid, metadata, ntype="observation", **extra):
    cols = {
        "id": nid,
        "type": ntype,
        "claim": f"claim of {nid}",
        "created_at": "2026-10-06T00:00:00Z",
        "status": "active",
        "metadata": metadata,
    }
    cols.update(extra)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    c.execute(f"INSERT INTO nodes ({names}) VALUES ({marks})", tuple(cols.values()))
    c.commit()


def _reopen_setup(core):
    """Force the one-time setup to run again on the next _get_db()."""
    core._db_setup_done_paths.clear()
    conn = core._get_db()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _rewind_to_pre_rung(core):
    """Put the DB in the state of a graph that has not yet run rung 7: no
    guard triggers, no quarantine table, user_version 6."""
    c = _raw(core)
    _drop_guard_triggers(c)
    c.execute("DROP TABLE IF EXISTS metadata_quarantine")
    c.execute("PRAGMA user_version = 6")
    c.commit()
    c.close()
    core._db_setup_done_paths.clear()


def _all_but_metadata(c, nid):
    cols = [r[1] for r in c.execute("PRAGMA table_info(nodes)").fetchall() if r[1] != "metadata"]
    row = c.execute(
        f"SELECT {', '.join(chr(34) + x + chr(34) for x in cols)} FROM nodes WHERE id = ?", (nid,)
    ).fetchone()
    return tuple(row)


# ----------------------------------------------------------------- C1


@pytest.mark.parametrize("blob", INVALID_BLOBS)
def test_c1_insert_invalid_json_refused(core, blob):
    c = _raw(core)
    with pytest.raises(sqlite3.DatabaseError, match=MSG):
        _ins(c, "ob_c1", blob)
    c.rollback()
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE id='ob_c1'").fetchone()[0] == 0


# ----------------------------------------------------------------- C2


@pytest.mark.parametrize("blob", INVALID_BLOBS)
def test_c2_update_to_invalid_json_refused_old_value_intact(core, blob):
    c = _raw(core)
    _ins(c, "ob_c2", '{"keep": 1}')
    with pytest.raises(sqlite3.DatabaseError, match=MSG):
        c.execute("UPDATE nodes SET metadata = ? WHERE id = 'ob_c2'", (blob,))
    c.rollback()
    assert c.execute("SELECT metadata FROM nodes WHERE id='ob_c2'").fetchone()[0] == '{"keep": 1}'


# ----------------------------------------------------------------- C3 (control)

VALID_BLOBS = [None, "{}", "[]", '"x"', "3", "null", '{"a": {"b": [1, 2]}}']


@pytest.mark.parametrize("blob", VALID_BLOBS)
def test_c3_valid_json_of_any_shape_passes_insert_and_update(core, blob):
    c = _raw(core)
    _ins(c, "ob_c3i", blob)
    assert c.execute("SELECT metadata FROM nodes WHERE id='ob_c3i'").fetchone()[0] == blob
    _ins(c, "ob_c3u", '{"start": true}')
    c.execute("UPDATE nodes SET metadata = ? WHERE id = 'ob_c3u'", (blob,))
    c.commit()
    assert c.execute("SELECT metadata FROM nodes WHERE id='ob_c3u'").fetchone()[0] == blob


def test_c3_update_of_other_column_with_null_metadata_unaffected(core):
    c = _raw(core)
    _ins(c, "ob_c3x", None)
    c.execute("UPDATE nodes SET claim = 'changed' WHERE id = 'ob_c3x'")
    c.commit()
    assert c.execute("SELECT claim FROM nodes WHERE id='ob_c3x'").fetchone()[0] == "changed"


# ----------------------------------------------------------------- C4


def test_c4_trigger_creation_idempotent_across_setup_runs(core):
    for _ in range(2):
        _reopen_setup(core)
    c = _raw(core)
    names = _guard_triggers(c)
    assert len(names) == 2 and len(set(names)) == 2, names
    events = sorted(
        ("INSERT" if "BEFORE INSERT" in s.upper() else "UPDATE")
        for (s,) in c.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes' AND sql LIKE ?",
            (GUARD_NEEDLE,),
        ).fetchall()
    )
    assert events == ["INSERT", "UPDATE"]
    # a dropped guard comes back on the next setup (self-heal, mirrors the FTS set)
    c.execute(f'DROP TRIGGER "{names[0]}"')
    c.commit()
    c.close()
    _reopen_setup(core)
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2


# ----------------------------------------------------------------- C5 / C6

_VALID_META = {
    "ob_v1": '{"a": 1}',
    "ob_v2": "[1, 2]",
    "ob_v3": None,
}
_BAD_META = {
    "ob_b1": "not json",
    "ob_b2": "",
    "ob_b3": '{"truncated": ',
}


def _seed_pre_rung(core):
    _rewind_to_pre_rung(core)
    c = _raw(core)
    for nid, m in _VALID_META.items():
        _ins(c, nid, m)
    for i, (nid, m) in enumerate(_BAD_META.items()):
        _ins(
            c, nid, m,
            confidence=0.5 + i / 10,
            recall_summary=f"summary {nid}",
            importance_score=1.25 + i,
        )
    before = {n: _all_but_metadata(c, n) for n in list(_VALID_META) + list(_BAD_META)}
    c.close()
    return before


def test_c5_rung_quarantines_invalid_rows_and_touches_nothing_else(core):
    before = _seed_pre_rung(core)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION

    c = _raw(core)
    for nid in _BAD_META:
        assert c.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0] is None
    q = {r["node_id"]: r["raw"] for r in c.execute("SELECT node_id, raw FROM metadata_quarantine")}
    assert q == _BAD_META  # exactly 3 rows, raw byte for byte, node_id correct
    assert c.execute("SELECT COUNT(*) FROM metadata_quarantine").fetchone()[0] == 3
    stamps = [r[0] for r in c.execute("SELECT quarantined_at FROM metadata_quarantine")]
    assert all(s and s.startswith("20") for s in stamps), stamps
    for nid, m in _VALID_META.items():
        assert c.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0] == m
    for nid, snap in before.items():
        assert _all_but_metadata(c, nid) == snap, f"{nid}: a non-metadata column changed"
    assert len(_guard_triggers(c)) == 2  # and the write guard is live after the rung


def test_c6_rung_twice_is_idempotent(core):
    _seed_pre_rung(core)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    # force the rung body to run a SECOND time over the already-quarantined graph
    c = _raw(core)
    c.execute("PRAGMA user_version = 6")
    c.commit()
    c.close()
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    c = _raw(core)
    assert c.execute("SELECT COUNT(*) FROM metadata_quarantine").fetchone()[0] == 3
    assert len(_guard_triggers(c)) == 2


# ----------------------------------------------------------------- C7


def test_c7_fault_in_quarantine_step_stays_at_v6_then_next_start_completes(core):
    _seed_pre_rung(core)
    # Fault injection with no dependence on the implementation's internals:
    # pre-create the quarantine table with a trigger that aborts every INSERT,
    # so the copy step raises. (The rung's CREATE TABLE IF NOT EXISTS keeps it.)
    c = _raw(core)
    c.executescript(
        """
        CREATE TABLE metadata_quarantine(node_id TEXT NOT NULL, raw TEXT, quarantined_at TEXT NOT NULL);
        CREATE TRIGGER inject_quarantine_fault BEFORE INSERT ON metadata_quarantine
        BEGIN SELECT RAISE(ABORT, 'injected quarantine fault'); END;
        """
    )
    c.close()

    assert _reopen_setup(core) == 6  # not bumped, no crash

    c = _raw(core)
    # nothing half-applied: originals still in place, no guard installed yet
    for nid, m in _BAD_META.items():
        assert c.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0] == m
    assert _guard_triggers(c) == []
    c.execute("DROP TRIGGER inject_quarantine_fault")
    c.commit()
    c.close()

    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION  # next start completes it
    c = _raw(core)
    assert c.execute("SELECT COUNT(*) FROM metadata_quarantine").fetchone()[0] == 3
    for nid in _BAD_META:
        assert c.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0] is None
    assert len(_guard_triggers(c)) == 2


# ----------------------------------------------------------------- C8


def _diagnose(core):
    stats = importlib.import_module("engram_stats")
    return json.loads(stats._diagnose_impl())


def test_c8_diagnose_names_finding_zero_on_clean_graph(core):
    finding = _diagnose(core)["metadata_quarantine"]
    assert finding["count"] == 0
    assert finding["flare"] is False


def test_c8_diagnose_reports_zero_not_omitted_when_table_missing(core):
    c = _raw(core)
    c.execute("DROP TABLE IF EXISTS metadata_quarantine")
    c.commit()
    c.close()
    finding = _diagnose(core)["metadata_quarantine"]
    assert finding["count"] == 0
    assert finding["flare"] is False


def test_c8_diagnose_reports_count_and_flare_when_rows_quarantined(core):
    _seed_pre_rung(core)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    finding = _diagnose(core)["metadata_quarantine"]
    assert finding["count"] == 3
    assert finding["flare"] is True
    assert sorted(finding["sample_ids"]) == sorted(_BAD_META)


# ----------------------------------------------------------------- C9
# One cell per read site. The guard triggers are dropped, an invalid blob is
# planted, and the REAL call path runs.


def _plant(core):
    c = _raw(core)
    _drop_guard_triggers(c)
    return c


def test_c9_site_cornerstone_link_about_self_lookup(core):
    c = _plant(core)
    _ins(c, "pn_bad", "not json", ntype="person")
    _ins(c, "pn_good", '{"is_self": true}', ntype="person")
    _ins(c, "ob_src", None)
    c.close()
    corner = importlib.import_module("engram_cornerstone")
    out = json.loads(corner._link_about_impl(node_id="ob_src"))
    assert "malformed" not in json.dumps(out)
    assert out.get("status") == "linked" and out.get("person_id") == "pn_good", out


def test_c9_site_epistemic_scan_emergence_self_lookup(core):
    c = _plant(core)
    _ins(c, "pn_bad", "not json", ntype="person")
    _ins(c, "pn_good", '{"is_self": true}', ntype="person")
    c.close()
    epi = importlib.import_module("engram_epistemic")
    out = json.loads(epi._scan_emergence_impl(focus="self"))
    assert out.get("status") == "ok" and "error" not in out, out  # valid self row resolved


def test_c9_site_lifecycle_reflect_active_goals(core):
    c = _plant(core)
    _ins(c, "gl_bad", "not json", ntype="goal", status="open")
    _ins(c, "gl_ok", '{"lifecycle_state": "active"}', ntype="goal", status="open")
    _ins(c, "gl_paused", '{"lifecycle_state": "paused"}', ntype="goal", status="open")
    c.close()
    life = importlib.import_module("engram_lifecycle")
    out = json.loads(life._reflect_impl())
    ids = {g["id"] for g in out["active_goals"]}
    # invalid blob reads as NULL field -> counted as active (state unknown = active,
    # same as a goal with no lifecycle_state); the valid rows keep their semantics.
    assert ids == {"gl_bad", "gl_ok"}


def test_c9_site_stats_diagnose_cornerstone_coverage(core):
    c = _plant(core)
    _ins(c, "cs_bad", "not json", ntype="cornerstone")
    _ins(c, "cs_ok", '{"tag": "some-tag"}', ntype="cornerstone")
    c.close()
    cov = _diagnose(core)["cornerstone_coverage"]
    assert "error" not in cov, cov
    assert cov["total"] == 2


def test_c9_site_trust_add_person_self_exists_check(core):
    c = _plant(core)
    _ins(c, "pn_bad", "not json", ntype="person")
    _ins(c, "pn_good", '{"is_self": true}', ntype="person")
    c.close()
    trust = importlib.import_module("engram_trust")
    out = json.loads(trust._add_person_impl(name="Someone", is_self=True))
    assert out.get("existing_self_id") == "pn_good", out


def test_c9_site_trust_set_person_lineage_self_lookup(core):
    c = _plant(core)
    _ins(c, "pn_bad", "not json", ntype="person")
    _ins(c, "pn_good", '{"is_self": true}', ntype="person")
    c.close()
    trust = importlib.import_module("engram_trust")
    out = json.loads(trust._set_person_lineage_impl(model_id="claude-opus-4-8"))
    assert out.get("status") == "ok" and out.get("person_id") == "pn_good", out


# ----------------------------------------------------------------- C10 pin


def test_c10_real_writer_output_passes_the_guard(core):
    """The person writer (metadata built with json.dumps) lands under the live
    guard: a guard that refused real writers would break the product."""
    trust = importlib.import_module("engram_trust")
    out = json.loads(trust._add_person_impl(name="Guarded Writer", is_self=True))
    assert "error" not in out, out
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2


# ----------------------------------------------------------------- C7 (a)/(b)
# Faults at the other two points of the rung, on a real temp DB at
# user_version 6 with 2 invalid rows + 1 valid. NOTE: the empty
# `metadata_quarantine` TABLE may exist after a fault (CREATE TABLE IF NOT
# EXISTS autocommits); only its ROWS are asserted to be rolled back.

_FAULT_BAD = {"ob_fb1": "not json", "ob_fb2": "{"}


def _seed_fault(core):
    _rewind_to_pre_rung(core)
    c = _raw(core)
    _ins(c, "ob_fv1", '{"ok": 1}')
    for nid, m in _FAULT_BAD.items():
        _ins(c, nid, m)
    c.close()


def _assert_rolled_back(core):
    c = _raw(core)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert _guard_triggers(c) == []
    has = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_quarantine'"
    ).fetchone()
    if has:  # the empty table may survive; its rows may not
        assert c.execute("SELECT COUNT(*) FROM metadata_quarantine").fetchone()[0] == 0
    for nid, m in _FAULT_BAD.items():
        assert c.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0] == m
    c.close()


def _assert_completed(core):
    c = _raw(core)
    assert c.execute("PRAGMA user_version").fetchone()[0] == core.LATEST_SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM metadata_quarantine").fetchone()[0] == 2
    assert len(_guard_triggers(c)) == 2
    assert c.execute(
        "SELECT COUNT(*) FROM nodes WHERE metadata IS NOT NULL AND NOT json_valid(metadata)"
    ).fetchone()[0] == 0
    c.close()


def test_c7a_fault_after_the_update_rolls_everything_back(core, monkeypatch):
    _seed_fault(core)
    real = core._ensure_metadata_json_guard_triggers

    def boom(conn):
        raise sqlite3.DatabaseError("injected: after the quarantine UPDATE")

    monkeypatch.setattr(core, "_ensure_metadata_json_guard_triggers", boom)
    assert _reopen_setup(core) == 6
    _assert_rolled_back(core)
    monkeypatch.setattr(core, "_ensure_metadata_json_guard_triggers", real)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    _assert_completed(core)


def test_c7b_fault_after_both_create_triggers_rolls_everything_back(core, monkeypatch):
    _seed_fault(core)
    real = core._ensure_metadata_json_guard_triggers

    def real_then_boom(conn):
        real(conn)
        raise sqlite3.DatabaseError("injected: after both CREATE TRIGGERs")

    monkeypatch.setattr(core, "_ensure_metadata_json_guard_triggers", real_then_boom)
    assert _reopen_setup(core) == 6
    _assert_rolled_back(core)
    monkeypatch.setattr(core, "_ensure_metadata_json_guard_triggers", real)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    _assert_completed(core)


# ----------------------------------------------------------------- OF metadata


def test_update_of_other_column_on_a_slipped_through_row_succeeds(core):
    """Belt scenario: a row that slipped past the guard (blob planted with the
    triggers dropped, guard re-ensured) must stay updatable on every OTHER
    column. Pins `UPDATE OF metadata` on the update trigger."""
    c = _raw(core)
    _drop_guard_triggers(c)
    _ins(c, "ob_slip", "not json")
    c.close()
    _reopen_setup(core)  # self-heal re-ensures the guard
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2
    c.execute("UPDATE nodes SET claim = 'edited', importance_score = 2.5 WHERE id = 'ob_slip'")
    c.commit()
    row = c.execute("SELECT claim, importance_score, metadata FROM nodes WHERE id='ob_slip'").fetchone()
    assert tuple(row) == ("edited", 2.5, "not json")


# ----------------------------------------------------------------- C8 live count


def test_c8_live_invalid_blob_is_visible_and_flares(core):
    c = _plant(core)
    _ins(c, "ob_live", "not json")
    c.close()
    f = _diagnose(core)["metadata_quarantine"]
    assert f["live_invalid_count"] == 1
    assert f["count"] == 0
    assert f["flare"] is True


def test_c8_clean_graph_both_counts_zero_no_flare(core):
    f = _diagnose(core)["metadata_quarantine"]
    assert (f["count"], f["live_invalid_count"], f["flare"]) == (0, 0, False)


def test_c8_live_count_still_computed_when_table_missing(core):
    c = _plant(core)
    c.execute("DROP TABLE IF EXISTS metadata_quarantine")
    _ins(c, "ob_live2", "{")
    c.close()
    f = _diagnose(core)["metadata_quarantine"]
    assert f["count"] == 0 and f["live_invalid_count"] == 1 and f["flare"] is True
