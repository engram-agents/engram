"""Contract cells C3 for U6b (#3665): the client shim routes the IDF batch
statement to the owner's typed `df` method (unit A's final contract).

Contract for `OwnerReadConnection.execute(sql, params)`:

  * `sql == engram_idf.IDF_BATCH_SQL` and `params` exactly one `str` that parses
    as a JSON list of `str` -> `read("df", {"terms": terms})`, NOT `read("read",
    ...)`; rows come back as a cursor of tuples exactly as for a normal read;
    budget / timeout / `.calls` accounting identical to the `read` route.
  * the owner refuses empty strings, terms over DF_TERM_MAX_BYTES UTF-8 bytes and
    an empty list by NAME, and the tokenizer has no length bound, so before
    sending the shim DROPS every empty or over-256-byte term (it cannot score);
    if nothing remains it returns an empty cursor with NO round trip and `.calls`
    unchanged. Order of refusals: closed -> RuntimeError, budget exhausted ->
    OwnerReadError, then the empty shortcut.
  * more than 1000 terms -> the ordinary `read` route (the owner rules).
  * any other `sql`, or IDF sql with params not matching -> `read("read", ...)`
    unchanged; the shim raises nothing of its own for them.
  * parity limit: a vocabulary term longer than 256 bytes would be scored on the
    direct path but not through the owner.
  * the FakeOwner `df` is faithful to the real owner: every REASON_CASES id, one
    ok case, and the vocab cases give the same status and reason on both.

Written BEFORE the implementation (contract-first).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_idf  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

from _fake_owner import FakeOwner, statement_of  # noqa: E402
from test_engramd_client_read_shim_3665 import Clock, StubRead  # noqa: E402
from test_engramd_df_3665 import REASON_CASES, _store, _store_with_vocab_ddl  # noqa: E402
from test_idf_batch_3665 import _SMALL_CORPUS, _make_db  # noqa: E402
from _owner_store import stamp_current_schema  # noqa: E402

IDF_SQL_LITERAL = (
    "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
)
TERMS = ["formula", "algorithm", "xylophone"]
DF_ROWS = [["formula", 10], ["algorithm", 2]]
OTHER_SQL = "SELECT count(*) FROM nodes WHERE is_current = 1"


def _idf_sql():
    return engram_idf.IDF_BATCH_SQL


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="dfs-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- 1 ------------------------------------------------------------------------

def test_idf_batch_sql_constant_pins_the_literal_statement():
    """C3.1b: engram_idf.IDF_BATCH_SQL equals the byte-exact statement (the constant cannot drift)."""
    assert engram_idf.IDF_BATCH_SQL == IDF_SQL_LITERAL


def test_idf_statement_routes_to_df_not_read(monkeypatch):
    """C3.1: the IDF statement + one JSON-list param calls read("df", {"terms": ...}) only,
    and returns the rows as a cursor of tuples."""
    stub = StubRead(ec.Ok(result=DF_ROWS))
    monkeypatch.setattr(ec, "read", stub)
    sp = Path("/tmp/x.sock")
    cur = ec.OwnerReadConnection(2.0, socket_path=sp).execute(_idf_sql(), (json.dumps(TERMS),))
    assert len(stub.calls) == 1
    method, params, _timeout, spath = stub.calls[0]
    assert method == "df"
    assert params == {"terms": TERMS}
    assert spath == sp
    first = cur.fetchone()
    assert type(first) is tuple and first == ("formula", 10)
    assert cur.fetchall() == [("algorithm", 2)]
    assert cur.fetchone() is None


def test_idf_statement_with_list_params_container_also_routes_to_df(monkeypatch):
    """C3.1c: params may be a list container (the shim accepts tuple|list); one str param still routes to df."""
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).execute(_idf_sql(), [json.dumps(TERMS)])
    assert [c[0] for c in stub.calls] == ["df"]


# --- 2 ------------------------------------------------------------------------

def test_any_other_sql_still_goes_through_read(monkeypatch):
    """C3.2: a non-IDF statement is sent as read("read", {"sql","args"}) unchanged."""
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).execute(OTHER_SQL, ("v", 3))
    method, params, _t, _s = stub.calls[0]
    assert method == "read"
    assert params == {"sql": OTHER_SQL, "args": ["v", 3]}


# --- 3 ------------------------------------------------------------------------

FALLTHROUGH_PARAMS = [
    pytest.param(("not json",), id="not-json"),
    pytest.param((json.dumps({"a": 1}),), id="json-object"),
    pytest.param((json.dumps([1, 2]),), id="json-list-of-int"),
    pytest.param((json.dumps(["a", 1]),), id="json-list-mixed"),
    pytest.param((json.dumps(TERMS), "extra"), id="two-params"),
    pytest.param((), id="no-params"),
    pytest.param((5,), id="non-str-param"),
]


@pytest.mark.parametrize("params", FALLTHROUGH_PARAMS)
def test_idf_statement_with_non_list_of_str_params_falls_through_to_read(monkeypatch, params):
    """C3.3: IDF sql whose params are not exactly one JSON list of str takes the ordinary
    read path, and the shim raises no exception of its own."""
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).execute(IDF_SQL_LITERAL, params)
    assert [c[0] for c in stub.calls] == ["read"]
    assert stub.calls[0][1] == {"sql": IDF_SQL_LITERAL, "args": list(params)}


# --- 3b: only the BYTE-EXACT statement routes to df ------------------------------------------

NEAR_MISS_SQL = [
    pytest.param(IDF_SQL_LITERAL + " LIMIT 1", id="limit-suffix"),
    pytest.param(IDF_SQL_LITERAL + " ", id="trailing-space"),
    pytest.param(IDF_SQL_LITERAL + ";", id="trailing-semicolon"),
    pytest.param(IDF_SQL_LITERAL.lower(), id="lower-case"),
    pytest.param(IDF_SQL_LITERAL.replace("WHERE term", "WHERE  term"), id="extra-internal-whitespace"),
]


def test_near_miss_cells_differ_from_the_statement_and_three_of_them_extend_it():
    """Anti-vacuity: each near miss is a different string; the extending ones (suffix,
    space, semicolon) start with the statement, which is what a startswith mutant accepts."""
    for param in NEAR_MISS_SQL:
        assert param.values[0] != IDF_SQL_LITERAL
    for suffix in (" LIMIT 1", " ", ";"):
        assert (IDF_SQL_LITERAL + suffix).startswith(IDF_SQL_LITERAL)


@pytest.mark.parametrize("sql", NEAR_MISS_SQL)
def test_a_near_miss_statement_takes_the_ordinary_read_route(monkeypatch, sql):
    """The shim routes to `df` ONLY when sql == IDF_BATCH_SQL byte for byte: a near miss with
    a perfectly valid JSON-list-of-str param is sent as read("read", {sql, args}) unchanged.
    Mutation: loosen the match to `sql.startswith(IDF_BATCH_SQL)` -> the suffix / space /
    semicolon cells go via `df` -> red; the exact-statement routing cells stay green."""
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).execute(sql, (json.dumps(TERMS),))
    assert [c[0] for c in stub.calls] == ["read"]
    assert stub.calls[0][1] == {"sql": sql, "args": [json.dumps(TERMS)]}


@pytest.mark.parametrize("sql", NEAR_MISS_SQL)
def test_a_near_miss_statement_over_the_fake_owner_is_rejected_unregistered_template(home, sql):
    """Against the FakeOwner the near miss is a Rejected `unregistered_template`, surfaced by
    the shim as OwnerReadError kind "rejected" (never rows from the df op).
    Mutation: prefix match -> the shim sends `df`, the fake answers rows -> no error -> red."""
    _backup_small_corpus(home)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        with pytest.raises(ec.OwnerReadError) as ei:
            shim.execute(sql, (json.dumps(["formula"]),))
        methods = [q["method"] for q in o.requests]
    assert ei.value.kind == "rejected", ei.value
    assert isinstance(ei.value.result, ec.Rejected) and ei.value.result.reason == "unregistered_template"
    assert "df" not in methods


@requires_linux_owner
@pytest.mark.parametrize("sql", NEAR_MISS_SQL)
def test_a_near_miss_statement_over_a_real_owner_is_rejected_unregistered_template(home, sql):
    """The same against a REAL owner. Mutation: as the fake-owner cell -> red."""
    _store(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        with pytest.raises(ec.OwnerReadError) as ei:
            shim.execute(sql, (json.dumps(["formula"]),))
    finally:
        owner.stop()
    assert ei.value.kind == "rejected", ei.value
    assert isinstance(ei.value.result, ec.Rejected) and ei.value.result.reason == "unregistered_template"


# --- 4 ------------------------------------------------------------------------

def test_df_route_budget_and_call_accounting(monkeypatch):
    """C3.4: the df route counts `.calls`, uses min(per_call, remaining) as timeout, and
    refuses with budget_exhausted WITHOUT calling the owner once remaining <= 0."""
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0, 0.6)
    args = (json.dumps(TERMS),)
    conn.execute(_idf_sql(), args)
    assert stub.calls[-1][0] == "df" and stub.calls[-1][2] == pytest.approx(0.6)
    clk.t += 0.7
    conn.execute(_idf_sql(), args)
    assert stub.calls[-1][2] == pytest.approx(0.3)
    assert conn.calls == 2
    clk.t += 0.3  # remaining exactly 0
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(_idf_sql(), args)
    assert ei.value.kind == "budget_exhausted" and ei.value.result is None
    assert len(stub.calls) == 2 and conn.calls == 2


def test_df_route_failures_are_typed_like_read(monkeypatch):
    """C3.4b: a Rejected / Unavailable on the df route raises OwnerReadError carrying the result, as for read."""
    rj = ec.Rejected(reason="df_term_empty", detail="d", remedy="m")
    monkeypatch.setattr(ec, "read", StubRead(rj))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).execute(_idf_sql(), (json.dumps(TERMS),))
    assert ei.value.kind == "rejected" and ei.value.result is rj


# --- 5 ------------------------------------------------------------------------

def _direct_row_conn(home):
    uri = (home / "knowledge.db").resolve().as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


@pytest.mark.parametrize("text,min_idf", [
    ("the ranking formula orders nodes with algorithm convergence", 0.0),
    ("the ranking formula orders nodes with algorithm convergence", 4.0),
    ("café über naïve résumé xylophone qqqzzz", 0.0),
])
@requires_linux_owner
def test_extract_keywords_over_a_live_owner_equals_direct(home, text, min_idf):
    """C3.5: extract_keywords over the shim and a live owner returns the same (token, score)
    list as over a direct connection, on _SMALL_CORPUS."""
    import engramd_owner as eo

    mem = _make_db(_SMALL_CORPUS)
    dest = sqlite3.connect(str(home / "knowledge.db"))
    mem.backup(dest)
    dest.close()
    stamp_current_schema(home / "knowledge.db")  # #3753: the owner refuses a store below the latest schema version (backup() replaces the header)
    direct = engram_idf.extract_keywords(_direct_row_conn(home), text, min_idf=min_idf)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        via = engram_idf.extract_keywords(shim, text, min_idf=min_idf)
    finally:
        owner.stop()
    assert via == direct
    if min_idf == 0.0 and "formula" in text:
        assert direct  # not vacuous


# --- 6: pins ------------------------------------------------------------------

def test_pin_client_and_owner_and_idf_constants_agree():
    """The equalities unit A could not assert: the client's term-byte cap and ceiling equal the
    owner's, and the owner's DF_TERMS_MAX equals the hook's real batch size."""
    assert ec._DF_TERM_MAX_BYTES == eo.DF_TERM_MAX_BYTES == 256
    assert ec._DF_TERMS_MAX == eo.DF_TERMS_MAX
    assert eo.DF_TERMS_MAX == engram_idf.IDF_BATCH_TERMS


