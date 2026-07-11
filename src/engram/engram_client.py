"""
ENGRAM Client — Direct Python API for LLM tool-use integration.

Wraps the engram server functions for use with any LLM that supports
function calling (Gemini, OpenAI, Ollama, Anthropic, etc.). No MCP needed.

Usage:
    from engram_client import EngramClient

    client = EngramClient(db_dir="~/.engram/eval-run-1")
    
    # Get tool declarations for your LLM
    gemini_tools = client.gemini_tool_declarations()
    openai_tools = client.openai_tool_declarations()
    
    # Execute a tool call returned by the LLM
    result = client.call("engram_add_evidence", {
        "url": "https://reuters.com/article",
        "title": "Gold drops 10%"
    })
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Multi-agent mode helpers
# ---------------------------------------------------------------------------

def _load_engram_config() -> dict:
    """Load ~/.engram/config.json (or $ENGRAM_HOME/config.json).

    Returns an empty dict if the file is absent or unparseable. Callers
    use .get() with defaults so missing keys degrade gracefully.
    """
    engram_home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    config_path = Path(engram_home) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def is_multi_agent_mode() -> bool:
    """True if this install is configured for multi-agent operation.

    Default: False (single-agent). Multi-mode is opt-in, set by
    agentctl spawn when this host hosts 2+ agents (PR 2 of the
    inter-agent-comms-v1 cohort).
    """
    config = _load_engram_config()
    return config.get("mode", "single") == "multi"


def get_counterparts() -> list[str]:
    """List of peer agent names on this host. Empty in single mode."""
    config = _load_engram_config()
    result = config.get("counterparts", [])
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Tool schema definitions (shared across all LLM formats)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = {
    "engram_add_evidence": {
        "description": "Register a source document (webpage, article, data release). Returns existing node if URL matches. Per-observation versioning (the evidence-block refactor derivation): file content_hash and git_sha live on each observation, not the evidence node. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: url, title.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with evidence fields. Required: url (canonical URL of the source — for local files use 'file://path/to/file'), title (article or page title). Optional: domain (auto-extracted from URL if omitted), source_date (ISO format, e.g. '2026-03-20'), content_snippet (truncated excerpt for offline re-reading).", "required": True},
        },
    },
    "engram_add_observation": {
        "description": "Extract and record a claim from a source document with full provenance. Single-payload signature — pass all fields as one JSON object string in payload_json. Required fields inside payload_json: quoted_text, interpretation, claim, quote_type. Source identification: include url+title to auto-create the evidence node, OR evidence_id to cite an existing one.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) containing observation fields. Required: quoted_text, interpretation, claim, quote_type (one of hard_data/official_statement/attributed_analysis/counterfactual_inference/unnamed_source/personal_communication/editorial). Optional: url, title, domain, source_date, evidence_id, is_predictive, predicted_event, resolution_timeframe, source_class (external/introspective/user_stated), content_hash, git_sha, standpoint_author_id, standpoint_collection_id, standpoint_override_tag, standpoint_lineage (provider:family, e.g. anthropic:opus — the training-lineage provenance axis; marks the EVIDENCE SOURCE that produced the claim, not who authored this node), fs_class (re-executable/frozen — falsification-sensitivity; omit to use the quote_type proxy).", "required": True},
        },
    },
    "engram_add_observation_batch": {
        "description": "Extract multiple observations from a single source in one call. Single-payload signature — pass all fields as one JSON object string in payload_json. Required inside payload_json: observations_json (itself a stringified JSON array of observation objects). Source identification: include url+title to auto-create the evidence node, OR evidence_id to cite an existing one.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with batch fields. Required: observations_json (stringified JSON array; each observation needs quoted_text, interpretation, claim, quote_type — optional: is_predictive, predicted_event, resolution_timeframe, source_class, standpoint_author_id, standpoint_collection_id, standpoint_override_tag, standpoint_lineage (provider:family; marks the EVIDENCE SOURCE that produced the claim, not who authored this node), fs_class (re-executable/frozen)). Source: url+title OR evidence_id. Optional: domain, source_date, content_hash, git_sha.", "required": True},
        },
    },
    "engram_surface": {
        "description": "Shallow surface: search KG and return a compact summary nudge (type counts, special nodes, top claims, age signal). No memory refresh — ambient awareness, not deliberate recall. Use to decide whether to dig deeper with engram_inspect or engram_get_subgraph.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with surface fields. Required: query (str — natural language search query, used for FTS keyword matching and as the default semantic query). Optional: top_k (int — max nodes to scan, default 10), semantic (bool — include semantic/embedding search; False for fast keyword-only, default True), embed_query (str — separate semantic-search string; when provided, semantic search uses embed_query while FTS still uses query).", "required": True},
        },
    },
    "engram_inspect": {
        "description": "Inspect a single node with three view modes: 'recall' (default — full focus claim + grouped logical-substrate neighbors with recall_summary + contextual neighbors with recall_keywords; the 'refresh my memory of this idea' view), 'deep' (all focus-node fields including confidence_history + 1-hop neighbors as adjacency map; for forensic / retract / supersede decisions), 'edges' (adjacency-map topology only, no node content; for graph-shape audits). dream_mode=True skips the recall-refresh side effect during maintenance / dream-cycle inspection.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with inspect fields. Required: node_id (str — the node ID to inspect, e.g. 'dv_NNNN'). Optional: view (str — one of 'recall' (default), 'deep', or 'edges'; controls the shape of returned data), dream_mode (bool — if True, skips the recall-refresh side effect and importance-score boost; use during maintenance / dream-cycle inspection, default False), include_superseded (bool — if True, include neighbors whose is_current=0 in topology and neighbor lists; default False).", "required": True},
        },
    },
    "engram_query": {
        "description": "First-class voluntary semantic-recall (autonoetic intentional-search complement to engram_surface's ambient/noetic auto-surfacing). Use when you want to recall something specifically. Combines FTS5 keyword + semantic similarity; returns tiered {id,summary}/{id,keywords} shape; refreshes recall on matched nodes. Use engram_inspect for full content on any result.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with query fields. Required: query (str — natural language search query). Optional: types (str — comma-separated node types to filter, e.g. 'derivation,observation_factual'; empty = all types), min_confidence (float — minimum confidence threshold 0.0–1.0, default 0.0), include_superseded (bool — include non-current/superseded nodes, default False), top_k (int — max results to return, default 10), summary_top_k (int — top-N results returned with recall_summary (Tier 1); remainder get recall_keywords only (Tier 2); default 3; ignored when return_debug=True), return_debug (bool — if True, return full legacy shape with composite_score and ranking internals for eval/harness use; suppresses tiered render; default False).", "required": True},
        },
    },
    "engram_query_pattern": {
        "description": "Run a named compositional graph-pattern query (KnowQL-inspired). Returns tiered {id,summary}/{id,keywords} shape. Six patterns: contradiction_obsolescence_ready, open_question_answerable, stale_load_bearing, cornerstone_candidate, tainted_still_valid, recent_resolution_echo. Three presets: high_precision / balanced (default) / high_recall. Telemetry logged per call.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with query_pattern fields. Required: pattern_name (str — one of: contradiction_obsolescence_ready, open_question_answerable, stale_load_bearing, cornerstone_candidate, tainted_still_valid, recent_resolution_echo). Optional: preset (str — one of high_precision / balanced (default) / high_recall; bundles cosine_threshold, top_k, min_confidence), cosine_threshold_override (float — if >= 0, overrides preset's cosine_threshold; default -1), top_k_override (int — if >= 0, overrides preset's top_k; default -1), min_confidence_override (float — if >= 0, overrides preset's min_confidence; default -1), summary_top_k (int — top-N candidates returned with recall_summary (Tier 1); remainder get recall_keywords only (Tier 2); default 3).", "required": True},
        },
    },
    "engram_get_subgraph": {
        "description": (
            "Browse a node's connection topology within N hops. "
            "BROWSING tool — shows connection topology + just-enough content to recognise which branch to follow. "
            "view='recall' (default): topology + hop-graduated summaries (root+1-hop: recall_summary+keywords; 2+hop: keywords only). "
            "view='edges': topology only, no content. "
            "For full node content, use engram_inspect. "
            "Chained-call pattern: subgraph → spot interesting node → engram_inspect that node → subgraph from there."
        ),
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with get_subgraph fields. Required: node_id (str — root node to explore, e.g. 'dv_NNNN'). Optional: depth (int — how many hops to traverse from root, default 2), direction (str — 'up' (toward evidence/sources), 'down' (toward derivations that cite this), or 'both' (default)), view (str — 'recall' (default, topology + hop-graduated summaries) or 'edges' (topology only; 'deep' is rejected)), dream_mode (bool — if True, skip recall_refresh on neighbour nodes; default False).", "required": True},
        },
    },
    "engram_derive": {
        "description": "Create a derived claim by combining evidence from existing nodes. You MUST specify reasoning_type to classify the argument. Types: deductive_modus_ponens (0.98), deductive_modus_tollens (0.98), deductive_hypothetical_syllogism (0.98), deductive_disjunctive (0.98), deductive_reductio (0.98), inductive_generalization (0.95, corroborative), inductive_enumeration (0.93, corroborative), inductive_statistical (0.90), inductive_causal (0.85), inductive_analogy (0.70), abductive_best_explanation (cap 0.80), abductive_elimination (cap 0.90), authority_expert (0.95), authority_consensus (0.98, corroborative). Single-payload signature — pass all fields as one JSON object string in payload_json. Required: claim, supporting_ids, logical_chain.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with derive fields. Required: claim (the atomic falsifiable claim), supporting_ids (comma-separated claim-bearing node IDs), logical_chain (explicit reasoning connecting premises to conclusion). Optional: reasoning_type (one of deductive_modus_ponens/deductive_modus_tollens/deductive_hypothetical_syllogism/deductive_disjunctive/deductive_reductio/inductive_generalization/inductive_enumeration/inductive_statistical/inductive_analogy/inductive_causal/abductive_best_explanation/abductive_elimination/authority_expert/authority_consensus — determines confidence computation), derivation_mode (LEGACY — use reasoning_type instead; chain or corroboration), context_ids (comma-separated 'cites' edges for definitions etc., not derives_from), use_stale (bool — opt-in MECH-5 stale-premise override), use_contested (bool — opt-in MECH-5 contradicted-premise override, #1654; set True only when deliberately building on a premise under an open unresolved contradiction, e.g. the derivation that will resolve it; auto-stamps metadata.built_on_contested, never author-supplied), warrant (str — optional Toulmin bridging principle; the general principle that licenses this inference — why do these premises support this claim? Leave blank if logical_chain fully captures it).", "required": True},
        },
    },
    "engram_add_axiom": {
        "description": "Record a foundational assumption taken as true without proof. Confidence fixed at 1.0, importance-anchored. Claim-bearing — derivations can cite axioms as premises. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: claim, basis.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with axiom fields. Required: claim (the axiom statement), basis (why this axiom is adopted — justification, not proof). Optional: context_ids (comma-separated node IDs cited).", "required": True},
        },
    },
    "engram_add_definition": {
        "description": "Record what a term means in this knowledge graph's context. No confidence score, not claim-bearing. Derivations can reference definitions via context_ids. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: term, definition.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with definition fields. Required: term (the term being defined), definition (what the term means). Optional: context_ids (comma-separated node IDs cited).", "required": True},
        },
    },
    "engram_add_conjecture": {
        "description": "Propose a hypothesis for investigation. Conjectures are provisional foundations — claim-bearing leaf nodes other derivations CAN cite (with confidence penalty). Use when you have a specific claim to build on before proving. For open-ended gaps, use engram_ask. Resolve via engram_resolve or promote via engram_supersede. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: claim, basis.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with conjecture fields. Required: claim (the hypothesis), basis (why this is worth investigating). Optional: initial_confidence (number in [0.10, 0.60], default 0.40), context_ids (comma-separated node IDs cited).", "required": True},
        },
    },
    "engram_add_goal": {
        "description": "Record a persistent directional goal. Goals are aspirational north-star directions (non-claim-bearing, no confidence, importance-anchored). Cannot serve as derivation premises. Use engram_goal_tension for value-level conflicts between goals. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: claim, motivation.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with goal fields. Required: claim (the goal statement — a desired state or direction), motivation (why this goal matters). Optional: context_ids (comma-separated node IDs cited).", "required": True},
        },
    },
    "engram_add_person": {
        "description": "Record a person the agent knows. Person nodes are relational (non-claim-bearing, no confidence, importance-anchored). Facts about a person should be stored as user_stated observations linked to the person node. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: name, role.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with person fields. Required: name (the person's name), role (their role or relationship to the agent). Optional: description (background, expertise, traits), aliases (comma-separated alternative names), context_ids (comma-separated node IDs cited), is_self (bool — mark as self-anchor; only one allowed).", "required": True},
        },
    },
    "engram_add_cornerstone": {
        "description": "Record an identity-forming cornerstone — a reframing pivot that durably restructured how the agent operates. Non-claim-bearing self-report node, importance-anchored. Tags cluster cornerstones into islands along a shared axis. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: tag, title, new_frame.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with cornerstone fields. Required: tag (clustering primitive — short axis label), title (one-line display title; used as claim text), new_frame (the frame that replaced the prior one). Optional: prior_frame (what was operating before; omit for inaugural), triggering_experience (narrative of the experience that caused the reframe), supporting_ids (comma-separated IDs of supporting nodes — feelings, observations, derivations, prior cornerstones).", "required": True},
        },
    },
    "engram_add_lesson": {
        "description": "Create a lesson node derived from one or more error incident observations. Lessons are abstract corrective patterns extracted from incidents (Gollwitzer-style implementation intentions). Claim-bearing, importance-anchored. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: claim, incident_ids, scaffolding_nudge, logical_chain.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with lesson fields. Required: claim (the abstract corrective pattern — action-focused), incident_ids (comma-separated observation IDs of concrete error incidents), scaffolding_nudge (the action-focused prompt injected when the tripwire fires), logical_chain (how incidents lead to this lesson). Optional: reasoning_type (default 'inductive_generalization'), context_ids (comma-separated node IDs cited).", "required": True},
        },
    },
    "engram_goal_tension": {
        "description": "Record a tension between two goals. Goal tensions are value-level conflicts (not factual contradictions) requiring root cause analysis and value examination. Both goals are preserved. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: goal_id_a, goal_id_b, description.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with goal-tension fields. Required: goal_id_a (first goal ID, gl_XXXX), goal_id_b (second goal ID, gl_XXXX), description (what tension exists between these goals). Optional: analysis (root cause analysis — why do these goals conflict?).", "required": True},
        },
    },
    "engram_link_about": {
        "description": "Create an `about` edge: this node is about a person (e.g., the agent's self-anchor). Symmetric and DAG-exempt — can be added retroactively. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: node_id.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with link_about fields. Required: node_id (the node to link, typically an observation, feeling_report, lesson, derivation, or cornerstone). Optional: person_id (target person node; defaults to self-anchor if empty).", "required": True},
        },
    },
    "engram_remove_edge": {
        "description": "Remove a non-cascade edge between two nodes — typically for correcting over-applied `about` edges without engram-surgical. Safe whitelist: about, tensions, subtask_of, serves, exemplifies. Blocked: derives_from, supports, supersedes, retracts, contradicts, resolves, cites (cascade-bearing, structural-commitment, or provenance). Idempotent: removing a non-existent edge returns success. Audit-logged in edit_history. Single-payload signature.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with remove_edge fields. Required: source_id (source node of the edge), target_id (target node of the edge), relation (one of: about, tensions, subtask_of, serves, exemplifies).", "required": True},
        },
    },
    "engram_add_task": {
        "description": "Create an actionable task that decomposes a goal into concrete work. Tasks are non-claim-bearing lightweight wrappers — conjectures hold the design content, tasks track what to do. Importance is dynamic: active (2.5), planned (2.0), done-milestone (1.5), done-routine (0.5). Single-payload signature — pass all fields as one JSON object string in payload_json. Required: description.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with task fields. Required: description (what needs to be done — concrete and actionable). Optional: goal_id (the goal this task serves, gl_XXXX, creates 'serves' edge), implements_ids (comma-separated conjecture or question IDs this task addresses), parent_task_id (parent task ID for subtasks, tk_XXXX; parent auto-promoted to milestone), scope ('milestone' or 'routine' default).", "required": True},
        },
    },
    "engram_update_task": {
        "description": "Update a task's status and rebalance its importance. Status transitions: planned→active (importance 2.5), →done (milestone 1.5, routine 0.5), →blocked (1.8). Single-payload signature — pass all fields as one JSON object string in payload_json. Required: task_id, new_status.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with update-task fields. Required: task_id (the task node ID, tk_XXXX), new_status (one of 'planned', 'active', 'done', 'blocked'). Optional: note (note about the status change, stored in metadata as part of status_history).", "required": True},
        },
    },
    "engram_report_feeling": {
        "description": "File a structured first-person self-report about a distinct internal state. Non-claim-bearing, no confidence, importance-anchored, dedup-exempt. Single-payload signature — pass all fields as one JSON object string in payload_json. Required fields inside payload_json: reported_state, trigger.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with feeling-report fields. Required: reported_state, trigger. Optional: categorical_tag (single human word like 'gratitude'), intensity_hint (0.0–1.0 or -1.0 to omit), context_ids (comma-separated node IDs the report cites).", "required": True},
        },
    },
    "engram_contradict": {
        "description": "Flag a conflict between two claim-bearing nodes (observations, derivations, theories). Single-payload signature — pass all fields as one JSON object string in payload_json. Required: node_id_a, node_id_b, description.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with contradict fields. Required: node_id_a (first conflicting node ID), node_id_b (second), description (what conflicts and why). Optional: root_cause (analysis of why the sources disagree).", "required": True},
        },
    },
    "engram_resolve": {
        "description": "Wire a 'resolves' edge from an existing claim-bearing node to a target (question, contradiction, prediction, conjecture, or goal_tension). Pure-wire as of issue #229 — does NOT create a derivation. Two-step workflow: compose the resolving derivation via engram_derive first, then call engram_resolve to wire it. When an existing canonical node already resolves the target, pass it directly. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: target_id, resolving_node_id.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with resolve fields. Required: target_id (qu_/ct_/pr_/cj_/gt_ node ID), resolving_node_id (a claim-bearing node ID — observation, derivation, theory, axiom, or conjecture — that resolves the target; must be is_current=1). Optional: prediction_outcome (confirmed/partially_confirmed/refuted/partially_refuted for predictions; supported/refuted/inconclusive for conjectures).", "required": True},
        },
    },
    "engram_ask": {
        "description": "Register an open research question. Questions are NOT claim-bearing — no node can cite them as premises. Use for open-ended gaps to investigate. For specific claims to build on before proving, use kg_add_conjecture instead. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: question.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with ask fields. Required: question (the specific, answerable question to investigate). Optional: context_ids (comma-separated relevant but insufficient node IDs), category (research / design / implementation / planning / meta), lacks (external_evidence / empirical_data / human_decision / implementation / synthesis / prerequisite).", "required": True},
        },
    },
    "engram_supersede": {
        "description": "Mark new_node_id as the successor of old_node_id. Purely relational — does NOT create a new node (caller creates replacement via type's canonical creation tool first). Single-payload signature — pass all fields as one JSON object string in payload_json. Required: old_node_id, new_node_id.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with supersede fields. Required: old_node_id (the node being superseded), new_node_id (the replacement, already created via its type's canonical creation tool). Optional: supersede_reason (short rationale for the revision; lands in supersedes-edge metadata).", "required": True},
        },
    },
    "engram_retract": {
        "description": "Retract an erroneous node and flag downstream dependents as tainted. Unlike engram_supersede (opinion evolved due to new evidence), engram_retract is for ERROR CORRECTION — the node was never valid. Single-payload signature — pass all fields as one JSON object string in payload_json. Required: node_id, error_type, reason. Note: the OUTER param is payload_json; there is also an INNER field replacement_json inside the payload — don't confuse them.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with retract fields. Required: node_id (the node to retract), error_type (one of fabricated_quote/wrong_citation/wrong_evidence/hallucinated_claim/duplicate/other), reason (human-readable explanation). Optional: replacement_json (inner JSON object string with replacement observation fields {quoted_text, interpretation, claim, quote_type, source_class}; cites the same evidence as the retracted node).", "required": True},
        },
    },
    "engram_reflect": {
        "description": "[Sleep cycle — pre-dream briefing.] Structured self-audit of the graph (contradictions, open questions, weak claims, overdue predictions, uncited observations, tainted and retracted nodes). Opens the dream cycle for the engram-sleep skill. Not a tool to reach for during awake-state work. High-volume categories (weakly_grounded, thin_support_derivations, uncited_observations) are tiered: top summary_top_k entries get full claim text (from recall_summary if set, else claim); the remainder get keyword-style entries (recall_keywords list, no claim key). Low-volume categories source-swap their content field from recall_summary with claim fallback.",
        "parameters": {
            "payload_json": {"type": "string", "description": "Optional JSON object (as a string) with reflect fields. Optional: summary_top_k (int — how many top-importance entries in each high-volume category receive summary-style rendering; remainder get keyword-style; default 5; clamped to >= 0; set 0 for all keyword-style; set high e.g. 50 for all summary-style). Empty payload '{}' or omitted payload runs the default reflect."},
        },
    },
    "engram_diagnose": {
        "description": "Comprehensive quantitative health audit. Side-effect-free. Returns health score 0-100 across five dimensions.",
        "parameters": {},
    },
    "engram_history": {
        "description": "Browse edit history and diagnostic snapshots. Side-effect-free.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with history fields. All optional: mode (str — 'edits' (default) or 'diagnostics'; edits = chronological audit trail of mutations; diagnostics = metric snapshots at each checkpoint), node_id (str — filter edits to a specific node; edits mode only), action (str — filter by action type: created, reopened, resolved, retracted, stale_flagged, superseded, tainted, trust_tier_set; edits mode only), since (str — ISO timestamp — return only entries after this time), limit (int — max entries to return, default 50, max 200).", "required": True},
        },
    },
    "engram_list_focused": {
        "description": "List the set of nodes currently in focus. Use this to maintain context across sessions.",
        "parameters": {},
    },
    "engram_list": {
        "description": "List nodes in compact triage format. No neighbor data, no memory refresh. Supports structured filters via filters_json (recursive AND/OR/NOT with contains/starts_with/ends_with operators). Default scans current-revision only; set include_superseded=True to scan the historical layer (text-layer leak audits).",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with list fields. All optional: node_type (str — filter by node type, e.g. 'question', 'observation_factual'; empty = all types; conflicts with 'type' field in filters_json), status (str — filter by status, e.g. 'open', 'resolved', 'active'; conflicts with 'status' field in filters_json), sort_by (str — sort order: 'id' (default), 'created' (newest first), 'importance' (highest first), 'recalls' (most recalled first)), limit (int — max results, default 100; set 0 for metadata-only; ignored if unlimited=True; hard ceiling 500 in single-field mode), filters_json (str — JSON-encoded recursive condition tree with AND/OR/NOT logic and eq/ne/gt/gte/lt/lte/in/not_in/between/contains/starts_with/ends_with/is_null/is_not_null operators; empty string = use single-field mode), fields_json (str — JSON-encoded list of column names to project; empty = default compact triage shape), unlimited (bool — if True, return all matched rows regardless of limit), include_superseded (bool — if True, scan non-current/superseded nodes too; default False).", "required": True},
        },
    },
    "engram_stats": {
        "description": "Get graph statistics: node/edge counts, open questions, open predictions, weakest nodes, confidence distribution, memory tier info. Supports mode windowing (all/1-turn/7-turn/30-turn) and sections selector.",
        "parameters": {
            "payload_json": {
                "type": "string",
                "description": (
                    'Optional JSON object with: mode (str: "all"|"1-turn"|"7-turn"|"30-turn", default "all") '
                    'and sections (list of str: structure|edges|confidence|open_questions|open_predictions|'
                    'reasoning_breakdown|weakest_nodes|health_score|memory, default all). '
                    'Default empty payload "{}" preserves legacy no-arg behavior.'
                ),
            },
        },
    },
    "engram_nap": {
        "description": "Persist context to ENGRAM without advancing the turn counter. Use BEFORE compaction or at end of work-burst to save knowledge; arms a nap_checkpoint feeling-report nudge. Forgetting stays paused. Single-payload signature — pass all fields as one JSON object string in payload_json.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with nap fields. Required: message (summary of what was learned/changed in this work-burst).", "required": True},
        },
    },
    "engram_advance_turn": {
        "description": "[Sleep cycle / first session only — DO NOT call awake.] End-of-day session checkpoint that advances the global turn counter (drives forgetting) and logs the session summary. IRREVERSIBLE — turn advances cannot be undone without direct DB intervention. For pre-compaction / end-of-burst persistence in awake state, use engram_nap instead. Single-payload signature — pass all fields as one JSON object string in payload_json.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) with advance_turn fields. Required: message (summary of what was learned/changed across the cohort being closed by this turn advance).", "required": True},
        },
    },
    "engram_scan_emergence": {
        "description": "[Sleep cycle — invoked by dream-mode dispatch; do not call awake.] Scan the graph for emergent patterns that could become cornerstones. Surfaces candidate cornerstones, contradictions, and dormant-but-load-bearing nodes. All fields optional — empty payload runs the default scan. Single-payload signature — pass all fields as one JSON object string in payload_json.",
        "parameters": {
            "payload_json": {
                "type": "string",
                "description": (
                    "Optional JSON object (as a string) with scan fields. "
                    "All fields optional: "
                    "min_cluster_size (int, default 3 — minimum cluster size to surface), "
                    "similarity_threshold (float, default 0.55 — cosine similarity threshold for clustering; higher = stricter), "
                    "focus (str, default 'self' — scope of the scan: 'self', 'all', or 'person:<pn_id>'), "
                    "node_type_filter (str — comma-separated node types to include; "
                    "default 'observation_factual,feeling_report,lesson,derivation,observation_predictive'). "
                    "Empty payload '{}' or omitted payload runs the default scan."
                ),
            },
        },
    },
}

# Tools that only read from the graph (no mutations).
# Note: engram_reflect is listed here because it is non-mutating, but it is
# sleep-cycle-only at the workflow level — see its [Sleep cycle] description.
# READ_ONLY membership does NOT imply awake-safe; check the description.
READ_ONLY_TOOLS = {
    "engram_query", "engram_get_subgraph", "engram_stats", "engram_reflect",
}


# ---------------------------------------------------------------------------
# Client class
# ---------------------------------------------------------------------------

class EngramClient:
    """Direct Python client for engram tools.
    
    Each instance manages its own database directory, so you can run
    parallel evaluations with isolated graphs.
    """

    def __init__(self, db_dir: Optional[str] = None, server_module_path: str = ""):
        """Initialize the client.
        
        Args:
            db_dir: Path to the database directory. Defaults to ~/.engram.
                    Use different paths for isolated eval runs.
            server_module_path: Path to the directory containing server.py.
                                If empty, assumes server.py is importable.
        """
        if server_module_path and server_module_path not in sys.path:
            sys.path.insert(0, server_module_path)

        # Override the data directory before importing server
        resolved_dir = str(Path(db_dir).expanduser()) if db_dir else None
        if resolved_dir:
            os.environ["ENGRAM_HOME"] = resolved_dir

        # Import fresh — needed if we're running multiple clients
        # with different db_dirs in the same process. Purge server,
        # engram_core, AND every "family" module (#872 waves 2-9) as one unit
        # (#1679): the path globals live on engram_core, and each family
        # module binds its own module-level `core` reference via
        # `import engram_core as core` (the project's canonical
        # shared-state-access convention). If a family module is left in
        # sys.modules while server+engram_core are purged and re-imported,
        # server.py's re-execution finds the family module ALREADY cached
        # and reuses it as-is — so that family module's `core` still points
        # at the FIRST client's now-stale engram_core object, not the fresh
        # one. Calls that route through a family module (e.g. engram_stats,
        # engram_observation) would then silently read/write against the
        # first client's DATA_DIR/DB_PATH instead of this client's.
        #
        # Purge by PREFIX ("server" exactly, or any "engram_*" module other
        # than this module itself) rather than an enumerated list of family
        # module names. An enumerated list is exactly the shape that caused
        # #1679: the original purge tuple (server, engram_core) silently fell
        # out of sync as family modules were added across #872 waves 2-9, and
        # a hardcoded 14-name list here would fall out of sync again the next
        # time a 13th family module is added — nothing would catch it, since
        # nothing derives the list from the modules that actually exist.
        # Matching by prefix is self-updating: any current or future
        # engram_* module gets purged and forced to re-bind a fresh `core`
        # on each new EngramClient instantiation, with no list to maintain.
        #
        # Self-exclusion: this __init__ method is executing as code that
        # lives INSIDE the "engram_client" module. Purging "engram_client"
        # from sys.modules here would not affect the currently-executing
        # frame (the function object keeps running), but it would orphan the
        # cached module — a subsequent `import engram_client` anywhere else
        # in the process would re-execute the module body and mint a NEW
        # EngramClient class object, breaking isinstance checks and any
        # module-level state associated with the original import. The
        # established prefix-purge pattern elsewhere in this codebase (see
        # tests/test_scope_export.py's _fresh_server_at helper) carries this
        # same self-exclusion for the same reason.
        for _mod in list(sys.modules):
            if _mod == "server" or (_mod.startswith("engram_") and _mod != "engram_client"):
                sys.modules.pop(_mod, None)
        import server as _server

        # Redirect ALL module-level paths via the centralized function.
        # This guarantees every path constant (including any newly added ones)
        # points to the sandbox directory. Manual patching is fragile — the test-isolation lesson
        # documents a contamination incident caused by missing one path.
        if resolved_dir:
            _server._configure_paths(resolved_dir)

        self._server = _server
        self._server._ensure_data_dir()

        # Build function dispatch table
        self._tools = {}
        for name in TOOL_SCHEMAS:
            func = getattr(self._server, name, None)
            if func:
                self._tools[name] = func

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def call(self, tool_name: str, arguments: dict[str, Any] = None) -> dict:
        """Execute a tool call and return parsed JSON result.

        Args:
            tool_name: Name of the kg_* tool to call.
            arguments: Dict of keyword arguments for the tool.

        Returns:
            Parsed JSON response from the tool.

        Single-payload tools (e.g. engram_add_observation): if the schema's
        only parameter is `payload_json` and the caller passes the legacy
        named-field dict instead, this auto-wraps it. Lets pre-migration
        callers keep working without each having to know about the JSON
        shape, while LLMs that follow the published schema pass payload_json
        directly. New code should pass `{"payload_json": json.dumps({...})}`
        explicitly.
        """
        if tool_name not in self._tools:
            return {"error": f"Unknown tool '{tool_name}'. Available: {self.tool_names}"}

        args = arguments or {}

        schema = TOOL_SCHEMAS.get(tool_name)
        if (schema
                and list(schema.get("parameters", {}).keys()) == ["payload_json"]
                and "payload_json" not in args):
            args = {"payload_json": json.dumps(args)}

        try:
            result_str = self._tools[tool_name](**args)
            return json.loads(result_str)
        except TypeError as e:
            return {"error": f"Invalid arguments for {tool_name}: {e}"}
        except Exception as e:
            return {"error": f"Tool execution error: {e}"}

    def call_raw(self, tool_name: str, arguments: dict[str, Any] = None) -> str:
        """Execute a tool call and return raw JSON string (for passing to LLMs)."""
        result = self.call(tool_name, arguments)
        return json.dumps(result)

    # -------------------------------------------------------------------
    # LLM-specific tool declaration formats
    # -------------------------------------------------------------------

    def gemini_tool_declarations(self) -> list[dict]:
        """Generate tool declarations for Google Gemini function calling.
        
        Returns a list suitable for google.genai.types.Tool(function_declarations=[...])
        """
        declarations = []
        for name, schema in TOOL_SCHEMAS.items():
            if name not in self._tools:
                continue

            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"].upper()}
                prop["description"] = param_spec.get("description", "")
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)

            decl = {
                "name": name,
                "description": schema["description"],
            }
            if properties:
                decl["parameters"] = {
                    "type": "OBJECT",
                    "properties": properties,
                    "required": required,
                }
            declarations.append(decl)
        return declarations

    def openai_tool_declarations(self) -> list[dict]:
        """Generate tool declarations for OpenAI / Ollama function calling.
        
        Returns a list of tool objects in OpenAI's format, compatible with:
        - OpenAI API (GPT-4, etc.)
        - Ollama (llama3.1, qwen2.5, mistral, etc.)
        - Any OpenAI-compatible API
        """
        tools = []
        for name, schema in TOOL_SCHEMAS.items():
            if name not in self._tools:
                continue

            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"]}
                if "description" in param_spec:
                    prop["description"] = param_spec["description"]
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)

            tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
            tools.append(tool)
        return tools

    def anthropic_tool_declarations(self) -> list[dict]:
        """Generate tool declarations for Anthropic Claude API.
        
        Returns a list of tool objects in Anthropic's format.
        """
        tools = []
        for name, schema in TOOL_SCHEMAS.items():
            if name not in self._tools:
                continue

            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"]}
                if "description" in param_spec:
                    prop["description"] = param_spec["description"]
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)

            tool = {
                "name": name,
                "description": schema["description"],
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
            tools.append(tool)
        return tools

    def _filtered_schemas(self, tool_filter: set = None):
        """Yield (name, schema) pairs, optionally filtered to a subset."""
        for name, schema in TOOL_SCHEMAS.items():
            if name not in self._tools:
                continue
            if tool_filter and name not in tool_filter:
                continue
            yield name, schema

    def gemini_tool_declarations_filtered(self, tool_filter: set) -> list[dict]:
        """Like gemini_tool_declarations but only for tools in tool_filter."""
        declarations = []
        for name, schema in self._filtered_schemas(tool_filter):
            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"].upper()}
                prop["description"] = param_spec.get("description", "")
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)
            decl = {"name": name, "description": schema["description"]}
            if properties:
                decl["parameters"] = {"type": "OBJECT", "properties": properties, "required": required}
            declarations.append(decl)
        return declarations

    def openai_tool_declarations_filtered(self, tool_filter: set) -> list[dict]:
        """Like openai_tool_declarations but only for tools in tool_filter."""
        tools = []
        for name, schema in self._filtered_schemas(tool_filter):
            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"]}
                if "description" in param_spec:
                    prop["description"] = param_spec["description"]
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)
            tools.append({"type": "function", "function": {
                "name": name, "description": schema["description"],
                "parameters": {"type": "object", "properties": properties, "required": required},
            }})
        return tools

    def anthropic_tool_declarations_filtered(self, tool_filter: set) -> list[dict]:
        """Like anthropic_tool_declarations but only for tools in tool_filter."""
        tools = []
        for name, schema in self._filtered_schemas(tool_filter):
            properties = {}
            required = []
            for param_name, param_spec in schema["parameters"].items():
                prop = {"type": param_spec["type"]}
                if "description" in param_spec:
                    prop["description"] = param_spec["description"]
                if "enum" in param_spec:
                    prop["enum"] = param_spec["enum"]
                properties[param_name] = prop
                if param_spec.get("required"):
                    required.append(param_name)
            tools.append({"name": name, "description": schema["description"],
                "input_schema": {"type": "object", "properties": properties, "required": required}})
        return tools

    def system_prompt(self) -> str:
        """Return RESPONSE_STYLE.md content for use as a system prompt (legacy; SKILL.md retired #1149)."""
        base_dir = Path(__file__).parent
        parts = []
        skill_path = base_dir / "SKILL.md"
        if skill_path.exists():
            parts.append(skill_path.read_text(encoding="utf-8"))
        style_path = base_dir / "RESPONSE_STYLE.md"
        if style_path.exists():
            parts.append(style_path.read_text(encoding="utf-8"))
        return "\n\n".join(parts) if parts else "You are a research agent with access to a structured knowledge graph."

    def reset(self):
        """Delete all data and start fresh."""
        import shutil
        # Mutable path globals live on engram_core (no server-side binding —
        # #872 wave 1); read through the server's own core reference so this
        # follows whatever core instance the imported server is bound to.
        data_dir = self._server.core.DATA_DIR
        if data_dir.exists():
            shutil.rmtree(data_dir)
        self._server._ensure_data_dir()


# ---------------------------------------------------------------------------
# Convenience: tool-use loop for any LLM
# ---------------------------------------------------------------------------

def run_tool_calls(client: EngramClient, tool_calls: list[dict]) -> list[dict]:
    """Execute a batch of tool calls and return results.
    
    Args:
        client: EngramClient instance.
        tool_calls: List of {"name": "engram_xxx", "arguments": {...}} dicts.
        
    Returns:
        List of {"name": "engram_xxx", "result": {...}} dicts.
    """
    results = []
    for tc in tool_calls:
        name = tc.get("name", tc.get("function", {}).get("name", ""))
        args = tc.get("arguments", tc.get("function", {}).get("arguments", {}))
        # Handle string-encoded arguments (OpenAI format)
        if isinstance(args, str):
            args = json.loads(args)
        result = client.call(name, args)
        results.append({"name": name, "result": result})
    return results


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    print("=== ENGRAM Client Smoke Test ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        client = EngramClient(
            db_dir=tmpdir,
            server_module_path=str(Path(__file__).parent),
        )

        print(f"Tools available: {len(client.tool_names)}")
        print(f"  {client.tool_names}\n")

        # Test direct calls — canonical wave-3 payload_json form
        r = client.call("engram_add_evidence", {
            "payload_json": json.dumps({
                "url": "https://reuters.com/test",
                "title": "Test Article",
            })
        })
        print(f"Add evidence: {r['status']} → {r['evidence_id']}")

        r = client.call("engram_add_observation", {
            "payload_json": json.dumps({
                "evidence_id": "ev_NNNN",
                "quoted_text": "Gold fell 9.6%",
                "interpretation": "Worst weekly decline in 15 years",
                "claim": "Gold fell 9.6% in the week ending March 20, 2026",
                "quote_type": "hard_data",
            }),
        })
        print(f"Add observation: {r['status']} → {r['observation_id']} (conf: {r['confidence']})")

        r = client.call("engram_stats")
        print(f"Stats: {r['node_counts_by_type']}")

        # Test tool declarations
        gemini = client.gemini_tool_declarations()
        print(f"\nGemini declarations: {len(gemini)} tools")

        openai = client.openai_tool_declarations()
        print(f"OpenAI declarations: {len(openai)} tools")

        anthropic = client.anthropic_tool_declarations()
        print(f"Anthropic declarations: {len(anthropic)} tools")

        # Test batch tool calls
        results = run_tool_calls(client, [
            {"name": "engram_query", "arguments": {"query": "gold"}},
            {"name": "engram_reflect", "arguments": {}},
        ])
        print(f"\nBatch call results: {len(results)} responses")

        print(f"\nSystem prompt length: {len(client.system_prompt())} chars")

        print("\n=== ALL GOOD ===")
