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
                   Self-reap is scoped to same-purpose instances only, so two
                   instances launched for different purposes do not cross-kill.

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
# single-instance: last-arm-wins. A persistent Monitor's bash process outlives
# its arming session — it survives compaction and session-end. Without this, a
# fresh arm after a compaction runs ALONGSIDE the surviving orphan and BOTH poll
# the same mentions endpoint, so every new @-mention fires the wake TWICE
# (double-delivery incident, 2026-06-13). Reap any prior instance of THIS monitor
# owned by THIS user, so the fresh arm cleanly takes over (and is the one that
# delivers to the current session). Scoped to our own uid (never touches another
# agent's monitor) and excludes our own pid and launching wrapper. NOTE: pgrep -f
# is a substring match, so a process merely *mentioning* this script name (e.g.
# an editor with the file open in a dev shell) could also match — harmless in
# production (agents don't edit a running monitor; SIGTERM + reseed-on-restart is
# recoverable). Set $FORUM_MENTION_NO_REAP to any non-empty value to disable.
# ---------------------------------------------------------------------------
if [ -z "${FORUM_MENTION_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped reap: only kill prior instances running with the SAME
    # --purpose key. For the "default" purpose (no --purpose arg passed), match
    # by script name only — identical to the pre-#1183 behaviour, ensuring
    # last-arm-wins for default instances. For an explicit non-default purpose
    # key, match by script name AND purpose so default instances (and instances
    # of a different non-default purpose) are never cross-killed (#1183).
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-mention-monitor\.sh'
    else
        _pgrep_pattern="forum-mention-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        kill "$_pid" 2>/dev/null || true
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
