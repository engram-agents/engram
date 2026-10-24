---
id: loop-mode-arm-claude-code
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Loop-mode policy (self-wake / autonomous cron fires)"
order: 20
merge: replace
version: 2
---
6. **Arm every `ScheduleWakeup` loop wake through `format_loop_prompt`; never write the prompt by hand.** The prompt must START with the `<loop-wake>` marker. If the marker is missing or not first, the wake is read as human activity and the loop defers forever. The marker is authoritative, not the harness `promptSource` field (unreliable across Claude Code versions). Run `CronList` after arming: that check is the real gate, not understanding the mechanism. Details: the `engram-loop` skill.
7. **Declare `waiting_on` during bounded waits.** When starting a wait with a
   known return time (fairy dispatch, CI run, short Monitor expiry), write
   `waiting_on: {"description": "...", "return_by": "<ISO timestamp>"}` into
   `$ENGRAM_HOME/loop-mode.json` and remove the field when the result arrives.
   Within ~10 min of `return_by` the Quiet Wake Check stays quiet; a longer
   wait earns one study-unit nudge, then quiet until `return_by`. Unknown-return
   waits (counterpart deliberating,
   user response) are not bounded — omit this. Full tick rule and snippets:
   `engram-loop` skill §"Tick rule (Quiet Wake Check — known-return waits)".
8. **Managed Codex has its own persistent wake path.** Missing Monitor, ScheduleWakeup or CronList tools are not a blocker; don't fake them with detached stdout-only scripts. Verify the deployed `codex-umwelt-wake.py` managed-app-server bridge and the per-agent systemd heartbeat timer, and set the loop marker to `pacer: "systemd"` (the ScheduleWakeup Stop hook ignores other pacers). In ordinary heartbeat mode, use dynamic current-thread discovery, never a persistent `CODEX_THREAD_ID`, so a fresh session is not stranded on a stale thread; pin `CODEX_THREAD_ID` only for a bounded loop that genuinely needs one fixed thread.

