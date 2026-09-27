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


if __name__ == "__main__":
    unittest.main()
