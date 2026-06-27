#!/usr/bin/env python3
"""
PreToolUse hook for Bash: warns when a pipe from a consequential command to
a truncating reader (tail/head) masks the upstream command's exit status.

THE PROBLEM
===========
Shell pipelines return the exit status of the last command in the chain.
When an agent writes:

    pytest tests/ | tail -20 && git commit -m 'wip'

the `&&` sees tail's exit code (almost always 0), not pytest's. A failing
test suite triggers a commit anyway — silent corruption of the git history.

Similarly:

    forum reply 42 | tail -1; echo '✓ done'

The echo always prints even if forum reply failed.

WHAT THIS HOOK DOES
===================
Detects the high-value shapes — consequential upstream | tail/head in a
dangerous context (chained via && to an actionable command, or followed by
a success-word echo) — and emits an advisory warning in additionalContext.
Never blocks. The warning explains how to fix: set -o pipefail, PIPESTATUS,
or read a positive success marker.

TIER: T2 (Convenience) — degrades UX on misconfigured pipe but is recoverable.
"""

import json
import re
import sys


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Consequential upstream commands — commands whose exit status matters for
# correctness (test runners, VCS, issue trackers, build tools, CI tools).
_CONSEQUENTIAL_RE = re.compile(
    r'(?:^|;|&&|\|\|)\s*'
    r'(?:pytest|python3|python|git|gh|forum|ia\b|baton|make|cargo|npm run)',
    re.MULTILINE,
)

# Truncating readers — pipe targets that discard the upstream's exit status.
# grep excluded: grep -q is a genuine conditional check, too many false positives.
_TRUNCATING_READER_RE = re.compile(r'\|\s*(?:\S+\s+)*(?:head|tail)\b')

# Pipefail guards — any of these present means the author is handling exit
# status correctly; skip the warning.
_PIPEFAIL_GUARD_RE = re.compile(
    r'set\s+-[a-zA-Z]*e[a-zA-Z]*o\s+pipefail'   # set -eo pipefail
    r'|set\s+-o\s+pipefail'                       # set -o pipefail
    r'|set\s+-[a-zA-Z]*e\b'                       # set -e (conservative signal)
    r'|PIPESTATUS',                               # PIPESTATUS array usage
)

# Dangerous context — actionable commands chained via && after the pipeline.
_DANGEROUS_CHAIN_RE = re.compile(
    r'&&\s*(?:git\s+commit|git\s+push|git\s+merge|'
    r'gh\s+pr|gh\s+issue|'
    r'forum\s+reply|forum\s+post|'
    r'ia\s+write|baton\s+flip|baton\s+init|'
    r'push\s+origin)'
)

# Dangerous context — success-word echo after the pipeline (semicolon or &&).
_SUCCESS_ECHO_RE = re.compile(
    r'(?:;|&&)\s*echo\s+["\']?[^"\']*'
    r'(?:done|✓|success|ok\b|OK\b|complete|sent|posted)',
)


def _has_pipe_exit_risk(command: str) -> bool:
    """Return True if the command has an unguarded consequential-pipe-to-truncating-reader
    in a dangerous context."""

    # Fast path: must have a pipe to a truncating reader at all.
    if not _TRUNCATING_READER_RE.search(command):
        return False

    # Must have a consequential upstream somewhere before a pipe.
    if not _CONSEQUENTIAL_RE.search(command):
        return False

    # If any pipefail guard is present, the author is handling it.
    if _PIPEFAIL_GUARD_RE.search(command):
        return False

    # Must be in a dangerous context: actionable chain or success echo.
    if _DANGEROUS_CHAIN_RE.search(command):
        return True
    if _SUCCESS_ECHO_RE.search(command):
        return True

    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)  # never block on parse errors

    try:
        command = hook_input.get("input", {}).get("command", "")
        if not isinstance(command, str) or not command:
            sys.exit(0)

        if not _has_pipe_exit_risk(command):
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": (
                    "[pipe-exit-warn] Exit status from the upstream command is masked by "
                    "| tail/head — the pipeline's exit status reflects tail/head, not the "
                    "upstream command. To check the upstream's result: use "
                    "'set -o pipefail' before the pipe, OR capture 'PIPESTATUS[0]' "
                    "immediately after, OR read a positive printed success marker "
                    "(a count, an ID, a 'sent →' line) — never absence-of-visible-error."
                ),
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a Bash call


if __name__ == "__main__":
    main()
