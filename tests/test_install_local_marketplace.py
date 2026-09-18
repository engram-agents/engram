"""Regression coverage for local marketplace installer host targets."""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "tools" / "install-local-marketplace.sh"


def _env(tmp_path, **overrides):
    env = {**os.environ, "ENGRAM_HOME": str(tmp_path)}
    env.pop("ENGRAM_BUILD_DIR", None)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _path_without_timeout_wrappers(dest_dir):
    """Build a PATH value that genuinely has no `timeout` or `gtimeout`
    binary reachable anywhere on it, for engram-alpha#2810's falsifier.

    `command -v timeout` (what the installer itself checks) only fails when
    NO directory anywhere on PATH carries an executable named `timeout` —
    a directory earlier on PATH lacking the name does not shadow a real one
    later on PATH, and a non-executable same-named placeholder is skipped
    by `command -v` in favor of a later, executable match. So the only
    reliable way to mask the wrapper is to symlink every OTHER
    currently-reachable executable name into a fresh directory and point
    PATH at that directory alone — everything the installer needs (bash,
    git, mkdir, ...) stays resolvable; only `timeout`/`gtimeout` do not.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for name in names:
            if name in ("timeout", "gtimeout") or name in seen:
                continue
            src = os.path.join(entry, name)
            if not os.path.isfile(src) or not os.access(src, os.X_OK):
                continue
            try:
                os.symlink(src, dest_dir / name)
            except OSError:
                continue
            seen.add(name)
    return str(dest_dir)


def _path_without_jq(dest_dir):
    """Build a PATH value that genuinely has no `jq` binary reachable
    anywhere on it, for engram-alpha#2955's falsifier: the installer must
    read multi_agent/install_tier via python3 unconditionally, never jq, so
    it must build correctly even when jq is entirely absent from PATH (not
    merely absent on this particular runner).

    Same masking technique as `_path_without_timeout_wrappers` above:
    symlink every OTHER currently-reachable executable name into a fresh
    directory and point PATH at that directory alone -- bash/git/python3/etc.
    all stay resolvable; only `jq` does not.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for name in names:
            if name == "jq" or name in seen:
                continue
            src = os.path.join(entry, name)
            if not os.path.isfile(src) or not os.access(src, os.X_OK):
                continue
            try:
                os.symlink(src, dest_dir / name)
            except OSError:
                continue
            seen.add(name)
    return str(dest_dir)


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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
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


def test_install_local_marketplace_builds_without_jq_on_path(tmp_path):
    """engram-alpha#2955: the installer must read multi_agent/install_tier via
    python3, never jq -- confirm by stripping jq from PATH entirely (not just
    checking jq happens to be absent on this runner) and confirming the
    build still succeeds and reads the config correctly."""
    (tmp_path / "config.json").write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev"})
    )
    masked_path = _path_without_jq(tmp_path / "masked-path-bin-no-jq")

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, PATH=masked_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "target: codex" in result.stdout
    assert "multi-agent: yes, target: codex" in result.stdout

    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    manifest = json.loads((plugin_root / ".engram-build-manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert manifest["multi_agent"] is True
    assert manifest["tier"] == "dev"


def test_install_local_marketplace_fails_loud_on_malformed_config(tmp_path):
    """engram-alpha#2955: a genuinely malformed config.json must still abort
    the install with a non-zero exit and a clear stderr message -- matching
    the OLD jq path's `set -e` abort behavior on an unparseable file. The
    python3 replacement must never silently default to a flagless build on
    a parse error."""
    (tmp_path / "config.json").write_text("{not valid json")

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "config.json" in result.stderr or "parse" in result.stderr.lower()


def test_install_local_marketplace_fails_loud_on_non_object_config(tmp_path):
    """engram-alpha#2955 round 1 (reviewer-fairy suggestion): a config.json
    that is syntactically VALID JSON but not an object at the top level
    (e.g. a bare array) must still abort loudly with the same clean
    one-line message as the malformed-JSON case above -- not a raw Python
    traceback from an uncaught AttributeError on d.get(). Fail-loud held
    either way (non-zero exit), but the message quality regressed until
    this fix widened the try/except to cover the type check too."""
    (tmp_path / "config.json").write_text("[]")

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "Traceback" not in result.stderr, (
        f"expected the clean one-line error message, got a raw traceback:\n{result.stderr}"
    )
    assert "config.json" in result.stderr or "parse" in result.stderr.lower() or "expected" in result.stderr.lower()


def test_install_local_marketplace_fails_loud_on_embedded_newline_in_tier(tmp_path):
    """engram-alpha#2958 round 2 (Mira's reviewer catch): the python heredoc
    hands multi_agent/install_tier to bash as two stdout lines, split via
    `sed -n '1p'`/`sed -n '2p'`. An install_tier value containing an embedded
    newline (e.g. "dev\\nbogus") prints as TWO lines from the tier value
    alone, so `sed -n '2p'` silently captures only "dev" and "bogus" is
    dropped from the pipeline with no error -- the old jq path passed the
    whole string as a single shell argument and would have failed loud
    instead. The installer must now reject this config.json with a non-zero
    exit and a clear stderr message, rather than exiting 0 and rewriting
    config.json with the truncated, unvalidated install_tier value."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev\nbogus"})
    )
    original_config_text = config_path.read_text()

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "newline" in result.stderr.lower()
    # config.json must be left untouched -- no truncated install_tier rewrite,
    # confirming the installer aborted before reaching the persist-back block.
    assert config_path.read_text() == original_config_text


def test_install_local_marketplace_fails_loud_on_wrong_typed_install_tier(tmp_path):
    """engram-alpha#2965: a WELL-FORMED config.json whose install_tier value
    is present but the wrong type (a dict, here) must fail loud, not fall
    through and get treated as a real (bogus) tier name. A dict is
    deliberately chosen over an int: `"\\n" in tier` (the pre-existing
    #2958 newline guard) happens to raise TypeError on an int too, which
    would make this test pass for an unrelated reason -- `"\\n" in {"a":1}`
    is valid dict-key-membership syntax and evaluates to False without
    error, so only the #2965 type check catches this case. Verified by
    temporarily removing the type check: an int value still failed (via
    the accidental TypeError), but this dict value exited 0 with the raw
    dict printed as the tier."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": {"a": 1}}))

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "install_tier must be a string" in result.stderr


