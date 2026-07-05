#!/usr/bin/env bash
# forum-updates-monitor.sh — persistent Monitor for new dm + baton wake-events
# from the unified /api/updates coordination feed (UCS Slice B).
#
# Replaces the local-FS collab-letter-monitor (inter-agent letters now live in
# the forum as DMs) AND adds real-time baton-turn wake (the prompt-hook only
# surfaces in-court batons at prompt time, not in real time). 100% LAN API — no
# local-filesystem watch — so it works on any machine, not just the 5090 (the
# UCS pure-API invariant; sibling to forum-mention-monitor.sh).
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   forum-updates-monitor.sh [--purpose <key>] [--kinds dm,baton]
#
# Multi-agent hosts may symlink this into /home/agents-shared/bin/ alongside
# ia/baton/forum (same convention; symlinking is a manual host-operator action).
#
# Discipline notes (load-bearing — do not remove; mirror forum-mention-monitor):
#   - resolves forum base URL from config, same precedence as the forum CLI:
#     config.json forum.url > $FORUM_URL > localhost:5002.
#   - CURSOR model (not seen-set): /api/updates is a (since, as_of] cursor feed —
#     the SERVER does the diffing. We persist the last as_of and poll
#     since=<cursor>; the feed never replays already-seen seqs. Seed = baseline to
#     the current as_of on the first successful poll (NO history replay on arm).
#   - never-advance-cursor on failed poll: curl --fail returns non-zero on
#     forum-unreachable; skip the cycle and PRESERVE the cursor. Advancing past a
#     window we never emitted would silently drop those wakes.
#   - engaged-suppression (#1077 part 2): while last-user-activity is within the
#     engaged window (default 360s), skip the emit cycle entirely AND do not
#     advance the cursor — events accumulate in (cursor, as_of] and emit once the
#     window expires.

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>, --kinds <csv>
# ---------------------------------------------------------------------------
PURPOSE="default"
KINDS="${FORUM_UPDATES_KINDS:-dm,baton}"
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
forum-updates-monitor.sh — watch the LAN coordination feed for new dm + baton events

Usage: forum-updates-monitor.sh [--purpose <key>] [--kinds dm,baton]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Self-reap by purpose, with one asymmetry (matches
                   forum-mention-monitor): a NAMED --purpose reaps only
                   same-named instances, so two different named purposes (e.g. a
                   smoke test) never cross-kill. The DEFAULT purpose (no
                   --purpose) reaps ALL instances incl. named ones — it is the
                   canonical last-arm-wins arm.
  --kinds <csv>    Comma-separated update kinds to watch (default: dm,baton).

Resolves SELF and forum BASE URL from ~/.engram/config.json.
URL resolution order: config.json forum.url > $FORUM_URL > http://localhost:5002

Cursor-based on GET /api/updates (since,as_of] — baselines to the current as_of
on first successful poll (no history replay on arm), never advances the cursor
on a failed poll. Emits one line per new dm / baton event. Polls every 30s.
Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        --kinds)
            KINDS="${2:?--kinds requires a value}"
            shift 2
            ;;
        *)
            echo "forum-updates-monitor.sh: unrecognised argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex (#1307).
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "forum-updates-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac
# Validate kinds: comma-separated [a-z] tokens only (goes into a URL query).
case "$KINDS" in
    *[!a-z,]*)
        echo "forum-updates-monitor: --kinds must be comma-separated lowercase tokens; got: '$KINDS'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity + forum URL (same precedence as forum-mention-monitor)
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os,urllib.parse;print(urllib.parse.quote(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""),safe=""))')"
BASE="$(python3 -c 'import json,os
c=json.load(open(os.path.expanduser("~/.engram/config.json")))
print(((c.get("forum") or {}).get("url") or os.environ.get("FORUM_URL") or "http://localhost:5002").rstrip("/"))')"
URL_BASE="$BASE/api/updates?agent=$SELF&kinds=$KINDS"

