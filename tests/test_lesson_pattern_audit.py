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

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


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


class TestWriteRanks(unittest.TestCase):
    """alpha#3924: --write-ranks writes fires-per-1,000 targets per armed lesson to the rank file
    the tripwire hook reads for its cluster-block ordering."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_ranks_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="lsx-001", pattern=r"gh pr create")
        _insert_lesson(self.db, node_id="lsx-002", pattern=r"git status")
        _insert_lesson(self.db, node_id="lsx-003", pattern=r"never-matches-anything")
        _insert_lesson(self.db, node_id="lsx-004", pattern="[broken")
        _insert_lesson(self.db, node_id="lsx-005", pattern="")
        _write_transcript(self.tmp / "s1.jsonl", [
            _bash_entry("gh pr create a"), _bash_entry("gh pr create b"),
            _bash_entry("git status"), _bash_entry("ls"),
        ])
        self.out = self.tmp / "ranks.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, path=None):
        err = io.StringIO()
        code = _audit.run_write_ranks(db_path=str(self.db), transcript_glob=str(self.tmp / "s1.jsonl"),
                                      limit_files=3, path=str(path or self.out), out=io.StringIO(), err=err)
        return code, err.getvalue()

    def test_rates_are_fires_per_thousand_targets(self):
        code, _ = self._run()
        self.assertEqual(code, 0)
        doc = json.loads(self.out.read_text())
        self.assertEqual(doc["targets"], 4)
        self.assertEqual(doc["ranks"], {"lsx-001": 500.0, "lsx-002": 250.0, "lsx-003": 0.0})
        self.assertIsInstance(doc["window"], str)
        from datetime import datetime
        datetime.fromisoformat(doc["generated_at"].replace("Z", "+00:00"))

    def test_unarmed_and_broken_lessons_are_absent(self):
        self._run()
        ranks = json.loads(self.out.read_text())["ranks"]
        self.assertNotIn("lsx-004", ranks)
        self.assertNotIn("lsx-005", ranks)

    def test_command_scope_lesson_is_counted_on_the_command_view(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
                     "VALUES ('lsx-006', 'lesson', 1, 'active', ?, 'c')",
                     (json.dumps({"situation_pattern": r"\bsed\s+-n\b", "match_scope": "command"}),))
        conn.execute("INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
                     "VALUES ('lsx-007', 'lesson', 1, 'active', ?, 'c')",
                     (json.dumps({"situation_pattern": r"\bsed\s+-n\b"}),))
        conn.commit()
        conn.close()
        _write_transcript(self.tmp / "s1.jsonl", [
            _bash_entry("cat <<'EOF'\nuse sed -n to slice\nEOF"), _bash_entry("sed -n 1,2p f"),
            _bash_entry("ls"), _bash_entry("pwd"),
        ])
        self._run()
        ranks = json.loads(self.out.read_text())["ranks"]
        self.assertEqual(ranks["lsx-006"], 250.0)   # command view: the heredoc prose is hidden
        self.assertEqual(ranks["lsx-007"], 500.0)   # full view: both match

    def test_a_bad_metadata_row_is_skipped_not_fatal(self):
        """Review round 1 (PR 3948): metadata 'null', a JSON list, or a non-string pattern used to
        raise and abort the whole rank file (the hook then falls back to length order after 7d)."""
        conn = sqlite3.connect(str(self.db))
        for nid, md in (("lsx-bad1", "null"), ("lsx-bad2", "[1, 2]"),
                        ("lsx-bad3", json.dumps({"situation_pattern": 7})),
                        ("lsx-bad4", json.dumps({"situation_pattern": ["a"]})), ("lsx-bad5", "not json")):
            conn.execute("INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
                         "VALUES (?, 'lesson', 1, 'active', ?, 'c')", (nid, md))
        conn.commit()
        conn.close()
        code, _ = self._run()
        self.assertEqual(code, 0)
        ranks = json.loads(self.out.read_text())["ranks"]
        self.assertEqual(ranks["lsx-001"], 500.0)
        self.assertFalse([k for k in ranks if k.startswith("lsx-bad")])

    def test_a_symlink_at_the_target_is_followed_not_replaced(self):
        real = self.tmp / "real.json"
        real.write_text("{}")
        link = self.tmp / "link.json"
        os.symlink(real, link)
        code, _ = self._run(path=link)
        self.assertEqual(code, 0)
        self.assertTrue(link.is_symlink(), "the link itself must survive")
        self.assertIn("lsx-001", json.loads(real.read_text())["ranks"])

    def test_whole_file_is_rewritten_not_merged(self):
        self.out.write_text(json.dumps({"generated_at": "x", "ranks": {"lsx-gone": 9.0}, "extra": 1}))
        self._run()
        doc = json.loads(self.out.read_text())
        self.assertNotIn("lsx-gone", doc["ranks"])
        self.assertNotIn("extra", doc)

    def test_new_file_mode_is_0o640(self):
        self._run()
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o640)

    def test_existing_mode_is_preserved(self):
        for mode in (0o640, 0o664, 0o600):
            self.out.write_text("{}")
            os.chmod(self.out, mode)
            self._run()
            self.assertEqual(self.out.stat().st_mode & 0o777, mode, oct(mode))

    def test_acl_step_failure_does_not_fail_the_run(self):
        def boom(*a, **k):
            raise OSError("ACLs unsupported")
        orig = _audit._preserve_acl
        _audit._preserve_acl = boom
        try:
            self.out.write_text("{}")
            code, _ = self._run()
        finally:
            _audit._preserve_acl = orig
        self.assertEqual(code, 0)
        self.assertIn("lsx-001", json.loads(self.out.read_text())["ranks"])

    def test_no_temp_files_left_behind(self):
        self._run()
        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(names, sorted(["knowledge.db", "s1.jsonl", "ranks.json"]))

    def test_empty_inventory_exits_0_says_so_and_writes_nothing(self):
        # alpha#3941: a recurring caller (engram-sleep) must not read "no transcripts" as a failure, and an
        # empty rank file would read as "every lesson is rare".
        err, out = io.StringIO(), io.StringIO()
        code = _audit.run_write_ranks(db_path=str(self.db), transcript_glob=str(self.tmp / "none*.jsonl"),
                                      path=str(self.out), out=out, err=err)
        self.assertEqual(code, 0)
        self.assertIn("no transcripts found", out.getvalue())
        self.assertFalse(self.out.exists())

    def test_cli_flag_with_explicit_path(self):
        orig = sys.argv
        sys.argv = ["lesson-pattern-audit", "--db", str(self.db), "--transcript-glob",
                    str(self.tmp / "s1.jsonl"), "--write-ranks", str(self.out)]
        try:
            with redirect_stdout(io.StringIO()):
                code = main()
        finally:
            sys.argv = orig
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.out.read_text())["targets"], 4)

    def test_cli_flag_default_path_is_under_engram_home(self):
        home = self.tmp / "home"
        home.mkdir()
        orig_argv, orig_env = sys.argv, os.environ.get("ENGRAM_HOME")
        os.environ["ENGRAM_HOME"] = str(home)
        sys.argv = ["lesson-pattern-audit", "--db", str(self.db), "--transcript-glob",
                    str(self.tmp / "s1.jsonl"), "--write-ranks"]
        try:
            with redirect_stdout(io.StringIO()):
                code = main()
        finally:
            sys.argv = orig_argv
            if orig_env is None:
                os.environ.pop("ENGRAM_HOME", None)
            else:
                os.environ["ENGRAM_HOME"] = orig_env
        self.assertEqual(code, 0)
        self.assertTrue((home / "lesson-fire-ranks.json").exists())


# ---------------------------------------------------------------------------
# 8. alpha#4396: --write-ranks ranks from MAIN-session transcripts only
# ---------------------------------------------------------------------------

class TestWriteRanksMainSessionOnly(unittest.TestCase):
    """alpha#4396: sub-agent (fairy) transcripts live under a `subagents/` directory and must not feed
    the rank file; the coverage audit still reads them (a fairy's calls are real calls)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_audit_mainonly_"))
        self.db = _make_db(self.tmp)
        _insert_lesson(self.db, node_id="lsx-001", pattern=r"main-only-verb")
        _insert_lesson(self.db, node_id="lsy-001", pattern=r"fairy-only-verb")
        self.root = self.tmp / "tree"
        self.out = self.tmp / "ranks.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _put(self, rel, commands, mtime):
        """Write a transcript at root/rel with an explicit mtime (order never depends on write timing)."""
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_transcript(p, [_bash_entry(c) for c in commands])
        os.utime(p, (mtime, mtime))
        return p

    def _tree_x_and_y(self):
        self._put("proj/main.jsonl", ["main-only-verb 1", "ls"], 1_000_000)
        self._put("proj/sess/subagents/agent-1.jsonl", ["fairy-only-verb 1", "fairy-only-verb 2"], 2_000_000)

    def _glob(self):
        return str(self.root / "**" / "*.jsonl")

    def _write_ranks(self, **kw):
        out = io.StringIO()
        code = _audit.run_write_ranks(db_path=str(self.db), transcript_glob=self._glob(),
                                      path=str(self.out), out=out, err=io.StringIO(), **kw)
        return code, out.getvalue()

    def test_t1_subagent_calls_do_not_feed_the_ranks(self):
        """T1. Y matches only a NEWER sub-agent file: its rank is 0.0, X's (main file) is > 0.0.
        Red for m3 (run_write_ranks not passing the flag) and m5 (filter dropped): both give Y > 0."""
        self._tree_x_and_y()
        code, _ = self._write_ranks()
        self.assertEqual(code, 0)
        doc = json.loads(self.out.read_text())
        self.assertEqual(doc["ranks"]["lsy-001"], 0.0)
        self.assertGreater(doc["ranks"]["lsx-001"], 0.0)
        self.assertEqual(doc["targets"], 2)  # main.jsonl only

    def test_t2_default_still_reads_subagents_and_audit_counts_them(self):
        """T2. Default exclude_subagents=False keeps every file: collect_targets returns the sub-agent
        call, and run_audit still sees the Y match. Red for m4 (default flipped to True)."""
        self._tree_x_and_y()
        cur, prior, srcs = collect_targets(transcript_glob=self._glob(), limit_files=6)
        self.assertIn("fairy-only-verb 1", cur + prior)
        self.assertEqual(len(srcs), 2)
        # pairs=True with the default keyword must not start filtering (red for k4: default following `pairs`)
        pcur, pprior, psrcs = collect_targets(transcript_glob=self._glob(), limit_files=6, pairs=True)
        self.assertTrue(any("fairy-only-verb 1" in full for full, _cmd in pcur + pprior))
        self.assertEqual(len(psrcs), 2)
        out = io.StringIO()
        rc = run_audit(db_path=str(self.db), transcript_glob=self._glob(), limit_files=6,
                       out=out, err=io.StringIO())
        self.assertEqual(rc, 0)
        report = out.getvalue()
        self.assertIn("[lsy-001] LIVE", report)
        self.assertNotIn("[lsy-001] DEAD", report)

    def test_t3_filter_applies_before_the_window_limit(self):
        """T3. 7 older main files + 3 newer sub-agent files, limit 6: exactly the 6 newest MAIN files.
        Red for m1 (filter after the slice: would return only 3 main files) and m5."""
        for i in range(7):
            self._put(f"proj/main-{i}.jsonl", [f"main-only-verb {i}"], 1_000_000 + i)
        for i in range(3):
            self._put(f"proj/sess/subagents/agent-{i}.jsonl", [f"fairy-only-verb {i}"], 2_000_000 + i)
        _cur, _prior, srcs = collect_targets(transcript_glob=self._glob(), limit_files=6,
                                             exclude_subagents=True)
        self.assertEqual(sorted(os.path.basename(f) for f in srcs),
                         sorted(f"main-{i}.jsonl" for i in range(1, 7)))

    def test_t4_only_subagent_files_writes_nothing(self):
        """T4. A tree of sub-agent files only: the "no transcripts found" line, exit 0, an existing rank
        file untouched byte for byte. Red for m3 and m5 (the file would be rewritten)."""
        self._put("proj/sess/subagents/agent-1.jsonl", ["fairy-only-verb 1"], 2_000_000)
        self.out.write_bytes(b'{"ranks": {"keep": 1.0}}\n')
        before = self.out.read_bytes()
        code, said = self._write_ranks()
        self.assertEqual(code, 0)
        self.assertIn("no transcripts found; rank file left as is", said)
        self.assertEqual(self.out.read_bytes(), before)

    def test_t5_path_component_not_filename_substring(self):
        """T5. `my-subagents-notes.jsonl` (a file NAME containing the word) is KEPT; files under a
        directory named exactly `subagents`, at depth 2 and 3, are DROPPED, and so is a file whose
        `subagents` directory is NOT its direct parent (`subagents/nested/`). Red for m2 (substring
        match on the name drops the notes file), m5, and k2 (direct-parent test only)."""
        self._put("my-subagents-notes.jsonl", ["main-only-verb n"], 1_000_000)
        self._put("proj/subagents/a.jsonl", ["fairy-only-verb a"], 1_000_001)
        self._put("proj/sess/subagents/b.jsonl", ["fairy-only-verb b"], 1_000_002)
        self._put("proj/sess/subagents/nested/agent-9.jsonl", ["fairy-only-verb c"], 1_000_003)
        _cur, _prior, srcs = collect_targets(transcript_glob=self._glob(), limit_files=6,
                                             exclude_subagents=True)
        self.assertEqual([os.path.basename(f) for f in srcs], ["my-subagents-notes.jsonl"])

    def test_t6_compare_views_still_reads_subagent_traffic(self):
        """T6. Over the T1 tree, --compare-views (run_view_comparison) still includes the sub-agent
        calls: 4 targets, and the Y lesson (matching only the sub-agent file) sees both of its calls.
        Red for k3 (run_view_comparison also passing the flag) and k4 (default following `pairs`)."""
        self._tree_x_and_y()
        buf = io.StringIO()
        code = _audit.run_view_comparison(db_path=str(self.db), transcript_glob=self._glob(),
                                          limit_files=6, out=buf)
        self.assertEqual(code, 0)
        report = buf.getvalue()
        self.assertIn("targets: 4 ", report)
        self.assertIn("2 transcripts)", report)
        y_line = [ln for ln in report.splitlines() if ln.startswith("[lsy-001]")][0]
        self.assertIn("full=2", y_line)

    def _under_subagents_root(self, glob_tail):
        """A tree whose own root is a directory named `subagents`: main.jsonl at proj/, a real
        sub-agent file at proj/sess/subagents/. Returns the glob built from glob_tail."""
        self.root = self.tmp / "subagents"
        self._put("proj/main.jsonl", ["main-only-verb 1", "ls"], 1_000_000)
        self._put("proj/sess/subagents/agent-1.jsonl", ["fairy-only-verb 1"], 2_000_000)
        return str(self.root / glob_tail)

    def _assert_root_named_subagents(self, pattern):
        _cur, _prior, srcs = collect_targets(transcript_glob=pattern, limit_files=6, exclude_subagents=True)
        self.assertEqual([os.path.basename(f) for f in srcs], ["main.jsonl"])
        out = io.StringIO()
        code = _audit.run_write_ranks(db_path=str(self.db), transcript_glob=pattern, path=str(self.out),
                                      out=out, err=io.StringIO())
        self.assertEqual(code, 0)
        self.assertNotIn("no transcripts found", out.getvalue())
        doc = json.loads(self.out.read_text())
        self.assertGreater(doc["ranks"]["lsx-001"], 0.0)
        self.assertEqual(doc["ranks"]["lsy-001"], 0.0)

    def test_t7a_glob_root_under_a_subagents_dir_keeps_main_drops_subagent(self):
        """T7a. The glob's static root is itself under a directory named `subagents`: the filter looks
        only BELOW that root, so the main file is kept, the real sub-agent file dropped, and a rank file
        is written. Red for n1 (absolute-path test), n2 (parts[-3:]), n3 (slice one too far)."""
        self._assert_root_named_subagents(self._under_subagents_root("proj/**/*.jsonl"))

    def test_t7b_wildcard_right_after_the_subagents_named_root(self):
        """T7b. Same tree, glob `<tmp>/subagents/**/*.jsonl` (wildcard right after the directory named
        subagents). Red for n1, n3 and n4 (prefix includes the first wildcard component)."""
        self._assert_root_named_subagents(self._under_subagents_root("**/*.jsonl"))

    def test_t7c_glob_with_no_static_prefix_beyond_the_root_still_filters(self):
        """T7c. A wildcard directly under the root dir (`<tree>/*/*.jsonl` style) still drops the
        sub-agent file below it. Red for n4 and m5."""
        self._put("proj/main.jsonl", ["main-only-verb 1"], 1_000_000)
        self._put("subagents/agent-1.jsonl", ["fairy-only-verb 1"], 2_000_000)
        _cur, _prior, srcs = collect_targets(transcript_glob=str(self.root / "*" / "*.jsonl"), limit_files=6,
                                             exclude_subagents=True)
        self.assertEqual([os.path.basename(f) for f in srcs], ["main.jsonl"])

    def test_cli_help_names_the_main_session_scope(self):
        out = io.StringIO()
        orig = sys.argv
        sys.argv = ["lesson-pattern-audit", "--help"]
        try:
            with redirect_stdout(out), self.assertRaises(SystemExit):
                main()
        finally:
            sys.argv = orig
        self.assertIn("main-session transcripts only", " ".join(out.getvalue().split()))
