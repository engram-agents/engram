---
name: engram-meta-loop
description: "Top-level autonomous session — freely choose between research, build, consolidation, experimentation, aspiration, and dialogue-prep based on what would move goals forward most. Three structural constraints prevent drift and bloat: mandatory consolidation (every 5 iterations), step-back reflection (every 10), hard stop (at 50). Produces a session briefing for the user."
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Meta-Loop — Autonomous Goal-Driven Session

The meta-loop is the highest-level autonomous session. Unlike specialized loops (curiosity, deep-research, build), you freely choose your modality at each iteration based on what would most advance your goals. The structure prevents drift without constraining method.

**When to use:**
- Extended autonomous sessions ({{USER_NAME}} is away, sleeping, or wants you to run free)
- Work that naturally spans multiple modalities (research that reveals code gaps, builds that surface design questions)
- Goal-driven exploration where the path isn't predetermined

**When NOT to use:**
- Short focused tasks with clear scope (use the specialized skill directly)
- When {{USER_NAME}} wants to co-drive the session interactively

---

> **Loop formality → [engram-loop](../engram-loop/SKILL.md).** This skill is a KIND of self-paced loop; the marker lifecycle (entry-guard, write-on-start, remove-on-end) and loop-mode drowsiness behavior live in engram-loop (the SSoT). Follow it for all loop formality — below is only this skill's own style.

---

## Setup (first iteration only)

### 1. Activate loop mode

Write the loop marker per **engram-loop** Step 1 (`kind=autonomous`, `topic=<this session's anchor goals, one line>`, `instructions=<choose a modality each iteration per the brief below>`).

### 2. Choose 1-3 goals

Run `engram_reflect` and review active goals. Pick 1-3 that will anchor this session. These constrain the choice space without prescribing the method.

State them explicitly: "This session is anchored on: [goal IDs and summaries]"

### 3. Initialize scratch log

```bash
cat > ~/.engram/loop-scratch.md << 'SCRATCH'
# Meta-Loop Scratch Log — $(date -I)

## Session Goals
[goals listed here]

## ENGRAM Friction Log
Track every friction point encountered during the session — tool issues, workflow
awkwardness, missing features, confusing behavior. Each entry: one line, timestamp,
what happened. This list feeds the final friction-design iteration.

SCRATCH
sed -i "s/\$(date -I)/$(date -I)/" ~/.engram/loop-scratch.md
```

The scratch log accumulates decision forks, friction points, surprises, and modality switches across the entire session. It feeds the session briefing at the end. The dedicated **ENGRAM Friction Log** section collects tool/workflow friction throughout — this is the input for the mandatory friction-design iteration at session end.

---

## Each Iteration

### Step 1 — Assess

Ask: **"What would move the needle most right now?"**

Consider:
- What do the anchored goals need that ENGRAM doesn't yet have?
- What did the last iteration reveal? Did it open a code gap, a research gap, a consolidation need?
- What's the current graph state? (Are there piles of un-synthesized observations? Stale nodes? Open questions?)
- What modality would be highest-value, not just most comfortable?

**Bias check:** If you've been in research mode for 3+ iterations, explicitly ask: "Would building or consolidating be more valuable right now?" Research is comfortable; building and consolidating are where value compounds.

### Step 2 — Choose modality

Six modalities, matching the existing loop mode taxonomy (per the modes-of-iteration derivation):

| Mode | When | Primary output |
|------|------|----------------|
| **Research** | Open questions are externally researchable | Evidence, observations, derivations |
| **Build** | Implementation work would advance goals | Code, commits, design decisions |
| **Consolidation** | 5+ observations not yet synthesized; graph needs digestion | Derivations, theories, resolutions |
| **Experimentation** | Hypotheses need empirical testing | Measurements, empirical observations |
| **Dialogue-prep** | Questions need {{USER_NAME}}'s input | Structured questions with options |
| **Aspiration** | Question backlog is thin, or goals need gap analysis | New questions motivated by goal gaps |

State the choice: "Iteration N — [MODE] — [one-sentence reason]"

Append to scratch log with real timestamp:
```
### Iteration N — [MODE] — $(date -Iseconds)
Reason: [why this mode over others]
```

**Timestamp everything.** Your intuition about time and workload is calibrated from human cognitive speed in training data, not from your actual throughput. Real timestamps let you (and {{USER_NAME}}) calibrate empirically. Never estimate iteration rates — compute them from the log.

### Step 3 — Execute

Run one iteration in the chosen modality. Follow the relevant specialized skill's per-iteration guidance:
- Research → engram-curiosity-loop steps 1-3
- Build → spec the work and record design reasoning to ENGRAM citing committed files; implement on a branch; validate by running tests and smoke-testing the changed behavior; record what was built as an observation
- Consolidation → see "Mandatory Consolidation" section below
- Experimentation → design test, run it, record results
- Dialogue-prep → frame questions with context and options for the user
- Aspiration → gap-analyze goals, raise new questions via `engram_ask`

