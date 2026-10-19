"""Tests for tools/lesson-pattern-audit.py (engram-alpha#2018).

The tool itself is Ariadne's -- designed, battle-tested, and cross-checked
against two agents' graphs before landing. These tests cover the landing
changes (import-not-reimplement of build_match_target, and the new CLI
flags) plus a genuine regression test against the exact failure shape one
of the tool's own bugs took (BUG 4 in the tool's docstring: a broken patch
reported every target as prior-session and passed clean).

Coverage:
  1. test_build_match_target_bash_via_import / _mcp — import sanity: the
     tool imports the real build_match_target from the hook file and it
     behaves as expected for both loci.
  2. test_session_split_produces_both_buckets — regression test for BUG 4's
     failure shape: a fixture with real matches in both the "current" and
     "prior" transcript must not collapse to one bucket.
  3. test_corpus_introspection_filters_target — a target string containing
     a CORPUS_INTROSPECTION literal is excluded from both buckets.
  4. test_cli_flags_wire_through — --db / --transcript-glob / --session /
     --transcripts each override their default and the CLI plumbing
     actually reaches collect_targets/run_audit.
  5. test_broken_pattern_reported_not_raised — a malformed situation_pattern
     is reported BROKEN, does not crash the run.
  6. test_empty_inventory_exits_2 — no matching transcripts -> exit code 2,
     stderr message, no crash.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = PROJECT_ROOT / "tools" / "lesson-pattern-audit.py"


def _import_audit():
    spec = importlib.util.spec_from_file_location("lesson_pattern_audit", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_audit = _import_audit()
build_match_target = _audit.build_match_target
collect_targets = _audit.collect_targets
run_audit = _audit.run_audit
main = _audit.main


# ---------------------------------------------------------------------------
# Fixture helpers
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
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_MINIMAL_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _insert_lesson(db: Path, *, node_id: str, pattern: str, claim: str = "test lesson"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, 'lesson', 1, 'active', ?, ?)",
        (node_id, json.dumps({"situation_pattern": pattern}), claim),
    )
    conn.commit()
    conn.close()


def _bash_entry(command: str) -> dict:
    """One transcript line shaped like a real assistant tool_use event."""
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]
        },
    }


def _write_transcript(path: Path, entries: list[dict]):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# 1. Import sanity
# ---------------------------------------------------------------------------

class TestImportBuildMatchTarget(unittest.TestCase):
    def test_build_match_target_bash_via_import(self):
        result = build_match_target({"tool_name": "Bash", "input": {"command": "ls -la"}})
        self.assertEqual(result, "ls -la")

    def test_build_match_target_mcp_via_import(self):
        result = build_match_target(
            {"tool_name": "mcp__plugin_engram_engram__engram_add_observation",
             "input": {"payload_json": "{}"}})
        self.assertIn("mcp__plugin_engram_engram__engram_add_observation", result)
        self.assertIn("payload_json", result)


# ---------------------------------------------------------------------------
# 2. Session split regression (BUG 4's failure shape)
# ---------------------------------------------------------------------------

class TestSessionSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_split_produces_both_buckets(self):
        """A fixture with the SAME real match string present in both a
        'current' and a 'prior' transcript must land in BOTH buckets, not
        collapse to one. This is a regression test against BUG 4's exact
        failure shape: a broken patch once reported every target as
        prior-session and passed clean -- i.e. cur ended up empty while
        prior silently absorbed everything (or vice versa)."""
        match_str = "widget-op run-diagnostic-xyz"
        current_file = self.tmp / "session_CURRENT.jsonl"
        prior_file = self.tmp / "session_PRIOR.jsonl"
        _write_transcript(current_file, [_bash_entry(match_str)])
        _write_transcript(prior_file, [_bash_entry(match_str)])

        cur, prior, srcs = collect_targets(
            transcript_glob=str(self.tmp / "*.jsonl"),
            limit_files=6,
            session="session_CURRENT",
        )

        self.assertEqual(len(srcs), 2)
        self.assertTrue(cur, "current-session bucket must not be empty")
        self.assertTrue(prior, "prior-session bucket must not be empty")
        self.assertIn(match_str, cur)
        self.assertIn(match_str, prior)


# ---------------------------------------------------------------------------
# 3. CORPUS_INTROSPECTION filtering
# ---------------------------------------------------------------------------

class TestCorpusIntrospectionFiltering(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_corpus_introspection_filters_target(self):
        introspective_cmd = "grep situation_pattern ~/.engram/knowledge.db"
        ordinary_cmd = "widget-op run-diagnostic-xyz"
        f = self.tmp / "session_ONE.jsonl"
        _write_transcript(f, [_bash_entry(introspective_cmd), _bash_entry(ordinary_cmd)])

        cur, prior, srcs = collect_targets(
            transcript_glob=str(self.tmp / "*.jsonl"),
            limit_files=6,
            session="session_ONE",
        )

        all_targets = cur + prior
        self.assertNotIn(introspective_cmd, all_targets,
                          "CORPUS_INTROSPECTION literal must be filtered out")
        self.assertIn(ordinary_cmd, all_targets,
                       "non-introspective target must survive the filter")


# ---------------------------------------------------------------------------
# 3b. Malformed "message" field is skipped, not raised
# ---------------------------------------------------------------------------

class TestMalformedMessageField(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_dict_message_field_is_skipped(self):
        """A transcript line whose 'message' field is a non-dict truthy
        value (e.g. a bare string) must be skipped, not raise AttributeError
        from calling .get() on it. A well-formed entry in the same file must
        still be collected."""
        ordinary_cmd = "widget-op run-diagnostic-xyz"
        f = self.tmp / "session_ONE.jsonl"
        _write_transcript(f, [
            {"type": "assistant", "message": "not-a-dict"},
            _bash_entry(ordinary_cmd),
        ])

        cur, prior, srcs = collect_targets(
            transcript_glob=str(self.tmp / "*.jsonl"),
            limit_files=6,
            session="session_ONE",
        )

        all_targets = cur + prior
        self.assertIn(ordinary_cmd, all_targets,
                       "well-formed entry must still be collected")


# ---------------------------------------------------------------------------
# 4. CLI flags wire through
# ---------------------------------------------------------------------------

class TestCliFlagsWireThrough(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="ls_test1", pattern=r"widget-op",
                        claim="widget-op lesson")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_flags_wire_through(self):
        match_str = "widget-op run-diagnostic-xyz"
        current_file = self.tmp / "session_CURRENT.jsonl"
        older_file = self.tmp / "session_OLDER.jsonl"
        _write_transcript(current_file, [_bash_entry(match_str)])
        _write_transcript(older_file, [_bash_entry(match_str)])

        argv = [
            "lesson-pattern-audit.py",
            "--db", str(self.db),
            "--transcript-glob", str(self.tmp / "*.jsonl"),
            "--transcripts", "2",
            "--session", "session_CURRENT",
        ]
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = argv
        try:
            with redirect_stdout(buf):
                rc = main()
        finally:
            sys.argv = old_argv

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("2 transcripts", out)
        self.assertIn("lessons: 1", out)
        self.assertIn("[ls_test1]", out)
        self.assertIn("LIVE", out)

    def test_transcripts_flag_limits_file_count(self):
        """--transcripts N caps how many files collect_targets reads."""
        for i in range(4):
            _write_transcript(self.tmp / f"session_{i}.jsonl",
                               [_bash_entry("widget-op something")])
        cur, prior, srcs = collect_targets(
            transcript_glob=str(self.tmp / "*.jsonl"), limit_files=2)
        self.assertEqual(len(srcs), 2)


# ---------------------------------------------------------------------------
# 5. BROKEN pattern handling
# ---------------------------------------------------------------------------

class TestBrokenPattern(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="ls_broken", pattern="[",
                        claim="broken pattern lesson")
        _write_transcript(self.tmp / "session_ONE.jsonl",
                           [_bash_entry("some ordinary command")])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_broken_pattern_reported_not_raised(self):
        out = io.StringIO()
        err = io.StringIO()
        rc = run_audit(db_path=str(self.db),
                        transcript_glob=str(self.tmp / "*.jsonl"),
                        limit_files=6, session="session_ONE",
                        out=out, err=err)
        self.assertEqual(rc, 0)
        report = out.getvalue()
        self.assertIn("[ls_broken]", report)
        self.assertIn("BROKEN", report)


# ---------------------------------------------------------------------------
# 6. Empty inventory
# ---------------------------------------------------------------------------

class TestEmptyInventory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_test_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="ls_any", pattern="anything",
                        claim="any lesson")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_inventory_exits_2(self):
        out = io.StringIO()
        err = io.StringIO()
        rc = run_audit(db_path=str(self.db),
                        transcript_glob=str(self.tmp / "no_such_dir" / "*.jsonl"),
                        limit_files=6, out=out, err=err)
        self.assertEqual(rc, 2)
        self.assertIn("no transcript-derived targets", err.getvalue())
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 7. alpha#3920: --compare-views
# ---------------------------------------------------------------------------

class TestCompareViews(unittest.TestCase):
    """A lesson that matches a command still matches it on the command view; a lesson that only
    matched prose inside a heredoc reports a lost match (REVIEW). Reports, never writes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_views_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="ls_slice", pattern=r"\bsed\s+-n\b")
        _insert_lesson(self.db, node_id="ls_plain", pattern=r"\bgit\s+status\b")
        _write_transcript(self.tmp / "s1.jsonl", [
            _bash_entry("cat <<'EOF'\nuse sed -n to slice\nEOF"),
            _bash_entry("sed -n 1,20p file"),
            _bash_entry("git status"),
        ])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        code = _audit.run_view_comparison(db_path=str(self.db),
                                          transcript_glob=str(self.tmp / "*.jsonl"),
                                          limit_files=3, out=buf)
        return code, buf.getvalue()

    def test_lost_match_is_reported_for_the_lesson_that_matched_prose(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("[ls_slice] REVIEW", out)
        self.assertIn("full=2 command=1 lost=1", out)

    def test_lesson_without_match_scope_is_listed_as_full(self):
        _, out = self._run()
        self.assertIn("[ls_slice] REVIEW scope=full", out)

    def test_a_lesson_with_no_lost_match_is_ok(self):
        _, out = self._run()
        self.assertIn("[ls_plain] ok", out)
        self.assertIn("full=1 command=1 lost=0", out)

    def test_collect_targets_pairs_gives_full_and_command(self):
        cur, prior, _ = _audit.collect_targets(transcript_glob=str(self.tmp / "*.jsonl"),
                                               limit_files=3, pairs=True)
        pairs = cur + prior
        self.assertTrue(all(isinstance(p, tuple) and len(p) == 2 for p in pairs))
        heredoc = [p for p in pairs if "use sed -n to slice" in p[0]][0]
        self.assertNotIn("use sed -n to slice", heredoc[1])

    def test_default_collect_targets_is_unchanged(self):
        cur, prior, _ = _audit.collect_targets(transcript_glob=str(self.tmp / "*.jsonl"), limit_files=3)
        self.assertTrue(all(isinstance(t, str) for t in cur + prior))

    def test_empty_inventory_exits_2(self):
        buf, err = io.StringIO(), io.StringIO()
        code = _audit.run_view_comparison(db_path=str(self.db),
                                          transcript_glob=str(self.tmp / "none*.jsonl"),
                                          out=buf, err=err)
        self.assertEqual(code, 2)
        self.assertIn("empty inventory", err.getvalue())
