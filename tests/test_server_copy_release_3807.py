"""Replaced `server` copies must become collectable between tests (#3807).

Contract (written before the helper):
  C1 after tests each purge + fresh-import `server`, the FIRST copy's `engram_core` is
     collectable in a LATER test (autouse wiring), and a direct release between two fresh
     imports kills the first copy.
  C2 the sweep clears lru_cache wrappers only in `fastmcp` / `uncalled_for` modules and the two
     named dicts in `uncalled_for.introspection`; never imports; never raises; returns the count.
  C3 `release_if_server_changed` sweeps only when the `server` module object changed
     (identity by weak reference: a freed predecessor never makes it skip; it never pins).
  C5 the autouse fixture works in a NESTED pytest session (no `tests/` on sys.path): the
     conftest loader resolves the helper by absolute path.
  C6 one module, one state: the loader returns the `sys.modules` entry if present, else
     registers what it loads, so the fixture and a test's `import` share one module object.
  C7 the loader never raises: a missing/unloadable helper gives None.
  C4 outcome-neutral (checked by running the suite files with and without the fixture).
"""

import functools
import gc
import os
import sys
import types
import weakref

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _release_server_copies as rsc  # noqa: E402  (tests/ is on sys.path)

_REFS = []


def _fresh_server(tmp_path):
    for k in list(sys.modules):
        if k == "server" or k.startswith("engram_"):
            del sys.modules[k]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server

    server._configure_paths(str(tmp_path))
    server._ensure_data_dir()
    import engram_core

    return weakref.ref(engram_core)


def _drop_fastmcp_modules(monkeypatch):
    for k in [k for k in sys.modules if k.partition(".")[0] in ("fastmcp", "uncalled_for")]:
        monkeypatch.delitem(sys.modules, k)


# ---- C1: wiring (order-dependent trio) -------------------------------------------------

def test_c1_a_fresh_import(tmp_path):
    _REFS.append(_fresh_server(tmp_path))


def test_c1_b_fresh_import(tmp_path):
    _REFS.append(_fresh_server(tmp_path))


def test_c1_c_first_copy_is_collectable_in_a_later_test():
    assert len(_REFS) == 2, "C1 cells must run in file order"
    gc.collect()
    assert _REFS[0]() is None, "first server copy is still alive: autouse release missing"


def test_c1_direct_release_between_fresh_imports(tmp_path):
    first = _fresh_server(tmp_path)
    _fresh_server(tmp_path)
    rsc.release_retained_server_copies()
    gc.collect()
    assert first() is None


# ---- C2: safety, with fakes ----------------------------------------------------------------

@pytest.fixture
def fakes(monkeypatch):
    def mk(name):
        m = types.ModuleType(name)

        @functools.lru_cache(maxsize=None)
        def fn(x):
            return x

        fn(1)
        m.fn = fn
        m._other_cache = {"k": 1}
        monkeypatch.setitem(sys.modules, name, m)
        return m

    return mk


def test_c2_clears_lru_in_fastmcp_not_plain_dict(fakes):
    m = fakes("fastmcp._probe3807")
    assert m.fn.cache_info().currsize == 1
    n = rsc.release_retained_server_copies()
    assert m.fn.cache_info().currsize == 0
    assert m._other_cache == {"k": 1}
    assert isinstance(n, int)


def test_c2_clears_lru_in_uncalled_for(fakes):
    m = fakes("uncalled_for._probe3807")
    rsc.release_retained_server_copies()
    assert m.fn.cache_info().currsize == 0
    # #4541: uncalled_for.* `*_cache` plain dicts are now cleared by rule (R1)
    assert m._other_cache == {}


def test_c2_other_package_untouched(fakes):
    m = fakes("otherpkg3807")
    rsc.release_retained_server_copies()
    assert m.fn.cache_info().currsize == 1


def test_c2_exact_count_on_fakes(monkeypatch):
    _drop_fastmcp_modules(monkeypatch)
    m = types.ModuleType("fastmcp._count3807")
    m.a = functools.lru_cache(maxsize=None)(lambda x: x)
    m.b = functools.lru_cache(maxsize=None)(lambda x: x)
    m.plain = lambda x: x
    monkeypatch.setitem(sys.modules, "fastmcp._count3807", m)
    intro = types.ModuleType("uncalled_for.introspection")
    intro._signature_cache = {1: 1}
    intro._parameter_cache = {2: 2}
    monkeypatch.setitem(sys.modules, "uncalled_for.introspection", intro)
    assert rsc.release_retained_server_copies() == 4
    assert intro._signature_cache == {} and intro._parameter_cache == {}


