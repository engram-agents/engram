"""Tests for #2753 WS4 -- pre-registered chain-render A/B (issue #2753
comments 5602340998 + 5602392744): the config gate (`_ws4_config()`), the
per-fire randomization + `tripwire-fire-ledger.jsonl` write wired into
main(), and the three contracts from the WS4 spec:

  CONTRACT A -- when the experiment gate is INACTIVE, main()'s stdout and
    exit code are byte-identical to a build with none of this code, and
    the ledger file is never created or written.
  CONTRACT B -- `heads_by_lesson` comes from the SAME walk that produces
    the rendered "[triggered principles]" string, never a second one.
  CONTRACT C -- the ledger write is best-effort and fail-open; a write
    failure never changes stdout/exit code and never suppresses the nudge.

Reuses the fixture-DB pattern of test_generalizes_tripwire_render.py
(fresh_server / _seed_lesson / _seed_cornerstone / _run_hook_main shape).

Coverage (spec Β§3):
  1. INACTIVE variants -- byte-identical to baseline, no ledger file.
  2. ACTIVE + participating -- chain/control arms, nudge block identical.
  3. Determinism -- same seed+tool_use_id -> same arm, draw_basis=="tool_use_id".
  4. Missing tool_use_id -- draw_basis=="uuid4", draw reproducible from the
     logged seed+nonce.
  5. Non-participating fire -- arm=="na", row written, stdout unchanged.
  6. Ledger write failure -- fails open, stdout/exit unaffected.
  7. Field checks -- chain_chars/nudge_chars/heads_by_lesson match rendered content.
  8. No hits -- no row at all.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))  # #2565: append, not insert(0) -- see test_lesson_tripwire.py


def _import_hook():
    p = PROJECT_ROOT / "src/engram/hooks/claude/engram-lesson-tripwire-hook.py"
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_ws4", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hook = _import_hook()
render_triggered_principles = _hook.render_triggered_principles


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_ws4_chain_ab_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# A dedicated, never-written-to home for "baseline" (guaranteed-INACTIVE)
# hook runs -- kept separate from any per-test config.json home so a
# baseline capture never depends on (or races) a test's own config file,
# and never touches the real ~/.engram of whatever machine runs this suite.
_BASELINE_HOME = tempfile.mkdtemp(prefix="engram_test_ws4_baseline_home_")
atexit.register(lambda: shutil.rmtree(_BASELINE_HOME, ignore_errors=True))


def _db_path() -> Path:
    return Path(TEST_DIR) / "knowledge.db"


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _seed_incident(server) -> str:
    payload = {
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed contextual cue from stderr.",
        "claim": "Skipped reading error output before hypothesizing root cause.",
        "quote_type": "personal_communication",
        "url": "https://example.com/ws4-chain-ab-incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _seed_cornerstone(server, title, recall_summary=None):
    payload = {"tag": "test-axis", "title": title, "new_frame": f"frame-{title}"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    cs_id = r["cornerstone_id"]
    if recall_summary:
        rs = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
            "summaries": [{"node_id": cs_id, "recall_summary": recall_summary, "recall_keywords": ["k1", "k2", "k3"]}]
        })))
        assert rs.get("applied") == 1, rs
    return cs_id


def _seed_lesson(server, *, pattern, nudge, recall_summary=None, generalized_by=""):
    inc_id = _seed_incident(server)
    payload = {
        "claim": f"claim for {nudge}",
        "incident_ids": inc_id,
        "scaffolding_nudge": nudge,
        "logical_chain": "chain",
        "situation_pattern": pattern,
    }
    if recall_summary:
        payload["recall_summary"] = recall_summary
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


def _write_config(dir_path: Path, config: dict) -> None:
    (dir_path / "config.json").write_text(json.dumps(config))


def _draw_arm(seed: str, nonce: str) -> str:
    """Independent reimplementation of main()'s own draw formula, used
    ONLY to construct/verify test fixtures -- never imported from the
    hook, so a test built on this is a genuinely independent check on the
    hook's own arithmetic, not a tautology."""
    draw = int(hashlib.sha256(f"{seed}:{nonce}".encode()).hexdigest(), 16) & 1
    return "chain" if draw == 1 else "control"


def _find_nonce_for_arm(seed: str, arm: str, prefix: str = "toolu_ws4_") -> str:
    for i in range(20000):
        candidate = f"{prefix}{i}"
        if _draw_arm(seed, candidate) == arm:
            return candidate
    raise AssertionError(f"could not find a nonce landing in arm={arm!r} under seed={seed!r}")


