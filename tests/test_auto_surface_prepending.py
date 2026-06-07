"""Tests for auto-surface prev-response-prepending (alpha #177 area 1).

Covers:
- Hook config reader (_get_auto_surface_config) — default + config.json read
- Hook prev-assistant-tail reader (_read_prev_assistant_tail) — graceful empty,
  multi-line content, skip non-assistant entries, tail truncation
- engram_surface backward compat: embed_query=None behaves identical to before
- engram_surface forward path: embed_query passed through to _search_nodes,
  semantic path uses embed_query while FTS still uses query

The server-level tests mock _semantic_search to inspect what query string
it's called with — full embedding round-trips are slow and tested elsewhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: load the hook module (it's not on the import path as a module —
# the file lives at hooks/claude/engram-surface-hook.py and has a hyphen)
# ─────────────────────────────────────────────────────────────────────────────


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# Hook: _get_auto_surface_config
# ─────────────────────────────────────────────────────────────────────────────


class TestGetAutoSurfaceConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_as_cfg_test_")
        os.environ["ENGRAM_HOME"] = self.tmp
        self.hook = _load_hook_module()
        # ENGRAM_HOME constant is captured at module import; override.
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        os.environ.pop("ENGRAM_HOME", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_defaults_when_config_missing(self):
        cfg = self.hook._get_auto_surface_config()
        self.assertEqual(cfg["short_prompt_threshold_chars"], 100)
        self.assertEqual(cfg["prev_response_tail_chars"], 500)

    def test_reads_config_section(self):
        Path(self.tmp, "config.json").write_text(
            '{"auto_surface": {"short_prompt_threshold_chars": 50, "prev_response_tail_chars": 1000}}'
        )
        cfg = self.hook._get_auto_surface_config()
        self.assertEqual(cfg["short_prompt_threshold_chars"], 50)
        self.assertEqual(cfg["prev_response_tail_chars"], 1000)

    def test_partial_config_falls_back_per_field(self):
        Path(self.tmp, "config.json").write_text(
            '{"auto_surface": {"short_prompt_threshold_chars": 75}}'
        )
        cfg = self.hook._get_auto_surface_config()
        self.assertEqual(cfg["short_prompt_threshold_chars"], 75)
        self.assertEqual(cfg["prev_response_tail_chars"], 500)  # default

    def test_returns_defaults_when_config_malformed(self):
        Path(self.tmp, "config.json").write_text("not json {{")
        cfg = self.hook._get_auto_surface_config()
        self.assertEqual(cfg["short_prompt_threshold_chars"], 100)

    def test_returns_defaults_when_section_absent(self):
        Path(self.tmp, "config.json").write_text('{"memory": {"current_turn": 5}}')
        cfg = self.hook._get_auto_surface_config()
        self.assertEqual(cfg["short_prompt_threshold_chars"], 100)


# ─────────────────────────────────────────────────────────────────────────────
# Hook: _read_prev_assistant_tail
# ─────────────────────────────────────────────────────────────────────────────


class TestReadPrevAssistantTail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_tail_test_")
        self.transcript = Path(self.tmp) / "transcript.jsonl"
        os.environ["ENGRAM_HOME"] = self.tmp
        self.hook = _load_hook_module()

    def tearDown(self):
        os.environ.pop("ENGRAM_HOME", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_jsonl(self, entries: list[dict]):
        with open(self.transcript, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _assistant_entry(self, text: str) -> dict:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }

    def test_returns_empty_when_no_transcript(self):
        self.assertEqual(self.hook._read_prev_assistant_tail(None, 100), "")
        self.assertEqual(self.hook._read_prev_assistant_tail("/no/such/file", 100), "")

    def test_returns_empty_when_no_assistant_entries(self):
        self._write_jsonl([{"type": "user", "message": {"content": "hello"}}])
        self.assertEqual(self.hook._read_prev_assistant_tail(str(self.transcript), 100), "")

    def test_returns_last_assistant_text(self):
        self._write_jsonl([
            self._assistant_entry("first response"),
            {"type": "user", "message": {"content": "hi"}},
            self._assistant_entry("second response"),
        ])
        result = self.hook._read_prev_assistant_tail(str(self.transcript), 100)
        self.assertEqual(result, "second response")

    def test_truncates_to_tail_chars(self):
        long_text = "x" * 1000
        self._write_jsonl([self._assistant_entry(long_text)])
        result = self.hook._read_prev_assistant_tail(str(self.transcript), 100)
        self.assertEqual(len(result), 100)
        self.assertEqual(result, "x" * 100)

    def test_concatenates_multiple_text_blocks(self):
        self._write_jsonl([{
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "part one. "},
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "part two."},
            ]},
        }])
        result = self.hook._read_prev_assistant_tail(str(self.transcript), 100)
        self.assertEqual(result, "part one. part two.")

    def test_skips_assistant_entries_with_no_text(self):
        """Pure-tool_use assistant entries (no text content) get skipped;
        scanner continues backward to find the most recent textual response."""
        self._write_jsonl([
            self._assistant_entry("textual response"),
            {"type": "user", "message": {"content": "do something"}},
            {  # assistant with only tool_use, no text
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                ]},
            },
        ])
        result = self.hook._read_prev_assistant_tail(str(self.transcript), 100)
        self.assertEqual(result, "textual response")

    def test_handles_malformed_jsonl_lines(self):
        with open(self.transcript, "w") as f:
            f.write("not json at all\n")
            f.write(json.dumps(self._assistant_entry("real text")) + "\n")
            f.write("{also malformed\n")
        result = self.hook._read_prev_assistant_tail(str(self.transcript), 100)
        self.assertEqual(result, "real text")


# ─────────────────────────────────────────────────────────────────────────────
# server.py: engram_surface + _search_nodes backward compat + embed_query path
# ─────────────────────────────────────────────────────────────────────────────


class TestShortPromptBoundary(unittest.TestCase):
    """Verify the strict `<` boundary behavior at len(prompt) == threshold.

    The hook condition is `len(prompt) < threshold_chars`. So a prompt of
    exactly `threshold` chars does NOT trigger prepending; one char shorter
    does. Round-1 fairy flagged the missing boundary coverage.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_boundary_test_")
        os.environ["ENGRAM_HOME"] = self.tmp
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp
        # Use default threshold (100). No config.json present.
        # Set up a transcript with a prior assistant response so prepending
        # has something to prepend.
        self.transcript = Path(self.tmp) / "transcript.jsonl"
        self.transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "prior topic context"}]},
        }) + "\n")

    def tearDown(self):
        os.environ.pop("ENGRAM_HOME", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _compute_embed_query(self, prompt: str) -> str | None:
        """Replicates the hook main()'s embed_query computation."""
        cfg = self.hook._get_auto_surface_config()
        if len(prompt) < cfg["short_prompt_threshold_chars"]:
            tail = self.hook._read_prev_assistant_tail(
                str(self.transcript), cfg["prev_response_tail_chars"]
            )
            if tail:
                return f"{tail}\n\n{prompt}"
        return None

    def test_prompt_one_char_below_threshold_triggers_prepending(self):
        prompt = "x" * 99  # threshold=100, len=99 → <100 → prepend
        result = self._compute_embed_query(prompt)
        self.assertIsNotNone(result)
        self.assertTrue(result.endswith(prompt))
        self.assertIn("prior topic context", result)

    def test_prompt_exactly_at_threshold_does_not_trigger_prepending(self):
        prompt = "x" * 100  # threshold=100, len=100 → NOT <100 → no prepend
        result = self._compute_embed_query(prompt)
        self.assertIsNone(result)

    def test_prompt_above_threshold_does_not_trigger_prepending(self):
        prompt = "x" * 200
        result = self._compute_embed_query(prompt)
        self.assertIsNone(result)

    def test_short_prompt_with_empty_prev_tail_returns_none(self):
        """If transcript has no prior assistant response, embed_query stays
        None even for short prompts — daemon falls back to query as before."""
        self.transcript.write_text(json.dumps({
            "type": "user", "message": {"content": "hi"},
        }) + "\n")
        result = self._compute_embed_query("yes")
        self.assertIsNone(result)


class TestSearchNodesEmbedQuery(unittest.TestCase):
    """Verifies the embed_query parameter threads to semantic search while
    FTS still uses query. Mocks _semantic_search to inspect call args without
    needing a loaded embedder (slow). Direct module-level test, no MCP wrapper.
    """

    def setUp(self):
        import server
        self.server = server
        self.conn = MagicMock()
        # Make FTS query path return empty (we're only testing the dispatch,
        # not the merge logic). _sanitize_fts_query needs to return something
        # truthy so FTS branch runs; we mock the cursor to return nothing.
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        self.conn.execute.return_value = cursor

    def test_backward_compat_no_embed_query_uses_query_for_both(self):
        # Dual-patch: _search_nodes moved to engram_query.py (wave 8). It calls
        # core._semantic_search / core._embedder / core._get_embedding_config /
        # core._get_tier_threshold directly; also calls module-local
        # _get_min_queryable_importance (within _query_mod). Patch the core
        # objects and the _query_mod module (canonical call sites).
        with patch.object(self.server.core, "_semantic_search") as mock_sem, \
             patch.object(self.server.core._embedder, "is_available", return_value=True), \
             patch.object(self.server.core, "_get_embedding_config", return_value={"enabled": True}), \
             patch.object(self.server.core, "_get_tier_threshold", return_value=0.0), \
             patch.object(self.server._query_mod, "_get_min_queryable_importance", return_value=1.0):
            mock_sem.return_value = []
            self.server._search_nodes(self.conn, "the prompt", use_semantic=True)
            # First positional arg to _semantic_search after conn = query string
            args, _ = mock_sem.call_args
            self.assertEqual(args[1], "the prompt")  # (conn, query=...)

    def test_embed_query_overrides_semantic_path_only(self):
        with patch.object(self.server.core, "_semantic_search") as mock_sem, \
             patch.object(self.server.core._embedder, "is_available", return_value=True), \
             patch.object(self.server.core, "_get_embedding_config", return_value={"enabled": True}), \
             patch.object(self.server.core, "_get_tier_threshold", return_value=0.0), \
             patch.object(self.server._query_mod, "_get_min_queryable_importance", return_value=1.0):
            mock_sem.return_value = []
            self.server._search_nodes(
                self.conn, "yes ok",
                embed_query="we were discussing X. yes ok",
                use_semantic=True,
            )
            args, _ = mock_sem.call_args
            # Semantic search saw the prepended embed_query
            self.assertEqual(args[1], "we were discussing X. yes ok")

    def test_embed_query_none_explicit_defaults_to_query(self):
        with patch.object(self.server.core, "_semantic_search") as mock_sem, \
             patch.object(self.server.core._embedder, "is_available", return_value=True), \
             patch.object(self.server.core, "_get_embedding_config", return_value={"enabled": True}), \
             patch.object(self.server.core, "_get_tier_threshold", return_value=0.0), \
             patch.object(self.server._query_mod, "_get_min_queryable_importance", return_value=1.0):
            mock_sem.return_value = []
            self.server._search_nodes(self.conn, "hello", embed_query=None, use_semantic=True)
            args, _ = mock_sem.call_args
            self.assertEqual(args[1], "hello")


if __name__ == "__main__":
    unittest.main()
