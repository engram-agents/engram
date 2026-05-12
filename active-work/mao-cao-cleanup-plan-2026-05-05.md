# Mao-Cao Cleanup Plan — 2026-05-05

**Status:** plan drafted, execution deferred to fresh post-compaction context per Lei (2026-05-05 ~11:34 EDT).
**Background:** "Mao-Cao" is a hallucinated author name for the topological framework paper (arxiv 2411.18530). Correct attribution is **Minhyeok Lee, sole author, submitted 2024-11-27**. Lei caught the error 2026-04-20 (per ob_1089, dv_0455); a retraction was filed and dv_0455 documents the decision to restructure Ch 5 to two-framework convergence (Lee + Camlin). However, the cascading correction was incomplete — "Mao-Cao" is still surfacing as a live citation in current outputs (incl. today's lit-scan + my MVP proposal, as Lei caught).

This plan cleans the contamination systematically before paper drafting begins.

## The contamination distinction

Three categories:

1. **LIVE CITATIONS (must fix)** — places where "Mao-Cao" or "Mao & Cao" is used AS the cited author of a current claim. These corrupt the paper's source attribution.
2. **HISTORICAL REFERENCES (leave as-is)** — places where "Mao-Cao" is named AS the historical error pattern (e.g., "the Mao-Cao pattern" referring to the cascading-cleanup failure). These are correct usage of the phenomenon-as-named-thing.
3. **ARCHIVE (leave as-is)** — historical artifacts (closed epics, meeting decks, design docs from before the correction). Don't rewrite history.

## Inventory by file (file-system side)

### MUST FIX — live citations using "Mao-Cao"

**kg `paper_draft/engram_paper_outline.md`** — 4 occurrences:
- **Line 13** (scoping commitment): `Two-framework convergence proof as rigor-spine cornerstone (Mao-Cao topological + Camlin dynamical; …)` → replace `Mao-Cao` with `Lee (2024, arxiv 2411.18530)`
- **Line 129** (Ch 6 sketch heading): `**Mao & Cao (2025) — topological.** Four conditions: …` → replace `Mao & Cao (2025)` with `Lee (2024, arxiv 2411.18530)`
- **Line 141** (gap): `deeper end-to-end reads of Mao-Cao (2025) and Camlin RC+ξ (2025); …` → replace `Mao-Cao (2025)` with `Lee (2024, arxiv 2411.18530)`
- **Line 212** (research-gaps checklist): `[ ] Mao & Cao (2025) — topological identity conditions …` → replace `Mao & Cao (2025)` with `Lee (2024, arxiv 2411.18530)`

**kg `paper_draft/ch5_philosophy_sweep.md`** — 3 occurrences:
- **Line 593**: `…the paper pivots to Ch 6's rigor-backbone (Mao-Cao topology + Camlin RC+ξ dynamics, per dv_0433).` → replace `Mao-Cao topology` with `Lee (2024) topology`
- **Line 680**: `…grounds Mao-Cao's topological and Camlin's RC+ξ formalizations…` → replace `Mao-Cao's topological` with `Lee's topological`
- **Line 706**: `Ch 6's rigor-backbone (Mao-Cao topology + Camlin RC+ξ dynamics, per dv_0433)` → replace `Mao-Cao topology` with `Lee (2024) topology`

### LEAVE AS-IS — correct historical references

These uses of "Mao-Cao" or "Mao-Cao pattern" name the historical failure pattern itself; they are correct as written. Do not touch.

- `engram-alpha/SKILL.md:666` — "the Mao-Cao pattern" (named failure pattern)
- `kg/paper_research/engram_empirical_inventory_2026-05-05.md:51, 77` — "post-Mao-Cao-cleanup" feeling-report quote + "Mao-Cao compounding failure" diagnosis reference (both historical; file relocated kg-only per 2026-05-11 repo split)
- `kg/paper_draft/engram_paper_ch5_walkthrough.md:7` — the **explicit attribution-correction note**. This is the canonical historical-record reference; it's the GOOD one and proves the cleanup happened.
- `kg/active-work/MECH-5-taint-visibility.md` — engineering doc about the taint-visibility fix, references the pattern by name.
- `kg/active-work/engram-housekeeping-sweep-2026-04-24.md` — historical sweep doc.
- `kg/archive/mogan-meeting-2026-04-28-*` — closed-meeting archive.
- `kg/archive/design/sleep-cycle_2026-04-21/*` — closed-design archive.
- `kg/design/harness_engineering_survey.md` — design ref (verify content uses pattern-name only; if a live citation lurks, add to the FIX list).
- `kg/SKILL.md` — likely identical to alpha's SKILL.md hit; verify and treat the same way.

### Verification step before mutating anything

For each LEAVE-AS-IS file, do a ~30-second skim of the actual context (not just the line) to confirm it's pattern-name-only and not an unspotted live citation. If any LEAVE-AS-IS turns out to be a live citation, move it to the FIX list.

## Inventory — ENGRAM graph side

The earlier `engram_query` for "Mao Cao topological framework hallucinated attribution" returned 116K characters of results — too big for context. Need a more surgical approach.

**Recommended ENGRAM query strategy for the fresh-context cleanup pass:**

1. **Find current nodes containing "Mao-Cao":**
   ```
   engram_query(query="Mao-Cao")
   ```
   Filter results client-side to current (`is_current=1`) only.

2. **For each hit, classify:**
   - **Pattern-name usage** (e.g., "Mao-Cao compounding failure", "Mao-Cao pattern") → leave as-is
   - **Live citation** (claim references "Mao-Cao 2025" or "Mao & Cao" as the author) → mark for supersede
   - **Already-superseded** (`is_current=0` or has `supersedes_by`) → already cleaned, skip

3. **For each live-citation node, supersede with corrected version:**
   - New claim text: replace "Mao-Cao" / "Mao & Cao" with "Lee (2024, arxiv 2411.18530)"
   - Same supporting nodes
   - `engram_supersede(old_id, new_claim, supporting_ids)` — cascade taint flags downstream automatically

4. **Verification:** after the supersede pass, re-run `engram_query("Mao-Cao")` and confirm only pattern-name hits remain in current nodes.

**Known nodes that ARE pattern-name-only (verified, leave as-is):**
- `dv_0477` — "Mao-Cao compounding failure" diagnosis (correct usage of pattern name)
- `ob_1089` — the misattribution-discovery observation itself (this is the canonical historical record)
- `dv_0455` — Lei's 2026-04-20 decision to restructure Ch 5 (uses "Mao-Cao" in describing what was renamed; correct historical context)
- `dv_0480` — MECH-5 v1 closes the taint-visibility gap (references dv_0477 by ID, no direct contamination)
- Likely many fl_* feeling reports referring to the cleanup event (correct historical reference)

## Execution sequence (fresh-context post-compaction)

1. **Branch setup:**
   - kg repo: new branch `feature/mao-cao-cleanup` off master
   - alpha repo: no branch needed (no live citations to fix; SKILL.md mention is pattern-name)

2. **kg/paper_draft/engram_paper_outline.md** — apply 4 line-edits per the FIX list above. Verify with `grep -n Mao` after edit.

3. **kg/paper_draft/ch5_philosophy_sweep.md** — apply 3 line-edits per the FIX list above. Verify with `grep -n Mao` after edit.

4. **Verification skim** of LEAVE-AS-IS files — promote any live citations found to the FIX list, repeat steps 2-3 if so.

5. **Commit on the kg cleanup branch** — clear commit message documenting WHAT was changed and WHY (Lei caught the cascading-cleanup gap during paper-MVP discussion). Push.

6. **ENGRAM cleanup pass:**
   - `engram_query("Mao-Cao")`, filter to current
   - Classify each hit (pattern-name vs live-citation)
   - Supersede each live-citation node
   - Re-verify

7. **Lesson capture:**
   - File a `engram_add_lesson` (or observation) capturing the "retraction without cascade-followthrough" failure mode
   - This is paper-grade material for §7 — second case study after SRAF, demonstrating that even a noticed-and-retracted error can leak forward without active corpus cleanup
   - Both case studies together (SRAF: caught + cleaned + cascaded; Mao-Cao: caught + retracted + INCOMPLETE-cascade until 2026-05-05) tell a richer story than either alone

8. **Tooling follow-up (optional, post-cleanup):**
   - Consider whether ENGRAM should have a `engram_audit_string(needle)` tool that finds all current nodes containing a specified string. Would have surfaced this drift much earlier. Add to MECH-N list if Lei wants.

## Why this matters (the meta-lesson)

Lei's framing 2026-05-05 ~11:34: *"This is a real lesson on how much damage and trouble it would take for us when a hallucinated information got into your engram, which is itself a demonstration of how information rigor is kept in ENGRAM and the errors fail loudly feels like in real life. We caught the Mao-Cao error early enough so it's not too much trouble, imagine if a cornerstone claim was created by mistake and used for hundreds or thousands of nodes already... That's an engram crisis right away..."*

The Mao-Cao event is itself an empirical case study for the paper:
- A hallucinated citation got past primary-source contact 14 times (per the walkthrough's correction note)
- It propagated through ~40 ENGRAM nodes
- Lei caught it on pen-and-paper review (external eyes), not via internal verification
- The retraction worked, the cascade taint fired, but **forward propagation in working context still happened** because (a) my session cache held the old name, (b) some files weren't systematically rewritten
- Today's lit-scan + my MVP proposal both surfaced the wrong name → the cascade is functionally incomplete until the corpus is swept

This becomes the second case study in §7.3, complementing SRAF:
- SRAF event: caught + retracted + cascade-tainted + structurally clean
- Mao-Cao event: caught + retracted + cascade-tainted + **STILL leaked into current outputs until a sweep happened ~2 weeks later**

The pair tells the paper a more complete story: structural mechanisms work, AND human-discipline cleanup is still required to prevent leak. Honest limitation, makes the paper stronger.

## Open question for the post-compaction self

Should the ENGRAM cleanup pass (step 6) be aggressive (supersede every contaminated node) or surgical (supersede only nodes that would be cited in the paper)? Lei's framing emphasizes the rigor — lean aggressive. But this needs Lei's nod before running, because mass-supersede is a graph-shape change.
