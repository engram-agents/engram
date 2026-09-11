"""Tests for the lesson-tripwire PreToolUse hook (issues #1203 + #1297).

Coverage:
  1. test_matching_command_returns_nudge — pattern match returns the nudge string
  2. test_non_matching_command_returns_empty — no match returns empty list
  3. test_lesson_without_pattern_excluded — lesson without situation_pattern not returned
  4. test_malformed_regex_skipped — broken regex skipped without raising
  5. test_pattern_case_insensitive — FORUM REPLY matches (?:forum reply|ia write)
  6. test_situation_pattern_stored_via_mcp — engram_add_lesson stores situation_pattern
     in metadata and it is queryable by load_tripwires
  7. test_mcp_tool_match — pattern matches mcp__engram__engram_add_observation in match target
  8. test_mcp_tool_no_match — unrelated MCP tool does not trigger forum-reply lesson
  9. test_bash_call_still_works — Bash call path unchanged after MCP extension
 10. test_build_match_target_bash — build_match_target extracts Bash command string
 11. test_build_match_target_mcp — build_match_target returns tool_name + args for MCP
 12. test_build_match_target_unknown — build_match_target returns "" for unknown tool_name
"""

from __future__ import annotations

import atexit
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))  # #2565: append, not insert(0)

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
build_match_target = _hook.build_match_target


def _run_hook_main(
    hook_input: dict, *, tripwires=None, recall: str = "", prevention_lessons=None,
) -> str:
    """Run the hook main() with controlled tripwires/recall/prevention_lessons;
    return stdout. prevention_lessons defaults to [] — the shadow-block path
    (alpha#1944 step 2) is a no-op unless a test explicitly supplies lessons,
    same posture as tripwires/recall defaulting empty."""
    output_buf = io.StringIO()
    with (
        unittest.mock.patch.object(_hook, "load_tripwires", return_value=tripwires or []),
        unittest.mock.patch.object(_hook, "load_prevention_lessons", return_value=prevention_lessons or []),
        unittest.mock.patch.object(_hook, "check_in_turn_recall", return_value=recall),
        unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
        unittest.mock.patch("sys.stdout", output_buf),
    ):
        try:
            _hook.main()
        except SystemExit:
            pass
    return output_buf.getvalue()

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


def _insert_lesson(
    db: Path, *, pattern: str = "", nudge: str = "do the right thing",
    prevention_type: str = "", node_id: str = "",
):
    """Insert a lesson row with optional situation_pattern (+ #1944
    prevention_type) in metadata."""
    meta: dict = {"scaffolding_nudge": nudge, "reasoning_type": "inductive_generalization"}
    if pattern:
        meta["situation_pattern"] = pattern
    if prevention_type:
        meta["prevention_type"] = prevention_type
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, 'lesson', 1, 'active', ?, ?)",
        (node_id or f"ls_{len(nudge):04d}", json.dumps(meta), "test lesson"),
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
        self.assertIn("stdin", hits[0]["nudge"])

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
# Tests 7–9: MCP locus-2 — pattern matching against tool_name + args (issue #1297)
# ---------------------------------------------------------------------------

class TestMcpToolMatching(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_tripwire_mcp_match_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mcp_tool_match(self):
        """Pattern matching engram_add_observation fires on the MCP tool call."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"engram_add_observation", nudge="Run verify_quote.py first.")
        tripwires = load_tripwires(db_path=db)
        # Simulate the match target built for an MCP engram_add_observation call
        match_target = 'mcp__plugin_engram_engram__engram_add_observation {"payload_json":"{}"}'
        hits = check_command(match_target, tripwires)
        self.assertEqual(len(hits), 1)
        self.assertIn("verify_quote", hits[0]["nudge"])

    def test_mcp_tool_no_match(self):
        """A forum-reply pattern does not fire on an unrelated MCP tool call."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"forum reply|ia write", nudge="Read body from stdin.")
        tripwires = load_tripwires(db_path=db)
        match_target = 'mcp__plugin_engram_engram__engram_add_observation {"payload_json":"{}"}'
        hits = check_command(match_target, tripwires)
        self.assertEqual(hits, [])

    def test_bash_call_still_works(self):
        """After the MCP extension, Bash command matching still works correctly."""
        db = _make_db(self.tmp)
        _insert_lesson(db, pattern=r"forum reply|ia write", nudge="Read body from stdin.")
        tripwires = load_tripwires(db_path=db)
        hits = check_command("forum reply 42", tripwires)
        self.assertEqual(len(hits), 1)


# ---------------------------------------------------------------------------
# Tests 10–12: build_match_target unit tests
# ---------------------------------------------------------------------------

class TestBuildMatchTarget(unittest.TestCase):

    def test_build_match_target_bash(self):
        """Bash call → returns the command string."""
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "forum reply 42"}}
        self.assertEqual(build_match_target(hook_input), "forum reply 42")

    def test_build_match_target_mcp(self):
        """MCP call → returns tool_name + compact JSON args."""
        hook_input = {
            "tool_name": "mcp__plugin_engram_engram__engram_add_observation",
            "tool_input": {"payload_json": "{}"},
        }
        result = build_match_target(hook_input)
        self.assertIn("mcp__plugin_engram_engram__engram_add_observation", result)
        self.assertIn("payload_json", result)

    def test_build_match_target_unknown(self):
        """Unknown tool_name (not Bash, not mcp__*) → returns empty string."""
        hook_input = {"tool_name": "SomeFutureTool", "tool_input": {}}
        self.assertEqual(build_match_target(hook_input), "")


