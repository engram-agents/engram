#!/usr/bin/env python3
"""
ENGRAM alpha bootstrap.

Invoked by the plugin installer (TARGET=claude, default) or directly
(TARGET=codex). Creates the seed graph (6 nodes + 2 edges) and renders
target-specific templates with the install-time placeholders.

Does NOT resolve {{AGENT_NAME}}, {{USER_NAME}}, {{TODAY}} — those belong to
the first-session dialogue, which runs when the user starts their first
session in the chosen CLI.

Expected env vars:
  ALPHA_DIR              — path to the alpha/ snapshot (for seed-manifest)
  ALPHA_TEMPLATES_DIR    — path to alpha/templates/
  ENGRAM_HOME            — target for the ENGRAM data dir; also where
                           server.py and engram_confidence.py
                           were copied (import path below)
  TARGET                 — 'claude' (default) | 'codex'
  CLAUDE_HOME            — required for TARGET=claude (target for ~/.claude)
  CODEX_HOME             — required for TARGET=codex (target for ~/.codex)

Seed graph (same for all targets — each install gets its own copy in
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
import re
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

if TARGET not in ("claude", "codex"):
    raise SystemExit(f"Unknown TARGET={TARGET!r}; must be 'claude' or 'codex'")

CLAUDE_HOME = Path(os.environ["CLAUDE_HOME"]) if TARGET == "claude" else None
CODEX_HOME = Path(os.environ["CODEX_HOME"]) if TARGET == "codex" else None

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
from template_render import COMPACT_BREADCRUMB_LINE, fold_compact_instructions  # noqa: E402


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


def _remove_compact_section(text: str) -> str:
    """Remove the '## Compact Instructions' section from a rendered template body.

    The compact content ships separately as codex-compact-prompt.md; the
    instructions= field carries identity content only.

    Removes from '## Compact Instructions\\n' through (but not including)
    the next '## ' section header. If no compact section found, returns
    text unchanged.
    """
    start = text.find("\n## Compact Instructions\n")
    if start == -1:
        return text
    # Find the next section header after the compact section
    next_section = text.find("\n## ", start + 1)
    if next_section == -1:
        # Compact section is last — remove to end
        return text[:start]
    return text[:start] + "\n" + text[next_section + 1:]


def merge_codex_config_toml(
    config_path: Path,
    instructions_body: str,
    compact_prompt_path: str,
) -> None:
    """Write Codex identity keys into config.toml, preserving pre-existing stanzas.

    Strategy (merge-not-clobber):
    - Read the existing file if present; otherwise start from empty string.
    - Strip any pre-existing instructions=, experimental_compact_prompt_file=,
      and # model = ... lines (to replace them idempotently).
    - Prepend the new identity block (instructions, compact_prompt_file, model
      comment) to the remaining content.
    - Write the result back.

    This ensures that plugin stanzas appended by 'codex plugin add' after an
    initial bootstrap run are preserved intact on subsequent runs.
    """
    # Read existing content (may contain plugin stanzas)
    existing = config_path.read_text() if config_path.exists() else ""

    # Strip lines that are part of the identity block we're (re)writing.
    # These patterns match:
    #   instructions = """..."""  (multiline — find start/end)
    #   experimental_compact_prompt_file = "..."
    #   # model = ...
    remaining = existing

    # Remove multiline instructions = """...""" block
    instr_start = remaining.find('instructions = """')
    if instr_start != -1:
        instr_end = remaining.find('"""', instr_start + len('instructions = """'))
        if instr_end != -1:
            instr_end += 3  # include the closing """
            # Also consume any trailing newline
            if instr_end < len(remaining) and remaining[instr_end] == "\n":
                instr_end += 1
            remaining = remaining[:instr_start] + remaining[instr_end:]

    # Remove experimental_compact_prompt_file = "..." line
    remaining = re.sub(
        r'^experimental_compact_prompt_file\s*=\s*"[^"]*"\n?',
        "",
        remaining,
        flags=re.MULTILINE,
    )

    # Remove # model = ... comment line
    remaining = re.sub(
        r'^# model\s*=.*\n?',
        "",
        remaining,
        flags=re.MULTILINE,
    )

    # Build the identity block
    identity_block = (
        f'instructions = """\n{instructions_body}\n"""\n'
        f'experimental_compact_prompt_file = "{compact_prompt_path}"\n'
        f"# model = codex-1  # uncomment and set your preferred Codex model\n"
    )

    # Prepend identity block; preserve remaining plugin stanzas
    # Ensure a blank line separator if remaining has content
    if remaining.strip():
        new_content = identity_block + "\n" + remaining.lstrip("\n")
    else:
        new_content = identity_block

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_content)


