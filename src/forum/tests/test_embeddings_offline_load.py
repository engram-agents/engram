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
from unittest import mock

import pytest

from forum import embeddings as emb

MODEL = emb.FORUM_EMBEDDING_MODEL


@pytest.fixture(autouse=True)
def _reset_model_singleton(monkeypatch):
    """Ensure a clean _model singleton + FORUM_NO_EMBEDDINGS/_ST_AVAILABLE state
    for every test in this file, and restore the real singleton afterward so
    other test modules in the same process aren't affected."""
    monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
    monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
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

        model = emb._get_model()

        assert model is not None, "expected model to load successfully"
        assert len(calls) == 1, (
            f"expected exactly one _SentenceTransformer call (offline success, "
            f"no fallback needed), got {len(calls)}: {calls}"
        )
        args, kwargs = calls[0]
        assert kwargs.get("local_files_only") is True, (
            f"expected local_files_only=True on the (only) call, got kwargs={kwargs}"
        )
        assert args == (MODEL,) or (args and args[0] == MODEL)


class TestCacheMissFallsBackToOnlineDownload:
    def test_cache_miss_falls_back_to_online_download(self, monkeypatch, capsys):
        calls = []

        class _FakeModel:
            pass

        def _fake_sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            if kwargs.get("local_files_only"):
                # Simulate a genuine cache-miss under local_files_only=True
                # (huggingface_hub raises an OSError-family exception here;
                # any Exception is treated as cache-miss by the fallback).
                raise OSError(
                    f"Model '{MODEL}' not found in local HF cache "
                    f"(local_files_only=True)"
                )
            return _FakeModel()

        monkeypatch.setattr(emb, "_SentenceTransformer", _fake_sentence_transformer)

        model = emb._get_model()

        assert model is not None, (
            "expected the online fallback to succeed and populate the singleton"
        )
        assert len(calls) == 2, (
            f"expected two _SentenceTransformer calls (offline attempt, then "
            f"online fallback), got {len(calls)}: {calls}"
        )
        first_args, first_kwargs = calls[0]
        assert first_kwargs.get("local_files_only") is True, (
            f"expected the first attempt to be offline (local_files_only=True), "
            f"got kwargs={first_kwargs}"
        )
        second_args, second_kwargs = calls[1]
        assert not second_kwargs.get("local_files_only"), (
            f"expected the fallback attempt to NOT force local_files_only "
            f"(online download), got kwargs={second_kwargs}"
        )

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

        model = emb._get_model()

        assert model is None


class TestHfHubEtagTimeoutEnvSet:
    def test_hf_hub_etag_timeout_env_set(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_ETAG_TIMEOUT", raising=False)
        for key in list(sys.modules):
            if key == "forum.embeddings":
                del sys.modules[key]
        import forum.embeddings as fresh_emb  # noqa: F401
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "1", (
            f"expected forum.embeddings import to set HF_HUB_ETAG_TIMEOUT=1 via "
            f"os.environ.setdefault, got: {os.environ.get('HF_HUB_ETAG_TIMEOUT')!r}"
        )

    def test_hf_hub_etag_timeout_respects_operator_override(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_ETAG_TIMEOUT", "30")
        for key in list(sys.modules):
            if key == "forum.embeddings":
                del sys.modules[key]
        import forum.embeddings as fresh_emb  # noqa: F401
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "30", (
            "setdefault() must not clobber an operator-set override"
        )