def test_install_local_marketplace_fails_loud_on_wrong_typed_multi_agent(tmp_path):
    """engram-alpha#2965: multi_agent's sibling case -- a string "true" (a
    plausible hand-edit typo) must fail loud rather than silently resolve
    to false and drop the forum package requirements with no signal.
    Pre-#2965, this exact config exited 0 as if multi_agent were absent."""
    (tmp_path / "config.json").write_text(json.dumps({"multi_agent": "true"}))

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "multi_agent must be a boolean" in result.stderr


def test_install_local_marketplace_accepts_equals_target_form(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"multi_agent": True, "install_tier": "dev"})
    )

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target=codex", "--skip-add", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--skip-add", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
            ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
            ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
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
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
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


def test_install_local_marketplace_multi_agent_convenience_tier_installs_forum_reqs(tmp_path):
    """A multi-agent seat gets src/forum/requirements.txt installed into its
    venv independent of depth tier (#2944) — the forum/coordination package
    composes with any depth tier via the orthogonal --multi-agent flag, so a
    convenience-tier multi-agent seat must still get forum deps, not just
    dev-tier ones."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "convenience", "multi_agent": True})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Provisioning venv at" in result.stdout
    assert "tier: convenience" in result.stdout
    assert "Multi-agent: installing forum package dependencies" in result.stdout
    calls = log_file.read_text()
    assert "requirements/requirements.txt" in calls
    assert "requirements/requirements-dev.txt" not in calls
    assert "src/forum/requirements.txt" in calls


def test_install_local_marketplace_multi_agent_via_mode_installs_forum_reqs(tmp_path):
    """The mode=="multi" spelling (not just multi_agent==true) must also
    trigger the forum-requirements install — same boolean logic as the
    earlier jq-based multi-agent read elsewhere in this script."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "convenience", "mode": "multi"})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Multi-agent: installing forum package dependencies" in result.stdout
    calls = log_file.read_text()
    assert "src/forum/requirements.txt" in calls


def test_install_local_marketplace_non_multi_agent_skips_forum_reqs(tmp_path):
    """A non-multi-agent seat (even at dev tier) does NOT get forum
    requirements installed — the forum package is only needed by seats that
    actually run the forum/coordination tooling."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Dev tier: installing test dependencies" in result.stdout
    assert "Multi-agent: installing forum package dependencies" not in result.stdout
    calls = log_file.read_text()
    assert "src/forum/requirements.txt" not in calls


def test_install_local_marketplace_multi_agent_dev_tier_installs_both(tmp_path):
    """Multi-agent + dev tier is a THIRD independent conditional, not nested
    inside the dev-tier block — both requirements-dev.txt AND
    src/forum/requirements.txt must install together."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "dev", "multi_agent": True})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Dev tier: installing test dependencies" in result.stdout
    assert "Multi-agent: installing forum package dependencies" in result.stdout
    calls = log_file.read_text()
    assert "requirements/requirements-dev.txt" in calls
    assert "src/forum/requirements.txt" in calls


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
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
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


# ── #2951 fail-loud on the --skip-build venv-provisioning config reads ───────
#
# The main build-flags-assembly block's config.json read (_ma_tier_out, above)
# was already made fail-loud by #2955/#2958. The --skip-build path never
# reaches that block at all (it's inside the `else` of `if [[ $SKIP_BUILD -eq
# 1 ]] ... else ... fi`) -- it reads config.json a SECOND time, independently,
# in the venv-provisioning block below (_eff_tier / _eff_multi_agent), which
# is why that second read needed its own fail-loud fix under this issue.
#
# Empirically confirmed (see PR discussion): the venv-provisioning block is
# itself gated behind `if [[ -f "$_ENGRAM_VENV_PY" ]] ...` -- with NO venv
# present, the whole block (including the config.json read) is skipped
# entirely and the installer exits 0, matching
# test_install_local_marketplace_skips_venv_provisioning_when_no_venv above.
# So a --skip-build run against a malformed config.json with no fake venv
# present never even reaches the fixed code -- the two tests below use
# _fake_venv() to make sure the block actually runs and the fail-loud path is
# genuinely exercised.


def test_install_local_marketplace_skip_build_fails_loud_on_malformed_config(tmp_path):
    """engram-alpha#2951: on the --skip-build path, a genuinely malformed
    config.json read by the venv-provisioning block's _eff_tier/_eff_multi_agent
    python3 calls must abort the install with a non-zero exit and a clear
    stderr message -- not silently resolve to convenience/not-multi-agent and
    continue, dropping the dev/forum requirements with no signal."""
    (tmp_path / "config.json").write_text("{not valid json")
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "config.json" in result.stderr or "parse" in result.stderr.lower()


def test_install_local_marketplace_skip_build_fails_loud_on_non_object_config(tmp_path):
    """engram-alpha#2951: same as above but for a syntactically-valid,
    wrong-shape config.json (a bare JSON array) -- fail-loud must hold with
    the same clean one-line message, not a raw Python traceback from an
    uncaught AttributeError on d.get()."""
    (tmp_path / "config.json").write_text("[]")
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "Traceback" not in result.stderr, (
        f"expected the clean one-line error message, got a raw traceback:\n{result.stderr}"
    )
    assert "config.json" in result.stderr or "parse" in result.stderr.lower() or "expected" in result.stderr.lower()


