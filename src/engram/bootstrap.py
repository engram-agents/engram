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
    text = _render_template_text(src, substitutions)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)


# --- Never clobber a user's existing identity file (#4440) -------------------
# The Claude path (CLAUDE.md) and the Codex path (config.toml) decide "is there a
# file here I must not destroy" through ONE helper, so they cannot disagree.

CLAUDE_MD_BACKUP_NAME = "CLAUDE.md.pre-engram"  # same name install.sh writes
CODEX_CONFIG_BACKUP_NAME = "config.toml.pre-engram"


def _target_present(path: Path) -> bool:
    """True if anything sits at `path`, a dangling symlink included.

    os.path.lexists, not Path.exists(): exists() follows a symlink and says False
    for a dangling one, which would skip the backup for exactly that case.
    """
    return os.path.lexists(path)


def _is_dangling_symlink(path: Path) -> bool:
    return _target_present(path) and path.is_symlink() and not path.exists()


def _refuse_dangling(path: Path) -> None:
    raise SystemExit(
        f"[bootstrap] REFUSED: {path} is a symlink to a path that does not exist "
        f"({os.readlink(path)}). Remove the link (rm '{path}') or repair it so it "
        f"points at a real file, then re-run."
    )


# os.link errnos meaning "this filesystem has no hard links" (exFAT/FAT, some CIFS/SMB).
_NO_HARDLINK_ERRNOS = frozenset(
    e for e in (
        errno.EPERM, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", None),
        errno.EXDEV, errno.ENOSYS,
    ) if e is not None
)


def _copy_exclusive(src: Path, dst: Path) -> None:
    """Copy bytes then metadata from `src` to `dst`, publishing it all at once; raise
    FileExistsError if anything is at `dst`.

    Property: after this returns or raises, `dst` either does not exist or holds a COMPLETE
    copy of `src` (bytes and metadata). It is built in a sibling temp `<dst>.partial-<pid>`
    (opened "xb", O_EXCL), the temp's bytes are fsync'd, then it is published with
    os.link(temp, dst): atomic, raises FileExistsError for anything at `dst` (a symlink
    included), never overwrites or truncates. The temp is always unlinked afterwards. No
    directory fsync is done. shutil.copy2/copyfile onto `dst` would open it for writing
    and follow links (#4380 ratchet).

    Fallback, honestly: on a filesystem without hard links (os.link fails with EPERM,
    EOPNOTSUPP, ENOTSUP, EXDEV or ENOSYS) the finished temp is published with
    os.replace(temp, dst). That is still atomic, so no partial file can appear, but it
    LOSES EXCLUSIVITY: os.replace overwrites whatever is at `dst`. The caller has already
    established that `dst` is absent, so the residual risk is the check-then-publish race,
    on those filesystems only. Any other OSError from os.link propagates with nothing at `dst`.
    """
    import shutil
    temp = Path(f"{dst}.partial-{os.getpid()}")
    try:
        with open(src, "rb") as src_f, open(temp, "xb") as temp_f:
            shutil.copyfileobj(src_f, temp_f)
            temp_f.flush()
            os.fsync(temp_f.fileno())
        shutil.copystat(src, temp)
        try:
            os.link(temp, dst)
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in _NO_HARDLINK_ERRNOS:
                raise
            os.replace(temp, dst)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _backup_once(path: Path, backup: Path) -> None:
    """Copy `path` to `backup` with metadata, once; an existing backup is never touched.

    A dangling symlink at `backup` is neither "a backup exists" (nothing is saved there)
    nor "absent" (a plain copy would write through the link): refuse and write nothing. So
    is a backup that is the same file as `path` (hard link or link to it: overwriting `path`
    would destroy both), and anything else that is not a regular file (directory, FIFO,
    device, socket). The backup itself is written with an exclusive create.
    """
    if _is_dangling_symlink(backup):
        raise SystemExit(
            f"[bootstrap] REFUSED: the backup path {backup} is a symlink to a path that "
            f"does not exist ({os.readlink(backup)}), so {path} cannot be saved there. "
            f"Remove the link (rm '{backup}') or repair it, then re-run."
        )
    if _target_present(backup) and os.path.samefile(path, backup):
        raise SystemExit(
            f"[bootstrap] REFUSED: the backup path {backup} is the same file as {path} "
            f"(a hard link or link to it), so it would be overwritten too. Move "
            f"'{backup}' away, then re-run."
        )
    if _target_present(backup) and not backup.is_file():
        raise SystemExit(
            f"[bootstrap] REFUSED: the backup path {backup} is not a regular file, so "
            f"{path} cannot be saved there. Move it away, then re-run."
        )
    if _target_present(backup):
        print(f"  backup: existing {backup} kept untouched", flush=True)
        return
    try:
        _copy_exclusive(path, backup)
    except FileExistsError:
        raise SystemExit(
            f"[bootstrap] REFUSED: something appeared at the backup path {backup} while "
            f"{path} was being saved, so nothing was written there. Move it away, then re-run."
        )
    except OSError as e:
        raise SystemExit(
            f"[bootstrap] REFUSED: could not save {path} to the backup path {backup} "
            f"({e}), so nothing was written there. Fix the cause, then re-run."
        )
    print(f"  backup: {path} -> {backup}", flush=True)


