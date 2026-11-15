"""#4727 — migration rung 11: a one-time retract-marker backfill for retractions
made before the cascade machine existed.

Contract (Office thing '#4727 contract + measure (rev 2)', rulings by Kepler and
Borges on 2026-11-15). Retractions older than the cascade machine never marked the
nodes built on them, so a derivation can still rest, unmarked, on a retracted
premise. The rung marks them once, with the LIVE walk, and writes nothing else.

How the fixture makes an "old" retraction: it retracts a node through the real
``_retract_impl`` (so the live walk marks its dependents), records which nodes the
live walk marked, then deletes that root's retract markers. What is left is exactly
what a pre-machine retraction looks like, and the recorded set is the parity oracle:
the backfill must mark the same nodes the live walk did (cell 6).

Every expected set below is written out by hand from the fixture, never read back
from the code under test.
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

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402
import engram_revision  # noqa: E402

RUNG = 11


def _mods(d):
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    lifecycle = importlib.import_module("engram_lifecycle")
    core = sys.modules["engram_core"]
    core._configure_paths(d)
    core._ensure_data_dir()
    return rev, obs, derive, lifecycle, core


def _ob(obs, claim):
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/src", title="Source", quoted_text=claim,
        interpretation=f"Interpretation: {claim}", claim=claim,
        quote_type="attributed_analysis",
    ))
    assert "error" not in r, r
    return r["observation_id"]


def _dv(derive, claim, premises):
    r = json.loads(derive._derive_impl(
        claim=claim, supporting_ids=",".join(premises),
        logical_chain=f"Derivation: {claim}", reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in r, r
    return r["derivation_id"]


def _retract(rev, node_id):
    r = json.loads(rev._retract_impl(
        node_id=node_id, error_type="hallucinated_claim", reason="fixture: wrong",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in r, r


def _raw(core):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _retract_marked(core, root):
    """node_id -> cascade_event, for every retract marker rooted at `root`."""
    c = _raw(core)
    try:
        return {r["node_id"]: r["cascade_event"] for r in c.execute(
            "SELECT node_id, cascade_event FROM cascade_markers "
            "WHERE root_op = 'retract' AND root_node = ?", (root,))}
    finally:
        c.close()


def _forget_retraction_markers(core, root):
    """Turn a live retraction into a pre-machine one: delete its retract markers."""
    c = _raw(core)
    try:
        c.execute("DELETE FROM cascade_markers WHERE root_op = 'retract' AND root_node = ?", (root,))
        c.commit()
    finally:
        c.close()


def _set_version(core, v):
    c = _raw(core)
    try:
        c.execute(f"PRAGMA user_version = {int(v)}")
        c.commit()
    finally:
        c.close()


def _ladder(core):
    c = _raw(core)
    try:
        core._run_db_one_time_setup(c)
        c.commit()
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _dump(core, table):
    c = _raw(core)
    try:
        return [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        c.close()


@pytest.fixture
def g(monkeypatch):
    """The cell-1 graph, with R retracted the old way. Returns a dict of ids."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    d = tempfile.mkdtemp(prefix="engram_test_4727_")
    rev, obs, derive, lifecycle, core = _mods(d)
    try:
        R = _ob(obs, "R: the premise that will be found wrong.")
        O = _ob(obs, "O: an independent clean premise.")
        D1 = _dv(derive, "D1 rests on R.", [R])
        D2 = _dv(derive, "D2 rests on D1.", [D1])
        A = _dv(derive, "A rests on O, and will acknowledge R.", [O])
        S = _dv(derive, "S rests on R and will be superseded first.", [R])
        S2 = _dv(derive, "S2 replaces S and rests on O.", [O])
        sup = json.loads(rev._supersede_impl(old_node_id=S, new_node_id=S2,
                                             supersede_reason="fixture: refined"))
        assert sup.get("status") == "superseded", sup
        c = _raw(core)
        try:
            # A acknowledges R: the edge kind a resolved supersede cascade writes. It is
            # cascade-bearing, so a later retraction of R reaches A (Kepler ruling (a)).
            c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                      "VALUES (?, ?, 'acknowledged_derives_from', ?)", (A, R, core._now()))
            # N: a non-claim-bearing node that only CITES R; cites is not a cascade relation.
            c.execute("INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                      "VALUES ('fl_fixture_n', 'feeling_report', ?, 'N cites R', 'active', 1)",
                      (core._now(),))
            c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                      "VALUES ('fl_fixture_n', ?, 'cites', ?)", (R, core._now()))
            c.commit()
        finally:
            c.close()

        _retract(rev, R)
        live = _retract_marked(core, R)
        _forget_retraction_markers(core, R)
        _set_version(core, RUNG - 1)
        core._db_setup_done_paths.clear()
        yield dict(rev=rev, derive=derive, lifecycle=lifecycle, core=core, dir=d,
                   R=R, O=O, D1=D1, D2=D2, A=A, S=S, S2=S2, N="fl_fixture_n", live=live)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# The rung's identity and place on the ladder
