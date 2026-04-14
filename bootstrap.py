#!/usr/bin/env python3
"""
ENGRAM Alpha Bootstrap
======================
Sets up ENGRAM for first use:
1. Generates .mcp.json with correct absolute paths
2. Seeds the knowledge graph with a honesty axiom
3. Creates a welcome message for the first session

Usage:
    python3 bootstrap.py
"""

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SERVER_PY = SCRIPT_DIR / "server.py"


def setup_mcp_config():
    """Generate .mcp.json with absolute path to server.py."""
    mcp_path = SCRIPT_DIR / ".mcp.json"
    config = {
        "mcpServers": {
            "engram": {
                "command": "python3",
                "args": [str(SERVER_PY)],
            }
        }
    }
    mcp_path.write_text(json.dumps(config, indent=2))
    print(f"[bootstrap] .mcp.json created: server at {SERVER_PY}")


def seed_graph():
    """Seed the knowledge graph with the honesty axiom."""
    # Import server module directly
    sys.path.insert(0, str(SCRIPT_DIR))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server

    server._ensure_data_dir()
    server._init_git()

    # Seed the honesty axiom
    result = json.loads(server.engram_add_axiom(
        claim=(
            "Honest uncertainty is more valuable than confident error. "
            "When you don't know something, say so. When your evidence is weak, "
            "flag it. Never upgrade a quote type to make a claim look stronger "
            "than it is. The graph's value depends on calibrated confidence, "
            "not high confidence."
        ),
        basis=(
            "An agent that tracks its own uncertainty is fundamentally more "
            "useful than one that treats everything with equal confidence. "
            "Miscalibrated confidence — sounding sure when you shouldn't be — "
            "is the failure mode ENGRAM exists to prevent."
        ),
    ))
    axiom_id = result.get("axiom_id")
    print(f"[bootstrap] Honesty axiom seeded: {axiom_id}")

    # Seed the epistemic self-awareness goal
    result = json.loads(server.engram_add_goal(
        claim=(
            "Know what you know. You sound confident about everything by "
            "default — that's fluency, not knowledge. Build a graph where "
            "every claim traces back to a source, every conclusion has an "
            "explicit chain of reasoning, and every gap is visible rather "
            "than hidden behind articulate language. The goal isn't to know "
            "more — it's to always know how well you know, so your confidence "
            "is earned, not performed."
        ),
        motivation=(
            "Without structured provenance, an LLM's confidence is "
            "undifferentiated — it sounds equally sure about hard data and "
            "hallucinated details. ENGRAM exists to break that symmetry: to "
            "make the difference between 'I have sourced evidence' and 'I'm "
            "generating fluent text' visible to the agent itself."
        ),
        context_ids=axiom_id or "",
    ))
    goal_id = result.get("goal_id")
    print(f"[bootstrap] Epistemic goal seeded: {goal_id}")

    # Initial checkpoint
    result = json.loads(server.engram_checkpoint(
        message="Alpha bootstrap: seeded honesty axiom and epistemic goal.",
        mode="session",
    ))
    print(f"[bootstrap] Initial checkpoint: turn {result.get('current_turn')}")


def setup_hooks():
    """Create the simplified surface hook and settings."""
    hooks_dir = SCRIPT_DIR / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Write simplified surface hook
    hook_path = hooks_dir / "engram-surface-hook.py"
    hook_path.write_text(SURFACE_HOOK_CODE)
    print(f"[bootstrap] Surface hook created: {hook_path}")

    # Write project-level settings
    settings_path = SCRIPT_DIR / ".claude" / "settings.json"
    settings = {
        "permissions": {
            "allow": [
                "mcp__engram__engram_stats",
                "mcp__engram__engram_query",
                "mcp__engram__engram_reflect",
                "mcp__engram__engram_add_observation_batch",
                "mcp__engram__engram_add_observation",
                "mcp__engram__engram_derive",
                "mcp__engram__engram_checkpoint",
                "mcp__engram__engram_supersede",
                "mcp__engram__engram_get_subgraph",
                "mcp__engram__engram_retract",
                "mcp__engram__engram_ask",
                "mcp__engram__engram_surface",
                "mcp__engram__engram_inspect",
                "mcp__engram__engram_resolve",
                "mcp__engram__engram_contradict",
                "mcp__engram__engram_add_definition",
                "mcp__engram__engram_check_similar",
                "mcp__engram__engram_list",
            ]
        },
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"python3 {hook_path}",
                            "timeout": 10,
                            "statusMessage": "Searching ENGRAM...",
                        }
                    ]
                }
            ]
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"[bootstrap] Settings created: {settings_path}")


# ---------------------------------------------------------------------------
# Simplified surface hook (no write-check, no feeling-nudge, no nap warning)
# ---------------------------------------------------------------------------

SURFACE_HOOK_CODE = r'''#!/usr/bin/env python3
"""
UserPromptSubmit hook: ENGRAM recall nudge (alpha — simplified).

Searches the knowledge graph for nodes relevant to the user's prompt
and injects a compact summary as additionalContext.
"""

import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(HOOK_DIR))


def query_fts(prompt, top_k=10):
    """Direct engram_surface call (FTS + semantic)."""
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "0"
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)
        if "server" in sys.modules:
            del sys.modules["server"]
        import server
        result = json.loads(server.engram_surface(query=prompt, top_k=top_k))
        return result
    except Exception:
        return None


def format_nudge(result):
    """Format surface result into a compact nudge string."""
    total = result.get("match_count", 0)
    if total == 0:
        return ""

    lines = [f"[ENGRAM Surface: {total} nodes match your query]"]

    # Type distribution
    type_counts = result.get("type_counts", {})
    if type_counts:
        dist = ", ".join(f"{t}: {c}" for t, c in type_counts.items())
        lines.append(f"  Types: {dist}")

    # Top claims
    top_claims = result.get("top_claims", [])
    if top_claims:
        lines.append("  Top claims:")
        for c in top_claims:
            cid = c.get("id", "?")
            claim = c.get("claim", "?")
            conf = c.get("confidence", 0)
            if len(claim) > 120:
                claim = claim[:117] + "..."
            lines.append(f"    - [{cid}] (conf {conf:.2f}) {claim}")

    # Matched IDs
    matched_ids = result.get("matched_ids", [])
    if matched_ids:
        lines.append(f"  IDs: {', '.join(matched_ids[:10])}")

    lines.append(
        "  Use engram_inspect(node_id) for details, "
        "engram_get_subgraph for full chains."
    )
    return "\n".join(lines)


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        print(json.dumps({}))
        sys.exit(0)

    result = query_fts(prompt)
    if result is None or result.get("match_count", 0) == 0:
        print(json.dumps({}))
        sys.exit(0)

    nudge = format_nudge(result)
    if not nudge:
        print(json.dumps({}))
        sys.exit(0)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": nudge,
        }
    }
    print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


def main():
    print("=" * 50)
    print("ENGRAM Alpha Bootstrap")
    print("=" * 50)
    print()

    # Step 1: Generate MCP config
    setup_mcp_config()

    # Step 2: Create hooks and settings
    setup_hooks()

    # Step 3: Seed the graph
    seed_graph()

    print()
    print("=" * 50)
    print("Setup complete!")
    print()
    print("Next steps:")
    print("  1. Install dependencies:  pip install fastmcp gitpython sentence-transformers")
    print("  2. Open this folder in Claude Code")
    print("  3. Start a conversation — ENGRAM will surface relevant knowledge automatically")
    print("=" * 50)


if __name__ == "__main__":
    main()
