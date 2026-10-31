# Metric gates: the standing policy

The standing rules for every launch that sets a metric gate: a gate is an interval against a pre-registered band, never a point against a line. Section 0 is the case that produced the rules, section 1 the rules (R1 to R12), section 2 the tool and where it is installed from, section 3 the status. The rules carry no names: whoever sets a band is "the design owner" of that lane.

## 0. The case that produced this

EngramManager issue #3740: the gate for routing a hook's reads through the owner process read "sequential p50 owner/baseline ≤ 1.0 in three runs". On a ratio whose run-to-run spread is about 1.6%, an equal-speed path fails that about seven times in eight. Nine runs on one seat gave mean 0.9967, 90% CI [0.987, 1.007], p = 0.54 against 1.0: neutral within ±3%, yet the gate read "not met" twice and a human had to override. The gate measured noise, not cost. Every rule below is the generalisation of that one defect.

## 1. Policy (the rules)

**R1. Pre-register the gate before the data.** Before the first measured run, write down: the metric and its unit; the population it ranges over (which seats, builds, stores, settings); the instrument and its version; the comparison design; the unit of analysis (R7); the planned n; the band; the verdict rule; who decides each band. **A verdict read on data that predates the gate is labelled POST-HOC and carries the smallest margin under which it holds**; it stands only if that margin is one nobody would reject (the first case: the 3.45.1/ON PASS holds for any margin at or above 0.65%, since its 90% upper bound is 1.0065), otherwise it waits for announced runs (the design owner's rule, 2026-10-30).

**R2. The verdict is an interval against a band, never a point against a line.** Every gate reports the effect estimate with a confidence interval (90% two-sided by default, which is the two one-sided tests at α = 0.05) and compares the interval to the pre-registered band. The sign of a point estimate is never a verdict. **A "no worse" gate is one-sided (non-inferiority):** it passes when the interval's bad edge is inside the margin, and a clearly better result passes by construction (the design owner's correction, 2026-10-30: a symmetric band would have failed an owner path measured at 0.87–0.905 for being too fast). A two-sided equivalence band is for claims of "unchanged" (an A/A control, an output that must not move), not for "no slower".

**R3. Three bands, decided by the interval's bad edge and the estimate, in this order.** At the planned n every gate lands in exactly one:
1. PASS (neutral or better): the interval's bad edge is at or inside the margin. Nothing to discuss.
2. BLOCK (large regression): the estimate is beyond the block bound (stricter variant, when the owner wants it: the interval's good edge is beyond it, so the large regression is shown, not merely possible). A wide interval whose estimate sits inside the bound is 'not enough data', never a block: blocking on the bad edge alone re-introduces noise-blocking one level up (the design owner's check on six contended-p99 ratios: mean 1.01, 90% CI [0.81, 1.21], a neutral estimate that an edge rule would have blocked).
3. TRADE-OFF (everything else): decided by the lane's design owner with the structural or safety gain named in writing; the project owner informed. 'Excludes zero' plays no role; the margin and the bound decide.
INCONCLUSIVE exists only in a sequential design (mSPRT or confidence sequences), where the gate may legitimately read 'continue'. A fixed-n gate has no verdict before its planned n and therefore nothing to call inconclusive.

**R4. The band comes from what the number means, not from the data.** The equivalence band is the smallest effect anyone would act on (the "smallest effect size of interest"): for latency, stated in absolute units per event times the event rate (a hook that fires per prompt; a tool that runs nightly), with a relative floor so a tiny absolute number on a fast path is not read as a large relative one. When no meaning-based band exists, fall back to the instrument's measured spread with the power analysis of R5, and say so. Suggested defaults for per-prompt hook latency (the B1 numbers the design owner set on #3740, 2026-10-30): PASS when the 90% CI upper bound of the p50 ratio is ≤ 1.03 (about 0.15 ms per fire, a chosen margin, not the noise; the noise only sets n), n ≥ 6; TRADE-OFF up to 10% or 0.5 ms per fire; BLOCK beyond that. Defaults are proposals; the design owner sets and records them.

**R5. Sample size is designed, not "three runs".** n comes from a pilot estimate of the **run-level** spread (R7), the margin, α and target power (0.8 by default) for the one-sided test. The tool computes it. With the measured run-level sd of 0.016 on the ratio, six runs give a 90% half-width of about 0.013, which is why six to ten is the working range and three is not.

