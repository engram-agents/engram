"""engram-alpha#4029 -- one shared window for in-process hook / tools loads, and the
`sys.path[0]` half of the #4023 polluter check turned hard.

The contract these cells state:

    H1. `hook_load_window()` (tests/_hook_load_window.py): inside the window
        anything may change `sys.path` and `sys.modules`. On exit
          (a) `sys.path` is exactly the list it was (same entries, same order);
          (b) every `sys.modules` key ADDED in the window whose module lives under
              src/engram (hooks, tools, core) or is named `tools` / `tools.*` is
              removed, and every such key that was REPLACED has its original
              object back (identity); keys added from elsewhere (stdlib, third
              party, stubs without a file) are left alone.
        Usable at function scope (a context manager, or the `isolate_hook_loads`
        fixture) and at module scope (the `isolate_hook_loads_module` fixture).
    H2. In `tests/conftest.py` the post-setup `sys.path[0]` leak into src/engram
        is a HARD error: at the polluter's own module teardown, naming the
        module, with `sys.path` restored EXACTLY to its setup value before the
        raise (so a following clean module sees the setup value).

H1 function-scope cells run in this process; the module-scope and H2 cells run a
real pytest in a SUBPROCESS over planted modules, under the shipped conftest.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TESTS_DIR = REPO_ROOT / "tests"
TIMEOUT_S = 600


def hook_load_window():
    # Imported at call time so each cell reports on its own (not one collection error).
    from _hook_load_window import hook_load_window as window
    return window()


# --- H1: the window, in process ---------------------------------------------------

def test_window_restores_sys_path_exactly_and_drops_added_tools_modules():
    before = list(sys.path)
    before_obj = sys.path
    with hook_load_window():
        sys.path.insert(0, str(SRC_ENGRAM / "hooks" / "claude"))
        sys.path.insert(0, str(SRC_ENGRAM / "hooks"))
        sys.path.append("/nonexistent-4029-tail")
        sys.modules["tools.planted_4029"] = types.ModuleType("tools.planted_4029")
        assert sys.path != before, "control: the window really changed sys.path"
    assert sys.path == before
    assert sys.path is before_obj, "restored in place, not rebound"
    assert "tools.planted_4029" not in sys.modules


def test_window_cleans_the_original_list_even_when_sys_path_is_rebound_inside_it(monkeypatch):
    """A test that loads a hook (which inserts into the live list) and THEN rebinds
    `sys.path` to a copy must not leave the polluted original behind."""
    original = sys.path
    before = list(original)
    with hook_load_window():
        sys.path.insert(0, str(SRC_ENGRAM / "hooks" / "claude"))
        monkeypatch.setattr(sys, "path", list(sys.path))
        sys.path.insert(0, str(SRC_ENGRAM))
    monkeypatch.undo()
    assert sys.path is original
    assert sys.path == before


def test_window_drops_an_added_module_whose_file_is_under_src_engram():
    mod = types.ModuleType("planted_core_4029")
    mod.__file__ = str(SRC_ENGRAM / "planted_core_4029.py")
    with hook_load_window():
        sys.modules["planted_core_4029"] = mod
    assert "planted_core_4029" not in sys.modules


def test_window_puts_back_the_exact_object_of_a_replaced_entry():
    original = types.ModuleType("tools.replaced_4029")
    sys.modules["tools.replaced_4029"] = original
    try:
        with hook_load_window():
            sys.modules["tools.replaced_4029"] = types.ModuleType("tools.replaced_4029")
            assert sys.modules["tools.replaced_4029"] is not original, "control"
        assert sys.modules["tools.replaced_4029"] is original
    finally:
        sys.modules.pop("tools.replaced_4029", None)


def test_window_puts_back_a_qualifying_key_deleted_inside_it():
    """A key present at entry and DELETED in the window (a test that pops a module
    before its own fresh import) gets the exact same object back on exit.
    Mutation: drop the deleted-key arm of the restore -> this cell is red."""
    original = types.ModuleType("tools.planted_r2")
    sys.modules["tools.planted_r2"] = original
    try:
        with hook_load_window():
            del sys.modules["tools.planted_r2"]
            assert "tools.planted_r2" not in sys.modules, "control"
        assert sys.modules.get("tools.planted_r2") is original
    finally:
        sys.modules.pop("tools.planted_r2", None)


# --- H1: a popped submodule must not survive as an attribute on its cached parent --

@pytest.fixture
def cached_parent(monkeypatch):
    """A synthetic `tools` package object, cached before the window opens. Never the
    real `tools` package; monkeypatch puts back whatever was in sys.modules."""
    parent = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", parent)
    return parent


def test_window_removes_the_parent_attribute_of_a_popped_submodule_absent_at_entry(cached_parent):
    assert not hasattr(cached_parent, "planted_r1"), "control: absent at entry"
    with hook_load_window():
        mod = types.ModuleType("tools.planted_r1")
        sys.modules["tools.planted_r1"] = mod
        cached_parent.planted_r1 = mod
        assert cached_parent.planted_r1 is mod, "control"
    assert "tools.planted_r1" not in sys.modules
    assert not hasattr(cached_parent, "planted_r1")


def test_window_restores_the_parent_attribute_object_that_was_there_at_entry(cached_parent):
    entry_obj = types.ModuleType("tools.planted_r1_entry")
    cached_parent.planted_r1 = entry_obj
    with hook_load_window():
        mod = types.ModuleType("tools.planted_r1")
        sys.modules["tools.planted_r1"] = mod
        cached_parent.planted_r1 = mod
        assert cached_parent.planted_r1 is not entry_obj, "control"
    assert "tools.planted_r1" not in sys.modules
    assert cached_parent.planted_r1 is entry_obj


def test_window_leaves_an_unrelated_parent_attribute_set_in_the_window_alone(cached_parent):
    unrelated = object()
    with hook_load_window():
        mod = types.ModuleType("tools.planted_r1")
        sys.modules["tools.planted_r1"] = mod
        cached_parent.planted_r1 = mod
        cached_parent.other_r1 = unrelated
    assert cached_parent.other_r1 is unrelated


def test_window_leaves_the_same_named_parent_attribute_alone_when_it_no_longer_is_the_popped_module(cached_parent):
    """The identity guard: the window popped `tools.planted_r3`, but the test then
    rebound the SAME attribute name on the parent to its own object. That object is
    the test's, so it must survive (not deleted, not overwritten by the entry value).
    Mutation: the guard always restores -> the sentinel is deleted -> red."""
    sentinel = object()
    with hook_load_window():
        mod = types.ModuleType("tools.planted_r3")
        sys.modules["tools.planted_r3"] = mod
        cached_parent.planted_r3 = mod
        cached_parent.planted_r3 = sentinel
        assert cached_parent.planted_r3 is not mod, "control"
    assert "tools.planted_r3" not in sys.modules
    assert cached_parent.planted_r3 is sentinel


def test_window_leaves_a_stdlib_module_imported_in_it_alone():
    sys.modules.pop("colorsys", None)
    try:
        with hook_load_window():
            import colorsys  # noqa: F401
        assert "colorsys" in sys.modules
    finally:
        sys.modules.pop("colorsys", None)


def test_window_leaves_an_added_module_with_no_file_outside_src_engram_alone():
    stub = types.SimpleNamespace(name="stub-4029")
    try:
        with hook_load_window():
            sys.modules["stub_module_4029"] = stub
        assert sys.modules.get("stub_module_4029") is stub
    finally:
        sys.modules.pop("stub_module_4029", None)


def test_window_restores_even_when_the_body_raises():
    before = list(sys.path)
    with pytest.raises(ZeroDivisionError):
        with hook_load_window():
            sys.path.insert(0, str(SRC_ENGRAM))
            sys.modules["tools.planted_4029"] = types.ModuleType("tools.planted_4029")
            1 / 0
    assert sys.path == before
    assert "tools.planted_4029" not in sys.modules


@pytest.mark.usefixtures("isolate_hook_loads")
def test_function_fixture_form_window_is_open_during_the_test():
    """Pairs with the next cell: this test pollutes inside the fixture's window."""
    sys.path.insert(0, str(SRC_ENGRAM / "hooks" / "claude"))
    sys.modules["tools.planted_fixture_4029"] = types.ModuleType("tools.planted_fixture_4029")
    _POLLUTED_BY_FIXTURE_CELL.append(list(sys.path))