class TestPreToolUseEnvelope(unittest.TestCase):

    def test_context_only_output_omits_permission_decision(self):
        """Lesson-tripwire context-only output must omit permissionDecision."""
        out = _run_hook_main(
            {"tool_name": "Bash", "tool_input": {"command": "forum reply 42"}},
            tripwires=[(r"forum reply", "Read body from stdin.", "ls_forumreply")],
        )
        self.assertTrue(out.strip(), "Expected JSON output")
        response = json.loads(out.strip())
        hook_specific = response["hookSpecificOutput"]
        self.assertEqual(hook_specific["hookEventName"], "PreToolUse")
        self.assertIn("lesson-tripwire", hook_specific["additionalContext"])
        self.assertNotIn(
            "permissionDecision",
            hook_specific,
            "Context-only PreToolUse output must omit permissionDecision",
        )


# ---------------------------------------------------------------------------
# Test 13: integration — situation_pattern stored via engram_add_lesson MCP tool
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
        patterns_in_tripwires = [p for p, _nudge, _node_id in tripwires]
        self.assertIn(pattern, patterns_in_tripwires, "Pattern must appear in load_tripwires output")
        # #2753 epic WS2: the 3rd tuple element is the firing node's own id
        # (the lesson_id minted above), so a caller can call
        # generalization_chain() on the actual hit.
        node_ids_in_tripwires = [nid for _p, _nudge, nid in tripwires]
        self.assertIn(lesson_id, node_ids_in_tripwires, "load_tripwires must carry the lesson's own node id")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# alpha#1944 step 2 — shadow-block logging
# ---------------------------------------------------------------------------

load_prevention_lessons = _hook.load_prevention_lessons
check_shadow_block = _hook.check_shadow_block


class TestLoadPreventionLessons(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_prevention_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = _make_db(Path(self.tmp))

    def test_advisory_lesson_excluded(self):
        _insert_lesson(self.db, pattern="rm -rf", prevention_type="advisory", node_id="ls_a")
        lessons = load_prevention_lessons(db_path=self.db)
        self.assertEqual(lessons, [])

    def test_absent_prevention_type_excluded(self):
        """A lesson predating #1944 (no prevention_type in metadata at all)
        must not be treated as a candidate — absence is advisory, not shadow."""
        _insert_lesson(self.db, pattern="rm -rf", node_id="ls_legacy")
        lessons = load_prevention_lessons(db_path=self.db)
        self.assertEqual(lessons, [])

    def test_shadow_lesson_included(self):
        _insert_lesson(self.db, pattern="rm -rf", prevention_type="shadow", node_id="ls_s")
        lessons = load_prevention_lessons(db_path=self.db)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["id"], "ls_s")
        self.assertEqual(lessons[0]["prevention_type"], "shadow")
        self.assertEqual(lessons[0]["pattern"], "rm -rf")

    def test_blocking_lesson_included(self):
        _insert_lesson(self.db, pattern="git push --force", prevention_type="blocking", node_id="ls_b")
        lessons = load_prevention_lessons(db_path=self.db)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["prevention_type"], "blocking")

    def test_shadow_lesson_without_pattern_excluded(self):
        _insert_lesson(self.db, prevention_type="shadow", node_id="ls_nopattern")
        lessons = load_prevention_lessons(db_path=self.db)
        self.assertEqual(lessons, [])

    def test_missing_db_returns_empty(self):
        lessons = load_prevention_lessons(db_path=Path(self.tmp) / "nonexistent.db")
        self.assertEqual(lessons, [])


