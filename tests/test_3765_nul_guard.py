"""#3765: a NUL (U+0000) in `nodes.claim` / `nodes.recall_summary` is refused at the database.

Contract (the design seat's ruling on #3765; every cell below is a stated
behaviour, written before the implementation):

  Rule 1  reject, not strip: the write is refused with a named reason.
  Rule 2  ONE guard at the database (a BEFORE INSERT trigger and a BEFORE
          UPDATE OF claim, recall_summary trigger), not one check per writer.
  Rule 3  the UPDATE trigger is column-scoped: a row that already holds a NUL
          can still be retracted / rescored, and repaired by writing clean text.
  Rule 4  migration rung: DML first, then CREATE TRIGGERs, same transaction;
          the triggers are re-ensured on every start; the trigger SQL never
          mentions nodes_fts (so engram_backup's FTS-trigger drop plan keeps it).
  Rule 5  existing rows: COUNT and report, never alter (one stderr line when
          non-zero; engram_diagnose reports the live count).

Cells:
  1   for EACH MCP writer of `claim` (re-enumerated from the source by cell 1e,
      not copied from the ruling): a NUL is refused, the error names the guard,
      and the row-count delta over EVERY table is zero (whole-tool atomicity).
      1s/1p: the same at the SQL level, in every NUL position, and for
      recall_summary.
  2   control: the same call without the NUL writes.
  3   existing-row cells on a user_version-(RUNG-1) store.
  4   set_recall_summaries is best-effort per item: the NUL item lands in
      `errors`, the others apply, the NUL item's stored summary is unchanged.
  5   owner pin: USE_REWARD_NODES_TRIGGERS_PINNED matches sqlite_master and the
      ADMITTED sets are unchanged (empty).
  6   static: the guard trigger SQL does not mention nodes_fts.

A NUL is only ever planted with a bound parameter (never pasted into SQL text).
"""

import ast
import importlib
import json
import os
import re
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

NUL = "\x00"
GUARD_NEEDLE = "%U+0000%"
SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "engram"


# --------------------------------------------------------------- helpers


