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
                   Duplicate-instance DETECTION (not reap — see #1933) is
                   purpose-scoped, with one asymmetry (matches
                   forum-mention-monitor): a NAMED --purpose only flags
                   same-named instances as suspects, so two different named
                   purposes (e.g. a smoke test) never cross-flag. The DEFAULT
                   purpose (no --purpose) flags ALL instances incl. named
                   ones. This monitor never kills a suspected duplicate — it
                   prints a warning naming the pid to stdout (the Monitor
                   event stream) and lets the agent decide whether to `kill
                   <pid>` by hand.
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
# duplicate-instance detection: WARN, never kill (#1933). A persistent
# Monitor's bash process outlives its arming session; without SOME check for
# a still-running prior instance, a post-compaction re-arm would run
# ALONGSIDE the orphan and double-deliver every wake — that risk is real and
# still worth flagging. But the old fix (SIGTERM every pgrep -f match)
# assumed pgrep's unanchored substring match over the WHOLE cmdline could
# never hit anything except a genuine prior monitor. It can: `agentctl
# --charge-file` appends the boot charge to claude's own argv, #1865 put this
# script's path INSIDE that charge text, and claude sits as this monitor's
# GRANDPARENT — outside the $$/$PPID exemptions below — so every
# charge-booted session was reaping (killing) itself four days running. A
# substring match must never be an execution decision (Lei's ruling, #1933):
# this block now only WARNS on stdout — the Monitor event stream, so the
# warning reaches the agent as a notification — and leaves every matched
# process alone. A true duplicate left alive double-delivers every event; the
# agent inspects the flagged pid and kills it by hand if it really is stale.
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
# Set $FORUM_UPDATES_NO_REAP to any non-empty value to disable (now disables
# the detection/warning pass, not a kill).
# ---------------------------------------------------------------------------
_check_forum_updates_dup() {
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

if [ -z "${FORUM_UPDATES_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Asymmetry (matches forum-mention-monitor): the DEFAULT purpose matches
    # the bare script name → candidates include ALL instances incl. named
    # ones. A NAMED purpose matches script + that --purpose only → two
    # different named purposes never cross-flag, but a default arm still
    # considers them candidates. Doc'd in --help.
    #
    # #2029 inventory answers: could this candidate pattern match the
    # invoking process (a boot charge quoting this script's path, #1933)?
    # YES, at the pgrep-candidate stage -- mitigated the same way as
    # forum-mention-monitor.sh: -u "$(id -u)" excludes any co-tenant on a
    # shared host from ever being a candidate, every candidate is
    # re-verified against /proc/<pid>/cmdline before being reported
    # (_check_forum_updates_dup), and the action is warn-not-kill
    # regardless. See tests/test_pgrep_liveness_reap_patterns.py and
    # tests/test_forum_monitor_duplicate_warn.py.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-updates-monitor\.sh'
    else
        _pgrep_pattern="forum-updates-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_forum_updates_dup "$_pid" "forum-updates-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  forum-updates-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-updates-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  forum-updates-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
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
