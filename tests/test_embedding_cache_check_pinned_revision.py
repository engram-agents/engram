"""The server's embedding-cache check must answer what the pinned loader will do.

Origin: release gate G6 on v0.5.7-rc1 (empty home, plain clone) failed at
installer step 5. The pre-warm downloads the default model by its pinned commit
(#4467); `huggingface_hub.snapshot_download(revision=<sha>)` writes
`blobs/` and `snapshots/<sha>/` but never `refs/main`. The check
`server._embedding_cache_status` called `try_to_load_from_cache(repo, file)`
with no revision, which resolves through `refs/main`, so it said "not cached"
right after a successful pinned download, and the startup gate printed FATAL.
Existing seats passed only because an older unpinned download had left
`refs/main` behind.

Contract (stated before the fix; each cell fails on unmodified dev):
  (a) empty cache -> the pinned download's layout (no refs/main) -> check True.
  (b) cache holds the model ONLY at a different revision than the pin -> check
      False, even though refs/main points at it: the loader resolves the pin,
      so a check that passes on another revision is the same defect facing the
      other way.
  (c) refs/main AT the pin (every existing seat) -> still True.
  (d) the local-path clauses are unchanged (existing tests in
      test_prewarm_embeddings.py; one smoke cell here).
  (e) the forum's loader resolves the pinned snapshot from the same layout
      (it has no separate cache check; shown, not assumed).
  (f) the FATAL text for a pinned model names a command that fetches the PIN,
      not a by-name download (which would fetch whatever `main` is today and
      still fail the pinned check if `main` has moved).
  Plus: a custom (unpinned) model keeps the by-name lookup and the old advice.

Runs against the REAL huggingface_hub on a fake cache directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "all-MiniLM-L6-v2"
OTHER_SHA = "0" * 40


def _import_server():
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    import engram_core  # noqa: F401
    return server


def _repo_dir(cache: Path, repo_id: str) -> Path:
    return cache / ("models--" + repo_id.replace("/", "--"))


def _write_snapshot(cache: Path, repo_id: str, sha: str, refs_main: bool) -> None:
    """The on-disk layout huggingface_hub writes. `refs_main=False` is what a
    download by commit id leaves behind (measured by the G6 control)."""
    repo = _repo_dir(cache, repo_id)
    snap = repo / "snapshots" / sha
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    (snap / "modules.json").write_text("[]")
    if refs_main:
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text(sha)


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = _import_server()
    import huggingface_hub.constants as hub_constants
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(hub_constants, "HF_HUB_CACHE", str(cache))
    srv._test_cache = cache
    return srv


def _pin():
    import engram_core
    return engram_core.EMBEDDING_MODEL_PINS[MODEL]


def test_a_pinned_download_layout_without_refs_main_counts_as_cached(server):
    pin = _pin()
    _write_snapshot(server._test_cache, pin["repo_id"], pin["revision"], refs_main=False)
    assert server._embedding_cache_status(MODEL) is True
    assert server._embedding_cache_status("sentence-transformers/" + MODEL) is True


def test_a_empty_cache_is_still_a_miss(server):
    assert server._embedding_cache_status(MODEL) is False


def test_b_only_a_different_revision_cached_is_a_miss_even_with_refs_main(server):
    pin = _pin()
    assert OTHER_SHA != pin["revision"]
    _write_snapshot(server._test_cache, pin["repo_id"], OTHER_SHA, refs_main=True)
    assert server._embedding_cache_status(MODEL) is False


def test_c_refs_main_at_the_pin_still_counts_as_cached(server):
    pin = _pin()
    _write_snapshot(server._test_cache, pin["repo_id"], pin["revision"], refs_main=True)
    assert server._embedding_cache_status(MODEL) is True


def test_c_the_check_makes_no_network_or_download_call(server):
    pin = _pin()
    _write_snapshot(server._test_cache, pin["repo_id"], pin["revision"], refs_main=True)
    import huggingface_hub
    real = huggingface_hub.snapshot_download

    def _offline_only(*args, **kwargs):
        assert kwargs.get("local_files_only") is True, "the check must never touch the network"
        return real(*args, **kwargs)

    with mock.patch("huggingface_hub.snapshot_download", side_effect=_offline_only):
        assert server._embedding_cache_status(MODEL) is True


def test_d_local_path_clause_unchanged(server, tmp_path):
    d = tmp_path / "local-model"
    d.mkdir()
    assert server._embedding_cache_status(str(d)) is False
    (d / "modules.json").write_text("[]")
    assert server._embedding_cache_status(str(d)) is True


def test_unpinned_custom_model_keeps_the_by_name_lookup(server):
    """A name outside EMBEDDING_MODEL_PINS is the user's own choice: looked up
    through refs/main exactly as before."""
    _write_snapshot(server._test_cache, "BAAI/bge-small-en", OTHER_SHA, refs_main=True)
    assert server._embedding_cache_status("BAAI/bge-small-en") is True
    assert server._embedding_cache_status("BAAI/other-model") is False


def test_e_forum_resolves_the_pinned_snapshot_from_a_layout_with_no_refs_main(tmp_path, monkeypatch):
    """The forum has no cache-status check; its loader resolves the pin with
    snapshot_download(local_files_only=True, revision=<sha>). Show that this
    works on the no-refs/main layout rather than assume it. Lives here, not in
    src/forum/tests: that suite's #4489 guard forbids reaching the real
    resolver against the real hub. File-loaded by path (no `src/` on sys.path:
    it would shadow packages for the rest of the run, #2037/#2063)."""
    import importlib.util
    import huggingface_hub.constants as hub_constants
    path = Path(__file__).resolve().parents[1] / "src" / "forum" / "embeddings.py"
    spec = importlib.util.spec_from_file_location("_forum_embeddings_4467", path)
    femb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(femb)

    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(hub_constants, "HF_HUB_CACHE", str(cache))
    pin = femb._MINILM_PIN
    _write_snapshot(cache, pin["repo_id"], pin["revision"], refs_main=False)
    snap = femb._resolve_pinned_snapshot(online=False)
    expect = _repo_dir(cache, pin["repo_id"]) / "snapshots" / pin["revision"]
    assert Path(snap).resolve() == expect.resolve()


def test_f_fatal_message_for_a_pinned_model_names_the_pinned_fetch(server, capsys):
    with mock.patch.object(server._embedder, "is_available", return_value=True), \
         mock.patch.object(server, "_embedding_cache_status", return_value=False), \
         mock.patch.object(server, "_get_embedding_config", return_value={"model": MODEL}):
        with pytest.raises(SystemExit):
            server._embedding_startup_gate()
    err = capsys.readouterr().err
    assert "_load_model" in err, err  # the loader's pinned path, not a by-name download
    assert "SentenceTransformer(" not in err, err


def test_f_fatal_message_for_an_unpinned_model_keeps_the_by_name_advice(server, capsys):
    with mock.patch.object(server._embedder, "is_available", return_value=True), \
         mock.patch.object(server, "_embedding_cache_status", return_value=False), \
         mock.patch.object(server, "_get_embedding_config", return_value={"model": "BAAI/bge-small-en"}):
        with pytest.raises(SystemExit):
            server._embedding_startup_gate()
    err = capsys.readouterr().err
    assert "SentenceTransformer('BAAI/bge-small-en')" in err, err


def test_f_pinned_remedy_command_survives_a_plugin_path_with_spaces_and_quotes(server, tmp_path):
    """The printed command is shell-parsed: a directory name with a space or a
    quote must round-trip through shlex to the same argv."""
    import shlex
    odd = tmp_path / "My Mac's plugin"
    odd.mkdir()
    with mock.patch.object(server, "__file__", str(odd / "server.py")), \
         mock.patch.object(sys, "executable", str(odd / "py thon")):
        cmd = server._embedding_prewarm_command(MODEL)
    argv = shlex.split(cmd)
    assert argv[0] == str(odd / "py thon") and argv[1] == "-c" and len(argv) == 3
    assert repr(str(odd)) in argv[2] and "_load_model('all-MiniLM-L6-v2')" in argv[2]
    compile(argv[2], "<remedy>", "exec")  # the payload is valid Python
