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
  - axiom (honesty ⊥ discretion) about axiom (honesty) [via context_ids]
  - goal (epistemic humility) about axiom (honesty)    [via context_ids]
"""

import errno
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for marker writes — same
    `date +%Y-%m-%dT%H:%M:%S%z` shape used by downstream readers.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")

ALPHA_DIR = Path(os.environ["ALPHA_DIR"])
TEMPLATES_DIR = Path(os.environ["ALPHA_TEMPLATES_DIR"])
# ENGRAM_HOME is REQUIRED here (unset stays a KeyError, as before). #3863 step 3a: this is an entry point, so a
# RELATIVE value raises ValueError naming it (the resolver sits beside this file in both layouts), never cwd-anchored.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engram_home_resolver  # noqa: E402

if "ENGRAM_HOME" not in os.environ:
    raise KeyError("ENGRAM_HOME")
ENGRAM_HOME = engram_home_resolver.engram_home()
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
# on env-var conventions (in plugin mode the venv data dir at ~/.engram/ (its data part: $ENGRAM_HOME) never
# gets server.py copied into it).
# ENGRAM_HOME is still read by server.py at module import (line 145) to
# resolve DATA_DIR — that's a separate concern from where server.py lives.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402
import engram_file_guard as fg  # noqa: E402


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


def _render_template_text(src: Path, substitutions: dict[str, str]) -> str:
    text = src.read_text()
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text


def substitute_template(
    src: Path, dst: Path, substitutions: dict[str, str]
) -> None:
    """Render `src` to `dst` through the file guard: an existing differing `dst` is refused."""
    text = _render_template_text(src, substitutions)
    try:
        fg.apply(fg.plan([fg.Item(dst, text)]))
    except fg.Refused as e:
        raise SystemExit(str(e))


# --- Never clobber a user's existing file -----------------------------------------------
# engram_file_guard owns the decision, the backup and the write for every protected path
# (CLAUDE.md, AGENTS.md, config.toml, codex-compact-prompt.md, warm-briefing.md,
# seed-manifest.json, first-run-pending). These names stay for callers and tests.

CLAUDE_MD_BACKUP_NAME = "CLAUDE.md.pre-engram"  # same name install.sh writes
CODEX_CONFIG_BACKUP_NAME = "config.toml.pre-engram"
AGENTS_MD_BACKUP_NAME = "AGENTS.md.pre-engram"  # #4452, the Codex counterpart of CLAUDE.md

_target_present = fg.target_present
_is_dangling_symlink = fg.is_dangling_symlink
_copy_exclusive = fg.copy_exclusive
_atomic_write_text = fg.atomic_write_text
_backup_path_problem = fg.backup_path_problem
_backup_once = fg.backup_once


def _refuse_dangling(path: Path) -> None:
    raise SystemExit(
        f"[bootstrap] REFUSED: {path} is a symlink to a path that does not exist "
        f"({os.readlink(path)}). Move the link aside (mv '{path}' '{path}.moved-aside') or "
        f"repair it so it points at a real file, then re-run."
    )


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


def merged_codex_config_text(
    existing: str,
    instructions_body: str,
    compact_prompt_path: str,
) -> str:
    """Return the config.toml text with the Codex identity keys set, preserving other stanzas.

    Pure: reads and writes nothing. The caller hands the result to the file guard, which
    backs the current file up before replacing it.

    Strategy (merge-not-clobber):
    - Start from the existing text (empty string if there is no file).
    - Strip any pre-existing instructions=, experimental_compact_prompt_file=,
      and # model = ... lines (to replace them idempotently).
    - Prepend the new identity block (instructions, compact_prompt_file, model
      comment) to the remaining content.

    This ensures that plugin stanzas appended by 'codex plugin add' after an
    initial bootstrap run are preserved intact on subsequent runs.
    """
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

    return new_content


def _codex_texts(substitutions: dict[str, str]) -> "tuple[str, str]":
    """Render the two Codex texts that depend on the substitutions. Pure: writes nothing.

    Returns (compact_prompt_text, instructions_body):
    1. ~/.engram/codex-compact-prompt.md ($ENGRAM_HOME)  - compact instructions (standalone)
    2. the instructions= body of ~/.codex/config.toml (the identity keys, merge-not-clobber)
    ~/.codex/AGENTS.md (project conventions placeholder) is the plain template render.

    The instructions= body is the full template.CLAUDE.md rendered with
    seed-IDs/ENGRAM_HOME, then adapted for Codex (CLAUDE_PLUGIN_ROOT -> CODEX_PLUGIN_ROOT;
    ~/.claude/CLAUDE.md self-refs -> ~/.codex/config.toml; output-styles ref ->
    a personality pointer). template.CLAUDE.md no longer contains a ## Compact Instructions
    section (as of engram-alpha#2067), so there is nothing left to remove here; the Codex
    compact prompt is rendered separately from templates/compact-instructions.md.

    {{AGENT_NAME}}, {{USER_NAME}}, {{SELF_NODE_ID}}, {{TODAY}} are intentionally
    left for the first-session skill to resolve.
    """
    compact_body = substitutions.get("{{COMPACT_INSTRUCTIONS}}", "")
    # Apply seed-IDs and ENGRAM_HOME (leave first-session markers alone)
    compact_rendered = compact_body
    for placeholder, value in substitutions.items():
        if placeholder != "{{COMPACT_INSTRUCTIONS}}":
            compact_rendered = compact_rendered.replace(placeholder, value)

    # Start from the template.CLAUDE.md, substitute seed-IDs + ENGRAM_HOME,
    # then apply Codex-specific adaptations, then remove compact section.
    raw = (TEMPLATES_DIR / "template.CLAUDE.md").read_text()
    rendered = raw
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

    # Strip a ## Compact Instructions section if present. This is currently
    # a no-op (the template no longer carries that section, engram-alpha#2067),
    # retained deliberately as a guard: docs/compaction-continuity.md
    # anticipates the section being re-added to template.CLAUDE.md later, and
    # if that happens this call makes the Codex path strip it automatically.
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
    return compact_rendered, rendered


def _utc_stamp() -> str:
    """UTC time as `%Y%m%dT%H%M%SZ`, the suffix of a settings.json.corrupt.<UTC> backup."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_json_indent = fg.json_indent  # moved into the guard with the MERGE class


def _check_settings(claude_home: Path) -> "tuple[Path, dict | None, str]":
    """Read-only validation of ~/.claude/settings.json, shared by the preflight and the final step.

    Returns (target, settings, text): `target` is where a write would go (a symlinked
    settings.json is edited through the link, since replacing the link would detach it),
    `settings` is the parsed object, or None when the file is absent (fine), `text` its text.
    A dangling symlink, an unreadable file, or anything that is not a JSON object REFUSES
    with SystemExit; the non-object case first copies the file with _backup_once to
    settings.json.corrupt.<UTC>. The file itself is never modified.
    """
    settings_path = claude_home / "settings.json"
    if _is_dangling_symlink(settings_path):
        _refuse_dangling(settings_path)
    target = Path(os.path.realpath(settings_path)) if settings_path.is_symlink() else settings_path
    if not _target_present(target):
        return target, None, ""
    try:
        raw = target.read_bytes()
    except OSError as e:
        raise SystemExit(
            f"[bootstrap] REFUSED: cannot read {settings_path} ({e}). Nothing was changed; "
            f"fix its permissions, then re-run."
        )
    reason = None
    settings = None
    text = ""
    try:
        text = raw.decode("utf-8")
        settings = json.loads(text)
        if not isinstance(settings, dict):
            reason = f"it holds JSON {type(settings).__name__}, not an object"
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        reason = f"it is not valid JSON ({e})"
    if reason is not None:
        backup = settings_path.parent / f"settings.json.corrupt.{_utc_stamp()}"
        existed = _target_present(backup)
        _backup_once(settings_path, backup)
        saved = (
            f"an earlier backup already at {backup} was left untouched"
            if existed else f"a copy was saved to {backup}"
        )
        raise SystemExit(
            f"[bootstrap] REFUSED: {settings_path} cannot be edited: {reason}; {saved}. "
            f"The file itself was left unchanged. Repair it (or move it aside), then re-run "
            f"with --retention-days."
        )
    return target, settings, text


def _preflight_settings(claude_home: Path) -> None:
    """Run at the very start of main() for `--retention-days` on the Claude target: refuse a
    bad settings.json BEFORE anything is created, so the user can repair it and simply re-run
    (once the graph is seeded, bootstrap's existing-DB guard refuses a re-run). Read-only apart
    from the corrupt-file backup."""
    _check_settings(claude_home)


def _set_cleanup_period_days(claude_home: Path, days: int) -> None:
    """Merge cleanupPeriodDays into ~/.claude/settings.json, only when the user asked (#4445).

    Called only when `--retention-days N` was given; without the flag bootstrap never
    reads or writes settings.json. Claude Code's default ~30-day rolling trim deletes chat
    transcripts, which are the evidence behind ENGRAM observations; the user decides how
    long to keep them. The write is the guard's MERGE class (engram_file_guard.apply_merge):
    cleanupPeriodDays is the only key ENGRAM owns in this file.

    * absent file: created as {"cleanupPeriodDays": N}, mode 0600.
    * valid JSON object without the key: stamped backup first, then merged; every other key,
      its order and the indent style are kept.
    * valid JSON object WITH the key: left byte-identical, no backup (the earlier choice stands).
    * anything else (invalid JSON, empty, a list or scalar): copied once to
      settings.json.corrupt.<UTC>, the file left byte-identical, and the run REFUSES.
      A settings.json is never replaced after a parse failure.
    """
    settings_path = claude_home / "settings.json"
    # Second check, not a copy of the first: settings.json can change after the preflight.
    target, settings, text = _check_settings(claude_home)
    if settings is not None and "cleanupPeriodDays" in settings:
        print(
            f"  settings.json → cleanupPeriodDays={settings['cleanupPeriodDays']} kept "
            f"(your earlier choice; --retention-days {days} did not change it; "
            f"edit {settings_path} to change it)",
            flush=True,
        )
        return
    try:
        claude_home.mkdir(parents=True, exist_ok=True)
        fg.apply_merge(settings_path, {"cleanupPeriodDays": days}, filename="settings.json", new_mode=0o600)
    except fg.MergeRefused as e:
        raise SystemExit(f"[bootstrap] REFUSED: settings.json was not changed ({e}).")
    except (OSError, UnicodeError) as e:
        # UnicodeError: a lone-surrogate JSON escape ("\\ud800") loads but cannot be encoded
        # as UTF-8; that is a ValueError, not an OSError, and must not become a traceback.
        _refuse_settings_write(settings_path, e)
    _print_retention_set(days, settings_path)


def _refuse_settings_write(settings_path: Path, err: Exception) -> None:
    raise SystemExit(
        f"[bootstrap] REFUSED: settings.json was not written ({err}). The install is otherwise "
        f"complete (graph seeded, files rendered, data home tightened); settings.json is "
        f"unchanged. Fix the cause, or add cleanupPeriodDays to {settings_path} by hand."
    )


def _print_retention_set(days: int, settings_path: Path) -> None:
    print(
        f"  settings.json → cleanupPeriodDays={days}: Claude Code keeps your chat "
        f"transcripts for {days} days before deleting them (change it anytime in "
        f"{settings_path})",
        flush=True,
    )


def _db_node_count(db_path: Path) -> int:
    """Return the number of rows in the nodes table, or 0 if the table
    doesn't exist yet (e.g. file exists but schema hasn't been created).

    Raises SystemExit on connection-level failures (PermissionError, locked DB,
    etc.) so FORCE=1 / FORCE_RESEED_EMPTY=1 refuse rather than silently
    treating an unreadable DB as empty and proceeding to seed live data.

    Opens the store read-only (`mode=ro`): bootstrap runs before any owner can
    exist and only counts rows. On a cleanly closed WAL store SQLite may still
    create a `-shm` (a shared-memory index, not data); the main file and the
    `-wal` are left unchanged. `immutable=1` is deliberately not used: it tells
    SQLite no other writer exists, which is false whenever a seat is live.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"{Path(db_path).absolute().as_uri()}?mode=ro", uri=True, timeout=0)
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


def _build_substitutions(ids: dict[str, str], quiet: bool = False) -> dict[str, str]:
    """Placeholder -> value map shared by every render (and by the pre-write guard)."""
    substitutions = dict(ids)
    substitutions["{{ENGRAM_HOOKS_DIR}}"] = str(ENGRAM_HOME / "hooks")
    substitutions["{{ENGRAM_HOME}}"] = str(ENGRAM_HOME)
    substitutions["{{PYTHON}}"] = os.environ.get("PYTHON_BIN", "python3")

    # Load compact-instructions.md into substitutions. The CODEX target writes
    # it as a separate file (codex-compact-prompt.md) and wires it into
    # config.toml. The CLAUDE target no longer uses this key at all (#2067
    # removed the ## Compact Instructions section from template.CLAUDE.md); it
    # stays loaded here because both targets share this substitutions dict.
    compact_instructions_path = TEMPLATES_DIR / "compact-instructions.md"
    if compact_instructions_path.exists():
        substitutions["{{COMPACT_INSTRUCTIONS}}"] = (
            compact_instructions_path.read_text()
        )
    else:
        # Fallback: leave the marker unreplaced (the template will still render
        # with the literal placeholder, which is visible and actionable).
        if not quiet:
            print(
                f"  WARN: compact-instructions.md not found at "
                f"{compact_instructions_path}; {{{{COMPACT_INSTRUCTIONS}}}} "
                f"left unresolved",
                flush=True,
            )
    return substitutions


_SEED_PLACEHOLDERS = (
    "{{AX_HONESTY}}", "{{AX_HONESTY_DISCRETION}}", "{{AX_PROVENANCE}}",
    "{{DF_ENGRAM}}", "{{DF_EPISTEMIC_IDENTITY}}", "{{GL_EPISTEMIC_HUMILITY}}",
)

# create_seed_graph on an empty graph mints these IDs every time: the sequences table counts
# from 1 per node type, in the fixed order of the calls above. Pinned against the real seeding
# by tests/test_engram_file_guard_slice1.py, so the check-only pass can predict every render
# without a seed manifest. If a run ever mints different IDs (a 0-node store that kept old
# counters), the post-seeding pass judges the REAL renders and refuses what it cannot replace.
_SEED_ORDER = (  # (placeholder, node-type prefix), in create_seed_graph's call order
    ("{{AX_HONESTY}}", "ax"),
    ("{{AX_HONESTY_DISCRETION}}", "ax"),
    ("{{AX_PROVENANCE}}", "ax"),
    ("{{DF_ENGRAM}}", "df"),
    ("{{DF_EPISTEMIC_IDENTITY}}", "df"),
    ("{{GL_EPISTEMIC_HUMILITY}}", "gl"),
)


def _seed_ids_on_empty_graph() -> dict[str, str]:
    """The IDs a fresh graph mints: each type's counter starts at 1, in _SEED_ORDER."""
    counts: dict[str, int] = {}
    ids: dict[str, str] = {}
    for placeholder, prefix in _SEED_ORDER:
        counts[prefix] = counts.get(prefix, 0) + 1
        ids[placeholder] = f"{prefix}_{counts[prefix]:04d}"
    return ids


_FIXED_SEED_IDS = _seed_ids_on_empty_graph()


def _predicted_claude_render() -> str | None:
    """What this run would write to CLAUDE.md, for the check-only pass.

    The seed IDs are fixed (`_FIXED_SEED_IDS`), so no seed manifest is read. An unreadable
    template -> None.
    """
    try:
        return _render_template_text(
            TEMPLATES_DIR / "template.CLAUDE.md",
            _build_substitutions(dict(_FIXED_SEED_IDS), quiet=True),
        )
    except OSError:
        return None


def _predicted_agents_render() -> str | None:
    """What this run would write to AGENTS.md, for the check-only pass.

    template.AGENTS.md carries no seed-ID placeholders: the render is the template under the
    same substitutions the real run uses (the unresolved first-session markers stay literal).
    An unreadable template -> None.
    """
    try:
        return _render_template_text(
            TEMPLATES_DIR / "template.AGENTS.md", _build_substitutions({}, quiet=True)
        )
    except OSError:
        return None


def _read_existing_text(path: Path) -> str:
    """Existing text of `path` for the merge ('' if absent, dangling or not a file: the guard
    refuses those). A file that exists but cannot be read as UTF-8 text refuses here."""
    if not fg.target_present(path) or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(
            f"[bootstrap] REFUSED: cannot read {path} as text ({e}), so ENGRAM cannot merge "
            f"into it. Nothing was written. Move it aside, then re-run."
        )


def _write_items(
    ids: dict[str, str], substitutions: dict[str, str], marker_payload: str,
    claude_md_text: "str | None", agents_md_text: "str | None",
) -> "list[fg.Item]":
    """Every protected path this run would write, with its new text (one list for both passes)."""
    manifest = json.dumps(
        {"seed_ids": ids, "alpha_dir": str(ALPHA_DIR), "target": TARGET}, indent=2
    )
    items = [
        # ENGRAM's own bookkeeping (fg.ENGRAM_OWNED_NAMES): rewritten when it differs, no consent.
        fg.Item(ENGRAM_HOME / "seed-manifest.json", manifest, owned=True),
        fg.Item(
            ENGRAM_HOME / "warm-briefing.md",
            _render_template_text(TEMPLATES_DIR / "template.warm-briefing.md", substitutions),
        ),
        fg.Item(ENGRAM_HOME / "first-run-pending", marker_payload, owned=True),
    ]
    if TARGET == "claude":
        assert CLAUDE_HOME is not None
        items.append(fg.Item(CLAUDE_HOME / "CLAUDE.md", claude_md_text, first_identity=True))
    else:
        assert CODEX_HOME is not None
        compact_text, instructions_body = _codex_texts(substitutions)
        compact_path = ENGRAM_HOME / "codex-compact-prompt.md"
        config_path = CODEX_HOME / "config.toml"
        items.append(fg.Item(compact_path, compact_text))
        items.append(fg.Item(
            config_path,
            merged_codex_config_text(
                _read_existing_text(config_path), instructions_body, str(compact_path)
            ),
            first_identity=True,
        ))
        items.append(fg.Item(CODEX_HOME / "AGENTS.md", agents_md_text, first_identity=True))
    return items


def _move_aside_command() -> str:
    """The shell command that sets the whole ENGRAM home aside, for a deliberate fresh start."""
    import shlex
    home = str(ENGRAM_HOME)
    return f"mv {shlex.quote(home)} {shlex.quote(home + '.moved-aside-')}$(date -u +%Y%m%dT%H%M%SZ)"


# Said BEFORE every whole-home move-aside command: the home holds live sockets and the running
# server, so moving it under them breaks the running ENGRAM.
MOVE_ASIDE_STOP_FIRST_NOTE = (
    "Stop the running ENGRAM server, daemons and monitors first: the home holds the installed "
    "plugin, the Python environment and live sockets."
)

# Said wherever a message gives the move-aside command: the home holds the installed plugin and venv.
MOVE_ASIDE_REINSTALL_NOTE = (
    "The installed plugin and its Python environment move too, so ENGRAM must be reinstalled afterwards."
)


def _memory_found() -> "list[str]":
    """The files of an existing agent's memory in ENGRAM_HOME (C5): warm-briefing.md,
    seed-manifest.json, a history/ or diary/ holding anything but dotfiles (a lone .DS_Store is
    not memory). seed-manifest.json counts here even though bootstrap itself rewrites it."""
    found = []
    for name in ("warm-briefing.md", "seed-manifest.json"):
        if fg.target_present(ENGRAM_HOME / name):
            found.append(str(ENGRAM_HOME / name))
    for name in ("history", "diary"):
        d = ENGRAM_HOME / name
        try:
            if d.is_dir() and any(not child.name.startswith(".") for child in d.iterdir()):
                found.append(f"{d}/ (not empty)")
        except OSError:
            found.append(f"{d}/ (unreadable)")
    return found


def _newest_by_mtime(paths: "list[Path]") -> Path:
    """The most recently modified file; ties go to the later name. An unreadable entry sorts oldest."""
    def key(p: Path) -> "tuple[int, str]":
        try:
            return (p.stat().st_mtime_ns, p.name)
        except OSError:
            return (-1, p.name)
    return max(paths, key=key)


def _refuse_existing_memory(found: "list[str]", db_state: str) -> None:
    lines = [
        f"[bootstrap] REFUSED: {ENGRAM_HOME} already holds an agent's memory but {db_state}, "
        f"so bootstrap will not seed a new graph beside it. --force and FORCE do not change "
        f"this. Nothing was written.",
        "  Found:",
        *[f"    - {f}" for f in found],
    ]
    backups = list((ENGRAM_HOME / "db-backup").glob("*.db")) if (ENGRAM_HOME / "db-backup").is_dir() else []
    if backups:
        lines.append(
            f"  The newest graph backup is {_newest_by_mtime(backups)}. Restore it to "
            f"{ENGRAM_HOME / 'knowledge.db'} (an empty knowledge.db there: move it aside "
            f"first), then re-run."
        )
    lines.append(
        f"  For a deliberate fresh start, set the whole home aside, then re-run. "
        f"{MOVE_ASIDE_STOP_FIRST_NOTE}\n"
        f"    {_move_aside_command()}\n"
        f"  {MOVE_ASIDE_REINSTALL_NOTE}"
    )
    raise SystemExit("\n".join(lines))


class _Args(NamedTuple):
    consent: "fg.Consent"
    retention_days: "int | None"


def _parse_args(argv: list[str]) -> _Args:
    """Return (consent, retention_days). Anything else is an error (no silent ignore).

    `--force` is consent to replace every differing file the refusal lists; `--replace PATH`
    (repeatable) is consent for that path only. Either one is for use only after the user has
    seen the list and said yes. `--retention-days N` is the only way bootstrap touches
    ~/.claude/settings.json; N must be an integer 1..36500.
    """
    force = False
    replace: list[str] = []
    retention_days = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--force":
            force = True
        elif arg == "--replace":
            if i + 1 >= len(argv):
                raise SystemExit("[bootstrap] --replace needs a PATH (the file the user agreed to replace)")
            replace.append(argv[i + 1])
            i += 1
        elif arg == "--retention-days":
            value = argv[i + 1] if i + 1 < len(argv) else None
            try:
                # ASCII digits only: int() would also take " 30", "+30", "3_0", Arabic digits.
                n = int(value) if value is not None and value.isascii() and value.isdigit() else None
            except ValueError:
                n = None
            if n is None or not 1 <= n <= 36500:
                raise SystemExit(
                    f"[bootstrap] --retention-days needs an integer from 1 to 36500 (days "
                    f"Claude Code keeps chat transcripts; 36500 is about 100 years), got "
                    f"{value if value is not None else 'nothing'!r}"
                )
            retention_days = n
            i += 1
        else:
            raise SystemExit(
                f"[bootstrap] unknown argument {arg!r}; the options are --force, "
                f"--replace PATH and --retention-days N"
            )
        i += 1
    return _Args(fg.consent_for(force, replace), retention_days)


def _tighten_home_step() -> None:
    """Make the data home owner-only (#4445); never lets a failure stop the install."""
    try:
        import engram_home_perms
        res = engram_home_perms.tighten_data_home(ENGRAM_HOME)
    except Exception as e:  # a stale build without the module must not break bootstrap
        print(f"  data home permissions: not tightened ({type(e).__name__}: {e})", flush=True)
        return
    if res.changed or res.acl_preserved or res.errors:
        print(
            f"  data home → owner-only: {len(res.changed)} path(s) tightened"
            + (f", {len(res.acl_preserved)} carrying a named-user ACL (group and other entries cleared, named entries and mask kept)"
               if res.acl_preserved else "")
            + (f", {len(res.errors)} skipped (first: {res.errors[0]})" if res.errors else ""),
            flush=True,
        )


def _check_existing_agent(db_path: Path) -> None:
    """Refuse (SystemExit) before ANY write when the home already holds an agent (C5).

    * knowledge.db with nodes: never seeded over, whatever FORCE / FORCE_RESEED_EMPTY say.
    * no knowledge.db, or one with 0 nodes, while memory files are present: refused even
      with --force, FORCE and FORCE_RESEED_EMPTY; no graph and no marker is created.
    * no knowledge.db and no memory (a truly empty home): seed.
    * an existing 0-node knowledge.db and no memory: seed only when FORCE=1 or
      FORCE_RESEED_EMPTY=1 names it (an aborted prior bootstrap).

    No path is deleted. A deliberate fresh start sets the whole home aside.
    """
    found = _memory_found()
    if not db_path.exists():
        if found:
            _refuse_existing_memory(found, "there is no knowledge.db")
        return
    force = os.environ.get("FORCE", "0") == "1"
    force_reseed_empty = os.environ.get("FORCE_RESEED_EMPTY", "0") == "1"
    node_count = _db_node_count(db_path)
    if node_count > 0:
        if force or force_reseed_empty:
            escape_var = "FORCE" if force else "FORCE_RESEED_EMPTY"
            raise SystemExit(
                f"[bootstrap] {escape_var}=1 refused: knowledge.db is non-empty "
                f"({node_count} nodes).\n"
                f"  Both FORCE=1 and FORCE_RESEED_EMPTY=1 require an empty (0-node) DB.\n"
                f"  To start fresh on purpose, set the whole home aside, then re-run bootstrap\n"
                f"  without any FORCE variable. {MOVE_ASIDE_STOP_FIRST_NOTE}\n"
                f"    {_move_aside_command()}\n"
                f"  {MOVE_ASIDE_REINSTALL_NOTE}"
            )
        raise SystemExit(
            f"ERROR: knowledge.db already exists at {db_path} ({node_count} nodes).\n"
            "  bootstrap.py refuses to overwrite an existing install.\n"
            "  - If this is your data, leave it as it is. To upgrade, use the\n"
            "    engram-upgrade skill.\n"
            "  - For a deliberate fresh start, set the whole home aside, then re-run.\n"
            f"    {MOVE_ASIDE_STOP_FIRST_NOTE}\n"
            f"    {_move_aside_command()}\n"
            f"    {MOVE_ASIDE_REINSTALL_NOTE}"
        )
    # knowledge.db exists with 0 nodes.
    if found:
        _refuse_existing_memory(found, "its knowledge.db holds no nodes")
    if not (force or force_reseed_empty):
        raise SystemExit(
            f"ERROR: knowledge.db already exists at {db_path} and holds no nodes.\n"
            "  bootstrap.py refuses to overwrite an existing install without being told.\n"
            "  - If this is an aborted prior bootstrap (verify the DB is truly empty),\n"
            "    re-run with FORCE_RESEED_EMPTY=1.\n"
            "  - Otherwise move the file aside (it is not removed), then re-run:\n"
            f"    mv {db_path} {db_path}.moved-aside-$(date -u +%Y%m%dT%H%M%SZ)"
        )
    escape_var = "FORCE" if force else "FORCE_RESEED_EMPTY"
    print(
        f"[bootstrap] {escape_var}=1 on empty DB — proceeding with seeding.",
        flush=True,
    )


def main() -> None:
    # Consent: --force = yes to every file the refusal lists, --replace PATH = yes to that
    # path only. Both are for use after the user has seen the list and said yes. They are CLI
    # flags, distinct from the FORCE env var below, which is about an empty knowledge.db.
    _args = _parse_args(sys.argv[1:])
    consent = _args.consent

    # #4445: a bad settings.json is refused before ANYTHING is created (no ENGRAM_HOME, no
    # knowledge.db), so repair + plain re-run works. Read-only apart from the backup copy.
    if _args.retention_days is not None and TARGET == "claude":
        _preflight_settings(CLAUDE_HOME)

    # Existing-agent check (C5), before any write. Neither FORCE nor FORCE_RESEED_EMPTY
    # deletes or seeds over memory; a clean slate is the user moving the whole home aside.
    db_path = ENGRAM_HOME / "knowledge.db"
    _check_existing_agent(db_path)

    # ONE check-only pass over every protected path this run would write, BEFORE the first
    # write: absent -> will be written; identical -> left alone; differs -> needs consent.
    # One refusal names every path that still needs it, and nothing has been written.
    # The seed IDs are fixed, so the renders are predicted without a seed manifest.
    marker_payload = json.dumps({
        "created": _now_iso(),
        "install_path": str(ALPHA_DIR),
    }, indent=2)
    predicted_claude = _predicted_claude_render()
    predicted_agents = _predicted_agents_render()
    if (TARGET == "claude" and predicted_claude is None) or (TARGET == "codex" and predicted_agents is None):
        raise SystemExit(
            f"[bootstrap] REFUSED: cannot read the templates under {TEMPLATES_DIR}. Nothing was written."
        )
    pre_plan = fg.plan(_write_items(
        dict(_FIXED_SEED_IDS), _build_substitutions(dict(_FIXED_SEED_IDS), quiet=True),
        marker_payload, predicted_claude, predicted_agents,
    ))
    pre_refusal = fg.refusal(pre_plan, consent)
    if pre_refusal is not None:
        raise SystemExit(pre_refusal)
    # Back up every consented, differing file NOW: a backup that cannot be written refuses
    # the run while nothing else has changed (no graph seeded, no file replaced).
    backups = fg.save_backups(pre_plan, consent)

    # In plugin mode, symlink ~/.engram/tools ($ENGRAM_HOME/tools) -> <plugin-root>/tools so that
    # existing skill/agent references to ~/.engram/tools/<file> ($ENGRAM_HOME) (which were
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
    # ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1: skip the tools/hooks plugin-symlink
    # block below entirely, even when plugin.json is present. Added for a
    # per-run isolated ENGRAM_HOME build (e.g. exam-room's benchmark harness,
    # engram-agents/exam-room#21-#22 era) that runs bootstrap.py as an
    # ordinary subprocess -- everything else in main() (seed graph, CLAUDE.md
    # render, warm-briefing, first-run-pending marker) works correctly for
    # that use case exactly as written, since each invocation is its own
    # fresh process with its own env vars. Only the symlink step is wrong
    # there: it points ENGRAM_HOME/tools and /hooks at the PLUGIN's bundled
    # copies, which live outside any per-run sandbox jail -- a benchmark run
    # that later gets copied wholesale into an isolated directory (per-run
    # `cp -r` of a golden skeleton) would either carry a symlink that
    # escapes the jail, or need `cp -rL` to dereference it, silently
    # depending on the caller getting that flag right. Skipping the
    # symlinks entirely for this case means the golden-skeleton build must
    # instead copy (not link) whatever real tool/hook files it needs,
    # which is the caller's job, not bootstrap.py's -- this flag only
    # removes the wrong-for-that-context default, it does not replace it
    # with anything.
    _skip_symlinks_raw = os.environ.get("ENGRAM_BOOTSTRAP_SKIP_SYMLINKS", "0")
    skip_symlinks = _skip_symlinks_raw == "1"
    if _skip_symlinks_raw not in ("0", "1"):
        # Ariadne's colleague-pass finding on PR#2171: a strict `== "1"`
        # check means a near-miss value (e.g. "true", "yes") silently falls
        # through as unset -- no skip AND no announcement, indistinguishable
        # from a caller who never set the var at all. A guard whose whole
        # purpose is a safety suppression must fail VISIBLE when
        # misconfigured, not silently do the opposite of what was asked.
        #
        # Reviewer-fairy nit on THIS PR: this WARN is deliberately NOT
        # gated on `plugin_marker.is_file()`, unlike the skip-announcement
        # below. That asymmetry is intentional, not an oversight to copy
        # the other gate onto: the skip-announcement would be misleading
        # in a non-plugin context because the flag had zero effect there
        # (nothing to skip). This WARN flags a misconfigured *intent* (a
        # typo'd env var) -- worth surfacing regardless of whether this
        # particular run's context would have made the flag consequential,
        # since the caller's mistake is the same either way.
        print(
            f"  WARN: ENGRAM_BOOTSTRAP_SKIP_SYMLINKS={_skip_symlinks_raw!r} is "
            "not '0' or '1' -- treating as unset (symlinks will NOT be "
            "skipped). Set it to exactly '1' to skip.",
            flush=True,
        )
    bootstrap_dir = Path(__file__).resolve().parent
    plugin_marker = bootstrap_dir / "plugin.json"
    if skip_symlinks and plugin_marker.is_file():
        # Reviewer-fairy nit on this PR: only announce the skip when there
        # was actually something to skip -- printing this whenever the env
        # var is set, even from a non-plugin source-tree run where
        # plugin_marker.is_file() is already False, would announce a skip
        # that never had anything to do.
        print(
            "  ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1 — skipping ~/.engram/tools ($ENGRAM_HOME/tools) and "
            "~/.engram/hooks plugin symlinks (this ENGRAM_HOME is not a real "
            "plugin install)",
            flush=True,
        )
    if plugin_marker.is_file() and not skip_symlinks:
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
                            f"  ~/.engram/tools ($ENGRAM_HOME/tools) (existing dir) → moved to {backup}"
                            f" before replacing with plugin-tools symlink",
                            flush=True,
                        )
                engram_tools_link.symlink_to(plugin_tools)
                print(
                    f"  ~/.engram/tools ($ENGRAM_HOME/tools) → symlink → {plugin_tools}",
                    flush=True,
                )
            except OSError as e:
                print(
                    f"  WARN: could not create ~/.engram/tools ($ENGRAM_HOME/tools) symlink: {e}",
                    flush=True,
                )
        else:
            print(
                f"  WARN: plugin tools dir not found at {plugin_tools} —"
                " skill references to ~/.engram/tools/* ($ENGRAM_HOME) will not resolve",
                flush=True,
            )

        # Same symlink shape for ~/.engram/hooks/ ($ENGRAM_HOME) — many existing surfaces
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
                            f"  ~/.engram/hooks ($ENGRAM_HOME/hooks) (existing dir) → moved to {backup}"
                            f" before replacing with plugin-hooks symlink",
                            flush=True,
                        )
                engram_hooks_link.symlink_to(plugin_hooks)
                print(
                    f"  ~/.engram/hooks ($ENGRAM_HOME/hooks) → symlink → {plugin_hooks}",
                    flush=True,
                )
            except OSError as e:
                print(
                    f"  WARN: could not create ~/.engram/hooks ($ENGRAM_HOME/hooks) symlink: {e}",
                    flush=True,
                )
        else:
            print(
                f"  WARN: plugin hooks dir not found at {plugin_hooks} —"
                " skill references to ~/.engram/hooks/* ($ENGRAM_HOME) will not resolve",
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

    substitutions = _build_substitutions(ids)

    # Every protected write goes through the guard: absent -> written; identical -> left
    # alone; differs and consented -> backed up first. The guard judges the REAL renders here
    # (the check-only pass judged predicted ones), so a path that changed meanwhile is refused.
    final_items = _write_items(
        ids, substitutions, marker_payload,
        _render_template_text(TEMPLATES_DIR / "template.CLAUDE.md", substitutions) if TARGET == "claude" else None,
        _render_template_text(TEMPLATES_DIR / "template.AGENTS.md", substitutions) if TARGET == "codex" else None,
    )
    try:
        written = fg.apply(
            fg.plan(final_items), consent,
            context=(
                f"{db_path} was already seeded just before this second check ran. "
                f"To retry, move it aside, then re-run with the same flags:\n"
                f"    mv {db_path} {db_path}.moved-aside-$(date -u +%Y%m%dT%H%M%SZ)"
            ),
            saved=backups,
        )
    except fg.Refused as e:
        raise SystemExit(str(e))
    for item in final_items:
        if item.path in written:
            print(f"  {item.path.name} → {item.path}", flush=True)
        else:
            print(f"  {item.path.name} unchanged (already holds what ENGRAM would write)", flush=True)

    # Owner-only data home (#4445), both targets. Runs BEFORE the settings step below so a
    # refusal there never skips it.
    _tighten_home_step()

    if TARGET == "claude":
        # #4445: LAST, and only when asked. It can refuse (a settings.json that is not a
        # JSON object), which must not skip anything above.
        if _args.retention_days is not None:
            _set_cleanup_period_days(CLAUDE_HOME, _args.retention_days)

    elif TARGET == "codex":
        if _args.retention_days is not None:
            print(
                "  --retention-days applies to the Claude target only and was ignored "
                "(settings.json was not touched)",
                flush=True,
            )


if __name__ == "__main__":
    main()