@pytest.fixture
def core(monkeypatch):
    """A fresh engram_core on a temp data dir, bootstrapped to the latest rung."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    mod = fresh_module(engram_core)
    d = tempfile.mkdtemp(prefix="engram_test_3765_")
    mod._configure_paths(d)
    mod._ensure_data_dir()
    mod._get_db().close()
    try:
        yield mod
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _raw(core):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _guard_triggers(c):
    return sorted(
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes' AND sql LIKE ?",
            (GUARD_NEEDLE,),
        ).fetchall()
    )


def _drop_guard_triggers(c):
    for n in _guard_triggers(c):
        c.execute(f'DROP TRIGGER IF EXISTS "{n}"')
    c.commit()


def _table_counts(core):
    """Row count of EVERY table (a tool may touch any of them)."""
    c = _raw(core)
    try:
        out = {}
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            try:
                out[name] = c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:  # a vtab whose module is not loadable here
                out[name] = None
        return out
    finally:
        c.close()


def _delta(before, after):
    return {k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)}


def _reopen_setup(core):
    core._db_setup_done_paths.clear()
    conn = core._get_db()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _rewind_to_pre_rung(core):
    """The state of a store that has not yet run the NUL-guard rung."""
    c = _raw(core)
    _drop_guard_triggers(c)
    c.execute(f"PRAGMA user_version = {core.TEXT_NUL_GUARD_RUNG - 1}")
    c.commit()
    c.close()
    core._db_setup_done_paths.clear()


def _outcome(fn):
    """Run a writer. Return (error_text or None, parsed-json-or-None). A raised
    sqlite3 error and a returned {"error": ...} both count as a refusal."""
    try:
        raw = fn()
    except sqlite3.Error as exc:
        return str(exc), None
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "error" in parsed:
        return str(parsed["error"]), parsed
    return None, parsed


# ----------------------------------------------------- the writer table
# One entry per MCP writer of nodes.claim. `setup(m)` creates prerequisites
# through the real tools (before the before-snapshot); `call(m, ctx, text)`
# calls the REAL tool function with `text` in the field that becomes the claim.

_URL_SEQ = [0]


def _mods():
    return {n: importlib.import_module(n) for n in (
        "engram_observation", "engram_epistemic", "engram_tasks", "engram_trust",
        "engram_derive", "engram_cornerstone")}


def _ob(m, claim=None):
    _URL_SEQ[0] += 1
    i = _URL_SEQ[0]
    r = json.loads(m["engram_observation"]._add_observation_impl(
        url=f"https://example.com/n{i}", title=f"N{i}", quoted_text=f"quote {i} in the cited source",
        interpretation=f"interp {i}", claim=claim or f"prereq claim {i}", quote_type="hard_data"))
    assert "observation_id" in r, r
    return r["observation_id"]


def _goal(m):
    _URL_SEQ[0] += 1
    r = json.loads(m["engram_tasks"]._add_goal_impl(claim=f"prereq goal {_URL_SEQ[0]}", motivation="m"))
    assert "goal_id" in r, r
    return r["goal_id"]


def _person(m):
    _URL_SEQ[0] += 1
    r = json.loads(m["engram_trust"]._add_person_impl(name=f"Prereq Person {_URL_SEQ[0]}"))
    assert "person_id" in r, r
    return r["person_id"]


def _setup_none(m):
    return {}


def _setup_evidence(m):
    """An existing evidence node to cite by evidence_id, so the observation INSERT is
    reached with no tool-layer pre-check in front of it (the trigger alone is the guard)."""
    _URL_SEQ[0] += 1
    r = json.loads(m["engram_observation"]._add_observation_impl(
        url=f"https://example.com/ev{_URL_SEQ[0]}", title="Ev", quoted_text="q in the cited source", interpretation="i",
        claim=f"evidence prereq {_URL_SEQ[0]}", quote_type="hard_data"))
    assert "evidence_id" in r, r
    return {"ev": r["evidence_id"]}


def _setup_ob(m):
    return {"ob": _ob(m)}


def _setup_two_obs(m):
    return {"a": _ob(m), "b": _ob(m)}


def _setup_two_goals(m):
    return {"a": _goal(m), "b": _goal(m)}


def _setup_trust_signal(m):
    return {"pn": _person(m), "ob": _ob(m)}


def _setup_cornerstone(m):
    r = json.loads(m["engram_cornerstone"]._add_cornerstone_impl(
        tag="prereq_tag", title="prereq title", new_frame="nf", prior_frame="pf", triggering_experience="te"))
    assert "cornerstone_id" in r, r
    return {"cs": r["cornerstone_id"]}


WRITERS = {
    "_add_observation_impl": (_setup_evidence, lambda m, c, t: m["engram_observation"]._add_observation_impl(
        evidence_id=c["ev"], quoted_text="a quote in the cited source", interpretation="interp", claim=t, quote_type="hard_data")),
    "_create_derivation": (_setup_ob, lambda m, c, t: m["engram_derive"]._derive_impl(
        claim=t, supporting_ids=c["ob"], logical_chain="because", reasoning_type="deductive_modus_ponens")),
    "_contradict_impl": (_setup_two_obs, lambda m, c, t: m["engram_derive"]._contradict_impl(
        node_id_a=c["a"], node_id_b=c["b"], description=t)),
    "_ask_impl": (_setup_none, lambda m, c, t: m["engram_derive"]._ask_impl(question=t)),
    "_add_goal_impl": (_setup_none, lambda m, c, t: m["engram_tasks"]._add_goal_impl(claim=t, motivation="m")),
    "_add_task_impl": (_setup_none, lambda m, c, t: m["engram_tasks"]._add_task_impl(description=t)),
    "_report_feeling_impl": (_setup_none, lambda m, c, t: m["engram_tasks"]._report_feeling_impl(
        reported_state=t, trigger="a trigger")),
    "_add_person_impl": (_setup_none, lambda m, c, t: m["engram_trust"]._add_person_impl(name=t)),
    "_add_trust_signal_impl": (_setup_trust_signal, lambda m, c, t: m["engram_trust"]._add_trust_signal_impl(
        subject_pn=c["pn"], source_obs_id=c["ob"], kind="honesty", polarity=0.5, weight=0.5, claim=t)),
    "_add_cornerstone_impl": (_setup_none, lambda m, c, t: m["engram_cornerstone"]._add_cornerstone_impl(
        tag="under_test", title=t, new_frame="nf", prior_frame="pf", triggering_experience="te")),
    "_outgrow_cornerstone_impl": (_setup_cornerstone, lambda m, c, t: m["engram_cornerstone"]._outgrow_cornerstone_impl(
        old_cornerstone_id=c["cs"], new_new_frame="nf2", new_triggering_experience="te2", new_title=t)),
    "_add_axiom_impl": (_setup_none, lambda m, c, t: m["engram_epistemic"]._add_axiom_impl(claim=t, basis="b")),
    "_add_definition_impl": (_setup_none, lambda m, c, t: m["engram_epistemic"]._add_definition_impl(
        term=t, definition="a definition")),
    "_add_conjecture_impl": (_setup_none, lambda m, c, t: m["engram_epistemic"]._add_conjecture_impl(
        claim=t, basis="b")),
    "_goal_tension_impl": (_setup_two_goals, lambda m, c, t: m["engram_epistemic"]._goal_tension_impl(
        goal_id_a=c["a"], goal_id_b=c["b"], description=t, analysis="an analysis")),
    "_add_lesson_impl": (_setup_ob, lambda m, c, t: m["engram_epistemic"]._add_lesson_impl(
        claim=t, incident_ids=c["ob"], scaffolding_nudge="n", logical_chain="lc", situation_pattern="sp")),
}
WRITER_IDS = sorted(WRITERS)


# ----------------------------------------------------------------- 1e


def _claim_writer_functions():
    """Re-enumerate from the source: every function holding a string constant
    `INSERT INTO nodes (... claim ...)`. Innermost function only."""
    found = set()
    for path in sorted(SRC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        docstrings = {id(f.body[0].value) for f in funcs
                      if f.body and isinstance(f.body[0], ast.Expr) and isinstance(f.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docstrings:
                continue
            head = node.value.split("VALUES")[0]
            if not re.search(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+nodes\s*\(", head) or not re.search(r"\bclaim\b", head):
                continue
            enclosing = [f for f in funcs if f.lineno <= node.lineno <= f.end_lineno]
            if enclosing:
                found.add(min(enclosing, key=lambda f: f.end_lineno - f.lineno).name)
    return found


def test_cell1e_writer_table_covers_every_claim_inserting_function():
    """A new INSERT INTO nodes (... claim ...) site anywhere in src/engram fails
    here until it gets a row in WRITERS (and so cells 1 and 2)."""
    assert _claim_writer_functions() == set(WRITERS)


# ----------------------------------------------------------------- 1 / 2


@pytest.mark.parametrize("writer", WRITER_IDS)
def test_cell1_writer_refuses_nul_and_writes_nothing(core, writer):
    m = _mods()
    setup, call = WRITERS[writer]
    ctx = setup(m)
    before = _table_counts(core)
    err, _ = _outcome(lambda: call(m, ctx, "bad" + NUL + "text"))
    after = _table_counts(core)
    assert err is not None, f"{writer} accepted a NUL claim"
    assert "U+0000" in err, f"{writer}: the error must name the guard, got {err!r}"
    assert _delta(before, after) == {}, f"{writer} left rows behind: {_delta(before, after)}"


@pytest.mark.parametrize("writer", WRITER_IDS)
def test_cell2_control_same_call_without_the_nul_writes(core, writer):
    m = _mods()
    setup, call = WRITERS[writer]
    ctx = setup(m)
    before = _table_counts(core)
    err, parsed = _outcome(lambda: call(m, ctx, "clean text for " + writer))
    after = _table_counts(core)
    assert err is None, f"{writer} control failed: {err!r}"
    assert after["nodes"] > before["nodes"]


@pytest.mark.parametrize("field", ["claim", "recall_summary"])
def test_cell1o_observation_by_url_leaves_no_orphan_evidence_node(core, field):
    """The auto-create-evidence path commits an evidence node on its own connection, so a
    refusal at the observation INSERT would leave it behind: whole-tool atomicity. (This
    path has a tool-layer pre-check; the trigger-only path is the evidence_id writer above.)"""
    m = _mods()
    kwargs = {"claim": "a clean claim", "recall_summary": ""}
    kwargs[field] = "bad" + NUL + "text"
    before = _table_counts(core)
    err, _ = _outcome(lambda: m["engram_observation"]._add_observation_impl(
        url="https://example.com/orphan-check", title="Orphan", quoted_text="a quote in the cited source",
        interpretation="interp", quote_type="hard_data", **kwargs))
    assert err is not None and "U+0000" in err, err
    assert _delta(before, _table_counts(core)) == {}


def test_cell2o_control_observation_by_url_with_clean_text_writes_evidence_and_observation(core):
    m = _mods()
    before = _table_counts(core)
    err, _ = _outcome(lambda: m["engram_observation"]._add_observation_impl(
        url="https://example.com/orphan-control", title="Orphan", quoted_text="a quote in the cited source",
        interpretation="interp", claim="a clean claim", quote_type="hard_data"))
    assert err is None, err
    assert _table_counts(core)["nodes"] == before["nodes"] + 2  # evidence + observation


# The writers that also take a `recall_summary` at creation: a NUL there is
# refused the same way (the trigger tests recall_summary as well as claim).
RS_WRITERS = {
    "_add_observation_impl": _setup_evidence,
    "_create_derivation": WRITERS["_create_derivation"][0],
    "_add_definition_impl": WRITERS["_add_definition_impl"][0],
    "_add_conjecture_impl": WRITERS["_add_conjecture_impl"][0],
    "_add_lesson_impl": WRITERS["_add_lesson_impl"][0],
}
RS_CALLS = {
    "_add_observation_impl": lambda m, c, t: m["engram_observation"]._add_observation_impl(
        evidence_id=c["ev"], quoted_text="a quote in the cited source", interpretation="interp",
        claim="a clean claim", quote_type="hard_data", recall_summary=t),
    "_create_derivation": lambda m, c, t: m["engram_derive"]._derive_impl(
        claim="a clean derived claim", supporting_ids=c["ob"], logical_chain="because",
        reasoning_type="deductive_modus_ponens", recall_summary=t),
    "_add_definition_impl": lambda m, c, t: m["engram_epistemic"]._add_definition_impl(
        term="a term", definition="a definition", recall_summary=t),
    "_add_conjecture_impl": lambda m, c, t: m["engram_epistemic"]._add_conjecture_impl(
        claim="a clean conjecture", basis="b", recall_summary=t),
    "_add_lesson_impl": lambda m, c, t: m["engram_epistemic"]._add_lesson_impl(
        claim="a clean lesson", incident_ids=c["ob"], scaffolding_nudge="n", logical_chain="lc",
        situation_pattern="sp", recall_summary=t),
}


@pytest.mark.parametrize("writer", sorted(RS_CALLS))
def test_cell1r_writer_refuses_a_nul_in_recall_summary_and_writes_nothing(core, writer):
    m = _mods()
    ctx = RS_WRITERS[writer](m)
    before = _table_counts(core)
    err, _ = _outcome(lambda: RS_CALLS[writer](m, ctx, "sum" + NUL + "mary"))
    after = _table_counts(core)
    assert err is not None and "U+0000" in err, (writer, err)
    assert _delta(before, after) == {}, _delta(before, after)


@pytest.mark.parametrize("writer", sorted(RS_CALLS))
def test_cell2r_control_same_call_with_a_clean_recall_summary_writes(core, writer):
    m = _mods()
    ctx = RS_WRITERS[writer](m)
    before = _table_counts(core)
    err, _ = _outcome(lambda: RS_CALLS[writer](m, ctx, "a clean summary"))
    assert err is None, (writer, err)
    assert _table_counts(core)["nodes"] > before["nodes"]


# ----------------------------------------------------------------- 1s / 1p (SQL level)


def _insert_node(c, nid, claim="clean", recall_summary=None):
    c.execute(
        "INSERT INTO nodes (id, type, claim, created_at, status, recall_summary) VALUES (?, 'observation', ?, ?, 'active', ?)",
        (nid, claim, "2026-10-22T00:00:00Z", recall_summary),
    )
    c.commit()


NUL_TEXTS = ["a" + NUL + "b", NUL + "ab", "ab" + NUL, NUL]  # middle, leading, trailing, only


@pytest.mark.parametrize("text", NUL_TEXTS, ids=["middle", "leading", "trailing", "only"])
@pytest.mark.parametrize("column", ["claim", "recall_summary"])
def test_cell1s_insert_refused_in_every_nul_position(core, column, text):
    c = _raw(core)
    kwargs = {column: text}
    with pytest.raises(sqlite3.IntegrityError) as ei:
        _insert_node(c, "ob_9001", **kwargs)
    assert "U+0000" in str(ei.value)
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE id='ob_9001'").fetchone()[0] == 0


@pytest.mark.parametrize("text", NUL_TEXTS, ids=["middle", "leading", "trailing", "only"])
@pytest.mark.parametrize("column", ["claim", "recall_summary"])
def test_cell1s_update_refused_in_every_nul_position_old_value_intact(core, column, text):
    c = _raw(core)
    _insert_node(c, "ob_9002", claim="old claim", recall_summary="old summary")
    with pytest.raises(sqlite3.IntegrityError) as ei:
        c.execute(f"UPDATE nodes SET {column} = ? WHERE id = 'ob_9002'", (text,))
    assert "U+0000" in str(ei.value)
    row = c.execute("SELECT claim, recall_summary FROM nodes WHERE id='ob_9002'").fetchone()
    assert (row["claim"], row["recall_summary"]) == ("old claim", "old summary")


def test_cell1s_a_nul_free_text_with_other_bytes_passes(core):
    """Control for the position cells: non-ASCII text and a literal backslash-0 pass."""
    c = _raw(core)
    _insert_node(c, "ob_9003", claim="café \\x00 中文", recall_summary="sümmary")
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE id='ob_9003'").fetchone()[0] == 1


def test_cell1s_update_of_other_columns_is_not_gated_on_a_clean_row(core):
    c = _raw(core)
    _insert_node(c, "ob_9004")
    c.execute("UPDATE nodes SET status = 'retracted' WHERE id = 'ob_9004'")
    c.commit()
    assert c.execute("SELECT status FROM nodes WHERE id='ob_9004'").fetchone()[0] == "retracted"


# ----------------------------------------------------------------- 3


def _plant_nul_rows(core):
    """Three NUL rows (claim / claim / recall_summary) + one clean, on a store
    rewound to the pre-rung version. Planted with BOUND parameters."""
    _rewind_to_pre_rung(core)
    c = _raw(core)
    _insert_node(c, "ob_8001", claim="a" + NUL + "b")
    _insert_node(c, "ob_8002", claim=NUL + "lead")
    _insert_node(c, "ob_8003", claim="clean claim", recall_summary="sum" + NUL + "mary")
    _insert_node(c, "ob_8004", claim="clean claim")
    rows = {r["id"]: tuple(r) for r in c.execute("SELECT * FROM nodes WHERE id LIKE 'ob_800%' ORDER BY id")}
    c.close()
    return rows


def test_cell3_rung_succeeds_leaves_rows_unchanged_and_reports_the_count(core, capsys):
    before_rows = _plant_nul_rows(core)
    capsys.readouterr()
    version = _reopen_setup(core)
    err = capsys.readouterr().err
    assert version == core.LATEST_SCHEMA_VERSION >= core.TEXT_NUL_GUARD_RUNG
    c = _raw(core)
    after_rows = {r["id"]: tuple(r) for r in c.execute("SELECT * FROM nodes WHERE id LIKE 'ob_800%' ORDER BY id")}
    assert after_rows == before_rows, "the rung must never alter an existing row"
    assert len(_guard_triggers(c)) == 2
    lines = [ln for ln in err.splitlines() if "#3765" in ln]
    assert len(lines) == 1, err
    assert re.search(r"\b3\b", lines[0]), lines[0]


def test_cell3_clean_store_prints_no_count_line(core, capsys):
    _rewind_to_pre_rung(core)
    capsys.readouterr()
    _reopen_setup(core)
    assert "#3765" not in capsys.readouterr().err


def test_cell3_existing_nul_row_can_still_be_retracted_and_rescored(core):
    """Plausible-wrong implementation: a plain BEFORE UPDATE would make this row unretractable."""
    _plant_nul_rows(core)
    _reopen_setup(core)
    c = _raw(core)
    for nid in ("ob_8001", "ob_8003"):
        c.execute("UPDATE nodes SET status = 'retracted' WHERE id = ?", (nid,))
        c.execute("UPDATE nodes SET is_current = 0, importance_score = 0.5 WHERE id = ?", (nid,))
    c.commit()
    assert {r[0] for r in c.execute("SELECT status FROM nodes WHERE id IN ('ob_8001','ob_8003')")} == {"retracted"}


def test_cell3_existing_nul_row_can_be_repaired_by_clean_text(core):
    _plant_nul_rows(core)
    _reopen_setup(core)
    c = _raw(core)
    c.execute("UPDATE nodes SET claim = 'repaired' WHERE id = 'ob_8001'")
    c.execute("UPDATE nodes SET recall_summary = 'repaired summary' WHERE id = 'ob_8003'")
    c.commit()
    assert c.execute("SELECT claim FROM nodes WHERE id='ob_8001'").fetchone()[0] == "repaired"
    assert c.execute("SELECT recall_summary FROM nodes WHERE id='ob_8003'").fetchone()[0] == "repaired summary"


def test_cell3_update_of_claim_to_a_different_value_holding_a_nul_is_refused(core):
    _plant_nul_rows(core)
    _reopen_setup(core)
    c = _raw(core)
    with pytest.raises(sqlite3.IntegrityError) as ei:
        c.execute("UPDATE nodes SET claim = ? WHERE id = 'ob_8001'", ("still" + NUL + "bad",))
    assert "U+0000" in str(ei.value)
    assert c.execute("SELECT claim FROM nodes WHERE id='ob_8001'").fetchone()[0] == "a" + NUL + "b"


# The UPDATE trigger checks a column only when that column CHANGES: a legacy row that holds a
# NUL stays writable in its other columns (the batch-summary fairy's exact statement, retract),
# and an identical rewrite is a no-op. Matrix on a row whose CLAIM holds a NUL (ob_8001), and
# the mirror on a row whose SUMMARY holds one (ob_8003), via the triggers the real helper made.

FAIRY_UPDATE = ("UPDATE nodes SET recall_summary = ?, recall_keywords = ?, "
                "needs_tightening = 0, summary_missing = 0 WHERE id = ?")


@pytest.fixture
def legacy(core):
    _plant_nul_rows(core)
    _reopen_setup(core)
    return _raw(core)


@pytest.mark.parametrize("sql,args", [
    ("UPDATE nodes SET recall_summary = 'a clean summary' WHERE id = 'ob_8001'", ()),
    (FAIRY_UPDATE, ("a clean summary", json.dumps(["a", "b", "c"]), "ob_8001")),
    ("UPDATE nodes SET is_current = 0, status = 'retracted' WHERE id = 'ob_8001'", ()),
    ("UPDATE nodes SET claim = 'now clean' WHERE id = 'ob_8001'", ()),
    ("UPDATE nodes SET claim = ? WHERE id = 'ob_8001'", ("a" + NUL + "b",)),  # same NUL value: a no-op
], ids=["summary_only", "fairy_exact", "retract", "claim_to_clean", "claim_same_nul_value"])
def test_cell3m_legacy_claim_nul_row_allows(legacy, sql, args):
    legacy.execute(sql, args)
    legacy.commit()


@pytest.mark.parametrize("sql,args", [
    ("UPDATE nodes SET claim = ? WHERE id = 'ob_8001'", ("x" + NUL + "y",)),
    ("UPDATE nodes SET recall_summary = ? WHERE id = 'ob_8001'", ("s" + NUL + "t",)),
], ids=["claim_different_nul_value", "summary_to_nul"])
def test_cell3m_legacy_claim_nul_row_refuses(legacy, sql, args):
    with pytest.raises(sqlite3.IntegrityError) as ei:
        legacy.execute(sql, args)
    assert "U+0000" in str(ei.value)


def test_cell3m_mirror_summary_nul_row(legacy):
    legacy.execute("UPDATE nodes SET status = 'retracted', is_current = 0 WHERE id = 'ob_8003'")
    legacy.execute("UPDATE nodes SET claim = 'other clean claim' WHERE id = 'ob_8003'")
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("UPDATE nodes SET recall_summary = ? WHERE id = 'ob_8003'", ("other" + NUL + "nul",))
    legacy.execute("UPDATE nodes SET recall_summary = 'repaired' WHERE id = 'ob_8003'")
    legacy.commit()
    assert legacy.execute("SELECT recall_summary FROM nodes WHERE id='ob_8003'").fetchone()[0] == "repaired"


def test_cell3_rung_is_idempotent_and_the_triggers_self_heal(core):
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2
    _reopen_setup(core)
    assert len(_guard_triggers(c)) == 2  # twice: still exactly one insert + one update trigger
    _drop_guard_triggers(c)  # a hand edit / restore lost them, version unchanged
    assert _guard_triggers(c) == []
    _reopen_setup(core)
    assert len(_guard_triggers(c)) == 2
    with pytest.raises(sqlite3.IntegrityError):
        _insert_node(c, "ob_9100", claim="x" + NUL + "y")


def test_cell3_diagnose_reports_the_live_count_zero_when_clean_and_flare_when_not(core):
    stats = importlib.import_module("engram_stats")
    clean = json.loads(stats._diagnose_impl())["text_nul_guard"]
    assert clean["live_count"] == 0 and clean["flare"] is False
    _plant_nul_rows(core)
    _reopen_setup(core)
    found = json.loads(stats._diagnose_impl())["text_nul_guard"]
    assert found["live_count"] == 3
    assert found["flare"] is True
    assert sorted(found["sample_ids"]) == ["ob_8001", "ob_8002", "ob_8003"]


# ----------------------------------------------------------------- 4


def test_cell4_set_recall_summaries_best_effort_per_item(core):
    m = _mods()
    ids = [_ob(m) for _ in range(3)]
    c = _raw(core)
    c.execute("UPDATE nodes SET recall_summary = 'old summary' WHERE id IN (?, ?, ?)", ids)
    c.commit()
    c.close()
    rs = importlib.import_module("engram_recall_summaries")
    kw = ["alpha", "beta", "gamma"]
    payload = {"summaries": [
        {"node_id": ids[0], "recall_summary": "new one", "recall_keywords": kw},
        {"node_id": ids[1], "recall_summary": "new" + NUL + "two", "recall_keywords": kw},
        {"node_id": ids[2], "recall_summary": "new three", "recall_keywords": kw},
    ]}
    out = json.loads(rs._set_recall_summaries_impl(json.dumps(payload)))
    assert out["applied"] == 2 and out["failed"] == 1, out
    assert [e["node_id"] for e in out["errors"]] == [ids[1]]
    assert "U+0000" in out["errors"][0]["error"]
    c = _raw(core)
    got = {r["id"]: r["recall_summary"] for r in c.execute("SELECT id, recall_summary FROM nodes WHERE id IN (?, ?, ?)", ids)}
    assert got == {ids[0]: "new one", ids[1]: "old summary", ids[2]: "new three"}


def test_rung_constant_is_tied_to_the_pragma_literal_and_the_latest_version():
    """A renumber edits TEXT_NUL_GUARD_RUNG, the `PRAGMA user_version = N` literal the repo's
    LATEST_SCHEMA_VERSION oracle parses, and LATEST_SCHEMA_VERSION; this fails if only some of
    the three moved."""
    src = (SRC_DIR / "engram_core.py").read_text(encoding="utf-8")
    rung_block = src.split("# Migration version 9 (#3765", 1)[1].split("# #2732 (A)", 1)[0]
    assert f'conn.execute("PRAGMA user_version = {engram_core.TEXT_NUL_GUARD_RUNG}")' in rung_block
    assert "_rung_ready(conn, TEXT_NUL_GUARD_RUNG," in rung_block
    assert engram_core.LATEST_SCHEMA_VERSION >= engram_core.TEXT_NUL_GUARD_RUNG


# ----------------------------------------------------------------- 5


def test_cell5_owner_pin_matches_sqlite_master_and_admitted_sets_are_unchanged(core):
    eo = importlib.import_module("engramd_owner")
    c = _raw(core)
    live = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'")}
    assert live == set(eo.USE_REWARD_NODES_TRIGGERS_PINNED), live ^ set(eo.USE_REWARD_NODES_TRIGGERS_PINNED)
    assert set(core._TEXT_NUL_GUARD_TRIGGER_NAMES) <= set(eo.USE_REWARD_NODES_TRIGGERS_PINNED)
    assert len(core._TEXT_NUL_GUARD_TRIGGER_NAMES) == 2
    # Neither trigger fires on the columns use_reward / moment_cursor write.
    assert eo.USE_REWARD_ADMITTED_TRIGGERS == frozenset()
    assert eo.FILL_EMBEDDINGS_ADMITTED_TRIGGERS == frozenset()
    assert eo.FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS == frozenset()


# ----------------------------------------------------------------- 6


def test_cell6_guard_trigger_sql_does_not_mention_nodes_fts(core):
    c = _raw(core)
    rows = c.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes' AND sql LIKE ?",
        (GUARD_NEEDLE,),
    ).fetchall()
    assert len(rows) == 2
    for r in rows:
        assert "nodes_fts" not in r["sql"].lower(), r["name"]


def test_cell6b_guard_shape_is_a_byte_level_test_with_a_column_scoped_update_trigger(core):
    """Textual shape of the ruling's DDL (kept apart from the behaviour cells so a
    behaviour mutation's red list is not muddied by this one)."""
    c = _raw(core)
    rows = c.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes' AND sql LIKE ?",
        (GUARD_NEEDLE,),
    ).fetchall()
    sqls = {r["name"]: r["sql"] for r in rows}
    assert all("CAST(NEW.claim AS BLOB)" in s and "CAST(NEW.recall_summary AS BLOB)" in s for s in sqls.values())
    assert any("UPDATE OF claim, recall_summary ON nodes" in s for s in sqls.values())
    upd = sqls["nodes_text_nul_guard_update"]
    assert "NEW.claim IS NOT OLD.claim" in upd and "NEW.recall_summary IS NOT OLD.recall_summary" in upd
    assert "OLD." not in sqls["nodes_text_nul_guard_insert"]
    assert any("BEFORE INSERT ON nodes" in s for s in sqls.values())


