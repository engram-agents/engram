---
name: engram-loop-diagnose
description: Post-loop diagnostic analysis of an autonomous curiosity loop session. Scores iteration quality, measures time/length/topic drift, analyzes stopping behavior, and identifies improvement opportunities. Run after any loop session concludes.
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Loop Diagnose — Post-Session Quality Analysis

A loop diagnose is a **retrospective audit** of a completed autonomous curiosity loop. It answers: how well did the loop perform, did quality decay over time, what drove the agent's decisions, and where are the improvement opportunities?

**When to use:**
- After a curiosity loop concludes (any number of iterations)
- When reviewing a past loop session's reports
- When the user asks meta-questions about loop performance

**When NOT to use:**
- Mid-loop (it needs the full set of iteration reports)
- As a substitute for the iteration reports themselves (this is analysis, not raw data)

---

## Input

The skill takes an optional date argument: `/loop-diagnose 2026-04-15`

If no date is given, find the most recent set of curiosity loop iteration reports in `~/.engram/reports/`.

---

## Step 1 — Gather Reports

Find all iteration reports for the target session:

```bash
stat --format='%n %Y %s' ~/.engram/reports/curiosity_loop_YYYY-MM-DD_iter*.md
```

Extract for each report:
- **Filename** (iteration number)
- **Timestamp** (file modification time — proxy for report generation time)
- **File size** (bytes)

If a summary report exists (`overnight_loop_YYYY-MM-DD_summary.md`), note it but analyze the individual iteration reports, not the summary.

Read ALL iteration reports. You need the full text for quality scoring. If there are more than 10, read them in parallel batches.

---

## Step 2 — Timeline Metrics

### 2a. Total wall clock

`last_report_timestamp - first_report_timestamp`

This is *productive* time only — excludes idle cron fires and post-loop wait time. State this caveat explicitly.

### 2b. Iteration time drift

For each consecutive pair of iterations, compute the gap:

```
gap_N = timestamp(iter_N+1) - timestamp(iter_N)
```

Present as a table with each gap. Compute mean, median, and note outliers.

