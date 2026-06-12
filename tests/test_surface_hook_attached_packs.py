"""Tests for attached-pack surfacing in engram-surface-hook.py.

Covers issue #858 slice 1: _get_attached_packs, _query_attached_packs,
format_nudge pack section, and end-to-end main() behavior.

All tests are hermetic: they use tmp_path ENGRAM_HOME and fixture pack DBs
built in-test with a minimal schema (nodes table + nodes_fts contentful FTS5
mirror). Node IDs in fixtures use xx_NNNN-style prefixes — not real ENGRAM
IDs — and the test file is in tests/ (excluded by the no-new-shipped-node-ids
CI gate, so fixture IDs here are safe).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers: load hook module + build fixture pack DB
# ---------------------------------------------------------------------------


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_pack_db(db_path: str, rows: list[dict]) -> None:
    """Create a minimal pack DB at db_path with nodes + nodes_fts tables.

    Each row dict must have: id, type, claim. Optional: confidence,
    recall_summary, recall_keywords (list → stored as JSON).
    is_current defaults to 1.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE nodes (
            rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
            id              TEXT NOT NULL,
            type            TEXT DEFAULT 'ob',
            claim           TEXT,
            confidence      REAL,
            recall_summary  TEXT,
            recall_keywords TEXT,
            is_current      INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim,
            content='nodes', content_rowid='rowid'
        )
    """)
    for row in rows:
        kw = row.get("recall_keywords")
        kw_json = json.dumps(kw) if kw is not None else None
        conn.execute(
            """INSERT INTO nodes (id, type, claim, confidence, recall_summary,
               recall_keywords, is_current)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row.get("type", "ob"),
                row.get("claim", ""),
                row.get("confidence"),
                row.get("recall_summary"),
                kw_json,
                row.get("is_current", 1),
            ),
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim) VALUES (last_insert_rowid(), ?)",
            (row.get("claim", ""),),
        )
    conn.commit()
    conn.close()


