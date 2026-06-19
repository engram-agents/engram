---
name: engram-learn-from-error
description: "Extract a reusable lesson from a specific mistake using ENGRAM's incident-based cognitive tripwire. Three-step process: write a task-level incident observation, derive a lesson node, verify the tripwire fires. Grounded in implementation intention theory (the two foundational implementation-intention derivations)."
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Learn From Error — Incident-Based Cognitive Tripwire

When you (or the user) identify a mistake you made, this skill guides you through extracting a reusable lesson and writing it to ENGRAM's cognitive tripwire system. The tripwire fires automatically when a future prompt semantically matches the incident observation.

**Architecture (incident-based design):**
- **Incidents** are observation nodes written in TASK-LEVEL language — they describe what happened concretely, in the same language future prompts will use.
- **Lessons** are derived from incidents — they hold the abstract corrective pattern and the scaffolding nudge.
- **Matching** happens on incidents (same semantic space as prompts). When engram_surface matches an incident, the hook follows the graph edge to surface the lesson's nudge.
- **Consolidation**: multiple incidents cluster around superseding lesson chains. More incidents = more matching surface area.

**When to use:**
- The user points out a mistake you made ("you should have read the error first")
- You self-detect a mistake during reflection
- A pattern of similar errors across sessions becomes apparent

**When NOT to use:**
- One-off mistakes caused by missing information (not a pattern)
- Mistakes that are already covered by an existing lesson (check `engram_reflect` → active_lessons)
- Task-specific errors that won't transfer to other situations

---

## Step 1 — Identify the specific mistake

Answer these questions about the specific instance:

1. **What task were you doing?** (diagnosing a bug, making a design decision, writing code, etc.)
2. **What did you actually do?** (jumped to a hypothesis, skipped reading the error, didn't run tests, etc.)
3. **What should you have done instead?** (read the error output first, consider alternatives, verify the assumption, etc.)
4. **What was the consequence?** (misdiagnosis, wasted effort, incorrect code, etc.)

State these explicitly before proceeding.

## Step 2 — Write the incident observation (TASK-LEVEL language)

**This is the matching surface — write it in the same language future prompts will use.**

Create an observation using `engram_add_observation` with `source_class: "introspective"`:

- **claim**: Describe the CONCRETE incident in task-level language. This is what future prompts will match against.
  - GOOD: "While debugging a data pipeline error, I hypothesized about the database connection without reading the error trace, which showed it was a simple JSON parsing failure."
  - BAD: "I tend to jump to hypotheses before reading error output." (metacognitive — won't match task-level prompts)
  - BAD: "ERROR PATTERN: skip reading errors." (abstract — no matching surface area)

- **interpretation**: Describe what went wrong and what should have happened instead. Include the cognitive step that was skipped.

**Why task-level language matters:** ENGRAM's semantic matching (all-MiniLM-L6-v2) operates in the same embedding space as user prompts. A prompt like "fix this authentication bug" will match "debugging an authentication error, I skipped reading the stack trace" but will NOT match "I have a tendency to skip error output" — the semantic distance is too large (empirically verified via task-level-vs-trait-level prompt-matching tests).

## Step 3 — Abstract to lesson and derive

Extract the reusable pattern following Gollwitzer's implementation intention format:
- **IF** [situation with key features] — the trigger
- **THEN** [correct action step] — the scaffolding nudge

**Critical: the THEN clause must be ACTION-FOCUSED, not PROBLEM-FOCUSED.**
- GOOD: "Read the error output before forming a hypothesis"
- BAD: "Don't skip reading the error output"

Create the lesson using `engram_add_lesson`:

```
engram_add_lesson(payload_json=json.dumps({
    "claim": "When diagnosing errors, read the error output before forming any hypothesis about the cause.",
    "incident_ids": "ob_XXXX",  # The incident observation from Step 2
    "scaffolding_nudge": "Read the error output, stack trace, or logs BEFORE forming any hypothesis about the cause.",
    "logical_chain": "[How the specific incident(s) generalize to this pattern]",
    "reasoning_type": "inductive_generalization",
    "context_ids": "<implementation-intention-derivation-ids>",  # Implementation intention theory nodes (look up the actual IDs in your install)
}))
```

**Check for consolidation:** The tool returns `similar_existing_lessons` if a similar lesson already exists. If so:
- If the existing lesson covers the same pattern: supersede it with a new lesson that cites BOTH the old and new incidents. More incidents = stronger matching surface.
- If the existing lesson is related but different: keep both separate.

## Step 4 — Verify the tripwire fires

Test that the incident matches a realistic future prompt:

```bash
echo '{"prompt": "[realistic task prompt that should trigger]"}' | python3 ~/.engram/hooks/engram-surface-hook.py 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); ctx=d.get('hookSpecificOutput',{}).get('additionalContext',''); print('TRIPWIRE FIRED' if 'Tripwire' in ctx else 'no tripwire')"
```

Test with:
1. A prompt that SHOULD trigger (similar task-level language to the incident)
2. A prompt that should NOT trigger (different task type)

**If the tripwire doesn't fire:** The incident observation's claim may not be in task-level language, or the semantic similarity is too low. Rewrite the incident to match how users would phrase the request.

## Step 5 — Growing the matching surface (future incidents)

When the SAME error pattern recurs with a new incident:
1. Write a new incident observation (Step 2) describing the new concrete situation
2. Create a new lesson via `engram_add_lesson` with ALL incident IDs (old + new)
3. Supersede the old lesson: `engram_supersede(old_lesson_id, new_lesson_id)`

Each new incident is another entry point — another way the lesson can fire. The lesson chain grows stronger over time through accumulated incidents, like a spider web with more strands catching more flies.

## Anti-patterns

- **Metacognitive incident language**: "I skip reading errors" — won't match task prompts. Always write incidents in the language of the task: "While debugging X, I did Y instead of Z."
- **Over-general lessons**: "Always check your work" — fires on everything, useless. The situation type must be specific.
- **Problem-focused nudges**: "Don't make hasty diagnoses" — tells the agent what NOT to do. Always phrase as the corrective action.
- **Domain-specific incidents**: "When working on auth.py" — won't transfer. Keep the domain details for context but write the claim at the right abstraction level.
- **Skipping consolidation**: When `similar_existing_lessons` is returned, don't ignore it. Consolidating incidents under one lesson chain is how the system gets smarter over time.