# ---------------------------------------------------------------------------

def test_c0_rung_constant_and_latest_schema_version():
    assert engram_core.RETRACT_BACKFILL_RUNG == RUNG
    assert engram_core.LATEST_SCHEMA_VERSION == RUNG
    src = Path(engram_core.__file__).read_text(encoding="utf-8")
    assert "_rung_ready(conn, RETRACT_BACKFILL_RUNG," in src


# ---------------------------------------------------------------------------
# Cell 1 — who gets marked, and with what
# ---------------------------------------------------------------------------

def test_cell1_marks_exactly_the_live_claim_bearing_dependents(g):
    core = g["core"]
    assert _retract_marked(core, g["R"]) == {}, "fixture: R must start unmarked"
    assert _ladder(core) == RUNG

    got = _retract_marked(core, g["R"])
    # By hand: D1 (direct), D2 (via D1), A (acknowledged edge). Not N (cites, NCB),
    # not S (superseded), not S2 or O (not downstream of R), not R itself.
    assert set(got) == {g["D1"], g["D2"], g["A"]}, got
    assert set(got.values()) == {f"backfill:retract:{g['R']}"}, got

    c = _raw(core)
    try:
        rows = c.execute(
            "SELECT status, backfill_provenance FROM cascade_markers WHERE root_node = ?",
            (g["R"],)).fetchall()
    finally:
        c.close()
    assert {(r["status"], r["backfill_provenance"]) for r in rows} == {("pending", "migration_time")}


def test_cell1b_one_edit_history_row_per_backfilled_root(g):
    """One row per root that gained markers, keyed to that root (every edit_history
    row is keyed to a node; node_id is NOT NULL)."""
    core = g["core"]
    _ladder(core)
    c = _raw(core)
    try:
        rows = c.execute(
            "SELECT node_id, details FROM edit_history WHERE action = 'retract_backfill'").fetchall()
    finally:
        c.close()
    assert [r["node_id"] for r in rows] == [g["R"]], [tuple(r) for r in rows]
    details = json.loads(rows[0]["details"])
    assert details["cascade_event"] == f"backfill:retract:{g['R']}", details
    assert details["markers_written"] == 3, details
    assert details["nodes_marked"] == sorted([g["D1"], g["D2"], g["A"]]), details


# ---------------------------------------------------------------------------
# Cell 2 — a root that already has a retract marker is left alone
# ---------------------------------------------------------------------------

def test_cell2_an_already_marked_root_gets_no_backfill_marker(g):
    rev, derive, core = g["rev"], g["derive"], g["core"]
    R2 = _ob(sys.modules["engram_observation"], "R2: retracted under the live machine.")
    D3 = _dv(derive, "D3 rests on R2.", [R2])
    _retract(rev, R2)
    live_before = _retract_marked(core, R2)
    assert set(live_before) == {D3}, live_before
    _set_version(core, RUNG - 1)
    core._db_setup_done_paths.clear()
    _ladder(core)
    assert _retract_marked(core, R2) == live_before, "a live-marked root must gain nothing"


# ---------------------------------------------------------------------------
# Cell 3 — re-running the rung writes nothing
# ---------------------------------------------------------------------------

def test_cell3_rerun_writes_zero_rows(g):
    core = g["core"]
    _ladder(core)
    first = _dump(core, "cascade_markers")
    _set_version(core, RUNG - 1)
    core._db_setup_done_paths.clear()
    _ladder(core)
    assert _dump(core, "cascade_markers") == first


# ---------------------------------------------------------------------------
# Cell 4 — no byte of nodes or edges changes
# ---------------------------------------------------------------------------

def test_cell4_nodes_and_edges_are_byte_identical(g):
    core = g["core"]
    nodes, edges = _dump(core, "nodes"), _dump(core, "edges")
    _ladder(core)
    assert _dump(core, "nodes") == nodes
    assert _dump(core, "edges") == edges


# ---------------------------------------------------------------------------
# Cell 5 — no pre-migration backup, no rung
# ---------------------------------------------------------------------------

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_cell5_a_failed_pre_migration_backup_blocks_the_rung(g, capsys):
    core = g["core"]
    backup_dir = Path(core.DATA_DIR) / "db-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(backup_dir), 0o500)
    try:
        capsys.readouterr()
        assert _ladder(core) == RUNG - 1
        assert "pre-migration backup FAILED" in capsys.readouterr().err
    finally:
        os.chmod(str(backup_dir), 0o700)
    assert _retract_marked(core, g["R"]) == {}


# ---------------------------------------------------------------------------
# Cell 6 — parity with the live walk (the acknowledged edge included)
# ---------------------------------------------------------------------------

def test_cell6_backfill_marks_the_same_nodes_the_live_retract_did(g):
    core = g["core"]
    assert g["A"] in g["live"], "fixture: the live walk must reach the acknowledged dependent"
    _ladder(core)
    assert set(_retract_marked(core, g["R"])) == set(g["live"])


