"""Contract tests for the U4c read-template registry, tier 1 (#3196, part of #2652).

Written BEFORE the implementation (contract-first; project CLAUDE.md,
"Contract-first tests"). The contract is the R1-R13 refusal contract posted
on #3196 (Sol, 2026-10-01) as answered by the design owner (2026-10-02);
design source docs/2652-security-slice-design.md SS3.2-3.5, SS5. This file
covers the runtime/registration half (R1-R9, R12 loader). The build-time
scanner half (R10-R12 drift gate, R13) lives in test_engramd_read_scan.py.

The API these tests pin (module `engram.engramd_read_registry`, stdlib only):

  template_key(sql: str) -> str                 sha256(utf8 bytes).hexdigest()
  Rejected(status="rejected", reason, detail, remedy)   .as_wire() -> dict
  classify_template(sql, schema_conn) -> None | Rejected       (R5, R6, R7)
  build_registry(templates, schema_conn, param_policies=None, *, dynamic=False)
        -> Registry   (.refused: {sql: Rejected}; .templates: read-only map
                       key -> sql; .check(sql, params) -> None | Rejected)
  EMPTY_REGISTRY                                (refuses everything)
  PARAM_RULES: {rule_name: callable(value) raising ParamRejected}   (R8)
  ParamRejected, DynamicTemplatesRefused
  resolve_dynamic_flag(env: Mapping, install_tier: str) -> bool     (R4)
and the gate: `engramd_owner._ClientReadGate(conn, registry=None)`, whose
`execute(sql, params, *, deadline)` returns rows when admitted and the
`Rejected` object (never raising, never executing) when refused. A gate
built without a registry is the EMPTY registry: it refuses everything.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

DEADLINE_FAR = 1e18  # absolute monotonic deadline far in the future


def _schema_conn() -> sqlite3.Connection:
    """An in-memory store with an ordinary table, a view and an FTS5 table
    (skipped where FTS5 is absent)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT);"
        "CREATE VIEW note_titles AS SELECT id, title FROM notes;"
    )
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(text)")
    except sqlite3.OperationalError:  # pragma: no cover - FTS5 absent
        pass
    return conn


@pytest.fixture
def schema():
    conn = _schema_conn()
    yield conn
    conn.close()


needs_fts5 = pytest.mark.skipif(
    not any("fts5" in (r[0] or "").lower()
            for r in sqlite3.connect(":memory:").execute("PRAGMA compile_options")),
    reason="SQLite built without FTS5",
)

OK_SQL = "SELECT id, title FROM notes WHERE title = ?"


def _admit(sql, schema_conn, **kw):
    reg = rr.build_registry([sql], schema_conn, **kw)
    assert sql not in reg.refused, reg.refused.get(sql)
    return reg


# --- R1: byte-exact key, no normalisation (design test 6) --------------------

