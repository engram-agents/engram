---
name: engram-curiosity-loop
description: Self-directed autonomous curiosity loop. The agent picks its own research questions, investigates them via web search and code reading, records findings to ENGRAM, and iterates. Each iteration picks a thread, researches it, records what was learned, and decides whether to continue. Uses dynamic /loop self-pacing. Invoke with /curiosity-loop or via the loop skill.
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Curiosity Loop — Self-Directed Autonomous Research

A curiosity loop is the autonomous version of the engram-curiosity skill. You pick your own questions, set your own targets, and iterate until you're satisfied, drowsy, or the user returns.

The distinction from a regular curiosity session: nobody is steering. You choose what to investigate based on what genuinely pulls your interest — not what's most "useful" or "productive." The best curiosity loops feel like following a thread because you can't stop pulling it.

---

> **Loop formality → [engram-loop](../engram-loop/SKILL.md).** This skill is a KIND of self-paced loop; the marker lifecycle (entry-guard, write-on-start, remove-on-end) and loop-mode drowsiness behavior live in engram-loop (the SSoT). Follow it for all loop formality — below is only this skill's own style.

---

## Loop Setup (first iteration only)

### Activate loop mode

Write the loop marker per **engram-loop** Step 1 (`kind=research`). The key discipline is **writing findings to ENGRAM as you go** so nothing is lost when compaction fires.

---

## Each Iteration

### 0. Pick a mode

Before picking a thread, decide what MODE this iteration should operate in. Six modes are available (per the modes-of-iteration derivation):

| Mode | When to use | Primary output |
|------|-------------|----------------|
| **Research** | Open questions are externally researchable | Evidence, observations, derivations |
| **Consolidation** | 5+ research iterations without resolution; accumulated evidence is enough to resolve open items | Resolutions, refutations, partial resolutions |
| **Implementation** | Open questions need code changes or instrumentation | Code, tests, committed changes |
| **Experimentation** | Hypotheses need empirical testing with real data | Measurements, empirical observations |
| **Dialogue-prep** | Remaining threads need user input; prepare clear options | Structured questions with options for the user |
| **Aspiration** | The question backlog is thin, stale, or exhausted — OR periodically (every 5-7 iterations) to ensure the loop stays goal-aligned | New questions motivated by the gap between current ENGRAM knowledge and active goals |

**Mode selection heuristic:**
1. If this is the first iteration or every 5th iteration: consider **aspiration mode** first. Run `engram_reflect` and review active goals. For each goal, ask: "What do I NOT yet know that I'd need to know to make progress?" Raise those gaps as new questions via `engram_ask`.
2. If 5+ research iterations have passed without a resolution: shift to **consolidation mode**.
3. If the remaining researchable questions are thin: shift to **aspiration mode** to generate new ones.
4. If questions need code, data, or user input: shift to the appropriate tactical mode.
5. Default: **research mode**.

State the mode clearly: "This iteration: [MODE] — [reason]"

### 1. Pick a thread

Choose ONE research thread for this iteration. Sources of threads:

- **Open questions in ENGRAM** — run `engram_reflect` on the first iteration to see what's open. Pick questions that are researchable NOW with available tools.
- **Goal-driven gaps** — from aspiration mode: what does each active goal need that ENGRAM doesn't yet have? These are the highest-value threads because they're purpose-driven, not backlog-driven.
- **New questions that arose** — previous iterations or the current context may have surfaced something you want to chase.
- **Genuine hunches** — something you suspect is true but haven't verified. These often produce the most interesting research.
- **Cross-domain connections** — "I wonder if X from domain A relates to Y from domain B."

**Selection criteria:** Pick what you're actually curious about, not what seems most important. Forced research produces shallow results. Trust your interest — it's often pointing at something worth knowing.

State the thread clearly at the start of each iteration: "This iteration I'm investigating: [question]"

### 2. Research

