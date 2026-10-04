"""Contract tests for tests/_forum_deps.import_forum_or_skip (#3381).

Contract: import a forum module; SKIP only when a THIRD-PARTY dependency is
missing (a ModuleNotFoundError naming a non-forum module). A missing
`forum` / `forum.*` module re-raises, and any other ImportError (e.g. the
tools/forum.py shadowing failure "cannot import name 'db' from 'forum'")
propagates untouched. The skip is derived from the failing import itself, so
it holds for a dependency the forum package gains tomorrow without a list edit.

Absence is simulated with a sys.meta_path finder, so these run whether or not
the real forum deps are installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from _forum_deps import import_forum_or_skip

_SRC = str(Path(__file__).resolve().parent.parent / "src")


class _BlockingFinder:
    def __init__(self, blocked: str, exc: Exception):
        self.blocked = blocked
        self.exc = exc

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.blocked or fullname.startswith(self.blocked + "."):
            raise self.exc
        return None


def _evict(blocked: str | None):
    return [
        k
        for k in list(sys.modules)
        if k == "forum"
        or k.startswith("forum.")
        or (blocked and (k == blocked or k.startswith(blocked + ".")))
    ]


@pytest.fixture
def block(monkeypatch):
    """Install a blocking finder; evict forum* (+ the blocked module); restore after."""
    saved = {}
    installed = []

    def _block(blocked: str, exc: Exception):
        for k in _evict(blocked):
            saved[k] = sys.modules.pop(k)
        monkeypatch.syspath_prepend(_SRC)
        finder = _BlockingFinder(blocked, exc)
        sys.meta_path.insert(0, finder)
        installed.append(finder)

    yield _block
    for f in installed:
        sys.meta_path.remove(f)
    for k in _evict(None):
        sys.modules.pop(k, None)
    sys.modules.update(saved)


def _import_no_skip(modname: str):
    """Call the helper in a cell whose contract is "must NOT skip".

    `pytest.skip` raises `Skipped`, a BaseException that passes straight through
    `pytest.raises(...)` and turns the outcome into "skipped" rather than failed.
    Convert a skip into an explicit failure so a wrong-skip regression is red.
    """
    try:
        return import_forum_or_skip(modname)
    except pytest.skip.Exception as s:
        pytest.fail(f"helper must not skip here: {s}")


@pytest.mark.parametrize("dep", ["bleach", "bs4", "markdown_it", "flask"])
def test_missing_third_party_dep_skips_naming_it(block, dep):
    block(dep, ModuleNotFoundError(f"No module named {dep!r}", name=dep))
    with pytest.raises(pytest.skip.Exception) as ei:
        import_forum_or_skip("forum.server")
    assert dep in str(ei.value)


def test_missing_internal_forum_module_reraises(block):
    block(
        "forum.render",
        ModuleNotFoundError("No module named 'forum.render'", name="forum.render"),
    )
    with pytest.raises(ModuleNotFoundError) as ei:
        _import_no_skip("forum.server")
    assert ei.value.name == "forum.render"


def test_non_modulenotfound_importerror_propagates(block):
    block("forum.render", ImportError("cannot import name 'x' from 'forum'"))
    with pytest.raises(ImportError) as ei:
        _import_no_skip("forum.server")
    assert not isinstance(ei.value, ModuleNotFoundError)
    assert "cannot import name" in str(ei.value)


def test_plain_importerror_naming_third_party_propagates(block):
    # A real `from bleach import nonexistent` raises ImportError(name="bleach"),
    # NOT ModuleNotFoundError: bleach is installed but broken/incompatible, which
    # is a real failure and must not be skipped as a "missing dependency".
    block(
        "forum.render",
        ImportError("cannot import name 'x' from 'bleach'", name="bleach"),
    )
    with pytest.raises(ImportError) as ei:
        _import_no_skip("forum.server")
    assert not isinstance(ei.value, ModuleNotFoundError)
    assert ei.value.name == "bleach"


def test_nothing_blocked_returns_module(block):
    pytest.importorskip("bleach")
    pytest.importorskip("flask")
    block("zz_not_a_real_blocked_module", ImportError("unused"))
    mod = _import_no_skip("forum.db")
    assert mod.__name__ == "forum.db"