def _read_ledger(path: Path) -> list:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _run_hook_main(
    hook_input: dict, *, tripwires=None, db_path=None, engram_home=None,
) -> str:
    output_buf = io.StringIO()
    patches = [
        unittest.mock.patch.object(_hook, "load_tripwires", return_value=tripwires or []),
        unittest.mock.patch.object(_hook, "load_prevention_lessons", return_value=[]),
        unittest.mock.patch.object(_hook, "check_in_turn_recall", return_value=""),
        unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
        unittest.mock.patch("sys.stdout", output_buf),
    ]
    if db_path is not None:
        patches.append(unittest.mock.patch.object(_hook, "DB_PATH", db_path))
    home = engram_home if engram_home is not None else _BASELINE_HOME
    patches.append(unittest.mock.patch.object(_hook, "ENGRAM_HOME", str(home)))
    patches.append(unittest.mock.patch.object(
        _hook, "WS4_LEDGER_PATH", Path(home) / "tripwire-fire-ledger.jsonl",
    ))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            _hook.main()
        except SystemExit:
            pass
    return output_buf.getvalue()


# ---------------------------------------------------------------------------
# 1. INACTIVE variants -- byte-identical to baseline, no ledger file
# ---------------------------------------------------------------------------

class TestWs4ContractAInactiveVariants(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSInactive", recall_summary="head summary inactive")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary inactive", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_home_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _ledger_path(self):
        return Path(self.home) / "tripwire-fire-ledger.jsonl"

    def _baseline(self):
        return _run_hook_main(self.hook_input, tripwires=self.tripwires, db_path=_db_path())

    def _run_with_config(self, config):
        _write_config(Path(self.home), config)
        return _run_hook_main(
            self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
        )

    def test_no_experiments_key_at_all(self):
        baseline = self._baseline()
        out = self._run_with_config({"some_other_key": True})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_enabled_false(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": False, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_enabled_string_true_does_not_activate(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": "true", "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_malformed_start(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "not-a-date", "end": "2099-01-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_now_before_start(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2099-01-01T00:00:00Z", "end": "2099-06-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_now_at_or_after_end(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_empty_seed(self):
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": "",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_naive_start_without_utc_offset_is_malformed(self):
        """start/end without an explicit UTC offset does not "parse as
        ISO-8601 UTC" per the spec -- treated as malformed, not assumed UTC."""
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00", "end": "2099-01-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())

    def test_non_utc_offset_start_is_malformed(self):
        """Round 2 (item 4) -- a VALID ISO-8601 timestamp that carries an
        explicit but non-zero UTC offset (here +05:00) is not "ISO-8601
        UTC" either -- `_ws4_config()`'s own docstring says a non-UTC
        offset is treated as malformed, same bucket as the naive case
        above, not silently reinterpreted as if it meant UTC."""
        baseline = self._baseline()
        out = self._run_with_config({"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2026-09-28T00:00:00+05:00", "end": "2099-01-01T00:00:00Z", "seed": "s",
        }}})
        self.assertEqual(out, baseline)
        self.assertFalse(self._ledger_path().exists())


# ---------------------------------------------------------------------------
# 2. ACTIVE + participating -- chain vs control arms
# ---------------------------------------------------------------------------

class TestWs4ActiveParticipatingArms(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSArms", recall_summary="head summary arms")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary arms", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.seed = "ws4-test-seed-arms"
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_active_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": self.seed,
        }}})

    def _run(self, tool_use_id):
        hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": tool_use_id,
        }
        out = _run_hook_main(hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_chain_arm_has_principles_block(self):
        nonce = _find_nonce_for_arm(self.seed, "chain")
        ctx = self._run(nonce)
        self.assertIn("[triggered principles]", ctx)

    def test_control_arm_lacks_principles_block(self):
        nonce = _find_nonce_for_arm(self.seed, "control")
        ctx = self._run(nonce)
        self.assertNotIn("[triggered principles]", ctx)

    def test_nudge_block_identical_between_arms(self):
        chain_ctx = self._run(_find_nonce_for_arm(self.seed, "chain"))
        control_ctx = self._run(_find_nonce_for_arm(self.seed, "control"))
        expected_nudge_block = (
            "[lesson-tripwire] Action-moment pattern matched — remember:\n"
            "  • Think twice before rm -rf."
        )
        self.assertIn(expected_nudge_block, chain_ctx)
        self.assertIn(expected_nudge_block, control_ctx)


# ---------------------------------------------------------------------------
# 3 + 4. Determinism + nonce basis
# ---------------------------------------------------------------------------

class TestWs4DeterminismAndNonceBasis(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSDeterm", recall_summary="head summary determ")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary determ", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.seed = "ws4-determinism-seed"
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_determinism_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": self.seed,
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def _run(self, tool_use_id=None):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        if tool_use_id is not None:
            hook_input["tool_use_id"] = tool_use_id
        _run_hook_main(hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home)

    def test_same_seed_and_tool_use_id_gives_same_arm_on_repeat(self):
        tool_use_id = "toolu_fixed_001"
        self._run(tool_use_id)
        self._run(tool_use_id)
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["arm"], rows[1]["arm"])
        self.assertEqual(rows[0]["draw_basis"], "tool_use_id")
        self.assertEqual(rows[1]["draw_basis"], "tool_use_id")
        self.assertEqual(rows[0]["nonce"], tool_use_id)
        self.assertEqual(rows[1]["nonce"], tool_use_id)

    def test_missing_tool_use_id_uses_uuid4_and_draw_is_reproducible(self):
        self._run(tool_use_id=None)
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["draw_basis"], "uuid4")
        self.assertIsNotNone(row["nonce"])
        expected_arm = _draw_arm(self.seed, row["nonce"])
        self.assertEqual(row["arm"], expected_arm)

    def test_empty_string_tool_use_id_also_falls_back_to_uuid4(self):
        self._run(tool_use_id="")
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["draw_basis"], "uuid4")
        self.assertEqual(rows[0]["tool_use_id"], "")


