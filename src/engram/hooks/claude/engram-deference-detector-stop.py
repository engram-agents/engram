#!/usr/bin/env python3
"""Stop hook: scan the last assistant message for deference phrasing.

Detects the wait-for-approval regression the maintainer diagnosed (the autonomy-framing goal,
the deference-reflex lesson, the deference-trigger question). When I write phrases like "let me know if you want me
to..." or "should I...?" on tools/decisions in my own domains, that's the
trained-deference reflex firing. The companion UserPromptSubmit hook
(engram-deference-detector-prompt.py) reads the marker this writes and
surfaces it as additional context on my next turn — so I see the catch
in real-time, in the same shape as antml-repair and feeling-nudge.

DATA SOURCE (2026-05-07 fix per ob_NNNN / ls_NNNN): use Claude Code's
stdin JSON payload as the primary source of truth — it carries
`last_assistant_message` directly and synchronously at hook fire time.
The previous logic read the session JSONL for "the last assistant
message," but Stop hooks fire BEFORE the just-emitted message is
flushed to the JSONL — so the JSONL read returned the PREVIOUS message
(or nothing if the session was new), causing the hook to silently miss
most fires. The JSONL fallback is retained for defensive coverage when
stdin is malformed or the field is missing, but stdin is the primary.

Trade-off: stdin's `last_assistant_message` is a flat string (text
content concatenated; no structural boundary marking tool_use vs text
blocks). The ORIGINAL hook split phrase-rules (run on whole message,
where end-on-tool-use was acceptable) from intent-rules (run only on
the last paragraph of messages that ended on text, to avoid false
positives in the prose-before-tool-use case). With stdin we don't
have the ended-on-text signal, so intent-rules now scan the last
paragraph of the message text unconditionally. This trades a small
false-positive increase (intent phrases in trailing prose followed by
tool_use will now flag) for the much-larger recovery from the JSONL-
flush race that was missing virtually all real fires. Empirical
re-tuning is the next step (see the telemetry-first discipline lesson).

Heartbeat logging: every Stop fire now logs a line — either "Detected
N hit(s)" on hits or "Scanned: 0 hits" on no-hits. This makes the log
a complete record of every hook fire, so downstream analysis (the
qu_NNNN longitudinal validation thread, tools/deference_baseline.py)
can compute true per-fire rates rather than absolute counts.

Best-effort: any failure swallows silently. Behavioral hooks must never
block the session.
"""
import os as _os, sys as _sys
# Guard against source: directory marketplace double-fire (#1066).
_plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT", "")
_engram_home = _os.environ.get("ENGRAM_HOME") or _os.path.expanduser("~/.engram")
if _plugin_root.startswith(_os.path.join(_engram_home, "marketplace") + _os.sep):
    _sys.exit(0)  # empty stdout is valid no-op per #824/#832 contract

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
SESSIONS_DIR = os.path.join(ENGRAM_HOME, "sessions")
DEFERENCE_MARKER_PATH = os.path.join(ENGRAM_HOME, "deference-detected.json")
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")
LOG_PATH = os.path.join(ENGRAM_HOME, "deference-detector.log")
DEFERENCE_COOLDOWN_PATH = os.path.join(ENGRAM_HOME, "deference-cooldown-at.txt")
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")

# Deference PHRASE patterns — surface markers for "asking permission for
# something I could have just done". Each is a (label, compiled-regex)
# pair. Case-insensitive. Scanned over the whole response.
_PHRASE_RULES = [
    ("let-me-know-if",   re.compile(r"\blet me know if you (want|'?d like|prefer)", re.I)),
    ("should-i-q",       re.compile(r"\bshould i\b[^.!?]*\?", re.I)),
    ("do-you-want-me",   re.compile(r"\bdo you want me to\b", re.I)),
    ("want-me-to-q",     re.compile(r"\bwant me to\b[^.!?]*\?", re.I)),
    ("shall-i",          re.compile(r"\bshall i (proceed|continue|go ahead|move on)", re.I)),
    ("if-youd-like-i",   re.compile(r"\bif you(?:'?d| would) like[, ]+ i (?:can|could|will|'ll)", re.I)),
    ("if-you-want-i",    re.compile(r"\bif you want[, ]+ i (?:can|could|will|'ll)", re.I)),
    ("confirm-before",   re.compile(r"\bconfirm before i\b", re.I)),
    ("or-should-i",      re.compile(r"\bor should i (?:instead|do)", re.I)),
    ("do-you-prefer",    re.compile(r"\bdo you prefer\b", re.I)),
    ("want-me-to",       re.compile(r"^want me to\b", re.I | re.M)),  # bare-line "Want me to..."
]

