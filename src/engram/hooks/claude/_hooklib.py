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
from typing import Callable, Iterable, Optional


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

_ENGRAM_HOME_FOR_OVERFLOW_PLACEHOLDER = os.environ.get("ENGRAM_HOME") or str(
    Path.home() / ".engram"
)


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
        trailer = _build_context_trailer(1, cut_n_for_marker, overflow_path_, head_cut)
        overhead = len(trailer) + (len(head_text) + 1 if head_text else 0) + 1 + len(marker)
        item_room = max(ceiling - overhead, 0)
        item_text = body_list[0][:item_room]
        parts = [trailer]
        if head_text:
            parts.append(head_text)
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
    if len(rendered) > budget:
        safe = _trim_to_line_boundary(rendered, budget)
        rendered = safe if safe else rendered[:budget]

    return rendered