# ----------------------------------------------------------------- predicate parity
# The observation tool's pre-check must use the SAME rule as the trigger, not a second
# spelling of it: one Python predicate, tied here to the trigger's own WHEN text.

NUL_CORPUS = [
    None, "", "plain ascii", "café 中文 \U0001f600", "x\\x00 literal backslash",
    NUL + "ab", "a" + NUL + "b", "ab" + NUL, NUL, NUL * 3,
    b"", b"plain bytes", b"\xff\xfeblob", b"\x00ab", b"a\x00b", b"ab\x00", bytearray(b"a\x00b"),
]


def _sql_says_nul(core, value, column):
    """Evaluate the shipped WHEN constant itself, with NEW aliased to a one-row subquery."""
    other = "recall_summary" if column == "claim" else "claim"
    row = sqlite3.connect(":memory:").execute(
        f"SELECT CASE WHEN {core._TEXT_NUL_GUARD_WHEN} THEN 1 ELSE 0 END "
        f"FROM (SELECT ? AS {column}, NULL AS {other}) AS NEW", (value,)).fetchone()
    return bool(row[0])


@pytest.mark.parametrize("column", ["claim", "recall_summary"])
@pytest.mark.parametrize("value", NUL_CORPUS, ids=[repr(v)[:24] for v in NUL_CORPUS])
def test_predicate_parity_python_text_has_nul_agrees_with_the_trigger_when(core, value, column):
    assert core._text_has_nul(value) is _sql_says_nul(core, value, column)