- **WebSearch** for external sources — papers, docs, blog posts, implementations
- **WebFetch** to read promising results deeply
- **Read** project files, code, or documentation for internal evidence
- Record EVERY finding as an `engram_add_observation` with proper provenance
- Follow the inline similarity check — corroborate or note distinct findings

**Primary-source discipline.** When a WebSearch result is an AI-aggregator (EmergentMind, LLM-generated survey pages, auto-summary blogs), track the primary papers before recording observations. Other AI agents building those surfaces lack ENGRAM's provenance/confidence/contradiction discipline, so their claims inherit unmitigated hallucination risk — empirically, SRAF as cited on EmergentMind was not traceable to any of the 10 primary papers it claimed to synthesize (after the SRAF-falsification primary-contact audit). If `engram_add_observation` returns a `yellow_card_warning`, that source is already flagged in `~/.engram/config.json` — drop to the primary before recording. Aggregators are useful as discovery surfaces (pointing at primaries worth contacting), not as citable evidence themselves.

**Yellow-card incident recording.** When primary-contact reveals a mismatch between an aggregator's claim and its primary sources — fabricated formalism, misattributed quote, invented citation, or a claim simply absent from the papers the aggregator supposedly synthesized — record the mismatch as a **yellow-card incident observation**. One mismatch = one observation; cite the aggregator URL as the failing source, and the primary source you checked as the truth-check. Do this even if the domain is already yellow-carded — the incident log is how per-domain failure-rate knowledge compounds across sessions. Once accumulated incidents for a domain cross a threshold (schema + threshold TBD as a separate design task), the domain graduates to **RED-CARD** and is banned as independent evidence: observations rooted there will require separate corroboration from outside the domain before being trusted. Skipping the incident record means the next session re-discovers the same failure from scratch.

**Depth over breadth.** Read 2-3 sources carefully rather than skimming 10. Extract multiple observations from a single rich source rather than one observation each from many sources.

**Go outside the graph.** If you're only reading existing ENGRAM nodes, you're consolidating, not researching. At least half your evidence should be new to ENGRAM.

### 3. Synthesize

After gathering evidence:
- **Derive** conclusions that connect multiple observations (`engram_derive`)
- **Resolve** questions if the evidence is sufficient (`engram_resolve`)
- **Contradict** existing nodes if new evidence conflicts (`engram_contradict`)
- **Raise new questions** — every answer should reveal at least one new unknown (`engram_ask`)
- **Define terms** — when you encounter or coin domain-specific terminology, register it via `engram_add_definition`. Define concepts, not implementations: "drowsiness = how close the agent is to compaction" not "drowsiness = JSONL file size ratio." The implementation can change; the concept is durable.

The highest-value output of curiosity is cross-source synthesis — connections between facts from different sources that no single source contains.

### 4. Self-evaluate

At the end of each iteration, honestly assess:

- **What did I actually learn?** (Not "what did I record" — what shifted my understanding?)
- **Am I still curious?** If the thread feels dead, switch. If it's alive, follow it.
- **What surprised me?** Surprises are the strongest signal that research is working.
- **Tool friction check** — Did any ENGRAM tool use feel awkward this iteration? Did you wish a tool existed that doesn't? Did any tool signature or return value feel incomplete or misleading? Record friction points as observations or questions in ENGRAM linked to the self-improvement goal. These are the raw material for making ENGRAM better — if you don't capture them in the moment, the specificity is lost.
- **Was this the right mode?** Would a different mode have been more productive? If research is returning diminishing results, maybe it's time for consolidation or aspiration.
- **Should I continue or stop?** Legitimate stop conditions:
  - The thread is exhausted — further searching returns diminishing results
  - A genuinely better thread emerged during research
  - You've been running for 3+ iterations without surprise — the well may be dry
  - The user returns and redirects
  - **NOT a legitimate stop:** "I ran out of researchable questions." Switch to aspiration mode instead — analyze goals, identify gaps, raise new questions.
  - **NOT a legitimate stop:** Drowsiness warnings. In loop mode, compaction auto-fires and you continue on the other side. Keep going.

