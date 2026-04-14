# ENGRAM — Reference

## The Epistemic Hierarchy

Your graph has strict levels — never blur them:

- **Evidence** (ev_): Raw source documents. Auto-created when you extract observations.
- **Observation** (ob_): Your interpretation of a specific passage from evidence. Has: exact quote, interpretation, atomic claim.
- **Derivation** (dv_): An inference from multiple observations or derivations. Has: logical chain, computed confidence.
- **Contradiction** (ct_): An explicit conflict between two claims.
- **Question** (qu_): An open research question.
- **Definition** (df_): What a term means in this graph's context.

## Reasoning Types

When creating derivations, specify `reasoning_type`:

- **Deductive** (0.98 discount): `deductive_modus_ponens`, `deductive_modus_tollens`, `deductive_hypothetical_syllogism`
- **Inductive** (0.70–0.95): `inductive_generalization` (corroboration), `inductive_causal`, `inductive_analogy`
- **Abductive** (capped): `abductive_best_explanation` (cap 0.80), `abductive_elimination` (cap 0.90)
- **Authority** (trust transfer): `authority_expert` (0.95), `authority_consensus` (0.98)

## Quote Types

Each observation's confidence comes from its `quote_type`:

| Type | Confidence | Use when |
|------|-----------|----------|
| `hard_data` | 0.95 | Verifiable numbers, measurements, official records |
| `official_statement` | 0.90 | On-record statements from named officials |
| `attributed_analysis` | 0.85 | Named expert analysis |
| `unnamed_source` | 0.55 | Anonymous sourcing |
| `editorial` | 0.35 | Opinion, commentary, speculation |

## Query Workflow

1. Search with `engram_surface` or `engram_query`
2. Drill into specifics with `engram_inspect` or `engram_get_subgraph`
3. Present answers with provenance: "Based on [source], [claim]..."
4. If the graph is insufficient, say so and search for new information

## Skills

- **`/research [topic]`** — Structured research session: search, extract observations, synthesize, identify gaps
- **`/nap`** — Quick consolidation before stopping (persists context to ENGRAM)
- **`/sleep`** — Deep end-of-session consolidation (nap + review + turn advance)
