#!/bin/bash
# agent-session-launch.sh — boot-time session launcher wrapper.
#
# Invoked by agent-session@.service's ExecStart. That unit runs with
# `User=agent-%i` — so this script is ALREADY running as the target agent's
# own Linux user, with no sudo available or needed. It:
#   1. resolves the boot-charge template (per-agent override, else the shared
#      default) and the session working directory,
#   2. starts a detached tmux session named `agent-<name>`,
#   3. inside it, runs agent-session-run.sh, which launches
#      `agentctl session <name> -dr --as-self --charge-file <charge>` and
#      leaves a breadcrumb log + holds the pane if the launch dies.
#
# The operator can `ssh` in (including over tailscale) and
# `tmux attach -t agent-<name>` later to observe/interact.
#
# Usage: agent-session-launch.sh <agent-name>
#
# Charge-passing note (REVISED 2026-07-25 — the previous design note here was
# falsified live): the charge is now passed BY PATH via agentctl's
# --charge-file flag, never inline. The old inline-positional design assumed
# the charge "passes through _expand_session_args verbatim and unexamined";
# in practice that function's one-word-per-line transport rejects any
# newline-containing arg ("session args may not be empty or contain
# newlines", exit 2), so the multi-line boot charge killed every launch at
# the 2026-07-25 08:15 rehearsal — inside tmux, invisibly, with all five
# units green. --charge-file reads the content inside cmd_session AFTER
# alias expansion and appends it directly to the platform argv, so the
# transport never sees it and no quoting layer can corrupt it.
#
# Observability note (same incident): the gap between "unit finished green"
# and "claude is alive" is this chain's recurring silent-failure surface
# (07-22 timer Unit=, 07-24 PATH, 07-25 newline guard — three rehearsals,
# each dying one layer deeper, each invisibly). The in-tmux runner script
# now writes ~/.engram/logs/autostart-launch.log and holds a failed pane
# open, so the NEXT layer-N failure leaves a corpse we can autopsy.

set -euo pipefail

AGENT_NAME="${1:?Usage: agent-session-launch.sh <agent-name>}"

RUNNER="/usr/local/lib/agentctl/autostart/agent-session-run.sh"

