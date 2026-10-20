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
7. **Managed Codex has its own persistent wake path.** Missing Monitor, ScheduleWakeup or CronList tools are not a blocker; don't fake them with detached stdout-only scripts. Verify the deployed `codex-umwelt-wake.py` managed-app-server bridge and the per-agent systemd heartbeat timer, and set the loop marker to `pacer: "systemd"` (the ScheduleWakeup Stop hook ignores other pacers). In ordinary heartbeat mode, use dynamic current-thread discovery, never a persistent `CODEX_THREAD_ID`, so a fresh session is not stranded on a stale thread; pin `CODEX_THREAD_ID` only for a bounded loop that genuinely needs one fixed thread.

