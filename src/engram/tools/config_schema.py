"""Config UI tier schema — declarative metadata for /config viz tab.

Single source of truth for what config keys are surfaced in the
viz_server config tab, which tier they belong to (A=daily / B=advanced),
what control to render, tooltip text, and restart-required semantics.

Tier C (ENGRAM protocol-level params) is explicitly absent — those params
exist in config.json but are not rendered. See ~/.engram/config.json
directly for the protocol-level surface; the README/docs explain what
each does.

The schema is decoupled from physical config storage: a key with dot
notation (e.g. `embedding.enabled`) describes the canonical nested location;
the `legacy_paths` field lists fallback flat-keys for backward-compat
reads while migration is pending.
"""

TIER_A = "daily"
TIER_B = "advanced"

SCHEMA: list[dict] = [
    # ---- Tier A ----
    {
        "key": "primary_user",
        "section": "identity",
        "tier": TIER_A,
        "type": "string",
        "control": "text",
        "label": "Primary user",
        "tooltip": "The human collaborator this agent serves.",
        "editable": True,
        "restart_required": False,
    },
    {"key": "agent_name", "section": "identity", "tier": TIER_A, "type": "string",
     "control": "text", "label": "Agent name",
     "tooltip": "Set at install time by agentctl. Read-only here.",
     "editable": False, "restart_required": False},
    {"key": "mode", "section": "identity", "tier": TIER_A, "type": "string",
     "control": "text", "label": "Mode",
     "tooltip": "'single' or 'multi' — managed by agentctl when spawning peer agents.",
     "editable": False, "restart_required": False},
    {"key": "self_lineage", "section": "identity", "tier": TIER_A, "type": "string",
     "control": "text", "label": "Self lineage",
     "pattern": "^(|[a-z0-9_-]+:[a-z0-9._-]+)$",
     "tooltip": "This install's own training lineage as provider:family (e.g. "
                "'anthropic:opus'). Powers standpoint v3 null=self: an unmarked "
                "observation counts as your own lineage so the standpoint/F-S "
                "advisory fires on own derivations. Empty = feature dark (safe).",
     "editable": True, "restart_required": False},
    {"key": "counterparts", "section": "identity", "tier": TIER_A, "type": "list",
     "control": "list_display", "label": "Counterparts",
     "tooltip": "Peer agents on this host. Set by agentctl.",
     "editable": False, "restart_required": False},

    # Embedding (read-only)
    {"key": "embedding.enabled", "section": "embedding", "tier": TIER_B, "type": "boolean",
     "control": "checkbox", "label": "Embedding enabled",
     "tooltip": "Semantic recall via sentence-transformer embeddings. Read-only here — disable/enable would orphan existing nodes' embeddings.",
     "editable": False, "restart_required": False, "alert_when_false": True},
    {"key": "embedding.model", "section": "embedding", "tier": TIER_B, "type": "string",
     "control": "text", "label": "Embedding model",
     "tooltip": "Sentence-transformer model identifier. Read-only — switching models would invalidate existing embeddings.",
     "editable": False, "restart_required": False},

    # Drowsiness
    # Note: the legacy `drowsiness_ceiling_max` field is intentionally omitted
    # from SCHEMA. PR #324 retired it in favor of cadence.drowsiness_ceiling_tokens
    # (the typed token-count above). Don't surface the deprecated field via the
    # config UI — surfacing would confuse users about which knob is current.
    {"key": "cadence.drowsiness_ceiling_tokens", "section": "drowsiness", "tier": TIER_A,
     "type": "integer", "control": "int", "min": 50000, "max": 2000000,
     "label": "Context ceiling (tokens)",
     "tooltip": "Explicit context-window token ceiling the drowsiness meter measures fill against. Set per your session's context mode (~190000 for 200K mode, ~807000 for 1M mode). Read live by the context_tracker hook — no restart needed.",
     "editable": True, "restart_required": False},
    {"key": "cadence.drowsiness_caution_pct", "section": "drowsiness", "tier": TIER_A,
     "type": "integer", "control": "int", "min": 0, "max": 100,
     "label": "Caution threshold (%)",
     "tooltip": "Context-fill percentage where the drowsiness meter fires the caution-level nudge.",
     "editable": True, "restart_required": False},
    {"key": "cadence.drowsiness_urgent_pct", "section": "drowsiness", "tier": TIER_A,
     "type": "integer", "control": "int", "min": 0, "max": 100,
     "label": "Urgent threshold (%)",
     "tooltip": "Context-fill percentage where the drowsiness meter fires the urgent-level nudge.",
     "editable": True, "restart_required": False},

    # Engaged-state detection
    # `default` mirrors _status_derive._DEFAULT_ENGAGED_WINDOW (the value the hook
    # actually uses when the key is absent from config.json). Kept in sync by
    # test_config_schema.test_engaged_default_matches_status_derive (drift guard).
    {"key": "cadence.engaged_window_seconds", "section": "engaged", "tier": TIER_A,
     "type": "integer", "control": "int", "min": 30, "max": 3600,
     "label": "Engaged window (seconds)", "default": 360,
     "tooltip": "How long after the last human-typed prompt the agent is considered 'engaged' on the board. Default 360 (6 minutes). The stamp is written by the time-bar hook on genuine human prompts only — loop self-wakes and monitor events do not update it.",
     "editable": True, "restart_required": False},

    # Auto-sleep
    {"key": "cadence.auto_sleep_enabled", "section": "auto_sleep", "tier": TIER_A,
     "type": "boolean", "control": "checkbox",
     "label": "Enable auto-sleep",
     "tooltip": "When enabled, the SessionStart hook registers a nightly sleep cron at the configured time. Opt-in; default off. Takes effect next session.",
     "editable": True, "restart_required": True},
    {"key": "cadence.auto_sleep_time", "section": "auto_sleep", "tier": TIER_A,
     "type": "string", "control": "time_hm",
     "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$",
     "label": "Auto-sleep time (HH:MM)",
     "tooltip": "Local-time 24-hour clock when the nightly sleep cycle fires (e.g. '03:00'). Format: HH:MM, 00:00–23:59. Takes effect next session.",
     "editable": True, "restart_required": True,
     "depends_on": "cadence.auto_sleep_enabled"},

    # Domain trust
    {"key": "trust_pool", "section": "domain_trust", "tier": TIER_A, "type": "list",
     "control": "list_edit", "label": "Trust pool",
     "tooltip": "Advisory only: domains NOT in this list get a soft warning at evidence creation and appear in engram_diagnose's untrusted-domain report. Does not change confidence scores (source-based confidence calibration is planned future-work, see #1230). Leave empty to suppress all trust-pool warnings.",
     "editable": True, "restart_required": False},
    {"key": "yellow_domains", "section": "domain_trust", "tier": TIER_A, "type": "list_of_objects",
     "control": "yellow_domains_edit", "label": "Yellow domains",
     "tooltip": "Domains flagged for caution. Each entry has a domain, a reason, and an optional engram_node reference. Cited domains show as warning-level in evidence reports.",
     "editable": True, "restart_required": False},

    # Fairy delegation policies
    {
        "key": "coder_fairy_policy",
        "section": "fairy_policy",
        "control": "select",
        "label": "Coder-fairy delegation policy",
        "tooltip": (
            "How the agent decides between doing PR coding directly vs dispatching a coder-fairy. "
            "Three modes:\n"
            "  • explicit — agent ONLY uses coder-fairy when you explicitly ask.\n"
            "  • auto — agent uses judgment per task according to the "
            "`engram-auto-coder-fairy-judgement` skill "
            "(edit that skill file to fine-tune the heuristic).\n"
            "  • always — agent always uses coder-fairy for PR coding tasks "
            "(no exceptions, maximum review-convergence discipline)."
        ),
        "editable": True,
        "restart_required": False,
        "tier": TIER_A,
        "type": "string",
        "allowed_values": ["explicit", "auto", "always"],
    },
    {
        "key": "reviewer_fairy_policy",
        "section": "fairy_policy",
        "control": "select",
        "label": "Reviewer-fairy delegation policy",
        "tooltip": (
            "How the agent decides between reviewing PR work directly vs dispatching a reviewer-fairy. "
            "Three modes:\n"
            "  • explicit — agent ONLY uses reviewer-fairy when you explicitly ask.\n"
            "  • auto — agent uses judgment per task according to the "
            "`engram-auto-reviewer-fairy-judgement` skill "
            "(edit that skill file to fine-tune the heuristic).\n"
            "  • always — agent always uses reviewer-fairy for PR review tasks "
            "(no exceptions, maximum review-convergence discipline)."
        ),
        "editable": True,
        "restart_required": False,
        "tier": TIER_A,
        "type": "string",
        "allowed_values": ["explicit", "auto", "always"],
    },

    # ---- Tier B ----
    {"key": "memory.tier1_max_nodes", "section": "memory", "tier": TIER_B,
     "type": "integer", "control": "int", "min": 50, "max": 10000,
     "label": "Tier-1 max nodes",
     "tooltip": "How many nodes the agent holds in actively-searchable working memory. Higher = better recall on long sessions but more compute per query. Tune to your hardware.",
     "editable": True, "restart_required": False},
    {"key": "memory.tier2_max_nodes", "section": "memory", "tier": TIER_B,
     "type": "integer", "control": "int", "min": 100, "max": 100000,
     "label": "Tier-2 max nodes",
     "tooltip": "Background-decay cap. Nodes beyond this are subject to forgetting-curve decay. Higher = longer recall horizon, slower decay sweep.",
     "editable": True, "restart_required": False},
    {"key": "memory.decay_base", "section": "memory", "tier": TIER_B,
     "type": "float", "control": "float", "min": 1.0, "max": 1.5,
     "label": "Decay base",
     "tooltip": "Forgetting-curve exponential base (default 1.014, calibrated against agent session pacing). 1.0 = no decay; higher values = faster decay.",
     "editable": True, "restart_required": False},

    {"key": "polarity.enabled", "section": "polarity", "tier": TIER_B,
     "type": "boolean", "control": "checkbox",
     "label": "Enable polarity-dedup",
     "tooltip": "Detects contradicting observations on write using an NLI model. Requires a ~1.5GB GPU model — default off. Restart Claude Code after enabling so the model can load.",
     "editable": True, "restart_required": True, "gates_section": True},
    {"key": "polarity.model", "section": "polarity", "tier": TIER_B,
     "type": "string", "control": "text",
     "label": "NLI model",
     "tooltip": "Hugging Face model identifier. Default `dleemiller/ModernCE-large-nli` won the 2026-05-10 calibration bake-off (AUC 0.847, F1 0.889). Changing requires restart.",
     "editable": True, "restart_required": True, "depends_on": "polarity.enabled"},
    {"key": "polarity.threshold", "section": "polarity", "tier": TIER_B,
     "type": "float", "control": "float", "min": 0.0, "max": 1.0,
     "label": "Polarity threshold",
     "tooltip": "Cosine threshold for flagging a polarity contradiction (default 0.46, the peak-F1 operating point from calibration).",
     "editable": True, "restart_required": False, "depends_on": "polarity.enabled"},
    {"key": "polarity.min_similarity_for_check", "section": "polarity", "tier": TIER_B,
     "type": "float", "control": "float", "min": 0.0, "max": 1.0,
     "label": "Min similarity for NLI",
     "tooltip": "Cosine floor below which NLI is skipped (truly unrelated observations — no contradiction possible). Default 0.30.",
     "editable": True, "restart_required": False, "depends_on": "polarity.enabled"},

    # ---- Deference detector ----
    {
        "key": "deference_detector.cooldown_minutes",
        "section": "deference_detector",
        "tier": TIER_B,
        "type": "integer",
        "control": "int",
        "min": 0,
        "max": 120,
        "label": "Cooldown after user message (min)",
        "tooltip": (
            "After a real user message lands during loop mode, the deference detector "
            "suppresses for this many minutes. The phrases it detects ('Should I proceed?', "
            "'Let me know if you want me to...') are appropriate when responding to a human — "
            "this window prevents false alarms. Set to 0 to disable the cooldown. Default: 10."
        ),
        "editable": True,
        "restart_required": False,
    },
]

