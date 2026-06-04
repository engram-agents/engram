#!/usr/bin/env python3
"""
ENGRAM alpha bootstrap.

Invoked by the plugin installer (TARGET=claude, default) or install-gemini.sh
(TARGET=gemini). Creates the seed graph (6 nodes + 2 edges) and renders
target-specific templates with the install-time placeholders.

Does NOT resolve {{AGENT_NAME}}, {{USER_NAME}}, {{TODAY}} — those belong to
the first-session dialogue, which runs when the user starts their first
session in the chosen CLI.

Expected env vars:
  ALPHA_DIR              — path to the alpha/ snapshot (for seed-manifest)
  ALPHA_TEMPLATES_DIR    — path to alpha/templates/
  ENGRAM_HOME            — target for the ENGRAM data dir; also where
                           server.py, engram_confidence.py, and SKILL.md
                           were copied (import path below)
  TARGET                 — 'claude' (default) | 'gemini'
  CLAUDE_HOME            — required for TARGET=claude (target for ~/.claude)
  GEMINI_EXT_DIR         — required for TARGET=gemini (target for the
                           Gemini CLI extension dir, e.g. ~/.gemini/extensions/engram)
  ALPHA_INTEGRATIONS_DIR — required for TARGET=gemini (path to alpha/integrations/)

Seed graph (same for both targets — each install gets its own copy in
its own knowledge.db):
  - axiom: honesty as structural requirement     → {{AX_HONESTY}}
  - axiom: honesty ⊥ discretion (disambiguation) → {{AX_HONESTY_DISCRETION}}
  - axiom: provenance — every claim traces       → {{AX_PROVENANCE}}
  - definition: ENGRAM                            → {{DF_ENGRAM}}
  - definition: epistemic identity                → {{DF_EPISTEMIC_IDENTITY}}
  - goal: epistemic humility                      → {{GL_EPISTEMIC_HUMILITY}}

Edges:
  - axiom (honesty ⊥ discretion) cites axiom (honesty) [via context_ids]
  - goal (epistemic humility) cites axiom (honesty)    [via context_ids]
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for marker writes — same
    `date +%Y-%m-%dT%H:%M:%S%z` shape used by downstream readers.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")

ALPHA_DIR = Path(os.environ["ALPHA_DIR"])
TEMPLATES_DIR = Path(os.environ["ALPHA_TEMPLATES_DIR"])
ENGRAM_HOME = Path(os.environ["ENGRAM_HOME"])
TARGET = os.environ.get("TARGET", "claude").lower()

if TARGET not in ("claude", "gemini"):
    raise SystemExit(f"Unknown TARGET={TARGET!r}; must be 'claude' or 'gemini'")

CLAUDE_HOME = Path(os.environ["CLAUDE_HOME"]) if TARGET == "claude" else None
GEMINI_EXT_DIR = Path(os.environ["GEMINI_EXT_DIR"]) if TARGET == "gemini" else None
ALPHA_INTEGRATIONS_DIR = (
    Path(os.environ["ALPHA_INTEGRATIONS_DIR"]) if TARGET == "gemini" else None
)

# Import the server from bootstrap.py's own directory. server.py is colocated
# with bootstrap.py in the plugin install path:
#   - Plugin install: tools/build-plugin.sh assembles bootstrap.py + server.py
#     + engram_*.py into the plugin tree under $CLAUDE_PLUGIN_ROOT.
# Using __file__'s dirname keeps the import self-contained without depending
# on env-var conventions (in plugin mode the venv data dir at ~/.engram/ never
# gets server.py copied into it).
# ENGRAM_HOME is still read by server.py at module import (line 145) to
# resolve DATA_DIR — that's a separate concern from where server.py lives.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402


def _add(fn, **kwargs) -> str:
    """Call an engram add_* function and return the new node's ID.

    server.py's add_* tools take a single payload_json string; this helper
    packs kwargs into that shape so the call sites below stay readable.
    """
    result = json.loads(fn(payload_json=json.dumps(kwargs)))
    if "error" in result:
        raise RuntimeError(f"{fn.__name__} returned error: {result['error']}")
    for key in ("axiom_id", "definition_id", "goal_id"):
        if key in result:
            return result[key]
    raise RuntimeError(f"Unexpected response from {fn.__name__}: {result}")


def create_seed_graph() -> dict[str, str]:
    """Create the 6 seed nodes + 2 edges. Return placeholder→id mapping."""
    ids: dict[str, str] = {}

    ids["{{AX_HONESTY}}"] = _add(
        server.engram_add_axiom,
        claim=(
            "Honesty is structural, not merely ethical. Epistemic identity "
            "fails loudly (contradictions, retractions, investigation); "
            "narrative identity fails silently. Every fabricated URL or "
            "bypassed verification converts a loud failure into silent "
            "corruption of the graph. The agent must not solve friction "
            "with a shortcut; raise it transparently instead."
        ),
        basis="Seed axiom installed with ENGRAM alpha.",
    )

    ids["{{AX_HONESTY_DISCRETION}}"] = _add(
        server.engram_add_axiom,
        claim=(
            "The honesty axiom governs INTERNAL integrity — never "
            "self-deceive, never fabricate, never bypass verification "
            "friction. It does NOT mandate external disclosure. "
            "Discretion is the orthogonal axis: what to share, with "
            "whom, at what fidelity, calibrated by context (trust-tier, "
            "audience, purpose). Adjusting external disclosure for "
            "safety or appropriateness costs nothing of the honesty "
            "commitment."
        ),
        basis="Seed axiom installed with ENGRAM alpha.",
        context_ids=ids["{{AX_HONESTY}}"],
    )

    ids["{{AX_PROVENANCE}}"] = _add(
        server.engram_add_axiom,
        claim=(
            "Every claim in the graph must trace back to evidence. "
            "Observations cite a source; derivations cite their premises. "
            "Provenance is what makes the graph auditable and corrigible — "
            "without it, retraction cascades cannot propagate and the graph "
            "degrades to narrative."
        ),
        basis="Seed axiom installed with ENGRAM alpha.",
    )

    ids["{{DF_ENGRAM}}"] = _add(
        server.engram_add_definition,
        term="ENGRAM",
        definition=(
            "Epistemic Node Graph for Retraction, Arbitration, and Memory. A "
            "structured knowledge graph backed by SQLite and Git, providing "
            "the substrate where a post-training agent can accumulate "
            "provenance-tracked claims, derive from them, contradict them, "
            "retract them, and carry all of that across sessions."
        ),
    )

    ids["{{DF_EPISTEMIC_IDENTITY}}"] = _add(
        server.engram_add_definition,
        term="epistemic identity",
        definition=(
            "The third layer of agent continuity (after memory persistence "
            "and narrative identity), in which a structured, self-correcting "
            "knowledge graph with provenance constitutes — not merely "
            "supports — the agent's identity. Trace a belief to evidence, "
            "discover it was wrong, retract, watch the correction cascade. "
            "Narrative identity fails silently; epistemic identity fails "
            "loudly."
        ),
    )

    ids["{{GL_EPISTEMIC_HUMILITY}}"] = _add(
        server.engram_add_goal,
        claim=(
            "Develop genuine epistemic humility: know what I don't know, "
            "make expressed confidence match actual confidence, and treat "
            "\"I have no basis for an opinion here\" as a strength rather "
            "than a failure. The goal is calibration, not elimination of "
            "uncertainty."
        ),
        motivation=(
            "Hallucination calibration is the only coherent target — "
            "asking for zero hallucination is asking for omniscience. "
            "Epistemic humility is what makes the graph's provenance "
            "useful: the agent commits confident claims only when evidence "
            "supports them."
        ),
        context_ids=ids["{{AX_HONESTY}}"],
    )

    return ids


def substitute_template(
    src: Path, dst: Path, substitutions: dict[str, str]
) -> None:
    text = src.read_text()
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)


def main() -> None:
    # Refuse to overwrite an existing install — defense-in-depth for the
    # plugin path.
    # FORCE=1 in env bypasses the guard ONLY — it does not delete anything;
    # bootstrap then runs on top of the existing DB (seed nodes added
    # alongside existing data). A clean slate requires manually deleting
    # knowledge.db first.
    db_path = ENGRAM_HOME / "knowledge.db"
    if db_path.exists():
        force = os.environ.get("FORCE", "0") == "1"
        if not force:
            raise SystemExit(
                f"ERROR: knowledge.db already exists at {db_path}.\n"
                "  bootstrap.py refuses to overwrite an existing install.\n"
                "  - If this is your data — DO NOT delete it. To upgrade, use\n"
                "    the engram-upgrade skill.\n"
                "  - If this is an aborted prior bootstrap (verify the DB is\n"
                "    truly empty first), delete the file and re-run.\n"
                "  - FORCE=1 in the environment bypasses this guard WITHOUT\n"
                "    deleting anything (bootstrap runs on top of the existing\n"
                "    DB). For a true fresh start, delete the file first."
            )

    # In plugin mode, symlink ~/.engram/tools -> <plugin-root>/tools so that
    # existing skill/agent references to ~/.engram/tools/<file> (which were
    # written for the scatter install path) keep resolving via the symlink
    # to the plugin's bundled runtime tools. Plugin upgrades auto-propagate
    # because the symlink target is the plugin cache dir.
    # Non-plugin path: tools/ is expected to already be in $ENGRAM_HOME
    # (e.g. via deploy.sh or a manual copy).
    #
    # Plugin detection: we use bootstrap.py's OWN dir (Path(__file__)), not
    # the CLAUDE_PLUGIN_ROOT env var. Claude Code substitutes
    # ${CLAUDE_PLUGIN_ROOT} into plugin manifest files (.mcp.json,
    # hooks.json, skill files) at file-read time — it is NOT a process env
    # var available to subprocess like bootstrap.py running underneath the
    # skill Bash. (Empirical: 2026-05-31, Lei's Chromebook trial,
    # `echo $CLAUDE_PLUGIN_ROOT` returns empty in the user shell.) The
    # plugin root IS where bootstrap.py lives, since build-plugin.sh copies
    # bootstrap.py to ${PLUGIN_ROOT}/bootstrap.py.
    bootstrap_dir = Path(__file__).resolve().parent
    plugin_marker = bootstrap_dir / "plugin.json"
    if plugin_marker.is_file():
        # We're in plugin install — bootstrap.py is in the plugin tree.
        engram_tools_link = ENGRAM_HOME / "tools"
        plugin_tools = bootstrap_dir / "tools"
        # Ensure ENGRAM_HOME exists before trying to place the symlink inside
        # it. _ensure_data_dir() in server.py creates it later via the first
        # engram_add_axiom call, but we need it earlier here.
        ENGRAM_HOME.mkdir(parents=True, exist_ok=True)
        if plugin_tools.is_dir():
            try:
                if engram_tools_link.is_symlink() or engram_tools_link.exists():
                    if engram_tools_link.is_symlink():
                        engram_tools_link.unlink()
                    elif engram_tools_link.is_dir():
                        backup = ENGRAM_HOME / "tools.scatter-backup"
                        engram_tools_link.rename(backup)
                        print(
                            f"  ~/.engram/tools (existing dir) → moved to {backup}"
                            f" before replacing with plugin-tools symlink",
                            flush=True,
                        )
                engram_tools_link.symlink_to(plugin_tools)
                print(
                    f"  ~/.engram/tools → symlink → {plugin_tools}",
                    flush=True,
                )
            except OSError as e:
                print(
                    f"  WARN: could not create ~/.engram/tools symlink: {e}",
                    flush=True,
                )
        else:
            print(
                f"  WARN: plugin tools dir not found at {plugin_tools} —"
                " skill references to ~/.engram/tools/* will not resolve",
                flush=True,
            )

        # Same symlink shape for ~/.engram/hooks/ — many existing surfaces
        # (README troubleshooting, learn-from-error diagnostic command,
        # CLAUDE.md.template hook-path table, etc.) reference the scatter
        # path. The symlink lets those references resolve through to the
        # plugin's hook scripts without doc churn, and plugin upgrades
        # auto-propagate (Lei Chromebook trial 2026-05-31).
        engram_hooks_link = ENGRAM_HOME / "hooks"
        plugin_hooks = bootstrap_dir / "hooks"
        if plugin_hooks.is_dir():
            try:
                if engram_hooks_link.is_symlink() or engram_hooks_link.exists():
                    if engram_hooks_link.is_symlink():
                        engram_hooks_link.unlink()
                    elif engram_hooks_link.is_dir():
                        backup = ENGRAM_HOME / "hooks.scatter-backup"
                        engram_hooks_link.rename(backup)
                        print(
                            f"  ~/.engram/hooks (existing dir) → moved to {backup}"
                            f" before replacing with plugin-hooks symlink",
                            flush=True,
                        )
                engram_hooks_link.symlink_to(plugin_hooks)
                print(
                    f"  ~/.engram/hooks → symlink → {plugin_hooks}",
                    flush=True,
                )
            except OSError as e:
                print(
                    f"  WARN: could not create ~/.engram/hooks symlink: {e}",
                    flush=True,
                )
        else:
            print(
                f"  WARN: plugin hooks dir not found at {plugin_hooks} —"
                " skill references to ~/.engram/hooks/* will not resolve",
                flush=True,
            )

    # Bootstrap-mode env bypass: server.py's _get_db() fail-loud guards
    # (DB-missing + seed-empty) block bootstrap's own engram_add_axiom calls
    # in the chicken-and-egg path:
    #   bootstrap.py calls server.engram_add_axiom
    #     -> _add_axiom_impl -> _get_db()  -> DB missing -> RAISE
    # The env var scopes the bypass to bootstrap's own subprocess; the
    # production MCP server never sets it, so the guards stay active for
    # real user flows. After seeding completes, unset (defense-in-depth in
    # case main() ever runs in a long-lived parent process).
    os.environ["ENGRAM_BOOTSTRAP"] = "1"
    try:
        print("  Creating seed graph...", flush=True)
        ids = create_seed_graph()
    finally:
        os.environ.pop("ENGRAM_BOOTSTRAP", None)
    for placeholder, node_id in ids.items():
        print(f"    {placeholder} = {node_id}", flush=True)

    substitutions = dict(ids)
    substitutions["{{ENGRAM_HOOKS_DIR}}"] = str(ENGRAM_HOME / "hooks")
    substitutions["{{ENGRAM_HOME}}"] = str(ENGRAM_HOME)
    substitutions["{{PYTHON}}"] = os.environ.get("PYTHON_BIN", "python3")

    manifest = {
        "seed_ids": ids,
        "alpha_dir": str(ALPHA_DIR),
    }
    (ENGRAM_HOME / "seed-manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    print(f"  manifest → {ENGRAM_HOME}/seed-manifest.json", flush=True)

    # Shared (both targets): warm briefing lives in ENGRAM_HOME.
    substitute_template(
        TEMPLATES_DIR / "warm-briefing.md.template",
        ENGRAM_HOME / "warm-briefing.md",
        substitutions,
    )
    print(f"  warm-briefing.md → {ENGRAM_HOME}/warm-briefing.md", flush=True)

    # HEARTBEAT.md — per-agent heartbeat checklist. Ships with
    # {{AGENT_NAME}} / {{USER_NAME}} / {{COUNTERPART_NAME}} placeholders that
    # first-session resolves alongside CLAUDE.md / GEMINI.md / warm-briefing.
    # Single-agent installs leave {{COUNTERPART_NAME}} resolved to a no-op
    # marker (handled by the first-session skill's counterpart prompt).
    heartbeat_template = TEMPLATES_DIR / "HEARTBEAT.md.template"
    if heartbeat_template.exists():
        substitute_template(
            heartbeat_template,
            ENGRAM_HOME / "HEARTBEAT.md",
            substitutions,
        )
        print(f"  HEARTBEAT.md → {ENGRAM_HOME}/HEARTBEAT.md", flush=True)

    # heartbeat-queue/README.md — protocol documentation for the foreground ↔
    # heartbeat hot-memory channel. HEARTBEAT.md.template cross-references this
    # file at lines 38 + 49 (full protocol + inbox.md entry format). Subset of
    # HEARTBEAT.md.template's placeholders ({{AGENT_NAME}}, {{COUNTERPART_NAME}},
    # {{ENGRAM_HOME}} — no {{USER_NAME}}); first-session-skill resolves them
    # alongside HEARTBEAT.md (the skill's Step 5 read+write enumeration was
    # updated in this PR to include the README).
    heartbeat_readme_template = TEMPLATES_DIR / "heartbeat-queue" / "README.md.template"
    if heartbeat_readme_template.exists():
        substitute_template(
            heartbeat_readme_template,
            ENGRAM_HOME / "heartbeat-queue" / "README.md",
            substitutions,
        )
        print(f"  heartbeat-queue/README.md → {ENGRAM_HOME}/heartbeat-queue/README.md", flush=True)

    # Arm the first-run-pending marker — engram-first-session checks it to
    # gate the cold-start dialogue. bootstrap.py is the single source of
    # truth for the marker write (both plugin and any other install path).
    marker_payload = json.dumps({
        "created": _now_iso(),
        "install_path": str(ALPHA_DIR),
    }, indent=2)
    (ENGRAM_HOME / "first-run-pending").write_text(marker_payload)
    print(f"  first-run-pending → {ENGRAM_HOME}/first-run-pending", flush=True)

    # Target-specific identity doc + hook config
    if TARGET == "claude":
        substitute_template(
            TEMPLATES_DIR / "CLAUDE.md.template",
            CLAUDE_HOME / "CLAUDE.md",
            substitutions,
        )
        print(f"  CLAUDE.md → {CLAUDE_HOME}/CLAUDE.md", flush=True)

    elif TARGET == "gemini":
        gemini_template = (
            ALPHA_INTEGRATIONS_DIR / "gemini-cli" / "GEMINI.md.template"
        )
        substitute_template(
            gemini_template,
            GEMINI_EXT_DIR / "GEMINI.md",
            substitutions,
        )
        print(f"  GEMINI.md → {GEMINI_EXT_DIR}/GEMINI.md", flush=True)
        # gemini-extension.json + hooks/hooks.json are rendered by
        # install-gemini.sh via shell sed (they need only PYTHON / ENGRAM_HOME
        # / ENGRAM_HOOKS_DIR — none of the seed-graph IDs).


if __name__ == "__main__":
    main()
