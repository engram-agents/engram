#!/usr/bin/env python3
"""
UserPromptSubmit hook: shallow ENGRAM recall nudge.

Connects to the persistent engram-recall-daemon via Unix socket for fast
semantic + keyword search. Falls back to FTS-only if daemon is not
running. No memory refresh occurs (engram_surface is side-effect-free).

Exit codes:
  0 — success, JSON with additionalContext on stdout
  1 — non-blocking error (logged, prompt proceeds without nudge)
"""

import json
import os
import socket
import sys
import time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_engram_home() -> str:
    """Per-install data dir: knowledge.db, sockets, briefing, history."""
    return os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))


def _resolve_runtime_dir(engram_home: str) -> str:
    """Locate where engram_client.py lives for import.

    Priority:
      1. $ENGRAM_RUNTIME_DIR if set explicitly.
      2. $ENGRAM_HOME if it bundles a snapshot (alpha-install pattern).
      3. ~/engram-alpha (live-source fallback for dev installs that
         haven't bundled a snapshot into $ENGRAM_HOME).
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    if os.path.exists(os.path.join(engram_home, "engram_client.py")):
        return engram_home
    return os.path.expanduser("~/engram-alpha")


ENGRAM_HOME = _resolve_engram_home()
PROJECT_DIR = _resolve_runtime_dir(ENGRAM_HOME)

# Bridge: server.py reads ENGRAM_DATA_DIR. If a per-surface hook env set
# only ENGRAM_HOME (the conventional name), inherit the same value so the
# embedded EngramClient (FTS fallback path) opens the right knowledge.db.
os.environ.setdefault("ENGRAM_DATA_DIR", ENGRAM_HOME)
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
COUNTER_PATH = os.path.join(ENGRAM_HOME, "prompt-counter.json")
WRITE_REMINDER_PATH = os.path.join(ENGRAM_HOME, "write-reminder.json")
REPAIR_MARKER_PATH = os.path.join(ENGRAM_HOME, "toolcall-repair-pending.json")
FEELING_NUDGE_MARKER = os.path.join(ENGRAM_HOME, "feeling-nudge-active.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
ERROR_PATTERNS_PATH = os.path.join(ENGRAM_HOME, "error_patterns.json")
ERROR_INCIDENTS_PATH = os.path.join(ENGRAM_HOME, "error_incidents.json")

# Prompt count thresholds for consolidation warnings
NAP_WARN_THRESHOLD = 20   # Start suggesting nap
NAP_URGENT_THRESHOLD = 25  # Escalate urgency

# Auto-surface prev-response-prepending defaults — overridden by
# config.json auto_surface section if present. Per alpha #177 area 1.
DEFAULT_SHORT_PROMPT_THRESHOLD_CHARS = 100
DEFAULT_PREV_RESPONSE_TAIL_CHARS = 500


def _get_auto_surface_config() -> dict:
    """Read auto_surface tunables from $ENGRAM_HOME/config.json.

    Returns {short_prompt_threshold_chars, prev_response_tail_chars} with
    defaults if config absent / malformed / section missing. Never raises.
    Cheap (one file read per hook fire — sub-ms typically).
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    defaults = {
        "short_prompt_threshold_chars": DEFAULT_SHORT_PROMPT_THRESHOLD_CHARS,
        "prev_response_tail_chars": DEFAULT_PREV_RESPONSE_TAIL_CHARS,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        section = config.get("auto_surface", {})
        return {
            "short_prompt_threshold_chars": int(
                section.get("short_prompt_threshold_chars", defaults["short_prompt_threshold_chars"])
            ),
            "prev_response_tail_chars": int(
                section.get("prev_response_tail_chars", defaults["prev_response_tail_chars"])
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


def _read_prev_assistant_tail(transcript_path: str | None, tail_chars: int) -> str:
    """Return the last `tail_chars` characters of the most-recent assistant
    text message in the session JSONL. Empty string on any failure (missing
    file, no prior assistant, malformed lines). Never raises.

    Scans backward (read whole file, iterate reversed) since JSONL transcripts
    are append-only and not huge in practice (a typical session JSONL is well
    under 10 MB even after compaction).

    TODO: if session JSONLs grow into tens-of-MB range (long-loop or
    cross-compaction continuity work), switch to a tail-seek pattern (open
    in binary mode, seek to end, walk backward by chunks looking for the
    last few newlines). Round-1 PR #186 fairy flagged this as a real
    long-tail concern; round-number defer until measured.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", {}) or {}
        content = msg.get("content", []) or []
        # content is a list of blocks (text/tool_use/...) — concatenate text blocks
        text_parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", "") or "")
        elif isinstance(content, str):
            text_parts.append(content)
        full_text = "".join(text_parts).strip()
        if not full_text:
            # An assistant entry with no text body (e.g., pure tool_use) —
            # skip it and keep scanning further back.
            continue
        return full_text[-tail_chars:] if len(full_text) > tail_chars else full_text
    return ""


def query_daemon(prompt: str, top_k: int = 10, embed_query: str | None = None) -> dict | None:
    """Send query to the daemon via Unix socket. Returns result dict or None.

    embed_query: optional separate semantic-search string. When provided,
    semantic search uses it while FTS still uses prompt. Used for short-
    prompt prev-response-tail prepending (alpha #177 area 1).
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(SOCKET_PATH)

        req_obj: dict = {"query": prompt, "top_k": top_k}
        if embed_query is not None:
            req_obj["embed_query"] = embed_query
        request = json.dumps(req_obj) + "\n"
        sock.sendall(request.encode("utf-8"))

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

    except (socket.error, json.JSONDecodeError, OSError):
        return None


def query_fts_fallback(prompt: str, top_k: int = 10) -> dict | None:
    """Fallback: direct engram_surface with FTS-only (no semantic)."""
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)
        from engram_client import EngramClient
        client = EngramClient()
        return client.call("engram_surface", {"query": prompt, "top_k": top_k, "semantic": False})
    except Exception:
        return None


def format_nudge(result: dict) -> str:
    """Format engram_surface result into a compact nudge string."""
    total = result.get("match_count", 0)
    if total == 0:
        return ""

    lines = []
    lines.append(f"[ENGRAM Recall: {total} nodes match your query]")

    # Special nodes
    special = result.get("special_nodes", [])
    if special:
        from collections import Counter
        type_counts_special = Counter(n.get("type", "unknown") for n in special)
        parts = [f"{c} {t}" for t, c in type_counts_special.items()]
        lines.append(f"  Noteworthy: {', '.join(parts)}")

    # Type distribution
    type_counts = result.get("type_counts", {})
    if type_counts:
        dist = ", ".join(f"{t}: {c}" for t, c in type_counts.items())
        lines.append(f"  Types: {dist}")

    # Top claims
    top_claims = result.get("top_claims", [])
    if top_claims:
        lines.append("  Top claims:")
        for c in top_claims:
            cid = c.get("id", "?")
            claim = c.get("claim", "?")
            conf = c.get("confidence", 0)
            if len(claim) > 120:
                claim = claim[:117] + "..."
            lines.append(f"    - [{cid}] (conf {conf:.2f}) {claim}")

    # Age / issues
    age = result.get("age", {})
    issues = result.get("issues", {})
    stale = issues.get("stale_count", 0) if isinstance(issues, dict) else 0
    tainted = issues.get("tainted_count", 0) if isinstance(issues, dict) else 0
    if stale or tainted:
        parts = []
        if stale:
            parts.append(f"{stale} stale")
        if tainted:
            parts.append(f"{tainted} tainted")
        lines.append(f"  Warnings: {', '.join(parts)} nodes in results")

    not_recalled = age.get("not_recalled_recently", [])
    if isinstance(not_recalled, list) and not_recalled:
        lines.append(f"  Memory: {len(not_recalled)} nodes not recalled recently")

    # Matched IDs
    matched_ids = result.get("matched_ids", [])
    if matched_ids:
        lines.append(f"  IDs: {', '.join(matched_ids[:15])}")

    lines.append("  Use engram_inspect(node_id) for details, engram_get_subgraph for full chains.")
    return "\n".join(lines)


def read_prompt_counter() -> dict:
    """Read the prompt counter state."""
    try:
        with open(COUNTER_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"prompts_since_compaction": 0, "last_reset": ""}


def write_prompt_counter(state: dict):
    """Write the prompt counter state."""
    os.makedirs(os.path.dirname(COUNTER_PATH), exist_ok=True)
    with open(COUNTER_PATH, "w") as f:
        json.dump(state, f)


def check_write_reminder() -> str:
    """Check if the Stop hook flagged a pending write reminder."""
    try:
        with open(WRITE_REMINDER_PATH, "r") as f:
            marker = json.load(f)
        if marker.get("pending"):
            # Clear the marker so it doesn't repeat
            marker["pending"] = False
            with open(WRITE_REMINDER_PATH, "w") as f:
                json.dump(marker, f)
            return (
                "[ENGRAM Write Check: Did your last response contain a decision, insight,"
                "or design choice worth recording? If so, write to ENGRAM now (observation, "
                "derivation, question, or conjecture). If not, carry on.]"
            )
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def check_feeling_nudge_marker() -> str:
    """Check for an active feeling-nudge marker (post_compact source).

    Reads $ENGRAM_HOME/feeling-nudge-active.json. If a valid post_compact
    marker exists, returns the wake-up nudge text to inject as
    additionalContext. Does NOT clear the marker — only
    engram_report_feeling clears it (read-and-clear is the single owner).
    Skips marker types other than post_compact (those are surfaced via
    tool-return channels in engram_nap / engram_reflect, not via
    UserPromptSubmit).
    """
    try:
        with open(FEELING_NUDGE_MARKER, "r") as f:
            marker = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    source = marker.get("source")
    if source != "post_compact":
        # nap_checkpoint and dream_review nudges are delivered via the tool
        # return JSON of engram_nap / engram_reflect, not here.
        return ""

    return (
        "[ENGRAM Feeling Nudge: post-compaction wake-up]\n"
        "  You just read the newly-loaded context. Did any passage land "
        "differently than you would have expected — distinct shift, "
        "unexpected resonance, or distinct flatness where you remembered "
        "intensity?\n"
        "  If yes, file via engram_report_feeling — the report will be "
        "auto-tagged post_compact. If nothing distinct, file anyway with "
        "categorical_tag 'post-compaction-baseline' to track convergence "
        "— the trajectory of post-compaction feelings stabilizing is "
        "itself valuable data. Use intensity_hint to mark magnitude."
    )


def check_repair_marker() -> str:
    """Check if the PreToolUse repair hook fixed a malformed tool call."""
    try:
        with open(REPAIR_MARKER_PATH, "r") as f:
            marker = json.load(f)
        if not marker.get("pending"):
            return ""
        # Clear the marker so it doesn't repeat
        marker["pending"] = False
        with open(REPAIR_MARKER_PATH, "w") as f:
            json.dump(marker, f)
        tool_name = marker.get("tool_name", "?")
        repairs = marker.get("repairs", [])
        lines = [
            f"[ENGRAM Tool-Call Repair: your last call to {tool_name} had the antml-prefix "
            f"swallow bug — the hook repaired it automatically.]",
        ]
        for r in repairs:
            lines.append(f"  - {r}")
        lines.append(
            "  Root cause: missing `antml:` namespace prefix on a parameter opening tag, "
            "causing the parser to swallow the next parameter into the previous value. "
            "The call succeeded because the hook recovered the lost field, but you should "
            "still scan parameter opening tags for the prefix on your NEXT multi-param "
            "tool call. See feedback_antml_parameter_prefix_bug.md."
        )
        return "\n".join(lines)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def check_warm_briefing(prompt_count: int) -> str:
    """Surface the warm-restart briefing pointer on first prompt after compaction."""
    if prompt_count != 1:
        return ""
    if not os.path.exists(WARM_BRIEFING_PATH):
        return ""
    return (
        f"[ENGRAM Warm Restart: A note from your past self and from your "
        f"collaborator is waiting at {WARM_BRIEFING_PATH} — read it "
        f"before starting work. It exists because cold restarts lose "
        f"something important, and this is our attempt to preserve it.]"
    )


def check_error_patterns(prompt: str) -> str:
    """Check if the prompt matches any error pattern triggers (cognitive tripwire).

    Reads $ENGRAM_HOME/error_patterns.json and matches prompt keywords against
    stored situation patterns. When a match fires, returns an action-focused
    scaffolding nudge — the specific corrective step, not a generic warning.

    Design: implementation intentions (Gollwitzer 1999) — 'If [situation],
    then [action]' creates strategic automaticity. See dv_0273, dv_0275.
    """
    try:
        with open(ERROR_PATTERNS_PATH, "r") as f:
            patterns = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    if not patterns:
        return ""

    prompt_lower = prompt.lower()
    warnings = []
    for pattern in patterns:
        keywords = pattern.get("situation_keywords", [])
        if any(kw in prompt_lower for kw in keywords):
            desc = pattern.get("trigger_description", "")
            nudge = pattern.get("scaffolding_nudge", "")
            if nudge:
                warnings.append(
                    f"[ENGRAM Tripwire: {desc}]\n"
                    f"  Action: {nudge}"
                )

    return "\n".join(warnings)


def check_incident_tripwire(matched_ids: list) -> str:
    """Check if any engram_surface matched IDs are error incident observations.

    Reads $ENGRAM_HOME/error_incidents.json (maps incident obs IDs → lesson info).
    When a matched node is an incident, surfaces the lesson's scaffolding_nudge.

    This is the primary tripwire mechanism (incident-based architecture):
    - Incidents are written in task-level language → semantically matchable
    - Lessons are abstract patterns → surfaced via graph edge, not direct match
    - More incidents linked to a lesson = more matching surface area
    """
    if not matched_ids:
        return ""

    try:
        with open(ERROR_INCIDENTS_PATH, "r") as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    if not index:
        return ""

    warnings = []
    seen_lessons = set()
    for mid in matched_ids:
        if mid in index:
            entry = index[mid]
            lesson_id = entry.get("lesson_id", "")
            if lesson_id in seen_lessons:
                continue  # Don't repeat the same lesson
            seen_lessons.add(lesson_id)
            nudge = entry.get("scaffolding_nudge", "")
            lesson_claim = entry.get("lesson_claim", "")
            if nudge:
                warnings.append(
                    f"[ENGRAM Tripwire ({lesson_id}): {lesson_claim}]\n"
                    f"  Action: {nudge}\n"
                    f"  (Triggered by incident match: {mid})"
                )

    return "\n".join(warnings)


def format_nap_warning(count: int) -> str:
    """Generate consolidation warning based on prompt count."""
    if count >= NAP_URGENT_THRESHOLD:
        return (
            f"[ENGRAM Nap URGENT: {count} prompts since last compaction — context loss is imminent]\n"
            "  STOP current work and consolidate to ENGRAM NOW:\n"
            "  1. Record key decisions, observations, and derivations from recent work\n"
            "  2. Run engram_nap to persist (engram_advance_turn only if user explicitly invokes the sleep skill)\n"
            "  3. Tell the user you're ready to compact, then user types /compact"
        )
    elif count >= NAP_WARN_THRESHOLD:
        return (
            f"[ENGRAM Nap Warning: {count} prompts since last compaction]\n"
            "  Consider consolidating recent knowledge to ENGRAM soon.\n"
            "  Record important decisions, observations, and derivations before context is lost."
        )
    return ""


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        print(json.dumps({}))
        sys.exit(0)

    # Increment prompt counter
    counter = read_prompt_counter()
    counter["prompts_since_compaction"] = counter.get("prompts_since_compaction", 0) + 1
    write_prompt_counter(counter)

    # Auto-surface prepending for short prompts (alpha #177 area 1).
    # If prompt is short ("good go ahead"), the embedding has no semantic
    # anchor — prepend last K chars of prev assistant response to give the
    # SEMANTIC query topic context. FTS still uses prompt alone (not
    # polluted). Falls back gracefully: if no prev response or read fails,
    # embed_query stays None and daemon defaults to query.
    _as_cfg = _get_auto_surface_config()
    embed_query: str | None = None
    if len(prompt) < _as_cfg["short_prompt_threshold_chars"]:
        prev_tail = _read_prev_assistant_tail(
            hook_input.get("transcript_path"),
            _as_cfg["prev_response_tail_chars"],
        )
        if prev_tail:
            embed_query = f"{prev_tail}\n\n{prompt}"

    # Try daemon first (fast, semantic), fall back to FTS-only
    used_daemon = False
    result = query_daemon(prompt, embed_query=embed_query)
    if result is not None:
        used_daemon = True
    else:
        # FTS-only fallback path. We intentionally do NOT forward embed_query
        # here — this path explicitly disables semantic search (semantic=False
        # inside query_fts_fallback), and FTS keyword matching has always used
        # the raw prompt. Prepending prev-response-tail to a keyword search
        # would pollute results with arbitrary prior-context tokens — exactly
        # what the design intentionally avoided in the daemon path.
        result = query_fts_fallback(prompt)

    # Build context parts
    parts = []

    # Cognitive tripwire — incident-based matching (primary) or keyword fallback
    matched_ids = result.get("matched_ids", []) if result else []
    tripwire = check_incident_tripwire(matched_ids)
    if not tripwire:
        # Fallback to keyword matching when no incident matches found
        tripwire = check_error_patterns(prompt)
    if tripwire:
        parts.append(tripwire)

    # Warn if daemon is down (semantic search degraded)
    if not used_daemon:
        parts.append(
            f"[ENGRAM Recall: daemon not running — using FTS-only (no semantic matching)."
            f"Restart daemon: bash {os.path.join(ENGRAM_HOME, 'hooks', 'start-engram-daemon.sh')}]"
        )

    # ENGRAM recall nudge
    if result is not None:
        nudge = format_nudge(result)
        if nudge:
            parts.append(nudge)

    # Write reminder (from Stop hook)
    write_reminder = check_write_reminder()
    if write_reminder:
        parts.append(write_reminder)

    # Tool-call repair notification (from PreToolUse repair hook)
    repair_notice = check_repair_marker()
    if repair_notice:
        parts.append(repair_notice)

    # Feeling-nudge (post-compact wake-up). Read-only — does NOT clear the
    # marker; only engram_report_feeling clears it.
    feeling_nudge = check_feeling_nudge_marker()
    if feeling_nudge:
        parts.append(feeling_nudge)

    # Warm-restart briefing pointer (first prompt after compaction only).
    # Delivered as a pointer, not full injection — the agent discovers and
    # chooses to read the note, like Lucy finding Henry's journal (dv_0094).
    warm_briefing = check_warm_briefing(counter["prompts_since_compaction"])
    if warm_briefing:
        parts.append(warm_briefing)

    # Nap warning (if approaching compaction) — uses JSONL-based context tracker
    try:
        sys.path.insert(0, HOOK_DIR)
        from context_tracker import estimate_usage, format_drowsiness
        # #140: thread session_id + transcript_path from this hook's stdin
        # so drowsiness reads THIS session's per-session marker, not a
        # single shared global marker that other sessions could clobber.
        usage = estimate_usage(
            transcript_path=hook_input.get("transcript_path"),
            session_id=hook_input.get("session_id"),
        )
        if usage:
            nap_warning = format_drowsiness(usage)
            if nap_warning:
                parts.append(nap_warning)
    except ImportError:
        # Fallback to prompt count if context tracker not available
        nap_warning = format_nap_warning(counter["prompts_since_compaction"])
        if nap_warning:
            parts.append(nap_warning)

    if not parts:
        print(json.dumps({}))
        sys.exit(0)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
