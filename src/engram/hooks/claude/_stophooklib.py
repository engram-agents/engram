"""Shared prologue helpers for the ENGRAM Stop hooks.

SSoT (spec: 2026-07-31-loop-arm-nudge-SPEC.md, PR-A) for the transcript-delta
idiom duplicated -- and already drifted -- across the Stop hooks:

  - hooks/claude/engram-stop-hook.py (the write-nudge; byte-offset delta scan
    over the transcript, JSONL per-line parse, message.content block walk)
  - hooks/claude/engram-deference-detector-stop.py (the deference-phrase
    scanner; its own docstring already calls out the drift: "content
    concatenated; no structural boundary marking tool_use vs text blocks" --
    a limitation engram-stop-hook.py's block walk had already solved)

`_prompthooklib.py` is the established precedent for this kind of extraction
on the UserPromptSubmit side (gh#1680 slice 1); this module is its Stop-side
counterpart. Same scope discipline: SAFE, behavior-preserving extraction of
the concerns that were genuinely duplicated (or, in the case of the
transcript-path session-marker fallback below, genuinely singly-owned by one
hook and worth centralizing before a second consumer needs it) -- it does
NOT merge the two hooks' distinct gates, state files, or business predicates.
Each hook keeps its own main()/gate logic, its own per-hook state-file path
(passed in by the caller -- never owned by this module), and runs as its own
independent process, registered independently in hooks.json.

Extracted here:

  - resolve_transcript_path(): candidate-transcript-path-else-session-marker
    resolution. This is engram-deference-detector-stop.py's EXISTING
    fallback logic (its `_last_assistant_structure`'s path resolution),
    lifted out verbatim so it is shared infrastructure rather than a single
    hook's private helper. engram-stop-hook.py does NOT call this function in
    this extraction -- it has never had a session-marker fallback (it reads
    `payload["transcript_path"]` directly, with no existence check), and
    routing it through this function would silently upgrade its behavior in
    the edge case where the payload path is temporarily missing/nonexistent
    but a session marker resolves a real one. That would violate this hook's
    explicit byte-identical constraint (it is live and load-bearing), so its
    transcript-path extraction is left untouched. See the PR body for this
    judgment call.

  - read_offset_state() / write_offset_state(): the byte-offset state
    file's read/write mechanics (`{transcript_path, byte_offset}`), matching
    engram-stop-hook.py's `_should_suppress`/`_update_state` byte-for-byte.
    The state-file PATH itself stays per-hook -- callers pass their own path
    in; this module never hardcodes or owns a state-file location.

  - read_delta(): read exactly `current_size - stored_offset` bytes starting
    at `stored_offset`. Deliberately takes `current_size` as an explicit
    argument (rather than re-stat'ing internally) so the caller's own
    getsize() call remains the single source of truth for the size used in
    both the rotation-guard comparison and the read length -- avoids a
    second, potentially-different stat() introducing a race the original
    code didn't have.

  - parse_jsonl_records(): decode a transcript delta as UTF-8 (errors=
    "replace"), split into lines, `json.loads` each non-blank line, skipping
    unparseable lines silently. Returns `(records, any_record_parsed)` --
    the `any_record_parsed` sentinel distinguishes "delta had no lines" from
    "delta had lines but every one failed to parse" (load-bearing per
    engram-stop-hook.py's all-garbage-sentinel path; preserved here as a
    first-class return value rather than a side-channel constant).

  - walk_content_blocks(): normalize a `message.content` (or a tool_result
    block's own `content` field -- same shape) payload that may be a bare
    string OR a list of block dicts, yielding each block dict in turn. A
    non-empty string is normalized into a single synthetic
    `{"type": "text", "text": <string>}` block; a list yields its dict items
    (non-dict items skipped); anything else yields nothing. This is the
    "bare-string-vs-list" normalization engram-deference-detector-stop.py's
    `_last_assistant_structure` already performs for its LIST branch (its
    STRING branch is untouched by this extraction -- see that hook's inline
    comment for why: its string branch preserves an empty-string edge case
    this generic walker does not reproduce identically).

  - emit_context(): print the `hookSpecificOutput.additionalContext`
    envelope to stdout, with `hookEventName: "Stop"`. NEW infrastructure --
    neither existing Stop hook currently emits additionalContext (both use
    other channels: engram-stop-hook.py uses `{"decision": "block", ...}`;
    engram-deference-detector-stop.py writes a marker file consumed by its
    UserPromptSubmit companion). Added here so PR-B's loop-arm hook has it
    ready-made; NOT wired into either existing hook's main() in this PR --
    doing so would change their observable output shape, which is exactly
    the kind of behavior change PR-A's "pure refactor" scope forbids.

Like `_hooklib.py` / `_prompthooklib.py`, this module lives alongside the
hooks it serves in BOTH build topologies (source: `hooks/claude/`; deployed:
`hooks/` -- the plugin build flattens `hooks/claude/* -> hooks/*`). Each hook
must add its own directory to sys.path before importing this module, exactly
as the UserPromptSubmit hooks already do for `_prompthooklib` -- this module
cannot do that for itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def resolve_transcript_path(
    transcript_path: Optional[str],
    session_id: Optional[str] = None,
    sessions_dir: Optional[str] = None,
) -> Optional[str]:
    """Resolve the transcript path to read.

    Preference order (matches engram-deference-detector-stop.py's existing,
    pre-extraction `_last_assistant_structure` path resolution exactly):

      1. `transcript_path` itself, if truthy AND it exists on disk.
      2. Else, if `session_id` is given: read
         `<sessions_dir>/<session_id>.json`'s `"transcript_path"` field, IF
         that resolved path ALSO exists on disk.
      3. Else `None`.

    `sessions_dir` defaults to `$ENGRAM_HOME/sessions` (`$ENGRAM_HOME` or
    `~/.engram`) -- the same formula every consumer of this module already
    computes for its own module-level `SESSIONS_DIR` constant. Callers that
    already have their own `SESSIONS_DIR` constant should pass it explicitly
    rather than rely on this default, to avoid any risk of the two diverging.

    Returns `None` on any failure: no `transcript_path` and no `session_id`,
    a missing/corrupt marker file, or a resolved candidate that doesn't
    exist on disk. The caller decides what "unresolvable" means for its own
    control flow.
    """
    if transcript_path and os.path.exists(transcript_path):
        return transcript_path
    if session_id:
        if sessions_dir is None:
            engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
            sessions_dir = os.path.join(engram_home, "sessions")
        marker_path = os.path.join(sessions_dir, f"{session_id}.json")
        try:
            with open(marker_path, encoding="utf-8") as f:
                marker = json.load(f)
            candidate = marker.get("transcript_path")
            if candidate and os.path.exists(candidate):
                return candidate
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    return None


def read_offset_state(state_file_path: str) -> tuple[Optional[str], int]:
    """Read a byte-offset state file (`{"transcript_path": ..., "byte_offset": ...}`).

    Raises on any read/parse failure (missing file, corrupt JSON, wrong
    types) -- mirrors engram-stop-hook.py's own pre-extraction
    try/except-then-fail-open control flow. The caller wraps this call in
    its own try/except and applies its own fail-open policy; this function
    does not swallow errors itself so that policy decision stays with the
    caller (different callers may want different fail directions).
    """
    with open(state_file_path, encoding="utf-8") as f:
        state = json.load(f)
    return state.get("transcript_path"), int(state.get("byte_offset", 0))


def write_offset_state(state_file_path: str, transcript_path: str) -> None:
    """Write `{"transcript_path": transcript_path, "byte_offset": <EOF size>}`.

    Best-effort: any failure (can't stat the transcript, can't write the
    state file) is silenced, matching engram-stop-hook.py's pre-extraction
    `_update_state` exactly -- state-file bookkeeping must never crash the
    hook. Uses tmp-file + os.replace so a concurrent reader never observes a
    partial write.
    """
    try:
        offset = 0
        if transcript_path:
            try:
                offset = os.path.getsize(transcript_path)
            except OSError:
                pass
        state = {"transcript_path": transcript_path, "byte_offset": offset}
        tmp = state_file_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_file_path)
    except Exception:  # noqa: BLE001
        pass


def read_delta(transcript_path: str, stored_offset: int, current_size: int) -> bytes:
    """Read exactly `current_size - stored_offset` bytes starting at `stored_offset`.

    Raises OSError on any failure (matches engram-stop-hook.py's
    pre-extraction inline read, which the caller wrapped in its own
    try/except OSError -> fail-open). `current_size` is taken as an
    explicit argument rather than re-stat'd here, so the caller's own
    getsize() call is the single source of truth used both for any
    rotation-guard comparison and for the read length here -- avoids a
    second stat() call racing against the first.
    """
    with open(transcript_path, "rb") as f:
        f.seek(stored_offset)
        return f.read(current_size - stored_offset)


def parse_jsonl_records(delta_bytes: bytes) -> tuple[list[dict], bool]:
    """Decode + split a transcript delta into parsed JSON records.

    Decodes as UTF-8 with errors="replace" (never raises on bad bytes).
    Splits on newlines, strips each line, skips blank lines. `json.loads`
    each remaining line; unparseable lines are skipped silently (fail-open
    at the line level -- one bad line must not lose the rest of the delta).

    Returns `(records, any_record_parsed)`:
      - `records`: the successfully-parsed lines that decoded to a JSON
        object (`dict`). Lines that parsed to a non-dict JSON value (e.g. a
        bare list or number) are NOT included in `records`, but DO still
        count toward `any_record_parsed` -- matching engram-stop-hook.py's
        pre-extraction per-line loop, which set its `any_record_parsed`
        flag immediately after a successful `json.loads`, before checking
        `isinstance(rec, dict)`.
      - `any_record_parsed`: True iff at least one line survived
        `json.loads`, regardless of the parsed value's type. Load-bearing:
        this is what lets a caller distinguish "empty delta" / "delta with
        only blank lines" from "delta had content but every line was
        garbage" (format drift) -- the two cases the all-garbage-sentinel
        path in engram-stop-hook.py's `_parse_delta_prose_and_writes`
        depends on.

    This function itself never raises (bad bytes are replaced, bad JSON
    lines are skipped) -- but a caller that goes on to access fields of a
    returned record in a way that isn't defensive (e.g. `rec["message"]["content"]`
    when `rec["message"]` isn't a dict) can still raise from ITS OWN code;
    that is unchanged from the pre-extraction shape and is each caller's own
    concern, not this function's.
    """
    text = delta_bytes.decode("utf-8", errors="replace")
    records: list[dict] = []
    any_record_parsed = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        any_record_parsed = True
        if isinstance(rec, dict):
            records.append(rec)
    return records, any_record_parsed


def walk_content_blocks(content) -> list[dict]:
    """Normalize a `message.content`-shaped payload into a list of block dicts.

    Claude Code's JSONL content fields (both the top-level
    `message.content` on assistant/user records, and a `tool_result`
    block's own inner `content` field) appear in production as EITHER a
    bare string OR a list of typed block dicts. This normalizes both shapes
    into a uniform list of block dicts:

      - Non-empty string -> a single synthetic `{"type": "text", "text":
        <the string>}` block.
      - Empty string / falsy -> `[]`.
      - List -> each item that IS a dict, in order (non-dict items
        skipped); anything else in the list is dropped.
      - Anything else (None, int, ...) -> `[]`.

    This is the "bare-string-vs-list" normalization
    engram-deference-detector-stop.py's `_last_assistant_structure` already
    performs for its list branch (its top-level string-vs-list DISPATCH is
    intentionally left inline in that hook rather than routed through this
    function -- see that hook's comment for the empty-string edge case that
    would otherwise diverge).
    """
    if isinstance(content, str):
        if content:
            return [{"type": "text", "text": content}]
        return []
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def emit_context(text: str) -> None:
    """Emit an additionalContext block on stdout (the Stop-hook channel).

    Mirrors `_prompthooklib.emit_context` exactly, except `hookEventName` is
    `"Stop"` (that module's is `"UserPromptSubmit"` -- the two hookEventName
    values are NOT interchangeable; Claude Code routes the envelope by this
    field). Not called by either PR-A-migrated hook (see module docstring);
    added for PR-B's loop-arm hook to use.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": text,
        }
    }))
