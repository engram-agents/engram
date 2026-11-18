#!/bin/bash
# agent-session-run.sh — runs INSIDE the detached tmux session started by
# agent-session-launch.sh. Launches the agent's platform session via
# agentctl and — the reason this script exists — makes an early death
# OBSERVABLE instead of evaporating with the pane.
#
# Background (2026-07-25 08:15 rehearsal): agentctl rejected the multi-line
# boot charge and exited 2 within milliseconds; the tmux session closed,
# taking the only copy of the error with it, while every systemd unit reported
# green. This runner preserves the launch corpse: it logs every launch/exit,
# tees platform stderr to the same log, and holds a failed pane open for
# autopsy rather than silently starting a replacement.
#
# The boot charge is resolved HERE, after agentctl detects the target
# platform as the agent itself. The launcher deliberately passes only the
# installed template directory: selecting a charge before this point would
# let a generic legacy override give a Codex session Claude-only guidance.
#
# Usage: agent-session-run.sh <agent-name> <charge-dir> <cwd>

set -uo pipefail   # NO -e: we must reach the exit-code handling below.

AGENT_NAME="${1:?Usage: agent-session-run.sh <agent-name> <charge-dir> <cwd>}"
CHARGE_DIR="${2:?missing <charge-dir>}"
CD_TARGET="${3:-$HOME}"

# Defensive PATH prepend for standalone runs; under the normal chain the
# launcher already exported this before starting the tmux server.
export PATH="$HOME/.local/bin:$PATH"

LOG_DIR="$HOME/.engram/logs"
LOG="$LOG_DIR/autostart-launch.log"
mkdir -p "$LOG_DIR"

_log_warn() {
    local message="$1"
    echo "[$(date -Is)] [$AGENT_NAME] WARNING: $message" >> "$LOG"
    echo "[autostart] WARNING: $message" >&2
}

# Choose a complete, harness-valid boot charge only after platform detection.
# Explicit per-harness overrides are user-owned and therefore never rewritten:
#   ~/.engram/boot-charge-claude.md
#   ~/.engram/boot-charge-codex.md
# The old generic boot-charge.md is Claude compatibility only.  It has no
# reliable harness declaration, so applying it to Codex could silently revive
# the precise cross-harness failure this resolver prevents.
_resolve_charge() {
    local platform="$1"
    local profile="default"
    local profile_file="$HOME/.engram/boot-charge-profile"
    local explicit_override="$HOME/.engram/boot-charge-${platform}.md"
    local legacy_override="$HOME/.engram/boot-charge.md"
    local base component rendered line inserted=0

    if [[ -r "$profile_file" ]]; then
        profile="$(head -n1 "$profile_file" | tr -d '[:space:]')"
        case "$profile" in
            default|quiet) ;;
            *)
                _log_warn "invalid boot-charge profile '$profile' in '$profile_file'; using default"
                profile="default"
                ;;
        esac
    fi

    if [[ -r "$explicit_override" ]]; then
        printf '%s\n' "$explicit_override"
        return 0
    fi

    if [[ "$platform" == "claude" && -r "$legacy_override" ]]; then
        printf '%s\n' "$legacy_override"
        return 0
    fi

    if [[ "$platform" == "codex" && -r "$legacy_override" ]]; then
        _log_warn "ignoring legacy generic boot charge '$legacy_override' for Codex; use '$HOME/.engram/boot-charge-codex.md' for an explicit Codex override"
    fi

    base="$CHARGE_DIR/boot-charge-${profile}.md"
    component="$CHARGE_DIR/boot-charge-${profile}-${platform}.md"
    # Codex policy is intentionally shared by profiles: only the common
    # default/quiet framing differs, while its harness actions must remain
    # single-sourced.
    if [[ "$platform" == "codex" ]]; then
        component="$CHARGE_DIR/boot-charge-codex.md"
    fi

    if [[ ! -r "$base" || ! -s "$base" || ! -r "$component" || ! -s "$component" ]]; then
        echo "ERROR: no readable boot-charge template for platform '$platform' profile '$profile' under '$CHARGE_DIR'" >&2
        return 1
    fi

    mkdir -p "$HOME/.engram"
    rendered="$(mktemp "$HOME/.engram/boot-charge-${platform}.XXXXXX.md")" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == '{{HARNESS_BOOT_INSTRUCTIONS}}' ]]; then
            cat "$component" >> "$rendered"
            inserted=1
        else
            printf '%s\n' "$line" >> "$rendered"
        fi
    done < "$base"
    if [[ "$inserted" -ne 1 ]]; then
        rm -f "$rendered"
        echo "ERROR: boot-charge template '$base' is missing the harness instruction placeholder" >&2
        return 1
    fi
    printf '%s\n' "$rendered"
}

