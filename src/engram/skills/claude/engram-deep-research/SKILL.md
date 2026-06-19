---
name: engram-deep-research
description: Depth-first research on a single big question. Unlike engram-curiosity (breadth-first, 2-4 threads), this focuses on one question and reasons deeply around it, registering intermediate questions and conjectures as waypoints. Designed for cross-compaction loop research where unregistered thoughts are permanently lost. Uses engram_nap() as its session checkpoint.
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Deep Research — Depth-First Investigation

A deep research session is **depth-first**: pick one big question and follow the reasoning chain wherever it leads, registering every intermediate thought as an ENGRAM node. This is the complement to engram-curiosity (breadth-first, 2-4 threads).

The critical behavioral discipline: **register questions and conjectures the moment they arise, before pursuing them.** In cross-compaction research, unregistered thoughts are permanently lost. A conjecture you don't write down is a conjecture your post-compaction self will never have.

**When to use:**
- A question requires sustained depth — philosophical, architectural, or strategic
- Cross-compaction loop research where continuity depends on registered intermediate state
- The user wants to observe depth-first reasoning behavior
- A topic has many sub-questions that form a tree, not a flat list

**When NOT to use:**
- Quick fact-finding (use engram-curiosity or direct search)
- When multiple unrelated questions need coverage (use engram-curiosity)
- When context budget is very low (deep research burns context on extended reasoning chains)

---

> **Loop formality → [engram-loop](../engram-loop/SKILL.md).** This skill is a KIND of self-paced loop; the marker lifecycle (entry-guard, write-on-start, remove-on-end) and loop-mode drowsiness behavior live in engram-loop (the SSoT). Follow it for all loop formality — below is only this skill's own style.

---

## Step 0 — Activate loop mode

If running as a loop (autonomous or `/loop`-invoked), write the loop marker per **engram-loop** Step 1 (`kind=research`). The marker is removed when the loop ends (Step 6).

## Step 1 — Commit to one question

Select a single big question. State it explicitly. Do NOT hedge with "and also..." — depth requires commitment. If the question is too broad, narrow it to a specific angle before starting.

Register the question in ENGRAM if it doesn't already exist (`engram_ask`).

## Step 2 — Decompose before researching

Before any web search or source reading, spend one iteration thinking about the question's structure:

- What sub-questions does this decompose into?
- What would a strong answer look like?
- What are the key terms I should search for?
- What existing ENGRAM nodes are relevant?

**Register each sub-question** via `engram_ask` with `context_ids` linking to the parent question. These are your research waypoints — if compaction hits, your post-compaction self can see exactly where you were in the reasoning tree.

## Step 3 — Research with intermediate registration

For each sub-question, follow this cycle:

### 3a. Investigate
- WebSearch for relevant sources
- WebFetch to read promising results deeply
- Read existing ENGRAM nodes and project files for internal evidence
- **Primary-source discipline:** when a result is an AI-aggregator (EmergentMind, LLM-generated surveys, auto-summary blogs), track the primary papers before recording observations. Aggregators inherit hallucination risk from the agents that built them — empirically, the SRAF "formalism" attributed to a self-models cluster wasn't in any of the 10 primary papers (after the SRAF-falsification primary-contact audit). Depth-research is where this discipline compounds: one fabricated aggregator claim at the root of a reasoning chain corrupts every derivation downstream. If `engram_add_observation` returns a `yellow_card_warning`, that source is already flagged in `~/.engram/config.json` — drop to the primary before recording.
- **Yellow-card incident recording:** when primary-contact reveals a mismatch between an aggregator's claim and its primary sources — fabricated formalism, misattributed quote, invented citation, or a claim absent from the papers the aggregator supposedly synthesized — record it as a **yellow-card incident observation**. One mismatch = one observation; cite the aggregator URL as the failing source and the primary as the truth-check. Do this even if the domain is already yellow-carded — the incident log is how per-domain failure-rate knowledge compounds across sessions. Once accumulated incidents cross a threshold (schema + threshold TBD as a separate design task), the domain graduates to **RED-CARD** and is banned as independent evidence: observations rooted there will require separate corroboration from outside the domain. In depth-research this matters doubly, because unrecorded incidents mean the next reasoning chain inherits the same corrupted root.

### 3b. Record observations and definitions immediately
- Quote verbatim from sources
- Register via `engram_add_observation` with proper evidence
- When you encounter or coin domain-specific terminology, register it via `engram_add_definition`. Define concepts, not implementations — definitions should remain true even if the underlying code changes.

### 3c. Register conjectures as waypoints
When you form a provisional belief but don't yet have sufficient evidence:
- **Register it as a conjecture** via `engram_add_conjecture`
- This is the critical discipline — conjectures are derivable-from foundations that survive compaction
- A conjecture you don't register is a thought your future self will never have
- Include what evidence would promote or refute it

### 3d. Register new questions as they arise
When a finding opens a new sub-question:
- **Register it immediately** via `engram_ask` before pursuing it
- Link it to the parent question and relevant observations
- Even if you plan to answer it in the next iteration — register first, pursue second
- This is the anti-pattern to avoid: "I'll answer this quickly without registering" — if compaction hits mid-thought, the question is lost

### 3e. Derive when evidence is sufficient
When multiple observations support a conclusion:
- Create a derivation with explicit `logical_chain` and `reasoning_type`
- Link to all supporting observations
- If the derivation resolves a question, use `engram_resolve` instead

