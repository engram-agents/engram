"""Tests for the _sanitize_fts_query rewrite (alpha #177 area 1).

Five tests covering the bi-gram OR-of-AND-pairs variant + fallback paths:

  1. Legacy path with conn=None — returns AND-of-quoted-tokens (unchanged).
  2. Bi-gram OR-of-AND-pairs with ≥2 keywords (`("k1" AND "k2") OR ...`).
  2b. Single-keyword case → bare quoted term, no AND-pair structure.
  3. Fallback when extract_keywords returns empty → legacy AND form.
  4. Fallback on engram_idf ImportError → legacy AND form.

Bi-gram structure locked 2026-05-18 PM after 7-variant harness comparison;
+0.061 MRR vs baseline. See PR #197 description for full empirical data.
"""

from __future__ import annotations

import sqlite3
import sys
import os
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_idf import ensure_vocab_table  # noqa: E402
from server import _sanitize_fts_query, _sanitize_fts_query_legacy  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: minimal in-memory DB with FTS and vocab
# ---------------------------------------------------------------------------

def _make_minimal_db(docs: list[str]) -> sqlite3.Connection:
    """Build an in-memory DB with nodes table + FTS index + fts5vocab.

    Mirrors the pattern from test_engram_idf.py so tests are self-contained.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE nodes (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            id          TEXT NOT NULL,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            is_current  INTEGER DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        )
    """)

    for i, claim in enumerate(docs):
        node_id = f"ob_{i:04d}"
        conn.execute(
            "INSERT INTO nodes (id, claim, is_current) VALUES (?, ?, 1)",
            (node_id, claim),
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation) "
            "VALUES (last_insert_rowid(), ?, NULL, NULL)",
            (claim,),
        )

    ensure_vocab_table(conn)
    conn.commit()
    return conn


