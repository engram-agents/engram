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


def test_install_local_marketplace_preserves_prior_codex_cache_across_dev_upgrades(tmp_path):
    """Keep each active cache path alive even when Codex removes its cache root."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex")

    codex_home = tmp_path / "codex-home"
    cache_root = codex_home / "plugins" / "cache" / "engram-local" / "engram"
    old_cache = cache_root / "0.1.0-dev.20260808000000.old0000"
    old_cache.mkdir(parents=True)
    (old_cache / "launch-engram-server.sh").write_text("#!/usr/bin/env bash\n")
    (old_cache / ".mcp.json").write_text(json.dumps({"mcpServers": {"engram": {}}}))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "codex-add-count"
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cache_root="$CODEX_HOME/plugins/cache/engram-local/engram"\n'
        f'counter="{counter}"\n'
        'count="$(cat "$counter" 2>/dev/null || printf 0)"\n'
        'count=$((count + 1))\n'
        'printf "%s" "$count" > "$counter"\n'
        'rm -rf "$cache_root"\n'
        'case "$count" in\n'
        '  1) version="0.1.0-dev.20260808010000.first111" ;;\n'
        '  2) version="0.1.0-dev.20260808020000.second22" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
        'mkdir -p "$cache_root/$version"\n'
        'printf "#!/usr/bin/env bash\\n" > "$cache_root/$version/launch-engram-server.sh"\n'
        'printf \'{"mcpServers":{"engram":{}}}\' > "$cache_root/$version/.mcp.json"\n'
    )
    codex.chmod(0o755)

    base_env = {
        "ENGRAM_BUILD_DIR": build_root,
        "CODEX_HOME": codex_home,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    first = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, **base_env, CLAUDE_PLUGIN_ROOT=old_cache),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    first_new_cache = cache_root / "0.1.0-dev.20260808010000.first111"
    assert old_cache.is_symlink()
    assert old_cache.resolve() == first_new_cache
    assert "Symlinking prior live cache" in first.stdout

    second = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, **base_env, CLAUDE_PLUGIN_ROOT=first_new_cache),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    second_new_cache = cache_root / "0.1.0-dev.20260808020000.second22"
    assert first_new_cache.is_symlink()
    assert first_new_cache.resolve() == second_new_cache
    assert "Symlinking prior live cache" in second.stdout


def test_install_local_marketplace_rejects_non_direct_claude_plugin_roots(tmp_path):
    """Compat cleanup must not follow traversal or nested hook cache paths."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cache_root="$CODEX_HOME/plugins/cache/engram-local/engram"\n'
        'rm -rf "$cache_root"\n'
        'new_cache="$cache_root/0.1.0-dev.20260808010000.new11111"\n'
        'mkdir -p "$new_cache"\n'
        'printf "#!/usr/bin/env bash\\n" > "$new_cache/launch-engram-server.sh"\n'
        'printf \'{"mcpServers":{"engram":{}}}\' > "$new_cache/.mcp.json"\n'
    )
    codex.chmod(0o755)

    for case in ("traversal", "nested"):
        case_root = tmp_path / case
        codex_home = case_root / "codex-home"
        cache_parent = codex_home / "plugins" / "cache" / "engram-local" / "engram"
        old_cache = cache_parent / "0.1.0-dev.20260808000000.old0000"
        old_cache.mkdir(parents=True)

        if case == "traversal":
            outside = codex_home / "plugins" / "cache" / "outside"
            outside.mkdir()
            (outside / "sentinel").write_text("keep")
            plugin_root = f"{cache_parent}/../../outside"
        else:
            nested = old_cache / "nested"
            nested.mkdir()
            plugin_root = nested

        result = subprocess.run(
            ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
            cwd=REPO_ROOT,
            env=_env(
                tmp_path,
                ENGRAM_BUILD_DIR=build_root,
                CODEX_HOME=codex_home,
                CLAUDE_PLUGIN_ROOT=plugin_root,
                PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        assert "Symlinking prior live cache" in result.stdout
        if case == "traversal":
            assert outside.is_dir()
            assert not outside.is_symlink()
            assert (outside / "sentinel").read_text() == "keep"
        else:
            assert not nested.exists()
            assert not nested.is_symlink()


def test_install_local_marketplace_does_not_preserve_plain_release_without_valid_active_root(tmp_path):
    """Fallback compat preservation is limited to development cache paths."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cache_root="$CODEX_HOME/plugins/cache/engram-local/engram"\n'
        'new_cache="$cache_root/0.1.0-dev.20260808010000.new11111"\n'
        'mkdir -p "$new_cache"\n'
        'printf "#!/usr/bin/env bash\\n" > "$new_cache/launch-engram-server.sh"\n'
        'printf \'{"mcpServers":{"engram":{}}}\' > "$new_cache/.mcp.json"\n'
    )
    codex.chmod(0o755)

    for active_root in (None, tmp_path / "outside"):
        case_root = tmp_path / ("absent" if active_root is None else "invalid")
        codex_home = case_root / "codex-home"
        cache_parent = codex_home / "plugins" / "cache" / "engram-local" / "engram"
        release = cache_parent / "0.1.0"
        release.mkdir(parents=True)
        (release / "sentinel").write_text("keep")

        env = {
            "ENGRAM_BUILD_DIR": build_root,
            "CODEX_HOME": codex_home,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
        if active_root is not None:
            active_root.mkdir()
            env["CLAUDE_PLUGIN_ROOT"] = active_root

        result = subprocess.run(
            ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
            cwd=REPO_ROOT,
            env=_env(tmp_path, **env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        assert "Symlinking prior live cache" not in result.stdout
        assert release.is_dir()
        assert not release.is_symlink()
        assert (release / "sentinel").read_text() == "keep"


def test_install_local_marketplace_replaces_direct_child_symlink_not_external_target(tmp_path):
    """A verified direct cache symlink must not delete its external target."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex")

    codex_home = tmp_path / "codex-home"
    cache_parent = codex_home / "plugins" / "cache" / "engram-local" / "engram"
    old_cache = cache_parent / "0.1.0-dev.20260808000000.old0000"
    external_target = tmp_path / "external-cache"
    external_target.mkdir()
    (external_target / "sentinel").write_text("keep")
    old_cache.parent.mkdir(parents=True)
    old_cache.symlink_to(external_target, target_is_directory=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cache_root="$CODEX_HOME/plugins/cache/engram-local/engram"\n'
        'rm -rf "$cache_root"\n'
        'new_cache="$cache_root/0.1.0-dev.20260808010000.new11111"\n'
        'mkdir -p "$new_cache"\n'
        'printf "#!/usr/bin/env bash\\n" > "$new_cache/launch-engram-server.sh"\n'
        'printf \'{"mcpServers":{"engram":{}}}\' > "$new_cache/.mcp.json"\n'
    )
    codex.chmod(0o755)

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(
            tmp_path,
            ENGRAM_BUILD_DIR=build_root,
            CODEX_HOME=codex_home,
            CLAUDE_PLUGIN_ROOT=f"{old_cache}/",
            PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    new_cache = cache_parent / "0.1.0-dev.20260808010000.new11111"
    assert old_cache.is_symlink()
    assert old_cache.resolve() == new_cache
    assert external_target.is_dir()
    assert (external_target / "sentinel").read_text() == "keep"
    assert "Symlinking prior live cache" in result.stdout


def _fake_venv(tmp_path, log_file, *, exit_code=0):
    """Create a fake $ENGRAM_HOME/venv/bin/python that logs every argv it sees.

    exit_code controls the fake python's exit status, so the failure-recovery
    path (pip install fails → install completes but exits non-zero) is testable.
    """
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log_file}"\n'
        f"exit {exit_code}\n"
    )
    fake_py.chmod(0o755)
    return fake_py


