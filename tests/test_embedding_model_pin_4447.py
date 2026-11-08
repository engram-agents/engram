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
import threading
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
    loaded = []  # the directory each fake constructor call "loaded"
    state = {"local_miss": False, "snapshot_dir": None, "snapshot_raises": False,
             "online_dir": None}

    def fake_st(*args, **kwargs):
        # Mimics sentence-transformers' name resolution (#4468): a first
        # argument that exists on disk is loaded AS that directory (cwd wins
        # over the hub); otherwise the name resolves in the hub cache, which
        # SENTENCE_TRANSFORMERS_HOME redirects.
        st_calls.append((args, kwargs))
        first = args[0]
        if os.path.exists(first):
            loaded.append(Path(first))
        elif os.environ.get("SENTENCE_TRANSFORMERS_HOME"):
            loaded.append(Path(os.environ["SENTENCE_TRANSFORMERS_HOME"]))
        else:
            loaded.append(state["snapshot_dir"])
        return _FakeModel()

    def fake_snapshot_download(*args, **kwargs):
        snap_calls.append((args, kwargs))
        if state["snapshot_raises"]:
            raise OSError("no such snapshot")
        if state["local_miss"] and kwargs.get("local_files_only"):
            raise OSError("not in local cache")
        if not kwargs.get("local_files_only") and state["online_dir"] is not None:
            return str(state["online_dir"])
        return str(state["snapshot_dir"])

    st_mod = types.ModuleType("sentence_transformers")
    st_mod.SentenceTransformer = fake_st
    hf_mod = types.ModuleType("huggingface_hub")
    hf_mod.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "sentence_transformers", st_mod)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf_mod)
    return types.SimpleNamespace(st=st_calls, snap=snap_calls, state=state, loaded=loaded)


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


def test_b_both_resolutions_carry_the_pinned_revision(env, tmp_path, monkeypatch):
    # #4468: the local attempt and the online fallback are both snapshot
    # downloads of the PINNED revision; the constructor is then handed the
    # verified absolute path (no revision, no name).
    _tmp_snapshot(tmp_path, monkeypatch)
    env.state["snapshot_dir"] = tmp_path
    env.state["local_miss"] = True
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    rev = engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["revision"]
    assert [bool(c[1].get("local_files_only")) for c in env.snap] == [True, False]
    assert all(c[1].get("revision") == rev for c in env.snap)
    assert len(env.st) == 1
    assert env.st[0][0] == (str(tmp_path),)
    assert "revision" not in env.st[0][1]


def test_n_full_repo_name_is_pinned_and_verified(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path, monkeypatch, name=FULL)
    env.state["snapshot_dir"] = tmp_path
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(FULL)
    assert mgr._model is not None and mgr._model_name == FULL
    assert env.snap[0][1].get("revision") == engram_core.EMBEDDING_MODEL_PINS[FULL]["revision"]
    assert env.st[0][0] == (str(tmp_path),)
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
    assert env.st == []  # verified BEFORE construction: nothing was parsed, no unpinned fallback


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
    assert env.st[0][0] == (str(tmp_path),)
    kw = env.snap[0][1]
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
def test_m_model_is_not_visible_while_unverified(env, tmp_path, monkeypatch, path):
    mgr = engram_core.EmbeddingManager()
    seen = []

    def stub(name, snap):
        seen.append((mgr._model, mgr._model_name))
        return None

    env.state["snapshot_dir"] = tmp_path
    monkeypatch.setattr(engram_core, "_verify_pinned_snapshot", stub)
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        mgr.revive(DEFAULT)
    assert seen == [(None, None)]
    assert mgr._model is not None and mgr._model_name == DEFAULT


# ---- #4468 fold: verify the snapshot, then load FROM that verified path ----


def _good(env, tmp_path, monkeypatch):
    _tmp_snapshot(tmp_path / "hubsnap", monkeypatch)
    env.state["snapshot_dir"] = tmp_path / "hubsnap"
    return tmp_path / "hubsnap"


def _tampered_copy(root, name_dir):
    """A model-named tree whose files carry the pinned names but other bytes."""
    for rel in engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["sha256"]:
        p = root / name_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"TAMPERED " + rel.encode())


@pytest.mark.parametrize("path", ["load", "revive"])
def test_o_constructor_gets_the_absolute_verified_path_the_verifier_hashed(
    env, tmp_path, monkeypatch, path
):
    snap = _good(env, tmp_path, monkeypatch)
    hashed = []
    real = engram_core._verify_pinned_snapshot

    def recording(name, s):
        hashed.append(Path(s))
        return real(name, s)

    monkeypatch.setattr(engram_core, "_verify_pinned_snapshot", recording)
    mgr = engram_core.EmbeddingManager()
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        assert mgr.revive(DEFAULT) is True
    (args, kwargs), = env.st
    assert os.path.isabs(args[0]) and args[0] != DEFAULT
    assert "revision" not in kwargs
    assert hashed == [Path(args[0])] == [snap]
    assert env.loaded == [snap]


