"""Behaviour contract for the remaining `json_extract(metadata, ...)` read
sites (#3485, follow-up to #3425 / #3484). Cells C1-C5 of the spec; the
static class guard (C6) lives in test_json_extract_metadata_guard_3485.py.

Written BEFORE the fix (contract-first; project CLAUDE.md, "Contract-first
tests"). The defect: a non-JSON blob in `nodes.metadata` makes SQLite's
`json_extract(metadata, ...)` raise `OperationalError: malformed JSON` for the
WHOLE query. The lesson-tripwire hook loaders end in `except Exception:
return []`, so the failure is silent: ONE bad blob silences the entire
tripwire layer (every lesson) on every prompt. The fix is the inner guard
`json_extract(CASE WHEN json_valid(metadata) THEN metadata END, ...)`.

Cell design: every red cell has a CONTROL on the same fixture with the bad row
removed (green on the unguarded code), so a red is about the blob and nothing
else. C3 (clean-data equivalence) and the non-object cells (C2: measured on
the unguarded code, none of `[1,2]`, `"str"`, `5`, `null`, SQL NULL aborts a
json_extract of a `$.key` path) are GREEN on the old code by design.

Mutations each cell turns red under are named in the docstrings.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
RECONCILE_PATH = REPO_ROOT / "tools" / "reconcile_lesson_demotions.py"
TEMPLATES_PATH = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hook():
    return _load("lesson_tripwire_hook_3485", HOOK_PATH)


GOOD_META = {
    "situation_pattern": "git push --force",
    "prevention_type": "shadow",
    "scaffolding_nudge": "do not force push",
    "tripwire_priority": "high",
}
GOOD_TRIPWIRE = ("git push --force", "do not force push", "ls_good", "high")
GOOD_PREVENTION = {
    "id": "ls_good",
    "pattern": "git push --force",
    "prevention_type": "shadow",
    "nudge": "do not force push",
}

BAD_BLOBS = [
    pytest.param("not json {{", id="garbage"),
    pytest.param("", id="empty-string"),
    pytest.param('{"situation_pattern": "x"', id="truncated-object"),
]
# Valid JSON that is not an object, plus SQL NULL. MEASURED on the unguarded
# code: none of these aborts a `$.key` json_extract (SQLite returns NULL).
NON_OBJECT_BLOBS = [
    pytest.param("[1,2]", id="array"),
    pytest.param('"str"', id="string"),
    pytest.param("5", id="number"),
    pytest.param("null", id="json-null-text"),
    pytest.param(None, id="sql-null"),
]


def _make_db(tmp_path, rows, name="k.db"):
    """A scratch store with a `nodes` table carrying only the columns the loaders
    read. No write-guard triggers exist on it, so a bad blob can be inserted
    (the guard lives in engram_core's setup, which this fixture never runs)."""
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT, type TEXT, is_current INTEGER, "
        "memory_status TEXT, claim TEXT, metadata TEXT)"
    )
    for r in rows:
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return db


def _good_row():
    return ("ls_good", "lesson", 1, "active", "good claim", json.dumps(GOOD_META))


def _bad_row(blob):
    return ("ls_bad", "lesson", 1, "active", "bad claim", blob)


# --- C1: the loaders survive a bad blob ---------------------------------------

def test_c1_control_good_row_alone_is_returned_by_both_loaders(hook, tmp_path):
    """CONTROL (green on the unguarded code): the bad row removed, each loader
    returns the good row. Pins the exact values the bad-blob cells compare to."""
    db = _make_db(tmp_path, [_good_row()])
    assert hook.load_tripwires(db) == [GOOD_TRIPWIRE]
    assert hook.load_prevention_lessons(db) == [GOOD_PREVENTION]


