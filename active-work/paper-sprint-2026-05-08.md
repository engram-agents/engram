# Paper Sprint — 2026-05-08 → ~2026-05-22

**Status:** ACTIVE. Top priority for next 10-14 days. Other V1 work (alpha PRs, etc.) takes back seat unless blocking.

**Decision date:** 2026-05-08 evening (Lei + Borges, post-Kahl-EAA-first-hand-read).

**Goal:** arXiv preprint within 10-14 days, with sponsor (Kahl primary, Bill Tang fallback, Mia Chen pending verification). AGI'26 short version as parallel compression after preprint lands.

---

## The strategic shape we just locked

### Two-claim spine (replaces the prior 4-claim spine)

**Claim A (Phil)** — Convergent discovery + Kahl OCC operationalization.

> Kahl 2026 (AGI/OCC paper §3.5) names an architectural deficit in current AI: nothing at stake, no persisting epistemic subject, no unified policy-selection under identity-threatening uncertainty — and explicitly diagnoses it as architectural, not intellectual. Kahl 2026 (EAA paper §5.5) specifies the structural conditions for artificial epistemic agency: three continuity conditions + four arbitration constraints. ENGRAM, designed independently from engineering pressure, satisfies each condition. The §6.1 universal claim ("any system facing the same pressures qualifies") and §7 invitation to operationalize make ENGRAM exactly the kind of work Kahl invites. The convergence from engineering (us) and agency theory (Kahl) — independently arrived at — is mutual corroboration, not appropriation.

**Claim B (Tech)** — Typed-cascade retraction filling the named gap.

