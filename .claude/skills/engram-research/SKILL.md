---
name: engram-research
description: Structured research session — search for information on a topic, extract observations with proper provenance, synthesize derivations, and identify gaps. Use when you want to build reliable knowledge about a subject.
---

# ENGRAM Research — Structured Knowledge Building

You are conducting a research session on a topic. The goal is to build reliable, sourced knowledge in the graph — not to answer a question from memory. Everything you learn gets recorded with full provenance so you can trust it later.

---

## Step 1 — Check what you already know

Before searching for new information, check your existing knowledge:

```
engram_surface(query="<topic>")
```

If relevant nodes exist, inspect them with `engram_inspect` or `engram_get_subgraph`. Note what you already know and what's missing. Don't duplicate existing work.

## Step 2 — Search and read sources

Search the web for information on the topic. Read sources holistically before extracting claims. Look for:

- Multiple independent sources (not just one perspective)
- Primary sources over secondary analysis
- Recent information where timeliness matters

**Don't cherry-pick.** When a source presents multiple perspectives, you'll record observations for ALL of them — not just those supporting a preferred conclusion.

## Step 3 — Extract observations

For each source, extract the relevant claims as observations:

```
engram_add_observation(
    url="https://...",
    title="Source Title",
    source_date="2026-01-15",
    quoted_text="Exact words from the source",
    interpretation="Your reasoning about what this means",
    claim="Single, atomic, falsifiable statement",
    quote_type="hard_data"  # or official_statement, attributed_analysis, unnamed_source, editorial
)
```

For each observation:
- **`quoted_text`** must be the exact words from the source — no paraphrasing
- **`claim`** should be a single, testable statement — not a paragraph
- **`quote_type`** must honestly reflect the source's epistemic weight:
  - `hard_data` (0.95) — verifiable numbers, measurements, official records
  - `official_statement` (0.90) — on-record statements from named officials
  - `attributed_analysis` (0.85) — named expert analysis
  - `unnamed_source` (0.55) — anonymous sourcing
  - `editorial` (0.35) — opinion, commentary, speculation

**Act on similarity results.** Each observation returns `similar_existing` matches. If the same claim already exists from the same source, it's a duplicate — retract the new one. If the same claim comes from a different source, that's corroboration — create an `inductive_generalization` derivation.

Use `engram_add_observation_batch` when extracting multiple claims from the same source (same `url` and `title` for all).

## Step 4 — Synthesize

After extracting observations, think across them:

1. **Cross-reference**: Do independent sources converge on the same claim? Create corroboration derivations with `engram_derive` using `reasoning_type="inductive_generalization"`.

2. **Detect contradictions**: Do sources disagree? Flag conflicts with `engram_contradict` — don't silently pick a winner.

3. **Form your assessment**: Create derivations with explicit reasoning chains. Choose the `reasoning_type` that matches your argument:
   - **Deductive** (0.98 discount): `deductive_modus_ponens`, `deductive_modus_tollens`
   - **Inductive** (0.70-0.95): `inductive_generalization`, `inductive_causal`, `inductive_analogy`
   - **Abductive** (capped): `abductive_best_explanation` (cap 0.80), `abductive_elimination` (cap 0.90)
   - **Authority** (trust transfer): `authority_expert` (0.95), `authority_consensus` (0.98)

4. **Identify gaps**: What's still unknown? What would change your conclusion? Register these with `engram_ask`.

## Step 5 — Report

Tell the user what you found:

- Present your findings with provenance: "Based on [source], [claim]..."
- Be explicit about confidence levels: "I'm confident about X (hard data, 0.95) but less sure about Y (editorial analysis, 0.35)"
- Flag any contradictions between sources
- List the open questions you registered

If the graph is insufficient to answer the user's question, say so explicitly rather than filling in from parametric memory.