@pytest.mark.parametrize("blob", BAD_BLOBS)
def test_c1_load_tripwires_survives_a_bad_blob(hook, tmp_path, blob):
    """Mutation: revert the guard in `_QUERY`, or guard only its SELECT list
    (WHERE bare) -> the query aborts, the loader's `except Exception` returns []
    -> red (the good lesson vanishes too). Guarding only the WHERE lines is
    behaviourally sufficient (the bad row never passes WHERE); the drift-guard
    test_json_extract_metadata_guard_3485.py catches that shape, not this cell."""
    db = _make_db(tmp_path, [_good_row(), _bad_row(blob)])
    assert hook.load_tripwires(db) == [GOOD_TRIPWIRE]


@pytest.mark.parametrize("blob", BAD_BLOBS)
def test_c1_load_prevention_lessons_survives_a_bad_blob(hook, tmp_path, blob):
    """Mutation: revert the guard in `_PREVENTION_QUERY`, or guard only its
    SELECT list (WHERE bare) -> red; `load_tripwires` cells stay green. A
    WHERE-only guard is caught by the drift-guard test, not this cell."""
    db = _make_db(tmp_path, [_good_row(), _bad_row(blob)])
    assert hook.load_prevention_lessons(db) == [GOOD_PREVENTION]


@pytest.mark.parametrize("blob", BAD_BLOBS)
def test_c1_the_fixture_really_aborts_the_unguarded_expression(tmp_path, blob):
    """Non-vacuity: on this fixture the bare expression raises, so the cells
    above are about the blob. (Independent of any code under test.)"""
    db = _make_db(tmp_path, [_good_row(), _bad_row(blob)])
    conn = sqlite3.connect(str(db))
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        conn.execute("SELECT json_extract(metadata, '$.situation_pattern') FROM nodes").fetchall()
    conn.close()


# --- C2: valid-JSON non-objects and NULL --------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_c2_non_object_blob_does_not_abort_and_is_not_returned(hook, tmp_path, blob):
    """MEASURED behaviour (green on the unguarded code, pinned so the guard
    cannot change it): the good row comes back from both loaders and the
    odd row is absent. Mutation: make the guard return something non-NULL for
    a non-object (e.g. `CASE WHEN json_valid(metadata) THEN metadata ELSE '{}'
    END` with a default pattern) -> the odd row appears -> red."""
    db = _make_db(tmp_path, [_good_row(), _bad_row(blob)])
    assert hook.load_tripwires(db) == [GOOD_TRIPWIRE]
    assert hook.load_prevention_lessons(db) == [GOOD_PREVENTION]


# --- C3: no behaviour change on clean data ------------------------------------

# The pre-change statements, verbatim (origin/dev at the time of the fix). The
# equivalence cell swaps them into the SAME loader code, so the ONLY thing that
# differs between "before" and "after" is the SQL text.
OLD_QUERY = """
SELECT
    id,
    COALESCE(
        json_extract(metadata, '$.scaffolding_nudge'),
        json_extract(metadata, '$.anchor_line'),
        json_extract(metadata, '$.surfacing_nudge'),
        claim
    ) AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern,
    json_extract(metadata, '$.tripwire_priority')  AS priority
FROM nodes
WHERE type IN ('lesson', 'cornerstone', 'axiom', 'goal')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""

OLD_PREVENTION_QUERY = """
SELECT
    id,
    json_extract(metadata, '$.situation_pattern')  AS pattern,
    json_extract(metadata, '$.prevention_type')    AS prevention_type,
    COALESCE(json_extract(metadata, '$.scaffolding_nudge'), claim) AS nudge
FROM nodes
WHERE type = 'lesson'
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
  AND json_extract(metadata, '$.prevention_type') IN ('shadow', 'blocking')
