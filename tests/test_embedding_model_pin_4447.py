"""#4447 slice B: the default embedding model is pinned to a revision and its
files are checksummed after load.

No network and no real model: SentenceTransformer and huggingface_hub's
snapshot_download are stubbed. Cells (c)-(f) build a temp "snapshot" whose
file hashes are written into a monkeypatched pin table, so no model bytes are
needed. Cell (h) is the one that checks the shipped constants against a real
HF cache snapshot, and skips when that cache is absent.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import types
from pathlib import Path

import pytest

import engram_core

DEFAULT = "all-MiniLM-L6-v2"
FULL = "sentence-transformers/all-MiniLM-L6-v2"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

class _FakeModel:
    pass


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ENGRAM_NO_EMBEDDINGS", raising=False)
    st_calls = []
    snap_calls = []
    state = {"st_fail_first": False, "snapshot_dir": None, "snapshot_raises": False}

    def fake_st(*args, **kwargs):
        st_calls.append((args, kwargs))
        if state["st_fail_first"] and len(st_calls) == 1:
            raise OSError("cache miss")
        return _FakeModel()

    def fake_snapshot_download(*args, **kwargs):
        snap_calls.append((args, kwargs))
        if state["snapshot_raises"]:
            raise OSError("no such snapshot")
        return str(state["snapshot_dir"])

    st_mod = types.ModuleType("sentence_transformers")
    st_mod.SentenceTransformer = fake_st
    hf_mod = types.ModuleType("huggingface_hub")
    hf_mod.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "sentence_transformers", st_mod)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf_mod)
    return types.SimpleNamespace(st=st_calls, snap=snap_calls, state=state)


def _tmp_snapshot(tmp_path, monkeypatch, name=DEFAULT):
    """Write a fake snapshot and point the pin table at its hashes."""
    real = engram_core.EMBEDDING_MODEL_PINS[name]
    sha = {}
    for rel in real["sha256"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = f"fake contents of {rel}".encode()
        p.write_bytes(data)
        sha[rel] = hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(
        engram_core.EMBEDDING_MODEL_PINS, name,
        {"repo_id": real["repo_id"], "revision": real["revision"], "sha256": sha},
    )
    return sha


def test_a_pin_table_shape():
    pins = engram_core.EMBEDDING_MODEL_PINS
    assert DEFAULT in pins and FULL in pins
    assert pins[DEFAULT] == pins[FULL]
    entry = pins[DEFAULT]
    assert entry["repo_id"] == FULL
    assert re.fullmatch(r"[0-9a-f]{40}", entry["revision"])
    assert set(entry["sha256"]) == {
        "model.safetensors", "config.json", "tokenizer.json", "vocab.txt",
        "modules.json", "sentence_bert_config.json", "1_Pooling/config.json",
        "config_sentence_transformers.json", "special_tokens_map.json",
        "tokenizer_config.json",
    }
    assert all(HEX64.match(v) for v in entry["sha256"].values())


def test_b_both_calls_carry_the_pinned_revision(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    env.state["st_fail_first"] = True
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    rev = engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["revision"]
    assert len(env.st) == 2
    assert env.st[0][1].get("local_files_only") is True
    assert env.st[0][1].get("revision") == rev
    assert not env.st[1][1].get("local_files_only")
    assert env.st[1][1].get("revision") == rev


def test_n_full_repo_name_is_pinned_and_verified(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch, name=FULL)
    env.state["snapshot_dir"] = tmp_path
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(FULL)
    assert mgr._model is not None and mgr._model_name == FULL
    assert env.st[0][1].get("revision") == engram_core.EMBEDDING_MODEL_PINS[FULL]["revision"]
    assert len(env.snap) == 1


def test_c_good_snapshot_loads_and_verifies_once(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    mgr = engram_core.EmbeddingManager()
    for _ in range(3):
        mgr._model = None
        mgr._model_name = None
        mgr._load_model(DEFAULT)
        assert mgr._model is not None
    assert len(env.snap) == 1
    kw = env.snap[0][1]
    assert kw.get("local_files_only") is True
    assert kw.get("revision") == engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["revision"]


def test_d_flipped_byte_refuses_with_both_hashes(env, tmp_path, monkeypatch, capsys):
    sha = _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    target = "1_Pooling/config.json"
    p = tmp_path / target
    b = bytearray(p.read_bytes())
    b[0] ^= 0x01
    p.write_bytes(bytes(b))
    actual = hashlib.sha256(bytes(b)).hexdigest()
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    err = capsys.readouterr().err
    assert mgr._model is None
    assert DEFAULT in mgr._failed_models
    assert target in err and sha[target] in err and actual in err
    assert len(env.st) >= 1 and all("revision" in c[1] for c in env.st)  # no unpinned fallback


def test_e_missing_file_refuses(env, tmp_path, monkeypatch, capsys):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    (tmp_path / "vocab.txt").unlink()
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    err = capsys.readouterr().err
    assert mgr._model is None
    assert DEFAULT in mgr._failed_models
    assert "vocab.txt" in err


def test_f_unresolvable_snapshot_refuses_with_reason(env, monkeypatch, capsys):
    env.state["snapshot_raises"] = True
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    err = capsys.readouterr().err
    assert mgr._model is None
    assert DEFAULT in mgr._failed_models
    assert "snapshot" in err.lower() and "no such snapshot" in err


def test_g_custom_model_is_unpinned_and_unverified(env, capsys):
    mgr = engram_core.EmbeddingManager()
    mgr._load_model("someone/custom-model")
    assert mgr._model is not None
    assert len(env.st) == 1 and "revision" not in env.st[0][1]
    assert env.snap == []
    assert capsys.readouterr().err == ""


def test_h_shipped_constants_match_the_real_cache_snapshot():
    entry = engram_core.EMBEDDING_MODEL_PINS[DEFAULT]
    snap = (
        Path(os.path.expanduser("~/.cache/huggingface/hub"))
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots" / entry["revision"]
    )
    if not snap.is_dir():
        pytest.skip(f"no cached snapshot at {snap}")
    for rel, want in entry["sha256"].items():
        h = hashlib.sha256()
        with open(snap / rel, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        assert h.hexdigest() == want, rel


# ---- fold round 1: the local-only revive() path is the same gate ----------


def _corrupt(tmp_path, rel="1_Pooling/config.json"):
    p = tmp_path / rel
    b = bytearray(p.read_bytes())
    b[0] ^= 0x01
    p.write_bytes(bytes(b))


def test_i_refusal_then_revive_keeps_refusing(env, tmp_path, monkeypatch, capsys):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    _corrupt(tmp_path)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None and DEFAULT in mgr._failed_models
    assert mgr.revive(DEFAULT) is False
    assert mgr._model is None and mgr._model_name is None
    assert DEFAULT in mgr._failed_models
    assert "1_Pooling/config.json" in capsys.readouterr().err


def test_j_revive_of_pinned_model_is_pinned_and_verified(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    mgr = engram_core.EmbeddingManager()
    assert mgr.revive(DEFAULT) is True
    assert len(env.st) == 1
    kw = env.st[0][1]
    assert kw.get("local_files_only") is True
    assert kw.get("revision") == engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["revision"]
    assert len(env.snap) == 1


def test_k_revive_of_custom_model_is_unpinned_and_unverified(env):
    mgr = engram_core.EmbeddingManager()
    assert mgr.revive("someone/custom-model") is True
    assert "revision" not in env.st[0][1]
    assert env.snap == []


def test_l_refused_default_does_not_block_a_custom_model(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    _corrupt(tmp_path)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None
    mgr._load_model("someone/custom-model")
    assert mgr._model is not None and mgr._model_name == "someone/custom-model"


@pytest.mark.parametrize("path", ["load", "revive"])
def test_m_model_is_not_visible_while_unverified(env, monkeypatch, path):
    mgr = engram_core.EmbeddingManager()
    seen = []

    def stub(name):
        seen.append((mgr._model, mgr._model_name))
        return None

    monkeypatch.setattr(engram_core, "_verify_pinned_snapshot", stub)
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        mgr.revive(DEFAULT)
    assert seen == [(None, None)]
    assert mgr._model is not None and mgr._model_name == DEFAULT
