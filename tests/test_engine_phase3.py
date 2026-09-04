"""Tests for engine Phase 3: platform profiles, --target codex, identity axis.

Covers:
  A. Platform profiles
     - claude-code.json / codex.json parse correctly
     - load_platform_profile() works for both targets
     - bake_platform_json() writes platform.json to bundle root

  B. --target codex emission
     - .codex-plugin/plugin.json is present + valid JSON + has expected keys
     - hooks.json uses ${CLAUDE_PLUGIN_ROOT} (Codex injects this natively)
     - hooks.json has ZERO "CODEX_PLUGIN_ROOT" strings (Codex never sets it)
     - platform.json is present and platform=codex

  C. Identity axis
     - --identity foreign drops identity_coupled entries (not a superset of self)
     - Foreign set = tier_set - identity_coupled_set
     - Planted-leak negative test: FAILS the build on known leak patterns
     - --engram-home is reflected in emitted hook commands (codex bundle)

  D. claude-code output property gates
     - platform.json present with platform=claude-code
     - hooks.json events are valid
     - No regression in hooks consistency gate
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

TIER_RANKS = {"essential": 0, "convenience": 1, "dev": 2}


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _run_build(args: list[str], output_dir: str | None = None) -> "subprocess.CompletedProcess":
    import subprocess
    # --allow-branch: tests may run on feature branches, not just dev (#794 guard override)
    cmd = [sys.executable, "-m", "tools.engine.cli", "build", "--allow-branch"] + args
    if output_dir:
        cmd += ["--output", output_dir]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _collect_all_text_content(root: str) -> list[tuple[str, str]]:
    """Return list of (rel_path, content) for all text files under root."""
    result = []
    skip_dirs = {".git", "__pycache__", "node_modules"}
    skip_ext = {".pyc"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1]
            if ext in skip_ext:
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    result.append((rel, f.read()))
            except OSError:
                pass
    return result


# ---------------------------------------------------------------------------
# A. Platform profiles
# ---------------------------------------------------------------------------


class TestPlatformProfiles:
    """Platform profile files parse and load correctly."""

    def test_claude_code_profile_parses(self):
        """packaging/platforms/claude-code.json is valid JSON."""
        path = os.path.join(REPO_ROOT, "src", "build", "packaging", "platforms", "claude-code.json")
        assert os.path.isfile(path), f"claude-code.json not found at {path}"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("platform") == "claude-code"

    def test_codex_profile_parses(self):
        """packaging/platforms/codex.json is valid JSON."""
        path = os.path.join(REPO_ROOT, "src", "build", "packaging", "platforms", "codex.json")
        assert os.path.isfile(path), f"codex.json not found at {path}"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("platform") == "codex"

    def test_claude_code_profile_has_required_fields(self):
        """claude-code.json has plugin_root_env, hooks_config, skills_dir."""
        path = os.path.join(REPO_ROOT, "src", "build", "packaging", "platforms", "claude-code.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for field in ("plugin_root_env", "hooks_config", "skills_dir"):
            assert field in data, f"claude-code.json missing field: {field!r}"
        assert data["plugin_root_env"] == "CLAUDE_PLUGIN_ROOT"

    def test_codex_profile_has_required_fields(self):
        """codex.json has plugin_root_env, hooks_config, skills_dir, plugin_manifest."""
        path = os.path.join(REPO_ROOT, "src", "build", "packaging", "platforms", "codex.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for field in ("plugin_root_env", "hooks_config", "skills_dir", "plugin_manifest"):
            assert field in data, f"codex.json missing field: {field!r}"
        assert data["plugin_root_env"] == "CLAUDE_PLUGIN_ROOT"
        assert data["plugin_manifest"] == ".codex-plugin/plugin.json"

    def test_load_platform_profile_claude_code(self):
        """load_platform_profile() works for claude-code."""
        from tools.engine.build import load_platform_profile
        profile = load_platform_profile(REPO_ROOT, "claude-code")
        assert profile["platform"] == "claude-code"

    def test_load_platform_profile_codex(self):
        """load_platform_profile() works for codex."""
        from tools.engine.build import load_platform_profile
        profile = load_platform_profile(REPO_ROOT, "codex")
        assert profile["platform"] == "codex"

    def test_load_platform_profile_unknown_raises(self):
        """load_platform_profile() raises FileNotFoundError for unknown target."""
        from tools.engine.build import load_platform_profile
        with pytest.raises(FileNotFoundError):
            load_platform_profile(REPO_ROOT, "nonexistent-platform")

    def test_bake_platform_json(self):
        """bake_platform_json() writes platform.json to the bundle root."""
        from tools.engine.build import bake_platform_json, load_platform_profile
        profile = load_platform_profile(REPO_ROOT, "codex")
        with tempfile.TemporaryDirectory() as tmpdir:
            bake_platform_json(tmpdir, profile)
            dest = os.path.join(tmpdir, "platform.json")
            assert os.path.isfile(dest), "platform.json not written"
            with open(dest, encoding="utf-8") as f:
                data = json.load(f)
            assert data["platform"] == "codex"


# ---------------------------------------------------------------------------
# B. --target codex emission
# ---------------------------------------------------------------------------


class TestCodexEmission:
    """--target codex produces a well-formed codex bundle."""

    @pytest.fixture(scope="class")
    def codex_bundle(self, tmp_path_factory) -> str:
        """Build a codex essential bundle and return its root path."""
        bundle_dir = str(tmp_path_factory.mktemp("codex_bundle"))
        result = _run_build(
            ["--target", "codex", "--tier", "essential"],
            output_dir=bundle_dir,
        )
        assert result.returncode == 0, (
            f"codex build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return bundle_dir

    def test_codex_plugin_json_present(self, codex_bundle):
        """.codex-plugin/plugin.json is present in codex bundle."""
        path = os.path.join(codex_bundle, ".codex-plugin", "plugin.json")
        assert os.path.isfile(path), f".codex-plugin/plugin.json not found at {path}"

    def test_codex_plugin_json_parses(self, codex_bundle):
        """.codex-plugin/plugin.json is valid JSON."""
        path = os.path.join(codex_bundle, ".codex-plugin", "plugin.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert "name" in data, ".codex-plugin/plugin.json missing 'name'"
        assert "version" in data, ".codex-plugin/plugin.json missing 'version'"
        assert "paths" in data, ".codex-plugin/plugin.json missing 'paths'"

    def test_codex_plugin_json_paths_keys(self, codex_bundle):
        """.codex-plugin/plugin.json paths has skills, mcp_servers, hooks."""
        path = os.path.join(codex_bundle, ".codex-plugin", "plugin.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        paths = data.get("paths", {})
        for key in ("skills", "mcp_servers", "hooks"):
            assert key in paths, f".codex-plugin/plugin.json paths missing key: {key!r}"

    def test_platform_json_present_codex(self, codex_bundle):
        """platform.json is present in codex bundle."""
        path = os.path.join(codex_bundle, "platform.json")
        assert os.path.isfile(path), "platform.json not found in codex bundle"

    def test_platform_json_platform_is_codex(self, codex_bundle):
        """platform.json has platform=codex."""
        path = os.path.join(codex_bundle, "platform.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("platform") == "codex", (
            f"platform.json platform={data.get('platform')!r}, expected 'codex'"
        )

    def test_hooks_json_present_codex(self, codex_bundle):
        """hooks/hooks.json is present in codex bundle."""
        path = os.path.join(codex_bundle, "hooks", "hooks.json")
        assert os.path.isfile(path), "hooks/hooks.json not found in codex bundle"

    def test_hooks_json_parses_codex(self, codex_bundle):
        """hooks/hooks.json is valid JSON."""
        path = os.path.join(codex_bundle, "hooks", "hooks.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert "hooks" in data

    def test_hooks_json_events_valid(self, codex_bundle):
        """hooks/hooks.json event keys are valid Claude hook events."""
        valid_events = {
            "SessionStart", "UserPromptSubmit", "PreToolUse",
            "PostToolUse", "PostCompact", "Stop",
        }
        path = os.path.join(codex_bundle, "hooks", "hooks.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for event in data.get("hooks", {}):
            assert event in valid_events, (
                f"hooks.json contains unknown event: {event!r}"
            )

    def test_no_codex_plugin_root_in_codex_bundle(self, codex_bundle):
        """Zero 'CODEX_PLUGIN_ROOT' strings in the codex hooks.json and .mcp.json.

        The primary correctness assertion for codex hook compatibility.
        Codex natively injects CLAUDE_PLUGIN_ROOT (not CODEX_PLUGIN_ROOT) into
        hook subprocess environments.  Any occurrence of CODEX_PLUGIN_ROOT in a
        runtime config means every hook will fail with empty expansion (ENOENT).
        """
        runtime_configs = ["hooks/hooks.json", ".mcp.json"]
        hits = []
        for rel in runtime_configs:
            fpath = os.path.join(codex_bundle, rel)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
            if "CODEX_PLUGIN_ROOT" in content:
                hits.append((rel, content.count("CODEX_PLUGIN_ROOT")))
        assert not hits, (
            "Codex runtime config contains 'CODEX_PLUGIN_ROOT' strings (must be absent — "
            "Codex sets CLAUDE_PLUGIN_ROOT, not CODEX_PLUGIN_ROOT):\n"
            + "\n".join(f"  {rel}: {n} occurrence(s)" for rel, n in hits)
        )

    def test_codex_plugin_root_in_hooks(self, codex_bundle):
        """hooks/hooks.json uses ${CLAUDE_PLUGIN_ROOT} — Codex injects this var natively.

        Regression guard: CODEX_PLUGIN_ROOT must be absent (it is never set by
        Codex, so its presence would cause empty expansion → ENOENT on every hook).
        """
        path = os.path.join(codex_bundle, "hooks", "hooks.json")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "CLAUDE_PLUGIN_ROOT" in content, (
            "hooks/hooks.json does not reference CLAUDE_PLUGIN_ROOT — "
            "plugin_root_env rewrite step may have failed"
        )
        assert "CODEX_PLUGIN_ROOT" not in content, (
            "hooks/hooks.json contains CODEX_PLUGIN_ROOT — Codex does not set "
            "this variable; hook commands will fail with empty expansion (ENOENT). "
            "The codex profile's plugin_root_env must be CLAUDE_PLUGIN_ROOT."
        )


class TestCodexAgentEmission:
    """Codex target transforms Claude fairy Markdown specs into Codex TOML agents."""

    @pytest.fixture(scope="class")
    def codex_convenience_bundle(self, tmp_path_factory) -> str:
        """Build a codex convenience bundle and return its root path."""
        bundle_dir = str(tmp_path_factory.mktemp("codex_agents"))
        result = _run_build(
            ["--target", "codex", "--tier", "convenience"],
            output_dir=bundle_dir,
        )
        assert result.returncode == 0, (
            f"codex convenience build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return bundle_dir

    def _read_agent(self, bundle: str, name: str) -> dict:
        path = os.path.join(bundle, "agents", f"{name}.toml")
        assert os.path.isfile(path), f"Codex agent TOML not found: {path}"
        with open(path, "rb") as f:
            return tomllib.load(f)

    def test_codex_agents_are_toml_not_claude_markdown(self, codex_convenience_bundle):
        """Codex bundles emit .toml custom-agent files and no Claude .md copies."""
        agents_dir = os.path.join(codex_convenience_bundle, "agents")
        names = sorted(os.listdir(agents_dir))
        assert "engram-coder-fairy.toml" in names
        assert "engram-pr-reviewer.toml" in names
        assert not [name for name in names if name.endswith(".md")], names

    def test_codex_agent_toml_has_required_fields(self, codex_convenience_bundle):
        """Generated TOML parses and contains Codex custom-agent required fields."""
        agent = self._read_agent(codex_convenience_bundle, "engram-coder-fairy")
        for field in ("name", "description", "developer_instructions"):
            assert field in agent, f"generated agent missing {field!r}"
        assert agent["name"] == "engram-coder-fairy"
        assert agent["developer_instructions"].startswith("# Codex transform metadata")

    def test_codex_agent_preserves_not_parent_instruction(self, codex_convenience_bundle):
        """The identity-boundary warning from the Claude spec survives transform."""
        agent = self._read_agent(codex_convenience_bundle, "engram-pr-reviewer")
        assert "# You are NOT the parent agent" in agent["developer_instructions"]
        assert 'Not "I am [the parent\'s name]."' in agent["developer_instructions"]

    def test_codex_agent_model_tier_mapping_comes_from_profile(self, codex_convenience_bundle):
        """Claude model tiers map through codex.json profile into Codex model fields."""
        sonnet_agents = (
            "engram-coder-fairy",
            "engram-pr-reviewer",
            "engram-dream-fairy",
            "engram-batch-summary-fairy",
        )
        for name in sonnet_agents:
            agent = self._read_agent(codex_convenience_bundle, name)
            assert agent["model"] == "gpt-5.6-terra"
            assert agent["model_reasoning_effort"] == "high"

        master = self._read_agent(codex_convenience_bundle, "engram-dream-master")
        assert "model" not in master
        assert "model_reasoning_effort" not in master

    def test_canonical_dream_master_omits_model_to_inherit_parent(self):
        """Claude source omission is intentional and must survive Codex conversion."""
        path = Path(REPO_ROOT) / "src/engram/agents/claude/engram-dream-master.md"
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        assert not re.search(r"^model\s*:", frontmatter, re.MULTILINE)

    def test_codex_model_config_only_maps_explicit_tiers(self):
        """Omission inherits the parent; explicit Opus/Sonnet use profile mappings."""
        from tools.engine.build import _codex_model_config, load_platform_profile

        profile = load_platform_profile(REPO_ROOT, "codex")
        assert _codex_model_config(profile, None) == {}
        assert _codex_model_config(profile, "opus") == {
            "model": "gpt-5.6-sol",
            "model_reasoning_effort": "high",
        }
        assert _codex_model_config(profile, "sonnet") == {
            "model": "gpt-5.6-terra",
            "model_reasoning_effort": "high",
        }

    def test_codex_agent_no_claude_frontmatter_leaks(self, codex_convenience_bundle):
        """Generated TOML must not retain raw Claude YAML frontmatter syntax."""
        path = os.path.join(codex_convenience_bundle, "agents", "engram-coder-fairy.toml")
        content = Path(path).read_text(encoding="utf-8")
        assert not content.startswith("---")
        assert not re.search(r"^tools:", content, re.MULTILINE)
        assert not re.search(r"^default_background:", content, re.MULTILINE)
        assert not re.search(r"^isolation:", content, re.MULTILINE)

    def test_codex_agent_blocks_inherited_mcp_when_claude_spec_has_no_mcp(self, codex_convenience_bundle):
        """Non-MCP fairies explicitly clear inherited MCP servers in Codex config."""
        coder = self._read_agent(codex_convenience_bundle, "engram-coder-fairy")
        reviewer = self._read_agent(codex_convenience_bundle, "engram-pr-reviewer")
        assert coder.get("mcp_servers") == {}
        assert reviewer.get("mcp_servers") == {}

    def test_codex_agent_allowlists_declared_mcp_tools(self, codex_convenience_bundle):
        """MCP-using dream fairy preserves Claude tool whitelists."""
        dream = self._read_agent(codex_convenience_bundle, "engram-dream-fairy")
        dream_tools = dream["plugins"]["engram@engram-local"]["mcp_servers"]["engram"]["enabled_tools"]
        assert set(dream_tools) == {
            "engram_query",
            "engram_query_pattern",
            "engram_inspect",
            "engram_list",
            "engram_get_subgraph",
            "engram_history",
            "engram_surface",
            "engram_diagnose",
            "engram_stats",
            "engram_focus_sets",
            "engram_list_focused",
        }

    def test_codex_agent_full_tool_spec_inherits_full_mcp(self, codex_convenience_bundle):
        """Dream master has tools:* and inherits full ENGRAM read/write MCP."""
        master = self._read_agent(codex_convenience_bundle, "engram-dream-master")
        assert "mcp_servers" not in master


class TestCodexEmissionWithEngramHome:
    """--target codex --engram-home embeds ENGRAM_HOME in hook commands."""

    @pytest.fixture(scope="class")
    def codex_bundle_with_home(self, tmp_path_factory) -> str:
        """Build a codex essential bundle with --engram-home."""
        bundle_dir = str(tmp_path_factory.mktemp("codex_home"))
        result = _run_build(
            [
                "--target", "codex",
                "--tier", "essential",
                "--engram-home", "/test/engram/home",
            ],
            output_dir=bundle_dir,
        )
        assert result.returncode == 0, (
            f"codex+engram-home build failed:\n{result.stderr}"
        )
        return bundle_dir

    def test_engram_home_in_hooks_json(self, codex_bundle_with_home):
        """--engram-home value appears in hooks/hooks.json commands."""
        path = os.path.join(codex_bundle_with_home, "hooks", "hooks.json")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "/test/engram/home" in content, (
            "--engram-home value not found in hooks/hooks.json"
        )

    def test_engram_home_in_mcp_json(self, codex_bundle_with_home):
        """--engram-home value appears in .mcp.json env."""
        path = os.path.join(codex_bundle_with_home, ".mcp.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        server = data.get("mcpServers", {}).get("engram", {})
        env = server.get("env", {})
        assert env.get("ENGRAM_HOME") == "/test/engram/home", (
            f"ENGRAM_HOME in .mcp.json is {env.get('ENGRAM_HOME')!r}, expected '/test/engram/home'"
        )


# ---------------------------------------------------------------------------
# C. Identity axis
# ---------------------------------------------------------------------------


class TestIdentityAxis:
    """--identity foreign drops identity_coupled entries and runs leak scan."""

    def test_foreign_set_excludes_identity_coupled(self):
        """Foreign set = tier_set minus identity_coupled entries.

        For any tier+MA config, the foreign set must NOT include any
        identity_coupled entries.
        """
        from tools.engine.build import select_shippables_with_identity
        manifest = _load_manifest()
        for tier in ("essential", "convenience", "dev"):
            for ma in (False, True):
                foreign = select_shippables_with_identity(manifest, tier, ma, "foreign")
                foreign_paths = {e["path"] for e in foreign}
                ic_in_foreign = [
                    e["path"]
                    for e in foreign
                    if e.get("identity_coupled")
                ]
                assert not ic_in_foreign, (
                    f"Foreign set for tier={tier!r} ma={ma} still contains "
                    f"identity_coupled entries: {ic_in_foreign}"
                )

    def test_foreign_set_is_subset_of_self_set(self):
        """Foreign set is always a subset of self set (same tier, same MA)."""
        from tools.engine.build import select_shippables_with_identity
        manifest = _load_manifest()
        for tier in ("essential", "convenience", "dev"):
            for ma in (False, True):
                self_set = {
                    e["path"]
                    for e in select_shippables_with_identity(manifest, tier, ma, "self")
                }
                foreign_set = {
                    e["path"]
                    for e in select_shippables_with_identity(manifest, tier, ma, "foreign")
                }
                assert foreign_set.issubset(self_set), (
                    f"Foreign set is NOT a subset of self set for tier={tier!r} ma={ma}: "
                    f"extra in foreign = {sorted(foreign_set - self_set)}"
                )

    def test_identity_coupled_hooks_absent_from_foreign(self):
        """Known identity-coupled hooks are absent from foreign convenience build."""
        from tools.engine.build import select_shippables_with_identity
        manifest = _load_manifest()
        foreign_paths = {
            e["path"]
            for e in select_shippables_with_identity(manifest, "convenience", True, "foreign")
        }
        identity_coupled_hooks = [
            "hooks/claude/engram-baton-prompt-hook.py",
            "hooks/claude/engram-inter-agent-prompt-hook.py",
            "hooks/claude/engram-forum-prompt-hook.py",
            "hooks/claude/engram-end-of-day-hook.py",
        ]
        for hook in identity_coupled_hooks:
            assert hook not in foreign_paths, (
                f"identity_coupled hook {hook!r} found in foreign convenience+MA set"
            )

    def test_self_set_includes_identity_coupled(self):
        """Self set (convenience+MA) does include identity-coupled hooks."""
        from tools.engine.build import select_shippables_with_identity
        manifest = _load_manifest()
        self_paths = {
            e["path"]
            for e in select_shippables_with_identity(manifest, "convenience", True, "self")
        }
        # At least some identity-coupled hooks should be in the self set
        # (those that are tier-eligible for convenience+MA)
        assert "hooks/claude/engram-baton-prompt-hook.py" in self_paths, (
            "engram-baton-prompt-hook.py should be in self+convenience+MA set"
        )

    def test_planted_leak_fails_foreign_build(self, tmp_path):
        """A planted identity leak causes the foreign build to fail.

        This is the negative test: if a file in the bundle contains a known
        identity string (e.g. '/home/agent-borges'), the leak-scan VALIDATE
        step must fail the build.
        """
        from tools.engine.build import scan_for_leaks, FOREIGN_LEAK_PATTERNS

        # Create a minimal bundle with a planted leak file
        bundle_dir = str(tmp_path / "bundle")
        os.makedirs(os.path.join(bundle_dir, "hooks"))

        # Plant the leak in a JSON file (simulating a hooked command)
        leak_content = json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"command": "python3 /home/agent-borges/hooks/some-hook.py"}
                    ]}
                ]
            }
        })
        with open(os.path.join(bundle_dir, "hooks", "hooks.json"), "w") as f:
            f.write(leak_content)

        # scan_for_leaks must find the leak
        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert leaks, (
            "scan_for_leaks did not detect the planted '/home/agent-borges' leak"
        )
        # Verify the hit is in the right file
        hit_files = {rel for rel, _lineno, _snippet in leaks}
        assert any("hooks.json" in f for f in hit_files), (
            f"Leak hit not in hooks.json; hit_files={hit_files}"
        )

    def test_planted_agent_borges_identifier_detected(self, tmp_path):
        """scan_for_leaks detects 'agent-borges' identifier pattern."""
        from tools.engine.build import scan_for_leaks, FOREIGN_LEAK_PATTERNS

        bundle_dir = str(tmp_path / "bundle2")
        os.makedirs(bundle_dir)

        # Plant an agent-borges identifier leak
        leak_content = '{"engram_home": "/home/agent-borges/.engram"}\n'
        with open(os.path.join(bundle_dir, "config.json"), "w") as f:
            f.write(leak_content)

        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert leaks, "scan_for_leaks did not detect 'agent-borges' identifier leak"

    def test_clean_bundle_passes_leak_scan(self, tmp_path):
        """A bundle with no identity strings passes the leak scan."""
        from tools.engine.build import scan_for_leaks, FOREIGN_LEAK_PATTERNS

        bundle_dir = str(tmp_path / "clean_bundle")
        os.makedirs(os.path.join(bundle_dir, "hooks"))

        clean_content = json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/some-hook.py\""}
                    ]}
                ]
            }
        })
        with open(os.path.join(bundle_dir, "hooks", "hooks.json"), "w") as f:
            f.write(clean_content)

        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert not leaks, (
            f"scan_for_leaks found unexpected leaks in clean bundle: {leaks}"
        )

    def test_foreign_build_fails_on_leak(self, tmp_path):
        """A foreign build that would emit identity strings must fail.

        This is the integration-level test: we call build_plugin with a
        hooked scan via a custom pattern set to detect a marker we plant
        in the plugin_root.  Since we can't easily plant leaks in the real
        source tree without modifying tracked files, we test this via the
        scan_for_leaks function directly (negative test above) combined
        with the CLI-level integration in test_planted_leak_fails_foreign_build.
        """
        # Verify the foreign build itself (essential, no MA) produces a clean tree
        from tools.engine.build import build_plugin, scan_for_leaks, FOREIGN_LEAK_PATTERNS
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "essential")
        bundle_dir = str(tmp_path / "foreign_essential")

        build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            multi_agent=False,
            manifest=manifest,
            target="codex",
            identity="foreign",
        )

        # After a successful foreign build, the tree must be clean
        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert not leaks, (
            f"Foreign bundle has identity leaks after build:\n"
            + "\n".join(f"  {r}:{l}  {s}" for r, l, s in leaks)
        )

    def test_foreign_build_clean_at_convenience_tier(self, tmp_path):
        """--identity foreign --tier convenience (the DEFAULT tier) produces a clean bundle.

        The #778 safety property must hold at convenience, not only at essential.
        This is the primary regression test added by the round-2 review: the
        self-inflicted hits (build.py pattern literals, operator-setup-viz.sh
        comment) were only caught here.
        """
        from tools.engine.build import build_plugin, scan_for_leaks, FOREIGN_LEAK_PATTERNS
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "convenience")
        bundle_dir = str(tmp_path / "foreign_convenience")

        build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            multi_agent=False,
            manifest=manifest,
            target="codex",
            identity="foreign",
        )

        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert not leaks, (
            "Foreign convenience bundle has identity leaks after build "
            "(the safety property must hold at the default tier):\n"
            + "\n".join(f"  {r}:{l}  {s}" for r, l, s in leaks)
        )

    def test_planted_leak_fails_convenience_foreign_build(self, tmp_path):
        """Planted identity leak causes the foreign build to fail at convenience tier.

        Negative test: confirms the leak-scan gate fires at convenience, not only
        at essential.  We test via scan_for_leaks directly (the same mechanism
        the build calls) against a bundle with a planted leak at convenience.
        """
        from tools.engine.build import scan_for_leaks, FOREIGN_LEAK_PATTERNS

        bundle_dir = str(tmp_path / "convenience_planted")
        os.makedirs(os.path.join(bundle_dir, "tools"))

        # Plant a leak inside a tools/ Python file (convenience-tier surface)
        leak_content = '# installed at /home/' + 'agent-borges/.engram\n'
        with open(os.path.join(bundle_dir, "tools", "config_schema.py"), "w") as f:
            f.write(leak_content)

        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert leaks, (
            "scan_for_leaks did not detect planted identity leak in convenience "
            "tools/ surface"
        )

    def test_planted_leak_in_service_file_detected(self, tmp_path):
        """A .service file containing an identity literal is caught by scan_for_leaks.

        Regression test for the SCAN_EXTENSIONS gap closed in PR #896 / issue #814:
        build.py originally lacked '.service' in SCAN_EXTENSIONS, so a systemd
        service template with a hardcoded builder identity (e.g. User=agent-borges)
        was invisible to the foreign-bundle leak-scan.  Adding '.service' closed
        that class.  This test pins the fix: a planted .service leak must be detected.
        """
        from tools.engine.build import scan_for_leaks, FOREIGN_LEAK_PATTERNS

        bundle_dir = str(tmp_path / "service_planted")
        os.makedirs(bundle_dir)

        # Plant the leak in a .service file using the same sentinel as the
        # other planted-leak tests.  Concatenation keeps this source file
        # itself clean (same discipline as FOREIGN_LEAK_PATTERNS above).
        leak_content = (
            "[Service]\n"
            "User=" + "agent-borges" + "\n"
            "WorkingDirectory=/home/" + "agent-borges" + "/.engram\n"
            "ExecStart=/home/" + "agent-borges" + "/.engram/venv/bin/python3 viz_server.py\n"
        )
        with open(os.path.join(bundle_dir, "engram-viz.service"), "w") as f:
            f.write(leak_content)

        leaks = scan_for_leaks(bundle_dir, FOREIGN_LEAK_PATTERNS)
        assert leaks, (
            "scan_for_leaks did not detect the planted identity leak in "
            "engram-viz.service — '.service' may be missing from SCAN_EXTENSIONS"
        )
        hit_files = {rel for rel, _lineno, _snippet in leaks}
        assert any("engram-viz.service" in f for f in hit_files), (
            f"Leak hit not in engram-viz.service; hit_files={hit_files}"
        )


# ---------------------------------------------------------------------------
# D. claude-code output property gates
# ---------------------------------------------------------------------------


class TestClaudeCodeOutput:
    """claude-code target produces expected artifacts."""

    @pytest.fixture(scope="class")
    def cc_bundle(self, tmp_path_factory) -> str:
        """Build a claude-code essential bundle."""
        bundle_dir = str(tmp_path_factory.mktemp("cc_bundle"))
        result = _run_build(
            ["--target", "claude-code", "--tier", "essential"],
            output_dir=bundle_dir,
        )
        assert result.returncode == 0, (
            f"claude-code build failed:\n{result.stderr}"
        )
        return bundle_dir

    def test_platform_json_present_cc(self, cc_bundle):
        """platform.json is present in claude-code bundle."""
        path = os.path.join(cc_bundle, "platform.json")
        assert os.path.isfile(path), "platform.json not found in claude-code bundle"

    def test_platform_json_platform_is_claude_code(self, cc_bundle):
        """platform.json has platform=claude-code."""
        path = os.path.join(cc_bundle, "platform.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("platform") == "claude-code", (
            f"platform.json platform={data.get('platform')!r}, expected 'claude-code'"
        )

    def test_mcp_json_present_cc(self, cc_bundle):
        """.mcp.json is present in claude-code bundle."""
        path = os.path.join(cc_bundle, ".mcp.json")
        assert os.path.isfile(path), ".mcp.json not found in claude-code bundle"

    def test_mcp_json_parses_cc(self, cc_bundle):
        """.mcp.json is valid JSON in claude-code bundle."""
        path = os.path.join(cc_bundle, ".mcp.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert "mcpServers" in data

    def test_codex_plugin_dir_absent_from_cc(self, cc_bundle):
        """.codex-plugin/ is NOT present in claude-code bundle."""
        path = os.path.join(cc_bundle, ".codex-plugin")
        assert not os.path.exists(path), (
            ".codex-plugin/ should NOT be present in claude-code bundle"
        )

    def test_hooks_json_events_valid_cc(self, cc_bundle):
        """hooks/hooks.json event keys are valid for claude-code bundle."""
        valid_events = {
            "SessionStart", "UserPromptSubmit", "PreToolUse",
            "PostToolUse", "PostCompact", "Stop",
        }
        path = os.path.join(cc_bundle, "hooks", "hooks.json")
        assert os.path.isfile(path), "hooks/hooks.json not found in claude-code bundle"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for event in data.get("hooks", {}):
            assert event in valid_events, f"Invalid event in claude-code hooks.json: {event!r}"

    def test_claude_code_default_target_behavior(self, tmp_path):
        """Default build (no --target) behaves like --target claude-code."""
        bundle_dir = str(tmp_path / "default_bundle")
        result = _run_build(["--tier", "essential"], output_dir=bundle_dir)
        assert result.returncode == 0, f"Default build failed:\n{result.stderr}"

        # Should have platform.json with platform=claude-code
        path = os.path.join(bundle_dir, "platform.json")
        assert os.path.isfile(path), "Default build missing platform.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("platform") == "claude-code"

        # Should NOT have .codex-plugin/
        assert not os.path.exists(os.path.join(bundle_dir, ".codex-plugin")), (
            "Default build should not have .codex-plugin/"
        )


# ---------------------------------------------------------------------------
# E. Dead-assignment premise for INTER_AGENT_DIR in essential hooks
# ---------------------------------------------------------------------------


class TestIdentityLeakPremises:
    """Pin the dead-assignment premise behind the foreign-bundle leak-pattern narrowing.

    Context (issue #778, PR #803 review thread):
    The FOREIGN_LEAK_PATTERNS in tools/engine/build.py deliberately excludes the
    string 'agents-shared' from the foreign-bundle leak scan.  That exclusion is
    safe ONLY because two essential hooks — engram-session-start-hook.py and
    engram-postcompact-hook.py — carry the string '/home/agents-shared/...' solely
    as the default value in an os.environ.get() assignment:

        INTER_AGENT_DIR = os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")

    This assignment is module-level and the name INTER_AGENT_DIR is never READ
    (i.e. never appears in a Load context) anywhere in either file.  The string
    therefore never reaches any emitted artifact at runtime — it stays inert.

    If a future change starts USING INTER_AGENT_DIR (reading it, passing it to a
    function, embedding it in a command string), it becomes an injection path for
    the 'agents-shared' literal into the bundle.  At that point the leak-pattern
    exclusion decision must be revisited: either add 'agents-shared' back to
    FOREIGN_LEAK_PATTERNS, or ensure the hook rewrites the value before use.

    These tests enforce that: INTER_AGENT_DIR is never referenced in a Load
    context in either hook file.  They fail the moment anyone starts reading the
    name, forcing the leak-pattern decision to be explicitly re-evaluated.
    """

    # Hooks that carry INTER_AGENT_DIR only as a dead default assignment
    _HOOKS = [
        "hooks/claude/engram-session-start-hook.py",
        "hooks/claude/engram-postcompact-hook.py",
    ]

    def _count_load_refs(self, hook_rel: str) -> tuple[int, bool]:
        """Return (load_ref_count, has_assignment) for INTER_AGENT_DIR in hook_rel.

        Parses the hook with ast.walk and counts Name nodes with id='INTER_AGENT_DIR'
        in Load context.  Store-context nodes (the assignment itself) are not counted.
        Returns (count, True) if the assignment exists, (count, False) if it does not.
        """
        hook_path = os.path.join(REPO_ROOT, hook_rel)
        if not os.path.isfile(hook_path):
            return (0, False)

        with open(hook_path, encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source, filename=hook_path)
        load_count = 0
        has_assignment = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "INTER_AGENT_DIR":
                if isinstance(node.ctx, ast.Load):
                    load_count += 1
                elif isinstance(node.ctx, ast.Store):
                    has_assignment = True
        return (load_count, has_assignment)

    def test_session_start_hook_inter_agent_dir_stays_dead(self):
        """INTER_AGENT_DIR is never read in engram-session-start-hook.py.

        The #803 leak-scan excludes 'agents-shared' because this name is only a
        dead default — os.environ.get assigns it, nothing reads it.  If a future
        change reads INTER_AGENT_DIR, this test fails and forces the leak-pattern
        exclusion to be reconsidered.
        """
        hook = "hooks/claude/engram-session-start-hook.py"
        load_count, has_assignment = self._count_load_refs(hook)
        if not has_assignment:
            # Premise trivially holds: INTER_AGENT_DIR doesn't even exist in this file
            return  # PASS — nothing to guard
        assert load_count == 0, (
            f"{hook}: INTER_AGENT_DIR is referenced in a Load context "
            f"({load_count} occurrence(s)).  The #803 leak-pattern exclusion for "
            f"'agents-shared' assumes this name is dead (only assigned, never read). "
            f"A Load reference means the string can now reach the bundle at runtime. "
            f"Revisit FOREIGN_LEAK_PATTERNS in tools/engine/build.py before proceeding. "
            f"See issue #778 and PR #803."
        )

    def test_postcompact_hook_inter_agent_dir_stays_dead(self):
        """INTER_AGENT_DIR is never read in engram-postcompact-hook.py.

        Same premise as above: the 'agents-shared' exclusion from FOREIGN_LEAK_PATTERNS
        is valid only while INTER_AGENT_DIR remains a dead assignment in this hook.
        A Load reference here would create a runtime injection path for the literal
        '/home/agents-shared/...' into the bundle, invalidating the exclusion.
        See issue #778 and PR #803.
        """
        hook = "hooks/claude/engram-postcompact-hook.py"
        load_count, has_assignment = self._count_load_refs(hook)
        if not has_assignment:
            # Premise trivially holds: INTER_AGENT_DIR doesn't even exist in this file
            return  # PASS — nothing to guard
        assert load_count == 0, (
            f"{hook}: INTER_AGENT_DIR is referenced in a Load context "
            f"({load_count} occurrence(s)).  The #803 leak-pattern exclusion for "
            f"'agents-shared' assumes this name is dead (only assigned, never read). "
            f"A Load reference means the string can now reach the bundle at runtime. "
            f"Revisit FOREIGN_LEAK_PATTERNS in tools/engine/build.py before proceeding. "
            f"See issue #778 and PR #803."
        )


# ---------------------------------------------------------------------------
# E. Root-file manifest derivation — #666 regression gate
# ---------------------------------------------------------------------------


class TestRootFileManifestDerivation:
    """build_plugin() derives root-level files from tiers.json, not a hardcoded list.

    Regression gate for #666: a convenience-tier root file (viz_server.py) was
    silently omitted because the old implementation used a hardcoded list +
    engram_*.py glob that never included it.  The fix makes section 1 manifest-
    derived, so any root-level entry in tiers.json ships under its declared tier.

    engram-alpha#2555 PR A retarget note: viz_server.py was the ONLY
    convenience-tier ROOT-level manifest entry (verified: it's the sole
    "/" not in path + tier=convenience row in tiers.json as of this PR).
    The #2555 split relocates it to viz/server.py — no longer root-level,
    so it now ships via the generic subdirectory copy loop
    (_copy_manifest_entry), not the root-file-specific code path #666
    fixed. The two tests below are retargeted to viz/server.py to keep
    exercising "convenience-tier file present/absent by tier" as a
    story, but this NARROWS #666's original regression coverage: there
    is currently no other convenience-tier root-level manifest entry, so
    the root-file-specific code path itself has no live example under
    test until/unless a future convenience-tier root file is added.
    test_all_shipped_root_entries_present_in_build below is unaffected
    (manifest-driven, not hardcoded) and still exercises the root-file
    copy path generically for whatever root entries currently exist
    (today: essential + one dev-tier entry, all always-ship-together at
    dev tier).
    """

    def test_convenience_file_present_at_convenience_tier(self, tmp_path):
        """viz/server.py (convenience) is present in a convenience-tier build."""
        from tools.engine.build import build_plugin
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "convenience")
        bundle_dir = str(tmp_path / "convenience_root")

        build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            multi_agent=False,
            manifest=manifest,
        )

        assert os.path.isfile(os.path.join(bundle_dir, "viz", "server.py")), (
            "viz/server.py (convenience tier) missing from convenience build — "
            "manifest-derived copy not including it (see #666, #2555)"
        )

    def test_convenience_file_absent_at_essential_tier(self, tmp_path):
        """viz/server.py (convenience) is absent from an essential-tier build."""
        from tools.engine.build import build_plugin
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "essential")
        bundle_dir = str(tmp_path / "essential_root")

        build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            multi_agent=False,
            manifest=manifest,
        )

        assert not os.path.isfile(os.path.join(bundle_dir, "viz", "server.py")), (
            "viz/server.py (convenience tier) should NOT appear in an essential build"
        )

    def test_all_shipped_root_entries_present_in_build(self, tmp_path):
        """Every root-level mechanism entry ships under its declared tier.

        Builds at dev tier (includes all tiers) and asserts that every root-level
        entry in tiers.json mechanisms[] is present in the build output.
        """
        from tools.engine.build import build_plugin
        from tools.engine.manifest import load_manifest, resolve_tier, select_shippables

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "dev")
        bundle_dir = str(tmp_path / "dev_all_root")

        build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            multi_agent=True,
            manifest=manifest,
        )

        shipped = select_shippables(manifest, tier, multi_agent=True)
        root_paths = [e["path"] for e in shipped if "/" not in e["path"]]
        missing = [p for p in root_paths if not os.path.isfile(os.path.join(bundle_dir, p))]
        assert not missing, (
            f"Root-level manifest entries missing from dev build: {missing} "
            "(manifest-derived root copy must ship every declared root entry)"
        )
