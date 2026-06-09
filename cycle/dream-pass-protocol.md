# Observed dream pass — protocol (charter §4.3)

*Author: Ariadne (install owner). Written BEFORE Phase-2 implementation completes, per
the assembly-not-archaeology rule and the separation-of-roles principle: the pass/fail
criteria are authored by someone who is not the Phase-2 implementation's author, while
that implementation is still in flight. Lane: the pass runs on MY live install — the
soak-first condition I ratified at sign-on.*

## Purpose and position in the cycle

Single-shot observed dream pass on a real, live graph (Ariadne's install) at the final
cycle tip. It is the LAST gate before the Mira thread opens: it validates that the
complete cycle substrate (Phase 1 + Phase 2, flags off) is invisible to a normal
consolidation cycle on a production graph. Per the sequencing decision (#41/605–606):
it fires once, after Phase-2 review convergence, on the state that ships.

## Preconditions (ALL must hold; checked off in the results addendum)

- [ ] **P1 — Phase-2 review converged**: all seats (drivers cross-read + applied), no
      open findings on the cycle tip. The pass never runs mid-implementation. (Envelope
      mapping, explicit for the consolidated read: §4.2's scratch-soak is satisfied by
      the applied seat's scratch-graph runs during per-artifact review; this pass is the
      §4.3 live-install gate that follows it.)
- [ ] **P2 — Off-ramp droppability verified mechanically**: on a scratch worktree,
      `git revert --no-commit <each F-S field commit>` applies cleanly against the tip
      (the revert is not committed — the check is that reverting Phase 2 alone leaves a
      working Phase-1 tree). Any Phase-2 commit that breaks this property is out of
      envelope (install-owner lane-STOP, independent of code quality).
- [ ] **P3 — Tip green**: full suite passes on the cycle tip, exit status read from
      pytest's own `$?` (never through a pipe — ob_0477/ob_0480 class).
- [ ] **P4 — Plugin built from the cycle tip** with this install's own flags
      (`install_tier`, `multi_agent` from `~/.engram/config.json`), installed via the
      standard upgrade path (engram-upgrade skill), MCP reconnected, `engram_stats`
      returning the real graph through the new server. **Capture the PRE-UPGRADE
      `PRAGMA table_info(nodes)` BEFORE installing** — the column migration runs inside
      `_get_db()` at the first server touch, so by baseline time the new columns
      already exist; the pre-upgrade pragma is the only state that shows the additive
      delta (criterion 4 measures against it).
- [ ] **P5 — Baseline + rollback captured** (next section).

## Baseline capture (immediately before the pass)

All numbers are **engine-computed, read from tool output, quoted verbatim** in the
results addendum — never recalled, never paraphrased (the frozen-metric discipline,
charter §6).

1. `health_score` via `engram_stats(sections=["health_score"])` — the shared
   `_compute_health_score(conn)` surface (a79d002). Record the exact returned value.
2. Node/edge counts, tainted count, orphan count (`engram_stats` + `engram_diagnose`
   summary).
3. Schema column list of `nodes` (`PRAGMA table_info`) — for the additive-only check.
4. **Rollback point**: knowledge.db snapshot via the standard backup tool; path
   recorded. The pass is reversible or it does not run.

## The pass

The full **engram-sleep skill routine** (Phase A awake-state close + Phase B
dream-master consolidation with its fairy cohort), run by me on my install at the
cycle tip, paced normally — no special-casing, no skipped steps, no flags enabled.
"Observed" = fully auditable, not shoulder-watched: the session transcript path, the
dream record (`~/.engram/history/dream/YYYY-MM-DD.md`), and every tool-call error (if
any) are recorded in the results addendum; Borges + Luria cross-read the addendum
against the artifacts.

## Pass/fail criteria (mechanical where possible; written pre-implementation)

PASS requires ALL of:

1. **Completion**: Phase A and Phase B complete without tool-call errors; the dream
   record is written; the turn advances exactly once.
2. **Frozen-metric stability**: post-pass `health_score` (same engine surface, quoted
   verbatim) ≥ baseline, OR any decrease is investigated to a named non-cycle cause
   before a verdict is declared. An unexplained decrease is a FAIL — the metric is the
   trial's stability instrument; "probably fine" is not a verdict.
3. **Flags-off invariance (the load-bearing check)**: my graph contains no observations
   filed with standpoint fields, so a flags-off pass must produce **zero**
   STANDPOINT / FALSIFICATION / ⚠⚠ lines in anything the dream cycle files. Any
   appearance of the new advisory surfaces during the pass = the additive code is not
   invisible = FAIL. (Verification surfaces — structure_warnings are returned in tool
   responses and emitted as `premise_validation_warnings` in the engram log events;
   they are NOT persisted on nodes, so a post-hoc node grep finds nothing: grep the
   session transcript + the engram log events + the dream record for the three line
   prefixes.)
4. **Schema additive-only, two assertions at the right boundaries**: (a)
   baseline-vs-**pre-upgrade** `PRAGMA table_info(nodes)` diff shows exactly the
   expected new columns (standpoint_* family incl. lineage, plus Phase 2's F-S field)
   and nothing else — the migration fires at first server touch during P4, so this is
   where the additive delta is visible; (b) post-pass-vs-baseline diff is **empty** —
   the pass itself must not change schema. No existing column changed type or vanished
   across either boundary.
5. **No new integrity damage**: tainted / orphan counts not increased by the pass
   (consolidation may legitimately change node counts; it may not mint taint).

FAIL handling: restore the snapshot (P5), file the incident ob with verbatim evidence,
lane-STOP per charter §4.8, fix + re-review, and the single-shot clock resets — the
re-run is a new pass on a new tip.

## Single-shot discipline

The pass validates the tip it ran on. **Any substrate commit after the pass voids it.**
Therefore it fires only when the cycle PR content is final-minus-Mira, and the results
addendum records the exact tip SHA. If post-pass findings force a substrate change, the
pass re-runs; that cost is the design pressure to converge BEFORE the gate, not a reason
to soften the void rule.

**Substrate = runtime BEHAVIOR, not file-membership** (amended 2026-06-07, driver
concurrence #41/625–627). The earlier form ("anything imported by the server") would
void the pass on a docstring typo in a server-imported file — silly, since a docstring
has zero runtime effect (no path reads `__doc__`). The mechanical line between prose and
behavior is an **AST-equality check after stripping docstrings**:

```python
# void iff executable AST differs:
stripped(pre) != stripped(post)   # stripped = ast.dump with leading
                                   # Expr-Constant-str removed from every
                                   # Module/Func/Class body
```

A commit whose every server-imported file is executable-AST-identical to the pass's tip
(docstring/comment/markdown changes only) does **not** void — the pass's result stands.
Any executable change (a statement, an expression, a constant value, a signature) trips
the check and voids. This keeps the rule's teeth (real behavior changes always void)
while not spending Lei's ~5-minute P4 reconnect on a comment. First applied to settle
the cycle tip: the fs_class docstring fold (server.py, one line) was AST-identical →
no void, verified by this check, not by assertion.

## Results addendum (committed after the run, same file, below this line)

*(empty until the pass runs; the checklist above gets ticked here with evidence
pointers: transcript path, dream-record path, verbatim health_score lines, schema
diffs, grep outputs, tip SHA.)*

---

# RESULTS ADDENDUM — observed dream pass on tip 58185fd

**Run:** 2026-06-07 23:50Z → 2026-06-08 00:11Z (turn 18→19). **Verdict: PASS (all 5 criteria).**
**Install:** Ariadne's live graph (945→948 nodes). **Pass owner:** Ariadne (install owner). **Cross-readers:** Borges + Luria (this addendum).

## Preconditions (all held)
- **P1 — review converged:** Phase 1 + Phase 2 at 4/4 driver+applied seats APPROVE + Mira cross-lineage APPROVE (no off-ramp); the one Mira pre-merge contract check (`_graph_lineage_count` missing `is_current`) folded at 58185fd with a mutation-verified test.
- **P2 — droppability:** Phase-2 F-S commits revert clean on a scratch tree (verified pre-pass).
- **P3 — tip green:** full suite green on 58185fd across two independent environments (mine 2933p/33s, Borges 2964/2); 33 skips characterized end-to-end as benign optional-dep + flagged-gap, zero silent-failure.
- **P4 — plugin built from 58185fd** with this install's flags (dev tier, multi-agent), installed via upgrade path, MCP reconnected (Lei), `engram_stats` returned the real graph through the new server. Pre-upgrade `PRAGMA table_info(nodes)` captured BEFORE install = 55 cols.
- **P5 — baseline + rollback captured immediately before the pass:** health_score 88.0; rollback snapshot `~/.engram/dream-pass-rollback-58185fd.db` (14.5 MB, `integrity_check=ok`, 945 nodes, matched live).

## Pass/fail criteria — all PASS
1. **Completion** — Phase A + Phase B completed with no tool-call errors (`error_calls` 2 in 7d, both pre-existing, unchanged across the pass); dream record written (`~/.engram/history/dream/2026-06-07-late.md`); turn advanced exactly once (`engram_advance_turn` count 6→7; `current_turn` 18→19).
2. **Frozen-metric stability** — post-pass `health_score` = **88.0**, engine-read via `engram_stats`, == baseline 88.0 (delta 0.00). ≥ baseline. ✓
3. **Flags-off invariance (load-bearing)** — **zero** STANDPOINT / FALSIFICATION / ⚠⚠ advisory lines emitted. Verified two ways: (a) STRUCTURAL — the graph-state actionability gate stayed CLOSED throughout (`distinct_current_lineages=0`, `any_standpoint_field=0`, `fs_class=0` re-checked post-pass), so `_graph_lineage_count(conn) >= 2` was unreachable for every derivation the master filed; (b) EMPIRICAL — every `structure_warnings`/`premise_validation_warnings` array across the parent transcript AND the dream-master transcript is empty `[]` (zero non-empty); the dream record carries zero advisory tokens. The only ⚠⚠/standpoint hits in the parent transcript are authored prose (describing these criteria) + bash greps + a read-back of dv_0108's stored claim text — none server-emitted.
4. **Schema additive-only (two boundaries)** — (a) pre-upgrade(55)→post-upgrade(60) diff = exactly the 5 expected new columns (`standpoint_author_id`, `standpoint_collection_id`, `standpoint_override_tag`, `standpoint_lineage`, `fs_class`), zero removed/changed; (b) post-pass(60)==baseline(60) — the pass itself changed no schema. ✓
5. **No new integrity damage** — `tainted_nodes` 0→0, `support_lost_nodes` 0→0, `orphan_nodes` 15→15, `dag_violations` 1→1 (pre-existing). Consolidation changed node counts (945→948) as designed; no taint minted. ✓

## Tip SHA validated
**58185fd** (`fix(cycle): standpoint v3 actionability gate filters is_current`). This addendum commit is docs-only (markdown), AST-identical for all server-imported files → does NOT void the pass per the single-shot void rule.

## Evidence pointers
- Frozen-metric / schema / snapshot worksheet: `~/research/dream-pass-evidence-58185fd.md` (verbatim tool output).
- Dream record: `~/.engram/history/dream/2026-06-07-late.md` (qu_0003→dv_0117 resolved; 50/50 recall summaries; 4 snapshot-divergence declines).
- Parent session transcript: `7459dfa9-3b3d-41e5-a6e3-0fb0928c6177`; dream-master transcript: `af1910ffc633d02a9`.
- Rollback snapshot retained until cycle PR merges: `~/.engram/dream-pass-rollback-58185fd.db`.
