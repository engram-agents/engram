#!/bin/bash
# clio-wake-launch.sh — launchd-invoked entrypoint for the macOS wake
# mechanism (engram-alpha#3002). Mirrors ../agent-session-launch.sh's
# structure; read that file's own header first if this one is confusing —
# the differences from it are called out explicitly below, everything else
# is the same pattern for the same reasons.
#
# Invoked by com.engram.clio-wake.plist's ProgramArguments every
# StartInterval (2h). Unlike the Linux systemd unit (User=agent-%i, a
# different Linux user per agent), this already runs as the single macOS
# user this seat lives under — no sudo, no user-switch, nothing agent-name
# parameterized.
#
# DELIBERATE DIFFERENCE FROM THE LINUX VERSION: the duplicate-launch check
# below is a QUIET, exit-0 SKIP, not an error. The Linux launcher treats an
# already-live session as an anomaly (fires once/day; a still-running
# session at that point is unexpected). This script fires every 2 hours, so
# an already-live session is the EXPECTED common case (a human is working,
# or the standing loop is running fine) — treating it as an error would spam
# the log every cycle for the entire time a normal session is open.
#
# LIVENESS CHECK, ROUND 2 (Aleph's colleague review on PR-3162, round 1):
# `tmux has-session -t clio-wake` only sees a session THIS launcher itself
# started. On a personal Mac (unlike the Linux side, where the agent user
# only ever runs inside its own tmux), an ordinary interactive session
# (e.g. `claude-lx` in a plain terminal) is invisible to that check — so
# every 2h this launcher would start a SECOND, competing Claude session
# beside a perfectly healthy interactive one. Fixed by checking for ANY
# live `claude` process owned by this user, not just this script's own
# tmux session.
#
# `pgrep -u "$(id -u)" -x claude` (the obvious fix, and Aleph's own
# suggested command) was tried FIRST and is ITSELF WRONG on this seat:
# measured directly, `pgrep` — with or without `-x`, with or without `-u`
# — cannot see this seat's own live `claude` process at all (verified
# against a known-live PID via `ps -p <pid>`, which resolves it correctly;
# `pgrep`'s process enumeration silently omits it, root cause not chased
# further since a working alternative exists). Plain `ps -u <uid> -o comm=`
# DOES see it reliably. So the check below uses `ps`, not `pgrep` — if this
# is ported to a different Mac, re-verify this exact command against a
# known-live PID before trusting it; do not assume `pgrep` failed for a
# reason specific to this one seat.
#
# Usage: clio-wake-launch.sh   (no args — this is a single, fixed seat)

set -euo pipefail

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
RUNNER="$RUNNER_DIR/clio-wake-run.sh"
CHARGE_DIR="$RUNNER_DIR"

LOG_DIR="$HOME/.engram/logs"
LOG="$LOG_DIR/clio-wake.log"
mkdir -p "$LOG_DIR"

# S1 (fail loud): touch a heartbeat file on EVERY invocation, before any
# branching — whether this run ends in a skip or a cold launch, this proves
# the checker itself is still being scheduled by launchd at all. Without
# this, an unloaded LaunchAgent or a launcher that dies before reaching
# either branch looks identical to a quiet, healthy seat — exactly the
# ambiguity engram-alpha#3002 is about. Reading/alerting on staleness here
# (e.g. a SessionStart check if this file is older than ~2x StartInterval)
# is left for a follow-up; this PR only makes the signal exist.
date -u +%Y-%m-%dT%H:%M:%SZ > "$HOME/.engram/wake-checker-heartbeat"

