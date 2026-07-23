#!/bin/bash
# agent-session-launch.sh — boot-time session launcher wrapper.
#
# Invoked by agent-session@.service's ExecStart. That unit runs with
# `User=agent-%i` — so this script is ALREADY running as the target agent's
# own Linux user, with no sudo available or needed. It:
#   1. reads the boot-charge template (per-agent override, else the shared
#      default),
#   2. starts a detached tmux session named `agent-<name>`,
#   3. inside it, execs `agentctl session <name> --as-self <charge>` — the
#      charge text becomes the platform CLI's initial prompt.
#
# The operator can `ssh` in (including over tailscale) and
# `tmux attach -t agent-<name>` later to observe/interact.
#
# Usage: agent-session-launch.sh <agent-name>
#
# Prompt-passing note (judgment call — see spec §Part B.1): the boot-charge
# content is passed to `agentctl session ... --as-self` as a single PLAIN
# (non-dash-leading) positional argument. agentctl's single-dash alias
# grammar (tools/agentctl header + _expand_session_args) only inspects args
# that themselves start with a single dash; the charge always starts with
# the literal "<boot-charge>" marker line, so it is never mistaken for a
# flag and passes through _expand_session_args verbatim and unexamined. No
# "--" separator is used between --as-self and the charge — introducing one
# would risk the platform CLI (claude/codex) treating "--" as an
# end-of-options marker rather than as part of the initial-prompt argument,
# which is exactly the ambiguity this note forecloses.
#
# tmux argv-passing note: the command + its arguments are passed to
# `tmux new-session` as SEPARATE argv words after a literal `--`, not as one
# joined string. tmux execs them directly with no shell re-parsing in this
# form, so charge content containing quotes, `$`, backticks, etc. is passed
# through byte-for-byte instead of being re-interpreted by an intermediate
# shell — verified empirically (tmux 3.4) before relying on it here.

set -euo pipefail

AGENT_NAME="${1:?Usage: agent-session-launch.sh <agent-name>}"

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

charge_content="$(cat "$charge_path")"

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

exec tmux new-session -d -s "$tmux_session" -- \
    agentctl session "$AGENT_NAME" --as-self "$charge_content"
