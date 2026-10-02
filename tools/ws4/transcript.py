"""Claude Code transcript parsing, heeding-window computation, and span
rendering for the WS4 adjudication kit.

Transcript layout is exactly as measured in ws4-adjudication-kit.md's
"Claude Code transcript layout" section (one JSON object per line):

  - Assistant tool call:
      {"type": "assistant", "message": {"role": "assistant",
       "content": [{"type": "tool_use", "id": "toolu_...",
                     "name": ..., "input": {...}}]}, "timestamp": ...}
  - Hook output (attachment):
      {"type": "attachment", "attachment": {"type": "hook_success",
       "hookName": "PreToolUse:Bash", "hookEvent": "PreToolUse",
       "toolUseID": "toolu_...", "stdout": "<hook JSON stdout, verbatim>",
       "command": "...engram-lesson-tripwire-hook.py..."}, "timestamp": ...}
  - Tool result: {"type": "user", "message": {"content":
      [{"type": "tool_result", "tool_use_id": "toolu_...", ...}]}}
  - Assistant text block: {"type": "text", "text": ...} inside an
    assistant message's content list.
  - Compaction boundary: {"type": "system", "subtype": "compact_boundary"},
    followed by {"type": "user", "isCompactSummary": true, ...}.
  - Human-typed / loop-wake prompt: a {"type": "user"} record whose content
    is text, not a tool_result, and that is not isCompactSummary.
  - Records with "isSidechain": true belong to sub-agents -- never the
    agent's own acts. Excluded from windows, outbound-act detection, and O2
    counts throughout this module.

Everything else (system subtypes stop_hook_summary / turn_duration /
scheduled_task_fire, pr-link, mode, ...) is ignored, per spec.
"""
from __future__ import annotations

import datetime as _dt
import json
from collections import namedtuple

from .constants import CHAIN_BLOCK_HEADER, tool_use_is_outbound_act

TRIPWIRE_COMMAND_MARKER = "engram-lesson-tripwire-hook"

FiredCall = namedtuple(
    "FiredCall", ["record_idx", "content_idx", "tool_use_id", "tool_name", "tool_input", "locate_basis"]
)
WindowResult = namedtuple("WindowResult", ["end_record_idx", "close_reason", "calls_counted", "k"])


class TranscriptReadError(Exception):
    """Raised when a transcript can't be read at all -- extract_spans.py
    catches this and routes the row to unlocatable.jsonl."""


