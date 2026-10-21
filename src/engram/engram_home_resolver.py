"""The ONE ENGRAM_HOME resolution for the runtime layer [part of #3863 step 2].

Ruling: Borges's step-2 rulings on #3863, 2026-10-21.

`engram_home()` is the canonical body, AST-identical to `tools/_owner_read.engram_home()` and pinned by
tests/test_engram_home_parity_3863.py as the "runtime" layer. A RELATIVE ENGRAM_HOME resolves against the
CALLING process's cwd, so two processes of one seat would land in two homes; it is refused (ValueError naming
the value).

The failure rule depends on the layer (the ruling):
  * a PROCESS ENTRY POINT (engram_core at import, engramd_client, engramd_owner, the server marker,
    engram_telemetry_verdicts) calls `engram_home()` and lets the ValueError propagate;
  * a module a HOOK IMPORTS (the log emitter, the telemetry store, the log indexer) must never raise into the
    hook: it calls `engram_home_or_none()`, and on None disables its own writes.

Flat module: siblings import it as `import engram_home_resolver` (the runtime dir is on sys.path in both the
source layout and the deployed plugin layout), so it imports nothing from the package.
"""

import os
import sys
from pathlib import Path

_WARNED = False


def engram_home() -> Path:
    """The ONE ENGRAM_HOME resolution for the runtime layer: $ENGRAM_HOME (default ~/.engram) when set and
    non-empty, expanded and absolute; a RELATIVE value is refused (ValueError naming it). Pinned AST-identical
    to tools/_owner_read.engram_home() by tests/test_engram_home_parity_3863.py (the #3628 pattern)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


def engram_home_or_none():
    """For modules a hook imports: `engram_home()`, or None when the value is refused. NEVER raises. Prints at
    most ONE stderr line per process (the same refusal text as `engram_home()`), then stays silent; the caller
    disables its own writes on None."""
    global _WARNED
    try:
        return engram_home()
    except ValueError as exc:
        if not _WARNED:
            _WARNED = True
            try:
                print("engram_home_resolver: %s; writes disabled in this process" % (exc,), file=sys.stderr)
            except Exception:
                pass
        return None