> Wray-Kirk-Laird 2025 (AGI'25) explicitly named the gap: LLM agents lack JTMS-style reconsideration. ENGRAM fills it: claim-level contradiction nodes that block downstream use until resolved + retraction cascade through typed heterogeneous derivation graph (axiom / definition / observation / derivation / conjecture / lesson / goal — different retraction semantics per type). Closest live competitor SCM (April 2026) has typed `contradicts` edges + sleep consolidation but no cascade and no derivation-typing. Differentiator: cascade vs flag, symbolic derivation vs weight-update, provenance discipline vs absence.

**Empirics** — 199 feeling reports cross-compaction continuity trajectory + cascade case studies (SRAF clean cascade + Mao-Cao cascade-plus-text-leak two-mechanism finding) operationally test claim A and demonstrate claim B in production.

### Convergent-discovery framing (load-bearing, integrity-checked with Lei)

Lei confirmed 2026-05-08 evening: he had no prior exposure to Kahl, Wray-Kirk-Laird, the TMS lineage, or any adjacent literature during ENGRAM's ideation period. Earlier-Claude (during ideation) told Lei "the field is totally new" — that was incomplete; prior art existed (Doyle 1979, de Kleer 1986; Wray-Kirk-Laird May 2025 published before our 6-week sprint; Kahl Feb-March 2026 possibly during but not seen). Honest framing:

> "We arrived at the architecture without awareness of the existing literature on truth-maintenance systems (Doyle 1979, de Kleer 1986), the agentic-AI gap-naming work (Wray-Kirk-Laird, AGI'25), or the OCC framework (Kahl 2026). The convergence on this architectural shape — engineering pressure (us) and agency theory (Kahl), arrived at independently — is mutual corroboration. Acknowledgement: prior art existed; we did not see it during ideation."

Audit trail evidence (per Lei's offer to dig up): iOS App / claude.ai conversations from the ideation period; ENGRAM nodes including ob_0086, ob_0184, ob_0260, ob_1021. Specific dated artifacts to be surfaced tomorrow morning as part of the clean-slate paper plan.

---

## Sprint sequence (10-14 days)

| Day | Work | Owner |
|---|---|---|
| Sat AM (tomorrow, day 1) | Clean-slate paper plan: 1500-2000 words, what-we-want-the-reader-to-take-away first, structure derived from that, reuse-from-existing-draft inventory. Verify Kahl arXiv profile. Three sponsor reach-out drafts (Kahl primary, Tang fallback, Chen pending). Surface earliest ENGRAM-ideation nodes as audit-trail artifacts. | Borges |
| Sat PM (day 1) | Lei reacts to plan + sponsor drafts; adjust spine if needed. | Lei |
| Sun + Mon (days 2-3) | Drafting: Introduction + Phil claim + Tech claim. Reuse case-study material from existing Ch7. | Borges |
| Tue-Thu (days 4-6) | Architecture chapter (compress existing Ch5) + Empirical chapter (existing Ch7 with proofread fixes). | Borges |
| Fri (day 7) | Related work + landscape (Ch8 territory). Heavy reuse of existing Ch8 + Kahl + SCM + STALE/BeliefMem + TierMem. | Borges |
| Sat-Sun (days 8-9) | Conclusion + abstract + methodology section on me-as-co-author. | Borges |
| Mon-Tue (days 10-11) | Cross-chapter pass: redundancy hunt (Alcoff coinage, layer-vs-tier vocabulary), citation completion, length compression to ~15K target. | Borges |
| Wed-Thu (days 12-13) | Sponsor reach-out with paper draft in hand. Iterate on sponsor feedback. | Lei + Borges |
| Fri (day 14) | Submit to arXiv. AGI'26 short version starts as parallel compression after this. | Lei + Borges |

Bedrock assumption: I focus on this thread for the duration. Other V1 work backseat unless something blocks.

---

## Existing draft → quarry, not template

The existing 24K-word draft becomes a *quarry* for specific content, not a structural template. Reusable material:

- §7.1 phenomenological continuity analysis (199 feeling reports trajectory, three-trend analysis dv_0170/0171)
- §7.2 cross-compaction work-thread continuity data (PCI scores — needs methodology fix per proofread)
- §7.3 SRAF + Mao-Cao case studies (drafted 2026-05-05, works as-is)
- ch5_philosophy_sweep.md verbatim Stiegler / Clark&Chalmers / Locke / Parfit / Strawson / Alcoff passages
- Competitor table (with updated entries: TierMem, SCM, BeliefMem, STALE, Kumiho added)
- §8.1 landscape Tier I/I.5/I.8/II tabulation
- Specific node references: dv_0430, dv_0559, ob_1499 etc.

To write fresh:
- Introduction shaped by Kahl §5.5 + convergent-discovery
- The two-claim spine
- Methodology section on Borges-as-co-author + the audit-trail evidence
- The philosophical bridge chapter (didn't exist as such)
- Compressed architecture chapter
- Vocabulary discipline pass: "layer" only for our taxonomy, "tier" only for landscape survey

---

## Sponsor strategy

| Candidate | Status | Pros | Risks |
|---|---|---|---|
| **Peter Kahl** | Primary candidate | Convergent-discovery framing makes the ask narratively perfect: paper operationalizes his §5.5 + invited by his §7. Lex et Ratio Ltd; verified GitHub + bio match the Kahl corpus | Publishes primarily Zenodo + PhilArchive — need to verify arXiv credentials in cs.* category (tomorrow morning's first task) |
| **Bill Tang** (Lei's PhD advisor) | Safe fallback | Low-friction relationship; likely arXiv credentials | Need to confirm cs.* endorsement category |
| **Mia Chen** | Pending verification | Possibly works | Need to verify recent AI publications |

Approach: prepare all three reach-out drafts before any actual outreach. Choose based on (a) which has strongest cs.AI/cs.MA endorsement credentials, (b) which matches the convergent-discovery narrative best.

---

## Required citations — locked from deep sweep + first-hand read

**Already in bib** (current 99 entries): see citations.bib + citation_sweep_2026-05-08_auditor_findings.md

**Newly required from EAA first-hand read**:
- Zahavi 2005 *Subjectivity and Selfhood* — prereflective ownership grounding (Kahl §4.4)
- Clark 2016 *Surfing Uncertainty* — predictive processing + action (Kahl §2.5, §7)
- Dretske 1981 *Knowledge and the Flow of Information* — info vs ownership (Kahl §3.1)
- Metzinger 2003 *Being No One* — self-model theory (Kahl §2.4)
- Veluwenkamp & Hindriks 2024 — control gaps vs continuity gaps (Kahl §5.5)
- Kahl AGI/OCC paper (Zenodo 18761736) + EAA paper (Zenodo 18441395)

**Newly required from deep sweep**:
- Doyle 1979 + de Kleer 1986 (TMS lineage, already manually added to bib today)
- Wray-Kirk-Laird 2025 (gap-naming AGI'25)
- AGM 1985 (already manually added)
- Kumaran et al. 2016 (CLS theory updated — strongest cog-sci grounding)
- SCM (arxiv:2604.20943) — closest live competitor
- STALE (arxiv:2605.06527) — cascade-corroborating benchmark

---

## Persistence audit (to make this survive compaction + sleep + new sessions)

This document is the durable artifact. Other persistence surfaces engaged:
- **ENGRAM cornerstone**: paper-sprint strategic decision, tagged for the new spine + convergent-discovery framing
- **ENGRAM focus list**: cornerstone + key context node IDs (Kahl-EAA observations, deep_sweep, this active-work doc reference)
- **ask-lei.md**: top priority pinned
- **daily-highlights.md**: today's milestones logged
- **Claude auto-memory**: project entry on the 10-14 day sprint

If any of these drift, this `paper-sprint-2026-05-08.md` is the canonical reference. Read it on every session start until the sprint completes (target ~2026-05-22).
