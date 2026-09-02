---
id: loop-mode-arm-claude-code
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Loop-mode policy (self-wake / autonomous cron fires)"
order: 20
merge: replace
version: 1
---
6. **Arm every `ScheduleWakeup` loop-wake via `format_loop_prompt` — never hand-construct the prompt.** It MUST lead with the `<loop-wake>` marker. Paraphrasing or hand-typing the prompt, instead of using `format_loop_prompt`'s output, is what drops or displaces that marker. An ABSENT marker — or one present but not leading — is misclassified as human activity → self-stamps last-user-activity → the engaged-gate defers *perpetually* (a failure mode indistinguishable from a real platform bug). An HTML-escaped `&lt;loop-wake&gt;` is fine — the classifiers unescape before matching; absence and position are what they cannot rescue. The `<loop-wake>` marker is authoritative — NOT the harness `promptSource` field (unreliable across Claude Code versions). Verify with `CronList`-after-arm — and note that this paragraph only *explains* the mechanism; the `CronList`-after-arm check is the actual gate. Understanding why the marker matters has never been what prevents a missing one.
7. **Managed Codex uses its own persistent wake path.** Do not treat missing Claude-only Monitor, ScheduleWakeup, or CronList tools as a blocker and do not imitate them with detached stdout-only scripts. Verify the deployed `codex-umwelt-wake.py` managed-app-server bridge and the per-agent systemd heartbeat timer, and set the loop marker to `pacer: "systemd"`; the ScheduleWakeup Stop hook intentionally ignores non-ScheduleWakeup pacers. Ordinary heartbeat mode must use dynamic current-thread discovery rather than a persistent explicit `CODEX_THREAD_ID`, so a fresh session is not stranded on a stale thread; reserve explicit binding for a bounded loop that genuinely requires deterministic same-thread routing.

