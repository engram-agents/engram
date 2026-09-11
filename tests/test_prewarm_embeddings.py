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
     `SentenceTransformer`'s clause-3 org-prefix rule: a slash-containing
     model name (e.g. "BAAI/bge-small-en") probes only the un-prefixed repo
     id; a bare name probes the "sentence-transformers/"-prefixed id FIRST,
     then the bare id (the legacy-name fallback, #2835).

Plus seven #2835 tests, covering all three clauses of the loader's
resolution branch (`_embedding_cache_status` previously mirrored only the
org-prefix rule, clause 3):

  9. test_existing_local_dir_with_modules_json_passes — clause 1: an
     existing local directory containing modules.json returns True, and the
     Hub is never consulted (try_to_load_from_cache patched to raise if
     called).

  10. test_existing_local_dir_with_only_config_json_passes — clause 1, same
      shape, config.json only.

  11. test_existing_local_dir_without_model_files_fails — clause 1 guard: an
      EMPTY existing directory returns False without raising. Prevents
      #345's hole from reopening for local-path installs.

  12. test_nonexistent_local_path_returns_false_instead_of_raising — the
      verbatim #2835 repro. Returns False instead of letting
      HFValidationError escape the gate, which is the reported bug and what
      this test guards. NOTE the mechanism, because an earlier version of
      this entry named the wrong one: "/opt/models/all-MiniLM-L6-v2" has
      THREE slashes, so it is refused by CLAUSE 2 and try_to_load_from_cache
      is never called at all. Deleting clause 3's `except ValueError`
      entirely would leave this test green — so it does not discriminate on
      the exception guard, and test 13 is the one that does.

  13. test_single_slash_nonexistent_path_is_caught_not_raised — "/models",
      genuinely one "/", so it passes clause 2 and DOES reach clause 3,
      where it is saved only by `except ValueError` around
      `try_to_load_from_cache`. Run against the REAL huggingface_hub. This
      is the ONLY test covering that exception guard: remove the guard and
      this is the test that goes red.

  14. test_legacy_bare_name_cached_unprefixed_passes — clause 3 fallback: a
      legacy no-org name (e.g. "bert-base-uncased") cached only under its
      bare repo id (not the "sentence-transformers/"-prefixed one) still
      returns True.

  15. test_path_shaped_name_gets_directory_remedy_not_prewarm_advice — the
      startup gate, given a path-shaped model name and a forced cache-status
      failure, prints the directory remedy (modules.json/config.json) and
      does NOT print the pre-warm one-liner.
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
            captured_repo_ids_slash = list(captured_repo_ids)
            captured_repo_ids.clear()
            server._embedding_cache_status(MODEL)
            captured_repo_ids_bare = list(captured_repo_ids)

        assert captured_repo_ids_slash == ["BAAI/bge-small-en"], (
            f"expected only the un-prefixed key probed for a slash-"
            f"containing name, got: {captured_repo_ids_slash!r}"
        )
        assert captured_repo_ids_bare == [
            f"sentence-transformers/{MODEL}",
            MODEL,
        ], (
            f"expected the prefixed key probed first, then the bare key, "
            f"for a bare name, got: {captured_repo_ids_bare!r}"
        )


# ---------------------------------------------------------------------------
# Test 9 (#2835): clause 1 -- existing local dir with modules.json passes,
# Hub never consulted
# ---------------------------------------------------------------------------

class TestExistingLocalDirWithModulesJsonPasses:
    def test_existing_local_dir_with_modules_json_passes(self, tmp_path):
        server = _import_server()

        (tmp_path / "modules.json").write_text("{}")

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=AssertionError(
                "try_to_load_from_cache must not be called for an existing "
                "local model directory"
            ),
        ):
            result = server._embedding_cache_status(str(tmp_path))

        assert result is True, (
            f"expected True for an existing directory containing "
            f"modules.json, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 10 (#2835): clause 1 -- existing local dir with only config.json
# passes, Hub never consulted
# ---------------------------------------------------------------------------

class TestExistingLocalDirWithOnlyConfigJsonPasses:
    def test_existing_local_dir_with_only_config_json_passes(self, tmp_path):
        server = _import_server()

        (tmp_path / "config.json").write_text("{}")

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=AssertionError(
                "try_to_load_from_cache must not be called for an existing "
                "local model directory"
            ),
        ):
            result = server._embedding_cache_status(str(tmp_path))

        assert result is True, (
            f"expected True for an existing directory containing only "
            f"config.json, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 11 (#2835): clause 1 guard -- existing but EMPTY local dir fails,
# does not raise (#345's hole must not reopen for local-path installs)
# ---------------------------------------------------------------------------

class TestExistingLocalDirWithoutModelFilesFails:
    def test_existing_local_dir_without_model_files_fails(self, tmp_path):
        server = _import_server()

        # tmp_path exists but is empty: no modules.json, no config.json.
        result = server._embedding_cache_status(str(tmp_path))

        assert result is False, (
            f"expected False for an existing-but-empty directory, got "
            f"{result!r}"
        )


# ---------------------------------------------------------------------------
# Test 12 (#2835): the reported bug -- a nonexistent local path returns
# False instead of letting HFValidationError escape. Real huggingface_hub,
# deliberately unpatched.
# ---------------------------------------------------------------------------

class TestNonexistentLocalPathReturnsFalseInsteadOfRaising:
    def test_nonexistent_local_path_returns_false_instead_of_raising(self):
        server = _import_server()

        path = "/opt/models/all-MiniLM-L6-v2"
        assert not os.path.exists(path), (
            f"test precondition failed: {path!r} unexpectedly exists on "
            f"this machine -- pick a different nonexistent path"
        )

        # Deliberately NOT patching huggingface_hub. This is the verbatim
        # #2835 repro: against the pre-fix gate, try_to_load_from_cache was
        # handed this path unvalidated and HFValidationError propagated out
        # as a raw traceback. It must now come back as a clean False.
        result = server._embedding_cache_status(path)

        assert result is False, (
            f"expected False for a nonexistent local path, got {result!r}"
        )

        # Pin the MECHANISM, because naming the wrong one is the mistake
        # this block is correcting. This input has THREE slashes, so it is
        # refused by CLAUSE 2 and the Hub is never consulted -- clause 3's
        # `except ValueError` is not what saves it, and deleting that guard
        # would leave this test green. The single-slash case in the next
        # test is the one that covers the guard. Asserting it here means a
        # future edit that changes which clause handles this input fails
        # loudly instead of quietly invalidating the comment.
        assert path.count("/") > 1, (
            "test precondition failed: this case is meant to exercise "
            "clause 2's structural refusal, which needs more than one '/'"
        )

        def _must_not_be_called(repo_id, filename):
            raise AssertionError(
                f"clause 2 should have refused {path!r} before any Hub "
                f"lookup, but try_to_load_from_cache was called with "
                f"{repo_id!r}"
            )

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_must_not_be_called,
        ):
            assert server._embedding_cache_status(path) is False


