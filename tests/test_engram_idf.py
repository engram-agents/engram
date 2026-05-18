"""Tests for engram_idf — IDF-based keyword extraction module.

All tests are self-contained: they build minimal in-memory SQLite schemas
with a nodes table + FTS index rather than touching production knowledge.db.

Test coverage:
  1. test_tokenize_basic               — lowercase, punctuation strip, underscore-preserve
  2. test_stopwords_layered            — STANDARD_STOPWORDS / DISCOURSE_MARKERS / STOPWORDS
  3. test_ensure_vocab_table_idempotent — create twice, no error
  4. test_idf_missing_token            — absent token returns None
  5. test_idf_present_token            — present token returns small positive float
  6. test_extract_keywords_filters_stopwords — discourse + standard stopwords filtered out
  7. test_extract_keywords_respects_threshold — min_idf gate
  8. test_extract_keywords_top_k       — top_k truncation
  9. test_extract_keywords_ignores_unknown_tokens — absent tokens silently excluded
"""

from __future__ import annotations

import math
import sqlite3
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_idf import (  # noqa: E402
    STANDARD_STOPWORDS,
    DISCOURSE_MARKERS,
    STOPWORDS,
    ensure_vocab_table,
    extract_keywords,
    idf,
    tokenize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_db(docs: list[str]) -> sqlite3.Connection:
    """Build an in-memory DB with a nodes table + FTS index + fts5vocab.

    *docs* is a list of claim strings.  Each becomes one current node in the
    nodes table with that string as its `claim`.  quoted_text and
    interpretation are left NULL (NULL is fine for fts5 content tables).
    """
    conn = sqlite3.connect(":memory:")

    # Minimal nodes table — only the columns idf() and extract_keywords() need.
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

    # FTS5 index over nodes.
    conn.execute("""
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        )
    """)

    # Populate.
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


# A varied synthetic corpus of 60 nodes — enough for meaningful IDF variance.
_CORPUS = [
    # Many occurrences of "formula" across documents (low IDF in small corpus)
    "the recall ranking formula is used to score nodes",
    "the ranking formula determines output order",
    "formula evaluation runs at surface time",
    "ranking formula is described in the design doc",
    "formula weights are tunable parameters",
    # "algorithm" in fewer documents (higher IDF)
    "the algorithm operates on the embedding space",
    "algorithm convergence is checked at each step",
    # Highly unique terms: "xylophone" appears only once (very high IDF)
    "xylophone appears in exactly one document",
    # Padding with varied vocabulary
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
    "the session log records each tool invocation timestamp",
    "hook scripts intercept claude code lifecycle events",
    "mcp server exposes tools as json rpc endpoints",
    "the skill file documents node types and workflows",
    "a derivation cites at least one premise node",
    "contradiction detection uses polarity classification",
    "the quiz tool tests self-knowledge of the graph",
    "inductive enumeration scans for uncaptured patterns",
    "the drowsiness meter uses token count estimates",
    "edit history tracks every mutation in append-only log",
    "diagnostic history captures metric snapshots per turn",
    "sequence tables assign monotonically increasing ids",
    "the confidence cap prevents overclaiming on weak evidence",
    "similarity hints surface related nodes as advisory",
    "blocking guards protect provenance integrity loudly",
    "advisory checks inform quality judgment without blocking",
    "the fts5vocab table tracks term document frequency counts",
    "per-session markers record cwd and model id",
    "the write-yield hook fires after each assistant turn",
    "end-of-day hook consolidates the day log entries",
    "the viz server exposes a health dashboard on port 5001",
    "log indexer writes structured events to the event table",
    "log emitter publishes jsonl lines for each tool call",
    "the auto-surface prepending refinement is alpha 177",
]


@pytest.fixture
def corpus_conn() -> sqlite3.Connection:
    """In-memory DB populated with _CORPUS (60 nodes)."""
    return _make_minimal_db(_CORPUS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_tokenize_basic(self):
        """Spec test: apostrophe-s drops; main tokens survive."""
        result = tokenize("Hello, EEC on C1's 'bound'.")
        assert result == ["hello", "eec", "on", "bound"], (
            f"Expected ['hello', 'eec', 'on', 'bound'] but got {result}"
        )

    def test_tokenize_node_ids_survive(self):
        """Spec test: node IDs with underscores stay intact."""
        result = tokenize("ax_0006 supersedes ax_0001")
        assert result == ["ax_0006", "supersedes", "ax_0001"], (
            f"Expected ['ax_0006', 'supersedes', 'ax_0001'] but got {result}"
        )

    def test_tokenize_lowercases(self):
        """Input is folded to lowercase before matching."""
        assert tokenize("HELLO WORLD") == ["hello", "world"]

    def test_tokenize_strips_punctuation(self):
        """Punctuation acts as token delimiters."""
        assert "." not in " ".join(tokenize("end. of. sentence."))

    def test_tokenize_pure_numbers_excluded(self):
        """Pure digit strings do not start with a letter — excluded."""
        result = tokenize("123 456 789")
        assert result == []

    def test_tokenize_preserves_order(self):
        """Token order mirrors input text order."""
        result = tokenize("alpha bravo charlie")
        assert result == ["alpha", "bravo", "charlie"]


class TestStopwordsLayered:
    def test_standard_stopword_present(self):
        """'the' is in STANDARD_STOPWORDS (sklearn layer)."""
        assert "the" in STANDARD_STOPWORDS

    def test_discourse_marker_present(self):
        """'great' is in DISCOURSE_MARKERS."""
        assert "great" in DISCOURSE_MARKERS

    def test_formula_not_in_either(self):
        """'formula' is a genuine content word — not a stopword."""
        assert "formula" not in STANDARD_STOPWORDS
        assert "formula" not in DISCOURSE_MARKERS

    def test_stopwords_union(self):
        """STOPWORDS is the union of both layers."""
        assert "the" in STOPWORDS
        assert "great" in STOPWORDS
        assert "formula" not in STOPWORDS

    def test_standard_stopwords_count(self):
        """Vendored list should have exactly 318 words (sklearn 1.4+)."""
        assert len(STANDARD_STOPWORDS) == 318

    def test_stopwords_superset(self):
        """STOPWORDS is a strict superset of STANDARD_STOPWORDS."""
        assert STANDARD_STOPWORDS < STOPWORDS


class TestEnsureVocabTable:
    def test_idempotent(self):
        """Calling ensure_vocab_table twice must not raise."""
        conn = _make_minimal_db(["dummy document for vocab test"])
        # First call is already done by _make_minimal_db; call again.
        ensure_vocab_table(conn)  # should not raise

    def test_table_queryable(self):
        """After creation, nodes_fts_vocab is queryable."""
        conn = _make_minimal_db(["hello world document"])
        rows = conn.execute("SELECT term, doc FROM nodes_fts_vocab LIMIT 5").fetchall()
        assert isinstance(rows, list)

    def test_raises_on_missing_fts_table(self):
        """Raises OperationalError when nodes_fts doesn't exist.

        SQLite defers fts5vocab resolution to query time rather than DDL time,
        so ensure_vocab_table probes the table immediately after creation to
        surface the error eagerly as documented.
        """
        conn = sqlite3.connect(":memory:")
        with pytest.raises(sqlite3.OperationalError):
            ensure_vocab_table(conn)  # nodes_fts not present → should raise


class TestIdf:
    def test_idf_missing_token_returns_none(self, corpus_conn):
        """A token not in the index returns None."""
        result = idf(corpus_conn, "zzzzznonexistent")
        assert result is None

    def test_idf_present_token(self, corpus_conn):
        """A frequent token (e.g. 'formula') returns a small positive float.

        'formula' appears in 5 of 60 documents → IDF = log(60/5) = ~2.48.
        The exact threshold is < 5 (well below rare-word territory).
        """
        score = idf(corpus_conn, "formula")
        assert score is not None
        assert score > 0
        # 'formula' appears in multiple docs → IDF should be moderate, not huge
        assert score < 5.0

    def test_idf_rare_token_is_high(self, corpus_conn):
        """A token appearing in only 1 doc has higher IDF than a frequent one."""
        score_formula = idf(corpus_conn, "formula")
        score_xylophone = idf(corpus_conn, "xylophone")
        assert score_formula is not None
        assert score_xylophone is not None
        assert score_xylophone > score_formula

    def test_idf_n_docs_parameter(self, corpus_conn):
        """Passing n_docs explicitly gives same result as auto-querying it."""
        n = corpus_conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
        ).fetchone()[0]
        score_auto = idf(corpus_conn, "formula")
        score_explicit = idf(corpus_conn, "formula", n_docs=n)
        assert score_auto == score_explicit

    def test_idf_empty_corpus(self):
        """With zero current nodes, idf returns None for any token."""
        conn = _make_minimal_db([])
        ensure_vocab_table(conn)
        result = idf(conn, "anything")
        assert result is None

    def test_idf_token_only_in_superseded_returns_none(self):
        """Tokens appearing only in superseded nodes return None — not a
        negative IDF.

        Regression test for the fts5 indexed-but-superseded gap: nodes_fts
        has no UPDATE trigger on is_current, so superseded rows stay in the
        FTS index permanently. Without flooring, df > n_docs yields negative
        log values; we floor by returning None so callers using min_idf=0.0
        still see a coherent contract.
        """
        # Build inline because _make_minimal_db doesn't support per-doc
        # is_current overrides. Mixed corpus: 1 current doc without the
        # token + 2 superseded docs containing it. n_docs=1, df=2 →
        # log(0.5) < 0 → must return None.
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE nodes (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL, claim TEXT,
                quoted_text TEXT, interpretation TEXT,
                is_current INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                claim, quoted_text, interpretation,
                content='nodes', content_rowid='rowid'
            )
        """)
        for i, (claim, is_current) in enumerate([
            ("common everyday words only", 1),
            ("rarewordzzz appears here", 0),
            ("rarewordzzz again", 0),
        ]):
            conn.execute(
                "INSERT INTO nodes (id, claim, is_current) VALUES (?, ?, ?)",
                (f"ob_{i:04d}", claim, is_current),
            )
            conn.execute(
                "INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation) "
                "VALUES (last_insert_rowid(), ?, NULL, NULL)",
                (claim,),
            )
        ensure_vocab_table(conn)
        conn.commit()

        # Pre-condition: df=2, n_docs=1 → naive log would yield log(0.5) ≈ -0.69
        df = conn.execute(
            "SELECT doc FROM nodes_fts_vocab WHERE term = 'rarewordzzz'"
        ).fetchone()[0]
        n_docs = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
        ).fetchone()[0]
        assert df == 2 and n_docs == 1, f"setup invariant violated: df={df}, n={n_docs}"

        # The fix: return None instead of leaking negative.
        assert idf(conn, "rarewordzzz") is None


class TestExtractKeywords:
    def test_filters_stopwords(self, corpus_conn):
        """Discourse markers and standard stopwords are removed before scoring.

        Input: "great, the formula is wonderful"
        - 'great' ∈ DISCOURSE_MARKERS → filtered
        - 'the' ∈ STANDARD_STOPWORDS → filtered
        - 'formula' → not a stopword; should be the only candidate
        - 'is' ∈ STANDARD_STOPWORDS → filtered
        - 'wonderful' ∈ DISCOURSE_MARKERS → filtered
        """
        results = extract_keywords(
            corpus_conn,
            "great, the formula is wonderful",
            min_idf=0.0,  # accept any non-stopword with IDF > 0
        )
        tokens = [t for t, _ in results]
        assert "formula" in tokens, f"Expected 'formula' in results; got {results}"
        # Stopwords must be absent
        for sw in ("great", "the", "is", "wonderful"):
            assert sw not in tokens, f"Stopword '{sw}' should have been filtered; got {results}"

    def test_respects_min_idf_threshold(self, corpus_conn):
        """With min_idf=10, even rare tokens are excluded (corpus is small, max IDF < 5)."""
        results = extract_keywords(
            corpus_conn,
            "great, the formula is wonderful",
            min_idf=10.0,
        )
        assert results == [], f"Expected empty list at min_idf=10; got {results}"

    def test_top_k_truncation(self, corpus_conn):
        """At most top_k tokens are returned."""
        # Use a text with many distinct content words
        text = (
            "algorithm embedding confidence evidence axiom derivation "
            "retraction semantic similarity convergence xylophone ranking"
        )
        results = extract_keywords(corpus_conn, text, min_idf=0.0, top_k=3)
        assert len(results) <= 3

    def test_top_k_sorted_by_idf_descending(self, corpus_conn):
        """Results are sorted by IDF score, highest first."""
        text = (
            "algorithm xylophone formula confidence evidence axiom "
            "derivation retraction semantic"
        )
        results = extract_keywords(corpus_conn, text, min_idf=0.0, top_k=20)
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True), (
            f"Expected descending IDF scores; got {scores}"
        )

    def test_ignores_unknown_tokens(self, corpus_conn):
        """Tokens absent from the corpus are silently excluded, not raised."""
        results = extract_keywords(
            corpus_conn,
            "completelymadeupword1 anotherfakeword2 formula",
            min_idf=0.0,
        )
        tokens = [t for t, _ in results]
        # Unknown tokens should not appear; formula may or may not appear
        assert "completelymadeupword1" not in tokens
        assert "anotherfakeword2" not in tokens
        # No exception should have been raised (test reaching here = pass)

    def test_custom_stopwords(self, corpus_conn):
        """Passing a custom stopwords set overrides the default STOPWORDS."""
        # With an empty stopwords set, "formula" should pass through
        results = extract_keywords(
            corpus_conn,
            "formula",
            min_idf=0.0,
            stopwords=frozenset(),
        )
        tokens = [t for t, _ in results]
        assert "formula" in tokens

    def test_deduplicates_tokens(self, corpus_conn):
        """Each token appears at most once even if repeated in text."""
        results = extract_keywords(
            corpus_conn,
            "formula formula formula formula formula",
            min_idf=0.0,
        )
        tokens = [t for t, _ in results]
        assert len(tokens) == len(set(tokens)), "Duplicate tokens in results"

    def test_empty_corpus_returns_empty(self):
        """With no nodes in the corpus, extract_keywords returns []."""
        conn = _make_minimal_db([])
        ensure_vocab_table(conn)
        results = extract_keywords(conn, "formula ranking algorithm", min_idf=0.0)
        assert results == []
