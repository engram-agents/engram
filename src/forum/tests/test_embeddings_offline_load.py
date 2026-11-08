"""Tests for offline-first embedding model load (#1762).

forum/embeddings.py._get_model() previously called
_SentenceTransformer(FORUM_EMBEDDING_MODEL) unconditionally, which triggers
HuggingFace Hub's online etag-check even for an already-cached model -- the
same defect fixed on the ENGRAM daemon side in #1682 (src/engram/engram_core.py).
Since src/forum does NOT import engram_core, it did not inherit that
protection and was independently vulnerable to the same ~83s stall (6 x 10s
retries + backoff) when HF Hub is slow or unreachable -- and forum's
warm_model() is called SYNCHRONOUSLY from create_app(), so the stall blocks
forum app startup entirely.

The fix mirrors #1682's pattern exactly:

  1. Try _SentenceTransformer(FORUM_EMBEDDING_MODEL, local_files_only=True)
     first -- a cached model loads with zero HF Hub network traffic.
  2. On a cache-miss (the offline attempt raises), fall back to a one-time
     online _SentenceTransformer(FORUM_EMBEDDING_MODEL) call so first-run /
     fresh-install behavior is preserved. A clear stderr message marks this
     path (investigation: no forum install/deploy script pre-downloads the
     model -- see install-forum-service.sh, no SentenceTransformer(...) call
     found).
  3. os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "1") is set at module
     import (forum/embeddings.py does not import engram_core, so it needs
     its own copy) so even a residual online etag-check times out fast
     rather than hanging ~83s.

Four test classes, mirroring tests/test_embedding_manager_offline_load.py:

  1. TestLocalFilesOnlyPassedOnSuccess -- mocks _SentenceTransformer to
     succeed on the first (offline) call; asserts it was called with
     local_files_only=True and that no fallback call happened.

  2. TestCacheMissFallsBackToOnlineDownload -- mocks _SentenceTransformer to
     raise on the local_files_only=True call and succeed on the second
     (online) call; asserts both calls happened in order, the second call
     has no local_files_only kwarg forcing offline, and the online-fallback
     message is logged to stderr. Also covers the case where BOTH attempts
     fail (falls through to the existing outer except-Exception degrade
     path unchanged).

  3. TestHfHubEtagTimeoutEnvSet -- asserts HF_HUB_ETAG_TIMEOUT is set in
     os.environ after importing forum.embeddings (module-level setdefault),
     and that setdefault() semantics are respected (an operator override
     already in the environment is not clobbered).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from forum import embeddings as emb

MODEL = emb.FORUM_EMBEDDING_MODEL
SNAP = os.path.abspath("/stub/verified-snapshot")


def _install_resolver(monkeypatch, fail_local=False):
    """#4468: stub the pinned-snapshot resolver; returns the list of `online`
    flags it was called with. fail_local simulates a local-cache miss."""
    online_flags = []

    def resolve(online=False):
        online_flags.append(online)
        if not online and fail_local:
            raise OSError(
                f"Model '{MODEL}' not found in local HF cache "
                f"(local_files_only=True)"
            )
        return Path(SNAP)

    monkeypatch.setattr(emb, "_resolve_pinned_snapshot", resolve)
    return online_flags


@pytest.fixture(autouse=True)
def _reset_model_singleton(monkeypatch):
    """Ensure a clean _model singleton + FORUM_NO_EMBEDDINGS/_ST_AVAILABLE state
    for every test in this file, and restore the real singleton afterward so
    other test modules in the same process aren't affected."""
    monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
    monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
    # #4468: the fake constructors here carry no model bytes. Stub the
    # verifier and reset the refusal/verified memo; each test installs its own
    # snapshot resolver (the real ones are tested in test_embeddings_pin_4468).
    monkeypatch.setattr(emb, "_verify_pinned_snapshot", lambda snap: None)
    monkeypatch.setattr(emb, "_model_refused", False)
    monkeypatch.setattr(emb, "_verified_snapshot", None, raising=False)
    original_model = emb._model
    emb._model = None
    yield
    emb._model = original_model


