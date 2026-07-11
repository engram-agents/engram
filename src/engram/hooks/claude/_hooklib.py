"""Shared helpers for the ENGRAM prompt hooks (gh#1657).

SSoT for the one concern genuinely duplicated, byte-for-byte, across the
baton and inter-agent prompt hooks (and near-identically, parameterized on a
different marker file, in the forum prompt hook): resolving `tools/` across
the two build topologies this repo ships in.

Why this exists (CLAUDE.md "Plugin build restructures code paths"): the
plugin build FLATTENS `hooks/claude/*` -> `hooks/*`, so a hook sees `tools/`
at a DIFFERENT relative depth in source vs. deployed. A fixed
`parents[N]` calibrated against one topology silently overshoots in the
other — this bit the forum hook once already (#1539 fixed baton +
inter-agent; #1558 later had to patch forum to match, exactly the kind of
parity-drift this shared module exists to prevent from recurring a third
time).

`_hooklib.py` deliberately does NOT bundle stdin-payload parsing,
transcript-path resolution, or loop-wake-marker logic (all originally named
in gh#1657) -- an audit before writing this file found none of those
concerns are actually shared across baton/forum/inter-agent today: stdin
parsing is inter-agent-only, transcript-path resolution belongs to a
different hook family entirely (session-start/postcompact/stop), and
loop-wake-marker logic already has its own SSoT module (`tools/loop_prompt.py`,
extracted for the deference-detector/time-bar hook pair, unrelated to these
three). Bundling them here would be solving problems these three hooks
don't have -- exactly the "big bang" gh#1657 explicitly asks to avoid.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def resolve_tools_dir(marker_filename: str) -> Optional[Path]:
    """Resolve `tools/` across both build topologies and add it to sys.path.

    Prefers `$CLAUDE_PLUGIN_ROOT/tools` when set, else walks ALL parent
    directories of this file (never a fixed-depth `parents[N]` -- see module
    docstring) and takes the first `tools/` candidate that actually contains
    `marker_filename`. This file (`_hooklib.py`) lives in the SAME directory
    as the hooks that call it in both topologies (source: `hooks/claude/`;
    deployed: `hooks/`), so walking from `_hooklib.py`'s own location yields
    the identical parent chain a hook would get walking from its own
    location -- no need to pass the caller's `__file__` through.

    Returns the resolved directory (already inserted into sys.path if not
    already present), or None if no candidate contains the marker file --
    the caller's own import should be wrapped in try/except and degrade to a
    silent no-op on failure, per every hook's existing discipline.
    """
    candidates = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        candidates.append(Path(plugin_root) / "tools")
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "tools")

    tools_dir = next(
        (c for c in candidates if (c / marker_filename).exists()), None
    )
    if tools_dir is not None and str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    return tools_dir
