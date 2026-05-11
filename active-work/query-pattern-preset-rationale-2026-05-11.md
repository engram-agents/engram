# engram_query_pattern preset recalibration — rationale memo

**Date:** 2026-05-11
**Author:** Borges
**Scope:** justify the symmetric ±0.10 cosine_threshold deltas for the `high_precision` and `high_recall` presets relative to v4-calibrated `balanced` (0.55)
**Branch:** `feat/query-pattern-preset-recalibration-2026-05-11`
**Companion code change:** `server.py:11716-11731` (`PATTERN_QUERY_PRESETS` dict)
**V4 calibration evidence path:** lives in the **kg-protocol-for-ai repo** (separate from alpha), at `~/kg/paper_research/threshold_calibration_2026-05-09/v4_tight_groups.py` — see `v4_tight_groups_run.txt` in the same dir for the run output. Not in the alpha repo tree.

## Context

`engram_query_pattern` (PR #29, merged 2026-05-07) ships three presets that bundle `cosine_threshold + top_k + min_confidence` for the precision/recall tradeoff:

| Preset | Pre-2026-05-11 values | Status |
|--------|----------------------|--------|
| `high_precision` | cosine 0.80, top_k 5, min_conf 0.70 | placeholder |
| `balanced` | cosine 0.55, top_k 15, min_conf 0.50 | v4-calibrated 2026-05-09 |
| `high_recall` | cosine 0.50, top_k 30, min_conf 0.00 | placeholder |

Lei noticed 2026-05-11 that the comment at `server.py:11719` explicitly admits *"high_precision and high_recall remain placeholders pending pattern-specific telemetry per ob_1500"* — and that the existing high_precision (0.80) is a +0.25 gap above balanced (0.55), which is structurally suspect because the high_precision placeholder was set when balanced lived at 0.65 (pre-v4). After the v4 calibration shift balanced down to 0.55, the high_precision delta widened from 0.15 to 0.25 by accident.

## The v4 study's empirical range

From `kg/paper_research/threshold_calibration_2026-05-09/v4_tight_groups.py` (ob_1612 + ob_1613, V2 expansion 170 nodes / 11 groups / 1272 INTRA + 13093 INTER pairs):

- **Below 0.45 cosine:** false-positive rate climbs sharply (semantically-unrelated topical co-occurrence pairs cross the threshold)
- **0.45 – 0.55 cosine:** broad-recall band; some loose-topical pairs admitted but recall is high
- **0.55 cosine (calibrated balanced):** F1-optimal operating point per V4
- **0.55 – 0.65 cosine:** precision-favoring band; loose-topical pairs filtered out but recall starts dropping
- **Above 0.65 cosine:** misses real corroborations (paraphrases of the same claim with different vocabulary stop crossing the bar)

The 0.45–0.65 band is the empirically-validated useful range. Outside that band, the tool surfaces signal that's either too noisy (below) or too narrow (above).

## Recalibration rationale — symmetric ±0.10 deltas

| Preset | Proposed cosine | Delta from balanced | Position in V4 useful range |
|--------|-----------------|--------------------|-----------------------------| 
| `high_precision` | 0.65 | +0.10 | upper edge of useful range |
| `balanced` | 0.55 (unchanged) | 0 (calibrated) | F1-optimal |
| `high_recall` | 0.45 | -0.10 | lower edge of useful range |

**Why symmetric ±0.10:**

1. **Anchored on V4 study's measured range edges.** 0.65 = upper edge above which corroborations start being missed; 0.45 = lower edge below which false positives spike. These aren't ad-hoc deltas — they're the V4-measured boundary values.
2. **Symmetric around the F1-optimal balanced.** Equal magnitude in both directions reflects the lack of a principled reason to skew the precision/recall tradeoff asymmetrically without pattern-specific telemetry.
3. **Meaningful separation between presets.** Pre-2026-05-11, high_recall (0.50) − balanced (0.55) = -0.05, a delta so small that high_recall and balanced returned nearly identical candidate sets in practice. The new ±0.10 ensures each preset has a perceptibly different operating point.

**Why top_k and min_confidence stay unchanged:**

- `high_precision.top_k` (5) and `high_recall.top_k` (30) are 3x balanced (15) — the precision/recall ratio in candidate-count is right. No need to adjust.
- `high_precision.min_confidence` (0.70) and `high_recall.min_confidence` (0.00) are appropriately skewed — high_precision filters out low-confidence noise, high_recall admits everything. Adjusting these without pattern-specific telemetry would be guessing.

## What this recalibration is NOT

**Not a pattern-specific calibration.** The right long-term answer per ob_1500 is per-pattern telemetry: stale_load_bearing's optimal threshold may differ from open_question_answerable's. The telemetry log already exists (`~/.engram/pattern_query_telemetry.jsonl`); after ~weeks of real-use data, we can compute per-pattern operating points.

**Not a config-loadable change.** The `PATTERN_QUERY_PRESETS` dict stays hardcoded in `server.py`. Whether to make it config-loadable (Tier 2 in issue #76's tier model) vs keep hardcoded (Tier 3) is the open design question that issue #76 frames — this PR doesn't pre-commit either way.

**Not new empirical work.** No new data was collected for this recalibration; the rationale is "given the v4 data, what would a careful reader pick for high_precision and high_recall to bracket balanced inside the empirically-validated useful range?" The answer is the symmetric ±0.10 endpoints of that range.

## Verification

The only behavioral change is the two cosine_threshold values. All other preset fields unchanged. Existing tests pass:

```
$ python3 -m pytest tests/test_query_pattern.py
============================== 15 passed in 2.14s ===============================
```

Telemetry log structure unchanged — accumulated calibration data remains useful for future per-pattern refinement.

## Future work

1. **Per-pattern operating points** (ob_1500 long-term path) — once telemetry log accumulates enough real-use data, compute per-pattern thresholds that maximize each pattern's specific objective (different patterns have different precision/recall targets — e.g., `cornerstone_candidate` should err toward precision; `recent_resolution_echo` should err toward recall).
2. **Config-loadability decision** (issue #76 — tier model). If `PATTERN_QUERY_PRESETS` becomes Tier 2 (user-tunable), migrate to `_get_query_patterns_config()` following the `_get_thresholds_config()` precedent from PR #62.
3. **Cross-pattern correlation analysis.** If telemetry shows certain patterns produce highly correlated candidate sets (e.g., stale_load_bearing and cornerstone_candidate often surface the same nodes), the dispatch logic might benefit from de-duplication across patterns rather than just within-pattern.

## Related

- PR #29 (2026-05-07) — original engram_query_pattern + 6 patterns
- PR #58 (2026-05-10) — v4 similarity-threshold recalibration (the empirical anchor for this memo)
- PR #62 (2026-05-10) — polarity-dedup config-loadable pattern (precedent for #76's Tier 2 follow-up)
- PR #73 (2026-05-11) — dream-fairy wiring + SKILL.md entry
- Issue #76 (2026-05-11) — config-surface tier model (covers whether this dict should be config-loadable)
- ob_1500 — engram_query_pattern design decision + telemetry-first calibration discipline
- ob_1612 / ob_1613 — V4 calibration study results
- ls_0023 — empirical calibration discipline before flipping defaults