_POLLUTED_BY_FIXTURE_CELL: list = []


def test_function_fixture_form_restored_after_its_test():
    assert _POLLUTED_BY_FIXTURE_CELL, "control: the polluting cell ran first"
    assert sys.path[0] != str(SRC_ENGRAM / "hooks" / "claude")
    assert "tools.planted_fixture_4029" not in sys.modules


# --- H1 (module scope) and H2: real pytest in a subprocess ------------------------

def _run_planted(tmp_path, modules, extra_env=None):
    d = tmp_path / "planted"
    d.mkdir()
    for name, body in modules.items():
        (d / name).write_text(body)
    pypath = os.pathsep.join(
        p for p in (str(TESTS_DIR), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ)
    env.update({"PYTHONPATH": pypath, "REPO_ROOT_4029": str(REPO_ROOT),
                "SETUP_PATH_FILE_4029": str(tmp_path / "setup-path.json")})
    env.update(extra_env or {})
    # `-p conftest` loads the REAL tests/conftest.py for modules living outside tests/.
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(d), "-q", "-p", "no:cacheprovider",
         "-p", "conftest", "--rootdir", str(d)],
        cwd=str(d), env=env, capture_output=True, text=True, timeout=TIMEOUT_S)


_RECORD_SETUP_PATH = textwrap.dedent('''\
    import json, os, sys
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _record_setup_path():
        with open(os.environ["SETUP_PATH_FILE_4029"], "w") as fh:
            json.dump(list(sys.path), fh)
        yield
    ''')

