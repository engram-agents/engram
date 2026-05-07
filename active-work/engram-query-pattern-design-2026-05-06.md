# engram_query_pattern — KnowQL-inspired pattern query tool

**Status:** design captured (not yet implemented)
**Started:** 2026-05-06 morning (formalization of 2026-05-05 PM decision)
**ENGRAM pointer:** ob_1500 (decision), ls_0023 (telemetry-first migration discipline)
**Design source:** ob_1500 ("design looks very clean") — Lei + Borges, 2026-05-05 PM
**Branch:** none yet — gated on calibration discussion + accumulation of telemetry tuples
**Depends on:** the antml-fix MCP-tool reshape (ob_1499) is **not** a hard prerequisite, but if the reshape lands first, this tool ships with the new single-JSON-payload signature shape from day one.

## Goal

Add ONE new MCP tool — `engram_query_pattern(pattern_name, **filter_args)` — that exposes a small library of named graph patterns over the claim store. Compositional graph queries that today require multi-call chains (a list + N inspects + filter logic) become single-call patterns the agent can compose orthogonally.

The tool does **mechanical work only**. It returns ranked candidates with scores. The agent does **judgment work** on the candidates — applying domain standards (sufficient evidence, semantic clusters, taint still applying). This layer separation is the core of the design and emerged from Lei's pushback on an earlier draft that conflated the two layers.

## What's in scope (v0.1)

### Tool signature

```
engram_query_pattern(
    pattern_name: str,         # one of the named patterns below
    preset: str = "balanced",  # high_precision | balanced | high_recall
    # plus pattern-specific filter args (date range, importance, type, etc.)
    # plus per-call overrides for any preset parameter
) -> str  # JSON: ranked candidates, scores, threshold, top_k
```

### Three named presets (parameter bundles)

Each preset is a bundle of (cosine_threshold, top_k, recency_weight, importance_weight). The exact numbers are deferred to a calibration discussion (per ob_1500: *"this design looks very clean, we can discuss how to calibrate the three default parameter sets later"*).

| preset | optimizes for | typical use |
|---|---|---|
| `high_precision` | few false positives | "is there an answer to this question already?" / "did we already record this?" |
| `balanced` (default) | the middle of the curve | most agent-side scans |
| `high_recall` | few false negatives | dream-fairy consolidation scans where missing a candidate is more costly than false-positive triage |

Each preset's parameters can be individually overridden by passing the parameter name as a kwarg.

### Pattern library — starts with the six dream-fairy categories (ob_1498)

| pattern_name | what it surfaces |
|---|---|
| `open_question_answerable` | open questions with a derivation chain nearby that would resolve them |
| `contradiction_obsolescence_ready` | active contradictions where one side is retracted/superseded — ready for resolution |
| `stale_load_bearing` | high-importance + low-recent-recall, excluding cornerstone-anchored |
| `cornerstone_candidate` | high-importance non-cornerstone nodes that have been persistently load-bearing |
| `tainted_still_valid` | tainted derivations whose substantive claim survives the cited retraction |
| `recent_resolution_echo` | recent resolutions that should propagate to similar still-open questions |

The two heuristic dream-fairy categories (observation_clusters_synthesizable, lesson_candidate_repeating_incident) are **not** in v0.1 — they require semantic clustering judgment the tool layer shouldn't do.

### Mechanical primitives the tool uses internally

- Graph queries with typed predicates (engram_get_subgraph extensions for richer edge filters).
- FTS5 keyword search (existing engram_query path).
- Cosine similarity over embeddings with configurable threshold (existing EmbeddingManager).
- Metadata filters (date range / importance band / confidence band / node_type / status).
- Score-distribution logging on every call: `(query_pattern, query_args, candidate_id, score, agent_action_if_followup)` — feeds the calibration loop.

## What does NOT ship in v0.1

- The two heuristic dream-fairy patterns (deferred to v0.2 once the agent-side judgment criteria are codified enough to be worth tooling).
- A general "compose your own pattern" DSL — the named-pattern registry is intentionally curated to keep the agent surface small.
- Calibrated default parameter values — those come from the calibration run + telemetry analysis.
- Any cross-agent / federated pattern (queries span only the local graph).
- A REPL or interactive query mode — strictly programmatic / agent-driven.

## Calibration plan (telemetry-first, per ls_0023)

This design carries an empirical-validation discipline lifted from ls_0023's antml-fix prescription:

1. **Pre-implementation:** ship the tool with placeholder defaults. ALL calls log a tuple: `(timestamp, pattern_name, args, candidate_id, score, was_followed_up_by_agent)`.
2. **Calibration run:** seed the pattern library from a small known-truth set (questions with known answers in the graph; contradictions with known resolutions). Score the recall and precision per preset. Use that to set initial defaults.
3. **Alpha-period telemetry:** after deploy, accumulate real-use tuples. Refine defaults based on agent-action signal (was the candidate acted on, ignored, or flagged as wrong?).
4. **Per-tool telemetry comparison:** if a future change adjusts the score function or preset bundles, gate it with the same total-error-rate-per-N-calls discipline ls_0023 prescribes for the antml-fix migration.

## Composes with

- **Parallel-fairy architecture (ob_1498):** the six fairies use this tool for tighter scans when it's available; until then, they fall back to the multi-call composition pattern they currently use.
- **antml-fix MCP-tool reshape (ob_1499):** if the per-tool single-JSON-payload migration ships first, this tool's signature collapses to a single payload parameter automatically (one args object with `pattern_name` and the filter kwargs nested in).
- **Cornerstone / focus-set tools:** `cornerstone_candidate` pattern reads from `engram_list_focused()` and `engram_focus_sets()` to know what's already anchored.

## Open questions

- **Default cosine thresholds** — deferred per ob_1500. Need a small calibration run before merge.
- **`top_k` cap per preset** — affects token budget on the agent side; choose with cognitive-burden in mind, not just statistical recall.
- **Pattern-specific `--explain` flag?** Sometimes useful to trace why a candidate ranked high. Probably v0.2.
- **Cross-pattern composition** — could two patterns be chained server-side (e.g., "stale_load_bearing AND in_focused_set")? Not v0.1; possibly v0.2 if a real use case shows up.

## Footnotes

- The KnowQL inspiration is the trajectory-design framing in Camlin (2025) RC+ξ §5.4 — declarative compositional patterns over a typed graph. ENGRAM's claim graph is a narrower domain, so the pattern library is curated, not user-extensible.
- Why "pattern" not "query"? `engram_query` already exists (FTS5 + semantic over claims). "Pattern" signals the compositional layer above point-tools.