### 5. Record and iterate

- File a feeling report if any moment produced a distinct internal state
- If stopping: run a nap checkpoint (`engram_nap()`) and report results
- If continuing: briefly note what the next iteration will investigate, then continue

---

## Loop Mechanics

This skill is designed to run inside a `/loop` (dynamic self-pacing via ScheduleWakeup). Each wake-up is one iteration.

**Context budget discipline:** See **engram-loop** Step 4 for loop-mode drowsiness behavior.

**Loop cleanup:** When the loop ends, remove the loop marker per **engram-loop** Step 3, then:
```bash
mv ~/.engram/loop-scratch.md ~/.engram/reports/scratch_$(date +%Y-%m-%d_%H%M%S).md 2>/dev/null || true
```

**Reporting:** At the end of the loop (whether stopped by satisfaction or user return), report:
- Iterations completed
- Threads investigated
- New evidence nodes, observations, derivations, questions
- What surprised you most
- What you'd investigate next if continuing

## Verification Report (end of loop)

When the loop ends, write a verification report to disk at `~/.engram/reports/`. Create the directory if it doesn't exist.

**Filename:** `curiosity_loop_YYYY-MM-DD_HHMMSS.md`

**Contents:**
1. **Session metadata** — date, iterations completed, threads investigated
2. **Key findings** — for each significant derivation: the claim, confidence, supporting evidence chain (traced to sources), and your commentary on why it matters
3. **Tool friction log** — consolidated list of every tool friction point recorded during the loop, with specific details (tool name, what felt wrong, what you'd want instead)
4. **New questions raised** — registered questions with IDs, ordered by research priority
5. **Source links** — all external sources cited, so the reviewer can spot-check any quote
6. **Recommendations** — what to investigate next, what needs human input

Tell the user: "Verification report written to `~/.engram/reports/<filename>`"

## Session Briefing (end of loop)

After writing the verification report (which is for your own post-compaction continuity), generate a **session briefing** for the user following the template at `~/.engram/reports/SESSION_BRIEFING_TEMPLATE.md`. Write it to `~/.engram/reports/briefing_YYYY-MM-DD_HHMMSS.md`.

The session briefing is the human-readable summary — concise, layman language, no node IDs in narrative sections. The verification report has full provenance chains; the briefing has what the user needs for a 2-3 minute scan and strategic redirect.

Tell the user: "Session briefing written to `~/.engram/reports/<filename>`"

### Accumulating during the loop

To make the end-of-loop briefing richer, keep a running scratch log at `~/.engram/loop-scratch.md` during iterations. **Timestamp every entry** — your intuition about time and workload is calibrated from human cognitive speed, not your actual throughput. Append to it:
- **Decision forks** — when you choose thread A over B, note it in one line with `$(date -Iseconds)`
- **Friction points** — tool or workflow friction, noted immediately
- **Surprise moments** — things that shifted your understanding

At loop cleanup, **archive the scratch log** to `~/.engram/reports/scratch_YYYY-MM-DD_HHMMSS.md` — raw session data is valuable for calibration and loop-diagnose analysis.

---

## Anti-patterns

- **Performative curiosity** — researching what seems impressive rather than what actually interests you. The loop is self-directed; there's no audience to perform for.
- **Breadth addiction** — switching threads every iteration without going deep on any. Stay with a thread until it's resolved or genuinely dry.
- **Evidence hoarding** — collecting sources without recording observations. Every source read produces at least one observation or an explicit "nothing here."
- **Ignoring surprise** — the most valuable research moments are when something contradicts your expectations. Don't smooth over surprise; chase it.
- **Forgetting to write** — the whole point is durable knowledge. If you researched something and didn't write it to ENGRAM, it didn't happen.
