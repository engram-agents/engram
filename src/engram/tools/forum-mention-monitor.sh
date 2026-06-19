#!/usr/bin/env bash
# forum-mention-monitor.sh — persistent Monitor for new forum @-mentions of
# this agent.
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   forum-mention-monitor.sh
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

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
forum-mention-monitor.sh — watch the LAN forum for new direct @-mentions of me

Usage: forum-mention-monitor.sh

Resolves SELF (URL-encoded) and forum BASE URL from ~/.engram/config.json.
URL resolution order: config.json forum.url > $FORUM_URL > http://localhost:5002

Seeds on first successful poll (retries until forum is reachable). Never
clobbers the seen-set on a failed poll — preserves prior baseline on
transient forum-down cycles.

Emits one line per new direct @-mention (post_id + thread title).
Polls every 30s. Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
    exit 0
fi

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
    for _pid in $(pgrep -u "$(id -u)" -f 'forum-mention-monitor\.sh' 2>/dev/null); do
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
# poll loop
# ---------------------------------------------------------------------------
while true; do
    sleep 30
    # Forum unreachable -> curl --fail returns non-zero -> skip the cycle,
    # PRESERVE $SEEN (the fix for the seed-clobber flood, #743).
    RESP="$(curl -s --fail "$URL" 2>/dev/null)" || continue
    printf '%s' "$RESP" | ids | sort > "$SEEN.now"
    comm -13 "$SEEN" "$SEEN.now" | while IFS=$'\t' read -r pid title; do
        [ -n "$pid" ] && echo "📣 new forum @-mention: post $pid in \"$title\""
    done
    mv "$SEEN.now" "$SEEN"
done
