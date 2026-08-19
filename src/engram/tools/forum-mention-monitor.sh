#!/usr/bin/env bash
# forum-mention-monitor.sh — persistent Monitor for new forum @-mentions of
# this agent.
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   forum-mention-monitor.sh [--purpose <key>]
#
# Multi-agent hosts may symlink this into /home/agents-shared/bin/ alongside
# ia/baton/forum (same convention; no install step creates the symlink — it is
# a manual host-operator action today).
#
# Discipline notes (load-bearing — do not remove):
#   - resolves forum base URL from config, same precedence as forum CLI:
#     config.json forum.url > $FORUM_URL > localhost:5002.
#   - seed-retry-until-first-successful-poll: baseline is always the real
#     current state. Handles arm-while-forum-down (waits), arm-while-up (seeds
#     now), and genuine-zero-mentions (empty baseline on a successful poll).
#   - never clobber seen-set on failed poll: curl --fail returns non-zero on
#     forum-unreachable; skip the cycle and preserve $SEEN. Otherwise the next
#     recovery would flood every historical mention as "new" (#743 / 2026-06-03
#     forum cutover incident).
#   - engaged-suppression (#1077 part 2): while last-user-activity is within
#     the engaged window (default 360s = 6 min), skip the emit cycle entirely —
#     mentions accumulate as unseen and are emitted after the window expires.
#     The 'engaged' status is published by the forum prompt hook independently.
#   - cross-cycle coalesce window (#1616 / UCS rc2 F10): the first new
#     mention detected opens a fixed coalesce window (default 120s, override
#     via $FORUM_MENTION_COALESCE_SECONDS); further new mentions across
#     subsequent polls accumulate in a buffer WITHOUT emitting; at window
#     close, ONE summary line is emitted for the whole burst (a hot thread
#     now wakes at most once per window instead of once per poll). $SEEN
#     still advances every successful poll regardless of window state —
#     mentions seen during the window are not re-emitted after flush; the
#     window changes WHEN/HOW a mention is announced, never the seen-set
#     semantics. The buffer is untouched on a failed poll — same
#     never-clobber discipline as $SEEN (#743), so a forum-down cycle
#     mid-window loses nothing. CAVEAT: on TaskStop/SIGTERM mid-window the
#     buffered-but-unflushed mentions are dropped (not flushed on exit);
#     $SEEN has already advanced for them, so they won't re-emit next arm —
#     recovery is the browse-on-wake routine (collab-loop 3b re-reads the
#     forum), not this monitor. Acceptable: TaskStop = session ending anyway.

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>
# ---------------------------------------------------------------------------
PURPOSE="default"
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
forum-mention-monitor.sh — watch the LAN forum for new direct @-mentions of me

Usage: forum-mention-monitor.sh [--purpose <key>]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Duplicate-instance DETECTION (not reap — see #1933) is
                   scoped to same-purpose instances only, so two instances
                   launched for different purposes do not cross-flag. This
                   monitor never kills a suspected duplicate — it prints a
                   warning naming the pid to stdout (the Monitor event
                   stream) and lets the agent decide whether to `kill <pid>`
                   by hand.

Resolves SELF (URL-encoded) and forum BASE URL from ~/.engram/config.json.
URL resolution order: config.json forum.url > $FORUM_URL > http://localhost:5002

Seeds on first successful poll (retries until forum is reachable). Never
clobbers the seen-set on a failed poll — preserves prior baseline on
transient forum-down cycles.

Coalesces new @-mentions into a summary: the first new mention opens a
coalesce window (default 120s, $FORUM_MENTION_COALESCE_SECONDS); further
new mentions across subsequent polls accumulate without emitting; at
window close, ONE summary line covers the whole burst, e.g.:
  "N new @-mentions across M threads: #200(x3), #199(x1)"
A hot thread wakes at most once per window instead of once per poll.
Polls every 30s. Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        *)
            echo "forum-mention-monitor.sh: unrecognised argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex (#1307).
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "forum-mention-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity + forum URL
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os,urllib.parse;print(urllib.parse.quote(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""),safe=""))')"
BASE="$(python3 -c 'import json,os
c=json.load(open(os.path.expanduser("~/.engram/config.json")))
print(((c.get("forum") or {}).get("url") or os.environ.get("FORUM_URL") or "http://localhost:5002").rstrip("/"))')"
URL="$BASE/api/agent/$SELF/mentions?kind=at_mention"   # #1040: at_mention only — a reply to a thread you authored is NOT a direct @-mention and must not over-fire this real-time wake

