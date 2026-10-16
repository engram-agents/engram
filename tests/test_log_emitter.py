"""Smoke + unit tests for engram_log_emitter.

Covers: idempotent init, bind+emit pipeline, level routing to l1/l2 files,
invalid-input failure-mode (drop event, don't raise), turn caching, JSONL
shape, threading safety.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
        self.emitter.bind(model_id="claude-opus-4-7[1m]", session_role="interactive")
        self.emitter.emit("engram.surface.fire", 1, {"specific": "thing"})
        line = (self.logs_dir / "sessions" / "sess_X.l1.jsonl").read_text().strip()
        evt = json.loads(line)
        self.assertEqual(evt["data"]["model_id"], "claude-opus-4-7[1m]")
        self.assertEqual(evt["data"]["session_role"], "interactive")
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


class TestSiteKwarg(EmitterTestBase):
    """alpha #2574 WS3: emit()'s new trailing `site` kwarg is purely additive
    and never threaded into the L1/L2 JSONL schema (see emit()'s own
    docstring)."""

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_SITE", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)

    def test_emit_accepts_site_kwarg(self):
        uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"}, site="surface-hook.surface_fire")
        self.assertIsNotNone(uuid)

    def test_site_kwarg_not_in_written_record(self):
        self.emitter.emit("engram.surface.fire", 1, {"k": "v"}, site="surface-hook.surface_fire")
        line = (self.logs_dir / "sessions" / "sess_SITE.l1.jsonl").read_text().strip()
        evt = json.loads(line)
        self.assertNotIn("site", evt)
        self.assertNotIn("site", evt["data"])

    def test_omitting_site_kwarg_unchanged(self):
        # Backward-compat: a caller that never passes site= behaves exactly
        # as before.
        uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})
        self.assertIsNotNone(uuid)


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


class FsyncTestBase(EmitterTestBase):
    """Shared setUp/tearDown for the fsync-durability contract (alpha #2801).

    Resets the module-level once-per-process warning flags so tests don't
    leak state into one another (contract requirement: "tests must reset it
    between tests").
    """

    def setUp(self):
        super().setUp()
        engram_log_emitter._FSYNC_WARNED = False
        engram_log_emitter._DIR_FSYNC_WARNED = False

    def tearDown(self):
        engram_log_emitter._FSYNC_WARNED = False
        engram_log_emitter._DIR_FSYNC_WARNED = False
        super().tearDown()


class TestFsyncDurability(FsyncTestBase):
    """Contract item 1: fsync the written fd after the write, before return,
    inside _write_lock."""

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_FS1", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)

    def test_fsync_called_on_target_fd_after_write(self):
        # Seed the file with a normal (unmocked) first emit so the file
        # already exists -- isolates this test to the PER-EVENT fsync call
        # (item 1) without also exercising the one-time directory fsync
        # (item 3), which only fires on the creating emit.
        self.emitter.emit("engram.surface.fire", 1, {"k": "seed"})

        target = self.logs_dir / "sessions" / "sess_FS1.l1.jsonl"
        real_fsync = os.fsync
        calls = []

        def fake_fsync(fd):
            # Verify the fd belongs to the target file (same inode/device)
            # and that the write already landed (post-flush) by the time
            # fsync is invoked -- i.e. fsync happens AFTER the write.
            fd_stat = os.fstat(fd)
            target_stat = os.stat(target)
            calls.append(
                {
                    "same_file": (fd_stat.st_ino, fd_stat.st_dev) == (target_stat.st_ino, target_stat.st_dev),
                    "content_at_fsync_time": target.read_text(),
                }
            )
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fake_fsync):
            uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "second"})

        self.assertIsNotNone(uuid)
        self.assertEqual(len(calls), 1, "expected exactly one fsync call for this event")
        self.assertTrue(calls[0]["same_file"], "fsync fd did not resolve to the target .l1.jsonl file")
        self.assertIn(uuid, calls[0]["content_at_fsync_time"], "write must land before fsync is called")


class TestFsyncFailureNotADrop(FsyncTestBase):
    """Contract item 2: a failed fsync must not be treated as a dropped
    event -- the line is still written, emit() still returns the uuid, and
    a warning fires once per process (not once per event)."""

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_FS2", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)
        # Seed the file so the two OSError-raising emits below exercise
        # ONLY the per-event fsync path, not the one-time directory fsync
        # (which only fires on the creating emit -- item 3's concern).
        self.emitter.emit("engram.surface.fire", 1, {"k": "seed"})

    def test_fsync_oserror_does_not_drop_event(self):
        warnings = []
        self.emitter._stderr = warnings.append

        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})

        self.assertIsNotNone(uuid, "emit() must still return the uuid when fsync fails")
        target = self.logs_dir / "sessions" / "sess_FS2.l1.jsonl"
        lines = target.read_text().strip().split("\n")
        self.assertEqual(json.loads(lines[-1])["uuid"], uuid, "the line must still be written")
        self.assertFalse(
            any("event dropped" in w for w in warnings),
            "a failed fsync must not fall into emit()'s catch-all 'event dropped' path",
        )

    def test_fsync_oserror_warns_once_per_process(self):
        warnings = []
        self.emitter._stderr = warnings.append

        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            u1 = self.emitter.emit("engram.surface.fire", 1, {"k": "one"})
            u2 = self.emitter.emit("engram.surface.fire", 1, {"k": "two"})

        self.assertIsNotNone(u1)
        self.assertIsNotNone(u2)
        fsync_warnings = [w for w in warnings if w.startswith("fsync failed")]
        self.assertEqual(
            len(fsync_warnings), 1,
            f"expected exactly one fsync-failure warning across two emits, got {warnings!r}",
        )

    def test_warn_fsync_once_helper_fires_only_once(self):
        # Unit-level check of the once-per-process mechanism itself,
        # decoupled from the integration path above.
        calls = []
        engram_log_emitter._warn_fsync_once(calls.append, OSError("a"))
        engram_log_emitter._warn_fsync_once(calls.append, OSError("b"))
        self.assertEqual(len(calls), 1)


class TestDirectoryFsyncOnCreate(FsyncTestBase):
    """Contract item 3: a newly-created file also gets its parent directory
    fsynced once (not on subsequent appends to the same file). A failed
    directory fsync follows the same not-a-drop / warn-once rule as item 2."""

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_FS3", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)

    def test_directory_fsync_only_on_creating_emit(self):
        dir_fsync_count = [0]
        real_fsync = os.fsync

        def fake_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                dir_fsync_count[0] += 1
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fake_fsync):
            self.emitter.emit("engram.surface.fire", 1, {"k": "first"})  # creates the file
            self.assertEqual(dir_fsync_count[0], 1, "expected exactly one directory fsync on the creating emit")
            self.emitter.emit("engram.surface.fire", 1, {"k": "second"})  # file already exists
            self.assertEqual(dir_fsync_count[0], 1, "directory fsync must not repeat on subsequent appends")

    def test_directory_fsync_oserror_does_not_drop_event(self):
        warnings = []
        self.emitter._stderr = warnings.append
        real_fsync = os.fsync

        def selective_fail_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated directory fsync failure")
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=selective_fail_fsync):
            uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})

        self.assertIsNotNone(uuid, "a failed directory fsync must not drop the event")
        target = self.logs_dir / "sessions" / "sess_FS3.l1.jsonl"
        self.assertTrue(target.exists())
        self.assertFalse(any("event dropped" in w for w in warnings))
        dir_warnings = [w for w in warnings if w.startswith("directory fsync failed")]
        self.assertEqual(len(dir_warnings), 1)

    def test_directory_fsync_warning_distinct_from_file_fsync_warning(self):
        # Both the file fsync AND the directory fsync fail on this (creating)
        # emit -- confirm both once-per-process flags are independent, i.e.
        # each warning fires (this test intentionally uses a distinct flag
        # for the directory case; see the implementation note in
        # engram_log_emitter.py).
        warnings = []
        self.emitter._stderr = warnings.append

        with mock.patch("os.fsync", side_effect=OSError("simulated failure")):
            uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})

        self.assertIsNotNone(uuid)
        file_warnings = [w for w in warnings if w.startswith("fsync failed")]
        dir_warnings = [w for w in warnings if w.startswith("directory fsync failed")]
        self.assertEqual(len(file_warnings), 1)
        self.assertEqual(len(dir_warnings), 1)


class _CountingWriteProxy:
    """Wraps a real file handle and counts .write() calls made through it,
    while delegating everything else (including .flush()/.fileno(), needed
    by the fsync path) to the real handle."""

    def __init__(self, fh):
        self._fh = fh
        self.write_calls = 0

    def write(self, data):
        self.write_calls += 1
        return self._fh.write(data)

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._fh.__exit__(*exc_info)


class TestWriteCallUnchanged(EmitterTestBase):
    """Contract item 4: the single f.write(line + "\\n") call stays one call.

    Behavioural form (round-1 review, PR #3168): wrap the append open
    (builtins.open until #3628, now open_regular_text_append) to
    return a counting proxy around the real handle and assert exactly one
    .write() call happens during one emit() -- the reviewer verified this
    survives a comment/docstring containing the literal text ".write(" and
    still fails if a second real write call is added, which the earlier
    inspect.getsource(...).count(".write(") source-text form could not
    distinguish either way.
    """

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_WC", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)

    def test_emit_makes_exactly_one_write_call(self):
        target = self.logs_dir / "sessions" / "sess_WC.l1.jsonl"
        # Append seam since #3628: engram_regular_io.open_regular_text_append
        # (non-blocking, regular-file-only), imported by name into the emitter.
        real_open = engram_log_emitter.open_regular_text_append
        proxies = []

        def fake_open(file, *args, **kwargs):
            fh = real_open(file, *args, **kwargs)
            if str(file) == str(target):
                proxy = _CountingWriteProxy(fh)
                proxies.append(proxy)
                return proxy
            return fh

        with mock.patch.object(engram_log_emitter, "open_regular_text_append", side_effect=fake_open):
            uuid = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})

        self.assertIsNotNone(uuid)
        self.assertEqual(len(proxies), 1, "expected exactly one open() call on the target file")
        self.assertEqual(proxies[0].write_calls, 1, "expected exactly one .write() call during one emit()")


class TestFlushFailureRoutesToDrop(FsyncTestBase):
    """Documents the deliberate flush/fsync boundary (round-1 review, PR
    #3168): f.flush() is NOT covered by contract item 2's not-a-drop
    carve-out. A flush() failure means the bytes plausibly never left the
    process, so it correctly falls into emit()'s generic "event dropped"
    catch-all -- unlike a post-write fsync failure (item 2), where the bytes
    are already out and dropping would be wrong."""

    def setUp(self):
        super().setUp()
        self.emitter = Emitter.init("sess_FLUSH", "/tmp/t.jsonl", initial_turn=1, logs_dir=self.logs_dir)

    def test_flush_oserror_is_dropped_not_warned(self):
        warnings = []
        self.emitter._stderr = warnings.append
        target = self.logs_dir / "sessions" / "sess_FLUSH.l1.jsonl"
        real_open = engram_log_emitter.open_regular_text_append  # append seam since #3628

        class _FlushFailProxy:
            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                return self._fh.write(data)

            def flush(self):
                raise OSError("simulated flush failure")

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return self._fh.__exit__(*exc_info)

        def fake_open(file, *args, **kwargs):
            fh = real_open(file, *args, **kwargs)
            if str(file) == str(target):
                return _FlushFailProxy(fh)
            return fh

        with mock.patch.object(engram_log_emitter, "open_regular_text_append", side_effect=fake_open):
            result = self.emitter.emit("engram.surface.fire", 1, {"k": "v"})

        self.assertIsNone(result, "a flush() failure must be treated as a dropped event")
        self.assertTrue(
            any("event dropped" in w for w in warnings),
            f"expected the generic 'event dropped' message, got {warnings!r}",
        )
        self.assertFalse(
            any(w.startswith("fsync failed") for w in warnings),
            "a flush() failure must not be misreported as an fsync-failure warning",
        )


if __name__ == "__main__":
    unittest.main()