@pytest.mark.parametrize("path", ["load", "revive"])
def test_p_model_named_directory_in_cwd_is_not_loaded(env, tmp_path, monkeypatch, path):
    snap = _good(env, tmp_path, monkeypatch)
    cwd = tmp_path / "cwd"
    _tampered_copy(cwd, DEFAULT)
    monkeypatch.chdir(cwd)
    mgr = engram_core.EmbeddingManager()
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        mgr.revive(DEFAULT)
    assert env.loaded == [snap]


@pytest.mark.parametrize("path", ["load", "revive"])
def test_q_sentence_transformers_home_cannot_redirect_the_load(env, tmp_path, monkeypatch, path):
    snap = _good(env, tmp_path, monkeypatch)
    _tampered_copy(tmp_path, "sthome")
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(tmp_path / "sthome"))
    mgr = engram_core.EmbeddingManager()
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        mgr.revive(DEFAULT)
    assert env.loaded == [snap]


@pytest.mark.parametrize("path", ["load", "revive"])
def test_r_verification_happens_before_construction(env, tmp_path, monkeypatch, path):
    snap = _good(env, tmp_path, monkeypatch)
    _corrupt(snap, "vocab.txt")
    mgr = engram_core.EmbeddingManager()
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        mgr.revive(DEFAULT)
    assert env.st == []  # the constructor was never called
    assert mgr._model is None and DEFAULT in mgr._failed_models


def test_s_cache_miss_with_fallback_downloads_once_then_verifies_then_constructs(
    env, tmp_path, monkeypatch
):
    snap = _good(env, tmp_path, monkeypatch)
    env.state["local_miss"] = True
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert [bool(c[1].get("local_files_only")) for c in env.snap] == [True, False]
    assert env.st[0][0] == (str(snap),) and env.loaded == [snap]
    assert mgr._model is not None


def test_t_revive_never_calls_snapshot_download_online(env, tmp_path, monkeypatch):
    _good(env, tmp_path, monkeypatch)
    env.state["local_miss"] = True
    mgr = engram_core.EmbeddingManager()
    assert mgr.revive(DEFAULT) is False
    assert env.snap and all(c[1].get("local_files_only") is True for c in env.snap)
    assert env.st == []
    assert mgr._model is None and DEFAULT in mgr._failed_models