# Name kept for the test-id set (test-id-set-check); it now also pins that a non-`*_cache` name survives.
def test_c2_clears_the_two_introspection_dicts_only(monkeypatch):
    intro = types.ModuleType("uncalled_for.introspection")
    intro._signature_cache = {1: 1}
    intro._parameter_cache = {2: 2}
    intro._unrelated_state = {3: 3}
    monkeypatch.setitem(sys.modules, "uncalled_for.introspection", intro)
    rsc.release_retained_server_copies()
    assert intro._signature_cache == {} and intro._parameter_cache == {}
    assert intro._unrelated_state == {3: 3}


def test_c2_never_imports(monkeypatch):
    _drop_fastmcp_modules(monkeypatch)
    assert rsc.release_retained_server_copies() == 0
    assert "fastmcp" not in sys.modules and "uncalled_for" not in sys.modules


def test_c2_none_marker_skipped_silently(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastmcp._broken3807", None)
    assert isinstance(rsc.release_retained_server_copies(), int)


def test_c2_missing_dict_names_do_not_raise(monkeypatch):
    intro = types.ModuleType("uncalled_for.introspection")
    monkeypatch.setitem(sys.modules, "uncalled_for.introspection", intro)
    assert isinstance(rsc.release_retained_server_copies(), int)  # must not raise


def test_c2_real_introspection_dicts_exist():
    import uncalled_for.introspection as i

    assert isinstance(i._signature_cache, dict) and isinstance(i._parameter_cache, dict)


# ---- C2b (#4541): uncalled_for `*_cache` plain dicts, by rule ----------------------------

def _fake(monkeypatch, name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    monkeypatch.setitem(sys.modules, name, m)
    return m


def test_r1_unseen_module_and_name_are_cleared(monkeypatch):
    m = _fake(monkeypatch, "uncalled_for._newmod3807", _whatever_cache={1: 1}, _again_cache={2: 2})
    rsc.release_retained_server_copies()
    assert m._whatever_cache == {} and m._again_cache == {}


def test_r2_non_cache_name_untouched(monkeypatch):
    m = _fake(monkeypatch, "uncalled_for._r2_3807", _registry={1: 1}, _x_cache={2: 2})
    rsc.release_retained_server_copies()
    assert m._registry == {1: 1}
    assert m._x_cache == {}


def test_r3_non_plain_dict_cache_not_cleared_and_no_raise(monkeypatch):
    class D(dict):
        pass

    m = _fake(monkeypatch, "uncalled_for._r3_3807", _a_cache=[1], _b_cache=None, _c_cache=D({1: 1}))
    rsc.release_retained_server_copies()
    assert m._a_cache == [1] and m._b_cache is None and m._c_cache == {1: 1}


def test_r4_other_packages_untouched(monkeypatch):
    o = _fake(monkeypatch, "otherpkg3807", _x_cache={1: 1})
    f = _fake(monkeypatch, "fastmcp._r4_3807", _y_cache={2: 2})
    rsc.release_retained_server_copies()
    assert o._x_cache == {1: 1}
    assert f._y_cache == {2: 2}


def test_r4b_lookalike_package_names_untouched(monkeypatch):
    a = _fake(monkeypatch, "uncalled_forever3807", _x_cache={1: 1})
    b = _fake(monkeypatch, "uncalled_for_x3807", _x_cache={1: 1})
    rsc.release_retained_server_copies()
    assert a._x_cache == {1: 1}
    assert b._x_cache == {1: 1}


def test_r5_exact_count_includes_new_dicts(monkeypatch):
    _drop_fastmcp_modules(monkeypatch)
    _fake(monkeypatch, "fastmcp._r5_3807", a=functools.lru_cache(maxsize=None)(lambda x: x))
    _fake(monkeypatch, "uncalled_for._r5_3807", b=functools.lru_cache(maxsize=None)(lambda x: x),
          _p_cache={1: 1}, _q_cache={2: 2}, _registry={3: 3}, _n_cache=None)
    assert rsc.release_retained_server_copies() == 4


def test_r6_never_raises_on_hostile_modules(monkeypatch):
    class Hostile:
        @property
        def __dict__(self):
            raise RuntimeError("no vars")

    monkeypatch.setitem(sys.modules, "uncalled_for._none3807", None)
    monkeypatch.setitem(sys.modules, "uncalled_for._hostile3807", Hostile())
    good = _fake(monkeypatch, "uncalled_for._good3807", _z_cache={1: 1})
    assert isinstance(rsc.release_retained_server_copies(), int)
    assert good._z_cache == {}  # a hostile neighbour does not stop the sweep


def test_r7_every_real_uncalled_for_cache_dict_is_cleared():
    import importlib
    import pkgutil

    uf = importlib.import_module("uncalled_for")
    mods = [uf]
    for info in pkgutil.walk_packages(uf.__path__, "uncalled_for."):
        mods.append(importlib.import_module(info.name))
    filled = []
    for mod in mods:
        for name, obj in list(vars(mod).items()):
            if name.endswith("_cache") and type(obj) is dict:
                obj[("sentinel3807", name)] = 1
                filled.append((mod.__name__, name))
    assert filled, "found no uncalled_for *_cache dicts at all: the guard would pass vacuously"
    rsc.release_retained_server_copies()
    left = [(m.__name__, n) for m in mods for n, o in vars(m).items()
            if n.endswith("_cache") and type(o) is dict and o]
    assert not left, f"uncalled_for cache dicts survived the release: {left}"


# ---- C3: trigger ---------------------------------------------------------------------------

@pytest.fixture
def reset_state():
    rsc._reset_for_tests()
    yield
    rsc._reset_for_tests()


def test_c3_first_call_with_server_sweeps_second_does_not(reset_state, monkeypatch):
    monkeypatch.setitem(sys.modules, "server", types.ModuleType("server"))
    assert rsc.release_if_server_changed() is True
    assert rsc.release_if_server_changed() is False


def test_c3_replaced_server_sweeps(reset_state, monkeypatch):
    first = types.ModuleType("server")  # still referenced elsewhere while replaced
    monkeypatch.setitem(sys.modules, "server", first)
    rsc.release_if_server_changed()
    monkeypatch.setitem(sys.modules, "server", types.ModuleType("server"))
    assert first is not sys.modules["server"]
    assert rsc.release_if_server_changed() is True


def test_c3_replaced_server_sweeps_when_first_is_freed(reset_state, monkeypatch):
    first = types.ModuleType("server")
    monkeypatch.setitem(sys.modules, "server", first)
    assert rsc.release_if_server_changed() is True
    ref = weakref.ref(first)
    del first
    sys.modules.pop("server", None)
    _assert_freed(ref, "first server module", [1])
    sys.modules["server"] = types.ModuleType("server")
    assert rsc.release_if_server_changed() is True


def _assert_freed(ref, what, budget):
    """The 'freed predecessor' precondition is observed, not assumed (#4420): a bare module has
    no cycle, so CPython frees it when its last reference goes. If it is still alive, spend the
    cell's ONE collection (`budget == [1]`, created per cell) and look again; if the budget is
    already spent, fail at once without collecting, so a retained reference can never turn a
    cell back into a loop of full collections."""
    if ref() is not None:
        assert budget[0] > 0, (
            f"{what} is still alive and this cell's one collection was already spent"
        )
        budget[0] -= 1
        gc.collect()
    assert ref() is None, f"{what} is still alive: the freed-predecessor precondition failed"


def test_c3a_freed_predecessor_never_makes_helper_skip(reset_state, monkeypatch):
    monkeypatch.setitem(sys.modules, "server", types.ModuleType("server"))  # restores after
    results = []
    budget = [1]  # one collection for the whole 50-iteration loop
    for _ in range(50):
        ref = weakref.ref(sys.modules["server"])
        sys.modules.pop("server", None)
        _assert_freed(ref, "predecessor server module", budget)
        sys.modules["server"] = types.ModuleType("server")
        results.append(rsc.release_if_server_changed())
    assert results == [True] * 50, f"{results.count(False)} of 50 replacements were missed"


def test_c3b_helper_does_not_keep_server_alive(reset_state, monkeypatch):
    server = types.ModuleType("server")
    monkeypatch.setitem(sys.modules, "server", server)
    assert rsc.release_if_server_changed() is True
    ref = weakref.ref(server)
    del server
    sys.modules.pop("server", None)
    _assert_freed(ref, "helper pins the last server module", [1])


def test_c3_assert_freed_fails_fast_when_the_collection_budget_is_spent(monkeypatch):
    """A retained reference (a tracer, a plugin) must not turn a cell into a collect loop: the
    first survivor spends the cell's one collection, the second fails without collecting."""
    calls = []
    monkeypatch.setattr(gc, "collect", lambda *a, **k: calls.append(1) or 0)
    alive = types.ModuleType("server")  # kept alive on purpose
    ref = weakref.ref(alive)
    budget = [1]
    with pytest.raises(AssertionError):  # budget spent on this one, still alive
        _assert_freed(ref, "first survivor", budget)
    with pytest.raises(AssertionError, match="already spent") as exc:
        _assert_freed(ref, "second survivor", budget)
    assert "second survivor" in str(exc.value)
    assert len(calls) == 1, f"expected exactly one collection, saw {len(calls)}"


def test_c3_cells_do_not_collect_on_the_healthy_path(reset_state, monkeypatch):
    """#4420: bare-module cells free by refcount, so none may call gc.collect at all on CPython
    (count must be exactly zero); on other interpreters at most one (the cell's budget). The
    replacement is a pure counter and never collects, so this cell cannot itself pay for a
    full collection on a heavy heap; an unhealthy cell fails its freed assertion instead."""
    calls = []
    monkeypatch.setattr(gc, "collect", lambda *a, **k: calls.append(1) or 0)
    limit = 0 if sys.implementation.name == "cpython" else 1
    for cell in (
        test_c3_replaced_server_sweeps_when_first_is_freed,
        test_c3a_freed_predecessor_never_makes_helper_skip,
        test_c3b_helper_does_not_keep_server_alive,
    ):
        del calls[:]
        rsc._reset_for_tests()
        cell(reset_state, monkeypatch)
        assert len(calls) <= limit, f"{cell.__name__} called gc.collect {len(calls)} times"


def test_c3_absent_server_does_not_sweep(reset_state, monkeypatch):
    monkeypatch.delitem(sys.modules, "server", raising=False)
    assert rsc.release_if_server_changed() is False


# ---- C5-C7: the conftest loader (nested pytest sessions) --------------------------------------

_HELPER = "_release_server_copies"


@pytest.fixture
def conftest_mod(request):
    """The conftest module that holds `_load_release_helper`, found via the plugin manager (its
    module name depends on the import mode), so the cells do not guess a `sys.modules` key."""
    for plugin in request.config.pluginmanager.get_plugins():
        if hasattr(plugin, "_load_release_helper"):
            return plugin
    raise AssertionError("no conftest plugin exposes _load_release_helper")


def _tests_dir():
    return os.path.dirname(os.path.abspath(__file__))


def test_c5_loader_works_without_tests_dir_on_sys_path(conftest_mod, monkeypatch):
    here = _tests_dir()
    monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.abspath(p or ".") != here])
    monkeypatch.delitem(sys.modules, _HELPER, raising=False)
    before = list(sys.path)
    mod = conftest_mod._load_release_helper()
    assert mod is not None and hasattr(mod, "release_if_server_changed")
    assert sys.path == before, "the loader must not touch sys.path"