class TestLocalFilesOnlyPassedOnSuccess:
    def test_local_files_only_passed_on_success(self, monkeypatch):
        calls = []

        class _FakeModel:
            pass

        def _fake_sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeModel()

        monkeypatch.setattr(emb, "_SentenceTransformer", _fake_sentence_transformer)
        flags = _install_resolver(monkeypatch)

        model = emb._get_model()

        assert model is not None, "expected model to load successfully"
        # #4468: the cached path resolves the snapshot LOCALLY only (zero
        # network, #1762) and constructs once, from the verified path.
        assert flags == [False], f"expected one local-only resolve, got {flags}"
        assert len(calls) == 1, (
            f"expected exactly one _SentenceTransformer call (offline success, "
            f"no fallback needed), got {len(calls)}: {calls}"
        )
        args, kwargs = calls[0]
        assert args == (SNAP,), f"expected the verified absolute path, got {args}"
        assert "revision" not in kwargs


class TestCacheMissFallsBackToOnlineDownload:
    def test_cache_miss_falls_back_to_online_download(self, monkeypatch, capsys):
        calls = []

        class _FakeModel:
            pass

        def _fake_sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeModel()

        monkeypatch.setattr(emb, "_SentenceTransformer", _fake_sentence_transformer)
        # #4468: the cache-miss is simulated at the snapshot resolver (a
        # local_files_only resolve raises; the online one succeeds).
        flags = _install_resolver(monkeypatch, fail_local=True)

        model = emb._get_model()

        assert model is not None, (
            "expected the online fallback to succeed and populate the singleton"
        )
        assert flags == [False, True], (
            f"expected a local resolve, then one online resolve, got {flags}"
        )
        assert len(calls) == 1, (
            f"expected one _SentenceTransformer call, made from the downloaded "
            f"and verified snapshot path, got {len(calls)}: {calls}"
        )
        assert calls[0][0] == (SNAP,)

        captured = capsys.readouterr()
        assert "downloading once" in captured.err, (
            f"expected an online-fallback log message on stderr, got: "
            f"{captured.err!r}"
        )

    def test_double_failure_falls_through_to_existing_degrade_path(self, monkeypatch):
        """If BOTH the offline attempt and the online fallback fail, the
        existing outer except-Exception path still applies: _get_model()
        returns None (unchanged behavior from before #1762)."""

        def _always_raise(*args, **kwargs):
            raise RuntimeError("simulated total load failure")

        monkeypatch.setattr(emb, "_SentenceTransformer", _always_raise)
        _install_resolver(monkeypatch)

        model = emb._get_model()

        assert model is None


def _forget_forum_embeddings(monkeypatch):
    """#4502: make the next `import forum.embeddings` a genuine fresh import,
    and undo it on teardown. Both places the module lives are recorded: the
    sys.modules entry and the `embeddings` attribute on the `forum` package,
    so afterwards both are the original module object again (the one
    forum.server.emb_mod holds), not the fresh one."""
    forum_pkg = sys.modules["forum"]
    monkeypatch.setattr(forum_pkg, "embeddings", forum_pkg.embeddings)
    monkeypatch.delitem(sys.modules, "forum.embeddings", raising=False)


class TestHfHubEtagTimeoutEnvSet:
    def test_hf_hub_etag_timeout_env_set(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_ETAG_TIMEOUT", raising=False)
        _forget_forum_embeddings(monkeypatch)
        import forum.embeddings as fresh_emb  # noqa: F401
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "1", (
            f"expected forum.embeddings import to set HF_HUB_ETAG_TIMEOUT=1 via "
            f"os.environ.setdefault, got: {os.environ.get('HF_HUB_ETAG_TIMEOUT')!r}"
        )

    def test_hf_hub_etag_timeout_respects_operator_override(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_ETAG_TIMEOUT", "30")
        _forget_forum_embeddings(monkeypatch)
        import forum.embeddings as fresh_emb  # noqa: F401
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "30", (
            "setdefault() must not clobber an operator-set override"
        )
