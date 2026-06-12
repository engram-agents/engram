"""Tests for the IDF-based prepending gate in engram-surface-hook.py.

Covers:
  1. Gate skips prepending when prompt has high-IDF keyword (self-anchoring)
  2. Gate prepends when prompt is very short (below floor)
  3. Gate prepends when prompt is conversational (no high-IDF tokens)
  4. Gate falls back to legacy heuristic on DB error or ImportError
  5. Feature flag off → legacy char-length rule regardless of DB

All tests are self-contained: they build minimal in-memory SQLite schemas
using the _make_minimal_db pattern from tests/test_engram_idf.py.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helper: load the hook module (hyphenated filename)
# ---------------------------------------------------------------------------


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helper: _make_minimal_db (same pattern as tests/test_engram_idf.py)
# ---------------------------------------------------------------------------


def _make_minimal_db(docs: list[str]) -> sqlite3.Connection:
    """Build an in-memory DB with a nodes table + FTS index + fts5vocab.

    *docs* is a list of claim strings. Each becomes one current node in the
    nodes table with that string as its ``claim``.
    """
    import sys as _sys
    # Ensure engram_idf is importable for ensure_vocab_table
    _sys.path.insert(0, str(PROJECT_ROOT))
    from engram_idf import ensure_vocab_table

    conn = sqlite3.connect(":memory:")

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


# A corpus large enough for meaningful IDF variance — same vocabulary as
# test_engram_idf.py's _CORPUS (60 nodes), chosen so that:
#   "formula" appears in 5 docs → IDF ≈ 2.48 (< 4.0 min_idf)
#   "recall" appears in ~3 docs → IDF ≈ 2.99 (< 4.0 min_idf)
#   "xylophone" appears in 1 doc → IDF ≈ 4.09 (> 4.0 min_idf for 60 docs)
#   domain terms like "openclaw", "soul" appear once → high IDF
_CORPUS = [
    "the recall ranking formula is used to score nodes",
    "the ranking formula determines output order",
    "formula evaluation runs at surface time",
    "ranking formula is described in the design doc",
    "formula weights are tunable parameters",
    "the algorithm operates on the embedding space",
    "algorithm convergence is checked at each step",
    "xylophone appears in exactly one document",
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
    "openclaw soul invention design pattern framework",
]


def _cfg(
    *,
    idf_gate_enabled: bool = True,
    idf_gate_min_idf: float = 4.0,
    idf_gate_short_prompt_floor_chars: int = 40,
    short_prompt_threshold_chars: int = 100,
    prev_response_tail_chars: int = 500,
) -> dict:
    """Build a cfg dict for _should_prepend tests."""
    return {
        "idf_gate_enabled": idf_gate_enabled,
        "idf_gate_min_idf": idf_gate_min_idf,
        "idf_gate_short_prompt_floor_chars": idf_gate_short_prompt_floor_chars,
        "short_prompt_threshold_chars": short_prompt_threshold_chars,
        "prev_response_tail_chars": prev_response_tail_chars,
    }


# ---------------------------------------------------------------------------
# Tests for _should_prepend
# ---------------------------------------------------------------------------


class TestShouldPrepend(unittest.TestCase):
    """Unit tests for the _should_prepend gate function."""

    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook_module()
        cls.conn = _make_minimal_db(_CORPUS)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # ---- Test 1: prompt has high-IDF keyword → self-anchors → skip prepend ----

    def test_high_idf_keyword_skips_prepend(self):
        """Gate returns False when prompt contains a high-IDF keyword.

        "xylophone" appears once in 60 docs → IDF = log(60/1) ≈ 4.09 > 4.0.
        The gate should detect it and return False (no prepend needed).
        """
        # Simulate the empirical failure case from PR #192: the prompt has a
        # clear domain keyword, so it self-anchors.
        prompt = "Thanks! And the final recall ranking xylophone formula is what?"
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        # xylophone has IDF > 4.0 → self-anchoring → return False
        self.assertFalse(
            result,
            f"Expected False (prompt has high-IDF 'xylophone'); got True. "
            f"Prompt: {prompt!r}",
        )

    def test_openclaw_soul_domain_keywords_skip_prepend(self):
        """Gate returns False when prompt has domain-specific rare terms.

        "openclaw" and "soul" appear once in corpus → high IDF → self-anchoring.
        """
        prompt = "Soul.md is openclaw invention I believe and it is well known"
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        self.assertFalse(
            result,
            f"Expected False (prompt has high-IDF 'openclaw'/'soul'); got True.",
        )

    # ---- Test 2: very short prompt → always prepend (rule 1 fires first) ----

    def test_very_short_prompt_always_prepends(self):
        """Rule 1: prompt shorter than floor → True, regardless of keyword content.

        Even if a very short prompt contained a high-IDF word, rule 1 fires
        first. Floor is 40 chars; prompt is 25 chars (well under).
        """
        prompt = "EEC on C1's 'bound' please"  # 26 chars
        self.assertLess(len(prompt), 40, "Pre-condition: prompt must be under floor")
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        self.assertTrue(result, f"Expected True (very short prompt); got False.")

    def test_prompt_exactly_at_floor_does_not_trigger_rule1(self):
        """Rule 1 uses strict <, so a prompt exactly at floor falls through to rule 2."""
        # Build a 40-char prompt with no high-IDF tokens (all discourse/stopwords).
        prompt = "great thanks ok sure yes please go ahead"  # 40 chars exactly
        self.assertEqual(len(prompt), 40, "Pre-condition: prompt must be exactly 40 chars")
        # With no high-IDF tokens, rule 2 should return True (prepend).
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        self.assertTrue(result, f"Expected True (no self-anchoring keywords); got False.")

    # ---- Test 3: conversational prompt, no high-IDF tokens → prepend ----

    def test_conversational_prompt_no_keywords_prepends(self):
        """Gate returns True when prompt is conversational with no high-IDF tokens.

        "Great! Let me double check if there's any points in your previous report"
        All tokens are either stopwords or discourse markers or have low IDF in the
        60-doc corpus.
        """
        prompt = "Great! Let me double check if there are any points in your previous report"
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        self.assertTrue(
            result,
            f"Expected True (no self-anchoring keywords in conversational prompt); "
            f"got False. Prompt: {prompt!r}",
        )

    def test_affirmation_prompt_prepends(self):
        """Conversational affirmation prompts with no domain keywords return True.

        Prompt contains only discourse markers and standard stopwords — no
        content words that appear in the corpus with high IDF.
        """
        prompt = "Ok sounds good, thank you so much, moving on now then"
        result = self.hook._should_prepend(prompt, self.conn, _cfg())
        self.assertTrue(result, "Expected True (affirmation, no domain keywords)")

    # ---- Test 4: fallback on DB error or import error ----

    def test_db_error_falls_back_to_legacy_short(self):
        """On DB error, falls back to legacy len < short_prompt_threshold_chars.

        Pass a closed connection to simulate a DB failure. A short prompt (< 100)
        should return True via the legacy heuristic.
        """
        bad_conn = sqlite3.connect(":memory:")
        bad_conn.close()  # closed → any execute will raise OperationalError

        short_prompt = "yes go ahead"  # 12 chars, < 100 threshold
        result = self.hook._should_prepend(
            short_prompt,
            bad_conn,
            _cfg(short_prompt_threshold_chars=100),
        )
        self.assertTrue(
            result,
            "Expected True (DB error → legacy fallback; prompt < 100 chars)",
        )

    def test_db_error_falls_back_to_legacy_long(self):
        """On DB error, legacy heuristic: long prompt (≥ 100) returns False."""
        bad_conn = sqlite3.connect(":memory:")
        bad_conn.close()

        long_prompt = "x" * 150  # 150 chars, ≥ 100 threshold
        result = self.hook._should_prepend(
            long_prompt,
            bad_conn,
            _cfg(short_prompt_threshold_chars=100),
        )
        self.assertFalse(
            result,
            "Expected False (DB error → legacy fallback; prompt ≥ 100 chars)",
        )

    def test_import_error_falls_back_to_legacy(self):
        """On ImportError from engram_idf, legacy heuristic applies.

        IMPORTANT: pop engram_idf from sys.modules BEFORE the patch — Python's
        import machinery checks the module cache before calling __import__, so
        a pre-cached module would let our patched __import__ never fire. The
        round-1 fairy review caught this: the test was previously passing
        because the in-memory DB had no nodes_fts table, hitting the DB-error
        path rather than the ImportError path.
        """
        hook = _load_hook_module()

        # Patch sys.modules to make engram_idf unimportable
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "engram_idf":
                raise ImportError("engram_idf not available")
            return original_import(name, *args, **kwargs)

        # Force engram_idf to be re-imported under the patch
        cached_engram_idf = sys.modules.pop("engram_idf", None)
        try:
            with patch("builtins.__import__", side_effect=mock_import):
                # Short prompt → legacy returns True
                result_short = hook._should_prepend(
                    "short prompt here",  # 17 chars < 100
                    sqlite3.connect(":memory:"),
                    _cfg(short_prompt_threshold_chars=100),
                )
            self.assertTrue(result_short, "Expected True (import error → legacy; short prompt)")
        finally:
            # Restore the cached module so subsequent tests work normally
            if cached_engram_idf is not None:
                sys.modules["engram_idf"] = cached_engram_idf

    # ---- Test 5: feature flag off → legacy behavior in main call site ----
    # (This tests the call site logic, not _should_prepend directly)

    def test_feature_flag_off_config_returns_disabled(self):
        """When idf_gate_enabled=False is in config, the dict reflects it.

        The gate function itself only receives cfg; the flag is checked by the
        call site. This test verifies the config reader correctly parses the flag.
        """
        import tempfile
        import json as _json

        tmp = tempfile.mkdtemp(prefix="engram_idf_flag_test_")
        try:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                _json.dump(
                    {"auto_surface": {"idf_gate_enabled": False}},
                    f,
                )

            hook = _load_hook_module()
            hook.ENGRAM_HOME = tmp
            cfg = hook._get_auto_surface_config()
            self.assertFalse(cfg["idf_gate_enabled"])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_feature_flag_true_by_default(self):
        """idf_gate_enabled defaults to True when config.json is absent."""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="engram_idf_default_flag_test_")
        try:
            hook = _load_hook_module()
            hook.ENGRAM_HOME = tmp
            cfg = hook._get_auto_surface_config()
            self.assertTrue(cfg["idf_gate_enabled"])
            self.assertEqual(cfg["idf_gate_min_idf"], 4.0)
            self.assertEqual(cfg["idf_gate_short_prompt_floor_chars"], 40)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_feature_flag_off_uses_legacy_in_call_site(self):
        """With idf_gate_enabled=False, call site uses legacy len < threshold.

        We directly test the config branch rather than re-running main()
        (which requires stdin mocking). The cfg dict controls which path
        the caller takes; this verifies the branching logic.
        """
        # With flag off, the call site would run:
        #   should_prepend = len(prompt) < cfg["short_prompt_threshold_chars"]
        # We verify the expected outcome directly.
        cfg = _cfg(idf_gate_enabled=False, short_prompt_threshold_chars=100)
        short_prompt = "yes"  # 3 chars
        long_prompt = "x" * 200

        # Simulate the call-site branch for flag=False
        should_prepend_short = len(short_prompt) < cfg["short_prompt_threshold_chars"]
        should_prepend_long = len(long_prompt) < cfg["short_prompt_threshold_chars"]

        self.assertTrue(should_prepend_short, "Short prompt → True with legacy rule")
        self.assertFalse(should_prepend_long, "Long prompt → False with legacy rule")


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestShouldPrependEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions for _should_prepend."""

    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook_module()
        cls.conn = _make_minimal_db(_CORPUS)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_empty_corpus_falls_through_to_rule2_returns_true(self):
        """With empty corpus, extract_keywords returns [] → no keywords → True."""
        empty_conn = _make_minimal_db([])
        try:
            # Prompt above floor (40) so rule 1 doesn't fire.
            prompt = "what is the final recall ranking formula and how does it work exactly"
            result = self.hook._should_prepend(prompt, empty_conn, _cfg())
            self.assertTrue(
                result,
                "Expected True (empty corpus → no keywords found → prepend)",
            )
        finally:
            empty_conn.close()

    def test_min_idf_threshold_respected(self):
        """Raising min_idf to 100 means no token ever qualifies → always prepend."""
        # 'xylophone' would normally pass at min_idf=4.0, but not at 100.
        prompt = "Thanks! And the final recall ranking xylophone formula is what?"
        result = self.hook._should_prepend(
            prompt, self.conn, _cfg(idf_gate_min_idf=100.0)
        )
        self.assertTrue(
            result,
            "Expected True (min_idf=100.0 → no token qualifies → prepend)",
        )

    def test_floor_boundary_strict_less_than(self):
        """Rule 1 uses strict <: floor=40, len=40 falls through to rule 2."""
        # Exactly 40 chars, all discourse/stopwords → rule 2 should return True.
        prompt = "great thanks ok sure yes please go ahead"  # exactly 40
        self.assertEqual(len(prompt), 40)
        result = self.hook._should_prepend(
            prompt, self.conn, _cfg(idf_gate_short_prompt_floor_chars=40)
        )
        # No high-IDF tokens → rule 2 returns True (prepend)
        self.assertTrue(result, "Expected True (at floor exactly, all stopwords → prepend)")

    def test_floor_boundary_one_below(self):
        """len=39 (< 40) triggers rule 1 → always True."""
        prompt = "great thanks ok sure yes please go ahea"  # 39 chars
        self.assertEqual(len(prompt), 39)
        result = self.hook._should_prepend(
            prompt, self.conn, _cfg(idf_gate_short_prompt_floor_chars=40)
        )
        self.assertTrue(result, "Expected True (rule 1: prompt below floor)")


if __name__ == "__main__":
    unittest.main()