def test_pin_fake_owner_constants_equal_the_production_ones():
    """The FakeOwner carries literal copies; each must equal production or the faithfulness
    cell would compare the fake with itself."""
    import _fake_owner as fo

    assert fo.IDF_BATCH_SQL == engram_idf.IDF_BATCH_SQL == eo.DF_SQL
    assert fo.DF_TERMS_MAX == eo.DF_TERMS_MAX
    assert fo.DF_TERM_MAX_BYTES == eo.DF_TERM_MAX_BYTES
    assert fo._DF_VOCAB_DDL_SQL == eo.DF_VOCAB_DDL_SQL
    assert fo._DF_VOCAB_VARIANT_RE.pattern == eo._DF_VOCAB_VARIANT_RE.pattern
    assert fo._DF_VOCAB_VARIANT_RE.flags == eo._DF_VOCAB_VARIANT_RE.flags
    assert fo._DF_SQL_COMMENT_RE.pattern == eo._DF_SQL_COMMENT_RE.pattern
    assert fo._DF_SQL_COMMENT_RE.flags == eo._DF_SQL_COMMENT_RE.flags


# --- 7: the client drops what the owner would refuse ------------------------------

def _routed(monkeypatch, terms, outcome=None):
    stub = StubRead(outcome or ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(2.0)
    cur = conn.execute(_idf_sql(), (json.dumps(terms),))
    return stub, conn, cur


def test_term_of_exactly_256_bytes_is_sent_and_257_is_dropped_rest_still_sent(monkeypatch):
    """C3.6a: 256-byte term sent; 257-byte term dropped; the other terms keep their order."""
    stub, _c, _cur = _routed(monkeypatch, ["formula", "a" * 256, "b" * 257, "algorithm"])
    assert [c[0] for c in stub.calls] == ["df"]
    assert stub.calls[0][1] == {"terms": ["formula", "a" * 256, "algorithm"]}


def test_term_cap_counts_utf8_bytes_not_characters(monkeypatch):
    """C3.6a: 128 two-byte characters = 256 bytes is sent; 129 of them (129 characters, 258
    bytes) is dropped. A characters-not-bytes cap would send both."""
    stub, _c, _cur = _routed(monkeypatch, ["é" * 128, "é" * 129, "formula"])
    assert stub.calls[0][1] == {"terms": ["é" * 128, "formula"]}


def test_empty_string_term_is_dropped(monkeypatch):
    """C3.6b."""
    stub, _c, _cur = _routed(monkeypatch, ["formula", "", "algorithm"])
    assert stub.calls[0][1] == {"terms": ["formula", "algorithm"]}


@pytest.mark.parametrize("terms", [[], [""], ["a" * 257], ["", "b" * 300, "é" * 129]],
                         ids=["empty-list", "only-empty", "only-too-long", "all-dropped-mix"])
def test_nothing_left_returns_an_empty_cursor_without_a_round_trip(monkeypatch, terms):
    """C3.6c: all terms dropped (or an empty list) -> empty cursor, ZERO owner calls, `.calls`
    unchanged."""
    stub, conn, cur = _routed(monkeypatch, terms)
    assert stub.calls == []
    assert conn.calls == 0
    assert cur.fetchall() == [] and cur.fetchone() is None


def test_more_than_1000_terms_take_the_ordinary_read_route(monkeypatch):
    """C3.6d: 1001 terms -> read("read", ...) with the statement and the whole JSON list as args
    (the owner rules); exactly 1000 terms -> df."""
    many = ["t%d" % i for i in range(1001)]
    stub, conn, _cur = _routed(monkeypatch, many)
    assert [c[0] for c in stub.calls] == ["read"]
    assert stub.calls[0][1] == {"sql": IDF_SQL_LITERAL, "args": [json.dumps(many)]}
    assert conn.calls == 1
    stub2, _c2, _cu2 = _routed(monkeypatch, many[:1000])
    assert [c[0] for c in stub2.calls] == ["df"]


def test_the_ceiling_counts_the_list_as_given_not_what_remains_after_dropping(monkeypatch):
    """C3.6d': 1000 good terms + one droppable 300-byte term = 1001 entries -> ordinary `read`
    route (no silent shrink to fit the cap; the owner rules)."""
    many = ["t%d" % i for i in range(1000)] + ["x" * 300]
    stub, _c, _cur = _routed(monkeypatch, many)
    assert [c[0] for c in stub.calls] == ["read"]


def test_order_closed_then_budget_then_empty_shortcut(monkeypatch):
    """C3.6f: a closed connection raises RuntimeError and an exhausted budget raises
    OwnerReadError(budget_exhausted) EVEN when every term would be dropped; the empty shortcut
    answers only on an open connection with budget left."""
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    empty = (json.dumps([""]),)
    conn = ec.OwnerReadConnection(1.0)
    assert conn.execute(_idf_sql(), empty).fetchall() == []  # open + budget -> shortcut
    clk.t += 1.5
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(_idf_sql(), empty)
    assert ei.value.kind == "budget_exhausted"
    conn.close()
    with pytest.raises(RuntimeError):  # closed wins over an exhausted budget
        conn.execute(_idf_sql(), empty)
    assert stub.calls == [] and conn.calls == 0


def test_a_non_str_sql_is_still_a_typeerror_before_the_shortcut(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=[])))
    with pytest.raises(TypeError):
        ec.OwnerReadConnection(2.0).execute(None, (json.dumps([""]),))