# ---------------------------------------------------------------------------
# Test 13 (#2835): "/models" has genuinely ONE slash, so it passes clause 2
# and reaches clause 3, where `except ValueError` around
# try_to_load_from_cache is the only thing that keeps HFValidationError from
# escaping. Real huggingface_hub, deliberately unpatched. This is the ONLY
# test covering that guard -- test 12 above does not, despite its input
# looking similar.
# ---------------------------------------------------------------------------

class TestSingleSlashNonexistentPathIsCaughtNotRaised:
    def test_single_slash_nonexistent_path_is_caught_not_raised(self):
        server = _import_server()

        path = "/models"
        assert not os.path.exists(path), (
            f"test precondition failed: {path!r} unexpectedly exists on "
            f"this machine -- pick a different nonexistent path"
        )
        assert path.count("/") == 1, (
            "test precondition failed: expected exactly one '/' so this "
            "case reaches clause 3, not clause 2"
        )

        result = server._embedding_cache_status(path)

        assert result is False, (
            f"expected False for a single-slash nonexistent path, got "
            f"{result!r}"
        )


# ---------------------------------------------------------------------------
# Test 14 (#2835): clause 3 fallback -- a legacy no-org name cached ONLY
# under its bare repo id still passes
# ---------------------------------------------------------------------------