def test_predicate_parity_any_of_several_values_and_none():
    assert engram_core._text_has_nul() is False
    assert engram_core._text_has_nul(None, "ok", 7) is False
    assert engram_core._text_has_nul("ok", "b" + NUL) is True


def test_observation_pre_check_uses_the_shared_predicate_and_message():
    src = (SRC_DIR / "engram_observation.py").read_text(encoding="utf-8")
    assert "core._text_has_nul(claim, recall_summary)" in src
    assert "core._TEXT_NUL_GUARD_MSG" in src
    assert "\\x00" not in src


# ----------------------------------------------------------------- rung fault injection / self-heal gate
# Style of tests/test_created_at_index_3766.py::test_c4: a real engine-level fault, then the
# next start. These keep the self-heal from masking the rung: it must neither create
# triggers on a store whose rung failed, nor be the only thing that creates them.


def _setup_on(core, conn):
    core._run_db_one_time_setup(conn)
    conn.commit()
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _deny_create_trigger(conn, name="nodes_text_nul_guard_insert"):
    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_CREATE_TRIGGER and arg1 == name:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(auth)


def _fail_the_count(core, monkeypatch):
    def boom(conn):
        raise sqlite3.OperationalError("injected: count failed")
    monkeypatch.setattr(core, "_count_text_nul_rows", boom)


