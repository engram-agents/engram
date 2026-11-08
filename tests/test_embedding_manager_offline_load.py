"""Tests for offline-first embedding model load (#1682).

EmbeddingManager._load_model() previously called
SentenceTransformer(model_name) unconditionally, which triggers HuggingFace
Hub's online etag-check even for an already-cached model — the source of a
recurring ~83s stall (6 x 10s retries + backoff) when HF Hub is slow or
unreachable. The fix:

  1. Try SentenceTransformer(model_name, local_files_only=True) first — a
     cached model loads with zero HF Hub network traffic.
  2. On a cache-miss (the offline attempt raises), fall back to a one-time
     online SentenceTransformer(model_name) call so first-run / fresh-install
     behavior (the daemon's own first cold launch downloading the model) is
     preserved. A clear stderr message marks this path.
  3. os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "1") is set at module
     import so even a residual online etag-check times out fast rather than
     hanging ~83s.

Three tests:

  1. test_local_files_only_passed_on_success — mocks SentenceTransformer to
     succeed on the first (offline) call; asserts it was called with
     local_files_only=True and that no fallback call happened.

  2. test_cache_miss_falls_back_to_online_download — mocks
     SentenceTransformer to raise on the local_files_only=True call and
     succeed on the second (online) call; asserts both calls happened in
     order, the second call has no local_files_only kwarg forcing offline,
     and the online-fallback message is logged to stderr.

  3. test_hf_hub_etag_timeout_env_set — asserts HF_HUB_ETAG_TIMEOUT is set
     in os.environ after importing engram_core (module-level setdefault),
     and that setdefault() semantics are respected (an operator override
     already in the environment is not clobbered).
"""

from __future__ import annotations

import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "all-MiniLM-L6-v2"
SNAP = os.path.abspath("/stub/verified-snapshot")


def _stub_resolver(engram_core, fail_local=False):
    """#4468: the pinned model is loaded from a resolved, verified snapshot
    path. Returns (patcher, online_flags): the resolver stub records the
    `online` flag of each call; fail_local simulates a local-cache miss."""
    from pathlib import Path
    flags = []

    def resolve(model_name, online=False):
        flags.append(online)
        if not online and fail_local:
            raise OSError(
                f"Model '{model_name}' not found in local HF cache "
                f"(local_files_only=True)"
            )
        return Path(SNAP)

    return mock.patch.object(engram_core, "_resolve_pinned_snapshot", resolve), flags


def _fresh_engram_core():
    """Import engram_core with clean module state (mirrors fresh_module pattern)."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import engram_core
    return engram_core


class TestLocalFilesOnlyPassedOnSuccess:
    def test_local_files_only_passed_on_success(self):
        engram_core = _fresh_engram_core()

        calls = []

        class _FakeModel:
            pass

        def _fake_sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeModel()

        fake_st_module = types.ModuleType("sentence_transformers")
        fake_st_module.SentenceTransformer = _fake_sentence_transformer

        mgr = engram_core.EmbeddingManager()
        resolver, flags = _stub_resolver(engram_core)

        with resolver, mock.patch.object(engram_core, "_verify_pinned_snapshot", return_value=None), mock.patch.dict("sys.modules", {"sentence_transformers": fake_st_module}):
            mgr._load_model(MODEL)

        assert mgr._model is not None, "expected model to load successfully"
        assert flags == [False], f"expected one local-only resolve, got {flags}"
        assert mgr._model_name == MODEL
        assert len(calls) == 1, (
            f"expected exactly one SentenceTransformer call (offline success, "
            f"no fallback needed), got {len(calls)}: {calls}"
        )
        args, kwargs = calls[0]
        # #4468: constructed FROM the verified absolute snapshot path.
        assert args == (SNAP,), f"expected the verified absolute path, got {args}"
        assert "revision" not in kwargs


class TestCacheMissFallsBackToOnlineDownload:
    def test_cache_miss_falls_back_to_online_download(self, capsys):
        engram_core = _fresh_engram_core()

        calls = []

        class _FakeModel:
            pass

        def _fake_sentence_transformer(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeModel()

        fake_st_module = types.ModuleType("sentence_transformers")
        fake_st_module.SentenceTransformer = _fake_sentence_transformer

        mgr = engram_core.EmbeddingManager()
        # #4468: the genuine cache-miss is simulated at the snapshot resolver
        # (the local_files_only resolve raises; the online one succeeds).
        resolver, flags = _stub_resolver(engram_core, fail_local=True)

        with resolver, mock.patch.object(engram_core, "_verify_pinned_snapshot", return_value=None), mock.patch.dict("sys.modules", {"sentence_transformers": fake_st_module}):
            mgr._load_model(MODEL)

        assert mgr._model is not None, (
            "expected the online fallback to succeed and populate self._model"
        )
        assert mgr._model_name == MODEL
        assert flags == [False, True], (
            f"expected a local resolve, then one online resolve, got {flags}"
        )
        assert len(calls) == 1 and calls[0][0] == (SNAP,), (
            f"expected one SentenceTransformer call from the downloaded and "
            f"verified path, got {calls}"
        )

        captured = capsys.readouterr()
        assert "downloading once" in captured.err, (
            f"expected an online-fallback log message on stderr, got: "
            f"{captured.err!r}"
        )

    def test_double_failure_falls_through_to_existing_failure_path(self):
        """If BOTH the offline attempt and the online fallback fail, the
        existing outer except-Exception path still applies: self._model is
        None and the model name is recorded in _failed_models (unchanged
        behavior from before #1682)."""
        engram_core = _fresh_engram_core()

        def _always_raise(*args, **kwargs):
            raise RuntimeError("simulated total load failure")

        fake_st_module = types.ModuleType("sentence_transformers")
        fake_st_module.SentenceTransformer = _always_raise

        mgr = engram_core.EmbeddingManager()
        resolver, _flags = _stub_resolver(engram_core)

        with resolver, mock.patch.object(engram_core, "_verify_pinned_snapshot", return_value=None), mock.patch.dict("sys.modules", {"sentence_transformers": fake_st_module}):
            mgr._load_model(MODEL)

        assert mgr._model is None
        assert mgr._model_name is None
        assert MODEL in mgr._failed_models


class TestHfHubEtagTimeoutEnvSet:
    def test_hf_hub_etag_timeout_env_set(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_ETAG_TIMEOUT", raising=False)
        engram_core = _fresh_engram_core()
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "1", (
            f"expected engram_core import to set HF_HUB_ETAG_TIMEOUT=1 via "
            f"os.environ.setdefault, got: {os.environ.get('HF_HUB_ETAG_TIMEOUT')!r}"
        )

    def test_hf_hub_etag_timeout_respects_operator_override(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_ETAG_TIMEOUT", "30")
        engram_core = _fresh_engram_core()
        assert os.environ.get("HF_HUB_ETAG_TIMEOUT") == "30", (
            "setdefault() must not clobber an operator-set override"
        )