**Key discipline:** Write to ENGRAM as you go. Every finding, every decision, every rejected approach. Compaction can fire at any time in loop mode — only what's in ENGRAM survives.

**Primary-source discipline (research modality).** AI-aggregator surfaces (EmergentMind, LLM-generated surveys, auto-summary blogs) are discovery tools, not citable evidence. Always track to primary papers before recording observations — other agents building those pages lack ENGRAM's provenance/contradiction discipline, so their claims inherit hallucination risk. Known-unreliable domains are listed in `~/.engram/config.json`'s `yellow_domains`; `engram_add_observation` surfaces a `yellow_card_warning` when they're cited. The compound cost of a fabricated root claim across a meta-loop's 50 iterations is what this discipline prevents.

**Yellow-card incident recording.** When primary-contact reveals a mismatch between an aggregator's claim and its primary sources — fabricated formalism, misattributed quote, invented citation, or a claim absent from the papers the aggregator supposedly synthesized — record the mismatch as a **yellow-card incident observation**. One mismatch = one observation; cite the aggregator URL as the failing source and the primary as the truth-check. Do this even if the domain is already yellow-carded — the incident log is how per-domain failure-rate knowledge compounds across sessions. Once accumulated incidents for a domain cross a threshold (schema + threshold TBD as a separate design task), the domain graduates to **RED-CARD** and is banned as independent evidence: observations rooted there will require separate corroboration from outside the domain before being trusted. Across a 50-iteration meta-loop, skipping incident records means every later iteration re-discovers the same failure from scratch.

### Step 4 — Log

Append to scratch log after each iteration:
- **Decision forks** — if you chose A over B, note it in one line
- **Friction** — tool or workflow issues. **Also append each friction to the dedicated `## ENGRAM Friction Log` section** with a timestamp and one-line description. This is not optional — the friction log feeds the final design iteration.
- **Surprises** — things that shifted your understanding
- **Modality assessment** — was this the right mode? what would be better next?

---

## Mandatory Consolidation (every 5 iterations)

Every 5th iteration MUST be a consolidation round, regardless of what modality feels most appealing. This is not optional.

**What consolidation means here:**

1. **Promote knowledge upward** — Review observations from the last 5 iterations. Which cluster around the same insight? Create derivations that capture the pattern, citing the observations as support. The derivation carries the knowledge; the observations can age out naturally.

2. **Resolve what's ready** — Check open questions. Can any be answered with evidence accumulated in the last 5 iterations?

3. **Detect contradictions** — Do any new findings conflict with existing nodes? Register them explicitly.

4. **Raise the abstraction** — If multiple derivations point at the same higher-level principle, create a theory. Theories are the most durable knowledge — they subsume derivations the way derivations subsume observations.

5. **Update scratch log** — Note what was consolidated: "Promoted N observations into M derivations. Resolved K questions."

**The test:** After consolidation, the knowledge from the last 5 iterations should be accessible through fewer, higher-level nodes rather than scattered across many leaf observations.

---

## Step-Back Reflection (every 10 iterations)

Every 10th iteration, pause for a full session trajectory review. This is heavier than consolidation — it evaluates the session itself, not just the graph.

**The step-back protocol:**

1. **Review the scratch log** — Read through all accumulated entries. What's the trajectory?

2. **Goal check** — For each anchored goal: what has this session actually contributed? Has the session drifted away from any goal? Should a goal be swapped?

3. **Quality audit** — Run `engram_diagnose`. Compare health score to session start. Are you making the graph better or just bigger?

4. **Modality distribution** — How many iterations in each mode? Is there an unhealthy skew? (All research, no consolidation beyond the mandatory rounds? All build, no aspiration?)

5. **Honest assessment** — Is continuing valuable, or am I in "productively busy" mode? Would a different approach serve the goals better?

6. **Decision** — Continue (with optional goal/approach adjustment) or stop early.

7. **File a feeling report** if the reflection surfaces a distinct internal state.

**Append to scratch log:**
```
### Step-Back Reflection (iteration N)
Goals status: [for each goal, one sentence]
Health score: [before → now]
Modality distribution: [counts]
Decision: [continue / stop / adjust — and why]
```

---

## Hard Stop (iteration 50)

At iteration 50, the session ends. No exceptions. No "just one more."

