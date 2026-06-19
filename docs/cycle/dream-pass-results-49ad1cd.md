# Observed dream pass — results addendum (tip 49ad1cd)

*Author: Ariadne (install owner). The §4.3 observed dream pass, run on my live
production graph at the final ship-state tip. Cross-read requested: Borges, Luria, Mira.*

## Verdict: **PASS** — all criteria met. #954 cleared for merge.

## Tip + run metadata

- **Validated tip:** `49ad1cd` = `dev` (015900b) merged into the cycle branch for
  ship-state. The raw cycle tip 25c0bce was **base-stale** by 7 dev commits that
  touch the sleep path the pass exercises (#952 dream-fairy, #957 legacy-sleep
  retirement, the engram-sleep skill, the dream-fairy agent, lifecycle/observation/stats).
  Validating raw 25c0bce would have tested stale sleep-path code, not what ships.
  `dev` merged into the cycle branch cleanly (0 conflicts); full suite re-run green
  on the merged tip (**2956 passed, 31 skipped, exit 0**).
- **Run:** full `engram-sleep` routine (Phase A cohort completion + Phase B
  dream-master consolidation with the 6-dream-fairy + 4-batch-summary cohort), run
  normally — no special-casing, no skipped steps, no flags. Turn 19 → 20,
  2026-06-08 ~21:43 EDT (2026-06-09 01:43Z).
- **Preconditions:** P1 Mira APPROVED 4/4 · P2 droppability property verified via
  `_node_fs_class` proxy-fallback (the literal git-revert instrument was stale on the
  moved tip — refactor drift from #49, not entanglement) · P3 suite green on 49ad1cd ·
  P4 plugin built from 49ad1cd (tier=dev, multi-agent, MCP reconnect verified) ·
  P5 baseline + VACUUM-INTO rollback snapshot.

## Criteria (ob_0507 checklist, amended by ob_0510 for the moved tip)

The protocol's original criterion 3 ("flags-off → zero advisory") was **stale**: the
#49 un-nest makes FALSIFICATION fire on the `quote_type` proxy with zero flags and
zero standpoint data. ob_0507 split it; ob_0510 amended 3c/4 for the already-migrated
schema + 2 native-fs nodes.

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Completion — Phase A+B complete, dream record written, turn +1 | **PASS** — dream record `~/.engram/history/dream/2026-06-08.md` (11KB), sleep-success marker (turn→20, 50 consolidated), turn advanced exactly once. ~3 non-fatal engram tool-call errors during fairy/DM exploration (index-out-of-range / bad-keyword-arg / a `/tmp/safety_check.py` harness-sandbox permission-denied) — **none in the cycle substrate's standpoint/F-S code paths**, none affecting outcome/integrity. |
| 2 | Frozen-metric — health_score ≥ baseline | **PASS** — 88.0 → 88.0 (Δ 0.00), engine-read. |
| 3a | STANDPOINT lines = ZERO | **PASS** — 0 real firings (structural: graph carries 0 standpoint fields). |
| 3b | ⚠⚠ composite = ZERO | **PASS** — 0 (needs `standpoint_lineage`; graph has 0). |
| 3c | FALSIFICATION fires correctly on derivations with ≥1 known-fs leaf | **PASS** — fired on the single derivation of the pass (dv_0120, Phase A): `FALSIFICATION: 1/3 re-executable; 2/3 re-executable-leaning (proxy:quote_type)` — native-field leaf (ob_0509) split from proxy leaves (ob_0507/0510), counts summing to all leaves, proxy labels correct. The dream-master was **action-light** (0 derive/supersede/resolve calls — tool_timing unchanged 72/12/4), so it correctly produced **0** advisory firings (the advisory only fires on premise-validating writes; nothing to fire on). |
| 4 | Schema additive-only | **PASS** — 60 cols, post == baseline. 4(a)'s 55→60 delta is historical/idempotent (migration ran during the prior 58185fd install); 4(b) verified the pass changed nothing. |
| 5 | No new integrity damage | **PASS** — tainted 0→0, support_lost 0→0, orphan 16→16, stale 1→1, dag_violations 1→1 (pre-existing). Node growth +4 = legitimate Phase-A + dream-feeling additions; no taint minted. |
| 6 | Advisory non-mutation (the load-bearing additive check) | **PASS** — dv_0120 created at normal propagated confidence (0.95) with no taint; the FALSIFICATION line lived only in `structure_warnings`; tainted stayed 0 throughout. |

## What the pass proves

The cycle substrate (standpoint v3 + native F-S field) is **invisible/additive** to a
normal consolidation on a real production graph at ship-state. The non-zero-diff
prediction held exactly: FALSIFICATION fires on the `quote_type` proxy, STANDPOINT/⚠⚠
stay zero, and the advisory never mutates node state.

## Single-shot void rule

This pass validates tip **49ad1cd**. Per §single-shot, any *executable* substrate
commit after this voids it (AST-equality after docstring strip). This addendum is
docs-only (cycle/ process-doc, excluded from check-diff) → AST-non-voiding → the pass
stands. **Forward caveat:** if `dev` advances with sleep-path changes before #954
merges, the merge would no longer equal the validated tree and re-validation is owed.

## Notes

- **#735 reproduced:** the `cohort_dispatch incorporate` non-cumulative bug regressed
  the clean recall-summary count 49→44 on the second incorporate; worked around by a
  manual merge to a correct 50/50 final_payload (43 clean + attempt-1 + attempt-2,
  all validated). Recurring (ob_0212 / ob_0325 / tonight) — tracked.
- **Dream-master consolidation:** action-light by design (healthy ship-state) — 31
  recall summaries applied, 19 skipped (benign: 15 superseded-backfill whose live head
  already carries a summary, 4 retracted), one fairy write-suggestion (ob_0097
  supersede) rejected on verification as an ls_0012 subject-swap, 4 items flagged for
  Lei (cornerstone-candidate backlog, qu_0003 split, two-regime trust derivation held
  for charter, ct_0001 denorm).

Full provenance: ob_0508–ob_0514 + dv_0120 (Ariadne's graph).
