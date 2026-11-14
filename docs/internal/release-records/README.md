# Release records

One file per release, read by release gate G3 (the skip-set comparison, `docs/internal/RELEASING.md` step 4).

## `vX.Y.Z-skips.json`

The skip set of a release's recorded whole run: which tests did not run, and why. The next release's gate seat compares its own whole run against it, so a test that newly stops running is named instead of hiding inside a green total.

| Field | Meaning |
|---|---|
| `release`, `commit` | the release and the full commit id the run was on |
| `interpreter`, `platform` | where it ran |
| `run` | the run's own label |
| `reason_source` | where the skip set and the skip reasons came from |
| `collected` | test ids collected by `pytest tests/ src/forum/tests/ --collect-only -q` |
| `counts` | outcomes by kind (`passed`, `failed`, `skipped`, `xfail`, `xpass`, `error`); they sum to `collected` |
| `skipped_total` | the number of skipped ids, equal to `counts.skipped` and to the ids listed in `skipped_by_reason` |
| `skipped_by_reason` | every skipped test id, grouped under its skip reason |
| `skipped_without_reason` | skipped ids with no recorded reason (0 when the reasons were captured) |

Two kinds of reason depend on the machine, not the code, so compare runs made the same way: reasons about the clone's git state (for example "HEAD is already contained in origin/dev") and reasons about optional packages missing from the interpreter.

## `v0.5.6-skips.json`

The first reference, made after the fact for v0.5.6 (`f2234796`), the release before this procedure existed. The skip set comes from the recorded whole run on 2026-11-10 (five shards, the `tests.yml` knobs, Linux, Python 3.12): 38,143 ids, 37,672 passed, 471 skipped, 0 failed. That run printed outcomes only, not reasons, so the reasons come from a second run of exactly those 471 ids at the same commit on 2026-11-12, in which all 471 skipped again.

## `v0.5.7-skips.json`

The skip set of the gate seat's G2 whole run on v0.5.7-rc2 (`96085799`, 2026-11-13; fresh public clone, five shards, the `tests.yml` knobs, Linux, Python 3.12): 38,817 ids, 38,341 passed, 476 skipped, 0 failed, reasons from a re-run of those ids at the same commit. The release commit `47328707` differs from rc2 only in `docs/CHANGELOG.md`; on 2026-11-14 the 476 ids were re-run there and all skipped again with the same reasons. It is the gate seat's run and not the hosted run on the release pull request, because the hosted runner skips more (592) and the next release's comparison must set like against like.

## `vX.Y.Z.md`

The release record (`docs/internal/RELEASING.md` step 10): who held which seat, the candidates, each gate with its run and result, the ids of the release, the soak, and what was not run. `v0.5.7.md` is the first.
