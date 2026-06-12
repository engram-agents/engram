"""Regression coverage for local marketplace installer host targets."""

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "tools" / "install-local-marketplace.sh"


def _env(tmp_path, **overrides):
    env = {**os.environ, "ENGRAM_HOME": str(tmp_path)}
    env.pop("ENGRAM_BUILD_DIR", None)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _write_fake_plugin(build_root, *, platform=None, codex_agent=False):
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "plugin.json").write_text(
        json.dumps({"name": "engram", "version": "0.1.0"})
    )
    if platform is not None:
        (build_root / "platform.json").write_text(json.dumps({"platform": platform}))
    if codex_agent:
        agents_dir = build_root / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "engram-probe-fairy.toml").write_text(
            'name = "engram-probe-fairy"\n'
            'description = "Probe"\n'
            'developer_instructions = "Probe"\n'
            '[mcp_servers.engram]\n'
            'enabled_tools = ["engram_query"]\n'
        )


def test_install_local_marketplace_builds_codex_target(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev"})
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "target: codex" in result.stdout
    assert "multi-agent: yes, target: codex" in result.stdout

    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    assert json.loads((plugin_root / "platform.json").read_text())["platform"] == "codex"
    assert (plugin_root / ".codex-plugin" / "plugin.json").exists()
    manifest = json.loads((plugin_root / ".engram-build-manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert manifest["multi_agent"] is True
    assert manifest["tier"] == "dev"


def test_install_local_marketplace_accepts_equals_target_form(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev"})
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target=codex", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "target: codex" in result.stdout
    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    assert json.loads((plugin_root / "platform.json").read_text())["platform"] == "codex"


def test_install_local_marketplace_defaults_to_claude_code_target(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev"})
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "target: claude-code" in result.stdout
    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    assert json.loads((plugin_root / "platform.json").read_text())["platform"] == "claude-code"


def test_install_local_marketplace_skip_build_rejects_target_mismatch(tmp_path):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "--skip-build target mismatch" in result.stderr
    assert str(build_root / "platform.json") in result.stderr


def test_install_local_marketplace_skip_build_rejects_missing_platform_json(tmp_path):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root)

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "--skip-build cannot verify target" in result.stderr
    assert str(build_root / "platform.json") in result.stderr


def test_install_local_marketplace_rejects_unknown_target(tmp_path):
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "nope", "--skip-add"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "Invalid --target: nope" in result.stderr


def test_install_local_marketplace_installs_codex_agent_toml(tmp_path):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex", codex_agent=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text("#!/usr/bin/env bash\nprintf 'fake codex %s\\n' \"$*\"\n")
    codex.chmod(0o755)

    codex_home = tmp_path / "codex-home"
    cache_dir = codex_home / "plugins" / "cache" / "engram-local" / "engram" / "0.1.0"
    cache_dir.mkdir(parents=True)
    (cache_dir / "launch-engram-server.sh").write_text("#!/usr/bin/env bash\n")
    (cache_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"engram": {"command": "${CLAUDE_PLUGIN_ROOT}/launch-engram-server.sh", "args": []}}})
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(
            tmp_path,
            ENGRAM_BUILD_DIR=build_root,
            CODEX_HOME=codex_home,
            PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "Patching Codex plugin MCP command" in result.stdout
    assert "Installing Codex custom-agent TOML files" in result.stdout
    patched_mcp = json.loads((cache_dir / ".mcp.json").read_text())
    assert patched_mcp["mcpServers"]["engram"]["command"] == str(cache_dir / "launch-engram-server.sh")

    installed = codex_home / "agents" / "engram-probe-fairy.toml"
    assert installed.exists()
    installed_text = installed.read_text()
    assert '[plugins."engram@engram-local".mcp_servers.engram]' in installed_text
    assert '[mcp_servers.engram]' not in installed_text