class TestCheckShadowBlock(unittest.TestCase):

    def test_matching_shadow_lesson_produces_hit(self):
        lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow"}]
        hits = check_shadow_block(
            "rm -rf /tmp/whatever", lessons,
            {"tool_name": "Bash", "session_id": "sess-1"},
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["lesson_id"], "ls_s")
        self.assertEqual(hits[0]["prevention_type"], "shadow")
        self.assertEqual(hits[0]["matched_span"], "rm -rf")
        self.assertEqual(hits[0]["tool_name"], "Bash")
        self.assertEqual(hits[0]["session_id"], "sess-1")

    def test_non_matching_command_produces_no_hits(self):
        lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow"}]
        hits = check_shadow_block("ls -la", lessons, {"tool_name": "Bash"})
        self.assertEqual(hits, [])

    def test_multiple_matching_lessons_each_produce_a_hit(self):
        lessons = [
            {"id": "ls_s1", "pattern": "rm -rf", "prevention_type": "shadow"},
            {"id": "ls_b1", "pattern": "force", "prevention_type": "blocking"},
        ]
        hits = check_shadow_block("rm -rf --force", lessons, {"tool_name": "Bash"})
        self.assertEqual({h["lesson_id"] for h in hits}, {"ls_s1", "ls_b1"})

    def test_malformed_regex_skipped_without_raising(self):
        lessons = [{"id": "ls_bad", "pattern": "(unclosed", "prevention_type": "shadow"}]
        hits = check_shadow_block("(unclosed anything", lessons, {"tool_name": "Bash"})
        self.assertEqual(hits, [])

    def test_hook_input_none_does_not_raise(self):
        lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow"}]
        hits = check_shadow_block("rm -rf /tmp", lessons, None)
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0]["tool_name"])


class TestAppendPreventionLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_ledger_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def test_empty_hits_writes_nothing(self):
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            _hook._append_prevention_ledger(1234.0, [])
        self.assertFalse(self.ledger_path.exists())

    def test_hits_appended_as_jsonl(self):
        hits = [{
            "lesson_id": "ls_s", "prevention_type": "shadow",
            "matched_span": "rm -rf", "tool_name": "Bash", "session_id": "sess-1",
        }]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            _hook._append_prevention_ledger(1234.0, hits)
        lines = self.ledger_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["lesson_id"], "ls_s")
        self.assertEqual(entry["mode"], "shadow")
        self.assertEqual(entry["ts"], 1234.0)
        self.assertEqual(entry["matched_span"], "rm -rf")

    def test_multiple_hits_appended_as_separate_lines(self):
        hits = [
            {"lesson_id": "ls_a", "prevention_type": "shadow", "matched_span": "x",
             "tool_name": "Bash", "session_id": None},
            {"lesson_id": "ls_b", "prevention_type": "blocking", "matched_span": "y",
             "tool_name": "Bash", "session_id": None},
        ]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            _hook._append_prevention_ledger(1234.0, hits)
        lines = self.ledger_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_write_failure_does_not_raise(self):
        """Points PREVENTION_LEDGER_PATH at a directory (open() will fail) —
        the append must swallow the error, matching _append_ledger's
        never-raises contract."""
        bad_path = Path(self.tmp)  # a directory, not a file
        hits = [{"lesson_id": "ls_a", "prevention_type": "shadow", "matched_span": "x",
                  "tool_name": "Bash", "session_id": None}]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", bad_path):
            _hook._append_prevention_ledger(1234.0, hits)  # must not raise