# --- 8: end to end with a 300-byte token ----------------------------------------------

LONG_TOKEN = "q" * 300


def _e2e_text():
    return "the ranking formula orders nodes with algorithm convergence " + LONG_TOKEN


def _backup_small_corpus(home):
    mem = _make_db(_SMALL_CORPUS)
    dest = sqlite3.connect(str(home / "knowledge.db"))
    mem.backup(dest)
    dest.close()
    stamp_current_schema(home / "knowledge.db")  # #3753: the owner refuses a store below the latest schema version (backup() replaces the header)


def test_extract_keywords_with_a_300_byte_token_over_the_fake_owner_equals_direct(home):
    """C3.6e (every host): the 300-byte token is not sent; the result list equals the direct
    path's (the token is absent from the corpus, so it could not score)."""
    _backup_small_corpus(home)
    text = _e2e_text()
    assert LONG_TOKEN in engram_idf.tokenize(text)  # the tokenizer really produces it
    direct = engram_idf.extract_keywords(_direct_row_conn(home), text, min_idf=0.0)
    assert direct  # not vacuous
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        via = engram_idf.extract_keywords(shim, text, min_idf=0.0)
        sent = [t for q in o.requests if q["method"] == "df" for t in q["params"]["terms"]]
        reads = [q for q in o.requests if q["method"] == "read"]
    assert via == direct
    assert sent and LONG_TOKEN not in sent
    assert all(q["params"]["sql"] != IDF_SQL_LITERAL for q in reads)


