"""Config UI tier schema — declarative metadata for /config viz tab.

Single source of truth for what config keys are surfaced in the
viz_server config tab, which tier they belong to (A=daily / B=advanced),
what control to render, tooltip text, and restart-required semantics.

Tier C (ENGRAM protocol-level params) is explicitly absent — those params
exist in config.json but are not rendered. See ~/.engram/config.json
directly for the protocol-level surface; the README/docs explain what
each does.

The schema is decoupled from physical config storage: a key with dot
notation (e.g. `github.enabled`) describes the canonical nested location;
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
    {"key": "counterparts", "section": "identity", "tier": TIER_A, "type": "list",
     "control": "list_display", "label": "Counterparts",
     "tooltip": "Peer agents on this host. Set by agentctl.",
     "editable": False, "restart_required": False},

    # GitHub (gated section)
    {"key": "github.enabled", "section": "github", "tier": TIER_A, "type": "boolean",
     "control": "checkbox", "label": "Enable GitHub integration",
     "tooltip": "Surfaces GitHub notifications from your collaborator and peer agents.",
     "editable": True, "restart_required": False, "gates_section": True},
    {"key": "github.primary_user_login", "section": "github", "tier": TIER_A, "type": "string",
     "control": "text", "label": "Your GitHub login",
     "tooltip": "Your GitHub username — used to identify your activity in notification streams.",
     "editable": True, "restart_required": False,
     "legacy_paths": ["primary_user_github_login"],
     "depends_on": "github.enabled"},
    {"key": "github.counterparts_logins", "section": "github", "tier": TIER_A, "type": "list",
     "control": "list_edit", "label": "Counterpart GitHub logins",
     "tooltip": "GitHub usernames of peer agents on this host. You set these — agentctl manages agent names but not their GitHub handles.",
     "editable": True, "restart_required": False,
     "legacy_paths": ["counterparts_github_logins"],
     "depends_on": "github.enabled"},

    # Embedding (read-only)
    {"key": "embedding.enabled", "section": "embedding", "tier": TIER_A, "type": "boolean",
     "control": "checkbox", "label": "Embedding enabled",
     "tooltip": "Semantic recall via sentence-transformer embeddings. Read-only here — disable/enable would orphan existing nodes' embeddings.",
     "editable": False, "restart_required": False, "alert_when_false": True},
    {"key": "embedding.model", "section": "embedding", "tier": TIER_A, "type": "string",
     "control": "text", "label": "Embedding model",
     "tooltip": "Sentence-transformer model identifier. Read-only — switching models would invalidate existing embeddings.",
     "editable": False, "restart_required": False},

    # Drowsiness
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

    # Auto-sleep
    {"key": "cadence.auto_sleep_enabled", "section": "auto_sleep", "tier": TIER_A,
     "type": "boolean", "control": "checkbox",
     "label": "Enable auto-sleep",
     "tooltip": "When enabled, the SessionStart hook registers a nightly sleep cron at the configured time. Opt-in; default off. Takes effect next session.",
     "editable": True, "restart_required": True},
    {"key": "cadence.auto_sleep_time", "section": "auto_sleep", "tier": TIER_A,
     "type": "string", "control": "text",
     "label": "Auto-sleep time (HH:MM)",
     "tooltip": "Local-time 24-hour clock when the nightly sleep cycle fires (e.g. '03:00'). Format: HH:MM, 00:00–23:59. Takes effect next session.",
     "editable": True, "restart_required": True,
     "depends_on": "cadence.auto_sleep_enabled"},

    # Domain trust
    {"key": "trust_pool", "section": "domain_trust", "tier": TIER_A, "type": "list",
     "control": "list_edit", "label": "Trust pool",
     "tooltip": "Domains in the trust pool get higher source-confidence weighting. Add news, official, or academic sources you consider reliable for your work.",
     "editable": True, "restart_required": False},
    {"key": "yellow_domains", "section": "domain_trust", "tier": TIER_A, "type": "list_of_objects",
     "control": "yellow_domains_edit", "label": "Yellow domains",
     "tooltip": "Domains flagged for caution. Each entry has a domain, a reason, and an optional engram_node reference. Cited domains show as warning-level in evidence reports.",
     "editable": True, "restart_required": False},

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
]

SECTIONS: list[dict] = [
    {"id": "identity",     "label": "Identity",                "tier": TIER_A, "order": 1},
    {"id": "github",       "label": "GitHub integration",      "tier": TIER_A, "order": 2, "section_gate": "github.enabled"},
    {"id": "embedding",    "label": "Embedding",               "tier": TIER_A, "order": 3},
    {"id": "drowsiness",   "label": "Drowsiness thresholds",   "tier": TIER_A, "order": 4},
    {"id": "auto_sleep",   "label": "Auto-sleep",              "tier": TIER_A, "order": 5},
    {"id": "domain_trust", "label": "Domain trust & caution",  "tier": TIER_A, "order": 6},
    {"id": "memory",       "label": "Memory",                  "tier": TIER_B, "order": 7},
    {"id": "polarity",     "label": "Polarity-dedup (NLI)",    "tier": TIER_B, "order": 8, "section_gate": "polarity.enabled"},
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