# ---------------------------------------------------------------------------
# duplicate-instance detection: WARN, never kill (#1933). A persistent
# Monitor's bash process outlives its arming session — it survives compaction
# and session-end. Without SOME check for a still-running prior instance, a
# fresh arm after a compaction runs ALONGSIDE the surviving orphan and BOTH
# poll the same mentions endpoint, so every new @-mention would fire the wake
# TWICE (double-delivery incident, 2026-06-13) — that risk is real and still
# worth flagging. But the old fix (SIGTERM every pgrep -f match) assumed
# pgrep's unanchored substring match over the WHOLE cmdline could only ever
# hit a genuine prior monitor. It cannot be trusted that way: `agentctl
# --charge-file` appends the boot charge to claude's own argv, #1865 put this
# script's path INSIDE that charge text, and claude sits as this monitor's
# GRANDPARENT — outside the $$/$PPID exemptions below — so every
# charge-booted session was reaping (killing) itself four days running. The
# old comment here called the substring-match risk "harmless in production"
# (an editor merely mentioning the filename) — #1933 is the counterexample:
# a substring match must never be an execution decision (Lei's ruling). This
# block now only WARNS on stdout — the Monitor event stream, so the warning
# reaches the agent as a notification — and leaves every matched process
# alone. A true duplicate left alive double-delivers every event; the agent
# inspects the flagged pid and kills it by hand if it really is stale.
#
# Detection is two-layered so the warning is worth reading: pgrep -f finds
# loose CANDIDATES first (same substring pattern as the old reap — cheap, but
# exactly the over-match above), then each candidate is re-verified against
# /proc/<pid>/cmdline, requiring argv[0] (leading `-` stripped, since a
# login-shell invocation like `exec -a "-bash" bash <script>` sets argv[0] to
# "-bash") to be a bash-family interpreter AND a later argv token to BE the
# script (exact match, or a ".../<script>" path suffix) — not merely present
# anywhere in the cmdline. Mirrors umwelt-bell.py's _is_bell_process (umwelt
# #205 — the same bug class: "matched editors/grep/review-shells/wrappers
# whose cmdline merely contained the filename"). $$/$PPID exemptions are kept
# as a cheap second layer on top of the identity check, not the only guard.
#
# Verification has THREE outcomes, not two — mirrors umwelt-bell.py's
# proc_cmdline()'s ok/gone/unknown split, which the very first port of this
# collapsed into a silent no-match. A process that vanishes mid-read (ENOENT/
# ESRCH — the file or pid is simply gone) is correctly silent: it lost the
# race, not a duplicate. But any OTHER read failure (e.g. EACCES — /proc read
# denied) means verification could NOT be completed, which is a DIFFERENT
# claim than "verified not a match" and must not be silently folded into it —
# say so loudly so the agent knows this candidate needs a manual look.
#
# Set $FORUM_MENTION_NO_REAP to any non-empty value to disable (now disables
# the detection/warning pass, not a kill).
# ---------------------------------------------------------------------------
_check_forum_mention_dup() {
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

if [ -z "${FORUM_MENTION_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped detection: only flag prior instances running with the
    # SAME --purpose key as suspects. For the "default" purpose (no --purpose
    # arg passed), match by script name only — identical candidate set to the
    # pre-#1183 behaviour. For an explicit non-default purpose key, match by
    # script name AND purpose so default instances (and instances of a
    # different non-default purpose) are never cross-flagged (#1183).
    #
    # #2029 inventory answers: could this candidate pattern match the
    # invoking process (a boot charge quoting this script's path, #1933)?
    # YES -- by design, at the pgrep-candidate stage alone. That is exactly
    # why this site is two-layered: -u "$(id -u)" scopes to this uid only
    # (so a co-tenant's equivalent process on a shared host can never be a
    # candidate at all), and every candidate is then re-verified against
    # /proc/<pid>/cmdline below (_check_forum_mention_dup) before it is even
    # WARNED about -- and the action is warn-not-kill regardless, so an
    # over-matched candidate can never cause a reap. See
    # tests/test_pgrep_liveness_reap_patterns.py for the synthetic-table
    # proof that this candidate pattern over-matches at layer 1, and
    # tests/test_forum_monitor_duplicate_warn.py for the end-to-end proof
    # that the self-quoting decoy is neither reported nor killed.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-mention-monitor\.sh'
    else
        _pgrep_pattern="forum-mention-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_forum_mention_dup "$_pid" "forum-mention-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  forum-mention-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-mention-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  forum-mention-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
    done
fi

# ---------------------------------------------------------------------------
# seen-set
# ---------------------------------------------------------------------------
# Seen-set keyed to the polled URL so concurrent instances (smoke tests,
# alternate forums) never share state — a fixed path lets a second instance
# clobber the live monitor's baseline and flood it (#743 class via
# cross-instance clobber). Override with $FORUM_MENTION_SEEN_FILE (tests).
# Note (#1040): appending ?kind=at_mention changed $URL, so the cksum changes —
# the monitor reseeds its baseline once on the post-upgrade first run (harmless;
# the seed-poll just re-establishes "current state" before emitting).
SEEN="${FORUM_MENTION_SEEN_FILE:-$HOME/.engram/.forum-mention-seen.$(printf '%s' "$URL" | cksum | cut -d' ' -f1)}"

# helper: extract "post_id\tthread_title" lines from JSON stdin
ids() {
    python3 -c 'import json,sys
try:
    for m in json.load(sys.stdin).get("mentions",[]): print(str(m.get("post_id"))+"\t"+m.get("thread_title","?"))
except Exception: pass'
}

# ---------------------------------------------------------------------------
# seed — retry until first SUCCESSFUL poll
# Handles: arm-while-forum-down (waits), arm-while-up (seeds now), and
# genuine-zero-mentions (empty baseline on a *successful* poll).
# ---------------------------------------------------------------------------
until RESP="$(curl -s --fail "$URL" 2>/dev/null)"; do sleep 30; done
printf '%s' "$RESP" | ids | sort > "$SEEN"

# ---------------------------------------------------------------------------
# cross-cycle coalesce window (#1616 / UCS rc2 F10)
# A hot thread can produce a new @-mention on almost every 30s poll, which
# without coalescing wakes the agent every cycle for as long as the thread
# stays hot. Fix: the FIRST new mention detected opens a FIXED (not
# sliding/extended) coalesce window; further new mentions across subsequent
# polls accumulate into a buffer WITHOUT emitting; at window close, ONE
# summary line is emitted for the whole burst (the Monitor tool still
# batches it as a single wake). The ScheduleWakeup heartbeat already floors
# liveness, so this loses zero responsiveness — just fewer, batched wakes.
#
# The buffer is a plain file, untouched on a failed poll (curl --fail
# non-zero -> `continue` below skips straight past both the $SEEN update AND
# the buffer/window logic) — same never-clobber discipline that protects
# $SEEN (#743), applied to the buffer too, so a forum-down cycle mid-window
# loses nothing.
# ---------------------------------------------------------------------------
COALESCE_SECONDS="${FORUM_MENTION_COALESCE_SECONDS:-120}"
# Poll cadence override — tests only (mirrors $FORUM_MENTION_SEEN_FILE's
# test-only role above); default of 30 is unchanged for real arms.
POLL_SECONDS="${FORUM_MENTION_POLL_SECONDS:-30}"
BUF="$SEEN.buf"
: > "$BUF"          # fresh arm — no pending burst carried over from a prior run
_WINDOW_OPEN_TS=""  # empty = no window currently open

# flush_buffer: $BUF holds one thread_id per buffered new mention (one line
# per mention, so a thread_id repeats once per mention in that thread — the
# source of the "×N" tally below). On a non-empty buffer, emit ONE summary
# line; always clear the buffer and close the window afterward. No-op
# (silently) on an empty buffer — called unconditionally once the window
# has elapsed, whether or not anything is actually buffered.
flush_buffer() {
    if [ -s "$BUF" ]; then
        python3 -c '
import collections, sys
lines = [l.strip() for l in sys.stdin if l.strip()]
n = len(lines)
counts = collections.OrderedDict()
for tid in lines:
    counts[tid] = counts.get(tid, 0) + 1
parts = ", ".join("#" + str(t) + "(×" + str(c) + ")" for t, c in counts.items())
mention_word = "mention" if n == 1 else "mentions"
thread_word = "thread" if len(counts) == 1 else "threads"
print("\U0001f4e3 " + str(n) + " new @-" + mention_word + " across " + str(len(counts)) + " " + thread_word + ": " + parts)
' < "$BUF"
        : > "$BUF"
    fi
    _WINDOW_OPEN_TS=""
}

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
    sleep "$POLL_SECONDS"
    # Suppress events during interactive (talking-to-user / 'engaged') sessions
    # (#1077 part 2). When the user is actively conversing, waking the agent
    # mid-turn adds noise. Mentions accumulate as unseen; the monitor emits
    # them once the engaged window expires (default 6 min of user inactivity).
    if [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            continue
        fi
    fi
    # Forum unreachable -> curl --fail returns non-zero -> skip the cycle,
    # PRESERVE $SEEN (the fix for the seed-clobber flood, #743) AND the
    # coalesce buffer/window (nothing below this line runs).
    RESP="$(curl -s --fail "$URL" 2>/dev/null)" || continue
    printf '%s' "$RESP" | ids | sort > "$SEEN.now"
    _new_pids="$(comm -13 "$SEEN" "$SEEN.now" | cut -f1)"
    mv "$SEEN.now" "$SEEN"
    if [ -n "$_new_pids" ]; then
        # Look up each newly-seen post's thread_id in the just-fetched $RESP
        # (no extra HTTP call) and append one line per mention to the buffer
        # — the coalesce window changes WHEN/HOW these are announced, never
        # whether $SEEN advances (already done above).
        printf '%s' "$RESP" | python3 -c '
import json, sys
wanted = set(sys.argv[1].split())
# Blanket-except + isinstance guards so a status-200-but-malformed body
# (non-dict, or "mentions" a non-list) degrades to "append nothing this
# cycle" rather than throwing mid-loop — matches ids()'"'"'s defensive style.
try:
    d = json.load(sys.stdin)
    mentions = d.get("mentions", []) if isinstance(d, dict) else []
    for m in mentions:
        if isinstance(m, dict) and str(m.get("post_id")) in wanted:
            print(m.get("thread_id", "?"))
except Exception:
    pass
' "$_new_pids" >> "$BUF"
        [ -z "$_WINDOW_OPEN_TS" ] && _WINDOW_OPEN_TS="$(date +%s)"
    fi
    # Check window elapsed every cycle (not just cycles with fresh mentions)
    # so a previously-opened window still flushes on schedule.
    if [ -n "$_WINDOW_OPEN_TS" ]; then
        _now="$(date +%s)"
        _elapsed=$(( _now - _WINDOW_OPEN_TS ))
        [ "$_elapsed" -ge "$COALESCE_SECONDS" ] && flush_buffer
    fi
done
