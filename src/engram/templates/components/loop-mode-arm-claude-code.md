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