def _write_config(engram_home: str, config: dict) -> None:
    """Write config.json to engram_home."""
    with open(os.path.join(engram_home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)


# ---------------------------------------------------------------------------
# Unit tests for _get_attached_packs
# ---------------------------------------------------------------------------


class TestGetAttachedPacks(unittest.TestCase):
    """Unit tests for _get_attached_packs()."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="engram_pack_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Case 1a: No attached_packs key → return []
    def test_no_attached_packs_key_returns_empty(self):
        """Config with no attached_packs key → _get_attached_packs returns []."""
        _write_config(self.tmp, {"primary_user": "test"})
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])

    # Case 1b: attached_packs is empty list
    def test_empty_attached_packs_returns_empty(self):
        _write_config(self.tmp, {"attached_packs": []})
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])

    # Case 1c: No config file at all
    def test_missing_config_returns_empty(self):
        # tmp dir has no config.json
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])

    # Case 4: enabled: false → filtered out
    def test_disabled_pack_filtered_out(self):
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "mypack", "path": "/some/path", "enabled": False},
            ]
        })
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])

    # Enabled pack returns correctly
    def test_enabled_pack_returned(self):
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "mypack", "path": "/some/path", "enabled": True},
            ]
        })
        result = self.hook._get_attached_packs()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "mypack")
        self.assertEqual(result[0]["path"], "/some/path")

    # Mixed enabled/disabled
    def test_mixed_enabled_disabled(self):
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "pack-a", "path": "/a", "enabled": True},
                {"id": "pack-b", "path": "/b", "enabled": False},
                {"id": "pack-c", "path": "/c", "enabled": True},
            ]
        })
        result = self.hook._get_attached_packs()
        self.assertEqual(len(result), 2)
        ids = [r["id"] for r in result]
        self.assertIn("pack-a", ids)
        self.assertIn("pack-c", ids)
        self.assertNotIn("pack-b", ids)

    # Empty id or path → filtered
    def test_empty_id_filtered(self):
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "", "path": "/some/path", "enabled": True},
            ]
        })
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])

    def test_empty_path_filtered(self):
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "mypack", "path": "", "enabled": True},
            ]
        })
        result = self.hook._get_attached_packs()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Unit tests for _query_attached_packs
# ---------------------------------------------------------------------------


class TestQueryAttachedPacks(unittest.TestCase):
    """Unit tests for _query_attached_packs()."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="engram_pack_query_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_pack(self, name: str, rows: list[dict]) -> str:
        """Create a pack dir with knowledge.db; return pack dir path."""
        pack_dir = os.path.join(self.tmp, name)
        os.makedirs(pack_dir, exist_ok=True)
        _build_pack_db(os.path.join(pack_dir, "knowledge.db"), rows)
        return pack_dir

    # Case 1: No attached_packs → return []
    def test_no_packs_returns_empty(self):
        """No attached_packs in config → _query_attached_packs returns []."""
        _write_config(self.tmp, {})
        result = self.hook._query_attached_packs("epistemology knowledge graph")
        self.assertEqual(result, [])

    # Case 2: Enabled pack with matching content → results appear, capped at quota
    def test_matching_pack_returns_results(self):
        """Enabled pack with matching FTS content → results with pack_id tagged."""
        pack_dir = self._make_pack("lib1", [
            {"id": "xx_0001", "type": "ob", "claim": "epistemology studies knowledge", "confidence": 0.8},
            {"id": "xx_0002", "type": "ob", "claim": "knowledge graph structures information"},
            {"id": "xx_0003", "type": "ob", "claim": "epistemology and knowledge theory"},
            {"id": "xx_0004", "type": "ob", "claim": "knowledge representation systems"},
            {"id": "xx_0005", "type": "ob", "claim": "epistemology foundation knowledge"},
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "lib1", "path": pack_dir, "enabled": True}]
        })
        result = self.hook._query_attached_packs("epistemology knowledge", quota=3)
        self.assertGreater(len(result), 0)
        self.assertLessEqual(len(result), 3)
        for r in result:
            self.assertEqual(r["pack_id"], "lib1")

    # Case 2b: 5+ matching nodes but quota=3 → capped at 3
    def test_quota_caps_results(self):
        """Results capped at quota even when more nodes match."""
        pack_dir = self._make_pack("lib_quota", [
            {"id": f"xx_{i:04d}", "type": "ob", "claim": f"memory knowledge retention fact {i}"}
            for i in range(10)
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "libq", "path": pack_dir, "enabled": True}]
        })
        result = self.hook._query_attached_packs("memory knowledge retention", quota=3)
        self.assertLessEqual(len(result), 3, "Results must be capped at quota=3")

    # Case 3a: Node WITH recall_summary + recall_keywords → fields present
    def test_node_with_recall_summary_and_keywords(self):
        """Node with recall_summary+recall_keywords → those fields in result."""
        pack_dir = self._make_pack("lib_rich", [
            {
                "id": "xx_0010",
                "type": "ob",
                "claim": "epistemology studies knowledge and its foundations",
                "confidence": 0.9,
                "recall_summary": "Core epistemology overview",
                "recall_keywords": ["epistemology", "knowledge", "foundations"],
            }
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "librich", "path": pack_dir, "enabled": True}]
        })
        result = self.hook._query_attached_packs("epistemology knowledge", quota=3)
        self.assertEqual(len(result), 1)
        r = result[0]
        self.assertEqual(r["recall_summary"], "Core epistemology overview")
        self.assertEqual(r["recall_keywords"], ["epistemology", "knowledge", "foundations"])

    # Case 3b: Node WITHOUT recall_summary → fallback in render
    def test_node_without_recall_summary(self):
        """Node without recall_summary → recall_summary is None in result."""
        pack_dir = self._make_pack("lib_plain", [
            {
                "id": "xx_0020",
                "type": "ob",
                "claim": "epistemology and knowledge are deeply related concepts in philosophy",
            }
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "libplain", "path": pack_dir, "enabled": True}]
        })
        result = self.hook._query_attached_packs("epistemology knowledge", quota=3)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["recall_summary"])

    # Case 4: enabled: false → ignored (already tested in _get_attached_packs,
    # but confirm end-to-end in _query_attached_packs too)
    def test_disabled_pack_ignored(self):
        """Disabled pack → _query_attached_packs returns []."""
        pack_dir = self._make_pack("lib_disabled", [
            {"id": "xx_0030", "type": "ob", "claim": "epistemology knowledge study"}
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "libdis", "path": pack_dir, "enabled": False}]
        })
        result = self.hook._query_attached_packs("epistemology knowledge")
        self.assertEqual(result, [])

    # Case 5: Pack path missing / DB unreadable → skipped silently
    def test_missing_pack_db_skipped_silently(self):
        """Missing pack DB → skipped silently, returns []."""
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "ghost", "path": "/nonexistent/path", "enabled": True}
            ]
        })
        # Must not raise — fail-open
        result = self.hook._query_attached_packs("knowledge memory")
        self.assertEqual(result, [])

    def test_corrupted_pack_db_skipped_silently(self):
        """Corrupted pack DB → skipped silently."""
        pack_dir = os.path.join(self.tmp, "corrupt_pack")
        os.makedirs(pack_dir, exist_ok=True)
        # Write garbage as the DB file
        with open(os.path.join(pack_dir, "knowledge.db"), "w") as f:
            f.write("this is not a sqlite database\n")
        _write_config(self.tmp, {
            "attached_packs": [{"id": "corrupt", "path": pack_dir, "enabled": True}]
        })
        result = self.hook._query_attached_packs("knowledge memory")
        self.assertEqual(result, [])

    # Case 6: Read-only invariant — pack DB mtime unchanged after query
    def test_pack_db_not_modified_after_query(self):
        """Pack DB file mtime must not change after _query_attached_packs call."""
        pack_dir = self._make_pack("lib_ro", [
            {"id": "xx_0040", "type": "ob", "claim": "knowledge memory retention study"}
        ])
        _write_config(self.tmp, {
            "attached_packs": [{"id": "libro", "path": pack_dir, "enabled": True}]
        })
        db_path = os.path.join(pack_dir, "knowledge.db")
        mtime_before = os.path.getmtime(db_path)
        size_before = os.path.getsize(db_path)

        self.hook._query_attached_packs("knowledge memory retention")

        mtime_after = os.path.getmtime(db_path)
        size_after = os.path.getsize(db_path)
        self.assertEqual(mtime_before, mtime_after, "Pack DB mtime changed — write occurred")
        self.assertEqual(size_before, size_after, "Pack DB size changed — write occurred")

    # Case 7: Quota across TWO packs — config-order priority, 3 total
    def test_quota_across_two_packs(self):
        """Config-order priority: pack-A fills up to quota before pack-B contributes."""
        # Pack A: 3 matching nodes
        pack_a_dir = self._make_pack("pack_a", [
            {"id": f"aa_{i:04d}", "type": "ob", "claim": f"knowledge memory retention concept {i}"}
            for i in range(3)
        ])
        # Pack B: 3 matching nodes
        pack_b_dir = self._make_pack("pack_b", [
            {"id": f"bb_{i:04d}", "type": "ob", "claim": f"knowledge memory retention concept {i}"}
            for i in range(3)
        ])
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "pack-a", "path": pack_a_dir, "enabled": True},
                {"id": "pack-b", "path": pack_b_dir, "enabled": True},
            ]
        })
        result = self.hook._query_attached_packs("knowledge memory retention", quota=3)
        self.assertEqual(len(result), 3, "Total must be capped at quota=3 across packs")
        # Config-order priority: pack-a fills first, pack-b only if quota remains
        pack_ids = [r["pack_id"] for r in result]
        # All 3 slots go to pack-a (it has 3 matching nodes, fills quota)
        self.assertEqual(pack_ids.count("pack-a"), 3,
                         "pack-a should fill all 3 quota slots (config order priority)")
        self.assertEqual(pack_ids.count("pack-b"), 0,
                         "pack-b should get 0 slots when pack-a fills quota")

    def test_quota_across_two_packs_partial_fill(self):
        """When pack-A has fewer than quota, pack-B fills the remainder."""
        # Pack A: only 1 matching node
        pack_a_dir = self._make_pack("pack_partial_a", [
            {"id": "aa_0000", "type": "ob", "claim": "knowledge memory retention unique alpha"}
        ])
        # Pack B: 3 matching nodes
        pack_b_dir = self._make_pack("pack_partial_b", [
            {"id": f"bb_{i:04d}", "type": "ob", "claim": f"knowledge memory retention concept {i}"}
            for i in range(3)
        ])
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "alpha", "path": pack_a_dir, "enabled": True},
                {"id": "beta", "path": pack_b_dir, "enabled": True},
            ]
        })
        result = self.hook._query_attached_packs("knowledge memory retention", quota=3)
        self.assertEqual(len(result), 3, "Should fill quota across both packs")
        pack_ids = [r["pack_id"] for r in result]
        self.assertEqual(pack_ids.count("alpha"), 1, "pack-a contributes 1")
        self.assertEqual(pack_ids.count("beta"), 2, "pack-b fills remaining 2 slots")

    def test_wal_mode_pack_no_sidecar_writes(self):
        """WAL-mode pack DB: _query_attached_packs must not create .db-shm/.db-wal sidecars.

        engram-pkg-built packs use WAL journal mode. A plain mode=ro connection
        creates a .db-shm sidecar (a write into the pack dir). The immutable=1
        URI flag suppresses sidecar creation. This test verifies the invariant
        empirically: after _query_attached_packs, the pack directory must contain
        only the original .db file — no .db-shm or .db-wal files.
        """
        pack_dir = os.path.join(self.tmp, "wal_pack")
        os.makedirs(pack_dir, exist_ok=True)
        db_path = os.path.join(pack_dir, "knowledge.db")

        # Build a WAL-mode fixture DB
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE nodes (
                rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
                id              TEXT NOT NULL,
                type            TEXT DEFAULT 'ob',
                claim           TEXT,
                confidence      REAL,
                recall_summary  TEXT,
                recall_keywords TEXT,
                is_current      INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                claim,
                content='nodes', content_rowid='rowid'
            )
        """)
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current) "
            "VALUES ('ww_0001', 'ob', 'epistemology knowledge memory foundations', 0.85, 1)"
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim) SELECT rowid, claim FROM nodes"
        )
        conn.commit()
        conn.close()

        # Record directory state before the query
        files_before = set(os.listdir(pack_dir))

        _write_config(self.tmp, {
            "attached_packs": [{"id": "wal-lib", "path": pack_dir, "enabled": True}]
        })

        # Run the query — this is the operation under test
        results = self.hook._query_attached_packs("epistemology knowledge memory", quota=3)

        # Directory state after the query must be identical
        files_after = set(os.listdir(pack_dir))
        self.assertEqual(
            files_before, files_after,
            f"Sidecar files created by _query_attached_packs: "
            f"{files_after - files_before} (immutable=1 must suppress WAL sidecars)"
        )
        # Sanity: the query actually returned results (DB is readable)
        self.assertGreater(len(results), 0, "WAL-mode pack query returned no results")


# ---------------------------------------------------------------------------
# Unit tests for format_nudge pack section rendering
# ---------------------------------------------------------------------------


class TestFormatNudgePackSection(unittest.TestCase):
    """Unit tests for the pack section in format_nudge."""

    def setUp(self):
        self.hook = _load_hook_module()

    def _base_result(self) -> dict:
        """Minimal own-graph result dict with one match (so total != 0)."""
        return {
            "match_count": 1,
            "top_claims": [{"id": "yy_0001", "type": "ob", "claim": "some test claim",
                            "confidence": 0.8, "recall_summary": None, "recall_keywords": None}],
            "special_nodes": [],
            "matched_meta": [],
            "matched_ids": ["yy_0001"],
            "age": {},
            "issues": {},
        }

    # No pack results → no "From attached libraries" section
    def test_no_pack_results_no_section(self):
        """format_nudge with no pack_results → no attached-libraries section."""
        nudge = self.hook.format_nudge(self._base_result(), pack_results=None)
        self.assertNotIn("From attached libraries", nudge)
        nudge2 = self.hook.format_nudge(self._base_result(), pack_results=[])
        self.assertNotIn("From attached libraries", nudge2)

    # Pack results present → section appears
    def test_pack_results_section_appears(self):
        """format_nudge with pack_results → 'From attached libraries' section rendered."""
        pack_results = [
            {
                "id": "xx_0001",
                "type": "ob",
                "claim": "external library claim about epistemology",
                "confidence": 0.85,
                "recall_summary": "Epistemology overview",
                "recall_keywords": ["epistemology", "knowledge"],
                "pack_id": "mylib",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        self.assertIn("From attached libraries (read-only — cite, never import):", nudge)
        self.assertIn("mylib:xx_0001", nudge)

    # Node with recall_summary → summary rendered via render_one_node_line
    def test_pack_node_with_summary_renders_summary(self):
        """Pack node with recall_summary → summary text appears in nudge."""
        pack_results = [
            {
                "id": "xx_0010",
                "type": "ob",
                "claim": "a long claim that should be truncated if needed",
                "confidence": 0.9,
                "recall_summary": "Core epistemology overview summary here",
                "recall_keywords": ["epistemology"],
                "pack_id": "lib1",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        self.assertIn("Core epistemology overview summary here", nudge)
        self.assertIn("`epistemology`", nudge)

    # Node without recall_summary, long claim → truncated claim
    def test_pack_node_without_summary_truncates_long_claim(self):
        """Pack node without recall_summary: claim > 120 chars → truncated with ellipsis."""
        long_claim = "x" * 130  # 130 chars, > 120 threshold
        pack_results = [
            {
                "id": "xx_0020",
                "type": "ob",
                "claim": long_claim,
                "confidence": None,
                "recall_summary": None,
                "recall_keywords": None,
                "pack_id": "lib2",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        # Truncated to 117 chars + "…"
        self.assertIn("…", nudge)
        # The rendered line should not include the full 130-char claim
        self.assertNotIn(long_claim, nudge)

    # Node without recall_summary, short claim → full claim rendered
    def test_pack_node_without_summary_short_claim(self):
        """Pack node without recall_summary: claim ≤ 120 chars → full claim rendered."""
        short_claim = "A short claim about epistemology."
        pack_results = [
            {
                "id": "xx_0030",
                "type": "ob",
                "claim": short_claim,
                "confidence": 0.7,
                "recall_summary": None,
                "recall_keywords": None,
                "pack_id": "lib3",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        self.assertIn(short_claim, nudge)

    # Namespaced ID format: pack_id:node_id
    def test_pack_namespaced_id_format(self):
        """Pack results rendered with 'pack_id:node_id' namespace prefix."""
        pack_results = [
            {
                "id": "xx_0050",
                "type": "ob",
                "claim": "namespaced claim",
                "confidence": 0.75,
                "recall_summary": "summary",
                "recall_keywords": None,
                "pack_id": "external-lib",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        self.assertIn("[external-lib:xx_0050]", nudge)

    # Pack section appears AFTER own-graph content
    def test_pack_section_after_own_graph(self):
        """Pack section appears after own-graph content in the nudge."""
        pack_results = [
            {
                "id": "xx_0060",
                "type": "ob",
                "claim": "pack claim",
                "confidence": 0.8,
                "recall_summary": "pack summary",
                "recall_keywords": None,
                "pack_id": "mylib",
            }
        ]
        nudge = self.hook.format_nudge(self._base_result(), pack_results=pack_results)
        own_graph_pos = nudge.find("[ENGRAM Recall:")
        pack_section_pos = nudge.find("From attached libraries")
        self.assertGreater(pack_section_pos, own_graph_pos,
                           "Pack section must appear after own-graph ENGRAM Recall header")


# ---------------------------------------------------------------------------
# End-to-end subprocess test
# ---------------------------------------------------------------------------


class TestMainWithAttachedPacks(unittest.TestCase):
    """End-to-end test: main() with an attached pack, run as subprocess."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="engram_pack_e2e_test_")
        # Build a fixture pack
        pack_dir = os.path.join(self.tmp, "testlib")
        os.makedirs(pack_dir, exist_ok=True)
        _build_pack_db(os.path.join(pack_dir, "knowledge.db"), [
            {
                "id": "xx_0100",
                "type": "ob",
                "claim": "epistemology studies knowledge and its foundations",
                "confidence": 0.85,
                "recall_summary": "Epistemology foundation summary",
                "recall_keywords": ["epistemology", "foundations"],
            },
        ])
        _write_config(self.tmp, {
            "attached_packs": [
                {"id": "testlib", "path": pack_dir, "enabled": True}
            ]
        })
        self.pack_dir = pack_dir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, prompt: str) -> dict:
        """Run the hook as a subprocess; return the parsed JSON output."""
        import subprocess
        hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        transcript_path = os.path.join(self.tmp, "transcript.jsonl")
        # Write empty transcript
        with open(transcript_path, "w") as f:
            f.write("")
        env = dict(os.environ)
        env.update({
            "ENGRAM_HOME": self.tmp,
            "HOME": self.tmp,
            "ENGRAM_RUNTIME_DIR": str(PROJECT_ROOT),
        })
        result = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps({
                "prompt": prompt,
                "session_id": "pack-e2e-test",
                "transcript_path": transcript_path,
            }),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0,
                         f"Hook exited non-zero: {result.returncode}\n"
                         f"stderr: {result.stderr[:500]}")
        stdout = result.stdout.strip()
        self.assertTrue(stdout, "Hook produced no stdout")
        return json.loads(stdout)

    def test_e2e_pack_results_appear_in_additional_context(self):
        """End-to-end: hook with attached pack surfaces namespaced results."""
        output = self._run_hook("epistemology knowledge foundations")
        context = (
            output
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        # The pack section or standalone mini-nudge must appear
        self.assertIn("attached libraries", context,
                      "Pack section should appear in additionalContext when pack has matching nodes")
        self.assertIn("testlib:xx_0100", context,
                      "Namespaced node ID should appear in output")

    def test_e2e_no_packs_no_section(self):
        """End-to-end: hook with no attached_packs → no 'attached libraries' section."""
        # Overwrite config with no attached_packs
        _write_config(self.tmp, {})
        output = self._run_hook("epistemology knowledge foundations")
        context = (
            output
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertNotIn("From attached libraries", context,
                         "No attached_packs → section must not appear")


# ---------------------------------------------------------------------------
# Unit tests for standalone mini-nudge footer text (round-2 fix)
# ---------------------------------------------------------------------------


class TestStandaloneMiniNudgeFooter(unittest.TestCase):
    """Verify the standalone pack-only mini-nudge footer points at engram-pkg,
    not engram_inspect / engram_get_subgraph.

    Covers both standalone paths in main():
      - Path A: result is not None but match_count=0 (0 own-graph matches)
      - Path B: result is None (own-graph query failed entirely)
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="engram_minifooter_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp
        # Wire up no prompt-counter, no write-reminder, no repair marker, etc.
        # by pointing all auxiliary paths into the tmp dir (they'll be absent → no-op).
        self.hook.COUNTER_PATH = os.path.join(self.tmp, "prompt-counter.json")
        self.hook.WRITE_REMINDER_PATH = os.path.join(self.tmp, "write-reminder.json")
        self.hook.REPAIR_MARKER_PATH = os.path.join(self.tmp, "toolcall-repair-pending.json")
        self.hook.FEELING_NUDGE_MARKER = os.path.join(self.tmp, "feeling-nudge-active.json")
        self.hook.WARM_BRIEFING_PATH = os.path.join(self.tmp, "warm-briefing.md")
        self.hook.ERROR_PATTERNS_PATH = os.path.join(self.tmp, "error_patterns.json")
        self.hook.ERROR_INCIDENTS_PATH = os.path.join(self.tmp, "error_incidents.json")
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(self.tmp, "knowledge.db")
        self.hook.SOCKET_PATH = os.path.join(self.tmp, "recall-daemon.sock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_pack_result(self) -> list[dict]:
        """A minimal pack result list simulating one matched pack node."""
        return [
            {
                "id": "zz_0001",
                "type": "ob",
                "claim": "pack claim about epistemology",
                "confidence": 0.8,
                "recall_summary": "pack summary",
                "recall_keywords": ["epistemology"],
                "pack_id": "testpack",
            }
        ]

    def _run_main_with_mocks(self, own_graph_result) -> str:
        """Run main() in-process with mocked own-graph + pack queries.

        own_graph_result: dict (match_count=0 simulates path A) or None (path B).
        Returns the additionalContext string, or "" if hook emitted {}.
        """
        pack_results = self._make_pack_result()

        # Patch daemon + fts fallback to return own_graph_result
        orig_daemon = self.hook.query_daemon
        orig_fts = self.hook.query_fts_fallback
        orig_pack = self.hook._query_attached_packs
        orig_tripwire = self.hook.check_incident_tripwire
        orig_patterns = self.hook.check_error_patterns
        orig_write = self.hook.check_write_reminder
        orig_repair = self.hook.check_repair_marker
        orig_feeling = self.hook.check_feeling_nudge_marker
        orig_warm = self.hook.check_warm_briefing

        self.hook.query_daemon = lambda *a, **kw: own_graph_result
        self.hook.query_fts_fallback = lambda *a, **kw: own_graph_result
        self.hook._query_attached_packs = lambda *a, **kw: pack_results
        self.hook.check_incident_tripwire = lambda *a, **kw: ""
        self.hook.check_error_patterns = lambda *a, **kw: ""
        self.hook.check_write_reminder = lambda *a, **kw: ""
        self.hook.check_repair_marker = lambda *a, **kw: ""
        self.hook.check_feeling_nudge_marker = lambda *a, **kw: ""
        self.hook.check_warm_briefing = lambda *a, **kw: ""

        hook_input = json.dumps({
            "prompt": "epistemology knowledge",
            "session_id": "minifooter-test",
            "transcript_path": os.path.join(self.tmp, "t.jsonl"),
        })

        captured_output = io.StringIO()
        orig_stdout = sys.stdout
        orig_stdin = sys.stdin
        orig_context_tracker = sys.modules.get("context_tracker")
        sys.modules["context_tracker"] = types.SimpleNamespace(
            estimate_usage=lambda *a, **kw: None,
            format_drowsiness=lambda *a, **kw: "",
        )
        try:
            sys.stdin = io.StringIO(hook_input)
            sys.stdout = captured_output
            try:
                self.hook.main()
            except SystemExit:
                pass
        finally:
            sys.stdout = orig_stdout
            sys.stdin = orig_stdin
            self.hook.query_daemon = orig_daemon
            self.hook.query_fts_fallback = orig_fts
            self.hook._query_attached_packs = orig_pack
            self.hook.check_incident_tripwire = orig_tripwire
            self.hook.check_error_patterns = orig_patterns
            self.hook.check_write_reminder = orig_write
            self.hook.check_repair_marker = orig_repair
            self.hook.check_feeling_nudge_marker = orig_feeling
            self.hook.check_warm_briefing = orig_warm
            if orig_context_tracker is None:
                sys.modules.pop("context_tracker", None)
            else:
                sys.modules["context_tracker"] = orig_context_tracker

        raw = captured_output.getvalue().strip()
        if not raw:
            return ""
        obj = json.loads(raw)
        return obj.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_path_a_zero_match_footer_uses_engram_pkg(self):
        """Path A (match_count=0): standalone mini-nudge footer contains 'engram-pkg'."""
        # result is not None but match_count=0 → format_nudge returns "" → standalone path
        own_graph_result = {
            "match_count": 0,
            "top_claims": [],
            "special_nodes": [],
            "matched_meta": [],
            "matched_ids": [],
            "age": {},
            "issues": {},
        }
        context = self._run_main_with_mocks(own_graph_result)
        self.assertIn("engram-pkg", context,
                      "Path A mini-nudge footer must reference engram-pkg")
        self.assertNotIn("engram_inspect(node_id)", context,
                         "Path A mini-nudge footer must NOT reference engram_inspect(node_id)")

    def test_path_b_unavailable_footer_uses_engram_pkg(self):
        """Path B (result=None): standalone mini-nudge footer contains 'engram-pkg'."""
        context = self._run_main_with_mocks(None)
        self.assertIn("engram-pkg", context,
                      "Path B mini-nudge footer must reference engram-pkg")
        self.assertNotIn("engram_inspect(node_id)", context,
                         "Path B mini-nudge footer must NOT reference engram_inspect(node_id)")

    def test_combined_nudge_footer_unchanged(self):
        """Combined-nudge path (own-graph has matches): footer still uses engram_inspect."""
        # format_nudge is called directly here — this does NOT touch main().
        own_graph_result = {
            "match_count": 1,
            "top_claims": [{"id": "yy_0001", "type": "ob", "claim": "test claim",
                            "confidence": 0.8, "recall_summary": None, "recall_keywords": None}],
            "special_nodes": [],
            "matched_meta": [],
            "matched_ids": ["yy_0001"],
            "age": {},
            "issues": {},
        }
        # Combined nudge: own-graph has matches, pack results appended inside format_nudge
        pack_results = self._make_pack_result()
        nudge = self.hook.format_nudge(own_graph_result, pack_results=pack_results)
        self.assertIn("engram_inspect(node_id)", nudge,
                      "Combined-nudge footer must still reference engram_inspect(node_id)")


if __name__ == "__main__":
    unittest.main()