def _render_codex_target(substitutions: dict[str, str]) -> None:
    """Render the three Codex identity surfaces.

    1. ~/.engram/codex-compact-prompt.md  — compact instructions (standalone)
    2. ~/.codex/config.toml               — identity keys (merge-not-clobber)
    3. ~/.codex/AGENTS.md                 — project conventions placeholder

    The instructions= body is the full template.CLAUDE.md rendered with
    seed-IDs/ENGRAM_HOME, then adapted for Codex (CLAUDE_PLUGIN_ROOT →
    CODEX_PLUGIN_ROOT; ~/.claude/CLAUDE.md self-refs → ~/.codex/config.toml;
    output-styles ref → personality/#816) with the ## Compact Instructions
    section removed (that content lives in codex-compact-prompt.md).

    {{AGENT_NAME}}, {{USER_NAME}}, {{SELF_NODE_ID}}, {{TODAY}} are intentionally
    left for the first-session skill to resolve.
    """
    assert CODEX_HOME is not None, "CODEX_HOME must be set for TARGET=codex"

    # 1. codex-compact-prompt.md — compact instructions file
    compact_body = substitutions.get("{{COMPACT_INSTRUCTIONS}}", "")
    # Apply seed-IDs and ENGRAM_HOME (leave first-session markers alone)
    compact_rendered = compact_body
    for placeholder, value in substitutions.items():
        if placeholder != "{{COMPACT_INSTRUCTIONS}}":
            compact_rendered = compact_rendered.replace(placeholder, value)
    compact_out = ENGRAM_HOME / "codex-compact-prompt.md"
    compact_out.parent.mkdir(parents=True, exist_ok=True)
    compact_out.write_text(compact_rendered)
    print(f"  codex-compact-prompt.md → {compact_out}", flush=True)

    # 2. ~/.codex/config.toml — instructions= body
    # Start from the template.CLAUDE.md, substitute seed-IDs + ENGRAM_HOME,
    # then apply Codex-specific adaptations, then remove compact section.
    claude_template_path = TEMPLATES_DIR / "template.CLAUDE.md"
    raw = claude_template_path.read_text()

    # Apply all substitutions (this folds {{COMPACT_INSTRUCTIONS}} back in,
    # giving us the full rendered text before we strip the compact section)
    rendered = raw
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

    # Remove the ## Compact Instructions section (lives in the separate file)
    rendered = _remove_compact_section(rendered)

    # Codex-specific textual adaptations
    rendered = rendered.replace("CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT")
    rendered = rendered.replace(
        "`~/.claude/CLAUDE.md`",
        "`~/.codex/config.toml`",
    )
    # Replace the output-styles reference with the personality/#816 pointer
    rendered = rendered.replace(
        "`~/.claude/output-styles/*`",
        "personality (see #816)",
    )

    compact_prompt_path = str(ENGRAM_HOME / "codex-compact-prompt.md")
    merge_codex_config_toml(
        CODEX_HOME / "config.toml",
        rendered,
        compact_prompt_path,
    )
    print(f"  config.toml → {CODEX_HOME}/config.toml", flush=True)

    # 3. ~/.codex/AGENTS.md — project conventions placeholder
    substitute_template(
        TEMPLATES_DIR / "template.AGENTS.md",
        CODEX_HOME / "AGENTS.md",
        substitutions,
    )
    print(f"  AGENTS.md → {CODEX_HOME}/AGENTS.md", flush=True)


