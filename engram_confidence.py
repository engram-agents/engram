"""Shared confidence computation constants for ENGRAM.

Single source of truth for the confidence model — imported by both
server.py (MCP server) and surgical.py (standalone correction tools).

Confidence is ALWAYS derived, never arbitrarily assigned:
  - Observations: from quote_type + source_class
  - Derivations: from reasoning_type + premise confidences
  - Axioms: definitionally 1.0
  - Definitions/questions/goals/etc: None (not claim-bearing)

See the structural-confidence-determination axiom (the load-bearing boundary) for why this separation matters.
"""

# ── Observation confidence: quote_type → base confidence ──────────────────

CONFIDENCE_MAP = {
    "hard_data": 0.95,
    "official_statement": 0.85,
    "attributed_analysis": 0.70,
    "unnamed_source": 0.50,
    "personal_communication": 0.40,
    "editorial": 0.35,
}

VALID_QUOTE_TYPES = set(CONFIDENCE_MAP.keys())

# ── Source class adjustments ──────────────────────────────────────────────

VALID_SOURCE_CLASSES = {"external", "introspective", "user_stated"}

SOURCE_CLASS_CONFIDENCE_DISCOUNT = {
    "external": 1.0,        # Standard — no adjustment
    "introspective": 0.95,  # Agent's own prior output — slight discount
    "user_stated": None,    # Override: treated as official_statement regardless of quote_type
}

# ── Prediction and conjecture caps ────────────────────────────────────────

PREDICTIVE_CONFIDENCE_CAP = 0.60
CONJECTURE_CONFIDENCE_DEFAULT = 0.40
CONJECTURE_CONFIDENCE_MIN = 0.10
CONJECTURE_CONFIDENCE_MAX = 0.60

# ── Reasoning types for derivations ──────────────────────────────────────

REASONING_TYPES = {
    # Deductive: if premises are true, conclusion MUST be true
    "deductive_modus_ponens",       # If P then Q; P; therefore Q
    "deductive_modus_tollens",      # If P then Q; not Q; therefore not P
    "deductive_hypothetical_syllogism",  # If P→Q and Q→R; therefore P→R
    "deductive_disjunctive",        # P or Q; not P; therefore Q
    "deductive_reductio",           # Assume P; derive contradiction; therefore not P
    # Inductive: more evidence = more likely, but never certain
    "inductive_generalization",     # Observed X in A,B,C → X generally true. REQUIRES 2+ distinct evidence sources.
    "inductive_enumeration",        # Source S enumerates items I1,I2,...,In sharing property P → P has n instances (single authoritative source, structurally-independent items)
    "inductive_statistical",        # X% of A are B; this is A; therefore probably B
    "inductive_analogy",            # A~B in X,Y,Z; A has W; therefore B has W
    "inductive_causal",             # A precedes B + mechanism → A causes B
    # Abductive: best available explanation, alternatives may exist
    "abductive_best_explanation",   # Observations → hypothesis that best explains them
    "abductive_elimination",        # Hypotheses H1,H2,H3; rule out H1,H2 → H3
    # Authority: trust transfer, not logical reasoning
    "authority_expert",             # Expert X says P → P is probably true
    "authority_consensus",          # Multiple independent sources agree → probably true
}

# Class-level grouping for confidence computation
REASONING_CLASS = {
    "deductive_modus_ponens": "deductive",
    "deductive_modus_tollens": "deductive",
    "deductive_hypothetical_syllogism": "deductive",
    "deductive_disjunctive": "deductive",
    "deductive_reductio": "deductive",
    "inductive_generalization": "inductive_corroboration",
    "inductive_enumeration": "inductive_corroboration",
    "inductive_statistical": "inductive_chain",
    "inductive_analogy": "inductive_chain",
    "inductive_causal": "inductive_chain",
    "abductive_best_explanation": "abductive",
    "abductive_elimination": "abductive",
    "authority_expert": "authority",
    "authority_consensus": "inductive_corroboration",
}

# Per-type confidence discount applied to the reasoning step itself
REASONING_DISCOUNT = {
    "deductive_modus_ponens": 0.98,
    "deductive_modus_tollens": 0.98,
    "deductive_hypothetical_syllogism": 0.98,
    "deductive_disjunctive": 0.98,
    "deductive_reductio": 0.98,
    "inductive_generalization": 0.95,
    "inductive_enumeration": 0.93,
    "inductive_statistical": 0.90,
    "inductive_analogy": 0.70,
    "inductive_causal": 0.85,
    "abductive_best_explanation": 0.80,
    "abductive_elimination": 0.88,
    "authority_expert": 0.95,
    "authority_consensus": 0.98,
}

# Abductive confidence caps — even with perfect premises, abductive
# reasoning can't exceed these ceilings
ABDUCTIVE_CONFIDENCE_CAP = {
    "abductive_best_explanation": 0.80,
    "abductive_elimination": 0.90,
}
