#!/usr/bin/env python3
"""UserPromptSubmit hook: detect end-of-day phrases + surface engram-sleep nudge.

When the user signals end-of-day with phrases like "good night", "call it
a day", "wrap up the day", "ending for tonight", etc., this hook injects
an additionalContext block suggesting the agent run engram-sleep to
consolidate the day's work. The agent surfaces this to the user, who can
say "Yes" (run the skill), "Not yet" (proceed normally), or just keep
working (ignore the nudge — the hook is non-blocking).

Part of the three-routine self-engram-maintenance architecture;
implementation of PR B of engram-alpha #133.

Gates:
- Skipped if prompt starts with `/` or `!` (commands, not natural language)
- Skipped if a sleep cycle completed within the last 4 hours (sleep-marker
  freshness check) — handles the re-fire case where user says "good night
  ... actually one more thing ... ok really good night" within the same
  session.
- The agent still uses judgment: false-positive matches (e.g., "yesterday
  we wrapped up the day with the bug fixed") will fire the hook, but the
  agent reads context and decides whether the nudge applies.

Exit codes:
  0 — success (JSON on stdout, possibly empty {})
  1 — non-blocking error (logged, prompt proceeds without nudge)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
SLEEP_MARKER = os.path.join(ENGRAM_HOME, "sessions", "last-sleep-success.json")

# Hours since last successful sleep below which the nudge is suppressed.
# Tuned to handle within-session re-fire ("good night... wait... ok night")
# without missing the next-day case (where the marker is ~24h old).
SLEEP_SUPPRESSION_HOURS = 4

# End-of-day trigger phrases — case-insensitive, word-boundary aware.
# Designed to catch natural wrap-up signals. False positives are
# acceptable; the agent uses surrounding context to judge whether to
# actually surface the sleep nudge to the user.
EOD_PATTERNS = [
    r"\bgood\s*night\b",
    r"\bcall\s+it\s+(?:a\s+)?(?:day|night)\b",
    r"\blet'?s\s+call\s+it\b",
    r"\bwrap(?:ping)?\s+up\s+(?:the\s+)?(?:day|night|session)\b",
    r"\bwind(?:ing)?\s+down\s+for\s+(?:the\s+)?(?:day|night)\b",
    r"\bend\s+of\s+(?:the\s+)?(?:day|night|session)\b",
    r"\bdone\s+for\s+(?:today|tonight|the\s+day|the\s+night)\b",
    r"\bsigning\s+off\b",
    r"\bturning\s+in\s+for\s+the\s+night\b",
    r"\bending\s+for\s+(?:tonight|today|the\s+(?:day|night))\b",
]

EOD_RE = re.compile("|".join(EOD_PATTERNS), re.IGNORECASE)


def sleep_recently() -> tuple[bool, str]:
    """True if a sleep cycle completed within SLEEP_SUPPRESSION_HOURS.

    Returns (suppressed, hours_since_str).
    """
    if not os.path.exists(SLEEP_MARKER):
        return False, ""
    try:
        with open(SLEEP_MARKER) as f:
            marker = json.load(f)
        completed_at_str = marker["completed_at"]
        completed_at = datetime.fromisoformat(
            completed_at_str.replace("Z", "+00:00")
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False, ""
    hours_since = (
        datetime.now(timezone.utc) - completed_at
    ).total_seconds() / 3600
    return hours_since < SLEEP_SUPPRESSION_HOURS, f"{hours_since:.1f}h"


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        # Hook protocol expects JSON on stdin; fail silently if not.
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        sys.exit(0)

    match = EOD_RE.search(prompt)
    if not match:
        sys.exit(0)

    # Gate: don't fire if a sleep cycle ran in the last few hours.
    suppressed, hours_since = sleep_recently()
    if suppressed:
        sys.exit(0)

    matched_phrase = match.group(0)

    additional_context = (
        f"[End-of-day detected] The phrase \"{matched_phrase}\" reads as an "
        f"end-of-day wrap-up signal. The canonical end-of-day routine is "
        f"`engram-sleep` — a single two-phase skill: Phase A (day-wide cohort "
        f"review, missed-node capture, warm-briefing rotation, history-file "
        f"reconcile; pre-turn-advance) then Phase B (dream-fairies + "
        f"consolidation + turn-advance + dream-record write).\n"
        f"If this looks like a real wrap-up moment, surface to the user: "
        f"\"It seems we're at the end of the day — should I run engram-sleep "
        f"to consolidate?\" Their \"Yes\" → run the skill; \"not yet\" or no "
        f"response → proceed with the actual request. If the phrase is "
        f"incidental (e.g., \"yesterday we wrapped up the day with X\"), "
        f"ignore this nudge."
    )

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
