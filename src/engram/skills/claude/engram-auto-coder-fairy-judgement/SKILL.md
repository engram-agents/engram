---
name: engram-auto-coder-fairy-judgement
description: "Decision rubric for whether to dispatch a coder-fairy at each PR-coding decision point when coder_fairy_policy is auto. Load at the delegation decision, not during implementation."
---

# engram-auto-coder-fairy-judgement

**When to load**: at each PR-coding decision point when `coder_fairy_policy == "auto"`.

**Purpose**: deterministic heuristic for whether to spawn a coder-fairy or do the work directly.

## Heuristic

Spawn a coder-fairy when ANY of the following conditions hold:

1. **Test files are touched** (`tests/` directory, `*_test.py`, `test_*.py`, or equivalent). Test changes have correctness implications that benefit from a fresh-eye pass.
2. **More than ~100 lines of substantive code change** estimated. Below this threshold, the fixed overhead (spec writing + dispatch + handoff) dominates per-line cost.
3. **Another fairy is already running on a stacked change.** Sequencing matters; the parallel fairy may be operating on related code.

Otherwise: do the work directly. Mentally spec the change first (what touches what, what could break, what tests apply) — the spec discipline applies even when you skip the fairy spawn.

## Per-invocation overrides

The user can override the heuristic in either direction:

- `"spawn a fairy"` / `"use coder-fairy"` / `"fairy this"` → spawn (full pipeline)
- `"do it directly"` / `"no fairy"` / `"just do it"` → do directly (skip fairy)

These overrides take precedence over the heuristic for the single invocation.

## Customization

Edit this file to fine-tune the heuristic for your workflow. The conditions, threshold, and override phrasings are all yours to adjust.

## Lineage

Initial heuristic calibrated by empirical input. Lean: aggressive toward staying direct.
