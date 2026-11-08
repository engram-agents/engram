"""Contract tests for the forum embedding device + CPU thread cap.

Mirrors #3905 (engram side, #3898) for src/forum/embeddings.py:

  FORUM_EMBEDDING_DEVICE       unset or empty -> "cpu"; otherwise exactly
                               ``cpu`` | ``cuda`` | ``cuda:N`` (case-sensitive,
                               no stripping). Anything else REFUSES at load with
                               a named ForumEmbeddingConfigError; _get_model()
                               returns None and never constructs the model.
  FORUM_EMBEDDING_CPU_THREADS  unset -> 4; an integer 1..1024 is used; an
                               invalid value prints ONE stderr line and uses 4.

The device reaches the _SentenceTransformer(...) call on both the cached path
and the cache-miss path (#4468: the miss is now handled at the snapshot
download, so there is one constructor call, made from the verified path). The thread cap is torch.set_num_threads(n), applied once per
successful load iff the LOADED model reports a CPU device.

Cell granularity is part of the contract: the fallback's ``device=`` is pinned
by exactly ONE test function (test_env_device_reaches_the_fallback_path), and
"not on CUDA" by exactly ONE (test_cpu_cap_is_not_applied_on_cuda).

torch is never imported for real here: a recording stub is placed in
sys.modules.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from forum import embeddings as emb

MODEL = emb.FORUM_EMBEDDING_MODEL
DEV_KEY = "FORUM_EMBEDDING_DEVICE"
THREADS_KEY = "FORUM_EMBEDDING_CPU_THREADS"


class _Dev:
    def __init__(self, type_):
        self.type = type_


class _CpuModel:
    device = _Dev("cpu")


class _CudaModel:
    device = _Dev("cuda")


class _NoDeviceModel:
    pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
    monkeypatch.delenv(DEV_KEY, raising=False)
    monkeypatch.delenv(THREADS_KEY, raising=False)
    monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
    # #4468: these tests load the default model through a fake constructor with
    # no model bytes. The loader now resolves the pinned snapshot, hashes it,
    # and constructs from that path; stub the resolver and the verifier so a
    # host without the HF cache stays hermetic (the real ones are tested in
    # test_embeddings_pin_4468).
    monkeypatch.setattr(emb, "_verify_pinned_snapshot", lambda snap: None)
    monkeypatch.setattr(emb, "_model_refused", False)
    monkeypatch.setattr(emb, "_verified_snapshot", None, raising=False)
    original = emb._model
    emb._model = None
    yield
    emb._model = original


@pytest.fixture
def torch_calls(monkeypatch):
    """Install a stub torch recording set_num_threads calls."""
    calls = []
    stub = types.ModuleType("torch")
    stub.set_num_threads = lambda n: calls.append(n)
    monkeypatch.setitem(sys.modules, "torch", stub)
    return calls


SNAP = os.path.abspath("/stub/verified-snapshot")


def _install(monkeypatch, model_cls=_CpuModel, fail_offline=False, fail_online=False):
    """Fake snapshot resolver + _SentenceTransformer; returns the recorded
    constructor (args, kwargs) list. fail_offline / fail_online make the
    LOCAL / ONLINE snapshot resolution raise (#4468: the cache miss and its
    online fallback live at the snapshot download, not the constructor)."""
    calls = []

    def resolve(online=False):
        if not online and fail_offline:
            raise OSError("simulated cache miss")
        if online and fail_online:
            raise RuntimeError("simulated online failure")
        return Path(SNAP)

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return model_cls()

    monkeypatch.setattr(emb, "_resolve_pinned_snapshot", resolve)
    monkeypatch.setattr(emb, "_SentenceTransformer", fake)
    return calls


def _config_error_cls():
    # Referenced lazily so this file imports before the class exists.
    return getattr(emb, "ForumEmbeddingConfigError", None)


# ---------------------------------------------------------------------------
# Device reaches the constructor
# ---------------------------------------------------------------------------

def test_default_device_is_cpu_on_the_offline_path(monkeypatch):
    calls = _install(monkeypatch)
    assert emb._get_model() is not None
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (SNAP,)
    assert "revision" not in kwargs
    assert kwargs.get("device") == "cpu", kwargs


def test_env_device_reaches_the_offline_path(monkeypatch):
    monkeypatch.setenv(DEV_KEY, "cuda:1")
    calls = _install(monkeypatch)
    assert emb._get_model() is not None
    assert calls[0][1].get("device") == "cuda:1", calls[0][1]


@pytest.mark.parametrize("env", [None, "cuda:1"])
def test_env_device_reaches_the_fallback_path(monkeypatch, env):
    if env is not None:
        monkeypatch.setenv(DEV_KEY, env)
    calls = _install(monkeypatch, fail_offline=True)
    assert emb._get_model() is not None
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (SNAP,)
    assert "local_files_only" not in kwargs
    assert kwargs.get("device") == (env or "cpu"), kwargs


@pytest.mark.parametrize("value", ["cpu", "cuda", "cuda:0", "cuda:12"])
def test_valid_device_forms_load(monkeypatch, value):
    monkeypatch.setenv(DEV_KEY, value)
    calls = _install(monkeypatch)
    assert emb._get_model() is not None
    assert calls[0][1].get("device") == value


def test_empty_device_means_default_cpu(monkeypatch, capsys):
    monkeypatch.setenv(DEV_KEY, "")
    calls = _install(monkeypatch)
    assert emb._get_model() is not None
    assert len(calls) == 1
    assert calls[0][1].get("device") == "cpu", calls[0][1]
    assert DEV_KEY not in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad",
    [
        "gpu", "auto", "CPU", "CUDA", "cuda:", "cuda:x", "cuda:-1", " ", " cpu", "cpu ",
        # Exact whole-string match: a trailing newline must not slip past `$`.
        "cpu\n", "cuda:1\n", "cpu\r", "\ncpu",
        # Non-ASCII digit (ARABIC-INDIC DIGIT ONE): [0-9], not \d.
        "cuda:١",
    ],
)
def test_invalid_device_refuses_naming_key_and_value(monkeypatch, capsys, bad):
    monkeypatch.setenv(DEV_KEY, bad)
    calls = _install(monkeypatch)
    assert emb._get_model() is None
    assert emb._model is None
    assert len(calls) == 0, calls
    err = capsys.readouterr().err
    assert DEV_KEY in err
    assert repr(bad) in err


def test_invalid_device_error_type_is_named_config_error(monkeypatch):
    """The validator raises ForumEmbeddingConfigError (a ValueError)."""
    cls = _config_error_cls()
    assert cls is not None, "ForumEmbeddingConfigError is not defined"
    assert issubclass(cls, ValueError)


# ---------------------------------------------------------------------------
# CPU thread cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["offline", "fallback"])
@pytest.mark.parametrize("env,expected", [(None, 4), ("2", 2)])
def test_cpu_cap_is_applied_on_cpu(monkeypatch, torch_calls, path, env, expected):
    if env is not None:
        monkeypatch.setenv(THREADS_KEY, env)
    _install(monkeypatch, model_cls=_CpuModel, fail_offline=(path == "fallback"))
    assert emb._get_model() is not None
    assert torch_calls == [expected]


def test_cpu_cap_is_not_applied_on_cuda(monkeypatch, torch_calls):
    _install(monkeypatch, model_cls=_CudaModel)
    assert emb._get_model() is not None
    assert torch_calls == []


def test_model_without_device_is_left_alone(monkeypatch, torch_calls):
    _install(monkeypatch, model_cls=_NoDeviceModel)
    assert emb._get_model() is not None
    assert torch_calls == []


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "4.5", "", " "])
def test_invalid_thread_value_falls_back_to_4_with_one_stderr_line(
    monkeypatch, capsys, torch_calls, bad
):
    monkeypatch.setenv(THREADS_KEY, bad)
    _install(monkeypatch)
    assert emb._get_model() is not None
    lines = [ln for ln in capsys.readouterr().err.splitlines() if THREADS_KEY in ln]
    assert len(lines) == 1, lines
    assert repr(bad) in lines[0]
    assert "4" in lines[0]
    assert torch_calls == [4]


def _threads_err_lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if THREADS_KEY in ln]


def test_threads_unset_is_4_and_silent(monkeypatch, capsys):
    assert emb._embedding_cpu_threads() == 4
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "raw,expected",
    [("1", 1), ("4", 4), ("64", 64), ("0004", 4), ("1000", 1000), ("1024", 1024)],
)
def test_threads_valid_values_are_used_silently(monkeypatch, capsys, raw, expected):
    monkeypatch.setenv(THREADS_KEY, raw)
    assert emb._embedding_cpu_threads() == expected
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "raw",
    [
        "0", "1025", "99999999999", "9" * 5000, "", "  4", "4\n", "+4", "-1",
        "4.0", "0x10", "４", "1e3", "00004", "9" * 4300, "9" * 4301,
    ],
)
def test_threads_everything_else_defaults_to_4_with_one_bounded_line(
    monkeypatch, capsys, raw
):
    monkeypatch.setenv(THREADS_KEY, raw)
    assert emb._embedding_cpu_threads() == 4  # never raises
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith(f"[forum] {THREADS_KEY}={raw[:40]!r} is not ")
    assert "an integer from 1 to 1024" in line
    assert line.endswith("; using 4")
    assert len(line) < 400  # a huge raw value is never echoed whole
    if len(raw) > 40:
        assert str(len(raw)) in line


def test_failed_load_applies_no_cap(monkeypatch, torch_calls):
    _install(monkeypatch, fail_offline=True, fail_online=True)
    assert emb._get_model() is None
    assert torch_calls == []


def test_torch_missing_does_not_break_the_load(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> ImportError
    _install(monkeypatch, model_cls=_CpuModel)
    assert emb._get_model() is not None