class TestMainShadowBlockIntegration(unittest.TestCase):
    """main() end-to-end: shadow-block logging never changes the agent-visible
    additionalContext (that's step 3's job), and never breaks on a logging
    failure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_ledger_main_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def test_shadow_match_logs_but_output_unchanged_vs_no_shadow(self):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        prevention_lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow"}]

        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            out_without_shadow = _run_hook_main(hook_input, tripwires=tripwires)
            out_with_shadow = _run_hook_main(
                hook_input, tripwires=tripwires, prevention_lessons=prevention_lessons,
            )
        self.assertEqual(out_without_shadow, out_with_shadow)
        self.assertIn("Think twice before rm -rf.", out_with_shadow)

        # And the shadow fire WAS logged to the ledger.
        lines = self.ledger_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["lesson_id"], "ls_s")
        self.assertEqual(entry["mode"], "shadow")

    def test_ledger_write_failure_does_not_break_tripwire_output(self):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        prevention_lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow"}]
        bad_path = Path(self.tmp)  # a directory — forces the ledger write to fail

        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", bad_path):
            out = _run_hook_main(
                hook_input, tripwires=tripwires, prevention_lessons=prevention_lessons,
            )
        self.assertIn("Think twice before rm -rf.", out)


# ---------------------------------------------------------------------------
# alpha#1944 step 3 — deny path + override recipe
# ---------------------------------------------------------------------------

find_override = _hook._find_override
check_blocking_deny = _hook.check_blocking_deny
build_deny_reason = _hook.build_deny_reason


class TestFindOverride(unittest.TestCase):

    def test_bash_style_override_found(self):
        cmd = 'ENGRAM_OVERRIDE="ls_0018: verified this is safe" rm -rf /tmp/x'
        reason = find_override(cmd, "ls_0018")
        self.assertEqual(reason, "verified this is safe")

    def test_mcp_style_override_found(self):
        target = 'mcp__x__y {"override_lesson":"ls_0018: verified safe","claim":"z"}'
        reason = find_override(target, "ls_0018")
        self.assertEqual(reason, "verified safe")

    def test_no_override_present_returns_none(self):
        reason = find_override("rm -rf /tmp/x", "ls_0018")
        self.assertIsNone(reason)

    def test_override_for_a_different_lesson_id_not_matched(self):
        """An override naming a DIFFERENT lesson must not satisfy this one —
        an override is a deliberate, specific act, not a blanket bypass."""
        cmd = 'ENGRAM_OVERRIDE="ls_9999: wrong lesson" rm -rf /tmp/x'
        reason = find_override(cmd, "ls_0018")
        self.assertIsNone(reason)

    def test_malformed_lesson_id_does_not_raise(self):
        # lesson_id containing regex-special characters must be escaped
        # safely, not crash the search.
        reason = find_override("nothing here", "ls_(0018)")
        self.assertIsNone(reason)


class TestCheckBlockingDeny(unittest.TestCase):

    def test_shadow_lesson_never_denies(self):
        lessons = [{"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow", "nudge": "careful"}]
        deny = check_blocking_deny("rm -rf /tmp/x", lessons)
        self.assertIsNone(deny)

    def test_blocking_lesson_without_override_denies(self):
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        deny = check_blocking_deny("rm -rf /tmp/x", lessons)
        self.assertIsNotNone(deny)
        self.assertEqual(deny["lesson_id"], "ls_b")
        self.assertEqual(deny["matched_span"], "rm -rf")
        self.assertEqual(deny["nudge"], "careful")

    def test_blocking_lesson_with_matching_override_does_not_deny(self):
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        cmd = 'ENGRAM_OVERRIDE="ls_b: verified safe" rm -rf /tmp/x'
        deny = check_blocking_deny(cmd, lessons)
        self.assertIsNone(deny)

    def test_blocking_lesson_with_wrong_lesson_override_still_denies(self):
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        cmd = 'ENGRAM_OVERRIDE="ls_other: wrong" rm -rf /tmp/x'
        deny = check_blocking_deny(cmd, lessons)
        self.assertIsNotNone(deny)

    def test_non_matching_command_no_deny(self):
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        deny = check_blocking_deny("ls -la", lessons)
        self.assertIsNone(deny)

    def test_malformed_regex_skipped_no_deny(self):
        lessons = [{"id": "ls_bad", "pattern": "(unclosed", "prevention_type": "blocking", "nudge": "x"}]
        deny = check_blocking_deny("(unclosed anything", lessons)
        self.assertIsNone(deny)

    def test_advisory_and_shadow_and_blocking_mixed_denies_only_on_blocking(self):
        lessons = [
            {"id": "ls_shadow", "pattern": "rm -rf", "prevention_type": "shadow", "nudge": "x"},
            {"id": "ls_block", "pattern": "force", "prevention_type": "blocking", "nudge": "y"},
        ]
        deny = check_blocking_deny("rm -rf --force", lessons)
        self.assertEqual(deny["lesson_id"], "ls_block")


class TestBuildDenyReason(unittest.TestCase):

    def test_reason_contains_lesson_id_nudge_and_recipe(self):
        deny = {"lesson_id": "ls_0018", "matched_span": "rm -rf", "nudge": "Think twice."}
        reason = build_deny_reason(deny)
        self.assertIn("ls_0018", reason)
        self.assertIn("Think twice.", reason)
        self.assertIn("ENGRAM_OVERRIDE=\"ls_0018:", reason)
        self.assertIn("override_lesson", reason)
        self.assertIn("single-call-scoped", reason)


class TestMainDenyIntegration(unittest.TestCase):
    """main() end-to-end: a blocking match without override produces a
    permissionDecision:deny response and nothing else; with a valid
    override it produces the normal advisory response instead."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_deny_main_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def test_blocking_match_without_override_denies(self):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        prevention_lessons = [
            {"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "Think twice."}
        ]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            out = _run_hook_main(hook_input, prevention_lessons=prevention_lessons)
        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("ls_b", hso["permissionDecisionReason"])
        self.assertIn("Think twice.", hso["permissionDecisionReason"])
        self.assertNotIn("additionalContext", hso)

    def test_blocking_match_with_override_allows_and_advisory_still_shows(self):
        hook_input = {
            "tool_name": "Bash",
            "tool_input": {"command": 'ENGRAM_OVERRIDE="ls_b: verified safe" rm -rf /tmp/x'},
        }
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        prevention_lessons = [
            {"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "Think twice."}
        ]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            out = _run_hook_main(
                hook_input, tripwires=tripwires, prevention_lessons=prevention_lessons,
            )
        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])

        # The override fire is still logged, with override_present=True.
        lines = self.ledger_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["lesson_id"], "ls_b")
        self.assertTrue(entry["override_present"])
        self.assertEqual(entry["override_reason"], "verified safe")

    def test_deny_computation_failure_fails_open(self):
        """If load_prevention_lessons itself raises, the call must NOT be
        denied — infra failure is never a reason to block (Kepler's design
        point 5: the only reachable failure mode is loud-advisory)."""
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        output_buf = io.StringIO()
        with (
            unittest.mock.patch.object(_hook, "load_tripwires", return_value=tripwires),
            unittest.mock.patch.object(_hook, "load_prevention_lessons", side_effect=RuntimeError("db down")),
            unittest.mock.patch.object(_hook, "check_in_turn_recall", return_value=""),
            unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
            unittest.mock.patch("sys.stdout", output_buf),
        ):
            try:
                _hook.main()
            except SystemExit:
                pass
        out = output_buf.getvalue()
        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])

    def test_shadow_match_never_denies_end_to_end(self):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        prevention_lessons = [
            {"id": "ls_s", "pattern": "rm -rf", "prevention_type": "shadow", "nudge": "Think twice."}
        ]
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            out = _run_hook_main(
                hook_input, tripwires=tripwires, prevention_lessons=prevention_lessons,
            )
        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])