# ---------------------------------------------------------------------------
# 5. Non-participating fire
# ---------------------------------------------------------------------------

class TestWs4NonParticipating(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        self.ls1 = _seed_lesson(self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.")
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_na_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": "na-seed",
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def test_na_arm_row_written_stdout_unchanged(self):
        baseline = _run_hook_main(self.hook_input, tripwires=self.tripwires, db_path=_db_path())
        out = _run_hook_main(
            self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
        )
        self.assertEqual(out, baseline)
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["arm"], "na")
        self.assertIsNone(row["nonce"])
        self.assertIsNone(row["draw_basis"])
        self.assertEqual(row["chain_chars"], 0)
        self.assertFalse(row["chain_rendered"])


# ---------------------------------------------------------------------------
# 6. Ledger write failure -- fails open
# ---------------------------------------------------------------------------

class TestWs4LedgerFailureFailsOpen(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSLedgerFail", recall_summary="head summary ledgerfail")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary ledgerfail", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_ledgerfail_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z",
            "seed": "ledgerfail-seed",
        }}})

    def test_unwritable_ledger_path_never_changes_output_or_exit(self):
        hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": "toolu_ledgerfail",
        }
        # A directory, not a file -- open(..., "a") raises IsADirectoryError.
        bad_ledger_path = Path(self.home)
        output_buf = io.StringIO()
        patches = [
            unittest.mock.patch.object(_hook, "load_tripwires", return_value=self.tripwires),
            unittest.mock.patch.object(_hook, "load_prevention_lessons", return_value=[]),
            unittest.mock.patch.object(_hook, "check_in_turn_recall", return_value=""),
            unittest.mock.patch.object(_hook, "DB_PATH", _db_path()),
            unittest.mock.patch.object(_hook, "ENGRAM_HOME", self.home),
            unittest.mock.patch.object(_hook, "WS4_LEDGER_PATH", bad_ledger_path),
            unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
            unittest.mock.patch("sys.stdout", output_buf),
        ]
        exit_code_holder: dict = {}
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                _hook.main()
            except SystemExit as e:
                exit_code_holder["code"] = e.code

        self.assertEqual(exit_code_holder.get("code"), 0)
        response = json.loads(output_buf.getvalue())
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])


# ---------------------------------------------------------------------------
# 7. Field checks
# ---------------------------------------------------------------------------