def _run_installer_skip_build(tmp_path, *, env_extra):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    return subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root, **env_extra),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def test_install_local_marketplace_provisions_venv_dev_tier_installs_test_deps(tmp_path):
    """At the dev tier the venv gets requirements-dev.txt (runtime + pytest)
    on top of the runtime requirements, so the local test runner works out of
    the box (Lei, 2026-08-10)."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Provisioning venv at" in result.stdout
    assert "tier: dev" in result.stdout
    assert "Dev tier: installing test dependencies" in result.stdout
    calls = log_file.read_text()
    assert "-m pip install --quiet -r" in calls
    assert "requirements/requirements.txt" in calls
    assert "requirements/requirements-dev.txt" in calls


def test_install_local_marketplace_provisions_venv_convenience_tier_runtime_only(tmp_path):
    """Below the dev tier, only the runtime requirements are installed into the
    venv — pytest is a dev-tier extra, not a runtime need."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "convenience"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Provisioning venv at" in result.stdout
    assert "tier: convenience" in result.stdout
    assert "Dev tier: installing test dependencies" not in result.stdout
    calls = log_file.read_text()
    assert "-m pip install --quiet -r" in calls
    assert "requirements/requirements.txt" in calls
    assert "requirements/requirements-dev.txt" not in calls


def test_install_local_marketplace_skips_venv_provisioning_when_no_venv(tmp_path):
    """A fresh ENGRAM_HOME with no venv is left alone — the provisioning block
    is a no-op, not an error, so a first install before venv creation still
    succeeds."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Provisioning venv at" not in result.stdout
    assert result.returncode == 0


def test_install_local_marketplace_runtime_req_failure_exits_nonzero(tmp_path):
    """A failed RUNTIME-requirements install must not exit 0 — a successful
    exit over a venv that can't start the server is the silent-broken-venv
    failure mode this PR exists to prevent (Lei, 2026-08-10). The install still
    completes (marketplace assembled, WARN logged) so it leaves a re-runnable
    state; the non-zero exit is the unattended-upgrade signal."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file, exit_code=1)

    # check=False: the whole point of this test is the non-zero exit.
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    # Install completed the provisioning attempt + surfaced the WARN...
    assert "Provisioning venv at" in result.stdout
    assert "tier: dev" in result.stdout
    assert "WARN: venv runtime-requirements install failed" in result.stdout
    # ...and the end-of-script ERROR that carries the non-zero exit.
    assert "ERROR: venv runtime-requirements provisioning failed" in result.stdout
    # The contract: non-zero exit (the unattended-upgrade signal).
    assert result.returncode != 0
    # The fake venv was actually invoked for the runtime install.
    assert "requirements/requirements.txt" in log_file.read_text()
