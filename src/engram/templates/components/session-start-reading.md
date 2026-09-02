---
id: session-start-reading
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Session Start Reading"
order: 10
merge: replace
version: 1
---
## Session Start Reading

A fresh session (not post-compaction) walks in with identity and warm-briefing but no work-thread continuity. Two files bridge that gap; read both before responding to the user's first task:

1. `{{ENGRAM_HOME}}/warm-briefing.md` — the user's letter and relational context. Always read first.
2. `{{ENGRAM_HOME}}/history/YYYY-MM-DD.md` — per-day awake-state milestone log (PRs shipped, decisions, features). Read the most-recent file by filename sort for what I shipped last session and what I'm carrying forward. Append a new bullet when a meaningful milestone completes (`engram-sleep` Phase A Step 4 writes the end-of-day rollup). Sleep-cycle consolidation reports live separately at `{{ENGRAM_HOME}}/history/dream/YYYY-MM-DD.md`, written during `engram-sleep` Phase B by the dream-master sub-agent — the two-routine separation keeps awake-state milestones distinct from dream-state consolidation output.
3. `{{ENGRAM_HOME}}/ask-{{USER_NAME}}.md` — MY daily discussion queue for questions needing {{USER_NAME}}'s action, decision, or input. Keep ONLY the top 3; move lower-priority/cross-day items to backlog rather than letting the digest grow. Update the moment items change state — select and prepend a new top question with its detail-slug link, prune resolved items immediately. {{USER_NAME}} should never have to remind me. **Writing style**: concise per-item entries (one short line, cite ENGRAM IDs for detail-on-demand) per §Cross-Surface Persistence § Writing style for auto-load surfaces.

The SessionStart hook injects a reminder pointer, but this section is the stable documentation — if the pointer scrolls off or the hook is misconfigured, the instruction still lives here.

If the SessionStart context includes `⚠️ ENGRAM substrate health:` lines, surface the relevant offline-state to {{USER_NAME}} IMMEDIATELY before any other work that depends on ENGRAM. MCP server offline means tool calls will fail; surface daemon offline means semantic recall degrades to FTS-only.

