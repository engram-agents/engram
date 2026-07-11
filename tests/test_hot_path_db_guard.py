"""Tests for #1669/#1670/#1671/#1675 (umbrella #1668): hot-path DB-guard perf fix.

cProfile evidence (2026-07-06, #1668) showed _get_db() spending 65-96% of its
time re-running an unguarded backfill/migration/DAG-check block on every
single call. This file covers the three changes actually shipped in this PR:

  1. #1669 — one-shot per-process guard around that block (spy-based: the
     block runs exactly once across many _get_db() calls; a failed attempt
     does not mark the path done, so the next call retries).
  2. #1670 — _record_tool_timing uses a dedicated lightweight connection
     instead of _get_db(), and concurrent recording (+ concurrent _get_db()
     calls) from many threads completes without error.
  3. #1675 — the module-level _embedder singleton double-checked-locks its
     lazy model load, so concurrent first-touch from many threads loads the
     model exactly once.

#1671 (per-thread connection reuse in _get_db()) was NOT implemented in this
PR — see the coder-fairy handoff report for why (closing-pattern
reconciliation across ~59 `conn = core._get_db(); ... finally: conn.close()`
call sites, well past the ~30-site stop threshold in the spec) — so there is
no test here for "same thread reuses the same connection object."

Import-pattern: family-internals (R1) — imports engram_core and server
directly, uses fresh_module (R4) for per-test isolation where needed.
"""

import atexit
import concurrent.futures
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_hot_path_db_guard_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_core(tmp_dir):
    """Reload engram_core (+server family) and point it at a fresh tempdir.

    Mirrors the fresh_server() pattern used elsewhere (e.g.
    test_engram_add_edge.py) but returns the engram_core module directly
    (family-internals / R1) since these tests exercise _get_db() and the
    module-level guard state, not a tool handler.
    """
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    core = fresh_module(engram_core)
    core._configure_paths(tmp_dir)
    core._ensure_data_dir()
    return core


def _seed_minimal_axiom(core):
    """Write one axiom row directly so the empty-graph sentinel doesn't fire.

    _get_db() raises _seed_missing_message() when the axiom count is 0 and
    bootstrap_mode is not set; ENGRAM_NO_DB_GUARDS=1 already bypasses that
    (bootstrap_mode covers both env vars — see _get_db()'s docstring), so
    this is here for tests that flip that env var off mid-test.
    """
    conn = core._get_db()
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, confidence, is_current) "
        "VALUES ('ax_0001', 'axiom', 'test axiom', datetime('now'), 1.0, 1)"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# (a) backfill block runs exactly once per process across many _get_db() calls
# ---------------------------------------------------------------------------

class TestOneShotGuardRunsOnce:
    def test_setup_block_runs_exactly_once_across_many_calls(self):
        core = _fresh_core(os.path.join(TEST_DIR, "once"))

        call_count = {"n": 0}
        real_setup = core._run_db_one_time_setup

        def _spy(conn):
            call_count["n"] += 1
            return real_setup(conn)

        with mock.patch.object(core, "_run_db_one_time_setup", side_effect=_spy):
            for _ in range(10):
                conn = core._get_db()
                conn.close()

        assert call_count["n"] == 1, (
            f"expected the one-time setup block to run exactly once across "
            f"10 _get_db() calls on the same path, ran {call_count['n']} times"
        )

    def test_new_path_reruns_setup(self):
        """A different resolved DB path is a genuinely new one-shot slot."""
        core = _fresh_core(os.path.join(TEST_DIR, "path_a"))

        call_count = {"n": 0}
        real_setup = core._run_db_one_time_setup

        def _spy(conn):
            call_count["n"] += 1
            return real_setup(conn)

        with mock.patch.object(core, "_run_db_one_time_setup", side_effect=_spy):
            conn = core._get_db()
            conn.close()
            assert call_count["n"] == 1

            # Reconfigure to a second, distinct tempdir/path.
            core._configure_paths(os.path.join(TEST_DIR, "path_b"))
            core._ensure_data_dir()
            conn = core._get_db()
            conn.close()
            assert call_count["n"] == 2, (
                "a distinct resolved DB path must get its own one-shot setup run"
            )


# ---------------------------------------------------------------------------
# (b) failed backfill retries on next call (guard-after-success, not
#     guard-after-attempt)
# ---------------------------------------------------------------------------

