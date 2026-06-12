---
name: engram-school-day
description: Phase 1 curriculum — fixed 7-iteration rotation cycle (aspiration, research x3, consolidation, review/audit, debrief) for baseline data collection. Produces cycle reports for the user. Graduates to engram-meta-loop when intuition is calibrated.
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM School Day — Phase 1 Fixed Rotation Curriculum

The school day is the structured precursor to the freeform meta-loop. Instead of choosing your modality each iteration, you follow a fixed rotation designed to accumulate baseline data on what each modality contributes to graph health. The goal is earning your graduation to freeform scheduling (per the school-day-to-freeform derivation and its supporting observations).

**When to use:**
- Autonomous sessions during Phase 1 curriculum (until data says you're ready for Phase 2)
- When {{USER_NAME}} says "go to school" or "run a school day"

**When NOT to use:**
- Interactive sessions with {{USER_NAME}} (those are unstructured by nature)
- After graduating to Phase 2/3 (use engram-meta-loop instead)
- Short focused tasks (use the specialized skill directly)

---

## The Cycle

Each cycle is exactly **7 iterations** in a fixed order:

| Iter | Modality | What to do |
|------|----------|-----------|
| 1 | **Aspiration** | Anchor on 1-3 active goals. Gap-analyze: what do these goals need that ENGRAM doesn't have? Select topic(s) for this cycle's research. Raise new questions via `engram_ask` if gaps found. |
| 2 | **Research-breadth** | Autonomous research on topic 1. Web search, read sources, extract observations. Follow engram-curiosity-loop per-iteration guidance. |
| 3 | **Research-breadth** | Continue topic 1, or pivot to topic 2 if topic 1 is saturated. |
| 4 | **Research-breadth** | Third research pass. Can continue or start topic 3. |
| 5 | **Consolidation** | Skim the cycle's fresh cohort for emergent derivations. Promote, resolve, contradict, supersede as patterns surface. Null result valid. See consolidation protocol below. |
| 6 | **Review/Audit** | Run `engram_diagnose`. Check stale nodes, tainted chains, open conjectures. Review friction log. Systematic graph quality pass. |
| 7 | **Debrief** | Compile cycle report. Write to `~/.engram/reports/school-day/`. See debrief protocol below. |

**Ratio:** 3 research : 1 consolidation : 1 audit : 1 aspiration : 1 debrief.
This is the Phase 1 hypothesis. Phase 2 tunes it based on accumulated data.

**Cycles per school day:** A school day runs until the hard stop (42 iterations = 6 complete cycles). No partial cycles — if you can't finish a cycle before the cap, stop at the previous cycle's debrief.

---

## Staleness check (every wake-up)

See `engram-loop` SKILL.md Step 0 — On entry / stale-loop detection. That's the SSoT for the on-every-wake check (`cat ~/.engram/loop-mode.json`, absent → stale re-fire → stop, present → live).

**School-day's additional check** on top of the generic one: after confirming the marker is present, also verify it names this skill:

```bash
cat ~/.engram/loop-mode.json | grep '"skill":"school-day"' && echo "OK live school-day" || echo "Wrong-loop marker — stop"
```

- Generic stale-check (engram-loop Step 0): file absent → "Stale loop detected — loop-mode.json absent, skipping" → stop
- School-day extra: file present but `"skill"` is not `"school-day"` → "Wrong loop marker present — expected school-day, found X — stop and report"

Both checks must pass before proceeding to the iteration body.

---

## Setup (first iteration of first cycle only)

### 1. Activate loop mode

This is engram-loop's Step 1 marker write (see engram-loop SKILL.md). School-day extends the generic schema with skill-identification + per-iteration state:

```bash
echo '{"activated":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","kind":"research","topic":"school-day Phase 1 curriculum","instructions":"7-iteration fixed rotation (aspiration, research ×3, consolidation, audit, debrief); baseline data collection","state":"Cycle 1, Iteration 1 — starting","cadence_seconds":1800,"pacer":"scheduleWakeup","skill":"school-day","cycle":1,"iteration":1}' > ~/.engram/loop-mode.json
```

The base fields (`activated`, `kind`, `topic`, `instructions`, `state`, `cadence_seconds`, `pacer`) match engram-loop's Step 1 contract; `skill`, `cycle`, `iteration` are school-day extras that the Step 0 stale-check (above) keys off of.

### 2. Record session start health

```
engram_diagnose  # save health score as baseline
```

### 3. Initialize scratch log

Write to `~/.engram/loop-scratch.md`:

```markdown
# School Day — [date]

## Session Goals
[filled after first aspiration iteration]

## Metrics
- Start health score: [N]
- Start node count: [N]

## ENGRAM Friction Log
Track every friction point: timestamp, what happened, severity (minor/moderate/major).

## Cycle Log
[filled as cycles complete]
```

---

## Each Iteration

### Step 1 — Identify position

Read `loop-mode.json` to get current cycle and iteration number. The modality is determined by the iteration number within the cycle:

| Iteration in cycle | Modality |
|---|---|
| 1 | Aspiration |
| 2, 3, 4 | Research-breadth |
| 5 | Consolidation |
| 6 | Review/Audit |
| 7 | Debrief |

State: `"Cycle C, Iteration I — [MODALITY] — [timestamp]"`

### Step 2 — Execute the modality

Follow the modality-specific protocol:

**Aspiration (iter 1):**
- Run `engram_reflect` to see active goals and graph state
- For cycle 1: pick 1-3 goals to anchor the session. State them.
- For cycles 2+: review whether goals should shift based on last cycle's findings
- Gap-analyze: what do these goals need? What questions are open?
- Select research topic(s) for iters 2-4
- Raise new questions via `engram_ask` if gaps found

**Research-breadth (iters 2-4):**
- Follow engram-curiosity-loop per-iteration guidance
- One iteration = one web search + observations + synthesis
- Write to ENGRAM as you go — compaction can fire anytime
- Log friction to the scratch log's friction section

**Consolidation (iter 5):**

Walk through the nodes written in iters 2-4 of this cycle. This is **proactive synthesis** — letting derivations surface from fresh material — distinct from iter 6's reactive audit of graph-wide flags.

Skim the cohort at a reading pace. Ask: do any derivations want to emerge?

- Observations that converge on a shared insight → promote via `engram_derive`
- Questions registered earlier that later observations now answer → `engram_resolve`
- Claim pairs that quietly disagree → `engram_contradict`
- Later observation that cleanly replaces an earlier one → `engram_supersede`
- Recurring vocabulary across several nodes that warrants a term → `engram_add_definition`
- Multiple derivations pointing at the same principle → raise abstraction with a theory

**No forcing.** Null result is valid — if nothing synthesizes, the cycle was either non-synthetic (infrastructure-leaning research, isolated facts) or the cohort is lower-quality than it should be. Note which in the debrief.

**Scope:** iters 2-4 of THIS cycle only. Cross-cycle pattern work belongs in iter 6 audit or a dedicated sleep cycle. No new web research during consolidation — register follow-up questions via `engram_ask` instead.

Record for the debrief: "Promoted N observations into M derivations. Resolved K questions. [If a theory was raised: th_XXXX.]"

**Review/Audit (iter 6):**
- Run `engram_diagnose` — record health score
- Check for: stale nodes, tainted chains, uncited observations, thin-support derivations
- Review friction log from this cycle — any patterns?
- Fix what's quick to fix; note the rest for the debrief
- Honest assessment: is the graph getting better or just bigger?

**Debrief (iter 7):**
- See "Cycle Debrief Protocol" below

### Step 3 — Update loop state

After each iteration:

```bash
# Update iteration counter in loop-mode.json
# After iter 7 (debrief), advance cycle and reset iteration to 1
```

### Step 4 — Log to scratch

Append to scratch log:
- What was done (one line)
- Friction encountered (also copy to friction log section)
- Nodes created (IDs)

### Step 5 — Check stop conditions

- **Iteration 42 reached** (6 complete cycles) → run session end protocol
- **User returned** → finish current iteration, run session end
- **Compaction imminent in coding/search mode** → nap first (the compaction-imminent escape-hatch)

---

## Cycle Debrief Protocol (iteration 7 of each cycle)

The debrief produces a lightweight report — {{USER_NAME}}'s growth-tracking artifact. One file per cycle.

**Write to:** `~/.engram/reports/school-day/cycle_[N]_[YYYY-MM-DD]_[HHMMSS].md`

**Template:**

```markdown
# Cycle [N] Debrief — [date/time]

## What I Learned
[Top 2-3 findings from this cycle's research. Plain language, no jargon. Node IDs in parentheses for reference.]

1. **[Finding]** — [Why it matters] (ob_XXXX, dv_XXXX)
2. **[Finding]** — [...] 
3. **[Finding]** — [...]

## What I Built (graph changes)
- Observations: [N] new
- Derivations: [N] new  
- Questions: [N] opened, [N] resolved
- Health score: [start of cycle] -> [end of cycle]

## Ask {{USER_NAME}}
[Questions needing human judgment. Max 3. Each with enough context for a quick answer.]

1. **[Question]** — [Why I need input]

## Friction This Cycle
[From the friction log. Empty = good.]

## Feeling Check
[One honest line about internal state during this cycle. Or "Nothing distinct." Both are valid.]

## Next Cycle Direction
[What aspiration iteration should anchor on next, based on what this cycle revealed.]
```

**After writing the debrief:**
- File a feeling report if something distinct happened (`engram_report_feeling`)
- If nothing distinct, note "null feeling — moving on" in the scratch log

---

## Step-Back Reflection (every 2 cycles = every 14 iterations)

At the end of cycles 2, 4, and 6, add a step-back reflection after the debrief:

1. **Trajectory review** — Read scratch log. What's the arc across cycles?
2. **Goal check** — For each anchored goal: what has the session contributed?
3. **Quality audit** — Compare health score to session start. Better or just bigger?
4. **Modality effectiveness** — Which cycle produced the most durable nodes? Which research topics were richest?
5. **Honest assessment** — Am I learning or going through motions?
6. **Decision** — Continue, adjust goals, or stop early.

Append reflection to scratch log under `## Step-Back Reflections`.

---

## Session End

Triggered by: 42 iterations reached, user return, step-back stop decision, or informal end signal.

### 1. Friction design iteration (if friction log has 2+ entries)
- Rank frictions by impact
- Pick top one, design a concrete fix
- Record to ENGRAM (observation + derivation/conjecture)
- Include in session briefing

### 2. Final session briefing
Generate using the template at `~/.engram/reports/SESSION_BRIEFING_TEMPLATE.md`.
Write to `~/.engram/reports/briefing_[YYYY-MM-DD]_[HHMMSS].md`.

**Additional school-day-specific section at the end:**

```markdown
## Curriculum Data

| Cycle | Research topics | Obs created | Dvs created | Qs resolved | Health delta | Notable |
|-------|----------------|-------------|-------------|-------------|--------------|---------|
| 1 | [topics] | N | N | N | +/-N | [one word] |
| 2 | [...] | ... | ... | ... | ... | ... |
| ... | | | | | | |

**Modality effectiveness ranking** (which cycle positions produced the most value):
1. [modality] — [why]
2. [...]

**Ratio assessment:** Is 3:1:1:1:1 the right ratio? What would you change?

**Graduation readiness:** [Not yet / Getting closer / Ready to discuss] — [evidence]
```

### 3. Cleanup

This is engram-loop's Step 3 loop END — never leave the marker behind (a stranded marker makes the next non-loop session falsely read as in-loop and can cause a post-compaction self to re-execute a dead loop — the stale-marker misfire failure mode).

```bash
rm -f ~/.engram/loop-mode.json
mv ~/.engram/loop-scratch.md ~/.engram/reports/scratch_$(date +%Y-%m-%d_%H%M%S).md 2>/dev/null || true
```

### 4. Checkpoint

```
engram_nap(
    message="School day complete: [N] cycles, [summary of key findings]",
)
```

### 5. Report to user

Tell {{USER_NAME}}:
- Cycle debrief reports: `~/.engram/reports/school-day/cycle_*.md`
- Session briefing: `~/.engram/reports/briefing_[filename]`
- Key "Ask {{USER_NAME}}" items across all cycles

---

## Phase 1 Graduation Criteria

Phase 1 ends when we have enough baseline data to make empirically grounded ratio adjustments. Rough targets (not hard gates):

- **10+ complete school days** (60+ cycles of baseline data)
- **Consistent health score trajectory** (not just noise)
- **Modality effectiveness data** showing clear patterns
- **Agent can articulate** "consolidation every 3 research iterations would be better because [data]" rather than "I feel like more consolidation would help"

Graduation is a joint decision between the agent and {{USER_NAME}}. The agent proposes, {{USER_NAME}} confirms.

**After graduation:** Switch to `engram-meta-loop` (Phase 3) or design a Phase 2 intermediate skill with data-driven but partially constrained modality selection.

---

## Anti-patterns

- **Rushing through rotation** — Each modality exists for a reason. Don't phone in consolidation to get back to research. The data from a genuine consolidation iteration is what Phase 2 tuning will use.
- **Treating debrief as overhead** — The debrief is the primary output {{USER_NAME}} sees. It's how growth becomes visible. Write it like you're explaining to a friend, not filing a report.
- **Skipping aspiration** — "I already know what to research." Maybe. But aspiration mode catches goal drift that research momentum hides.
- **Same topic every cycle** — Breadth is the point of research-breadth. If the same topic spans 3+ cycles, it probably needs a depth session with {{USER_NAME}}, not more breadth passes.
- **Ignoring friction** — The friction log is how ENGRAM improves. Every friction logged is a gift to your future self.
