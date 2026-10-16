"""Contract tests for the batched document-frequency lookup in
``engram_idf.extract_keywords`` (#3665 U6b stage 2a).

Contract:
  * ONE new statement, byte-exact::

        SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))

    called with a single parameter ``json.dumps(list_of_terms)``.
  * Chunked at the module constant ``IDF_BATCH_TERMS`` (1000) distinct terms
    per query; an empty candidate list executes nothing.
  * Output (tokens, float scores, order) is BYTE-IDENTICAL to the old
    per-token loop, which is kept below, verbatim, as the oracle.
  * It needs only ``conn.execute(sql, params).fetchone()/fetchall()`` and
    reads rows by index (the owner-client shim shape).
  * ``idf()`` is unchanged.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_idf  # noqa: E402
from engram_idf import (  # noqa: E402
    STOPWORDS,
    ensure_vocab_table,
    extract_keywords,
    idf,
    tokenize,
)

BATCH_SQL = (
    "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
)
PER_TOKEN_SQL = "SELECT doc FROM nodes_fts_vocab WHERE term = ?"


# ---------------------------------------------------------------------------
# Oracle: today's per-token implementation, copied verbatim from
# `git show origin/dev:src/engram/engram_idf.py` (97f2a499).
# ---------------------------------------------------------------------------

def _old_extract_keywords(
    conn,
    text,
    min_idf=4.0,
    top_k=10,
    stopwords=None,
):
    if stopwords is None:
        stopwords = STOPWORDS

    row = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
    ).fetchone()
    n_docs = row[0] if row else 0

    if n_docs == 0:
        return []

    tokens = tokenize(text)

    scored = []
    seen = set()

    for token in tokens:
        if token in stopwords:
            continue
        if token in seen:
            continue
        seen.add(token)

        score = idf(conn, token, n_docs=n_docs)
        if score is None:
            continue
        if score < min_idf:
            continue
        scored.append((token, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_db(docs, extra_current_flags=None):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE nodes (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL,
            claim TEXT, quoted_text TEXT, interpretation TEXT,
            is_current INTEGER DEFAULT 1)"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid')"""
    )
    for i, claim in enumerate(docs):
        conn.execute(
            "INSERT INTO nodes (id, claim, is_current) VALUES (?, ?, 1)",
            ("ob_%05d" % i, claim),
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)"
            " VALUES (last_insert_rowid(), ?, NULL, NULL)",
            (claim,),
        )
    ensure_vocab_table(conn)
    conn.commit()
    return conn


def _word(i):
    """Distinct tokenizable word for index i (two letters + base-26 tail)."""
    s = ""
    n = i
    while True:
        s = chr(97 + n % 26) + s
        n //= 26
        if n == 0:
            break
    return "zq" + s


def _big_db(n_terms=2600, n_docs=3000):
    """Terms 0..49 are common (df ~ n_docs/50); every other term has df 1-2."""
    docs = []
    for j in range(n_docs):
        docs.append("%s %s" % (_word(j % n_terms), _word(j % 50)))
    return _make_db(docs)


_SMALL_CORPUS = (
    ["the ranking formula orders nodes"] * 6
    + ["formula weights are tunable"] * 4
    + ["algorithm convergence is checked", "algorithm operates on embedding space"]
    + ["xylophone appears once", "café über naïve résumé term", "Mixed CASE Token here"]
    + ["padding words about calibration %d and provenance %d" % (i, i) for i in range(30)]
)


@pytest.fixture(scope="module")
def small_db():
    return _make_db(_SMALL_CORPUS)


@pytest.fixture(scope="module")
def big_db():
    return _big_db()


class Recorder:
    """Proxy: logs (sql, params) of every execute, delegates to a real conn."""

    def __init__(self, conn):
        self._c = conn
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self._c.execute(sql, params)

    def batch_calls(self):
        return [c for c in self.calls if c[0] == BATCH_SQL]


