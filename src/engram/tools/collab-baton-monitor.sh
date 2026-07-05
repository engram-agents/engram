#!/usr/bin/env bash
# collab-baton-monitor.sh — persistent Monitor for baton flips addressed to this agent.
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   collab-baton-monitor.sh [--purpose <key>] [--once] [DIR]
#
# --purpose <key> (optional): purpose key for this instance (default: "default").
#   Self-reap is scoped to same-purpose instances only.
# --once (optional): run one poll iteration and exit (testing / one-shot use).
# DIR (optional): baton directory to watch.
#   Resolution order: first positional arg > $BATON_DIR > /home/agents-shared/projects
#
# Discipline notes (load-bearing — do not remove):
#   - to-me filter: emit only when the LATEST turn-log entry is "FROM → SELF: reason"
#     with FROM != SELF and FROM != "initialized". This catches genuine flips TO this
#     agent by someone else. It does NOT wake on: flips to other agents, genuine
#     self-flips (borges → borges), or self-inits (initialized → borges on creation).
#   - fail-loud on empty $SELF (#630 equivalent): an empty name would match every
#     agent's baton — refuse to arm rather than mis-arm.
#   - never-clobber seen-set on transient empty listing (#743): gate the seen-set
#     update on a non-empty _compute_current_keys output; a transient failure or a
#     moment with no batons turned to me must not wipe the known-flips set.
#   - per-target seen-file: the seen path is keyed to the watched BATON_DIR so a
#     second instance watching a scratch dir can never overwrite a live monitor's set.
#   - single-instance self-reap: last-arm-wins (same as collab-letter-monitor).
#   - de-dup key: "basename:turn_since" — a file's turn_since changes with every
#     flip, so (a) re-polling the same flip does NOT re-emit, and (b) a later flip
#     of the same baton (new turn_since) DOES emit once.
#   - --once mode skips startup seeding and the engaged-window check; the caller
#     controls the seen-file content directly (for test isolation).

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>, --once, DIR positional
# ---------------------------------------------------------------------------
PURPOSE="default"
_ONCE=false
_dir_arg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
collab-baton-monitor.sh — watch baton dir for flips to this agent

Usage: collab-baton-monitor.sh [--purpose <key>] [--once] [DIR]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Self-reap is scoped to same-purpose instances only, so two
                   instances launched for different purposes do not cross-kill.
  --once           Run one poll iteration and exit (no sleep, no seeding).
                   Intended for testing and one-shot checks.
  DIR              Path to the baton directory to watch.
                   Default: $BATON_DIR, then /home/agents-shared/projects

Resolves SELF from ~/.engram/config.json agent_name. Exits with error if
agent_name is not set (to prevent filter-inversion mis-arm).

Emits one line per new baton flipped to this agent by someone else.
Format: "📌 baton flipped to you: <name> by <from> — <reason>"
Polls every 2s. Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        --once)
            _ONCE=true
            shift
            ;;
        -*)
            echo "collab-baton-monitor.sh: unrecognised option: $1" >&2
            exit 1
            ;;
        *)
            _dir_arg="$1"
            shift
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex.
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "collab-baton-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""))')"
# Fail loud if agent_name is unset: with an empty $SELF the to-me filter
# matches every agent's baton — refuse to arm rather than mis-arm.
[ -z "$SELF" ] && { echo "collab-baton-monitor: agent_name not set in ~/.engram/config.json — refusing to arm" >&2; exit 1; }

# ---------------------------------------------------------------------------
# single-instance: last-arm-wins (same pattern as collab-letter-monitor).
# A persistent Monitor's bash process outlives its arming session and survives
# compaction. Without this, a fresh arm runs alongside the surviving orphan and
# fires every flip twice. Skipped in --once mode (tests + one-shot use).
# Set $COLLAB_MONITOR_NO_REAP to any non-empty value to disable globally.
# ---------------------------------------------------------------------------
if [ -z "${COLLAB_MONITOR_NO_REAP:-}" ] && ! $_ONCE && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped reap: only kill prior instances of the SAME purpose.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='collab-baton-monitor\.sh'
    else
        _pgrep_pattern="collab-baton-monitor\.sh.*--purpose ${PURPOSE}"
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
BATON_DIR="${_dir_arg:-${BATON_DIR:-/home/agents-shared/projects}}"

# ---------------------------------------------------------------------------
# seen-set — keyed to the watched BATON_DIR so concurrent instances (e.g. a
# smoke test watching a scratch dir) never share state.
# Override with $COLLAB_BATON_MONITOR_SEEN_FILE (tests).
# ---------------------------------------------------------------------------
SEEN="${COLLAB_BATON_MONITOR_SEEN_FILE:-$HOME/.engram/.collab-baton-monitor-seen.$(printf '%s' "$BATON_DIR" | cksum | cut -d' ' -f1)}"

