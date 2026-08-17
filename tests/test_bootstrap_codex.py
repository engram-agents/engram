"""Tests for bootstrap.py codex target and Component A no-compact-section guard.

Covers:
- Claude no-compact-section guard: engram-alpha#2067 removed the
  ``## Compact Instructions`` section from the Claude template entirely (see
  docs/compaction-continuity.md). This is the durable regression guard for
  Component A — it must not silently come back (see test docstring for
  lifecycle note).
- Codex target renders all three files with correct content, including the
  compact-instructions.md content that now feeds Codex exclusively.
- config.toml merge idempotency: run bootstrap-codex twice; identity keys
  present once, no duplication; pre-existing plugin stanzas preserved.

All tests use temp dirs — NEVER touch real ~/.engram or ~/.codex.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "src" / "engram" / "templates"


def _bootstrap_env(
    workdir: Path,
    target: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict for a bootstrap.py subprocess run."""
    engram_home = workdir / ".engram"
    codex_home = workdir / ".codex"
    claude_home = workdir / ".claude"

    # Mirror what install.sh does: copy runtime files into engram_home
    engram_home.mkdir(parents=True, exist_ok=True)
    runtime_files = [
        "server.py", "engram_client.py", "engram_confidence.py",
        "engram_filter.py", "engram_idf.py", "engram_ids.py",
        "engram_log_emitter.py", "engram_log_indexer.py",
    ]
    for fname in runtime_files:
        src = REPO_ROOT / "src" / "engram" / fname
        if src.exists():
            shutil.copy2(src, engram_home / fname)

    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(TEMPLATES_DIR),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(claude_home),
        "CODEX_HOME": str(codex_home),
        "PYTHON_BIN": sys.executable,
        "TARGET": target,
    })
    env.pop("ENGRAM_BOOTSTRAP", None)
    if extra:
        env.update(extra)
    return env


