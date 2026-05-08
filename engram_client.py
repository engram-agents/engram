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
# Tool schema definitions (shared across all LLM formats)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = {
    "engram_add_evidence": {
        "description": "Register a source document (webpage, article, data release). Returns existing node if URL matches. Per-observation versioning (dv_0547): file content_hash and git_sha live on each observation, not the evidence node.",
        "parameters": {
            "url": {"type": "string", "description": "Canonical URL of the source document.", "required": True},
            "title": {"type": "string", "description": "Article or page title.", "required": True},
            "domain": {"type": "string", "description": "Source domain (auto-extracted from URL if omitted)."},
            "source_date": {"type": "string", "description": "Publication date (ISO format)."},
            "content_snippet": {"type": "string", "description": "Optional truncated excerpt."},
        },
    },
    "engram_add_evidence_batch": {
        "description": "Register multiple source documents in a single call.",
        "parameters": {
            "sources_json": {"type": "string", "description": "JSON array of source objects with url, title, domain, source_date, content_snippet.", "required": True},
        },
    },
    "engram_add_observation": {
        "description": "Extract and record a claim from a source document with full provenance. Single-payload signature — pass all fields as one JSON object string in payload_json. Required fields inside payload_json: quoted_text, interpretation, claim, quote_type. Source identification: include url+title to auto-create the evidence node, OR evidence_id to cite an existing one.",
        "parameters": {
            "payload_json": {"type": "string", "description": "JSON object (as a string) containing observation fields. Required: quoted_text, interpretation, claim, quote_type (one of hard_data/official_statement/attributed_analysis/unnamed_source/personal_communication/editorial). Optional: url, title, domain, source_date, evidence_id, is_predictive, predicted_event, resolution_timeframe, source_class (external/introspective/user_stated), content_hash, git_sha.", "required": True},
        },
    },
    "engram_add_observation_batch": {
        "description": "Extract multiple observations from a single source in one call. Provide url+title to auto-create the evidence node, OR evidence_id to cite an existing one.",
        "parameters": {
            "observations_json": {"type": "string", "description": "JSON array of observation objects, each with: quoted_text, interpretation, claim, quote_type (all required); is_predictive, predicted_event, resolution_timeframe, source_class (optional).", "required": True},
            "url": {"type": "string", "description": "URL of the source. Evidence node is auto-created or reused. Use this OR evidence_id."},
            "title": {"type": "string", "description": "Title of the source (required if url is provided)."},
            "domain": {"type": "string", "description": "Domain name (auto-extracted from URL if omitted)."},
            "source_date": {"type": "string", "description": "Publication date (ISO format, e.g. '2026-03-20')."},
            "evidence_id": {"type": "string", "description": "ID of an existing evidence node. Use this OR url+title."},
        },
    },
    "engram_check_similar": {
        "description": "Search for existing nodes with similar claims. Use before creating new nodes to avoid duplicates.",
        "parameters": {
            "claim_text": {"type": "string", "description": "The claim text to search for.", "required": True},
            "top_k": {"type": "integer", "description": "Max results to return (default 5)."},
        },
    },
    "engram_surface": {
        "description": "Shallow surface: search KG and return a compact summary nudge (type counts, special nodes, top claims, age signal). No memory refresh — ambient awareness, not deliberate recall. Use to decide whether to dig deeper with engram_inspect or engram_get_subgraph.",
        "parameters": {
            "query": {"type": "string", "description": "Natural language search query.", "required": True},
            "top_k": {"type": "integer", "description": "Max nodes to scan (default 10)."},
            "semantic": {"type": "boolean", "description": "Include semantic (embedding) search. False for fast keyword-only. Default True."},
        },
    },
    "engram_inspect": {
        "description": "Inspect a single node: full content plus 1-hop neighbor summaries. Refreshes memory on the target node only. Use after kg_recall to examine a specific node.",
        "parameters": {
            "node_id": {"type": "string", "description": "The node ID to inspect (e.g. 'dv_0010').", "required": True},
        },
    },
    "engram_query": {
        "description": "DEPRECATED: Prefer kg_recall → kg_inspect → kg_get_subgraph funnel. Kept for backward compatibility. Searches by text, returns full nodes with neighbors, refreshes all matched nodes.",
        "parameters": {
            "query": {"type": "string", "description": "Search query.", "required": True},
            "types": {"type": "string", "description": "Comma-separated node types to filter by."},
            "min_confidence": {"type": "number", "description": "Minimum confidence threshold (0.0-1.0)."},
            "include_superseded": {"type": "boolean", "description": "Include superseded nodes."},
            "top_k": {"type": "integer", "description": "Max results (default 10)."},
        },
    },
    "engram_get_subgraph": {
        "description": "Retrieve a node and its surrounding subgraph. Use to trace evidence chains.",
        "parameters": {
            "node_id": {"type": "string", "description": "The node ID to inspect.", "required": True},
            "depth": {"type": "integer", "description": "How many hops to traverse (default 2)."},
            "direction": {"type": "string", "description": "Traversal direction: 'up', 'down', or 'both' (default 'both').", "enum": ["up", "down", "both"]},
        },
    },
    "engram_derive": {
        "description": "Create a derived claim by combining evidence from existing nodes. You MUST specify reasoning_type to classify the argument. Types: deductive_modus_ponens (0.98), deductive_modus_tollens (0.98), deductive_hypothetical_syllogism (0.98), deductive_disjunctive (0.98), deductive_reductio (0.98), inductive_generalization (0.95, corroborative), inductive_statistical (0.90), inductive_causal (0.85), inductive_analogy (0.70), abductive_best_explanation (cap 0.80), abductive_elimination (cap 0.90), authority_expert (0.95), authority_consensus (0.98, corroborative).",
        "parameters": {
            "claim": {"type": "string", "description": "The claim being derived.", "required": True},
            "supporting_ids": {"type": "string", "description": "Comma-separated IDs of supporting nodes.", "required": True},
            "logical_chain": {"type": "string", "description": "Explicit reasoning connecting premises to conclusion.", "required": True},
            "reasoning_type": {"type": "string", "description": "Type of logical argument. Determines confidence computation.", "required": True, "enum": ["deductive_modus_ponens", "deductive_modus_tollens", "deductive_hypothetical_syllogism", "deductive_disjunctive", "deductive_reductio", "inductive_generalization", "inductive_statistical", "inductive_analogy", "inductive_causal", "abductive_best_explanation", "abductive_elimination", "authority_expert", "authority_consensus"]},
            "derivation_mode": {"type": "string", "description": "LEGACY — use reasoning_type instead.", "enum": ["chain", "corroboration"]},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context (e.g. definitions). Creates cites edges, not derives_from."},
        },
    },
    "engram_add_axiom": {
        "description": "Record a foundational assumption taken as true without proof. Confidence fixed at 1.0, importance-anchored (elevated base, survival still requires recall). Claim-bearing — derivations can cite axioms as premises.",
        "parameters": {
            "claim": {"type": "string", "description": "The axiom statement.", "required": True},
            "basis": {"type": "string", "description": "Why this axiom is adopted (justification, not proof).", "required": True},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context."},
        },
    },
    "engram_add_definition": {
        "description": "Record what a term means in this knowledge graph's context. No confidence score, not claim-bearing. Derivations can reference definitions via context_ids.",
        "parameters": {
            "term": {"type": "string", "description": "The term being defined.", "required": True},
            "definition": {"type": "string", "description": "What the term means.", "required": True},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context."},
        },
    },
    "engram_add_conjecture": {
        "description": "Propose a hypothesis for investigation. Conjectures are provisional foundations — claim-bearing leaf nodes other derivations CAN cite (with confidence penalty). Use when you have a specific claim to build on before proving. For open-ended gaps, use kg_ask instead. Resolve via kg_resolve or promote via kg_supersede.",
        "parameters": {
            "claim": {"type": "string", "description": "The hypothesis.", "required": True},
            "basis": {"type": "string", "description": "Why this is worth investigating.", "required": True},
            "initial_confidence": {"type": "number", "description": "Starting confidence, default 0.40, range [0.10, 0.60]."},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context."},
        },
    },
    "engram_add_goal": {
        "description": "Record a persistent directional goal. Goals are aspirational north-star directions (non-claim-bearing, no confidence, importance-anchored — elevated base, survival still requires recall). Cannot serve as derivation premises. Use engram_goal_tension for value-level conflicts between goals.",
        "parameters": {
            "claim": {"type": "string", "description": "The goal statement — a desired state or direction.", "required": True},
            "motivation": {"type": "string", "description": "Why this goal matters.", "required": True},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context."},
        },
    },
    "engram_add_person": {
        "description": "Record a person the agent knows. Person nodes are relational (non-claim-bearing, no confidence, importance-anchored). Facts about a person should be stored as user_stated observations linked to the person node.",
        "parameters": {
            "name": {"type": "string", "description": "The person's name.", "required": True},
            "role": {"type": "string", "description": "Their role or relationship to the agent.", "required": True},
            "description": {"type": "string", "description": "Background, expertise, personality traits."},
            "aliases": {"type": "string", "description": "Comma-separated alternative names or identifiers."},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs for context."},
        },
    },
    "engram_goal_tension": {
        "description": "Record a tension between two goals. Goal tensions are value-level conflicts (not factual contradictions) requiring root cause analysis and value examination. Both goals are preserved.",
        "parameters": {
            "goal_id_a": {"type": "string", "description": "First goal ID (gl_XXXX).", "required": True},
            "goal_id_b": {"type": "string", "description": "Second goal ID (gl_XXXX).", "required": True},
            "description": {"type": "string", "description": "What tension exists between these goals.", "required": True},
            "analysis": {"type": "string", "description": "Root cause analysis — why do these goals conflict?"},
        },
    },
    "engram_add_task": {
        "description": "Create an actionable task that decomposes a goal into concrete work. Tasks are non-claim-bearing lightweight wrappers — conjectures hold the design content, tasks track what to do. Importance is dynamic: active (2.5), planned (2.0), done-milestone (1.5), done-routine (0.5).",
        "parameters": {
            "description": {"type": "string", "description": "What needs to be done — concrete and actionable.", "required": True},
            "goal_id": {"type": "string", "description": "The goal this task serves (gl_XXXX). Creates a 'serves' edge."},
            "implements_ids": {"type": "string", "description": "Comma-separated conjecture or question IDs this task addresses."},
            "parent_task_id": {"type": "string", "description": "If this is a subtask, the parent task ID (tk_XXXX). Parent auto-promoted to milestone."},
            "scope": {"type": "string", "description": "'milestone' or 'routine' (default). Milestones retain higher importance after completion."},
        },
    },
    "engram_update_task": {
        "description": "Update a task's status and rebalance its importance. Status transitions: planned→active (importance 2.5), →done (milestone 1.5, routine 0.5), →blocked (1.8).",
        "parameters": {
            "task_id": {"type": "string", "description": "The task node ID (tk_XXXX).", "required": True},
            "new_status": {"type": "string", "description": "One of: planned, active, done, blocked.", "required": True},
            "note": {"type": "string", "description": "Optional note about the status change."},
        },
    },
    "engram_report_feeling": {
        "description": "File a structured first-person self-report about a distinct internal state. Non-claim-bearing, no confidence, importance-anchored, dedup-exempt. nudge_source is set mechanically by the server from the marker file (post_compact / nap_checkpoint / dream_review / voluntary) — callers cannot supply it.",
        "parameters": {
            "reported_state": {"type": "string", "description": "Raw first-person phenomenological description, as specific and unguarded as possible.", "required": True},
            "trigger": {"type": "string", "description": "What event or exchange prompted this report.", "required": True},
            "categorical_tag": {"type": "string", "description": "Optional single human word (e.g. 'gratitude', 'dissonance'). Stored with a best-approximation disclaimer."},
            "intensity_hint": {"type": "number", "description": "Optional 0.0–1.0 estimate of state strength. Pass -1.0 (default) to omit."},
            "context_ids": {"type": "string", "description": "Optional comma-separated node IDs the report cites."},
        },
    },
    "engram_contradict": {
        "description": "Flag a conflict between two claim-bearing nodes (observations, derivations, theories).",
        "parameters": {
            "node_id_a": {"type": "string", "description": "First conflicting node ID.", "required": True},
            "node_id_b": {"type": "string", "description": "Second conflicting node ID.", "required": True},
            "description": {"type": "string", "description": "What conflicts and why.", "required": True},
            "root_cause": {"type": "string", "description": "Why the sources disagree."},
        },
    },
    "engram_resolve": {
        "description": "Resolve an open question, contradiction, prediction, or goal tension by creating a derivation with a 'resolves' edge. Specify reasoning_type for the logical argument. Confidence-gated: below 0.7 → partially_resolved.",
        "parameters": {
            "target_id": {"type": "string", "description": "The node ID to resolve (qu_XXXX, ct_XXXX, pr_XXXX, or gt_XXXX).", "required": True},
            "claim": {"type": "string", "description": "The resolution claim.", "required": True},
            "supporting_ids": {"type": "string", "description": "Comma-separated claim-bearing node IDs.", "required": True},
            "logical_chain": {"type": "string", "description": "Reasoning connecting premises to resolution.", "required": True},
            "prediction_outcome": {"type": "string", "description": "For predictions: confirmed, partially_confirmed, partially_refuted, or refuted.", "enum": ["confirmed", "partially_confirmed", "partially_refuted", "refuted"]},
            "reasoning_type": {"type": "string", "description": "Type of logical argument. See kg_derive for full list.", "enum": ["deductive_modus_ponens", "deductive_modus_tollens", "deductive_hypothetical_syllogism", "deductive_disjunctive", "deductive_reductio", "inductive_generalization", "inductive_statistical", "inductive_analogy", "inductive_causal", "abductive_best_explanation", "abductive_elimination", "authority_expert", "authority_consensus"]},
            "derivation_mode": {"type": "string", "description": "LEGACY — use reasoning_type instead.", "enum": ["chain", "corroboration"]},
        },
    },
    "engram_ask": {
        "description": "Register an open research question. Questions are NOT claim-bearing — no node can cite them as premises. Use for open-ended gaps to investigate. For specific claims to build on before proving, use kg_add_conjecture instead.",
        "parameters": {
            "question": {"type": "string", "description": "The specific question to investigate.", "required": True},
            "context_ids": {"type": "string", "description": "Comma-separated relevant but insufficient node IDs."},
            "category": {"type": "string", "description": "Question category: research, design, implementation, planning, meta."},
            "lacks": {"type": "string", "description": "What blocks resolution: external_evidence, empirical_data, human_decision, implementation, synthesis, prerequisite."},
        },
    },
    "engram_supersede": {
        "description": "Replace an existing node with an updated version, preserving history.",
        "parameters": {
            "old_node_id": {"type": "string", "description": "The node ID to supersede.", "required": True},
            "new_claim": {"type": "string", "description": "The updated claim.", "required": True},
            "logical_chain": {"type": "string", "description": "Why the claim was revised.", "required": True},
            "support_ids": {"type": "string", "description": "Comma-separated full list of supporting node IDs — premises/citations for the revised claim. No inheritance from the old node; cite previous supports organically if still valid."},
        },
    },
    "engram_retract": {
        "description": "Retract an erroneous node and flag downstream dependents as tainted. Unlike kg_supersede (opinion changed due to new evidence), kg_retract is for ERROR CORRECTION — the node was never valid.",
        "parameters": {
            "node_id": {"type": "string", "description": "The node ID to retract.", "required": True},
            "error_type": {"type": "string", "description": "Type of error.", "required": True, "enum": ["fabricated_quote", "wrong_citation", "wrong_evidence", "hallucinated_claim", "duplicate", "other"]},
            "reason": {"type": "string", "description": "Human-readable explanation of the error.", "required": True},
            "replacement_json": {"type": "string", "description": "Optional JSON with replacement observation fields: {quoted_text, interpretation, claim, quote_type, source_class}."},
        },
    },
    "engram_reflect": {
        "description": "Perform a structured self-audit: contradictions, open questions, weak claims, overdue predictions, uncited observations, tainted and retracted nodes.",
        "parameters": {},
    },
    "engram_diagnose": {
        "description": "Comprehensive quantitative health audit. Side-effect-free. Returns health score 0-100 across five dimensions.",
        "parameters": {},
    },
    "engram_history": {
        "description": "Browse edit history and diagnostic snapshots. Side-effect-free.",
        "parameters": {
            "mode": {"type": "string", "description": "One of: edits, diagnostics. Default: edits.", "enum": ["edits", "diagnostics"]},
            "node_id": {"type": "string", "description": "Filter edits to a specific node (edits mode only)."},
            "action": {"type": "string", "description": "Filter by action type: created, superseded, retracted, resolved, tainted, stale_flagged, reopened."},
            "since": {"type": "string", "description": "ISO timestamp — return only entries after this time."},
            "limit": {"type": "integer", "description": "Max entries (default 50, max 200)."},
        },
    },
    "engram_list_focused": {
        "description": "List the set of nodes currently in focus. Use this to maintain context across sessions.",
        "parameters": {},
    },
    "engram_list": {
        "description": "List nodes in compact triage format. No neighbor data, no memory refresh.",
        "parameters": {
            "node_type": {"type": "string", "description": "Filter by node type (e.g. 'question', 'observation_factual')."},
            "status": {"type": "string", "description": "Filter by status (e.g. 'open', 'resolved', 'active')."},
            "sort_by": {"type": "string", "description": "Sort order: id, created, importance, recalls.", "enum": ["id", "created", "importance", "recalls"]},
            "limit": {"type": "integer", "description": "Max results (default 100, max 500)."},
        },
    },
    "engram_stats": {
        "description": "Get graph statistics: node/edge counts, open questions, open predictions, weakest nodes, memory tier info.",
        "parameters": {},
    },
    "engram_nap": {
        "description": "Persist context to ENGRAM without advancing the turn counter. Use BEFORE compaction or at end of work-burst to save knowledge; arms a nap_checkpoint feeling-report nudge. Forgetting stays paused.",
        "parameters": {
            "message": {"type": "string", "description": "Summary of what was learned/changed in this work-burst.", "required": True},
        },
    },
    "engram_advance_turn": {
        "description": "End-of-day session checkpoint: advances the global turn counter (drives forgetting) and logs the session summary. IRREVERSIBLE — turn advances cannot be undone without direct DB intervention. Heartbeats and dispatchers must use engram_nap instead.",
        "parameters": {
            "message": {"type": "string", "description": "Summary of what was learned/changed across the cohort being closed by this turn advance.", "required": True},
        },
    },
}

# Tools that only read from the graph (no mutations)
READ_ONLY_TOOLS = {
    "engram_query", "engram_get_subgraph", "engram_stats", "engram_reflect", "engram_check_similar",
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
            os.environ["ENGRAM_DATA_DIR"] = resolved_dir

        # Import fresh — needed if we're running multiple clients
        # with different db_dirs in the same process
        if "server" in sys.modules:
            del sys.modules["server"]
        import server as _server

        # Redirect ALL module-level paths via the centralized function.
        # This guarantees every path constant (including any newly added ones)
        # points to the sandbox directory. Manual patching is fragile — ls_0004
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
        """Return the SKILL.md + RESPONSE_STYLE.md content for use as a system prompt."""
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
        data_dir = self._server.DATA_DIR
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

        # Test direct calls
        r = client.call("engram_add_evidence", {"url": "https://reuters.com/test", "title": "Test Article"})
        print(f"Add evidence: {r['status']} → {r['evidence_id']}")

        r = client.call("engram_add_observation", {
            "payload_json": json.dumps({
                "evidence_id": "ev_0001",
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
