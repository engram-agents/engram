#!/bin/bash
# credential-probe.sh -- THROWAWAY, ONE-TIME diagnostic (engram-alpha#3002).
#
# This is NOT part of the real wake mechanism. It is never invoked by
# clio-wake-launch.sh, clio-wake-run.sh, or any other part of the real wake
# chain -- it exists to answer a single open question BEFORE the real
# LaunchDaemon migration is installed, and is meant to be loaded exactly
# once (via `sudo launchctl bootstrap system/...`, see
# com.engram.clio-credential-probe.plist.template + this directory's
# README.md "Before installing" section), checked, and then immediately
# `sudo launchctl bootout`'d. Never leave this loaded as a standing job.
#
# THE QUESTION: the real wake mechanism's user-level LaunchAgent
# (gui/<uid> domain) cannot be installed on this seat -- this macOS account
# has never had a GUI/console login, so that launchd domain doesn't exist
# for it. The fix under discussion is a LaunchDaemon (system/ domain,
# root-installed, with a UserName key) instead -- LaunchDaemons don't
# require a GUI session. But a LaunchDaemon-started process also has NO GUI
# session anywhere in its ancestry, and it is UNKNOWN whether this account's
# macOS login Keychain -- where Claude Code stores its
# "Claude Code-credentials" secret -- can be read without one. If it
# can't, a LaunchDaemon-started `claude` process would silently fail to
# authenticate: exactly the dark-seat problem #3002 exists to close, one
# layer deeper. This script answers that question empirically, once,
# without guessing or attempting a fix.
#
# SAFETY: never prints or stores the secret value itself. The `security -w`
# call's stdout is unconditionally redirected to /dev/null; only the exit
# code and stderr text are ever persisted, into a fixed, well-known path
# ($HOME/.engram/credential-probe-result.txt) so a human with no Claude
# session running at all can still read the answer.
#
# Exits with the SAME exit code `security` itself returned, so the
# LaunchDaemon's own exit status (inspectable via `launchctl print` after
# the fact) is directly meaningful too.
#
# Usage: credential-probe.sh   (no args)

set -uo pipefail   # NO -e: we must reach the result-file write on failure.

RESULT_FILE="$HOME/.engram/credential-probe-result.txt"
mkdir -p "$(dirname "$RESULT_FILE")"

STDERR_FILE="$(mktemp)"
trap 'rm -f "$STDERR_FILE"' EXIT

security find-generic-password -s "Claude Code-credentials" -w >/dev/null 2>"$STDERR_FILE"
ec=$?

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ $ec -eq 0 ]]; then
    echo "[$ts] SUCCESS: Keychain item read OK (exit 0) -- LaunchDaemon-context Keychain access confirmed." > "$RESULT_FILE"
else
    # Flatten to a single line -- stderr from `security` can itself carry
    # embedded newlines, and the result file is documented as single-line.
    err="$(tr '\n' ' ' < "$STDERR_FILE" | sed 's/  */ /g; s/ *$//')"
    echo "[$ts] FAILURE: exit=$ec stderr=$err" > "$RESULT_FILE"
fi

exit "$ec"