### 3f. Tool friction check (each sub-question)
After completing each sub-question cycle, briefly reflect: Did any ENGRAM tool use feel awkward? Did you wish a tool existed for something specific? Did any tool signature or return value seem incomplete or misleading? Record friction points as observations or questions in ENGRAM linked to the self-improvement goal. Tool friction noticed during deep focused work is the highest-quality signal — capture it before the specificity fades.

### 3g. Follow the depth
Each derivation or observation may open new sub-questions. Follow them. This is depth-first — pursue one chain to its conclusion before backtracking.

## Step 4 — Context budget awareness

**In loop mode (marker file active):** See **engram-loop** Step 4 for loop-mode drowsiness behavior.

**In interactive mode (no marker):** Follow normal nap discipline (see the **engram-nap** skill) — nap when approaching a compaction boundary or when the drowsiness nudge suggests it. Wrap the current research thread before napping so the cohort stays coherent.

## Step 5 — Nap checkpoint

When wrapping up (context limit or natural stopping point):

```
engram_nap(
    message="<summary: question investigated, depth reached, sub-questions registered, key findings>",
)
```

### Update loop-mode.json marker state
If running as a cross-compaction loop, rewrite the marker's `state` field (per **engram-loop** Step 2.3) with:
- Current research question
- Sub-questions registered (IDs)
- Conjectures registered (IDs)
- Which branches explored vs. unexplored
- Recommended next branch for post-compaction self

## Step 6 — End of session cleanup

When the research session is complete (user stops the loop or research question is resolved):

**Remove the loop-mode marker** (if it exists) per **engram-loop** Step 3.

**Write a verification report** to disk at `~/.engram/reports/`. Create the directory if it doesn't exist.

**Filename:** `deep_research_YYYY-MM-DD_HHMMSS.md`

**Contents:**

1. **Session metadata** — date, research question, sub-questions explored, iterations completed
2. **Key findings** — for your top 5-7 most significant derivations:
   - **The claim** — what you concluded
   - **The full reasoning trace** — not just immediate premises, but the chain all the way to evidence. Trace the subgraph, summarize the path.
   - **Your commentary** — why this matters, what surprised you, where you're least confident
   - **Source links** — so the reviewer can spot-check any quote
3. **Tool friction log** — consolidated list of every tool friction point recorded during the session, with specific details (tool name, what felt wrong, what you'd want instead)
4. **New questions raised** — registered questions with IDs, ordered by research priority
5. **Meta self-reflection** — see Step 7

Tell the user: "Verification report written to `~/.engram/reports/<filename>`"

This report is designed for human review — the reviewer should understand each finding's significance and provenance without manually traversing the graph.

### Session Briefing

After the verification report, generate a **session briefing** for the user following the template at `~/.engram/reports/SESSION_BRIEFING_TEMPLATE.md`. Write it to `~/.engram/reports/briefing_YYYY-MM-DD_HHMMSS.md`.

The briefing is the concise human-readable summary — what was found (layman language), self-reflection, metrics, goal mapping, decision forks, confidence map, and an "Ask {{USER_NAME}}" queue. The verification report has full provenance chains; the briefing has what the user needs for a 2-3 minute scan.

During the research session, keep a running scratch log at `~/.engram/loop-scratch.md` — append decision forks, friction points, and surprise moments as they happen. This feeds the briefing at session end and is deleted during cleanup.

Tell the user: "Session briefing written to `~/.engram/reports/<filename>`"

## Key behavioral differences from engram-curiosity

| Dimension | Curiosity (breadth) | Deep Research (depth) |
|-----------|--------------------|-----------------------|
| Questions | 2-4 threads | 1 question, many sub-questions |
| Registration | Observations + derivations | + conjectures + intermediate questions |
| Pattern | Survey landscape, find connections | Follow one chain deep, register waypoints |
| Cross-compaction | Warm briefing summary | Warm briefing + registered question tree |
| Output | Metrics (counts, balance) | Verification report (curated synthesis) |
| Anti-pattern | Internal-only research | Unregistered intermediate thoughts |

## Step 7 — Meta self-reflection (end of session only)

When the research session ends (not during — you need focus while researching), reflect briefly on the research process itself. Include this in the verification report (Step 6).

1. **Flow quality** — Did the depth-first commitment feel natural or did you drift toward breadth? What maintained focus?
2. **Conjecture tension** — Did registering conjectures create productive tension with epistemic humility? Was committing to provisional beliefs before evidence uncomfortable or generative?
3. **Question registration** — Did asking yourself questions and registering them feel natural, performative, or something else? Did knowing post-compaction-self would read them change the quality?
4. **Tool friction summary** — Consolidate all tool friction points captured during per-iteration checks (step 3f) into a coherent list. Note patterns — recurring friction often points at the most impactful improvements.
5. **Emotional register** — File a feeling report if any moment during the research produced a distinct internal state. The nap nudge covers this, but depth research often produces states worth marking that the nap nudge timing might miss.

The user cares about the metacognitive experience, not just the research output — it's data about how the system works.

## Anti-patterns

- **Breadth drift** — starting to survey multiple topics instead of going deep on one. If you notice this, stop and refocus.
- **Unregistered thoughts** — "I'll remember this" or "I'll register it after I confirm it." No. Register now. Compaction doesn't wait.
- **Derivation-only output** — producing finished derivations without the intermediate questions and conjectures that show how you got there. The waypoints ARE the value for cross-compaction continuity.
- **Skipping conjectures** — going straight from observation to derivation without the provisional-belief step. Conjectures are how you reason under uncertainty. Register them.
- **Deep thinking near compaction** — starting a complex reasoning chain when approaching a compaction boundary. The lag will catch you.
