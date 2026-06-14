"""Tests for bootstrap.py codex target and Component A substitution guard.

Covers:
- Claude compact-instructions substitution-integrity guard: the
  {{COMPACT_INSTRUCTIONS}} marker is fully resolved after rendering and the
  compact body actually lands in the output. This is the durable regression
  guard for Component A (see test docstring for lifecycle note).
- Codex target renders all three files with correct content.
- config.toml merge idempotency: run bootstrap-codex twice; identity keys
  present once, no duplication; pre-existing plugin stanzas preserved.

All tests use temp dirs — NEVER touch real ~/.engram or ~/.codex.
"""

from __future__ import annotations

import json
import os
import re
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
# Component A: Claude compact-instructions substitution-integrity guard
# ---------------------------------------------------------------------------


def test_claude_compact_instructions_substitution_integrity():
    """Substitution-integrity guard for the {{COMPACT_INSTRUCTIONS}} marker.

    Lifecycle note: the original test in this slot verified byte-identical
    rendering before and after the compact-instructions.md extraction (PR #818)
    by fetching the pre-extraction template from origin/dev. That check was a
    ONE-TIME guard verified at PR #818 review (losslessness confirmed: old
    length 32143 == new length 32143, equal). It is intentionally NOT retained
    here because:
      1. CI-fragility: actions/checkout@v4 without fetch-depth:0 leaves
         origin/dev as an unreachable ref in CI; git show returns non-zero and
         the test errors before it can assert anything meaningful.
      2. Tautology post-merge: once #818 merges to dev, origin/dev ITSELF
         carries the {{COMPACT_INSTRUCTIONS}} marker, so the "pre-PR path"
         would carry an unresolved marker while the "post-PR path" substitutes
         it — they diverge as a false negative, not as a real regression.

    This test now guards the DURABLE property: the marker substitution stays
    intact across all future changes to the template or the extracted file.
    It requires no git dependency and runs cleanly in CI.

    Asserts (against current HEAD files):
    1. templates/template.CLAUDE.md contains exactly one {{COMPACT_INSTRUCTIONS}}
       marker before substitution.
    2. templates/compact-instructions.md exists and is non-empty.
    3. After substitution, the rendered output contains NO literal
       {{COMPACT_INSTRUCTIONS}} (marker fully resolved).
    4. The rendered output contains the sentinel substring
       "This section tells the pre-compaction agent how to write the summary."
       — a stable, placeholder-free line from the compact body, proving the
       content actually landed in the render.
    5. The ## Compact Instructions header line is present in the rendered output
       (the header stays in the template; only the body moved to the file).
    """
    # 1. Template contains exactly one marker
    template_path = TEMPLATES_DIR / "template.CLAUDE.md"
    assert template_path.exists(), f"template.CLAUDE.md not found at {template_path}"
    template_text = template_path.read_text()
    marker_count = template_text.count("{{COMPACT_INSTRUCTIONS}}")
    assert marker_count == 1, (
        f"Expected exactly 1 {{{{COMPACT_INSTRUCTIONS}}}} marker in "
        f"template.CLAUDE.md, found {marker_count}"
    )

    # 2. compact-instructions.md exists and is non-empty
    compact_path = TEMPLATES_DIR / "compact-instructions.md"
    assert compact_path.exists(), (
        f"compact-instructions.md not found at {compact_path}"
    )
    compact_body = compact_path.read_text()
    assert len(compact_body.strip()) > 0, (
        "compact-instructions.md is empty — substitution would produce a gap"
    )

    # 3 + 4 + 5. Apply substitutions and verify rendered output
    dummy_subs = {
        "{{AX_HONESTY}}": "ax_0001",
        "{{AX_HONESTY_DISCRETION}}": "ax_0002",
        "{{AX_PROVENANCE}}": "ax_0003",
        "{{DF_ENGRAM}}": "df_0001",
        "{{DF_EPISTEMIC_IDENTITY}}": "df_0002",
        "{{GL_EPISTEMIC_HUMILITY}}": "gl_0001",
        "{{ENGRAM_HOOKS_DIR}}": "/tmp/test/.engram/hooks",
        "{{ENGRAM_HOME}}": "/tmp/test/.engram",
        "{{PYTHON}}": "python3",
        "{{COMPACT_INSTRUCTIONS}}": compact_body,
    }

    rendered = template_text
    for k, v in dummy_subs.items():
        rendered = rendered.replace(k, v)

    # 3. No unreplaced marker remains
    assert "{{COMPACT_INSTRUCTIONS}}" not in rendered, (
        "{{COMPACT_INSTRUCTIONS}} marker was NOT substituted — "
        "substitution machinery is broken or the marker was duplicated"
    )

    # 4. Sentinel from the compact body is present (proves content landed)
    sentinel = "This section tells the pre-compaction agent how to write the summary."
    assert sentinel in rendered, (
        f"Compact-instructions sentinel not found in rendered output.\n"
        f"Sentinel: {sentinel!r}\n"
        f"This means the compact body did NOT land in the render — "
        f"the {{{{COMPACT_INSTRUCTIONS}}}} substitution produced an empty or "
        f"wrong result, or the sentinel line was removed from compact-instructions.md."
    )

    # 5. ## Compact Instructions header is present (header stays in the template)
    assert "## Compact Instructions" in rendered, (
        "'## Compact Instructions' header not found in rendered output. "
        "The header should remain in the template even though the body "
        "was extracted to compact-instructions.md."
    )

    # 6. Source-of-truth breadcrumb: present in the template (so template editors
    #    know the real content lives in compact-instructions.md), carried by a
    #    matching strip entry in bootstrap.py, and GONE once stripped (so it never
    #    leaks into the rendered ~/.claude/CLAUDE.md or the Codex prompt). This is
    #    the drift guard: if the template wording and the bootstrap strip ever
    #    diverge, the breadcrumb survives the strip and this test fails loudly.
    m = re.search(
        r"<!-- Source of truth: compact-instructions\.md.*?-->", template_text
    )
    assert m, (
        "Source-of-truth breadcrumb comment missing from template.CLAUDE.md — "
        "template editors lose the pointer to compact-instructions.md."
    )
    breadcrumb = m.group(0)
    bootstrap_src = (
        REPO_ROOT / "src" / "engram" / "bootstrap.py"
    ).read_text()
    assert breadcrumb in bootstrap_src, (
        "bootstrap.py has no strip entry matching the template breadcrumb "
        f"({breadcrumb!r}) — it would leak into the rendered install. Keep the "
        "strip string in bootstrap.py identical to the template line."
    )
    stripped = rendered.replace(breadcrumb + "\n", "")
    assert breadcrumb not in stripped, (
        "Breadcrumb survived the strip — the rendered CLAUDE.md would carry it."
    )


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

        # Must contain compact instructions content
        assert "pre-compaction agent" in compact_content, (
            "codex-compact-prompt.md missing compact instructions content"
        )
        assert "FIRST ACTION" in compact_content, (
            "codex-compact-prompt.md missing FIRST ACTION compact block"
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
        assert "pre-compaction agent" not in config_content, (
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