def _claude_md_state(path: Path, expected: str | None) -> str:
    """'absent' | 'dangling' | 'identical' | 'differs' for the Claude identity file."""
    if not _target_present(path):
        return "absent"
    if _is_dangling_symlink(path):
        return "dangling"
    if expected is not None:
        try:
            if path.read_text() == expected:
                return "identical"
        except (OSError, UnicodeDecodeError):
            pass
    return "differs"


def _claude_md_guard(
    path: Path, expected: str | None, force: bool, seeded_db: Path | None = None
) -> None:
    """Refuse (SystemExit) unless writing `path` is safe; back it up first under --force.

    `seeded_db` is set only by the late guard, which runs after seeding: its refusal
    message then says the DB already exists and how to recover.
    """
    state = _claude_md_state(path, expected)
    if state == "dangling":
        _refuse_dangling(path)
    if state == "differs":
        backup = path.parent / CLAUDE_MD_BACKUP_NAME
        if not force and seeded_db is not None:
            raise SystemExit(
                f"[bootstrap] REFUSED: {path} already exists and differs from what ENGRAM "
                f"would write, and {seeded_db} was already seeded before this check ran. "
                f"Recover: remove {seeded_db} and re-run with --force (backs the file up to "
                f"{backup} first), or move {path} away and re-run."
            )
        if not force:
            raise SystemExit(
                f"[bootstrap] REFUSED: {path} already exists and differs from what ENGRAM "
                f"would write. Re-run with --force (backs it up to {backup} first), "
                f"or move the file away yourself."
            )
        _backup_once(path, backup)


