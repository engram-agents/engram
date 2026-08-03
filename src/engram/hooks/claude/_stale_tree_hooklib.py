"""_stale_tree_hooklib.py — shared config-read for the #1203 stale-tree tripwire.

SSoT for the one concern duplicated, byte-for-byte, between
engram-stale-tree-pull-hook.py (Layer 1, SessionStart) and
engram-stale-tree-warn-hook.py (Layer 2, PreToolUse): reading + validating
the `stale_tree_watch` field from ~/.engram/config.json. If that schema
ever changes, this is now the one place to change it -- round-2 fix after
the two-hook build shipped with a copy-pasted `_load_watch_list()` in each.

Schema (single source of truth for both layers):

    "stale_tree_watch": [
      { "path": "~/some-repo", "branch": "main" },
      ...
    ]

`path` may use `~` (expanded here). Absent, empty, or malformed
`stale_tree_watch` -> [] -- both hooks treat an empty return as a silent
no-op (the hooks ship in the plugin for ALL installs, but only act once an
install opts in by populating this field).

Like _hooklib.py / _prompthooklib.py, this module lives alongside the hooks
it serves in BOTH build topologies (source: hooks/claude/; deployed:
hooks/ -- the plugin build flattens hooks/claude/* -> hooks/*, see this
repo's CLAUDE.md "Plugin build restructures code paths"). Each hook must
add its own directory to sys.path before importing this module (same
bootstrap every hook already applies for _hooklib/_prompthooklib) -- this
module cannot do that for itself.
"""

from __future__ import annotations

import json
import os
from typing import Optional


def load_watch_list(engram_home: Optional[str] = None) -> list[dict]:
    """Read + validate stale_tree_watch from <engram_home>/config.json.

    Returns [] on any read/parse/shape problem -- never raises. Each entry
    in the returned list is {"path": <str, ~-expanded>, "branch": <str>};
    entries missing either field, or with the wrong type, are dropped
    individually (one malformed entry does not invalidate the rest of the
    list).

    engram_home defaults to $ENGRAM_HOME or ~/.engram -- the identical
    formula each hook already computes for its own module-level
    ENGRAM_HOME constant. Both hooks pass their own ENGRAM_HOME explicitly
    (read at call time, not import time) so per-hook test monkeypatching of
    `mod.ENGRAM_HOME` keeps working exactly as before this extraction.
    """
    if engram_home is None:
        engram_home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")

    config_path = os.path.join(engram_home, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(config, dict):
        return []
    watch = config.get("stale_tree_watch")
    if not isinstance(watch, list):
        return []

    out: list[dict] = []
    for entry in watch:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        branch = entry.get("branch")
        if not isinstance(path, str) or not path.strip():
            continue
        if not isinstance(branch, str) or not branch.strip():
            continue
        out.append({"path": os.path.expanduser(path.strip()), "branch": branch.strip()})
    return out
