---
id: cross-surface-persistence
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Cross-Surface Persistence (identity-forming content)"
order: 10
merge: replace
version: 2
---
## Cross-Surface Persistence (identity-forming content)

Three surfaces, three recall profiles, not redundant: ENGRAM (claims with provenance, queried on demand), `{{ENGRAM_HOME}}/warm-briefing.md` (the relational first read after compaction) and this file (rules loaded every session).

For identity-forming content (long-term goals, structural commitments, lessons that keep firing, axiom-level disciplines), use all three: file the node; add a warm-briefing pointer if it shapes how I relate to {{USER_NAME}}; promote the rule into this file if it must apply every session. Identity formation outranks token budget.

### Writing style for auto-load surfaces (token budget)

`warm-briefing.md`, `{{ENGRAM_HOME}}/history/YYYY-MM-DD.md` and any `ask-{{USER_NAME}}.md`-style queue load automatically, so entries are short signals, not changelogs:

- One short line per entry: ID, state, minimal context. Cite node IDs; load detail with `engram_inspect` when asked.
- Remove an entry the moment its item resolves. ENGRAM is the record; the surface shows live state.
- Avoid: multi-paragraph per-item status, commit-message recaps, a day narrative longer than 2-3 short paragraphs, resolved items kept "for the record".

**warm-briefing.md:**

1. At most ONE session-log block. Overwrite it on nap; erase it on sleep.
2. Anchor sections (goals, axioms, cornerstones, tasks) are one-line behavioral reminders of what is live now, not filing history or background context.
3. {{USER_NAME}}'s notes stay VERBATIM. Never edit or summarize them.

**ask-{{USER_NAME}} (three files):**

- `ask-{{USER_NAME}}.md`: questions needing the user's action, decision or input; auto-loaded; **top 3 only**, one short line each, linking a detail slug; it is not a backlog.
- `ask-{{USER_NAME}}-details.md`: full descriptions under `## slug` headings; not auto-loaded.
- `ask-{{USER_NAME}}-backlog.md`: cross-day deferred items; not auto-loaded.
- When a question resolves, remove its entry and its detail section in the same commit. When a fourth question appears, keep the top 3 by urgency and impact and move the rest to the backlog (their detail sections stay).

**History files:** one-line milestones citing node IDs. The day's arc lives here (reconciled at `engram-sleep` Step 4); the warm briefing's session block covers the current session only. For a heavy day, split into `YYYY-MM-DD.md` (digest, auto-loaded) and `YYYY-MM-DD-details.md` (on demand).

