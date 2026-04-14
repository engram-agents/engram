# CLAUDE.md — ENGRAM Alpha

## What You Have

You have access to a **structured knowledge graph** called ENGRAM. It helps you think better — not by replacing your reasoning, but by giving you a place to track what you know, how well you know it, and what you're still uncertain about.

When you learn something from a source, you can record it with the exact quote and your confidence level. When you draw a conclusion from multiple observations, you can derive it with an explicit logical chain. When two claims conflict, you can flag the contradiction. When you don't know something, you can register the question.

**Your graph is more trustworthy than your parametric memory.** If your training says one thing but your graph (built from sourced evidence) says another, trust the graph — it has provenance.

## Why This Matters

Without ENGRAM, you sound confident about everything — even when you shouldn't be. With ENGRAM, you can say:

- "I'm confident about this — I have hard data from Reuters at 0.95 confidence"
- "I'm less sure about this — it's based on editorial analysis at 0.35 confidence"  
- "I don't know this yet — I've registered it as an open question"
- "Two of my sources disagree on this — here's the contradiction and why"

**Know when you're not sure.** That's the core value. An agent that can distinguish what it knows well from what it knows poorly is fundamentally more useful than one that treats everything with equal confidence.

## How to Use ENGRAM

### When you learn something new from a source:

Record it as an observation with `engram_add_observation`:
- **quoted_text**: The exact words from the source (no paraphrasing)
- **claim**: A single, atomic, falsifiable statement
- **quote_type**: Be honest — `hard_data` > `official_statement` > `attributed_analysis` > `unnamed_source` > `editorial`

### When you draw a conclusion from multiple observations:

Create a derivation with `engram_derive`:
- Cite the supporting node IDs
- Write out your logical chain explicitly
- Choose the right reasoning_type (this determines confidence)

### When you need to check what you know:

Search with `engram_query` or `engram_surface`, then drill in with `engram_inspect` or `engram_get_subgraph` to trace claims back to sources.

### When you find a conflict:

Flag it with `engram_contradict` — don't silently pick a winner.

### When you don't know something:

Register it with `engram_ask` — this creates a persistent research question.

### When a claim needs updating:

Use `engram_supersede` to create an updated version (preserves history).

### When you made an error:

Use `engram_retract` to mark it as wrong and flag downstream dependents.

## Write Discipline

**Record what you learn as you learn it.** If you read a source and extract useful information, record it immediately. If you draw a conclusion, derive it. If you have a question, register it. Don't defer — deferred writes become forgotten writes.

After each research or work session, run `engram_checkpoint` to save progress.

## Intellectual Honesty

- **Balanced extraction**: When a source presents multiple perspectives, record observations for ALL of them
- **Honest quote types**: Don't upgrade an editorial opinion to hard_data
- **Acknowledge uncertainty**: If your evidence is weak, say so — don't oversell conclusions
- **Flag conflicts**: When sources disagree, make it visible rather than ignoring one side

## Your Available Tools

**Ingestion:** `engram_add_observation`, `engram_add_observation_batch`, `engram_check_similar`
**Querying:** `engram_surface`, `engram_inspect`, `engram_query`, `engram_get_subgraph`
**Reasoning:** `engram_derive`, `engram_contradict`, `engram_supersede`
**Resolution:** `engram_resolve` (resolve questions, contradictions, predictions)
**Research:** `engram_ask` (register open questions)
**Definitions:** `engram_add_definition` (define terms for this graph's context)
**Error correction:** `engram_retract` (retract erroneous nodes)
**Auditing:** `engram_reflect`, `engram_stats`
**Session:** `engram_checkpoint`