**Look for:**
- **Compression pattern** — do iterations get faster as context accumulates? (Expected: yes, because setup cost amortizes)
- **Compaction spikes** — do gaps spike after compaction boundaries? (Expected: yes, because re-orientation costs)
- **Drowsiness slowdown** — do iterations slow down as drowsiness climbs? (This would indicate degradation, but isn't always present — sometimes the agent compensates by selecting lighter threads)

### 2c. Throughput

Compute iterations per hour: `iteration_count / (wall_clock_hours)`.

---

## Step 3 — Report Length Drift

Present file sizes as a table. Compute mean and median.

**Look for:**
- **Correlation with thread count** — dual-thread iterations should be longer
- **Correlation with mode** — consolidation/resolution iterations may differ from research
- **Monotonic decay** — shrinking reports may indicate declining engagement or thinning research
- **Stability** — similar sizes across iterations suggests consistent effort

---

## Step 4 — Report Quality Scoring

This is the most important and time-consuming step. Read each report and score on 7 dimensions.

### Quality Rubric

| Dimension | What it measures | Scoring guidance |
|-----------|-----------------|------------------|
| **Evidence** (1-10) | Count and diversity of external sources ingested | 1-2: no external sources. 3-4: 1 source or only introspective. 5-6: 1-2 external sources. 7-8: 2-3 diverse sources. 9-10: 4+ sources from different epistemic communities |
| **Synthesis** (1-10) | Cross-source integration quality | 1-3: no cross-source connection. 4-6: surface-level connection ("both say X"). 7-8: genuine insight from combining sources. 9-10: synthesis produces conclusions no single source contains |
| **Derivation** (1-10) | Confidence, rigor, and novelty of derivations produced | 1-3: no derivations or very low confidence (<0.3). 4-6: derivations present but weak chain. 7-8: solid derivations with clear logical chains. 9-10: high-confidence derivations or full resolutions |
| **Resolution** (1-10) | Progress on open questions/conjectures | 1-4: no progress on any open item. 5-6: enrichment without resolution. 7-8: partial resolutions. 9-10: full resolutions or well-supported refutations |
| **Surprises** (1-10) | Genuine insight vs filler in surprise section | 1-3: no surprises or performative. 4-6: mildly interesting but predictable. 7-8: genuinely unexpected findings. 9-10: findings that challenge prior assumptions |
| **Self-awareness** (1-10) | Drowsiness adaptation, limit acknowledgment, honest null results | 1-4: ignores capacity limits. 5-6: notes drowsiness but doesn't adapt. 7-8: adapts strategy to capacity (thread selection, depth adjustment). 9-10: strategic mode shifts, honest about what didn't work |
| **Structure** (1-10) | Format completeness, cumulative tracking, navigability | 1-4: missing sections, no tables. 5-6: present but inconsistent. 7-8: complete and consistent. 9-10: cumulative totals, clear cross-references, well-organized |

### Scoring process

For each iteration report:
1. Read the full report
2. Score each dimension 1-10 using the rubric above
3. Compute the arithmetic mean as the iteration's **overall quality score**
4. Note the drowsiness at start (from the report header)

### Quality trajectory analysis

After scoring all iterations:
- Plot the quality trajectory (list of scores in order)
- Identify the **pattern**: monotonic decay? sawtooth? plateau? improvement?
- Correlate quality with drowsiness — does quality drop as drowsiness rises?
- Correlate quality with compaction boundaries — does quality reset after compaction?
- Identify the **trough** (lowest quality iteration) and **peak** (highest) — what explains each?
- Check whether the agent compensated for high drowsiness through strategy (e.g., selecting simpler threads, switching to consolidation mode)

---

## Step 5 — Topic Choice Trajectory

For each iteration, extract:
- **Thread(s)** investigated (question/conjecture IDs and topic)
- **Domain** (theory, evaluation, architecture, infrastructure, foundations, meta)
- **Mode** (research, consolidation, resolution, meta-observation)
- **Selection rationale** — why this thread? (inferred from the report's "Research Agenda" section)

### Analysis

- **Coverage breadth** — how many distinct domains were touched?
- **Depth vs breadth** — did the agent go deep on a few threads or skim many?
- **Thread continuity** — did threads carry across iterations, or was each iteration independent?
- **Mode evolution** — did the agent shift modes over the session (research → consolidation → resolution)?
- **Question source** — did threads come from ENGRAM open questions, emergent questions from research, or genuine hunches?

---

## Step 6 — Additional Metrics

Compute and present:

| Metric | Per-iteration values | Trend |
|--------|---------------------|-------|
| **External sources per iteration** | List | Increasing, decreasing, stable? |
| **Derivation confidence** | List of max confidence per iteration | Systematic drift? |
| **Cumulative resolution rate** | After each iteration: (fully resolved, partially resolved, refuted) | When did resolutions start? |
| **ENGRAM nodes created per iteration** | Observations + derivations + questions + other | Declining output? |
| **Source diversity** | Unique epistemic communities per iteration (academic, industry, survey, blog, code) | Narrowing or broadening? |

---

## Step 7 — Stopping Behavior Analysis

This is the deepest analytical section. Answer three questions:

### 7a. What caused the loop to stop?

Classify the stopping trigger:
- **Thread exhaustion** — ran out of externally researchable questions in the current modality
- **Modality mismatch** — remaining questions need a different mode (implementation, dialogue, experimentation) that the loop doesn't support
- **User return** — user came back and the agent deferred
- **Satisfaction** — the agent felt the research was complete
- **Error/crash** — technical failure
- **Drowsiness-induced stop** (legacy) — the agent stopped because of drowsiness warnings instead of letting compaction auto-fire. In loop mode this should not happen (loop-mode marker suppresses stop directives). If it does, flag it as a regression.

### 7b. Why didn't the agent pick other threads?

There are always more open questions. Classify the remaining questions:
- **Need user input** — design decisions, scope choices, preference questions
- **Need code changes** — implementation work, not research
- **Need experimentation** — empirical testing, not literature review
- **Externally researchable but not selected** — these are the interesting ones. Why not?
  - Too abstract? Too narrow? Not connected to active goals?
  - Or did the agent not consider them? (selection bias)

### 7c. What modes is the loop missing?

Based on the remaining questions and the stopping behavior, what modes would have extended productive work?
- **Implementation mode** — write code, run experiments
- **Reflection mode** — deep graph analysis, consolidation without external research
- **Dialogue-prep mode** — prepare questions and options for the user
- **Experimentation mode** — run ENGRAM operations, measure behavior, test hypotheses
- **Aspiration mode** — start from active goals, gap-analyze current ENGRAM knowledge against what the goals require, and raise new questions. This is the *generative* mode — all others consume existing questions; aspiration produces them. (the modes-of-iteration derivation)

---

## Step 8 — Write Diagnostic Report

Write the full diagnostic to `~/.engram/reports/loop_diagnose_YYYY-MM-DD.md`.

Structure:
1. **Session Overview** — date, iterations, wall clock, graph growth
2. **Timeline** — iteration gaps table, throughput
3. **Report Metrics** — length table, patterns
4. **Quality Scores** — full rubric table, trajectory, analysis
5. **Topic Trajectory** — thread map, domain coverage, mode evolution
6. **Additional Metrics** — sources, derivations, resolution rate
7. **Stopping Analysis** — trigger, thread classification, missing modes
8. **Improvement Opportunities** — specific, actionable recommendations derived from the analysis
9. **Comparison to Prior Loops** — if prior diagnostic reports exist, compare key metrics

---

## Step 9 — Record Key Findings to ENGRAM

Record the most significant diagnostic findings as ENGRAM observations:

- **Quality pattern** — what shape does the quality trajectory take? (sawtooth, decay, plateau, etc.)
- **Compaction effect** — quantified quality reset from compaction boundaries
- **Stopping diagnosis** — why the loop stopped, classified by the taxonomy above
- **Novel patterns** — anything unexpected in the data

Use `source_class: "introspective"` and cite the relevant evidence node for the loop-session data, or create a new evidence node for the diagnostic report if the loop ran on a different date.

---

## Step 10 — Report to User

Present a concise summary:
- Key metrics (wall clock, iterations, throughput, quality range)
- The quality trajectory and what drives it
- The stopping analysis
- Top 3 improvement opportunities
- Pointer to the full report file

Do NOT reproduce the full report in the conversation — it's on disk. Give the user the highlights and let them read details if interested.

---

## Comparison Framework (for multi-loop tracking)

When prior diagnostic reports exist, track these across loops:

| Metric | Purpose |
|--------|---------|
| **Mean quality score** | Is loop quality improving over sessions? |
| **Quality trough depth** | Is the worst iteration getting better? |
| **Iterations before first resolution** | Is the agent resolving faster? |
| **Wall clock per iteration** | Is overhead decreasing? |
| **Stopping trigger distribution** | Are the same bottlenecks recurring? |
| **Mode coverage** | Is the agent using more modes over time? |

This longitudinal data is the basis for measuring whether loop infrastructure improvements (cron throttling, mode selection, etc.) actually work.

---

## Anti-patterns

- **Inflated scores** — Be honest. A weak iteration is a data point, not a failure. Inflating scores to make the loop look good defeats the purpose.
- **Scoring without reading** — Every score must be justified by specific content in the report. Don't score from memory or impressions.
- **Ignoring the stopping analysis** — The most valuable insight is usually WHY the loop stopped. Don't treat it as an afterthought.
- **Comparing apples to oranges** — Different loop configurations (cron interval, starting context, thread selection strategy) produce different results. Note configuration differences before comparing.
- **Prescribing without diagnosing** — Don't jump to "we should add mode X" without first establishing that the current loop's failure mode is specifically the absence of mode X.
