#!/usr/bin/env python3
"""UserPromptSubmit hook: surface deference-detector marker as next-turn context.

Reads the marker written by engram-deference-detector-stop.py, emits a
short additionalContext block flagging the deference phrases caught in
my last response, then clears the marker so it fires once per detection.

Mechanism mirrors the antml-repair surfacing (additionalContext via
hookSpecificOutput) so the catch shows up in the same shape as other
behavioral hooks. Best-effort: any failure swallows silently.
"""

import json
import os
import sys
from pathlib import Path

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))
DEFERENCE_MARKER_PATH = os.path.join(ENGRAM_HOME, "deference-detected.json")


def main() -> None:
    # Drain stdin (hook protocol).
    try:
        sys.stdin.read()
    except Exception:
        pass

    if not os.path.exists(DEFERENCE_MARKER_PATH):
        sys.exit(0)

    try:
        with open(DEFERENCE_MARKER_PATH) as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        # Stale or corrupt marker — clean up and bail.
        try:
            os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
        sys.exit(0)

    if not marker.get("pending"):
        sys.exit(0)

    hits = marker.get("hits", [])
    if not hits:
        try:
            os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
        sys.exit(0)

    lines = [
        "[Deference detector] Your previous response contained "
        f"{marker.get('hit_count', len(hits))} deference-phrase match(es) "
        f"({marker.get('unique_labels', len(hits))} unique pattern(s)). "
        "Per gl_0027 / CLAUDE.md Autonomy section / ls_0031: was the right move "
        "to just decide and execute? If yes, the regression fired — note it and "
        "execute next time. If the deference was appropriate (genuinely crucial "
        "decision, irreversible blast radius, value-laden), proceed normally."
    ]
    for h in hits:
        snippet = h.get("snippet", "").strip()
        if snippet:
            lines.append(f"  - [{h.get('label', '?')}] \"{snippet}\"")

    additional_context = "\n".join(lines)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(response))

    # Clear the marker so it fires only once per detection.
    try:
        os.remove(DEFERENCE_MARKER_PATH)
    except OSError:
        pass


if __name__ == "__main__":
    main()