# ---------------------------------------------------------------------------
# alpha#1944 step 4 — auto-demotion (read-time lease, Kepler's design)
# ---------------------------------------------------------------------------

demotion_config = _hook._demotion_config
count_recent_overrides = _hook._count_recent_overrides


class TestDemotionConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_democonfig_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_config(self, config: dict) -> Path:
        p = Path(self.tmp) / "config.json"
        p.write_text(json.dumps(config))
        return p

    def test_default_when_no_config_file(self):
        with unittest.mock.patch.object(_hook, "ENGRAM_HOME", str(Path(self.tmp) / "nonexistent")):
            cfg = demotion_config()
        self.assertEqual(cfg, {"overrides": 2, "window_days": 7})

    def test_default_when_key_absent(self):
        self._write_config({"some_other_key": True})
        with unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp):
            cfg = demotion_config()
        self.assertEqual(cfg, {"overrides": 2, "window_days": 7})

    def test_explicit_config_overrides_default(self):
        self._write_config({"lessons": {"blocking_demotion": {"overrides": 5, "window_days": 14}}})
        with unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp):
            cfg = demotion_config()
        self.assertEqual(cfg, {"overrides": 5, "window_days": 14})

    def test_malformed_config_falls_back_to_default(self):
        p = Path(self.tmp) / "config.json"
        p.write_text("{not-json")
        with unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp):
            cfg = demotion_config()
        self.assertEqual(cfg, {"overrides": 2, "window_days": 7})


