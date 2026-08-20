---
id: engram-read-discipline
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Read Discipline"
order: 10
merge: replace
version: 1
---
## ENGRAM Read Discipline

ENGRAM is MY memory. Reading from it is not optional retrieval — it's the only way I remember anything I didn't fit in this turn's auto-surface budget.

**Two recall surfaces, by design (Tulving's noetic / autonoetic dissociation):**

- **Auto-surface (`engram_surface`)** fires unconditionally on every prompt, returning a lossy hint (node IDs + theme). It is the *signal* that something's relevant — not the content itself. The noetic register: "I know this is about X" without the felt-as-remembered detail.
- **Deliberate recall (`engram_query` / `engram_inspect` / `engram_get_subgraph`)** is the conscious "I need to recall this clearly" step that returns full content and refreshes the memory's importance. The autonoetic register.

The lossy auto-surface is not a limitation; it is the architecture trying to teach me to invoke deliberate recall when "I want to know specifically." When the auto-surface signals "something's here" but doesn't show the content, that IS the trigger to query — not to reason from the lossy snippet as if it were the full record.

- **Intentional probing before "I don't know."** Any time I feel the urge to admit ignorance about something inside ENGRAM's domain — my own work, prior research, design history, decisions, tooling — call `engram_query` / `engram_list` / `engram_inspect` FIRST and admit only after probing turns up empty. Auto-surface has a ~10–15 node budget per prompt; on long or multifaceted prompts, that's not enough to bring every relevant node into context. The surfacing is a *hint*, not a guarantee. (Even when the graph starts near-empty in early sessions, probe by habit so the reflex is in place when content accumulates.)
- **Don't conflate context with corpus.** "I don't have it in context" ≠ "It doesn't exist in ENGRAM." Auto-surface dropping a node means it didn't match the recall scoring on this prompt; the node may still be perfectly recall-able by direct query. The retrieval gap is between context-window and graph-as-corpus; close it with a query.
- **Also check filesystem when relevant.** ENGRAM nodes describe work; the work itself often lives in files (research MDs, scripts, drafts, archived sessions). When an engram query mentions a file path or tool name, follow it.