# Per-agent environment file (optional), same seam as the Linux side's
# boot-env: ~/.engram/boot-env, sourced first so the whole
# tmux -> agentctl -> claude chain inherits it.
BOOT_ENV_FILE="$HOME/.engram/boot-env"
if [[ -r "$BOOT_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$BOOT_ENV_FILE"
    set +a
fi

if [[ ! -x "$RUNNER" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: runner '$RUNNER' missing or not executable" >> "$LOG"
    exit 1
fi

# Session working directory: per-seat override at ~/.engram/boot-cwd (one
# absolute path), else this repo checkout if present, else $HOME. Same
# rationale as the Linux side: an unfamiliar cwd makes the platform CLI
# block on a first-run "trust this folder?" dialog before remote-control
# registration.
BOOT_CWD_FILE="$HOME/.engram/boot-cwd"
if [[ -d "$HOME/GitHub/engram-alpha" ]]; then
    cd_target="$HOME/GitHub/engram-alpha"
else
    cd_target="$HOME"
fi
if [[ -r "$BOOT_CWD_FILE" ]]; then
    cwd_candidate="$(head -n1 "$BOOT_CWD_FILE")"
    if [[ -d "$cwd_candidate" ]]; then
        cd_target="$cwd_candidate"
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: boot-cwd '$cwd_candidate' is not a directory; falling back" >> "$LOG"
    fi
fi

# launchd starts jobs with a minimal environment — the platform CLI is
# installed per-user at ~/.local/bin (and here, Homebrew's bin too, since
# this seat's PATH fix lives in ~/.zshrc, which launchd does not source).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

TMUX_SESSION="clio-wake"

# The quiet-skip case (see header comment): an already-live session is the
# expected common case at this cadence, not an anomaly. Seat-wide check
# (any `claude` process for this user), NOT just this launcher's own tmux
# session — see the header comment for why `pgrep` is not used here.
#
# ROUND 2 (Aleph's colleague review, PR-3162): the obvious `ps ... | grep
# -qx claude` has its own real bug under `set -o pipefail` (this script's
# own setting) — `grep -q` exits the instant it finds a match, closing its
# end of the pipe while `ps` may still be writing; `ps` then dies of
# SIGPIPE (exit 141), and pipefail reports THAT as the pipeline's status
# since grep's own 0 isn't the rightmost non-zero one to report — but there
# is no non-zero to report from grep, so bash reports ps's 141 instead,
# and `if` sees a nonzero/false exactly when the match was real and early
# in `ps`'s output. Measured directly: reproduces on very close to half of
# runs when the match is the first line of a large stream (own stress test:
# 25/50 falsely "not found"). `awk` reads its full input regardless of
# whether it already knows the answer, so it never triggers this race —
# and splitting on `/` and comparing the LAST field also correctly handles
# a `comm` value reported as a full path (e.g. `/usr/local/bin/claude`),
# not just a bare name.
if ps -u "$(id -u)" -o comm= 2>/dev/null | awk -F/ '$NF=="claude"{f=1} END{exit !f}'; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skip: a claude process is already live for this user" >> "$LOG"
    exit 0
fi

# Only reachable when no claude process is live at all — the pre-checks
# below (claude on PATH, agentctl's --charge-file support) are pure
# validation ahead of a cold launch, so running them only here (not on
# every skip-path tick) is free per the maintainer-seat fairy's suggestion.
if ! command -v claude >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: 'claude' not found on PATH ($PATH) — cannot start a session." >> "$LOG"
    exit 1
fi

agentctl_usage="$(agentctl session 2>&1 || true)"
if ! grep -q -- '--charge-file' <<<"$agentctl_usage"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: installed agentctl does not support --charge-file (stale install?) — refusing to launch." >> "$LOG"
    exit 1
fi

# COLLISION GUARD (maintainer-seat fairy's round-4 catch on PR-3162): the
# liveness check above proves no CLAUDE PROCESS is live anywhere for this
# user — it says nothing about whether a tmux session named "clio-wake"
# still exists. clio-wake-run.sh's own designed failure path holds a failed
# pane open with `sleep infinity`/`sleep 2147483647` after any agentctl
# failure specifically so the failure is inspectable — but that means a
# `clio-wake` tmux session CAN outlive the claude process that ran inside
# it. Reaching this point with that session still present means it is
# exactly that kind of held failure pane (never a live one — the ps check
# above already ruled that out) blocking `tmux new-session` from ever
# creating a fresh session under the same name. Reproduced live: one
# transient agentctl failure, left unaddressed, wedges every later 2h tick
# forever with the bare tmux error reaching only launchd's own catch-all
# log, never this structured one — precisely the dark-seat failure mode
# #3002 exists to close, this time caused by the mechanism's own failure
# handling. Surface it loudly and stop; do NOT kill the held pane
# automatically — an operator should look at why the earlier launch failed
# before anything retries into the same state.
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: tmux session '$TMUX_SESSION' already exists (a held failure pane from an earlier launch, since no claude process is live) — refusing to start a second one. Attach with: tmux attach -t $TMUX_SESSION" >> "$LOG"
    exit 1
fi

# B3 (req 4, Aleph's round-1 review): only reachable once the check above
# has confirmed NO claude process is live for this user AT ALL — not merely
# that this launcher's own tmux session is absent. That is what makes it
# safe to treat any loop-mode.json found here as orphaned rather than
# belonging to a session we simply couldn't see: engram-sleep's Step 3.5
# always removes the marker on a clean sleep, so its presence together with
# zero live claude processes means the session that owned it died mid-loop
# (a crash, an expired Remote Control login, a killed terminal) without
# cleaning up. Left in place, the plugin's engram-loop-arm-stop-hook.py
# would then BLOCK every turn of the fresh wake session on "arm now" —
# unconditionally, with no staleness check of its own — silently defeating
# this charge's own "hold, don't resume" instruction the moment it fired.
# Moving it aside (not deleting — an operator may want to inspect it) is
# the mechanical fix; prose in the charge cannot reach this, because the
# Stop hook fires before the charge's own next turn even begins.
LOOP_MARKER="$HOME/.engram/loop-mode.json"
if [[ -e "$LOOP_MARKER" ]]; then
    stale_path="${LOOP_MARKER}.stale-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$LOOP_MARKER" "$stale_path"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] moved stale loop marker aside: $stale_path (no live claude process owned it)" >> "$LOG"
fi

# No intermediate shell re-parse of the charge path — same argv-after-`--`
# pattern as the Linux launcher, for the same reason (the runner's own
# argument handling is the single place that interprets these values).
exec tmux new-session -d -s "$TMUX_SESSION" -- \
    "$RUNNER" "$CHARGE_DIR" "$cd_target"