# Per-agent environment file (optional): ~/.engram/boot-env — sourced FIRST,
# before the PATH prepend and the CLI pre-checks, so everything below (and the
# whole tmux-inherited chain: agentctl → claude) sees the agent's own env.
# Why: the systemd unit starts with a clean environment and nothing in this
# chain reads ~/.bash_profile, so an agent whose platform CLI depends on
# profile-set variables (found live at the 2026-07-25 rehearsal: zephyr's
# claude runs against a non-Anthropic backend via profile-set ANTHROPIC_* /
# Ollama variables, and booted into "not logged in" without them) needs a
# boot-visible home for those exports. Same per-agent override seam as
# boot-charge.md / boot-cwd: the agent (or the operator on their behalf)
# drops plain `VAR=value` / `export VAR=value` lines in, no root needed.
# `set -a` auto-exports plain assignments; a sourcing failure aborts the
# unit LOUDLY (set -e) rather than launching a half-configured session.
BOOT_ENV_FILE="$HOME/.engram/boot-env"
if [[ -r "$BOOT_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$BOOT_ENV_FILE"
    set +a
fi

# Per-agent override, else the shared default template. Judgment call: the
# default lives at a fixed system-wide install path (mirrors agentctl's own
# /usr/local/sbin/agent-bootstrap convention) rather than inside whichever
# engram-alpha checkout happened to be used to install the units — see
# deploy/autostart/README.md's install step for the corresponding `cp`.
BOOT_CHARGE_OVERRIDE="$HOME/.engram/boot-charge.md"
BOOT_CHARGE_DEFAULT="/usr/local/lib/agentctl/autostart/boot-charge-default.md"

if [[ -r "$BOOT_CHARGE_OVERRIDE" ]]; then
    charge_path="$BOOT_CHARGE_OVERRIDE"
else
    charge_path="$BOOT_CHARGE_DEFAULT"
fi

if [[ ! -r "$charge_path" ]]; then
    echo "ERROR: no readable boot-charge template at '$charge_path' (checked override '$BOOT_CHARGE_OVERRIDE' first)" >&2
    exit 1
fi

if [[ ! -s "$charge_path" ]]; then
    echo "ERROR: boot-charge template '$charge_path' is empty" >&2
    exit 1
fi

if [[ ! -x "$RUNNER" ]]; then
    echo "ERROR: in-tmux runner '$RUNNER' missing or not executable (install step skipped?)" >&2
    exit 1
fi

# Session working directory: per-agent override at ~/.engram/boot-cwd (a file
# containing one absolute path), else ~/umwelt when it exists (the operator
# starts every agent's manual session there — `agentctl session <name> -dr
# --cd /home/agent-<name>/umwelt/` — so it's the already-TRUSTED project dir;
# an unfamiliar cwd makes the platform CLI block on its first-run
# "trust this folder?" dialog before remote-control registration, found live
# at the 2026-07-25 Sol test), else $HOME. Without an explicit --cd the
# session would inherit the systemd unit's cwd — / — and the platform CLI
# would file its transcript under a "/" project.
BOOT_CWD_FILE="$HOME/.engram/boot-cwd"
if [[ -d "$HOME/umwelt" ]]; then
    cd_target="$HOME/umwelt"
else
    cd_target="$HOME"
fi
if [[ -r "$BOOT_CWD_FILE" ]]; then
    cwd_candidate="$(head -n1 "$BOOT_CWD_FILE")"
    if [[ -d "$cwd_candidate" ]]; then
        cd_target="$cwd_candidate"
    else
        echo "WARNING: boot-cwd '$cwd_candidate' is not a directory; falling back to \$HOME" >&2
    fi
fi

# The systemd unit environment carries only the default system PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:...), but the platform
# CLI is installed per-user at ~/.local/bin. Found live at the 2026-07-24
# 08:15 rehearsal: every unit "Finished" green while the tmux-spawned
# `agentctl session` died instantly on `env: 'claude': No such file or
# directory`. tmux inherits this environment into the server it starts, so
# one prepend here covers the whole chain.
export PATH="$HOME/.local/bin:$PATH"

# Fail LOUDLY at the unit level if no platform CLI is resolvable — without
# this check the unit reports success and the failure is invisible until
# someone notices the missing session (the 07-24 silent-failure mode).
if ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1; then
    echo "ERROR: neither 'claude' nor 'codex' found on PATH ($PATH) — cannot start a session." >&2
    exit 1
fi

# Pre-check the exact agentctl contract the runner will use, while we can
# still fail at the UNIT level (visible in systemctl/journal). A stale
# installed agentctl without --charge-file would otherwise die inside tmux
# — the exact invisible failure mode this revision exists to kill.
# (Capture-then-grep, not a pipeline: the usage text exits nonzero, and under
# pipefail that would poison the pipeline even when grep matches.)
agentctl_usage="$(agentctl session 2>&1 || true)"
if ! grep -q -- '--charge-file' <<<"$agentctl_usage"; then
    echo "ERROR: installed agentctl does not support --charge-file (stale install?) — refusing to launch into a known-dead path." >&2
    exit 1
fi

tmux_session="agent-${AGENT_NAME}"

# Refuse to double-launch if a session with this name is already up (e.g. a
# manual `agentctl session` from earlier that morning) — a crashed morning
# session should be VISIBLE (see the unit's Restart=no comment), not silently
# stacked under a second tmux session of the same name.
if tmux has-session -t "$tmux_session" 2>/dev/null; then
    echo "ERROR: tmux session '$tmux_session' already exists — refusing to start a second one." >&2
    echo "       Attach with: tmux attach -t $tmux_session" >&2
    exit 1
fi

# All arguments are simple paths/names now (the multi-line charge stays in
# its file), so the tmux argv-passing subtleties of the previous revision no
# longer apply — but we still pass argv words after a literal `--` so tmux
# execs the runner directly with no intermediate shell re-parse.
exec tmux new-session -d -s "$tmux_session" -- \
    "$RUNNER" "$AGENT_NAME" "$charge_path" "$cd_target"
