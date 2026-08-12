---
name: engram-learn-from-error
description: "Extract a reusable lesson from a specific mistake using ENGRAM's incident-based cognitive tripwire. Write a task-level incident observation, derive a lesson node, optionally arm an action-time regex trigger, and verify each layer fires. Covers both firing layers — prompt-time semantic matching and PreToolUse pattern matching — and how to scope and test a situation_pattern so it stays a signal rather than becoming friction. Grounded in implementation intention theory (the two foundational implementation-intention derivations)."
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

### Two firing layers — arm both

A lesson can fire at two different moments, and they catch different failures. Most lessons should have both.

| | **Prompt-time (semantic)** | **Action-time (regex)** |
|---|---|---|
| Fires at | `UserPromptSubmit` | `PreToolUse` (`Bash` + `mcp__.*`) |
| Matches on | incident claim, embedding similarity to the prompt | `situation_pattern` regex vs the tool call |
| Armed by | writing the incident in task-level language (Step 2) | setting `situation_pattern` on the lesson (Step 3b) |
| Catches | "this whole task resembles a past mistake" | "this specific call is the mistake, right now" |
| Blind to | a mistake that emerges mid-turn, after the prompt | a mistake with no distinctive tool-call shape |

The prompt-time layer cannot see a mistake that only becomes possible three tool calls into a turn — by then the prompt is long past. The action-time layer sees the call itself, immediately before it runs.

**The action-time layer is ADVISORY — it never blocks.** Every exit path in the tripwire hook is `sys.exit(0)`; its only output is `additionalContext`, injected before the tool runs. The tool then runs regardless. So a `situation_pattern` buys a *reminder at the moment of action*, never a guarantee. Design nudges accordingly: they must be readable and act-on-able in one glance, because nothing is stopping the call.

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

## Step 3b — Arm the action-time tripwire (`situation_pattern`)

If the mistake has a recognisable **tool-call shape**, add a `situation_pattern` to the lesson. This is a real regex, matched with `re.search(pattern, target, re.IGNORECASE)`.

**It is not a separate call.** `situation_pattern` is simply another key in the *same* `engram_add_lesson` payload from Step 3 — add it alongside `claim` and `scaffolding_nudge`:

```
"situation_pattern": r"(?s)(?=.*<channel>)(?=.*<content>)",
```

(To arm a pattern on a lesson that already exists, `engram_update_lesson` takes the same field in place — no supersede needed.)

**Know exactly what you are matching against** — this is the single most common source of patterns that never fire:

| Tool | Match target |
|---|---|
| `Bash` | the raw shell command string, nothing else |
| `mcp__*` | `"{tool_name} {compact_json_of_args}"` |
| anything else | `""` — the hook exits early, your pattern can never fire |

Two consequences worth internalising:

- For a **Bash** call, a body written via heredoc into a temp file **is** part of the command string, so its content is matchable. A body read from a pre-existing file is **not**.
- For an **MCP** call, the tool name is a substring of the target, so `engram_add_observation` alone is a valid pattern — and that is exactly why bare content patterns over-fire (see below).

### Scope by CHANNEL, not by content alone

The hook is registered globally on `Bash` and `mcp__.*`. What decides whether *your* lesson fires is your pattern — so a pattern that keys only on content will fire on every tool call that happens to contain that content, including your own routine ones.

Worked example. A lesson wanting "don't put internal identifiers in outbound messages" cannot key on the identifier shape alone: identifiers legitimately appear in nearly every knowledge-graph write. Requiring **channel AND content together** fixes it, via two lookaheads:

```
(?s)(?=.*(?:\bsend-message\b|outbound_call))(?=.*\b(?:PREFIX)_\d{4,}\b)
```

- Each `(?=...)` is an unordered requirement, so channel and content may appear in either order.
- The result is zero-width and anchors anywhere, so `re.search` succeeds only if both hold.
- `(?s)` makes `.` cross newlines. **Whether it is load-bearing depends on which target you are matching**, and the difference is not obvious:

| Target | Real newlines? | `(?s)` |
|---|---|---|
| `Bash` — heredoc body inside the command string | **yes** | **required.** Without it `.` stops at the first newline and a multi-line body silently never matches. |
| `mcp__*` — args serialized with `json.dumps` | no — newlines are escaped to a literal `\n` **two-character sequence** | not needed; harmless to include |

Verified both directions: on a real heredoc command the same pattern matches with `(?s)` and fails without it; on a serialized MCP payload it matches either way, because there is no newline character in the target at all.

**Include `(?s)` by default.** It is free on MCP targets and load-bearing on Bash ones, and "which target will this pattern actually see" is exactly the question that is easy to get wrong when the lesson covers both.

**Start with the channels that produced real incidents, and add more one at a time as new incidents occur.** A pattern that covers every plausible channel up front is indistinguishable from an over-broad one, and its cost lands on your own most-used tools. Breadth is earned by evidence, not anticipated.

### Test the pattern BEFORE arming it

An untested regex has two silent failure modes: it never fires, or it fires constantly. Both look like "nothing happened." Build a small table of shaped inputs and check every row — including the ones that **must not** fire:

```python
import re, json
PATTERN = r"..."          # the candidate
def bash(cmd): return cmd
def mcp(name, args): return f"{name} {json.dumps(args, separators=(',',':'), ensure_ascii=False)}"

cases = [                                    # (label, target, should_fire)
    ("the real incident shape",      bash("..."),                     True),
    ("same channel, benign",         bash("..."),                     False),
    ("routine own-tool call",        mcp("mcp__...__write", {...}),   False),
]
for label, target, should in cases:
    fired = bool(re.search(PATTERN, target, re.IGNORECASE))
    print(f"{'ok ' if fired == should else 'FAIL'}  fired={fired}  expect={should}  {label}")
```

**The must-not-fire rows are the important half.** They are what keeps the tripwire from becoming friction you learn to ignore — and an ignored tripwire is worse than none, because it looks like coverage.

## Step 4 — Verify the tripwire fires

Verify **each layer you armed** — they fail independently, and passing one says nothing about the other.

### Prompt-time layer

Test that the incident matches a realistic future prompt:

```bash
echo '{"prompt": "[realistic task prompt that should trigger]"}' | python3 ~/.engram/hooks/engram-surface-hook.py 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); ctx=d.get('hookSpecificOutput',{}).get('additionalContext',''); print('TRIPWIRE FIRED' if 'Tripwire' in ctx else 'no tripwire')"
```

Test with:
1. A prompt that SHOULD trigger (similar task-level language to the incident)
2. A prompt that should NOT trigger (different task type)

**If the tripwire doesn't fire:** The incident observation's claim may not be in task-level language, or the semantic similarity is too low. Rewrite the incident to match how users would phrase the request.

### Action-time layer

If you armed a `situation_pattern`, the Step 3b table already proved the regex. What it did **not** prove is that the lesson is reachable by the hook at all — the hook only considers lessons that are active and carry a non-empty `situation_pattern`. Confirm the lesson made it into that set, then exercise it for real: make one harmless call of the shape the pattern targets, and check the nudge appears before the result.

**A pattern that tests green in isolation and never fires in practice is the expected failure**, not a surprise. Usual causes:

- The lesson is no longer **active *and* current**. The hook's query requires both — so a **superseded** lesson drops out even though nothing deactivated it. Note that Step 5 below has you supersede a lesson every time an incident recurs, which means the routine way of *strengthening* a lesson is also the routine way of silently unarming its predecessor. Re-arm the successor.
- The `situation_pattern` never made it onto the node at write time.
- The tool's match target is `""`, so the pattern was never consulted at all.

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

### Anti-patterns specific to `situation_pattern`

- **Prose as a regex**: writing the trigger as an English sentence ("when sending a message to another agent"). It never fires against a real command, but *why* is messier than any single sentence captures, and the mess is worth knowing because it changes how you'd debug it:
  - Usually the sentence is a **valid regex that only ever matches text resembling itself** — so it is dead in practice, yet will fire on the freak occasion that sentence appears verbatim in a command (quoting the pattern in an issue body does it), which reads as *intermittently alive* rather than dead.
  - It is **not strictly a literal match**. Ordinary punctuation is regex syntax: a trailing `.` is a wildcard, so the pattern matches a strict *superset* of itself; a `?` makes the preceding character optional.
  - **Unbalanced parentheses fail to compile**, and the hook swallows that: `except re.error: pass`. So a malformed prose pattern is dropped silently, arriving back at "never fires" by a completely different route — and with no error anywhere.

  **A prose pattern is indistinguishable from a working one until you test it**, and these three routes are indistinguishable from each other without compiling it yourself.

- **Assuming nothing catches prose for you.** `engram_add_lesson` and `engram_update_lesson` already **refuse** an obviously-prose `situation_pattern` at write time, unless you pass `bypass_prose_check: true`. The predicate is a literal-space count over 8 — chosen from a colleague round that measured 33 real patterns across three graphs: live command-shaped patterns carried 0–5 literal spaces, prose carried 16–70, and nothing landed in 6–15, so the threshold sits in an empty band.
  That gate means **long** prose is refused loudly at creation rather than dying quietly. The residual risk it cannot see is **short** prose — a six-word trigger slips under the threshold — which is exactly why the testing step above is still required even with the gate in place. If you ever find yourself reaching for `bypass_prose_check`, that is the moment to re-read this section rather than the moment to pass the flag.
- **Content without channel**: matching a token that also appears in your routine calls. It fires constantly, you learn to skim it, and it stops working *while still looking armed*.
- **Forgetting `(?s)` on a Bash target**: a heredoc body lands in the command string with *real* newlines, so without `(?s)` the match dies at the first one. (An MCP target is JSON-serialized, so its newlines are already escaped and `(?s)` changes nothing there — which is precisely why omitting it can pass every MCP test you write and still fail in production on the Bash path.)
- **Anticipating channels**: covering every surface you can imagine rather than the ones with incidents. Add channels one at a time, on evidence.
- **Assuming it blocks**: it does not. If the mistake is *destructive* and a nudge-after-the-fact is useless, a `situation_pattern` is the wrong tool — the tripwire will faithfully warn you about the `rm -rf` while it runs.
- **Arming without a must-not-fire test**: proving it fires is half the work. Proving it stays quiet on your ordinary calls is what determines whether it survives as a signal.
