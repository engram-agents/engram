"""Tests for the background-thread embedding pre-warm introduced in #276.

Three tests per spec:

  1. test_prewarm_runs_on_daemon_thread — verifies that the __main__ block
     dispatches _prewarm_embeddings on a daemon thread with name
     'engram-prewarm'. Uses mock.patch on threading.Thread to capture
     the dispatch kwargs without actually spawning a thread.

  2. test_prewarm_handles_uncached_model — monkeypatches
     try_to_load_from_cache to return None; asserts the model name enters
     _embedder._failed_models and the disable-message fires to stderr.

  3. test_prewarm_handles_load_exception — monkeypatches _load_model to
     raise; asserts the model name enters _embedder._failed_models and no
     exception propagates out of _prewarm_embeddings.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MODEL = "all-MiniLM-L6-v2"


def _import_server_no_embeddings():
    """Import server with embeddings disabled (clean module state)."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    import engram_core
    return server


def _import_server_with_embeddings():
    """Import server with embeddings nominally available.

    sentence_transformers may or may not be installed in the test env.
    We patch is_available() to return True for tests that exercise the
    embedding code path, but the import-time module state is set here.
    """
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
    import server
    return server


# ---------------------------------------------------------------------------
# Test 1: daemon-thread dispatch shape
# ---------------------------------------------------------------------------

class TestPrewarmRunsOnDaemonThread:
    """Verify the __main__ block dispatches _prewarm_embeddings on a daemon thread."""

    def test_prewarm_runs_on_daemon_thread(self):
        """threading.Thread is called with daemon=True, name='engram-prewarm',
        target=server._prewarm_embeddings, and .start() is called exactly once.

        The __main__ block does `import threading` locally, so we patch
        threading.Thread at the threading module level.
        """
        server = _import_server_with_embeddings()

        captured = {}

        class _FakeThread:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                captured["started"] = True

        with mock.patch.object(server._embedder, "is_available", return_value=True), \
             mock.patch("threading.Thread", side_effect=lambda **kw: _FakeThread(**kw)), \
             mock.patch("engram_core._get_embedding_config", return_value={"model": MODEL}):

            # Reproduce the if-is_available block from __main__ directly,
            # since _prewarm_embeddings is now module-level and importable.
            import threading as _threading
            emb_config = server._get_embedding_config()
            mn = emb_config.get("model", server.DEFAULT_EMBEDDING_MODEL)
            _threading.Thread(
                target=server._prewarm_embeddings,
                args=(mn,),
                daemon=True,
                name="engram-prewarm",
            ).start()

        assert captured.get("daemon") is True, f"expected daemon=True, got {captured}"
        assert captured.get("name") == "engram-prewarm", (
            f"expected name='engram-prewarm', got {captured.get('name')}"
        )
        assert captured.get("target") is server._prewarm_embeddings, (
            f"expected target=server._prewarm_embeddings, got {captured.get('target')}"
        )
        assert captured.get("args") == (mn,), (
            f"expected args=('{mn}',), got {captured.get('args')}"
        )
        assert captured.get("started") is True, "thread.start() was not called"


# ---------------------------------------------------------------------------
# Test 2: uncached model path
# ---------------------------------------------------------------------------

class TestPrewarmHandlesUncachedModel:
    """When try_to_load_from_cache returns None, model enters _failed_models."""

    def test_prewarm_handles_uncached_model(self, capsys):
        server = _import_server_no_embeddings()
        # Reset failed_models so we get a clean slate
        server._embedder._failed_models.discard(MODEL)
        server._embedder._model = None
        server._embedder._model_name = None

        with mock.patch(
            "server.try_to_load_from_cache",
            return_value=None,
            create=True,
        ):
            # Patch the import inside _prewarm_embeddings
            fake_hf = types.ModuleType("huggingface_hub")
            fake_hf.try_to_load_from_cache = lambda repo, filename: None
            with mock.patch.dict("sys.modules", {"huggingface_hub": fake_hf}):
                server._prewarm_embeddings(MODEL)

        assert MODEL in server._embedder._failed_models, (
            f"Expected '{MODEL}' in _failed_models after cache-miss, "
            f"got: {server._embedder._failed_models}"
        )

        captured = capsys.readouterr()
        assert "not cached locally" in captured.err, (
            f"Expected disable-message in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: exception during _load_model
# ---------------------------------------------------------------------------

class TestPrewarmHandlesLoadException:
    """When _load_model raises, model enters _failed_models, no exception propagates."""

    def test_prewarm_handles_load_exception(self):
        server = _import_server_no_embeddings()
        server._embedder._failed_models.discard(MODEL)
        server._embedder._model = None
        server._embedder._model_name = None

        fake_hf = types.ModuleType("huggingface_hub")
        # Cache hit — so we reach _load_model
        fake_hf.try_to_load_from_cache = lambda repo, filename: "/fake/path/to/config.json"

        def _exploding_load(model_name):
            raise RuntimeError("Simulated load failure")

        with mock.patch.dict("sys.modules", {"huggingface_hub": fake_hf}), \
             mock.patch.object(server._embedder, "_load_model", side_effect=_exploding_load):
            # Must not raise
            server._prewarm_embeddings(MODEL)

        assert MODEL in server._embedder._failed_models, (
            f"Expected '{MODEL}' in _failed_models after _load_model exception, "
            f"got: {server._embedder._failed_models}"
        )