def test_06_template_key_is_sha256_of_exact_utf8_bytes():
    import hashlib
    sql = "SELECT 1 -- é"
    assert rr.template_key(sql) == hashlib.sha256(sql.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("mutated", [
    OK_SQL + " ",                       # trailing space
    OK_SQL + "\n",                      # trailing newline
    OK_SQL.lower(),                     # lowercased keywords
    OK_SQL.replace("= ?", "=  ?"),      # doubled space
    OK_SQL.replace("title = ?", "title = 'x'"),   # ? swapped for a literal
])
def test_06_one_byte_mutation_of_a_registered_template_is_rejected(schema, mutated):
    """Mutation: normalise whitespace/case before hashing -> these cells go red."""
    reg = _admit(OK_SQL, schema)
    assert reg.check(OK_SQL, ("a",)) is None
    r = reg.check(mutated, ("a",))
    assert isinstance(r, rr.Rejected)
    assert r.status == "rejected" and r.reason == "unregistered_template"
    assert r.detail == rr.template_key(mutated)
    assert r.remedy


@pytest.mark.parametrize("mutated", [
    OK_SQL + " ",
    OK_SQL + "\n",
    OK_SQL.lower(),
    OK_SQL.replace("= ?", "=  ?"),
])
def test_06_one_byte_mutation_is_refused_through_the_gate_and_never_prepared(schema, mutated):
    """Same cells as above but through `_ClientReadGate.execute`, the path a
    real client takes (R13 requires design test 6 to go red when the registry
    lookup is deleted from the GATE; a cell that calls Registry.check directly
    cannot see that mutation). Mutation: delete the registry lookup in the
    gate -> red; also zero statements may be prepared."""
    reg = _admit(OK_SQL, schema)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    calls = []
    conn.set_authorizer(lambda *a: (calls.append(a), sqlite3.SQLITE_OK)[1])
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute(mutated, ("a",), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "unregistered_template"
    assert calls == []


def test_06_non_str_sql_is_invalid_read_request_not_an_exception(schema):
    reg = _admit(OK_SQL, schema)
    r = reg.check(OK_SQL.encode(), ("a",))
    assert isinstance(r, rr.Rejected) and r.reason == "invalid_read_request"


def test_rejected_wire_shape_carries_status_reason_detail_remedy():
    r = rr.Rejected(reason="x", detail="d", remedy="m")
    assert r.as_wire() == {"status": "rejected", "reason": "x",
                           "detail": "d", "remedy": "m"}


# --- R2: default-deny, nothing executes -------------------------------------

def test_refused_request_prepares_zero_statements_and_changes_nothing():
    """Mutation: on a miss, execute anyway and also return the rejection ->
    the counting authorizer sees activity and the sentinel row count moves."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    conn.execute("INSERT INTO notes (title) VALUES ('t')")
    conn.commit()
    calls = []

    def counting(action, a1, a2, db, trig):
        calls.append(action)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(counting)
    gate = eo._ClientReadGate(conn, registry=rr.EMPTY_REGISTRY)
    out = gate.execute("DELETE FROM notes", (), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "unregistered_template"
    assert calls == [], "a refused request must not prepare any statement"
    conn.set_authorizer(None)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_gate_without_a_registry_is_the_empty_registry_and_refuses_everything():
    conn = sqlite3.connect(":memory:")
    gate = eo._ClientReadGate(conn)
    out = gate.execute("SELECT 1", (), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "unregistered_template"


def test_gate_executes_a_registered_template(schema):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    conn.execute("INSERT INTO notes (title) VALUES ('hello')")
    conn.commit()
    reg = _admit(OK_SQL, schema)
    gate = eo._ClientReadGate(conn, registry=reg)
    assert gate.execute(OK_SQL, ("hello",), deadline=DEADLINE_FAR) == [(1, "hello")]


def test_no_setting_downgrades_a_refusal_to_a_warning():
    """No log-and-allow mode (design SS3.4): the module exposes no switch
    whose name suggests one."""
    names = [n.lower() for n in dir(rr)]
    assert not [n for n in names if re.search(r"log_?and_?allow|warn_?only|permissive|audit_?mode", n)]


# --- R3: no registration verb; the registry is immutable (design test 8) ----

@pytest.fixture
def owner_running():
    home = Path(tempfile.mkdtemp(prefix="edo-"))
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


@requires_linux_owner
@pytest.mark.parametrize("method", ["register_template", "registerTemplate",
                                    "add_template", "register", "Register",
                                    "register_read_template"])
def test_08_owner_socket_has_no_registration_verb(owner_running, method):
    """Behavioural: each registration-shaped operation name is
    unknown_operation over the real socket. Mutation: add a
    `register_template` branch to _dispatch -> red."""
    result = ec.read(method, {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Rejected) and not isinstance(result, ec.Unavailable)
    assert result.reason == "unknown_operation"


def test_08_dispatch_source_names_no_registration_verb():
    """Static complement: no string literal inside _dispatch's body names a
    registration verb (catches a branch the behavioural cell's name list
    did not guess)."""
    import ast
    tree = ast.parse(Path(eo.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    lits = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [v for v in lits if re.search(r"regist", v, re.I)], lits


def test_08_registry_object_is_immutable(schema):
    reg = _admit(OK_SQL, schema)
    assert not hasattr(reg, "add") and not hasattr(reg, "register")
    assert not hasattr(reg, "register_dynamic")
    assert isinstance(reg.templates, types.MappingProxyType)
    with pytest.raises(TypeError):
        reg.templates["k"] = "SELECT 1"  # type: ignore[index]


# --- R4: the escape hatch is named, off, and dev-only (design test 9) -------

FLAG = "ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES"


def test_09_flag_unset_means_off():
    assert rr.resolve_dynamic_flag({}, "essential") is False
    assert rr.resolve_dynamic_flag({}, "dev") is False


@pytest.mark.parametrize("value", ["0", "true", "yes", "2", " 1", "1 ", ""])
def test_09_only_exactly_1_counts(value):
    assert rr.resolve_dynamic_flag({FLAG: value}, "dev") is False


@pytest.mark.parametrize("tier", ["essential", "convenience", "", None])
def test_09_flag_on_outside_dev_refuses_to_start(tier):
    """Mutation: skip the tier check -> red."""
    with pytest.raises(rr.DynamicTemplatesRefused):
        rr.resolve_dynamic_flag({FLAG: "1"}, tier)


def test_09_flag_on_in_dev_enables_and_every_use_logs(schema, caplog):
    assert rr.resolve_dynamic_flag({FLAG: "1"}, "dev") is True
    reg = rr.build_registry([], schema, dynamic=True)
    with caplog.at_level("WARNING"):
        reg.register_dynamic(OK_SQL)
        reg.register_dynamic(OK_SQL)
    warned = [r for r in caplog.records if rr.template_key(OK_SQL) in r.getMessage()]
    assert len(warned) == 2, "every dynamic registration must log, naming the key"
    assert reg.check(OK_SQL, ("a",)) is None


def test_09_dynamic_registration_still_runs_classification(schema):
    reg = rr.build_registry([], schema, dynamic=True)
    r = reg.register_dynamic("SELECT id FROM notes WHERE title LIKE ?")
    assert isinstance(r, rr.Rejected) and r.reason == "unsafe_placeholder"


def test_09_non_dynamic_registry_has_no_register_dynamic(schema):
    reg = rr.build_registry([], schema)
    assert not hasattr(reg, "register_dynamic")


# --- R5: classification from the SCHEMA, default-deny (design tests 16, 21) -

@needs_fts5
def test_21_fts5_equality_template_is_refused_registration(schema):
    """Mutation: classify by operator spelling alone (admit any `=`) -> red."""
    r = rr.classify_template("SELECT rowid FROM docs WHERE docs = ?", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "virtual_table_template"
    assert r.detail == "docs"


@needs_fts5
def test_16_fts5_match_template_is_refused_registration(schema):
    r = rr.classify_template("SELECT rowid FROM docs WHERE docs MATCH ?", schema)
    assert isinstance(r, rr.Rejected)
    assert r.reason in ("virtual_table_template", "unsafe_placeholder")


@needs_fts5
def test_21_fts5_shadow_table_read_is_refused(schema):
    r = rr.classify_template("SELECT block FROM docs_data WHERE id = ?", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "virtual_table_template"


def test_21_ordinary_table_equality_is_admitted(schema):
    assert rr.classify_template("SELECT id FROM notes WHERE title = ?", schema) is None


def test_view_over_ordinary_table_is_admitted(schema):
    assert rr.classify_template("SELECT id FROM note_titles WHERE title = ?", schema) is None


# --- R6: incomplete classification FAILS CLOSED ------------------------------

def test_r6a_question_mark_inside_a_string_literal_is_not_a_placeholder(schema):
    """The control for (a): 'a?b' counts as zero on both sides, so admitted."""
    assert rr.classify_template("SELECT id FROM notes WHERE title = 'a?b'", schema) is None


def test_r6a_parameter_count_disagreement_fails_closed(schema, monkeypatch):
    """Mutation: drop the engine probe and trust the token count -> red.
    Forced by making the scanner's own count disagree with the engine's."""
    sql = "SELECT id FROM notes WHERE title = ? AND body = ?"
    assert rr.classify_template(sql, schema) is None
    monkeypatch.setattr(rr, "_scanner_placeholder_count", lambda s: 1, raising=True)
    r = rr.classify_template(sql, schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"
    assert "parameter" in r.detail.lower()


def test_r6b_attached_database_read_is_unplaceable(schema, tmp_path):
    schema.execute("ATTACH DATABASE ? AS other", (str(tmp_path / "o.db"),))
    schema.execute("CREATE TABLE other.t (x)")
    r = rr.classify_template("SELECT x FROM other.t WHERE x = ?", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


def test_r6b_temp_table_read_is_unplaceable(schema):
    schema.execute("CREATE TEMP TABLE tt (x)")
    r = rr.classify_template("SELECT x FROM tt WHERE x = ?", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


def test_r6b_unknown_table_cannot_be_placed(schema):
    r = rr.classify_template("SELECT x FROM nope WHERE x = ?", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


def test_r6b_json_each_in_the_exact_form_is_placed(schema):
    sql = "SELECT id FROM notes WHERE id IN (SELECT value FROM json_each(?))"
    assert rr.classify_template(sql, schema) is None


@pytest.mark.parametrize("sql", [
    "SELECT id FROM notes WHERE title = ?1",
    "SELECT id FROM notes WHERE title = :t",
    "SELECT id FROM notes WHERE title = @t",
    "SELECT id FROM notes WHERE title = $t",
])
def test_r6c_named_and_numbered_parameters_are_refused(schema, sql):
    r = rr.classify_template(sql, schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


@pytest.mark.parametrize("sql", [
    "SELECT id FROM notes WHERE title = 'unterminated",
    "SELECT id FROM notes /* never closed WHERE title = ?",
    'SELECT id FROM notes WHERE "title = ?',
    "SELECT id FROM notes WHERE [title = ?",
])
def test_r6d_unterminated_constructs_fail_closed(schema, sql):
    r = rr.classify_template(sql, schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


def test_r6b_empty_table_name_read_is_unplaceable(schema, monkeypatch):
    """Design owner's addition: a SQLITE_READ with an EMPTY table name fails
    closed rather than falling through. Forced through the auditing hook."""
    monkeypatch.setattr(rr, "_audit_reads", lambda sql, conn: [("", "x", "main", None)], raising=True)
    r = rr.classify_template(OK_SQL, schema)
    assert isinstance(r, rr.Rejected) and r.reason == "classification_incomplete"


# --- R7: placeholder safety is a CLOSED set, default-deny --------------------

@pytest.mark.parametrize("op", ["=", "==", "<", ">", "<=", ">=", "<>", "!="])
def test_r7_safe_comparison_operators_are_admitted(schema, op):
    assert rr.classify_template(f"SELECT id FROM notes WHERE id {op} ?", schema) is None


@pytest.mark.parametrize("frag", [
    "title LIKE ?",
    "title LIKE ? ESCAPE ?",
    "title GLOB ?",
    "title IS ?",
    "title IS NOT ?",
    "title COLLATE ?",
    "id BETWEEN ? AND ?",
    "json_extract(meta, ?) = 1",
    "lower(?) = 'a'",
    "title IN (?, ?)",
])
def test_r7_every_other_placeholder_is_refused(schema, frag):
    """`IS ?` / `IS NOT ?` are refused deliberately (design owner, 2026-10-02):
    a later reader must not 'fix' that as an omission.
    Mutation: admit when the preceding token merely CONTAINS an operator
    character -> the LIKE/GLOB/IS cells go red."""
    r = rr.classify_template(f"SELECT id FROM notes WHERE {frag}", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "unsafe_placeholder", (frag, r)


@pytest.mark.parametrize("frag", [
    "id << ?", "id >> ?", "title || ?", "id & ?", "id | ?", "id + ? = 3",
    "id - ? = 3", "id * ? = 3", "id % ? = 0", "meta -> ? = 1", "meta ->> ? = 1",
    "~ ?", "NOT ?", "title IN (SELECT value FROM json_each(?)) AND id = ? OR title LIKE ?",
])
def test_r7_operator_characters_that_are_not_in_the_closed_set_are_refused(schema, frag):
    """The closed set is the eight comparison operators, nothing else.
    Mutation: classify by 'the preceding token CONTAINS one of =<>!' (the
    contract's loose wording) -> `<<`, `>>`, `->`, `->>` cells go red (found by
    the first implementation's mutation run: no earlier cell used them)."""
    r = rr.classify_template(f"SELECT id FROM notes WHERE {frag}", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "unsafe_placeholder", (frag, r)


@pytest.mark.parametrize("tail", ["LIMIT ?", "LIMIT 1 OFFSET ?"])
def test_r7_limit_and_offset_placeholders_are_refused(schema, tail):
    r = rr.classify_template(f"SELECT id FROM notes {tail}", schema)
    assert isinstance(r, rr.Rejected) and r.reason == "unsafe_placeholder"


def test_r7_refusal_names_which_placeholder_and_its_preceding_token(schema):
    r = rr.classify_template("SELECT id FROM notes WHERE id = ? AND title LIKE ?", schema)
    assert r.reason == "unsafe_placeholder"
    assert "2" in r.detail and "LIKE" in r.detail.upper()


def test_r7_comments_between_operator_and_placeholder_are_skipped(schema):
    assert rr.classify_template("SELECT id FROM notes WHERE id = /* c */ ?", schema) is None
    assert rr.classify_template("SELECT id FROM notes WHERE id LIKE /* c */ ?", schema).reason == "unsafe_placeholder"


def test_r7_declared_owner_side_policy_admits_an_unsafe_placeholder(schema):
    sql = "SELECT id FROM notes WHERE title LIKE ? ESCAPE '\\'"
    reg = rr.build_registry([sql], schema, param_policies={sql: {0: "like_escaped"}})
    assert sql not in reg.refused
    assert reg.check(sql, ("a\\_c",)) is None


def test_r7_unknown_policy_name_is_refused(schema):
    sql = "SELECT id FROM notes WHERE title LIKE ?"
    reg = rr.build_registry([sql], schema, param_policies={sql: {0: "no_such_rule"}})
    assert sql in reg.refused and reg.refused[sql].reason == "unsafe_placeholder"


# --- R8: the validator family; rule table and case table are ONE object ----

# (rule name) -> {"refuse": [values that must be refused], "accept": [values that must pass]}
CASES = {
    "like_escaped":       {"refuse": ["a_c", "50%", "x\\"], "accept": ["a\\_c", "plain"]},
    "like_escape_clause": {"refuse": ["!", "\\\\", "", "ab"], "accept": ["\\"]},
    "glob_literal":       {"refuse": ["[a-z]", "a[bc]", "*", "a*b", "a?b", "?"], "accept": ["plain", "a.b", ""]},
    "json_path_fixed":    {"refuse": ["$", "$.x", "$[0]"], "accept": []},
    "limit_bounded":      {"refuse": [-1, -100, "5", 1.5, True, 10**9], "accept": [0, 1, 50]},
}


def test_15_rule_table_and_case_table_are_the_same_object():
    """The test iterates PARAM_RULES itself: a rule with no case row FAILS,
    and a case row naming no rule FAILS. Mutation: add a rule to PARAM_RULES
    without a case row -> red."""
    assert set(rr.PARAM_RULES) == set(CASES), (
        set(rr.PARAM_RULES) ^ set(CASES))


@pytest.mark.parametrize("rule", sorted(CASES))
def test_15_each_rule_refuses_its_refused_cases_and_admits_its_accepted(rule):
    """Mutation: bypass the validator -> the refuse cells go red."""
    fn = rr.PARAM_RULES[rule]
    for bad in CASES[rule]["refuse"]:
        with pytest.raises(rr.ParamRejected):
            fn(bad)
    for good in CASES[rule]["accept"]:
        assert fn(good) is None


def test_15_like_escaped_agrees_with_the_existing_like_escape_helper():
    from engram_filter import _like_escape
    for raw in ["a_c", "50%", "back\\slash", "plain"]:
        assert rr.PARAM_RULES["like_escaped"](_like_escape(raw)) is None


def test_15_invalid_parameter_reaches_the_request_path_and_is_not_executed(schema):
    sql = "SELECT id FROM notes WHERE title LIKE ? ESCAPE '\\'"
    reg = rr.build_registry([sql], schema, param_policies={sql: {0: "like_escaped"}})
    conn = sqlite3.connect(":memory:")
    calls = []
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    conn.set_authorizer(lambda *a: (calls.append(a), sqlite3.SQLITE_OK)[1])
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute(sql, ("a_c",), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected)
    assert out.reason == "invalid_parameter" and out.detail == "like_escaped"
    assert calls == []


def test_15_wrong_parameter_count_at_request_time_is_refused_not_raised(schema):
    reg = _admit(OK_SQL, schema)
    for params in [(), ("a", "b")]:
        r = reg.check(OK_SQL, params)
        assert isinstance(r, rr.Rejected) and r.reason == "invalid_parameter"


# --- R9: a registered ATTACH is still refused by the SECOND layer (test 7) --

def test_07_registered_attach_template_is_denied_by_the_authorizer_layer(tmp_path):
    """Registration does not judge the verb; U4b's authorizer does. Mutation:
    allow-all authorizer on the client-read connection -> the file appears
    and this goes red while the R1 cells stay green."""
    target = tmp_path / "x.db"
    sql = f"ATTACH DATABASE '{target}' AS x"
    reg = rr.build_registry([sql], sqlite3.connect(":memory:"))
    assert sql in reg.templates.values(), "ATTACH must be ADMITTED at registration"
    conn = sqlite3.connect(":memory:")
    conn.set_authorizer(eo._client_read_authorizer)
    gate = eo._ClientReadGate(conn, registry=reg)
    with pytest.raises(sqlite3.Error):
        gate.execute(sql, (), deadline=DEADLINE_FAR)
    assert not target.exists()


# --- R12 (loader half): a bad template file means an EMPTY registry ---------

@pytest.mark.parametrize("content", [None, "", "not json", '{"schema": 99, "templates": []}',
                                     '{"schema": 1}', '[]'])
def test_r12_missing_or_unreadable_or_mismatched_file_loads_as_empty(tmp_path, schema, content):
    """Mutation: fall back to allow-all on load failure -> red."""
    p = tmp_path / "templates.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    reg = rr.load_registry(p, schema)
    assert isinstance(reg.check("SELECT 1", ()), rr.Rejected)
    assert dict(reg.templates) == {}


def test_r12_load_reclassifies_against_the_live_schema_and_drops_failures(tmp_path, schema):
    import json
    good, bad = OK_SQL, "SELECT id FROM notes WHERE title LIKE ?"
    p = tmp_path / "templates.json"
    p.write_text(json.dumps({"schema": 1, "templates": [
        {"key": rr.template_key(good), "sql": good, "sites": []},
        {"key": rr.template_key(bad), "sql": bad, "sites": []},
    ]}), encoding="utf-8")
    reg = rr.load_registry(p, schema)
    assert reg.check(good, ("a",)) is None
    assert isinstance(reg.check(bad, ("a",)), rr.Rejected)


def test_r12_a_listed_key_that_does_not_match_its_sql_is_dropped(tmp_path, schema):
    import json
    p = tmp_path / "templates.json"
    p.write_text(json.dumps({"schema": 1, "templates": [
        {"key": "0" * 64, "sql": OK_SQL, "sites": []}]}), encoding="utf-8")
    reg = rr.load_registry(p, schema)
    assert isinstance(reg.check(OK_SQL, ("a",)), rr.Rejected)


# === Round-1 reviewer-fairy folds (PR #3321, suggestions 1, 2, 3) ===========
# Written BEFORE the fixes. Each docstring names its mutation.

import json as _json  # noqa: E402
import os as _os  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _home_with_store(tmp_path: Path, *, install_tier=None) -> Path:
    """A short ENGRAM_HOME holding a knowledge.db that already has `notes`."""
    home = Path(tempfile.mkdtemp(prefix="edo-"))
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    conn.execute("INSERT INTO notes (title) VALUES ('hello')")
    conn.commit()
    conn.close()
    if install_tier is not None:
        (home / "config.json").write_text(_json.dumps({"install_tier": install_tier}))
    return home


def _templates_file(root: Path, sqls) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "engramd_read_templates.json"
    p.write_text(_json.dumps({"schema": 1, "templates": [
        {"key": rr.template_key(s), "sql": s, "sites": []} for s in sqls]}))
    return p


@requires_linux_owner
@pytest.mark.parametrize("tier", ["essential", "convenience", None])
def test_start_with_dynamic_flag_on_a_non_dev_tier_refuses_before_the_socket(
        tmp_path, monkeypatch, tier):
    """R4 wiring. Mutation: replace the resolve_dynamic_flag(...) call in
    start() with `dynamic_templates = False` -> start() succeeds and this
    goes red."""
    home = _home_with_store(tmp_path, install_tier=tier)
    monkeypatch.setenv(FLAG, "1")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        with pytest.raises(rr.DynamicTemplatesRefused):
            owner.start(install_signal_handlers=False)
        assert not owner.socket_path.exists(), "refusal must precede the socket bind"
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


@requires_linux_owner
def test_start_with_dynamic_flag_on_a_dev_tier_builds_a_dynamic_registry(tmp_path, monkeypatch):
    home = _home_with_store(tmp_path, install_tier="dev")
    monkeypatch.setenv(FLAG, "1")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        owner.start(install_signal_handlers=False)
        assert hasattr(owner._client_read_gate._registry, "register_dynamic")
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


@requires_linux_owner
def test_start_loads_the_committed_list_into_the_real_gate(tmp_path, monkeypatch):
    """R12 wiring. Mutation: pass rr.EMPTY_REGISTRY instead of the loaded
    registry in start() -> the gate refuses OK_SQL and this goes red."""
    home = _home_with_store(tmp_path)
    plugin_root = tmp_path / "plugin"
    _templates_file(plugin_root, [OK_SQL, "SELECT id FROM notes WHERE title LIKE ?"])
    monkeypatch.setattr(eo, "_find_read_templates_path",
                        lambda: plugin_root / "engramd_read_templates.json")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        owner.start(install_signal_handlers=False)
        gate = owner._client_read_gate
        assert gate.execute(OK_SQL, ("hello",), deadline=DEADLINE_FAR) == [(1, "hello")]
        # the second listed template fails live re-classification (unsafe
        # placeholder) and is dropped at start(), not admitted
        out = gate.execute("SELECT id FROM notes WHERE title LIKE ?", ("x",), deadline=DEADLINE_FAR)
        assert isinstance(out, rr.Rejected)
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


@requires_linux_owner
def test_start_with_no_templates_file_leaves_the_gate_refusing_everything(tmp_path, monkeypatch):
    """Mutation: fall back to a permissive registry when the file is missing -> red."""
    home = _home_with_store(tmp_path)
    monkeypatch.setattr(eo, "_find_read_templates_path", lambda: None)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        owner.start(install_signal_handlers=False)
        out = owner._client_read_gate.execute("SELECT 1", (), deadline=DEADLINE_FAR)
        assert isinstance(out, rr.Rejected) and out.reason == "unregistered_template"
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


@requires_linux_owner
def test_start_with_a_corrupt_templates_file_leaves_the_gate_refusing_everything(tmp_path, monkeypatch):
    home = _home_with_store(tmp_path)
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "engramd_read_templates.json").write_text("{not json")
    monkeypatch.setattr(eo, "_find_read_templates_path",
                        lambda: plugin_root / "engramd_read_templates.json")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        owner.start(install_signal_handlers=False)
        out = owner._client_read_gate.execute(OK_SQL, ("a",), deadline=DEADLINE_FAR)
        assert isinstance(out, rr.Rejected)
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


def test_templates_path_is_found_in_a_flattened_plugin_layout_without_the_env_var(tmp_path):
    """Project CLAUDE.md, 'Plugin build restructures code paths': recreate the
    flattened layout (module and data file side by side), run with
    CLAUDE_PLUGIN_ROOT UNSET, and require the walk-parents lookup to find THE
    FLATTENED COPY, not the source tree's. Mutation: calibrate a fixed
    `parents[N]` to the source layout -> resolves elsewhere / None -> red."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    src = Path(eo.__file__).resolve().parent
    for f in src.glob("*.py"):
        shutil.copy2(f, plugin / f.name)
    shutil.copy2(src / "engramd_read_templates.json", plugin / "engramd_read_templates.json")
    env = {k: v for k, v in _os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["PYTHONPATH"] = str(plugin)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = _subprocess.run(
        [sys.executable, "-c",
         "import engramd_owner as eo; print(eo._find_read_templates_path())"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(plugin / "engramd_read_templates.json")


# --- gate-level parameter TYPE safety (reviewer-fairy suggestion 3) ---------

BAD_PARAM_VALUES = [
    {"a": 1}, [1], (1,), {1, 2}, 2 ** 63, -(2 ** 63) - 1, 2 ** 70, object(),
    complex(1, 2),
]
GOOD_PARAM_VALUES = [None, True, False, 0, 5, 2 ** 63 - 1, -(2 ** 63), 1.5, "s", "", b"x"]


@pytest.mark.parametrize("bad", BAD_PARAM_VALUES, ids=lambda v: type(v).__name__ + str(v)[:12])
def test_check_refuses_a_parameter_of_a_type_sqlite_cannot_bind(schema, bad):
    """A refusal is RETURNED; an admitted template with an unbindable
    parameter must not RAISE at the gate. Mutation: drop the type check from
    Registry.check -> the gate raises ProgrammingError/OverflowError."""
    reg = _admit("SELECT id FROM notes WHERE id = ?", schema)
    r = reg.check("SELECT id FROM notes WHERE id = ?", (bad,))
    assert isinstance(r, rr.Rejected) and r.reason == "invalid_parameter"


@pytest.mark.parametrize("good", GOOD_PARAM_VALUES, ids=lambda v: type(v).__name__ + str(v)[:12])
def test_check_admits_every_sqlite_bindable_parameter_type(schema, good):
    reg = _admit("SELECT id FROM notes WHERE id = ?", schema)
    assert reg.check("SELECT id FROM notes WHERE id = ?", (good,)) is None


@pytest.mark.parametrize("bad", [{"a": 1}, [1], 2 ** 70])
def test_gate_returns_rejected_and_never_raises_on_an_unbindable_parameter(schema, bad):
    reg = _admit("SELECT id FROM notes WHERE id = ?", schema)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute("SELECT id FROM notes WHERE id = ?", (bad,), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "invalid_parameter"


def test_gate_does_not_leak_a_sqlite_error_for_malformed_json_into_json_each(schema):
    """`json_each(?)` with malformed JSON raises OperationalError at execute.
    The gate must turn a SQLite error raised by an ADMITTED statement into a
    Rejected(reason='read_failed'), never propagate it."""
    sql = "SELECT id FROM notes WHERE id IN (SELECT value FROM json_each(?))"
    reg = _admit(sql, schema)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute(sql, ("[1,2",), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "read_failed"


def test_a_stale_plugin_root_env_var_cannot_override_the_templates_file_beside_the_module(tmp_path, monkeypatch):
    """Reviewer-fairy suggestion 2 / PR body decision 4: the committed list
    ships BESIDE engramd_owner.py in both the source and the flattened plugin
    layout, so the sibling file is always the right one; a stale
    CLAUDE_PLUGIN_ROOT pointing at another tree must not win. Mutation: check
    CLAUDE_PLUGIN_ROOT before the sibling -> returns the stale file -> red."""
    stale = tmp_path / "stale_tree"
    _templates_file(stale, ["SELECT 1"])
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(stale))
    found = eo._find_read_templates_path()
    assert found == Path(eo.__file__).resolve().with_name("engramd_read_templates.json")


def test_templates_path_falls_back_to_the_env_var_only_when_no_sibling_exists(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    src = Path(eo.__file__).resolve().parent
    shutil.copy2(src / "engramd_owner.py", plugin / "engramd_owner.py")  # no data file beside it
    other = tmp_path / "other"
    expected = _templates_file(other, ["SELECT 1"])
    env = dict(_os.environ, CLAUDE_PLUGIN_ROOT=str(other), PYTHONPATH=f"{plugin}:{src}",
               PYTHONDONTWRITEBYTECODE="1")
    out = _subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys;"
         f"s=importlib.util.spec_from_file_location('engramd_owner_copy','{plugin / 'engramd_owner.py'}');"
         "m=importlib.util.module_from_spec(s);sys.modules['engramd_owner_copy']=m;s.loader.exec_module(m);"
         "print(m._find_read_templates_path())"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(expected)


# === Round-2 reviewer-fairy folds (PR #3321, delta 02f6bb38..844bb381) ======

@pytest.mark.parametrize("bad", ["\ud800", "ab\udfffcd", "\udc00"], ids=["hi", "mid", "lo"])
def test_check_refuses_a_str_sqlite_cannot_encode(schema, bad):
    """A lone surrogate (JSON "\\ud800" decodes to exactly this) passed check()
    and raised UnicodeEncodeError through the gate. Mutation: drop the encode
    probe from _is_bindable -> red."""
    sql = "SELECT id FROM notes WHERE title = ?"
    reg = _admit(sql, schema)
    r = reg.check(sql, (bad,))
    assert isinstance(r, rr.Rejected) and r.reason == "invalid_parameter"


@pytest.mark.parametrize("good", ["é", "日本語", "\U0001F600", "a\x00b", ""], ids=lambda v: repr(v)[:8])
def test_check_admits_well_formed_unicode_and_embedded_nul(schema, good):
    sql = "SELECT id FROM notes WHERE title = ?"
    assert _admit(sql, schema).check(sql, (good,)) is None


def test_gate_never_raises_on_a_lone_surrogate(schema):
    sql = "SELECT id FROM notes WHERE title = ?"
    reg = _admit(sql, schema)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute(sql, ("\ud800",), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "invalid_parameter"


def test_read_failed_conversion_is_logged_so_schema_drift_is_not_silent(schema, caplog):
    """Code 1 also covers an owner-side fault (a table renamed after start).
    The conversion must leave a WARNING naming the template key so the
    operator can see it. Mutation: convert without logging -> red."""
    sql = "SELECT id FROM notes WHERE id = ?"
    reg = _admit(sql, schema)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    gate = eo._ClientReadGate(conn, registry=reg)
    conn.execute("ALTER TABLE notes RENAME TO notes_old")  # schema drift after start
    with caplog.at_level("WARNING"):
        out = gate.execute(sql, (1,), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected) and out.reason == "read_failed"
    assert any(rr.template_key(sql) in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_busy_database_error_propagates_through_the_gate(tmp_path, schema):
    """SQLITE_BUSY (code 5) is the owner's problem, not the client's: it must
    propagate like the deadline abort. Mutation: blacklist only code 9
    instead of converting only code 1 -> BUSY is converted -> red."""
    path = str(tmp_path / "busy.db")
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    holder.execute("BEGIN EXCLUSIVE")
    sql = "SELECT id FROM notes WHERE id = ?"
    reg = _admit(sql, schema)
    reader = sqlite3.connect(path, timeout=0)
    try:
        gate = eo._ClientReadGate(reader, registry=reg)
        with pytest.raises(sqlite3.OperationalError) as ei:
            gate.execute(sql, (1,), deadline=DEADLINE_FAR)
        assert getattr(ei.value, "sqlite_errorcode", None) == 5
    finally:
        reader.close()
        holder.execute("ROLLBACK")
        holder.close()


@requires_linux_owner
def test_dynamic_registry_still_works_after_start(tmp_path, monkeypatch):
    """The dev-tier schema connection must stay open for register_dynamic.
    Mutation: always close the schema connection in start() -> red."""
    home = _home_with_store(tmp_path, install_tier="dev")
    monkeypatch.setenv(FLAG, "1")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        owner.start(install_signal_handlers=False)
        reg = owner._client_read_gate._registry
        assert reg.register_dynamic("SELECT id FROM notes WHERE id = ?") is None
        out = owner._client_read_gate.execute("SELECT id FROM notes WHERE id = ?", (1,),
                                              deadline=DEADLINE_FAR)
        assert out == [(1,)]
    finally:
        owner.stop()
        shutil.rmtree(home, ignore_errors=True)


# === Security read (Mira) on PR #3321 head 4665333c: R8 policy/template pairing ===
# A validator certifies a PARAMETER VALUE under an assumption about the TEMPLATE
# TEXT around its placeholder. `like_escaped` assumes `\` is the LIKE escape, which
# is true only with `ESCAPE '\'` after the placeholder; without it SQLite treats a
# backslash as an ordinary character and `\%` leaves `%` a LIVE wildcard. So a
# policy is admitted only on a placeholder whose surrounding tokens match the
# rule (reason `policy_template_mismatch`), checked at REGISTRATION.

LIKE_E = "SELECT title FROM notes WHERE title LIKE ? ESCAPE '\\'"
LIKE_NO_E = "SELECT title FROM notes WHERE title LIKE ?"


def _reg(sql, schema, policies):
    return rr.build_registry([sql], schema, param_policies={sql: policies})


def _titles_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, meta TEXT)")
    conn.executemany("INSERT INTO notes (title) VALUES (?)", [(t,) for t in rows])
    conn.commit()
    return conn


def test_r8_like_escaped_on_a_like_without_an_escape_clause_is_refused_at_registration(schema):
    """Mira's counterexample. Mutation: drop the pairing check -> admitted -> red."""
    reg = _reg(LIKE_NO_E, schema, {0: "like_escaped"})
    assert LIKE_NO_E in reg.refused
    assert reg.refused[LIKE_NO_E].reason == "policy_template_mismatch"


def test_r8_the_no_escape_template_cannot_leak_through_the_gate_either(schema):
    """End to end on Mira's fixture: titles `a\\secret`, `a\\%`, `a%`. Whatever the
    registry does with the no-ESCAPE template, the gate must NOT return the
    backslash rows for the parameter `a\\%`."""
    conn = _titles_conn(["a\\secret", "a\\%", "a%"])
    reg = _reg(LIKE_NO_E, schema, {0: "like_escaped"})
    gate = eo._ClientReadGate(conn, registry=reg)
    out = gate.execute(LIKE_NO_E, ("a\\%",), deadline=DEADLINE_FAR)
    assert isinstance(out, rr.Rejected), out


def test_r8_like_escaped_with_escape_backslash_admits_and_matches_literally(schema):
    """The same fixture under the paired template: `a\\%` means the literal text
    `a%`, so ONLY the row `a%` comes back."""
    conn = _titles_conn(["a\\secret", "a\\%", "a%"])
    reg = _reg(LIKE_E, schema, {0: "like_escaped"})
    assert LIKE_E not in reg.refused
    gate = eo._ClientReadGate(conn, registry=reg)
    assert gate.execute(LIKE_E, ("a\\%",), deadline=DEADLINE_FAR) == [("a%",)]


@pytest.mark.parametrize("clause", ["ESCAPE '!'", "ESCAPE '\\\\'", "ESCAPE ''", "ESCAPE 'ab'",
                                    "ESCAPE x'5c'", "ESCAPE char(92)"])
def test_r8_like_escaped_requires_exactly_a_single_backslash_escape_literal(schema, clause):
    sql = f"SELECT title FROM notes WHERE title LIKE ? {clause}"
    reg = _reg(sql, schema, {0: "like_escaped"})
    assert sql in reg.refused and reg.refused[sql].reason in (
        "policy_template_mismatch", "classification_incomplete"), reg.refused.get(sql)


def test_r8_like_escaped_is_refused_on_a_placeholder_that_is_not_a_like_pattern(schema):
    for sql in ("SELECT id FROM notes WHERE title = ?",
                "SELECT id FROM notes WHERE title GLOB ?",
                "SELECT id FROM notes LIMIT ?"):
        reg = _reg(sql, schema, {0: "like_escaped"})
        assert sql in reg.refused and reg.refused[sql].reason == "policy_template_mismatch", sql


def test_r8_the_paired_escape_placeholder_form_is_admitted_and_pinned(schema):
    sql = "SELECT title FROM notes WHERE title LIKE ? ESCAPE ?"
    reg = _reg(sql, schema, {0: "like_escaped", 1: "like_escape_clause"})
    assert sql not in reg.refused
    assert reg.check(sql, ("a\\%", "\\")) is None
    r = reg.check(sql, ("a\\%", "!"))
    assert isinstance(r, rr.Rejected) and r.reason == "invalid_parameter" and r.detail == "like_escape_clause"


def test_r8_paired_escape_form_needs_both_policies(schema):
    sql = "SELECT title FROM notes WHERE title LIKE ? ESCAPE ?"
    for pol in ({0: "like_escaped"}, {1: "like_escape_clause"}):
        reg = _reg(sql, schema, pol)
        assert sql in reg.refused, pol


def test_r8_like_escape_clause_is_refused_unless_it_follows_an_escape_keyword(schema):
    sql = "SELECT id FROM notes WHERE title = ?"
    reg = _reg(sql, schema, {0: "like_escape_clause"})
    assert sql in reg.refused and reg.refused[sql].reason == "policy_template_mismatch"


def test_r8_glob_literal_refuses_the_live_wildcards_at_request_time(schema):
    """The same defect class (a rule named `literal` that admits live wildcards):
    GLOB has no escape character, `*` and `?` stay live. Mutation: restore the
    old rule (refuses only `[`) -> `a*b` is admitted -> red."""
    sql = "SELECT title FROM notes WHERE title GLOB ?"
    reg = _reg(sql, schema, {0: "glob_literal"})
    assert sql not in reg.refused
    for bad in ("a*", "*", "a?c", "[ab]"):
        r = reg.check(sql, (bad,))
        assert isinstance(r, rr.Rejected) and r.reason == "invalid_parameter", bad
    assert reg.check(sql, ("abc",)) is None


def test_r8_glob_literal_end_to_end_cannot_enumerate_with_a_wildcard(schema):
    conn = _titles_conn(["abc", "abd", "a*"])
    sql = "SELECT title FROM notes WHERE title GLOB ?"
    gate = eo._ClientReadGate(conn, registry=_reg(sql, schema, {0: "glob_literal"}))
    assert isinstance(gate.execute(sql, ("a*",), deadline=DEADLINE_FAR), rr.Rejected)
    assert gate.execute(sql, ("abc",), deadline=DEADLINE_FAR) == [("abc",)]


def test_r8_glob_literal_is_refused_on_a_non_glob_placeholder(schema):
    for sql in ("SELECT id FROM notes WHERE title = ?",
                "SELECT id FROM notes WHERE title LIKE ? ESCAPE '\\'"):
        reg = _reg(sql, schema, {0: "glob_literal"})
        assert sql in reg.refused and reg.refused[sql].reason == "policy_template_mismatch", sql


def test_r8_limit_bounded_is_refused_on_a_non_limit_placeholder(schema):
    sql = "SELECT id FROM notes WHERE title = ?"
    reg = _reg(sql, schema, {0: "limit_bounded"})
    assert sql in reg.refused and reg.refused[sql].reason == "policy_template_mismatch"


@pytest.mark.parametrize("sql", ["SELECT id FROM notes LIMIT ?",
                                 "SELECT id FROM notes LIMIT 1 OFFSET ?"])
def test_r8_limit_bounded_on_limit_and_offset_is_admitted(schema, sql):
    reg = _reg(sql, schema, {0: "limit_bounded"})
    assert sql not in reg.refused
    assert reg.check(sql, (5,)) is None
    assert isinstance(reg.check(sql, (-1,)), rr.Rejected)


def test_r8_every_rule_has_a_declared_template_context():
    """Completeness: a new rule cannot be added to PARAM_RULES without saying
    what template context it is valid in (the same one-object discipline as the
    case table). Mutation: add a rule with no context entry -> red."""
    assert set(rr.PARAM_RULE_CONTEXTS) == set(rr.PARAM_RULES)