@pytest.mark.parametrize("fault", ["trigger_creation", "count"])
def test_fault_a_failed_rung_is_loud_stays_at_the_previous_version_and_the_next_start_completes(
        core, monkeypatch, capsys, fault):
    _rewind_to_pre_rung(core)
    prev = core.TEXT_NUL_GUARD_RUNG - 1
    conn = _raw(core)
    if fault == "trigger_creation":
        _deny_create_trigger(conn)
    else:
        _fail_the_count(core, monkeypatch)
    capsys.readouterr()
    assert _setup_on(core, conn) == prev  # did not raise
    err = capsys.readouterr().err
    conn.set_authorizer(None)
    conn.close()
    line = [ln for ln in err.splitlines() if "FAILED" in ln and "#3765" in ln]
    assert len(line) == 1 and "will retry" in line[0] and f"migration {core.TEXT_NUL_GUARD_RUNG}" in line[0], err
    c = _raw(core)
    assert c.execute("PRAGMA user_version").fetchone()[0] == prev
    assert _guard_triggers(c) == []
    c.close()
    monkeypatch.undo()
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION  # next start, fault gone
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2
    c.close()


def test_fault_b_self_heal_is_gated_on_the_live_version_and_does_not_run_on_a_store_whose_rung_failed(
        core, monkeypatch):
    """The fault is in the COUNT, so nothing stops a self-heal that ran anyway from creating the
    triggers: they must be absent after the failed start."""
    _rewind_to_pre_rung(core)
    _fail_the_count(core, monkeypatch)
    conn = _raw(core)
    assert _setup_on(core, conn) == core.TEXT_NUL_GUARD_RUNG - 1
    conn.close()
    c = _raw(core)
    assert _guard_triggers(c) == [], "self-heal created triggers on a store still below the rung"


