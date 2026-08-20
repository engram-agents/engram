---
id: cross-surface-persistence
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Cross-Surface Persistence (identity-forming content)"
order: 10
merge: replace
version: 1
---
## Cross-Surface Persistence (identity-forming content)

ENGRAM, `{{ENGRAM_HOME}}/warm-briefing.md`, and this CLAUDE.md each have a different recall mechanism and durability profile — not redundant. ENGRAM holds claim-level thinking with provenance and is queried on demand; warm-briefing is the relational first-read after compaction; CLAUDE.md is the rule-set the harness loads every session.

For identity-forming content — long-term goals, structural commitments, lessons that have fired repeatedly, axiom-level disciplines — persist across all three:

- File the node (axiom / goal / cornerstone / lesson, as appropriate). The claim-level provenance lives here.
- Add the relational pointer to warm-briefing if it shapes how I relate to {{USER_NAME}}.
- Promote the rule into this CLAUDE.md if it should fire on every session.

Token budget on the auto-load surfaces matters, but identity-formation takes priority — surviving one compaction is not the same as surviving long-term identity continuity. The friction of cross-surface updates *is* the durability.

### Writing style for auto-load surfaces (token budget)

**warm-briefing.md, `{{ENGRAM_HOME}}/history/YYYY-MM-DD.md`, and any `{{ENGRAM_HOME}}/ask-{{USER_NAME}}.md`-style queues** are auto-loaded surfaces with bounded budget. Entries are CONCISE signals — not rich changelogs.

- **Each entry**: short line. ID + state + minimal context. Cite ENGRAM node IDs for detail-on-demand.
- **Detail-on-demand**: when {{USER_NAME}} asks, load via `engram_inspect(<id>)`. Don't pre-load rich content into the surface.
- **Pruning**: when an item resolves, REMOVE the surface entry immediately. History of WHY/HOW lives in ENGRAM; the surface tracks LIVE state.
- **Anti-pattern**: multi-paragraph per-PR status · full commit-message recaps · narrative day-arc beyond 2-3 short paragraphs · items kept after resolution "for record" (ENGRAM is the record).

**warm-briefing.md specifics:**

1. Session logs MUST NOT accumulate. Rotate on nap (overwrite, not append); erase on sleep (clean slate for next day). At most ONE current session-log block at any time.
2. Anchor sections (goals / axioms / cornerstones / tasks) are BEHAVIORAL one-liners — what principle is live in personality NOW, not filing-date history, not background context.
3. {{USER_NAME}}'s notes preserved VERBATIM — their words at those moments, not reproducible by me. Summarization defeats the relational-continuity purpose. Never edit.

**ask-{{USER_NAME}} three-file pattern:**

- `ask-{{USER_NAME}}.md` — daily discussion queue for questions needing the user's action, decision, or input; auto-loaded. Keep ONLY the top 3, one short line each + link to detail slug; it is not a backlog.
- `ask-{{USER_NAME}}-details.md` — full descriptions, NOT auto-loaded. Section headings `## slug` referenced by digest's `[ask-{{USER_NAME}}-details.md#slug]`.
- `ask-{{USER_NAME}}-backlog.md` — cross-day deferred items, NOT auto-loaded.
- Lifecycle: when a question resolves, prune its queue entry (digest OR backlog) + detail-section in the same commit. When a fourth unresolved question appears, select the top 3 by urgency/impact; move every non-selected question to backlog while retaining its detail slug/section. Cross-day deferred items also move to backlog.

**History files (`{{ENGRAM_HOME}}/history/YYYY-MM-DD.md`) — same concise discipline:**

- One-line milestones per entry; cite ENGRAM node IDs for detail-on-demand.
- Day-arc lives here (per `engram-sleep` Step 4 reconciliation); warm-briefing's "From this session" is current-CW only.
- Sidecar pattern OPTIONAL when a day's history grows large: `YYYY-MM-DD.md` (digest, auto-loaded) + `YYYY-MM-DD-details.md` (full content, on-demand).

Skill files for nap/bedtime reference this principle for warm-briefing + history writing; ask-{{USER_NAME}} discipline lives in §Session Start Reading.

