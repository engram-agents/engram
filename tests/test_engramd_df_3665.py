"""Contract cells for the owner's typed `df` method (#3665, the `df` unit).

Design seat ruling (crossing 9958; #3665 comment 6124428822): the IDF batch
statement `SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM
json_each(?))` cannot be a registered read template (virtual_table_template), so
the owner serves it as a TYPED method `df`, on the client-read connection, with
its own fixed statement. Client SQL never reaches the vocab table.

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire:

  read("df", {"terms": [<str>, ...]})
    -> Ok(result=[[term, doc], ...])  rows only for terms PRESENT (absent terms
                                       omitted; the caller treats absent as df=0;
                                       row order is NOT part of the contract)
    -> Rejected(reason=<one NAMED reason per violation>)

Reasons, in CHECK ORDER (the first violated wins):
  df_params_shape, df_terms_not_list, df_terms_empty, df_terms_too_many,
  then per element in index order: df_term_not_str, df_term_empty,
  df_term_too_long; then (store-touching) vocab_table_absent,
  unsupported_vocab_variant.

Self-contained on dev: the fts5 store is built by local helpers below. U6b pins
`DF_TERMS_MAX` to `engram_idf.IDF_BATCH_TERMS`; that constant does not exist on
dev, so this file asserts the literal 1000 only.

Registry coverage: tests/test_engramd_read_registry.py (cell 21 family) pins that
templates over a generic FTS5 table are refused; the last cell here adds the exact
nodes_fts_vocab / json_each form (contract cell 4 of the ruling).
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner
from _owner_store import stamp_current_schema

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_idf  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

DIRECT_SQL = "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
PRESENT = ["formula", "algorithm", "xylophone", "ranking"]
ABSENT = ["qqqzzz", "nonexistentterm"]
_CORPUS = [
    "formula algorithm ranking",
    "formula ranking",
    "formula xylophone",
    "algorithm ranking ranking",
    "unrelated words only",
]


# --- helpers -----------------------------------------------------------------

def _store(home: Path, vocab_variant: str | None = "row") -> Path:
    """knowledge.db with a nodes_fts fts5 table over a tiny corpus and a
    nodes_fts_vocab of the given fts5vocab type ('row'/'col'/'instance'), or NO
    vocab table when None."""
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(content)")
    for i, text in enumerate(_CORPUS):
        conn.execute("INSERT INTO nodes VALUES (?, ?)", (i, text))
        conn.execute("INSERT INTO nodes_fts(rowid, content) VALUES (?, ?)", (i, text))
    if vocab_variant == "row":
        engram_idf.ensure_vocab_table(conn)
    elif vocab_variant is not None:
        conn.execute(
            "CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(nodes_fts, '%s')" % vocab_variant
        )
    conn.commit()
    conn.close()
    stamp_current_schema(db)  # #3753: the owner refuses a store below the latest schema version
    return db


def _store_with_vocab_ddl(home: Path, vocab_ddl: str) -> Path:
    """Same corpus as `_store`, but `nodes_fts_vocab` is created by running
    `vocab_ddl` verbatim (the hostile / spelling-variant DDL cells)."""
    db = _store(home, vocab_variant=None)
    conn = sqlite3.connect(str(db))
    conn.execute(vocab_ddl)
    conn.commit()
    conn.close()
    return db


def _direct(db: Path, terms) -> dict:
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {t: d for t, d in conn.execute(DIRECT_SQL, (json.dumps(list(terms)),)).fetchall()}
    finally:
        conn.close()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="edf-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home, request_deadline=0.6):
    owner = eo.EngramdOwner(home, request_deadline=request_deadline)
    owner.start(install_signal_handlers=False)
    return owner


@pytest.fixture
def df_owner(home):
    _store(home)
    owner = _start(home)
    try:
        yield owner
    finally:
        owner.stop()


def _df(owner, terms, timeout=3.0):
    return ec.read("df", {"terms": terms}, timeout=timeout, socket_path=owner.socket_path)


def _as_dict(result) -> dict:
    assert isinstance(result, ec.Ok), result
    assert isinstance(result.result, list)
    for row in result.result:
        assert isinstance(row, list) and len(row) == 2, row
        assert isinstance(row[0], str) and isinstance(row[1], int) and not isinstance(row[1], bool), row
    out = {t: d for t, d in result.result}
    assert len(out) == len(result.result), "a term appears twice"
    return out


def _recording(owner):
    calls = []

    def recording(*a):
        calls.append(a)
        return eo._client_read_authorizer(*a)

    owner._client_read_conn.set_authorizer(recording)
    return calls


def _a(n):
    return "a" * n


# --- constants ---------------------------------------------------------------

def test_df_caps_are_named_constants():
    """DF_TERMS_MAX is the hook's real batch size (1000; U6b pins it to
    engram_idf.IDF_BATCH_TERMS); DF_TERM_MAX_BYTES is a named byte cap, >= 256."""
    assert eo.DF_TERMS_MAX == 1000
    assert isinstance(eo.DF_TERM_MAX_BYTES, int) and eo.DF_TERM_MAX_BYTES >= 256


# --- parity (contract cell 1) ------------------------------------------------

@requires_linux_owner
def test_df_equals_a_direct_vocab_query_including_an_absent_term(df_owner):
    """Present + absent terms -> rows only for present terms, dict-equal to a
    direct vocab SELECT on the same store."""
    oracle = _direct(df_owner.store_path, PRESENT + ABSENT)
    assert set(oracle) == set(PRESENT)  # not vacuous: 4 present, 2 absent
    got = _as_dict(_df(df_owner, PRESENT + ABSENT))
    assert got == oracle
    assert not set(got) & set(ABSENT)


# --- per-reason rejection (contract cell 2) ---------------------------------

REASON_CASES = [
    pytest.param(None, "df_params_shape", id="params-none"),
    pytest.param(["formula"], "df_params_shape", id="params-list"),
    pytest.param("formula", "df_params_shape", id="params-str"),
    pytest.param({}, "df_params_shape", id="terms-key-missing"),
    pytest.param({"terms": ["formula"], "extra": 1}, "df_params_shape", id="extra-key"),
    pytest.param({"terms": "formula"}, "df_terms_not_list", id="terms-str"),
    pytest.param({"terms": None}, "df_terms_not_list", id="terms-none"),
    pytest.param({"terms": {"a": 1}}, "df_terms_not_list", id="terms-dict"),
    pytest.param({"terms": []}, "df_terms_empty", id="terms-empty"),
    pytest.param({"terms": ["t%d" % i for i in range(1001)]}, "df_terms_too_many", id="1001-terms"),
    pytest.param({"terms": ["formula", True]}, "df_term_not_str", id="element-bool"),
    pytest.param({"terms": ["formula", 5]}, "df_term_not_str", id="element-int"),
    pytest.param({"terms": ["formula", 1.5]}, "df_term_not_str", id="element-float"),
    pytest.param({"terms": ["formula", None]}, "df_term_not_str", id="element-none"),
    pytest.param({"terms": [[102, 111, 114]]}, "df_term_not_str", id="element-list-of-ints"),
    pytest.param({"terms": ["formula", {"a": 1}]}, "df_term_not_str", id="element-dict"),
    pytest.param({"terms": ["formula", ""]}, "df_term_empty", id="element-empty-string"),
    pytest.param({"terms": ["formula", "x" * 100000]}, "df_term_too_long", id="element-too-long"),
]


@pytest.mark.parametrize("params,reason", REASON_CASES)
@requires_linux_owner
def test_df_each_violation_has_its_own_named_reason(df_owner, params, reason):
    """Each violation -> Rejected with exactly its own reason, a detail and a remedy."""
    result = ec.read("df", params, timeout=3.0, socket_path=df_owner.socket_path)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == reason
    assert result.detail and result.remedy


@requires_linux_owner
def test_df_element_detail_names_the_first_offending_index(df_owner):
    result = _df(df_owner, ["formula", "algorithm", 7, ""])
    assert isinstance(result, ec.Rejected) and result.reason == "df_term_not_str"
    assert "2" in result.detail


ORDER_CASES = [
    pytest.param({"terms": [], "extra": 1}, "df_params_shape", id="shape-before-empty"),
    pytest.param({"terms": "x", "extra": 1}, "df_params_shape", id="shape-before-not-list"),
    pytest.param({"terms": [5] * 1001}, "df_terms_too_many", id="too-many-before-not-str"),
    pytest.param({"terms": ["", ""] + [5] * 999}, "df_terms_too_many", id="too-many-before-empty"),
    pytest.param({"terms": ["", 5]}, "df_term_empty", id="index-order-empty-first"),
    pytest.param({"terms": [5, ""]}, "df_term_not_str", id="index-order-not-str-first"),
    pytest.param({"terms": ["ok", 5, "a" * 100000]}, "df_term_not_str", id="not-str-before-later-too-long"),
    pytest.param({"terms": ["a" * 100000, ""]}, "df_term_too_long", id="too-long-first-index-wins"),
    pytest.param({"terms": [""]}, "df_term_empty", id="empty-is-not-too-long"),
]


@pytest.mark.parametrize("params,reason", ORDER_CASES)
@requires_linux_owner
def test_df_check_order_earlier_violation_wins(df_owner, params, reason):
    result = ec.read("df", params, timeout=3.0, socket_path=df_owner.socket_path)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == reason


# --- boundaries ---------------------------------------------------------------

@requires_linux_owner
def test_df_exactly_cap_terms_ok_and_one_more_is_too_many(df_owner):
    terms = ["zz%04d" % i for i in range(999)] + ["formula"]
    assert len(terms) == 1000 == eo.DF_TERMS_MAX
    got = _as_dict(_df(df_owner, terms))
    assert got == _direct(df_owner.store_path, ["formula"])
    over = _df(df_owner, terms + ["algorithm"])
    assert isinstance(over, ec.Rejected) and over.reason == "df_terms_too_many"


@requires_linux_owner
def test_df_term_of_exactly_the_byte_cap_is_ok_and_one_more_byte_is_too_long(df_owner):
    cap = eo.DF_TERM_MAX_BYTES
    ok = _df(df_owner, [_a(cap), "formula"])
    assert _as_dict(ok) == _direct(df_owner.store_path, ["formula"])
    over = _df(df_owner, [_a(cap + 1)])
    assert isinstance(over, ec.Rejected) and over.reason == "df_term_too_long"


@requires_linux_owner
def test_df_byte_cap_counts_utf8_bytes_not_characters(df_owner):
    """A 2-byte char: cap//2 of them is exactly the cap in bytes (ok); one more is
    cap+2 bytes yet fewer than cap characters (rejected)."""
    cap = eo.DF_TERM_MAX_BYTES
    at_cap = "é" * (cap // 2)
    assert len(at_cap.encode("utf-8")) == cap and len(at_cap) < cap
    assert isinstance(_df(df_owner, [at_cap]), ec.Ok)
    over = "é" * (cap // 2 + 1)
    assert len(over) <= cap < len(over.encode("utf-8"))
    result = _df(df_owner, [over])
    assert isinstance(result, ec.Rejected) and result.reason == "df_term_too_long"


@requires_linux_owner
def test_df_lone_surrogate_term_is_ok_and_contributes_no_row(df_owner):
    """The byte-cap check encodes with 'surrogatepass', so a lone-surrogate term
    cannot raise inside validation (a plain utf-8 encode raises -> internal_error
    plus a traceback). Alongside a real term: ok, the real row present, no row
    for the surrogate. Alone: ok with []."""
    oracle = _direct(df_owner.store_path, ["formula"])
    assert oracle  # not vacuous
    got = _as_dict(_df(df_owner, ["formula", "\ud800"]))
    assert got == oracle
    assert "\ud800" not in got
    alone = _df(df_owner, ["\ud800"])
    assert isinstance(alone, ec.Ok) and alone.result == []


# --- nothing is prepared on a refused request (every refusal reason) ---------

@pytest.mark.parametrize("params,reason", [(p.values[0], p.values[1]) for p in REASON_CASES])
@requires_linux_owner
def test_df_param_refusal_prepares_nothing_on_the_store_connection(df_owner, params, reason):
    calls = _recording(df_owner)
    result = ec.read("df", params, timeout=3.0, socket_path=df_owner.socket_path)
    assert isinstance(result, ec.Rejected) and result.reason == reason, result
    assert calls == [], "a refused df must prepare nothing on the store connection"
    ping = ec.read("ping", {}, timeout=2.0, socket_path=df_owner.socket_path)
    assert isinstance(ping, ec.Ok), ping


@requires_linux_owner
def test_df_byte_cap_boundary_refusal_prepares_nothing(df_owner):
    calls = _recording(df_owner)
    result = _df(df_owner, [_a(eo.DF_TERM_MAX_BYTES + 1)])
    assert isinstance(result, ec.Rejected) and result.reason == "df_term_too_long"
    assert calls == []


# --- routing ------------------------------------------------------------------

class _Tripwire:
    """Stand-in for `_owner_conn` / `_owner_lock`: any use fails loudly."""

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        raise AssertionError(f"`df` touched an owner-internal object: .{name}")

    def __enter__(self):
        raise AssertionError("`df` took the owner-internal lock")

    def __exit__(self, *exc):
        return False


@requires_linux_owner
def test_df_executes_on_the_client_read_connection_only(df_owner):
    calls = _recording(df_owner)
    original_conn, original_lock = df_owner._owner_conn, df_owner._owner_lock
    df_owner._owner_conn = _Tripwire()
    df_owner._owner_lock = _Tripwire()
    try:
        result = _df(df_owner, PRESENT)
    finally:
        df_owner._owner_conn, df_owner._owner_lock = original_conn, original_lock
    assert _as_dict(result) == _direct(df_owner.store_path, PRESENT)
    assert calls, "the lookup must have been prepared on the client-read connection"


# --- method table / displacement guard (contract cell 3) --------------------

@requires_linux_owner
def test_df_is_in_the_method_table_displacement_guarded_with_no_registration_verb(df_owner):
    assert "df" in df_owner._methods
    assert "df" not in eo.NO_STORE_METHODS
    assert not [m for m in df_owner._methods if re.search(r"regist", m, re.I)]
    tree = ast.parse(Path(eo.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    lits = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [v for v in lits if re.search(r"regist", v, re.I)], lits


@requires_linux_owner
def test_df_is_displacement_guarded(df_owner, monkeypatch):
    import engram_walguard

    def displaced(path):
        raise engram_walguard.SubstrateDisplaced({"reason": "test"}, "n/a")

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", displaced)
    result = _df(df_owner, PRESENT)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "substrate_displaced"


# --- deadline -----------------------------------------------------------------

@requires_linux_owner
def test_df_waiting_for_the_client_read_lock_stops_at_the_deadline(df_owner):
    lock = df_owner._client_read_lock
    assert lock.acquire(timeout=1.0)
    try:
        start = time.monotonic()
        result = _df(df_owner, PRESENT, timeout=3.0)
        elapsed = time.monotonic() - start
    finally:
        lock.release()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"
    assert elapsed < df_owner.request_deadline + 0.5, elapsed


@requires_linux_owner
def test_df_with_no_time_left_inside_the_deadline_is_deadline_exceeded(home):
    _store(home)
    owner = _start(home, request_deadline=eo.READ_RESPONSE_RESERVE / 2)
    try:
        result = _df(owner, PRESENT, timeout=3.0)
    finally:
        owner.stop()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"


# --- vocab variant / absent --------------------------------------------------

@pytest.mark.parametrize("variant", ["col", "instance"])
@requires_linux_owner
def test_df_reads_only_the_row_vocab_variant(home, variant):
    """col/instance -> unsupported_vocab_variant. SEMANTIC reason: the variants
    return different column shapes/row semantics; `df` needs the 'row' shape
    (term, doc, cnt). Not a pragma-surface reason (all variants fire the same pragma)."""
    _store(home, vocab_variant=variant)
    owner = _start(home)
    try:
        result = _df(owner, PRESENT)
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "unsupported_vocab_variant"
        assert isinstance(ec.read("ping", {}, timeout=2.0, socket_path=owner.socket_path), ec.Ok)
    finally:
        owner.stop()


@requires_linux_owner
def test_df_row_variant_is_ok(home):
    db = _store(home, vocab_variant="row")
    owner = _start(home)
    try:
        result = _df(owner, PRESENT)
    finally:
        owner.stop()
    assert _as_dict(result) == _direct(db, PRESENT)


@requires_linux_owner
def test_df_vocab_absent_is_named_and_the_owner_does_not_create_it(home):
    db = _store(home, vocab_variant=None)
    owner = _start(home)
    try:
        result = _df(owner, PRESENT)
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "vocab_table_absent"
        assert result.detail and result.remedy
        # The remedy describes what the owner-connection shim actually does (U6b follow-up):
        # no df=0 claim, the shim treats every term as absent from the vocabulary.
        assert "df=0" not in result.remedy
        assert "absent from the vocabulary" in result.remedy
        assert isinstance(ec.read("ping", {}, timeout=2.0, socket_path=owner.socket_path), ec.Ok)
    finally:
        owner.stop()
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='nodes_fts_vocab'").fetchone()[0] == 0
    finally:
        conn.close()


@requires_linux_owner
def test_df_additive_contract_version_stays_1_1(df_owner):
    # #3822 (U11): the later, additive `use_reward` write bumped the shared constant 1.1 -> 1.2.
    assert ec.CONTRACT_VERSION == "1.3"
    assert df_owner.contract_version == "1.3"


# --- registry class (contract cell 4) ---------------------------------------

def test_vocab_template_stays_refused_by_the_registry(tmp_path):
    """The df statement over nodes_fts_vocab is NOT registrable as a read template:
    the entry stays refused with virtual_table_template (so `df` is a typed method,
    not a registry entry)."""
    db = _store(tmp_path)
    schema = sqlite3.connect(str(db))
    try:
        reg = rr.build_registry([DIRECT_SQL], schema)
    finally:
        schema.close()
    assert DIRECT_SQL in reg.refused
    assert reg.refused[DIRECT_SQL].reason == "virtual_table_template"


# =============================================================================
# Round 2 (PR #3733 reviewer findings F1, F2, F4, F5)
# =============================================================================

# --- F1: injection resistance -------------------------------------------------

HOSTILE_TERMS = ["x') OR 1=1 --", "a'b", 'q"r', "%", "_", "\\", "a\x00b"]


@requires_linux_owner
def test_df_hostile_terms_are_bound_data_never_sql(df_owner):
    """F1. Contract: client terms reach SQL ONLY as the single json.dumps(terms)
    bound parameter of the fixed DF_SQL; the only statements a successful `df`
    prepares at top level are DF_VOCAB_DDL_SQL and DF_SQL.

    How the statements are captured and filtered: `set_trace_callback` on the
    client-read connection. The callback reports (a) every top-level statement
    with its bound parameters EXPANDED into the text, and (b) statements the
    fts5vocab engine itself runs internally, each prefixed `-- ` (SQLite's
    nested-statement trace form; e.g. `-- PRAGMA 'main'.data_version`). (b) are
    discarded; (a) must equal exactly [DF_VOCAB_DDL_SQL, DF_SQL with `?` replaced
    by the quoted json.dumps(terms)]. A mutant that interpolates terms into the
    SQL text changes the (a) statement, so this goes red. Rows: only the real
    term; `%` / `_` / the injection string contribute nothing (no wildcard)."""
    terms = HOSTILE_TERMS + ["formula"]
    seen = []
    # SQLite's trace EXPANDS bound parameters, so an interpolated statement and a
    # bound one trace identically; the (sql, params) pair handed to the gate is
    # what separates them. Spy on it (pass-through) as the second half of the cell.
    gate = df_owner._client_read_gate
    real_execute_typed = gate.execute_typed
    handed = []

    def spy(sql, params=(), *, deadline):
        handed.append((sql, params))
        return real_execute_typed(sql, params, deadline=deadline)

    gate.execute_typed = spy
    df_owner._client_read_conn.set_trace_callback(seen.append)
    try:
        result = _df(df_owner, terms)
    finally:
        df_owner._client_read_conn.set_trace_callback(None)
        del gate.execute_typed
    assert handed == [(eo.DF_VOCAB_DDL_SQL, ()), (eo.DF_SQL, (json.dumps(terms),))], handed
    assert _as_dict(result) == _direct(df_owner.store_path, ["formula"])
    assert set(_as_dict(result)) == {"formula"}
    top_level = [stmt for stmt in seen if not stmt.startswith("-- ")]
    bound = "'" + json.dumps(terms).replace("'", "''") + "'"
    assert top_level == [eo.DF_VOCAB_DDL_SQL, eo.DF_SQL.replace("?", bound)], top_level


# --- F2: execute_typed accepts only the two typed statements -------------------

def test_typed_statements_set_is_exactly_the_two_constants():
    assert eo._TYPED_STATEMENTS == frozenset({eo.DF_SQL, eo.DF_VOCAB_DDL_SQL})


def test_execute_typed_refuses_a_non_member_statement_before_preparing_anything(tmp_path):
    """F2. A statement outside `_TYPED_STATEMENTS` raises ValueError naming its
    first 60 characters, and nothing is prepared (no authorizer call, no trace)."""
    db = _store(tmp_path)
    conn = sqlite3.connect(str(db))
    calls, traced = [], []
    conn.set_authorizer(lambda *a: calls.append(a) or sqlite3.SQLITE_OK)
    conn.set_trace_callback(traced.append)
    try:
        gate = eo._ClientReadGate(conn, home=tmp_path)
        evil = "SELECT 'a-statement-that-is-well-over-sixty-characters-long-xxxxxxxxxxxxxxxxxxxxx'"
        with pytest.raises(ValueError) as info:
            gate.execute_typed(evil, (), deadline=time.monotonic() + 5.0)
        assert evil[:60] in str(info.value) and evil[:61] not in str(info.value)
        with pytest.raises(ValueError):
            gate.execute_typed(eo.DF_SQL + " ", (json.dumps(["formula"]),), deadline=time.monotonic() + 5.0)
        assert calls == [] and traced == []
    finally:
        conn.close()


def test_execute_typed_still_runs_both_members(tmp_path):
    db = _store(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        gate = eo._ClientReadGate(conn, home=tmp_path)
        deadline = time.monotonic() + 5.0
        ddl = gate.execute_typed(eo.DF_VOCAB_DDL_SQL, (), deadline=deadline)
        assert len(ddl) == 1 and "fts5vocab" in ddl[0][0]
        rows = gate.execute_typed(eo.DF_SQL, (json.dumps(["formula"]),), deadline=deadline)
        assert {t: d for t, d in rows} == _direct(db, ["formula"])
    finally:
        conn.close()


# --- F4: df-side cells for the shared helper ----------------------------------

class _TypedRaisingGate:
    """Stand-in client-read gate whose execute_typed raises the given error."""

    def __init__(self, exc):
        self._exc = exc

    def execute_typed(self, sql, params=(), *, deadline):
        raise self._exc


def _handle_df(owner, params):
    return owner._handle_df(params, time.monotonic() + 5.0)


@requires_linux_owner
def test_df_with_no_client_read_gate_is_owner_stopping_but_validation_wins(df_owner):
    real = df_owner._client_read_gate
    df_owner._client_read_gate = None
    try:
        wire = _handle_df(df_owner, {"terms": ["formula"]})
        assert wire["status"] == "rejected" and wire["reason"] == "owner_stopping", wire
        invalid = _handle_df(df_owner, {"terms": []})
        assert invalid["reason"] == "df_terms_empty", invalid
    finally:
        df_owner._client_read_gate = real


@pytest.mark.parametrize("exc,reason", [
    pytest.param(sqlite3.OperationalError("database is locked"), "read_failed", id="operational-error"),
    pytest.param(sqlite3.DataError("string or blob too big"), "read_failed", id="data-error"),
    pytest.param(sqlite3.OperationalError("interrupted"), "deadline_exceeded", id="interrupt-by-message"),
])
@requires_linux_owner
def test_df_sqlite_errors_map_to_their_named_reasons(df_owner, exc, reason):
    real = df_owner._client_read_gate
    df_owner._client_read_gate = _TypedRaisingGate(exc)
    try:
        wire = _handle_df(df_owner, {"terms": ["formula"]})
    finally:
        df_owner._client_read_gate = real
    assert wire["status"] == "rejected" and wire["reason"] == reason, wire


@requires_linux_owner
def test_df_interrupt_by_errorcode_is_deadline_exceeded(df_owner):
    """The primary (3.11+) path: sqlite_errorcode == SQLITE_INTERRUPT, with a
    message that is NOT 'interrupted' so only the errorcode can classify it."""
    exc = sqlite3.OperationalError("some other text")
    exc.sqlite_errorcode = getattr(sqlite3, "SQLITE_INTERRUPT", 9)
    real = df_owner._client_read_gate
    df_owner._client_read_gate = _TypedRaisingGate(exc)
    try:
        wire = _handle_df(df_owner, {"terms": ["formula"]})
    finally:
        df_owner._client_read_gate = real
    assert wire["status"] == "rejected" and wire["reason"] == "deadline_exceeded", wire


# --- F5: the vocab-variant pattern is anchored on `USING fts5vocab(` ----------

@pytest.mark.parametrize("ddl", [
    pytest.param(
        "CREATE TABLE nodes_fts_vocab (term TEXT, doc INTEGER DEFAULT 'fts5vocab(t,row)')",
        id="plain-table-with-default-text",
    ),
    pytest.param(
        "CREATE VIRTUAL TABLE nodes_fts_vocab /* USING fts5vocab(nodes_fts,row) */ "
        "USING fts5vocab(nodes_fts, col)",
        id="comment-row-before-real-col-clause",
    ),
])
@requires_linux_owner
def test_df_misleading_text_outside_the_module_clause_is_unsupported_vocab_variant(home, ddl):
    _store_with_vocab_ddl(home, ddl)
    owner = _start(home)
    try:
        result = _df(owner, PRESENT)
    finally:
        owner.stop()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "unsupported_vocab_variant"


@pytest.mark.parametrize("ddl", [
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(nodes_fts, 'row')", id="single-quoted"),
    pytest.param('CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab("nodes_fts", "row")', id="double-quoted"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab([nodes_fts], [row])", id="bracketed"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(`nodes_fts`, `row`)", id="backticked"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(nodes_fts, row)", id="bare"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(\n  nodes_fts,\n  'row'\n)", id="newline-separated"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING FTS5Vocab(nodes_fts, 'ROW')", id="mixed-case"),
    pytest.param("CREATE VIRTUAL TABLE nodes_fts_vocab USING   fts5vocab  (nodes_fts , 'row' )", id="extra-spaces"),
])
@requires_linux_owner
def test_df_every_accepted_row_spelling_still_returns_the_row_result(home, ddl):
    db = _store_with_vocab_ddl(home, ddl)
    owner = _start(home)
    try:
        result = _df(owner, PRESENT)
    finally:
        owner.stop()
    assert _as_dict(result) == _direct(db, PRESENT)
