"""Contract cells for the shared moment-cursor advance (#3856, U13 of #2652, phase 1).

Written BEFORE the implementation (contract-first; project CLAUDE.md). The contract:

  engram_moment_cursor.read_cursor(value) -> int
      THE Python cursor rule: an int that is NOT a bool and lies in [-(2**53), 2**53), else 0.
  engram_moment_cursor.CURSOR_SQL_EXPR
      the SQL expression for the STORED cursor under the SAME rule.
  engram_moment_cursor.advance_moment_cursor(conn, person_id, from_cursor)
      -> {"applied": bool, "cursor": int | None, "reason": None | "stale" | "not_person" | "not_object"}
      ONE compare-and-set UPDATE (json_set of the one key), never rewriting another key, never committing.
      from_cursor: a real int, not bool, in [-(2**53), 2**53), else ValueError.

Both existing direct callers (the surface hook, engram_query) run it; the old full-dict rewrite is gone.
Self-contained: the store is built with the real schema under tmp; nothing touches ~/.engram.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402

LO, HI = -(2 ** 53), 2 ** 53  # [LO, HI)
HOOK_PATH = Path(_SRC_ENGRAM) / "hooks" / "claude" / "engram-surface-hook.py"


def _mod():
    import engram_moment_cursor
    return engram_moment_cursor


def make_store(home: Path) -> Path:
    live = (Path.home() / ".engram").resolve()
    h = Path(home).resolve()
    assert h != live and live not in h.parents, h
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def put(db, nid, metadata_text, *, type_="person", is_current=1):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status)"
        " VALUES (?,?,?,?,?,?,?)",
        (nid, type_, f"claim {nid}", "2026-01-01T00:00:00", metadata_text, is_current, "active"))
    conn.commit()
    conn.close()


def raw_meta(db, nid):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT metadata FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    finally:
        conn.close()


def whole_row(db, nid):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    finally:
        conn.close()


def direct_write(db, nid, key, value):
    """A concurrent writer (engram_update_person-like): sets one key through its own connection."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET metadata = json_set(metadata, ?, ?) WHERE id = ?",
                 ("$." + key, value, nid))
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    return make_store(tmp_path)


def advance(db, nid, frm):
    m = _mod()
    conn = sqlite3.connect(str(db))
    try:
        r = m.advance_moment_cursor(conn, nid, frm)
        conn.commit()
        return r
    finally:
        conn.close()


MOMENTS = [{"description": f"Moment {i}."} for i in range(3)]


def person_meta(**extra):
    d = {"name": "Zephyr", "special_moments": MOMENTS}
    d.update(extra)
    return json.dumps(d)


# --- (1) concurrent-field survival -------------------------------------------------------------------


def test_shared_function_concurrent_field_survives(db):
    put(db, "pn_1", person_meta(special_moment_cursor=2))
    stale_read = json.loads(raw_meta(db, "pn_1"))
    frm = stale_read["special_moment_cursor"]
    direct_write(db, "pn_1", "pronouns", "they/them")          # lands between the caller's read and the advance
    r = advance(db, "pn_1", frm)
    assert r == {"applied": True, "cursor": 3, "reason": None}
    after = json.loads(raw_meta(db, "pn_1"))
    assert after["pronouns"] == "they/them"
    assert after["special_moment_cursor"] == 3


