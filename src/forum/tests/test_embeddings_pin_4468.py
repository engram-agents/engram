"""#4468 (part of #4447): the forum embedding loader pins the model revision,
checksums the snapshot, and loads FROM the verified snapshot path.

No network and no real model. A temp "snapshot" whose files hash into a
monkeypatched pin table stands in for the HF cache; huggingface_hub's
snapshot_download is stubbed. The constructor stub MIMICS sentence-
transformers' name resolution (a first argument that exists on disk is loaded
as that directory; otherwise the name resolves in the hub cache, which
SENTENCE_TRANSFORMERS_HOME redirects) and records WHICH directory it loaded,
so the "verified bytes are the loaded bytes" cells are a real control.
FORUM_NO_EMBEDDINGS is unset inside each cell and module state is restored.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from forum import embeddings as emb

HEX64 = r"[0-9a-f]{64}"
FILES = {
    "model.safetensors": b"weights",
    "config.json": b"{}",
    "1_Pooling/config.json": b"{pool}",
    "vocab.txt": b"good vocab",
}


class _Vec(list):
    def tolist(self):
        return list(self)


class _Model:
    def encode(self, text, **kw):
        return _Vec([0.0, 1.0])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_tree(root: Path, overrides: dict | None = None) -> Path:
    for rel, data in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((overrides or {}).get(rel, data))
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_refused", False, raising=False)
    monkeypatch.setattr(emb, "_verified_snapshot", None, raising=False)

    snap = _write_tree(tmp_path / "hubcache" / "snap")
    pin = {
        "repo_id": "sentence-transformers/all-MiniLM-L6-v2",
        "revision": "a" * 40,
        "sha256": {rel: _sha(d) for rel, d in FILES.items()},
    }
    monkeypatch.setattr(emb, "_MINILM_PIN", pin, raising=False)

    st_calls = []   # (args, kwargs) per constructor call
    loaded = []     # the directory each constructor call "loaded"
    state = {"raises_local": False, "raises_online": False, "snap": snap,
             "online_snap": None, "fail_construct": False}

    def fake_st(*args, **kwargs):
        st_calls.append((args, kwargs))
        if state["fail_construct"]:
            raise OSError("constructor failed")
        first = args[0]
        if os.path.exists(first):          # sentence-transformers: a path wins
            loaded.append(Path(first))
        elif os.environ.get("SENTENCE_TRANSFORMERS_HOME"):
            loaded.append(Path(os.environ["SENTENCE_TRANSFORMERS_HOME"]))
        else:                              # default hub cache
            loaded.append(state["snap"])
        return _Model()

    monkeypatch.setattr(emb, "_SentenceTransformer", fake_st)

    hub = types.ModuleType("huggingface_hub")
    snap_calls = []

    def snapshot_download(repo_id, **kwargs):
        snap_calls.append((repo_id, kwargs))
        if kwargs.get("local_files_only"):
            if state["raises_local"]:
                raise OSError("not in local cache")
        elif state["raises_online"]:
            raise OSError("no network")
        if not kwargs.get("local_files_only") and state["online_snap"] is not None:
            return str(state["online_snap"])
        return str(state["snap"])

    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    hashed = []
    real_verify = emb._verify_pinned_snapshot

    def recording_verify(snap_arg):
        hashed.append(Path(snap_arg))
        return real_verify(snap_arg)

    monkeypatch.setattr(emb, "_verify_pinned_snapshot", recording_verify)
    return types.SimpleNamespace(
        st_calls=st_calls, loaded=loaded, snap_calls=snap_calls, state=state,
        snap=snap, pin=pin, hashed=hashed,
    )


def test_good_snapshot_loads_and_verifies_once(env):
    assert emb.encode("a") == [0.0, 1.0]
    assert emb.encode("b") == [0.0, 1.0]
    assert emb.encode("c") == [0.0, 1.0]
    assert len(env.snap_calls) == 1
    assert len(env.st_calls) == 1
    assert len(env.hashed) == 1
    assert env.snap_calls[0][1].get("local_files_only") is True
    assert env.snap_calls[0][1].get("revision") == env.pin["revision"]


def test_flipped_byte_refuses_and_is_remembered(env, capsys, monkeypatch):
    (env.snap / "config.json").write_bytes(b"{X")
    assert emb.encode("a") is None
    assert emb._model is None
    err = capsys.readouterr().err
    assert "Refusing embedding model" in err
    assert "config.json" in err
    assert env.pin["sha256"]["config.json"] in err
    assert _sha(b"{X") in err
    assert len(re.findall(HEX64, err)) >= 2
    n_snap = len(env.snap_calls)
    assert len(env.st_calls) == 0 and n_snap == 1

    # Second call: no resolve, no re-hash, no construct. (Counting stubs, not
    # raising ones: _get_model swallows exceptions from the constructor, so a
    # raise would read as "no model" and hide a reload.)
    stub_calls = []

    def counting(*a, **k):
        stub_calls.append(1)
        return _Model()

    monkeypatch.setattr(emb, "_SentenceTransformer", counting)
    monkeypatch.setattr(
        emb, "_verify_pinned_snapshot", lambda s: stub_calls.append(1)
    )
    assert emb.encode("b") is None
    assert emb.available() is False
    assert stub_calls == []
    assert len(env.snap_calls) == n_snap


def test_missing_file_refuses(env, capsys):
    (env.snap / "1_Pooling" / "config.json").unlink()
    assert emb.encode("a") is None
    assert emb._model is None
    err = capsys.readouterr().err
    assert "Refusing embedding model" in err
    assert "1_Pooling/config.json" in err
    assert env.st_calls == []


def test_unresolvable_snapshot_is_a_retryable_load_failure(env, capsys):
    """Neither the local cache nor the online download yields the snapshot:
    a LOAD failure (as the online constructor failure was), not a remembered
    refusal."""
    env.state["raises_local"] = True
    env.state["raises_online"] = True
    assert emb.encode("a") is None
    assert emb._model is None
    assert emb._model_refused is False
    err = capsys.readouterr().err
    assert "Failed to load embedding model" in err and "no network" in err
    assert env.st_calls == []


def test_constructor_receives_the_absolute_verified_path_not_the_name(env):
    assert emb.encode("a") == [0.0, 1.0]
    (args, kwargs), = env.st_calls
    assert os.path.isabs(args[0])
    assert args[0] != emb.FORUM_EMBEDDING_MODEL
    assert "revision" not in kwargs
    assert env.hashed == [Path(args[0])]
    assert env.loaded == [env.snap]


def test_model_named_directory_in_cwd_is_not_loaded(env, tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    _write_tree(cwd / emb.FORUM_EMBEDDING_MODEL, {"vocab.txt": b"TAMPERED"})
    monkeypatch.chdir(cwd)
    assert emb.encode("a") == [0.0, 1.0]
    assert env.loaded == [env.snap]
    assert (env.loaded[0] / "vocab.txt").read_bytes() == b"good vocab"


def test_sentence_transformers_home_cannot_redirect_the_load(env, tmp_path, monkeypatch):
    tampered = _write_tree(tmp_path / "sthome", {"vocab.txt": b"TAMPERED"})
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(tampered))
    assert emb.encode("a") == [0.0, 1.0]
    assert env.loaded == [env.snap]


def test_verification_happens_before_construction(env):
    (env.snap / "vocab.txt").write_bytes(b"TAMPERED")
    assert emb.encode("a") is None
    assert env.st_calls == []          # the constructor was never called
    assert emb._model is None


def test_cache_miss_downloads_the_pinned_revision_then_verifies_then_constructs(env):
    env.state["raises_local"] = True
    assert emb.encode("a") == [0.0, 1.0]
    assert [bool(k.get("local_files_only")) for _r, k in env.snap_calls] == [True, False]
    assert env.snap_calls[1][1].get("revision") == env.pin["revision"]
    assert len(env.hashed) == 1
    (args, _kw), = env.st_calls
    assert os.path.isabs(args[0]) and env.hashed == [Path(args[0])]


def test_failed_constructor_is_retryable_and_not_rehashed(env):
    env.state["fail_construct"] = True
    assert emb.encode("a") is None
    assert emb._model_refused is False
    env.state["fail_construct"] = False
    assert emb.encode("b") == [0.0, 1.0]
    assert len(env.hashed) == 1        # verified once, reused on the retry
    assert len(env.st_calls) == 2


def test_model_is_not_visible_while_unverified(env, monkeypatch):
    seen = []

    def stub(snap):
        seen.append(emb._model)
        return None

    monkeypatch.setattr(emb, "_verify_pinned_snapshot", stub, raising=False)
    assert emb.encode("a") == [0.0, 1.0]
    assert seen == [None]
    assert emb._model is not None


def test_unexpected_verification_exception_is_remembered(env, capsys, monkeypatch):
    verify_calls = []

    def verify(snap):
        verify_calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(emb, "_verify_pinned_snapshot", verify)
    assert emb.encode("a") is None
    assert emb.encode("b") is None
    assert emb._model is None
    assert len(verify_calls) == 1
    assert env.st_calls == []
    err = capsys.readouterr().err
    assert "Refusing embedding model" in err
    assert "RuntimeError" in err


def test_refusal_line_carries_the_restart_hint(env, capsys):
    (env.snap / "config.json").write_bytes(b"{X")
    assert emb.encode("a") is None
    assert "restart the forum" in capsys.readouterr().err


def test_constructor_failure_is_not_remembered(env):
    """A failed CONSTRUCTOR stays retryable: it is not a verification problem
    and sets no refusal flag."""
    env.state["fail_construct"] = True
    assert emb.encode("a") is None
    assert emb._model_refused is False


# ---- fold: online download is limited to the ten pinned files; partial heals ----


def _partial_snapshot(tmp_path, keep=("config.json",)):
    """A snapshot directory holding only some of the pinned files (an
    interrupted first download)."""
    part = tmp_path / "partial"
    for rel in keep:
        p = part / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(FILES[rel])
    return part


def test_online_download_is_limited_to_the_ten_pinned_files(env):
    env.state["raises_local"] = True
    assert emb.encode("a") == [0.0, 1.0]
    local, online = env.snap_calls
    assert "allow_patterns" not in local[1]
    assert online[1].get("allow_patterns") == list(env.pin["sha256"])


def test_partial_snapshot_heals_with_one_online_resolve(env, tmp_path):
    env.state["snap"] = _partial_snapshot(tmp_path)
    env.state["online_snap"] = env.snap
    assert emb.encode("a") == [0.0, 1.0]
    flags = [bool(k.get("local_files_only")) for _r, k in env.snap_calls]
    assert flags == [True, False]
    assert env.snap_calls[1][1].get("allow_patterns") == list(env.pin["sha256"])
    assert len(env.hashed) == 2          # the partial dir, then the healed one
    (args, _kw), = env.st_calls
    assert Path(args[0]) == env.snap and env.loaded == [env.snap]


def test_partial_snapshot_still_missing_after_the_heal_is_a_refusal(env, tmp_path, capsys):
    part = _partial_snapshot(tmp_path)
    env.state["snap"] = part
    env.state["online_snap"] = part      # the online resolve cannot complete it
    assert emb.encode("a") is None
    assert emb._model_refused is True
    assert env.st_calls == []
    assert len([1 for _r, k in env.snap_calls if not k.get("local_files_only")]) == 1
    err = capsys.readouterr().err
    assert "Refusing embedding model" in err and "missing" in err
    assert emb.encode("b") is None
    assert len(env.snap_calls) == 2      # remembered: no further resolve


def test_checksum_mismatch_is_never_healed_online(env):
    (env.snap / "config.json").write_bytes(b"{X")
    assert emb.encode("a") is None
    assert emb._model_refused is True
    assert env.st_calls == []
    assert all(k.get("local_files_only") for _r, k in env.snap_calls)


def test_thread_cap_failure_is_retryable_and_not_rehashed(env, monkeypatch):
    """Documented rule: only a VERIFICATION failure is a remembered refusal;
    a failure to construct or to cap threads is retryable. The verified path
    is memoised, so the retry does not re-hash."""
    calls = []

    def flaky(model, threads):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("cap failed")

    monkeypatch.setattr(emb, "_cap_cpu_threads", flaky)
    assert emb.encode("a") is None
    assert emb._model_refused is False and emb._model is None
    assert emb.encode("b") == [0.0, 1.0]     # retried and now loaded
    assert len(env.hashed) == 1              # no re-hash: the path is memoised


# ---- fold B2: the snapshot directory's ENTRY SET is checked, not just a list --


def _no_online(env):
    return all(k.get("local_files_only") for _r, k in env.snap_calls)


def test_extra_top_level_file_is_refused_without_online_or_construct(env, capsys):
    (env.snap / "adapter_config.json").write_text("{}")
    assert emb.encode("a") is None
    assert emb._model_refused is True and env.st_calls == []
    assert _no_online(env)
    err = capsys.readouterr().err
    assert "Refusing embedding model" in err and "adapter_config.json" in err
    n = len(env.snap_calls)
    assert emb.encode("b") is None      # remembered: no resolve, no re-walk
    assert len(env.snap_calls) == n and len(env.hashed) == 1


def test_extra_file_in_a_nested_dir_is_refused(env, capsys):
    (env.snap / "1_Pooling" / "extra.json").write_text("{}")
    assert emb.encode("a") is None
    assert env.st_calls == [] and _no_online(env)
    assert "1_Pooling/extra.json" in capsys.readouterr().err


def test_extra_file_behind_a_symlinked_directory_is_refused(env, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "adapter_config.json").write_text("{}")
    os.symlink(elsewhere, env.snap / "linked")
    assert emb.encode("a") is None
    assert env.st_calls == [] and _no_online(env)
    assert "linked/adapter_config.json" in capsys.readouterr().err


def test_readme_and_empty_directories_are_allowed(env):
    (env.snap / "README.md").write_text("# card")
    (env.snap / "empty_dir").mkdir()
    assert emb.encode("a") == [0.0, 1.0]
    assert len(env.st_calls) == 1


def test_readme_below_the_top_level_is_not_allowed(env, capsys):
    (env.snap / "1_Pooling" / "README.md").write_text("# card")
    assert emb.encode("a") is None
    assert "1_Pooling/README.md" in capsys.readouterr().err

def _run_with_timeout(fn, timeout=20):
    """Run fn() in a daemon thread; fail (not hang) if it does not return."""
    box = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the entry-set walk did not terminate"
    if "e" in box:
        raise box["e"]
    return box["r"]


def test_symlink_cycle_and_alias_terminate_with_the_right_verdict(env):
    snap = env.snap
    verify = lambda: _run_with_timeout(lambda: emb._verify_pinned_snapshot(snap))
    os.symlink(".", snap / "1_Pooling" / "loop")      # a directory linked to itself
    assert verify() is None
    os.symlink("1_Pooling", snap / "alias")           # alias to an already-walked dir
    assert verify() is None
    (snap / "1_Pooling" / "extra.json").write_text("{}")
    reason = verify()                                 # a cycle plus an extra file
    assert reason is not None and "1_Pooling/extra.json" in reason
    (snap / "1_Pooling" / "extra.json").unlink()
    os.symlink("1_Pooling", snap / "0alias")          # sorts BEFORE its target
    reason = verify()                                 # fails closed, order-dependent
    assert reason is not None and "0alias/config.json" in reason