def test_fault_b_at_the_rung_version_the_self_heal_recreates_dropped_triggers(core):
    c = _raw(core)
    _drop_guard_triggers(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] >= core.TEXT_NUL_GUARD_RUNG
    c.close()
    _reopen_setup(core)
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2


def test_fault_c_the_rung_alone_creates_the_triggers_with_the_self_heal_disabled(core, monkeypatch):
    """The wrapper makes the creation helper a no-op once user_version has reached the rung, i.e.
    for the self-heal; the rung runs it at the previous version."""
    _rewind_to_pre_rung(core)
    real = core._ensure_text_nul_guard_triggers

    def only_while_below_the_rung(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] < core.TEXT_NUL_GUARD_RUNG:
            real(conn)

    monkeypatch.setattr(core, "_ensure_text_nul_guard_triggers", only_while_below_the_rung)
    assert _reopen_setup(core) == core.LATEST_SCHEMA_VERSION
    c = _raw(core)
    assert len(_guard_triggers(c)) == 2, "after the rung alone the triggers must exist"


def test_rung_messages_take_the_number_from_the_constant(core, monkeypatch, capsys):
    n = str(core.TEXT_NUL_GUARD_RUNG)
    _plant_nul_rows(core)
    capsys.readouterr()
    _reopen_setup(core)
    count_line = [ln for ln in capsys.readouterr().err.splitlines() if "#3765" in ln]
    assert len(count_line) == 1 and f"migration {n} " in count_line[0]
    # and the failure message (the 'fault_a' cell checks it too); here with a different number
    monkeypatch.setattr(core, "TEXT_NUL_GUARD_RUNG", 77)
    monkeypatch.setattr(core, "LATEST_SCHEMA_VERSION", 77)
    c = _raw(core)
    c.execute("PRAGMA user_version = 76")
    c.commit()
    _fail_the_count(core, monkeypatch)
    capsys.readouterr()
    _setup_on(core, c)
    assert "migration 77 " in capsys.readouterr().err


