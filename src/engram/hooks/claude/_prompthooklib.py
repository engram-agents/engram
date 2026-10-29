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

  - get_comms_surface() (alpha#2019 slice A): reads the single-fact
    ``comms_surface`` config key ("umwelt" | "forum"). This is NEW shared
    logic, not an extraction from a pre-existing per-hook copy -- it lives
    here because it is comms-surface-consuming-hook-shared from day one
    (the forum prompt hook is the first consumer; the baton/inter-agent
    hooks are expected to gain the same conditional in a later slice), and
    because every existing config-resolution helper in this module already
    follows the engram_home-parameter pattern this function reuses.

NOT extracted (deliberately, per this slice's hard scope bounds): HTTP
stacks (ForumClient vs urllib), the forum-configured gate
(_is_forum_configured -- left exactly as-is in each hook), forum's own
_get_forum_url (stack-specific, not unified with
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
import sys
from pathlib import Path
from typing import Optional


def engram_home() -> Path:
    """The ENGRAM home for this layer: $ENGRAM_HOME (default ~/.engram) when set and non-empty,
    expanded and absolute; a RELATIVE value is refused (ValueError naming it). A copy of
    tools/_owner_read.engram_home() (the canonical body), kept
    here because the prompt hooks must keep running when _hooklib cannot be imported;
    tests/test_engram_home_parity_3863.py pins the copies AST-identical (the #3628 pattern)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


def engram_home_or_exit() -> Path:
    """engram_home() for a hook's import-time use: a refused (relative) ENGRAM_HOME is a fail-open
    exit, not a traceback. The hook prints ONE stderr line naming the value, writes nothing and exits 0
    (Claude Code reads any other non-zero exit as a non-blocking error and still shows the trace)."""
    try:
        home = engram_home()
    except ValueError as exc:
        sys.stderr.write("engram hook: %s; doing nothing\n" % (exc,))
        sys.exit(0)
    raw = os.environ.get("ENGRAM_HOME")
    if raw and raw != str(home):
        # `~/x` or `/a/../b`: modules that read the raw env var (the log emitter, the telemetry store)
        # must see the same absolute home, not a literal `~` directory under the cwd.
        os.environ["ENGRAM_HOME"] = str(home)
    return home


_JSON_TYPE_NAMES = (
    (bool, "boolean"), (int, "number"), (float, "number"),
    (str, "string"), (list, "array"), (type(None), "null"),
)


def _json_type_name(value) -> str:
    for py_type, name in _JSON_TYPE_NAMES:
        if isinstance(value, py_type):
            return name
    return type(value).__name__


def hooklib_io():
    """The sibling ``_hooklib`` module, for its non-blocking regular-file
    helpers (``open_regular_text``, ``read_regular_text``,
    ``open_regular_text_write``; #3628).

    The prompt hooks must keep running when ``_hooklib`` cannot be imported
    (they report the config as unknown and exit 0; see the module docstring and
    ``report_config_unknown``), so they cannot import its helpers at module
    scope. A call site reaches them as ``hooklib_io().read_regular_text(p)``:
    when the import fails this raises ``OSError`` -- the exception every one of
    those sites already treats as "unreadable / unwritable" -- instead of an
    ImportError no handler there catches. The same OSError is raised for a
    ``_hooklib`` that imports but predates the helpers (a stale copy). No
    helper is copied here.
    """
    # _hooklib.py sits beside this file in both build topologies, so make sure
    # that directory is importable (as _stophooklib does): a loader such as
    # spec_from_file_location does not add it, and the hooks' own bootstrap
    # only runs for the hooks, not for a test that loads this module alone.
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    if _this_dir not in sys.path:
        sys.path.insert(0, _this_dir)
    try:
        import _hooklib
    except ImportError as e:
        raise OSError(f"_hooklib unavailable: {e}") from e
    for name in ("open_regular_text", "read_regular_text", "open_regular_text_write"):
        if not hasattr(_hooklib, name):
            raise OSError(f"_hooklib lacks {name}")
    return _hooklib


def load_config_checked(
    engram_home: Optional[str] = None,
) -> "tuple[Optional[dict], Optional[str]]":
    """Load <engram_home>/config.json, able to say "unknown". Never raises.

    Returns ``(config, reason)``:
      - file ABSENT                         -> ``({}, None)``  (not configured)
      - valid JSON, top level an object     -> ``(dict, None)``
      - present but unreadable (OSError)    -> ``(None, "unreadable: <Class>")``
      - present, not valid JSON             -> ``(None, "unparsable: <Class>")``
      - valid JSON, top level not an object -> ``(None, "not-an-object: <type>")``
        where <type> is a JSON type name (array, string, number, boolean, null)

    ``config is None`` means UNKNOWN: a file is there but could not be used,
    which is not the same as "no config". The reason follows the probe
    convention in _hooklib (``<class>: <message>``, see split_probe_reason).
    engram_home defaults to $ENGRAM_HOME or ~/.engram, the same formula as
    load_config.
    """
    try:
        if engram_home is None:
            engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        config_path = Path(engram_home) / "config.json"
        try:
            # #3628: non-blocking, regular-file-only (see hooklib_io). Raw
            # bytes through .buffer, exactly what read_bytes() returned.
            with hooklib_io().open_regular_text(config_path) as f:
                raw = f.buffer.read()
        except (FileNotFoundError, NotADirectoryError):
            return {}, None
        except (OSError, ValueError) as e:
            # ValueError: an embedded NUL in the path.
            return None, f"unreadable: {type(e).__name__}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # JSONDecodeError, UnicodeDecodeError, RecursionError
            return None, f"unparsable: {type(e).__name__}"
        if not isinstance(data, dict):
            return None, f"not-an-object: {_json_type_name(data)}"
        return data, None
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}"


def load_config(engram_home: Optional[str] = None) -> dict:
    """Load <engram_home>/config.json. Returns {} on any failure.

    A thin wrapper over load_config_checked that deliberately cannot tell
    "absent" from "unreadable"; a caller that must tell them apart (the
    prompt hooks' main()) uses load_config_checked.

    engram_home defaults to $ENGRAM_HOME or ~/.engram -- the identical
    formula every hook already computes at module scope for its own
    ENGRAM_HOME constant -- so existing call sites that call this with no
    arguments (``_load_config()``) see no behavior change.
    """
    cfg, _reason = load_config_checked(engram_home)
    return cfg or {}


def report_config_unknown(
    hook_name: str,
    key: str,
    engram_home: str,
    reason: str,
    inject,
    skipped: str,
) -> None:
    """The unreadable-config handler shared by the three prompt hooks.

    ALWAYS writes one stderr line. If the shared rate limit says a notice is
    due, calls ``inject(line)`` with ONE bounded injected line and only then
    stamps the window (a stamp is written after the response is out, never
    for a line nobody saw). If _hooklib cannot be imported, the stderr line is
    all there is -- still not silent. Never raises; the caller exits 0.
    """
    try:
        from _hooklib import (
            write_probe_unknown_stderr, probe_unknown_line,
            probe_notice_due, mark_probe_notice,
        )
    except Exception:
        try:
            msg = "".join(" " if ord(c) < 32 or 127 <= ord(c) <= 159 else c
                          for c in str(reason))[:200]
            sys.stderr.write(f"{hook_name}: config unknown: {msg}"[:300] + "\n")
        except Exception:
            pass
        return
    write_probe_unknown_stderr(hook_name, "config", reason)
    try:
        if not probe_notice_due(engram_home, key):
            return
        inject(probe_unknown_line("config.json unreadable", reason, skipped))
        # Stamp strictly AFTER a successful flush: if flush raises, the line
        # was never delivered, so no window starts (the outer except skips it).
        sys.stdout.flush()
        mark_probe_notice(engram_home, key)
    except Exception:
        pass


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
    _v = config.get("agent_name")  # #4137: null / non-string -> ""
    name = _v.strip() if isinstance(_v, str) else ""
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


def get_comms_surface(
    config: Optional[dict] = None,
    engram_home: Optional[str] = None,
) -> str:
    """Resolve this agent's comms surface: "umwelt" or "forum".

    Single-fact SSoT (issue #2019, Lei-approved 2026-08-19): reads
    ``config.json["comms_surface"]``.

    Semantics:
      - value == "umwelt" -> "umwelt".
      - value == "forum" -> "forum".
      - key ABSENT -> "forum". This is a TRUE optional (a legitimate default,
        not a failure-hiding fallback — absence genuinely means forum-canonical): an
        absent key genuinely means "forum-canonical install", not a
        missing-value error -- so this branch is silent, no warning.
      - any OTHER value (unrecognized string, wrong type, etc.) -> "forum",
        with a one-line warning to stderr naming the bad value. Loud, never
        fatal -- a hook must never crash on a bad config.

    A config.json that fails to load at all (missing file, unreadable,
    malformed JSON) is indistinguishable from "comms_surface key absent"
    from this function's perspective, because load_config() already
    swallows those failures into ``{}`` (its own documented contract) --
    so that case also falls into the silent absent-key branch, not the
    loud malformed-value branch. The loud branch is reserved for a
    config.json that DID load, but whose comms_surface value is not one
    of the two recognized strings.

    If config is None, loads it via load_config(engram_home) first --
    matches get_agent_name()'s calling convention above.
    """
    if config is None:
        config = load_config(engram_home)
    if "comms_surface" not in config:
        return "forum"
    value = config.get("comms_surface")
    if value in ("umwelt", "forum"):
        return value
    print(
        f"[_prompthooklib] comms_surface={value!r} is not a recognized "
        f"value (expected 'umwelt' or 'forum') -- defaulting to 'forum'",
        file=sys.stderr,
    )
    return "forum"


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