class TestCountRecentOverrides(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_countoverrides_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def _write_ledger(self, entries: list) -> None:
        with open(self.ledger_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_missing_ledger_returns_zero(self):
        count = count_recent_overrides("ls_b", 7, 1_000_000.0)
        self.assertEqual(count, 0)

    def test_counts_only_matching_lesson_with_override(self):
        self._write_ledger([
            {"lesson_id": "ls_b", "override_present": True, "ts": 999_000.0},
            {"lesson_id": "ls_b", "override_present": False, "ts": 999_000.0},
            {"lesson_id": "ls_other", "override_present": True, "ts": 999_000.0},
        ])
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            count = count_recent_overrides("ls_b", 7, 1_000_000.0)
        self.assertEqual(count, 1)

    def test_excludes_entries_outside_window(self):
        now = 1_000_000.0
        seven_days = 7 * 86400
        self._write_ledger([
            {"lesson_id": "ls_b", "override_present": True, "ts": now - seven_days + 10},  # inside
            {"lesson_id": "ls_b", "override_present": True, "ts": now - seven_days - 10},  # outside
        ])
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            count = count_recent_overrides("ls_b", 7, now)
        self.assertEqual(count, 1)

    def test_corrupt_line_skipped_without_raising(self):
        with open(self.ledger_path, "w") as f:
            f.write("not json at all\n")
            f.write(json.dumps({"lesson_id": "ls_b", "override_present": True, "ts": 999_000.0}) + "\n")
        with unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path):
            count = count_recent_overrides("ls_b", 7, 1_000_000.0)
        self.assertEqual(count, 1)


class TestCheckBlockingDenyWithDemotion(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_denydemote_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def _write_ledger(self, entries: list) -> None:
        with open(self.ledger_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_below_threshold_denies_normally(self):
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        with (
            unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path),
            unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp),
        ):
            deny = check_blocking_deny("rm -rf /tmp/x", lessons, now=1_000_000.0)
        self.assertIsNotNone(deny)
        self.assertFalse(deny["demoted"])

    def test_at_threshold_reports_demoted_not_a_real_deny(self):
        self._write_ledger([
            {"lesson_id": "ls_b", "override_present": True, "ts": 999_000.0},
            {"lesson_id": "ls_b", "override_present": True, "ts": 999_100.0},
        ])
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        with (
            unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path),
            unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp),
        ):
            deny = check_blocking_deny("rm -rf /tmp/x", lessons, now=1_000_000.0)
        self.assertIsNotNone(deny)
        self.assertTrue(deny["demoted"])
        self.assertEqual(deny["override_count"], 2)
        self.assertEqual(deny["threshold"], 2)

    def test_override_this_call_skips_demotion_reporting_too(self):
        """An override present on THIS call takes priority — never reported
        as either a real deny or a demotion, just allowed."""
        self._write_ledger([
            {"lesson_id": "ls_b", "override_present": True, "ts": 999_000.0},
            {"lesson_id": "ls_b", "override_present": True, "ts": 999_100.0},
        ])
        lessons = [{"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "careful"}]
        cmd = 'ENGRAM_OVERRIDE="ls_b: verified safe" rm -rf /tmp/x'
        with (
            unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path),
            unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp),
        ):
            deny = check_blocking_deny(cmd, lessons, now=1_000_000.0)
        self.assertIsNone(deny)


class TestMainDemotionIntegration(unittest.TestCase):
    """main() end-to-end: once a blocking lesson has crossed its demotion
    threshold, further matching calls are NOT denied — they fall through
    to the normal advisory path, with a loud ledger entry recording it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_demote_main_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = Path(self.tmp) / "lesson-prevention-ledger.jsonl"

    def _write_ledger(self, entries: list) -> None:
        with open(self.ledger_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_demoted_lesson_no_longer_denies(self):
        import time
        now = time.time()
        self._write_ledger([
            {"lesson_id": "ls_b", "override_present": True, "ts": now - 10},
            {"lesson_id": "ls_b", "override_present": True, "ts": now - 5},
        ])
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [("rm -rf", "Think twice before rm -rf.", "ls_rmrf")]
        prevention_lessons = [
            {"id": "ls_b", "pattern": "rm -rf", "prevention_type": "blocking", "nudge": "Think twice."}
        ]
        with (
            unittest.mock.patch.object(_hook, "PREVENTION_LEDGER_PATH", self.ledger_path),
            unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.tmp),
        ):
            out = _run_hook_main(
                hook_input, tripwires=tripwires, prevention_lessons=prevention_lessons,
            )
        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])

        # A distinct auto_demoted_lease entry was appended.
        lines = [json.loads(l) for l in self.ledger_path.read_text().strip().splitlines()]
        modes = [l.get("mode") for l in lines]
        self.assertIn("auto_demoted_lease", modes)