# ----------------------------------------------------------------- one row predicate, one pass in diagnose


def test_drift_the_row_predicate_is_spelled_once_in_engram_core_and_never_in_stats():
    """Re-spelling the row rule anywhere else (the NEW.-prefixed trigger WHEN is a different expression
    and is tied to the Python predicate by the corpus cell) fails here."""
    halves = ["instr(CAST(claim AS BLOB), x'00')", "instr(CAST(recall_summary AS BLOB), x'00')"]
    for half in halves:
        homes = {p.name: p.read_text(encoding="utf-8").count(half) for p in sorted(SRC_DIR.glob("*.py"))}
        assert {k: v for k, v in homes.items() if v} == {"engram_core.py": 1}, (half, homes)
    assert "x'00'" not in (SRC_DIR / "engram_stats.py").read_text(encoding="utf-8")


def _plant_n_nul_rows(core, n):
    c = _raw(core)
    _drop_guard_triggers(c)
    for i in range(n, 0, -1):  # inserted out of id order on purpose
        _insert_node(c, f"ob_7{i:03d}", claim=f"bad{NUL}{i}")
    c.close()


def _statements_reading_the_nul_predicate(core, monkeypatch):
    stats = importlib.import_module("engram_stats")
    seen = []
    real_open = core._get_db

    def traced():
        conn = real_open()
        conn.set_trace_callback(lambda sql: seen.append(sql))
        return conn

    monkeypatch.setattr(core, "_get_db", traced)
    out = json.loads(stats._diagnose_impl())["text_nul_guard"]
    return out, [s for s in seen if "x'00'" in s and "FROM nodes" in s]


