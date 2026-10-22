"""A window around in-process loads of a hook or an `src/engram/tools` module (#4029).

A hook is meant to set its own `sys.path` (module-level `sys.path.insert(0, ...)`)
and to import its siblings; run inside the pytest process, that leaves the entry
in front of `sys.path` and the imported modules cached for every later test file.
Production code is not changed for this -- the test that loads a hook in process
opens this window around the load instead.

Contract. Within the window anything may change `sys.path` and `sys.modules`.
On exit:
  (a) `sys.path` is restored EXACTLY -- same entries, same order, in place (and the
      list that was `sys.path` at entry is `sys.path` again);
  (b) `sys.modules`: each key ADDED in the window whose module lives under
      src/engram (hooks, tools, core) or is named `tools` / `tools.*` is popped,
      and each such key that was REPLACED (or removed) in the window gets the
      exact original object back. Keys added from elsewhere (stdlib, third party,
      a stub with no file) are left alone.
  (c) parent attribute: for each dotted key popped or restored, whose parent
      package is still in `sys.modules`, the parent's attribute for the last name
      component goes back to its value at entry (absent at entry: deleted) -- but
      only when it still points at the module the window popped or replaced; any
      other value is the test's own and is left alone.
      Bound: an attribute still pointing at a module object the window never saw in
      `sys.modules` at exit (the test overwrote the key, left the attribute) is left
      as is.

Three ways to use it: the context manager `hook_load_window()`; the function-scope
fixture `isolate_hook_loads`; the module-scope fixture `isolate_hook_loads_module`
(both in tests/conftest.py). Use module scope when a module's later tests rely on
the loaded module staying imported.
"""
from __future__ import annotations

import contextlib
import os
import sys

_SRC_ENGRAM = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram")


def _is_tools_name(key: str) -> bool:
    return key == "tools" or key.startswith("tools.")


def _under_src_engram(module) -> bool:
    """True when the module's file is under src/engram. `abspath`, not `realpath`:
    the repo-root `tools/` is a symlink into src/engram/tools and the sys.path
    entry that found a module is what matters (same reasoning as the #2565 guard)."""
    path = getattr(module, "__file__", None)
    if not isinstance(path, str):
        return False
    path = os.path.abspath(path)
    return path == _SRC_ENGRAM or path.startswith(_SRC_ENGRAM + os.sep)


def _ours(key: str, module) -> bool:
    return _is_tools_name(key) or _under_src_engram(module)


@contextlib.contextmanager
def hook_load_window():
    path_obj = sys.path
    path_before = list(path_obj)
    modules_before = dict(sys.modules)
    # Entry-time attributes of every cached module of ours (the parents a dotted key
    # can hang off), taken now because "absent at entry" cannot be learned later.
    attrs_before = {}
    for name, mod in modules_before.items():
        if _ours(name, mod) and isinstance(getattr(mod, "__dict__", None), dict):
            attrs_before[name] = dict(mod.__dict__)
    try:
        yield
    finally:
        # The list object that was `sys.path` at entry, restored IN PLACE, and made
        # `sys.path` again: a test that rebinds `sys.path` inside the window (a
        # monkeypatch.setattr(sys, "path", ...) after the hook already ran) would
        # otherwise leave the polluted original behind when its patch is undone.
        path_obj[:] = path_before
        sys.path = path_obj
        dropped = []  # (key, the module object the window left under that key)
        for key in [k for k in list(sys.modules) if k not in modules_before]:
            if _ours(key, sys.modules[key]):
                dropped.append((key, sys.modules[key]))
                del sys.modules[key]
        for key, original in modules_before.items():
            now = sys.modules.get(key)
            if now is not original and (_ours(key, original) or _ours(key, now)):
                if now is not None:
                    dropped.append((key, now))
                sys.modules[key] = original
        for key, gone in dropped:
            parent_name, _, last = key.rpartition(".")
            parent = sys.modules.get(parent_name) if parent_name else None
            entry = attrs_before.get(parent_name)
            if parent is None or entry is None or getattr(parent, last, None) is not gone:
                continue  # no cached parent, or the attribute is not the popped module
            if last in entry:
                setattr(parent, last, entry[last])
            else:
                delattr(parent, last)