def _set_cleanup_period_days(claude_home: Path, days: int = 36500) -> None:
    """Merge cleanupPeriodDays into ~/.claude/settings.json if not already set.

    Claude Code's default ~30-day rolling trim silently deletes chat transcripts.
    For ENGRAM agents, transcripts are identity history and ENGRAM evidence anchors
    — losing them means losing re-auditability for any observation cited by jsonl
    path. Setting a large value (36500 = 100 years) at install time preserves them
    without the user needing to know the trim exists.

    Only sets the key when absent — never overrides a value the user already chose.
    """
    settings_path = claude_home / "settings.json"
    try:
        settings: dict = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        settings = {}
    if "cleanupPeriodDays" not in settings:
        settings["cleanupPeriodDays"] = days
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        print(
            f"  settings.json → cleanupPeriodDays={days} "
            f"(preserves transcript history; override anytime in {settings_path})",
            flush=True,
        )


def _db_node_count(db_path: Path) -> int:
    """Return the number of rows in the nodes table, or 0 if the table
    doesn't exist yet (e.g. file exists but schema hasn't been created).

    Raises SystemExit on connection-level failures (PermissionError, locked DB,
    etc.) so FORCE=1 / FORCE_RESEED_EMPTY=1 refuse rather than silently
    treating an unreadable DB as empty and proceeding to seed live data.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                # nodes table not created yet — DB is effectively empty (new install)
                return 0
            raise SystemExit(
                f"[bootstrap] ERROR: cannot query {db_path}: {exc}\n"
                "  Refusing to proceed — DB state is ambiguous.\n"
                "  Release any DB locks, then re-run bootstrap."
            ) from exc
        finally:
            conn.close()
    except SystemExit:
        raise  # let the inner SystemExit propagate unchanged
    except Exception as exc:
        raise SystemExit(
            f"[bootstrap] ERROR: cannot read {db_path}: {exc}\n"
            "  Refusing to proceed — DB state is ambiguous.\n"
            "  Fix the file permissions or remove the lock, then re-run bootstrap."
        ) from exc


def main() -> None:
    # Refuse to overwrite an existing install — defense-in-depth for the
    # plugin path.
    #
    # Named escape valves (in order of check):
    #   FORCE=1             — on an EMPTY DB (0 nodes): proceed with seeding.
    #                         On a non-empty DB: REFUSE (would corrupt the graph).
    #   FORCE_RESEED_EMPTY=1 — same semantics as FORCE=1-on-empty-DB; explicit
    #                         name for callers that want clarity at the call site.
    #
    # Neither escape valve deletes anything. A true clean slate requires
    # manually deleting knowledge.db first, then re-running bootstrap.
    db_path = ENGRAM_HOME / "knowledge.db"
    if db_path.exists():
        force = os.environ.get("FORCE", "0") == "1"
        force_reseed_empty = os.environ.get("FORCE_RESEED_EMPTY", "0") == "1"
        if force or force_reseed_empty:
            node_count = _db_node_count(db_path)
            escape_var = "FORCE" if force else "FORCE_RESEED_EMPTY"
            if node_count > 0:
                raise SystemExit(
                    f"[bootstrap] {escape_var}=1 refused: knowledge.db is non-empty "
                    f"({node_count} nodes).\n"
                    f"  Both FORCE=1 and FORCE_RESEED_EMPTY=1 require an empty (0-node) DB.\n"
                    f"  To start fresh: delete {db_path} manually (copy it first if unsure),\n"
                    f"  then re-run bootstrap without any FORCE variable."
                )
            print(
                f"[bootstrap] {escape_var}=1 on empty DB — proceeding with seeding.",
                flush=True,
            )
            # DB file exists but has 0 nodes — safe to seed.
        else:
            raise SystemExit(
                f"ERROR: knowledge.db already exists at {db_path}.\n"
                "  bootstrap.py refuses to overwrite an existing install.\n"
                "  - If this is your data — DO NOT delete it. To upgrade, use\n"
                "    the engram-upgrade skill.\n"
                "  - If this is an aborted prior bootstrap (verify the DB is\n"
                "    truly empty first), delete the file and re-run.\n"
                "  - FORCE_RESEED_EMPTY=1 re-seeds an empty (0-node) DB.\n"
                "  - For a true fresh start, delete the file first."
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
        # template.CLAUDE.md hook-path table, etc.) reference the scatter
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

    # Load compact-instructions.md into substitutions so the Claude target
    # can fold it back in (lossless) and the codex target can write it as a
    # separate file.
    compact_instructions_path = TEMPLATES_DIR / "compact-instructions.md"
    if compact_instructions_path.exists():
        substitutions["{{COMPACT_INSTRUCTIONS}}"] = (
            compact_instructions_path.read_text()
        )
    else:
        # Fallback: leave the marker unreplaced (the template will still render
        # with the literal placeholder, which is visible and actionable).
        print(
            f"  WARN: compact-instructions.md not found at "
            f"{compact_instructions_path}; {{{{COMPACT_INSTRUCTIONS}}}} "
            f"left unresolved",
            flush=True,
        )

    # Strip the source-of-truth breadcrumb that sits above the marker in
    # template.CLAUDE.md. It's a guide for whoever edits the template (the real
    # Compact Instructions live in compact-instructions.md), not for the rendered
    # install — so it must not leak into the agent's ~/.claude/CLAUDE.md or the
    # Codex prompt. The codex target iterates substitutions and .replace(), so
    # one empty-value entry removes the whole comment line there. The Claude
    # target handles the breadcrumb via fold_compact_instructions() (see below).
    # COMPACT_BREADCRUMB_LINE is the canonical string from template_render.py
    # (SSoT); tests/test_bootstrap_codex.py asserts it matches the template.
    substitutions[COMPACT_BREADCRUMB_LINE] = ""

    manifest = {
        "seed_ids": ids,
        "alpha_dir": str(ALPHA_DIR),
        "target": TARGET,
    }
    (ENGRAM_HOME / "seed-manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    print(f"  manifest → {ENGRAM_HOME}/seed-manifest.json", flush=True)

    # Shared (both targets): warm briefing lives in ENGRAM_HOME.
    substitute_template(
        TEMPLATES_DIR / "template.warm-briefing.md",
        ENGRAM_HOME / "warm-briefing.md",
        substitutions,
    )
    print(f"  warm-briefing.md → {ENGRAM_HOME}/warm-briefing.md", flush=True)

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
        # Use the SSoT fold function for the compact-instructions fold (#1193) so
        # bootstrap stays wired to template_render.fold_compact_instructions() and
        # cannot diverge from the canonical fold logic via inline substitution drift.
        _template_text = (TEMPLATES_DIR / "template.CLAUDE.md").read_text()
        _compact_text = substitutions.get("{{COMPACT_INSTRUCTIONS}}")
        if _compact_text is not None:
            _claude_text = fold_compact_instructions(_template_text, _compact_text)
        else:
            # Compact file absent (fallback from above): strip breadcrumb only,
            # leave {{COMPACT_INSTRUCTIONS}} unresolved so the gap is visible.
            _claude_text = _template_text.replace(COMPACT_BREADCRUMB_LINE, "")
        # Apply remaining (non-compact) substitutions. Exclude the two compact-fold
        # keys — both are already handled above (by fold_compact_instructions or the
        # fallback .replace call); re-applying them would be a harmless no-op but
        # exclusion makes the intent explicit.
        for _ph, _val in substitutions.items():
            if _ph not in (COMPACT_BREADCRUMB_LINE, "{{COMPACT_INSTRUCTIONS}}"):
                _claude_text = _claude_text.replace(_ph, _val)
        CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        (CLAUDE_HOME / "CLAUDE.md").write_text(_claude_text)
        print(f"  CLAUDE.md → {CLAUDE_HOME}/CLAUDE.md", flush=True)

        _set_cleanup_period_days(CLAUDE_HOME)

    elif TARGET == "codex":
        _render_codex_target(substitutions)


if __name__ == "__main__":
    main()
