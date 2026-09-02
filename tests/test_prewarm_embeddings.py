"""Tests for the embedding-required startup gate (#345).

Embeddings are mandatory in current ENGRAM: a node with a NULL embedding is
excluded from the semantic-recall candidate set entirely (both the
sqlite-vec path and the pure-Python cosine fallback filter
`embedding IS NOT NULL`). This module tests the synchronous startup gate
(`server._embedding_startup_gate`) that fails loud, at server boot, when the
embedding model is unavailable — plus the background-thread model load
(`server._prewarm_embeddings`) that the gate starts on success, introduced
originally in #276.

Four tests per spec (`embedding-required-startup-gate.md`):

  1. test_cache_miss_exits_nonzero_with_recovery_message — the cache-presence
     check (`_embedding_cache_status`) returns False; the gate must
     `sys.exit(1)` and the stderr message must name the recovery command
     (the pre-warm one-liner) and the model name.

  2. test_unavailable_exits_nonzero_with_recovery_message —
     `_embedder.is_available()` returns False (e.g. sentence_transformers
     not importable); the gate must `sys.exit(1)` and the stderr message
     must name the recovery command (pip install).

  3. test_no_embeddings_env_bypasses_gate_entirely — ENGRAM_NO_EMBEDDINGS=1
     is set; the gate must return normally (no SystemExit) and must not even
     reach the is_available()/cache-status checks (full bypass, not just a
     pass-through).

  4. test_happy_path_starts_daemon_thread_without_synchronous_load — cache
     hit + is_available() True; the gate must start a daemon thread named
     'engram-prewarm' targeting `_prewarm_embeddings`, and must NOT call
     `_embedder._load_model` itself (synchronously) — only the thread target
     does.

Plus the two original #276 daemon-thread tests, updated for the new split
(cache-check now lives in `_embedding_cache_status` / the sync gate, not in
`_prewarm_embeddings` itself):

  5. test_prewarm_loads_model_and_logs_success — `_prewarm_embeddings` calls
     `_embedder._load_model` and logs success to stderr when the model loads.

  6. test_prewarm_handles_load_exception — `_load_model` raises;
     `_prewarm_embeddings` must not propagate the exception, must log it to
     stderr (never swallow silently), and must add the model to
     `_failed_models`.

Plus two round-2 tests (`embedding-gate-round2.md`), folding the round-1
review's blocker + escalated suggestion on PR #2485:

  7. test_enabled_false_bypasses_gate_entirely — `embedding.enabled: false`
     in config; the gate must return normally (no SystemExit), must not
     reach `is_available()`/`_embedding_cache_status()` (full bypass, same
     shape as the ENGRAM_NO_EMBEDDINGS bypass), must start no thread, and
     must print nothing. This is a regression fix: the round-1 gate never
     read `enabled` at all, so this documented, still-live opt-out could no
     longer boot the server.

  8. test_cache_key_slash_handling — `_embedding_cache_status` mirrors
     `SentenceTransformer`'s own org-prefix rule: a slash-containing model
     name (e.g. "BAAI/bge-small-en") produces the un-prefixed cache key; a
     bare name still produces the "sentence-transformers/"-prefixed key.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MODEL = "all-MiniLM-L6-v2"


def _import_server():
    """Import a clean copy of server + engram_core.

    Does NOT touch ENGRAM_NO_EMBEDDINGS — callers set/unset it themselves so
    each test controls the escape-hatch state explicitly.
    """
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    import engram_core  # noqa: F401
    return server


class _FakeThread:
    """Captures threading.Thread(**kwargs) without spawning a real thread."""

    instances: list["_FakeThread"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True


# ---------------------------------------------------------------------------
# Test 1: cache miss is fatal
# ---------------------------------------------------------------------------

class TestCacheMissIsFatal:
    def test_cache_miss_exits_nonzero_with_recovery_message(self, capsys):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        with mock.patch.object(server._embedder, "is_available", return_value=True), \
             mock.patch.object(server, "_embedding_cache_status", return_value=False), \
             mock.patch.object(server, "_get_embedding_config", return_value={"model": MODEL}):
            with pytest.raises(SystemExit) as exc_info:
                server._embedding_startup_gate()

        assert exc_info.value.code != 0, (
            f"expected non-zero exit, got {exc_info.value.code!r}"
        )

        captured = capsys.readouterr()
        assert MODEL in captured.err, (
            f"expected model name '{MODEL}' in stderr, got: {captured.err!r}"
        )
        assert "SentenceTransformer" in captured.err, (
            f"expected the pre-warm recovery command in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: is_available() False is also fatal
# ---------------------------------------------------------------------------

class TestUnavailableIsFatal:
    def test_unavailable_exits_nonzero_with_recovery_message(self, capsys):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        with mock.patch.object(server._embedder, "is_available", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                server._embedding_startup_gate()

        assert exc_info.value.code != 0, (
            f"expected non-zero exit, got {exc_info.value.code!r}"
        )

        captured = capsys.readouterr()
        assert "pip install" in captured.err, (
            f"expected the pip-install recovery command in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: ENGRAM_NO_EMBEDDINGS=1 fully bypasses the gate
# ---------------------------------------------------------------------------

class TestNoEmbeddingsBypassesGate:
    def test_no_embeddings_env_bypasses_gate_entirely(self, capsys):
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        try:
            server = _import_server()

            with mock.patch.object(
                server._embedder, "is_available",
                side_effect=AssertionError("is_available() must not be called when bypassed"),
            ), mock.patch.object(
                server, "_embedding_cache_status",
                side_effect=AssertionError("_embedding_cache_status() must not be called when bypassed"),
            ):
                # Must return normally -- no SystemExit.
                server._embedding_startup_gate()

            captured = capsys.readouterr()
            assert captured.err == "", (
                f"expected no error output when bypassed, got: {captured.err!r}"
            )
        finally:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)


# ---------------------------------------------------------------------------
# Test 4: happy path starts the daemon thread, never loads synchronously
# ---------------------------------------------------------------------------

class TestHappyPathStartsDaemonThread:
    def test_happy_path_starts_daemon_thread_without_synchronous_load(self):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        _FakeThread.instances = []

        with mock.patch.object(server._embedder, "is_available", return_value=True), \
             mock.patch.object(server, "_embedding_cache_status", return_value=True), \
             mock.patch.object(server, "_get_embedding_config", return_value={"model": MODEL}), \
             mock.patch.object(
                 server._embedder, "_load_model",
                 side_effect=AssertionError("_load_model must not be called synchronously"),
             ), \
             mock.patch("threading.Thread", side_effect=lambda **kw: _FakeThread(**kw)):
            # Must not raise -- _load_model is mocked to blow up if called
            # on this (the main) thread.
            server._embedding_startup_gate()

        assert len(_FakeThread.instances) == 1, (
            f"expected exactly one thread dispatch, got {len(_FakeThread.instances)}"
        )
        thread = _FakeThread.instances[0]
        assert thread.kwargs.get("daemon") is True, (
            f"expected daemon=True, got {thread.kwargs}"
        )
        assert thread.kwargs.get("name") == "engram-prewarm", (
            f"expected name='engram-prewarm', got {thread.kwargs.get('name')}"
        )
        assert thread.kwargs.get("target") is server._prewarm_embeddings, (
            f"expected target=server._prewarm_embeddings, got {thread.kwargs.get('target')}"
        )
        assert thread.kwargs.get("args") == (MODEL,), (
            f"expected args=('{MODEL}',), got {thread.kwargs.get('args')}"
        )
        assert thread.started is True, "thread.start() was not called"


# ---------------------------------------------------------------------------
# Test 5: _prewarm_embeddings loads the model and logs success (background)
# ---------------------------------------------------------------------------

class TestPrewarmLoadsModel:
    def test_prewarm_loads_model_and_logs_success(self, capsys):
        # Plain import -- `import server` (module, not __main__) never
        # triggers _embedding_startup_gate(), so ENGRAM_NO_EMBEDDINGS is
        # irrelevant here; left unset for clarity.
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        server._embedder._failed_models.discard(MODEL)
        server._embedder._model = None
        server._embedder._model_name = None

        def _fake_load(model_name):
            server._embedder._model = object()
            server._embedder._model_name = model_name

        with mock.patch.object(server._embedder, "_load_model", side_effect=_fake_load):
            server._prewarm_embeddings(MODEL)

        captured = capsys.readouterr()
        assert "loaded (background)" in captured.err, (
            f"expected the success message in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: exception during _load_model is logged, never swallowed silently
# ---------------------------------------------------------------------------

class TestPrewarmHandlesLoadException:
    def test_prewarm_handles_load_exception(self, capsys):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        server._embedder._failed_models.discard(MODEL)
        server._embedder._model = None
        server._embedder._model_name = None

        def _exploding_load(model_name):
            raise RuntimeError("Simulated load failure")

        with mock.patch.object(server._embedder, "_load_model", side_effect=_exploding_load):
            # Must not raise -- runs on a daemon thread, can't crash the process.
            server._prewarm_embeddings(MODEL)

        assert MODEL in server._embedder._failed_models, (
            f"Expected '{MODEL}' in _failed_models after _load_model exception, "
            f"got: {server._embedder._failed_models}"
        )

        captured = capsys.readouterr()
        assert "Simulated load failure" in captured.err, (
            f"Expected the exception logged to stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 7 (round 2): embedding.enabled: false fully bypasses the gate
# ---------------------------------------------------------------------------

class TestEnabledFalseBypassesGate:
    def test_enabled_false_bypasses_gate_entirely(self, capsys):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        with mock.patch.object(
            server, "_get_embedding_config",
            return_value={"model": MODEL, "enabled": False},
        ), mock.patch.object(
            server._embedder, "is_available",
            side_effect=AssertionError("is_available() must not be called when enabled=False"),
        ), mock.patch.object(
            server, "_embedding_cache_status",
            side_effect=AssertionError("_embedding_cache_status() must not be called when enabled=False"),
        ), mock.patch("threading.Thread", side_effect=lambda **kw: _FakeThread(**kw)):
            _FakeThread.instances = []
            # Must return normally -- no SystemExit.
            server._embedding_startup_gate()

        assert _FakeThread.instances == [], (
            f"expected no thread dispatch, got {_FakeThread.instances}"
        )

        captured = capsys.readouterr()
        assert captured.err == "", (
            f"expected no error output when enabled=False, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 8 (round 2): cache key mirrors SentenceTransformer's slash rule
# ---------------------------------------------------------------------------

class TestCacheKeySlashHandling:
    def test_cache_key_slash_handling(self):
        server = _import_server()

        captured_repo_ids = []

        def _fake_try_to_load_from_cache(repo_id, filename):
            captured_repo_ids.append(repo_id)
            return None

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_fake_try_to_load_from_cache,
        ):
            server._embedding_cache_status("BAAI/bge-small-en")
            server._embedding_cache_status(MODEL)

        assert captured_repo_ids == [
            "BAAI/bge-small-en",
            f"sentence-transformers/{MODEL}",
        ], (
            f"expected un-prefixed key for a slash-containing name and a "
            f"prefixed key for a bare name, got: {captured_repo_ids!r}"
        )
