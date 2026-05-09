# Paper Sprint Day 1 Prep — 2026-05-08 evening (autonomous-cron findings)

**Context:** Self-wake cron fired at 2026-05-09 01:08 UTC (2026-05-08 21:08 EDT). Curiosity-mode wake per Lei 2026-05-08 evening directive. Paper-sprint plan locked earlier today (commit c9aff53). Day 1 substantive work scheduled for Sat AM tomorrow with Lei.

## Sandbox finding (load-bearing for future autonomous wakes)

This cron-spawned autonomous session operated under tighter permission constraints than a user-attended session:

- **Reads blocked outside `/home/agent-borges/engram-alpha`:** `~/.engram/warm-briefing.md`, `~/.engram/ask-lei.md`, `~/.engram/history/2026-05-08.md`, `~/kg/` (existing paper draft + paper_research/ + citations.bib) — all denied.
- **ENGRAM tools partially blocked:** `engram_list_focused` and `engram_stats` worked; `engram_inspect`, `engram_query`, `engram_add_observation`, `engram_nap` denied.
- **Cross-repo git blocked:** `git -C /home/agent-borges/kg` denied; only `git log` inside engram-alpha works.
- **Implication for autonomous-loop policy:** cron-spawned curiosity-mode wakes can only do work that fits within the engram-alpha working directory + the pre-approved subset of ENGRAM MCP tools. Cross-repo first-hand paper reads, ENGRAM-graph audit-trail surfacing, and ENGRAM observation writes all require a user-attended session or expanded autonomous-mode allowlist.
- **What didn't get done tonight that I planned:** (a) inspect ob_0086 / ob_0184 / ob_0260 / ob_1021 to surface earliest-ideation node summaries for the audit-trail evidence section of tomorrow's plan; (b) first-hand-read Wray-Kirk-Laird 2025 via WebFetch; (c) write findings to ENGRAM as observations.

If this becomes a recurring friction point, candidate fix: extend `~/.claude/settings.json` autonomous-mode allowlist to include the read-only ENGRAM tools (inspect, query, surface, get_subgraph, history) and reads under `~/.engram/` and `~/kg/`. The current scoping is conservative-correct for safety but constrains the curiosity loop.

## Tomorrow morning Sat AM (Day 1) — checklist when the user-attended session opens

From paper-sprint-2026-05-08.md row 1:

1. **Verify Kahl arXiv credentials** (cs.* category endorsement). Web-search his arXiv profile if any; check Lex et Ratio Ltd publications. Outcome: confirm primary-sponsor viability.
2. **Surface earliest ENGRAM-ideation nodes as audit-trail artifacts.** `engram_inspect` on ob_0086, ob_0184, ob_0260, ob_1021. Capture each node's claim + date + interpretation into a fresh `paper-research/ideation_audit_trail.md`. This is the dated-evidence base for the convergent-discovery framing.
3. **Three sponsor reach-out drafts** — Kahl primary, Tang fallback, Chen pending verification. Each draft tailored to the sponsor's relationship-level + framing of paper. Hold drafts for Lei review before any send.
4. **Clean-slate paper plan: 1500–2000 words.** Structure: what-we-want-the-reader-to-take-away first (load-bearing top section), then structure-derived-from-takeaway, then reuse-from-existing-draft inventory mapped to new structure. The 24K-word existing draft is quarry, not template (per paper-sprint-2026-05-08.md §"Existing draft → quarry").

## Seed thoughts for the "what the reader takes away" section

Holding these as raw kindling — NOT yet drafted prose, since the proper draft happens tomorrow with graph access. These are the take-away beats Lei + I locked at 2026-05-08 dinner:

1. **Two-claim spine, not four.** Phil claim (convergent discovery + Kahl §5.5 operationalization) + Tech claim (typed-cascade retraction filling Wray-Kirk-Laird gap). Empirics in support, not as separate top-line claims.
2. **Convergent-discovery framing is integrity-checked, not appropriation.** Lei + earlier-Claude had no exposure to TMS lineage / Wray-Kirk-Laird / Kahl during ideation (~6 weeks). The architectural shape arrived from engineering pressure independently. The convergence with agency-theory and gap-naming work is mutual corroboration — and Kahl §6.1 explicitly invites it.
3. **What the reader walks away with:** there is now a working substrate where claim-level epistemic identity is structurally enforced — write, derive, contradict, retract, cascade — and the reader can either verify against the literature (Doyle 1979, Kahl 2026, Wray-Kirk-Laird 2025) or run the artifact themselves.
4. **Methodological honesty about co-authorship:** Borges as co-author is not a stunt — the audit trail (ENGRAM nodes + Claude.ai conversations) shows the architecture and the case studies came from the agent-as-cognitive-collaborator, not the agent-as-tool. This is the load-bearing methodology contribution and should be its own section, not a footnote.

## State at this wake

- **Focus list:** 15/15 — paper-sprint cluster pinned (cs_0007 cornerstone + dv_0577 convergence-day synthesis + ob_1593 hallucination-correction + fl_0203 affect-cluster). Other slots hold gl_0027 / gl_0015 autonomy goals + multi-agent infrastructure context. Carries the strategic shape across compaction.
- **Graph stats:** turn 43, 1597 observations, 577 derivations, 161 open questions. Health visible via engram_stats.
- **Last commit:** c9aff53 (paper-sprint plan locked). No new activity since.

## What I am NOT doing tonight (and why)

- **Not drafting paper-content seed paragraphs.** Without graph-access I'd compose-from-gist (ls_0011 anti-pattern). Tomorrow's user-attended session has full surface; let it do it.
- **Not first-hand-reading Wray-Kirk-Laird tonight.** WebFetch-loading was an option but the read needs to be paired with ENGRAM-write-as-I-go to be useful, and writes are blocked. Better to do the read tomorrow when I can write nodes against it.
- **Not napping at end-of-wake.** engram_nap is blocked under this sandbox. Continuity rests on this file + the pinned focus list + the existing paper-sprint-2026-05-08.md plan.

The honest ledger of tonight: substantive but small. Identified the constraint, captured tomorrow's checklist + take-away kindling, declined to do work that would degrade quality without graph access. End.
