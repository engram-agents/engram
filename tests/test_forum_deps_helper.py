"""Contract tests for tests/_forum_deps.import_forum_or_skip (#3381).

Contract: import a forum module; SKIP only when a THIRD-PARTY dependency is
missing (a ModuleNotFoundError naming a non-forum module). A missing
`forum` / `forum.*` module re-raises, and any other ImportError (e.g. the
tools/forum.py shadowing failure "cannot import name 'db' from 'forum'")
propagates untouched. The skip is derived from the failing import itself, so
it holds for a dependency the forum package gains tomorrow without a list edit.

The contract runs on a SYNTHETIC `forum` package built under tmp_path that
mirrors the real import shape (render -> bleach/bs4/markdown_it; db -> render;
server -> db + flask). The four third-party names are supplied as empty stub
modules by a sys.meta_path finder, except the one a case blocks. Nothing here
touches the real `src/forum` package or any real third-party module, so the
cells behave identically whether or not the forum deps are installed.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

import pytest

from _forum_deps import import_forum_or_skip

_THIRD_PARTY = ("bleach", "bs4", "markdown_it", "flask")

_PKG_FILES = {
    "forum/__init__.py": "",
    "forum/render.py": "import bleach\nimport bs4\nimport markdown_it\nRENDER = 1\n",
    "forum/db.py": "from .render import RENDER\nDB = RENDER\n",
    "forum/server.py": "from .db import DB\nimport flask\n",
}


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        pass


class _SyntheticFinder:
    """Stub the third-party names; raise `blocked[name]` for any blocked name."""

    def __init__(self, blocked: dict[str, Exception]):
        self.blocked = blocked

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.blocked:
            raise self.blocked[fullname]
        if fullname in _THIRD_PARTY:
            return importlib.util.spec_from_loader(fullname, _StubLoader())
        return None


def _managed(k: str) -> bool:
    return k == "forum" or k.startswith("forum.") or k in _THIRD_PARTY


@pytest.fixture
def block(monkeypatch, tmp_path):
    """Build the synthetic package; `block(name, exc)` installs the finder.

    Evicts real `forum*` and the four third-party names from sys.modules for
    the test and restores them afterwards (dropping whatever the test imported).
    """
    for rel, text in _PKG_FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if _managed(k)}
    monkeypatch.syspath_prepend(str(tmp_path))
    installed = []

    def _block(blocked: str | None, exc: Exception | None = None):
        finder = _SyntheticFinder({blocked: exc} if blocked else {})
        sys.meta_path.insert(0, finder)
        installed.append(finder)
        importlib.invalidate_caches()

    yield _block
    for f in installed:
        sys.meta_path.remove(f)
    for k in [k for k in list(sys.modules) if _managed(k)]:
        del sys.modules[k]
    sys.modules.update(saved)
    importlib.invalidate_caches()


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


@pytest.mark.parametrize("dep", _THIRD_PARTY)
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
    block("bleach", ImportError("cannot import name 'x' from 'bleach'", name="bleach"))
    with pytest.raises(ImportError) as ei:
        _import_no_skip("forum.server")
    assert not isinstance(ei.value, ModuleNotFoundError)
    assert ei.value.name == "bleach"


def test_unlisted_new_dependency_skips_naming_it(block, tmp_path):
    # The forum package "gains a dependency tomorrow": a third-party name that
    # is in neither _THIRD_PARTY nor any list the helper could hard-code. A
    # helper keyed to today's four names would re-raise here instead of skipping.
    (tmp_path / "forum/render.py").write_text(
        "import bleach\nimport bs4\nimport markdown_it\nimport newdep_xyz\nRENDER = 1\n"
    )
    block(
        "newdep_xyz",
        ModuleNotFoundError("No module named 'newdep_xyz'", name="newdep_xyz"),
    )
    with pytest.raises(pytest.skip.Exception) as ei:
        import_forum_or_skip("forum.server")
    assert "newdep_xyz" in str(ei.value)


def test_missing_bare_forum_package_reraises(block):
    # ModuleNotFoundError naming exactly `forum` (the package itself is absent)
    # is a real failure, not a missing third-party dependency: it must propagate.
    block("forum", ModuleNotFoundError("No module named 'forum'", name="forum"))
    with pytest.raises(ModuleNotFoundError) as ei:
        _import_no_skip("forum.server")
    assert ei.value.name == "forum"


def test_nothing_blocked_returns_module(block):
    block(None)
    mod = _import_no_skip("forum.server")
    assert mod.__name__ == "forum.server"
    assert mod.__file__.endswith("server.py")