# A small corpus where "formula" and "ranking" appear rarely (high IDF),
# while stopwords and common function words are plentiful.
_CORPUS = [
    "the recall ranking formula is used to score nodes",
    "confidence scores capture uncertainty in each claim",
    "evidence nodes anchor factual observations to sources",
    "axiom nodes encode foundational principles",
    "derivation nodes record logical inferences",
    "the graph maintains provenance for every edge",
    "retraction cascades fire when a node is superseded",
    "semantic similarity is computed using cosine distance",
    "embedding vectors are generated at insertion time",
    "the surface budget limits how many nodes are returned",
    "focus mode pins high-priority nodes into context",
    "lesson nodes encode experience-based reminders",
    "cornerstone nodes define long-term identity claims",
    "goals drive the direction of knowledge accumulation",
    "the honesty axiom requires provenance for every claim",
    "trust pool thresholds gate citation validity",
    "the drowsiness meter tracks context window utilization",
    "warm briefing summarizes relational context on wakeup",
    "compaction preserves focused nodes across resets",
    "auto-surface fires on each session prompt automatically",
    "structured memory differs from narrative identity",
    "epistemic identity requires provenance and retraction",
    "the fts index is rebuilt from the content table",
    "virtual table operations require sqlite fts5 extension",
    "idf scores reflect inverse document frequency",
    "stopwords filter common english tokens before scoring",
    "discourse markers capture conversational filler words",
    "keyword extraction returns the highest idf tokens",
    "the tokenizer preserves underscore separated node ids",
    "recall measurement benchmarks surface quality over time",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSanitizeFtsQuery:
    def test_legacy_path_with_no_conn(self):
        """Spec test 1: conn=None falls through to legacy AND-of-quoted-tokens."""
        result = _sanitize_fts_query("the formula is great", conn=None)
        # Legacy behavior: every whitespace token quoted and AND-joined.
        assert result is not None
        # Each token should appear quoted in the output.
        assert '"the"' in result
        assert '"formula"' in result
        assert '"is"' in result
        assert '"great"' in result
        # Should be space-joined (AND semantics), not " OR "-joined.
        assert " OR " not in result

    def test_bigram_pairs_with_high_idf_prompt(self):
        """Spec test 2 (bi-gram variant): conn present + extract_keywords
        returns 2+ keywords → produces OR-of-AND-pairs structure.

        Bi-gram variant locked in 2026-05-18 PM after 7-variant harness
        comparison; +0.061 MRR vs baseline. Format is
        `("k1" AND "k2") OR ("k1" AND "k3") OR ...`.

        Patches extract_keywords to isolate the bi-gram structure logic from
        corpus IDF calibration (the 30-doc test fixture can't support
        production's min_idf=4.0; max achievable idf is log(30)=3.4).
        """
        conn = _make_minimal_db(_CORPUS)
        # Patch extract_keywords inside the server module to return known
        # keywords so the test exercises the bi-gram path deterministically.
        with patch("engram_idf.extract_keywords") as mock_extract:
            mock_extract.return_value = [
                ("formula", 4.52),
                ("ranking", 4.78),
                ("final", 4.26),
            ]
            result = _sanitize_fts_query("the formula and ranking are correct", conn=conn)
        assert result is not None, f"Expected non-None FTS query, got {result!r}"
        # Bi-gram structure: 3 keywords → C(3,2) = 3 pairs OR-joined
        # Expected shape: '("formula" AND "ranking") OR ("formula" AND "final") OR ("ranking" AND "final")'
        assert " OR " in result, f"Expected OR-joined pairs, got: {result!r}"
        assert " AND " in result, f"Expected AND-pairs, got: {result!r}"
        # Each pair should be paren-wrapped.
        import re as _re
        pair_matches = _re.findall(r"\(\"[^\"]+\"\s+AND\s+\"[^\"]+\"\)", result)
        assert len(pair_matches) == 3, (
            f"Expected exactly 3 AND-pairs from 3 keywords, got {len(pair_matches)}: {result!r}"
        )
        # All 3 keywords should appear at least once.
        tokens_in_result = _re.findall(r'"([^"]+)"', result)
        for kw in ("formula", "ranking", "final"):
            assert kw in tokens_in_result, f"Expected {kw!r} in result: {result!r}"

    def test_bigram_single_keyword_returns_bare_term(self):
        """Spec test 2b: when only one keyword survives IDF filter, the
        bi-gram path returns a single quoted term (no AND-pair structure)."""
        conn = _make_minimal_db(_CORPUS)
        with patch("engram_idf.extract_keywords") as mock_extract:
            mock_extract.return_value = [("formula", 4.52)]
            result = _sanitize_fts_query("some prompt with one keyword", conn=conn)
        assert result == '"formula"', f"Expected bare quoted term, got: {result!r}"

    def test_fallback_when_no_high_idf_tokens(self):
        """Spec test 3: if extract_keywords returns empty, fallback to legacy."""
        # The passed text is "great beautiful ok thanks please" — every token
        # is in DISCOURSE_MARKERS or STANDARD_STOPWORDS, so they all get
        # filtered at the stopword stage BEFORE any IDF lookup happens.
        # extract_keywords returns [] for the stopword reason; the IDF check
        # is never reached for any token in this query. (Defense-in-depth
        # note: the 30-doc test corpus also caps achievable IDF at
        # log(30) ≈ 3.40 < min_idf=4.0, so even non-stopword tokens couldn't
        # clear the threshold here — but that's a backstop, not what fires.)
        conn = _make_minimal_db(_CORPUS)
        result = _sanitize_fts_query("great beautiful ok thanks please", conn=conn)
        # Legacy fallback: returns AND-of-quoted-tokens (all tokens quoted, space-joined).
        assert result is not None
        assert " OR " not in result, (
            f"Expected legacy AND fallback, not OR-match; got: {result!r}"
        )
        # The tokens should still be quoted individually (legacy behavior).
        assert '"great"' in result

    def test_fallback_on_import_error(self):
        """Spec test 4: if engram_idf raises (import error or runtime error), fallback to legacy.

        The function's except-Exception catch covers both ImportError and any
        runtime failure from extract_keywords. We simulate the failure by patching
        extract_keywords to raise ImportError — the except block must return the
        legacy form.
        """
        conn = _make_minimal_db(_CORPUS)
        # Patch engram_idf.extract_keywords to raise ImportError.
        # The except Exception block in _sanitize_fts_query catches this and
        # falls back to _sanitize_fts_query_legacy.
        with patch("engram_idf.extract_keywords", side_effect=ImportError("mocked import failure")):
            result = _sanitize_fts_query("the formula and ranking", conn=conn)

        # Should have fallen back to legacy form (no OR, tokens quoted).
        assert result is not None
        assert " OR " not in result, (
            f"Expected legacy fallback on ImportError, got: {result!r}"
        )