**R6. No peeking, no repeat-until-pass, no dropping.** A fixed-n verdict is read once, at the planned n, and is final. More data is allowed only under a plan declared before the first look: group-sequential with its per-look level (default a 95% interval at every look, up to three looks), or always-valid (mSPRT / confidence sequences). An extension announced after a fixed-n verdict is a second look and does not hold the error rate: the design owner's simulation of B1 itself (true ratio at the margin, run sd 0.016, 90% interval, 6 runs then 3 per look, 200,000 trials) gives a false PASS of 4.95% at one look, 7.6% at two, 9.5% at three and 16% at ten, and once the first look has spent the 90% interval no later level restores 5%; a 95% interval at every look holds 5% for up to three looks, and an equal-speed path still passes 98.75% at n = 6. Every recorded run on a cell counts; none is dropped. A gate that is re-run until it passes has no verdict.

**R7. Paired, interleaved, logged, and the run is the unit.** Compare on the same host in the same window, alternating ON/OFF (or A/B) phases, order randomised or alternated, with host load and the instrument's build recorded at each run. **The unit of analysis is the run (or a block of interleaved pairs), never the individual fire:** fires within a run share host state, so 300 fires times k runs analysed as independent give intervals far too narrow (pseudoreplication). The tool takes per-run statistics, or a cluster bootstrap over runs. An A/A comparison (OFF vs OFF, or the same build twice) is the instrument's control and must span several runs; it must read PASS in both directions, or the instrument, not the change, is what moved.