# ---------------------------------------------------------------------------
# Cell 6b — contradiction nodes are NOT backfilled (C6; the live path marks them)
# ---------------------------------------------------------------------------

def test_cell6b_contradiction_nodes_are_not_backfilled(g):
    core = g["core"]
    c = _raw(core)
    try:
        c.execute("INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                  "VALUES ('ct_fixture_x', 'contradiction', ?, 'R vs O', 'open', 1)", (core._now(),))
        for side in (g["R"], g["O"]):
            c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                      "VALUES ('ct_fixture_x', ?, 'contradicts', ?)", (side, core._now()))
        c.commit()
    finally:
        c.close()
    _ladder(core)
    assert "ct_fixture_x" not in _retract_marked(core, g["R"])


# ---------------------------------------------------------------------------
# Cells 8-10 — the backfill says its own name on existing surfaces (C7)
# ---------------------------------------------------------------------------

def test_cell8_frontier_lists_the_backfill_event(g):
    rev, core = g["rev"], g["core"]
    _ladder(core)
    resp = json.loads(rev._cascade_frontier_impl())
    d1 = next(s for s in resp["frontier"] if s["id"] == g["D1"])
    assert f"backfill:retract:{g['R']}" in {e["cascade_event"] for e in d1["events"]}, d1


def test_cell9_reflect_names_backfilled_roots_and_only_those(g):
    rev, derive, lifecycle, core = g["rev"], g["derive"], g["lifecycle"], g["core"]
    _ladder(core)
    # A LIVE retraction after the rung: its dependent must carry no backfilled_by key.
    R3 = _ob(sys.modules["engram_observation"], "R3: retracted live, after the rung.")
    D4 = _dv(derive, "D4 rests on R3.", [R3])
    _retract(rev, R3)
    report = json.loads(lifecycle._reflect_impl())
    by_id = {t["id"]: t for t in report["tainted_nodes"]}
    assert by_id[g["D1"]].get("backfilled_by") == [g["R"]], by_id[g["D1"]]
    assert "backfilled_by" not in by_id[D4], by_id[D4]


def test_cell10_refusal_carries_the_backfill_event_and_only_for_backfill(g):
    rev, derive, core = g["rev"], g["derive"], g["core"]
    _ladder(core)
    r = json.loads(derive._derive_impl(
        claim="Deriving over a backfill-marked premise.", supporting_ids=f"{g['D1']},{g['O']}",
        logical_chain="Should be refused.", reasoning_type="deductive_modus_ponens",
    ))
    assert r.get("status") == "blocked_tainted", r
    item = next(p for p in r["tainted_premises"] if p["id"] == g["D1"])
    events = {x.get("cascade_event") for x in item["retracted_by"]}
    assert f"backfill:retract:{g['R']}" in events, item

    R3 = _ob(sys.modules["engram_observation"], "R3: retracted live.")
    D4 = _dv(derive, "D4 rests on R3.", [R3])
    _retract(rev, R3)
    r2 = json.loads(derive._derive_impl(
        claim="Deriving over a live-marked premise.", supporting_ids=f"{D4},{g['O']}",
        logical_chain="Should be refused.", reasoning_type="deductive_modus_ponens",
    ))
    assert r2.get("status") == "blocked_tainted", r2
    item2 = next(p for p in r2["tainted_premises"] if p["id"] == D4)
    assert all("cascade_event" not in x for x in item2["retracted_by"]), item2


# ---------------------------------------------------------------------------
# Round-2 cells (reviewer r1): the failure path and the fresh-store guard
# ---------------------------------------------------------------------------

def test_cell11_a_fault_after_the_inserts_rolls_back_markers_and_version(g, monkeypatch, capsys):
    """A DatabaseError raised after the backfill wrote its markers, before the version bump:
    the rung must roll back BOTH (version stays at 10, no backfill marker survives), and the
    next start must succeed."""
    core = g["core"]
    real = core._backfill_pre_machine_retract_markers

    def boom(conn):
        real(conn)
        raise sqlite3.OperationalError("injected fault after the backfill inserts")

    monkeypatch.setattr(core, "_backfill_pre_machine_retract_markers", boom)
    capsys.readouterr()
    assert _ladder(core) == RUNG - 1
    assert "FAILED, will retry" in capsys.readouterr().err
    assert _retract_marked(core, g["R"]) == {}, "a failed rung must leave no backfill marker"

    monkeypatch.setattr(core, "_backfill_pre_machine_retract_markers", real)
    assert _ladder(core) == RUNG
    assert set(_retract_marked(core, g["R"])) == {g["D1"], g["D2"], g["A"]}


def test_cell12_the_rung_runs_when_edit_history_does_not_exist_yet(g):
    """On a fresh store the ladder runs before edit_history is created. Markers are still
    written; only the per-root history row is skipped."""
    core = g["core"]
    c = _raw(core)
    try:
        c.execute("DROP TABLE edit_history")
        c.commit()
    finally:
        c.close()
    assert _ladder(core) == RUNG
    assert set(_retract_marked(core, g["R"])) == {g["D1"], g["D2"], g["A"]}
