#!/usr/bin/env python3
"""Stop hook: scrape last assistant message for ENGRAM node-id mentions and
bump utility_score for every mentioned ID found in the substrate DB.

Implements alpha #177 area 4 (broader credit-assignment per the maintainer):
the prior utility-reward mechanism only fired when a recalled node was
later cited as a `derives_from` premise in engram_derive (server.py:2694).
This misses prose-mention, inline ID-citation, and informed-but-uncited
use. The stop hook captures the prose-mention / inline-citation cases
by scanning the response text after every assistant turn and bumping
utility_score for any mentioned node IDs directly — no window
intersection required.

Mechanism (V1.2 — simplified in PR #215 after #214 retired recall_window; the maintainer)
---------
1. Read `last_assistant_message` from Claude Code's Stop-hook stdin
   payload (synchronous, not subject to the JSONL flush race that bit
   the deference-detector pre-2026-05-07 per ob_NNNN / ls_NNNN).
2. Collect all assistant text blocks from the current turn via JSONL
   walk (collect_turn_text).
3. Extract node-shaped tokens via engram_ids.find_node_ids (SSoT
   regex shipped in PR #212, supports \\d{4,} past 9999).
4. Apply the shared Q-update (engram_restamp.bump_utility:
   `Q_new = Q_old + α(1 - Q_old)`) with α = engram_restamp.USE_ALPHA["mention"]
   -- the one use-action table, not a private constant. If the DB has
   the ID it bumps; if not (e.g. `in_NNNN` shape-false-positive), the
   shared update silently skips it.

Note: the original V1.0 hook intersected mentioned IDs with a
recall_window (substrate-vs-elsewhere-context tracking). This was
removed in PR #214 because post-cutover node IDs always come from
substrate context — agents don't have training-time knowledge of
session-specific IDs like ob_XXXX. The intersection was a no-op
tautology. PR #214 also eliminated recall_window.json entirely,
making the file-path reference a dead constant.

Restamp (Lei, 2026-10-08, engram-alpha#2005 section 3): a mention also
restamps recall_turn + importance_score (NOT recall_count), so a node used by
id in prose stops aging. The whole "bump, then restamp" sequence is the one
shared engram_restamp.use_reward (action "mention"); its restamp runs in a
SAVEPOINT and a failure returns as `restamp_error` -- this hook commits (the
bump always survives) and logs it to LOG_PATH, never silent. If the shared
module fails to IMPORT, "bump_utility failed" is logged and the bump is lost.

Idempotency: a node mentioned twice in the same response receives a
single bump (find_node_ids dedups in order). Mentioned across two
turns → two bumps (matches MemRL repeated-engagement intent).

Independent of engram_derive's existing credit path: derive scans
premise IDs, this hook scans prose. Both can fire on the same turn
for the same ID — that's the design (deliberate citation + prose
narration = stronger engagement signal than either alone).

Best-effort: any failure swallows silently. Behavioral hooks must
never block the session.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
from _hooklib import engram_home_or_exit  # noqa: E402
ENGRAM_HOME = str(engram_home_or_exit())


_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)
from _hooklib import atomic_write_json, resolve_runtime_dir
from _hooklib import read_state_json_classified  # #3521 row 40
from _hooklib import import_owner_flags as _hooklib_import_owner_flags  # #3822 Stage B
# #3628: every read and the log append are non-blocking and regular-file-only.
from _hooklib import open_regular_text, open_regular_text_append

# Resolves the directory containing engram_ids.py -- hoisted to
# _hooklib.resolve_runtime_dir (gh#2570); this hook's own sentinel
# (engram_ids.py, distinct from engram_client.py/engram_log_emitter.py used
# by other hooks) is preserved unchanged.
_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_ids.py")

# Ensure runtime modules are importable. Uses the plugin-aware resolution
# above instead of the retired scatter-layout sys.path.insert(0, ENGRAM_HOME).
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

# Separate resolve call for the emitter (gh#2570 §1): an import of
# engram_log_emitter must probe for engram_log_emitter, not piggyback on
# whatever directory engram_ids.py happened to resolve to above -- one call
# per module actually imported, even when both currently resolve to the
# same plugin root in practice.
_EMITTER_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_log_emitter.py")
if _EMITTER_RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _EMITTER_RUNTIME_DIR)

# alpha #2574 WS3: separate resolver for engram_telemetry_store.py -- per
# _hooklib.resolve_runtime_dir's own docstring, "one call per module you
# actually import; do not collapse two different imports onto one sentinel
# just because they happen to live in the same directory today" (gh#2570).
_TELEMETRY_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_telemetry_store.py")
if _TELEMETRY_RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _TELEMETRY_RUNTIME_DIR)
try:
    from engram_telemetry_store import write_telemetry_record
except Exception:
    def write_telemetry_record(manifest_id, event_type, fields=None):  # pragma: no cover -- import-failure fallback
        return False

# alpha #2574 WS3: this hook's TRIGGER-ENTRY site literal (participation
# manifest_id) for engram.telemetry.reached -- see main()'s first line.
_MAIN_MANIFEST_ID = "hooks/claude/engram-utility-credit-mention-stop.py:main"
_ENACTMENT_SITE = "utility-credit-mention-stop.enactment"
_HOOK_FIRE_SITE = "utility-credit-mention-stop.hook_fire"

# Shared use-action table + Q-update + restamp (recall_turn + importance_score;
# NOT recall_count) -- the same module deliberate recall
# (engram_query._refresh_recall, count_recall=True) and engram_core's
# _utility_reward use, so the alpha, the Q formula and the forgetting-clock
# formula each have one home. Own resolver call + sentinel per imported module
# (gh#2570). On import failure `engram_restamp` is None: bump_utility raises
# (credit_mentions logs "bump_utility failed") -- never a silent skip.
_RESTAMP_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_restamp.py")
if _RESTAMP_RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RESTAMP_RUNTIME_DIR)
try:
    import engram_restamp
    _RESTAMP_IMPORT_ERROR = ""
except Exception as _e:  # pragma: no cover -- exercised via engram_restamp=None in tests
    engram_restamp = None
    _RESTAMP_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

KNOWLEDGE_DB = os.path.join(ENGRAM_HOME, "knowledge.db")
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")
LOG_PATH = os.path.join(ENGRAM_HOME, "utility-credit-mention.log")

_ACTION = "mention"  # this hook's key in engram_restamp.USE_ALPHA / RESTAMP_ACTIONS


def _mention_alpha() -> float:
    """alpha for a mention, read from the shared table at call time."""
    if engram_restamp is None:
        raise RuntimeError(f"engram_restamp unavailable ({_RESTAMP_IMPORT_ERROR})")
    return engram_restamp.USE_ALPHA.get(_ACTION, engram_restamp.USE_ALPHA_DEFAULT)

# #1698 slice 3 §2 — enactment detection (mention-proxy). Paths mirror the
# unified principle-trigger registry/state that engram-surface-hook.py owns
# (same $ENGRAM_HOME, same filenames — this Stop hook reads them, never
# rebuilds them).
PRINCIPLE_TRIGGERS_PATH = os.path.join(ENGRAM_HOME, "principle_triggers.json")
PRINCIPLE_TRIGGER_STATE_PATH = os.path.join(ENGRAM_HOME, "principle-trigger-state.json")

# Open question resolved (spec §2, option (a)): this Stop hook is a
# different process/event (Stop) from the surface hook (UserPromptSubmit)
# and has no shared in-memory counter. The surface hook already persists
# prompts_since_compaction to this exact file on every prompt
# (read_prompt_counter/write_prompt_counter in engram-surface-hook.py) so it
# survives the per-prompt subprocess boundary — reuse it here (read-only)
# rather than inventing a second counter that could drift from the surface
# hook's.
PROMPT_COUNTER_PATH = os.path.join(ENGRAM_HOME, "prompt-counter.json")

_ENACTMENT_WINDOW_PROMPTS = 10  # "no trigger fire... within the last k prompts" (design doc §4)


def _mention_alpha_or_none():
    try:
        return _mention_alpha()
    except Exception:
        return None


# --- #3822 Stage B: the owner write, behind `engramd.utility_credit_writes_via_owner` ---------------------------
# The flag is read ONLY through engramd_owner_flags.flag_on over the module-level CONFIG_PATH. Flag OFF (the default) is
# today's code path, unchanged. Flag ON: the bump goes to the owner's typed `use_reward` op and the store is never opened
# from here; every non-success outcome is a loud loss, never a fall back to the store.
_OWNER_FLAG_KEY = "utility_credit_writes_via_owner"
_OWNER_FLAGS_FAILURE = None  # class NAME of a flag-helper failure (reset per read); bump_utility reports it

# Time. The Stop hook is registered with timeout 5 s (hooks/hooks.json). Both owner attempts together may wait at most
# OWNER_WRITE_BUDGET_S, leaving OWNER_WRITE_RESERVE_S for interpreter start, telemetry, enactment detection and the
# emitter: BUDGET + RESERVE = 3.5 + 1.5 = 5.0 <= 5. Attempt 1 waits min(T, BUDGET); the single retry (same request_id,
# only after an INDETERMINATE first attempt) waits min(T, what is left) and only if at least OWNER_RETRY_MIN_S is left.
OWNER_WRITE_BUDGET_S = 3.5
OWNER_WRITE_RESERVE_S = 1.5
OWNER_RETRY_MIN_S = 0.25
# T is the client's named bound for this op, engramd_client.OWNER_WRITE_CLIENT_TIMEOUT (2.0 s; the owner's lock wait is 1.0 s,
# so a typed `busy` arrives inside it). Imported by name at call time: the hook keeps no copy of the number. Worst case with
# the retry: attempt 1 waits min(2.0, 3.5) = 2.0, the retry min(2.0, 3.5 - 2.0 = 1.5) = 1.5, total 3.5 = the budget.
_monotonic = time.monotonic  # module-level so tests can drive the clock


class _OwnerWriteLost(Exception):
    """The bump was not (or not verifiably) applied through the owner. `args[0]` is the complete log line."""


def _import_owner_flags():
    """Thin alias; the body lives once in `_hooklib.import_owner_flags`. A module attribute because tests patch it."""
    return _hooklib_import_owner_flags(ENGRAM_HOME)


def _owner_writes_enabled() -> bool:
    """`engramd.utility_credit_writes_via_owner`, read ONLY through engramd_owner_flags.flag_on. Anything but the JSON
    boolean true is OFF. ANY exception (and a SystemExit) importing the helper or calling `flag_on` reads OFF -- the hook
    cannot know the flag is ON -- and its CLASS NAME (never the message) is recorded in `_OWNER_FLAGS_FAILURE`, which
    bump_utility reports: recorded, not silent. KeyboardInterrupt is not caught.

    Flag OFF still performs this read on every fire (the helper import plus one config read), so "the OFF path is unchanged"
    means the same results, the same store content and the same log content, not that nothing extra runs."""
    global _OWNER_FLAGS_FAILURE
    _OWNER_FLAGS_FAILURE = None
    try:
        engramd_owner_flags = _import_owner_flags()
        return engramd_owner_flags.flag_on(_OWNER_FLAG_KEY, CONFIG_PATH) is True
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- see docstring
        try:
            _OWNER_FLAGS_FAILURE = type(exc).__name__
        except Exception:
            _OWNER_FLAGS_FAILURE = "unknown"
        return False


def _import_engramd_client():
    """Lazy import (flag OFF never imports it), resolved layout-agnostically via the shared resolver -- plugin root first,
    then a walk-parents search; never a fixed parents[N] (project CLAUDE.md, "Plugin build restructures code paths")."""
    runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engramd_client.py")
    sys.path[:] = [runtime_dir] + [p for p in sys.path if p != runtime_dir]
    import engramd_client
    return engramd_client


def _code(value, fallback: str = "unknown") -> str:
    """A reason code from the client/owner reduced to `[A-Za-z0-9_]{1,64}`; anything else is `fallback`. The log never
    carries free text from the owner."""
    text = value if isinstance(value, str) else ""
    return text if re.fullmatch(r"[A-Za-z0-9_]{1,64}", text) else fallback


OWNER_RESTAMP_ERROR_MAX = 400  # chars of an owner-supplied restamp_error kept in the log (real messages measured ~260, path included)


def _one_line(value, limit: int | None = None) -> str:
    """Owner-supplied text reduced to ONE bounded printable line: every run of whitespace or control characters becomes a
    single space, then the text is cut to `limit` (default OWNER_RESTAMP_ERROR_MAX) characters with a trailing '...'.
    The direct path's restamp_error comes from this process's own shared function and is logged as before."""
    limit = OWNER_RESTAMP_ERROR_MAX if limit is None else limit
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _owner_write_timeout(ec) -> float:
    return float(ec.OWNER_WRITE_CLIENT_TIMEOUT)


def _owner_use_reward(node_ids) -> int:
    """Send ONE `use_reward` (action "mention") for `node_ids` and return the count bumped. Raises _OwnerWriteLost with the
    log line for every outcome that is not a good ack. Params are EXACTLY {node_ids, action, request_id}: no alpha, no
    value, no config path (the owner derives alpha from USE_ALPHA and reads its own config). One request_id per fire,
    reused for the single retry of an INDETERMINATE first attempt (the owner's dedupe then answers duplicate=true, no
    second bump); determinate outcomes are never retried."""
    try:
        ec = _import_engramd_client()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- any import failure is an owner failure
        raise _OwnerWriteLost(
            f"bump_utility failed: owner client unavailable ({type(exc).__name__}); the store is not opened as a fallback"
        ) from exc
    rid = str(uuid.uuid4())
    params = {"node_ids": list(node_ids), "action": _ACTION, "request_id": rid}
    bound = _owner_write_timeout(ec)
    started = _monotonic()
    result = None
    after_indeterminate = False
    for attempt in (1, 2):
        remaining = OWNER_WRITE_BUDGET_S - (_monotonic() - started)
        if attempt == 2 and remaining < OWNER_RETRY_MIN_S:
            break
        result = ec.write("use_reward", params, request_id=rid, timeout=max(0.0, min(bound, remaining)))
        if isinstance(result, ec.Unavailable) and result.reason in ("timeout", "protocol"):
            after_indeterminate = True
            continue  # INDETERMINATE: the op may have committed; retry once with the SAME request_id
        break
    return _owner_outcome(ec, result, after_indeterminate)


def _indeterminate_after_retry(code: str) -> str:
    return f"bump_utility indeterminate: {code}; the first attempt may have been applied once"


def _owner_outcome(ec, result, after_indeterminate: bool = False) -> int:
    """Map the final result to the count or to _OwnerWriteLost. `after_indeterminate`: the FIRST attempt was indeterminate
    (it may be running or committed), so a non-Ok answer to the retry says nothing about whether the bump was applied:
    every such outcome except readback_mismatch (committed but unverified, wherever it occurs) is reported as
    INDETERMINATE, never with the 'not applied' / 'failed' words reserved for outcomes of a first attempt."""
    if isinstance(result, ec.Ok):
        body = result.result
        bumped = body.get("bumped") if isinstance(body, dict) else None
        if not isinstance(bumped, int) or isinstance(bumped, bool):
            if after_indeterminate:
                raise _OwnerWriteLost(_indeterminate_after_retry("unusable_answer"))
            raise _OwnerWriteLost(
                f"bump_utility failed: owner returned an unusable acknowledgement ({type(body).__name__})")
        err = body.get("restamp_error")
        if err:
            _log(f"restamp failed: {_one_line(err)}")
        return bumped
    if isinstance(result, ec.Unavailable):
        if result.reason in ("timeout", "protocol"):
            raise _OwnerWriteLost(
                "bump_utility indeterminate: owner did not answer (may have been applied once)")
        if after_indeterminate:
            raise _OwnerWriteLost(_indeterminate_after_retry(f"unavailable_{_code(result.reason)}"))
        if result.reason == "internal":
            raise _OwnerWriteLost(
                "bump_utility indeterminate: owner client error (internal); the request may have been sent")
        raise _OwnerWriteLost(f"bump_utility failed: owner unavailable (reason={_code(result.reason)})")
    if isinstance(result, ec.Rejected):
        reason = _code(result.reason, "invalid_reason")
        if after_indeterminate and reason != "readback_mismatch":
            raise _OwnerWriteLost(_indeterminate_after_retry(reason))
        if reason == "busy":
            raise _OwnerWriteLost("bump_utility failed: owner busy (not applied)")
        if reason == "unknown_operation":
            raise _OwnerWriteLost(
                "bump_utility failed: owner rejected: unknown_operation: contract skew, this owner does not know "
                "use_reward; restart or upgrade the engramd owner to this plugin's version (the bump was not applied)")
        if reason == "readback_mismatch":
            raise _OwnerWriteLost(
                "bump_utility committed but unverified: owner readback_mismatch; the bump may have been applied once "
                "and must not be blindly repeated")
        raise _OwnerWriteLost(f"bump_utility failed: owner rejected: {reason}")
    if after_indeterminate:
        raise _OwnerWriteLost(_indeterminate_after_retry("unusable_answer"))
    raise _OwnerWriteLost(f"bump_utility failed: owner returned an unusable answer ({type(result).__name__})")


def bump_utility(
    node_ids,
    alpha: float | None = None,
    db_path: str = KNOWLEDGE_DB,
    *,
    restamp: bool = False,
    config_path: str = CONFIG_PATH,
) -> int:
    """Apply the shared Q-update (engram_restamp.bump_utility) to each node_id.

    `alpha=None` (the default) means the shared table's "mention" entry; an
    explicit alpha overrides it (kept for tests / callers that pin a value).

    Returns the count of nodes actually updated. IDs not present in
    `nodes` are silently skipped (the shape-regex can match non-node
    tokens like `in_NNNN` per engram_ids docstring).

    With `restamp=True` the whole "bump, then restamp" sequence is the shared
    `engram_restamp.use_reward` (action "mention"; `alpha` overrides the table).
    The restamp runs in its own SAVEPOINT there and a failure comes back as
    `restamp_error`; this hook commits (the bump always survives) and logs the
    error to LOG_PATH -- visible, never silent.

    #3822 Stage B: with `engramd.utility_credit_writes_via_owner` ON the write goes to the owner's `use_reward` op
    (`_owner_use_reward`) and this function never opens `db_path`, never reaches the WAL pragma and does not need
    `engram_restamp`; `db_path` and `config_path` are ignored (the owner owns its store and reads its own config). An
    explicit `alpha`, or `restamp=False`, is refused with ValueError: the owner op has exactly one shape (action
    "mention", alpha from the shared table, bump then restamp), so honouring either would silently write something else.
    """
    if not node_ids:
        return 0
    if _owner_writes_enabled():
        if alpha is not None:
            raise ValueError("an explicit alpha is not supported with owner writes (the owner derives alpha from USE_ALPHA)")
        if not restamp:
            raise ValueError("restamp=False is not supported with owner writes (the owner op always restamps)")
        return _owner_use_reward(node_ids)
    if _OWNER_FLAGS_FAILURE is not None:
        _log(f"bump_utility: owner flag helper failed ({_code(_OWNER_FLAGS_FAILURE)}); using the direct path")
    if engram_restamp is None:
        raise RuntimeError(f"engram_restamp unavailable ({_RESTAMP_IMPORT_ERROR})")
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if restamp:
            result = engram_restamp.use_reward(
                conn, node_ids, _ACTION, config_path=config_path, alpha=alpha
            )
            updated = result.bumped
            conn.commit()
            if result.restamp_error:
                _log(f"restamp failed: {result.restamp_error}")
        else:
            if alpha is None:
                alpha = _mention_alpha()
            updated = len(engram_restamp.bump_utility(conn, node_ids, alpha))
            conn.commit()
        return updated
    finally:
        conn.close()


def _log(line: str, log_path: str = LOG_PATH) -> None:
    try:
        with open_regular_text_append(log_path) as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {line}\n")
    except Exception:
        pass


def _is_real_user_message(content) -> bool:
    """Return True if `content` represents a genuine user message (not tool_result).

    Claude Code's JSONL uses user-typed entries for both real user input AND
    for tool_result payloads (the harness's response to a tool_use block).
    Tool_result entries must NOT act as stop-markers when walking backwards
    through a turn's assistant prose.

    Real user message conditions:
      - content is a non-empty bare string (always real user text), OR
      - content is a non-empty list of blocks where NO block has type == "tool_result"

    Empty content (empty string or empty list) is treated as NOT a real user
    message — Claude Code does not emit empty-content user entries in
    practice, and treating an empty list as a turn boundary would cut off
    the backwards walk prematurely.
    """
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        if not content:
            return False
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def collect_turn_text(transcript_path: str, last_assistant_message: str) -> str:
    """Collect ALL assistant text blocks from the current turn in the JSONL.

    Walks the JSONL at `transcript_path` backwards, collecting text blocks
    from `assistant`-type entries and stopping when a real user message is
    encountered (a user entry whose content is not a tool_result payload).

    Concatenates the JSONL-collected text with `last_assistant_message`
    (the just-emitted block from stdin that hasn't flushed to the JSONL yet).

    Returns the combined string. Falls back to `last_assistant_message` alone
    on any read/parse error.
    """
    collected: list[str] = []
    if transcript_path and os.path.exists(transcript_path):
        try:
            with open_regular_text(transcript_path) as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                entry_type = obj.get("type")
                if entry_type == "assistant":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if content is None:
                        continue
                    if isinstance(content, str):
                        if content:
                            collected.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text") or ""
                                if t:
                                    collected.append(t)
                elif entry_type == "user":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if _is_real_user_message(content):
                        break  # reached the boundary of this turn
                    # tool_result user entry — part of the same turn, keep walking
        except OSError:
            pass

    # Reverse to restore chronological order, then append stdin's last message.
    parts = list(reversed(collected))
    if last_assistant_message:
        parts.append(last_assistant_message)
    return "\n".join(parts)


def _read_prompt_count(path: str = PROMPT_COUNTER_PATH) -> int:
    """Read the CURRENT prompts_since_compaction counter (#1698 slice 3 §2).

    Reads $ENGRAM_HOME/prompt-counter.json — the same file
    engram-surface-hook.py's read_prompt_counter()/write_prompt_counter()
    already maintain across every UserPromptSubmit fire. This Stop hook runs
    as a separate process on a separate hook event and has no in-memory
    access to the surface hook's counter; reusing its persisted file (read-
    only) is the resolved answer to the spec's open question — never invent
    a second counter that could drift from the surface hook's.

    Returns 0 on any missing/malformed file (fail-open — worst case an
    enactment is judged eligible/ineligible against a stale-zero counter,
    never a crash).
    """
    try:
        with open_regular_text(path) as f:
            data = json.load(f)
        return int(data.get("prompts_since_compaction", 0))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def _write_state_atomic(path: str, state: dict) -> None:
    """Delegates to _hooklib.atomic_write_json() (tmp + os.replace) so a
    concurrent reader never sees a partial write. Raises on failure —
    callers (here, `_check_enactments`'s own caller in `credit_mentions`)
    wrap this in a best-effort try/except, per this file's house rule that
    behavioral hooks must never block the session.
    """
    atomic_write_json(path, state)


def _with_state_lock(lock_path, fn):
    """Run fn() (a read-modify-write closure) while holding an exclusive,
    BLOCKING flock on lock_path. Unlike #1709's non-blocking pattern, this
    one blocks -- the critical section is a fast local file read+write (no
    daemon round-trip), so a brief wait is cheap and correctness (no lost
    update) matters more than never-block here. Degrades to running fn()
    unlocked if fcntl, the lockfile open, or the flock call itself fails
    (non-POSIX, permissions, a file type or filesystem without flock
    support) -- same
    never-crash contract as #1709's degrade path, just without the
    non-blocking contention branch (there's nothing to suppress; fn() still
    runs, just unprotected).

    #1720: this exact ~15-line helper is duplicated rather than shared, in
    THREE files (#3658): engram-surface-hook.py, engram_core.py (the MCP server
    process), and this one (engram-utility-credit-mention-stop.py, the Stop hook). Hook scripts
    are separate subprocesses invoked by Claude Code, hooks do not import each
    other, and engram_core.py may not be imported by a hook, so there is no
    shared import surface today, and a new shared module for ~15 lines isn't
    worth the engineering (see spec docs/internal/specs/1720-principle-state-write-race.md).
    If any copy changes, change the other two; tests/test_utility_state_lock_3658.py
    pins all three AST-identical (docstring dropped).
    """
    fd = None
    try:
        import fcntl
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking -- no LOCK_NB
    except Exception as exc:
        # #3658 review: BOTH setup steps (the open and the flock) degrade here;
        # flock can raise on a file the open accepted (EOPNOTSUPP on a FIFO on
        # macOS). The lock was never taken, so it is never unlocked: just close
        # the fd and run fn() unlocked.
        # #1743: this degrade was silent -- nothing distinguished "locked and
        # protected" from "running unlocked," the same diagnosis-costing gap
        # #1737/#1742 fixed for the non-blocking cooldown lock. Emit one
        # never-block/never-raise stderr tell (hook/server stderr goes to logs,
        # not the model, so it is safe on every call) so a fallback-to-unlocked
        # -- which leaves fn()'s read-modify-write unprotected against a lost
        # update -- leaves a trace instead of being invisible. (#1742's
        # state-file-flag half does not map onto this generic helper, which has
        # no access to fn()'s state dict; the stderr line is the proportionate
        # tell here.) Kept byte-identical with the sibling copy (surface-hook /
        # engram_core) per the #1720 sync note above -- if one changes, change
        # the other.
        try:
            import sys
            print(
                f"[engram _with_state_lock] flock degraded to UNLOCKED "
                f"({lock_path}): {type(exc).__name__}: {exc} -- read-modify-write "
                f"is unprotected; a concurrent write may be lost",
                file=sys.stderr,
            )
        except Exception:
            pass
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        return fn()  # degrade: run unlocked rather than crash or skip
    try:
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


def _emit_enactment(
    principle_id: str,
    prompt_count: int,
    *,
    session_id: str = "unknown",
    transcript_path: str = "",
) -> None:
    """Emit engram.trigger.enactment telemetry (#1698 slice 3 §4) — same
    Emitter pattern/failure-mode contract as engram-surface-hook.py's
    engram.trigger.fire (§1.3): best-effort, drop silently on any failure.

    alpha #2574 WS3: construction (import + Emitter.init) is narrowed to its
    own try/except so an INIT_FAILED outcome is recordable and distinct
    from an ordinary DROPPED emit (docs/internal/2574-telemetry-design.md §1/§5) --
    this is the design doc's own decisive/measured site for the TIME axis
    (§1's NEGATIVE EVIDENCE discussion): this function's entire body is the
    emit, so `reached` cannot be counted here at all -- it belongs to the
    TRIGGER ENTRY frame (`main`), not this per-site wrapper.
    """
    try:
        from engram_log_emitter import Emitter
        emitter = Emitter.init(session_id=session_id, transcript_path=transcript_path)
    except Exception:
        write_telemetry_record(
            manifest_id=_ENACTMENT_SITE,
            event_type="engram.trigger.enactment",
            fields={"outcome": "init_failed"},
        )
        return

    _uid = emitter.emit(
        event_type="engram.trigger.enactment",
        level=1,
        data={"principle_id": principle_id, "prompt_seq": prompt_count},
        site="utility-credit-mention-stop.enactment",
    )
    write_telemetry_record(
        manifest_id=_ENACTMENT_SITE,
        event_type="engram.trigger.enactment",
        fields={"outcome": "delivered" if _uid is not None else "dropped"},
    )


def _check_enactments(
    mentioned_ids,
    state_path: str = PRINCIPLE_TRIGGER_STATE_PATH,
    prompt_count_path: str = PROMPT_COUNTER_PATH,
    *,
    session_id: str = "unknown",
    transcript_path: str = "",
) -> None:
    """Mention-proxy enactment detection (design doc §4, v1-implementable
    proxy). A mentioned node ID that IS a principle_id in the registry, with
    no trigger fire for that principle in the trailing window, counts as an
    unprompted enactment: the practice happened without the nudge firing.
    """
    try:
        with open_regular_text(PRINCIPLE_TRIGGERS_PATH) as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    except OSError:
        # Only a non-regular target (FIFO / directory: the helper refuses it) is
        # treated as absent; any other OSError on a regular file propagates, as
        # it did before the non-blocking opens (#3628).
        if os.path.exists(PRINCIPLE_TRIGGERS_PATH) and not os.path.isfile(PRINCIPLE_TRIGGERS_PATH):
            return
        raise
    # #1731: registry values are LISTS of entries (a dual-role trigger node
    # now gets one entry per principle it triggers). Tolerate an old-shape
    # single dict too (lazy-read shim, same as the surface hook's consumer).
    principle_ids = set()
    for _raw in registry.values():
        for _e in (_raw if isinstance(_raw, list) else [_raw]):
            _pid = _e.get("principle_id")
            if _pid:
                principle_ids.add(_pid)
    hits = principle_ids & set(mentioned_ids)
    if not hits:
        return

    prompt_count = _read_prompt_count(prompt_count_path)
    # #3658: the read-modify-write below runs under the SAME blocking flock the
    # surface hook's write-back takes (`<state>.lock`, `_with_state_lock`), with
    # the READ inside the lock (#1720 rationale: a snapshot read before the lock
    # is stale by the time we write it, and would overwrite a concurrent
    # surface update). The lock has no timeout, same as the surface hook; if one
    # is ever added it must be added to every caller in one PR. Lock setup
    # failure degrades to running unlocked with the helper's own stderr line.
    # Telemetry is emitted after the lock is released: it does I/O.
    emitted = []

    def _rmw() -> None:
        # #3521 row 40: one read rule shared with the other two readers of this
        # file (_hooklib.read_state_json_classified). ABSENT -> {}, a JSON object
        # -> as-is, anything else (bad JSON, a non-dict, an unreadable file, a
        # non-regular target, a symlink) is UNKNOWN: skip the write so the file
        # keeps its bytes (never moved here: only the per-prompt surface hook does
        # that). The one stderr line is printed HERE, before credit_mentions'
        # `except Exception` could swallow anything; nothing is raised.
        state, state_reason, _movable = read_state_json_classified(state_path)
        if state_reason is not None:
            try:
                msg = re.sub(r"[\x00-\x1f\x7f]", " ", state_reason)[:200]
                print(
                    f"[engram principle-trigger-state] unusable state file "
                    f"({msg}) -- enactment count skipped; file left untouched",
                    file=sys.stderr,
                )
            except Exception:
                pass
            return

        changed = False
        for pid in hits:
            entry = state.get(pid)
            if isinstance(entry, int):
                entry = {"last_fired_prompt": entry, "strength": 1.0, "enactments": 0, "fires": 0}
            if not isinstance(entry, dict):
                entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0}
            last_fired = entry.get("last_fired_prompt", 0)
            if (prompt_count - last_fired) >= _ENACTMENT_WINDOW_PROMPTS:
                entry["enactments"] = entry.get("enactments", 0) + 1
                state[pid] = entry
                changed = True
                emitted.append(pid)
        if changed:
            _write_state_atomic(state_path, state)

    _with_state_lock(str(state_path) + ".lock", _rmw)
    for pid in emitted:
        _emit_enactment(pid, prompt_count, session_id=session_id, transcript_path=transcript_path)


def credit_mentions(
    last_message: str,
    *,
    db_path: str = KNOWLEDGE_DB,
    alpha: float | None = None,
    session_id: str = "unknown",
    transcript_path: str = "",
    config_path: str = CONFIG_PATH,
) -> dict:
    """Run the credit-assignment pipeline against `last_message`.

    Returns a stats dict with mentioned_count / updated_count. Bumps
    utility for every ENGRAM node ID typed in the agent's prose for
    this turn. The prior recall_window intersection was removed in
    PR #214: post-cutover node IDs always originate from substrate
    context, so the intersection was a tautology.

    Pure function (no I/O beyond the configured paths) — testable
    independently of stdin parsing.

    session_id/transcript_path (#1698 slice 3): threaded through purely for
    `engram.trigger.enactment` telemetry attribution (_check_enactments →
    _emit_enactment). Optional/defaulted so every pre-existing caller in
    this repo (which omits them) is unaffected.
    """
    stats = {
        "mentioned_count": 0,
        "updated_count": 0,
    }
    if not last_message:
        return stats

    try:
        from engram_ids import find_node_ids
    except Exception:
        return stats

    mentioned = find_node_ids(last_message)
    stats["mentioned_count"] = len(mentioned)
    if not mentioned:
        return stats

    # #1698 slice 3 §2 — enactment detection piggybacked on this existing
    # mention-parsing pass. Independent of the utility-bump path below (a
    # bump_utility failure must not skip enactment detection, and vice
    # versa) — its own try/except, matching this file's per-concern
    # failure-isolation style.
    try:
        _check_enactments(mentioned, session_id=session_id, transcript_path=transcript_path)
    except Exception as e:
        _log(f"enactment check failed: {type(e).__name__}: {e}")

    try:
        # `mentioned` is already deduplicated in first-occurrence order by
        # engram_ids.find_node_ids; pass directly without re-wrapping in set()
        # (which would discard the ordering without any correctness gain).
        stats["updated_count"] = bump_utility(
            mentioned, alpha, db_path, restamp=True, config_path=config_path
        )
    except _OwnerWriteLost as e:
        _log(str(e.args[0]))
    except Exception as e:
        _log(f"bump_utility failed: {type(e).__name__}: {e}")
    return stats


def main() -> None:
    # alpha #2574 WS3: `reached` -- unconditional, before any guard/early
    # return in this function.
    write_telemetry_record(manifest_id=_MAIN_MANIFEST_ID, event_type="engram.telemetry.reached", fields={})

    _t0 = time.perf_counter()

    last_message = ""
    session_id = "unknown"
    transcript_path = ""
    try:
        raw = sys.stdin.read()
        if raw:
            payload = json.loads(raw)
            # Stop hooks can re-fire; guard against double credit-assignment.
            if payload.get("stop_hook_active", False):
                return
            lam = payload.get("last_assistant_message")
            if isinstance(lam, str):
                last_message = lam
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                transcript_path = tp
    except Exception:
        return

    # Combine all assistant prose blocks from the current turn (JSONL walk)
    # with the just-emitted last_assistant_message from stdin.
    turn_text = collect_turn_text(transcript_path, last_message)

    stats = credit_mentions(turn_text, session_id=session_id, transcript_path=transcript_path)
    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    if stats["mentioned_count"] > 0:
        _log(
            f"session={session_id} "
            f"mentioned={stats['mentioned_count']} "
            f"updated={stats['updated_count']} "
            f"duration_ms={_duration_ms}"
        )

    # alpha #2574 WS3: construction (import + Emitter.init) is narrowed to
    # its own try/except so an INIT_FAILED outcome is recordable and
    # distinct from an ordinary DROPPED emit (docs/internal/2574-telemetry-design.md
    # §1/§5).
    emitter = None
    try:
        from engram_log_emitter import Emitter
        emitter = Emitter.init(
            session_id=session_id,
            transcript_path=transcript_path,
        )
    except Exception:
        write_telemetry_record(
            manifest_id=_HOOK_FIRE_SITE,
            event_type="engram.hook.fire",
            fields={"outcome": "init_failed"},
        )

    if emitter is not None:
        _uid = emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-utility-credit-mention-stop",
                "hook_type": "Stop",
                "duration_ms": _duration_ms,
                "exit_code": 0,
                "mentioned_count": stats["mentioned_count"],
                "updated_count": stats["updated_count"],
                "alpha_mention": _mention_alpha_or_none(),
            },
            site="utility-credit-mention-stop.hook_fire",
        )
        write_telemetry_record(
            manifest_id=_HOOK_FIRE_SITE,
            event_type="engram.hook.fire",
            fields={"outcome": "delivered" if _uid is not None else "dropped"},
        )


if __name__ == "__main__":
    main()
