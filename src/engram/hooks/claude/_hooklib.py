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

_hooklib is self-contained (stdlib-only, importable alone): tests copy it by
itself into synthetic hooks/ layouts, and every hook imports it, so a missed
import here would fail every hook at once. Do not import ENGRAM core modules
from it. The non-blocking regular-file helpers are deliberately duplicated in
engram_regular_io.py (core) and pinned identical by
tests/test_regular_io_parity_3628.py.
"""

from __future__ import annotations

import functools
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional


def engram_home() -> Path:
    """The ENGRAM home for this layer: $ENGRAM_HOME (default ~/.engram) when set and non-empty,
    expanded and absolute; a RELATIVE value is refused (ValueError naming it). A copy of
    tools/_owner_read.engram_home() (the canonical body), kept
    here because this layer must stay self-contained; tests/test_engram_home_parity_3863.py pins
    the two AST-identical (the #3628 pattern)."""
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


def resolve_runtime_dir(engram_home: str, sentinel: str) -> str:
    """Locate the directory containing `sentinel` for import.

    Canonical resolver for the "where do the runtime modules live" question,
    hoisted here after gh#2570 found it independently re-implemented three
    times (engram-utility-credit-mention-stop.py, engram-session-start-hook.py,
    engram-surface-hook.py) with two of the three copies missing fixes #1712
    and #1727 that had only ever landed in the surface-hook copy. This is
    that copy, verbatim, with its one hard-coded filename ("engram_client.py")
    generalized to the `sentinel` parameter -- each caller passes the name of
    the module IT actually imports (e.g. "engram_client.py", "engram_ids.py",
    "engram_log_emitter.py"). One call per module you actually import; do not
    collapse two different imports onto one sentinel just because they
    happen to live in the same directory today (gh#2570 -- an import of the
    emitter must probe for the emitter, not for whatever else is nearby).

    Priority:
      1. $ENGRAM_RUNTIME_DIR if set explicitly.
      2. Plugin root: this file lives at <plugin_root>/hooks/_hooklib.py in
         both topologies (source: hooks/claude/; deployed: hooks/ -- flat
         layout, tools/build-plugin.sh copies hooks without a platform
         subdir), so the plugin root is two dirname() levels up from
         __file__. `sentinel` lives at <plugin_root>/. The plugin bundle is
         the canonical runtime; when present it MUST win so a stale
         data-dir snapshot can never shadow it (fixes #1152: scatter cleanup
         can leave ~/.engram/engram_client.py that does `import server`
         against a removed ~/.engram/server.py, crash-looping the daemon).
      3. $ENGRAM_HOME if it bundles a snapshot (scatter-install fallback only
         -- reached only when there is no plugin bundle; covers scatter
         installs that copy `sentinel` into the data dir).
      4. Walk parents from __file__ for the nearest ancestor whose own
         src/engram/ actually contains `sentinel`, instead of guessing a
         fixed absolute path (#1712: the fixed guess resolves to the WRONG
         checkout when this process is running from a worktree nested
         under the guessed path, e.g. .claude/worktrees/<id>/ under
         ~/engram-alpha -- silently shadowing the actually-running copy with
         a sibling one).
      5. ~/engram-alpha (live-source fallback for dev installs, last-ditch,
         unchanged).

    This file lives in the SAME directory as the hooks that call it, in both
    topologies (mirrors resolve_tools_dir's own reasoning above) -- so
    walking from _hooklib.py's own __file__ yields the identical parent
    chain a hook would get walking from its own location.

    Never raises (reviewer-fairy blocker, PR #1727, originally found in the
    surface-hook copy): this function is called at bare module-import time
    by every caller (`_RUNTIME_DIR = resolve_runtime_dir(...)`, unguarded),
    so the walk itself must never raise. Path.exists() -- unlike
    os.path.exists() used at every other rung above -- raises
    PermissionError on an unreadable ancestor instead of returning False; an
    unhandled exception here would crash the whole hook process at import
    time. Same precedent as resolve_tools_dir's own docstring ("the
    caller's own import should be wrapped ... and degrade to a silent
    no-op on failure") -- wrapped here instead of at the call site so this
    function keeps a never-raises contract for every caller.
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(plugin_root, sentinel)):
        return plugin_root
    if os.path.exists(os.path.join(engram_home, sentinel)):
        return engram_home
    try:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "src" / "engram"
            if (candidate / sentinel).exists():
                return str(candidate)
    except OSError:
        pass
    return os.path.expanduser("~/engram-alpha")  # last-ditch, unchanged


def import_owner_flags(engram_home: str):
    """Lazy import of the plugin-root module engramd_owner_flags (#3663), resolved
    like _import_engramd_client: the shared resolver, never a fixed parents[N]. An
    explicit ENGRAM_RUNTIME_DIR that does not hold the module (it exists to redirect
    the CLIENT import) is retried once without the override, so redirecting the
    client can never make a flag that is ON read as OFF.

    Hoisted once from the byte-similar copies in engram-stop-hook.py and
    engram-user-identity-hook.py (#3665); each hook keeps a thin `_import_owner_flags`
    alias that passes its own ENGRAM_HOME.

    Each candidate dir (the explicit one first, then the resolved one) is tried in turn
    and any `Exception` from a candidate is remembered, not raised, so a bad copy in one
    candidate does not hide a good copy in the next. After the loop: if nothing failed,
    or the last failure is a plain "module not found" for this module, raises
    ImportError("... not found ..."); otherwise re-raises the LAST failure (its own
    class, so the visible report names it). KNOWN LIMITS: (a) a SystemExit from an
    explicit-dir copy is not an Exception; it propagates at once and a good copy in
    the resolved dir is not tried (the caller still reads OFF -- its catch takes
    SystemExit -- and reports the class); (b) a resolved dir already on sys.path
    behind a bad explicit copy is not retried.

    The ENGRAM_RUNTIME_DIR pop/restore and the sys.path insert are process-global
    side effects and stay inside this one function: after a call, success or raise,
    ENGRAM_RUNTIME_DIR is exactly what it was before."""
    explicit = os.environ.pop("ENGRAM_RUNTIME_DIR", None)
    try:
        resolved = resolve_runtime_dir(engram_home, "engramd_owner_flags.py")
    finally:
        if explicit is not None:
            os.environ["ENGRAM_RUNTIME_DIR"] = explicit
    last = None
    for runtime_dir in ([explicit] if explicit else []) + [resolved]:
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        try:
            import engramd_owner_flags
            return engramd_owner_flags
        except Exception as exc:  # a bad copy in one candidate must not hide a good one in the next
            last = exc
    if last is None or (isinstance(last, ModuleNotFoundError) and last.name == "engramd_owner_flags"):
        raise ImportError("engramd_owner_flags.py not found under the runtime dir")
    raise last  # the class of the LAST failure, so the visible report names it


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
      - src/engram/viz/data/config.py's write_config_key() (os.replace()
        there would strip the agent's ownership of its own config -- #372).
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


# ---------------------------------------------------------------------------
# emit_context() -- bounded hook-injection helper (gh#2863 WS1)
# ---------------------------------------------------------------------------
# Generalizes the mechanism proven in the merged engram-lesson-tripwire-hook.py
# (_build_trailer / _render_tripwire_block / select_and_cap_hits /
# write_tripwire_overflow / _placeholder_overflow_path) for ANY hook, not
# just the tripwire. Design: Borges's comment on the issue
# (2026-09-12T14:02:48Z) + Kepler's two amendments (2026-09-12T14:31:26Z) --
# read both before changing this section; it is a transcription of that
# already-approved design, not a re-derivation of it. Companion module:
# _harness.py (get_harness_budgets()) supplies the per-harness `budget` /
# `head_budget` numbers a caller passes in here as `budget` / uses to pick
# its own `target`.

_ENGRAM_HOME_FOR_OVERFLOW_PLACEHOLDER = str(engram_home_or_exit())


def _placeholder_overflow_path() -> str:
    """A representative-length dummy overflow path, used ONLY during
    emit_context()'s internal sizing loop below -- never written to disk,
    never returned to any caller as a real path.

    Mirrors the chicken-and-egg the merged tripwire hook already solves
    (its own _placeholder_overflow_path): the trailer's "-- see {path}"
    text depends on the REAL overflow path, which a caller's
    overflow_writer only produces once the final cut/truncated item list is
    known -- but that split is exactly what the sizing loop is computing.
    Sized against a representative dummy path instead of invoking
    overflow_writer speculatively during the loop; a few characters'
    difference between this placeholder and the eventual real path is
    immaterial at typical target/budget sizes (hundreds to low thousands of
    characters) -- not worth the complexity of an exact reservation (same
    reasoning as the tripwire hook's own copy).

    Deliberately generic ("hook-overflow", not any one hook's own directory
    name): unlike the tripwire hook, which owns its own overflow_writer and
    therefore knows its own real directory name, this helper's
    overflow_writer is caller-supplied and arbitrary -- emit_context() has
    no way to know what directory (or naming scheme) any given caller's
    writer will actually use. The placeholder's job is only to be a
    plausible LENGTH, not a plausible real path.
    """
    return str(
        Path(_ENGRAM_HOME_FOR_OVERFLOW_PLACEHOLDER)
        / "hook-overflow"
        / f"9999999999-{os.getpid()}.txt"
    )


def _build_context_trailer(
    shown_count: int, cut_count: int, overflow_path: str = "", head_cut: bool = False
) -> str:
    """Build the "N shown . M cut[ . head trimmed][ -- see path]" trailer
    line for emit_context().

    cut_count == 0, head_cut False -> "▸ {shown} shown · 0 cut" (no clause).
    cut_count == 0, head_cut True  -> "▸ {shown} shown · 0 cut · head trimmed".
    cut_count > 0, path given, head_cut False ->
        "▸ {shown} shown · {cut} cut — see {path}".
    cut_count > 0, path given, head_cut True ->
        "▸ {shown} shown · {cut} cut · head trimmed — see {path}".
    cut_count > 0, no path, head_cut False -> "▸ {shown} shown · {cut} cut"
        (no clause) -- either overflow_writer was None (the caller's own
        explicit "no overflow file" choice) or a provided writer failed at
        runtime; both read identically here, per the WS1 spec's own Part B
        item 7 (only the REASON the path is empty differs between those two
        cases, never the rendered text).
    cut_count > 0, no path, head_cut True ->
        "▸ {shown} shown · {cut} cut · head trimmed" (no "— see" clause,
        same reasoning as above).

    `head_cut` (engram-alpha#2892): True when emit_context()'s own Stage 1
    head-trim actually cut something -- including the all-or-nothing
    full-drop-to-"" case (`_trim_to_line_boundary` returns "" when no
    newline fits within the room available). Before this parameter
    existed, a dropped head was entirely invisible here: `cut_count` only
    ever counted body-item selection, so a caller could see "0 cut" even
    though the head vanished whole. The `· head trimmed` clause is a
    SEPARATE signal from the shown/cut counts -- it never folds into or
    replaces them, because a reader needs to be able to tell "supplementary
    body detail was cut" from "the tl;dr itself is missing" as two
    independent facts, not one conflated number.

    DECIDED, not incidental (Borges's second review catch, 2026-09-13): a
    trimmed/dropped head's own text is NEVER written to the overflow file,
    and `head_cut` NEVER carries a path of its own -- #2892 is a
    VISIBILITY fix (the trailer must stop lying by omission), not a new
    head-content-recovery mechanism. Folding a head-cut into either the
    numeric `M` count or a dynamic `— see <path>` clause would make the
    trailer's true worst-case length depend on a value (a real path's
    length, or a carried digit) that Stage 1's own worst-case sizing cannot
    know in advance -- exactly the class of bug this whole file exists to
    close, reintroduced at the accounting layer instead of the render
    layer. Because `head_cut`'s own clause text is fixed and small, Stage 1
    can and does reserve room for it unconditionally (see emit_context()'s
    own `worst_trailer` computation), which is only possible because this
    clause never varies in length and never depends on anything computed
    later.

    One trailer-format function shared by BOTH the internal sizing loop and
    the final emission in emit_context() below -- one render path, not two
    copies that could drift apart in format (mirrors
    engram-lesson-tripwire-hook.py's own _build_trailer /
    _render_tripwire_block split).
    """
    clause = f"▸ {shown_count} shown · {cut_count} cut"
    if head_cut:
        clause += " · head trimmed"
    if cut_count > 0 and overflow_path:
        clause += f" — see {overflow_path}"
    return clause


def _trim_to_line_boundary(text: str, limit: int) -> str:
    """Return the longest prefix of `text` that is <= `limit` chars AND
    consists only of COMPLETE lines -- the returned string never ends
    mid-line/mid-word.

    If `text` already fits within `limit`, it is returned unchanged
    (nothing is cut, so nothing can straddle a line -- this is not a "must
    end with a trailing newline" requirement, only a "never truncate inside
    a line" one). Otherwise this cuts at the LAST '\\n' at or before
    `limit`, dropping the partial trailing line entirely rather than
    emitting it truncated mid-word. If no '\\n' exists within `limit` at
    all (even the first line doesn't fit), returns "" -- an empty string
    trivially satisfies "ends on a line boundary" and is the safe direction
    when nothing more specific fits.

    This is the mechanism behind Part B item 6's head/line-boundary
    contract (Borges's own words, verbatim, because paraphrasing risks
    losing the precision): "the cut is min(2,000 chars, last complete LINE
    under ~2,048 bytes), so a line that starts before the cap and ends
    after it is dropped whole. So the head must be composed of SHORT lines
    and must itself end on a line boundary -- being under budget is not
    sufficient."
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_nl = truncated.rfind("\n")
    if last_nl == -1:
        return ""
    return truncated[:last_nl]


def emit_context(
    head: str,
    body: Iterable[str],
    *,
    budget: int,
    target: int,
    allow_single_overflow: bool = False,
    overflow_writer: "Callable[[list], str] | None" = None,
    kept_out: "list | None" = None,
) -> str:
    """Render a bounded additionalContext block: trailer FIRST (survives the
    harness's tail truncation), then `head`, then as many `body` items as
    fit -- generalizing the mechanism proven in the merged
    engram-lesson-tripwire-hook.py (`select_and_cap_hits` /
    `_render_tripwire_block` / `_build_trailer` / `write_tripwire_overflow`)
    for any hook, not just the tripwire (gh#2863 WS1).

    Parameters
    ----------
    head:
        Caller-supplied tl;dr prose (already the caller's own summary --
        this helper does not compose it). May be multi-line. Rendered right
        after the trailer, before any `body` items.
    body:
        An ORDERED iterable of already-most-important-first item strings
        (the caller does the ordering; this helper only decides how many
        fit). Materialized to a list on entry (may be a generator).
    budget:
        The HARD, harness-wide ceiling in characters (from
        `_harness.get_harness_budgets()["budget"]`). Never exceeded, no
        exception, full stop (Kepler's Amendment 1).
    target:
        The SOFT, per-hook ceiling in characters (analogous to the
        tripwire's own `TRIPWIRE_BLOCK_CHAR_CEILING`), always `<= budget`.
        `allow_single_overflow` is scoped to this number only -- it never
        permits exceeding `budget`.
    allow_single_overflow:
        When the render of the single most-important `body` item (plus
        trailer and head) already exceeds `target`, this flag decides what
        "exceeds" means: False (default) truncates that item's own text
        in-band so the render fits within `target`; True instead lets that
        single item's render run past `target` (never past `budget` --
        see below).
    overflow_writer:
        Optional `Callable[[list], str]`, called with the list of
        cut/truncated item strings and expected to return a path string
        where they were persisted (mirrors
        `write_tripwire_overflow()`'s own never-raises,
        returns-""-on-failure contract exactly -- also wrapped defensively
        here in case a caller's own writer doesn't honor it). `None` (the
        default) means the caller has nowhere to persist overflow -- a
        valid, distinct choice from a writer that itself fails at runtime;
        both end up with an empty path, and the trailer reads identically
        either way (see `_build_context_trailer`'s docstring).
    kept_out:
        Optional OUT-parameter (#3621). When a `list` is passed it is
        EXTENDED with the 0-based indices (into `body` as the caller passed
        it) of the body items rendered IN FULL in the returned string. A body
        item that was cut, or truncated in-band (the `truncated_first`
        shape), is NOT listed; neither is any item if the final `budget`
        backstop had to shorten the render. The returned string is
        byte-identical with and without it. Anything that is not a `list`
        (including the default `None`) is ignored.

    Contract
    --------
    1. Renders through the SAME code path it measures during sizing -- the
       nested `_render()` closure below is the one and only rendering
       function for the "N full body items" shape; the sizing loop and the
       final (non-`truncated_first`) emission both call it. No separate
       sizing-estimate formula and final-render formula to drift apart
       (mirrors `select_and_cap_hits`'s own docstring on this exact
       point). The `truncated_first` shape (a single truncated item plus
       an in-band marker) is a genuinely different shape from `_render()`'s
       own "N full items" output, so it has its own small render function,
       `_render_truncated_first()` below -- but that function shares
       `_render()`'s own `head_text`/`head_cut` accounting (computed once,
       in Stage 1, closed over by both) rather than recomputing it, so head
       handling itself never has a second copy.
    2. Trailer LEADS, always emitted, `0 cut` included:
       `▸ N shown · M cut[ · head trimmed][ — see <path>]` -- the
       `— see <path>` clause only when `M > 0` AND a real path exists, and
       the `· head trimmed` clause only when Stage 1's head-trim actually
       cut something, including the all-or-nothing full-drop case
       (engram-alpha#2892; see `_build_context_trailer`). Always the
       literal first line of the returned string, so it survives the
       harness's TAIL truncation even when everything after it doesn't.
    3. Never exceeds `budget`, full stop, no exception. `allow_single_overflow`
       only ever permits exceeding `target`; if the single most-important
       `body` item, even after that allowance, would still exceed `budget`,
       it is truncated in-band with an explicit marker
       (`…[truncated, full text in <path>]`) rather than dropped or kept
       whole (Borges's decision, confirmed by Kepler's Amendment 1).
    4. Head/line-boundary rule (Part B item 6): the leading trailer+head
       portion is arranged so that, if it must be shortened at all to fit,
       the cut lands on a `\\n` boundary -- never mid-line, never mid-word
       (see `_trim_to_line_boundary`). This is the helper's job, not the
       caller's: a caller may pass an arbitrarily long or raggedly-lined
       `head` and still get a render that never straddles a line. This
       trim happens exactly ONCE per call, sized against a WORST-CASE
       trailer (as if every body item were cut) -- deliberately
       cut-count-independent, so head-trim severity never varies across
       Stage 2's sizing-loop candidates. When the head must be cut at all
       -- including the all-or-nothing full-drop-to-"" case -- the trailer
       signals this explicitly (the `· head trimmed` clause above) rather
       than the drop being silent (engram-alpha#2892).
    5. The `target` ceiling is re-validated against the REAL `overflow_path`
       once Stage 3 resolves it, not just the placeholder path used during
       Stage 2's sizing loop (engram-alpha#2893): if the real path's length
       pushes the render over `target`, `shown_n` is shrunk (never below 1)
       and re-rendered against the same real path until it fits, falling
       through to the same `truncated_first` construction as a last resort
       if even a single item's render doesn't fit `target` against the real
       path (subject to the same `allow_single_overflow`/`budget` exception
       as the placeholder-sized determination below).
       Within that `truncated_first` shape the in-band marker is part of the
       render too (engram-alpha#2899): Stage 1 sizes the head without it, so
       when trailer + head + marker would exceed the ceiling,
       `_render_truncated_first()` re-trims a local copy of the head on a
       line boundary (trailer says `head trimmed`) until it fits. If trailer
       + marker alone exceed the ceiling the head is dropped entirely and the
       overshoot of `target` is unavoidable -- it is never an overshoot of
       `budget`.

    6. `kept_out` (#3621) reports what the model can actually see: the indices
       of the items present whole in the returned string. It errs toward
       UNDER-reporting (a caller that records "shown" ids from it would rather
       repeat a display than record an id the model never saw). It never
       changes the returned string and never makes this function raise.

    Never raises on well-typed inputs. A caller-supplied `overflow_writer`
    that raises is caught and treated as a failed write (empty path),
    matching `write_tripwire_overflow()`'s own posture.
    """
    body_list = list(body)
    n = len(body_list)
    head_text = head or ""

    use_writer = overflow_writer is not None
    placeholder_path = _placeholder_overflow_path() if use_writer else ""

    # --- Stage 1: head's own line-boundary rule, independent of body-item
    # selection. Sized against the WORST-CASE trailer (as if every body
    # item were cut) so head is safe under `target` no matter how Stage 2
    # below actually resolves shown_n -- a real trailer for a smaller
    # cut_count is the same length or shorter in every case that matters
    # here (dropping the "— see <path>" clause entirely once cut_count
    # reaches 0), so sizing against the worst case is the conservative,
    # safe direction, not an approximation that could go the wrong way.
    # This trim happens exactly ONCE, here, for the whole call --
    # `head_text` and `head_cut` below are closed over by `_render()` and
    # `_render_truncated_first()`, never recomputed per rendering shape.
    # Deliberately simpler than sizing per-call against each shape's own
    # actual trailer: doing so would make head-trim severity a function of
    # `cut_count` (a shorter trailer at a later, less-cut Stage-2 candidate
    # means more head room, i.e. potentially LESS head trimming at higher
    # shown_n) -- a real behavior change from today's conservative,
    # cut-count-independent sizing, not a pure refactor (Borges's review
    # catch, 2026-09-13, on an earlier draft of this fix).
    #
    # `head_cut=True` is passed UNCONDITIONALLY here, even though the real
    # head_cut outcome isn't known yet -- the worst-case trailer must
    # reserve room for the `· head trimmed` clause's own (fixed, small,
    # never-a-path) length too, exactly the same way it already reserves
    # room for the worst-case `M`-digit count before the real `M` is known.
    # Without this, a head-cut clause added ON TOP of a worst-case that
    # never budgeted for it could push the assembled render past `target`
    # by the clause's own width -- a second review catch (Borges,
    # 2026-09-13) on the same draft: the head-cut signal must be a FIXED,
    # small trailer clause that never carries a path and never folds into
    # the numeric `M cut` count (a dropped/trimmed head's own text is NOT
    # written to the overflow file -- #2892 is a visibility fix, not a new
    # head-recovery mechanism), so its worst-case length is always exactly
    # this same fixed clause, reservable unconditionally.
    worst_trailer = (
        _build_context_trailer(0, n, placeholder_path, head_cut=True)
        if n > 0
        else _build_context_trailer(0, 0, head_cut=True)
    )
    sep = 1 if head_text else 0
    head_room = target - len(worst_trailer) - sep
    if head_room < 0:
        head_room = 0
    # head_cut (engram-alpha#2892): True iff this trim actually cuts
    # anything, including the all-or-nothing full-drop-to-"" case --
    # `_trim_to_line_boundary` is only ever invoked when this condition
    # holds, so it IS the definition of "did Stage 1 cut the head" (no
    # separate before/after string comparison needed).
    head_cut = len(head_text) > head_room
    if head_cut:
        head_text = _trim_to_line_boundary(head_text, head_room)

    def _render(shown_n: int, cut_n: int, overflow_path: str) -> str:
        trailer = _build_context_trailer(shown_n, cut_n, overflow_path, head_cut)
        parts = [trailer]
        if head_text:
            parts.append(head_text)
        parts.extend(body_list[:shown_n])
        return "\n".join(parts)

    def _render_truncated_first(cut_n_for_marker: int, overflow_path_: str) -> str:
        """The genuinely-different "single truncated item + in-band
        marker" shape -- shares `head_text`/`head_cut` (Stage 1, above)
        with `_render()` rather than recomputing them, so there is exactly
        one head-accounting computation per call to `emit_context()`, never
        one per rendering shape. Called from BOTH the original
        (placeholder-sized) `truncated_first` determination below AND the
        real-path degrade path in Stage 3 (engram-alpha#2893) -- one
        construction, two call sites, no drift between them."""
        ceiling = budget if allow_single_overflow else target
        marker = (
            f"…[truncated, full text in {overflow_path_}]"
            if overflow_path_
            else "…[truncated]"
        )
        local_head = head_text
        local_head_cut = head_cut
        trailer = _build_context_trailer(1, cut_n_for_marker, overflow_path_, local_head_cut)
        overhead = len(trailer) + (len(local_head) + 1 if local_head else 0) + 1 + len(marker)
        if overhead > ceiling and local_head:
            # engram-alpha#2899: Stage 1 sized the head against the trailer
            # only and never reserved this marker, so trailer + head +
            # marker alone can exceed the ceiling -- and the item_room
            # clamp below would then hide the shortfall. Re-trim a LOCAL
            # copy of the head (head_text/head_cut stay untouched: the
            # Stage-3 degrade call site calls this function again) on a
            # line boundary so trailer + head + marker fits. The trailer is
            # rebuilt with head_cut=True so the cut is never silent; that
            # clause is already reserved by Stage 1's worst_trailer.
            # Layout: trailer \n head \n item+marker, hence the two joins.
            local_head_cut = True
            trailer = _build_context_trailer(
                1, cut_n_for_marker, overflow_path_, local_head_cut
            )
            head_room_local = ceiling - len(trailer) - len(marker) - 2
            local_head = _trim_to_line_boundary(local_head, head_room_local)
            overhead = (
                len(trailer) + (len(local_head) + 1 if local_head else 0) + 1 + len(marker)
            )
        # Unavoidable overshoot (documented in emit_context()'s contract
        # item 5): if trailer + marker alone exceed the ceiling, the head is
        # already empty here and the render stays over `target` (never over
        # `budget` -- the final backstop clamps).
        item_room = max(ceiling - overhead, 0)
        item_text = body_list[0][:item_room]
        parts = [trailer]
        if local_head:
            parts.append(local_head)
        parts.append((item_text + marker) if item_text else marker)
        return "\n".join(parts)

    # --- Stage 2: greedy body-item selection under `target`, generalizing
    # engram-lesson-tripwire-hook.py's select_and_cap_hits -- drop from the
    # tail (least-important-currently-shown item) until the fully rendered
    # block fits, always keeping at least the first item when n >= 1 (an
    # empty visible block would defeat the point of emitting at all; a
    # single, possibly-truncated item is still strictly better than none).
    shown_n = n
    rendered = _render(0, 0, "") if n == 0 else None
    if n > 0:
        for candidate in range(n, 0, -1):
            cut_n = n - candidate
            path_for_sizing = placeholder_path if cut_n > 0 else ""
            candidate_rendered = _render(candidate, cut_n, path_for_sizing)
            if len(candidate_rendered) <= target or candidate == 1:
                shown_n = candidate
                rendered = candidate_rendered
                break

    cut_n = n - shown_n
    truncated_first = False
    if n > 0 and len(rendered) > target:
        # Only reachable at shown_n == 1 -- the loop above stops shrinking
        # once candidate == 1 regardless of fit, so this is the "even the
        # single most-important item alone doesn't fit `target`" case.
        if allow_single_overflow and len(rendered) <= budget:
            pass  # allowed past `target`, still within the hard `budget`
        else:
            truncated_first = True

    # --- Stage 3: resolve the REAL overflow path. The writer sees every
    # fully-cut item, plus -- when the first item itself is being
    # truncated -- that item's own FULL untruncated text, so the marker's
    # "<path>" and the trailer's "-- see <path>" clause point at the SAME
    # file (Kepler's "one file per emission").
    cut_items = body_list[shown_n:] if n > 0 else []
    full_first_text = body_list[0] if truncated_first else None
    writer_items = ([full_first_text] if full_first_text is not None else []) + cut_items

    def _write_overflow(items: list) -> str:
        if use_writer and items:
            try:
                return overflow_writer(items) or ""
            except Exception:
                return ""
        return ""

    overflow_path = _write_overflow(writer_items)

    if truncated_first:
        rendered = _render_truncated_first(cut_n, overflow_path)
    else:
        # Contract 1: the final (non-truncated_first) emission calls
        # _render() itself, not a hand-rebuilt copy -- one renderer for
        # this shape, per Contract 1's own sentence (previously this
        # branch hand-rebuilt trailer/parts/join inline, which is the
        # un-filed third defect Borges found reading the code -- the
        # mechanical seam both #2892 and #2893 actually live in).
        rendered = _render(shown_n, cut_n, overflow_path)

        # engram-alpha#2893: the sizing loop above measured candidates
        # against a PLACEHOLDER overflow path; now that the REAL path is
        # known, re-validate against `target` and shrink `shown_n` (never
        # below 1), re-rendering against the SAME real `overflow_path`
        # each time (the path itself doesn't change here -- only which
        # body items are shown/cut does), until it fits.
        #
        # Deliberate bound-hit resolution, stated explicitly rather than
        # left for a reader to infer: if shrinking shown_n all the way
        # down to a single item still doesn't fit `target` against the
        # real path, degrade to the SAME truncated_first path used when
        # Stage 2's own placeholder-sized pass already needed it -- exactly
        # as if Stage 2 had landed here originally, against the real path,
        # rather than a fallback nobody decided on.
        while shown_n > 1 and len(rendered) > target:
            shown_n -= 1
            cut_n = n - shown_n
            # engram-alpha#2892/#2893 follow-up: re-derive the overflow
            # FILE's own content, not just shown_n/cut_n, on every shrink --
            # otherwise the trailer's "-- see <path>" clause keeps pointing
            # at a file written against the PRE-shrink cut set, silently
            # missing the text of whichever item(s) this iteration just
            # moved from "shown" to "cut". Mirrors the floor-degrade branch
            # below, which already re-derives writer_items/overflow_path
            # before its own final render -- this loop was the one place
            # that hadn't been brought in line with that pattern.
            cut_items = body_list[shown_n:]
            overflow_path = _write_overflow(cut_items)
            rendered = _render(shown_n, cut_n, overflow_path)

        if (
            shown_n == 1
            and len(rendered) > target
            and not (allow_single_overflow and len(rendered) <= budget)
        ):
            # Same bound-hit resolution as above, at the floor: even one
            # item no longer fits `target` against the real path (and
            # allow_single_overflow's own budget-scoped exception doesn't
            # rescue it either) -- degrade to truncated_first. This
            # re-derives writer_items/overflow_path against the now-larger
            # cut set (a SECOND overflow_writer call, orphaning the first
            # file on disk) -- an accepted, rare tradeoff (Sol's
            # measurement: 17/3500 fixture cases needed any correction at
            # all, each 2-3 chars over; reaching all the way down to this
            # floor should be rarer still), not something this code tries
            # to avoid (`_placeholder_overflow_path()`'s own docstring:
            # "a few characters' difference... is immaterial").
            truncated_first = True
            full_first_text = body_list[0]
            cut_items = body_list[1:]
            writer_items = [full_first_text] + cut_items
            overflow_path = _write_overflow(writer_items)
            rendered = _render_truncated_first(cut_n, overflow_path)

    # --- Final hard invariant: NEVER exceed `budget`, no exception, ever
    # (Kepler's Amendment 1). Every path above is already designed to stay
    # within `budget`; this is a defensive backstop, not the primary
    # mechanism -- it should not trigger in normal operation.
    backstop_hit = False
    if len(rendered) > budget:
        backstop_hit = True
        safe = _trim_to_line_boundary(rendered, budget)
        rendered = safe if safe else rendered[:budget]

    # #3621: report which body items are present IN FULL. A truncated_first
    # item is not; if the backstop above shortened the render we cannot say
    # what survived, so under-report (nothing kept).
    if isinstance(kept_out, list):
        try:
            if not truncated_first and not backstop_hit:
                kept_out.extend(range(shown_n if n > 0 else 0))
        except Exception:
            pass

    return rendered


# ---------------------------------------------------------------------------
# Route ledger (#3458 section A)
#
# The probe mod (tools/probes/mods-recall) rates what the recall ARMS surface.
# #3458 widens its ratings to the two LIVE recall routes, so each route records
# -- at fire time, because signals such as utility change on use and cannot be
# recovered from the live graph later -- one JSON line per fire:
#
#   <probeDir>/<session_id>/routes/<route>.jsonl
#
# probeDir rule (mirrors the mod's default: config.ts DEFAULTS.probeDir
# '~/.engram/probe/mods-recall', resolved by record.ts defaultProbeDir against
# ENGRAM_HOME when that is set): `<engram_home()>/probe/mods-recall`, via the
# layer's engram_home(). The mod and this hook must name the same directory,
# or the hook records nothing for a probe that is live (alpha #3863, 6b; the
# mod side and the hook side are pinned together by
# tests/test_route_probe_dir_parity_3863.py).
#
# Opt-in by presence: nothing is written unless `<probeDir>/<session_id>/`
# already exists. Absence of that directory means "the probe is not live for
# this session", which is a true optional (not an error): the hook changes
# nothing -- no file, no directory, no log line. Fail-open: any other ledger
# error costs one stderr line `route-ledger: <reason>`; the hook's own stdout
# is never touched. The ledger OBSERVES: it records what the route's own
# response carried and never recomputes or approximates a ranking signal.
# Two fields are not read from the response (#3551): `ranked` is PRODUCER-
# computed (engram_query sets it per row: True = composite-ranked, False =
# special-type bypass row, whose null composite_score is by design and is not
# listed in missing_signals) and `in_context` / `in_context_why` are HOOK-
# computed at write time from three sources (shown earlier this session,
# created since session start, currently focused). The rest are read as-is.
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import time as _time

# Per-candidate signal fields read from the route's own response. A field the
# response does not carry for a candidate is null, and its name is listed once
# in the line's `missing_signals`.
ROUTE_SIGNALS = (
    "type", "similarity", "importance_score", "utility", "ranked", "composite_score",
    "match_type", "recall_turn", "created_at", "in_context",
)
# Response-key aliases: the debug shape of engram_query names utility
# `utility_score`. Same value under another key, not an approximation.
_ROUTE_SIGNAL_ALIASES = {"utility": ("utility", "utility_score")}


def route_probe_dir() -> Path:
    """`<engram_home()>/probe/mods-recall` (see the block comment above)."""
    return engram_home() / "probe" / "mods-recall"


def prompt_fire_key(prompt_text: str) -> str:
    """prompt_surface fire_key: sha256(prompt_text)[:16]."""
    return _hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


def tool_fire_key(tool_name, tool_input) -> str:
    """tool_recall fire_key: `<tool_name>:` + sha256 of the canonical
    (sort_keys) JSON of tool_input, first 16 hex."""
    canon = json.dumps(tool_input, sort_keys=True)
    return f"{tool_name}:{_hashlib.sha256(canon.encode('utf-8')).hexdigest()[:16]}"


def _signal_from(entry: dict, name: str):
    """Nested `entry["signals"]` first (the surface response shape, #3462),
    then the flat key (legacy / debug shapes). No default: absent -> None."""
    nested = entry.get("signals")
    sources = (nested, entry) if isinstance(nested, dict) else (entry,)
    for src in sources:
        for key in _ROUTE_SIGNAL_ALIASES.get(name, (name,)):
            if key in src and src[key] is not None:
                return src[key]
    return None


def _parse_iso_utc(text):
    """ISO timestamp (naive = UTC, trailing Z ok) -> aware datetime, or None."""
    from datetime import datetime, timezone
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _open_regular_fd(path):
    """An O_RDONLY fd for `path`, or None when it cannot be opened or is not a
    regular file. O_NONBLOCK makes open() on a FIFO return at once instead of
    waiting for a writer; the fstat then rejects it before any read. The ledger
    reads run in a hook before its response is printed, and a try/except is
    fail-open for an exception, not for a hang."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _not_regular_error(fd, path):
    """The OSError for a path that opened but is not a regular file. A
    directory keeps the IsADirectoryError a plain open() raises for it; any
    other kind (FIFO, socket, device) is an EINVAL OSError. Does not close fd:
    the caller's except clause does, once."""
    import errno
    try:
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
    except OSError:
        is_dir = False
    code = errno.EISDIR if is_dir else errno.EINVAL
    return OSError(code, os.strerror(code) + " (not a regular file)", str(path))


def open_regular_text(path, encoding=None):
    """`open(path, encoding=encoding)` for a read that must never block (#3591).

    A plain open() of a FIFO waits for a writer; a hook under a 5 s timeout
    hangs and is killed. This opens O_NONBLOCK (returns at once on a FIFO),
    fstat-checks that the target is a regular file, and returns a text file
    object over the fd. Anything else raises OSError (IsADirectoryError for a
    directory, EINVAL otherwise), so a call site that already treats an
    unreadable file as absent -- `except OSError` or broader -- needs only
    `open(p)` -> `open_regular_text(p)`. FileNotFoundError / PermissionError
    come from os.open exactly as from open(). For a regular file the result is
    the same text wrapper open() returns."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "r", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def read_regular_text(path, encoding=None):
    """`Path(path).read_text(encoding=encoding)` that must never block on a
    FIFO (#3591): the whole text of a regular file, OSError otherwise (see
    open_regular_text)."""
    with open_regular_text(path, encoding=encoding) as f:
        return f.read()


def read_state_json_classified(path):
    """Read a JSON-object state file and CLASSIFY the outcome (#3521 row 40).

    Returns `(state, unknown_reason, movable)`; never raises:

      * ABSENT (FileNotFoundError)              -> `({}, None, False)`
      * a SYMLINK (even to a valid file, even
        dangling)                               -> `({}, "SymlinkStatePath: ...", False)`
      * a JSON object (dict)                    -> `(state, None, False)`
      * bad JSON / undecodable bytes / absurd
        nesting (the bytes WERE read)           -> `({}, "<Class>: <msg>", True)`
      * valid JSON that is not an object        -> `({}, "NotADict: <type> ...", True)`
      * ANY OSError (PermissionError, EMFILE,
        EIO, a FIFO / directory ...)            -> `({}, "<Class>: <msg>", False)`

    `movable` is True ONLY when the file was actually READ and found unusable:
    the maintainer's ruling moves aside the UNPARSEABLE file, and an unreadable
    file has not been shown unparseable. A transient OSError (an fd limit, an
    I/O hiccup) on a perfectly valid file must never cost its history, so every
    OSError is UNKNOWN and never movable: the caller neither moves nor writes
    it, and the bytes stay where they are. The reason string is
    `"<ExceptionClass>: <message>"`, the shape `split_probe_reason` /
    `probe_unknown_line` already consume. The read never blocks (see
    open_regular_text).

    A symlinked path is UNKNOWN and never movable (maintainer ruling 5): the
    read follows a link and fstat-checks its TARGET, so a link to a regular
    file would read as regular, and the move-aside `os.replace` would rename
    the LINK and leave the corrupt target in place behind a `.corrupt` link.
    `os.lstat` runs first; a dangling link is still a link (UNKNOWN, not
    absent). Kept AST-identical with `engram_regular_io.read_state_json_classified`
    (pinned by tests/test_regular_io_parity_3628.py)."""
    try:
        try:
            lst = os.lstat(path)
        except FileNotFoundError:
            return {}, None, False
        except OSError as exc:
            return {}, f"{type(exc).__name__}: {exc}", False
        if stat.S_ISLNK(lst.st_mode):
            return {}, (
                "SymlinkStatePath: state path is a symlink; replace the link "
                "with a regular file to restore cooldowns"
            ), False
        try:
            data = json.loads(read_regular_text(path))
        except FileNotFoundError:
            return {}, None, False
        except (ValueError, RecursionError) as exc:  # bad JSON, bad bytes, absurd nesting
            return {}, f"{type(exc).__name__}: {exc}", True
        except OSError as exc:
            return {}, f"{type(exc).__name__}: {exc}", False
        if not isinstance(data, dict):
            return {}, f"NotADict: state is a {type(data).__name__}, not an object", True
        return data, None, False
    except Exception as exc:  # defensive: the contract is "never raises"
        return {}, f"{type(exc).__name__}: {exc}", False


def open_regular_text_write(path, encoding=None):
    """`open(path, "w", encoding=encoding)` that must never block (#3591).

    A write-open of a FIFO waits for a reader. O_WRONLY|O_NONBLOCK makes that
    fail at once with ENXIO when no reader exists (an OSError); and when a
    reader does exist the fstat still rejects the FIFO before anything is
    written. O_CREAT|O_TRUNC with mode 0o666 (umask applies) matches what
    open(path, "w") does, so a regular file is created / truncated and written
    exactly as before. Raises OSError for a non-regular target."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "w", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_regular_text_append(path, encoding=None):
    """`open(path, "a", encoding=encoding)` that must never block (#3628).

    Same shape as open_regular_text_write, with O_APPEND|O_CREAT (no O_TRUNC)
    and mode 0o666 (umask applies), matching open(path, "a"). A reader-less
    FIFO fails at os.open with ENXIO; a FIFO that has a reader is rejected by
    the fstat before any byte is written. Raises OSError for a non-regular
    target."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "a", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


_NOT_REGULAR_DB_ERROR_CLS = None


def _not_regular_db_error_cls():
    """The refusal class for connect_regular_ro, built once per process.

    It subclasses sqlite3.OperationalError (so a handler that classifies a
    sqlite failure -- e.g. surface-hook._check_db_liveness's
    `except sqlite3.DatabaseError -> unhealthy` -- keeps the branch a
    directory already took on dev) AND OSError (so a handler written for
    "could not open the file" also matches). A plain OSError would be caught
    by the later broad `except Exception` there and report a FIFO DB healthy.
    sqlite3 is imported here, not at module level: every hook imports this
    module, and the class cannot be declared without sqlite3 in hand, so it is
    built on first use and cached in a module global (one class object)."""
    global _NOT_REGULAR_DB_ERROR_CLS
    if _NOT_REGULAR_DB_ERROR_CLS is None:
        import sqlite3

        class NotRegularDatabaseError(sqlite3.OperationalError, OSError):
            """The database path exists but is not a regular file."""

        _NOT_REGULAR_DB_ERROR_CLS = NotRegularDatabaseError
    return _NOT_REGULAR_DB_ERROR_CLS


def connect_regular_ro(db_path, uri_params="mode=ro", **connect_kwargs):
    """`sqlite3.connect(f"file:{db_path}?{uri_params}", uri=True, **kwargs)`
    that refuses a non-regular file instead of blocking on it (#3628).

    A `mode=ro` connect to a FIFO hangs (measured: > 5 s), so a hook under a
    5 s timeout is killed. This stats first (os.stat follows symlinks and does
    not block on a FIFO) and, when the path exists and is not a regular file,
    raises NotRegularDatabaseError -- a sqlite3.OperationalError AND an
    OSError, message "not a regular file: <path>". A path that cannot be
    statted (missing, dangling symlink, unreadable parent) falls through to
    the raw connect, so sqlite raises exactly what it raised before. Extra
    keyword arguments (timeout=...) pass through untouched; a timeout bounds
    lock waits, not a FIFO open, so it is not part of this defence.
    `uri_params` is the URI query, e.g. "mode=ro&immutable=1".

    Residual, NOT closed here. #3577 states it: "The pair is check-then-act:
    measured by two people independently, with the file regular at the `stat`
    and swapped for a FIFO before the connect, `sqlite3.connect` hung until
    killed at 6 s." SQLite takes a path, not a descriptor, so the fd+fstat
    route the other helpers in this module use is not available here; the
    race-free /proc/self/fd/N route is unverified and not portable (see
    #3577). This helper narrows the window to stat-then-connect; it does not
    close it."""
    import sqlite3
    try:
        st = os.stat(db_path)
    except OSError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise _not_regular_db_error_cls()(f"not a regular file: {db_path}")
    return sqlite3.connect(
        f"file:{db_path}?{uri_params}", uri=True, **connect_kwargs)


def session_started_at(session_id):
    """This session's `started_at` from `$ENGRAM_HOME/sessions/<sid>.json`
    (aware datetime), or None when the marker is missing/malformed. Shared
    helper for the route ledger's `created` source; mirrors the surface hook's
    `_get_session_started_at` (left untouched: that script is not importable)."""
    if not session_id:
        return None
    try:
        home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        with open_regular_text(Path(home) / "sessions" / f"{session_id}.json", encoding="utf-8") as f:
            marker = json.load(f)
        return _parse_iso_utc(marker.get("started_at"))
    except Exception:
        return None


def _read_session_marker(session_id):
    """The session marker dict from `$ENGRAM_HOME/sessions/<sid>.json`, or None
    when it is missing, malformed or not an object."""
    if not session_id:
        return None
    try:
        home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        fd = _open_regular_fd(Path(home) / "sessions" / f"{session_id}.json")
        if fd is None:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            marker = json.load(f)
        return marker if isinstance(marker, dict) else None
    except Exception:
        return None


def _marker_compaction_count(marker):
    """The marker's `compaction_count` as an int, or None when the marker is
    absent or lacks a usable value (a bool is not a count)."""
    if not isinstance(marker, dict):
        return None
    n = marker.get("compaction_count")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        return n
    return None


_SUMMARY_MARK = b'"isCompactSummary":true'
_BOUNDARY_MARK = b'"subtype":"compact_boundary"'
# A real summary line is tens of KB (12 KB measured); json.loads of a matched
# line costs about 3.5x the line in peak memory, so a line above this is skipped.
_SUMMARY_LINE_CAP = 1_000_000
_NODE_ID_RE = None  # compiled lazily: `_re` is imported further down the module


def _summary_text(record):
    """The text of a parsed compaction-summary record, or None when `record` is
    not one: it must carry a top-level `isCompactSummary` that `is True` and a
    `message.content` that is a string or a list of text blocks."""
    if not isinstance(record, dict) or record.get("isCompactSummary") is not True:
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b["text"] for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str))
    return None


def _last_summary_ids(transcript_path) -> tuple:
    """(ids, readable): node ids that appear as text in the LAST compaction
    summary of the session transcript. Found from the END of the file by a
    backwards bytes search (mmap `rfind`); a hit that does not parse, or whose
    record is not a real summary (`_summary_text`), is skipped and the search
    continues backwards, so a record that merely quotes the phrase inside an
    escaped string never matches (its quotes are escaped, so the bytes differ).
    No transcript path, an unreadable file, or no summary record at all ->
    readable False (unknown, never an empty set)."""
    global _NODE_ID_RE
    import mmap
    try:
        if not isinstance(transcript_path, str) or not transcript_path:
            return set(), False
        if _NODE_ID_RE is None:
            _NODE_ID_RE = _re.compile(r"\b[a-z]{2}_\d{4,}(?:_[A-Za-z0-9]+)?\b")
        # O_NONBLOCK: open() on a FIFO would block until a writer appears, and
        # this runs in a hook that fires on every prompt and tool call. Only a
        # regular file is ever mapped.
        fd = _open_regular_fd(transcript_path)
        if fd is None:
            return set(), False
        try:
            with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as m:
                end = len(m)
                while end > 0:
                    hit = m.rfind(_SUMMARY_MARK, 0, end)
                    if hit < 0:
                        return set(), False
                    start = m.rfind(b"\n", 0, hit) + 1
                    stop = m.find(b"\n", hit)
                    if stop < 0:
                        stop = len(m)
                    if stop - start > _SUMMARY_LINE_CAP:
                        # Treated like an unparseable hit. The cap is on the
                        # LINE, not the file: a backward-scan window or a file
                        # size cap would make a far-back summary read unknown,
                        # a behaviour change this code deliberately does not take.
                        end = start
                        continue
                    try:
                        text = _summary_text(json.loads(m[start:stop]))
                    except ValueError:
                        text = None
                    if text is not None:
                        # A compact_boundary record AFTER this summary means a
                        # newer compaction has begun and its own summary is not
                        # written yet: this one is stale, so unreadable. File
                        # order, not timestamps: on a real session the summary's
                        # `timestamp` is about 2 minutes BEFORE the marker's
                        # `started_at`, so a "newer than started_at" test would
                        # reject the right summary. Measured on one real
                        # transcript, in 5 of 5 compactions the boundary record
                        # precedes its summary, and the summary precedes the
                        # next boundary.
                        if m.find(_BOUNDARY_MARK, hit) >= 0:
                            return set(), False
                        return set(_NODE_ID_RE.findall(text)), True
                    end = start
        finally:
            os.close(fd)
        return set(), False
    except Exception:  # empty file (mmap ValueError), permissions, anything
        return set(), False


def _earlier_shown_ids(routes_dir: Path, compaction_count=None) -> tuple:
    """(ids, readable): every id in the `shown` list of the lines already in
    `routes_dir/*.jsonl` (both routes) that were written in the CURRENT context
    window (#3556). `compaction_count` unknown (None) or 0: every earlier line
    counts, as before. N > 0: a line counts only when its `compaction_count`
    equals N; a lower count, or no `compaction_count` key (written by older
    code), does not. Dropping old-format lines is the conservative direction:
    it under-reports in_context and never over-reports it. A malformed line is
    skipped; a file that cannot be read makes `readable` False. A routes dir
    that does not exist yet is readable-and-empty (nothing has been shown).
    A missing, corrupt or count-less marker makes the count None, so every
    earlier line counts again and `shown` can over-report in that case."""
    windowed = isinstance(compaction_count, int) and compaction_count > 0
    ids: set = set()
    readable = True
    try:
        files = sorted(routes_dir.glob("*.jsonl")) if routes_dir.is_dir() else []
    except Exception:
        return ids, False
    for path in files:
        try:
            fd = _open_regular_fd(path)
            if fd is None:
                readable = False
                continue
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if windowed and row.get("compaction_count") != compaction_count:
                        continue
                    shown = row.get("shown")
                    if isinstance(shown, list):
                        ids.update(i for i in shown if isinstance(i, str))
        except Exception:
            readable = False
    return ids, readable


# ---------------------------------------------------------------------------
# Shown record (#3613 S1): what the two LIVE recall routes actually rendered
# ---------------------------------------------------------------------------
# Both hooks append ONE JSON line per fire to `<ENGRAM_HOME>/sessions/
# <session_id>.shown.jsonl` -- OUTSIDE the probe dir, so it exists on every seat
# (the probe ledger's `shown` is only written when the probe dir is live).
# Line: {ts, route, compaction_count, prompt_index, ids}. `ids` are the own-graph
# ids actually rendered (never the cap-cut ones). `prompt_index` is a per-session
# monotonically increasing integer, shared by both routes (last line's + 1).
# The in-context resolver reads this record as part of its `shown` source,
# windowed by compaction_count exactly like `_earlier_shown_ids`.
#
# Bounds: at most SHOWN_RECORD_MAX_IDS ids per line (head kept) and at most
# SHOWN_RECORD_MAX_BYTES per file. Past the byte bound the OLDEST lines are
# dropped (the file is rewritten to about half the bound), which loses only
# older context windows' lines. The write is fail-open: any error is silent
# apart from at most one stderr line, and never changes a hook's output. It is
# written whatever the exclusion kill switch says.
#
# Retention (#3622): `prune_shown_records` (called once per SessionStart) deletes
# records whose mtime is older than SHOWN_RECORD_RETENTION_DAYS; an append
# refreshes mtime, so a session written to within that window is never pruned.
# No-follow: the append opens with O_NOFOLLOW, and the prune lstat()s -- a symlink
# at a record path is neither written through nor unlinked-as-a-file.
#
# Final cut (#3621): the recall block is ONE atomic body item that emit_context
# may still cut or truncate, so the surface hook records the fire's ids all-or-
# nothing -- only if the block was rendered IN FULL (`emit_context(kept_out=)`),
# else an EMPTY-ids line, after the response was printed. Under-record, never over.

SHOWN_RECORD_MAX_IDS = 64
SHOWN_RECORD_MAX_BYTES = 262144
SHOWN_RECORD_RETENTION_DAYS = 14
SHOWN_RECORD_PRUNE_MAX_ENTRIES = 5000


def _safe_session_name(session_id):
    """`session_id` when it is usable as ONE file-name component, else None
    (the same guard `write_route_ledger` applies to its probe-dir key)."""
    sid = session_id if isinstance(session_id, str) else ""
    if (
        not sid or sid == "unknown" or "/" in sid or "\\" in sid or ".." in sid
        or sid in {".", ".."} or sid != Path(sid).name
    ):
        return None
    return sid


def shown_record_path(session_id):
    """`<ENGRAM_HOME>/sessions/<session_id>.shown.jsonl`, or None for an unsafe id."""
    sid = _safe_session_name(session_id)
    if sid is None:
        return None
    home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    return Path(home) / "sessions" / f"{sid}.shown.jsonl"


def _shown_record_ids(session_id, compaction_count=None) -> tuple:
    """(ids, readable) from the shown record, windowed exactly as
    `_earlier_shown_ids` windows the probe ledger: count unknown/0 -> every line;
    N > 0 -> only lines whose `compaction_count` == N. A missing record is
    readable-and-empty; a record that cannot be read as a regular file makes
    `readable` False."""
    path = shown_record_path(session_id)
    ids: set = set()
    if path is None:
        return ids, True
    try:
        if not os.path.lexists(path):
            return ids, True
        fd = _open_regular_fd(path)
        if fd is None:
            return ids, False
        windowed = isinstance(compaction_count, int) and compaction_count > 0
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if windowed and row.get("compaction_count") != compaction_count:
                    continue
                rec = row.get("ids")
                if isinstance(rec, list):
                    ids.update(i for i in rec if isinstance(i, str))
        return ids, True
    except Exception:
        return set(), False


def append_shown_record(route: str, session_id, ids) -> None:
    """Append this fire's line to the shown record (see the block comment).
    Never raises; at most one stderr line. Call it AFTER the resolver for this
    fire was built, so a node is never "already shown" by the fire being
    written.

    `prompt_index` is best-effort: it is read from the last parseable line + 1
    with no lock, so it may duplicate under concurrent fires, or reset to 0 on
    an unparseable file. Consumers must not treat it as a strict sequence."""
    try:
        path = shown_record_path(session_id)
        if path is None or not path.parent.is_dir():
            return  # no session dir: nothing to record into
        marker = _read_session_marker(session_id)
        kept: list = []
        for i in ids or ():
            if isinstance(i, str) and i not in kept:
                kept.append(i)
        kept = kept[:SHOWN_RECORD_MAX_IDS]
        lines: list = []
        fd = _open_regular_fd(path)
        if fd is not None:
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                lines = f.readlines()
        elif os.path.lexists(path):
            raise OSError(f"{path.name} is not a regular file")
        last = -1
        for raw in reversed(lines):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            n = row.get("prompt_index") if isinstance(row, dict) else None
            if isinstance(n, int) and not isinstance(n, bool):
                last = n
                break
        line = {
            "ts": int(_time.time() * 1000), "route": route,
            "compaction_count": _marker_compaction_count(marker),
            "prompt_index": last + 1, "ids": kept,
        }
        text = json.dumps(line) + "\n"
        text_bytes = len(text.encode("utf-8"))
        if sum(len(x.encode("utf-8")) for x in lines) + text_bytes > SHOWN_RECORD_MAX_BYTES:
            keep: list = []
            size = text_bytes
            for raw in reversed(lines):
                size += len(raw.encode("utf-8"))
                if size > SHOWN_RECORD_MAX_BYTES // 2:
                    break
                keep.append(raw)
            keep.reverse()
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            try:
                os.unlink(tmp)  # a stale tmp (or a symlink planted there): remove, never follow
            except FileNotFoundError:
                pass
            tfd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(tfd, "".join(keep).encode("utf-8"))
            finally:
                os.close(tfd)
            os.replace(tmp, path)
        # O_NONBLOCK: a write-open of a FIFO blocks until a reader appears.
        # O_NOFOLLOW: a symlink planted at the record path is refused (ELOOP), never written through.
        wfd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(wfd).st_mode):
                return
            os.write(wfd, text.encode("utf-8"))
        finally:
            os.close(wfd)
    except Exception as exc:  # fail-open
        try:
            msg = f"shown-record: {type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
            print(msg[:300], file=sys.stderr)
        except Exception:
            pass


def _shown_record_candidate_session(name):
    """The session name when `name` is a shown-record candidate (`<sid>.shown.jsonl`
    or a stale trim leftover `<sid>.shown.jsonl.<digits>.tmp`), else None."""
    suffix = ".shown.jsonl"
    sid = None
    if name.endswith(suffix):
        sid = name[: -len(suffix)]
    elif name.endswith(".tmp"):
        head, sep, pid = name[: -len(".tmp")].rpartition(suffix + ".")
        if sep and pid.isascii() and pid.isdigit():
            sid = head
    # An empty session name is never ours: append cannot create one.
    return sid or None


def prune_shown_records(
    keep_session_id=None, max_age_days=SHOWN_RECORD_RETENTION_DAYS, now=None
) -> int:
    """Delete shown records (and stale trim tmp leftovers) older than
    `max_age_days` from `<ENGRAM_HOME>/sessions/`; return how many were deleted.

    Only direct entries named `<sid>.shown.jsonl` / `<sid>.shown.jsonl.<digits>.tmp`
    are candidates. A candidate goes only if it is a regular file by `lstat` (a
    symlink, directory, FIFO or socket is never followed or unlinked), its mtime is
    strictly older than the cutoff (a future mtime stays), and its session is not
    `keep_session_id`. A `max_age_days <= 0` deletes every non-keep record, fresh
    ones included (the only caller uses the default). Scans at most SHOWN_RECORD_PRUNE_MAX_ENTRIES entries. Never
    raises; at most one stderr line."""
    deleted = 0
    first_err = None
    try:
        home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        sessions = Path(home) / "sessions"
        cutoff = (_time.time() if now is None else now) - max_age_days * 86400
        with os.scandir(sessions) as it:
            for scanned, entry in enumerate(it):
                if scanned >= SHOWN_RECORD_PRUNE_MAX_ENTRIES:
                    break
                try:
                    sid = _shown_record_candidate_session(entry.name)
                    if sid is None or (keep_session_id is not None and sid == keep_session_id):
                        continue
                    st = os.lstat(entry.path)
                    if not stat.S_ISREG(st.st_mode) or not st.st_mtime < cutoff:
                        continue
                    os.unlink(entry.path)
                    deleted += 1
                except FileNotFoundError:
                    continue  # raced with a concurrent unlink: not ours to count
                except Exception as exc:
                    if first_err is None:
                        first_err = exc
    except Exception as exc:
        if first_err is None:
            first_err = exc
    if first_err is not None:
        try:
            msg = f"shown-record prune: {type(first_err).__name__}: {first_err}"
            msg = "".join(c if c.isprintable() else " " for c in msg)
            print(msg[:300], file=sys.stderr)
        except Exception:
            pass
    return deleted


def _focused_ids(read_conn=None, on_failure=None) -> tuple:
    """(ids, readable): ids currently focused, read-only, one query. Any DB
    problem (missing file, locked, no column) -> readable False.

    `read_conn` (#3710, U8): a connection the CALLER owns, e.g. the tripwire
    hook's engramd owner shim, so a flag-ON fire never opens knowledge.db. It
    is used as-is and never closed here, and a failure on it reads False with
    no fallback to a direct open. Without it: one direct read-only open,
    closed in a `finally`, as before. One execute either way, so the read
    registry keeps one site for this template.

    `on_failure` (#3827): an optional `cb(exc)` called ONCE with the exception when the read
    fails, so a caller can record WHY (the owner hooks record an owner failure). It changes
    nothing else: the return is still `(set(), False)`, and a callback that raises is ignored
    (fail-open: the resolver drops nothing, only the silence goes)."""
    conn = None
    owned = read_conn is None
    try:
        if owned:
            home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
            # Short lock wait: this source is optional and an unreadable result is
            # already handled (readable False -> in_context None), so a locked DB
            # must not cost the surface hook a full second.
            db_path = Path(home) / "knowledge.db"
            # timeout bounds lock waits, not a FIFO open, so it is not part of the
            # FIFO defence; connect_regular_ro refuses a non-regular file.
            conn = connect_regular_ro(db_path, timeout=0.2)
        else:
            conn = read_conn
        rows = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 ORDER BY focused_at"
        ).fetchall()
        return {r[0] for r in rows}, True
    except Exception as exc:
        if on_failure is not None:
            try:
                on_failure(exc)
            except Exception:
                pass
        return set(), False
    finally:
        if owned and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _in_context_for(node_id, created_at, *, shown_ids, shown_ok, started,
                    focused, focused_ok, summary_ids=frozenset(), summary_ok=True):
    """`(in_context, why)` for one candidate against the pre-read sources
    (see `make_in_context_resolver`). First source that fired wins, in the order
    summary, shown, created, focused. That order is a decision to align with
    the offline recompute tool so live and offline rows compare; it is not a
    measured fact. None fired -> (False, None); a source that cannot be read and
    no readable source fired -> (None, None). `summary_ok` is True with an empty
    `summary_ids` when the summary source is off (no compaction yet)."""
    if node_id in summary_ids:
        return True, "summary"
    if node_id in shown_ids:
        return True, "shown"
    created_dt = _parse_iso_utc(created_at)
    created_known = started is not None and created_dt is not None
    if created_known and created_dt >= started:
        return True, "created"
    if node_id in focused:
        return True, "focused"
    if not (shown_ok and created_known and focused_ok and summary_ok):
        return None, None
    return False, None


def make_in_context_resolver(session_id, routes_dir: Path, marker=None, read_conn=None,
                             on_failure=None):
    """Build `resolve(node_id, created_at) -> (in_context, why)` for ONE ledger
    write. Reads the sources once, so call it BEFORE appending the line
    being written (a node is never "already shown" by the very fire being
    written). `why` is the FIRST source that fired, in the order summary,
    shown, created, focused. None fired -> (False, None). A source that cannot
    be read and no readable source fired -> (None, None): unknown, not false.

    Compaction (#3556): `shown` counts only ledger lines of the current context
    window, and once the session has compacted (marker `compaction_count` > 0)
    the ids in the LAST compaction summary are a fourth source, read lazily here
    from the marker's transcript. The summary is read now rather than at
    SessionStart because on the one session measured (n=1) SessionStart ran
    about 1.2 s BEFORE the transcript's compact_boundary record, with the
    summary a few lines after it, so it is probably not in the file yet when the
    marker is written. A session that has not compacted (count 0 or unknown)
    never opens the transcript. `marker` is the already-read marker dict (read
    here when omitted). `read_conn` is passed to `_focused_ids` (the only
    source that reads the store; see there); `on_failure` rides with it (only when given)."""
    if marker is None:
        marker = _read_session_marker(session_id)
    count = _marker_compaction_count(marker)
    shown_ids, shown_ok = _earlier_shown_ids(routes_dir, count)
    # #3613 S1: the live routes' own shown record, present on every seat (the
    # probe ledger above exists only while the probe is live). Union of both.
    rec_ids, rec_ok = _shown_record_ids(session_id, count)
    shown_ids = set(shown_ids) | rec_ids
    shown_ok = shown_ok and rec_ok
    started = _parse_iso_utc(marker.get("started_at")) if marker else None
    focused, focused_ok = (_focused_ids(read_conn=read_conn) if on_failure is None
                           else _focused_ids(read_conn=read_conn, on_failure=on_failure))
    if count:
        summary_ids, summary_ok = _last_summary_ids(marker.get("transcript_path"))
    else:
        summary_ids, summary_ok = set(), True
    return functools.partial(
        _in_context_for, shown_ids=shown_ids, shown_ok=shown_ok,
        started=started, focused=focused, focused_ok=focused_ok,
        summary_ids=summary_ids, summary_ok=summary_ok)


# ---------------------------------------------------------------------------
# Live in-context exclusion (#3613 Part 2)
# ---------------------------------------------------------------------------
# Config `recall_in_context_exclusion.enabled` in $ENGRAM_HOME/config.json,
# default OFF: only a JSON `true` turns it on (anything else, or an unreadable
# config, is off). Everything below is fail-open: a resolver that cannot be
# built or called drops NOTHING.

def in_context_exclusion_enabled(engram_home=None) -> bool:
    """True only when config.json has `recall_in_context_exclusion.enabled: true`."""
    try:
        home = engram_home or os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        # open_regular_text: a FIFO at config.json fails fast (OSError -> off)
        # instead of blocking the hook (#3628).
        with open_regular_text(Path(home) / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        section = config.get("recall_in_context_exclusion")
        return isinstance(section, dict) and section.get("enabled") is True
    except Exception:
        return False


def build_exclusion_resolver(session_id, read_conn=None, on_failure=None):
    """`resolve(node_id, created_at) -> (in_context, why)` for the live
    exclusion, or None when it cannot be built (fail-open: None = drop nothing).
    Same resolver the probe ledger uses; the probe routes dir is read only when
    the probe is live for this session. `read_conn`, `on_failure`: see `_focused_ids`."""
    try:
        sid = _safe_session_name(session_id)
        if sid is None:
            return None
        marker = _read_session_marker(sid)
        kw = {} if on_failure is None else {"on_failure": on_failure}
        return make_in_context_resolver(
            sid, route_probe_dir() / sid / "routes", marker=marker,
            read_conn=read_conn, **kw)
    except Exception:
        return None


def in_context_drop_ids(candidates, resolver, exempt_types=()) -> set:
    """The ids among `candidates` (dicts with `id`, optionally `created_at` /
    `type`) to drop as already in the model's context: those the resolver
    answers `in_context is True` for, whatever the `why`. `None` (unknown) and
    `False` keep the candidate. A candidate whose `type` is in `exempt_types`
    is never dropped. Any exception -> an empty set (all-or-nothing: a
    half-applied exclusion would make the output depend on where it failed)."""
    try:
        if resolver is None:
            return set()
        drop: set = set()
        for c in candidates:
            if not isinstance(c, dict):
                continue
            nid = c.get("id")
            if not nid or nid in drop:
                continue
            if exempt_types and c.get("type") in exempt_types:
                continue
            in_context, _why = resolver(nid, c.get("created_at"))
            if in_context is True:
                drop.add(nid)
        return drop
    except Exception:
        return set()


def build_route_candidates(result: Optional[dict], in_context=None) -> tuple:
    """(candidates, missing_signals) from a daemon/query response, BEFORE any
    route-side cut. `in_context`, when given, is the resolver from
    `make_in_context_resolver`: it sets `in_context` / `in_context_why` per
    candidate (the response never carries them); without it `in_context` is
    null and named missing, as before. Every candidate carries
    `in_context_why` (None unless a resolver sets it). A candidate with
    `ranked is False` does not list its null composite_score as missing
    (bypass rows are never composite-ranked, by design). Ranking = the
    response's own `matched_ids` order (falling back to matched_meta /
    top_claims / special_nodes order when the response carries no
    matched_ids). Each signal is read from whichever of
    matched_meta / top_claims / special_nodes carries it for that id; absent
    -> None, and the name is added once to missing_signals."""
    result = result if isinstance(result, dict) else {}
    sources: dict = {}
    pool = (
        list(result.get("matched_meta") or [])
        + list(result.get("top_claims") or [])
        + list(result.get("special_nodes") or [])
    )
    order = [i for i in (result.get("matched_ids") or []) if i]
    for e in pool:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        sources.setdefault(e["id"], []).append(e)
        if not result.get("matched_ids") and e["id"] not in order:
            order.append(e["id"])
    candidates, missing = [], []
    for rank, nid in enumerate(order, start=1):
        cand = {"id": nid, "rank": rank, "in_context_why": None}
        for name in ROUTE_SIGNALS:
            val = None
            for e in sources.get(nid, ()):
                val = _signal_from(e, name)
                if val is not None:
                    break
            if name == "in_context" and in_context is not None:
                val, why = in_context(nid, cand.get("created_at"))
                cand["in_context_why"] = why
            cand[name] = val
            if val is None and name not in missing:
                if name == "composite_score" and cand.get("ranked") is False:
                    continue  # bypass row: null composite is by design (#3551)
                missing.append(name)
        candidates.append(cand)
    return candidates, missing


def write_route_ledger(
    route: str,
    session_id,
    fire_key: str,
    query,
    result: Optional[dict],
    shown: list,
    cut_reasons: dict,
    *,
    source: str,
    default_cut: str = "cap",
    extra: Optional[dict] = None,
    read_conn=None,
    on_failure=None,
) -> None:
    """Append one ledger line (see block comment). Never raises; at most one
    stderr line. `cut_reasons` maps id -> the route's own cut name; any
    candidate neither shown nor named gets `default_cut`. `read_conn`: passed
    to the in-context resolver (see `_focused_ids`), `on_failure` likewise."""
    try:
        sid = session_id if isinstance(session_id, str) else ""
        # "unknown" is the hooks' own missing-session placeholder, and a
        # separator / dot-dot would escape the probe dir: never a ledger key.
        # "." would resolve to the probe root itself (`probeDir/.`), so a line
        # would land in `<probeDir>/routes/`: it must be a plain single name.
        if (
            not sid or sid == "unknown" or "/" in sid or "\\" in sid or ".." in sid
            or sid in {".", ".."} or sid != Path(sid).name
        ):
            return
        session_dir = route_probe_dir() / sid
        if not session_dir.is_dir():
            return  # probe not live for this session: true optional, silent
        # Read the in_context sources BEFORE this line is appended (C4).
        marker = _read_session_marker(sid)
        kw = {} if on_failure is None else {"on_failure": on_failure}
        resolver = make_in_context_resolver(sid, session_dir / "routes", marker=marker,
                                            read_conn=read_conn, **kw)
        candidates, missing = build_route_candidates(result, in_context=resolver)
        shown_set = set(shown)
        cut = {
            c["id"]: cut_reasons.get(c["id"], default_cut)
            for c in candidates if c["id"] not in shown_set
        }
        line = {
            "ts": int(_time.time() * 1000),
            "route": route,
            "session_id": sid,
            "compaction_count": _marker_compaction_count(marker),
            "fire_key": fire_key,
            "source": source,
            "query": query,
        }
        if extra:
            line.update(extra)
        line.update({
            "candidates": candidates,
            "missing_signals": missing,
            "shown": list(shown),
            "cut": cut,
        })
        text = json.dumps(line) + "\n"
        routes_dir = session_dir / "routes"
        if not routes_dir.is_dir():
            os.mkdir(routes_dir, 0o700)
            os.chmod(routes_dir, 0o700)  # mkdir's mode is masked by umask
        # O_NONBLOCK: a write-open of a FIFO blocks until a reader appears, and
        # nothing in this hook's response is printed until this function returns.
        fd = os.open(routes_dir / f"{route}.jsonl",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return  # a FIFO with a reader would otherwise accept the write
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # fail-open: the route's own output is never affected
        try:
            msg = f"route-ledger: {type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
            print(msg[:300], file=sys.stderr)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Advisory-probe `unknown` state (part of #3521, rows 1-4)
# ---------------------------------------------------------------------------
# A probe that cannot measure must say so. "Never block" means the probe never
# blocks the tool call; it never meant a probe may REPORT HEALTHY on a failure.
# Each probe's result is tri-state -- True healthy / False confirmed not-ok /
# None UNKNOWN -- and UNKNOWN renders ONE bounded line (the #3504 loader notice
# in engram-lesson-tripwire-hook.py is the template), rate-limited for the
# per-prompt / per-tool-call hooks. This is the ONE shared implementation of
# sanitize + line-building + the stamp-file rate limit; the tripwire hook's own
# copies are deliberately not refactored onto it here (a follow-up).
#
# Reason convention: an unknown probe returns `(None, "<class>: <message>")`
# where <class> is an exception type name or a short reason class
# (`git-missing`, `timeout`, `nonzero`, `unparsable`, `pgrep-unknown`, ...).
# The raw <message> goes ONLY to the sanitized stderr line; the injected line
# carries the <class> alone.

import re as _re

PROBE_NOTICE_WINDOW_SECONDS = 1800
PROBE_NOTICE_STAMP_NAME = "probe-unknown-notice.json"
_PROBE_CONTROL_CHARS_RE = _re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_probe_text(text, limit: int) -> str:
    """Each control character (newline included) -> one space, then cut to
    `limit` characters, so a message is always one bounded line. Never raises."""
    try:
        return _PROBE_CONTROL_CHARS_RE.sub(" ", str(text))[:limit]
    except Exception:
        return "<unprintable>"[:limit]


def split_probe_reason(reason) -> tuple:
    """`"<class>: <message>"` -> (class, message). A reason with no colon is
    all class. Never raises."""
    try:
        cls, _sep, msg = str(reason).partition(":")
        return cls.strip(), msg.strip()
    except Exception:
        return "unknown", ""


def write_probe_unknown_stderr(hook_name: str, probe: str, reason) -> None:
    """Write exactly one stderr line:
    `<hook_name>: <probe> unknown: <class>: <message>` (message sanitized,
    <= 200 chars; whole line <= 300). Never raises."""
    try:
        cls, msg = split_probe_reason(reason)
        line = (
            f"{sanitize_probe_text(hook_name, 60)}: {sanitize_probe_text(probe, 60)} unknown: "
            f"{sanitize_probe_text(cls, 80)}: {sanitize_probe_text(msg, 200)}"
        )
        sys.stderr.write(line[:300] + "\n")
    except Exception:
        pass


def probe_unknown_line(label: str, reason, suffix: str, prefix: str = "[probe] ") -> str:
    """The ONE injected line for an unknown probe, <= 200 chars:
    `[probe] <label> state UNKNOWN (<class>); <suffix>`. Carries the exception
    type / reason class only, never the raw message. Never raises."""
    try:
        cls, _ = split_probe_reason(reason)
        line = (
            f"{prefix}{sanitize_probe_text(label, 90)} state UNKNOWN "
            f"({sanitize_probe_text(cls, 40)}); {sanitize_probe_text(suffix, 60)}"
        )
        return line[:200]
    except Exception:
        return f"{prefix}probe state UNKNOWN"[:200]


def _probe_stamp_path(engram_home) -> Path:
    return Path(engram_home) / PROBE_NOTICE_STAMP_NAME


def _probe_stamp_read(engram_home) -> dict:
    """The `shown_at` map from the stamp file, or {} when the file is absent,
    unreadable, corrupt or wrongly shaped. Never raises."""
    try:
        data = json.loads(read_regular_text(_probe_stamp_path(engram_home), encoding="utf-8"))
        shown = data["shown_at"]
        return shown if isinstance(shown, dict) else {}
    except Exception:
        return {}


def probe_notice_due(engram_home, key: str, now=None) -> bool:
    """True unless a notice for `key` was shown within the last window. An
    absent, unreadable, corrupt, wrongly-shaped or future-dated stamp is "due":
    the rate limit fails VISIBLE, never silent. Never raises."""
    try:
        now = _time.time() if now is None else now
        ts = _probe_stamp_read(engram_home).get(str(key)[:300])
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return True
        return not (0 <= now - ts < PROBE_NOTICE_WINDOW_SECONDS)
    except Exception:
        return True


def mark_probe_notice(engram_home, key: str, now=None) -> None:
    """Start a new rate-limit window for `key` (call only AFTER the response
    carrying the line was produced). Expired and malformed entries are dropped
    so the file stays small. Never raises: an unwritable stamp just means the
    next unknown shows the line again."""
    try:
        now = _time.time() if now is None else now
        kept = {
            k: v for k, v in _probe_stamp_read(engram_home).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and 0 <= now - v < PROBE_NOTICE_WINDOW_SECONDS
        }
        kept[str(key)[:300]] = now
        path = _probe_stamp_path(engram_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"shown_at": kept})
    except Exception:
        pass


def probe_own_server_py(engram_home: str) -> tuple:
    """Tri-state twin of is_own_server_py_running (which is unchanged and still
    folds every failure into False). Returns (state, reason):

        (True, None)      pgrep matched (rc 0)
        (False, None)     pgrep ran and found no match (rc 1)
        (None, reason)    pgrep could not answer: not installed, timed out,
                          exited with an error rc >= 2, or the path could not
                          be resolved. Never evidence of absence.

    Never raises."""
    import subprocess

    try:
        server_path = resolve_own_server_py_path(engram_home)
        result = subprocess.run(
            ["pgrep", "-f", server_path], capture_output=True, timeout=2
        )
        if result.returncode == 0:
            return True, None
        if result.returncode == 1:
            return False, None
        return None, f"pgrep-unknown: pgrep exited {result.returncode}"
    except subprocess.TimeoutExpired:
        return None, "pgrep-unknown: pgrep timed out"
    except Exception as exc:
        return None, f"pgrep-unknown: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Meaningful-span marker (#3613 Part 1A, consumer side)
# ---------------------------------------------------------------------------
# Producers (loop stub, probe rating request, monitors, bell) wrap the
# meaningful text of a notification in OPEN..CLOSE so recall consumers can
# search only that text instead of the fixed wrapper that dominates the match.

ENGRAM_MARKER_OPEN = "⟦ENGRAM⟧"
ENGRAM_MARKER_CLOSE = "⟦/ENGRAM⟧"


def marker_recall_query(text):
    """Return ``(state, query)`` for a recall consumer; ``state`` is one of
    ``"none"`` (no marker: ``query is text``), ``"marked"`` (``query`` is the
    marked span text, several top-level spans joined by ``"\\n"``) or
    ``"empty"`` (markers present but nothing meaningful inside; ``query`` is
    ``""``).

    Balanced markers yield their top-level spans (nesting by depth count).
    Unbalanced markers (an unclosed opener, or a closer at depth 0 after the
    first opener) fall back to ONE span from the first opener to the LAST
    closer after it (or to the end of the text when none follows). Marker
    tokens inside a span are removed, spans are stripped, empties dropped.
    Never raises: any unexpected input is ``("none", text)``."""
    try:
        if not isinstance(text, str) or ENGRAM_MARKER_OPEN not in text:
            return ("none", text)
        opener, closer = ENGRAM_MARKER_OPEN, ENGRAM_MARKER_CLOSE
        spans: list = []
        depth = 0
        start = 0
        stray = False
        seen_open = False
        for m in _re.finditer(_re.escape(opener) + "|" + _re.escape(closer), text):
            if m.group(0) == opener:
                if depth == 0:
                    start = m.end()
                depth += 1
                seen_open = True
            elif depth > 0:
                depth -= 1
                if depth == 0:
                    spans.append(text[start:m.start()])
            elif seen_open:
                stray = True  # closer at depth 0 after the first opener
        if depth > 0 or stray:
            begin = text.index(opener) + len(opener)
            last_close = text.rfind(closer)
            spans = [text[begin:last_close] if last_close >= begin else text[begin:]]
        cleaned = []
        for span in spans:
            span = span.replace(opener, "").replace(closer, "").strip()
            if span:
                cleaned.append(span)
        if not cleaned:
            return ("empty", "")
        return ("marked", "\n".join(cleaned))
    except Exception:
        return ("none", text)