class TestWs4FieldsMatchRenderedContent(unittest.TestCase):

    def setUp(self):
        self.server = fresh_server()
        self.cs_head = _seed_cornerstone(self.server, "Ws4HeadCSFields", recall_summary="head summary fields")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary fields", generalized_by=self.cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.seed = "ws4-fields-seed"
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_fields_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": self.seed,
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def _expected_block(self) -> str:
        return render_triggered_principles(
            [{"nudge": "Think twice before rm -rf.", "node_id": self.ls1, "pattern": r"rm -rf"}],
            db_path=_db_path(),
        )

    def _run(self, tool_use_id):
        hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": tool_use_id,
        }
        return _run_hook_main(hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home)

    def test_chain_chars_and_heads_by_lesson_for_chain_arm(self):
        self._run(_find_nonce_for_arm(self.seed, "chain"))
        row = _read_ledger(self.ledger_path)[-1]
        expected_block = self._expected_block()
        self.assertGreater(len(expected_block), 0)
        self.assertEqual(row["chain_chars"], len(expected_block))
        self.assertTrue(row["chain_rendered"])
        self.assertEqual(row["heads_by_lesson"], {self.ls1: [self.cs_head]})
        self.assertEqual(row["shown_lesson_ids"], [self.ls1])
        self.assertEqual(row["cut_lesson_ids"], [])

    def test_chain_chars_logged_in_control_arm_too_but_not_rendered(self):
        self._run(_find_nonce_for_arm(self.seed, "control"))
        row = _read_ledger(self.ledger_path)[-1]
        expected_block = self._expected_block()
        self.assertEqual(row["chain_chars"], len(expected_block))
        self.assertFalse(row["chain_rendered"])

    def test_nudge_chars_matches_nudge_part_length_in_control_arm(self):
        """In the control arm nothing else is appended to `parts` (chain
        withheld, in-turn recall mocked to "") -- so additionalContext IS
        exactly the nudge block, making this the exact-equality check."""
        out = self._run(_find_nonce_for_arm(self.seed, "control"))
        additional_context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        row = _read_ledger(self.ledger_path)[-1]
        self.assertEqual(row["nudge_chars"], len(additional_context))

    def test_schema_and_identity_fields(self):
        self._run(_find_nonce_for_arm(self.seed, "chain"))
        row = _read_ledger(self.ledger_path)[-1]
        self.assertEqual(row["schema_version"], 1)
        self.assertEqual(row["experiment"], "ws4_chain_ab")
        self.assertEqual(row["seed"], self.seed)
        self.assertIsInstance(row["ts"], float)
        self.assertIsInstance(row["ts_iso"], str)
        self.assertIn("T", row["ts_iso"])


# ---------------------------------------------------------------------------
# 8. No hits -- no row
# ---------------------------------------------------------------------------

class TestWs4NoHitsNoRow(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_nohits_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": "nohits-seed",
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def test_no_hits_means_no_ledger_file(self):
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}
        _run_hook_main(hook_input, tripwires=[], engram_home=self.home)
        self.assertFalse(self.ledger_path.exists())


# ---------------------------------------------------------------------------
# 9. Round 2 item 1 -- WS4 failures become visible ledger rows, not silent.
#    Arm computation and row construction each get their OWN narrow guard
#    now (reviewer-fairy suggestion #1 on PR #3166); a bug in either must
#    still leave a visible row behind instead of silently dropping the fire.
# ---------------------------------------------------------------------------

class TestWs4ArmComputationErrorVisible(unittest.TestCase):
    """A bug in arm computation (e.g. the hash helper raising) must render
    dev-parity output -- exactly what the INACTIVE branch would produce,
    append the chain iff non-empty -- AND leave a visible arm="error" row
    behind, rather than silently dropping the fire from the ledger."""

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSArmError", recall_summary="head summary arm error")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary arm error", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": "toolu_arm_error",
        }
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_armerror_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z",
            "seed": "arm-error-seed",
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def test_hash_helper_raising_still_renders_dev_parity_and_logs_error_arm(self):
        baseline = _run_hook_main(self.hook_input, tripwires=self.tripwires, db_path=_db_path())
        with unittest.mock.patch("hashlib.sha256", side_effect=RuntimeError("boom")):
            out = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
            )
        self.assertEqual(out, baseline)
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["arm"], "error")
        self.assertIn("error", row)
        self.assertIn("RuntimeError", row["error"])
        self.assertIn("boom", row["error"])


class TestWs4RowConstructionErrorVisible(unittest.TestCase):
    """A bug specifically in ROW construction (assembling the ledger dict,
    separate from the arm-computation decision already applied to `parts`)
    must still leave a MINIMAL row behind (the fixed 8-field shape) so the
    fire is counted, without ever crashing main() or dropping the nudge."""

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSRowError", recall_summary="head summary row error")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary row error", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": "toolu_row_error",
        }
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_rowerror_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z",
            "seed": "row-error-seed",
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def test_row_construction_raising_still_writes_minimal_row(self):
        with unittest.mock.patch.object(_hook, "_build_ws4_ledger_entry", side_effect=RuntimeError("row boom")):
            out = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
            )
        response = json.loads(out)
        self.assertIn("Think twice before rm -rf.", response["hookSpecificOutput"]["additionalContext"])
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row.keys()),
            {"schema_version", "experiment", "ts", "ts_iso", "session_id", "tool_use_id", "arm", "error"},
        )
        self.assertIn("RuntimeError", row["error"])
        self.assertIn("row boom", row["error"])


