"""Contract cells for ``extract_keywords(..., n_docs=)`` (#3740 Cut B).

Contract:
  * ``n_docs`` is the LAST parameter, optional, keyword-friendly.
  * ``n_docs is None`` -> behaviour byte-identical to before: ONE
    ``SELECT COUNT(*) FROM nodes WHERE is_current = 1`` is executed.
  * an int >= 0 -> NO COUNT statement is executed by extract_keywords; 0 -> [];
    scores use the given value (log(n_docs / df)).
  * non-int, bool or negative -> ValueError (a caller bug is loud, never recounted).
"""

from __future__ import annotations

import inspect
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_idf import extract_keywords  # noqa: E402
from test_idf_batch_3665 import _SMALL_CORPUS, Recorder, _make_db  # noqa: E402

COUNT_SQL = "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
TEXT = "ranking formula algorithm xylophone calibration provenance"


def _count_calls(rec):
    return [c for c in rec.calls if "COUNT(" in c[0].upper()]


def test_a_given_n_docs_executes_no_count_and_equals_the_recounted_result():
    """Mutation: extract_keywords ignores n_docs and recounts -> a COUNT is recorded -> red."""
    conn = _make_db(_SMALL_CORPUS)
    true_n = conn.execute(COUNT_SQL).fetchone()[0]
    expected = extract_keywords(conn, TEXT, min_idf=0.0)
    assert expected, "vacuous: the oracle call returned nothing"
    rec = Recorder(conn)
    got = extract_keywords(rec, TEXT, min_idf=0.0, n_docs=true_n)
    assert _count_calls(rec) == []
    assert got == expected


def test_b_n_docs_zero_returns_empty_and_does_not_recount():
    """Mutation: `if not n_docs` -> 0 is recounted (COUNT recorded / non-empty) -> red."""
    conn = _make_db(_SMALL_CORPUS)
    rec = Recorder(conn)
    assert extract_keywords(rec, TEXT, min_idf=0.0, n_docs=0) == []
    assert _count_calls(rec) == []


def test_c_a_different_n_docs_shifts_every_score_by_log_ratio():
    """Proves the parameter is used. Mutation: ignore n_docs -> scores equal -> red."""
    conn = _make_db(_SMALL_CORPUS)
    n = conn.execute(COUNT_SQL).fetchone()[0]
    base = extract_keywords(conn, TEXT, min_idf=0.0, n_docs=n)
    dbl = extract_keywords(conn, TEXT, min_idf=0.0, n_docs=2 * n)
    assert base and [t for t, _ in base] == [t for t, _ in dbl]
    for (_, s1), (_, s2) in zip(base, dbl):
        assert s2 - s1 == pytest.approx(math.log(2), abs=1e-9)


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "10"])
def test_d_bad_n_docs_raises_valueerror(bad):
    """Mutation: skip the type/negative validation -> no ValueError -> red."""
    conn = _make_db(_SMALL_CORPUS)
    with pytest.raises(ValueError):
        extract_keywords(conn, TEXT, min_idf=0.0, n_docs=bad)


def test_e_none_explicit_equals_omitted_and_counts_exactly_once():
    conn = _make_db(_SMALL_CORPUS)
    r1, r2 = Recorder(conn), Recorder(conn)
    a = extract_keywords(r1, TEXT, min_idf=0.0)
    b = extract_keywords(r2, TEXT, min_idf=0.0, n_docs=None)
    assert a == b and a
    assert [c[0] for c in _count_calls(r1)] == [COUNT_SQL]
    assert [c[0] for c in _count_calls(r2)] == [COUNT_SQL]


def test_signature_n_docs_is_last_keyword_default_none():
    params = list(inspect.signature(extract_keywords).parameters.values())
    assert params[-1].name == "n_docs" and params[-1].default is None
    assert [p.name for p in params[:5]] == ["conn", "text", "min_idf", "top_k", "stopwords"]
