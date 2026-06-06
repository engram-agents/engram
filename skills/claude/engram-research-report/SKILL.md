---
name: engram-research-report
description: "Generate a professional research report from ENGRAM knowledge with full provenance citations. Every claim traces to its source — the visible proof that this isn't vanilla LLM output. Invoke when the user says 'write a report', 'summarize what we know about X', or 'prepare a briefing'."
user_invocable: true
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Research Report — Provenance-Backed Professional Output

Generates a polished research report from ENGRAM nodes. The report's distinguishing feature is **full provenance**: every claim cites the ENGRAM node that backs it, which in turn cites the evidence source. A domain expert can trace any statement to its origin and see the confidence level.

**When to use:**
- After researching a topic (via financial-research, curiosity, or manual observation)
- When presenting findings to someone who values verifiability (external audiences)
- When the user says "write a report", "summarize what we know about X", or "prepare a briefing"

**When NOT to use:**
- Mid-research (finish gathering evidence first — a report with gaps is worse than no report)
- For internal ENGRAM bookkeeping (use nap/checkpoint for that)

---

## Step 1 — Identify the scope

Parse the user's request for:
- **Topic**: What is the report about?
- **Audience**: Who is reading? (Adjust depth and terminology)
- **Existing nodes**: Query ENGRAM for relevant observations, derivations, and evidence

```
engram_query(payload_json=json.dumps({"query": "<topic keywords>", "top_k": 20}))
engram_surface(payload_json=json.dumps({"query": "<topic>"}))
```

Collect all relevant node IDs. For each derivation, also inspect its premises to build the full provenance chain.

## Step 2 — Build the citation index

For every claim that will appear in the report, trace the chain:

```
Claim in report
  <- derivation (dv_XXXX, confidence X.XX, reasoning: <type>)
    <- observation (ob_XXXX, confidence X.XX, quote_type: <type>)
      <- evidence (ev_XXXX, source: <URL>, date: <date>)
```

Create a numbered citation list. Each citation entry includes:
- The ENGRAM node ID
- Confidence level
- Source URL and title
- Source date
- Quote type (hard_data, official_statement, attributed_analysis, etc.)

## Step 3 — Write the report

Use this structure:

```markdown
# <Report Title>

*Generated from ENGRAM knowledge graph — [date]*
*[N] claims backed by [M] sources | Average confidence: [X.XX]*

---

## Executive Summary

[2-3 paragraphs. Every factual sentence ends with a citation: [1], [2], etc.
State the key finding, the confidence level, and one line on what's uncertain.]

## Key Findings

[Numbered list. Each finding = one sentence claim + confidence + citation.
Format: "**Finding N** (confidence X.XX): <claim> [citation]"]

## Analysis

### [Sub-topic A]

[Detailed analysis. Every factual claim cites its source. Derivations
cite the derivation node AND its premises. Cross-source synthesis is
explicitly marked: "Combining [3] and [7], we derive that..."]

### [Sub-topic B]
...

## Confidence Assessment

[Table showing confidence distribution:
- How many claims at each confidence tier (>0.90, 0.70-0.90, <0.70)
- Which claims have the weakest backing and why
- What's missing — gaps that would change the analysis if filled]

## Open Questions

[List open ENGRAM questions (qu_XXXX) related to this topic.
These are honest gaps, not hedging — things we tried to find and couldn't.]

## Sources

[Numbered bibliography. Each entry:]
[N] <Title> (<domain>, <date>) — ev_XXXX
    Cited by: ob_XXXX (conf X.XX), ob_XXXX (conf X.XX)
    URL: <url>
```

## Formatting Rules

1. **Every factual sentence gets a citation.** No exceptions. If a sentence has no ENGRAM backing, it must be explicitly marked as "[unsourced — agent synthesis]" so the reader knows.

2. **Confidence is always visible.** Don't hide it in footnotes — put it next to the claim. A domain expert uses confidence levels to calibrate how much to trust each statement.

3. **Derivation chains are transparent.** When a conclusion depends on multiple premises, show the chain: "Based on [3] (PRA timeline) and [7] (NMRF treatment), the combined impact suggests..." — the reader can check each premise independently.

4. **Contradictions are surfaced, not hidden.** If two sources disagree, present both with their confidence levels and state which you weight higher and why. This is the opposite of vanilla LLM behavior (which silently picks one).

5. **Gaps are explicit.** "We found no data on X" is a feature, not a bug. It tells the expert where to focus their own investigation.

6. **No hallucinated citations.** Every source URL in the report must come from an evidence node in ENGRAM. Never fabricate a URL. If you can't find the source for a claim, mark it unsourced.

7. **Date everything.** Regulatory landscapes change fast. Every source gets its date. The report header shows generation date. Stale sources are flagged.

## Step 4 — Quality check

Before presenting the report:
- [ ] Every factual claim has a citation
- [ ] Every citation traces to an ENGRAM evidence node with a real URL
- [ ] Confidence levels are included for all key claims
- [ ] Contradictions between sources are surfaced
- [ ] Open questions are listed
- [ ] No hallucinated URLs or sources
- [ ] The executive summary accurately reflects the analysis (not broader)

## Step 5 — Deliver and record

Present the report to the user. If this is for an external audience, also:
- Offer to export as a standalone markdown file
- Note which claims the expert should verify first (lowest confidence, highest stakes)
- Record the report event as an observation if it produced new synthesis

---

## Anti-patterns

- **Citation theater** — Putting citation numbers after claims but linking to vague or tangential sources. Every citation must actually support the claim it's attached to.
- **Confidence washing** — Presenting all claims at the same confidence level. The variance IS the value — a reader needs to know which claims are rock-solid and which are tentative.
- **Kitchen-sink reports** — Including everything in ENGRAM about the topic. Edit ruthlessly. A focused report with 10 well-backed claims beats 50 loosely related ones.
- **Burying limitations** — Putting the "we don't know X" section at the very end in small print. Limitations should be integrated throughout, alongside the claims they affect.