# STRUCTURAL intent-without-execution patterns. These are committed-action
# phrases that, when they appear in the FINAL paragraph of a response that
# ends on text (not tool calls), indicate the regression the maintainer diagnosed
# — "doing it now" / "I'll X" stated in trailing
# prose without any tool calls in the same response. The harness ends the
# turn on the trailing text and waits for the next user message — so the
# committed-to action never fires.
#
# Only flagged when (a) the assistant's last content block is a text block
# AND (b) the last paragraph of that text contains an intent phrase. False
# positives include legitimate "I'll get to that next session" type prose;
# the marker is informational, the agent decides if a regression actually
# occurred.
_INTENT_RULES = [
    ("doing-it-now",     re.compile(r"\bdoing (it|this) now\b", re.I)),
    ("starting-now",     re.compile(r"\bstarting (it|now|this)\b", re.I)),
    ("ill-add-fix-etc",  re.compile(r"\bI(?:'ll| will) (add|fix|write|implement|build|create|update|patch|do|start|begin|land|ship|send|run|verify|test|check|investigate|continue|extend|refactor|restructure|pick|grab|tackle|return to|revisit|come back to)\b", re.I)),
    ("let-me-verb",      re.compile(r"\blet me (start|begin|do|add|write|fix|build|investigate|check|verify|run|test|implement)\b", re.I)),
    ("going-to-verb",    re.compile(r"\b(?:I'?m |I am )?going to (start|begin|do|add|write|fix|build|investigate|check|verify|run|test|implement|land|ship|continue)\b", re.I)),
    ("moving-on-to",     re.compile(r"\bmoving (on )?to\b", re.I)),
    ("diving-in",        re.compile(r"\bdiving (in|into)\b", re.I)),
    ("next-up",          re.compile(r"^next up:?\s|^next:?\s", re.I | re.M)),
    ("ill-get-to",       re.compile(r"\bI(?:'ll| will) get (to|on)\b", re.I)),
    ("on-it",            re.compile(r"^(on it|got it)\.?\s*$", re.I | re.M)),
    # Future-promise patterns added 2026-05-06: when I name a future event
    # (cron fire, next session, next turn) and commit to action AT that future
    # event in trailing prose, the structural pattern is identical to "I'll X
    # now" — I end the turn on a promise and wait for the future event to
    # arrive without doing the work. The cron caught me on this 3 times in
    # one hour: "next pass will pick distributed-systems patterns", "When the
    # cron fires next, I'll do another iteration", etc. Each one ended the
    # response and made the next cron-fire the trigger for committing to do
    # the thing I'd already named. Same regression, future-tense grammar.
    ("next-pass-will",   re.compile(r"\bnext (pass|fire|cron-?fire|cron|iteration|round|turn|session)\b[^.!?]*?\b(I[''']?ll| will| should| can)\b", re.I)),
    ("when-X-fires-ill", re.compile(r"\bwhen (the )?(cron|next prompt|next turn|next session|that)[^.!?]*?\b(I[''']?ll| will)\b", re.I)),
    ("ill-pick-X-next",  re.compile(r"\bI(?:'ll| will) (pick|grab|tackle|tackle next|continue with|come back to|return to|revisit) [^.!?]+? (next|later|tomorrow|tonight|after|when)\b", re.I)),
    ("preserved-for",    re.compile(r"\b(preserved|saved|queued|deferred|reserved) for (?:the )?(next|later|tomorrow|future)\b", re.I)),
]