@requires_linux_owner
def test_extract_keywords_with_a_300_byte_token_over_a_live_owner_equals_direct(home):
    """C3.6e (real owner): same, through the real owner, which would have refused the token."""
    _backup_small_corpus(home)
    text = _e2e_text()
    direct = engram_idf.extract_keywords(_direct_row_conn(home), text, min_idf=0.0)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        via = engram_idf.extract_keywords(shim, text, min_idf=0.0)
    finally:
        owner.stop()
    assert via == direct and direct


# --- 9: statement_of, and the IDF statement on `read` --------------------------------------

def test_statement_of_normalises_df_and_read_requests():
    assert statement_of("df", {"terms": ["a", "b"]}) == (IDF_SQL_LITERAL, [json.dumps(["a", "b"])])
    assert statement_of("read", {"sql": OTHER_SQL, "args": [1]}) == (OTHER_SQL, [1])
    assert statement_of("ping", {}) == (None, None)
    assert statement_of("read", None) == (None, None)


def test_fake_owner_refuses_the_idf_statement_on_read_like_the_real_owner(home):
    """The real owner drops the listed IDF template at load -> `refused_at_load`, detail `virtual_table_template` (#3753;
    it was `unregistered_template` before); the fake must say the same, or a suite could pass with the batch travelling as `read`.
    Mutation: the fake answers the IDF statement on `read` with ok -> red."""
    _backup_small_corpus(home)
    with FakeOwner(home) as o:
        res = ec.read("read", {"sql": IDF_SQL_LITERAL, "args": [json.dumps(["formula"])]},
                      timeout=5.0, socket_path=o.socket_path)
    assert isinstance(res, ec.Rejected) and res.reason == "refused_at_load", res
    assert res.detail == "virtual_table_template", res


