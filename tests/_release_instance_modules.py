"""Release module copies pinned by a cached test instance (#4427).

pytest caches a class-based test's instance on its Function item (`item._instance`), and the
item list lives for the whole session. A test that stores a re-imported `server` / `engram_*`
module on `self` (the `self.server = _fresh_server_module(...)` pattern) therefore pins that
module copy, and its whole `engram_*` set, until the session ends. Dropping the attribute right
after the test makes the copy collectable.

Swept, and nothing else: instance attributes of the item's CACHED instance whose value is a
`types.ModuleType` (subclasses included). Other attributes are left alone, the instance's
`__dict__` is never cleared wholesale, and `item.instance` is never read (it would create an
instance for an item that never had one). Never imports anything, never raises.
"""

from __future__ import annotations

import types


def release_instance_module_attrs(item) -> int:
    """Delete module-valued attributes from `item`'s cached instance; return how many."""
    try:
        try:
            inst = item.__dict__.get("_instance")
        except Exception:
            inst = None
        if inst is None:
            inst = getattr(item, "_instance", None)
        if inst is None:
            return 0
        d = getattr(inst, "__dict__", None)
        if not isinstance(d, dict):
            return 0
        removed = 0
        for name in [k for k, v in list(d.items()) if isinstance(v, types.ModuleType)]:
            try:
                del d[name]
                removed += 1
            except Exception:
                continue
        return removed
    except Exception:
        return 0