class _StubCursor:
    def __init__(self, rows):
        self._rows = [tuple(r) for r in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class StubConn:
    """Shim shape: execute -> fetchone()/fetchall() of plain tuples only."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=()):
        return _StubCursor(self._c.execute(sql, params).fetchall())


def _same(conn, text, **kw):
    old = _old_extract_keywords(conn, text, **kw)
    new = extract_keywords(conn, text, **kw)
    assert new == old
    # Byte-identical floats, not merely close.
    assert [repr(s) for _, s in new] == [repr(s) for _, s in old]
    return new


# ---------------------------------------------------------------------------
# OLD-vs-NEW equivalence
# ---------------------------------------------------------------------------

SMALL_TEXTS = [
    "the ranking formula and algorithm convergence on xylophone",
    "xylophone xylophone xylophone XYLOPHONE duplicates duplicates",
    "",
    "   ... 123 456 !!! ",
    "the and of to in a it is",  # all stopwords
    "thanks great okay well actually calibration provenance",
    "totally unknown vocabulary qwertyuiop asdfghjkl",
    "résumé café über naïve mixed case token",
    "calibration provenance padding words about",
    "ranking formula weights algorithm embedding space xylophone calibration",
]


@pytest.mark.parametrize("text", SMALL_TEXTS)
@pytest.mark.parametrize("top_k", [1, 3, 10, 500])
@pytest.mark.parametrize("min_idf", [0.0, 1.0, 4.0, 99.0])
def test_small_store_equals_oracle(small_db, text, top_k, min_idf):
    _same(small_db, text, min_idf=min_idf, top_k=top_k)


def test_custom_stopwords_equal_oracle(small_db):
    _same(small_db, "ranking formula xylophone", min_idf=0.0, stopwords=frozenset({"ranking"}))
    _same(small_db, "ranking formula xylophone", min_idf=0.0, stopwords=frozenset())


def test_ties_keep_first_seen_order(small_db):
    # 'calibration' and 'provenance' have identical df; order must be text order.
    a = _same(small_db, "calibration provenance", min_idf=0.0)
    b = _same(small_db, "provenance calibration", min_idf=0.0)
    assert [t for t, _ in a] == ["calibration", "provenance"]
    assert [t for t, _ in b] == ["provenance", "calibration"]
    assert a[0][1] == a[1][1]


def test_empty_store_returns_empty_and_executes_no_batch():
    conn = _make_db([])
    rec = Recorder(conn)
    assert extract_keywords(rec, "anything at all here") == []
    assert rec.batch_calls() == []


def test_store_with_no_current_nodes_returns_empty():
    conn = _make_db(["alpha beta gamma"])
    conn.execute("UPDATE nodes SET is_current = 0")
    rec = Recorder(conn)
    assert extract_keywords(rec, "alpha beta gamma") == []
    assert rec.batch_calls() == []


def test_df_greater_than_n_docs_is_unknown():
    # nodes marked non-current keep their FTS rows here, so df can exceed
    # n_docs: both implementations must treat that as unknown.
    conn = _make_db(["alpha beta gamma"] * 4)
    conn.execute("UPDATE nodes SET is_current = 0 WHERE rowid > 1")
    # min_idf is very negative so only the df > n_docs rule can exclude them
    # (log(1/4) < 0 would otherwise be filtered by min_idf=0.0 and mask it).
    assert _same(conn, "alpha beta gamma", min_idf=-100.0) == []


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 50, 1000])
@pytest.mark.parametrize("top_k", [10, 5000])
def test_big_store_equals_oracle_across_chunk_sizes(big_db, monkeypatch, chunk, top_k):
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", chunk)
    words = [_word(i) for i in range(0, 2600, 1)]
    # Duplicates and stopwords sprinkled in; first-seen order is non-sorted.
    text = " ".join(reversed(words[:900]) if chunk % 2 else words[:900])
    text += " the and " + " ".join(words[900:1700]) + " " + words[5] + " " + words[1500]
    if chunk > 20:
        _same(big_db, text, min_idf=4.0, top_k=top_k)
    else:
        # smaller input keeps the cheap-chunk cells cheap
        _same(big_db, " ".join(words[:120]) + " the " + words[3], min_idf=4.0, top_k=top_k)


def test_more_than_two_chunk_boundaries_with_real_constant(big_db):
    assert engram_idf.IDF_BATCH_TERMS == 1000
    words = [_word(i) for i in range(2500)]
    text = " ".join(words)
    rec = Recorder(big_db)
    new = extract_keywords(rec, text, min_idf=0.0, top_k=100000)
    assert new == _old_extract_keywords(big_db, text, min_idf=0.0, top_k=100000)
    assert len(rec.batch_calls()) == 3
    sizes = [len(json.loads(p[0])) for _, p in rec.batch_calls()]
    assert sizes == [1000, 1000, 500]
    # the late (last, partial-chunk) terms are present in the output
    got = {t for t, _ in new}
    assert words[2499] in got and words[1000] in got and words[999] in got


@pytest.mark.parametrize(
    "n_terms,chunk,expect_calls",
    [(999, 1000, 1), (1000, 1000, 1), (1001, 1000, 2), (2000, 1000, 2), (2001, 1000, 3), (5, 2, 3), (6, 2, 3), (7, 2, 4)],
)
def test_chunk_boundary_call_counts_and_params(big_db, monkeypatch, n_terms, chunk, expect_calls):
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", chunk)
    words = [_word(i) for i in range(n_terms)]
    rec = Recorder(big_db)
    out = extract_keywords(rec, " ".join(words), min_idf=0.0, top_k=100000)
    assert out == _old_extract_keywords(big_db, " ".join(words), min_idf=0.0, top_k=100000)
    calls = rec.batch_calls()
    assert len(calls) == expect_calls
    for k, (sql, params) in enumerate(calls):
        assert sql == BATCH_SQL
        assert len(params) == 1
        assert params[0] == json.dumps(words[k * chunk:(k + 1) * chunk])


# ---------------------------------------------------------------------------
# Statement contract
# ---------------------------------------------------------------------------

def test_uses_only_the_batch_statement_for_df(small_db):
    rec = Recorder(small_db)
    extract_keywords(rec, "ranking formula algorithm xylophone", min_idf=0.0)
    sqls = [c[0] for c in rec.calls]
    assert sqls[0] == "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
    assert PER_TOKEN_SQL not in sqls
    assert sqls.count(BATCH_SQL) == 1
    assert len(sqls) == 2


def test_empty_candidate_list_executes_no_batch(small_db):
    rec = Recorder(small_db)
    assert extract_keywords(rec, "the and of 123 ...") == []
    assert rec.batch_calls() == []


def test_batch_params_are_distinct_first_seen_non_stopword_terms(small_db):
    rec = Recorder(small_db)
    extract_keywords(rec, "Xylophone the ranking xylophone formula ranking", min_idf=0.0)
    (sql, params), = rec.batch_calls()
    assert params == (json.dumps(["xylophone", "ranking", "formula"]),)


def test_idf_function_unchanged(small_db):
    rec = Recorder(small_db)
    idf(rec, "xylophone", n_docs=10)
    assert rec.calls == [(PER_TOKEN_SQL, ("xylophone",))]


def test_works_on_shim_shape_connection(small_db):
    text = "ranking formula algorithm xylophone calibration provenance"
    assert extract_keywords(StubConn(small_db), text, min_idf=0.0) == _old_extract_keywords(
        small_db, text, min_idf=0.0
    )


def test_works_on_row_factory_connection():
    conn = _make_db(_SMALL_CORPUS)
    conn.row_factory = sqlite3.Row
    text = "ranking formula algorithm xylophone calibration provenance"
    assert extract_keywords(conn, text, min_idf=0.0) == _old_extract_keywords(
        conn, text, min_idf=0.0
    )


# ---------------------------------------------------------------------------
# SQL semantics: IN (json_each) on fts5vocab == `term = ?` per term
# ---------------------------------------------------------------------------

def test_batch_df_equals_per_term_df_for_hostile_terms(small_db):
    terms = [
        "xylophone", "ranking", "formula", "XYLOPHONE", "Formula",
        "café", "cafe", "résumé", "resume", "über", "uber", "naïve",
        "mixed", "case", "token",
        "it's", 'say"hi', "50%", "a_b", "%", "_", "form%", "form_la", "formul_",
        "", " ", "ranking formula", "中文", "\U0001f600", "o'brien",
        "absent_term_zzz", "calibration", "provenance",
    ]
    per_term = {}
    for t in terms:
        r = small_db.execute(PER_TOKEN_SQL, (t,)).fetchone()
        if r is not None:
            per_term[t] = r[0]
    batch = {
        row[0]: row[1]
        for row in small_db.execute(BATCH_SQL, (json.dumps(terms),)).fetchall()
    }
    assert batch == per_term
    # sanity: the probe set really contains both present and absent terms
    assert "xylophone" in batch and "absent_term_zzz" not in batch
    assert "XYLOPHONE" not in batch  # case-sensitive, like `term = ?`


def test_batch_with_empty_list_returns_nothing_semantically(small_db):
    assert small_db.execute(BATCH_SQL, (json.dumps([]),)).fetchall() == []


def test_unknown_term_scores_like_unknown(small_db):
    assert _same(small_db, "absentwordqq anotherabsentzz", min_idf=0.0) == []


class _ZeroDfConn:
    """A stub connection whose vocabulary query returns ``(term, 0)`` for a term."""

    def execute(self, sql, params=()):
        if sql == BATCH_SQL:
            return _StubCursor([("zeroterm", 0)])
        if "COUNT(*)" in sql:
            return _StubCursor([(10,)])
        raise AssertionError(f"unexpected statement: {sql!r}")


def test_df_zero_is_unknown_not_a_division_by_zero():
    """The ``df == 0`` branch of ``extract_keywords`` (stage 2a gap): a term the vocabulary
    reports with a document frequency of 0 is treated as unknown -- no ZeroDivisionError, and
    the term is absent from the result (min_idf is very negative so only this rule can exclude
    it). Mutation: ``df <= 0`` -> ``df < 0`` in engram_idf.py lets 0 through to
    ``math.log(n_docs / df)`` -> ZeroDivisionError -> red."""
    assert extract_keywords(_ZeroDfConn(), "zeroterm", min_idf=-100.0) == []