@requires_linux_owner
def test_the_real_owner_also_refuses_the_idf_statement_on_read(home):
    """The fact the fake imitates, checked against the real owner."""
    _store(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        res = ec.read("read", {"sql": IDF_SQL_LITERAL, "args": [json.dumps(["formula"])]},
                      timeout=5.0, socket_path=owner.socket_path)
    finally:
        owner.stop()
    assert isinstance(res, ec.Rejected) and res.reason == "refused_at_load", res
    assert res.detail == "virtual_table_template", res


# --- 10: FAITHFULNESS -- the fake's df against a REAL owner ------------------------------

class _Pair:
    """A FakeOwner and a real owner, each over its own copy of the same store."""

    def __init__(self, variant="row", ddl=None):
        self.fake_home = Path(tempfile.mkdtemp(prefix="dff-"))
        self.real_home = Path(tempfile.mkdtemp(prefix="dfr-"))
        for h in (self.fake_home, self.real_home):
            if ddl is None:
                _store(h, variant)
            else:
                _store_with_vocab_ddl(h, ddl)

    def __enter__(self):
        self.fake = FakeOwner(self.fake_home)
        self.fake.start()
        self.real = eo.EngramdOwner(self.real_home, request_deadline=0.6)
        self.real.start(install_signal_handlers=False)
        return self

    def __exit__(self, *exc):
        self.real.stop()
        self.fake.stop()
        shutil.rmtree(self.fake_home, ignore_errors=True)
        shutil.rmtree(self.real_home, ignore_errors=True)

    def ask(self, params):
        f = ec.read("df", params, timeout=5.0, socket_path=self.fake.socket_path)
        r = ec.read("df", params, timeout=5.0, socket_path=self.real.socket_path)
        return f, r


@pytest.fixture(scope="module")
def pair_row():
    with _Pair("row") as p:
        yield p


@pytest.mark.parametrize("params,reason", REASON_CASES)
@requires_linux_owner
def test_fake_df_gives_the_same_status_and_reason_as_the_real_owner(pair_row, params, reason):
    """FAITHFULNESS: for every REASON_CASES id the fake and a REAL owner both reject with the
    same reason (and that reason is the table's). Mutation: the fake skips validation, or
    returns another reason for one case -> red for that id."""
    f, r = pair_row.ask(params)
    assert isinstance(r, ec.Rejected) and r.reason == reason, r
    assert isinstance(f, ec.Rejected), f
    assert f.reason == r.reason == reason


@requires_linux_owner
def test_fake_df_gives_the_same_rows_as_the_real_owner(pair_row):
    """One ok case compared row for row (order is not part of the contract: sorted)."""
    terms = ["formula", "algorithm", "xylophone", "ranking", "qqqzzz"]
    f, r = pair_row.ask({"terms": terms})
    assert isinstance(f, ec.Ok) and isinstance(r, ec.Ok), (f, r)
    assert sorted(f.result) == sorted(r.result)
    assert {t for t, _ in r.result} == {"formula", "algorithm", "xylophone", "ranking"}  # not vacuous


@pytest.mark.parametrize("variant,reason", [(None, "vocab_table_absent"),
                                            ("col", "unsupported_vocab_variant"),
                                            ("instance", "unsupported_vocab_variant")])
@requires_linux_owner
def test_fake_df_matches_the_real_owner_on_store_side_rejections(variant, reason):
    with _Pair(variant) as p:
        f, r = p.ask({"terms": ["formula"]})
    assert isinstance(r, ec.Rejected) and r.reason == reason, r
    assert isinstance(f, ec.Rejected) and f.reason == reason, f


_HOSTILE_VOCAB_DDLS = [
    pytest.param(
        "CREATE TABLE nodes_fts_vocab (term TEXT, doc INTEGER DEFAULT 'fts5vocab(t,row)')",
        id="plain-table-with-default-text",
    ),
    pytest.param(
        "CREATE VIRTUAL TABLE nodes_fts_vocab /* USING fts5vocab(nodes_fts,row) */ "
        "USING fts5vocab(nodes_fts, col)",
        id="comment-row-before-real-col-clause",
    ),
]


@pytest.mark.parametrize("ddl", _HOSTILE_VOCAB_DDLS)
@requires_linux_owner
def test_fake_df_matches_the_real_owner_on_hostile_vocab_ddl(ddl):
    """FAITHFULNESS: text that merely looks like a row module clause (a column default; a
    commented-out clause before a real col clause) is unsupported_vocab_variant on BOTH."""
    with _Pair(ddl=ddl) as p:
        f, r = p.ask({"terms": ["formula"]})
    assert isinstance(r, ec.Rejected) and r.reason == "unsupported_vocab_variant", r
    assert isinstance(f, ec.Rejected) and f.reason == "unsupported_vocab_variant", f


@requires_linux_owner
def test_fake_df_matches_the_real_owner_on_an_accepted_spelling():
    """Not vacuous: an accepted odd spelling answers ok with identical rows on both."""
    ddl = "CREATE VIRTUAL TABLE nodes_fts_vocab USING   FTS5Vocab  ( [nodes_fts] , `ROW` )"
    with _Pair(ddl=ddl) as p:
        f, r = p.ask({"terms": ["formula", "algorithm"]})
    assert isinstance(f, ec.Ok) and isinstance(r, ec.Ok), (f, r)
    assert sorted(f.result) == sorted(r.result) and r.result


# --- 11: vocab_table_absent is a fact about the graph, not an owner failure (U6b follow-up) ---------
#
# Design seat ruling (crossing 10237): on the df route the shim maps the owner's
# `vocab_table_absent` rejection to the EMPTY result (every term absent from the vocabulary);
# every other rejection, and every non-df route, still raises OwnerReadError "rejected".
# Written BEFORE the implementation.

def _small_corpus_without_vocab(home):
    """_SMALL_CORPUS in home/knowledge.db with nodes_fts_vocab dropped."""
    _backup_small_corpus(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("DROP TABLE nodes_fts_vocab")
    conn.commit()
    conn.close()


def _assert_empty_cursor(cur):
    assert cur.fetchone() is None
    assert cur.fetchall() == []


def test_vocab_table_absent_over_the_fake_owner_is_an_empty_cursor(home):
    """CELL 1 (fake owner): the df rejection vocab_table_absent -> empty cursor, one round trip counted.
    Mutation M_NO_MAP (shim raises) -> red."""
    _small_corpus_without_vocab(home)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        cur = shim.execute(_idf_sql(), (json.dumps(["formula"]),))
        methods = [q["method"] for q in o.requests]
    _assert_empty_cursor(cur)
    assert shim.calls == 1 and methods == ["df"]


@requires_linux_owner
def test_vocab_table_absent_over_a_real_owner_is_an_empty_cursor(home):
    """CELL 1 (real owner): the underlying reply really is the owner's vocab_table_absent."""
    _small_corpus_without_vocab(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        raw = ec.read("df", {"terms": ["formula"]}, timeout=5.0, socket_path=owner.socket_path)
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        cur = shim.execute(_idf_sql(), (json.dumps(["formula"]),))
    finally:
        owner.stop()
    assert isinstance(raw, ec.Rejected) and raw.reason == "vocab_table_absent", raw
    _assert_empty_cursor(cur)
    assert shim.calls == 1


@requires_linux_owner
@pytest.mark.parametrize("variant", ["col", "instance"])
def test_unsupported_vocab_variant_still_raises_over_a_real_owner(home, variant):
    """CELL 2a: only vocab_table_absent is mapped; an unsupported variant stays a rejection.
    Mutation M_MAP_ALL_REJECTED -> red."""
    _store(home, variant)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        with pytest.raises(ec.OwnerReadError) as ei:
            shim.execute(_idf_sql(), (json.dumps(["formula"]),))
    finally:
        owner.stop()
    assert ei.value.kind == "rejected"
    assert isinstance(ei.value.result, ec.Rejected) and ei.value.result.reason == "unsupported_vocab_variant"


def test_unsupported_vocab_variant_still_raises_over_the_fake_owner(home):
    """CELL 2a (fake owner). Mutation M_MAP_ALL_REJECTED -> red."""
    _store(home, "col")
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        with pytest.raises(ec.OwnerReadError) as ei:
            shim.execute(_idf_sql(), (json.dumps(["formula"]),))
    assert ei.value.kind == "rejected" and ei.value.result.reason == "unsupported_vocab_variant"


@pytest.mark.parametrize("reason", ["df_term_empty", "df_terms_empty", "owner_stopping",
                                    "deadline_exceeded", "read_failed", "unsupported_vocab_variant",
                                    # look-alikes: the mapping is the EXACT string, not a fuzzy match
                                    "VOCAB_TABLE_ABSENT", " vocab_table_absent",
                                    "vocab_table_absent ", "vocab_table_present",
                                    "vocab_table_absent_x", "vocab_table", "absent"])
def test_every_other_rejection_on_the_df_route_still_raises(monkeypatch, reason):
    """CELL 2b: a stub returns the Rejected; every reason except vocab_table_absent raises
    kind "rejected" carrying the result. Mutation M_MAP_ALL_REJECTED -> red."""
    rj = ec.Rejected(reason=reason, detail="d", remedy="m")
    monkeypatch.setattr(ec, "read", StubRead(rj))
    conn = ec.OwnerReadConnection(2.0)
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(_idf_sql(), (json.dumps(TERMS),))
    assert ei.value.kind == "rejected" and ei.value.result is rj
    assert conn.calls == 1


def test_vocab_table_absent_on_the_ordinary_read_route_still_raises(monkeypatch):
    """CELL 2c: the mapping is df-route only. A non-IDF read, and an IDF statement that
    falls through to `read` (params not a JSON list), that get vocab_table_absent still raise.
    Mutation M_MAP_ON_READ_ROUTE -> red."""
    rj = ec.Rejected(reason="vocab_table_absent", detail="d", remedy="m")
    stub = StubRead(rj)
    monkeypatch.setattr(ec, "read", stub)
    for sql, params in [(OTHER_SQL, ()), (IDF_SQL_LITERAL, ("not json",))]:
        with pytest.raises(ec.OwnerReadError) as ei:
            ec.OwnerReadConnection(2.0).execute(sql, params)
        assert ei.value.kind == "rejected" and ei.value.result is rj
    assert [c[0] for c in stub.calls] == ["read", "read"]


def test_vocab_table_absent_on_the_df_route_is_empty_and_counted_with_a_stub(monkeypatch):
    """CELL 1 (stub): the empty cursor does not depend on a particular owner; Unavailable stays an error.
    Mutation M_NO_MAP -> red."""
    stub = StubRead(ec.Rejected(reason="vocab_table_absent", detail="d", remedy="m"))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(2.0)
    _assert_empty_cursor(conn.execute(_idf_sql(), (json.dumps(TERMS),)))
    assert conn.calls == 1 and [c[0] for c in stub.calls] == ["df"]
    monkeypatch.setattr(ec, "read", StubRead(ec.Unavailable(socket_path="/tmp/x.sock", bound=1.0, agent_steps=["a"], human_steps=["h"])))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).execute(_idf_sql(), (json.dumps(TERMS),))
    assert ei.value.kind == "unavailable"


def test_extract_keywords_over_the_shim_with_the_vocab_table_dropped_is_empty(home):
    """CELL 3: no keyword passes when every term is absent. Direct-path observation: the
    direct connection RAISES sqlite3.OperationalError inside extract_keywords (no such table);
    the hook's recall-site `except Exception` is what makes the direct observable empty.
    Mutation M_NO_MAP -> OwnerReadError, red."""
    _small_corpus_without_vocab(home)
    text = "the ranking formula orders nodes with algorithm convergence"
    with pytest.raises(sqlite3.OperationalError):
        engram_idf.extract_keywords(_direct_row_conn(home), text, min_idf=0.0)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        assert engram_idf.extract_keywords(shim, text, min_idf=0.0) == []
        assert "df" in [q["method"] for q in o.requests]


@requires_linux_owner
def test_extract_keywords_over_a_real_owner_with_the_vocab_table_dropped_is_empty(home):
    """CELL 3 (real owner)."""
    _small_corpus_without_vocab(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        out = engram_idf.extract_keywords(shim, "the ranking formula algorithm", min_idf=0.0)
    finally:
        owner.stop()
    assert out == []
