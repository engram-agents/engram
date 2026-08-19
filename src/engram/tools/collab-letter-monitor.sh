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
#   - engaged-suppression (#1077 part 2): while last-user-activity is within
#     the engaged window (default 360s = 6 min), skip the emit cycle entirely —
#     letters accumulate as unseen and are emitted after the window expires.
#     The 'engaged' status is published by the forum prompt hook independently.

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
# duplicate-instance detection: WARN, never kill (#1933, ported to this
# script by #1935). A persistent Monitor's bash process outlives its arming
# session — it survives compaction and session-end. Without SOME check for a
# still-running prior instance, a fresh arm after a compaction runs ALONGSIDE
# the surviving orphan and BOTH poll the same dir, so every new letter fires
# the wake TWICE (double-delivery incident, 2026-06-13). But the old fix here
# (SIGTERM every pgrep -f match) assumed pgrep's unanchored substring match
# over the WHOLE cmdline could only ever hit a genuine prior monitor. It
# cannot be trusted that way: `agentctl --charge-file` appends the boot
# charge to claude's own argv, and if a charge ever quotes this script's
# launch line, claude sits as this monitor's GRANDPARENT — outside the
# $$/$PPID exemptions below — so a charge-booted session would reap (kill)
# itself (#1933's exact mechanism, applied here per #1935's finding). The old
# comment here called the substring-match risk "harmless in production" (an
# editor merely mentioning the filename) — #1933 is the counterexample: a
# substring match must never be an execution decision (Lei's ruling). This
# block now only WARNS on stdout — the Monitor event stream, so the warning
# reaches the agent as a notification — and leaves every matched process
# alone. A true duplicate left alive double-delivers every event; the agent
# inspects the flagged pid and kills it by hand if it really is stale.
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
_check_collab_letter_dup() {
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

if [ -z "${COLLAB_MONITOR_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped detection: only flag prior instances running with the
    # SAME --purpose key as suspects. For the "default" purpose (no --purpose
    # arg passed), match by process-name + script name only — identical
    # candidate SET semantics to the pre-#1183 behaviour (still scoped to
    # default instances only). For an explicit non-default purpose key, match
    # by process-name + script name AND purpose so default instances (and
    # instances of a different non-default purpose) are never cross-flagged
    # (#1183). See tests/test_pgrep_liveness_reap_patterns.py for the
    # synthetic-table proof that this candidate pattern excludes the
    # self-quoting decoy, and tests/test_forum_monitor_duplicate_warn.py for
    # the end-to-end proof of the equivalent pattern on the sibling forum
    # monitors.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='^(\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\s+\S*collab-letter-monitor\.sh'
    else
        _pgrep_pattern="^(\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\s+\S*collab-letter-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_collab_letter_dup "$_pid" "collab-letter-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  collab-letter-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  collab-letter-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  collab-letter-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
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

# Path to the last-user-activity stamp (written by the time-bar hook on each
# genuine human-typed prompt). Used to detect the 'engaged' / talking-to-user
# state — same source as _status_derive._recently_engaged() in Python.
LAST_USER_ACTIVITY="$HOME/.engram/last-user-activity"
# Engaged-window in seconds: matches _status_derive._DEFAULT_ENGAGED_WINDOW (360).
# If cadence.engaged_window_seconds is customised in config.json, update this too.
_ENGAGED_WINDOW=360

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    # Suppress events during interactive (talking-to-user / 'engaged') sessions
    # (#1077 part 2). When the user is actively conversing, waking the agent
    # mid-turn adds noise. Letters accumulate as unseen; the monitor emits
    # them once the engaged window expires (default 6 min of user inactivity).
    if [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            sleep 2; continue
        fi
    fi
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