def _codex_config_preflight(path: Path) -> None:
    """Codex: refuse a dangling symlink; otherwise back up an existing file once.

    The merge itself stays (foreign stanzas survive); the backup covers what the
    strip removes, i.e. the user's own top-level instructions block.
    """
    if not _target_present(path):
        return
    if _is_dangling_symlink(path):
        _refuse_dangling(path)
    _backup_once(path, path.parent / CODEX_CONFIG_BACKUP_NAME)


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

    1. ~/.engram/codex-compact-prompt.md ($ENGRAM_HOME)  — compact instructions (standalone)
    2. ~/.codex/config.toml               — identity keys (merge-not-clobber)
    3. ~/.codex/AGENTS.md                 — project conventions placeholder

    The instructions= body is the full template.CLAUDE.md rendered with
    seed-IDs/ENGRAM_HOME, then adapted for Codex (CLAUDE_PLUGIN_ROOT →
    CODEX_PLUGIN_ROOT; ~/.claude/CLAUDE.md self-refs → ~/.codex/config.toml;
    output-styles ref → personality/#816). template.CLAUDE.md no longer
    contains a ## Compact Instructions section (as of engram-alpha#2067),
    so there is nothing left to remove here; the Codex compact prompt is
    rendered separately, at step 1 above, from templates/compact-instructions.md.

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

    # Apply all substitutions. template.CLAUDE.md no longer contains
    # {{COMPACT_INSTRUCTIONS}} (as of engram-alpha#2067), so this loop no
    # longer folds the compact instructions back in — the compact prompt was
    # already rendered separately in step 1 above.
    rendered = raw
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

    # Strip a ## Compact Instructions section if present. This is currently
    # a no-op — the template no longer carries that section (engram-alpha#2067)
    # — retained deliberately as a guard: docs/compaction-continuity.md
    # anticipates the section being re-added to template.CLAUDE.md later, and
    # if that happens this call makes the Codex path strip it automatically
    # rather than relying on someone remembering to re-add the strip.
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


def _predicted_claude_render() -> str | None:
    """What this run would write to CLAUDE.md, for the pre-write identity check.

    Seed IDs are only known after seeding, and the guard must run BEFORE anything is
    written. So the IDs come from a previous run's seed-manifest.json (the aborted-then-
    retried install, where an empty graph re-seeds to the same IDs). No usable manifest
    -> None, and an existing file is then treated as differing.
    """
    try:
        ids = json.loads((ENGRAM_HOME / "seed-manifest.json").read_text())["seed_ids"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(ids, dict) or not all(isinstance(ids.get(k), str) for k in _SEED_PLACEHOLDERS):
        return None
    try:
        return _render_template_text(
            TEMPLATES_DIR / "template.CLAUDE.md", _build_substitutions(ids, quiet=True)
        )
    except OSError:
        return None


def _parse_args(argv: list[str]) -> bool:
    """Return True if --force was given. Anything else is an error (no silent ignore)."""
    force = False
    for arg in argv:
        if arg == "--force":
            force = True
        else:
            raise SystemExit(f"[bootstrap] unknown argument {arg!r}; the only option is --force")
    return force


def main() -> None:
    # --force: the one overwrite path for an existing, differing CLAUDE.md (#4440). It is a
    # CLI flag, distinct from the FORCE env var below, which is about the knowledge.db.
    force_identity_file = _parse_args(sys.argv[1:])

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

    # #4440: decide about the user's identity file BEFORE the first write of this run
    # (seeding, symlinks, manifest), so a refusal HERE leaves nothing half-done. (That holds
    # for this early guard only; the late guard below runs after seeding.)
    if TARGET == "claude":
        assert CLAUDE_HOME is not None
        _claude_md_guard(CLAUDE_HOME / "CLAUDE.md", _predicted_claude_render(), force_identity_file)
    elif TARGET == "codex":
        assert CODEX_HOME is not None
        _codex_config_preflight(CODEX_HOME / "config.toml")

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
        # #2067: the Claude template no longer folds in compact-instructions.md
        # (removed the ## Compact Instructions section entirely — see
        # docs/compaction-continuity.md). A plain single-file substitution is
        # the whole render now (substitute_template() creates CLAUDE_HOME).
        if not force_identity_file:
            # Skipping this guard under --force rests on the early guard's predicted render
            # matching the real one, which holds because create_seed_graph mints fixed IDs.
            # Defence in depth: the early guard compared against a PREDICTED render; this
            # one compares against the real one. (Under --force the early guard already
            # backed the file up.)
            _claude_md_guard(
                CLAUDE_HOME / "CLAUDE.md",
                _render_template_text(TEMPLATES_DIR / "template.CLAUDE.md", substitutions),
                False,
                seeded_db=ENGRAM_HOME / "knowledge.db",
            )
        substitute_template(
            TEMPLATES_DIR / "template.CLAUDE.md",
            CLAUDE_HOME / "CLAUDE.md",
            substitutions,
        )
        print(f"  CLAUDE.md → {CLAUDE_HOME}/CLAUDE.md", flush=True)

        _set_cleanup_period_days(CLAUDE_HOME)

    elif TARGET == "codex":
        _render_codex_target(substitutions)


if __name__ == "__main__":
    main()