def test_c6_a_fresh_import_for_shared_state(tmp_path):
    rsc._reset_for_tests()
    _fresh_server(tmp_path)


def test_c6_b_fixture_and_test_file_share_one_module():
    assert rsc._last_server_ref is not None, "fixture did not update the module the tests import"
    rsc._reset_for_tests()


def test_c6_c_loader_registers_what_it_loads(conftest_mod, monkeypatch):
    monkeypatch.delitem(sys.modules, _HELPER, raising=False)
    mod = conftest_mod._load_release_helper()
    assert mod is not None and sys.modules.get(_HELPER) is mod
    assert conftest_mod._load_release_helper() is mod


def test_c7_loader_returns_none_on_missing_file(conftest_mod, monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, _HELPER, raising=False)
    monkeypatch.setattr(conftest_mod, "__file__", str(tmp_path / "empty_dir" / "conftest.py"))
    assert conftest_mod._load_release_helper() is None
    assert _HELPER not in sys.modules


def test_c7_loader_returns_none_on_exec_error(conftest_mod, monkeypatch, tmp_path):
    (tmp_path / f"{_HELPER}.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.delitem(sys.modules, _HELPER, raising=False)
    monkeypatch.setattr(conftest_mod, "__file__", str(tmp_path / "conftest.py"))
    assert conftest_mod._load_release_helper() is None
    assert _HELPER not in sys.modules