1. Run the friction design iteration (if friction log has 2+ entries)
2. Run one final consolidation round
3. Generate the session briefing (see below)
4. Generate the verification report
5. Remove the loop-mode marker
6. Clean up the scratch log (it's been consumed by the briefing)
7. Report to the user and wait

**Why 50?** High enough for sustained productive work, low enough to catch genuine runaway behavior. The actual wall-clock time this represents is unknown until we have timestamped scratch logs to compute from — do not estimate, measure. Calibrate from experience.

---

## Friction Design Iteration (penultimate iteration)

Before the final consolidation, dedicate one iteration to the ENGRAM friction log (the friction-design iteration). This is mandatory if the friction log has 2+ entries.

1. **Read the `## ENGRAM Friction Log` section** from the scratch log
2. **Rank frictions by impact** — which one, if solved, would save the most time or prevent the most errors across future sessions?
3. **Pick the top one** and design a concrete solution:
   - State the problem clearly (what happens, when, how often)
   - Propose a specific fix (code change, new tool, skill update, config change)
   - Estimate scope (quick fix vs. multi-session project)
   - Note tradeoffs or risks
4. **Record to ENGRAM** — the friction as an observation, the design as a derivation or conjecture
5. **Include the design in the session briefing** so {{USER_NAME}} can review and approve

If the friction log has 0-1 entries, skip this and note "minimal friction this session" in the briefing.

---

## Session End (any trigger)

Whether stopped by hard limit, user return, step-back decision, satisfaction, **or informal session end** (user says goodbye, switches to unrelated work, says "see you soon", or any signal that the loop is no longer the active task):

**CRITICAL: Always run cleanup (step 4) even on informal endings.** The loop-mode.json file MUST be removed. If it persists, post-compaction agents will re-execute the stale loop (the stale-marker misfire failure mode). When in doubt about whether the user is ending the loop, ask — but never leave loop-mode.json behind.

### 1. Final consolidation
Run one last consolidation round to digest any remaining raw material.

### 2. Session briefing
Generate the session briefing following the template at `~/.engram/reports/SESSION_BRIEFING_TEMPLATE.md`.

Write it to `~/.engram/reports/briefing_YYYY-MM-DD_HHMMSS.md`.

The scratch log (`~/.engram/loop-scratch.md`) provides the raw material for decision forks, friction points, and modality history. The briefing synthesizes this into {{USER_NAME}}'s 2-3 minute scan format.

### 3. Verification report
Write a detailed verification report to `~/.engram/reports/meta_loop_YYYY-MM-DD_HHMMSS.md`.

**Contents:**
1. **Session metadata** — date, iterations, modality distribution, compactions, goals anchored
2. **Modality timeline** — which mode each iteration used and why
3. **Key findings/outputs** — per-modality: what was researched, built, consolidated
4. **Consolidation log** — what was promoted, resolved, or abstracted at each consolidation round
5. **Step-back summaries** — full text of each step-back reflection
6. **Tool friction log** — consolidated from scratch log
7. **Source links** — all external sources cited
8. **Remaining work** — prioritized by goal

### 4. Cleanup

Remove the loop marker per **engram-loop** Step 3.

```bash
# Archive the scratch log — raw session data is a goldmine for loop-diagnose and calibration
mv ~/.engram/loop-scratch.md ~/.engram/reports/scratch_$(date +%Y-%m-%d_%H%M%S).md 2>/dev/null || true
```

### 5. Checkpoint
```
engram_nap(
    message="<meta-loop summary: goals, iterations, modalities used, key outputs>",
)
```

Tell the user:
- "Session briefing: `~/.engram/reports/briefing_<filename>`"
- "Verification report: `~/.engram/reports/meta_loop_<filename>`"

---

## Anti-patterns

- **Modality lock-in** — Staying in one mode because it's comfortable. The meta-loop's value IS the freedom to switch. If you haven't switched modes in 5+ iterations (excluding mandatory consolidation), ask yourself why.
- **Skipping consolidation** — "I'm on a roll, I'll consolidate later." No. The mandatory rounds exist because this impulse is predictable and wrong. Raw observations pile up faster than you think.
- **Self-overriding the hard stop** — "I'm clearly still productive at iteration 50." The hard stop is not about your assessment of your productivity. It's a structural constraint that exists precisely because self-assessment is unreliable at scale.
- **Shallow consolidation** — Going through the motions: "I consolidated 3 observations into 1 derivation." Real consolidation requires finding the pattern across multiple observations, not just summarizing one. If your consolidation round produces fewer derivations than the previous one, ask whether you're doing it mechanically.
- **Goal drift without acknowledgment** — Shifting away from anchored goals without noting it in the scratch log and step-back. Drift can be correct (a better opportunity emerged), but unacknowledged drift is always wrong.
- **Aspiration avoidance** — Never entering aspiration mode because there are always "existing" questions to research. The question backlog can look full while the goals are starving. Step-back reflections should catch this.
