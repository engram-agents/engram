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

BACKGROUNDED COMMANDS (issue #2061)
====================================
For a foreground command, the risk model above requires an *in-command*
actionable consumer (an `&&`-chained command, or a success-word echo) because
that is the only consumer that exists — the agent sees the exit status
directly and reacts to it inline.

For a backgrounded command (`tool_input.run_in_background` truthy), that
model inverts: there is no in-command consumer to inspect, because the
harness's own "completed (exit code 0)" notification is the consumer, and it
reports the *pipeline's* exit code — tail/head's, not the upstream's. A bare
`pytest tests/ | tail -8` run in the background looks like it succeeded via
the harness notification even when pytest failed. So for backgrounded
commands the actionable-consumer requirement is dropped: a bare truncating
reader alone is enough to warn, because the out-of-band consumer is always
there.

The same trap has a pipe-free variant that the backgrounded branch also
detects: a redirect followed by a separate tail/head statement, e.g.
`pytest tests/ -q > log.txt; tail -30 log.txt` (measured incident, 2026-08-03 — 58
failures in `log.txt` presented as a clean pass because the notification
reflects `tail`'s exit code, not pytest's). Detection here is deliberately
modest: a `>`/`>>` redirect anywhere in the command followed later by a
`;`/`&&`-separated tail/head invocation, with no attempt to match the
redirected filename against the one tail/head reads. Foreground behavior is
unchanged — this shape is only checked when backgrounded.

Scope bound, stated so silence here isn't misread as breakage (#2112
round 3): the trailing-echo variant of the 2026-08-03 incident family
(`pytest ...; echo "done"` backgrounded — the echo resets the reported
status with no pipe and no redirect) is a THIRD shape this pipe-scoped
hook deliberately does not cover.

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
#
# The anchor alternation includes a single `|` (issue #1899): without it, a
# consequential command MID-PIPELINE — e.g. `echo '{}' | python3 ctx.py |
# head -20`, where python3 sits between two pipes rather than at the start —
# was unreachable, because the anchor only recognized start-of-string, `;`,
# `&&`, or `||` as valid positions before a command name. `\|{1,2}` (rather
# than listing `\|` and `\|\|` as separate alternatives) matches both a
# single pipe and a double pipe in one alternative — greedy `{1,2}` prefers
# the longer match, so `||` is still consumed whole and doesn't collapse to
# two single-pipe matches.
_CONSEQUENTIAL_RE = re.compile(
    r'(?:^|;|&&|\|{1,2})\s*'
    # Skip any VAR=value env prefixes before the command name — without this,
    # 'ENGRAM_NO_EMBEDDINGS=1 pytest ... | tail' (this repo's own documented
    # pytest convention) and 'FOO=bar git push | tail' were invisible
    # (colleague probe, #2112 round 3). The prefix alone never makes a
    # command consequential; it only must not hide one.
    r'(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
    r'(?:pytest|python3|python|git|gh|forum|ia\b|baton|make|cargo|npm run)',
    re.MULTILINE,
)

# Truncating readers — pipe targets that discard the upstream's exit status.
# grep excluded: grep -q is a genuine conditional check, too many false positives.
_TRUNCATING_READER_RE = re.compile(r'\|\s*(?:\S+\s+)*(?:head|tail)\b')

# Redirect-then-read shape (issue #2061, the 2026-08-03 incident): no pipe at all — the
# consequential command redirects its output to a file, and a LATER,
# semicolon/&&-separated statement tails/heads that (or another) file. This
# masks the upstream's exit status just as badly as a pipe does for a
# backgrounded command, because the harness's completion notification still
# reports the *last* statement's exit code (tail's), not the redirected
# command's. Deliberately modest: matches a `>`/`>>` redirect anywhere,
# followed later by a `;`/`&&`-separated tail/head invocation — no attempt to
# match the redirected filename against the file tail/head reads.
_REDIRECT_THEN_READ_RE = re.compile(
    r'>>?\s*\S+.*?(?:;|&&)\s*(?:\S+\s+)*(?:head|tail)\b',
    re.DOTALL,
)

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


def _has_pipe_exit_risk(command: str, backgrounded: bool = False) -> bool:
    """Return True if the command has an unguarded consequential-pipe-to-truncating-reader
    in a dangerous context.

    ``backgrounded`` (issue #2061): when True, the in-command actionable-consumer
    requirement (dangerous chain / success echo) is dropped — the harness's own
    completion notification is the out-of-band consumer for a backgrounded
    command, so a bare truncating reader is itself the dangerous context.
    Foreground behavior is unchanged.
    """

    # Fast path: must have a pipe to a truncating reader — OR, for a
    # backgrounded command only, a redirect-then-separate-tail/head shape
    # (issue #2061: `cmd > log; tail log` has no pipe but is the same trap
    # for a backgrounded command's out-of-band completion notification).
    has_pipe_reader = bool(_TRUNCATING_READER_RE.search(command))
    has_redirect_reader = backgrounded and bool(
        _REDIRECT_THEN_READ_RE.search(command)
    )
    if not (has_pipe_reader or has_redirect_reader):
        return False

    # Must have a consequential upstream somewhere before a pipe.
    if not _CONSEQUENTIAL_RE.search(command):
        return False

    # If any pipefail guard is present, the author is handling it.
    if _PIPEFAIL_GUARD_RE.search(command):
        return False

    # Backgrounded: the out-of-band harness notification is always a consumer,
    # so the bare pipe alone is the dangerous context — no in-command
    # actionable-consumer required.
    if backgrounded:
        return True

    # Foreground: must be in a dangerous context: actionable chain or success echo.
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
        tool_input = hook_input.get("tool_input") or hook_input.get("input") or {}
        if not isinstance(tool_input, dict):
            sys.exit(0)

        command = tool_input.get("command", "")
        if not isinstance(command, str) or not command:
            sys.exit(0)

        backgrounded = bool(tool_input.get("run_in_background"))

        if not _has_pipe_exit_risk(command, backgrounded=backgrounded):
            sys.exit(0)

        if backgrounded:
            context = (
                "[pipe-exit-warn] Backgrounded command pipes a consequential upstream "
                "into | tail/head. The harness's own completion notification "
                "(\"completed (exit code 0)\") reports the pipeline's exit status — "
                "tail/head's, not the upstream command's — so it can look like success "
                "even when the upstream failed. There is no in-command consumer to "
                "gate on here; the notification IS the consumer, and it's "
                "fabricated-looking for pipelines. To check the upstream's result: use "
                "'set -o pipefail' before the pipe, OR capture 'PIPESTATUS[0]' "
                "immediately after, OR read a positive printed success marker "
                "(a count, an ID, a 'sent →' line) — never a piped exit code."
            )
        else:
            context = (
                "[pipe-exit-warn] Exit status from the upstream command is masked by "
                "| tail/head — the pipeline's exit status reflects tail/head, not the "
                "upstream command. To check the upstream's result: use "
                "'set -o pipefail' before the pipe, OR capture 'PIPESTATUS[0]' "
                "immediately after, OR read a positive printed success marker "
                "(a count, an ID, a 'sent →' line) — never absence-of-visible-error."
            )

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context,
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a Bash call


if __name__ == "__main__":
    main()