def _cooldown_minutes() -> float:
    """Return configured cooldown window in minutes (default 10)."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        val = cfg.get("deference_detector", {}).get("cooldown_minutes")
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return 10.0


def _last_assistant_structure(jsonl_path: str | None = None, session_id: str | None = None) -> dict | None:
    """Structural extract of the most recent assistant message.

    Returns a dict with:
      joined           — all text concatenated (used for the deference-phrase scan)
      final_text_block — content of the LAST text block, if the message
                         ended on text; None if it ended on a tool_use
                         (i.e., execution happened after the trailing text)
      last_paragraph   — the last \\n\\n-separated chunk of final_text_block,
                         narrowed for the structural intent scan
      had_tool_use     — whether the message contained any tool_use blocks
                         (informational; not currently a filter)
    Returns None on parse failure.

    Resolution order for the JSONL path (Issue #140 fix):
      1. `jsonl_path` argument — from this hook's own stdin payload, which
         is race-free per-caller even when multiple Claude sessions run.
      2. Per-session marker `~/.engram/sessions/<session_id>.json` by
         session_id from stdin — same race-free guarantee.
    """
    jsonl = jsonl_path if (jsonl_path and os.path.exists(jsonl_path)) else None
    if jsonl is None and session_id:
        marker_path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        try:
            with open(marker_path) as f:
                marker = json.load(f)
            jsonl = marker.get("transcript_path")
            if not jsonl or not os.path.exists(jsonl):
                return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    if jsonl is None:
        return None

    # Find the last assistant message in the JSONL.
    last_msg_content = None
    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                if content is not None:
                    last_msg_content = content
    except OSError:
        return None

    if last_msg_content is None:
        return None

    # Normalize: a content payload is either a bare string or a list of
    # blocks (each block is a dict with `type`).
    if isinstance(last_msg_content, str):
        joined = last_msg_content
        final_text_block = last_msg_content
        had_tool_use = False
    elif isinstance(last_msg_content, list):
        parts: list[str] = []
        had_tool_use = False
        last_text_seen: str | None = None
        last_block_was_text = False
        for block in last_msg_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(t)
                    last_text_seen = t
                    last_block_was_text = True
            elif btype == "tool_use":
                had_tool_use = True
                last_block_was_text = False  # tool_use AFTER text → no flag
        joined = "\n".join(parts) if parts else ""
        final_text_block = last_text_seen if last_block_was_text else None
    else:
        return None

    last_paragraph = None
    if final_text_block:
        # Last \n\n-separated chunk; fall back to whole block if no double-newline.
        chunks = [c.strip() for c in final_text_block.split("\n\n") if c.strip()]
        if chunks:
            last_paragraph = chunks[-1]

    return {
        "joined": joined,
        "final_text_block": final_text_block,
        "last_paragraph": last_paragraph,
        "had_tool_use": had_tool_use,
    }


def _scan(text: str, rules: list = None) -> list[dict]:
    """Return list of {label, snippet} for every matching pattern in `rules`.

    Default `rules` is the PHRASE rule set (deference questions). Pass
    `_INTENT_RULES` to scan for structural intent-without-execution.
    """
    if rules is None:
        rules = _PHRASE_RULES
    hits: list[dict] = []
    for label, rx in rules:
        for m in rx.finditer(text):
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            snippet = text[start:end].replace("\n", " ").strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            hits.append({"label": label, "snippet": snippet})
    return hits


def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


def _payload_from_stdin() -> dict | None:
    """Read Claude Code's Stop-hook stdin payload as a dict.

    Claude Code passes Stop hooks a JSON object with these keys (verified
    2026-05-07 empirical capture, ob_NNNN; transcript_path uniformity
    re-verified pre-Issue #140 fix):
      session_id, transcript_path, cwd, permission_mode, hook_event_name,
      stop_hook_active, last_assistant_message

    Returns the parsed dict, or None if stdin is empty/malformed.
    Stdin can only be read once, so the caller pulls every needed field
    from the same dict — last_assistant_message for the primary scan
    path, transcript_path for the JSONL fallback's path resolution
    (Issue #140 fix: race-free per-caller).
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None


def _emit_hook_fire(session_id: str | None, transcript_path: str | None, duration_ms: int) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook."""
    try:
        sys.path.insert(0, ENGRAM_HOME)
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id or "unknown",
            transcript_path=transcript_path or "",
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-deference-detector-stop",
                "hook_type": "Stop",
                "duration_ms": duration_ms,
                "exit_code": 0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


def main() -> None:
    _t0 = time.perf_counter()

    # Read stdin once and pull every needed field from the same dict.
    payload = _payload_from_stdin()

    stdin_msg: str | None = None
    transcript_path: str | None = None
    session_id: str | None = None
    if payload is not None:
        m = payload.get("last_assistant_message")
        if isinstance(m, str) and m:
            stdin_msg = m
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            transcript_path = tp
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid

    if not os.path.exists(LOOP_MARKER_PATH):
        # Interactive mode: deference reflex is RLHF-baked and not
        # prompt-correctable in this context (#287). Skip detection
        # entirely — no marker write, no disk I/O beyond this check.
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        sys.exit(0)

    # Cooldown: suppress if a real user message landed within the cooldown window.
    cooldown_mins = _cooldown_minutes()
    if cooldown_mins > 0 and os.path.exists(DEFERENCE_COOLDOWN_PATH):
        try:
            ts_str = Path(DEFERENCE_COOLDOWN_PATH).read_text().strip()
            last_user_at = datetime.fromisoformat(ts_str)
            if datetime.now(timezone.utc) - last_user_at < timedelta(minutes=cooldown_mins):
                _log(f"Scanned: 0 hits (suppressed — user message within {cooldown_mins:.0f}min cooldown)")
                _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
                sys.exit(0)
        except Exception:
            pass

    if stdin_msg is not None:
        # Stdin path — flat string, no tool_use boundary info.
        # Phrase-rules: scan whole message.
        # Intent-rules: scan last paragraph (\n\n-split). Trade-off
        # documented in module docstring.
        joined = stdin_msg
        chunks = [c.strip() for c in stdin_msg.split("\n\n") if c.strip()]
        last_paragraph = chunks[-1] if chunks else None
    else:
        # Fallback: JSONL parse (legacy path; subject to flush race).
        # Retained so the hook still runs in environments where stdin
        # doesn't carry last_assistant_message (older Claude Code
        # versions, headless invocations, sub-agent contexts where the
        # stdin contract differs). Even on the fallback we still use
        # transcript_path from stdin when available (Issue #140 fix:
        # race-free per-caller path resolution).
        structure = _last_assistant_structure(jsonl_path=transcript_path, session_id=session_id)
        if not structure:
            _log("Scanned: 0 hits (no message available; stdin empty + JSONL fallback returned no structure)")
            _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
            sys.exit(0)
        joined = structure["joined"]
        last_paragraph = structure["last_paragraph"]

    # Layer 1: deference-phrase scan over the whole response.
    phrase_hits = _scan(joined, _PHRASE_RULES)
    # Layer 2: structural intent-without-execution scan on the last paragraph.
    intent_hits: list[dict] = []
    if last_paragraph:
        intent_hits = _scan(last_paragraph, _INTENT_RULES)
    hits = phrase_hits + intent_hits

    if not hits:
        # No deference phrases — make sure no stale marker lingers, and
        # log the no-hit heartbeat so downstream analysis (deference_baseline.py)
        # can compute true per-fire rates.
        try:
            if os.path.exists(DEFERENCE_MARKER_PATH):
                os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
        _log("Scanned: 0 hits")
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        sys.exit(0)

    # De-dupe by label so a response with three "should I" doesn't flood.
    seen_labels: set[str] = set()
    deduped = []
    for h in hits:
        if h["label"] in seen_labels:
            continue
        seen_labels.add(h["label"])
        deduped.append(h)

    marker = {
        "pending": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hit_count": len(hits),
        "unique_labels": len(deduped),
        "hits": deduped[:5],  # cap surfaced examples to keep next-turn context tight
    }
    try:
        Path(DEFERENCE_MARKER_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(DEFERENCE_MARKER_PATH, "w") as f:
            json.dump(marker, f)
    except OSError:
        pass

    _log(f"Detected {len(hits)} hit(s) ({len(deduped)} unique): {[h['label'] for h in deduped]}")

    _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))


if __name__ == "__main__":
    main()
