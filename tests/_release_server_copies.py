"""Release the memo caches that keep replaced `server` copies alive (#3807).

Tests purge `server` / `engram_*` from sys.modules and re-import per test. Unbounded
function-keyed caches in third-party libs then pin every old copy (and its whole `engram_*`
module set, ~4 MB each) forever. Clearing them right after a fresh import makes the old copy
collectable. Pure memoization is cleared, so this is semantically safe.

Swept, and nothing else: every module-level `functools.lru_cache` wrapper in a module whose
top-level package is `fastmcp` or `uncalled_for`, plus every module-level plain `dict`
(`type(x) is dict`) whose name ends in `_cache` in a module whose top-level package is
`uncalled_for` (a rule, not a name list: releases of uncalled_for add such dicts). Never imports anything, never raises.
"""

from __future__ import annotations

import sys
import weakref

_PACKAGES = ("fastmcp", "uncalled_for")

# weakref.ref to the last server module seen; must NOT keep it alive.
_last_server_ref = None


def _is_lru_wrapper(obj) -> bool:
    return (
        callable(obj)
        and callable(getattr(obj, "cache_clear", None))
        and callable(getattr(obj, "cache_info", None))
    )


def release_retained_server_copies() -> int:
    """Clear the memo caches described in the module docstring; return how many were cleared."""
    cleared = 0
    try:
        mods = list(sys.modules.items())
    except Exception:
        return 0
    for name, mod in mods:
        try:
            if mod is None or name.partition(".")[0] not in _PACKAGES:
                continue
            for obj in list(vars(mod).values()):
                if _is_lru_wrapper(obj):
                    obj.cache_clear()
                    cleared += 1
        except Exception:
            continue
    for name, mod in mods:
        try:
            if mod is None or name.partition(".")[0] != "uncalled_for":
                continue
            for attr, obj in list(vars(mod).items()):
                if attr.endswith("_cache") and type(obj) is dict:
                    obj.clear()
                    cleared += 1
        except Exception:
            continue
    return cleared


def release_if_server_changed() -> bool:
    """Sweep only when the `server` module object differs from the one seen at the last call.

    The previous module is remembered by weak reference, not `id()`: tests purge `server` and
    then import a new one, so the old module is freed first and CPython may give the new one
    the same address; an `id()` comparison would then wrongly report "unchanged". A dead
    reference never equals a live module. The reference does not keep the module alive.

    The first call with a `server` present counts as changed; with none present, no sweep.
    A module that cannot be weakly referenced counts as changed every time; never raises.
    """
    global _last_server_ref
    server = sys.modules.get("server")
    if server is None:
        return False
    if _last_server_ref is not None and _last_server_ref() is server:
        return False
    try:
        _last_server_ref = weakref.ref(server)
    except TypeError:
        _last_server_ref = None
    release_retained_server_copies()
    return True


def _reset_for_tests() -> None:
    global _last_server_ref
    _last_server_ref = None
