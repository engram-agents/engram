"""Tests for the lesson-tripwire PreToolUse hook (issue #1203).

Coverage:
  1. test_matching_command_returns_nudge — pattern match returns the nudge string
  2. test_non_matching_command_returns_empty — no match returns empty list
  3. test_lesson_without_pattern_excluded — lesson without situation_pattern not returned
  4. test_malformed_regex_skipped — broken regex skipped without raising
  5. test_pattern_case_insensitive — FORUM REPLY matches (?:forum reply|ia write)
  6. test_situation_pattern_stored_via_mcp — engram_add_lesson stores situation_pattern
     in metadata and it is queryable by load_tripwires
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC_ENGRAM))

# ---------------------------------------------------------------------------
# Load the hook module (hyphenated filename, not importable via normal import)
# ---------------------------------------------------------------------------

def _import_hook():
    p = PROJECT_ROOT / "src/engram/hooks/claude/engram-lesson-tripwire-hook.py"
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_hook = _import_hook()
load_tripwires = _hook.load_tripwires
check_command = _hook.check_command

# ---------------------------------------------------------------------------
# Helpers for tests 1–5: a minimal temp DB with just the nodes table
# ---------------------------------------------------------------------------

_MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1,
    memory_status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    claim TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT '',
    logical_chain TEXT NOT NULL DEFAULT '',
    confidence_history TEXT NOT NULL DEFAULT '[]'
)
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal SQLite DB with the nodes table."""
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_MINIMAL_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _insert_lesson(db: Path, *, pattern: str = "", nudge: str = "do the right thing"):
    """Insert a lesson row with optional situation_pattern in metadata."""
    meta: dict = {"scaffolding_nudge": nudge, "reasoning_type": "inductive_generalization"}
    if pattern:
        meta["situation_pattern"] = pattern
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, 'lesson', 1, 'active', ?, ?)",
        (f"ls_{len(nudge):04d}", json.dumps(meta), "test lesson"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests 1–5: unit-testing load_tripwires + check_command with temp DB
# ---------------------------------------------------------------------------

class TestLoadTripwires(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_tripwire_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matching_command_returns_nudge(self):
        """A command matching the pattern → returns the nudge string."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"(?:forum reply|ia write)", nudge="Read body from stdin; --body silently no-ops.")
        tripwires = load_tripwires(db_path=db)
        hits = check_command("forum reply 42", tripwires)
        self.assertEqual(len(hits), 1)
        self.assertIn("stdin", hits[0])

    def test_non_matching_command_returns_empty(self):
        """A command that doesn't match any pattern → empty list."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"(?:forum reply|ia write)", nudge="Read body from stdin.")
        tripwires = load_tripwires(db_path=db)
        hits = check_command("git commit -m 'wip'", tripwires)
        self.assertEqual(hits, [])

    def test_lesson_without_pattern_excluded(self):
        """A lesson with no situation_pattern in metadata is not returned by load_tripwires."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern="", nudge="This lesson has no pattern.")
        tripwires = load_tripwires(db_path=db)
        self.assertEqual(tripwires, [], "Lessons without situation_pattern must not appear in tripwires")

    def test_malformed_regex_skipped(self):
        """A lesson with a broken regex is skipped without raising."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern="[", nudge="Malformed pattern lesson.")
        tripwires = load_tripwires(db_path=db)
        # load_tripwires returns it (pattern is stored), but check_command skips it silently
        hits = check_command("forum reply 42", tripwires)
        self.assertEqual(hits, [], "Malformed regex must be silently skipped, not raised")

    def test_pattern_case_insensitive(self):
        """FORUM REPLY (uppercase) matches (?:forum reply|ia write) pattern (case-insensitive)."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"(?:forum reply|ia write)", nudge="Read body from stdin.")
        tripwires = load_tripwires(db_path=db)
        hits = check_command("FORUM REPLY 42", tripwires)
        self.assertEqual(len(hits), 1, "Pattern match must be case-insensitive")


# ---------------------------------------------------------------------------
# Test 6: integration — situation_pattern stored via engram_add_lesson MCP tool
# ---------------------------------------------------------------------------

TEST_DIR = tempfile.mkdtemp(prefix="engram_tripwire_mcp_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_server():
    """Create a fresh server instance with an empty database."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_incident(server) -> str:
    """Create a claim-bearing observation node usable as an incident."""
    payload = {
        "quoted_text": "I ran forum reply --body flag and it silently no-oped.",
        "interpretation": "The --body flag is silently ignored; stdin is required.",
        "claim": "forum reply --body flag silently ignores the body; use stdin.",
        "quote_type": "personal_communication",
        "url": "https://example.com/incident",
        "title": "forum reply --body incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


class TestSituationPatternStoredViaMcp(unittest.TestCase):

    def test_situation_pattern_stored_via_mcp(self):
        """engram_add_lesson with situation_pattern stores it in metadata JSON."""
        server = _fresh_server()
        inc_id = _seed_incident(server)

        pattern = r"(?:forum reply|ia write)"
        payload = {
            "claim": "forum reply / ia write read body from stdin; --body silently no-ops.",
            "incident_ids": inc_id,
            "scaffolding_nudge": "Pipe the body via stdin, not --body flag.",
            "logical_chain": "Single incident → action-moment tripwire pattern.",
            "situation_pattern": pattern,
        }
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
        self.assertNotIn("error", r, r)
        lesson_id = r["lesson_id"]
        self.assertTrue(lesson_id.startswith("ls_"), r)

        # Verify directly in DB that situation_pattern was stored
        db_path = Path(TEST_DIR) / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (lesson_id,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "Lesson node must exist in DB")
        meta = json.loads(row[0])
        self.assertIn("situation_pattern", meta, f"situation_pattern missing from metadata: {meta}")
        self.assertEqual(meta["situation_pattern"], pattern)

        # Also verify that load_tripwires returns this lesson
        tripwires = load_tripwires(db_path=db_path)
        patterns_in_tripwires = [p for p, _ in tripwires]
        self.assertIn(pattern, patterns_in_tripwires, "Pattern must appear in load_tripwires output")


if __name__ == "__main__":
    unittest.main()
