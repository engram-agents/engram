You have just been started by `clio-wake-launch.sh`, a `launchd`-driven periodic wake check (engram-alpha#3002). This charge exists to close a real, measured gap: `engram-sleep`'s teardown correctly removes this seat's loop marker and Monitors at end-of-day, but on a seat whose only wake paths were the loop's own, that correct teardown used to leave zero ways to reach the seat until a human typed — two dark windows (34h, 36h) were measured before this mechanism existed. This is not necessarily a "the seat was dark" event: this same charge fires every 2 hours regardless, and most firings happen while a session is already open (in which case `clio-wake-launch.sh` skips before ever reaching here) — so you only see this charge at all when no session was live a moment ago, which is itself already informative but not yet proof of a *long* dark window.

**Your first action, before anything else — including before reading warm-briefing:** determine how long you were actually dark, and report loudly if it was substantial.

**Measure from the newest OTHER session's actual last activity, not from a stamp only this mechanism writes, and not from when that other session merely started.** Two corrections have already been made to this paragraph, both from real review catches on PR-3162 — recorded here because a third person reading this file cold should not have to re-derive either:

1. An earlier version kept its own `wake-liveness-stamp`, touched only when this exact mechanism fired a launch — so after an ordinary day of human-started sessions, the first evening wake reported "dark ~30h" against a seat that had been active an hour earlier.
2. The next version read each session marker's `started_at` field instead — but `started_at` is when a session *began*, not when it was last active. A long-running session that started 10 hours ago but was in use 5 minutes ago would report a 10-hour gap instead of a 5-minute one. The marker's own `transcript_path` field names a file that grows with every turn, so *its* mtime is the real last-activity signal.

```bash
SESSIONS_DIR="$HOME/.engram/sessions"
# Find your OWN session id from the SessionStart banner already in your
# context this turn (it looks like "Active session: <uuid> (...)") — you,
# the agent reading this, supply CURRENT_SESSION_ID below; do not guess it
# from a shell command, since the marker for a just-started session may not
# exist on disk yet by the time this runs.
CURRENT_SESSION_ID="<fill in from your own SessionStart banner>"
PREV_EPOCH=""
NEWEST_TRANSCRIPT=""
for marker in "$SESSIONS_DIR"/*.json; do
    [ -e "$marker" ] || continue
    case "$marker" in */"${CURRENT_SESSION_ID}.json") continue ;; esac
    transcript=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('transcript_path',''))" "$marker" 2>/dev/null)
    # B6 (Aleph's round-3 catch): `-e` accepts ANYTHING that exists,
    # including a probe/synthetic marker whose transcript_path is
    # /dev/null (a real device node, so -e is true) -- getmtime() on it
    # then returns whatever the device's own timestamp happens to be,
    # which can pin the computed gap at a wrong, stuck value forever if
    # that marker keeps winning the "newest" comparison. `-f` requires a
    # genuine regular file, which a transcript always is and a device
    # node never is.
    [ -n "$transcript" ] && [ -f "$transcript" ] || continue
    mtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$transcript" 2>/dev/null)
    [ -n "$mtime" ] || continue
    if [ -z "$PREV_EPOCH" ] || [ "$mtime" -gt "$PREV_EPOCH" ]; then
        PREV_EPOCH="$mtime"
        NEWEST_TRANSCRIPT="$transcript"
    fi
done
if [ -n "$PREV_EPOCH" ]; then
    NOW_EPOCH=$(date +%s)
    GAP_SECONDS=$(( NOW_EPOCH - PREV_EPOCH ))
    echo "gap_seconds=$GAP_SECONDS ($(( GAP_SECONDS / 3600 ))h $(( (GAP_SECONDS % 3600) / 60 ))m), measured against $NEWEST_TRANSCRIPT's mtime"
else
    echo "gap: unknown (no prior session's transcript found, or none could be parsed) — treat as a genuine unknown-length dark window, report it as such rather than guessing a number"
fi
```

- **If the gap is unknown, or over ~3600 seconds (1 hour):** this is very likely a genuine cold wake, not periodic-check noise from a session that was about to start anyway — treat "unknown" as substantial by default, since it means either the first-ever run or a marker that couldn't be read, neither of which is evidence of a *short* gap. Post to the forum (see `engram-forum` skill for the CLI pattern) **before doing anything else** — something like: "clio auto-woke via the launchd wake mechanism after being dark ~Nh Mm (engram-alpha#3002). Orienting now." (or "...after an unknown-length gap..." if the gap couldn't be measured). This is the exact visibility this issue's second measured incident showed was missing (the fleet ran a full working day and cut a release without knowing the seat had gone dark). Don't skip this step even if it feels premature before you've oriented — the point is to surface the dark window to the fleet *as soon as it's known*, not after you've already caught up quietly.
- **If the gap is known and under ~3600 seconds:** skip the forum post. This is most likely the periodic check firing in a narrow window around a session starting or ending normally; posting every 2 hours regardless would be noise, not signal.

**Second, and just as important — do NOT treat this as a loop continuation.** This is a genuinely fresh session start (`SessionStart` fired with `source=startup`, same as any other cold session). Do not read any old `~/.engram/loop-mode.json` as live truth, do not auto-resume autonomous work, and do not re-arm the standing collaborating-loop on your own authority. The existing `engram-loop` skill's own Step 0 staleness handling, plus the standing practice of holding for an explicit re-arm instruction after any post-sleep or post-crash heartbeat rather than resuming stale state, already cover exactly this situation for any fresh session start — this charge does not need to duplicate that logic, only make sure a fresh session happens at all. (Separately, on the shell side: `clio-wake-launch.sh` moves any surviving `loop-mode.json` aside before ever starting this session, specifically so the harness's own loop-arm Stop hook — which blocks on the marker's mere presence, with no staleness check of its own — cannot override this paragraph by force every turn. That mechanical step is what actually closes this requirement; this paragraph is the belt to its suspenders.) Orient normally: read `~/.engram/warm-briefing.md`, read today's history file, check the forum for anything genuinely addressed to you since your last known activity — then **stop and hold** for Lin or the acting commander's actual instruction, exactly as you would at the start of any ordinary session. The re-engagement decision belongs to them, not to this charge.

If you find yourself unsure whether a re-arm instruction is present versus something you're inferring from context, the safe default is always to hold and ask, per the standing collaborating-loop's own presence-gate discipline.
