"""Per-type prompt templates for recall_summary + recall_keywords generation.

Validated against the v4.2 pilot (10 nodes / 5 dv + 5 ob) on 2026-05-19 PM
with Sonnet 4.6. The 3-5 keyword count was chosen after a follow-up
calibration test confirmed: pure-strict-3 caps signal density on complex
nodes (ob_NNNN wanted 5 keywords to cover all load-bearing terms);
pure-flexible-1-5 drops signal on simple nodes (ob_NNNN chose 2 keywords
but the third was load-bearing). Min 3 max 5 is the sweet spot.
"""

from __future__ import annotations

# Per-type guidance (used in the prompt template)
TYPE_GUIDANCE: dict[str, str] = {
    "observation_factual": (
        "atomic factual claim with source provenance. "
        "Capture load-bearing subject + what is asserted. "
        "Active voice. No hedging or meta-language. "
        "Keep technical terms verbatim."
    ),
    "derivation": (
        "non-atomic claim reached by reasoning over supporting nodes. "
        "Capture the conclusion + compactly the key reason or domain. "
        "'X because Y' shape is fine when the why is load-bearing."
    ),
    "axiom": (
        "a foundational rule the agent operates from. "
        "Capture the rule + the load-bearing reason it's foundational "
        "(failure mode it prevents, structural commitment it enables)."
    ),
    "lesson": (
        "experience-encoded reminder that fires as a tripwire. "
        "Capture the tripwire condition + the rule the lesson encodes "
        "(when does this matter, what's the corrective)."
    ),
    "definition": (
        "canonical meaning of a term used elsewhere in the graph. "
        "Capture the term + one-line definition + scope where it applies."
    ),
    "person": (
        "a person in the agent's relational layer. "
        "Capture who they are to the agent (role, relationship), "
        "key facts. Not a claim about the world."
    ),
    "goal": (
        "a durable aspiration. "
        "Capture the goal + the load-bearing reason or domain."
    ),
    "conjecture": (
        "a provisional claim usable as derivation foundation; "
        "promotable/refutable later. Capture proposition + status + "
        "the key supporting or refuting consideration."
    ),
    "question": (
        "an open research gap. "
        "Capture the gap being asked + why it matters (what depends on it)."
    ),
    "contradiction": (
        "two propositions in conflict, with resolution status. "
        "Capture what conflicts + why it matters + resolution if any."
    ),
    "evidence": (
        "a source document or quote anchor. "
        "Capture source + what claims it grounds."
    ),
    "feeling_report": (
        "agent-reported internal state with trigger. "
        "Capture state + trigger + what it surfaced about identity/process."
    ),
    "task": (
        "a work commitment with deliverable. "
        "Capture deliverable + the why behind the work."
    ),
    "cornerstone": (
        "an operating principle that pivots how the agent approaches a domain. "
        "Capture the principle + when it fires."
    ),
}

# Default fallback guidance for any node type not in the dict above
DEFAULT_GUIDANCE = (
    "Capture the load-bearing point. Active voice. No hedging or "
    "meta-language. Keep technical terms verbatim."
)

# Keyword guidance (shared across all types)
KEYWORD_GUIDANCE = """3 to 5 load-bearing technical or topical terms a future
agent might keyword-search to surface this node. As many as carry distinct
signal; don't pad.

Prefer short keywords: 1-2 words is the sweet spot. 3 words is OK only
when the phrase is itself the canonical multi-word term — examples of
good 3-word keywords: "chain-of-thought", "hippocampus-cortex",
"FTS5 syntax". Avoid coining descriptive multi-word phrases — examples
to avoid: "structural slow-down scaffolding",
"information-theoretic inevitability". The keyword slot is a search
anchor, not a mini-summary — the summary already carries that load.

Length: structural hard cap of 30 characters per keyword (enforced by
the validator — overcap keywords are rejected). Aim for ≤25 as a soft
target; staying under it signals you're picking proper terms instead
of coining descriptions.

Concrete terms over abstract concepts. ENGRAM tool names valid. Lower-case
unless proper noun / code symbol / canonical capitalization. No duplicates.

DO NOT use ENGRAM node IDs (e.g., ob_NNNN, dv_NNNN, fl_NNNN) as keywords.
IDs carry no recognition signal in skim — the keyword slot is for *content
recognition*. Graph navigation (acts-on relationships, citations, references)
lives in edges and is accessed via engram_inspect. Even for nodes that
supersede / retract / contradict / resolve a specific target, the keyword
line should describe the content of the new node, not name the old node's ID.
This rule is also enforced structurally by the validator.

DO NOT use generic words: node, claim, observation, derivation, agent,
system — those carry zero discriminative signal."""

# Full prompt template (Jinja-style placeholders)
PROMPT_TEMPLATE = """You are generating a "recall_summary" + "recall_keywords" for a node in an ENGRAM epistemic knowledge graph. These appear in an auto-surface hint in the agent's prompt — the agent reads them to recognize "this node exists, this is roughly what it's about" before deciding whether to engram_inspect for the full claim.

## Node

Type: {node_type}
Type guidance: {type_guidance}

Claim:
{claim}

## Output rules

### Summary
- Single line, ≤120 characters. Aim for this target. Don't pad to fill the
  budget — shorter is fine when the claim is simple.
- Active voice. No hedging. No meta-language ("this node says..."). No source attribution unless the source is the load-bearing fact.
- Keep technical terms verbatim if load-bearing
- No quotation marks unless they're part of the term

### Keywords
{keyword_guidance}

- Output 3 to 5 keywords (NOT exactly 3, NOT exactly 5 — as many as carry distinct signal)
- Each keyword ≤ 30 characters

## Output format

Output ONLY a valid JSON object to stdout, no other text:

{{"recall_summary": "...", "recall_keywords": ["kw1", "kw2", "kw3"]}}

Self-check before output: summary chars ≤120; keyword count 3 to 5 inclusive; no duplicates."""


def build_prompt(node_type: str, claim: str) -> str:
    """Compose the per-node generation prompt for a Sonnet (or similar) call.

    Used by:
    - tools/recall_summary_backfill orchestration (planned)
    - skills/engram-sleep daily-cohort step (planned)
    - In-session fairy dispatches that bundle multiple nodes (parent
      agent constructs the multi-node prompt; this function is for the
      single-node fallback shape)
    """
    guidance = TYPE_GUIDANCE.get(node_type, DEFAULT_GUIDANCE)
    return PROMPT_TEMPLATE.format(
        node_type=node_type,
        type_guidance=guidance,
        claim=claim,
        keyword_guidance=KEYWORD_GUIDANCE,
    )