# gh-stack ensure (#2391): the stacked-PR merge verb must be discoverable via
# `gh --help` on every seat, and the extension is per-user — a seat without it
# cannot merge a stack member at all. Idempotent (list-then-install), quiet,
# and NON-FATAL by design: a seat with no network or no gh still boots, and
# the CLAUDE.md rule section remains the knowledge carrier either way.
if command -v gh >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1 \
        && ! timeout 10 gh extension list 2>/dev/null | grep -q 'github/gh-stack'; then
    echo "[$(date -Is)] [$AGENT_NAME] gh-stack extension absent — installing (30s cap)" >> "$LOG"
    timeout 30 gh extension install github/gh-stack >>"$LOG" 2>&1 \
        || echo "[$(date -Is)] [$AGENT_NAME] WARN: gh-stack extension install failed or timed out (non-fatal; see CLAUDE.md 'Stacked PRs')" >> "$LOG"
fi

platform="$(agentctl __detect-platform-as-self)" || {
    echo "[$(date -Is)] [$AGENT_NAME] ERROR: could not detect platform for boot session" >> "$LOG"
    echo "[autostart] could not detect the agent platform; refusing to guess boot arguments" >&2
    exit 1
}

# -r asks Claude to register remote control. Codex has one daemon-backed TUI
# in this pane instead; agentctl accepts and silently consumes -r there. Keep
# the Claude boot contract while omitting the unnecessary Codex alias.
case "$platform" in
    claude) session_aliases=(-d -r) ;;
    codex) session_aliases=(-d) ;;
    *)
        echo "[$(date -Is)] [$AGENT_NAME] ERROR: unsupported boot platform '$platform'" >> "$LOG"
        echo "[autostart] unsupported agent platform '$platform'; refusing to guess boot arguments" >&2
        exit 1
        ;;
esac

# Model-family pin (public issue 20). A seat that boots on another model family
# than its recorded self_lineage is stopped by the model gate, so the launcher
# passes the FAMILY word (opus, sonnet, haiku, fable; never a model id) derived
# from ~/.engram/config.json self_lineage. Claude only; every path that cannot
# decide starts the seat as it always did and SAYS so in the log and on the pane.
# Order matters:
#   (A)  any non-empty ANTHROPIC_DEFAULT_*_MODEL in this environment (boot-env
#        files are sourced before this runs): pass nothing, stop. A seat that
#        remaps models through the environment must not get a literal family.
#   (A2) the same through the settings files: an `env` block (user, then project,
#        then local; later wins per key; a non-string or empty value counts as
#        unset) that sets a non-empty ANTHROPIC_DEFAULT_*_MODEL: pass nothing,
#        one INFO line, whatever `model` says.
#   (A') lineage provider other than anthropic: pass nothing, one INFO line.
#        Unset or unusable lineage, or an unknown anthropic family: WARNING.
#   (B)  anthropic only: read `model` from the settings the harness merges
#        (user, then project, then local; later wins; managed settings and
#        CLAUDE_CONFIG_DIR are NOT read; neither is the ANTHROPIC_MODEL variable
#        nor any other key of a settings `env` block), drop a trailing [...] suffix. Same
#        family: pass nothing (keeps a [1m] variant). Absent: pass the family.
#        Different family or an unrecognised value: pass the lineage family and
#        WARN. An alias that only STARTS with a family word (for example `opusplan`)
#        is an unrecognised value on purpose: the lineage family is passed with a
#        WARNING (a decision, pinned by test_unrecognised_settings_value_passes_lineage_flag_and_warns).
#        Settings that cannot be read: pass nothing and WARN; no guess.
# Settings are never edited. The ANTHROPIC_DEFAULT_*_MODEL variables are only READ (rule A); the launcher
# never sets, unsets or overrides them, nor ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY.
_log_info() {
    echo "[$(date -Is)] [$AGENT_NAME] INFO: $1" >> "$LOG"
    echo "[autostart] INFO: $1" >&2
}