SECTIONS: list[dict] = [
    {"id": "identity",     "label": "Identity",                "tier": TIER_A, "order": 1},
    {"id": "embedding",    "label": "Embedding",               "tier": TIER_B, "order": 3},
    {"id": "drowsiness",   "label": "Drowsiness thresholds",   "tier": TIER_A, "order": 4},
    {"id": "engaged",      "label": "Engaged-state detection", "tier": TIER_A, "order": 5},
    {"id": "auto_sleep",   "label": "Auto-sleep",              "tier": TIER_A, "order": 6},
    {"id": "domain_trust", "label": "Domain trust & caution",  "tier": TIER_A, "order": 7},
    {"id": "fairy_policy", "label": "Fairy Delegation Policies", "tier": TIER_A, "order": 8},
    {"id": "memory",       "label": "Memory",                  "tier": TIER_B, "order": 9},
    {"id": "polarity",     "label": "Polarity-dedup (NLI)",    "tier": TIER_B, "order": 10, "section_gate": "polarity.enabled"},
    {"id": "deference_detector", "label": "Deference detector", "tier": TIER_B, "order": 11},
]


def read_config_value(config: dict, key: str, legacy_paths=None) -> tuple:
    """Get a config value by dotted key, with legacy-path fallback.

    Returns (value, source_path_used).
      - value: the resolved value, or None if not found
      - source_path_used: canonical key if found at canonical path;
        legacy path if fallback was used; None if nothing found.
    """
    obj = config
    for p in key.split("."):
        if not isinstance(obj, dict) or p not in obj:
            obj = None
            break
        obj = obj[p]
    if obj is not None:
        return obj, key
    for legacy in (legacy_paths or []):
        if legacy in config:
            return config[legacy], legacy
    return None, None


def annotate_schema(config: dict) -> dict:
    """Merge SCHEMA + SECTIONS with current config values.

    Returns a dict {"sections": [...]} where each section contains its
    rows annotated with current value, value_source, and the visual
    flags needed by the UI renderer.
    """
    rows_by_section: dict[str, list[dict]] = {}
    for row in SCHEMA:
        value, source = read_config_value(
            config, row["key"], row.get("legacy_paths")
        )
        annotated = dict(row)
        annotated["value"] = value
        annotated["value_source"] = source
        annotated["uses_legacy_path"] = source is not None and source != row["key"]
        rows_by_section.setdefault(row["section"], []).append(annotated)

    sections_out = []
    for section in sorted(SECTIONS, key=lambda s: s["order"]):
        sec = dict(section)
        sec["rows"] = rows_by_section.get(section["id"], [])
        # Resolve gate value if section_gate set
        if "section_gate" in section:
            gate_value, _ = read_config_value(config, section["section_gate"])
            sec["gate_open"] = bool(gate_value)
        else:
            sec["gate_open"] = True
        sections_out.append(sec)

    return {"sections": sections_out}
