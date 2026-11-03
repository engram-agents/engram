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
#      `agentctl session <name> -d [-r for Claude] --as-self --charge-file
#      <charge>` and
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
#
# A HOST-COMMON file (/home/agents-shared/autostart/boot-env.common, override
# via AGENT_BOOT_ENV_COMMON in the inherited environment) is sourced
# immediately BEFORE the per-agent file, so one file can carry settings common
# to every seat on the host while a seat's own boot-env still wins. Absent is
# normal and silent; present-but-unreadable is ONE stderr WARNING and the
# launch continues (one permission slip on a shared file must not stop every
# seat from booting, but must not be silent either); a sourcing failure
# aborts exactly like a broken per-agent file.
BOOT_ENV_COMMON_FILE="${AGENT_BOOT_ENV_COMMON:-/home/agents-shared/autostart/boot-env.common}"
if [[ -e "$BOOT_ENV_COMMON_FILE" ]]; then
    if [[ -r "$BOOT_ENV_COMMON_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$BOOT_ENV_COMMON_FILE"
        set +a
    else
        echo "WARNING: host-common boot-env '$BOOT_ENV_COMMON_FILE' exists but is not readable; launching without it" >&2
    fi
fi
BOOT_ENV_FILE="$HOME/.engram/boot-env"
if [[ -r "$BOOT_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$BOOT_ENV_FILE"
    set +a
fi

# User-session runtime environment (#4328). The systemd unit starts with a clean
# environment (same mechanism as the PATH prepend below) and tmux inherits THIS
# script's environment, so every session in the chain started without
# XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS, and `systemctl --user` failed
# with "Failed to connect to bus: No medium found" in every tool shell although
# the user bus socket /run/user/<uid>/bus exists and works. Fill both here, AFTER
# both boot-env files, so anything a seat or the host set (non-empty) always wins
# and is never overridden; an EMPTY value counts as unset. The DBUS address is
# derived from the LIVE XDG_RUNTIME_DIR (whatever it is by now), not the default.
# A missing piece leaves the variable unset and prints ONE stderr WARNING; it
# never aborts the launch. AGENT_RUN_USER_BASE (default /run/user) is a test seam.
_run_uid="$(id -u 2>/dev/null || true)"
_run_dir="${AGENT_RUN_USER_BASE:-/run/user}/$_run_uid"
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    _run_owner=""
    if [[ -n "$_run_uid" && -d "$_run_dir" ]]; then
        _run_owner="$(stat -c %u "$_run_dir" 2>/dev/null || true)"
    fi
    if [[ -z "$_run_uid" ]]; then
        echo "WARNING: autostart session env: XDG_RUNTIME_DIR not set: could not determine the uid ('id -u' failed); 'systemctl --user' will not work in the session" >&2
    elif [[ ! -d "$_run_dir" ]]; then
        echo "WARNING: autostart session env: XDG_RUNTIME_DIR not set: '$_run_dir' is not a directory; 'systemctl --user' will not work in the session" >&2
    elif [[ "$_run_owner" != "$_run_uid" ]]; then
        echo "WARNING: autostart session env: XDG_RUNTIME_DIR not set: '$_run_dir' is owned by uid '${_run_owner:-unknown}', not uid '$_run_uid'" >&2
    else
        export XDG_RUNTIME_DIR="$_run_dir"
    fi
fi
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -n "${XDG_RUNTIME_DIR:-}" ]]; then
    if [[ -S "$XDG_RUNTIME_DIR/bus" ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    else
        echo "WARNING: autostart session env: DBUS_SESSION_BUS_ADDRESS not set: '$XDG_RUNTIME_DIR/bus' is not a socket; 'systemctl --user' will not work in the session" >&2
    fi
fi
unset _run_uid _run_dir _run_owner

# The runner chooses the charge AFTER agentctl detects the platform as this
# agent. Passing only the installed directory is intentional: a generic
# legacy override cannot be allowed to select Claude-only instructions before
# we know this is a Claude session.
BOOT_CHARGE_DIR="/usr/local/lib/agentctl/autostart"

if [[ ! -x "$RUNNER" ]]; then
    echo "ERROR: in-tmux runner '$RUNNER' missing or not executable (install step skipped?)" >&2
    exit 1
fi

# The PATH prepend comes BEFORE the cwd selection (it used to follow it): the
# trust check below needs python3, and selection and preflight must see the
# same PATH so they cannot disagree.
# The systemd unit environment carries only the default system PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:...), but the platform
# CLI is installed per-user at ~/.local/bin. Found live at the 2026-07-24
# 08:15 rehearsal: every unit "Finished" green while the tmux-spawned
# `agentctl session` died instantly on `env: 'claude': No such file or
# directory`. tmux inherits this environment into the server it starts, so
# one prepend here covers the whole chain.
export PATH="$HOME/.local/bin:$PATH"

# Trust check (#4170), ONE function shared by the cwd selection and the
# preflight so they can never disagree. Claude Code records trust per absolute
# path in ~/.claude.json -> projects.<path>.hasTrustDialogAccepted. READ-ONLY:
# the file belongs to Claude Code and this script never writes or creates it.
# ABSENT, UNREADABLE or UNPARSEABLE reads as UNTRUSTED, never as fine. Exact-path
# match only (no realpath/parent-directory inference): when unsure, say untrusted.
# CLAUDE_CONFIG_DIR (checked AFTER boot-env is sourced) fails CLOSED: if set and
# non-empty, Claude Code may keep its config elsewhere, so $HOME/.claude.json
# proves nothing; we do not guess the layout of $CLAUDE_CONFIG_DIR/.claude.json
# (a guess would be a new silent-pass route).
# Returns: 0 trusted; 1 untrusted; 2 unverifiable (CLAUDE_CONFIG_DIR set);
#          3 unverifiable (python3 missing). For SELECTION 2 and 3 count as
# untrusted, so the choice falls through to the plain order.
cwd_trust_status() {
    if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
        return 2
    elif ! command -v python3 >/dev/null 2>&1; then
        return 3
    fi
    python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    ok = data["projects"][sys.argv[2]]["hasTrustDialogAccepted"] is True
except Exception:
    ok = False
sys.exit(0 if ok else 1)
' "$HOME/.claude.json" "$1" 2>/dev/null
}

# Session working directory, resolved AT LAUNCH TIME from $HOME (nothing is
# written at rename time, no absolute path is persisted). Selection:
#   1. an explicit first line of ~/.engram/boot-cwd (normalised), if it names an
#      existing directory, WINS EVEN IF UNTRUSTED (still launches, with the
#      preflight ERROR below);
#   2. else the FIRST EXISTING directory among $HOME/engram-alpha, $HOME/umwelt,
#      $HOME that is TRUSTED (cwd_trust_status == 0);
#   3. else none is trusted: the first existing one in that same order, and the
#      preflight below prints its ERROR.
# When trust is unverifiable (CLAUDE_CONFIG_DIR set, python3 missing) every
# candidate counts as untrusted, so rule 3 applies: engram-alpha > umwelt >
# $HOME, with the existing ERROR wording for those causes.
# Rationale: a seat with both ~/engram-alpha and ~/umwelt where only ~/umwelt is
# trusted used to boot cleanly; plain order would stall it on the dialog.
# Without an explicit --cd the session would inherit the systemd unit's cwd —
# / — and the platform CLI would file its transcript under a "/" project.
# An unfamiliar cwd also makes Claude Code block on its first-run "trust this
# folder?" dialog, which nobody sees at boot (#4170: ~3 h stall on a freshly
# renamed seat that booted in $HOME) — hence the trust preflight further down.
BOOT_CWD_FILE="$HOME/.engram/boot-cwd"
cd_target=""
cd_arm=""
boot_cwd_bad=""
if [[ -r "$BOOT_CWD_FILE" ]]; then
    cwd_candidate="$(head -n1 "$BOOT_CWD_FILE")"
    # Normalise before the -d test (hand-edited / CRLF files): strip a trailing
    # CR, leading/trailing whitespace, and ONE trailing "/" unless the whole
    # value is "/". Relative paths are deliberately left as they are.
    cwd_candidate="${cwd_candidate%$'\r'}"
    cwd_candidate="${cwd_candidate#"${cwd_candidate%%[![:space:]]*}"}"
    cwd_candidate="${cwd_candidate%"${cwd_candidate##*[![:space:]]}"}"
    if [[ "$cwd_candidate" != "/" && "$cwd_candidate" == */ ]]; then
        cwd_candidate="${cwd_candidate%/}"
    fi
    if [[ -d "$cwd_candidate" ]]; then
        cd_target="$cwd_candidate"
        cd_arm='boot-cwd'
    else
        boot_cwd_bad="$cwd_candidate"
    fi
fi
if [[ -z "$cd_target" ]]; then
    first_existing=""
    first_existing_arm=""
    for cand_arm in '$HOME/engram-alpha' '$HOME/umwelt' '$HOME'; do
        cand="$HOME${cand_arm#\$HOME}"   # not ${x/#pat/$HOME}: bash patsub_replacement turns "&" in $HOME into the match
        [[ -d "$cand" ]] || continue
        if [[ -z "$first_existing" ]]; then
            first_existing="$cand"
            first_existing_arm="$cand_arm"
        fi
        rc=0
        cwd_trust_status "$cand" || rc=$?
        if [[ "$rc" -eq 0 ]]; then
            cd_target="$cand"
            cd_arm="$cand_arm, first trusted"
            break
        fi
    done
    if [[ -z "$cd_target" ]]; then
        cd_target="${first_existing:-$HOME}"
        cd_arm="${first_existing_arm:-\$HOME}, none trusted"
    fi
fi
if [[ -n "$boot_cwd_bad" ]]; then
    echo "WARNING: boot-cwd '$boot_cwd_bad' is not a directory; falling back to '$cd_target'" >&2
fi
echo "boot cwd: $cd_target (from $cd_arm)" >&2

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

# Trust preflight (#4170). If the chosen cwd is not trusted (see
# cwd_trust_status above), the session will stall unseen on the trust dialog.
# This is a LOUD WARNING, not a refusal: the launch proceeds so the human has a
# session to attach to and accept the dialog.
rc=0
cwd_trust_status "$cd_target" || rc=$?
case "$rc" in
    0) ;;
    2) echo "ERROR: CLAUDE_CONFIG_DIR is set ('$CLAUDE_CONFIG_DIR'), so trust for '$cd_target' cannot be verified from ~/.claude.json (treated as UNTRUSTED). If the session stalls, attach once: tmux attach -t $tmux_session, and accept the trust dialog." >&2 ;;
    3) echo "ERROR: python3 not found on PATH, so trust for '$cd_target' cannot be verified (treated as UNTRUSTED). If the session stalls, attach once: tmux attach -t $tmux_session, and accept the trust dialog." >&2 ;;
    *) echo "ERROR: Claude Code has not trusted '$cd_target' (no hasTrustDialogAccepted=true in ~/.claude.json); the session will stall on the trust dialog. One-time fix: tmux attach -t $tmux_session, and accept the dialog." >&2 ;;
esac

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
    "$RUNNER" "$AGENT_NAME" "$BOOT_CHARGE_DIR" "$cd_target"
