#!/usr/bin/env python3
"""
PreToolUse hook (Bash + MCP): surfaces lesson tripwires at action-moment.

PROBLEM SOLVED
==============
The prompt-time recall hook (engram-surface-hook.py) fires at UserPromptSubmit
and matches lessons against the user's prompt text. But action-moment lessons —
ones whose true retrieval cue is a specific CLI command or MCP tool call about
to be executed — systematically fail to surface because the encoding cue is
absent from the prompt. This is encoding specificity (Tulving & Thomson 1973):
retrieval requires reinstatement of the encoding context.

THREE-WAY TAXONOMY OF LESSON RETRIEVAL CUES (forum thread #135):
  Locus 1 — Bash-command-cued: cue is a shell command shape.
             Caught by this hook (matches against the Bash command string).
  Locus 2 — MCP-tool-cued: cue is an MCP tool invocation.
             Caught by this hook (matches against tool_name + serialized args).
  Locus 3 — Semantic-content-cued: cue is the content of an assertion, not any
             tool call. Not catchable at PreToolUse; stays with prompt-time hook.

HOW THIS HOOK WORKS
===================
Fires on every Bash or MCP-tool PreToolUse event. Queries active lessons AND
cornerstones that have a `situation_pattern` field in their metadata JSON
(#1691 — cornerstones nudge via `scaffolding_nudge` or `anchor_line`). For Bash calls, matches
the pending command against each pattern. For MCP calls, matches against
"{tool_name} {json(args)}" — so a pattern like `engram_add_observation` fires
before that specific MCP tool call. For any match, injects the lesson's
`scaffolding_nudge` as additionalContext before the tool call runs.
Complements (does not replace) the prompt-time recall hook.

TIER: T2 (Convenience) — degrades gracefully on DB unavailability; never blocks.
Issues: #1203 (Bash/locus-1) + #1297 (MCP/locus-2) — lesson-tripwire encoding-specificity gap.
#2858 — the harness previews only the first ~1964-2016 bytes / ~1964-2000
characters of a hook's additionalContext inline (composite rule measured
across two seats: min(2000 chars, last complete line under ~2048 bytes)); a
heavily-loaded seat with many matching patterns silently lost most nudges
past that cut, with no signal anything was dropped — and since the harness
truncates the TAIL of the whole additionalContext string, a trailer placed
at the END of this hook's own block was cut right along with the nudges on
exactly the large captures this issue is about. Fixed by ordering hits by
specificity, rendering the trailer FIRST (so it survives the tail-cut even
when everything after it doesn't), capping the fully-rendered block
(trailer + header + bullets) to a character ceiling below the harness's own
cut, persisting the overflow to a file, and always emitting a
"N shown · M cut" trailer on the tripwire block.

#2863 WS2 — this hook's own bespoke render mechanism (formerly
select_and_cap_hits / _render_tripwire_block / _build_trailer / its own
_placeholder_overflow_path) was the REFERENCE IMPLEMENTATION
`_hooklib.emit_context()` was generalized FROM (see that function's own
docstring). This hook has now been folded onto the shared helper: the
actual rendering (trailer + header + bullets, capped to
TRIPWIRE_BLOCK_CHAR_CEILING as `target`, never exceeding the harness's hard
`budget`) is emit_context()'s job now, called from main() below.
write_tripwire_overflow() is unchanged and still owns the on-disk overflow
format (node_id-keyed, one block per cut hit) — it is invoked from a small
overflow_writer adapter that reconstructs the cut hit dicts BY POSITION
from emit_context()'s own selection (see main()'s own comments at the
call site for why position, not string value).

#2753 WS4 — the "[triggered principles]" chain rendered above (WS2) is
now the subject of a pre-registered experiment (issue #2753 comments
5602340998 + 5602392744): does rendering that chain change whether an
agent walks past the nudge? Gated by `experiments.ws4_chain_ab` in
config.json (see `_ws4_config()`); OFF by default and OFF whenever the
gate config is missing/malformed/out-of-window (CONTRACT A — inactive
means byte-identical stdout/exit-code to a build with none of this code,
and no ledger file touched at all). When ACTIVE, every tripwire fire (a
fire = check_command() returning at least one hit) is independently
coin-flipped — chain rendered or withheld — via a seeded sha256 draw over
a per-fire nonce (the pending call's tool_use_id when present, else a
fresh uuid4), and exactly one JSONL row is appended to
`$ENGRAM_HOME/tripwire-fire-ledger.jsonl` recording the draw inputs, the
assigned arm, and the rendered/would-render chain length — the fire
ledger the pre-registration assumed already existed but didn't (see
`_ws4_config()` / `_append_ws4_fire_ledger()` docstrings for schema). The
`arm` field is one of `"chain"`, `"control"`, `"na"` (not participating —
no chain to render either way), or -- round 2, reviewer-fairy suggestion
#1 on PR #3166 -- `"error"`: arm computation and ledger-row construction
each get their OWN narrow guard now (not one broad catch-all around both
plus the write), so a bug in either still leaves a visible row behind
(`arm == "error"` plus an `error` string) instead of silently dropping
the fire from the experiment's denominator with zero signal.
"""
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# A RELATIVE ENGRAM_HOME is refused (alpha #3863 6b, step 3a): it would resolve against this process's cwd, a
# different home than the daemon's or the server's. Fail open: one stderr line, nothing written, exit 0.
# (Inline, not from _hooklib: this hook must still run when the sibling lib cannot import. engram_home() is a
# VERBATIM copy of tools/_owner_read.engram_home(), pinned by tests/test_engram_home_parity_3863.py.)
def engram_home() -> Path:
    """The ENGRAM home: $ENGRAM_HOME (default ~/.engram) when set and non-empty, expanded and absolute;
    a RELATIVE value is refused (ValueError naming it). Verbatim pinned copy (see the comment above)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


try:
    _ENGRAM_HOME_PATH = engram_home()
except ValueError as _exc:
    sys.stderr.write("engram hook: %s; doing nothing\n" % (_exc,))
    sys.exit(0)
if os.environ.get("ENGRAM_HOME"):
    os.environ["ENGRAM_HOME"] = str(_ENGRAM_HOME_PATH)  # `~/x` -> absolute for every raw reader

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import atomic_write_json, resolve_runtime_dir, emit_context, write_route_ledger, tool_fire_key
from _hooklib import import_owner_flags as _hooklib_import_owner_flags
# #3628: this hook runs on EVERY tool call. Every file open goes through these
# non-blocking, regular-file-only helpers (a FIFO makes a plain open() wait for
# a peer and the harness kills the hook silently); a non-regular target raises
# OSError, which every handler below already swallows (`except Exception`).
from _hooklib import (
    open_regular_text, open_regular_text_append, open_regular_text_write,
    read_regular_text,
)
from _hooklib import (
    append_shown_record, build_exclusion_resolver, in_context_drop_ids,
    in_context_exclusion_enabled,
)
# #3628: every mode=ro sqlite connect goes through this (refuses a FIFO or
# directory instead of hanging; raises a sqlite3.OperationalError + OSError).
from _hooklib import connect_regular_ro
# #3521 row 9: a crash reaching main()'s outer handler must not look like
# "no lesson matched" (see the handler at the bottom of main()).
from _hooklib import (
    mark_probe_notice, probe_notice_due, probe_unknown_line,
    write_probe_unknown_stderr,
)
from _harness import detect_harness, get_harness_budgets
# #3613: meaningful-span marker. Fail-open: an older/absent helper degrades to
# "no marker" (the pre-change behaviour), never an import crash.
try:
    from _hooklib import marker_recall_query
except Exception:
    def marker_recall_query(text):
        return ("none", text)

ENGRAM_HOME = str(_ENGRAM_HOME_PATH)
DB_PATH = Path(ENGRAM_HOME) / "knowledge.db"
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")

# ---------------------------------------------------------------------------
# U6b stage 2c-1 (#3665) -- the flagged read through the engramd owner
# ---------------------------------------------------------------------------
# With `engramd.lesson_tripwire_reads_via_owner` true in $ENGRAM_HOME/config.json
# (read ONLY through engramd_owner_flags.flag_on), the four read sites of this
# hook -- load_tripwires, render_triggered_principles (the chain), the prevention
# load in load_prevention_lessons, and check_in_turn_recall (its COUNT and
# engram_idf.extract_keywords) -- read through ONE engramd_client.OwnerReadConnection
# shared by the whole fire, and never open knowledge.db. Flag OFF is the direct
# path, unchanged. Modelled on U6a (engram-stop-hook.py).
#
# Owner failure (any OwnerReadError, or the client failing to import): the failing
# site returns its EMPTY result for this fire (never a deny, never presented as "no
# tripwires matched"), the first failure is recorded, and main() adds ONE loud
# notice to the tripwire block's head, rate-limited through the shared probe-notice
# stamp under kind `tripwire-owner`. A failed flag helper reads OFF and is reported
# (stderr line every run; the same line once per window under `tripwire-owner-flag`).

# The hook is registered with a 3 s timeout (hooks/hooks.json): the whole fire's owner
# time is bounded at 2.0 s (3 s minus interpreter start and the rest of the fire), each
# read at 1.0 s, both end-to-end (the shim re-arms every read against the remaining budget).
OWNER_FIRE_BUDGET_S = 2.0
OWNER_READ_TIMEOUT_S = 1.0

_OWNER_FLAG_KEY = "lesson_tripwire_reads_via_owner"
OWNER_NOTICE_KEY = "tripwire-owner"
OWNER_FLAG_NOTICE_KEY = "tripwire-owner-flag"

# Set by main() for ONE run (None outside main(): standalone callers of the sites stay
# on the direct path). {"on": bool, "conn": shim or None, "failure": dict or None}.
_OWNER_RUN = None

# Set by _owner_reads_enabled() when the flag helper fails at import or at `flag_on`
# (class NAME only, never the message); reset at the top of every call.
_OWNER_FLAGS_FAILURE = None


class _ClientImportFailure(Exception):
    """engramd_client could not be imported. args = (cause class name,)."""


def _import_owner_flags():
    """Thin alias (#3665): the body lives once in `_hooklib.import_owner_flags`; the name
    stays a module attribute because callers and tests patch it."""
    return _hooklib_import_owner_flags(ENGRAM_HOME)


def _owner_flags_failure_line():
    """The one report line, or None when no failure is recorded / anything goes wrong."""
    if _OWNER_FLAGS_FAILURE is None:
        return None
    try:
        cls = "".join(ch if ch.isprintable() else "?" for ch in str(_OWNER_FLAGS_FAILURE))[:80]
        return f"engram-lesson-tripwire-hook: owner flag helper failed: {cls}"
    except Exception:
        return None


def _owner_reads_enabled() -> bool:
    """`engramd.lesson_tripwire_reads_via_owner`, read ONLY through
    engramd_owner_flags.flag_on (one definition shared with the owner launcher); anything
    but the boolean `true` is OFF, the direct path. ANY exception (and a SystemExit)
    importing the helper or calling `flag_on` reads OFF -- the hook cannot know the flag is
    ON -- and its CLASS NAME (never the message) is recorded in `_OWNER_FLAGS_FAILURE`
    (reset on every call) for main() to report. KeyboardInterrupt is not caught."""
    global _OWNER_FLAGS_FAILURE
    _OWNER_FLAGS_FAILURE = None
    try:
        engramd_owner_flags = _import_owner_flags()
        # `is True`: only the JSON boolean true is ON; a truthy non-bool must not read as ON.
        return engramd_owner_flags.flag_on(_OWNER_FLAG_KEY, CONFIG_PATH) is True
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- see docstring
        try:
            _OWNER_FLAGS_FAILURE = type(exc).__name__
        except Exception:
            _OWNER_FLAGS_FAILURE = "unknown"
        return False


def _import_engramd_client():
    """Lazy import (flag OFF must never import it), resolved layout-agnostically via the
    shared resolver -- plugin root first, then a walk-parents search; never a fixed
    parents[N] (project CLAUDE.md, "Plugin build restructures code paths")."""
    runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engramd_client.py")
    # Position 0 even if already present further down: an earlier insertion must not let
    # another directory win this import.
    sys.path[:] = [runtime_dir] + [p for p in sys.path if p != runtime_dir]
    import engramd_client
    return engramd_client


def _owner_active() -> bool:
    return _OWNER_RUN is not None and bool(_OWNER_RUN.get("on"))


def _owner_conn():
    """The ONE OwnerReadConnection of this fire, created lazily at first use and shared by
    all four read sites. Raises _ClientImportFailure when the client cannot be imported."""
    run = _OWNER_RUN
    if run["conn"] is None:
        try:
            ec = _import_engramd_client()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 -- any import failure is an owner failure; KeyboardInterrupt is not caught
            raise _ClientImportFailure(type(exc).__name__) from exc
        run["conn"] = ec.OwnerReadConnection(
            total_budget=OWNER_FIRE_BUDGET_S, per_call_timeout=OWNER_READ_TIMEOUT_S)
    return run["conn"]


def _focused_read_conn():
    """(build, conn) for the in-context resolver's one store read, `_hooklib._focused_ids`
    (#3710, U8). Flag OFF: (True, None), the direct path as before. Flag ON: (True, the
    fire's owner connection). Flag ON but the client cannot be imported: the failure is
    recorded (one loud notice) and (False, None): the caller builds NO resolver (drops
    nothing), because the alternative is a direct open on a flag-ON fire."""
    if not _owner_active():
        return True, None
    try:
        return True, _owner_conn()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- KeyboardInterrupt is not caught
        _record_owner_failure("focused_ids", exc)
        return False, None


def _is_owner_exc(exc: BaseException) -> bool:
    return isinstance(exc, _ClientImportFailure) or (
        type(exc).__name__ == "OwnerReadError" and hasattr(exc, "kind"))


def _record_owner_failure(site: str, exc: BaseException) -> bool:
    """Record the FIRST owner failure of this run (kind, the typed Unavailable/Rejected
    result, the cause CLASS name only). Returns True when `exc` was an owner failure (even
    if an earlier one is already kept); False leaves the caller to treat it as it always did."""
    if not _owner_active() or not _is_owner_exc(exc):
        return False
    run = _OWNER_RUN
    if run["failure"] is None:
        if isinstance(exc, _ClientImportFailure):
            kind, result, cause = "client_import", None, (exc.args[0] if exc.args else "unknown")
        else:
            kind, result = exc.kind, getattr(exc, "result", None)
            cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else None
        run["failure"] = {"site": site, "kind": kind, "result": result, "cause": cause}
    return True

# ---------------------------------------------------------------------------
# #2858 -- tripwire-block preview cap (harness-truncation defense)
# ---------------------------------------------------------------------------
# Claude Code's harness only shows a preview of a PreToolUse hook's
# additionalContext INLINE; everything past that is spilled to a file the
# agent must deliberately open, and usually doesn't. When many tripwire
# patterns match one command (a heavily-loaded seat, or a broad pattern),
# this hook used to emit ALL hits in DB-query order with no cap -- so most
# nudges were silently truncated out of context, with no signal anything
# was cut at all.
#
# Round 2 (this fix): two colleagues (Borges, Ariadne) independently
# measured the ACTUAL harness truncation boundary on real captures from live
# sessions: it lands around 1964-2016 bytes / 1964-2000 characters (two
# measurement rounds, reconciled to a composite rule: min(2000 characters,
# last complete line under ~2048 bytes)). 1800 characters on the FULLY
# RENDERED block (trailer + header + bullets, exactly as emitted -- not just
# the bullet lines in isolation) sits safely under that boundary with
# measured margin: worst-case byte expansion ratio ~1.008 for this file's
# actual nudge text gives ~1814 bytes at 1800 chars -- ~200 bytes of
# headroom under the 2016-byte observed ceiling. That headroom is a reserved
# margin for JSON-envelope overhead and whatever other hook blocks may
# precede this one on the same tool call -- not a value to treat as exact;
# if the envelope/other-blocks overhead is measured precisely later, retune
# this constant rather than treating it as derived science.
#
# #2863 WS2: this is now passed as `target` to `_hooklib.emit_context()`
# (see main()), which owns the actual capping/trailer-rendering mechanism
# this hook's own bespoke select_and_cap_hits/_render_tripwire_block/
# _build_trailer used to implement directly. The ceiling VALUE and its
# margin reasoning above are unchanged.
TRIPWIRE_BLOCK_CHAR_CEILING = 1800

# #2863 WS2: the fixed header line emit_context()'s `head` argument renders
# right after the trailer -- extracted as a module constant (was previously
# inlined inside _render_tripwire_block) so main() and this module's own
# tests share exactly one copy of the literal text.
TRIPWIRE_HEADER = "[lesson-tripwire] Action-moment pattern matched — remember:"

# #3485: every json_extract reads through CASE WHEN json_valid(metadata) THEN
# metadata END. A non-JSON blob in nodes.metadata otherwise makes SQLite abort
# the WHOLE query ("malformed JSON"), and load_tripwires' `except Exception:
# return []` then silences the tripwire layer for EVERY lesson, not just the
# bad row. The guarded read yields NULL for a bad blob, so only that row drops
# out. (Plain SQL text, no helper: the hook cannot import server code.)
_QUERY = """
SELECT
    id,
    COALESCE(
        json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.scaffolding_nudge'),
        json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.anchor_line'),
        json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.surfacing_nudge'),
        claim
    ) AS nudge,
    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern')  AS pattern,
    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.tripwire_priority')  AS priority,
    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.match_scope')  AS match_scope,
    recall_summary,
    claim
FROM nodes
WHERE type IN ('lesson', 'cornerstone', 'axiom', 'goal')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern') IS NOT NULL
  AND json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern') != ''
"""


# ---------------------------------------------------------------------------
# #3503 -- make the loaders' fail-soft `except` VISIBLE
# ---------------------------------------------------------------------------
# Both loaders below end in `except Exception: return []`, so a caller could not
# tell "no lessons" from "the loader failed" (a locked file past the timeout, a
# missing table after a partial migration, a file that is not a database all
# looked identical to an empty lesson set). The loaders still return [] and
# never raise (fail-soft is the hook's whole contract); the failure is now also
#   1. recorded in LOADER_FAILURES (reset at the top of every main() run),
#   2. written as ONE sanitized stderr line (the `route-ledger:` convention), and
#   3. surfaced to the agent as ONE bounded line inside the tripwire block
#      (rendered by emit_context, see main()), at most once per
#      LOADER_NOTICE_WINDOW_SECONDS, tracked by a small stamp file.
LOADER_FAILURES: list = []
LOADER_NOTICE_WINDOW_SECONDS = 1800
# State file beside the hook's other state (ENGRAM_HOME, like the overflow dir
# and the ledgers). Module constant so a test can point it elsewhere.
LOADER_NOTICE_STAMP_PATH = Path(ENGRAM_HOME) / "lesson-tripwire-loader-notice.json"
_LOADER_NAMES = ("load_tripwires", "load_prevention_lessons")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_exc_text(text: str, limit: int) -> str:
    """Each control character (newline included) -> one space, then cut to
    `limit` characters, so an exception message is always one bounded line."""
    return _CONTROL_CHARS_RE.sub(" ", text)[:limit]


def _record_loader_failure(loader: str, exc: BaseException) -> None:
    """Record one loader failure and write exactly one stderr line:
    `lesson-tripwire: <loader> failed: <ExcType>: <message>` (message
    sanitized, <= 200 chars; whole line <= 300). Never raises."""
    try:
        etype = _sanitize_exc_text(type(exc).__name__, 80)
        try:
            msg = _sanitize_exc_text(str(exc), 200)
        except Exception:
            msg = "<unprintable exception message>"
        LOADER_FAILURES.append({"loader": loader, "exc_type": etype, "message": msg})
        sys.stderr.write(f"lesson-tripwire: {loader} failed: {etype}: {msg}"[:300] + "\n")
    except Exception:
        pass


def _loader_notice_now() -> float:
    return time.time()


def _loader_notice_due(now: float) -> bool:
    """True unless a notice was shown within the last window. An absent,
    unreadable, corrupt, wrongly-shaped or future-dated stamp is "due": the
    rate limit must fail VISIBLE, never silent. Never raises."""
    try:
        shown_at = json.loads(read_regular_text(LOADER_NOTICE_STAMP_PATH, encoding="utf-8"))["shown_at"]
        if isinstance(shown_at, bool) or not isinstance(shown_at, (int, float)):
            return True
        return not (0 <= now - shown_at < LOADER_NOTICE_WINDOW_SECONDS)
    except Exception:
        return True


def _mark_loader_notice_shown(now: float) -> None:
    """Start a new rate-limit window. Never raises (an unwritable stamp just
    means the next failure shows the line again)."""
    try:
        LOADER_NOTICE_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(LOADER_NOTICE_STAMP_PATH, {"shown_at": now})
    except Exception:
        pass


def _pending_loader_notice():
    """Return (line, now): the one visible line to inject this run, or ("", None).

    A recorded failure of EITHER loader is shown (rate-limited), including a
    prevention-only failure with no tripwire hits: the hook never blocks on an
    infrastructure failure, so a failed prevention load leaves the blocking
    gate off for the run and this line is the only visible signal. With no
    hits the line is the whole block (main renders it through emit_context).
    The caller writes the rate-limit stamp only after the response carrying
    the line has been printed.
    Shape: `[lesson-tripwire] <loaders> failed (<ExcType>); tripwires or
    blocking lessons may be missing this prompt`, <= 200 chars, no exception
    message text."""
    try:
        if not LOADER_FAILURES:
            return "", None
        names = [n for n in _LOADER_NAMES if any(f["loader"] == n for f in LOADER_FAILURES)]
        now = _loader_notice_now()
        if not _loader_notice_due(now):
            return "", None
        types: list = []
        for f in LOADER_FAILURES:
            if f["exc_type"] not in types:
                types.append(f["exc_type"])
        types_s = "/".join(t[:40] for t in types)[:80]
        line = f"[lesson-tripwire] {', '.join(names)} failed ({types_s}); tripwires or blocking lessons may be missing this prompt"
        return line[:200], now
    except Exception:
        return "", None


# #3665 -- the owner-failure notice. Two parts as in U6a: what the agent can do itself, and
# what to tell the human. Dynamic text is bounded and control-character-free; the exception
# CLASS of a client failure is shown, never its message, SQL text or row data.
_OWNER_NOTICE_CAP = 1400
_OWNER_FLAG_OFF_STEP = (
    "To go back to the direct-read path meanwhile, set "
    "engramd.lesson_tripwire_reads_via_owner to false in $ENGRAM_HOME/config.json."
)
_OWNER_AGENT_BASE = [
    "Do NOT read knowledge.db directly to work around this; tell the human what is shown here.",
    "Do not retry in a loop: this notice repeats at most once per window.",
]


def _notice_lines(title: str, items) -> str:
    return "  " + title + ":\n" + "\n".join(
        f"    - {_sanitize_exc_text(str(i), 200)}" for i in items)


def _owner_failure_notice(failure: dict) -> str:
    """The loud text for one recorded owner failure. Never raises beyond what the caller
    guards; bounded to _OWNER_NOTICE_CAP characters."""
    site, kind, result = failure["site"], failure["kind"], failure.get("result")
    cause = _sanitize_exc_text(str(failure.get("cause") or ""), 80)
    head = (
        f"[lesson-tripwire] ENGRAM UNAVAILABLE: tripwires and lessons {COULD_NOT_READ} this fire "
        f"(the engramd owner read failed at {site}: {kind}). "
        'This is NOT "no tripwires matched": the check could not run, so a tripwire or a '
        "blocking lesson may have been missed. No tool call was blocked."
    )
    lines = [head]
    agent = list(_OWNER_AGENT_BASE)
    human = []
    if kind == "unavailable" and result is not None:
        lines.append(
            f"  Socket: {_sanitize_exc_text(str(result.socket_path), 200)} "
            f"(bound {OWNER_READ_TIMEOUT_S}s per read, {OWNER_FIRE_BUDGET_S}s per fire)")
        agent += list(result.agent_steps)
        human += list(result.human_steps)
    elif kind == "rejected" and result is not None:
        lines.append(f"  Reason: {_sanitize_exc_text(str(result.reason), 200)}")
        lines.append(f"  Remedy: {_sanitize_exc_text(str(result.remedy), 200)}")
        lines.append("  This refusal is not retryable: repeating the same request cannot change the answer.")
        human.append("Act on the Remedy line above (typically: register the template / restart the owner "
                     "on a matching version).")
    elif kind == "unusable_answer":
        lines.append("  The owner returned an answer of the wrong shape.")
        human.append("Restart the engramd owner on the current version and report this if it persists.")
    elif kind == "budget_exhausted":
        lines.append(f"  The fire's owner time budget ({OWNER_FIRE_BUDGET_S}s) ran out before this read.")
        human.append("Check why the owner answers slowly (engramd logs, host load).")
    elif kind == "client_import":
        lines.append(f"  The engramd client module (engramd_client.py) could not be loaded (cause: {cause}).")
        human.append("Check that engramd_client.py exists at the plugin root (the directory above hooks/); "
                     "re-run the plugin install or upgrade if it is missing.")
    else:  # client_failure and anything a future client adds
        lines.append(f"  The engramd client failed while reading (cause: {cause or kind}).")
        human.append("This is a client-side bug: report it with the cause line above.")
    human.append(_OWNER_FLAG_OFF_STEP)
    lines += [_notice_lines("Agent steps", agent), _notice_lines("Human steps", human)]
    return "\n".join(lines)[:_OWNER_NOTICE_CAP]


COULD_NOT_READ = "could NOT be read"


def _pending_owner_notice():
    """Return (text, kind): the one owner-side line to inject this run, or ("", None).
    A recorded owner failure -> the loud notice under `tripwire-owner`; a failed flag helper
    -> its report line under `tripwire-owner-flag` (the two cannot co-occur: a failed helper
    reads the flag OFF). Both ride the shared probe-notice window, so a repeat inside the
    window is silent on stdout. The caller stamps the window only after the response was
    printed and flushed. Never raises."""
    try:
        run = _OWNER_RUN
        if run is not None and run.get("failure") is not None:
            text, key = _owner_failure_notice(run["failure"]), OWNER_NOTICE_KEY
        else:
            text, key = _owner_flags_failure_line(), OWNER_FLAG_NOTICE_KEY
        if not text or not probe_notice_due(ENGRAM_HOME, key):
            return "", None
        return text, key
    except Exception:
        return "", None


def _write_owner_stderr_lines() -> None:
    """The per-run stderr lines (every run, class/kind only): the failed flag helper, and the
    first owner failure. Never raises."""
    try:
        line = _owner_flags_failure_line()
        if line is not None:
            sys.stderr.write(line + "\n")
        run = _OWNER_RUN
        if run is not None and run.get("failure") is not None:
            f = run["failure"]
            sys.stderr.write(
                f"engram-lesson-tripwire-hook: owner read failed: {f['kind']} at {f['site']}\n"[:300])
        sys.stderr.flush()
    except Exception:
        pass


# alpha#3920 (part of #3911): which view of the call a lesson's pattern runs against.
#   "command"  the call with heredoc bodies / prose-field values hidden (opt-in, per lesson)
#   "full"     today's raw target, byte for byte (the default, and what a lesson with NO
#              match_scope gets: an unaudited seat must not silently lose a body-text lesson)
# Anything else in the metadata is treated as "full" (a typo must not blind a lesson).
MATCH_SCOPES = ("command", "full")
DEFAULT_MATCH_SCOPE = "full"


def _coerce_match_scope(value) -> str:
    """NULL/absent -> the default view; a known value -> itself; anything else -> "full"."""
    if value is None:
        return DEFAULT_MATCH_SCOPE
    return value if value in MATCH_SCOPES else "full"


def load_tripwires(db_path: Path = DB_PATH):
    """Return list of (pattern_str, nudge_str, node_id, priority_str, match_scope_str,
    recall_summary, claim) from active lessons with situation_pattern. (alpha#3920 added the
    5th element, "command" or "full"; alpha#3924 added the 6th and 7th, the node's own
    recall_summary and claim, for the cluster block's one-liners; callers unpack the tail
    variadically.)

    #2753 epic WS2: node_id was added (3rd tuple element, minimal extension
    of the pre-existing (pattern, nudge) shape) so check_command's hits can
    carry the firing node's id for generalization_chain() lookups, without
    touching what the existing nudge block renders.

    alpha#3270/#3112: priority was added (4th tuple element) carrying the
    lesson's tripwire_priority metadata field -- "high" or "normal", NULL
    on every lesson minted before this field existed. Coerced to "normal"
    here (not left as None) so every downstream consumer can compare by
    string equality without an extra None-check; a lesson with no opinion
    sorts exactly like a lesson explicitly marked "normal"."""
    # #3665: with the owner flag ON the fire's shared shim stands in for the connection and
    # knowledge.db is never opened (no existence check of the local file either: the store
    # belongs to the owner). An OWNER failure is recorded for the loud notice instead of as a
    # loader failure; the result is EMPTY either way. One execute site, so the read-template
    # registry lists this function once.
    owner = _owner_active()
    if not owner and not db_path.exists():
        return []
    try:
        conn = _owner_conn() if owner else connect_regular_ro(db_path, timeout=2.0)
        rows = conn.execute(_QUERY).fetchall()
        if not owner:
            conn.close()
        return [
            (row[2], row[1], row[0], row[3] or "normal", _coerce_match_scope(row[4] if len(row) > 4 else None),
             row[5] if len(row) > 5 else None, row[6] if len(row) > 6 else None)
            for row in rows if row[1] and row[2]
        ]
    except Exception as exc:
        if not (owner and _record_owner_failure("load_tripwires", exc)):
            _record_loader_failure("load_tripwires", exc)
        return []


def check_command(match_target: str, tripwires, command_target: str | None = None) -> list:
    """Return list of {"nudge": str, "node_id": str, "pattern": str,
    "priority": str} dicts for every situation_pattern that matches
    match_target.

    #2753 epic WS2: hits carry node_id (in addition to the pre-existing
    nudge text) so the caller can compute generalization_chain() per hit for
    the additive "[triggered principles]" section, without changing what
    the existing nudge block renders (main() reads hit["nudge"] where it
    used to read the hit string directly).

    #2858: hits ALSO carry the firing pattern itself now (3rd-then-4th
    field, minimal extension of the same shape) -- purely so main() can
    order hits by specificity (len(pattern), descending) before rendering
    and capping, per the preview-truncation fix above. This does NOT change
    the matching logic or the circumstances under which a hit fires -- still
    exactly one hit per matching pattern.

    alpha#3270/#3112: `tripwires` entries are unpacked with a variadic tail
    (`*rest`) rather than a strict 4-tuple, so every existing caller that
    constructs bare (pattern, nudge, node_id) 3-tuples by hand (test
    fixtures, mocked load_tripwires) keeps working unchanged -- priority
    defaults to "normal" when the 4th element is absent, identical to what
    load_tripwires() itself now coerces a NULL column to. Only
    load_tripwires()'s own real DB-reading path needs the 4th element;
    nothing about the matching logic changes."""
    hits = []
    for pattern, nudge, node_id, *rest in tripwires:
        priority = rest[0] if rest else "normal"
        # alpha#3920: 5th element = match_scope. A lesson runs on the command view only when the
        # caller supplied one AND the lesson's scope is "command" (opt-in; a bare tuple gets "full");
        # a caller that passes no command_target keeps today's behaviour for every lesson.
        scope = rest[1] if len(rest) > 1 else DEFAULT_MATCH_SCOPE
        target = command_target if (command_target is not None and scope == "command") else match_target
        try:
            if re.search(pattern, target, re.IGNORECASE):
                hits.append({
                    "nudge": nudge, "node_id": node_id, "pattern": pattern,
                    "priority": priority,
                })
        except re.error:
            pass  # malformed pattern — skip silently
    return hits


def _tripwire_bullet(hit: dict) -> str:
    """Render one hit's bullet line -- the exact per-item body string
    emit_context() renders, one per shown hit (was previously inlined
    inside the now-removed _render_tripwire_block). Extracted as its own
    function so main() and this module's tests share one copy of the
    format."""
    return f"  • {hit['nudge']}"


# #2863 WS2: select_and_cap_hits / _render_tripwire_block / _build_trailer /
# this module's own _placeholder_overflow_path (the bespoke mechanism
# `_hooklib.emit_context()` was generalized FROM -- see that function's
# docstring) are REMOVED. main() below calls emit_context() directly for
# both the selection (shown vs. cut) AND the rendering (trailer + header +
# bullets) -- one call, one source of truth, replacing four separate
# hand-written functions that had to be kept in lockstep by hand. The
# empirical equivalence (byte-identical selection + rendering on every
# fixture this file's own test suite exercises, including the
# always-show-at-least-one-hit edge case) was verified before this removal
# landed -- see the PR description for the comparison.


def write_tripwire_overflow(cut_hits: list, now: float | None = None) -> str:
    """Persist the full node_id + nudge text of every hit emit_context()
    cut (or, for the rare item-alone-exceeds-the-hard-budget case, marked
    truncated-in-band -- see main()'s own overflow_writer adapter), so the
    trailer/marker in the visible tripwire block can point somewhere real
    instead of leaving the reader with no way to recover what was dropped.

    #2858 Piece 2: written to ENGRAM_HOME/tripwire-overflow/<ts>-<pid>.txt (a
    NEW directory, created on demand). File name is deterministic enough for a
    test to reconstruct the exact path given the same `now` + pid. `now`
    defaults to time.time() but accepts an injected value so tests can assert
    on an exact resulting path.

    Never raises -- like every other helper in this file, a broken overflow-
    file write must never suppress the nudges that DID make it into the
    visible block, and must never crash the hook (this hook's whole contract,
    per the module docstring, is "never blocks"). Returns "" on any failure,
    including an empty cut_hits list (nothing to write).
    """
    if not cut_hits:
        return ""
    try:
        if now is None:
            import time as _time
            now = _time.time()
        overflow_dir = Path(ENGRAM_HOME) / "tripwire-overflow"
        overflow_dir.mkdir(parents=True, exist_ok=True)
        overflow_path = overflow_dir / f"{int(now)}-{os.getpid()}.txt"
        blocks = [
            f"[{hit.get('node_id', '?')}] {hit.get('nudge', '')}"
            for hit in cut_hits
        ]
        with open_regular_text_write(overflow_path, encoding="utf-8") as f:
            f.write("\n\n".join(blocks) + "\n")
        return str(overflow_path)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# alpha#3924 (part of #3911) -- cluster block for co-firing lessons
# ---------------------------------------------------------------------------
# When MORE than CLUSTER_THRESHOLD lessons match one call (on 10-18 one publish call fired 19),
# the inline budget showed about one nudge in full and cut the rest to an overflow filename. The
# co-firing lessons share a MOMENT, not a subject, so the display is grouped, never the claims
# merged: one header, ONE LINE PER MATCHED LESSON (id + recall_summary), and ONE full nudge. Every
# full nudge still goes to the overflow file. At or below the threshold nothing changes.
CLUSTER_THRESHOLD = 3
LESSON_RANKS_PATH = Path(ENGRAM_HOME) / "lesson-fire-ranks.json"
RANKS_MAX_AGE_DAYS = 7          # an older rank file is not trusted: length order for all
RANKS_FUTURE_SKEW_DAYS = 1      # a generated_at this far ahead (clock skew) counts as age 0
SUMMARY_CLAIM_CUT = 140
SUMMARY_LINE_CAP = 200  # a legacy or hostile oversized recall_summary must not cut other lessons' lines
NO_SUMMARY = "(no summary)"


def _ranks_now() -> float:
    """Injectable clock for the rank file's age (tests patch this; nothing sleeps)."""
    return time.time()


