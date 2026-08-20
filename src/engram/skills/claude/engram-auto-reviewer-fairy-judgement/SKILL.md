---
name: engram-auto-reviewer-fairy-judgement
description: "Decision rubric for whether to dispatch a reviewer-fairy at each PR-review decision point when reviewer_fairy_policy is auto. Load at the review-dispatch decision."
---

# engram-auto-reviewer-fairy-judgement

**When to load**: at each PR-review decision point when `reviewer_fairy_policy == "auto"`.

**Purpose**: deterministic heuristic for whether to spawn a reviewer-fairy or do the review directly.

## Scope gate — check this BEFORE the heuristic below

This heuristic governs only one decision: **an author's own dispatch choice on their own diff.** It does not transfer to the **colleague seat** in a multi-agent repo's reviewer-fairy → colleague → maintainer pipeline. If the PR under review is a counterpart's (not mine), and I am the colleague/counterpart reviewer, review it directly as the full agent — never dispatch a reviewer-fairy for that seat, regardless of size or test-touch. The colleague layer exists specifically to catch what a single-diff-isolated fairy structurally cannot (the diff's relationship to a moving target branch, cross-PR context, a fresh full-context read) — delegating it to a fairy skips the layer even when the fairy's findings turn out correct. (Caught live on engram-alpha PR-1904, 2026-08-06: a colleague dispatched a reviewer-fairy for their own colleague-review duty on a counterpart's PR, applying the author-side heuristic to the wrong seat.)

So: first ask *whose diff is this, and which seat am I filling* — author or colleague. Only apply the heuristic below when the answer is "author, my own diff."

## Heuristic

Spawn a reviewer-fairy when ANY of the following conditions hold:

1. **Test files are touched** (`tests/` directory, `*_test.py`, `test_*.py`, or equivalent). Test changes have correctness implications that benefit from a fresh-eye pass.
2. **More than ~100 lines of substantive code change** estimated. Below this threshold, the fixed overhead (spec writing + dispatch + handoff) dominates per-line cost.
3. **Another fairy is already running on a stacked change.** Sequencing matters; the parallel fairy may be operating on related code.

Otherwise: do the review directly. Apply the same rigor — read the diff, check correctness, flag risks — without the dispatch overhead.

## Per-invocation overrides

The user can override the heuristic in either direction:

- `"review-fairy this"` / `"just review"` / `"spawn reviewer-fairy only"` → spawn reviewer-fairy (even if below threshold)
- `"review it yourself"` / `"no reviewer-fairy"` → review directly (skip fairy)

These overrides take precedence over the heuristic for the single invocation. The reviewer-only invocation works post-commit too (`"review what I just wrote"`).

## Customization

Edit this file to fine-tune the heuristic for your workflow. The conditions, threshold, and override phrasings are all yours to adjust.

## Lineage

Initial heuristic calibrated by empirical input. Lean: aggressive toward staying direct.
