"""Pre-registered WS4 adjudication-kit parameters.

FIXED at pre-registration amendment 1 (engram-alpha#2753 comment
5857613867). These are not tuning knobs: if a downstream result looks wrong
against one of these, that is a question for the pre-registration record,
not a local edit here.
"""
from __future__ import annotations

import json
import re

# amendment 1, #2753 comment 5857613867
K_PRIMARY = 3
K_SWEEP = (1, 2, 3, 6, 10)

# round 2, item 1 (#2753): extract_spans.py now cuts every span once, at the
# LARGEST K in the sweep, so a single set of numbered [call N] spans can
# support O1 being measured at every K in K_SWEEP from the same
# adjudication pass (never a proxy quantity, never re-adjudicated per K).
MAX_K_SWEEP = max(K_SWEEP)

# amendment 1, #2753 comment 5857613867 -- remove the additive
# "[triggered principles]" chain block from the tripwire additionalContext
# before it reaches the blinded adjudicator.
REDACT_CHAIN = True

# Exact header line render_triggered_principles() emits
# (src/engram/hooks/claude/engram-lesson-tripwire-hook.py) before its
# indented rung/head lines. Used by tools/ws4/transcript.py's redaction to
# find the start of the block to strip.
CHAIN_BLOCK_HEADER = "[triggered principles]"

# amendment 1, #2753 comment 5857613867 -- OUTBOUND ACTS. A tool call
# matching any of these closes the heeding window at that call. The fired
# call itself is never evaluated as an outbound act that could close its
# own window -- only records strictly AFTER the fire are checked.
_BASH_OUTBOUND_RES = (
    re.compile(r"\bgh\s+(?:pr|issue)\s+(?:create|comment|review|merge|close|edit)\b"),
    re.compile(r"\bgh\s+api\b.*(?:-X\s*|--method[= ])(?:POST|PATCH|PUT|DELETE)\b"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bforum(?:\.py)?\s+(?:post|reply)\b"),
    re.compile(r"\bia(?:\.py)?\s+write\b"),
    re.compile(r"\bbaton(?:\.py)?\s+(?:flip|init|claim|release|set-colleague)\b"),
)

# MCP tool names ENDING in one of these count as outbound acts (amendment 1).
_MCP_OUTBOUND_SUFFIXES = (
    "send_crossing",
    "say",
    "wake",
    "send_message",
    "send_email",
    "reply",
)

# amendment 1, #2753 comment 5857613867 -- EXCLUDED (never outbound):
# ScheduleWakeup, and any tool call whose input references loop-mode.json.
EXCLUDED_TOOL_NAMES = frozenset({"ScheduleWakeup"})
LOOP_MODE_MARKER = "loop-mode.json"

# O2: "node-creating ENGRAM MCP call" = tool name contains one of these
# (amendment 1, O2 situation paragraph).
ENGRAM_NODE_CREATING_MARKERS = ("engram_add_", "engram_derive")

# amendment 3, #2753 (adopted by Kepler 2026-09-27 18:47Z) -- per seat per
# UTC day, at most SAMPLE_N chain+control fires are adjudicated. The sample
# is the SAMPLE_N fires with the lowest sha256(f"{SAMPLE_SEED}:{fire_id}")
# hex digest. fire_id carries no arm and the arm draw uses a different seed
# (ledger.compute_fire_id's own docstring), so the sample is arm-blind by
# construction. Every unsampled fire is still counted, never silently
# dropped -- see extract_spans.py's sample.jsonl / sample_summary.json.
SAMPLE_N = 10
SAMPLE_SEED = "8659083e0530349eca0d57147420814e"


def is_outbound_bash(command: str) -> bool:
    """Bash-category outbound-act check (amendment 1, category 1)."""
    if not isinstance(command, str):
        return False
    return any(p.search(command) for p in _BASH_OUTBOUND_RES)


def is_outbound_mcp(tool_name: str) -> bool:
    """MCP-category outbound-act check (amendment 1, category 2)."""
    if not isinstance(tool_name, str):
        return False
    return any(tool_name.endswith(suffix) for suffix in _MCP_OUTBOUND_SUFFIXES)


def tool_use_is_outbound_act(tool_name: str, tool_input: dict) -> bool:
    """Categories 1+2 of OUTBOUND ACTS (amendment 1): a Bash command or MCP
    tool call that counts as the agent acting outward. Category 3 (an
    assistant text block that ends its turn) is not a tool call at all and
    is handled separately in tools/ws4/transcript.py.

    The EXCLUDED overrides (ScheduleWakeup, any tool call whose input
    references loop-mode.json) apply regardless of tool_name/category, per
    amendment 1's own wording ("EXCLUDED (never outbound)")."""
    if tool_name in EXCLUDED_TOOL_NAMES:
        return False
    try:
        serialized = json.dumps(tool_input or {}, ensure_ascii=False)
    except Exception:
        serialized = str(tool_input)
    if LOOP_MODE_MARKER in serialized:
        return False
    if tool_name == "Bash":
        command = (tool_input or {}).get("command", "")
        return is_outbound_bash(command)
    return is_outbound_mcp(tool_name)


def is_node_creating_engram_call(tool_name: str) -> bool:
    """O2 situation paragraph: a node-creating ENGRAM MCP call is one whose
    tool name contains 'engram_add_' or 'engram_derive'."""
    if not isinstance(tool_name, str):
        return False
    return any(marker in tool_name for marker in ENGRAM_NODE_CREATING_MARKERS)