def _read_rank_table() -> tuple:
    """Read $ENGRAM_HOME/lesson-fire-ranks.json ONCE. Returns (ranks, reason, age_days):
    ranks = {lesson id: fires per 1,000 targets} and reason None when the file is usable;
    otherwise ({}, reason, None) with reason one of "no file", "unreadable", "malformed",
    "stale". Never raises: every failure means length order for all lessons, never a block."""
    try:
        text = read_regular_text(LESSON_RANKS_PATH, encoding="utf-8")
    except FileNotFoundError:
        return {}, "no file", None
    except Exception:
        return {}, "unreadable", None
    try:
        doc = json.loads(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("ranks"), dict):
            return {}, "malformed", None
        stamp = doc.get("generated_at")
        if not isinstance(stamp, str):
            return {}, "malformed", None
        generated = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_s = _ranks_now() - generated.timestamp()
    except Exception:
        return {}, "malformed", None
    if age_s < -RANKS_FUTURE_SKEW_DAYS * 86400:
        return {}, "malformed", None
    age_s = max(age_s, 0.0)
    if age_s > RANKS_MAX_AGE_DAYS * 86400:
        return {}, "stale", None
    ranks = {}
    for nid, rate in doc["ranks"].items():
        if (isinstance(nid, str) and isinstance(rate, (int, float)) and not isinstance(rate, bool)
                and math.isfinite(rate) and rate >= 0):
            ranks[nid] = float(rate)
    return ranks, None, int(age_s // 86400)


def _cluster_key(h: dict, ranks: dict) -> tuple:
    """Sort key for one hit of a cluster (see _cluster_order for the rule)."""
    nid = h.get("node_id") or ""
    rated = nid in ranks
    return (
        0 if h.get("priority") == "high" else 1,
        0 if rated else 1,
        ranks[nid] if rated else 0.0,
        -len(h.get("pattern") or ""),
        nid,
    )


def _cluster_order(hits: list) -> tuple:
    """Order a cluster's hits and say how. Within one tripwire_priority ("high" first): ascending
    fires-per-1,000 (a lesson that fires on almost every such call carries the least news per fire),
    then longest situation_pattern, then lesson id, so ties are total. A lesson with no rate (absent
    from the file, or the file unusable) sorts AFTER every rated peer of its priority, by length then
    id. Returns (ordered hits, note) where note is the text after `order: `."""
    ranks, reason, age_days = _read_rank_table()
    ordered = sorted(hits, key=lambda h: _cluster_key(h, ranks))
    if reason:
        return ordered, f"length fallback ({reason})"
    unrated = sum(1 for h in ordered if (h.get("node_id") or "") not in ranks)
    return ordered, f"rarity (ranks {age_days}d old, {unrated} unrated)"


def _cluster_summary(recall_summary, claim) -> str:
    """One line: the lesson's recall_summary; else its claim cut to 140 characters; else a
    placeholder. Never empty, never multi-line."""
    summary = " ".join(str(recall_summary).split()) if recall_summary else ""
    if summary:
        return summary[:SUMMARY_LINE_CAP]
    flat_claim = " ".join(str(claim).split()) if claim else ""
    return flat_claim[:SUMMARY_CLAIM_CUT] if flat_claim else NO_SUMMARY


def _cluster_trigger(hook_input: dict) -> str:
    """The tool name (MCP) or the Bash verb (first word of the command) the cluster fired on."""
    tool = hook_input.get("tool_name") or "tool"
    if tool == "Bash":
        ti = hook_input.get("tool_input") or hook_input.get("input") or {}
        words = (ti.get("command") or "").split() if isinstance(ti, dict) else []
        words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        return f"Bash `{words[0]}`" if words else "Bash"
    return tool


def _cluster_head(count: int, trigger: str, note: str, overflow_path: str, nudge: str = "full") -> str:
    """The cluster header. `nudge` says what follows the one-liners (alpha#3955): "full" (today's
    wording, byte-identical), "truncated" (a prefix of the first nudge follows), or "absent" (no room
    for even a prefix; the header must not say "below" then)."""
    tail = f"; all {count} full texts: {overflow_path}" if overflow_path else ""
    what = {
        "full": "full text of the first below",
        "truncated": "first nudge truncated below",
        "absent": "first nudge not shown",
    }[nudge]
    return (f"[lesson-tripwire] {count} lessons matched {trigger} (order: {note}); "
            f"one line each, {what}{tail}:")


CLUSTER_MIN_NUDGE_PREFIX = 40   # a nudge prefix shorter than this is noise: the one-liners win instead


def _cluster_truncate_nudge(bullet: str, keep: int, overflow_path: str) -> str:
    """The first `keep` characters of the head bullet, then ONE marker line. The cut is on a
    character boundary that stays valid text (a lone high surrogate at the cut is dropped)."""
    cut = bullet[:keep]
    if cut and "\ud800" <= cut[-1] <= "\udbff":
        cut = cut[:-1]
    marker = (f"  \u2026[nudge truncated; rest in {overflow_path}]" if overflow_path
              else "  \u2026[nudge truncated]")
    return cut.rstrip("\n") + "\n" + marker


def _with_notice(head, notice) -> str:
    """The head as emit_context renders it: the notice line (when due) above the header line."""
    return f"{notice}\n{head}" if notice else head


def _cluster_fits(head, body, overflow_path, budget, target) -> bool:
    """True iff emit_context (same kwargs main() uses) renders EVERY body item whole."""
    kept: list = []
    emit_context(head, body, budget=budget, target=target, allow_single_overflow=True,
                 overflow_writer=lambda _items: overflow_path, kept_out=kept)
    return len(kept) == len(body)


def _cluster_plan(count, trigger, note, overflow_path, lines, bullet, notice, budget, target):
    """Decide what the cluster block shows of the head nudge (alpha#3955). Returns
    (head, body, nudge_state) with nudge_state "full" / "truncated" / "absent"; `head` is the cluster
    header line WITHOUT `notice` (the caller prepends the notice when it renders; the planner only
    counts it, so its fit matches the real render). The block is
    measured by emit_context itself (same call, same kwargs main() makes), so the plan cannot drift
    from the real render: a candidate is accepted only when EVERY body item comes back whole.
    Order of preference: whole nudge, else the longest prefix that fits, else one-liners only."""
    head = _cluster_head(count, trigger, note, overflow_path, "full")
    if _cluster_fits(_with_notice(head, notice), lines + [bullet], overflow_path, budget, target):
        return head, lines + [bullet], "full"
    head = _cluster_head(count, trigger, note, overflow_path, "truncated")
    lo, hi, best = CLUSTER_MIN_NUDGE_PREFIX, len(bullet) - 1, None
    while lo <= hi:     # the fit is monotonic in the prefix length: the rendered length never shrinks
        # as `keep` grows (a longer prefix is never shorter text) and "fits" means length <= target,
        # so the fitting keeps form a prefix of the range and binary search finds its end.
        mid = (lo + hi) // 2
        body = lines + [_cluster_truncate_nudge(bullet, mid, overflow_path)]
        if _cluster_fits(_with_notice(head, notice), body, overflow_path, budget, target):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return head, lines + [_cluster_truncate_nudge(bullet, best, overflow_path)], "truncated"
    return _cluster_head(count, trigger, note, overflow_path, "absent"), list(lines), "absent"


# ---------------------------------------------------------------------------
# #2753 epic WS2 — generalization-chain render at the tripwire (D3 point 1)
# ---------------------------------------------------------------------------
# ADDITIVE to the existing nudge block above: for each hit this fire, walk
# generalization_chain() on its node id and render one merged, deduped-by-
# head "[triggered principles]" section. Dedupe is scoped to THIS ONE fire
# (Case 1 in the issue's D3) -- no new cooldown/suppression across fires
# (Case 2 is explicitly unchanged, per the issue). Never raises: the call
# site in main() wraps this in its own try/except, matching
# check_in_turn_recall's posture -- a failure here must never suppress the
# existing nudge block, and vice versa.

_GENERALIZATION_CHAIN_FN = None
_GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = False


def _import_generalization_chain():
    """Lazily import generalization_chain from engram_core (#2753 WS1) --
    lazy, like engram_idf's import in check_in_turn_recall below, so this
    hook never crashes at import time if engram_core isn't yet resolvable
    (e.g. a partial/mid-upgrade install). Memoized (including a failed
    lookup) so a missing module doesn't re-pay the resolve+import cost on
    every hit within one process; each hook invocation is a fresh process
    anyway, so the memo only helps within a single fire's multiple hits."""
    global _GENERALIZATION_CHAIN_FN, _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED
    if _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED:
        return _GENERALIZATION_CHAIN_FN
    _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = True
    try:
        runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engram_core.py")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        from engram_core import generalization_chain
        _GENERALIZATION_CHAIN_FN = generalization_chain
    except Exception:
        _GENERALIZATION_CHAIN_FN = None
    return _GENERALIZATION_CHAIN_FN


def _truncate_for_label(text: str, limit: int = 60) -> str:
    """Short, single-line excerpt for the merged-head summary line -- never
    raises on a non-string/None input (defensive: claim/recall_summary are
    both nullable per generalization_chain's own docstring)."""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_triggered_principles(
    hits: list, db_path: "Path | None" = None, heads_out: "dict | None" = None,
) -> str:
    """#2753 epic WS2, D3 point 1: for this ONE tripwire fire, walk each
    hit's generalization_chain() and render ONE additive "[triggered
    principles]" section, deduped by HEAD across all of this fire's hits
    (D3's Case 1 -- within one fire; Case 2, the existing per-node
    recent-id/repeat-count cooldown machinery, is untouched and not this
    function's concern).

    Per head: every non-head node reached (from any hit sharing that head,
    merged into one subgraph, ordered by depth) renders as a one-liner
    (`recall_summary` only -- never `claim`, which is absent on non-head
    entries by generalization_chain's own contract); the head itself
    renders its `claim` IN FULL. When more than one of this fire's
    chain-bearing hits share a head, one line names the convergence
    ("N of these M matches sit under <head_id> (<short label>)") before the
    merged rungs.

    A hit whose own node has NO generalizes ancestor at all (already a head
    with nothing above it -- chain contains only the node itself) renders
    NOTHING for that hit; the section is emitted at all only when at least
    one hit produced a real chain OR at least one hit's chain reported
    `cycle_detected` (see below).

    #2753 WS2 round 2 -- `cycle_detected` (WS1's cycle-guard fix, 5e14f83b):
    a hit whose chain comes back with `cycle_detected: True` has NO valid
    head to render (structurally impossible in a valid DAG -- the resolved
    graph actually contains a cycle). Rendering the normal head/rung
    structure is impossible (there is no head), and silently skipping it
    like the no-ancestor case above would be the exact bug WS1 fixed one
    layer down: an omitted section reads as "no chain exists", which is
    false -- a chain WAS found, it's just corrupt. So a cycle-detected hit
    renders its own one-line malformed-data notice instead, visibly
    distinct from both a normal rendered chain and from omission.

    #2753 WS2 round 3 -- `truncated` (present since WS1's original commit,
    read by this function for the first time): a hit whose chain reports
    `heads: []` and `cycle_detected: False` and `truncated: True` has real
    partial ancestry -- `max_depth` was hit while nodes were still pending
    expansion, so a head genuinely exists above what was walked, it just
    wasn't reached. Falling into the same silent skip as the no-ancestor
    case would misreport "no chain exists" exactly as the cycle_detected
    case would -- a milder instance of the same problem, since the data
    here is incomplete rather than corrupt. So a truncated-with-no-head hit
    renders its own one-line "chain truncated" notice (no rungs shown --
    the walk never reached far enough to have any to show) -- worded and
    marked (`…`, not `⚠`) to read as incomplete-but-honest data rather than
    malformed data, and never both: `cycle_detected` and
    `truncated` are mutually exclusive by generalization_chain's own
    contract (`cycle_detected` requires `truncated: False`), so the two
    checks below never fire on the same hit.

    Returns "" on: no hits, no importable generalization_chain (not consulted when the
    connection is the owner shim, which serves the walk itself, #3740), any DB/walk
    failure, or no hit producing a real chain and no hit reporting
    cycle_detected or a truncated-with-no-head walk. Never raises -- this
    hook's T2 "never blocks" contract (see module docstring) applies to
    this additive section exactly as it does to the pre-existing nudge
    block.

    db_path defaults to None (resolved to the module-level DB_PATH INSIDE
    the function body, not as a mutable default-argument value) so a test
    that monkeypatches DB_PATH after this module is imported is honored on
    the no-arg call path main() uses -- a `db_path: Path = DB_PATH` default
    would instead freeze at the value DB_PATH held at function-definition
    time (import time), same footgun load_tripwires's own default already
    carries (harmless there since every caller passes db_path explicitly).

    heads_out (#2753 WS4): optional caller-supplied dict, mutated IN PLACE
    to map each hit's own node_id -> the list of head ids THAT hit
    contributed to -- the exact `heads` list already used below to update
    heads_seen, never a second independent walk (CONTRACT B: this must
    read off the SAME computation that produces the string this function
    returns, not a walk that could disagree with it). A hit that renders
    nothing here (no ancestor, cycle_detected, or truncated-with-no-head)
    is simply never written to heads_out -- left at whatever the caller
    pre-set (conventionally `[]`) for that node_id. Ignored entirely when
    None (the default); passing it never changes this function's own
    return value.
    """
    if not hits:
        return ""
    if db_path is None:
        db_path = DB_PATH
    generalization_chain = None
    conn = None
    owner = _owner_active()
    if not owner:
        generalization_chain = _import_generalization_chain()
        if generalization_chain is None:
            return ""
    try:
        if owner:
            # #3665: the fire's shared shim; positional rows (no row_factory exists on it).
            conn = _owner_conn()
            if not callable(getattr(conn, "generalization_chain", None)):
                # no owner-served walk on this connection: walk here, as before
                generalization_chain = _import_generalization_chain()
                if generalization_chain is None:
                    return ""
            # else (#3740 R1): the shim serves the walk through the owner; the hook neither
            # imports engram_core nor depends on it being importable in this process.
        else:
            conn = connect_regular_ro(db_path, timeout=1.0)
            conn.row_factory = sqlite3.Row
        # head_id -> {"nodes": {resolved_id: entry, ...}, "hit_ids": [node_id, ...]}
        heads_seen: dict = {}
        head_order: list = []
        relevant_hit_count = 0
        # #2753 WS2 round 2 -- node ids whose own chain() call reported
        # cycle_detected: True (WS1 5e14f83b). Kept separate from
        # heads_seen: a cycle-detected chain has NO head to merge into, by
        # definition (cycle_detected implies heads == []), so it is never a
        # candidate for the head-dedupe machinery above -- it renders its
        # own standalone malformed-data line instead. Dedup by node_id in
        # first-seen order (a hit list should not repeat a node_id in
        # practice, but this stays cheap insurance).
        cycle_detected_ids: list = []
        # #2753 WS2 round 3 -- node ids whose own chain() call reported
        # heads == [] with truncated: True (real ancestry exists, the walk
        # was just cut off by max_depth before reaching a head). Kept
        # separate from heads_seen for the same reason cycle_detected_ids
        # is: there is no head to merge into. Kept separate from
        # cycle_detected_ids too, even though both are "heads == [] and
        # nothing to merge into" -- the two states are mutually exclusive
        # (see docstring) and read differently to a human, so each gets its
        # own notice line and its own dedup list.
        truncated_ids: list = []
        for hit in hits:
            node_id = hit.get("node_id") if isinstance(hit, dict) else None
            if not node_id:
                continue
            # #3740 Cut A: the owner shim carries the whole walk as ONE typed `chain` request
            # (the owner runs engram_core.generalization_chain itself); a plain sqlite
            # connection has no such method and walks as before. An OwnerReadError from the
            # shim propagates to the handler below, like any other owner failure.
            if hasattr(conn, "generalization_chain"):
                chain = conn.generalization_chain(node_id)
            else:
                chain = generalization_chain(conn, node_id)
            nodes = chain.get("nodes") or {}
            start_resolved = chain.get("start_resolved_id")
            heads = chain.get("heads") or []
            if chain.get("cycle_detected"):
                if node_id not in cycle_detected_ids:
                    cycle_detected_ids.append(node_id)
                continue
            if not heads and chain.get("truncated"):
                if node_id not in truncated_ids:
                    truncated_ids.append(node_id)
                continue
            # No generalizes ancestor at all -- already a head with nothing
            # above it. Nothing to render for this hit (D3: "render nothing
            # extra for it; only render the section when there is an
            # actual chain to show").
            if not heads or (len(nodes) <= 1 and start_resolved in nodes):
                continue
            relevant_hit_count += 1
            if heads_out is not None:
                # #2753 WS4 CONTRACT B -- the SAME `heads` list about to be
                # walked below to update heads_seen, not a second walk.
                heads_out[node_id] = list(heads)
            for head_id in heads:
                if head_id not in heads_seen:
                    heads_seen[head_id] = {"nodes": {}, "hit_ids": []}
                    head_order.append(head_id)
                heads_seen[head_id]["hit_ids"].append(node_id)
                heads_seen[head_id]["nodes"].update(nodes)
    except Exception as exc:
        if owner:
            _record_owner_failure("render_triggered_principles", exc)
        return ""
    finally:
        # the owner shim is shared by every site of the fire: not closed here
        if conn is not None and not owner:
            try:
                conn.close()
            except Exception:
                pass

    if not heads_seen and not cycle_detected_ids and not truncated_ids:
        return ""

    lines = ["[triggered principles]"]
    for node_id in cycle_detected_ids:
        lines.append(
            f"  ⚠ {node_id} — principle-chain data is malformed (a cycle "
            "was detected in the resolved generalizes graph); chain not shown"
        )
    for node_id in truncated_ids:
        lines.append(
            f"  … {node_id} — principle chain truncated (max depth reached "
            "before a head was found); further ancestry likely exists but "
            "is not shown"
        )
    for head_id in head_order:
        entry = heads_seen[head_id]
        merged_nodes = entry["nodes"]
        contributing = entry["hit_ids"]
        head_entry = merged_nodes.get(head_id, {})
        if len(contributing) > 1:
            label = _truncate_for_label(head_entry.get("claim") or head_entry.get("recall_summary"))
            lines.append(
                f"  {len(contributing)} of these {relevant_hit_count} matches sit under "
                f"{head_id} ({label})" if label else
                f"  {len(contributing)} of these {relevant_hit_count} matches sit under {head_id}"
            )
        non_head_ids = sorted(
            (nid for nid in merged_nodes if nid != head_id),
            key=lambda nid: merged_nodes[nid].get("depth", 0),
        )
        for nid in non_head_ids:
            summary = merged_nodes[nid].get("recall_summary") or ""
            lines.append(f"  ↑ {nid} — {summary}")
        head_claim = head_entry.get("claim") or ""
        lines.append(f"  ↑ {head_id} — {head_claim}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# #2753 WS4 -- pre-registered chain-render A/B (issue #2753 comments
# 5602340998 + 5602392744, ratified)
# ---------------------------------------------------------------------------
# Does rendering the "[triggered principles]" chain built above change
# whether an agent walks past the nudge? Design: PER-FIRE randomization --
# each fire independently renders the chain or not, via a coin flip whose
# inputs (seed + nonce) are logged so the assignment is reconstructable
# after the fact. The pre-registration also requires logging the RENDERED
# LENGTH per fire (the chain lengthens every nudge, so length is a
# confound) -- see _append_ws4_fire_ledger's schema below.
#
# Measured gap this PR closes: the pre-registration assumed "the hook
# already logs every fire." It did not -- before this PR the hook's only
# JSONL writes were the prevention ledger (_append_prevention_ledger,
# below) and the in-turn recall ledger (_append_ledger, further below).
# Neither logs the general tripwire-fire event this experiment needs a
# denominator over.
#
# CONTRACT A (load-bearing): when the gate below is INACTIVE, main()'s
# stdout and exit code are byte-identical to pre-WS4 dev for every input,
# and this ledger file is never created or written. This is what makes
# this PR safe to merge and deploy before anyone flips the config switch
# on -- see _ws4_config()'s own docstring for the exact ACTIVE predicate.

WS4_LEDGER_PATH = Path(ENGRAM_HOME) / "tripwire-fire-ledger.jsonl"
# Round 2 (reviewer-fairy suggestion #3 on PR #3166): pure append, no
# rotation or size cap -- bounded only by the configured experiment
# window (see the example config's ~1-week `start`/`end` above), not by
# design. A longer-running future experiment must add rotation first.


def _ws4_config() -> dict:
    """Read experiments.ws4_chain_ab from config.json; safe default
    INACTIVE (never raises -- same posture as _demotion_config()/
    _in_turn_config() above/below).

    ACTIVE (returns {"active": True, "seed": <str>}) iff ALL hold:
      - `enabled` is the boolean True (not merely truthy -- the string
        "true" or the integer 1 does NOT activate the experiment);
      - `start` and `end` are both strings that parse as ISO-8601 WITH an
        explicit UTC offset (`Z` or `+00:00`) -- a naive/offset-less or
        non-UTC-offset timestamp does not count as "parses as ISO-8601
        UTC" and is treated as malformed;
      - `start <= now < end` (now = the real wall clock at call time, UTC);
      - `seed` is a non-empty string.
    Anything missing, wrong-typed, or malformed -- including the whole
    `experiments` or `ws4_chain_ab` section being absent -- returns
    INACTIVE ({"active": False, "seed": None}). This is CONTRACT A's gate:
    every caller below must treat INACTIVE as "behave exactly as if none
    of this WS4 code existed at all."
    """
    inactive = {"active": False, "seed": None}
    try:
        with open_regular_text(Path(ENGRAM_HOME) / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("experiments", {}) or {}).get("ws4_chain_ab", {}) or {}
        if section.get("enabled") is not True:
            return inactive
        seed = section.get("seed")
        if not isinstance(seed, str) or not seed:
            return inactive
        start_raw = section.get("start")
        end_raw = section.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            return inactive
        from datetime import datetime, timezone, timedelta
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except Exception:
            return inactive
        if start_dt.utcoffset() != timedelta(0) or end_dt.utcoffset() != timedelta(0):
            return inactive
        now_dt = datetime.now(timezone.utc)
        if not (start_dt <= now_dt < end_dt):
            return inactive
        return {"active": True, "seed": seed}
    except Exception:
        return inactive


def _build_ws4_ledger_entry(
    *, ts, ts_iso, hook_input, raw_tool_use_id, nonce, draw_basis, seed,
    arm, shown_lesson_ids, cut_lesson_ids, heads_by_lesson, nudge_chars,
    principles_block, chain_rendered, error, chain_cut=False,
) -> dict:
    """Assemble one full WS4 fire-ledger row (schema per the WS4 spec).

    Pulled out of main() as its OWN function (round 2, reviewer-fairy
    suggestion #1 on PR #3166) so row construction can be guarded
    separately from arm computation: main() wraps ONLY this call in its
    own narrow try/except, so a bug here can never also swallow the
    coin-flip decision already computed and applied to `parts` above --
    and on a failure here, main() falls back to a MINIMAL row (see the
    call site) instead of dropping the fire's ledger row entirely.

    `error` is the arm-computation error string (or None on a healthy
    draw) -- written into the row as `"error"` ONLY when non-None, so the
    field stays ABSENT (not null) on every healthy row."""
    entry = {
        "schema_version": 1,
        "experiment": "ws4_chain_ab",
        "ts": ts,
        "ts_iso": ts_iso,
        "session_id": hook_input.get("session_id"),
        "transcript_path": hook_input.get("transcript_path"),
        "tool_name": hook_input.get("tool_name"),
        "tool_use_id": raw_tool_use_id,
        "nonce": nonce,
        "draw_basis": draw_basis,
        "seed": seed,
        "arm": arm,
        "shown_lesson_ids": shown_lesson_ids,
        "cut_lesson_ids": cut_lesson_ids,
        "heads_by_lesson": heads_by_lesson,
        "nudge_chars": nudge_chars,
        "chain_chars": len(principles_block),
        "chain_rendered": chain_rendered,
        # #3677: True iff the final total-budget pass dropped the chain block.
        # chain_chars above stays the WOULD-RENDER length; chain_rendered is
        # whether the chain was delivered (False when cut).
        "chain_cut": bool(chain_cut),
    }
    if error is not None:
        entry["error"] = error
    return entry


def _append_ws4_fire_ledger(entry: dict) -> None:
    """Best-effort append of one JSONL row per WS4 fire (CONTRACT C): a
    write failure here (unwritable path, disk error, ...) must never
    change stdout or the exit code, and must never suppress the nudge or
    the chain block main() already decided to render or withhold -- this
    call always happens strictly AFTER that decision, purely to log it."""
    try:
        with open_regular_text_append(WS4_LEDGER_PATH, encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# #3677 -- the final total-budget pass. main() joins [tripwire block, chain,
# in-turn recall]; the block alone is bounded by emit_context, but the joined
# string was not, and the harness cuts the TAIL of an oversized injection (where
# the chain and recall sit). The pass cuts in whole units, recall (whole id
# lines from the end) first, then the chain (one whole block), never the block,
# and says so on the FIRST line, counted inside the budget via the reserve.
# Priority order: block, then chain, then recall; the chain also goes when the
# marker would not fit after the recall is gone (better than truncating a lesson
# hit in-band to save a chain).
# ---------------------------------------------------------------------------

# Room taken off the budget whenever the pass must cut, so the first-line clause
# (" · tail cut: R recall lines, C chain lines", <= 52 chars for 6-digit counts)
# can never push the total over the budget. Nothing is reserved when nothing is cut.
_TAIL_CUT_RESERVE = 64
_TRAILER_PREFIX_RE = re.compile(r"^▸ \d+ shown · \d+ cut")


def _tail_cut_clause(recall_cut: int, chain_cut_lines: int, own_line: bool = False) -> str:
    """The first-line marker. Inside a tripwire trailer it is a clause that sits
    right after "N cut"; with no tripwire block it is its own first line."""
    text = f"tail cut: {recall_cut} recall lines, {chain_cut_lines} chain lines"
    return f"▸ {text}" if own_line else f" · {text}"


def _apply_tail_cut(parts: list, recall_cut: int, chain_cut_lines: int, has_block: bool) -> list:
    """Return `parts` with the marker on the FIRST line (a new list; never mutates).
    With a tripwire block, the clause goes after `▸ N shown · M cut` and before any
    ` · head trimmed` / ` — see <path>` text, so the trailer's path stays last."""
    out = list(parts)
    if has_block and out:
        m = _TRAILER_PREFIX_RE.match(out[0])
        if m:
            out[0] = out[0][: m.end()] + _tail_cut_clause(recall_cut, chain_cut_lines) + out[0][m.end():]
            return out
    return [_tail_cut_clause(recall_cut, chain_cut_lines, own_line=True)] + out


def _place_chain(parts: list, block: str, budget) -> tuple:
    """(delivered, cut_lines): whether the chain block is kept whole by C3's rule,
    and, when it is dropped, how many lines the dropped block had (else 0)."""
    if _chain_fits(parts, block, budget):
        return True, 0
    return False, block.count("\n") + 1


def _chain_fits(parts: list, block: str, budget) -> bool:
    """C3's chain rule: keep the chain iff the pieces so far plus the whole chain
    block fit the budget. Fail-open toward delivering (any error, or no known
    budget, keeps the chain exactly as before #3677)."""
    try:
        return budget is None or len("\n".join(parts + [block])) <= budget
    except Exception:
        return True


# ---------------------------------------------------------------------------
# alpha#1944 step 2 — shadow-block logging
# ---------------------------------------------------------------------------
# A SEPARATE query + check path from load_tripwires/check_command above,
# deliberately not folded into them: those two feed the advisory-nudge
# injection every lesson/cornerstone/axiom/goal has always had, and this
# repo's tests construct bare (pattern, nudge) 2-tuples as fixtures for that
# path. Widening that tuple shape to carry prevention_type would silently
# break every such fixture. prevention_type only exists on lesson nodes
# (engram_epistemic.PREVENTION_TYPES), so this path queries just lessons,
# runs independently, and — like check_in_turn_recall — must never suppress
# or be suppressed by the advisory-nudge path.

PREVENTION_LEDGER_PATH = Path(ENGRAM_HOME) / "lesson-prevention-ledger.jsonl"

# #3485: same json_valid guard as _QUERY above (one bad blob must not abort the
# whole read and silence every shadow/blocking lesson).
_PREVENTION_QUERY = """
SELECT
    id,
    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern')  AS pattern,
    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.prevention_type')    AS prevention_type,
    COALESCE(json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.scaffolding_nudge'), claim) AS nudge
FROM nodes
WHERE type = 'lesson'
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern') IS NOT NULL
  AND json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.situation_pattern') != ''
  AND json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.prevention_type') IN ('shadow', 'blocking')
"""


def load_prevention_lessons(db_path: Path = DB_PATH):
    """Return list of {id, pattern, prevention_type, nudge} for lessons
    whose prevention_type is 'shadow' or 'blocking' (advisory/absent
    lessons are excluded — they have nothing to log or deny here)."""
    # #3665: see load_tripwires. A failed owner read is an EMPTY result plus a recorded owner
    # failure -- never "no lessons" silently, and the deny gate stays fail-open.
    owner = _owner_active()
    if not owner and not db_path.exists():
        return []
    try:
        conn = _owner_conn() if owner else connect_regular_ro(db_path, timeout=2.0)
        rows = conn.execute(_PREVENTION_QUERY).fetchall()
        if not owner:
            conn.close()
        return [
            {"id": row[0], "pattern": row[1], "prevention_type": row[2], "nudge": row[3] or ""}
            for row in rows if row[1] and row[2]
        ]
    except Exception as exc:
        if not (owner and _record_owner_failure("load_prevention_lessons", exc)):
            _record_loader_failure("load_prevention_lessons", exc)
        return []


# alpha#1944 step 3 — override token detection. Kepler's verified design
# (Lei-authorized throwaway test, PreToolUse deny confirmed reachable on
# this plugin): the override is STATELESS and single-call-scoped — nothing
# persists, nothing to re-arm. The retry IS the override. Bash embeds it as
# a harmless leading env-var assignment (ENGRAM_OVERRIDE="ls_NNNN: <reason>"
# <same command> — the shell sets the var and runs the command unchanged;
# the hook only reads the literal command STRING, never executes it). MCP
# tools carry it as an "override_lesson" field in payload_json, which lands
# inside build_match_target's compact-JSON args string the same way any
# other field would. One regex covers both shapes since both end up
# somewhere inside match_target.
#
# The hook itself can only ALLOW or DENY — it cannot strip override_lesson
# before the call reaches the tool. That gap is closed centrally instead:
# server.py's _parse_payload() pops override_lesson before any tool's own
# unknown-field check runs, so every MCP tool is covered without touching
# each one's individual allowlist (see _parse_payload's own docstring).
_OVERRIDE_TOKEN_RE = r'(?:ENGRAM_OVERRIDE\s*=\s*"|"override_lesson"\s*:\s*")%s\s*:\s*([^"]*)"'


def _find_override(match_target: str, lesson_id: str) -> str | None:
    """Return the override reason for lesson_id if a matching
    ENGRAM_OVERRIDE / override_lesson token is present in match_target,
    else None. Never raises."""
    try:
        m = re.search(_OVERRIDE_TOKEN_RE % re.escape(lesson_id), match_target)
    except re.error:
        return None
    return m.group(1).strip() if m else None


def check_shadow_block(match_target: str, prevention_lessons, hook_input: dict | None = None) -> list:
    """Return a list of would-have-blocked fire records — one per matching
    shadow/blocking lesson, including whether an override token for that
    lesson was present (#1944 step 3 — the override token IS the label
    step 4's auto-demotion counter reads). Never raises: a malformed
    pattern is skipped silently, matching check_command's posture."""
    hits = []
    for lesson in prevention_lessons:
        pattern = lesson["pattern"]
        try:
            match = re.search(pattern, match_target, re.IGNORECASE)
        except re.error:
            continue  # malformed pattern — skip silently, same as check_command
        if not match:
            continue
        override_reason = _find_override(match_target, lesson["id"])
        hits.append({
            "lesson_id": lesson["id"],
            "prevention_type": lesson["prevention_type"],
            "matched_span": match.group(0),
            "tool_name": (hook_input or {}).get("tool_name"),
            "session_id": (hook_input or {}).get("session_id"),
            "override_present": override_reason is not None,
            "override_reason": override_reason,
        })
    return hits


# alpha#1944 step 4 — auto-demotion, Kepler's read-time-lease design
# (forum #387, 2026-09-04): the hook NEVER writes the graph directly — that
# is the no-raw-DB-edits rule, and the audit trail (field_traces,
# edit_history, turn stamp) belongs to engram_update_lesson, the module
# that owns it. "Mechanically demoted" stays true anyway because the hook
# computes the EFFECTIVE prevention_type at read time from the ledger, not
# only from the lesson's stored metadata: once a blocking lesson has N
# overrides inside the window, THIS call (and every call after it, until
# the durable write lands) is treated as advisory. A lease, not a latch —
# it decays to safe immediately from the signal already on disk; the
# durable write is a follow-up (a person, or a dream-cycle reconcile pass
# reading the "threshold crossed" ledger entries this emits) tracked
# separately, not built in this hook.
_DEMOTION_DEFAULTS = {"overrides": 2, "window_days": 7}


def _demotion_config() -> dict:
    """Read lessons.blocking_demotion from config.json; safe defaults.

    Default (overrides=2, window_days=7) is Kepler's pick made 2026-09-04,
    not a considered policy from Lei — retune via config.json:
      {"lessons": {"blocking_demotion": {"overrides": 2, "window_days": 7}}}
    Reasoning stated on the issue: a lesson only reaches "blocking" after a
    zero-false-positive shadow window, so one override is plausibly an
    accepted-risk case; two legitimate overrides in a week is evidence the
    pattern matches legitimate calls and should stop blocking.
    """
    try:
        with open_regular_text(Path(ENGRAM_HOME) / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("lessons", {}) or {}).get("blocking_demotion", {}) or {}
        return {
            "overrides": int(section.get("overrides", _DEMOTION_DEFAULTS["overrides"])),
            "window_days": float(section.get("window_days", _DEMOTION_DEFAULTS["window_days"])),
        }
    except Exception:
        return dict(_DEMOTION_DEFAULTS)


def _count_recent_overrides(lesson_id: str, window_days: float, now: float) -> int:
    """Count override_present=True ledger entries for lesson_id within the
    last window_days. Best-effort: any read failure counts as 0, never
    raises. Only called on an actual blocking-lesson match (rare by
    design — the whole point of blocking is deterrence), so scanning the
    full ledger stays cheap in practice."""
    if not PREVENTION_LEDGER_PATH.exists():
        return 0
    cutoff = now - (window_days * 86400)
    count = 0
    try:
        with open_regular_text(PREVENTION_LEDGER_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if (
                    entry.get("lesson_id") == lesson_id
                    and entry.get("override_present")
                    and entry.get("ts", 0) >= cutoff
                ):
                    count += 1
    except Exception:
        return count
    return count


def check_blocking_deny(match_target: str, prevention_lessons, now: float | None = None) -> dict | None:
    """Return a decision for the FIRST 'blocking' lesson matched without a
    valid override this call, or None if nothing to report.

    Only prevention_type == 'blocking' is ever consulted — 'shadow' never
    denies or demotes (that is the entire distinction; shadow only logs,
    via check_shadow_block above). A lesson-id-matching override token
    lets THIS call through outright (still recorded as a hit by
    check_shadow_block); it is checked before the demotion count so an
    overridden call is never ALSO reported as a demotion.

    Otherwise checks the read-time lease: if this lesson already has
    demotion_config()["overrides"] overrides inside the window, the
    returned dict carries demoted=True and override_count/threshold —
    main() must NOT deny on that, only log + warn. A genuine denial (no
    override this call, threshold not yet crossed) carries demoted=False.
    """
    if now is None:
        import time as _time
        now = _time.time()
    cfg = _demotion_config()
    for lesson in prevention_lessons:
        if lesson["prevention_type"] != "blocking":
            continue
        pattern = lesson["pattern"]
        try:
            match = re.search(pattern, match_target, re.IGNORECASE)
        except re.error:
            continue
        if not match:
            continue
        if _find_override(match_target, lesson["id"]) is not None:
            continue  # overridden this call — do not deny or demote-report
        override_count = _count_recent_overrides(lesson["id"], cfg["window_days"], now)
        base = {
            "lesson_id": lesson["id"],
            "matched_span": match.group(0),
            "nudge": lesson.get("nudge", ""),
        }
        if override_count >= cfg["overrides"]:
            base.update({
                "demoted": True,
                "override_count": override_count,
                "threshold": cfg["overrides"],
                "window_days": cfg["window_days"],
            })
        else:
            base["demoted"] = False
        return base
    return None


def build_deny_reason(deny: dict) -> str:
    """Build the permissionDecisionReason text for a blocking denial —
    lesson id, matched span, the nudge, and the exact override recipe
    (Kepler's verified design: JSON permissionDecision form, its reason
    rendering cleanly as the tool error rather than a hook-crash-shaped
    exit-2 message)."""
    lesson_id = deny["lesson_id"]
    return (
        f"[lesson-tripwire BLOCKING] {lesson_id} matched {deny['matched_span']!r} "
        f"— this call is blocked, not merely advised.\n"
        f"{deny['nudge']}\n\n"
        f"If you've reviewed this and the call is correct, retry with the exact "
        f"same call plus a single-use override naming this lesson and your reason:\n"
        f"  Bash:  ENGRAM_OVERRIDE=\"{lesson_id}: <your reason>\" <same command>\n"
        f"  MCP:   add \"override_lesson\": \"{lesson_id}: <your reason>\" to payload_json\n"
        f"The override is single-call-scoped — nothing persists, nothing to re-arm."
    )


def _log_threshold_crossed(now: float, deny: dict) -> None:
    """alpha#1944 step 4 — the read-time-lease signal: a loud stderr line
    (hook stdout is reserved for the additionalContext/deny JSON contract;
    stderr is safe to use freely) plus a distinct ledger entry, emitted
    the FIRST time a call would have denied but the override threshold was
    already crossed. Best-effort, never raises."""
    lesson_id = deny["lesson_id"]
    try:
        print(
            f"[lesson-tripwire] AUTO-DEMOTED (lease) {lesson_id}: "
            f"{deny['override_count']} overrides in the last {deny['window_days']} "
            f"day(s) (threshold {deny['threshold']}) — treating as advisory on this "
            f"call; durable prevention_type write still pending (engram_update_lesson).",
            file=sys.stderr,
        )
    except Exception:
        pass
    try:
        with open_regular_text_append(PREVENTION_LEDGER_PATH) as f:
            f.write(json.dumps({
                "ts": now,
                "lesson_id": lesson_id,
                "mode": "auto_demoted_lease",
                "override_count": deny["override_count"],
                "threshold": deny["threshold"],
                "window_days": deny["window_days"],
            }) + "\n")
    except Exception:
        pass


def _append_prevention_ledger(now: float, hits: list) -> None:
    """Best-effort append of one JSONL entry per would-have-blocked fire
    (alpha#1944 step 2, extended step 3 with override fields — the log a
    future promotion/demotion gate reads). Mirrors _append_ledger's
    never-raises contract: a ledger failure must never suppress or delay
    the tripwire path it rides alongside."""
    if not hits:
        return
    try:
        with open_regular_text_append(PREVENTION_LEDGER_PATH) as f:
            for hit in hits:
                f.write(json.dumps({
                    "ts": now,
                    "lesson_id": hit["lesson_id"],
                    "mode": hit["prevention_type"],
                    "tool_name": hit["tool_name"],
                    "session_id": hit["session_id"],
                    "matched_span": hit["matched_span"],
                    "override_present": hit.get("override_present", False),
                    "override_reason": hit.get("override_reason"),
                    # step 4 field (independent-predicate verdict) lands
                    # when the auto-demotion counter is wired.
                }) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# alpha#3920 (part of #3911): the two views of a call
# ---------------------------------------------------------------------------
# A pattern written for a COMMAND fired on prose that only mentions the command (a heredoc
# body, an MCP `body`/`claim` field). Each call now has a `full` view (today's target) and a
# `command` view with that prose hidden; a lesson's `match_scope` picks which one its pattern runs
# on. Safe direction: anything that cannot be parsed falls back to the full text (a missed strip is
# noise, a wrong strip is blindness).

# MCP arguments whose VALUES are prose. One constant, so adding a field is a one-line change.
PROSE_FIELDS = frozenset({
    "body", "claim", "quoted_text", "interpretation", "logical_chain", "recall_summary",
    "description", "text", "message", "content",
})

_HEREDOC_PLACEHOLDER = "<<HEREDOC>>"
_PROSE_PLACEHOLDER = "<<TEXT>>"
# A heredoc fed to an interpreter or a shell is a script, not prose: its body RUNS, so the command
# view must not hide it (a wrong strip is blindness).
_EXEC_WORDS = (r"(?:sudo|env|exec|xargs|eval|source|bash|sh|zsh|dash|ksh|fish|ssh|node|deno|bun|"
               r"perl|ruby|php|lua|osascript|python[0-9.]*|pypy[0-9.]*|ipython[0-9.]*|"
               r"sqlite3|psql|mysql|mariadb|sqlcmd|duckdb|redis-cli|mongosh)(?![\w-])")
_EXEC_BEFORE_RE = re.compile(r"(?:^|[\s;&|(`/])" + _EXEC_WORDS)
# `cat <<EOF | bash`: the consumer comes AFTER the operator.
_EXEC_AFTER_RE = re.compile(r"\|\s*(?:[\w./=-]+\s+)*?" + _EXEC_WORDS)
_HEREDOC_OP_RE = re.compile(r"""<<(-?)[ \t]*(?:'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))""")


def _heredoc_ops_on_line(line: str) -> list:
    """[(strip_tabs, delimiter, start, end)] for each heredoc operator on one line, in order. Operators inside
    single or double quotes are ignored, and `<<<` (a here-string) is not a heredoc."""
    ops = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if line.startswith("<<<", i):
            i += 3
            continue
        if line.startswith("<<", i):
            m = _HEREDOC_OP_RE.match(line, i)
            if m:
                ops.append((m.group(1) == "-", m.group(2) or m.group(3) or m.group(4), i, m.end()))
                i = m.end()
                continue
        i += 1
    return ops


def strip_heredoc_bodies(command: str) -> str:
    """The command with each heredoc BODY replaced by `<<HEREDOC>>` (alpha#3920). The operator line
    and the terminator line stay, and so does everything after the terminator. If any heredoc has no
    terminator the original text is returned unchanged."""
    if "<<" not in command:
        return command
    lines = command.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        pending = _heredoc_ops_on_line(line)
        for n, (strip_tabs, delim, op_start, op_end) in enumerate(pending):
            # The operator's own consumer: text between the previous operator and this one, or
            # after this one up to the next (`cat <<A; bash <<B`, `cat <<EOF | bash`).
            before = line[(pending[n - 1][3] if n else 0):op_start]
            after = line[op_end:(pending[n + 1][2] if n + 1 < len(pending) else len(line))]
            runs = bool(_EXEC_BEFORE_RE.search(before) or _EXEC_AFTER_RE.search(after))
            start = i
            while i < len(lines):
                cand = lines[i].lstrip("\t") if strip_tabs else lines[i]
                if cand == delim:
                    break
                i += 1
            else:
                return command  # unterminated: never hide what cannot be parsed
            out.extend(lines[start:i] if runs else [_HEREDOC_PLACEHOLDER])
            out.append(lines[i])  # the terminator line stays
            i += 1
    return "\n".join(out)


def _redact_prose(value, key=None):
    if isinstance(value, dict):
        return {k: _redact_prose(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_prose(v, key) for v in value]
    if key in PROSE_FIELDS and isinstance(value, str):
        return _PROSE_PLACEHOLDER
    if key == "payload_json" and isinstance(value, str):
        # ENGRAM's own tools take their arguments as one JSON-encoded string.
        try:
            inner = json.loads(value)
        except Exception:
            return value
        if isinstance(inner, (dict, list)):
            return json.dumps(_redact_prose(inner), separators=(",", ":"), ensure_ascii=False)
    return value


def build_match_views(hook_input: dict) -> tuple:
    """(full, command) match targets for one call. `full` is exactly build_match_target's result."""
    full = build_match_target(hook_input)
    if not full:
        return ("", "")
    tool_name = hook_input.get("tool_name", "")
    ti = hook_input.get("tool_input") or hook_input.get("input") or {}
    if tool_name == "Bash":
        return (full, strip_heredoc_bodies(full))
    if tool_name.startswith("mcp__"):
        try:
            args_str = json.dumps(_redact_prose(ti), separators=(",", ":"), ensure_ascii=False)
        except Exception:
            return (full, full)
        return (full, f"{tool_name} {args_str}")
    return (full, full)


def build_match_target(hook_input: dict) -> str:
    """Build the string to match situation_pattern against, or "" to skip.

    Bash calls   → the shell command string.
    MCP calls    → "{tool_name} {compact_json_args}" so patterns like
                   `engram_add_observation` match the tool name substring.
    Other calls  → "" (caller exits early).
    """
    tool_name = hook_input.get("tool_name", "")
    ti = hook_input.get("tool_input") or hook_input.get("input") or {}

    if tool_name == "Bash":
        return ti.get("command", "") if isinstance(ti.get("command"), str) else ""

    if tool_name.startswith("mcp__"):
        try:
            args_str = json.dumps(ti, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            args_str = ""
        return f"{tool_name} {args_str}"

    return ""


# ---------------------------------------------------------------------------
# In-turn ambient recall (#1690, P2 of the recall-triggering blueprint)
# ---------------------------------------------------------------------------
# Piggybacks on this hook's existing PreToolUse spawn (one process, two
# checks) instead of registering a new hook — hook-spawn overhead is the
# dominant latency cost (~300ms/spawn), so the general-recall check rides
# the spawn the tripwire already pays for.
#
# Novelty gate (all must pass, else silence):
#   1. config auto_surface.in_turn_recall.enabled is true (default TRUE +
#      zero cooldown since v0.3.0 — #1746, validated by the zero-cooldown
#      ledger experiment; enabled=false is the per-install kill-switch)
#   2. cooldown: ≥ cooldown_seconds since the last in-turn fire (cheap
#      state-file check, runs before any DB work)
#   3. the pending tool call's args contain ≥1 high-IDF term not seen in
#      any recent fire's term set (topic-shift detection)
# On gate-pass: query the recall daemon with the novel terms, render up to
# max_lines nodes not recently rendered by this channel.

STATE_PATH = Path(ENGRAM_HOME) / "in-turn-recall-state.json"
SOCKET_PATH = Path(ENGRAM_HOME) / "recall-daemon.sock"
LEDGER_PATH = Path(ENGRAM_HOME) / "in-turn-recall-ledger.jsonl"
_RECENT_TERM_SETS_KEPT = 10
_RECENT_IDS_KEPT = 30


def _append_ledger(build_entry) -> None:
    """Best-effort append of one JSONL telemetry entry per gate-passing
    in-turn-recall fire (#1749). Takes a zero-arg callable so that entry
    CONSTRUCTION runs inside the guard too — an exception while building
    the dict (e.g. a malformed hook_input) must be swallowed exactly like
    a write failure, never escape into the recall path (review round 2).
    Never-raises: a ledger failure must never suppress or delay the
    tripwire/recall path. Call sites are inside the existing flock-guarded
    region, so appends are already serialized here -- no new locking is
    added."""
    try:
        entry = build_entry()
        with open_regular_text_append(LEDGER_PATH) as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# rec-3 (#266): junk-token stoplist for the novel-terms gate. Shell/code
# execution tokens carry zero recall signal but pollute the high-IDF
# novel-term set (a seat-level ledger measured ~30% of novel tokens as
# junk-class). The canonical list now lives as `JUNK_STOPLIST` in engram_idf
# (#1784: single source of truth shared with the rec-4 measurement harness so
# the filter and the measurement can't drift). It is imported lazily at
# point-of-use — NOT at module top-level — because this hook must never crash
# at import when engram_idf isn't yet on sys.path (the whole recall path is
# guarded and returns "" if the import fails). `_in_turn_config()` therefore
# carries a None sentinel for "use the canonical default", kept distinct from
# a config-supplied `[]` which DISABLES filtering; the sentinel is resolved to
# JUNK_STOPLIST only after the import succeeds.


def _apply_junk_stoplist(terms: list, stoplist: frozenset) -> list:
    """Drop junk-class tokens (case-insensitive) from an ordered term list,
    preserving order. rec-3 (#266) -- keeps shell/code execution noise out of
    the novel-term set that drives in-turn recall + the ledger."""
    if not stoplist:
        return terms
    return [t for t in terms if t.lower() not in stoplist]


def _in_turn_config() -> dict:
    """Read auto_surface.in_turn_recall from config.json; safe defaults.

    min_idf is graph-size-relative by default (#1734): IDF's ceiling is
    ln(N_current_nodes), so a fixed absolute threshold dead-zones small
    graphs (6.0 was unreachable under ~400 nodes). min_idf_ratio=0.7 gives
    ~4.16 at 380 nodes and ~6.03 at 5500 nodes — matching both
    empirically-working absolute values. An explicit min_idf still wins
    verbatim (absolute override; existing configs keep exact behavior).
    """
    defaults = {
        # Defaults flipped for v0.3.0 (#1746, Lei's 2026-07-10 ruling): the
        # release-candidate default is ON with ZERO channel cooldown — the
        # n=252 zero-cooldown ledger showed junk fires render nothing and
        # cost ~0.7s daemon time/hr, while the render-side layers (recent_ids
        # + term-set memory) handle repetition. cooldown_seconds is retained
        # as a config knob (and kill-switch enabled=false stays documented).
        "enabled": True,
        "cooldown_seconds": 0,
        "max_lines": 3,
        "min_idf": None,
        "min_idf_ratio": 0.7,
        # None sentinel = "not overridden, use the canonical engram_idf.JUNK_STOPLIST
        # (resolved at point-of-use after the lazy import)". A config-supplied []
        # is a real empty frozenset that DISABLES filtering — kept distinct below.
        "junk_stoplist": None,
    }
    try:
        with open_regular_text(Path(ENGRAM_HOME) / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("auto_surface", {}) or {}).get("in_turn_recall", {}) or {}
        raw_min_idf = section.get("min_idf")
        return {
            "enabled": bool(section.get("enabled", defaults["enabled"])),
            "cooldown_seconds": int(section.get("cooldown_seconds", defaults["cooldown_seconds"])),
            "max_lines": int(section.get("max_lines", defaults["max_lines"])),
            "min_idf": float(raw_min_idf) if raw_min_idf is not None else None,
            "min_idf_ratio": float(section.get("min_idf_ratio", defaults["min_idf_ratio"])),
            "junk_stoplist": (
                frozenset(str(t).lower() for t in section["junk_stoplist"])
                if isinstance(section.get("junk_stoplist"), list)
                else defaults["junk_stoplist"]
            ),
        }
    except Exception:
        return defaults


def _resolve_runtime_dir() -> str:
    """Locate engram_idf.py — plugin root (two levels up, flat layout) first,
    then ENGRAM_HOME snapshot, then a walk-parents search for the nearest
    ancestor whose own src/engram/ actually contains engram_idf.py, then the
    dev-source fallback (last-ditch, unchanged). Same ladder as
    engram-surface-hook.py; never a fixed parents[N] against src/ layout."""
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(plugin_root, "engram_idf.py")):
        return plugin_root
    if os.path.exists(os.path.join(ENGRAM_HOME, "engram_idf.py")):
        return ENGRAM_HOME
    # Walk parents from __file__ and take the first ancestor that actually
    # contains src/engram/engram_idf.py, instead of guessing a fixed
    # absolute path (#1712: the fixed guess resolves to the WRONG checkout
    # when this process is running from a git worktree nested under the
    # guessed path, e.g. .claude/worktrees/<id>/ under ~/engram-alpha --
    # silently shadowing the actually-running copy with a sibling one).
    # Reviewer-fairy blocker (PR #1727, found in the sibling surface-hook
    # copy, applied here too for consistency): Path.exists() raises
    # PermissionError on an unreadable ancestor instead of returning False
    # like os.path.exists() does at the rungs above -- wrap so this function
    # keeps its never-raises contract regardless of caller-side guarding.
    try:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "src" / "engram"
            if (candidate / "engram_idf.py").exists():
                return str(candidate)
    except OSError:
        pass
    return os.path.expanduser("~/engram-alpha/src/engram")  # last-ditch, unchanged


def _query_recall_daemon(query: str, top_k: int = 5) -> dict | None:
    """Minimal daemon socket query (same protocol as engram-surface-hook)."""
    import socket as _socket
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(SOCKET_PATH))
        sock.sendall((json.dumps({"query": query, "top_k": top_k}) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        response = json.loads(data.decode("utf-8").strip())
        if response.get("status") == "ok":
            return response.get("result", {})
        return None
    except Exception:
        return None


_RECALL_FOOTER = "\n  (ambient action-moment recall; engram_inspect(id) for detail)"


def check_in_turn_recall(match_target: str, hook_input: dict | None = None,
                         record_out: dict | None = None,
                         max_chars: int | None = None, cut_reserve: int = 0) -> str:
    """Budgeted general recall at action-moment, behind the novelty gate.

    hook_input (#1749): the raw PreToolUse payload, passed through from
    main() so the fire ledger can stamp session_id/transcript_path/tool_name
    per entry. Optional -- existing callers/tests that don't pass it keep
    working, with those fields recorded as null.

    record_out (#3670): optional out-parameter for the shown record. When a
    dict is passed, a gate-passing fire stores the ids it rendered in
    ``record_out["ids"]`` (possibly ``[]``) and does NOT append the record
    itself: the caller appends it only after the response was written out,
    so a record never claims more than was shown. When None (every other
    caller: tests, tools/perf/bench_in_turn_recall.py) no shown record is
    appended at all -- appending here, before anything was printed, was the
    bug. The return type and the route ledger write are unchanged.

    There is no emit_context cut on the recall text, but main() appends it LAST
    to the joined additionalContext, and the harness silently cuts the TAIL of
    an oversized injection. So main() records ``record_out["ids"]`` only when
    the WHOLE joined string fits the harness budget; otherwise it records an
    empty-ids line (under-record, never over-record).

    max_chars / cut_reserve (#3677): the final total-budget pass. ``max_chars``
    is the room the rendered recall may take (None = no cap, today's behaviour).
    When the full rendering is longer than it, id lines are cut WHOLE from the
    END until the REAL rendered string (header + footer included) fits
    ``max_chars - cut_reserve`` -- BEFORE the route ledger, ``recent_ids`` and
    the fire-ledger row are written, so all of them list only the survivors (a
    cut id is shown again on a later fire). Cut ids carry the route-ledger cut
    name "tail_budget"; ``record_out["tail_cut_lines"]`` is how many id lines
    were cut. If no id line survives the result is "" with outcome "budget_cut"."""
    import time

    cfg = _in_turn_config()
    if not cfg["enabled"]:
        return ""

    # Atomic cooldown claim (#1709) — non-blocking exclusive flock over the
    # WHOLE check->record sequence below (cooldown read, novelty gate, daemon
    # query, _record_fire/_write_state). Under concurrent PreToolUse
    # invocations, exactly one caller wins the lock and proceeds; every
    # contender returns "" immediately (suppress, never block — a lost race
    # only risks a miss, which this T2 default-off channel already tolerates
    # per _write_state's docstring). Locks a DEDICATED lockfile, never
    # STATE_PATH itself (STATE_PATH is rewritten via os.replace, which would
    # break the lock's file-descriptor association).
    lock_fd = None
    lock_acquired = False
    lock_degraded = False
    lock_degrade_reason = ""
    try:
        import fcntl
        # O_NONBLOCK (#3628): defence in depth. Measured on macOS, O_CREAT|O_RDWR
        # on a FIFO already returns at once and the flock below then fails
        # ENOTSUP (-> suppress, "" -- unchanged); O_NONBLOCK keeps a platform
        # where that open could wait from hanging. No effect on a regular file.
        lock_fd = os.open(str(STATE_PATH) + ".lock",
                          os.O_CREAT | os.O_RDWR | os.O_NONBLOCK, 0o600)
    except Exception as exc:
        # fcntl unavailable (non-POSIX) or the lockfile couldn't be opened
        # (e.g. permissions) — degrade to the unlocked path exactly as
        # before this fix. The race is a perf issue, not a correctness one;
        # disabling the feature over an environment limitation would be
        # worse than accepting the (already-tolerated) race.
        #
        # #1737: this degrade was completely silent -- nothing distinguished
        # "locked and suppressed correctly" from "running unlocked," which
        # cost a full diagnosis round during PR #1733's review (a shared-/tmp
        # lockfile collision made this fallback engage, and the only tell was
        # stampede-shaped latency numbers downstream). Two cheap, never-
        # block/never-raise tells: one stderr line now (hook stderr goes to
        # hook logs, not the model -- safe on every call), and a state-file
        # flag below wherever state ends up written this call, so any future
        # bench/diagnostic can see it without re-deriving the cause.
        lock_fd = None
        lock_degraded = True
        lock_degrade_reason = f"{type(exc).__name__}: {exc}"
        try:
            print(
                f"[engram-lesson-tripwire-hook] in_turn_recall lock degraded to "
                f"unlocked path: {lock_degrade_reason}",
                file=sys.stderr,
            )
        except Exception:
            pass
    else:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except (BlockingIOError, OSError):
            # Another caller currently holds the claim on this fire-window —
            # suppress immediately rather than block.
            try:
                os.close(lock_fd)
            except Exception:
                pass
            return ""

    try:
        # Gate 2 — cooldown (state-file read only; no DB touched). Broad
        # except: any state-read failure (incl. PermissionError) must
        # degrade to empty state, never block the tripwire path.
        try:
            with open_regular_text(STATE_PATH) as f:
                state = json.load(f)
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        # #1737: carry the degrade tell into the state file, EAGERLY
        # persisted the moment the flag transitions -- independent of
        # whatever the cooldown/novelty gates below decide to do. Reviewer-
        # fairy catch on PR #1742's first review: piggybacking purely on
        # record_fire/_write_state (which several early-return gates below
        # skip entirely) meant the flag could go stale in BOTH directions
        # under sustained cooldown-heavy traffic -- under-reporting an
        # ongoing degrade (never written because every call keeps hitting
        # the cooldown gate), or over-reporting a resolved one (a stale
        # True surviving because no call has reached a write since
        # recovery). Eagerly writing just the transition, here, before any
        # gate can short-circuit, closes both gaps while still only paying
        # the extra write on an actual True<->False flip (steady state,
        # once clear, costs nothing).
        prev_degraded = bool(state.get("lock_degraded"))
        if lock_degraded:
            state["lock_degraded"] = True
            state["lock_degrade_reason"] = lock_degrade_reason
            flag_changed = not prev_degraded
        else:
            flag_changed = prev_degraded
            state.pop("lock_degraded", None)
            state.pop("lock_degrade_reason", None)
        if flag_changed:
            try:
                atomic_write_json(str(STATE_PATH), state)
            except OSError:
                pass
        now = time.time()
        last_fire = state.get("last_fire_ts", 0)
        if isinstance(last_fire, (int, float)) and (now - last_fire) < cfg["cooldown_seconds"]:
            return ""

        # Gate 3 — high-IDF novel terms in the pending tool call.
        # extract_keywords returns (token, idf_score) PAIRS — unwrap to tokens
        # (#1696 reviewer-fairy blocker: the flat-string treatment TypeError'd
        # against the real module and was masked by a wrong-contract test stub).
        runtime_dir = _resolve_runtime_dir()
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        try:
            from engram_idf import extract_keywords, STOPWORDS, JUNK_STOPLIST
            # Resolve the None sentinel from _in_turn_config() to the canonical
            # shared list now that the import has succeeded (#1784). A
            # config-supplied frozenset — including an empty one (filtering
            # disabled) — passes through unchanged.
            junk = cfg["junk_stoplist"] if cfg["junk_stoplist"] is not None else JUNK_STOPLIST
            # #3665: with the owner flag ON the fire's shared shim stands in for the
            # connection (the SQL strings below are registry templates, byte for byte).
            owner = _owner_active()
            conn = _owner_conn() if owner else connect_regular_ro(DB_PATH, timeout=1.0)
            try:
                # #3740: the current-node count, when this site computes it, is
                # handed to extract_keywords so the COUNT runs once per fire.
                n_docs = None
                if cfg["min_idf"] is not None:
                    effective_min_idf = cfg["min_idf"]
                else:
                    n_docs = conn.execute(
                        "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
                    ).fetchone()[0]
                    if n_docs == 0:
                        return ""
                    effective_min_idf = cfg["min_idf_ratio"] * math.log(n_docs)
                # rec-3 (#266) round 2 (colleague-review suggestion): exclude
                # junk tokens BEFORE the top_k cut, so >=5 high-IDF junk tokens
                # can't crowd a real lower-ranked term out of `pairs` entirely.
                # MERGE with the built-in STOPWORDS -- never replace, which
                # would drop common-English stopword filtering. The post-filter
                # below stays as a backstop.
                pairs = extract_keywords(
                    conn, match_target, min_idf=effective_min_idf, top_k=5,
                    stopwords=STOPWORDS | junk, n_docs=n_docs)
            finally:
                if not owner:
                    conn.close()
            terms = [tok for tok, _score in pairs]
            # rec-3 (#266): drop junk-class shell/code tokens BEFORE the
            # novelty check + fire, so execution noise neither triggers recall
            # nor pollutes the term-memory/ledger. Targeted, not a min_idf bump.
            terms = _apply_junk_stoplist(terms, junk)
        except Exception as exc:
            # #3665: an owner failure here is recorded for the loud notice (the site still
            # returns ""); anything else is swallowed exactly as before.
            _record_owner_failure("check_in_turn_recall", exc)
            return ""
        if not terms:
            return ""
        seen_terms = {t for ts in state.get("recent_term_sets", []) if isinstance(ts, list) for t in ts}
        novel = [t for t in terms if t not in seen_terms]
        if not novel:
            return ""

        def _write_state() -> None:
            """Best-effort atomic state write via _hooklib.atomic_write_json()
            (tmp + os.replace so a concurrent reader never sees a truncated
            file; a lost race only risks an early re-fire, which is
            acceptable for this T2 channel)."""
            try:
                atomic_write_json(str(STATE_PATH), state)
            except OSError:
                pass

        def _record_fire() -> None:
            """Stamp cooldown + term-set memory. Runs on EVERY gate-pass — also
            when nothing new rendered — so a near-miss (novel terms, daemon hits
            all already-rendered) cannot re-trigger the daemon on every call
            (#1696 reviewer-fairy: the render-only stamping bypassed gate 2)."""
            state["last_fire_ts"] = now
            term_sets = state.get("recent_term_sets", [])
            if not isinstance(term_sets, list):
                term_sets = []
            term_sets.append(list(terms))
            state["recent_term_sets"] = term_sets[-_RECENT_TERM_SETS_KEPT:]

        def _ledger_entry(outcome: str, match_count: int, rendered_ids_: list, skipped_recent_: int) -> dict:
            return {
                "ts": now,
                "session_id": hook_input.get("session_id") if hook_input else None,
                "transcript_path": hook_input.get("transcript_path") if hook_input else None,
                "tool_name": hook_input.get("tool_name") if hook_input else None,
                "novel_terms": novel,
                "terms": terms,
                "effective_min_idf": effective_min_idf,
                "match_count": match_count,
                "rendered_ids": rendered_ids_,
                "skipped_recent": skipped_recent_,
                "cooldown_seconds": cfg["cooldown_seconds"],
                "outcome": outcome,
            }

        # Route ledger (#3458 A): one line per gate-passing fire when the
        # probe is live for this session (opt-in by presence, fail-open --
        # see _hooklib.write_route_ledger). Observation only. This route has
        # no FTS fallback (daemon-only), so source is always "daemon"; a
        # daemon that returned nothing records zero candidates.
        recall_query = " ".join(novel)

        def _route_ledger(result_, shown_: list, cut_: dict) -> None:
            if not hook_input:
                return
            try:
                _build, _rc = _focused_read_conn()
                if not _build:
                    return
                write_route_ledger(
                    "tool_recall", hook_input.get("session_id"),
                    tool_fire_key(hook_input.get("tool_name"), hook_input.get("tool_input")),
                    recall_query, result_, shown_, cut_,
                    source="daemon", default_cut="not_in_pool",
                    extra={"novel_terms": list(novel)},
                    read_conn=_rc,
                    on_failure=lambda _e: _record_owner_failure("focused_ids", _e),
                )
            except Exception as exc:  # e.g. an unserialisable tool_input: fail-open
                print(f"route-ledger: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
            # #3613 S1 / #3670: the shown record is NOT appended here (this runs
            # before anything is printed). The pending ids are handed to main(),
            # which appends once the response was written out, still after the
            # ledger's resolver read its sources. Written whatever the
            # exclusion kill switch says.
            if record_out is not None:
                record_out["ids"] = list(shown_)

        # Gate passed — query the daemon with the novel terms.
        result = _query_recall_daemon(recall_query)
        if not result or not result.get("match_count"):
            _route_ledger(result, [], {})
            _record_fire()
            _write_state()
            _append_ledger(lambda: _ledger_entry("no_matches", 0, [], 0))
            return ""

        recent_ids = set(state.get("recent_ids", []))
        lines = []
        rendered_ids = []
        skipped_recent = 0
        pool = (result.get("special_nodes") or []) + (result.get("top_claims") or [])
        _cut_names: dict = {}  # route ledger: id -> this route's own cut name
        # #3613 live in-context exclusion: drop candidates already in the
        # model's context BEFORE the max_lines cap, so the next ones backfill.
        # Default OFF (`recall_in_context_exclusion.enabled`); fail-open (a
        # resolver that cannot be built or called drops nothing). lesson /
        # cornerstone are exempt on THIS route: a tripwire's job is
        # action-time salience, so it re-shows even what is in context.
        try:
            _build, _rc = _focused_read_conn() if in_context_exclusion_enabled(ENGRAM_HOME) else (False, None)
            if _build:
                _resolver = build_exclusion_resolver(
                    hook_input.get("session_id") if hook_input else None, read_conn=_rc,
                    on_failure=lambda _e: _record_owner_failure("focused_ids", _e))
                _drop = in_context_drop_ids(
                    [n for n in pool if isinstance(n, dict)], _resolver,
                    exempt_types=("lesson", "cornerstone"))
                if _drop:
                    pool = [n for n in pool if not (isinstance(n, dict) and n.get("id") in _drop)]
                    _cut_names.update({i: "in_context" for i in _drop})
        except Exception:
            pass
        for n in pool:
            nid = n.get("id", "?")
            if len(lines) >= cfg["max_lines"]:
                # cfg["max_lines"] reached: every remaining pool entry is cut
                # by that cap (named "max_lines" for the ledger). The cap
                # branch continues past the cap to record those cut names;
                # the rendered lines and counts are unchanged.
                if nid in recent_ids:
                    _cut_names[nid] = "dedup"
                else:
                    _cut_names[nid] = "max_lines"
                continue
            if nid in recent_ids:
                skipped_recent += 1
                _cut_names[nid] = "dedup"
                continue
            kw = n.get("recall_keywords")
            kw_str = (" · ".join(f"`{k}`" for k in kw) + " — ") if isinstance(kw, list) and kw else ""
            body = n.get("recall_summary") or (n.get("claim", "") or "")[:120]
            lines.append(f"  - [{nid}] {kw_str}{body}")
            rendered_ids.append(nid)

        # #3677: the total-budget cut, BEFORE anything below records what was
        # shown. Whole id lines from the END; measured on the real rendering.
        tail_cut = 0
        if max_chars is not None and lines:
            _header = f"[ENGRAM in-turn recall — novel terms: {', '.join(novel)}]\n"
            if len(_header + "\n".join(lines) + _RECALL_FOOTER) > max_chars:
                _limit = max_chars - cut_reserve
                keep = len(lines)
                while keep > 0 and len(_header + "\n".join(lines[:keep]) + _RECALL_FOOTER) > _limit:
                    keep -= 1
                for _nid in rendered_ids[keep:]:
                    _cut_names[_nid] = "tail_budget"
                tail_cut = len(lines) - keep
                lines = lines[:keep]
                rendered_ids = rendered_ids[:keep]
        if record_out is not None:
            record_out["tail_cut_lines"] = tail_cut

        _route_ledger(result, rendered_ids, _cut_names)
        _record_fire()
        if not lines:
            _write_state()
            _outcome = "budget_cut" if tail_cut else "all_suppressed"
            _append_ledger(lambda: _ledger_entry(_outcome, result.get("match_count", 0), [], skipped_recent))
            return ""

        ids = state.get("recent_ids", [])
        if not isinstance(ids, list):
            ids = []
        ids.extend(rendered_ids)
        state["recent_ids"] = ids[-_RECENT_IDS_KEPT:]
        _write_state()
        _append_ledger(lambda: _ledger_entry("rendered", result.get("match_count", 0), rendered_ids, skipped_recent))

        return (
            f"[ENGRAM in-turn recall — novel terms: {', '.join(novel)}]\n"
            + "\n".join(lines)
            + _RECALL_FOOTER
        )
    finally:
        if lock_fd is not None and lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass


def main() -> None:
    """One fire. The owner state (#3665) lives for exactly this call: reset on entry and on
    every way out, so the sites called outside main() (the existing suites) stay direct."""
    global _OWNER_RUN, _OWNER_FLAGS_FAILURE
    _OWNER_RUN = None
    _OWNER_FLAGS_FAILURE = None
    try:
        _main_impl()
    finally:
        _write_owner_stderr_lines()  # every run: class/kind only, whichever way the fire ended
        _OWNER_RUN = None


def _main_impl() -> None:
    global _OWNER_RUN
    LOADER_FAILURES.clear()  # #3503: the record is per run
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        # #3521 row 9: out of scope -- malformed harness input keeps its
        # silent exit (the outer-handler crash notice below does not cover it).
        sys.exit(0)

    # #3665: the flag, read once per fire. A failed helper reads OFF and is reported (the
    # stderr line is written at the end of the fire, with any owner failure).
    _OWNER_RUN = {"on": _owner_reads_enabled(), "conn": None, "failure": None}
    owner_notice, owner_notice_key = "", None

    # #3521 row 9: set right after a response JSON was printed, so the outer
    # handler never prints a second object.
    response_printed = False
    # #3677: WS4 rows are written after the total-budget pass (a late chain drop
    # patches them); the crash handler below flushes any still pending.
    _ws4_rows: list = []
    try:
        match_target, command_target = build_match_views(hook_input)  # alpha#3920
        if not match_target or not match_target.strip():
            sys.exit(0)

        # alpha#1944 step 3 — the deny gate, checked FIRST and computed
        # inside its own fail-open try/except. This is the one place this
        # hook can refuse a tool call at all; every other check in this
        # function only ever ADDS advisory context. A failure loading
        # prevention lessons, or any other exception while computing the
        # deny decision, MUST resolve to "do not deny" — infra failure is
        # never a reason to block a call (Kepler's design point 5: the only
        # reachable failure mode is loud-advisory, never silent no-op and
        # never deadlock). prevention_lessons is computed once and reused
        # by the shadow-block logging below.
        prevention_lessons = []
        deny = None
        try:
            prevention_lessons = load_prevention_lessons()
            if prevention_lessons:
                deny = check_blocking_deny(match_target, prevention_lessons)
        except Exception:
            deny = None

        if deny is not None and deny.get("demoted"):
            # Read-time lease (step 4): do NOT deny — this lesson's
            # override count already crossed the auto-demotion threshold,
            # so the effective state is advisory for this call even though
            # the durable prevention_type write hasn't landed. Log loudly
            # and fall through to the normal advisory path below.
            try:
                import time as _time
                _log_threshold_crossed(_time.time(), deny)
            except Exception:
                pass
            deny = None

        if deny is not None:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": build_deny_reason(deny),
                }
            }
            print(json.dumps(response))
            # Defensive only: sys.exit(0) below raises SystemExit, which the
            # `except Exception` handler cannot see, so this is not load-bearing.
            response_printed = True
            sys.exit(0)

        parts = []

        # #3677 -- final total-budget pass state. _tb_budget None (an unreadable
        # budget) disables the pass: fail-open toward delivering, as before.
        try:
            _tb_budget = get_harness_budgets(detect_harness())["budget"]
        except Exception:
            _tb_budget = None
        _block_call = None        # (head, body, kwargs) of the tripwire block's emit_context call
        _chain_cut = False        # the chain block was dropped by the pass
        _chain_cut_lines = 0      # lines in the dropped chain block
        _chain_in_parts = False   # the chain block is currently in `parts`
        _tripwire_head = TRIPWIRE_HEADER  # alpha#3924: the cluster block swaps in its own header line

        tripwires = load_tripwires()
        hits = check_command(match_target, tripwires, command_target=command_target) if tripwires else []
        # #3503: at most one visible loader-failure line per window; "" when
        # nothing failed (clean path: head/body below stay byte-identical).
        loader_notice, loader_notice_now = _pending_loader_notice()
        # #3665: the owner-side line (a recorded owner failure, or a failed flag helper), at
        # most once per window under its own kind. It joins the loader line in the block's head
        # (owner failures bypass the loader record, so one failure never yields both).
        owner_notice, owner_notice_key = _pending_owner_notice()
        head_notice = "\n".join(x for x in (owner_notice, loader_notice) if x)

        def _late_owner_notice() -> None:
            """The chain and the recall sites run AFTER the block was rendered, so an owner
            failure at either is only known now. Add the notice then: re-render the block with
            it in the head (or create the block when there were no hits), exactly as the
            earlier notice would have been. If that pushes the joined string past the total
            budget while the chain is in it, the chain goes whole (block > chain > recall)."""
            nonlocal parts, _block_call, owner_notice, owner_notice_key, head_notice
            nonlocal _chain_cut, _chain_in_parts, _chain_cut_lines
            if owner_notice_key == OWNER_NOTICE_KEY:
                return  # already carried by the head (the first failure is the one kept)
            text, key = _pending_owner_notice()
            if key != OWNER_NOTICE_KEY:
                return
            owner_notice, owner_notice_key = text, key
            head_notice = "\n".join(x for x in (owner_notice, loader_notice) if x)
            if _block_call is not None:
                head = f"{head_notice}\n{_tripwire_head}" if _block_call[1] else head_notice
                _block_call = (head, _block_call[1], _block_call[2])
                parts[0] = emit_context(_block_call[0], _block_call[1], **_block_call[2])
            else:
                budgets = get_harness_budgets(detect_harness())
                _block_call = (
                    head_notice, [], dict(budget=budgets["budget"], target=TRIPWIRE_BLOCK_CHAR_CEILING))
                parts.insert(0, emit_context(_block_call[0], _block_call[1], **_block_call[2]))
            if _tb_budget is not None and _chain_in_parts and len("\n".join(parts)) > _tb_budget:
                _chain_part = parts[1]
                parts = [p for p in parts if p is not _chain_part]
                _chain_cut, _chain_in_parts = True, False
                _chain_cut_lines = _chain_part.count("\n") + 1
                for _row in _ws4_rows:
                    if "chain_rendered" in _row:
                        _row["chain_cut"], _row["chain_rendered"] = True, False

        # #2858 Piece 1 — order by specificity (pattern length, descending)
        # before rendering/capping. Ties preserve check_command's original
        # (DB-query) order — sorted() is stable, so this is just the sort
        # key, no explicit tiebreak needed. This does NOT change which hits
        # fire, only the order they're considered for the character-ceiling
        # selection below (a longer, more specific pattern is judged more
        # likely to be the useful nudge when not everything can fit).
        #
        # alpha#3270/#3112 — pattern length alone is a proxy for specificity
        # that is UNCORRELATED with how much the lesson's author wants it to
        # win a scarce slot: measured twice independently, a short pattern
        # guarding a destructive/costly action (git stash; a comment-vs-
        # review-state gh call) lost its slot to a longer, broad, low-cost
        # reminder. Kepler's ruling: rank the author-set tripwire_priority
        # field first ("high" before "normal"), THEN the existing
        # -len(pattern) tiebreak within a tier. A hit set where every
        # priority is "normal" (every lesson minted before this field
        # existed, or one that never opted in) reduces to exactly today's
        # pure-length order -- this is a strict refinement of the old key,
        # not a replacement of it.
        #
        # Reviewer-fairy round 1 (PR #2860): this resort also changes the
        # order `shown_hits` carries into render_triggered_principles below
        # (Piece 3) -- including in the ZERO-CUT case, where shown_hits ==
        # hits by MEMBERSHIP but not by the pre-sort DB-query ORDER. That
        # section only uses order to decide which "generalizes" head prints
        # first when several hits share one, so this is a presentation-only
        # effect (which head-group appears first), never a correctness one
        # (which heads appear, or what they say) -- not worth threading a
        # second, unsorted hits list through main() just to preserve an
        # ordering nothing downstream depends on.
        shown_hits = []
        if hits:
            hits = sorted(
                hits,
                key=lambda h: (
                    0 if h.get("priority") == "high" else 1,
                    -len(h.get("pattern") or ""),
                ),
            )
            # #2863 WS2 — render via the shared _hooklib.emit_context()
            # helper instead of this hook's own former select_and_cap_hits/
            # _render_tripwire_block/_build_trailer (removed above).
            # `bullets` is the SAME ordered (most-specific-first) list of
            # per-hit strings the old _render_tripwire_block used to build
            # inline; emit_context() now owns the capping (which prefix
            # fits under `target`) AND the trailer/header rendering in one
            # call -- verified (see PR description) to select and render
            # byte-identically to the old mechanism on this file's own test
            # fixtures, including the always-show-at-least-one-hit case
            # (allow_single_overflow=True reproduces that guarantee: the
            # single most-important hit is still shown in full as long as
            # it fits the harness's hard `budget`, only truncated in-band
            # in the pathological case where even one hit alone exceeds
            # that hard ceiling -- something the old mechanism had no
            # protection against at all).
            bullets = [_tripwire_bullet(h) for h in hits]

            # alpha#3924: more than CLUSTER_THRESHOLD co-firing lessons render as ONE cluster block
            # (a one-line summary per lesson + one full nudge) instead of K+ bullets. Prepared inside
            # its own guard: any failure falls back to today's rendering, never to silence.
            _cluster_kept: list = []
            _cluster_nudge_idx = None
            _cluster_path = ""
            _cluster_nudge_partial = False
            if len(hits) > CLUSTER_THRESHOLD:
                try:
                    _ordered, _note = _cluster_order(hits)
                    _extras = {t[2]: (t[5] if len(t) > 5 else None, t[6] if len(t) > 6 else None)
                               for t in tripwires}
                    _lines = [
                        f"  - {h.get('node_id', '?')}: "
                        f"{_cluster_summary(*_extras.get(h.get('node_id'), (None, None)))}"
                        for h in _ordered
                    ]
                    # Every clustered hit's full nudge goes to the overflow file (the inline block
                    # shows one); its path rides in the header so the other nudges stay reachable.
                    _path = write_tripwire_overflow(_ordered)
                    # alpha#3955: the head nudge is never dropped whole -- it is rendered truncated to
                    # the room the one-liners leave, and the header says which of the three it is.
                    _cb = get_harness_budgets(detect_harness())["budget"]
                    _cluster_hdr, _bullets, _state = _cluster_plan(
                        len(_ordered), _cluster_trigger(hook_input), _note, _path, _lines,
                        _tripwire_bullet(_ordered[0]), head_notice, _cb, max(1, _cb - _TAIL_CUT_RESERVE))
                    hits, _cluster_path = _ordered, _path
                    _tripwire_head = _cluster_hdr
                    _cluster_nudge_idx = len(_lines)
                    _cluster_nudge_partial = _state != "full"
                    bullets = _bullets
                except Exception:
                    _cluster_nudge_idx = None

            # emit_context() calls this ONLY with the item strings it
            # itself decided to cut/truncate -- always either a trailing
            # SUFFIX of `bullets` (the ordinary cut case), or, in the rare
            # case where even the single most-important hit's own block
            # exceeds the harness's hard budget, the ENTIRE list (that
            # hit's own FULL untruncated text is included alongside the
            # genuinely-cut rest, so nothing is unrecoverable -- see
            # emit_context()'s own docstring, "Stage 3"). Reconstructed
            # back to hit dicts BY POSITION (never by nudge-text VALUE,
            # which could collide if two different hits happen to share
            # identical nudge text) so write_tripwire_overflow()'s existing
            # node_id-keyed on-disk format is preserved byte-for-byte.
            cut_hits_holder: dict = {"cut": []}

            def _tripwire_overflow_writer(items: list) -> str:
                cut_n = len(items)
                cut = hits[-cut_n:] if 0 < cut_n <= len(hits) else []
                cut_hits_holder["cut"] = cut
                if not cut:
                    return ""
                path = write_tripwire_overflow(cut)
                if not path:
                    return ""
                # alpha#3112's cheapest independent-of-ranking mitigation:
                # name the cut node IDs right in the trailer, not just a
                # count -- a reader deciding whether to open the overflow
                # file at all needs to know WHICH lessons got cut, and the
                # cases this bug actually bit on (git-stash; comment-vs-
                # review-state) are exactly the ones worth that one glance.
                # Appended space-separated after the path: _build_context_
                # trailer's own overflow_path capture group is `(\S+)`, so
                # this parenthetical never gets swallowed into the path
                # itself and every existing caller/test that reads the
                # trailer's path group is unaffected.
                ids = ", ".join(h.get("node_id", "?") for h in cut)
                return f"{path} ({ids})"

            # #3677: remembers the first call's overflow path, so the (rare)
            # corner re-render of this block under a smaller budget does not write
            # a second overflow file for the same cut items.
            _ow_memo: dict = {}

            def _cached_overflow_writer(items: list) -> str:
                key = tuple(items)
                if key not in _ow_memo:
                    _ow_memo[key] = _tripwire_overflow_writer(items)
                return _ow_memo[key]

            budgets = get_harness_budgets(detect_harness())
            _block_call = (
                # #3503: the failure line, when due, is part of the head (so it
                # shares emit_context's target/budget accounting and sits above
                # the bullets); unchanged TRIPWIRE_HEADER otherwise.
                f"{head_notice}\n{_tripwire_head}" if head_notice else _tripwire_head,
                bullets,
                (dict(
                    budget=budgets["budget"],
                    target=TRIPWIRE_BLOCK_CHAR_CEILING,
                    allow_single_overflow=True,
                    overflow_writer=_cached_overflow_writer,
                ) if _cluster_nudge_idx is None else dict(
                    # alpha#3924: the one-liners alone can exceed the 1800-char inline ceiling
                    # (19 x ~130), so the cluster block is bounded by the harness budget; if even
                    # those do not fit, emit_context cuts whole lines from the tail and says so in
                    # its trailer. Every full nudge is already in the overflow file (written above).
                    budget=budgets["budget"],
                    target=max(1, budgets["budget"] - _TAIL_CUT_RESERVE),
                    allow_single_overflow=True,
                    overflow_writer=lambda _items: _cluster_path,
                    kept_out=_cluster_kept,
                )),
            )
            parts.append(emit_context(_block_call[0], _block_call[1], **_block_call[2]))

            # shown_hits feeds render_triggered_principles below (Piece 3)
            # — never the full, uncapped `hits`, so a "[triggered
            # principles]" line never cites a lesson whose own nudge text
            # was cut from the visible block. Derived from cut_hits_holder
            # (set as a side effect of the emit_context() call above, via
            # the overflow_writer): everything NOT cut is shown, except the
            # pathological case above where the writer receives ALL hits
            # (item 0's full text for recovery + the genuinely-cut rest) —
            # there, item 0 is still "shown" (marker-truncated in-band, not
            # dropped), so it is never left out of the principles section.
            cut_hits = cut_hits_holder["cut"]
            shown_hits = hits[: len(hits) - len(cut_hits)] if cut_hits else hits
            if not shown_hits and hits:
                shown_hits = hits[:1]
            if _cluster_nudge_idx is not None:
                # alpha#3924: in cluster mode only the lesson whose FULL nudge is in the visible
                # block counts as shown (a one-liner is not its nudge text); none if the budget
                # cut that nudge. kept_out lists the body items emit_context rendered in full.
                # alpha#3955: a truncated or absent nudge also counts as not shown (under-report).
                shown_hits = (hits[:1] if _cluster_nudge_idx in _cluster_kept
                              and not _cluster_nudge_partial else [])
        elif head_notice:
            # #3503: a loader failed and there are no hits, so the block would
            # otherwise be silent -- the line IS the block, still rendered by
            # emit_context (trailer first, same budget rules). #3665: likewise for
            # the owner-side line.
            budgets = get_harness_budgets(detect_harness())
            _block_call = (
                head_notice,
                [],
                dict(budget=budgets["budget"], target=TRIPWIRE_BLOCK_CHAR_CEILING),
            )
            parts.append(emit_context(_block_call[0], _block_call[1], **_block_call[2]))

        # #2753 epic WS2 (D3 point 1) — generalization-chain render, ADDITIVE
        # after the nudge block above and independently guarded: a failure
        # computing the principles section must NEVER suppress the existing
        # nudge (already appended above), and vice versa (the existing nudge
        # append above already ran and is untouched by anything below).
        #
        # #2858 Piece 3 — only SHOWN hits feed this, never the full/uncapped
        # `hits` list: a principle-chain citation for a hit whose own nudge
        # text was cut from the visible block above would reference
        # something the reader can't see the reason for.
        if hits:
            shown_lesson_ids = [
                h.get("node_id") for h in shown_hits if isinstance(h, dict)
            ]
            heads_by_lesson = {nid: [] for nid in shown_lesson_ids if nid}
            try:
                principles_block = render_triggered_principles(
                    shown_hits, heads_out=heads_by_lesson,
                )
            except Exception:
                principles_block = ""

            # #2753 WS4 -- pre-registered chain A/B (issue #2753 comments
            # 5602340998 + 5602392744). CONTRACT A: whenever the gate below
            # is INACTIVE, this reduces to EXACTLY the pre-WS4 two lines it
            # replaces (the `if principles_block: parts.append(...)` in the
            # `if not ws4_cfg["active"]:` branch) -- heads_by_lesson is
            # computed unconditionally above because render_triggered_
            # principles' own returned string is never affected by
            # heads_out (CONTRACT B, see that function's docstring), so
            # populating it costs nothing and needs no gate of its own.
            ws4_cfg = _ws4_config()
            if not ws4_cfg["active"]:
                if principles_block:
                    # #3677: whole-block rule, decided where the chain is appended.
                    _delivered, _cl = _place_chain(parts, principles_block, _tb_budget)
                    if _delivered:
                        parts.append(principles_block)
                        _chain_in_parts = True
                    else:
                        _chain_cut, _chain_cut_lines = True, _cl
            else:
                # Round 2 (reviewer-fairy suggestion #1 on PR #3166): arm
                # computation and ledger-row construction now get their OWN
                # narrow guards, instead of one broad try/except wrapping
                # both plus the write -- a bug in EITHER must still leave a
                # visible ledger row behind (arm="error" + an `error`
                # string) rather than silently dropping the fire with zero
                # signal. For an experiment, a silent hole in the
                # denominator is worse than a loud one. CONTRACT A is
                # untouched (this whole `else` only runs when ACTIVE).
                # CONTRACT C's fail-open write stays inside
                # _append_ws4_fire_ledger, unchanged.
                nudge_chars = len(parts[-1]) if parts else 0
                raw_tool_use_id = None
                # Known limit (PR 3948 review): in cluster mode only the full-nudge lesson is
                # `shown`, and `cut_hits` stays empty (no hit was cut, the rest appear as
                # one-liners), so the one-liner-only lessons are in NEITHER list here. They are
                # not a hole in the WS4 denominator by accident: their full nudge was not shown.
                cut_lesson_ids = []
                arm = "na"
                nonce = None
                draw_basis = None
                chain_rendered = False
                arm_error = None
                appended_chain = False
                try:
                    raw_tool_use_id = hook_input.get("tool_use_id")
                    cut_lesson_ids = [
                        h.get("node_id") for h in cut_hits if isinstance(h, dict)
                    ]
                    participating = bool(principles_block)
                    if participating:
                        if isinstance(raw_tool_use_id, str) and raw_tool_use_id:
                            nonce = raw_tool_use_id
                            draw_basis = "tool_use_id"
                        else:
                            import uuid as _uuid
                            nonce = _uuid.uuid4().hex
                            draw_basis = "uuid4"
                        import hashlib as _hashlib
                        draw = int(
                            _hashlib.sha256(
                                f"{ws4_cfg['seed']}:{nonce}".encode()
                            ).hexdigest(),
                            16,
                        ) & 1
                        arm = "chain" if draw == 1 else "control"
                        if arm == "chain":
                            # #3677: the arm draw above is untouched by the cut.
                            _delivered, _cl = _place_chain(parts, principles_block, _tb_budget)
                            if _delivered:
                                parts.append(principles_block)
                                appended_chain = True
                                chain_rendered = True
                                _chain_in_parts = True
                            else:
                                _chain_cut, _chain_cut_lines = True, _cl
                        # control arm: principles_block computed but withheld.
                except Exception as exc:
                    # Dev-parity fallback -- render exactly as the INACTIVE
                    # branch above would (append the chain iff non-empty),
                    # then mark the row so the fire is still visible instead
                    # of silently dropped. Guard against double-appending if
                    # the exception struck AFTER a successful chain-arm
                    # append above.
                    if principles_block and not appended_chain and not _chain_cut:
                        _delivered, _cl = _place_chain(parts, principles_block, _tb_budget)
                        if _delivered:
                            parts.append(principles_block)
                            chain_rendered = True
                            _chain_in_parts = True
                        else:
                            _chain_cut, _chain_cut_lines = True, _cl
                    arm = "error"
                    arm_error = f"{type(exc).__name__}: {str(exc)[:200]}"

                # Round 3 (#3173 reviewer comment 5858155739): the
                # timestamp computation used to sit here, between the two
                # guards -- unguarded by either. An exception there (e.g.
                # a broken `time`/`datetime` shim) reached main()'s outer
                # `except Exception: sys.exit(0)`, printing nothing and
                # silently dropping the nudge already appended to `parts`.
                # It now lives INSIDE the row-construction guard below, so
                # a failure there is caught the same way a `_build_ws4_
                # ledger_entry` failure already is. `now`/`ts_iso` default
                # to None so the minimal fallback row is still written with
                # whatever timestamp is available (i.e. none) rather than
                # dropping the fire.
                now = None
                ts_iso = None
                # #3677: the row is written AT CREATION, as on dev (a hook killed
                # during the recall keeps it), except in the sliver where a late
                # chain drop (the marker not fitting once the recall is gone) could
                # still change it: only then is it held until the pass has settled.
                _defer_row = False
                try:
                    _defer_row = (_tb_budget is not None and
                                  len("\n".join(parts)) > _tb_budget - _TAIL_CUT_RESERVE)
                except Exception:
                    _defer_row = False
                try:
                    import time as _time
                    from datetime import datetime as _ws4_dt, timezone as _ws4_tz
                    now = _time.time()
                    ts_iso = _ws4_dt.fromtimestamp(now, tz=_ws4_tz.utc).isoformat()
                    entry = _build_ws4_ledger_entry(
                        ts=now, ts_iso=ts_iso, hook_input=hook_input,
                        raw_tool_use_id=raw_tool_use_id, nonce=nonce,
                        draw_basis=draw_basis, seed=ws4_cfg["seed"], arm=arm,
                        shown_lesson_ids=shown_lesson_ids,
                        cut_lesson_ids=cut_lesson_ids,
                        heads_by_lesson=heads_by_lesson,
                        nudge_chars=nudge_chars,
                        principles_block=principles_block,
                        chain_rendered=chain_rendered, error=arm_error,
                        chain_cut=_chain_cut,
                    )
                    if _defer_row:
                        _ws4_rows.append(entry)
                    else:
                        _append_ws4_fire_ledger(entry)
                except Exception as row_exc:
                    # Row construction itself failed -- still count the
                    # fire with a MINIMAL row (fixed 8-field shape) rather
                    # than dropping it with zero signal. `now`/`ts_iso` may
                    # still be None here if the timestamp computation above
                    # is what raised -- the minimal row is written with
                    # whatever timestamp is available. The write below
                    # keeps its own internal fail-open behaviour.
                    _fallback_row = {
                        "schema_version": 1,
                        "experiment": "ws4_chain_ab",
                        "ts": now,
                        "ts_iso": ts_iso,
                        "session_id": hook_input.get("session_id"),
                        "tool_use_id": raw_tool_use_id,
                        "arm": arm,
                        # Deliberate (round 3, #3173 reviewer comment
                        # 5858155739): when BOTH the arm-computation guard
                        # above and this row-construction guard fail on the
                        # same fire, keep the ARM error rather than the row
                        # error -- `or` short-circuits on `arm_error` when
                        # it is already a truthy string. This is the more
                        # informative of the two: it means the coin-flip
                        # decision itself was corrupted, not just its
                        # downstream bookkeeping.
                        "error": arm_error or f"{type(row_exc).__name__}: {str(row_exc)[:200]}",
                    }
                    if _defer_row:
                        _ws4_rows.append(_fallback_row)
                    else:
                        _append_ws4_fire_ledger(_fallback_row)

        # #3665: an owner failure at the chain site is only known now; add the notice before the
        # recall is sized (so its room accounts for it). Fail-open like everything below.
        try:
            _late_owner_notice()
        except Exception:
            pass

        # alpha#1944 step 2 — shadow-block logging. Independently guarded,
        # same posture as in-turn recall below: a logging failure must never
        # suppress a tripwire hit, and a tripwire failure must never suppress
        # this. Nothing here changes what the agent sees — shadow lessons
        # (and any blocking lesson that WAS overridden, since deny already
        # returned above otherwise) still only ever inject the advisory
        # nudge above; this purely appends to the promotion-gate ledger.
        try:
            import time as _time
            shadow_hits = (
                check_shadow_block(match_target, prevention_lessons, hook_input)
                if prevention_lessons else []
            )
            _append_prevention_ledger(_time.time(), shadow_hits)
        except Exception:
            pass

        # In-turn ambient recall (#1690) — independently guarded; a recall
        # failure must never suppress a tripwire hit (and vice versa).
        # #3613: lesson matching above/below stays on the RAW match_target (a
        # tripwire must fire on the raw action). Only the in-turn recall's
        # term source is narrowed to the marked span; an empty marker skips it.
        try:
            _recall_state, _recall_text = marker_recall_query(match_target)
        except Exception:
            _recall_state, _recall_text = "none", match_target
        # #3670: the shown record is appended only AFTER the response was
        # written out. check_in_turn_recall leaves the pending ids in
        # _pending_shown; a call that raised discards them (the model saw no
        # recall text), and a fire that passed the gate but rendered nothing
        # leaves ids == [] so the per-fire line (prompt_index) stays continuous.
        _pending_shown: dict = {}
        # #3677: the room the recall may take, decided from what is already in
        # `parts`, so the recall is trimmed (and its state written) ONCE, for the
        # survivors. After a chain cut the reserve is already owed (a cut happened);
        # otherwise it is taken only if the recall itself has to be cut.
        _recall_kw: dict = {}
        try:
            if _tb_budget is not None:
                _room = _tb_budget - (len("\n".join(parts)) + 1 if parts else 0)
                if _chain_cut:
                    _recall_kw = {"max_chars": _room - _TAIL_CUT_RESERVE}
                else:
                    _recall_kw = {"max_chars": _room, "cut_reserve": _TAIL_CUT_RESERVE}
        except Exception:
            _recall_kw = {}
        try:
            if _recall_state == "empty":
                recall = ""
            else:
                recall = check_in_turn_recall(_recall_text, hook_input, _pending_shown, **_recall_kw)
        except Exception:
            recall = ""
            _pending_shown.clear()
        # #3665: and an owner failure at the recall site is only known after it ran.
        try:
            _late_owner_notice()
        except Exception:
            pass
        if recall:
            parts.append(recall)

        # #3677: the first-line marker, counted inside the budget. Fail-open: an
        # exception here leaves `parts` exactly as it was (dev-shaped output).
        try:
            _recall_cut_n = int(_pending_shown.get("tail_cut_lines", 0) or 0)
            if _tb_budget is not None and (_recall_cut_n or _chain_cut):
                _final = _apply_tail_cut(parts, _recall_cut_n, _chain_cut_lines, _block_call is not None)
                if len("\n".join(_final)) > _tb_budget and _chain_in_parts:
                    # The chain fit by the whole-block rule but the clause does not,
                    # and the recall is already gone (its room was below the
                    # reserve). Priority is block > chain > recall, so the chain
                    # goes too, whole: truncating a lesson hit in-band to keep a
                    # chain would be the worse trade.
                    _chain_part = parts[1] if (_block_call is not None and len(parts) > 1) else parts[0]
                    parts = [p for p in parts if p is not _chain_part]
                    _chain_cut, _chain_in_parts = True, False
                    _chain_cut_lines = _chain_part.count("\n") + 1
                    for _row in _ws4_rows:
                        if "chain_rendered" in _row:
                            _row["chain_cut"], _row["chain_rendered"] = True, False
                    _final = _apply_tail_cut(parts, _recall_cut_n, _chain_cut_lines, _block_call is not None)
                if len("\n".join(_final)) > _tb_budget and _block_call is not None:
                    # Corner: the block alone is within the reserve of the budget.
                    # Re-render it (deterministic) under budget - reserve; the
                    # overflow path is reused, not rewritten (see _cached_overflow_writer).
                    _new_block = emit_context(
                        _block_call[0], _block_call[1],
                        **{**_block_call[2], "budget": _tb_budget - _TAIL_CUT_RESERVE})
                    parts = [_new_block] + parts[1:]
                    _final = _apply_tail_cut(parts, _recall_cut_n, _chain_cut_lines, True)
                if len("\n".join(_final)) <= _tb_budget:
                    parts = _final
        except Exception:
            pass
        # WS4 rows are written once the pass has settled (a late chain drop patches them).
        while _ws4_rows:
            _append_ws4_fire_ledger(_ws4_rows.pop(0))

        if not parts:
            # Nothing is printed, nothing was shown: the empty-ids line is true.
            if "ids" in _pending_shown:
                try:
                    append_shown_record("tool_recall", hook_input.get("session_id"),
                                        _pending_shown["ids"])
                except Exception:  # fail-open: a non-dict hook_input etc.
                    pass
            sys.exit(0)

        additionalContext = "\n".join(parts)
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": additionalContext,
            }
        }
        print(json.dumps(response))
        response_printed = True
        if loader_notice:
            # #3503: the window is consumed only once the line has actually
            # been written out; an exit before this point (or a failed
            # print/flush) leaves no stamp, so the next failure shows it again.
            try:
                sys.stdout.flush()
            except Exception:
                sys.exit(0)
            _mark_loader_notice_shown(loader_notice_now)
        if owner_notice:
            # #3665: the same ordering for the owner-side window (its own stamp).
            try:
                sys.stdout.flush()
            except Exception:
                sys.exit(0)
            mark_probe_notice(ENGRAM_HOME, owner_notice_key)
        # #3670: the shown record is appended only once the response has been
        # written out AND flushed; a failed print/flush records nothing, so no
        # unseen id is ever recorded. A printed-but-oversized string (the
        # harness may cut its tail, where the recall sits) records empty ids.
        try:
            sys.stdout.flush()
        except Exception:
            sys.exit(0)
        if "ids" in _pending_shown:
            # The recall block is appended LAST and has no emit_context cut of
            # its own, but the joined string is subject to the HARNESS tail cut:
            # when it exceeds the harness budget the recall may not have reached
            # the model, so record EMPTY ids (the line still lands, keeping
            # prompt_index continuous). Fail-open toward under-recording: a
            # budget lookup that raises is treated as over budget.
            # NOTE (Borges, #3674 review): get_harness_budgets()["budget"]
            # (9,500 on claude-code) sits a little UNDER the measured harness
            # ceiling, so this check also under-records in that narrow band.
            # That is the intended direction: do NOT "fix" it to the measured
            # number.
            # #3677: since the total-budget pass above this can no longer fire
            # (the joined string is <= budget by construction); kept as a guard.
            try:
                _fits = len(additionalContext) <= get_harness_budgets(
                    detect_harness())["budget"]
            except Exception:
                _fits = False
            try:
                append_shown_record("tool_recall", hook_input.get("session_id"),
                                    _pending_shown["ids"] if _fits else [])
            except Exception:  # fail-open: a non-dict hook_input etc.
                pass
        sys.exit(0)

    except Exception as exc:
        # #3521 row 9: a crash here used to be byte-identical to a healthy
        # "nothing matched" run (exit 0, empty stdout, empty stderr). Now: one
        # stderr line always, and -- only if no response was printed yet and the
        # rate-limit window is open -- one UNKNOWN context line. Never denies,
        # always exits 0, and the handler itself must never raise.
        try:
            while _ws4_rows:  # #3677: a pending WS4 row is still a fire
                _append_ws4_fire_ledger(_ws4_rows.pop(0))
        except Exception:
            pass
        try:
            reason = f"{type(exc).__name__}: {exc}"
            write_probe_unknown_stderr("engram-lesson-tripwire-hook", "tripwire-run", reason)
            if not response_printed and probe_notice_due(ENGRAM_HOME, "tripwire-run"):
                line = probe_unknown_line(
                    "lesson-tripwire", reason,
                    "tripwire checks were skipped for this call",
                    prefix="[lesson-tripwire] ",
                )
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": line,
                    }
                }))
                sys.stdout.flush()
                mark_probe_notice(ENGRAM_HOME, "tripwire-run")
        except BaseException:
            # The print/flush above may have failed because the stdout reader is
            # gone; a normal sys.exit(0) would then fail again in interpreter
            # shutdown's stdout flush and replace the exit code (120). os._exit
            # skips that flush; stderr is flushed first so the line is not lost.
            try:
                sys.stderr.flush()
            except BaseException:
                pass
            # Only a hook run as a script may skip shutdown: an in-process caller
            # (such as a test) must get a catchable SystemExit, never a killed process.
            if __name__ == "__main__":
                os._exit(0)
        sys.exit(0)  # hook failures must never block a tool call


if __name__ == "__main__":
    main()
