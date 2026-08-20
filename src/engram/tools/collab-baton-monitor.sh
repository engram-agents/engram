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
#   - duplicate-instance detection: warn-not-kill (#1933, ported by #1935),
#     same as collab-letter-monitor and the forum-*-monitor siblings.
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
# duplicate-instance detection: WARN, never kill (#1933, ported to this
# script by #1935). A persistent Monitor's bash process outlives its arming
# session — it survives compaction and session-end. Without SOME check for a
# still-running prior instance, a fresh arm after a compaction runs ALONGSIDE
# the surviving orphan and BOTH poll the same baton dir, so every flip fires
# the wake TWICE. But the old fix here (SIGTERM every pgrep -f match) assumed
# pgrep's unanchored substring match over the WHOLE cmdline could only ever
# hit a genuine prior monitor. It cannot be trusted that way: `agentctl
# --charge-file` appends the boot charge to claude's own argv, and if a charge
# ever quotes this script's launch line, claude sits as this monitor's
# GRANDPARENT — outside the $$/$PPID exemptions below — so a charge-booted
# session would reap (kill) itself (#1933's exact mechanism, applied here per
# #1935's finding). A substring match must never be an execution decision
# (Lei's ruling, #1933). This block now only WARNS on stdout — the Monitor
# event stream, so the warning reaches the agent as a notification — and
# leaves every matched process alone. A true duplicate left alive
# double-delivers every event; the agent inspects the flagged pid and kills
# it by hand if it really is stale. Skipped in --once mode (tests + one-shot
# use).
#
# Detection is two-layered so the warning is worth reading: pgrep -f finds
# loose CANDIDATES first (an anchored process-name pattern — see below —
# cheap, and already excludes the self-quoting-charge-text decoy at this
# layer, unlike the forum monitors' looser candidate pattern), then each
# candidate is re-verified against /proc/<pid>/cmdline, requiring argv[0]
# (leading `-` stripped, since a login-shell invocation like `exec -a "-bash"
# bash <script>` sets argv[0] to "-bash") to be a bash-family interpreter AND
# a later argv token to BE the script (exact match, or a ".../<script>" path
# suffix) — not merely present anywhere in the cmdline. Mirrors
# umwelt-bell.py's _is_bell_process (umwelt #205) and forum-mention-monitor.sh
# / forum-updates-monitor.sh's _check_*_dup (#1933).
#
# Verification has THREE outcomes, not two — mirrors the forum monitors'
# ok/gone/unknown split. A process that vanishes mid-read (ENOENT/ESRCH — the
# file or pid is simply gone) is correctly silent: it lost the race, not a
# duplicate. But any OTHER read failure (e.g. EACCES — /proc read denied)
# means verification could NOT be completed, which is a DIFFERENT claim than
# "verified not a match" and must not be silently folded into it — say so
# loudly so the agent knows this candidate needs a manual look.
#
# The candidate pattern itself is ALSO anchored on the process name
# (`^(\S*/)?bash\s+...`, mirroring deploy/autostart/boot-charge-*.md's
# #2028 monitor-verify fix) rather than the forum monitors' bare unanchored
# script-name substring — belt-and-suspenders on top of the /proc layer:
# even the loose candidate stage cannot mistake a charge-text decoy (whose
# cmdline does not itself START with a bash-family interpreter) for a
# genuine instance.
#
# Set $COLLAB_MONITOR_NO_REAP to any non-empty value to disable (now disables
# the detection/warning pass, not a kill).
# ---------------------------------------------------------------------------
_check_collab_baton_dup() {
    # $1=pid $2=script-basename. Reads /proc/<pid>/cmdline ONCE (NUL-separated
    # argv). Exit codes:
    #   0 = confirmed duplicate — stdout is the cmdline (argv joined on
    #       spaces, truncated to ~100 chars — the boot charge alone runs
    #       ~2.5KB and must not flood the Monitor event stream).
    #   1 = not a duplicate, including "process gone mid-read" (a race, not
    #       an error) — silent, nothing on stdout.
    #   2 = could NOT verify (a non-ENOENT/ESRCH OSError, e.g. permission
    #       denied) — stdout is a short reason string; caller must warn, not
    #       silently skip.
    python3 -c '
import errno, os, sys
pid, script = sys.argv[1], sys.argv[2]
try:
    with open("/proc/" + pid + "/cmdline", "rb") as f:
        raw = f.read()
except OSError as e:
    if e.errno in (errno.ENOENT, errno.ESRCH):
        sys.exit(1)  # process gone mid-read: not a match, not an error
    print(str(e) or type(e).__name__)
    sys.exit(2)  # could not verify — do not silently treat as "not a match"
argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
if not argv:
    sys.exit(1)
interp = os.path.basename(argv[0]).lstrip("-")  # login shell: argv[0] == "-bash"
if interp not in ("bash", "sh", "dash", "ksh", "zsh", "ash"):
    sys.exit(1)
if not any(a == script or a.endswith("/" + script) for a in argv[1:]):
    sys.exit(1)
cmd = " ".join(argv)
print(cmd[:100])
' "$1" "$2" 2>/dev/null
}

if [ -z "${COLLAB_MONITOR_NO_REAP:-}" ] && ! $_ONCE && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped detection: only flag prior instances running with the
    # SAME --purpose key as suspects. For the "default" purpose (no --purpose
    # arg passed), match by process-name + script name only. For an explicit
    # non-default purpose key, match by process-name + script name AND
    # purpose so default instances (and instances of a different non-default
    # purpose) are never cross-flagged. See
    # tests/test_pgrep_liveness_reap_patterns.py for the synthetic-table
    # proof that this candidate pattern excludes the self-quoting decoy, and
    # tests/test_forum_monitor_duplicate_warn.py for the end-to-end proof of
    # the equivalent pattern on the sibling forum monitors.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='^(\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\s+\S*collab-baton-monitor\.sh'
    else
        _pgrep_pattern="^(\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\s+\S*collab-baton-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_collab_baton_dup "$_pid" "collab-baton-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  collab-baton-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  collab-baton-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  collab-baton-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
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