_resolve_model_pin() {
    local var kind text end_seen=0
    model_flag=()
    for var in $(compgen -e | grep -E '^ANTHROPIC_DEFAULT_.*_MODEL$'); do
        if [[ -n "${!var}" ]]; then
            _log_info "model pin: $var is set in the environment; passing no --model"
            return 0
        fi
    done
    if ! command -v python3 >/dev/null 2>&1; then
        _log_warn "model pin: python3 not found; cannot read self_lineage, starting as before"
        return 0
    fi
    while IFS=$'\t' read -r kind text; do
        case "$kind" in
            FLAG) model_flag=(--model "$text") ;;
            INFO) _log_info "$text" ;;
            WARN) _log_warn "$text" ;;
            END) end_seen=1 ;;
        esac
    done < <(python3 -I - "$HOME" "$CD_TARGET" <<'PY'
import atexit, json, re, sys
from pathlib import Path

home, cwd = Path(sys.argv[1]), Path(sys.argv[2])
FAMILIES = ("opus", "sonnet", "haiku", "fable")


def out(kind, text=""):
    print(f"{kind}\t{text}")


# the last line on every path (normal end, sys.exit, after the excepthook)
atexit.register(out, "END")


def _internal_error(exc_type, exc, tb):
    # Any failure the code below did not foresee must still be said in the log.
    out("WARN", f"model pin: internal error {exc_type.__name__}: {exc}; passing no --model")


sys.excepthook = _internal_error


try:
    lineage = json.loads((home / ".engram" / "config.json").read_text(encoding="utf-8")).get("self_lineage")
except (OSError, ValueError, AttributeError):
    lineage = None
parts = lineage.split(":") if isinstance(lineage, str) else []
if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
    out("WARN", f"model pin: self_lineage is unset or has no provider:family form ({lineage!r}); starting as before, no --model passed")
    sys.exit(0)
provider, family = parts[0].strip().lower(), parts[1].strip().lower()
if provider != "anthropic":
    out("INFO", f"model pin: self_lineage {lineage!r} names provider {provider!r}, not anthropic; passing no --model")
    sys.exit(0)
if family not in FAMILIES:
    out("WARN", f"model pin: self_lineage family {family!r} is not one of {', '.join(FAMILIES)}; starting as before, no --model passed")
    sys.exit(0)

value = None
remap = {}
for path in (home / ".claude" / "settings.json", cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        continue
    except (OSError, ValueError):
        out("WARN", f"model pin: cannot read settings file {path}; not guessing the merged model, passing no --model")
        sys.exit(0)
    env = data.get("env")
    if isinstance(env, dict):
        for key, val in env.items():
            if isinstance(key, str) and re.fullmatch(r"ANTHROPIC_DEFAULT_[A-Z0-9_]*_MODEL", key):
                remap[key] = val if isinstance(val, str) and val else None
    if "model" in data and data["model"] not in (None, ""):
        value = data["model"]

set_names = sorted(k for k, v in remap.items() if v)
if set_names:
    out("INFO", f"model pin: settings env sets {set_names[0]}; passing no --model")
    sys.exit(0)

if value is None:
    out("FLAG", family)
    sys.exit(0)
norm = re.sub(r"\[[^\]]*\]$", "", str(value)).strip().lower()
m = re.match(r"^(?:claude-)?(opus|sonnet|haiku|fable)(?![a-z])", norm)
if not m:
    out("WARN", f"model pin: settings model {value!r} names no known family; passing --model {family} from self_lineage")
    out("FLAG", family)
elif m.group(1) == family:
    pass
else:
    out("WARN", f"model pin: settings model {value!r} ({m.group(1)}) disagrees with self_lineage 'anthropic:{family}'; passing --model {family}")
    out("FLAG", family)
PY
)
    # No END record means python3 died or was cut short: never trust a partial result.
    if [[ "$end_seen" -ne 1 ]]; then
        model_flag=()
        _log_warn "model pin: python3 produced no end record; starting as before, no --model passed"
    fi
}

model_flag=()
if [[ "$platform" == "claude" ]]; then
    _resolve_model_pin
fi

CHARGE_FILE="$(_resolve_charge "$platform")" || exit 1
# A generated file is internal runtime state; preserve explicit user-owned
# overrides untouched. agentctl consumes --charge-file synchronously, so it
# is safe to remove the generated file as soon as the session exits.
if [[ "$CHARGE_FILE" == "$HOME/.engram/boot-charge-${platform}."*.md ]]; then
    trap 'rm -f "$CHARGE_FILE"' EXIT
fi
if [[ ! -r "$CHARGE_FILE" || ! -s "$CHARGE_FILE" ]]; then
    echo "ERROR: boot-charge template '$CHARGE_FILE' is unreadable or empty" >&2
    exit 1
fi

echo "[$(date -Is)] [$AGENT_NAME] launching: agentctl session $AGENT_NAME ${session_aliases[*]} --as-self ${model_flag[*]:+${model_flag[*]} }--cd $CD_TARGET --charge-file $CHARGE_FILE" >> "$LOG"

# stderr rides through tee into the log AND stays on the pane. stdout is left
# alone — the platform CLI's interactive UI owns it.
agentctl session "$AGENT_NAME" "${session_aliases[@]}" --as-self \
    ${model_flag[@]+"${model_flag[@]}"} \
    --cd "$CD_TARGET" --charge-file "$CHARGE_FILE" \
    2> >(tee -a "$LOG" >&2)
ec=$?

echo "[$(date -Is)] [$AGENT_NAME] session process exited ec=$ec" >> "$LOG"

if [[ $ec -ne 0 ]]; then
    echo ""
    echo "[autostart] LAUNCH FAILED (exit $ec) — full log: $LOG"
    echo "[autostart] holding this pane open for inspection; kill it with:"
    echo "            tmux kill-session -t agent-$AGENT_NAME"
    sleep infinity
fi

exit "$ec"
