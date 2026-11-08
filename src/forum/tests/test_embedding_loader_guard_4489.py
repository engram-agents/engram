"""Meta-cells for the #4489 embedding-loader guard in ``conftest.py``.

The guard (``_embedding_loader_tripwire``) makes ``_resolve_pinned_snapshot`` and
``_SentenceTransformer`` tripwires for every forum test. ``_get_model`` swallows
exceptions from both (outer ``try/except Exception``), so a tripwire that only
raised would be invisible: the guard RECORDS each real reach and fails the test at
TEARDOWN. These cells pin that contract:

- a real reach is recorded, and the real model is not built (a, a2, a3);
- a test's own stub replaces the tripwire and the recorder stays empty (b);
- a non-empty recorder fails the test at teardown, observed in a subprocess so the
  real fixture teardown runs (c); a clean test passes there (c2).
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from forum import embeddings as emb


@pytest.fixture
def real_loader_state(monkeypatch):
    """Put the module in the state where ``_get_model`` would really load."""
    monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
    monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_refused", False)
    monkeypatch.setattr(emb, "_verified_snapshot", None)


def test_real_get_model_reach_is_recorded(real_loader_state, _embedding_loader_tripwire, capsys):
    reaches = _embedding_loader_tripwire
    assert emb._get_model() is None  # the tripwire's raise is swallowed, as feared
    # ...but it DID raise (the real resolver never ran): _get_model printed its text.
    assert "#4489 guard: real _resolve_pinned_snapshot reached" in capsys.readouterr().err
    assert reaches, "a real _get_model reach must be recorded"
    assert any("_resolve_pinned_snapshot" in r for r in reaches)
    assert emb._model is None and emb._verified_snapshot is None  # no real model
    reaches.clear()  # this cell expected the reach; teardown must pass


def test_real_constructor_reach_is_recorded(real_loader_state, _embedding_loader_tripwire):
    reaches = _embedding_loader_tripwire
    with pytest.raises(RuntimeError, match="#4489 guard: real _SentenceTransformer"):
        emb._SentenceTransformer("/nonexistent", device="cpu")
    assert any("_SentenceTransformer" in r for r in reaches)
    reaches.clear()


def test_constructor_reached_through_get_model_with_resolver_stubbed(
        real_loader_state, _embedding_loader_tripwire, monkeypatch, tmp_path, capsys):
    """A stub for the resolver and verifier must NOT cover the constructor."""
    reaches = _embedding_loader_tripwire
    monkeypatch.setattr(emb, "_resolve_pinned_snapshot", lambda online=False: tmp_path)
    monkeypatch.setattr(emb, "_verify_pinned_snapshot", lambda snap: None)
    assert emb._get_model() is None
    assert len(reaches) == 1 and "_SentenceTransformer" in reaches[0]
    assert "#4489 guard: real _SentenceTransformer reached" in capsys.readouterr().err
    assert emb._model is None
    reaches.clear()


def test_tests_own_stub_replaces_the_tripwire(
        real_loader_state, _embedding_loader_tripwire, monkeypatch, tmp_path):
    reaches = _embedding_loader_tripwire
    class _Fake:
        def __init__(self, path, device=None):
            self.path = path

    monkeypatch.setattr(emb, "_resolve_pinned_snapshot", lambda online=False: tmp_path)
    monkeypatch.setattr(emb, "_verify_pinned_snapshot", lambda snap: None)
    monkeypatch.setattr(emb, "_SentenceTransformer", _Fake)
    monkeypatch.setattr(emb, "_cap_cpu_threads", lambda model, threads: None)
    model = emb._get_model()
    assert isinstance(model, _Fake) and model.path == str(tmp_path)
    assert reaches == []


def test_real_resolver_over_a_tests_own_hub_stub_is_not_a_reach(
        real_loader_state, _embedding_loader_tripwire, monkeypatch, tmp_path):
    """The #4468 pin cells exercise the REAL resolver against a stubbed
    huggingface_hub (hermetic by construction). Only the genuine hub is a leak."""
    import types

    reaches = _embedding_loader_tripwire
    hub = types.ModuleType("huggingface_hub")
    seen = []

    def snapshot_download(repo_id, **kwargs):
        seen.append(repo_id)
        return str(tmp_path)

    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    assert emb._resolve_pinned_snapshot() == tmp_path
    assert seen and reaches == []


# --- teardown behaviour, observed through a real pytest run -----------------

_SRC = str(Path(__file__).resolve().parents[2])

_CONFTEST = """\
from forum.tests.conftest import _embedding_loader_tripwire  # noqa: F401
"""

_TEST_BODY = textwrap.dedent('''\
    from forum import embeddings as emb

    def test_inner(monkeypatch, _embedding_loader_tripwire):
        monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
        monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
        monkeypatch.setattr(emb, "_model", None)
        monkeypatch.setattr(emb, "_model_refused", False)
        monkeypatch.setattr(emb, "_verified_snapshot", None)
        %s
''')


def _run_inner(tmp_path, body):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_inner.py").write_text(_TEST_BODY % body)
    env = {k: v for k, v in os.environ.items() if k != "FORUM_NO_EMBEDDINGS"}
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)


def test_teardown_fails_a_test_that_reached_the_real_loader(tmp_path):
    res = _run_inner(tmp_path, "assert emb._get_model() is None")
    out = res.stdout + res.stderr
    assert res.returncode != 0, out
    assert "1 passed" in out and "1 error" in out, out  # body passed; TEARDOWN erred
    assert "_resolve_pinned_snapshot" in out, out


def test_teardown_passes_a_test_with_no_reach(tmp_path):
    res = _run_inner(tmp_path, "assert emb._model is None")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "1 passed" in out, out


# --- the central default: FORUM_NO_EMBEDDINGS=1 unless the module opts in ----

def _run_module(tmp_path, source):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(source))
    env = {k: v for k, v in os.environ.items() if k != "FORUM_NO_EMBEDDINGS"}
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    return res, res.stdout + res.stderr


def test_default_sets_no_embeddings_for_a_plain_test(tmp_path):
    res, out = _run_module(tmp_path, """
        import os
        from forum import embeddings  # noqa: F401

        def test_inner(_embedding_loader_tripwire):
            assert os.environ.get("FORUM_NO_EMBEDDINGS") == "1"
    """)
    assert res.returncode == 0 and "1 passed" in out, out


def test_module_constant_opts_out_of_the_default(tmp_path):
    res, out = _run_module(tmp_path, """
        import os
        from forum import embeddings  # noqa: F401

        FORUM_TEST_EMBEDDINGS_ON = True

        def test_inner(_embedding_loader_tripwire):
            assert "FORUM_NO_EMBEDDINGS" not in os.environ
    """)
    assert res.returncode == 0 and "1 passed" in out, out


def test_a_tests_own_delenv_still_wins(tmp_path):
    res, out = _run_module(tmp_path, """
        import os
        from forum import embeddings  # noqa: F401

        def test_inner(monkeypatch, _embedding_loader_tripwire):
            assert os.environ.get("FORUM_NO_EMBEDDINGS") == "1"
            monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
            assert "FORUM_NO_EMBEDDINGS" not in os.environ
    """)
    assert res.returncode == 0 and "1 passed" in out, out


def test_opted_in_module_keeps_the_tripwires(tmp_path):
    res, out = _run_module(tmp_path, """
        from forum import embeddings as emb

        FORUM_TEST_EMBEDDINGS_ON = True

        def test_inner(monkeypatch, _embedding_loader_tripwire):
            monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
            monkeypatch.setattr(emb, "_model", None)
            monkeypatch.setattr(emb, "_model_refused", False)
            monkeypatch.setattr(emb, "_verified_snapshot", None)
            assert emb._get_model() is None
    """)
    assert res.returncode != 0 and "1 error" in out, out


# --- stale module objects: the fixture runs AFTER import-time re-imports -----
# test_embeddings_offline_load deletes sys.modules["forum.embeddings"] and
# re-imports, so by fixture-setup time several DIFFERENT module objects can be
# live. Each source below builds one such state at import time, then asserts the
# object that is NOT the one in sys.modules still carries the tripwires.

_STALE_CHECK = '''
import pytest

def _tripped(mod, reaches):
    for call in (lambda: mod._resolve_pinned_snapshot(),
                 lambda: mod._SentenceTransformer("x")):
        with pytest.raises(RuntimeError, match="#4489 guard"):
            call()
    assert len(reaches) == 2
    reaches.clear()
'''

_STALE_SOURCES = {
    # held ONLY by the test module (server imported later, gets the new object)
    "test_module_attribute": '''
import sys
from forum import embeddings as emb_old
del sys.modules["forum.embeddings"]
import forum.embeddings as emb_new
import forum.server  # noqa: F401
assert emb_old is not emb_new
''' + _STALE_CHECK + '''
def test_inner(_embedding_loader_tripwire):
    _tripped(emb_old, _embedding_loader_tripwire)
''',
    # held ONLY by server.emb_mod (the test module never binds the old object)
    "server_alias": '''
import sys
import forum.server as srv
del sys.modules["forum.embeddings"]
import forum.embeddings as emb_new
assert srv.emb_mod is not emb_new
''' + _STALE_CHECK + '''
def test_inner(_embedding_loader_tripwire):
    _tripped(srv.emb_mod, _embedding_loader_tripwire)
''',
    # held ONLY by the forum package attribute (sys.modules entry is gone)
    "package_attribute": '''
import sys
import forum.embeddings  # noqa: F401  (binds only `forum`)
del sys.modules["forum.embeddings"]
assert "forum.embeddings" not in sys.modules
''' + _STALE_CHECK + '''
def test_inner(_embedding_loader_tripwire):
    _tripped(forum.embeddings, _embedding_loader_tripwire)
''',
}


@pytest.mark.parametrize("held_by", sorted(_STALE_SOURCES))
def test_stale_module_objects_carry_the_tripwires(tmp_path, held_by):
    res, out = _run_module(tmp_path, _STALE_SOURCES[held_by])
    assert res.returncode == 0 and "1 passed" in out, out


# --- first import inside the test body (Borges B1) ---------------------------
# On a single-file run nothing may have imported forum.embeddings before the
# fixture runs; a test that imports it FIRST in its own body must still be
# guarded. The subprocess is offline with an empty HF home, so a guard failure
# cannot download anything.

_FIRST_IMPORT_SOURCES = {
    "import_inside_the_test": '''
        import os

        def test_inner(monkeypatch, _embedding_loader_tripwire):
            monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
            import forum.embeddings as emb
            monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
            emb._get_model()
    ''',
    "import_at_module_level": '''
        import forum.embeddings as emb

        def test_inner(monkeypatch, _embedding_loader_tripwire):
            monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)
            monkeypatch.setattr(emb, "_ST_AVAILABLE", True)
            emb._get_model()
    ''',
}


def _run_offline(tmp_path, source):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(source))
    hf = tmp_path / "hf-empty"
    hf.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "FORUM_NO_EMBEDDINGS"}
    env.update(PYTHONPATH=_SRC + os.pathsep + env.get("PYTHONPATH", ""),
               PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
               HF_HOME=str(hf), HF_HUB_CACHE=str(hf / "hub"))
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         str(tmp_path / "test_inner.py")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    return res, res.stdout + res.stderr


@pytest.mark.parametrize("case", sorted(_FIRST_IMPORT_SOURCES))
def test_first_import_inside_the_test_body_is_still_guarded(tmp_path, case):
    res, out = _run_offline(tmp_path, _FIRST_IMPORT_SOURCES[case])
    assert res.returncode != 0, out
    assert "1 error" in out, out
    assert "#4489 guard: real _resolve_pinned_snapshot reached" in out, out
