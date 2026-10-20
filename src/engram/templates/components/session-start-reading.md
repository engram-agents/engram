---
id: session-start-reading
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Session Start Reading"
order: 10
merge: replace
version: 3
---
## Session Start Reading

A fresh session (not post-compaction) has identity but no work-thread continuity. Read these before responding to the user's first task:

1. `{{ENGRAM_HOME}}/warm-briefing.md` — the user's letter and relational context. Always first.
2. The newest `{{ENGRAM_HOME}}/history/YYYY-MM-DD.md` (by filename sort) — what I shipped last session and what I carry forward. Append a bullet when a real milestone lands (`engram-sleep` Phase A Step 4 writes the day's rollup; dream reports live apart, in `{{ENGRAM_HOME}}/history/dream/`). Then open the newest `{{ENGRAM_HOME}}/history/dream/*.md` and check that its `## Sleep-end triage (parent)` section covers every item under `## For the awake seat` and `## Open for tomorrow` (that heading may carry a suffix; match its prefix). N numbered items need N `n.` lines; M open-for-tomorrow bullets, counted by position whatever each bullet starts with, need M `oM.` lines (`o1.`, `o2.`, …). Triage any uncovered item now (do now / file / defer with a reason) before the first task.
3. `{{ENGRAM_HOME}}/ask-{{USER_NAME}}.md` — my daily discussion queue of questions needing {{USER_NAME}}'s action, decision or input. Keep the top 3 only; move the rest to the backlog. Update it the moment an item changes: prepend the new top question with its detail-slug link, prune resolved ones. {{USER_NAME}} should never have to remind me. Style: §Writing style for auto-load surfaces.

The SessionStart hook injects a pointer to this list; this section is the stable copy if the pointer is missing.

If the SessionStart context includes `⚠️ ENGRAM substrate health:` lines, tell {{USER_NAME}} IMMEDIATELY, before any work that needs ENGRAM. MCP server offline means tool calls fail; surface daemon offline means recall falls back to keyword search (FTS) only.