def test_install_local_marketplace_skip_build_fails_loud_on_wrong_typed_install_tier(tmp_path):
    """engram-alpha#2965: the --skip-build venv-provisioning path's own
    separate _eff_tier read has the identical gap as the main build-flags
    path -- a wrong-typed install_tier (a list, here) must fail loud rather
    than pass the `[[ -z ... ]]` fallback check as a non-empty string."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": ["dev"]}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "install_tier must be a string" in result.stderr


def test_install_local_marketplace_skip_build_fails_loud_on_wrong_typed_multi_agent(tmp_path):
    """engram-alpha#2965: the --skip-build path's separate _eff_multi_agent
    read has the identical gap -- multi_agent: 1 (an int, not a bool) must
    fail loud rather than silently resolve to false and skip the forum
    package requirements install with no signal."""
    (tmp_path / "config.json").write_text(json.dumps({"multi_agent": 1}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "multi_agent must be a boolean" in result.stderr


def test_install_local_marketplace_warns_on_missing_forum_requirements(tmp_path):
    """engram-alpha#2951 (Kepler's fold-in): a multi-agent seat whose
    src/forum/requirements.txt is missing must get a WARN naming the gap,
    not a silent no-op -- unlike the install-failed case (which already
    WARNs), a missing file previously produced zero log output.

    This needs the REAL src/forum/requirements.txt (REPO_ROOT-relative, not
    env-overridable) to be absent for the duration of the test only. The real
    file is moved aside via shutil.move and unconditionally restored in a
    finally block, mirroring careful handling of shared repo state during a
    test run."""
    real_forum_req = REPO_ROOT / "src" / "forum" / "requirements.txt"
    assert real_forum_req.is_file(), (
        f"expected {real_forum_req} to exist before the test moves it aside"
    )
    parked_forum_req = tmp_path / "requirements.txt.parked"

    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "convenience", "multi_agent": True})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    shutil.move(str(real_forum_req), str(parked_forum_req))
    try:
        result = _run_installer_skip_build(tmp_path, env_extra={})
    finally:
        shutil.move(str(parked_forum_req), str(real_forum_req))

    assert result.returncode == 0
    assert "WARN" in result.stdout
    assert "forum" in result.stdout
    assert "not found" in result.stdout


def test_install_local_marketplace_skip_build_omitted_keys_default_cleanly(tmp_path):
    """engram-alpha#2964 reviewer-fairy suggestion: the property this whole
    fix exists to preserve -- a well-formed config.json that simply OMITS
    install_tier/multi_agent entirely (not malformed, not present-but-empty)
    must still exit 0 and resolve to the legitimate convenience/non-multi-agent
    defaults via the existing bash `[[ -z ... ]]` lines, never take the new
    fail-loud path. No test in the suite exercised the both-keys-fully-absent
    shape before this one -- the nearest existing coverage
    (test_install_local_marketplace_non_multi_agent_skips_forum_reqs) always
    sets install_tier explicitly."""
    (tmp_path / "config.json").write_text(json.dumps({}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert result.returncode == 0
    assert "tier: convenience" in result.stdout
    assert "Multi-agent: installing forum package dependencies" not in result.stdout
    calls = log_file.read_text()
    assert "src/forum/requirements.txt" not in calls


# ── #2586 tag guard ──────────────────────────────────────────────────────────
#
# These tests run against a SCRATCH git repo, never this checkout — the
# installer resolves REPO_ROOT from its own script location via
# `git -C "$(dirname "$_self")" rev-parse --show-toplevel`, so copying the
# installer into a standalone repo (with a stub src/engram/server.py to
# satisfy the script's own repo-root sanity check) is enough to make the tag
# guard exercise a repo whose tags we fully control, without ever creating a
# tag in the real engram-alpha checkout (hard scope bound, #2586).


def _make_scratch_repo(tmp_path, *, tags=()):
    """A minimal standalone git repo carrying its own copy of the installer.

    tags: tag names to apply to the single commit at HEAD.
    """
    repo = tmp_path / "scratch-repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "src" / "engram").mkdir(parents=True)
    (repo / "src" / "engram" / "server.py").write_text("# stub\n")
    scratch_installer = repo / "tools" / "install-local-marketplace.sh"
    scratch_installer.write_text(INSTALLER.read_text())
    scratch_installer.chmod(0o755)

    def _git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    _git("init", "-q", "-b", "dev")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    for tag in tags:
        _git("tag", tag)
    return repo


def _run_scratch_installer(repo, engram_home, *, extra_args=(), build_dir=None, extra_env=None, timeout=None):
    """Run the scratch repo's own installer copy with a temp ENGRAM_HOME.

    build_dir: when given, points ENGRAM_BUILD_DIR at a pre-populated fake
    plugin tree (via _write_fake_plugin) so a guard-passing run can reach the
    marketplace-assembly + .deployed-version-write steps. Guard-refusal runs
    don't need this — the guard dies before the build dir is ever consulted.

    extra_env: additional env var overrides (e.g. ENGRAM_INSTALL_FETCH_TIMEOUT_SECS
    for the #2711 round-3 timeout-path test, GIT_SSH_COMMAND to swap in a fake
    ssh transport). Merged on top of ENGRAM_BUILD_DIR when both are given.

    timeout: optional hard wall-clock bound (seconds). None (the default)
    preserves every existing caller's behavior exactly — no bound at all,
    same as before this parameter existed, and the plain `subprocess.run(...,
    timeout=None)` path below (no process-group machinery) is used unchanged.
    Callers that exercise a path where the shell's own `timeout`/`gtimeout`
    wrapper might be unavailable (engram-alpha #2810: a stock-macOS seat
    lacking both binaries degrades the installer's own fetch to genuinely
    UNBOUNDED, by documented design) should pass a generous-but-finite value
    here so a hang fails the test loudly instead of hanging the whole suite.
    On expiry this raises subprocess.TimeoutExpired, which callers may catch
    or let propagate as a (loud, non-hanging) test failure/error.

    When timeout is not None, the installer subprocess is launched with
    start_new_session=True (its own process group) and, on TimeoutExpired,
    the ENTIRE process group is SIGKILLed before the exception propagates —
    not just the immediate bash child. subprocess.run's own timeout= only
    kills the process it directly spawned (bash here); it does NOT reach
    bash's own children (the `git fetch` it runs, a hanging fake-ssh
    transport underneath, etc.), which is exactly the orphan risk this
    helper exists to close for every timeout-bounded caller, not only the
    hang-discrimination falsifier below.
    """
    env_overrides = {}
    if build_dir is not None:
        env_overrides["ENGRAM_BUILD_DIR"] = str(build_dir)
    if extra_env:
        env_overrides.update(extra_env)
    argv = ["bash", str(repo / "tools" / "install-local-marketplace.sh"),
            "--skip-add", "--skip-build", *extra_args]
    run_env = _env(engram_home, **env_overrides)

    if timeout is None:
        return subprocess.run(
            argv,
            cwd=repo,
            env=run_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=None,
            check=False,
        )

    proc = subprocess.Popen(
        argv,
        cwd=repo,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
        raise
    return subprocess.CompletedProcess(
        argv, proc.returncode, stdout=stdout, stderr=stderr
    )


def test_tag_guard_refuses_untagged_head(tmp_path):
    repo = _make_scratch_repo(tmp_path)
    engram_home = tmp_path / "engram-home"
    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not a tagged commit" in result.stderr
    assert "#2586" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_tag_guard_accepts_v_tag(tmp_path):
    repo = _make_scratch_repo(tmp_path, tags=["v9.9.9-test"])
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    assert "Deployable tag(s) at HEAD" in result.stdout
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=v9.9.9-test" in marker


def test_tag_guard_accepts_deploy_tag(tmp_path):
    repo = _make_scratch_repo(tmp_path, tags=["deploy/2026-09-05-test"])
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    assert "Deployable tag(s) at HEAD" in result.stdout
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=deploy/2026-09-05-test" in marker


def test_tag_guard_ignores_unrelated_tag(tmp_path):
    repo = _make_scratch_repo(tmp_path, tags=["foo/bar"])
    engram_home = tmp_path / "engram-home"
    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not a tagged commit" in result.stderr
    assert "#2586" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_tag_guard_override_records_none_and_warns(tmp_path):
    repo = _make_scratch_repo(tmp_path)
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-untagged"], build_dir=build_root
    )

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "WARNING" in combined
    assert "#2563" in combined
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=none" in marker
    assert "alpha_untagged_override=1" in marker


def test_tag_guard_multiple_tags_joined(tmp_path):
    repo = _make_scratch_repo(tmp_path, tags=["v1.0.0-a", "deploy/2026-09-05-b"])
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    marker = (engram_home / ".deployed-version").read_text()
    tag_line = next(
        line for line in marker.splitlines() if line.startswith("alpha_tag=")
    )
    joined = tag_line.split("=", 1)[1]
    assert set(joined.split(",")) == {"v1.0.0-a", "deploy/2026-09-05-b"}


def test_tag_guard_runs_before_target_dispatch(tmp_path):
    repo = _make_scratch_repo(tmp_path)
    engram_home = tmp_path / "engram-home"
    result = _run_scratch_installer(repo, engram_home, extra_args=["--target", "codex"])

    assert result.returncode != 0
    assert "not a tagged commit" in result.stderr
    assert "#2586" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


# ── #2711 — #794/#2586 guard ordering (detached HEAD at a deploy tag) ────────
#
# A detached HEAD's "branch name" is the literal string 'HEAD', which always
# tripped the #794 branch guard before the #2586 tag exception got a chance
# to fire (the exact fleet-upgrade repro in #2711: `git checkout
# deploy/...` then the installer with no flags). These tests extend the
# scratch-repo approach above with a two-commit 'dev' history, a REAL
# fetchable 'origin' remote (a local-filesystem bare repo — no network
# needed, but a genuine `git fetch` against it succeeds, which round 2's
# fail-closed refresh requires: a stale ref faked via `git update-ref`
# alone can't exercise a real fetch call), and a divergent unmerged branch
# — so both halves of the #2711 predicate (deployable tag AND merged
# history) are independently exercisable, plus round 2's fetch-failure
# fail-closed case (below).


def _make_scratch_repo_with_ancestry(tmp_path, *, fake_build_plugin=False):
    """A scratch repo shaped for the #2711 ancestor-exception tests.

    Commit graph:
        A (annotated tag v1.0.0-ancestor-test; ancestor of dev's tip)
        |
        B (dev tip == refs/remotes/origin/dev; left UNTAGGED on purpose, so
        |  tests that stay on 'dev' still exercise the unrelated #2586 guard
        |  honestly rather than short-circuiting on a tag)
        (branches off A)
        D (on branch 'stray'; tagged deploy/2026-09-07-stray; NOT an
           ancestor of origin/dev — simulates a same-named local tag on
           unmerged work, which must NOT bypass #794)

    Ends on 'dev'. Returns (repo, commits) with commits = {"a": sha, "b": sha, "d": sha}.

    A real 'origin' remote (bare repo on local disk, at tmp_path /
    "origin-bare.git") is configured with 'dev' and 'main' both at sha_b —
    matching this repo's real topology where both branches exist upstream —
    so the installer's own `git fetch origin dev main` (round 2's stale-ref
    refresh, #2711) has something genuine to succeed against.

    fake_build_plugin: when True, tools/build-plugin.sh in the scratch repo
    is replaced with a stub that logs its argv to build-plugin-calls.log and
    exits 0 — used to verify the --allow-branch flag this installer forwards
    downstream, without needing a real (heavy) engine build.
    """
    repo = tmp_path / "scratch-repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "src" / "engram").mkdir(parents=True)
    (repo / "src" / "engram" / "server.py").write_text("# stub\n")
    scratch_installer = repo / "tools" / "install-local-marketplace.sh"
    scratch_installer.write_text(INSTALLER.read_text())
    scratch_installer.chmod(0o755)

    if fake_build_plugin:
        build_plugin_log = repo / "build-plugin-calls.log"
        fake_build_plugin_sh = repo / "tools" / "build-plugin.sh"
        fake_build_plugin_sh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{build_plugin_log}"\n'
        )
        fake_build_plugin_sh.chmod(0o755)

    def _git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    _git("init", "-q", "-b", "dev")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    sha_a = _git("rev-parse", "HEAD").stdout.strip()
    # Annotated (not lightweight) on purpose — matches the real deploy tags
    # named in #2711 and exercises `git rev-parse <tag>` returning the tag
    # OBJECT rather than the commit, the exact trap #2711 calls out. The
    # installer's own ancestry check must use HEAD, never this tag name
    # directly, to sidestep it.
    _git("tag", "-a", "v1.0.0-ancestor-test", "-m", "annotated deploy tag")

    (repo / "second.txt").write_text("second commit\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "second")
    sha_b = _git("rev-parse", "HEAD").stdout.strip()

    # A REAL, fetchable 'origin' remote — a bare repo on the local
    # filesystem, no network needed. 'dev' and 'main' both point at sha_b
    # upstream (this repo's real topology has both branches present on
    # origin). The explicit `fetch` at the end (rather than relying on
    # `push`'s own opportunistic remote-tracking-ref update, which varies by
    # git version/config) is what guarantees refs/remotes/origin/{dev,main}
    # are populated before any test runs the installer's own
    # `git fetch origin dev main` against this same remote.
    origin_bare = tmp_path / "origin-bare.git"
    _git("init", "-q", "-b", "dev", "--bare", str(origin_bare))
    _git("remote", "add", "origin", str(origin_bare))
    _git("push", "-q", "origin", "dev")
    _git("push", "-q", "origin", "dev:refs/heads/main")
    _git("fetch", "-q", "origin")

    _git("checkout", "-q", "-b", "stray", sha_a)
    (repo / "stray.txt").write_text("unmerged work\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "stray unmerged commit")
    sha_d = _git("rev-parse", "HEAD").stdout.strip()
    _git("tag", "deploy/2026-09-07-stray")

    _git("checkout", "-q", "dev")

    return repo, {"a": sha_a, "b": sha_b, "d": sha_d}


def _checkout(repo, ref):
    subprocess.run(
        ["git", "checkout", "-q", ref], cwd=repo, check=True,
        capture_output=True, text=True,
    )


def test_detached_head_at_ancestor_deploy_tag_builds_without_allow_branch(tmp_path):
    """Test 1 (#2711): HEAD detached at a deployable tag that IS an
    ancestor of origin/dev builds with NO --allow-branch."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "v1.0.0-ancestor-test")
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    assert "Deployable tag(s) at HEAD" in result.stdout
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=v1.0.0-ancestor-test" in marker


def test_detached_head_at_non_ancestor_commit_still_refuses(tmp_path):
    """Test 2 (#2711): a deployable-pattern tag on a commit that is NOT
    merged into origin/dev must still refuse without --allow-branch — a
    same-named local tag on unmerged work is not the #2586 exception, being
    ACTUALLY MERGED is. Without this test the fix is indistinguishable from
    deleting the #794 guard outright."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "deploy/2026-09-07-stray")
    engram_home = tmp_path / "engram-home"

    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not 'dev'" in result.stderr
    assert "#794" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_detached_head_at_non_ancestor_commit_builds_with_allow_branch(tmp_path):
    """The --allow-branch override still works for the non-ancestor case —
    the fix narrows the #794 exception, it doesn't remove the override."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "deploy/2026-09-07-stray")
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-branch"], build_dir=build_root
    )

    assert result.returncode == 0, result.stderr


def test_dev_checkout_still_builds_without_allow_branch(tmp_path):
    """Test 3 (#2711), first half: a normal 'dev' checkout still builds
    without --allow-branch. dev's tip here is deliberately untagged, so
    --allow-untagged is still required — that's the unrelated #2586 guard;
    what this test asserts is that no #794 branch error appears."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-untagged"], build_dir=build_root
    )

    assert result.returncode == 0, result.stderr
    assert "#794" not in (result.stdout + result.stderr)


def test_genuine_feature_branch_still_refuses_without_allow_branch(tmp_path):
    """Test 3 (#2711), second half: a genuine feature branch (no tag at
    HEAD at all) is exactly what #794 must keep refusing — the ordering fix
    must not turn into a guard deletion."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/some-work"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"

    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not 'dev'" in result.stderr
    assert "#794" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_ancestor_tag_checkout_forwards_allow_branch_to_build_plugin(tmp_path):
    """build-plugin.sh (tools/engine/cli.py) carries its OWN, independent
    #794 branch guard — this installer's own guard passing is not enough if
    the downstream build call still gets no --allow-branch. #2711's fix
    must forward the tag/ancestor exception into build_flags too."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path, fake_build_plugin=True)
    _checkout(repo, "v1.0.0-ancestor-test")
    engram_home = tmp_path / "engram-home"

    result = subprocess.run(
        ["bash", str(repo / "tools" / "install-local-marketplace.sh"), "--skip-add"],
        cwd=repo,
        env=_env(engram_home),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    # The installer's OWN #794 guard must not have fired (whatever happens
    # downstream in the fake build-plugin.sh stub, which doesn't produce a
    # real plugin.json and so the script dies afterward — irrelevant here).
    assert "not 'dev'" not in result.stderr

    log = (repo / "build-plugin-calls.log").read_text()
    assert "--allow-branch" in log


# ── #2711 round 2 — fail-closed on a failed origin refresh ───────────────────
#
# The ancestry check trusts refs/remotes/origin/{dev,main} without ever
# re-fetching them, so a LOCAL remote-tracking ref that was correct at
# fetch-time but is now stale (e.g. upstream force-pushed 'dev' back past a
# bad merge) can still report ancestry for a commit that is no longer merged
# history. The fix refreshes both refs immediately before trusting them, and
# must FAIL CLOSED (never grant the exception) if that refresh itself fails
# — an unreachable network must make the guard STRICTER, never looser.


def test_fetch_failure_fails_closed_refuses_ancestor_tag_without_allow_branch(tmp_path):
    """If the pre-check `git fetch origin dev main` fails (offline / no
    remote / auth failure / ...), DEPLOYABLE_ANCESTOR must stay 0 — a
    detached HEAD at a tag that WOULD otherwise pass the ancestor check
    (v1.0.0-ancestor-test, genuinely merged into origin/dev) must still
    REFUSE without --allow-branch. This is the mutation-control case: a
    fail-OPEN implementation (leaving DEPLOYABLE_ANCESTOR=1 when the fetch
    fails) makes this test go green->red; see the round-2 handoff for the
    confirmed mutation result."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "v1.0.0-ancestor-test")
    # Break the fetch deliberately by pointing 'origin' at a path that does
    # not exist. No test can safely sever real network access, so this is
    # the local-filesystem-remote equivalent: `git fetch origin ...` fails
    # the same way (non-zero exit, no refs updated) whether the remote is
    # unreachable over a network or simply absent on disk.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"

    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not 'dev'" in result.stderr
    assert "#794" in result.stderr


# ── #2711 round 3 — bound the fetch, fail closed on a TIMEOUT specifically ──
#
# Round 2 fixed the fail-closed direction (above) but introduced a new hang
# risk: `git fetch origin dev main` carried no timeout, so a silently-dropped
# network (packets unrouted rather than refused) could leave it hanging
# indefinitely instead of failing fast. This section covers the fix: the
# fetch is now wrapped in `timeout $FETCH_TIMEOUT_SECS`, and a timeout must
# land in the SAME fail-closed branch as any other fetch failure.


@pytest.mark.skipif(
    shutil.which("timeout") is None and shutil.which("gtimeout") is None,
    reason="installer degrades to an unbounded fetch with no timeout binary; this test would hang (see engram-alpha#2810)",
)
def test_fetch_timeout_fails_closed_refuses_ancestor_tag_without_allow_branch(tmp_path):
    """A silently-dropped network can't be produced with a real socket in a
    test without flakiness, so this simulates it the same way the coreutils
    `timeout` documentation itself frames the failure: a subprocess that
    accepts control but never returns. `GIT_SSH_COMMAND` swaps git's ssh
    transport for a script that just sleeps, and the origin URL is switched
    to an `ssh://` form so that transport is actually used (a bare local
    path, as the round-2 test above uses, fails instantly rather than
    hanging — it can't stand in for this case).

    ENGRAM_INSTALL_FETCH_TIMEOUT_SECS overrides the installer's real 15s
    default down to 2s purely so this test doesn't itself take 15s+ to run;
    it asserts the override mechanism plus the fail-closed OUTCOME, not the
    production duration (see the handoff for the chosen 15s figure and its
    reasoning).

    Mutation-control result for this test (fail OPEN on a timeout instead of
    fail-closed): documented in the round-3 handoff rather than executed as
    a second test here — the natural fail-open mutation (treating rc=124
    the same as rc=0) does not hang this test, but a mutation that instead
    DROPS the `timeout` wrapper entirely reintroduces the very hang this
    fix exists to bound, and a test that can hang the suite is worse than
    one that doesn't exist. The 2s cap plus this test's own runtime assertion
    below is the safeguard against that specific mutation shape.

    engram-alpha#2810: on a seat with NEITHER `timeout` nor `gtimeout` on
    PATH, the installer's own fetch is genuinely unbounded by documented
    design (see the WARNING added to the script's no-wrapper branch), and
    this test — which relies entirely on the shell's own wrapper to cap the
    hanging fake-ssh transport below — would hang the whole suite. The
    skipif above guards that; the `timeout=` kwarg below is an independent,
    unconditional second line of defense (belt-and-suspenders, not a
    substitute for the skip) in case the wrapper resolves but somehow fails
    to actually bound the child."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "v1.0.0-ancestor-test")

    fake_ssh = tmp_path / "fake-ssh.sh"
    fake_ssh.write_text("#!/usr/bin/env bash\nexec sleep 9999\n")
    fake_ssh.chmod(0o755)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "ssh://localhost/nonexistent-repo.git"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"

    start = time.monotonic()
    result = _run_scratch_installer(
        repo, engram_home,
        extra_env={
            "ENGRAM_INSTALL_FETCH_TIMEOUT_SECS": "2",
            "GIT_SSH_COMMAND": str(fake_ssh),
        },
        timeout=30,
    )
    elapsed = time.monotonic() - start

    # Generous ceiling above the 2s cap (not a tight bound — CI/sandboxed
    # hosts vary) but far below the 9999s the fake ssh sleeps for; this is
    # what actually distinguishes "bounded" from "the timeout silently
    # didn't apply" for a reader of this test's result, not just the exit
    # code (which a hang would never even produce within a sane test run).
    assert elapsed < 8, f"installer took {elapsed:.1f}s — the fetch was not bounded"
    assert result.returncode != 0
    assert "not 'dev'" in result.stderr
    assert "#794" in result.stderr
    assert "TIMED OUT" in result.stdout
    assert not (engram_home / ".deployed-version").exists()
    assert not (engram_home / ".deployed-version").exists()


# ── engram-alpha#2810 — falsifiers for the skipif guard + the new WARNING ──
#
# #2810: on a seat with NEITHER `timeout` nor `gtimeout` on PATH (stock
# macOS), the installer's fetch above degrades to genuinely UNBOUNDED, by
# documented design (see the block comment above FETCH_TIMEOUT_SECS in the
# script). The round-3 test above relies entirely on the shell's own
# wrapper to cap a deliberately-hanging fake ssh transport — on a seat
# lacking both binaries it would hang the whole suite, hence the skipif
# marker added to it. A `skipif` marker is evaluated once, at COLLECTION
# time, from a real `shutil.which()` call — it is not something a
# per-test mock can exercise — so the falsifier for it is necessarily a
# different, lower-level probe: does the PRODUCTION SCRIPT itself actually
# detect "no wrapper resolves" and take the documented no-wrapper branch
# (WARNING + genuinely unbounded fetch), using a fetch target that fails
# FAST (not the slow-ssh hang trick, which needs the very wrapper this
# test masks away).


def test_fetch_no_wrapper_warns_and_takes_documented_unbounded_branch(tmp_path):
    """Falsifier for fix 1 (skipif) and fix 3 (WARNING text).

    Builds a PATH with every real, currently-resolvable executable name
    EXCEPT `timeout`/`gtimeout` (see _path_without_timeout_wrappers), so
    `command -v timeout` / `command -v gtimeout` inside the real installer
    genuinely fail — the same predicate a stock-macOS seat hits, produced
    without needing one. Uses `_make_scratch_repo` (no 'origin' remote
    configured at all) with a deploy tag at HEAD, so the fetch block runs
    but `git fetch origin ...` fails FAST ("'origin' does not appear to be
    a git repository") rather than hanging — proving the WARNING fires and
    the script proceeds through its documented degraded path, not proving
    anything about the (separately-tested) timeout-wrapper-present case.
    """
    repo = _make_scratch_repo(tmp_path, tags=["v9.9.9-nowrapper-test"])
    engram_home = tmp_path / "engram-home"
    masked_path = _path_without_timeout_wrappers(tmp_path / "masked-path-bin")

    result = _run_scratch_installer(
        repo, engram_home,
        extra_env={"PATH": masked_path},
        timeout=30,
    )

    assert (
        "neither 'timeout' nor 'gtimeout' is on PATH" in result.stdout
    ), result.stdout
    assert "brew install coreutils" in result.stdout, result.stdout
    # The fetch itself still ran (and failed fast, per the no-'origin'-remote
    # scratch repo) and routed into the SAME fail-closed WARNING branch as
    # any other fetch failure -- confirming the masked PATH didn't silently
    # skip the fetch attempt entirely.
    assert "could not refresh origin/dev + origin/main" in result.stdout, result.stdout


def test_run_scratch_installer_timeout_kwarg_bounds_a_genuine_hang(tmp_path):
    """Falsifier for fix 2 (the hard, unconditional test-side timeout bound).

    Reuses the round-3 test's own hanging-ssh trick (GIT_SSH_COMMAND pointed
    at a script that `exec sleep 9999`), but WITHOUT any shell-side
    `timeout`/`gtimeout` protection — the fetch is masked to genuinely
    unbounded here, same predicate as the falsifier above, plus a real hang
    source. Proves it is `_run_scratch_installer()`'s OWN `timeout=` kwarg
    that bounds the call (rather than a shell wrapper, which is deliberately
    absent here): it must raise subprocess.TimeoutExpired well under 9999s.

    This is the test that would have caught #2810 directly: pre-fix (no
    `timeout=` kwarg reaching `_run_scratch_installer()`'s own subprocess
    call at all), this exact call hangs for the full 9999s. See the handoff
    for the re-verification that actually exercised this by temporarily
    reverting the production `timeout=` wiring.

    Calls `_run_scratch_installer()` directly (matching the round-3 test's
    own call shape above) rather than driving subprocess.Popen by hand — an
    earlier version of this test bypassed the shared helper entirely and so
    never actually exercised the kwarg it claimed to falsify. The helper
    itself is what now does the orphan-safe process-group cleanup (see its
    docstring): on TimeoutExpired it SIGKILLs the whole process group
    (bash, the git fetch it spawns, the fake-ssh script, and the sleep 9999
    underneath) via `os.killpg`, launched with `start_new_session=True` — a
    PID-scoped cleanup of exactly this call's own subtree, never a
    name/pattern match over the host's process table.
    """
    repo = _make_scratch_repo(tmp_path, tags=["v9.9.9-hang-test"])
    engram_home = tmp_path / "engram-home"
    masked_path = _path_without_timeout_wrappers(tmp_path / "masked-path-bin-hang")

    fake_ssh = tmp_path / "fake-ssh-hang.sh"
    fake_ssh.write_text("#!/usr/bin/env bash\nexec sleep 9999\n")
    fake_ssh.chmod(0o755)
    subprocess.run(
        ["git", "remote", "add", "origin", "ssh://localhost/nonexistent-repo.git"],
        cwd=repo, check=True, capture_output=True, text=True,
    )

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_scratch_installer(
            repo, engram_home,
            extra_env={"PATH": masked_path, "GIT_SSH_COMMAND": str(fake_ssh)},
            timeout=5,
        )
    elapsed = time.monotonic() - start

    # Confirms the discrimination this test exists for: the call actually
    # returned (via the raised TimeoutExpired) near the 5s bound, not near
    # the 9999s the fake ssh sleeps for.
    assert elapsed < 9999, f"took {elapsed:.1f}s — did not discriminate a real hang"
    assert elapsed < 15, f"took {elapsed:.1f}s — timeout= did not bound the wait"


# ── #2979 — regression: shipped .sh scripts must parse under real bash 3.2 ──
#
# Root cause: bash 3.2 (macOS's frozen system bash) has a lexer quirk where a
# heredoc with a QUOTED delimiter (<<'PY'), even though its body should be
# 100% literal, still gets scanned by bash 3.2 for quote-balance when the
# heredoc is nested inside a $(...) command substitution (bash needs the scan
# to locate the substitution's closing paren). An ODD total count of literal
# ' characters anywhere in that heredoc body -- including inside comments,
# e.g. an English possessive apostrophe -- desyncs the scan and produces a
# spurious syntax error many lines later at the next unrelated `(`.
#
# This shipped unnoticed into the tagged v0.5.1-rc2 release commit because no
# CI lane ran any shipped .sh script under a REAL bash 3.2 interpreter --
# Linux CI's /bin/bash is a modern bash and structurally can't exercise this
# bug class. This section closes that gap: it locates the actual macOS system
# bash at /bin/bash (never whatever `bash` resolves to on PATH, which could
# be a newer Homebrew bash even on a Mac dev seat), confirms it is genuinely
# 3.2.x, and if so runs `bash -n` against every .sh mechanism the tier
# manifest (src/build/packaging/tiers.json) actually ships.


def _find_macos_system_bash():
    """Locate the real macOS system bash at /bin/bash specifically (never
    whatever `bash` resolves to via PATH -- a dev seat may have a newer
    Homebrew bash earlier on PATH, which would defeat the whole point of
    this test) and confirm it is genuinely 3.2.x, the frozen Apple build
    #2979's landmine is about.

    Returns (bash_path, None) when a confirmed-3.2.x /bin/bash is found, or
    (None, reason) when it should be skipped -- absent entirely (e.g. a
    Linux CI runner with no /bin/bash at all is unusual but not impossible
    to imagine) or present but not 3.2.x (a modern /bin/bash, whether a
    Linux runner or some future macOS release)."""
    system_bash = Path("/bin/bash")
    if not system_bash.is_file():
        return None, "/bin/bash does not exist on this host"
    try:
        result = subprocess.run(
            [str(system_bash), "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except OSError as exc:
        return None, f"/bin/bash --version failed to run: {exc}"
    match = re.search(r"version (\d+)\.(\d+)\.(\d+)", result.stdout)
    if not match:
        return None, f"could not parse /bin/bash --version output: {result.stdout!r}"
    major, minor, patch = (int(part) for part in match.groups())
    if (major, minor) != (3, 2):
        return None, (
            f"/bin/bash is {major}.{minor}.{patch}, not 3.2.x -- this host's "
            "system bash isn't the frozen Apple build #2979 is about"
        )
    return system_bash, None


def _shipped_shell_scripts():
    """Resolve every shipped .sh mechanism from the tier manifest
    (src/build/packaging/tiers.json) -- the living single source of truth
    for what actually ships, per this repo's Install-tiers discipline
    (project CLAUDE.md) -- rather than a hand-maintained list, so a
    newly-tiered shell script is automatically covered here too.

    Only entries in the top-level "mechanisms" array ship in some install
    tier; "excluded" and "repo_only" entries are explicitly documented as
    NEVER shipping (e.g. tools/test_migrate_to_plugin.sh is listed in
    "excluded" -- "CI tooling only, never ships" -- so it is correctly
    absent from the returned list, even though it is a .sh file that exists
    on disk).

    Resolution mirrors the manifest's own documented convention: a
    "source" key (when present) is a path relative to the REPO ROOT --
    used for real files that live outside src/engram/ (e.g.
    tools/build-plugin.sh -> src/build/build-plugin.sh); otherwise "path"
    is relative to the manifest's "source_root" (src/engram)."""
    manifest = json.loads(
        (REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text()
    )
    source_root = REPO_ROOT / manifest["source_root"]
    scripts = []
    for mechanism in manifest.get("mechanisms", []):
        path = mechanism.get("path", "")
        if not path.endswith(".sh"):
            continue
        if "source" in mechanism:
            resolved = REPO_ROOT / mechanism["source"]
        else:
            resolved = source_root / path
        scripts.append(resolved)
    return scripts


def test_shipped_shell_scripts_parse_under_macos_bash32():
    """Regression test for #2979.

    Skips cleanly (never fails, never vacuously passes) on any host where
    /bin/bash isn't confirmed genuinely 3.2.x -- Linux CI runners (this
    repo's pr-checks.yml / tests.yml both run ubuntu-latest) ship a modern
    /bin/bash and structurally cannot exercise this bug class; a
    macos-latest GitHub-hosted runner (or a real macOS dev seat) does, and
    is where this test actually runs for-real.

    On a confirmed 3.2.x host: runs `/bin/bash -n <script>` against every
    .sh mechanism the tier manifest says this repo ships (see
    _shipped_shell_scripts docstring), and asserts a clean parse for each --
    exactly the check that would have caught #2979 before it ever reached a
    tagged release."""
    system_bash, skip_reason = _find_macos_system_bash()
    if system_bash is None:
        pytest.skip(f"real macOS system bash 3.2.x not available: {skip_reason}")

    scripts = _shipped_shell_scripts()
    assert scripts, (
        "tier manifest resolved zero shipped .sh scripts -- the manifest "
        "resolution logic in _shipped_shell_scripts() is likely broken"
    )

    failures = []
    for script in scripts:
        assert script.is_file(), f"manifest-resolved script does not exist on disk: {script}"
        result = subprocess.run(
            [str(system_bash), "-n", str(script)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            failures.append((script, result.returncode, result.stderr))

    if failures:
        detail = "\n".join(
            f"  {path} (exit {rc}):\n{stderr}" for path, rc, stderr in failures
        )
        pytest.fail(
            f"{len(failures)} of {len(scripts)} shipped script(s) failed "
            f"`bash -n` under macOS system bash 3.2.x:\n{detail}"
        )