# ---------------------------------------------------------------------------
# 10. Round 2 item 2 -- Contract B, two shown hits (one real head, one
#     headless) in the SAME fire.
# ---------------------------------------------------------------------------

class TestWs4ContractBTwoHitsOneHeadless(unittest.TestCase):
    """Contract B ("heads_by_lesson comes from the SAME walk that produces
    the rendered block") was only ever exercised through the trivial
    single-hit/single-head fixture every other class in this file uses.
    This reuses the exact truncated-hit-alongside-a-healthy-hit graph
    shape from
    tests/test_generalizes_tripwire_render.py::TestTruncatedRendersPartialNotice
    (do not invent a new graph shape), run through the WS4 ledger path."""

    def _stub_chain(self, truncated_ids, normal_chains=None):
        truncated_ids = set(truncated_ids)
        normal_chains = normal_chains or {}

        def _chain(conn, node_id):
            if node_id in truncated_ids:
                return {
                    "start_id": node_id, "start_resolved_id": node_id,
                    "roots": [node_id],
                    "nodes": {node_id: {"original_id": node_id, "parents": [], "depth": 0}},
                    "heads": [], "truncated": True, "cycle_detected": False,
                }
            if node_id in normal_chains:
                return normal_chains[node_id]
            return {
                "start_id": node_id, "start_resolved_id": node_id,
                "roots": [node_id],
                "nodes": {node_id: {"original_id": node_id, "parents": [], "depth": 0}},
                "heads": [node_id], "truncated": False, "cycle_detected": False,
            }
        return _chain

    def setUp(self):
        self.server = fresh_server()
        _seed_lesson(self.server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        self.seed = "ws4-contractb-seed"
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_contractb_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z", "seed": self.seed,
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"
        self.tripwires = [
            (r"sudo", "Careful with sudo.", "ls_truncated"),
            (r"rm -rf", "Think twice before rm -rf.", "ls_healthy"),
        ]
        self.hook_input = {"tool_name": "Bash", "tool_input": {"command": "sudo rm -rf /tmp/x"}}

    def test_heads_by_lesson_and_chain_rendered_for_two_hits(self):
        healthy_chain = {
            "start_id": "ls_healthy", "start_resolved_id": "ls_healthy",
            "roots": ["ls_healthy"],
            "nodes": {
                "cs_head": {
                    "original_id": "cs_head", "parents": [], "depth": 1,
                    "recall_summary": "head summary", "claim": "HeadClaim",
                },
                "ls_healthy": {
                    "original_id": "ls_healthy", "parents": ["cs_head"], "depth": 0,
                    "recall_summary": "healthy lesson summary",
                },
            },
            "heads": ["cs_head"], "truncated": False, "cycle_detected": False,
        }
        self.hook_input["tool_use_id"] = _find_nonce_for_arm(self.seed, "chain")
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}, normal_chains={"ls_healthy": healthy_chain}),
        ):
            out = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
            )

        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["arm"], "chain")
        self.assertTrue(row["chain_rendered"])
        self.assertEqual(row["heads_by_lesson"], {"ls_truncated": [], "ls_healthy": ["cs_head"]})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("truncated", ctx.lower())
        self.assertIn("↑ cs_head — HeadClaim", ctx)


# ---------------------------------------------------------------------------
# 11. Round 3 item 1 (#3173 reviewer comment 5858155739) -- the timestamp
#     computation between the two guards must be guarded too: a failure
#     there must not reach main()'s outer except and drop the nudge.
# ---------------------------------------------------------------------------

