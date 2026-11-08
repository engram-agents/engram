"""Forum test defaults.

#3638: the server's PR-baton init asks GitHub whether the PR exists
(`baton_gate._pr_identity`). The forum suite creates PR batons with made-up
numbers and must never reach the network, so by default the reader answers
"exists, author unknown" (existence passes; the author check has nothing to
compare). `test_baton_init_pr_exists_3638.py` overrides it per case.

The stub is set on the module object `forum.server._load_baton_gate()` returns,
which is the one the server actually calls.
"""
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