def _run_bootstrap(workdir: Path, target: str, extra: dict[str, str] | None = None):
    """Run bootstrap.py and return the CompletedProcess."""
    env = _bootstrap_env(workdir, target, extra)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Component A: Claude no-compact-section guard (engram-alpha#2067)
# ---------------------------------------------------------------------------


def test_claude_template_has_no_compact_section():
    """template.CLAUDE.md must not carry the ## Compact Instructions section.

    Lifecycle note: this slot previously guarded the OPPOSITE property — that
    the {{COMPACT_INSTRUCTIONS}} marker was present and got folded with
    compact-instructions.md into the Claude render (PR #818, then #1193's
    SSoT fold). engram-alpha#2067 removed the ``## Compact Instructions``
    section from the Claude template entirely: it was measured redundant
    with the warm-briefing template and inert outside the summarizer (see
    docs/compaction-continuity.md for the full argument). This test now
    guards the new invariant — that removal doesn't silently regress — while
    ``compact-instructions.md`` itself keeps shipping as the Codex-only
    compact-prompt source (see test_codex_bootstrap_renders_three_files).

    Asserts (against current HEAD files):
    1. templates/template.CLAUDE.md contains no {{COMPACT_INSTRUCTIONS}}
       marker.
    2. templates/template.CLAUDE.md contains no "## Compact Instructions"
       header.
    3. templates/compact-instructions.md still exists and is non-empty (it
       ships — Codex depends on it).
    4. An end-to-end Claude bootstrap run produces a ~/.claude/CLAUDE.md with
       neither the marker nor the header.
    """
    # 1 + 2. Template source has no marker, no header.
    template_path = TEMPLATES_DIR / "template.CLAUDE.md"
    assert template_path.exists(), f"template.CLAUDE.md not found at {template_path}"
    template_text = template_path.read_text()
    assert "{{COMPACT_INSTRUCTIONS}}" not in template_text, (
        "template.CLAUDE.md still contains a {{COMPACT_INSTRUCTIONS}} marker — "
        "engram-alpha#2067 removed the Claude-side fold; the marker should be gone."
    )
    assert "## Compact Instructions" not in template_text, (
        "template.CLAUDE.md still contains a '## Compact Instructions' header — "
        "engram-alpha#2067 removed this section from the Claude template. "
        "Read docs/compaction-continuity.md before adding it back."
    )

    # 3. compact-instructions.md still ships (Codex-only now).
    compact_path = TEMPLATES_DIR / "compact-instructions.md"
    assert compact_path.exists(), (
        f"compact-instructions.md not found at {compact_path} — it must keep "
        "shipping; it is now the Codex-only compact-prompt source."
    )
    compact_body = compact_path.read_text()
    assert len(compact_body.strip()) > 0, (
        "compact-instructions.md is empty — the Codex compact prompt would be empty"
    )

    # 4. End-to-end: a Claude bootstrap run must not resurrect either string.
    workdir = Path(tempfile.mkdtemp(prefix="engram_claude_no_compact_"))
    try:
        result = _run_bootstrap(workdir, "claude")
        assert result.returncode == 0, (
            f"claude bootstrap failed:\n{result.stdout}\n{result.stderr}"
        )
        claude_md = (workdir / ".claude" / "CLAUDE.md").read_text()
        assert "{{COMPACT_INSTRUCTIONS}}" not in claude_md, (
            "Rendered ~/.claude/CLAUDE.md still contains a raw "
            "{{COMPACT_INSTRUCTIONS}} marker."
        )
        assert "## Compact Instructions" not in claude_md, (
            "Rendered ~/.claude/CLAUDE.md still contains a '## Compact Instructions' "
            "section."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Component B: Codex target renders all three files
# ---------------------------------------------------------------------------


def test_codex_bootstrap_renders_three_files():
    """codex target renders:
    - ~/.engram/codex-compact-prompt.md (compact content)
    - ~/.codex/config.toml (instructions= with identity content, no compact section)
    - ~/.codex/AGENTS.md (project conventions placeholder)

    Verifications:
    - instructions= has identity content (collaborator/authority/who-I-am)
    - instructions= does NOT contain the ## Compact Instructions section
    - compact file contains compact content
    - seed-IDs + ENGRAM_HOME are resolved; {{AGENT_NAME}}/{{USER_NAME}} remain
    - seed-manifest.json has "target": "codex"
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_codex_render_"))
    try:
        result = _run_bootstrap(workdir, "codex")
        assert result.returncode == 0, (
            f"bootstrap.py codex target failed:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

        engram_home = workdir / ".engram"
        codex_home = workdir / ".codex"

        # --- codex-compact-prompt.md ---
        compact_path = engram_home / "codex-compact-prompt.md"
        assert compact_path.exists(), "codex-compact-prompt.md was not created"
        compact_content = compact_path.read_text()

        # Must contain compact instructions content — read the source file
        # at test time (not a hardcoded literal) so this cannot go stale
        # when the wording of compact-instructions.md changes. The seed-ID/
        # ENGRAM_HOME substitutions don't touch this file's content (it has
        # no such placeholders), so the raw source text lands unchanged.
        compact_source = (TEMPLATES_DIR / "compact-instructions.md").read_text()
        assert compact_source.strip() in compact_content, (
            "codex-compact-prompt.md missing compact instructions content "
            "(source file body not found verbatim in rendered output)"
        )
        # Must contain the "read warm-briefing + history before the task"
        # directive — the specific behavioral requirement the old "FIRST
        # ACTION" sentinel guarded. Restated in v2 wording as the "Do these
        # two reads first" block in compact-instructions.md; guarded here as
        # its own phrase (distinct from the full-body check above) because
        # it is the one durable, specific-content property worth naming.
        assert "Do these two reads first" in compact_content, (
            "codex-compact-prompt.md missing the read-before-task directive "
            "('Do these two reads first' block)"
        )

        # {{AGENT_NAME}} must remain (first-session's job)
        assert "{{AGENT_NAME}}" in compact_content, (
            "codex-compact-prompt.md: {{AGENT_NAME}} was resolved but should "
            "remain for first-session"
        )

        # Seed IDs and ENGRAM_HOME must be resolved (no raw placeholders for these)
        assert "{{AX_HONESTY}}" not in compact_content, (
            "codex-compact-prompt.md: {{AX_HONESTY}} was not resolved"
        )

        # --- config.toml ---
        config_path = codex_home / "config.toml"
        assert config_path.exists(), "config.toml was not created"
        config_content = config_path.read_text()

        # Must have instructions= multiline field
        assert 'instructions = """' in config_content, (
            "config.toml missing instructions= TOML field"
        )

        # Must have experimental_compact_prompt_file
        assert "experimental_compact_prompt_file" in config_content, (
            "config.toml missing experimental_compact_prompt_file"
        )

        # Must have model comment
        assert "# model = " in config_content, (
            "config.toml missing # model = comment"
        )

        # Instructions= must contain identity content (collaborator, authority)
        assert "My Collaborator" in config_content, (
            "config.toml instructions= missing 'My Collaborator' section"
        )
        assert "Authority Structure" in config_content, (
            "config.toml instructions= missing 'Authority Structure' section"
        )
        assert "Who I Am" in config_content, (
            "config.toml instructions= missing 'Who I Am' section"
        )

        # Instructions= must NOT contain the compact section
        assert "## Compact Instructions" not in config_content, (
            "config.toml instructions= must NOT contain ## Compact Instructions "
            "(that content belongs in codex-compact-prompt.md)"
        )
        assert compact_source.strip() not in config_content, (
            "config.toml instructions= must NOT contain compact instructions body"
        )

        # CLAUDE_PLUGIN_ROOT → CODEX_PLUGIN_ROOT adaptation
        assert "CODEX_PLUGIN_ROOT" in config_content, (
            "config.toml missing CODEX_PLUGIN_ROOT (adaptation not applied)"
        )
        assert "CLAUDE_PLUGIN_ROOT" not in config_content, (
            "config.toml still references CLAUDE_PLUGIN_ROOT (adaptation failed)"
        )

        # Seed IDs resolved in instructions=
        assert "{{AX_HONESTY}}" not in config_content, (
            "config.toml: {{AX_HONESTY}} was not resolved"
        )
        assert "{{ENGRAM_HOME}}" not in config_content, (
            "config.toml: {{ENGRAM_HOME}} was not resolved"
        )

        # {{AGENT_NAME}} and {{USER_NAME}} must remain for first-session
        assert "{{AGENT_NAME}}" in config_content, (
            "config.toml: {{AGENT_NAME}} should remain for first-session"
        )
        assert "{{USER_NAME}}" in config_content, (
            "config.toml: {{USER_NAME}} should remain for first-session"
        )

        # --- AGENTS.md ---
        agents_path = codex_home / "AGENTS.md"
        assert agents_path.exists(), "AGENTS.md was not created"
        agents_content = agents_path.read_text()

        assert "{{AGENT_NAME}}" in agents_content, (
            "AGENTS.md: {{AGENT_NAME}} should remain for first-session"
        )

        # --- seed-manifest.json has target field ---
        manifest_path = engram_home / "seed-manifest.json"
        assert manifest_path.exists(), "seed-manifest.json not found"
        manifest = json.loads(manifest_path.read_text())
        assert manifest.get("target") == "codex", (
            f"seed-manifest.json 'target' field is {manifest.get('target')!r}, "
            f"expected 'codex'"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Component B: config.toml merge idempotency
# ---------------------------------------------------------------------------


def test_codex_config_toml_merge_idempotency():
    """Running bootstrap codex twice must not duplicate identity keys.

    First run: creates config.toml with identity block.
    Second run (FORCE=1): updates identity block in place; no duplication.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_codex_idempotent_"))
    try:
        # First run
        result = _run_bootstrap(workdir, "codex")
        assert result.returncode == 0, (
            f"First codex bootstrap failed:\n{result.stdout}\n{result.stderr}"
        )

        config_path = workdir / ".codex" / "config.toml"
        assert config_path.exists()

        first_content = config_path.read_text()
        assert first_content.count('instructions = """') == 1, (
            "After first run: instructions= appears more than once"
        )
        assert first_content.count("experimental_compact_prompt_file") == 1, (
            "After first run: experimental_compact_prompt_file appears more than once"
        )

        # Remove the knowledge.db so bootstrap can run again (FORCE bypasses the
        # overwrite-refusal guard for the DB; config.toml is separate).
        db_path = workdir / ".engram" / "knowledge.db"
        db_path.unlink(missing_ok=True)

        # Second run (FORCE=1 so the DB check is bypassed)
        result2 = _run_bootstrap(workdir, "codex", extra={"FORCE": "1"})
        assert result2.returncode == 0, (
            f"Second codex bootstrap failed:\n{result2.stdout}\n{result2.stderr}"
        )

        second_content = config_path.read_text()
        assert second_content.count('instructions = """') == 1, (
            "After second run: instructions= duplicated in config.toml"
        )
        assert second_content.count("experimental_compact_prompt_file") == 1, (
            "After second run: experimental_compact_prompt_file duplicated"
        )
        assert second_content.count("# model = ") == 1, (
            "After second run: # model = comment duplicated"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_codex_config_toml_preserves_preexisting_stanzas():
    """Pre-existing plugin stanzas in config.toml must survive bootstrap.

    Simulates the case where 'codex plugin add' ran BEFORE bootstrap (or ran
    in between bootstrap runs), appending plugin/marketplace stanzas to the
    file. Bootstrap must preserve those stanzas.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_codex_stanza_"))
    try:
        # Create a pre-existing config.toml with plugin stanzas
        codex_home = workdir / ".codex"
        codex_home.mkdir(parents=True)
        preexisting_stanzas = (
            "\n[marketplaces.engram-local]\n"
            'source = "file:///home/user/.engram/marketplace"\n'
            "\n[plugins.engram]\n"
            "enabled = true\n"
            "\n[hooks.state.engram-session-start-hook]\n"
            "enabled = true\n"
        )
        (codex_home / "config.toml").write_text(preexisting_stanzas)

        # Run bootstrap (FORCE=1 not needed since no DB exists yet)
        result = _run_bootstrap(workdir, "codex")
        assert result.returncode == 0, (
            f"codex bootstrap failed:\n{result.stdout}\n{result.stderr}"
        )

        config_content = (codex_home / "config.toml").read_text()

        # Identity keys must be present
        assert 'instructions = """' in config_content, (
            "config.toml missing instructions= after merge with pre-existing stanzas"
        )
        assert "experimental_compact_prompt_file" in config_content, (
            "config.toml missing experimental_compact_prompt_file"
        )

        # Pre-existing stanzas must be preserved
        assert "[marketplaces.engram-local]" in config_content, (
            "Pre-existing [marketplaces.engram-local] stanza was destroyed by bootstrap"
        )
        assert "[plugins.engram]" in config_content, (
            "Pre-existing [plugins.engram] stanza was destroyed by bootstrap"
        )
        assert "[hooks.state.engram-session-start-hook]" in config_content, (
            "Pre-existing [hooks.state.*] stanza was destroyed by bootstrap"
        )

        # Identity keys must appear only once (not duplicated with pre-existing)
        assert config_content.count('instructions = """') == 1, (
            "instructions= appears more than once after merge"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Component B: Claude target still writes seed-manifest.json with target field
# ---------------------------------------------------------------------------


def test_claude_bootstrap_manifest_has_target_field():
    """seed-manifest.json written by claude bootstrap must include 'target': 'claude'."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_claude_manifest_"))
    try:
        result = _run_bootstrap(workdir, "claude")
        assert result.returncode == 0, (
            f"claude bootstrap failed:\n{result.stdout}\n{result.stderr}"
        )

        manifest_path = workdir / ".engram" / "seed-manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest.get("target") == "claude", (
            f"seed-manifest.json 'target' should be 'claude', "
            f"got {manifest.get('target')!r}"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
