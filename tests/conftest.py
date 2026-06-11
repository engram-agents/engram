"""Pytest configuration for engram-alpha tests.

Adds the repo root to sys.path so test files can `import server`.
This mirrors the `sys.path.insert(0, dirname(__file__))` pattern each
file uses when run as a script — keeping tests runnable both ways.
"""

import os
import socket
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")
if SRC_ENGRAM not in sys.path:
    sys.path.insert(0, SRC_ENGRAM)
# Phase 2: tools/engine moved to src/build/engine; tools/engine is a compat
# symlink at repo root. REPO_ROOT must come BEFORE SRC_ENGRAM in sys.path so
# Python finds tools/ at REPO_ROOT/tools/ (which has the engine symlink) rather
# than at src/engram/tools/ (which no longer has engine/ after Phase 2).
# pytest may have already added REPO_ROOT at a later position; force it to front.
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

# Test-mode bypass for the _get_db() fail-loud guards added with the plugin
# bootstrap contract. Test fixtures monkey-patch DATA_DIR/DB_PATH to a tempdir
# and let the first tool handler's _get_db() open-and-create the DB — the
# fail-loud would block the legitimate test-fixture setup otherwise. Matches
# the ENGRAM_NO_EMBEDDINGS / ENGRAM_NO_POLARITY precedent. Set early so test
# files that import server at module load time see it.
os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")


@pytest.fixture(autouse=True)
def mock_dns_resolution(request, monkeypatch):
    """Mock socket.getaddrinfo so tests don't depend on DNS infra.

    Tests that exercise the DNS-validation code path explicitly opt out via
    @pytest.mark.real_dns marker (test_url_validation.py's DNS-checking tests).

    Default behavior: fake hostnames with 'totally-fake-domain'
    in them raise gaierror (EAI_NONAME) to preserve fabricated-URL test coverage;
    everything else resolves to 127.0.0.1.
    """
    if request.node.get_closest_marker("real_dns"):
        return

    def fake_getaddrinfo(host, port=None, *args, **kwargs):
        if "totally-fake-domain" in host:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)


def fresh_module(mod: types.ModuleType) -> types.ModuleType:
    """Return a freshly reloaded copy of a repo module, isolated for testing.

    R4 rationale (import-pattern contract v1.1): the caller passes the imported
    module **object** (e.g. ``fresh_module(engram_focus)``), not a string.  The
    import statement ``import engram_focus`` at the call site is the AST-visible
    dependency edge that ``run_touched_tests.py`` needs for precise selection.  A
    string-parameterised helper would be invisible to ``ast.walk`` and would
    reintroduce the opacity the contract removes.

    The purge covers ``server`` and all ``engram_*`` family modules so that
    cross-module state (e.g. ``engram_core.DATA_DIR`` shared between server and
    family modules) resets consistently.  After purge the module is re-imported
    by its ``__name__`` (the import machinery reloads all transitive deps too).

    For modules that expose ``_configure_paths`` (server-backed family modules),
    the caller is responsible for calling it with a temp dir *after* this
    function returns — ``fresh_module`` does not know which temp dir to use.
    See ``tests/IMPORT_RUBRIC.md`` §R4 for the full usage pattern.

    Args:
        mod: The imported module object whose family should be reloaded.

    Returns:
        The freshly imported module (same ``__name__`` as *mod*).
    """
    mod_name = mod.__name__
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import importlib
    return importlib.import_module(mod_name)
