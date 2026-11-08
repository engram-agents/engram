"""Module-valued attributes on a cached test instance must not outlive the test (#4427).

pytest caches a class-based test's instance on its Function item, and the item list lives for
the whole session, so `self.server = <re-imported module>` pins that module copy (and its whole
`engram_*` set) until the session ends. A central `pytest_runtest_teardown` hook in
`tests/conftest.py` deletes module-valued attributes from the cached instance.

Contract (written before the helper):
  H1 the helper deletes exactly the instance attributes whose value is a `types.ModuleType`
     (subclass instances included), leaves every other attribute, returns the number removed.
  H2 an instance with `__slots__`, an item with no cached instance, and a `None` instance do
     not raise and return 0.
  H3 it never calls `item.instance` (that would create an instance for an item without one).
  I1 NESTED pytest session: a class whose setup_method re-imports a stub module and stores it
     on `self` leaves at most 2 live copies at session end with the repo conftest hook active
     (original + the one in sys.modules), and 6 with the hook removed.
  F1 `tests/test_backup_push_status_4205.py` run alone leaves no live `server` module of this
     repo outside sys.modules once the session ends (module global `fresh_server_diag` released).
"""

import os
import subprocess
import sys
import textwrap
import types

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TESTS_DIR)

import _release_instance_modules as rim  # noqa: E402

_REPO = os.path.dirname(_TESTS_DIR)
_REPO_CONFTEST = os.path.join(_TESTS_DIR, "conftest.py")


class _Item:
    def __init__(self, instance=None, cached=True):
        if cached:
            self._instance = instance

    touched = False

    @property
    def instance(self):
        # recorded, not just raised: a helper that swallows the error must still be caught
        type(self).touched = True
        raise AssertionError("helper must not touch item.instance")


class _ModSub(types.ModuleType):
    pass


def test_h1_removes_only_module_attrs():
    inst = types.SimpleNamespace(m=types.ModuleType("a"), n=types.ModuleType("b"), x=1, f=len)
    assert rim.release_instance_module_attrs(_Item(inst)) == 2
    assert sorted(vars(inst)) == ["f", "x"]
    assert inst.x == 1 and inst.f is len


def test_h1_module_subclass_instance_removed():
    inst = types.SimpleNamespace(m=_ModSub("sub"), keep="k")
    assert rim.release_instance_module_attrs(_Item(inst)) == 1
    assert vars(inst) == {"keep": "k"}


def test_h2_slots_none_and_missing_do_not_raise():
    class Slotted:
        __slots__ = ("m",)

    s = Slotted()
    s.m = types.ModuleType("a")
    assert rim.release_instance_module_attrs(_Item(s)) == 0
    assert rim.release_instance_module_attrs(_Item(None)) == 0
    assert rim.release_instance_module_attrs(_Item(cached=False)) == 0
    assert rim.release_instance_module_attrs(object()) == 0


def test_h3_never_calls_item_instance():
    inst = types.SimpleNamespace(m=types.ModuleType("a"))
    _Item.touched = False
    assert rim.release_instance_module_attrs(_Item(inst)) == 1
    assert rim.release_instance_module_attrs(_Item(cached=False)) == 0
    assert _Item.touched is False


# ---- nested sessions ---------------------------------------------------------------------

_COUNT_PLUGIN = """
import gc, os, types


def pytest_sessionfinish(session, exitstatus):
    import sys
    if os.environ.get("CENSUS_PURGE_4427"):
        # what the next test file's fresh import does: sys.modules lets go of the current copy
        for k in list(sys.modules):
            if k == "server" or k.startswith("engram_"):
                del sys.modules[k]
    gc.collect()
    name = os.environ["CENSUS_NAME_4427"]
    root = os.environ.get("CENSUS_ROOT_4427", "")
    n = 0
    for o in gc.get_objects():
        if isinstance(o, types.ModuleType) and getattr(o, "__name__", None) == name:
            if not root or str(getattr(o, "__file__", "") or "").startswith(root):
                n += 1
    with open(os.environ["CENSUS_OUT_4427"], "w") as fh:
        fh.write(str(n))
"""

_CONFTEST = f"""
import importlib.util, os, sys
_spec = importlib.util.spec_from_file_location("_repo_conftest_4427", {_REPO_CONFTEST!r})
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({{k: v for k, v in vars(_mod).items() if not k.startswith("__")}})
if os.environ.get("HOOK_OFF_4427"):
    globals().pop("pytest_runtest_teardown", None)
"""

_STUB_TEST = """
import importlib
import sys

import pytest

import stub_4427  # collection-time original, held by this module global


class TestBursting:
    def setup_method(self, method):
        sys.modules.pop("stub_4427", None)
        self.stub = importlib.import_module("stub_4427")

    @pytest.fixture(autouse=True)
    def _late(self, request):
        yield
        # stored during FIXTURE TEARDOWN: only a trylast hook sees it
        sys.modules.pop("stub_4427", None)
        self.late = importlib.import_module("stub_4427")

    def test_0(self): pass
    def test_1(self): pass
    def test_2(self): pass
    def test_3(self): pass
    def test_4(self): pass
"""


def _nested(tmp_path, args, name, root="", hook_off=False, cwd=None, purge=False):
    out = tmp_path / "census.txt"
    plug = tmp_path / "plug"
    plug.mkdir(exist_ok=True)
    (plug / "census_4427.py").write_text(_COUNT_PLUGIN)
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plug), str(tmp_path)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.update(CENSUS_NAME_4427=name, CENSUS_ROOT_4427=root, CENSUS_OUT_4427=str(out),
               ENGRAM_NO_EMBEDDINGS="1")
    if hook_off:
        env["HOOK_OFF_4427"] = "1"
    if purge:
        env["CENSUS_PURGE_4427"] = "1"
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "census_4427", "-q"]
    cmd += args
    proc = subprocess.run(cmd, cwd=cwd or tmp_path, env=env, capture_output=True, text=True,
                          timeout=300)
    assert out.exists() and proc.returncode == 0, (
        f"nested run failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return int(out.read_text())


def _stub_session(tmp_path, hook_off):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "stub_4427.py").write_text("X = 1\n")
    (tmp_path / "test_burst_4427.py").write_text(textwrap.dedent(_STUB_TEST))
    return _nested(tmp_path, ["--rootdir", str(tmp_path), "-c", os.devnull,
                              "test_burst_4427.py"], "stub_4427", hook_off=hook_off)


def test_i1_hook_active_bounds_live_copies(tmp_path):
    assert _stub_session(tmp_path, hook_off=False) <= 2


def test_i1_control_without_hook_pins_every_copy(tmp_path):
    assert _stub_session(tmp_path, hook_off=True) >= 6


def test_f1_backup_push_status_file_releases_server_global(tmp_path):
    n = _nested(tmp_path, ["tests/test_backup_push_status_4205.py"], "server",
                root=_REPO, cwd=_REPO, purge=True)
    print("F1 census", n)
    assert n == 0, f"{n} live server copies pinned outside sys.modules"
