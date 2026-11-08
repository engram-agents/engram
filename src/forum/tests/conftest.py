"""Forum test defaults.

#3638: the server's PR-baton init asks GitHub whether the PR exists
(`baton_gate._pr_identity`). The forum suite creates PR batons with made-up
numbers and must never reach the network, so by default the reader answers
"exists, author unknown" (existence passes; the author check has nothing to
compare). `test_baton_init_pr_exists_3638.py` overrides it per case.

The stub is set on the module object `forum.server._load_baton_gate()` returns,
which is the one the server actually calls.
"""
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _stub_pr_identity(monkeypatch):
    from forum import server

    gate = server._load_baton_gate()
    if gate is not None:
        monkeypatch.setattr(gate, "_pr_identity",
                            lambda pr_number, repo: ("exists", None), raising=True)
    yield


# ---------------------------------------------------------------------------
# #1459: forum auth plumbing (TEST-ONLY; the logic lives in auth_plumbing.py).
#
# The server's one before_request guard refuses writes (and reads naming an agent)
# without a bearer token for that agent. The ~90 existing forum test files predate it
# and call the Flask test client as a named agent. Rather than weaken the guard, every
# test client this directory creates attaches a token for the identity each request
# names. Tests of the guard itself mark themselves ``raw_auth`` and see the real,
# unaided server. No production switch bypasses the guard.
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "raw_auth: do not add the #1459 test-client auth plumbing; exercise the real guard")


@pytest.fixture(autouse=True)
def _forum_test_client_auth(request, monkeypatch):
    if request.node.get_closest_marker("raw_auth"):
        yield
        return
    from flask import Flask

    from forum.tests.auth_plumbing import wrap_client

    real_test_client = Flask.test_client

    def test_client_with_auth(self, *args, **kwargs):
        return wrap_client(real_test_client(self, *args, **kwargs))

    monkeypatch.setattr(Flask, "test_client", test_client_with_auth)
    yield


# ---------------------------------------------------------------------------
# #4489: no forum test may reach the REAL embedding-model loader.
#
# `_get_model` resolves a pinned snapshot (`_resolve_pinned_snapshot`, which can
# start a ~90 MB download) and then builds the model (`_SentenceTransformer`).
# The forum suite sets no FORUM_NO_EMBEDDINGS by default, so a test that reaches
# either one downloads or silently loads the real model. `_get_model` wraps both
# calls in `try/except Exception` and returns None, so a tripwire that only
# RAISED would be swallowed and the test would pass. These tripwires therefore
# RECORD each real call and the fixture fails the test at TEARDOWN.
#
# Order matters: the tripwires go in FIRST (autouse), a test's own
# `monkeypatch.setattr` then replaces them, and only a REAL call lands here.
# Scope: every test in this directory. The fixture is two attribute swaps (no
# import, no model work) and `forum.server` (imported by the autouse fixture
# above for every test) imports `forum.embeddings`, so "touches embeddings" is
# every forum test; a narrower marker would miss a test that reaches the loader
# through create_app()/warm_model() or db.py. It patches EVERY live module object
# for `forum.embeddings` (sys.modules, the package attribute, server's alias, and
# any the test module holds) because test_embeddings_offline_load deletes the
# sys.modules entry and re-imports, leaving test files holding an older object.
#
# Known bypasses, accepted (the guard is a tripwire for ordinary tests, not a
# sandbox): (a) `importlib.reload(emb)` inside a test restores the real
# `_SentenceTransformer`, and a module imported fresh after a `del sys.modules[..]`
# INSIDE the test body is not patched (today those tests only import). (b) The
# resolver pass-through is wider than "the test's own stub hub": a stub
# `huggingface_hub` that DELEGATES to the real hub passes through unrecorded
# (measured, with or without the first-statement import below); a test must not
# do that. The genuine hub imported for the first time during the test body is
# recorded, measured where sentence_transformers is installed (importing
# forum.embeddings first in the fixture pulls the hub in before hub_at_setup is
# read); on a host without sentence_transformers that import is not measured.
# (c) A thread that outlives its test records into nothing, or
# into the next test's list (server.py and db.py start no threads today).
# (d) FORUM_TEST_EMBEDDINGS_ON only stops the default; it does not stub the
# loader, and the tripwires stay, so an opted-in module must stub it itself.
# (S1) The recording guard is in-process only. A child process (e.g.
# `python -m forum.server`) is protected solely by the inherited
# FORUM_NO_EMBEDDINGS=1, never by the tripwires. Today every spawn site either
# sets it explicitly (test_verify_probe_env_3597._server) or builds the child's
# env from os.environ; a child given a hand-built env without the variable would
# load the real model unrecorded.
# ---------------------------------------------------------------------------
def _embedding_module_objects(request):
    mods = []

    def _add(m):
        if m is not None and getattr(m, "__name__", None) == "forum.embeddings" \
                and not any(m is x for x in mods):
            mods.append(m)

    _add(sys.modules.get("forum.embeddings"))
    pkg = sys.modules.get("forum")
    _add(getattr(pkg, "embeddings", None))
    server = sys.modules.get("forum.server")
    _add(getattr(server, "emb_mod", None))
    test_mod = getattr(request, "module", None)
    for value in list(vars(test_mod).values()) if test_mod is not None else ():
        _add(value)
    return mods