def load_transcript(path) -> list:
    """Read a JSONL transcript into a list of record dicts, in file order.

    A record that isn't valid JSON is skipped (individual-line corruption
    is treated as tolerable -- the surrounding lines still carry a usable
    window); a transcript that can't be opened at all raises
    TranscriptReadError, which is the "transcript can't be read" branch of
    Piece 1's unlocatable accounting."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        raise TranscriptReadError(str(exc)) from exc
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def get_message_content(record: dict) -> list:
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return content
    return []


def is_sidechain(record: dict) -> bool:
    return bool(record.get("isSidechain"))


def is_human_prompt(record: dict) -> bool:
    """A {"type": "user"} record whose content is text, not a tool_result,
    and that is not isCompactSummary (spec, transcript-layout section)."""
    if is_sidechain(record):
        return False
    if record.get("type") != "user":
        return False
    if record.get("isCompactSummary"):
        return False
    content = get_message_content(record)
    if not content:
        raw_content = (record.get("message") or {}).get("content")
        return isinstance(raw_content, str) and bool(raw_content.strip())
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result":
            return False
    return True


def is_compaction_boundary(record: dict) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


def _next_non_sidechain(records: list, start_idx: int):
    for j in range(start_idx + 1, len(records)):
        if not is_sidechain(records[j]):
            return j
    return None


def _parse_ts(value) -> "float | None":
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def find_fired_tool_use(records: list, tool_use_id, tool_name, ts) -> "FiredCall | None":
    """Locate the fired tool call: by tool_use_id when present; otherwise
    by the nearest assistant tool_use to ts with matching tool_name (spec,
    Piece 1). locate_basis records which strategy found it: "tool_use_id"
    or "nearest_ts"."""
    if tool_use_id:
        for ridx, rec in enumerate(records):
            if is_sidechain(rec) or rec.get("type") != "assistant":
                continue
            for cidx, item in enumerate(get_message_content(rec)):
                if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("id") == tool_use_id:
                    return FiredCall(ridx, cidx, item.get("id"), item.get("name"), item.get("input") or {}, "tool_use_id")
        return None

    target_ts = _parse_ts(ts)
    best = None
    best_distance = None
    for ridx, rec in enumerate(records):
        if is_sidechain(rec) or rec.get("type") != "assistant":
            continue
        rec_ts = _parse_ts(rec.get("timestamp"))
        for cidx, item in enumerate(get_message_content(rec)):
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            if item.get("name") != tool_name:
                continue
            if target_ts is None or rec_ts is None:
                distance = 0.0  # no usable timestamps -- first match wins
            else:
                distance = abs(rec_ts - target_ts)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = FiredCall(ridx, cidx, item.get("id"), item.get("name"), item.get("input") or {}, "nearest_ts")
    return best


def compute_window(records: list, fired: FiredCall, k: int) -> WindowResult:
    """Heeding window: starts after the fired call; closes at the FIRST of
    (a) k further tool calls, or (b) the first OUTBOUND ACT. The fired call
    never closes its own window -- only content strictly after
    (fired.record_idx, fired.content_idx) is evaluated.

    "Further tool calls" are counted at the individual tool_use
    content-item granularity (a turn may contain several parallel
    tool_use items; each is one call) -- see handoff's ambiguous-decisions
    log for this granularity choice.

    If neither closing condition is ever met before the transcript ends,
    the window is reported closed at "end_of_transcript" (not specified by
    the spec; a defensive fallback so callers never index past the
    transcript -- see handoff).
    """
    n = len(records)
    calls_counted = 0
    for ridx in range(fired.record_idx, n):
        rec = records[ridx]
        if is_sidechain(rec):
            continue
        if rec.get("type") == "assistant":
            content = get_message_content(rec)
            start_ci = fired.content_idx + 1 if ridx == fired.record_idx else 0
            for cidx in range(start_ci, len(content)):
                item = content[cidx]
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    calls_counted += 1
                    if tool_use_is_outbound_act(item.get("name", ""), item.get("input") or {}):
                        return WindowResult(ridx, "outbound_act", calls_counted, k)
                    if calls_counted >= k:
                        return WindowResult(ridx, "k_calls", calls_counted, k)
            # Category 3 outbound act: an assistant text block that is the
            # LAST content item of its turn, with the next non-sidechain
            # record being a human-typed prompt (not a tool_result).
            if content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
                nxt_idx = _next_non_sidechain(records, ridx)
                if nxt_idx is not None and is_human_prompt(records[nxt_idx]):
                    return WindowResult(ridx, "outbound_act", calls_counted, k)
    return WindowResult(n - 1, "end_of_transcript", calls_counted, k)


def find_tripwire_attachment(records: list, fired: FiredCall):
    """The attachment record carrying THIS fire's own tripwire stdout:
    toolUseID == the located call's own transcript id (never the ledger
    row's tool_use_id field directly -- when that was null, the id we
    matched by nearest_ts is what the attachment will actually carry), and
    command containing "engram-lesson-tripwire-hook" (spec: "identify the
    tripwire one by command containing engram-lesson-tripwire-hook").

    Beware self-reference (spec): an assistant tool_use whose INPUT quotes
    "[triggered principles]" is not a hook record -- this function only
    ever looks at {"type": "attachment"} records, so that confusion cannot
    arise here.
    """
    call_id = fired.tool_use_id
    if not call_id:
        return None
    for ridx in range(fired.record_idx, len(records)):
        rec = records[ridx]
        if is_sidechain(rec) or rec.get("type") != "attachment":
            continue
        att = rec.get("attachment") or {}
        if att.get("toolUseID") != call_id:
            continue
        if TRIPWIRE_COMMAND_MARKER not in (att.get("command") or ""):
            continue
        return rec
    return None


def extract_nudge_text(attachment_record: dict) -> "str | None":
    """hookSpecificOutput.additionalContext, inside the attachment's
    stdout (JSON, verbatim per spec). Returns None on any parse problem."""
    att = attachment_record.get("attachment") or {}
    stdout = att.get("stdout")
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return (payload.get("hookSpecificOutput") or {}).get("additionalContext")


def redact_chain_block(text: str) -> "tuple[str, bool]":
    """Remove the "[triggered principles]" header line through the end of
    its indented lines (spec: "that header line through the end of its
    indented ↑ lines"). render_triggered_principles() (the hook) renders
    the header with NO leading whitespace and every body line (rung/head/
    cycle-detected/truncated notices, and the cross-hit convergence line)
    with exactly a two-space indent -- so "the end of its indented lines"
    is the first following line that does NOT start with two spaces, or
    end of string.

    A no-op (removed=False) when the header isn't present at all -- e.g.
    control-arm or non-participating fires, where there was never a chain
    block to redact.
    """
    if CHAIN_BLOCK_HEADER not in text:
        return text, False
    lines = text.split("\n")
    out = []
    i = 0
    removed = False
    while i < len(lines):
        if lines[i] == CHAIN_BLOCK_HEADER:
            removed = True
            i += 1
            while i < len(lines) and lines[i].startswith("  "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), removed


def head_named_in_span(span_text: str, head_ids) -> bool:
    """Leak audit: True if any head id still appears anywhere in the
    (already redacted) span text -- e.g. the agent quoted it."""
    return any(hid and hid in span_text for hid in head_ids)


def _render_tool_use(item: dict) -> str:
    name = item.get("name", "?")
    try:
        input_str = json.dumps(item.get("input") or {}, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        input_str = str(item.get("input"))
    return f"**{name}**\n```json\n{input_str}\n```"


def _render_tool_result(item: dict) -> str:
    content = item.get("content", item)
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def build_span(records: list, fired: FiredCall, window: WindowResult, fire_id: str, k: int, redact_chain: bool = True) -> str:
    """Render one fire's markdown span: the fired call, the nudge text
    (verbatim, chain-redacted if redact_chain), then each subsequent
    call/result/text up to the window close. Header carries the fire id,
    never the arm (spec, Piece 1).

    Round 2, item 1: every tool_use content item strictly after the fired
    call is prefixed with a visible 1-based "[call N]" label, incremented
    at exactly the same points compute_window's own calls_counted
    increments (same traversal order, same fired-record/subsequent-record
    split, same sidechain exclusion, same "type == tool_use" filter) -- so
    a corrective_at index the adjudicator writes against "[call N]" always
    means the same call compute_window would count towards K. Text blocks
    and tool_results are never numbered -- only tool calls are "calls"."""
    lines = [f"# Fire {fire_id}", ""]

    lines.append("## Fired call")
    lines.append(_render_tool_use({"name": fired.tool_name, "input": fired.tool_input}))
    lines.append("")

    attachment = find_tripwire_attachment(records, fired)
    lines.append("## Nudge (verbatim)")
    if attachment is None:
        lines.append("_(tripwire attachment not found in transcript)_")
    else:
        nudge_text = extract_nudge_text(attachment) or ""
        if redact_chain:
            nudge_text, _ = redact_chain_block(nudge_text)
        lines.append(nudge_text)
    lines.append("")

    lines.append(f"## Subsequent activity (window closed: {window.close_reason}, k={k})")
    call_number = 0
    for ridx in range(fired.record_idx, window.end_record_idx + 1):
        rec = records[ridx]
        if is_sidechain(rec):
            continue
        if ridx == fired.record_idx:
            content = get_message_content(rec)
            for cidx in range(fired.content_idx + 1, len(content)):
                item = content[cidx]
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    call_number += 1
                    lines.append(f"[call {call_number}]")
                    lines.append(_render_tool_use(item))
                elif item.get("type") == "text":
                    lines.append(item.get("text", ""))
            continue
        rtype = rec.get("type")
        if rtype == "assistant":
            for item in get_message_content(rec):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    call_number += 1
                    lines.append(f"[call {call_number}]")
                    lines.append(_render_tool_use(item))
                elif item.get("type") == "text":
                    lines.append(item.get("text", ""))
        elif rtype == "user":
            if rec.get("isCompactSummary"):
                lines.append("_(compaction summary)_")
                continue
            content = get_message_content(rec)
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_result":
                    lines.append("_(tool_result)_\n```\n" + _render_tool_result(item) + "\n```")
                elif item.get("type") == "text":
                    lines.append(item.get("text", ""))
            if not content:
                raw_content = (rec.get("message") or {}).get("content")
                if isinstance(raw_content, str) and raw_content.strip():
                    lines.append(raw_content)
        elif is_compaction_boundary(rec):
            lines.append("_(compaction boundary)_")
        # Anything else (other system subtypes, pr-link, mode, ...) is
        # ignored per spec ("ignore anything not listed here").
    return "\n\n".join(lines)


def o2_scan_end(records: list, fired_record_idx: int) -> int:
    """O2's scan range: "from the fire to the end of that session or the
    next compaction boundary in the transcript." Returns the last record
    index (inclusive) to scan."""
    for ridx in range(fired_record_idx + 1, len(records)):
        if is_sidechain(records[ridx]):
            continue
        if is_compaction_boundary(records[ridx]):
            return ridx - 1
    return len(records) - 1
