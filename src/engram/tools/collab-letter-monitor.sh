#!/usr/bin/env bash
# collab-letter-monitor.sh — persistent Monitor for new inter-agent letters
# addressed to this agent.
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   collab-letter-monitor.sh [--purpose <key>] [DIR]
#
# --purpose <key> (optional): purpose key for this instance (default: "default").
#   Self-reap is scoped to same-purpose instances only (#1183).
# DIR (optional): inter-agent directory to watch.
#   Resolution order: first positional arg > $INTER_AGENT_DIR > /home/agents-shared/inter-agent
#
# Multi-agent hosts may symlink this into /home/agents-shared/bin/ alongside
# ia/baton/forum (same convention; no install step creates the symlink — it is
# a manual host-operator action today).
#
# Discipline notes (load-bearing — do not remove):
#   - to-me filter (#630): emit only letters whose frontmatter to: line names
#     $SELF. Filters on recipient, not author — correct at 3+ agents where
#     author-filter would wake on ALL traffic between other agents.
#   - fail-loud on empty $SELF: an empty name silently inverts the filter
#     (matches every letter). Refuse to arm rather than mis-arm.
#   - never-clobber seen-set on transient empty ls (#743): gate the seen-set
#     update on a non-empty listing; a transient empty ls must preserve $SEEN.
#   - per-target seen-file (#743 via cross-instance clobber): the seen path is
#     keyed to the watched target so a second instance (smoke test, scratch
#     dir) can never overwrite a live monitor's baseline and flood it.
#   - own-write exclusion kept as belt-and-suspenders: a self-addressed letter
#     is the only case it changes, and skipping it is correct there too.

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>, DIR positional
# ---------------------------------------------------------------------------
PURPOSE="default"
_dir_arg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
collab-letter-monitor.sh — watch inter-agent letter dir for new letters to me

Usage: collab-letter-monitor.sh [--purpose <key>] [DIR]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Self-reap is scoped to same-purpose instances only, so two
                   instances launched for different purposes do not cross-kill.
  DIR              Path to the inter-agent directory to watch.
                   Default: $INTER_AGENT_DIR, then /home/agents-shared/inter-agent

Resolves SELF from ~/.engram/config.json agent_name. Exits with error if
agent_name is not set (to prevent filter-inversion mis-arm).

Emits one line per new letter whose frontmatter to: line names this agent.
Polls every 2s. Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        -*)
            echo "collab-letter-monitor.sh: unrecognised option: $1" >&2
            exit 1
            ;;
        *)
            _dir_arg="$1"
            shift
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex (#1307).
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "collab-letter-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""))')"
# Fail loud if agent_name is unset: with an empty $SELF the to-me filter
# matches any to: line — i.e. every letter — so you'd wake on ALL traffic,
# the exact noise this skill exists to avoid. Refuse to arm rather than mis-arm.
[ -z "$SELF" ] && { echo "collab-letter-monitor: agent_name not set in ~/.engram/config.json — refusing to arm" >&2; exit 1; }

# ---------------------------------------------------------------------------
# single-instance: last-arm-wins. A persistent Monitor's bash process outlives
# its arming session — it survives compaction and session-end. Without this, a
# fresh arm after a compaction runs ALONGSIDE the surviving orphan and BOTH poll
# the same dir, so every new letter fires the wake TWICE (double-delivery
# incident, 2026-06-13). Reap any prior instance of THIS monitor owned by THIS
# user, so the fresh arm cleanly takes over (and is the one that delivers to the
# current session). Scoped to our own uid (never touches another agent's
# monitor) and excludes our own pid and launching wrapper. NOTE: pgrep -f is a
# substring match, so a process merely *mentioning* this script name (e.g. an
# editor with the file open in a dev shell) could also match — harmless in
# production (agents don't edit a running monitor; SIGTERM + reseed-on-restart
# is recoverable). Set $COLLAB_MONITOR_NO_REAP to any non-empty value to disable.
# ---------------------------------------------------------------------------
if [ -z "${COLLAB_MONITOR_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped reap: only kill prior instances running with the SAME
    # --purpose key. For the "default" purpose (no --purpose arg passed), match
    # by script name only — identical to the pre-#1183 behaviour, ensuring
    # last-arm-wins for default instances. For an explicit non-default purpose
    # key, match by script name AND purpose so default instances (and instances
    # of a different non-default purpose) are never cross-killed (#1183).
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='collab-letter-monitor\.sh'
    else
        _pgrep_pattern="collab-letter-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        kill "$_pid" 2>/dev/null || true
    done
fi

# ---------------------------------------------------------------------------
# resolve watch directory
# ---------------------------------------------------------------------------
DIR="${_dir_arg:-${INTER_AGENT_DIR:-/home/agents-shared/inter-agent}}"

# ---------------------------------------------------------------------------
# seen-set — keyed to the watched DIR so concurrent instances never share state.
# A fixed path floods siblings (#743 class via cross-instance clobber): a smoke
# instance watching a scratch dir would overwrite the live monitor's baseline,
# whose next tick then emits the entire production history as "new".
# Override with $COLLAB_MONITOR_SEEN_FILE (tests).
# ---------------------------------------------------------------------------
SEEN="${COLLAB_MONITOR_SEEN_FILE:-$HOME/.engram/.collab-monitor-seen.$(printf '%s' "$DIR" | cksum | cut -d' ' -f1)}"

# Seed the seen-set with everything already present, so we emit only NEW arrivals.
ls "$DIR"/*.md 2>/dev/null | sort > "$SEEN"

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    ls "$DIR"/*.md 2>/dev/null | sort > "$SEEN.now"
    # Only diff+update on a non-empty listing — a transient empty `ls` must not
    # clobber $SEEN (the seed-clobber flood, #743). A genuinely empty dir simply
    # waits for the first letter.
    if [ -s "$SEEN.now" ]; then
        comm -13 "$SEEN" "$SEEN.now" | while read -r f; do
            # To-me filter (#630): emit only when the frontmatter to: line names me.
            # Word-boundary match tolerates multi-recipient lines ('to: a, b' — #631).
            # Own-write exclusion kept as belt-and-suspenders (a self-addressed letter
            # is the only case it changes, and skipping it is correct there too).
            case "$f" in *"_${SELF}.md") continue;; esac
            # Scan the full frontmatter block (between the two --- markers), not a
            # fixed line cap — a to: line below a fixed cap would be silently
            # invisible to the wake path as frontmatter grows.
            if awk '/^---$/{c++; next} c>=2{exit} c==1{print}' "$f" 2>/dev/null \
                 | grep -qiE "^to:.*\b${SELF}\b"; then
                echo "📬 new letter TO me: $(basename "$f")"
            fi
        done
        mv "$SEEN.now" "$SEEN"
    fi
    sleep 2
done