**Why `paired` is a required field (the EngramManager design owner's measurement on real A6 cells, 2026-10-31).** The A6 tool runs the baseline and the owner path interleaved inside one run, so each run is a pair, and the band was set on the per-run ratio. A Welch interval across runs puts the between-run load swing into both arms' variance and so reads a different, wider quantity than the one the band was set on. The same `tost_mean` run both ways on real A6 cells (SQLite 3.53.0, n = 6 runs per cell, margin 0.03, `log_ratio`, alpha 0.05):

| cell | ratio | paired 90% CI | Welch 90% CI | Welch width / paired width |
|---|---|---|---|---|
| tripwire, synthetic store | 0.8642 | [0.8608, 0.8677] | [0.8546, 0.8740] | 2.81x |
| session-start, real-store copy | 0.1091 | [0.1079, 0.1103] | [0.1070, 0.1113] | 1.79x |
| session-start, synthetic store | 0.0766 | [0.0761, 0.0772] | [0.0755, 0.0778] | 2.09x |

The estimates agree; the Welch interval is 1.8x to 2.8x wider (ratio of interval widths from the rounded bounds). None of these cells changes verdict, because all sit far below 1.03; for a cell near 1.0 an interval two to three times wider can cross 1.03 and turn a PASS into a TRADE-OFF. Hence the gate file states `paired` and `judge` pairs the arms by `run_id` (the id sets must match), with no default.

**R8. Tails are their own metrics.** p50 and p99 (or any tail) have separate bands and separate intervals. Percentile intervals come from the bootstrap (BCa when skewed), never from a t-interval on the raw latencies. Means of heavy-tailed data get a bootstrap interval too.

**R9. Several metrics, named roles.** Each gate names one primary metric and any guardrails. Guardrails are non-inferiority tests (one-sided: not worse than the bound). With k metrics tested at once, either correct (Holm for strict control, Benjamini–Hochberg for dashboards) or state the per-metric α and accept the family-wise risk in writing.

**R10. Counts and rates use exact methods.** Direct store opens (must be 0), lock-wait rate (≤ 1%), timeouts (0): these are count gates. A zero-count rule stays a hard rule. A rate gate gets a Wilson or Clopper–Pearson interval and is read against its band like any other.

**R11. Correctness is a separate gate from performance.** Output equality (owner stdout == baseline stdout) is binary and never traded off; it is checked before any latency number is read.

**R12. The report is a fixed table.** Metric · population · instrument + version · design · n planned / n used · estimate · CI · band · verdict · host, build, load · data file + hash. This table is the artifact the conclusion-warrant box reads; a gate without it has no verdict.

## 2. Tool

The library is its own repository, shared by every project that sets a gate: `engram-agents/metricgate`, at tag `v0.1.0`. It depends on numpy and scipy only. It provides `preregister` (validate the R1 fields and stamp the gate), `tost_mean`, `bootstrap_ci`, `rate_ci`, `power_n`, `aa_check`, `verdict`, `adjust_p`, `judge` (read a cell against its gate, counting runs from the records), `report` (the R12 table), and a `metricgate` command line. Its README and `src/metricgate/{core,gate,cli}.py` are the API.

**Install it into a development venv, never into the plugin venv or the requirements.** engram-alpha's use of it is dev tier (T3): the engram-specific parts below never ship to a user install, and the library is an optional dependency of the development tools.

```
pip install "metricgate @ git+ssh://git@github.com/engram-agents/metricgate@v0.1.0"
```

A seat without it still runs every tool: the tool prints one line naming this install line and leaves its other output unchanged.

**engram-alpha's dev-tier parts**

- `src/engram/tools/metricgate_adapter.py` (linked as `tools/metricgate_adapter.py`) is the ONE import site of the library: `have_metricgate`, `require_metricgate`, `append_run_record`, `judge_cell`. No other file imports `metricgate`.
- `tools/engramd_a6_latency.py --emit-runs DIR` appends, after a completed run, two run records (one per path, one shared `run_id`) for the gate cell's sequential p50 to `DIR/a6-<hook>-<sqlite_version>-excl<on|off>-<baseline|owner>.jsonl`. `--gate tools/gates/a6-<hook>.json` (with `--emit-runs`) then prints the library's R12 report under the table, headed `metricgate verdict (R12)`. The tool's own `sequential p50 owner/baseline` ratio is a point ratio and not the gate.
- `tools/engramd_lockwait_report.py --gate` reads the lock-wait rate B2 through a Wilson 90% interval (a local mirror of the library's `rate_ci`): `PASS` when the interval's upper bound is at most 1% and `timed_out` is 0.

**The gate-file convention**

- One pre-registered gate file per hook that has an A6 arm: `tools/gates/a6-<hook>.json` for `user-identity`, `stop`, `tripwire`, `session-start` and `surface`.
- Each file is the OUTPUT of `metricgate preregister` (so it carries `registered_at`, `metricgate_version` and `gate_sha256`), generated with `python -m metricgate.cli preregister spec.json -o tools/gates/a6-<hook>.json`, and committed unedited. Editing a field afterwards without re-registering breaks the hash, and `judge` prints the break in the report.
- `gate_sha256` is the sha256 of the canonical JSON (sorted keys, compact separators, ASCII) of the gate without its own `gate_sha256` key. Publish it where the gate is announced (the PR body) at registration, so a backdated edit shows against a hash posted before the first run. A test recomputes it for every file.
- One cell is one (SQLite build, exclusion setting) pair on the installed layout, real-store copy; the cell is named in the JSONL file name, and each run appends one record pair. The planned n is 6 runs. Use one fresh directory per announced cell (one build, one SQLite build, one exclusion setting, one planned n); appending a 7th run to a planned-6 cell makes the cell NO_VERDICT by design. More runs on a cell without a look plan declared in advance is a second look (R6), and `judge` refuses it.
- Every `--emit-runs` invocation also appends one line to the cell's attempts ledger, `a6-<hook>-<sqlite>-excl<on|off>-attempts.jsonl` in the same directory, before any record is written: the run's id and time, whether its records were emitted, and if not, why (`contract_violation: <first violation>`, `source_layout`, `no_real_store_cell`, `synthetic_store`, `unreadable_context`, or `layout_timeout` for a run the layout timeout killed). A contract break stops the cell rather than being replaced: a ledger with a `contract_violation` line makes the cell NO_VERDICT (`contract_violation_in_cell`, naming the run) whatever the records say, and "one announced replacement per timeout" is read from the ledger's `layout_timeout` lines, which `--gate` prints as tagged lines beside the verdict.
- A new gate (a release health score, a recall latency, an embedding regeneration, an engine latency) is registered the same way before its first measured run.

## 3. Status

- DESIGNED: 2026-10-30. Rules R1 to R12 and the tool specified by the lane's design owner; approved by the project owner.
- BUILT: 2026-10-31. The library at tag `v0.1.0`, its contract tests written before the code.
- ADOPTED: by the change that introduced this document. The A6 tool emits run records and reads them against the checked-in gate files, the lock-wait report reads B2 through an interval, this policy is the standing text, and the CLAUDE.md template carries the one-line rule (it names no path).
- The five checked-in gate files were registered with the pre-merge `dev` SHA as their `instrument_version`. At the first VERIFIED run they are re-registered with the merged tool's SHA (tracked on engram-alpha #4297).
- VERIFIED: pending. One real gate decided end to end through the tool (the next batch-B build's fresh runs), its R12 table on the issue, and an A/A control on file.

Dates here are alignment facts for humans, not a schedule: each phase ends when its outcome is accepted.