class _InterleavingConn:
    """Proxy: right before the FIRST UPDATE statement, a second connection writes `pronouns`. Against the old
    read-modify-write (SELECT, then UPDATE of the whole stale dict) that write is lost; against one atomic
    statement it cannot be."""

    def __init__(self, real, db, nid):
        self._real, self._db, self._nid, self._fired = real, db, nid, False

    def execute(self, sql, *a, **k):
        if not self._fired and sql.lstrip().upper().startswith("UPDATE"):
            self._fired = True
            direct_write(self._db, self._nid, "pronouns", "she/her")
        return self._real.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _load_hook(db, monkeypatch):
    spec = importlib.util.spec_from_file_location("engram_surface_hook_3856", str(HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "KNOWLEDGE_DB_PATH", str(db))
    monkeypatch.setattr(mod, "_person_moment_in_cooldown", lambda pid, *a, **k: False)
    monkeypatch.setattr(mod, "_record_person_moment_render", lambda pid: None)
    return mod


def test_hook_caller_concurrent_field_survives(db, monkeypatch):
    put(db, "pn_1", person_meta(special_moment_cursor=2))
    hook = _load_hook(db, monkeypatch)
    real = sqlite3

    class _Shim:
        def __getattr__(self, n):
            return getattr(real, n)

        @staticmethod
        def connect(*a, **k):
            return _InterleavingConn(real.connect(*a, **k), db, "pn_1")

    monkeypatch.setattr(hook, "sqlite3", _Shim())
    cand = hook._select_person_moment_candidate(
        {"id": "pn_1", "special_moments": MOMENTS, "special_moment_cursor": 2})
    assert cand is not None and cand[0] == "Moment 2."
    hook._commit_person_moment_render("pn_1", cand[1])
    after = json.loads(raw_meta(db, "pn_1"))
    assert after["pronouns"] == "she/her"
    assert after["special_moment_cursor"] == 3


def test_query_caller_concurrent_field_survives(db, monkeypatch):
    import engram_query as eq
    put(db, "pn_1", person_meta(special_moment_cursor=2))
    monkeypatch.setattr(eq, "_person_moment_in_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(eq, "_record_person_moment_render", lambda pid: None)
    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    conn = _InterleavingConn(real, db, "pn_1")
    try:
        text = eq._select_and_render_person_moment(conn, "pn_1")
    finally:
        real.close()
    assert text == "Moment 2."
    after = json.loads(raw_meta(db, "pn_1"))
    assert after["pronouns"] == "she/her"
    assert after["special_moment_cursor"] == 3


# --- (2) compare-and-set -----------------------------------------------------------------------------


def test_cas_two_advances_same_from_exactly_one_applied(db):
    put(db, "pn_1", person_meta(special_moment_cursor=3))
    first = advance(db, "pn_1", 3)
    second = advance(db, "pn_1", 3)
    assert first == {"applied": True, "cursor": 4, "reason": None}
    assert second == {"applied": False, "cursor": 4, "reason": "stale"}
    assert json.loads(raw_meta(db, "pn_1"))["special_moment_cursor"] == 4


def test_cas_absent_cursor_is_zero(db):
    put(db, "pn_1", person_meta())
    assert advance(db, "pn_1", 0)["applied"] is True
    assert json.loads(raw_meta(db, "pn_1"))["special_moment_cursor"] == 1
    assert advance(db, "pn_1", 0) == {"applied": False, "cursor": 1, "reason": "stale"}


def test_stale_returns_current_cursor_under_the_rule(db):
    put(db, "pn_1", '{"special_moment_cursor": true}')
    r = advance(db, "pn_1", 5)
    assert r == {"applied": False, "cursor": 0, "reason": "stale"}   # true reads as 0, not 1


def test_replay_after_success_reports_stale_with_from_plus_one(db):
    put(db, "pn_1", person_meta(special_moment_cursor=7))
    assert advance(db, "pn_1", 7)["applied"] is True
    assert advance(db, "pn_1", 7) == {"applied": False, "cursor": 8, "reason": "stale"}


def test_function_does_not_commit(db):
    m = _mod()
    put(db, "pn_1", person_meta(special_moment_cursor=1))
    conn = sqlite3.connect(str(db))
    assert m.advance_moment_cursor(conn, "pn_1", 1)["applied"] is True
    conn.rollback()
    conn.close()
    assert json.loads(raw_meta(db, "pn_1"))["special_moment_cursor"] == 1


@pytest.mark.parametrize("bad", [True, False, 1.5, "3", None, HI, LO - 1, 2 ** 63, [1]])
def test_from_cursor_must_be_a_real_int_in_range(db, bad):
    put(db, "pn_1", person_meta(special_moment_cursor=1))
    before = whole_row(db, "pn_1")
    m = _mod()
    conn = sqlite3.connect(str(db))
    with pytest.raises(ValueError):
        m.advance_moment_cursor(conn, "pn_1", bad)
    conn.close()
    assert whole_row(db, "pn_1") == before


@pytest.mark.parametrize("edge", [LO, HI - 1, -1, 0])
def test_from_cursor_range_edges_accepted(db, edge):
    put(db, "pn_1", json.dumps({"special_moment_cursor": edge}))
    r = advance(db, "pn_1", edge)
    assert r == {"applied": True, "cursor": edge + 1, "reason": None}


# --- (3) cursor-rule parity --------------------------------------------------------------------------

# raw JSON literal stored -> value the ONE rule must give (both sides)
PARITY = [
    (None, 0),                     # key absent
    ("0", 0), ("5", 5), ("-1", -1),
    ("1.5", 0), ('"3"', 0), ("true", 0), ("false", 0), ("null", 0),
    ("1e3", 0),
    (str(HI - 1), HI - 1),
    (str(LO), LO),
    (str(HI), 0), (str(LO - 1), 0),
    (str(2 ** 63 - 1), 0),
    (str(2 ** 63), 0),             # beyond int64: SQLite json_type says integer, json_extract says a float
    (str(-(2 ** 63) - 1), 0),
]


@pytest.mark.parametrize("literal,expected", PARITY)
def test_cursor_rule_parity_python_and_sql(db, literal, expected):
    m = _mod()
    text = "{}" if literal is None else '{"special_moment_cursor": %s}' % literal
    put(db, "pn_1", text)
    py = m.read_cursor(json.loads(text).get("special_moment_cursor"))
    conn = sqlite3.connect(str(db))
    sql = conn.execute(f"SELECT {m.CURSOR_SQL_EXPR} FROM nodes WHERE id='pn_1'").fetchone()[0]
    conn.close()
    assert py == expected, (literal, py)
    assert sql == expected and type(sql) is int, (literal, sql)
    assert py == sql


def test_stored_true_still_rotates(db):
    """The defect the one rule prevents: a stored `true` must not wedge the cursor (Python sent 1, SQL read 0)."""
    put(db, "pn_1", '{"special_moments": [{"description":"a"}], "special_moment_cursor": true}')
    m = _mod()
    frm = m.read_cursor(True)
    assert advance(db, "pn_1", frm) == {"applied": True, "cursor": 1, "reason": None}


def test_read_cursor_rejects_bool_and_non_int():
    m = _mod()
    for v in (True, False, 1.0, "2", None, [], {}, b"1"):
        assert m.read_cursor(v) == 0
    assert m.read_cursor(4) == 4


# --- (4) refusals ------------------------------------------------------------------------------------


def _refusal_fixtures(db):
    put(db, "ob_1", person_meta(special_moment_cursor=1), type_="observation_factual")
    put(db, "pn_old", person_meta(special_moment_cursor=1), is_current=0)
    put(db, "pn_arr", "[1, 2]")
    put(db, "pn_null", None)
    put(db, "pn_str", '"just a string"')
    put(db, "pn_num", "5")
    put(db, "pn_s", '"s"')


@pytest.mark.parametrize("nid,reason", [
    ("ob_1", "not_person"),
    ("nope", "not_person"),
    ("pn_old", "not_person"),
    ("pn_arr", "not_object"),
    ("pn_null", "not_object"),
    ("pn_str", "not_object"),
    ("pn_num", "not_object"),
    ("pn_s", "not_object"),
])
def test_refusals_leave_row_byte_unchanged(db, nid, reason):
    """The `from` is the cursor value that WOULD match: a non-object blob reads cursor 0, so a not_object cell
    must send 0 (from=1 never matches with or without the object test and would not discriminate); the
    not_person rows store cursor 1 and are sent 1."""
    _refusal_fixtures(db)
    before = whole_row(db, nid)
    frm = 0 if reason == "not_object" else 1
    r = advance(db, nid, frm)
    assert r == {"applied": False, "cursor": None, "reason": reason}
    assert whole_row(db, nid) == before


def _drop_guards(db):
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TRIGGER nodes_metadata_json_guard_update")     # legacy rows predate the guards
    conn.execute("DROP TRIGGER nodes_metadata_json_guard_insert")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("text", ["{not json", '{"a":007}', '{"special_moment_cursor":}'])
def test_malformed_metadata_text_is_not_object_not_a_crash(db, text):
    """A malformed legacy row answers not_object without raising `malformed JSON`, row byte-unchanged -- from the
    cursor value that WOULD match (0), so the cursor predicate is genuinely in play."""
    _drop_guards(db)
    put(db, "pn_bad", text)
    before = whole_row(db, "pn_bad")
    assert advance(db, "pn_bad", 0) == {"applied": False, "cursor": None, "reason": "not_object"}
    assert whole_row(db, "pn_bad") == before


MALFORMED_TEXTS = ['{not json', '{"a":007}', '{"special_moment_cursor":}', '{"special_moment_cursor":1', '{', '}{',
                   '[1,', '"abc', 'tru']


@pytest.mark.parametrize("text", MALFORMED_TEXTS)
def test_the_self_guarded_cursor_predicate_never_raises_on_a_malformed_row_whatever_the_evaluation_order(db, text):
    """PINS the self-guard on the module constant the UPDATE actually uses (`CURSOR_GUARDED_SQL_EXPR`, spliced into
    `_ADVANCE_SQL`; no second copy). It is run ALONE and with the object test LAST, so it cannot lean on the object
    test coming first. Mutation: `CURSOR_GUARDED_SQL_EXPR = CURSOR_SQL_EXPR` (guard removed) -> every case raises
    `malformed JSON` and this goes red."""
    _drop_guards(db)
    put(db, "pn_bad", text)
    m = _mod()
    assert m.CURSOR_GUARDED_SQL_EXPR in m._ADVANCE_SQL                      # one definition, used by the statement
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT json_valid(metadata) FROM nodes WHERE id='pn_bad'").fetchone() == (0,)
        assert conn.execute("SELECT " + m.CURSOR_GUARDED_SQL_EXPR + " FROM nodes WHERE id='pn_bad'").fetchone() == (None,)
        rows = conn.execute("SELECT 1 FROM nodes WHERE id='pn_bad' AND " + m.CURSOR_GUARDED_SQL_EXPR + " = 0 AND "
                            + m._IS_OBJECT_SQL).fetchall()                   # the object test LAST
        assert rows == []
    finally:
        conn.close()


CORRUPT = {
    # label: (stored text, the cursor the PYTHON rule reads = what callers send, expected reason)
    "dup_keys_last_wins_in_python": ('{"special_moment_cursor":1,"special_moment_cursor":2}', 2, "stale"),
    "dup_keys_str_then_int": ('{"special_moment_cursor":"x","special_moment_cursor":7}', 7, "stale"),
    "escaped_key_spelling": ('{"special_moment_cursor":4,"special\\u005fmoment_cursor":8}', 8, "stale"),
    "nan_in_the_blob": ('{"special_moment_cursor":NaN}', 0, "not_object"),
    "nan_elsewhere": ('{"a":NaN,"special_moment_cursor":3}', 3, "not_object"),
}


@pytest.mark.parametrize("label", sorted(CORRUPT))
def test_the_three_corrupt_row_classes_fail_safe_the_parsers_disagree_and_nothing_applies(db, label):
    """The claim 'SQL agrees with Python' is bounded to every value json.dumps can emit. These corrupt rows
    (duplicate keys, an escaped key spelling, NaN) are read differently by json.loads and SQLite; the callers send
    the Python-side value and the statement compares the SQL-side one. Pinned as FAIL-SAFE, not reconciled: the
    advance answers applied False (stale or not_object), the row is byte-unchanged, nothing raises."""
    text, py_cursor, reason = CORRUPT[label]
    _drop_guards(db)
    put(db, "pn_c", text)
    try:
        parsed = json.loads(text)
        assert emc_read(parsed.get("special_moment_cursor")) == py_cursor      # what the callers would send
    except ValueError:
        raise AssertionError("fixture must parse in Python")
    before = whole_row(db, "pn_c")
    r = advance(db, "pn_c", py_cursor)
    assert r["applied"] is False and r["reason"] == reason, r
    assert whole_row(db, "pn_c") == before


def emc_read(v):
    return _mod().read_cursor(v)


# --- (5) rider: no other key is rewritten ------------------------------------------------------------

OTHER_KEYS_FIXTURES = {
    "ascii_escaped": json.dumps({"name": "石磊", "aliases": ["Léa", "ß"], "nested": {"a": [1, {"b": None}], "z": "é"},
                                 "special_moment_cursor": 2, "tail": 1.5}),
    "raw_non_ascii_spaced": json.dumps({"name": "石磊", "nested": {"k": "v"}, "special_moment_cursor": 0},
                                       ensure_ascii=False, indent=2),
    "cursor_absent": json.dumps({"name": "x", "pronouns": "they/them", "nested": {"deep": {"er": [1, 2, 3]}}}),
}


@pytest.mark.parametrize("name", sorted(OTHER_KEYS_FIXTURES))
def test_other_keys_unchanged_in_set_order_and_value(db, name):
    text = OTHER_KEYS_FIXTURES[name]
    put(db, "pn_1", text)
    before = json.loads(text)
    frm = _mod().read_cursor(before.get("special_moment_cursor"))
    assert advance(db, "pn_1", frm)["applied"] is True
    after = json.loads(raw_meta(db, "pn_1"))
    cur = "special_moment_cursor"
    assert [k for k in before if k != cur] == [k for k in after if k != cur]
    for k in before:
        if k != cur:
            assert after[k] == before[k], k
    assert after[cur] == frm + 1


# --- (6) differential: hook-direct vs engram_query-direct --------------------------------------------

DIFF_FIXTURES = {
    "absent": {"name": "A", "special_moments": MOMENTS},
    "two": {"name": "B", "special_moments": MOMENTS, "special_moment_cursor": 2},
    "true": {"name": "C", "special_moments": MOMENTS, "special_moment_cursor": True},
    "string": {"name": "D", "special_moments": MOMENTS, "special_moment_cursor": "3"},
    "non_ascii_nested": {"name": "石磊", "aliases": ["Léa"], "nested": {"k": [1, 2]}, "pronouns": "他",
                         "special_moments": MOMENTS, "special_moment_cursor": 7},
    "past_int64": {"name": "E", "special_moments": MOMENTS, "special_moment_cursor": 2 ** 63},
}


@pytest.mark.parametrize("name", sorted(DIFF_FIXTURES))
def test_differential_hook_direct_vs_query_direct(tmp_path, monkeypatch, name):
    import engram_query as eq
    meta = DIFF_FIXTURES[name]
    dbs = {}
    for side in ("hook", "query"):
        home = tmp_path / side
        home.mkdir()
        dbs[side] = make_store(home)
        put(dbs[side], "pn_1", json.dumps(meta))
    m = _mod()
    # hook side
    hook = _load_hook(dbs["hook"], monkeypatch)
    entry = {"id": "pn_1", "special_moments": MOMENTS,
             "special_moment_cursor": m.read_cursor(meta.get("special_moment_cursor", 0))}
    cand = hook._select_person_moment_candidate(entry)
    assert cand is not None
    hook._commit_person_moment_render("pn_1", cand[1])
    # query side
    monkeypatch.setattr(eq, "_person_moment_in_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(eq, "_record_person_moment_render", lambda pid: None)
    conn = sqlite3.connect(str(dbs["query"]))
    conn.row_factory = sqlite3.Row
    try:
        text = eq._select_and_render_person_moment(conn, "pn_1")
    finally:
        conn.close()
    assert text == cand[0]
    assert json.loads(raw_meta(dbs["hook"], "pn_1")) == json.loads(raw_meta(dbs["query"], "pn_1"))
    frm = m.read_cursor(meta.get("special_moment_cursor"))
    assert json.loads(raw_meta(dbs["hook"], "pn_1"))["special_moment_cursor"] == frm + 1


# --- hook import failure is best-effort False, never the old full-dict rewrite ------------------------


def test_hook_advance_import_failure_returns_false_and_writes_nothing(db, monkeypatch):
    put(db, "pn_1", person_meta(special_moment_cursor=2))
    hook = _load_hook(db, monkeypatch)
    monkeypatch.setattr(hook, "_advance_moment_cursor", None)
    before = whole_row(db, "pn_1")
    assert hook._advance_person_moment_cursor("pn_1", 2) is False
    assert whole_row(db, "pn_1") == before


def test_hook_advance_returns_false_when_stale_and_true_when_applied(db, monkeypatch):
    put(db, "pn_1", person_meta(special_moment_cursor=2))
    hook = _load_hook(db, monkeypatch)
    assert hook._advance_person_moment_cursor("pn_1", 2) is True
    assert hook._advance_person_moment_cursor("pn_1", 2) is False
    assert hook._advance_person_moment_cursor("missing", 0) is False
    assert json.loads(raw_meta(db, "pn_1"))["special_moment_cursor"] == 3


_SURFACE_SCRIPT = r"""
import json, os, sqlite3, sys, tempfile
sys.path.insert(0, sys.argv[1])
home = tempfile.mkdtemp(prefix="emc-surf-")
os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
os.environ["ENGRAM_NO_POLARITY"] = "1"
import server
server._configure_paths(home)
server._ensure_data_dir()
obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
    "claim": "anchor claim", "quoted_text": "anchor claim", "interpretation": "interp",
    "quote_type": "hard_data", "url": "https://example.com/anchor", "title": "anchor"})))
pid = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "Nebulaperson"})))["person_id"]
for i in range(2):
    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps(
        {"person_id": pid, "description": "moment %d" % i, "node_id": obs["observation_id"]})))
    assert "error" not in r, r
stored = json.loads(sys.argv[2])            # the raw JSON literal to store, or the string "ABSENT"
conn = sqlite3.connect(os.path.join(home, "knowledge.db"))
meta = json.loads(conn.execute("SELECT metadata FROM nodes WHERE id=?", (pid,)).fetchone()[0])
meta.pop("special_moment_cursor", None)
if stored != "ABSENT":
    meta["special_moment_cursor"] = stored
raw = json.dumps(meta)
if sys.argv[2] == "true":
    raw = raw[:-1] + ', "special_moment_cursor": true}'
    raw = raw.replace('{, ', '{')
conn.execute("UPDATE nodes SET metadata=? WHERE id=?", (raw, pid))
conn.commit()
conn.close()
out = json.loads(server.engram_surface(payload_json=json.dumps({"query": "Nebulaperson", "top_k": 10, "semantic": False})))
entry = next(e for e in out["special_nodes"] if e["id"] == pid)
print(json.dumps({"cursor": entry.get("special_moment_cursor"), "has": "special_moment_cursor" in entry}))
"""


@pytest.mark.parametrize("stored,expected", [
    ("true", 0), ('"3"', 0), ("1.5", 0), ("4", 4), ("9223372036854775808", 0), ('"ABSENT"', 0),
], ids=["bool-true", "string", "float", "int", "past-int64", "absent"])
def test_query_surface_entry_carries_the_rule_normalised_cursor(tmp_path, stored, expected):
    """BEHAVIOURAL (replaces a source-text grep): drive the real `engram_surface` path on a real store whose
    person has a stored cursor that the ONE rule reads as 0 (a bool `true`, a string, a float, a value past
    the window) and assert the entry sent to the hook carries the rule-normalised value. Run in a subprocess
    because the server test idiom clears `engram_*` modules from sys.modules. Mutation: restore the raw
    `_pmeta.get("special_moment_cursor", 0)` -> the `true` cell sends True/1 and goes red."""
    import subprocess
    root = str(Path(__file__).resolve().parent.parent / "src" / "engram")
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT")}
    out = subprocess.run([sys.executable, "-c", _SURFACE_SCRIPT, root, stored], capture_output=True, text=True,
                         env=env, timeout=120, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["has"] is True
    assert got["cursor"] == expected and type(got["cursor"]) is int, got
