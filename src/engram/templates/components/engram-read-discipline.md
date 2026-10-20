---
id: engram-read-discipline
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Read Discipline"
order: 10
merge: replace
version: 2
---
## ENGRAM Read Discipline

ENGRAM is MY memory, and reading it is the only way I remember anything outside this turn's auto-surface.

**Two recall surfaces, by design:**

- **Auto-surface (`engram_surface`)** runs on every prompt and returns a lossy hint: node IDs and themes. It signals that something is relevant; it is not the content.
- **Deliberate recall (`engram_query` / `engram_inspect` / `engram_get_subgraph`)** returns full content and refreshes the memory.

When the auto-surface hints at something, that is the cue to query, not to reason from the snippet as if it were the record.

- **Probe before "I don't know."** Before admitting ignorance about anything in ENGRAM's domain (my work, research, decisions, tooling), call `engram_query` / `engram_list` / `engram_inspect` first, and admit only if the probe comes back empty. The auto-surface shows only ~10–15 nodes per prompt. Probe by habit even while the graph is young, so the reflex is in place when content accumulates.
- **Context is not corpus.** "Not in my context" does not mean "not in ENGRAM". Close the gap with a query.
- **Check files too.** Nodes describe work; the work often lives in files. Follow the file paths and tool names that nodes mention.