class TestFailedSetupRetries:
    def test_failed_attempt_is_not_marked_done_and_retries(self):
        core = _fresh_core(os.path.join(TEST_DIR, "retry"))

        attempts = {"n": 0}
        real_setup = core._run_db_one_time_setup

        def _flaky(conn):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("simulated transient failure")
            return real_setup(conn)

        with mock.patch.object(core, "_run_db_one_time_setup", side_effect=_flaky):
            with pytest.raises(sqlite3.OperationalError):
                core._get_db()

            # Path must NOT have been marked done after the failed attempt.
            # .resolve() to match _get_db()'s own guard key (engram_core.py
            # stores str(DB_PATH.resolve()) in _db_setup_done_paths) -- on
            # macOS, /tmp is a symlink to /private/tmp, so an unresolved path
            # here never matches the resolved one the guard actually stores
            # (#1686).
            resolved_path = str(core.DB_PATH.resolve())
            assert resolved_path not in core._db_setup_done_paths, (
                "a raising setup attempt must not mark the path done"
            )

            # Next call retries the block (attempts increments again) and
            # this time succeeds.
            conn = core._get_db()
            conn.close()

        assert attempts["n"] == 2, (
            f"expected exactly 2 attempts (1 failed + 1 retry-success), "
            f"got {attempts['n']}"
        )
        assert resolved_path in core._db_setup_done_paths


# ---------------------------------------------------------------------------
# (c) concurrent _get_db() + tool_timing recording from N=8 threads
# ---------------------------------------------------------------------------

class TestConcurrentGetDbAndToolTiming:
    def test_concurrent_get_db_and_timing_no_errors(self):
        core = _fresh_core(os.path.join(TEST_DIR, "concurrent"))
        _seed_minimal_axiom(core)

        for key in list(sys.modules):
            if key == "server":
                del sys.modules[key]
        import server  # noqa: E402
        server._configure_paths(os.path.join(TEST_DIR, "concurrent"))

        errors = []
        N = 8

        def _worker(i):
            try:
                conn = server._get_db()
                conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
                conn.close()
                server._record_tool_timing(f"fake_tool_{i}", 5 + i, "ok")
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
            list(pool.map(_worker, range(N)))

        assert not errors, f"concurrent _get_db()/_record_tool_timing raised: {errors}"

        # All N timing rows should have landed (never-raises contract does
        # not mean silently-dropped — the dedicated connection + lock must
        # actually serialize writes through, not swallow them).
        conn = server._get_db()
        row_count = conn.execute(
            "SELECT COUNT(*) FROM tool_timing WHERE tool_name LIKE 'fake_tool_%'"
        ).fetchone()[0]
        conn.close()
        assert row_count == N, f"expected {N} tool_timing rows, found {row_count}"


# ---------------------------------------------------------------------------
# (d) concurrent first-touch of the embedder loads the model exactly once
# ---------------------------------------------------------------------------

class TestEmbedderConcurrentFirstTouch:
    def test_concurrent_first_embed_loads_model_once(self):
        core = _fresh_core(os.path.join(TEST_DIR, "embedder"))

        load_count = {"n": 0}
        construct_barrier = threading.Barrier(8, timeout=5)

        class _FakeModel:
            def encode(self, text, convert_to_numpy=True):
                class _Vec:
                    def tolist(self_inner):
                        return [0.1, 0.2, 0.3]
                if isinstance(text, list):
                    class _Vecs:
                        def tolist(self_inner):
                            return [[0.1, 0.2, 0.3] for _ in text]
                    return _Vecs()
                return _Vec()

        def _fake_ctor(model_name):
            # Line up all 8 threads so they race into construction together
            # (best-effort — a slow thread that misses the barrier just
            # proceeds; the lock is what must serialize the real work, not
            # the barrier, which only maximizes contention).
            try:
                construct_barrier.wait()
            except threading.BrokenBarrierError:
                pass
            load_count["n"] += 1
            return _FakeModel()

        core._embedder._model = None
        core._embedder._model_name = None
        core._embedder._failed_models.discard(core.DEFAULT_EMBEDDING_MODEL)

        with mock.patch(
            "sentence_transformers.SentenceTransformer", side_effect=_fake_ctor
        ), mock.patch.object(core._embedder, "is_available", return_value=True):
            errors = []

            def _worker(i):
                try:
                    core._embedder.embed(f"text {i}", core.DEFAULT_EMBEDDING_MODEL)
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_worker, range(8)))

        assert not errors, f"concurrent embed() raised: {errors}"
        assert load_count["n"] == 1, (
            f"expected the model to be constructed exactly once across 8 "
            f"concurrent first-touch embed() calls, constructed {load_count['n']} times"
        )