"""


def _m(**kw):
    return json.dumps(kw)


CLEAN_CORPUS = [
    # priority high / normal / absent
    ("c_high", "lesson", 1, "active", "claim-a", _m(situation_pattern="p-high", scaffolding_nudge="n-a", tripwire_priority="high", prevention_type="shadow")),
    ("c_norm", "lesson", 1, "active", "claim-b", _m(situation_pattern="p-norm", scaffolding_nudge="n-b", tripwire_priority="normal", prevention_type="blocking")),
    ("c_abs", "lesson", 1, "active", "claim-c", _m(situation_pattern="p-abs", scaffolding_nudge="n-c")),
    # the three nudge fallbacks: anchor_line, surfacing_nudge, claim
    ("c_anchor", "lesson", 1, "active", "claim-d", _m(situation_pattern="p-anchor", anchor_line="anchor-d", prevention_type="shadow")),
    ("c_surf", "lesson", 1, "active", "claim-e", _m(situation_pattern="p-surf", surfacing_nudge="surf-e", prevention_type="blocking")),
    ("c_claim", "lesson", 1, "active", "claim-f", _m(situation_pattern="p-claim", prevention_type="shadow")),
    # empty-string pattern (excluded)
    ("c_empty", "lesson", 1, "active", "claim-g", _m(situation_pattern="", prevention_type="shadow")),
    # types in _QUERY's list that are not lessons (in _QUERY, never in _PREVENTION_QUERY)
    ("cs_1", "cornerstone", 1, "active", "claim-h", _m(situation_pattern="p-cs", scaffolding_nudge="n-cs")),
    ("ax_1", "axiom", 1, "active", "claim-i", _m(situation_pattern="p-ax")),
    ("gl_1", "goal", 1, "active", "claim-j", _m(situation_pattern="p-gl", prevention_type="shadow")),
    # a type outside the list
    ("ob_1", "observation", 1, "active", "claim-k", _m(situation_pattern="p-ob", prevention_type="shadow")),
    # memory_status not active / is_current 0
    ("c_faded", "lesson", 1, "faded", "claim-l", _m(situation_pattern="p-faded", prevention_type="shadow")),
    ("c_old", "lesson", 0, "active", "claim-m", _m(situation_pattern="p-old", prevention_type="shadow")),
    # a non-shadow/blocking prevention_type, and NULL metadata
    ("c_adv", "lesson", 1, "active", "claim-n", _m(situation_pattern="p-adv", prevention_type="advisory")),
    ("c_null", "lesson", 1, "active", "claim-o", None),
    # a valid non-object (valid JSON, so unchanged by the guard)
    ("c_arr", "lesson", 1, "active", "claim-p", "[1,2]"),
]


def test_c3_clean_data_loaders_are_identical_to_the_pre_change_sql(hook, tmp_path, monkeypatch):
    """GREEN on old code by design. Same loader code, the old statement text
    swapped in via the module constant, over a corpus with every branch of the
    two statements. Mutation: change the guard's THEN/ELSE (e.g. return '{}'
    for invalid) or drop `claim` from the COALESCE -> the outputs differ -> red."""
    db = _make_db(tmp_path, CLEAN_CORPUS)
    new_t, new_p = hook.load_tripwires(db), hook.load_prevention_lessons(db)
    monkeypatch.setattr(hook, "_QUERY", OLD_QUERY)
    monkeypatch.setattr(hook, "_PREVENTION_QUERY", OLD_PREVENTION_QUERY)
    old_t, old_p = hook.load_tripwires(db), hook.load_prevention_lessons(db)
    assert new_t == old_t and new_p == old_p
    # Non-vacuity: the corpus exercises the branches (not two empty lists).
    by_id = {t[2]: t for t in new_t}
    assert by_id["c_high"][3] == "high" and by_id["c_norm"][3] == "normal"
    assert by_id["c_abs"][3] == "normal"  # NULL priority coerced by the loader
    assert by_id["c_anchor"][1] == "anchor-d"
    assert by_id["c_surf"][1] == "surf-e"
    assert by_id["c_claim"][1] == "claim-f"
    assert {"cs_1", "ax_1", "gl_1"} <= set(by_id)
    assert not {"c_empty", "ob_1", "c_faded", "c_old", "c_null", "c_arr"} & set(by_id)
    # _PREVENTION_QUERY is lessons only (the goal gl_1 is not in it) and only
    # shadow/blocking (c_adv is not).
    assert {p["id"] for p in new_p} == {"c_high", "c_norm", "c_anchor", "c_surf", "c_claim"}


def test_c3_the_loaders_keep_their_fail_soft_except(hook, tmp_path):
    """The loaders' `except Exception: return []` is deliberately UNCHANGED
    (it is why the original failure was silent). Pinned: a store with no
    `nodes` table still yields [] rather than raising."""
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    assert hook.load_tripwires(db) == []
    assert hook.load_prevention_lessons(db) == []


# --- C4: the reconcile tool ---------------------------------------------------

@pytest.fixture
def reconcile():
    return _load("reconcile_lesson_demotions_3485", RECONCILE_PATH)


def _lesson_row(lesson_id, meta_text):
    return (lesson_id, "lesson", 1, "active", "claim", meta_text)


def _hot_ledger(path, lesson_ids, now):
    with open(path, "w") as f:
        for lid in lesson_ids:
            for entry in (
                {"ts": now - 3600, "lesson_id": lid, "mode": "blocking", "override_present": True},
                {"ts": now - 1800, "lesson_id": lid, "mode": "blocking", "override_present": True},
                {"ts": now - 1800, "lesson_id": lid, "mode": "auto_demoted_lease",
                 "override_count": 2, "threshold": 2, "window_days": 7},
            ):
                f.write(json.dumps(entry) + "\n")


def test_c4_control_healthy_blocking_lesson_alone_is_a_candidate(reconcile, tmp_path):
    """CONTROL (green on the unguarded code)."""
    now = time.time()
    db = _make_db(tmp_path, [_lesson_row("ls_ok", json.dumps({"prevention_type": "blocking"}))])
    ledger = tmp_path / "ledger.jsonl"
    _hot_ledger(ledger, ["ls_ok"], now)
    out = reconcile.find_candidates(db_path=db, ledger_path=ledger, now=now)
    assert [c["lesson_id"] for c in out] == ["ls_ok"]


@pytest.mark.parametrize("blob", BAD_BLOBS)
def test_c4_bad_blob_on_a_ledgered_lesson_is_skipped_not_raised(reconcile, tmp_path, blob):
    """Mutation: revert the guard on the reconcile query -> the per-lesson
    SELECT raises OperationalError out of find_candidates -> red."""
    now = time.time()
    db = _make_db(tmp_path, [
        _lesson_row("ls_ok", json.dumps({"prevention_type": "blocking"})),
        _lesson_row("ls_zbad", blob),
    ])
    ledger = tmp_path / "ledger.jsonl"
    _hot_ledger(ledger, ["ls_ok", "ls_zbad"], now)
    out = reconcile.find_candidates(db_path=db, ledger_path=ledger, now=now)
    assert [c["lesson_id"] for c in out] == ["ls_ok"]


def test_c4_bad_blob_sorted_BEFORE_the_healthy_lesson_does_not_hide_it(reconcile, tmp_path):
    """The ids are walked in sorted order; the bad row comes first here, so an
    implementation that stops at (rather than skips) the bad row loses the
    healthy one. Mutation: `break` instead of `continue` on a bad row -> red."""
    now = time.time()
    db = _make_db(tmp_path, [
        _lesson_row("ls_abad", "not json {{"),
        _lesson_row("ls_ok", json.dumps({"prevention_type": "blocking"})),
    ])
    ledger = tmp_path / "ledger.jsonl"
    _hot_ledger(ledger, ["ls_abad", "ls_ok"], now)
    out = reconcile.find_candidates(db_path=db, ledger_path=ledger, now=now)
    assert [c["lesson_id"] for c in out] == ["ls_ok"]


def test_c4_ledger_read_errors_still_raise_reconcile_error(reconcile, tmp_path):
    """Other contract preserved: an existing-but-unreadable ledger (here a
    directory) still raises ReconcileError, never an empty candidate list."""
    db = _make_db(tmp_path, [_lesson_row("ls_ok", json.dumps({"prevention_type": "blocking"}))])
    ledger = tmp_path / "ledger_dir"
    ledger.mkdir()
    with pytest.raises(reconcile.ReconcileError):
        reconcile.find_candidates(db_path=db, ledger_path=ledger, now=time.time())


# --- C5: templates regenerated; end to end through the owner's read gate ------

def _templates():
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))["templates"]


def _template_for(site_suffix):
    found = [t for t in _templates() if any(s.endswith(site_suffix) for s in t["sites"])]
    assert len(found) == 1, (site_suffix, [t["sites"] for t in found])
    return found[0]


def test_c5_registered_templates_are_the_hook_statements_verbatim(hook):
    """Registry coherence. Mutation: hand-edit one template's SQL (or change
    the hook SQL and skip regeneration) -> red here AND in the pre-existing
    test_engramd_read_scan r12 drift gate."""
    tw = _template_for("engram-lesson-tripwire-hook.py:load_tripwires")
    pv = _template_for("engram-lesson-tripwire-hook.py:load_prevention_lessons")
    assert tw["sql"] == hook._QUERY
    assert pv["sql"] == hook._PREVENTION_QUERY
    for t in (tw, pv):
        assert t["key"] == rr.template_key(t["sql"])
    assert "json_valid" in tw["sql"] and "json_valid" in pv["sql"]


def _real_store_with_bad_blob(home: Path, rows):
    """knowledge.db with the REAL nodes schema, then the write-guard triggers
    dropped so a bad blob can be planted."""
    import os
    import engram_core

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        for name in engram_core._METADATA_GUARD_TRIGGER_NAMES:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for nid, ntype, claim, meta in rows:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current) "
                "VALUES (?, ?, ?, '2026-01-01T00:00:00', ?, 1)",
                (nid, ntype, claim, meta),
            )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


@pytest.fixture
def owner_home():
    d = Path(tempfile.mkdtemp(prefix="je3485-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _owner_read(owner, sql):
    return ec.read("read", {"sql": sql, "args": []}, timeout=2.0, socket_path=owner.socket_path)


@requires_linux_owner
@pytest.mark.parametrize("site,expected", [
    ("engram-lesson-tripwire-hook.py:load_tripwires",
     [["ls_good", "do not force push", "git push --force", "high"]]),
    ("engram-lesson-tripwire-hook.py:load_prevention_lessons",
     [["ls_good", "git push --force", "shadow", "do not force push"]]),
])
def test_c5_registered_template_reads_through_the_owner_with_a_bad_blob_present(owner_home, site, expected):
    """End to end through the REAL owner's `read` gate (the path the
    owner-read tests use) against the REAL committed template list and a REAL
    nodes schema holding one bad blob. Mutation: revert that template's guard
    (regenerate the file from the reverted hook) -> the gate's execute aborts
    -> the client does not get the good row -> red."""
    sql = _template_for(site)["sql"]
    _real_store_with_bad_blob(owner_home, [
        ("ls_good", "lesson", "good claim", json.dumps(GOOD_META)),
        ("ls_bad", "lesson", "bad claim", "not json {{"),
    ])
    owner = eo.EngramdOwner(owner_home, request_deadline=2.0)
    owner.start(install_signal_handlers=False)
    try:
        result = _owner_read(owner, sql)
    finally:
        owner.stop()
    assert isinstance(result, ec.Ok), result
    assert result.result == expected


@requires_linux_owner
@pytest.mark.parametrize("site", [
    "engram-lesson-tripwire-hook.py:load_tripwires",
    "engram-lesson-tripwire-hook.py:load_prevention_lessons",
])
def test_c5_control_same_read_without_the_bad_blob_returns_the_good_row(owner_home, site):
    """CONTROL (green on the unguarded code): the bad row removed."""
    sql = _template_for(site)["sql"]
    _real_store_with_bad_blob(owner_home, [("ls_good", "lesson", "good claim", json.dumps(GOOD_META))])
    owner = eo.EngramdOwner(owner_home, request_deadline=2.0)
    owner.start(install_signal_handlers=False)
    try:
        result = _owner_read(owner, sql)
    finally:
        owner.stop()
    assert isinstance(result, ec.Ok), result
    assert len(result.result) == 1 and result.result[0][0] == "ls_good"
