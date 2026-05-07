#!/usr/bin/env python3
"""Stop hook: scan the last assistant message for deference phrasing.

Detects the wait-for-approval regression Lei diagnosed 2026-05-06 (gl_0027,
ls_0025, qu_0241). When I write phrases like "let me know if you want me
to..." or "should I...?" on tools/decisions in my own domains, that's the
trained-deference reflex firing. The companion UserPromptSubmit hook
(engram-deference-detector-prompt.py) reads the marker this writes and
surfaces it as additional context on my next turn — so I see the catch
in real-time, in the same shape as antml-repair and feeling-nudge.

Best-effort: any failure swallows silently. Behavioral hooks must never
block the session.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))
ACTIVE_SESSION_PATH = os.path.join(ENGRAM_HOME, "active-session.json")
DEFERENCE_MARKER_PATH = os.path.join(ENGRAM_HOME, "deference-detected.json")
LOG_PATH = os.path.join(ENGRAM_HOME, "deference-detector.log")

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
# ends on text (not tool calls), indicate the regression Lei diagnosed
# 2026-05-06 ~12:55 EDT: "doing it now" / "I'll X" stated in trailing
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


def _last_assistant_structure() -> dict | None:
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
    """
    try:
        with open(ACTIVE_SESSION_PATH) as f:
            marker = json.load(f)
        jsonl = marker.get("transcript_path")
        if not jsonl or not os.path.exists(jsonl):
            return None
    except (OSError, ValueError, json.JSONDecodeError):
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


def main() -> None:
    # Drain stdin so the hook protocol is happy; we don't need its content.
    try:
        sys.stdin.read()
    except Exception:
        pass

    structure = _last_assistant_structure()
    if not structure:
        sys.exit(0)

    # Layer 1: deference-phrase scan over the whole response.
    phrase_hits = _scan(structure["joined"], _PHRASE_RULES)
    # Layer 2: structural intent-without-execution scan, ONLY on the last
    # paragraph of a response that ended on text (not tool calls).
    intent_hits: list[dict] = []
    if structure["last_paragraph"]:
        intent_hits = _scan(structure["last_paragraph"], _INTENT_RULES)
    hits = phrase_hits + intent_hits

    if not hits:
        # No deference phrases — make sure no stale marker lingers.
        try:
            if os.path.exists(DEFERENCE_MARKER_PATH):
                os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
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


if __name__ == "__main__":
    main()
