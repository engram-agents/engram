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