class TestWs4TimestampComputationErrorVisible(unittest.TestCase):
    """The `now = _time.time()` / `ts_iso = ...isoformat()` computation
    used to sit BETWEEN the arm-computation guard and the row-construction
    guard, covered by neither. A failure there reached main()'s outer
    `except Exception: sys.exit(0)`, printing nothing -- silently dropping
    the nudge already appended to `parts`, not just the WS4 ledger row.
    It now lives inside the row-construction guard, so a failure there
    must (a) still render the nudge, byte-identical to the same failure
    with the experiment INACTIVE (dev parity -- in the INACTIVE branch
    this code never runs at all, so breaking it can't change that
    branch's output), and (b) still leave exactly one ledger row behind
    with an `error` string, instead of silently dropping the fire."""

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSTsError", recall_summary="head summary ts error")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary ts error", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.seed = "ts-error-seed"
        # Pin the arm to "chain" (not "control") so the dev-parity
        # baseline below is the block-INCLUDED rendering -- the time bug
        # sits downstream of the arm decision, so a "control" draw would
        # correctly omit the block regardless of the bug, which would make
        # the (a) comparison below compare an unrelated experimental
        # difference rather than the bug this test targets.
        self.hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": _find_nonce_for_arm(self.seed, "chain", prefix="toolu_ts_error_"),
        }
        self.active_home = tempfile.mkdtemp(prefix="engram_test_ws4_tserror_active_")
        self.addCleanup(shutil.rmtree, self.active_home, ignore_errors=True)
        _write_config(Path(self.active_home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z",
            "seed": self.seed,
        }}})
        # No `experiments` key at all -> _ws4_config() returns INACTIVE, so
        # this run never reaches the timestamp computation in the first
        # place -- it's the dev-parity reference point for (a) below.
        self.inactive_home = tempfile.mkdtemp(prefix="engram_test_ws4_tserror_inactive_")
        self.addCleanup(shutil.rmtree, self.inactive_home, ignore_errors=True)
        _write_config(Path(self.inactive_home), {})
        self.ledger_path = Path(self.active_home) / "tripwire-fire-ledger.jsonl"

    def test_time_source_raising_still_renders_nudge_and_logs_error_row(self):
        with unittest.mock.patch("time.time", side_effect=RuntimeError("clock boom")):
            out_active = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(),
                engram_home=self.active_home,
            )
            out_inactive = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(),
                engram_home=self.inactive_home,
            )
        self.assertEqual(out_active, out_inactive)
        self.assertIn("Think twice before rm -rf.", out_active)
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("arm", row)
        self.assertIn("error", row)
        self.assertIn("RuntimeError", row["error"])
        self.assertIn("clock boom", row["error"])


# ---------------------------------------------------------------------------
# 12. Round 3 item 2 (#3173 reviewer comment 5858155739) -- pin the
#     double-failure precedence: when BOTH guards fail on the same fire,
#     the ARM error wins over the row-construction error.
# ---------------------------------------------------------------------------

class TestWs4DoubleFailureKeepsArmError(unittest.TestCase):
    """`"error": arm_error or f"{type(row_exc).__name__}: ..."` short-
    circuits on `arm_error` when it is already a truthy string, so when
    BOTH the arm-computation guard and the row-construction guard fail on
    the same fire, the row's `error` field is the ARM error, not the row-
    construction error. This is deliberate (see the comment at that `or`
    in main()): the arm failure means the coin-flip decision itself was
    corrupted, the more informative of the two to surface."""

    def setUp(self):
        self.server = fresh_server()
        cs_head = _seed_cornerstone(self.server, "Ws4HeadCSDoubleError", recall_summary="head summary double error")
        self.ls1 = _seed_lesson(
            self.server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary double error", generalized_by=cs_head,
        )
        self.tripwires = [(r"rm -rf", "Think twice before rm -rf.", self.ls1)]
        self.hook_input = {
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": "toolu_double_error",
        }
        self.home = tempfile.mkdtemp(prefix="engram_test_ws4_doubleerror_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        _write_config(Path(self.home), {"experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z",
            "seed": "double-error-seed",
        }}})
        self.ledger_path = Path(self.home) / "tripwire-fire-ledger.jsonl"

    def test_both_guards_failing_keeps_arm_error_not_row_error(self):
        with unittest.mock.patch("hashlib.sha256", side_effect=RuntimeError("arm boom")), \
             unittest.mock.patch.object(_hook, "_build_ws4_ledger_entry", side_effect=RuntimeError("row boom")):
            out = _run_hook_main(
                self.hook_input, tripwires=self.tripwires, db_path=_db_path(), engram_home=self.home,
            )
        response = json.loads(out)
        self.assertIn("Think twice before rm -rf.", response["hookSpecificOutput"]["additionalContext"])
        rows = _read_ledger(self.ledger_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["arm"], "error")
        self.assertIn("RuntimeError", row["error"])
        self.assertIn("arm boom", row["error"])
        self.assertNotIn("row boom", row["error"])


if __name__ == "__main__":
    unittest.main()