def test_u_memo_holds_the_verified_path_and_reload_does_not_rehash(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._pin_verified == {DEFAULT: str(snap)}
    mgr._model = None
    mgr._model_name = None
    mgr._load_model(DEFAULT)
    assert len(env.snap) == 1
    assert [c[0] for c in env.st] == [(str(snap),), (str(snap),)]


def test_v_refused_model_leaves_no_memo(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    _corrupt(snap)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._pin_verified == {}


def test_w_real_sentence_transformers_loads_the_hub_snapshot_not_the_cwd_copy(
    tmp_path, monkeypatch
):
    """Borges's measurement as a cell: with a tampered ./all-MiniLM-L6-v2 in
    the working directory, the REAL loader must load the hub snapshot's vocab.
    Skipped when sentence_transformers or the pinned cache is absent."""
    pytest.importorskip("sentence_transformers")
    # Ask the cache the LOADER will use (HF_HOME / HF_HUB_CACHE aware), local
    # only, and skip cleanly when the pinned snapshot is not there. A real
    # network download is never attempted by this cell.
    try:
        hub_snap = engram_core._resolve_pinned_snapshot(DEFAULT)
    except Exception as e:
        pytest.skip(f"pinned snapshot not in the cache the loader uses ({type(e).__name__})")
    if not hub_snap.is_dir():
        pytest.skip(f"no cached snapshot at {hub_snap}")
    monkeypatch.delenv("ENGRAM_NO_EMBEDDINGS", raising=False)
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    import shutil
    cwd = tmp_path / "cwd"
    shutil.copytree(hub_snap, cwd / DEFAULT, symlinks=False)
    (cwd / DEFAULT / "vocab.txt").write_text("[PAD]\n[UNK]\nTAMPERED\n")
    monkeypatch.chdir(cwd)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is not None
    loaded_from = Path(mgr._model.tokenizer.name_or_path).resolve()
    assert loaded_from == hub_snap.resolve(), loaded_from
    assert cwd.resolve() not in loaded_from.parents


# ---- fold: online download limited to the ten pinned files; partial heals --


def _partial(tmp_path, keep=("config.json",)):
    """A snapshot dir holding only some pinned files (an interrupted download),
    with the pin table pointing at the full set's hashes."""
    part = tmp_path / "partial"
    for rel in keep:
        p = part / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"fake contents of {rel}".encode())
    return part


def test_x_online_download_is_limited_to_the_ten_pinned_files(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    env.state["local_miss"] = True
    engram_core.EmbeddingManager()._load_model(DEFAULT)
    local, online = env.snap
    assert "allow_patterns" not in local[1]
    assert online[1].get("allow_patterns") == list(
        engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["sha256"]
    )


def test_y_partial_snapshot_heals_with_one_online_resolve(env, tmp_path, monkeypatch):
    full = _good(env, tmp_path, monkeypatch)
    env.state["snapshot_dir"] = _partial(tmp_path)
    env.state["online_dir"] = full
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert [bool(c[1].get("local_files_only")) for c in env.snap] == [True, False]
    assert env.snap[1][1].get("allow_patterns") == list(
        engram_core.EMBEDDING_MODEL_PINS[DEFAULT]["sha256"]
    )
    assert mgr._model is not None and env.loaded == [full]
    assert env.st[0][0] == (str(full),)


def test_z_partial_snapshot_still_missing_after_the_heal_is_a_refusal(
    env, tmp_path, monkeypatch, capsys
):
    _good(env, tmp_path, monkeypatch)
    part = _partial(tmp_path)
    env.state["snapshot_dir"] = part
    env.state["online_dir"] = part
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None and DEFAULT in mgr._failed_models
    assert env.st == []
    assert len([1 for c in env.snap if not c[1].get("local_files_only")]) == 1
    assert "Refusing embedding model" in capsys.readouterr().err


def test_za_checksum_mismatch_is_never_healed_online(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    _corrupt(snap)
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None and DEFAULT in mgr._failed_models
    assert env.st == []
    assert all(c[1].get("local_files_only") for c in env.snap)


def test_zb_revive_never_heals_a_partial_snapshot_online(env, tmp_path, monkeypatch):
    full = _good(env, tmp_path, monkeypatch)
    env.state["snapshot_dir"] = _partial(tmp_path)
    env.state["online_dir"] = full
    mgr = engram_core.EmbeddingManager()
    assert mgr.revive(DEFAULT) is False
    assert env.st == []
    assert all(c[1].get("local_files_only") for c in env.snap)


# ---- fold B2: the snapshot directory's ENTRY SET is checked, not just a list --


def _local_only(env):
    return all(c[1].get("local_files_only") for c in env.snap)


@pytest.mark.parametrize("path", ["load", "revive"])
def test_zc_extra_top_level_file_is_refused_without_online_or_construct(
    env, tmp_path, monkeypatch, capsys, path
):
    snap = _good(env, tmp_path, monkeypatch)
    (snap / "adapter_config.json").write_text("{}")
    mgr = engram_core.EmbeddingManager()
    if path == "load":
        mgr._load_model(DEFAULT)
    else:
        assert mgr.revive(DEFAULT) is False
    assert mgr._model is None and DEFAULT in mgr._failed_models
    assert env.st == [] and _local_only(env)
    assert "adapter_config.json" in capsys.readouterr().err
    assert mgr._pin_verified == {}


def test_zd_extra_file_in_a_nested_dir_is_refused(env, tmp_path, monkeypatch, capsys):
    snap = _good(env, tmp_path, monkeypatch)
    (snap / "1_Pooling" / "extra.json").write_text("{}")
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None and env.st == [] and _local_only(env)
    assert "1_Pooling/extra.json" in capsys.readouterr().err


def test_ze_extra_file_behind_a_symlinked_directory_is_refused(
    env, tmp_path, monkeypatch, capsys
):
    snap = _good(env, tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "adapter_config.json").write_text("{}")
    os.symlink(elsewhere, snap / "linked")
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None and env.st == [] and _local_only(env)
    assert "linked/adapter_config.json" in capsys.readouterr().err


def test_zf_readme_and_empty_directories_are_allowed(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    (snap / "README.md").write_text("# card")
    (snap / "empty_dir").mkdir()
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is not None and env.st[0][0] == (str(snap),)


def test_zg_readme_below_the_top_level_is_not_allowed(env, tmp_path, monkeypatch, capsys):
    snap = _good(env, tmp_path, monkeypatch)
    (snap / "1_Pooling" / "README.md").write_text("# card")
    mgr = engram_core.EmbeddingManager()
    mgr._load_model(DEFAULT)
    assert mgr._model is None
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


def test_zh_symlink_cycle_and_alias_terminate_with_the_right_verdict(env, tmp_path, monkeypatch):
    snap = _good(env, tmp_path, monkeypatch)
    verify = lambda: _run_with_timeout(
        lambda: engram_core._verify_pinned_snapshot(DEFAULT, snap)
    )
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
