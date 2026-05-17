"""Smoke + unit tests for engram_log_emitter.

Covers: idempotent init, bind+emit pipeline, level routing to l1/l2 files,
invalid-input failure-mode (drop event, don't raise), turn caching, JSONL
shape, threading safety.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Project root is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engram_log_emitter
from engram_log_emitter import Emitter, emit_if_initialized


class EmitterTestBase(unittest.TestCase):
    def setUp(self):
        # Reset module singleton between tests
        engram_log_emitter._EMITTER_INSTANCE = None
        self.tmp = tempfile.mkdtemp(prefix="engram_log_test_")
        self.logs_dir = Path(self.tmp) / "logs"
        # Stop knowledge.db lookup from finding a real DB during tests
        os.environ["ENGRAM_HOME"] = self.tmp

    def tearDown(self):
        engram_log_emitter._EMITTER_INSTANCE = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_HOME", None)


class TestInit(EmitterTestBase):
    def test_init_creates_singleton(self):
        e1 = Emitter.init("sess_A", "/tmp/transcript.jsonl", initial_turn=5, logs_dir=self.logs_dir)
        e2 = Emitter.init("sess_A", "/tmp/transcript.jsonl", initial_turn=5, logs_dir=self.logs_dir)
        self.assertIs(e1, e2)
        self.assertEqual(e1.session_id, "sess_A")

    def test_init_with_different_session_returns_existing(self):
        e1 = Emitter.init("sess_A", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        e2 = Emitter.init("sess_B", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        self.assertIs(e1, e2)
        self.assertEqual(e2.session_id, "sess_A")  # original wins

    def test_get_returns_none_before_init(self):
        self.assertIsNone(Emitter.get())

    def test_sessions_dir_created(self):
        Emitter.init("sess_A", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        self.assertTrue((self.logs_dir / "sessions").is_dir())


class TestEmit(EmitterTestBase):
    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_X", "/tmp/t.jsonl", initial_turn=42, logs_dir=self.logs_dir)

    def test_basic_emit_writes_l1_file(self):
        uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})
        self.assertIsNotNone(uuid)
        p = self.logs_dir / "sessions" / "sess_X.l1.jsonl"
        self.assertTrue(p.exists())
        line = p.read_text().strip()
        evt = json.loads(line)
        self.assertEqual(evt["event_type"], "engram.surface.fire")
        self.assertEqual(evt["sessionId"], "sess_X")
        self.assertEqual(evt["turn"], 42)
        self.assertEqual(evt["level"], 1)
        self.assertEqual(evt["data"], {"k": "v"})
        self.assertEqual(evt["uuid"], uuid)
        self.assertIsNone(evt["parentUuid"])
        self.assertIsNone(evt["tool_use_id"])

    def test_l2_routes_to_separate_file(self):
        self.emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": "hi"})
        l1 = self.logs_dir / "sessions" / "sess_X.l1.jsonl"
        l2 = self.logs_dir / "sessions" / "sess_X.l2.jsonl"
        self.assertFalse(l1.exists())
        self.assertTrue(l2.exists())

    def test_bind_context_merges_into_data(self):
        self.emitter.bind(context_mode="1m", model_id="claude-opus-4-7[1m]")
        self.emitter.emit("engram.surface.fire", 1, {"specific": "thing"})
        line = (self.logs_dir / "sessions" / "sess_X.l1.jsonl").read_text().strip()
        evt = json.loads(line)
        self.assertEqual(evt["data"]["context_mode"], "1m")
        self.assertEqual(evt["data"]["model_id"], "claude-opus-4-7[1m]")
        self.assertEqual(evt["data"]["specific"], "thing")

    def test_emit_returns_uuid_usable_as_parent(self):
        u1 = self.emitter.emit("engram.surface.fire", 1, {})
        u2 = self.emitter.emit("engram.surface.fire", 1, {}, parent_uuid=u1)
        lines = (self.logs_dir / "sessions" / "sess_X.l1.jsonl").read_text().strip().split("\n")
        evt2 = json.loads(lines[1])
        self.assertEqual(evt2["parentUuid"], u1)

    def test_tool_use_id_carries_through(self):
        self.emitter.emit("engram.tool.engram_call", 1, {}, tool_use_id="toolu_abc")
        evt = json.loads((self.logs_dir / "sessions" / "sess_X.l1.jsonl").read_text().strip())
        self.assertEqual(evt["tool_use_id"], "toolu_abc")


class TestFailureMode(EmitterTestBase):
    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_F", "/tmp/t.jsonl", logs_dir=self.logs_dir)

    def test_invalid_event_type_drops_silently(self):
        # Not engram.* namespace
        result = self.emitter.emit("foo.bar", 1, {})
        self.assertIsNone(result)
        l1 = self.logs_dir / "sessions" / "sess_F.l1.jsonl"
        self.assertFalse(l1.exists())

    def test_short_event_type_drops_silently(self):
        result = self.emitter.emit("engram.surface", 1, {})  # only 2 parts
        self.assertIsNone(result)

    def test_invalid_level_drops(self):
        for bad_level in (0, 3, "1", None):
            result = self.emitter.emit("engram.x.y", bad_level, {})
            self.assertIsNone(result)
        l1 = self.logs_dir / "sessions" / "sess_F.l1.jsonl"
        self.assertFalse(l1.exists())

    def test_non_dict_data_drops(self):
        result = self.emitter.emit("engram.x.y", 1, ["not", "a", "dict"])
        self.assertIsNone(result)

    def test_emit_swallows_underlying_exceptions(self):
        # Corrupt the sessions_dir to force an IO error
        self.emitter.sessions_dir = Path("/proc/1/no-writable-path-anywhere")
        # Must NOT raise
        result = self.emitter.emit("engram.surface.fire", 1, {})
        self.assertIsNone(result)


class TestThreadSafety(EmitterTestBase):
    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_T", "/tmp/t.jsonl", logs_dir=self.logs_dir)

    def test_concurrent_emits_no_interleaved_lines(self):
        def worker(n):
            for i in range(n):
                self.emitter.emit("engram.x.y", 1, {"i": i})

        threads = [threading.Thread(target=worker, args=(50,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 8 threads × 50 events = 400 lines, each a valid JSON object
        lines = (self.logs_dir / "sessions" / "sess_T.l1.jsonl").read_text().strip().split("\n")
        self.assertEqual(len(lines), 400)
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                self.fail(f"interleaved/corrupted line: {line!r}")


class TestTurnFromConfig(EmitterTestBase):
    def test_turn_seeded_from_config_json(self):
        # Drop a config.json in the test ENGRAM_HOME with current_turn=42
        config_path = Path(self.tmp) / "config.json"
        config_path.write_text('{"memory": {"current_turn": 42, "decay_base": 1.014}}')
        # Don't pass initial_turn; emitter should read config.json
        e = Emitter.init("sess_C", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        e.emit("engram.x.y", 1, {})
        line = (self.logs_dir / "sessions" / "sess_C.l1.jsonl").read_text().strip()
        evt = json.loads(line)
        self.assertEqual(evt["turn"], 42)

    def test_turn_defaults_to_none_if_no_config(self):
        # No config.json — should not crash; turn becomes None (which serializes to JSON null)
        e = Emitter.init("sess_C2", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        result = e.emit("engram.x.y", 1, {})
        self.assertIsNotNone(result)  # emit succeeds
        line = (self.logs_dir / "sessions" / "sess_C2.l1.jsonl").read_text().strip()
        evt = json.loads(line)
        self.assertIsNone(evt["turn"])

    def test_explicit_initial_turn_overrides_config(self):
        # Config says 42; explicit init says 99; explicit wins
        config_path = Path(self.tmp) / "config.json"
        config_path.write_text('{"memory": {"current_turn": 42}}')
        e = Emitter.init("sess_C3", "/tmp/t.jsonl", initial_turn=99, logs_dir=self.logs_dir)
        e.emit("engram.x.y", 1, {})
        evt = json.loads((self.logs_dir / "sessions" / "sess_C3.l1.jsonl").read_text().strip())
        self.assertEqual(evt["turn"], 99)


class TestEmitIfInitialized(EmitterTestBase):
    def test_no_op_when_not_initialized(self):
        # Module singleton was reset in setUp
        self.assertIsNone(emit_if_initialized("engram.x.y", 1, {}))

    def test_routes_through_when_initialized(self):
        Emitter.init("sess_Q", "/tmp/t.jsonl", logs_dir=self.logs_dir)
        uuid = emit_if_initialized("engram.x.y", 1, {"via": "convenience"})
        self.assertIsNotNone(uuid)
        p = self.logs_dir / "sessions" / "sess_Q.l1.jsonl"
        self.assertTrue(p.exists())


if __name__ == "__main__":
    unittest.main()