# ---------------------------------------------------------------------------
# _compute_current_keys: emit sorted "basename.md:turn_since" pairs for every
# baton file in BATON_DIR whose YAML frontmatter turn field equals $SELF.
# Empty output when the dir is missing, empty, or no batons are turned to us.
# ---------------------------------------------------------------------------
_compute_current_keys() {
    for _f in "$BATON_DIR"/*.md; do
        [ -f "$_f" ] || continue
        # Extract turn: field from YAML frontmatter (between the two --- markers).
        # /^turn:[[:space:]]/ matches 'turn:' followed by whitespace — does NOT
        # match 'turn_since:' or 'turn_reason:' (those have longer key names).
        _turn=$(awk '
            /^---$/ { c++; next }
            c >= 2  { exit }
            c == 1 && /^turn:[[:space:]]/ {
                sub(/^turn:[[:space:]]*/, "")
                gsub(/"/, "")
                print
                exit
            }
        ' "$_f" 2>/dev/null)
        [ "$_turn" = "$SELF" ] || continue
        # Extract turn_since: field from the same frontmatter block.
        _ts=$(awk '
            /^---$/ { c++; next }
            c >= 2  { exit }
            c == 1 && /^turn_since:[[:space:]]/ {
                sub(/^turn_since:[[:space:]]*/, "")
                gsub(/"/, "")
                print
                exit
            }
        ' "$_f" 2>/dev/null)
        # Skip files with no turn_since (malformed frontmatter).
        [ -n "$_ts" ] || continue
        printf '%s:%s\n' "$(basename "$_f")" "$_ts"
    done | sort
}

# ---------------------------------------------------------------------------
# startup seeding: populate SEEN with the current state so we emit only FUTURE
# flips, not the entire history on arm. Skipped in --once mode so tests can
# control the starting SEEN content directly.
# ---------------------------------------------------------------------------
if ! $_ONCE; then
    _compute_current_keys > "$SEEN"
fi

# Ensure the seen-file exists (may be a caller-provided path in tests).
touch "$SEEN" 2>/dev/null || true

# Path to the last-user-activity stamp (written by the time-bar hook on each
# genuine human-typed prompt). Used to detect the 'engaged' / talking-to-user
# state — same source as collab-letter-monitor.
LAST_USER_ACTIVITY="$HOME/.engram/last-user-activity"
_ENGAGED_WINDOW=360

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    # Suppress events during interactive (talking-to-user / 'engaged') sessions.
    # Skip this check in --once mode for test determinism.
    if ! $_ONCE && [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            sleep 2; continue
        fi
    fi

    _compute_current_keys > "$SEEN.now"

    # Only diff+update on a non-empty listing — a transient empty output must
    # not clobber $SEEN (#743 class: seen-set flood on empty listing).
    if [ -s "$SEEN.now" ]; then
        comm -13 "$SEEN" "$SEEN.now" | while read -r _key; do
            # _key = "PR-1469.md:2026-06-26T15:05:26Z"
            _fname="${_key%%:*}"
            _f="$BATON_DIR/$_fname"

            # Find the latest turn log entry.
            # Turn log lines look like:
            #   - 2026-06-26T15:05:26Z ariadne → borges: reason text here
            _latest=$(grep '^- ' "$_f" 2>/dev/null | grep ' → ' | tail -1)
            [ -n "$_latest" ] || continue

            # Extract FROM (agent before →), TO (agent after →), and REASON.
            _from=$(printf '%s' "$_latest" | sed -E 's/^- [^ ]+ ([^ ]+) .*/\1/')
            _to=$(printf '%s' "$_latest" | sed -E 's/.*→ ([^ ]+):.*/\1/')
            _reason=$(printf '%s' "$_latest" | sed -E 's/.*→ [^ ]+: //')

            # Emit only when the latest flip is a genuine hand-off TO me:
            #   _to  == SELF          — baton is addressed to this agent
            #   _from != SELF         — suppress genuine self-flips (borges → borges)
            #   _from != "initialized" — suppress self-init false-positives:
            #                           a baton created with turn=SELF has a turn-log
            #                           entry "initialized → SELF" which is not a flip
            #   -n "$_from"           — conservative: if FROM is unparseable, don't
            #                           spuriously wake (the @-mention path backstops)
            if [ "$_to" = "$SELF" ] && [ "$_from" != "$SELF" ] && [ "$_from" != "initialized" ] && [ -n "$_from" ]; then
                _bname="${_fname%.md}"
                echo "📌 baton flipped to you: ${_bname} by ${_from} — ${_reason}"
            fi
        done
        mv "$SEEN.now" "$SEEN"
    fi

    $_ONCE && break
    sleep 2
done
