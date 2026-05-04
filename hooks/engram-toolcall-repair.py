#!/usr/bin/env python3
"""
PreToolUse hook for ENGRAM tools: detects and repairs the antml-prefix
swallow corruption pattern in parsed tool parameters.

THE BUG THIS REPAIRS
====================
When the agent emits a tool call in raw XML and forgets the `antml:`
namespace prefix on a parameter's closing tag, the harness parser keeps
reading past the bare close until it finds the next properly-namespaced
tag. Everything in between — including a bare opening tag for the next
parameter and its content — gets swallowed into the previous parameter's
value.

Example: agent intends to send

  quoted_text="Q"
  interpretation="I"
  claim="C"
  quote_type="QT"

But emits a bare close on interpretation and a bare open on claim. The
parser sees the namespaced open of quote_type as the first valid tag
after interpretation's open, so interpretation's value becomes:

  "I</interpretation>\n<parameter name=\"claim\">C</parameter>"

and the dict the tool receives has no `claim` key at all. Pydantic
reports "Missing required argument [claim]" — a confusing error because
the agent did write the value; it just got glued onto the wrong field.

THE REPAIR
==========
The lost content is fully recoverable from the corrupted value. We
detect the pattern `</X>...<parameter name="Y">CONTENT(</parameter>|$)`
inside any string parameter, split the value at the bare close, restore
the original parameter, and put the recovered CONTENT into parameter Y.

A repair entry is appended to ~/.engram/toolcall-repairs.jsonl and an
additionalContext string is emitted so the model sees the repair
directly in the tool result (rather than only via a delayed marker).
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))
REPAIR_LOG = Path(
    os.environ.get(
        "ENGRAM_REPAIR_LOG",
        str(Path(ENGRAM_HOME) / "toolcall-repairs.jsonl"),
    )
).expanduser()
REPAIR_MARKER = Path(
    os.environ.get(
        "ENGRAM_REPAIR_MARKER",
        str(Path(ENGRAM_HOME) / "toolcall-repair-pending.json"),
    )
).expanduser()

# Tools we protect. Value is the set of legitimate parameter names for
# that tool — used as a sanity check when deciding whether a recovered
# parameter name is real (rejects garbage from false-positive matches).
PROTECTED_TOOLS = {
    "mcp__engram__engram_add_observation": {
        "quoted_text", "interpretation", "claim", "quote_type",
        "url", "title", "domain", "source_date", "evidence_id",
        "is_predictive", "predicted_event", "resolution_timeframe",
        "source_class", "content_hash", "git_sha",
    },
    "mcp__engram__engram_add_observation_batch": {
        "observations_json", "url", "title", "domain", "source_date",
        "evidence_id", "content_hash", "git_sha",
    },
    "mcp__engram__engram_nap": {
        "message",
    },
    "mcp__engram__engram_advance_turn": {
        "message",
    },
    "mcp__engram__engram_add_axiom": {
        "claim", "basis", "context_ids",
    },
    "mcp__engram__engram_add_definition": {
        "term", "definition", "context_ids",
    },
    "mcp__engram__engram_add_conjecture": {
        "claim", "basis", "initial_confidence", "context_ids",
    },
    "mcp__engram__engram_add_goal": {
        "claim", "motivation", "context_ids",
    },
    "mcp__engram__engram_add_person": {
        "name", "role", "description", "aliases", "context_ids", "is_self",
    },
    "mcp__engram__engram_add_cornerstone": {
        "tag", "title", "new_frame", "prior_frame",
        "triggering_experience", "supporting_ids",
    },
    "mcp__engram__engram_outgrow_cornerstone": {
        "old_cornerstone_id", "new_new_frame", "new_triggering_experience",
        "new_supporting_ids", "new_title",
    },
    "mcp__engram__engram_link_about": {
        "node_id", "person_id",
    },
    "mcp__engram__engram_goal_tension": {
        "goal_id_a", "goal_id_b", "description", "analysis",
    },
    "mcp__engram__engram_add_lesson": {
        "claim", "incident_ids", "scaffolding_nudge", "logical_chain",
        "reasoning_type", "context_ids",
    },
    "mcp__engram__engram_add_task": {
        "description", "goal_id", "implements_ids", "parent_task_id", "scope",
    },
    "mcp__engram__engram_update_task": {
        "task_id", "new_status", "note",
    },
    "mcp__engram__engram_report_feeling": {
        "reported_state", "trigger", "categorical_tag",
        "intensity_hint", "context_ids",
    },
    "mcp__engram__engram_derive": {
        "claim", "supporting_ids", "logical_chain", "reasoning_type",
        "derivation_mode", "context_ids",
    },
    "mcp__engram__engram_contradict": {
        "node_id_a", "node_id_b", "description", "root_cause",
    },
    "mcp__engram__engram_ask": {
        "question", "context_ids", "category", "lacks",
    },
    "mcp__engram__engram_resolve": {
        "target_id", "claim", "supporting_ids", "logical_chain",
        "prediction_outcome", "reasoning_type", "derivation_mode",
    },
    "mcp__engram__engram_supersede": {
        "old_node_id", "new_claim", "logical_chain", "additional_support_ids",
    },
    "mcp__engram__engram_retract": {
        "node_id", "error_type", "reason", "replacement_json",
    },
    "mcp__engram__engram_focus": {
        "node_ids", "reason",
    },
    "mcp__engram__engram_unfocus": {
        "node_ids",
    },
}

# Detects: </CLOSED>...<parameter name="REOPEN">RECOVERED(</parameter>|end-of-string)
# DOTALL so `.` matches newlines. Non-greedy `.*?` to capture minimal recovered
# content. Trailing `(?:</parameter>|\Z)` handles both "reopen's close was also
# bare and got swallowed" and "reopen's close was at end of value" cases.
SWALLOW_RE = re.compile(
    r'</(?P<closed>[a-zA-Z_][\w]*)>\s*'
    r'<parameter\s+name=["\'](?P<reopen>[a-zA-Z_][\w]*)["\']>'
    r'(?P<recovered>.*?)'
    r'(?:</parameter>|\Z)',
    re.DOTALL,
)

MAX_REPAIR_PASSES = 5  # safety limit for recursive multi-level damage


def attempt_repair(tool_name: str, tool_input: dict) -> tuple[dict, list[str]]:
    """Try to repair swallow-pattern corruption in tool_input.

    Returns (repaired_input, list_of_repair_descriptions). If no repair
    was needed or possible, returns (dict(tool_input), []).
    """
    if tool_name not in PROTECTED_TOOLS:
        return dict(tool_input), []

    expected = PROTECTED_TOOLS[tool_name]
    repaired = dict(tool_input)
    repairs = []

    for _pass in range(MAX_REPAIR_PASSES):
        made_change = False
        for pname, pvalue in list(repaired.items()):
            if not isinstance(pvalue, str):
                continue

            m = SWALLOW_RE.search(pvalue)
            if not m:
                continue

            closed = m.group("closed")
            reopen = m.group("reopen")
            recovered = m.group("recovered")

            # Sanity checks. Skip if any fails — better to leave the call
            # alone than corrupt it further (design point 3: silent
            # fall-through on unrecoverable patterns, let server reject).
            if closed != pname and closed != "parameter":
                # Bare close tag name must either match the parameter
                # holding it (e.g., </message> inside message field) or
                # be the literal </parameter> close (variant observed
                # 2026-04-18 where the raw close tag leaked unchanged
                # into the value). Other mismatches could be legitimate
                # XML in content — refuse to repair.
                continue
            if reopen not in expected:
                # Recovered name isn't a valid parameter for this tool.
                continue
            if repaired.get(reopen):
                # Target parameter already has a value; don't overwrite.
                continue

            # Apply the repair: trim the corrupted tail off the original
            # parameter and put the recovered content into its true home.
            clean_value = pvalue[:m.start()].rstrip()
            repaired[pname] = clean_value
            repaired[reopen] = recovered.strip()
            repairs.append(
                f"split '{pname}' at bare </{closed}>, "
                f"recovered '{reopen}' ({len(recovered)} chars)"
            )
            made_change = True
            # Restart the inner loop with the updated dict
            break

        if not made_change:
            break

    return repaired, repairs


def log_repair(tool_name: str, repairs: list[str],
                original: dict, repaired: dict) -> None:
    """Append a repair event to the jsonl log file."""
    REPAIR_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "tool_name": tool_name,
        "repairs": repairs,
        "params_before": sorted(original.keys()),
        "params_after": sorted(repaired.keys()),
    }
    with open(REPAIR_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def write_marker(tool_name: str, repairs: list[str]) -> None:
    """Write a marker file the UserPromptSubmit hook surfaces next turn."""
    REPAIR_MARKER.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "pending": True,
        "tool_name": tool_name,
        "repairs": repairs,
        "timestamp": datetime.now().isoformat(),
    }
    with open(REPAIR_MARKER, "w") as f:
        json.dump(marker, f)


def format_additional_context(tool_name: str, repairs: list[str]) -> str:
    """Build the additionalContext string the model sees this turn."""
    short_name = tool_name.replace("mcp__engram__", "")
    lines = [
        f"[antml-prefix repair] Your {short_name} call had "
        f"{len(repairs)} swallow-pattern corruption(s). "
        "I repaired the tool_input before invocation; the call will "
        "succeed, but fix the emit next time."
    ]
    for r in repairs:
        lines.append(f"  - {r}")
    lines.append(
        "Root cause: a parameter's closing tag was emitted without the "
        "antml: prefix, so the harness parser swallowed the next "
        "parameter's opening tag and value into the previous parameter. "
        "See ob_0216 for mechanics."
    )
    return "\n".join(lines)


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)  # never block on hook errors

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if not isinstance(tool_input, dict):
        sys.exit(0)

    repaired, repairs = attempt_repair(tool_name, tool_input)

    if not repairs:
        sys.exit(0)  # no repair needed, let the call through unchanged

    # Repair happened — record it and surface the repaired dict
    try:
        log_repair(tool_name, repairs, tool_input, repaired)
        write_marker(tool_name, repairs)
    except Exception:
        pass  # logging failures must not block the call

    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": repaired,
            "additionalContext": format_additional_context(tool_name, repairs),
        }
    }
    print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