_MODULE_SCOPE_LOADER = _RECORD_SETUP_PATH + textwrap.dedent('''\
    import os, types

    pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")

    def test_first_load_stays_visible_to_the_next_test_in_the_module():
        sys.path.insert(0, os.path.join(os.environ["REPO_ROOT_4029"], "src", "engram", "hooks", "claude"))
        sys.modules["tools.planted_4029"] = types.ModuleType("tools.planted_4029")

    def test_module_scope_keeps_the_loaded_module_until_the_module_ends():
        assert "tools.planted_4029" in sys.modules
        assert sys.path[0].endswith(os.path.join("src", "engram", "hooks", "claude"))
    ''')

_FOLLOWER_CHECKS_SETUP_PATH = textwrap.dedent('''\
    import json, os, sys

    def test_follower_sees_the_setup_path_exactly():
        with open(os.environ["SETUP_PATH_FILE_4029"]) as fh:
            assert sys.path == json.load(fh)

    def test_follower_has_no_planted_tools_module():
        assert "tools.planted_4029" not in sys.modules
    ''')


def test_module_scope_window_restores_at_module_end_and_the_check_stays_quiet(tmp_path):
    proc = _run_planted(tmp_path, {
        "test_loader.py": _MODULE_SCOPE_LOADER,
        "test_zz_follower.py": _FOLLOWER_CHECKS_SETUP_PATH})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "4 passed" in out, out[-3000:]
    assert "#4023" not in out, out[-3000:]


_PLANT_PATH_LEAK = _RECORD_SETUP_PATH + textwrap.dedent('''\
    import os

    @pytest.fixture(scope="module", autouse=True)
    def _pollute():
        yield
        sys.path.insert(0, os.path.join(os.environ["REPO_ROOT_4029"], "src", "engram", "hooks", "claude"))

    def test_pollutes_path():
        assert True
    ''')


def test_h2_a_path_leaker_errors_at_its_own_teardown_and_the_follower_is_not_hit(tmp_path):
    """H2. The leaker ERRORS at its own teardown, named; `sys.path` is restored
    to its setup value BEFORE the raise, so the follower (which compares against
    the leaker's recorded setup value) passes. Exactly one error.
    Mutation: raise without restoring -> the follower half is red.
    Mutation: path half back to non-failing -> no error, the error asserts are red."""
    proc = _run_planted(tmp_path, {
        "test_plant_leak.py": _PLANT_PATH_LEAK,
        "test_zz_follower.py": _FOLLOWER_CHECKS_SETUP_PATH})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "ERROR at teardown of test_pollutes_path" in out, out[-3000:]
    assert "test_plant_leak" in out, out[-3000:]
    assert re.search(r"\b1 error\b", out), out[-3000:]
    assert "3 passed" in out, out[-3000:]
    assert "test_follower_sees_the_setup_path_exactly" not in "".join(
        ln for ln in out.splitlines() if ln.startswith(("ERROR", "FAILED"))), out[-3000:]