class TestLegacyBareNameCachedUnprefixedPasses:
    def test_legacy_bare_name_cached_unprefixed_passes(self):
        server = _import_server()

        def _fake_try_to_load_from_cache(repo_id, filename):
            # A hit ONLY for the bare legacy name -- the prefixed form was
            # never published upstream for this model.
            if repo_id == "bert-base-uncased":
                return "/fake/cache/path/config.json"
            return None

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_fake_try_to_load_from_cache,
        ):
            result = server._embedding_cache_status("bert-base-uncased")

        assert result is True, (
            f"expected True for a legacy bare name cached only unprefixed, "
            f"got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 15 (#2835): the startup gate gives a path-shaped name the directory
# remedy, not the pre-warm one-liner
# ---------------------------------------------------------------------------

class TestPathShapedNameGetsDirectoryRemedyNotPrewarmAdvice:
    def test_path_shaped_name_gets_directory_remedy_not_prewarm_advice(self, capsys):
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        server = _import_server()

        path = "/opt/models/all-MiniLM-L6-v2"

        with mock.patch.object(server._embedder, "is_available", return_value=True), \
             mock.patch.object(server, "_embedding_cache_status", return_value=False), \
             mock.patch.object(server, "_get_embedding_config", return_value={"model": path}):
            with pytest.raises(SystemExit) as exc_info:
                server._embedding_startup_gate()

        assert exc_info.value.code != 0, (
            f"expected non-zero exit, got {exc_info.value.code!r}"
        )

        captured = capsys.readouterr()
        assert "modules.json" in captured.err and "config.json" in captured.err, (
            f"expected the directory remedy naming modules.json/config.json "
            f"in stderr, got: {captured.err!r}"
        )
        assert (
            "from sentence_transformers import SentenceTransformer"
            not in captured.err
        ), (
            f"expected the pre-warm one-liner NOT present for a path-shaped "
            f"name, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 16 (#2841 round 1): clause 3's COMMON case -- the prefixed candidate is
# the one that hits. Tests 8 and 14 cover probe order and the bare fallback;
# nothing covered the ordinary production path where
# "sentence-transformers/<name>" is what is actually cached.
# ---------------------------------------------------------------------------

class TestBareNameHitsOnPrefixedCandidate:
    def test_bare_name_hits_on_prefixed_candidate(self):
        server = _import_server()

        prefixed = f"sentence-transformers/{MODEL}"
        probed = []

        def _fake(repo_id, filename):
            probed.append(repo_id)
            return "/fake/cache/config.json" if repo_id == prefixed else None

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache", side_effect=_fake
        ):
            result = server._embedding_cache_status(MODEL)

        assert result is True, (
            f"expected True when the prefixed candidate is cached, got "
            f"{result!r}"
        )
        # Short-circuit: the bare candidate must never be probed once the
        # prefixed one hits. Without this the test would still pass if the
        # implementation probed every candidate regardless.
        assert probed == [prefixed], (
            f"expected exactly one probe, of the prefixed id, got {probed!r}"
        )


# ---------------------------------------------------------------------------
# Test 17 (#2841 round 1): clause 2's BACKSLASH arm. Only the count("/") > 1
# arm had (incidental) coverage; a Windows-shaped name exercised nothing.
# ---------------------------------------------------------------------------

class TestBackslashNameIsRefusedWithoutConsultingTheHub:
    def test_backslash_name_is_refused_without_consulting_the_hub(self):
        server = _import_server()

        name = r"C:\models\all-MiniLM-L6-v2"
        assert not os.path.exists(name), (
            f"test precondition failed: {name!r} unexpectedly exists"
        )

        def _must_not_be_called(repo_id, filename):
            raise AssertionError(
                f"clause 2 should have refused {name!r} before any Hub "
                f"lookup, but try_to_load_from_cache was called with "
                f"{repo_id!r}"
            )

        with mock.patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_must_not_be_called,
        ):
            result = server._embedding_cache_status(name)

        assert result is False, (
            f"expected False for a backslash-containing name, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 18 (#2841 round 1): _looks_like_local_model_path in isolation. It was
# only exercised transitively, for one absolute-path input, via test 15 --
# while the PR body documents several boundary decisions for it that had no
# assertion anywhere.
# ---------------------------------------------------------------------------

class TestLooksLikeLocalModelPath:
    def test_hub_repo_ids_are_not_path_shaped(self):
        server = _import_server()
        for name in ("BAAI/bge-small-en", MODEL, "bert-base-uncased"):
            assert server._looks_like_local_model_path(name) is False, (
                f"{name!r} is a hub repo id, not a path"
            )

    def test_path_shaped_names_are_detected(self):
        server = _import_server()
        for name in ("/opt/models/x", "~/models/x", "./models/x",
                     "../models/x", r"C:\models\x"):
            assert server._looks_like_local_model_path(name) is True, (
                f"{name!r} should be recognised as path-shaped"
            )

    def test_bare_relative_path_is_a_known_accepted_edge(self):
        server = _import_server()
        # "models/foo" IS a real relative path, and this returns False for
        # it -- a deliberate accepted edge, not an endorsement. A single "/"
        # cannot be treated as path evidence without misreading every
        # org-qualified hub id ("BAAI/bge-small-en") as a directory, and
        # that misreading is the worse error: it would hand the wrong
        # remedy to the common case in order to serve a rare one. The
        # consequence here is bounded -- such a name is still REFUSED by
        # the gate, it just gets the pre-warm remedy instead of the
        # directory remedy. Pinned so the tradeoff fails loudly if someone
        # changes the rule without revisiting it.
        assert server._looks_like_local_model_path("models/foo") is False