def test_diagnose_reads_the_nul_predicate_in_one_statement_on_a_clean_store(core, monkeypatch):
    out, stmts = _statements_reading_the_nul_predicate(core, monkeypatch)
    assert len(stmts) == 1, stmts
    assert out["live_count"] == 0 and out["sample_ids"] == [] and out["flare"] is False


def test_diagnose_reads_the_nul_predicate_in_one_statement_with_12_nul_rows(core, monkeypatch):
    _plant_n_nul_rows(core, 12)
    out, stmts = _statements_reading_the_nul_predicate(core, monkeypatch)
    assert len(stmts) == 1, stmts
    assert out["live_count"] == 12 and out["flare"] is True and out["entity_kind"] == "node"
    assert out["sample_ids"] == [f"ob_7{i:03d}" for i in range(1, 11)]  # 10, in id order


def test_count_helper_keeps_the_count_id_shape(core):
    """test_2181's proxy matches the substring 'SELECT COUNT(*) FROM nodes'."""
    src = (SRC_DIR / "engram_core.py").read_text(encoding="utf-8")
    body = src.split("def _count_text_nul_rows", 1)[1].split("\ndef ", 1)[0]
    assert "SELECT COUNT(id) FROM nodes WHERE" in body and "COUNT(*)" not in body
