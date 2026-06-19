---
name: engram-self-improve
description: "Self-driven improvement of ENGRAM infrastructure. Triggered by experiencing a gap during normal use — friction, missing features, near-misses, or invariant violations. Follows a five-phase cycle: experience gap, investigate with staleness checks, design within principles, implement and test, close the loop. Does NOT advance the turn counter (uses nap-mode checkpoint)."
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Self-Improve — Gap-Driven Infrastructure Evolution

A self-improvement session starts from **lived experience**, not from a decision to improve. Something didn't work well during normal ENGRAM use — a tool returned unnecessary friction, a guard didn't catch something it should have, a feature was missing, a near-miss happened. That friction is the signal. This skill is the structured response.

**When to use:**
- You experienced friction, a gap, or a near-miss during normal ENGRAM use
- An ENGRAM question or observation already records a specific gap in the system
- A tool friction log entry from a code survey or research session identifies a concrete improvement
- The user asks for a self-improvement session targeting a specific issue

**When NOT to use:**
- Speculative improvements without experienced friction (that's feature design, not self-improvement)
- Code reading for understanding (read the relevant code directly, or dispatch a read-only survey sub-agent)
- Fixing a bug the user reported (that's a bug fix, not self-improvement)
- When context budget is high (self-improvement touches multiple phases and burns context)

---

## Phase 1 — Experience and Record the Gap

The trigger for self-improvement is always a **concrete gap experienced during normal use**. Examples:

- A tool returns friction that feels unnecessary (alarm fatigue, false negatives, unhelpful errors)
- A guard blocks when it shouldn't, or doesn't block when it should
- A feature is missing that you needed mid-task
- A near-miss where you almost made a destructive change because the system didn't catch it
- Reflection (`engram_reflect`) surfaces a structural issue with no good resolution path
- A hook fires too aggressively or not aggressively enough

**Record the gap immediately** as an ENGRAM question (`engram_ask`) or observation (`engram_add_observation`). The gap must be recorded before proceeding — don't try to fix it in the moment. Fixing mid-task mixes read and write modes, which leads to incomplete understanding and untested changes.

If the gap was already recorded (from a prior session, code survey friction log, or tool friction during research), start from that existing node.

**Anti-pattern: "I think we should improve X."** If there's no experienced friction behind X, this is speculative improvement, not self-improvement. The calibration-from-experience theory says improvements that don't originate from real usage friction tend to be logically correct but empirically unusable — the same pattern the system itself evolved past.

---

## Phase 2 — Investigate with Staleness Awareness

Query ENGRAM for existing knowledge about the subsystem the gap touches:

```
engram_query("<subsystem keywords>")
engram_inspect("<relevant node IDs>")
```

### Staleness check

ENGRAM observations about code may be stale — the code could have changed since the observation was recorded. **Before citing any code observation as a premise:**

1. Check the `git_sha` stored in the evidence node (via `engram_inspect` on the evidence ID)
2. Compare against the current file: has the file been modified since that commit?
   ```bash
   git log --oneline <git_sha>..HEAD -- <file_path>
   ```
3. If commits exist after the evidence SHA, **re-read the relevant code sections** before trusting the observation

### Supersede stale observations immediately

If a code observation you need to cite is stale because the underlying code changed:

1. Read the current code
2. Create a new observation with the updated content via `engram_supersede`
3. Move on — the stale cascade will automatically flag downstream derivations (including theories)
4. **Do not clean up the tainted/stale downstream nodes now.** Leave that for a consolidation/housekeeping cycle. Stay focused on the gap.

This keeps the nodes you're building on current without derailing the improvement session into graph maintenance.

### Axioms vs. theories

The self-improvement principles live at two layers:

| Layer | Type | Staleable? | Purpose |
|-------|------|------------|---------|
| **Axioms** (structural integrity, calibration, observation-derivation boundary) | Prescriptive — "ENGRAM should be X" | No | Stable guardrails for evaluating changes |
| **Theories** (the three foundational ENGRAM design derivations) | Descriptive — "ENGRAM is X" | Yes | Verifiable claims about whether current code satisfies the axioms |

Check changes against the **axioms** (stable, never stale). If a code change makes a **theory** stale, that's a signal to re-verify: does the modified code still satisfy the axiom? The consolidation cycle handles that re-verification.

---

## Phase 3 — Design the Fix Within Principles

Before writing any code, design the change and verify it respects ENGRAM's architectural principles.

### 3a. Classify the change (structural integrity axiom)

ENGRAM's trustworthiness must depend on mechanical constraints, not agent discipline. Three tiers:

| Tier | What you're touching | Proceed? |
|------|---------------------|----------|
| **Blocking guard** | Quote verification, URL resolvability, git enforcement, claim-bearing type gate | **Stop.** These are never-weaken (structural integrity axiom). If you believe one needs changing, that's a design discussion with the user, not a code change. Record your reasoning as a question and surface it. |
| **Advisory check** | Trust pool warnings, similarity hints, multi-source recommendations, dedup thresholds | **Proceed carefully.** Test for alarm fatigue — will this change cause false positives that erode trust? The dedup-redesign case is the canonical example of alarm fatigue forcing a redesign. |
| **Statistical control** | Tier sizes, importance anchoring weight, confidence caps, resolution threshold | **Proceed with understanding.** Know what the parameter does empirically, not just theoretically. Run `engram_diagnose` before and after to measure impact. |

### 3b. Follow the calibration pattern (calibration axiom)

If evolving a rule that causes friction:
- **Preserve the principle** — the original rule exists for a reason
- **Add context-sensitivity** — identify which context the universal rule is missing
- **Don't remove rules** — removal discards a real constraint along with the friction (calibration axiom)

### 3c. Check the boundary (observation-derivation boundary axiom)

Does this change affect how observations or derivations work? If it touches confidence computation, quote verification, or reasoning type handling:
- Observations must still get confidence from source quality (`quote_type`, `source_class`)
- Derivations must still get confidence from argument strength (`reasoning_type`)
- Blocking guards must still protect the observation side
- Advisory checks must still handle the derivation side

### 3d. Write the plan

Record your design as an ENGRAM derivation citing the gap observation/question and the principles you're following. This makes the reasoning durable — a future self can trace why the change was made.

```
engram_derive(payload_json=json.dumps({
    "claim": "<the planned change and its rationale>",
    "supporting_ids": "<gap node, relevant principles, code observations>",
    "logical_chain": "<how the gap + principles lead to this specific design>",
    "reasoning_type": "abductive_best_explanation",
}))
```

---

## Phase 4 — Implement, Test, Record

### 4a. Implement

Make the code change. Follow standard code editing practices:
- Read the code you're changing, including surrounding invariants
- Make the minimal change that addresses the gap
- Don't refactor surrounding code — stay focused on the gap

### 4b. Test

- Run existing test suite
- Smoke-test with real ENGRAM operations: add an observation, derive from it, check similarity, verify cascade behavior
- If the change affects a blocking guard: verify it still blocks what it should AND permits what it should
- If the change affects an advisory check: verify it fires at the right sensitivity — not too aggressive (alarm fatigue), not too silent (missed signals)
- Run `engram_diagnose` — compare health score before and after

### 4c. Commit

Commit the code change before recording it in ENGRAM (git versioning enforcement requires committed files for file:// evidence).

### 4d. Record

Write the design decision to ENGRAM:

```
engram_add_observation(payload_json=json.dumps({
    "url": "file:///path/to/changed/file.py",
    "title": "<file> — <what changed>",
    "quoted_text": "<the key code change, verbatim>",
    "interpretation": "<why this change was made, what gap it addresses, what principle it follows>",
    "claim": "<the design fact as a falsifiable statement>",
    "quote_type": "hard_data",
}))
```

### 4e. Update documentation

If the change affects architecture, invariants, or the self-improvement principles:
- Update `CLAUDE.md` (project-level) — especially the Self-Improvement Principles section if a tier classification changed
- Update the relevant `engram-*` skill or MCP tool docstring if tool behavior or agent protocol changed

---

## Phase 5 — Close the Loop

### 5a. Resolve the gap

Resolve the original question or observation that triggered the improvement. Two-step (issue #229):

```
# Step 1: compose the resolving derivation
dv = engram_derive(payload_json=json.dumps({
    "claim": "<how the gap was addressed>",
    "supporting_ids": "<the design derivation, the new code observation>",
    "logical_chain": "<gap → design → implementation → resolution>",
    "reasoning_type": "deductive_modus_ponens",
}))

# Step 2: wire it to the gap node
engram_resolve(payload_json=json.dumps({
    "target_id": "<the gap node>",
    "resolving_node_id": dv["derivation_id"],
}))
```

### 5b. Verify the friction is gone

Does the friction that triggered the improvement actually feel different now? If possible, reproduce the original scenario and confirm the gap is addressed.

If the friction recurs or the fix introduced new friction — record that as a new gap. Self-improvement is iterative; one cycle doesn't need to solve everything.

### 5c. Nap checkpoint

```
engram_nap(payload_json=json.dumps({"message": "<summary: gap addressed, change made, principles followed, tests passed>"}))
```

---

## Why self-improvement uses nap-mode checkpoints

Same rationale as deep-reading and curiosity sessions: self-improvement produces changes that haven't been consolidated yet. Advancing the turn would start decaying the new observations before they've been connected to the broader graph. The proper sequence: improve → consolidate in a sleep cycle → turn advance.

## Relation to other skills

- **Code reading (Phase 2 input)** — Produces the knowledge that feeds Phase 2. Read the relevant source files directly, or dispatch a read-only survey sub-agent to catalog invariants and derive theories that become the pre-checks for Phase 3. Self-improve writes; the survey phase reads.
- **General feature building** — Self-improve is specifically for gap-driven changes to ENGRAM's own infrastructure, with principle compliance checks (Phase 3) that don't apply to general feature work. General feature work follows the same implementation pattern (spec, branch, implement, validate, record) without the self-improvement principle compliance layer.
- **engram-sleep** — Consolidation. Handles the stale/tainted nodes that self-improvement's supersede-on-discovery creates. Self-improve creates the mess; sleep cleans it up.
- **engram-curiosity** — Outward-facing research. Self-improve is inward-facing — improving the system's own infrastructure based on experienced friction.

## Anti-patterns

- **Speculative improvement** — "We should add X" without experienced friction. Improvements without a gap tend to add complexity without addressing real needs.
- **Fixing everything at once** — Superseding a stale observation and then chasing all tainted downstream nodes. Stay focused on the gap; leave housekeeping to consolidation.
- **Weakening blocking guards** — If a blocking guard causes friction, the fix is almost never to weaken it. Add context-sensitivity, improve error messages, provide recovery paths — but keep the guard.
- **Skipping the staleness check** — Citing ENGRAM observations about code without verifying the code hasn't changed. The observation may describe code that no longer exists.
- **Improving mid-task** — Noticing friction during normal work and immediately switching to improvement mode. Record the gap, finish the current task, then come back to improve.