# ---------------------------------------------------------------------------
# single-instance: last-arm-wins (purpose-scoped). A persistent Monitor's bash
# process outlives its arming session; without this a post-compaction re-arm
# would run ALONGSIDE the orphan and double-deliver every wake. Reap prior
# instances of THIS monitor owned by THIS uid, excluding our own pid + wrapper.
# Set $FORUM_UPDATES_NO_REAP to any non-empty value to disable.
# ---------------------------------------------------------------------------
if [ -z "${FORUM_UPDATES_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Asymmetry (matches forum-mention-monitor): the DEFAULT purpose matches the
    # bare script name → reaps ALL instances incl. named ones (canonical
    # last-arm-wins). A NAMED purpose matches script + that --purpose only → two
    # different named purposes never cross-kill, but a default arm still reaps
    # them. Doc'd in --help.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-updates-monitor\.sh'
    else
        _pgrep_pattern="forum-updates-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        kill "$_pid" 2>/dev/null || true
    done
fi

# ---------------------------------------------------------------------------
# cursor file — keyed to the polled URL so concurrent instances never share
# state. Override with $FORUM_UPDATES_CURSOR_FILE (tests).
# ---------------------------------------------------------------------------
CURSOR="${FORUM_UPDATES_CURSOR_FILE:-$HOME/.engram/.forum-updates-cursor.$(printf '%s' "$URL_BASE" | cksum | cut -d' ' -f1)}"

# helper: read /api/updates JSON on stdin, print one wake line per update.
# Python uses ONLY double-quoted strings so the whole program can sit inside
# bash single-quotes with no nested-quote escaping (the fragile '"'"' dance).
emit() {
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for u in d.get("updates", []):
    k = u.get("kind")
    if k == "dm":
        body = (u.get("body") or "").replace("\n", " ").strip()
        if len(body) > 90:
            body = body[:90] + "…"
        print("✉️  new DM from " + str(u.get("sender", "?")) + ": " + body)
    elif k == "baton":
        title = (u.get("title") or "").strip()
        if len(title) > 50:
            title = title[:50] + "…"
        print("\U0001f3be baton " + str(u.get("project_id", "?")) + " → turn: " + str(u.get("turn", "?")) + " (" + title + ")")
    else:
        print("\U0001f514 new " + str(k) + " update (seq " + str(u.get("seq", "?")) + ")")
'
}

# helper: read /api/updates JSON on stdin, print the served as_of watermark.
as_of() {
    python3 -c 'import json,sys
try: print(int(json.load(sys.stdin).get("as_of",0)))
except Exception: print(0)'
}

# ---------------------------------------------------------------------------
# seed — retry until first SUCCESSFUL poll; baseline cursor = current as_of so
# arming does NOT replay history. Handles arm-while-forum-down (waits).
# ---------------------------------------------------------------------------
until RESP="$(curl -s --fail "$URL_BASE&since=0" 2>/dev/null)"; do sleep 30; done
printf '%s' "$RESP" | as_of > "$CURSOR"

# Path to the last-user-activity stamp (written by the time-bar hook on each
# genuine human-typed prompt) — same engaged-detection source as the
# mention-monitor / _status_derive._recently_engaged().
LAST_USER_ACTIVITY="$HOME/.engram/last-user-activity"
_ENGAGED_WINDOW=360  # matches _status_derive._DEFAULT_ENGAGED_WINDOW

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    sleep 30
    # engaged-suppression: while the user is actively conversing, skip the emit
    # cycle AND leave the cursor put — events accumulate in (cursor, as_of] and
    # emit once the engaged window expires.
    if [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            continue
        fi
    fi
    _cur="$(cat "$CURSOR" 2>/dev/null || echo 0)"
    # Forum unreachable -> curl --fail returns non-zero -> skip the cycle and
    # PRESERVE the cursor (never advance past an un-emitted window).
    RESP="$(curl -s --fail "$URL_BASE&since=$_cur" 2>/dev/null)" || continue
    printf '%s' "$RESP" | emit
    # advance the cursor to the served watermark only after a successful poll
    _new="$(printf '%s' "$RESP" | as_of)"
    [ -n "$_new" ] && [ "$_new" -ge "$_cur" ] 2>/dev/null && printf '%s' "$_new" > "$CURSOR"
done
