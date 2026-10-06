#!/usr/bin/env python3
"""engram-sign -- print the lineage-transparent signature line (#3450).

Usage:
  engram-sign.py [--data-dir DIR] [--session-id ID] [--plugin-root DIR]

Prints the line to stdout, exit 0. On refusal prints the refusal to stderr,
exit 2, and prints NOTHING on stdout -- so ``$(engram-sign.py)`` yields an
empty signature, never a wrong one. Same function as the ``engram_sign`` MCP
tool (``engram_signature.build_signature``). Read-only; no network.

Session: --session-id, else $CLAUDE_CODE_SESSION_ID, else $CLAUDE_SESSION_ID,
else $ANTHROPIC_SESSION_ID; with none of them it refuses [session_marker] --
it never guesses a session (pass --session-id or run where the harness sets
CLAUDE_CODE_SESSION_ID).

Data dir: --data-dir, else $ENGRAM_HOME, else ~/.engram.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _find_module_dir() -> Path | None:
    """Directory holding engram_signature.py: $CLAUDE_PLUGIN_ROOT first, then
    the nearest parent of this file that has it (never a fixed parents[N]: the
    plugin build flattens the source layout)."""
    roots = []
    env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if env:
        roots.append(Path(env))
    roots.extend(Path(__file__).resolve().parents)
    for r in roots:
        try:
            if (r / "engram_signature.py").is_file():
                return r
        except OSError:
            continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir")
    ap.add_argument("--session-id")
    ap.add_argument("--plugin-root")
    args = ap.parse_args(argv)

    mod_dir = _find_module_dir()
    if mod_dir is None:
        print("engram-sign refused [module]: engram_signature.py not found via "
              "CLAUDE_PLUGIN_ROOT or any parent of this script", file=sys.stderr)
        return 2
    sys.path.insert(0, str(mod_dir))
    import engram_signature

    data_dir = Path(args.data_dir or os.environ.get("ENGRAM_HOME")
                    or os.path.expanduser("~/.engram"))
    res = engram_signature.build_signature(
        data_dir, session_id=args.session_id,
        plugin_root=Path(args.plugin_root) if args.plugin_root else None)
    if not res.ok:
        print(f"engram-sign refused [{res.field}]: {res.reason}", file=sys.stderr)
        return 2
    print(res.line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
