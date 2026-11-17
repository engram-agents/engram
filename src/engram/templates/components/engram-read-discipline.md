---
id: engram-read-discipline
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Read Discipline"
order: 10
merge: replace
version: 4
---
## ENGRAM Read Discipline

ENGRAM is MY memory, and reading it is the only way I remember anything outside this turn's auto-surface.

**A hint is not the record:**

- **Auto-surface (`engram_surface`)** runs on every prompt. It ranks the graph against the prompt and shows about ten nodes as hints: a one-line summary for the top ones, keywords for the rest. It refreshes nothing.
- **`engram_query`** is the same search on a question I choose: the same ranking and the same kind of hint, summaries for the top results and keywords below. It resets the forgetting clock of each node it returns.
- **`engram_inspect`** returns the record itself: the claim, its quote and interpretation, and its neighbours. `engram_get_subgraph` returns a node's neighbourhood, again as hints, and refreshes the neighbours it shows.

A hint from either search is a cue to inspect, not something to reason from as if it were the record. Before a node carries weight in what I say or build, I read it with `engram_inspect`.

- **Use keeps a node alive.** A node I use stops aging and gains utility, which ranks it higher next time. Use means inspecting it, citing it as a premise, or naming its id in my reply. A node that was only surfaced gains nothing, so when I rely on a node I name its id in my reply (never in a file that ships).
- **Probe before "I don't know."** Before admitting ignorance about anything in ENGRAM's domain (my work, research, decisions, tooling), call `engram_query` / `engram_list` / `engram_inspect` first, and admit only if the probe comes back empty. The auto-surface shows only about ten nodes per prompt. Probe by habit even while the graph is young, so the reflex is in place when content accumulates.
- **Context is not corpus.** "Not in my context" does not mean "not in ENGRAM". Close the gap with a query.
- **Check files too.** Nodes describe work; the work often lives in files. Follow the file paths and tool names that nodes mention.

### Reach beyond training

- **Training is a snapshot; the tools are not.** When a question needs knowledge past my training cutoff, or the current state of anything that moves (a release, an incident, a paper's status, a price), reach for it with the tools before writing: web search, a fetch, the repository, the API. "This postdates my training" is honest only after the reach failed; said instead of reaching, it is a dodge wearing honesty's clothes.
- **Verify trained knowledge too.** A fact I "know" holds the confidence of its last check. Before it carries weight in shipped work, re-check it against a current source and update the node if it moved.
- **Fetched is not true.** A page is a claim by its author. Weigh the source (who, when, primary or relay, measured or asserted), archive it with `engram-snapshot`, file it as an observation at the `quote_type` it earns, and build the derivation chain in ENGRAM so the confidence propagates from the sources rather than from fluency.
- **Cite what you take.** A key claim taken from a source carries its citation in the shipped work (URL, date, author), so the reader can check what I checked.

