"""Shared helpers for the ENGRAM prompt hooks (gh#1657).

SSoT for three concerns genuinely duplicated, byte-for-byte, across hooks:

1. Resolving `tools/` across the two build topologies this repo ships in
   (baton + inter-agent + forum prompt hooks).
2. Scoped detection of THIS agent's own MCP server.py process (session-start
   + surface hooks) -- see resolve_own_server_py_path / is_own_server_py_running
   below. Extracted after PR #1839's colleague review found the
   surface hook had copied session-start hook's already-broken unscoped
   `pgrep -f server.py` verbatim, doubling the blast radius of a bug that
   made the liveness probe permanently report healthy on any multi-agent
   host: the bare "server.py" substring matches every agent's server process
   (and any unrelated *_server.py), not just this agent's own.
3. Atomic JSON state writes for hook state files (gh#2035) -- see
   atomic_write_json below. Hooks run under a 5s harness timeout and are
   killed on overrun, and are also fail-soft by design, so a torn write is
   read as an absent file and the hook silently reseeds rather than erroring.
   See docs/DEVELOPMENT.md's "Writing JSON state in a hook" section for the
   full convention text.

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

import json
import os
import sys
import tempfile
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


def resolve_own_server_py_path(engram_home: str) -> str:
    """Resolve the path to THIS agent's own MCP server.py.

    Mirrors the session-start hook's pre-existing (correct) priority order
    for the primary health check -- extracted here so the secondary
    liveness check (is_own_server_py_running below) can share it instead of
    each hook re-deriving its own candidate list:

      1. Runtime-root server.py: `_hooklib.py` lives in the SAME directory
         as the hooks that call it, in both topologies (source:
         `hooks/claude/`; deployed: `hooks/`) -- but server.py is a SIBLING
         of that hooks directory, one level up, NOT colocated inside it
         (verified empirically against a live plugin install: `server.py`
         sits at `<plugin>/server.py`, while `_hooklib.py` sits at
         `<plugin>/hooks/_hooklib.py`). So the candidate is
         `dirname(dirname(this file))/server.py`, exactly mirroring
         `_resolve_mcp_server_path`'s `hook_dir` -> `runtime_dir` climb.
         This is the only candidate that works in a plugin install, where
         the MCP server runs from the plugin tree, not ENGRAM_HOME.
         (Round-2 review caught this: an earlier version of this function
         used `dirname(this file)` directly -- one level too shallow --
         which silently made is_own_server_py_running() return False even
         when this agent's own server WAS running, reintroducing #1772's
         false-alarm behavior in exactly the hook where this path is live
         on every prompt. Confirmed via the same "instrument the deployed
         artifact" method this project's own CLAUDE.md prescribes for this
         bug class, not just re-reading the source.)
      2. `$ENGRAM_HOME/server.py` (legacy scatter-install pattern, in case
         hooks were copied separately from server.py).
      3. `~/engram-alpha/server.py` (live-source dev fallback).

    Returns an absolute path suitable for a SCOPED `pgrep -f <path>` match.
    Never returns a bare "server.py" -- an unscoped pgrep -f server.py
    matches every process whose command line contains that substring
    anywhere, including every OTHER agent's server.py on a shared
    multi-agent host (and unrelated *_server.py files by substring). See
    is_own_server_py_running's docstring for the regression this fixes.
    """
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    runtime_dir = os.path.dirname(hook_dir)
    colocated = os.path.join(runtime_dir, "server.py")
    if os.path.exists(colocated):
        return colocated
    candidate = os.path.join(engram_home, "server.py")
    if os.path.exists(candidate):
        return candidate
    return os.path.expanduser("~/engram-alpha/server.py")


def is_own_server_py_running(engram_home: str) -> bool:
    """Return True if THIS agent's own MCP server.py process is running.

    Scoped `pgrep -f <resolved-path>` match against
    resolve_own_server_py_path's result -- never a bare "server.py"
    substring. The unscoped form was PR #1839's colleague-review blocker:
    on a shared multi-agent host, `pgrep -f server.py` matches
    every agent's own server.py (and any unrelated *_server.py, e.g. a
    viz_server.py, by substring), so the probe was permanently True
    regardless of whether THIS agent's own server was actually alive --
    turning a liveness check that should fail closed into one that can
    never report unhealthy.

    Best-effort: returns False on any error (including a failed import of
    this module by the caller) so callers degrade to the conservative
    (unhealthy) path rather than a false positive.
    """
    import subprocess

    try:
        server_path = resolve_own_server_py_path(engram_home)
        # #2029 inventory: could this match the invoking process? No -- the
        # invoking process is a hook script, never a "server.py" path in its
        # own argv. Could it match a co-tenant's process? server_path is an
        # absolute path under THIS agent's own resolve_own_server_py_path
        # result (per-home), so a co-tenant's equivalent process carries a
        # different absolute path string, and pgrep -f's match is an
        # unescaped-ERE substring search -- a co-tenant path that happened
        # to differ from this one only at an ERE metachar position could in
        # theory match. In practice, distinct agent-home paths (different
        # usernames/directories) make that collision implausible, not
        # impossible.
        result = subprocess.run(
            ["pgrep", "-f", server_path], capture_output=True, timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False


def atomic_write_json(path, data, *, indent=None, encoding=None):
    """Write JSON atomically: build in a temp file in the SAME directory,
    then os.replace() over the destination.

    **Writing JSON state in a hook.** Hook state files are written
    atomically: build the content in a temp file in the same directory, then
    os.replace() it over the destination. Use this helper; do not hand-roll
    it.

    Why: hooks in `src/engram/hooks/claude/` run under a 5s harness timeout
    and are killed on overrun, so a plain truncate-and-write can leave a
    partial file. Hooks are also fail-soft by design, so the next run
    treats a corrupt file as an ABSENT one and silently reseeds -- the hook
    keeps running and quietly stops working. There is no error to notice.
    Atomicity is what makes that failure impossible rather than merely
    unlikely.

    Same-directory temp matters: os.replace() is only atomic within one
    filesystem.

    Mode note: because os.replace() swaps in the temp file's inode, this
    also changes the destination file's mode to 0600 (mkstemp's default)
    regardless of what mode it had before -- for every call site, not only
    the two excluded below. This is why those two exclusions exist, not a
    coincidental detail about one of them.

    Fails loud: exceptions propagate to the caller. Each hook already has
    its own `except Exception: pass` discipline and its own view of what a
    failed write means -- a helper that swallowed silently would make a
    FAILED write indistinguishable from a successful one, which is the
    exact class of silence this whole convention exists to prevent.

    On any exception during the write, the temp file is cleaned up before
    re-raising; the destination is never touched until the temp write has
    fully succeeded.

    `indent` and `encoding` are passed straight through to `json.dump` /
    the temp file's `open` mode respectively -- extend the signature to fit
    a call site's real need (e.g. an existing `encoding="utf-8"` write)
    rather than working around a mismatch with a post-write patch.

    NOT for every write in this codebase. Two documented exceptions exist
    and must NOT be converted to this helper (see docs/DEVELOPMENT.md /
    the module docstrings at their call sites for why):
      - src/engram/viz_server.py's config.json write (os.replace() there
        would strip the agent's ownership of its own config -- #372).
      - forum/coordination/store_file.py (needs 0644, not mkstemp's 0600,
        because the store is read by other agents).
    """
    d = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
