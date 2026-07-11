"""Shared prologue helpers for the ENGRAM UserPromptSubmit prompt hooks.

SSoT (gh#1680 slice 1/3) for the prologue helpers duplicated across the
three UserPromptSubmit hooks:
  - hooks/claude/engram-baton-prompt-hook.py
  - hooks/claude/engram-inter-agent-prompt-hook.py
  - hooks/claude/engram-forum-prompt-hook.py

Scope discipline: this is the SAFE, behavior-preserving foundation for a
later process-merge of the three hooks (#1680) -- it removes the 3-copy
prologue drift WITHOUT merging the three hooks' distinct dispatch loops,
HTTP stacks, or gates. Each hook still runs as its own standalone process,
registered independently in hooks.json, with its own main()/gate logic
untouched. Independently revertible from any later process-merge slice.

Extracted here (each confirmed byte-identical, or PROVEN behaviorally
equivalent, across the hooks that used it prior to extraction):

  - load_config(): read <engram_home>/config.json. Byte-identical across
    all three source hooks (baton, inter-agent, forum) prior to extraction,
    modulo each hook closing over its own module-level ENGRAM_HOME constant
    -- the engram_home parameter here defaults to the identical formula
    ($ENGRAM_HOME env var or ~/.engram) each hook already computed, so
    every pre-extraction call site (``_load_config()``, zero args) is
    unaffected.

  - get_agent_name(): resolve the agent's own name via config ->
    $USER -> $LOGNAME -> pwd, ``agent-`` prefix stripped. Byte-identical
    (modulo the config=None default's engram_home source, same reasoning
    as load_config) in the baton and inter-agent hooks. The forum hook's
    pre-extraction copy differed only cosmetically: a required (not
    Optional[dict]) config parameter that its one call site never omitted,
    a shorter docstring, and an inline ``import pwd`` instead of a
    module-level one -- control flow and return value are identical for
    every real call site in all three hooks (each always passes an
    explicit config dict), so unifying to this Optional-signature version
    changes nothing observable for forum's call site either.

  - emit_context(): print the additionalContext hookSpecificOutput
    envelope to stdout. Byte-identical in the baton and inter-agent hooks.
    The forum hook builds its own response dict inline at a differently
    shaped call site (a single construction at the end of main(), not a
    reusable helper called from multiple early-return points) and is left
    untouched -- extracting it there would be introducing a new use of
    this helper, not removing an existing duplication, which is out of
    this slice's "no behavior change" scope.

  - bootstrap_tools_dir(): the try/except wrapper around
    ``_hooklib.resolve_tools_dir(marker)``. Structurally identical across
    all three hooks, parameterized only by the marker filename each hook
    passes (``forum_api.py`` for baton/inter-agent, ``_status_derive.py``
    for forum).

NOT extracted (deliberately, per this slice's hard scope bounds): HTTP
stacks (ForumClient vs urllib), the multi-agent/forum-configured gates
(_is_multi_agent_mode vs _is_forum_configured -- left exactly as-is in
each hook), forum's own _get_forum_url (stack-specific, not unified with
forum_api.forum_url_from_config), cursor files, the status-publish POST,
and the baton auto-archive pass.

Like _hooklib.py, this module lives alongside the hooks it serves in BOTH
build topologies (source: hooks/claude/; deployed: hooks/ -- the plugin
build flattens hooks/claude/* -> hooks/*, see this repo's CLAUDE.md
"Plugin build restructures code paths"). Each hook must add its own
directory to sys.path before importing this module, exactly as it already
does for _hooklib -- this module cannot do that for itself (a module
can't add its own directory to sys.path before it has been found and
imported in the first place).
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
from typing import Optional


def load_config(engram_home: Optional[str] = None) -> dict:
    """Load <engram_home>/config.json. Returns {} on any failure.

    engram_home defaults to $ENGRAM_HOME or ~/.engram -- the identical
    formula every hook already computes at module scope for its own
    ENGRAM_HOME constant -- so existing call sites that call this with no
    arguments (``_load_config()``) see no behavior change.
    """
    if engram_home is None:
        engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    config_path = Path(engram_home) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_agent_name(
    config: Optional[dict] = None,
    engram_home: Optional[str] = None,
) -> str:
    """Resolve this agent's own name.

    Priority:
      1. config.json["agent_name"] field (explicit; wins if set).
      2. $USER env var, agent- prefix stripped.
      3. $LOGNAME env var, agent- prefix stripped (Claude Code hook context
         populates $LOGNAME but not $USER).
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string (hook will see no matching projects/DMs/threads --
         safe).

    If config is None, loads it via load_config(engram_home) first --
    matches the baton/inter-agent hooks' pre-extraction default. Every
    real call site across the three hooks passes an explicit config dict,
    so this fallback path is not exercised by any of them today; it is
    preserved for contract-completeness (and because forum's own
    pre-extraction copy simply required the argument outright, which this
    broader signature is a strict superset of).
    """
    if config is None:
        config = load_config(engram_home)
    name = config.get("agent_name", "").strip()
    if name:
        return name

    def _strip_agent_prefix(username: str) -> str:
        if username.startswith("agent-"):
            return username[len("agent-"):]
        return username

    for envvar in ("USER", "LOGNAME"):
        username = os.environ.get(envvar, "").strip()
        if username:
            return _strip_agent_prefix(username)

    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        if username:
            return _strip_agent_prefix(username)
    except KeyError:
        pass

    return ""


def emit_context(text: str) -> None:
    """Emit an additionalContext block on stdout (the UserPromptSubmit channel)."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }))


def bootstrap_tools_dir(marker_filename: str) -> Optional[Path]:
    """Best-effort delegate to _hooklib.resolve_tools_dir(marker_filename).

    Wraps the import + call in the same try/except-degrade-to-None
    discipline every hook already applied to this exact call inline: any
    failure (e.g. _hooklib somehow not importable) degrades to None
    rather than raising, matching every hook's "never crash the prompt"
    discipline. Assumes the caller's own directory is already on
    sys.path (every hook adds it before importing this module, and
    _hooklib.py lives in that same directory in both topologies), so this
    delegate needs no walk-parents of its own.
    """
    try:
        from _hooklib import resolve_tools_dir
        return resolve_tools_dir(marker_filename)
    except Exception:
        return None