def assert_no_loader_reaches(reaches):
    """The teardown check (a function so the guard's own cells can name it)."""
    assert not reaches, (
        "this test reached the REAL embedding-model loader (#4489): "
        f"{reaches}. Stub emb._get_model, or emb._resolve_pinned_snapshot, "
        "emb._verify_pinned_snapshot AND emb._SentenceTransformer, in the test; "
        "or set FORUM_NO_EMBEDDINGS=1."
    )


@pytest.fixture(autouse=True)
def _embedding_loader_tripwire(request, monkeypatch):
    # FIRST statement, before hub_at_setup is read: on a single-file run nothing
    # may have imported forum.embeddings yet, and importing it later (inside
    # _embedding_module_objects) would pull the genuine huggingface_hub in AFTER
    # hub_at_setup, so the resolver tripwire would pass the real hub through.
    importlib.import_module("forum.embeddings")
    reaches: list[str] = []
    # Central default: the forum suite is not about the embedding layer, so
    # create_app()/warm_model()/probe_deps() must not load it. A module that
    # genuinely needs the ambient variable unset sets the module-level constant
    # FORUM_TEST_EMBEDDINGS_ON = True. A test's own setenv/delenv runs after this
    # fixture and wins. The tripwires below stay installed either way.
    if not getattr(request.module, "FORUM_TEST_EMBEDDINGS_ON", False):
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    # The resolver reaches the network/cache only through `huggingface_hub`
    # (imported inside the function from sys.modules). The #4468 pin cells run
    # the REAL resolver against a test-installed stub hub, which is hermetic. So
    # the resolver tripwire passes through iff the test swapped in its own hub
    # module since this setup; the genuine (or not-yet-imported) hub is a reach.
    hub_at_setup = sys.modules.get("huggingface_hub")

    def _ctor_tripwire(*args, **kwargs):
        reaches.append("_SentenceTransformer")
        raise RuntimeError("#4489 guard: real _SentenceTransformer reached")

    def _make_resolve_tripwire(real_resolve):
        def _resolve_tripwire(*args, **kwargs):
            hub_now = sys.modules.get("huggingface_hub")
            if hub_now is not None and hub_now is not hub_at_setup:
                return real_resolve(*args, **kwargs)
            reaches.append("_resolve_pinned_snapshot")
            raise RuntimeError("#4489 guard: real _resolve_pinned_snapshot reached")
        return _resolve_tripwire

    for mod in _embedding_module_objects(request):
        monkeypatch.setattr(mod, "_resolve_pinned_snapshot",
                            _make_resolve_tripwire(mod._resolve_pinned_snapshot),
                            raising=True)
        monkeypatch.setattr(mod, "_SentenceTransformer", _ctor_tripwire, raising=True)
    yield reaches
    assert_no_loader_reaches(reaches)
