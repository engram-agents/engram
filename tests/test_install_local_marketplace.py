"""Regression coverage for local marketplace installer host targets."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "tools" / "install-local-marketplace.sh"
DEPLOY_TAG_INIT = REPO_ROOT / "src" / "build" / "engine" / "__init__.py"
DEPLOY_TAG_MODULE = REPO_ROOT / "src" / "build" / "engine" / "deploy_tag.py"


def _login_home_path(tmp_path):
    """Where a test's login HOME lives (not created). The one place the formula is written."""
    # a SIBLING of ENGRAM_HOME: some tests need ENGRAM_HOME itself to not exist yet
    return tmp_path.parent / (tmp_path.name + "-login-home")


def _login_home(tmp_path):
    """A temp login HOME. The venv is always $HOME/.engram/venv, whatever ENGRAM_HOME is
    (Borges' ruling on #3863), so the installer must never see the real HOME's venv."""
    home = _login_home_path(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    return home


def _login_home_guard(tmp_path):
    """Remove the sibling login home before and after a test (#3966).

    The sibling is not pytest's, so pytest never deletes it. Under
    `-o tmp_path_retention_policy=failed` a passing test's tmp_path IS deleted and its number
    reused by the next test whose name truncates to the same prefix, while the old sibling
    survives holding .engram/venv/bin, and the next _fake_venv() mkdir raises FileExistsError.
    """
    shutil.rmtree(_login_home_path(tmp_path), ignore_errors=True)
    yield
    shutil.rmtree(_login_home_path(tmp_path), ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_login_home(tmp_path):
    yield from _login_home_guard(tmp_path)


def _env(tmp_path, **overrides):
    real_gitconfig = Path(os.environ.get("HOME", "")) / ".gitconfig"
    env = {**os.environ, "ENGRAM_HOME": str(tmp_path), "HOME": str(_login_home(tmp_path))}
    if real_gitconfig.is_file() and "GIT_CONFIG_GLOBAL" not in env:
        env["GIT_CONFIG_GLOBAL"] = str(real_gitconfig)  # git identity only; the temp HOME has none
    env.pop("ENGRAM_BUILD_DIR", None)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _fixture_pid_is_running(pid):
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return status.returncode == 0 and not status.stdout.strip().startswith("Z")


def _cleanup_fixture_process_group(pid):
    """Clean only a process group named by this test's fake SSH PID file."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _path_without_timeout_wrappers(dest_dir):
    """Build a PATH value with no `timeout` or `gtimeout` binary.

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
    # The installer now delegates its portable timeout to Python. The active
    # test interpreter may not have been discoverable from PATH (for example,
    # a venv invoked by absolute path), so expose it explicitly as python3.
    python3 = dest_dir / "python3"
    if not python3.exists():
        python3.symlink_to(sys.executable)
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
    assert not _AXIS_LOG.search(result.stdout)

    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    assert json.loads((plugin_root / "platform.json").read_text())["platform"] == "codex"
    assert (plugin_root / ".codex-plugin" / "plugin.json").exists()
    manifest = json.loads((plugin_root / ".engram-build-manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert "multi_agent" not in manifest
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
    assert not _AXIS_LOG.search(result.stdout)

    plugin_root = tmp_path / "marketplace" / "plugins" / "engram"
    manifest = json.loads((plugin_root / ".engram-build-manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert "multi_agent" not in manifest
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


def test_install_local_marketplace_ignores_wrong_typed_multi_agent(tmp_path):
    """#4255: the installer reads neither multi_agent nor mode any more, so the
    #2965 fail-loud on a wrong-typed multi_agent (a string "true") is gone with
    the key: the install proceeds. install_tier keeps its own type check."""
    (tmp_path / "config.json").write_text(json.dumps({"multi_agent": "true"}))

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert "multi_agent" not in result.stderr


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


_AGENT_NAME = "engram-probe-fairy.toml"
_AGENT_WRITTEN = (
    'name = "engram-probe-fairy"\n'
    'description = "Probe"\n'
    'developer_instructions = "Probe"\n'
    '[plugins."engram@engram-local".mcp_servers.engram]\n'
    'enabled_tools = ["engram_query"]\n'
)


def _run_codex_agent_install(tmp_path, existing=None):
    """Run the real installer for the codex target against a fake plugin that ships one agent file.
    `existing` is a callable(agents_dir) that plants whatever sits in $CODEX_HOME/agents beforehand."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="codex", codex_agent=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text("#!/usr/bin/env bash\nprintf 'fake codex %s\\n' \"$*\"\n")
    codex.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    agents_dir = codex_home / "agents"
    agents_dir.mkdir(parents=True)
    if existing is not None:
        existing(agents_dir)
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-build", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root, CODEX_HOME=codex_home,
                 PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}"),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return result, agents_dir


def _agent_backups(agents_dir):
    return sorted(p for p in agents_dir.iterdir() if ".engram-backup." in p.name)


def test_codex_agent_file_that_differs_is_backed_up_and_the_backup_is_printed(tmp_path):
    """An existing engram-*.toml that differs from what would be written may hold the user's edits:
    it is saved to a stamped backup before it is replaced, and ONE printed line names the file and
    its backup. No question is asked (these are ENGRAM's own file names)."""
    mine = b'name = "engram-probe-fairy"\ndescription = "edited by the user, keep me"\n'
    result, agents = _run_codex_agent_install(tmp_path, lambda d: (d / _AGENT_NAME).write_bytes(mine))
    backups = _agent_backups(agents)
    assert len(backups) == 1 and backups[0].read_bytes() == mine, "the user's bytes were not kept in a backup"
    assert (agents / _AGENT_NAME).read_text() == _AGENT_WRITTEN
    lines = [ln for ln in result.stdout.splitlines() if "backup:" in ln and _AGENT_NAME in ln]
    assert len(lines) == 1, "expected exactly one printed backup line for the file:\n" + result.stdout
    assert str(agents / _AGENT_NAME) in lines[0] and str(backups[0]) in lines[0], lines[0]
    # control: the line filter above does find a backup line when one is planted in the same shape
    planted = f"  backup: {agents / _AGENT_NAME} -> {backups[0]}"
    assert [ln for ln in [planted] if "backup:" in ln and _AGENT_NAME in ln] == [planted]


def test_codex_agent_file_that_is_new_or_identical_gets_no_backup(tmp_path):
    result, agents = _run_codex_agent_install(tmp_path)  # absent: written
    assert (agents / _AGENT_NAME).read_text() == _AGENT_WRITTEN
    assert _agent_backups(agents) == [] and "backup:" not in result.stdout

def test_codex_agent_file_identical_to_what_would_be_written_is_not_touched(tmp_path):
    planted = {}

    def plant(d):
        (d / _AGENT_NAME).write_text(_AGENT_WRITTEN)
        planted["mtime"] = (d / _AGENT_NAME).stat().st_mtime_ns

    result, agents = _run_codex_agent_install(tmp_path, plant)
    assert _agent_backups(agents) == [] and "backup:" not in result.stdout
    assert (agents / _AGENT_NAME).stat().st_mtime_ns == planted["mtime"], "an identical file was rewritten"


def test_codex_agent_name_that_is_not_a_file_is_left_alone_with_a_warning(tmp_path):
    result, agents = _run_codex_agent_install(tmp_path, lambda d: (d / _AGENT_NAME).mkdir())
    assert (agents / _AGENT_NAME).is_dir() and not list((agents / _AGENT_NAME).iterdir())
    assert "WARN" in result.stdout and "not all installed" in result.stdout
    assert _agent_backups(agents) == []


def _persist_tier_block():
    """The python code of install-local-marketplace.sh's install_tier persist block."""
    text = INSTALLER.read_text()
    blocks = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "<<'PY'" in line:
            body = []
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "PY":
                    break
                body.append(lines[j])
            code = "\n".join(body)
            if "install_tier" in code and "apply_merge" in code:
                blocks.append(code)
    assert len(blocks) == 1, "the install_tier persist block moved or vanished"
    return blocks[0]


def _run_persist(tmp_path, content, tier):
    cfg = tmp_path / "config.json"
    cfg.write_bytes(content)
    proc = subprocess.run(
        [sys.executable, "-c", _persist_tier_block(), str(cfg), tier, str(REPO_ROOT / "src" / "engram")],
        capture_output=True, text=True,
    )
    return proc, cfg


def test_persisted_install_tier_is_merged_with_a_stamped_backup_first(tmp_path):
    old = json.dumps({"z": 1, "install_tier": "dev", "a": {"k": [1, 2]}}, indent=2).encode() + b"\n"
    proc, cfg = _run_persist(tmp_path, old, "convenience")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(cfg.read_text())
    assert data["install_tier"] == "convenience" and data["a"] == {"k": [1, 2]} and list(data) == ["z", "install_tier", "a"]
    backups = [p for p in tmp_path.iterdir() if ".engram-backup." in p.name]
    assert len(backups) == 1 and backups[0].read_bytes() == old


def test_persisted_install_tier_that_is_already_stored_writes_and_backs_up_nothing(tmp_path):
    old = b'{"install_tier":   "dev",  "note": "odd spacing is kept"}'
    proc, cfg = _run_persist(tmp_path, old, "dev")
    assert proc.returncode == 0, proc.stderr
    assert cfg.read_bytes() == old
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


@pytest.mark.parametrize("content", [b"{not json", b'{"install_tier": "dev", "install_tier": "essential"}', b"[1]"])
def test_persisted_install_tier_never_repairs_a_config_it_cannot_merge(tmp_path, content):
    proc, cfg = _run_persist(tmp_path, content, "convenience")
    assert proc.returncode != 0 and "left unchanged" in proc.stderr
    assert cfg.read_bytes() == content
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


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
    """Create a fake $HOME/.engram/venv/bin/python (HOME = the temp login home, the venv is
    never under ENGRAM_HOME: Borges' ruling on #3863) that logs every argv it sees.

    exit_code controls the fake python's exit status, so the failure-recovery
    path (pip install fails → install completes but exits non-zero) is testable.
    """
    venv_bin = _login_home(tmp_path) / ".engram" / "venv" / "bin"
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


def test_install_local_marketplace_convenience_tier_skips_forum_reqs_even_with_multi_agent_key(tmp_path):
    """#4255: forum requirements install exactly at the dev tier. A convenience-tier
    seat whose config still says multi_agent: true no longer gets them (#2944 did)."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "convenience", "multi_agent": True})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Provisioning venv at" in result.stdout
    assert "tier: convenience" in result.stdout
    assert "installing forum package dependencies" not in result.stdout
    calls = log_file.read_text()
    assert "requirements/requirements.txt" in calls
    assert "requirements/requirements-dev.txt" not in calls
    assert "src/forum/requirements.txt" not in calls


def test_install_local_marketplace_mode_multi_does_not_install_forum_reqs_below_dev(tmp_path):
    """#4255: the mode=="multi" spelling is not read either."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "convenience", "mode": "multi"})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "installing forum package dependencies" not in result.stdout
    calls = log_file.read_text()
    assert "src/forum/requirements.txt" not in calls


def test_install_local_marketplace_dev_tier_installs_forum_reqs_without_any_key(tmp_path):
    """#4255: the dev tier gets the forum test dependencies whatever the config says
    about the retired multi_agent axis (it used to need multi_agent: true)."""
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Dev tier: installing test dependencies" in result.stdout
    assert "Dev tier: installing forum package dependencies" in result.stdout
    calls = log_file.read_text()
    assert "src/forum/requirements.txt" in calls


def test_install_local_marketplace_multi_agent_dev_tier_installs_both(tmp_path):
    """Dev tier with the old multi_agent key still present: both requirements-dev.txt
    AND src/forum/requirements.txt install together."""
    (tmp_path / "config.json").write_text(
        json.dumps({"install_tier": "dev", "multi_agent": True})
    )
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert "Dev tier: installing test dependencies" in result.stdout
    assert "Dev tier: installing forum package dependencies" in result.stdout
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


def test_install_local_marketplace_skip_build_ignores_wrong_typed_multi_agent(tmp_path):
    """#4255: the --skip-build path no longer reads multi_agent either, so
    multi_agent: 1 (an int, not a bool) is not an error (it was, under #2965)."""
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

    assert result.returncode == 0, result.stderr
    assert "multi_agent" not in result.stderr


def test_install_local_marketplace_warns_on_missing_forum_requirements(tmp_path):
    """engram-alpha#2951 (Kepler's fold-in): a dev-tier seat (multi-agent until #4255)
    whose src/forum/requirements.txt is missing must get a WARN naming the gap,
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

    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
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
    assert "installing forum package dependencies" not in result.stdout
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


def _make_scratch_repo(tmp_path, *, tags=(), configure_origin=False):
    """A minimal standalone git repo carrying its own copy of the installer.

    tags: tag names to apply to the single commit at HEAD.

    configure_origin (#3103): when True, wires up a real bare 'origin'
    remote (dev + main both pushed to HEAD), so a deployable-pattern tag at
    HEAD can pass the confirmed-ancestor guard. The helper always copies the
    shared wrapper because the installer invokes it for tagged guard checks.
    Without local origin refs, the wrapper fails closed. Mirrors
    test_deploy_tag_guard.py's own `_make_simple_repo(..., configure_origin=...)`
    convention.
    """
    repo = tmp_path / "scratch-repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "src" / "engram").mkdir(parents=True)
    (repo / "src" / "engram" / "server.py").write_text("# stub\n")
    (repo / "src" / "build" / "engine").mkdir(parents=True)
    (repo / "src" / "build" / "engine" / "__init__.py").write_text(
        DEPLOY_TAG_INIT.read_text()
    )
    (repo / "src" / "build" / "engine" / "deploy_tag.py").write_text(
        DEPLOY_TAG_MODULE.read_text()
    )
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

    if configure_origin:
        origin_bare = tmp_path / "origin-bare.git"
        _git("init", "-q", "-b", "dev", "--bare", str(origin_bare))
        _git("remote", "add", "origin", str(origin_bare))
        _git("push", "-q", "origin", "dev")
        _git("push", "-q", "origin", "dev:refs/heads/main")
        _git("fetch", "-q", "origin")

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
    Callers that exercise a deliberately hanging fetch should pass a
    generous-but-finite value here so a regression fails loudly instead of
    hanging the whole suite.
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
    repo = _make_scratch_repo(tmp_path, tags=["v9.9.9-test"], configure_origin=True)
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    assert "Deployable tag(s) at HEAD" in result.stdout
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=v9.9.9-test" in marker


def test_tag_guard_accepts_deploy_tag(tmp_path):
    repo = _make_scratch_repo(tmp_path, tags=["deploy/2026-09-05-test"], configure_origin=True)
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


def test_tagged_head_without_local_origin_refs_fails_closed_but_override_records_none(tmp_path):
    """T5: a tagged HEAD without local origin refs is not deployable."""
    repo = _make_scratch_repo(tmp_path, tags=["v9.9.9-no-local-origin"])
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    refused = _run_scratch_installer(repo, engram_home)
    assert refused.returncode != 0
    assert "NOT confirmed" in refused.stderr

    allowed = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-untagged"], build_dir=build_root
    )
    assert allowed.returncode == 0, allowed.stderr
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=none" in marker
    assert "alpha_untagged_override=1" in marker


def test_tag_guard_multiple_tags_joined(tmp_path):
    repo = _make_scratch_repo(
        tmp_path, tags=["v1.0.0-a", "deploy/2026-09-05-b"], configure_origin=True
    )
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


# ── #3103 — untagged guard must decide on CONFIRMED ancestry, not raw tag
# presence (the shell/engine disagreement borges-funes found in #3103) ───────
#
# #3092/#3099 unified the engine's own `cli run upgrade` path onto the shared
# deployable_tag_at()/refresh_and_check_deployable_tag_at() predicate, which
# requires BOTH a deployable-pattern tag AND confirmed ancestry (HEAD is an
# ancestor of origin/dev or origin/main). The shell's #794 branch guard
# (the shared wrapper result, above) already consulted that same predicate — but
# the SEPARATE #2586 untagged guard a few lines below it still decided on
# raw `DEPLOY_TAGS` presence alone, independent of ancestry. The gap: on
# branch 'dev' itself, #794 passes trivially (branch name IS 'dev'), so the
# untagged guard was the ONLY thing standing between an unmerged/unpushed
# locally-tagged commit and a real deploy — and it was silently deciding on
# the wrong signal. Every existing unmerged-tag test (the #2711 section
# below) exercises this via a DETACHED HEAD or a non-dev 'stray' branch,
# where #794 already refuses regardless — none of them reaches the cell
# this section covers: HEAD literally ON branch 'dev', with a local,
# unpushed, tagged child commit.


def _make_scratch_repo_with_unpushed_tagged_dev_commit(tmp_path):
    """A scratch repo shaped for #3103's contract test.

    Commit graph:
        A (== refs/remotes/origin/{dev,main} — genuinely pushed)
        |
        B (local 'dev' tip, a child of A; tag v9.9.9 on B; NEVER pushed)

    Ends on branch 'dev' (not detached — this is the load-bearing
    difference from the #2711 fixtures below: HEAD's branch name really is
    'dev', so #794 passes trivially and only the #2586/#3103 untagged guard
    is in play). Mirrors _make_scratch_repo_with_ancestry's real-git-plumbing
    approach (a real bare 'origin', not a faked ref) one section below,
    reused here rather than inventing a new fixture shape.
    """
    repo = tmp_path / "scratch-repo-unpushed-tag"
    (repo / "tools").mkdir(parents=True)
    (repo / "src" / "engram").mkdir(parents=True)
    (repo / "src" / "engram" / "server.py").write_text("# stub\n")
    (repo / "src" / "build" / "engine").mkdir(parents=True)
    (repo / "src" / "build" / "engine" / "__init__.py").write_text(DEPLOY_TAG_INIT.read_text())
    (repo / "src" / "build" / "engine" / "deploy_tag.py").write_text(DEPLOY_TAG_MODULE.read_text())
    scratch_installer = repo / "tools" / "install-local-marketplace.sh"
    scratch_installer.write_text(INSTALLER.read_text())
    scratch_installer.chmod(0o755)

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

    origin_bare = tmp_path / "origin-bare-unpushed-tag.git"
    _git("init", "-q", "-b", "dev", "--bare", str(origin_bare))
    _git("remote", "add", "origin", str(origin_bare))
    _git("push", "-q", "origin", "dev")
    _git("push", "-q", "origin", "dev:refs/heads/main")
    _git("fetch", "-q", "origin")

    # A child commit on 'dev' itself, never pushed — HEAD stays on branch
    # 'dev' (so #794 passes trivially) but the tip is NOT merged history.
    (repo / "unpushed.txt").write_text("unpushed local work\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "unpushed local commit")
    sha_b = _git("rev-parse", "HEAD").stdout.strip()
    _git("tag", "v9.9.9")

    return repo, {"a": sha_a, "b": sha_b}


def test_shell_refuses_local_tag_on_unpushed_dev_commit(tmp_path):
    """#3103 contract test, shell half: a repo on branch 'dev' with a local,
    unpushed child commit carrying a deployable-pattern tag must REFUSE —
    not the old "proceeds because DEPLOY_TAGS is non-empty" behavior. The
    message must not claim "not a tagged commit" (false — it IS tagged);
    it must say the tag isn't confirmed merged, echoing the same
    "unconfirmed merged history" framing the fetch-failure WARNINGs use."""
    repo, commits = _make_scratch_repo_with_unpushed_tagged_dev_commit(tmp_path)
    engram_home = tmp_path / "engram-home"

    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not a tagged commit" not in result.stderr
    assert "v9.9.9" in result.stderr
    assert "not confirmed" in result.stderr or "NOT confirmed" in result.stderr
    assert "unconfirmed merged history" in result.stderr
    assert "#2586" in result.stderr
    assert "#3103" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_shell_allow_untagged_proceeds_for_unpushed_tagged_dev_commit(tmp_path):
    """Same fixture, with --allow-untagged: proceeds (same override as the
    plain-untagged case) and records alpha_tag=none — the override still
    means "don't trust the tag", regardless of which of the three cases
    triggered the guard."""
    repo, commits = _make_scratch_repo_with_unpushed_tagged_dev_commit(tmp_path)
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-untagged"], build_dir=build_root
    )

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "WARNING" in combined
    assert "v9.9.9" in combined
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=none" in marker
    assert "alpha_untagged_override=1" in marker


def test_engine_predicate_also_refuses_unpushed_tagged_dev_commit(tmp_path):
    """#3103 contract test, engine half: `cli run upgrade` (already correct
    per #3092/#3099 — not something this fix touches) routes entirely
    through refresh_and_check_deployable_tag_at(), which falls through to
    this same predicate regardless of fetch outcome (both refs are already
    genuinely up to date here, so no fetch is even required to see this).
    Asserting the shared predicate returns None (refused) against this SAME
    fixture is the faithful cross-check that the engine path independently
    refuses this exact scenario too — the two paths must agree, which is
    the whole point of #3103. `deployable_tag_at()` is a pure function of
    its `repo_path` argument (runs `git -C repo_path ...` internally), so
    it's imported from THIS repo's own tools/engine compat symlink (same
    import test_deploy_tag_guard.py uses) and simply pointed at the scratch
    repo — no need to duplicate deploy_tag.py inside the scratch repo for
    this half of the check (that copy exists solely for the shell script's
    own `python3 -c` invocation, tested separately above). (Exercising the
    full `cli run upgrade` flow end-to-end would additionally require
    config.json/.deployed-version/changeset-ack scaffolding unrelated to
    the deploy-tag guard itself — see this PR's handoff for why the
    predicate-level check was chosen instead.)"""
    from tools.engine.deploy_tag import deployable_tag_at

    repo, commits = _make_scratch_repo_with_unpushed_tagged_dev_commit(tmp_path)

    assert deployable_tag_at(str(repo)) is None


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
    (repo / "src" / "build" / "engine").mkdir(parents=True)
    (repo / "src" / "build" / "engine" / "__init__.py").write_text(DEPLOY_TAG_INIT.read_text())
    (repo / "src" / "build" / "engine" / "deploy_tag.py").write_text(DEPLOY_TAG_MODULE.read_text())
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


def test_detached_head_at_non_ancestor_commit_with_allow_branch_alone_still_refuses_on_untagged_guard(
    tmp_path,
):
    """Post-#3103: --allow-branch alone no longer suffices for this case.

    Before #3103, the #2586 untagged guard decided on raw DEPLOY_TAGS
    presence alone — so once --allow-branch cleared the #794 branch guard,
    a stray-but-tagged commit passed the untagged guard for free (DEPLOY_TAGS
    was non-empty). #3103 makes the untagged guard ALSO require confirmed
    ancestry, so this exact scenario now falls into case 3 (tagged, but NOT
    a confirmed ancestor) and needs --allow-untagged too — --allow-branch
    and --allow-untagged guard two independent risks (#794: unmerged branch;
    #2586/#3103: unconfirmed-merged health-snapshot provenance), and clearing
    one says nothing about the other. See
    test_detached_head_at_non_ancestor_commit_builds_with_both_overrides
    immediately below for the "override still works" coverage this test
    used to carry alone."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "deploy/2026-09-07-stray")
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo, engram_home, extra_args=["--allow-branch"], build_dir=build_root
    )

    assert result.returncode != 0
    # Case-3 message (tagged but NOT confirmed ancestor), not case-1 (no tag
    # at all) — the guard must not claim "not a tagged commit" when HEAD
    # plainly carries one.
    assert "not a tagged commit" not in result.stderr
    assert "deploy/2026-09-07-stray" in result.stderr
    assert "NOT confirmed to be merged" in result.stderr
    assert "#3103" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


def test_detached_head_at_non_ancestor_commit_builds_with_both_overrides(tmp_path):
    """The override still works for the non-ancestor case — the fix narrows
    the #794 exception and tightens the #2586 untagged guard's ancestry
    check, but the escape hatch remains: pass BOTH --allow-branch (for #794)
    AND --allow-untagged (for #2586/#3103), and the build proceeds. This is
    the "override still works" coverage the pre-#3103 single-flag test used
    to carry — split here because two independent guards now require two
    independent overrides for this scenario."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "deploy/2026-09-07-stray")
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo,
        engram_home,
        extra_args=["--allow-branch", "--allow-untagged"],
        build_dir=build_root,
    )

    assert result.returncode == 0, result.stderr
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=none" in marker
    assert "alpha_untagged_override=1" in marker


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


# ── #3133 — bounded refresh falls through to local tracked refs ──────────────


def test_offline_origin_uses_local_refs_for_merged_tag_in_shell_and_wrapper(tmp_path):
    """T1: a failed refresh still permits a tagged local ancestor."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "v1.0.0-ancestor-test")
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    from tools.engine.deploy_tag import refresh_and_check_deployable_tag_at

    assert refresh_and_check_deployable_tag_at(str(repo), timeout=2.0) == "v1.0.0-ancestor-test"

    result = _run_scratch_installer(repo, engram_home, build_dir=build_root)

    assert result.returncode == 0, result.stderr
    marker = (engram_home / ".deployed-version").read_text()
    assert "alpha_tag=v1.0.0-ancestor-test" in marker
    assert "alpha_untagged_override=1" not in marker


def test_offline_origin_refuses_tagged_child_outside_local_refs(tmp_path):
    """T2: local tracked refs cannot authorize an unpushed tagged child."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "deploy/2026-09-07-stray")
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"

    from tools.engine.deploy_tag import refresh_and_check_deployable_tag_at

    assert refresh_and_check_deployable_tag_at(str(repo), timeout=2.0) is None

    result = _run_scratch_installer(repo, engram_home)

    assert result.returncode != 0
    assert "not 'dev'" in result.stderr
    assert "#794" in result.stderr
    assert not (engram_home / ".deployed-version").exists()


# ── #3133 — timeout remains bounded without shell timeout binaries ───────────


@pytest.mark.parametrize("timeout_value", ["2", "2s", "0.5m", "1h", "0.01d"])
def test_installer_accepts_positive_numeric_and_gnu_suffix_fetch_timeouts(
    tmp_path, timeout_value
):
    """The shell passes the shared parser's positive decimal/suffix contract."""
    repo = _make_scratch_repo(
        tmp_path, tags=["v9.9.9-timeout-value"], configure_origin=True
    )
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")

    result = _run_scratch_installer(
        repo,
        engram_home,
        extra_env={"ENGRAM_INSTALL_FETCH_TIMEOUT_SECS": timeout_value},
        build_dir=build_root,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "alpha_tag=v9.9.9-timeout-value" in (engram_home / ".deployed-version").read_text()


@pytest.mark.parametrize(
    "timeout_value", ["", "s", "0", "-1", "nan", "inf", "-inf", "garbage"]
)
def test_installer_rejects_invalid_fetch_timeout_before_fetch(tmp_path, timeout_value):
    """Bad config is a config error, never an offline authorization fallback."""
    repo = _make_scratch_repo(
        tmp_path, tags=["v9.9.9-invalid-timeout"], configure_origin=True
    )
    fetch_started = tmp_path / "fetch-started"
    fake_ssh = tmp_path / "fake-ssh.sh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "printf started > \"$FETCH_STARTED_FILE\"\n"
        "exit 1\n"
    )
    fake_ssh.chmod(0o755)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "ssh://localhost/nonexistent-repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    result = _run_scratch_installer(
        repo,
        tmp_path / "engram-home",
        extra_env={
            "ENGRAM_INSTALL_FETCH_TIMEOUT_SECS": timeout_value,
            "GIT_SSH_COMMAND": str(fake_ssh),
            "FETCH_STARTED_FILE": str(fetch_started),
        },
        timeout=10,
    )

    assert result.returncode != 0
    assert "Invalid ENGRAM_INSTALL_FETCH_TIMEOUT_SECS" in result.stderr
    assert "WARNING" not in result.stderr
    assert not fetch_started.exists(), "invalid timeout reached git fetch"


def test_installer_fetch_timeout_default_is_fifteen_seconds():
    assert 'FETCH_TIMEOUT_INPUT="15"' in INSTALLER.read_text()


def test_fetch_timeout_falls_through_to_local_refs_without_shell_wrapper(tmp_path):
    """T4: a real fake-SSH hang falls through to local refs and preserves the tag."""
    repo, commits = _make_scratch_repo_with_ancestry(tmp_path)
    _checkout(repo, "v1.0.0-ancestor-test")

    fake_ssh = tmp_path / "fake-ssh.sh"
    ssh_pid_file = tmp_path / "fake-ssh.pid"
    child_pid_file = tmp_path / "fake-ssh-child.pid"
    fake_ssh.write_text(
        # Record the ssh pid FIRST: under load the installer's 2s bound can
        # fire before a later-written pid file exists (#3292); recorded first,
        # it always does.  Then run the sleeper as a GRANDCHILD of git (a
        # backgrounded child of this ssh) so the child pid proves the group
        # kill reaches descendants two levels down.
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$$\" > \"$FAKE_SSH_PID_FILE\"\n"
        "sleep 30 &\n"
        "printf '%s\\n' \"$!\" > \"$FAKE_SSH_CHILD_PID_FILE\"\n"
        "wait\n"
    )
    fake_ssh.chmod(0o755)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "ssh://localhost/nonexistent-repo.git"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    masked_path = _path_without_timeout_wrappers(tmp_path / "masked-path-bin-timeout")

    try:
        start = time.monotonic()
        result = _run_scratch_installer(
            repo, engram_home,
            extra_env={
                "ENGRAM_INSTALL_FETCH_TIMEOUT_SECS": "2",
                "GIT_SSH_COMMAND": str(fake_ssh),
                "FAKE_SSH_PID_FILE": str(ssh_pid_file),
                "FAKE_SSH_CHILD_PID_FILE": str(child_pid_file),
                "PATH": masked_path,
            },
            build_dir=build_root,
            timeout=30,
        )
        elapsed = time.monotonic() - start

        # Generous ceiling above the 2s cap (not a tight bound — CI/sandboxed
        # hosts vary) but far below the fixture's 30s sleeper.  This is a real
        # unreachable-origin hang, not a fast failure substituted for it.
        assert elapsed < 8, f"installer took {elapsed:.1f}s — the fetch was not bounded"
        # Lower bound: the 2s bound was actually waited on against a hang, not
        # a fast failure standing in for it (slack under 2 for timer/startup).
        assert elapsed >= 1.5, f"installer took {elapsed:.1f}s — no hang was waited on"
        assert result.returncode == 0, result.stderr
        assert "UNBOUNDED" not in (result.stdout + result.stderr)
        marker = (engram_home / ".deployed-version").read_text()
        assert "alpha_tag=v1.0.0-ancestor-test" in marker
        # The ssh pid is written first, so it must exist (flake-proof).
        assert ssh_pid_file.exists()
        assert not _fixture_pid_is_running(int(ssh_pid_file.read_text().strip()))
        # The grandchild pid is written second; if the 2s bound fired before
        # bash reached it (heavy load) there is nothing to check, so skip only
        # this assertion -- never fail on its absence.
        if child_pid_file.exists():
            assert not _fixture_pid_is_running(
                int(child_pid_file.read_text().strip())
            )
    finally:
        # A direct-kill-only mutant can leave this known fake-SSH group alive.
        for pid_file in (ssh_pid_file, child_pid_file):
            if pid_file.exists():
                _cleanup_fixture_process_group(int(pid_file.read_text().strip()))


# ── #3133 — no timeout/gtimeout prerequisite ─────────────────────────────────


def test_fetch_without_shell_timeout_binaries_uses_python_wrapper(tmp_path):
    """T4: PATH without timeout/gtimeout remains bounded by Python."""
    repo = _make_scratch_repo(
        tmp_path, tags=["v9.9.9-nowrapper-test"], configure_origin=True
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    engram_home = tmp_path / "engram-home"
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    masked_path = _path_without_timeout_wrappers(tmp_path / "masked-path-bin")

    result = _run_scratch_installer(
        repo, engram_home,
        extra_env={"PATH": masked_path},
        build_dir=build_root,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "UNBOUNDED" not in (result.stdout + result.stderr)
    assert "alpha_tag=v9.9.9-nowrapper-test" in (engram_home / ".deployed-version").read_text()


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
    /bin/bash and structurally cannot exercise this bug class. A real
    macOS machine's frozen bash 3.2 does: this test runs for-real only on a
    Mac seat, run locally whenever a shipped shell script changes. Hosted
    macOS runners are banned from CI (CLAUDE.md, "No macOS runners in CI",
    #3192).

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


def test_install_local_marketplace_looks_for_the_venv_under_home_not_under_engram_home(tmp_path):
    """Borges' ruling on #3863: the venv is always $HOME/.engram/venv. With ENGRAM_HOME at a
    path holding a space (and a decoy venv there), the installer provisions the HOME one."""
    eh = tmp_path / "my engram home"
    eh.mkdir()
    (eh / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    decoy_log, real_log = tmp_path / "decoy.log", tmp_path / "real.log"
    decoy_bin = eh / "venv" / "bin"
    decoy_bin.mkdir(parents=True)
    (decoy_bin / "python").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{decoy_log}"\n')
    (decoy_bin / "python").chmod(0o755)
    _fake_venv(tmp_path, real_log)

    result = _run_installer_skip_build(tmp_path, env_extra={"ENGRAM_HOME": eh})

    assert f"Provisioning venv at {_login_home(tmp_path)}/.engram/venv" in result.stdout
    assert "-m pip install --quiet -r" in real_log.read_text()
    assert not decoy_log.exists(), "the installer used a venv under ENGRAM_HOME"


# --- login-home isolation (#3966) -------------------------------------------------------
# These cells drive _login_home_guard() directly instead of leaning on two tests running one
# after the other: test order is not stable under pytest-xdist, and a cell that only fails
# when a particular earlier test happened to run first is not a control. Driving the guard
# by hand plants the stale sibling itself, so the result is the same on every run.


def _plant_stale_venv(tmp_path):
    stale = _login_home_path(tmp_path) / ".engram" / "venv" / "bin"
    stale.mkdir(parents=True)
    (stale / "python").write_text("stale")
    return stale


def test_login_home_guard_removes_a_stale_sibling_before_the_body(tmp_path):
    """Pre-clean half: a sibling left by an earlier test that held this same tmp_path number
    (tmp_path_retention_policy=failed deletes passing tmp_paths but not their siblings)."""
    stale = _plant_stale_venv(tmp_path)
    guard = _login_home_guard(tmp_path)
    next(guard)  # the point where the test body would run
    assert not stale.exists()
    assert not _login_home_path(tmp_path).exists()
    guard.close()


def test_login_home_guard_removes_the_sibling_after_the_body(tmp_path):
    """Post-clean half: what the body created must not outlive the test."""
    guard = _login_home_guard(tmp_path)
    next(guard)
    stale = _plant_stale_venv(tmp_path)  # what a _fake_venv caller leaves behind
    with pytest.raises(StopIteration):
        next(guard)
    assert not stale.exists()
    assert not _login_home_path(tmp_path).exists()


def test_login_home_guard_is_wired_as_an_autouse_fixture(request):
    assert "_isolate_login_home" in request.fixturenames


def test_fake_venv_mkdir_stays_strict(tmp_path):
    """_fake_venv must keep failing loudly on a leaked venv, not paper over it (#3966)."""
    log = tmp_path / "log"
    _fake_venv(tmp_path, log)
    with pytest.raises(FileExistsError):
        _fake_venv(tmp_path, log)


def test_installer_runs_the_stale_process_check_informationally_after_the_version_stamp():
    """#3938.  Grep-level structural cell (it does not run the installer): the stale-process block
    exists after the `.deployed-version` write, runs the tool with --informational inside an `if !`
    guard so a non-zero exit (a broken tool is exit 4) cannot abort the install under `set -e`
    (the pipeline is the guard's condition, and pipefail is on), and finds the tool through the
    script's own location rather than a fixed path."""
    text = INSTALLER.read_text()
    stamp = text.index('> "$ENGRAM_HOME/.deployed-version"')
    block_start = text.index("check_stale_processes.py", stamp)
    assert block_start > stamp
    assert re.search(r"^set -euo pipefail$", text, re.M)
    assert re.search(r'^_stale_tool="\$\(dirname "\$_self"\)/check_stale_processes\.py"$', text, re.M)
    assert text.index('_stale_tool="$(dirname "$_self")') > stamp
    run = [ln for ln in text[stamp:].splitlines() if 'python3 "$_stale_tool"' in ln]
    assert len(run) == 1, run
    assert run[0].lstrip().startswith("if ! ")
    assert "--informational" in run[0]
    assert 'WARNING: check_stale_processes.py could not run' in text[stamp:]


# ── #4099: an unreadable checkout fails loudly, never silently ───────────────
#
# `REPO_ROOT="$(git ... rev-parse --show-toplevel 2>/dev/null)"` used to exit the
# script under `set -e` with git's own status (128) and NO output when git refused
# the checkout (e.g. "detected dubious ownership"), before the guard on the next
# line could print anything.

_DUBIOUS_TEXT = (
    "fatal: detected dubious ownership in repository at '{path}'\n"
    "To add an exception for this directory, call:\n\n"
    "\tgit config --global --add safe.directory {path}\n"
)


def _fake_git_failing_toplevel(tmp_path, stderr_text, *, exit_code=128):
    """A `git` shim that fails `rev-parse --show-toplevel` and passes everything else
    to the real git. Returns the PATH value to run the installer under."""
    real_git = shutil.which("git")
    shim_dir = tmp_path / "fake-git-bin"
    shim_dir.mkdir()
    text_file = tmp_path / "fake-git-stderr.txt"
    text_file.write_text(stderr_text)
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--show-toplevel" ]; then\n'
        f'    cat "{text_file}" >&2\n'
        f"    exit {exit_code}\n"
        "  fi\n"
        "done\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    return f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _run_with_failing_toplevel(tmp_path, stderr_text):
    repo = _make_scratch_repo(tmp_path)
    engram_home = tmp_path / "engram-home"
    path = _fake_git_failing_toplevel(tmp_path, stderr_text.replace("{path}", str(repo)))
    result = _run_scratch_installer(
        repo, engram_home, extra_env={"PATH": path},
        extra_args=("--allow-branch", "--allow-untagged"),
    )
    return repo, result


def test_unresolvable_repo_root_exits_one_with_message(tmp_path):
    """C1: git failing on --show-toplevel exits 1 (not 128) with a loud stderr line."""
    _, result = _run_with_failing_toplevel(tmp_path, _DUBIOUS_TEXT)

    assert result.returncode == 1, (result.returncode, result.stderr)
    assert "could not resolve repo root" in result.stderr


def test_unresolvable_repo_root_carries_git_stderr_and_safe_directory_remedy(tmp_path):
    """C2: git's own stderr is shown, and a dubious-ownership refusal names the remedy
    with the actual checkout path."""
    repo, result = _run_with_failing_toplevel(tmp_path, _DUBIOUS_TEXT)

    assert "detected dubious ownership" in result.stderr  # git's text, not swallowed
    assert f"git config --global --add safe.directory '{repo}'" in result.stderr


def test_unresolvable_repo_root_without_dubious_ownership_has_no_remedy(tmp_path):
    """C2 (negative): a different git failure still gets git's text and exit 1, but is
    not told to run a safe.directory command that would not help."""
    _, result = _run_with_failing_toplevel(tmp_path, "fatal: some other git failure\n")

    assert result.returncode == 1
    assert "could not resolve repo root" in result.stderr
    assert "some other git failure" in result.stderr
    assert "safe.directory" not in result.stderr


def test_resolvable_repo_root_is_unchanged(tmp_path):
    """C3: with a working git, the repo root resolves and the install proceeds past it."""
    repo = _make_scratch_repo(tmp_path)
    result = _run_scratch_installer(
        repo, tmp_path / "engram-home", extra_args=("--allow-branch", "--allow-untagged")
    )

    assert "could not resolve repo root" not in result.stderr
    assert "safe.directory" not in result.stderr


# ── #4100: a missing config.json is the fresh-install default, a broken one fails ──


def _run_skip_build_no_check(tmp_path):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    return subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def test_venv_block_missing_config_json_uses_fresh_install_default(tmp_path):
    """C4: venv present + no config.json -> convenience / single-agent, one log line,
    no exit 1 from this block, and the block reached pip with the default tier."""
    assert not (tmp_path / "config.json").exists()
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_skip_build_no_check(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "failed to parse" not in result.stderr
    notes = [ln for ln in result.stdout.splitlines() if "does not exist yet" in ln]
    assert len(notes) == 1, result.stdout
    assert "config.json" in notes[0] and "fresh-install default" in notes[0]
    assert "Provisioning venv at" in result.stdout
    assert "tier: convenience" in result.stdout
    calls = log_file.read_text()
    assert "requirements/requirements.txt" in calls
    assert "requirements-dev.txt" not in calls  # not tier dev
    assert "src/forum/requirements.txt" not in calls  # not multi-agent


@pytest.mark.parametrize("content", ["{not valid json", "", json.dumps({"install_tier": 0})])
def test_venv_block_malformed_config_json_still_fails_loud(tmp_path, content):
    """C5: a present-but-broken config.json still exits 1 with the existing message
    (the #2951/#2965 contract is not weakened by C4)."""
    (tmp_path / "config.json").write_text(content)
    _fake_venv(tmp_path, tmp_path / "venv-calls.log")

    result = _run_skip_build_no_check(tmp_path)

    assert result.returncode == 1
    assert "failed to parse" in result.stderr
    assert "does not exist yet" not in result.stdout


def test_venv_block_config_json_is_directory_still_fails_loud(tmp_path):
    """C5b: a config.json that is a DIRECTORY is present, not missing: the
    fresh-install default applies only when nothing exists at the path."""
    (tmp_path / "config.json").mkdir()
    _fake_venv(tmp_path, tmp_path / "venv-calls.log")

    result = _run_skip_build_no_check(tmp_path)

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "failed to parse" in result.stderr
    assert "does not exist yet" not in result.stdout


def test_venv_block_config_json_dangling_symlink_still_fails_loud(tmp_path):
    """C5c: a DANGLING symlink config.json is present (a link exists), not missing."""
    (tmp_path / "config.json").symlink_to(tmp_path / "no-such-target.json")
    assert not (tmp_path / "config.json").exists()
    _fake_venv(tmp_path, tmp_path / "venv-calls.log")

    result = _run_skip_build_no_check(tmp_path)

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "failed to parse" in result.stderr
    assert "does not exist yet" not in result.stdout


def test_venv_block_absent_venv_with_missing_config_is_skipped(tmp_path):
    """C6: no venv -> the block is skipped (no note, no provisioning), as before."""
    assert not (tmp_path / "config.json").exists()

    result = _run_skip_build_no_check(tmp_path)

    assert result.returncode == 0
    assert "Provisioning venv at" not in result.stdout
    assert "does not exist yet" not in result.stdout


# ── #4103: the tier-aware venv block pre-warms the embedding model ───────────
#
# Contract (slice 2 spec, A1-A6). The pre-warm calls the RUNTIME's own load path
# from the just-assembled plugin tree (engram_core._embedder._load_model; model name
# from engram_core._get_embedding_config), decides "already cached" with the server's
# own predicate (server._embedding_cache_status), and never carries a model string of
# its own. These cells run the real snippet under a real python against a FAKE plugin
# tree (fake server.py / engram_core.py), so no network and no real model.

_FAKE_SERVER_PY = '''\
import os
def _cache_root():
    return os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
def _embedding_cache_status(model_name):
    return os.path.exists(os.path.join(_cache_root(), "cached-" + model_name.replace("/", "_")))
def _looks_like_local_model_path(model_name):
    return os.path.isabs(model_name) or model_name.count("/") > 1
'''

_FAKE_CORE_PY = '''\
import json, os
DEFAULT_EMBEDDING_MODEL = "fake-default-model"
def _get_embedding_config():
    path = os.path.join(os.environ["ENGRAM_HOME"], "config.json")
    try:
        with open(path) as f:
            return json.load(f).get("embedding", {"model": DEFAULT_EMBEDDING_MODEL})
    except OSError:
        return {"model": DEFAULT_EMBEDDING_MODEL}
class _Embedder:
    def __init__(self):
        self._model = None
    def _load_model(self, model_name):
        with open(os.environ["FAKE_LOAD_LOG"], "a") as f:
            f.write(model_name + "\\n")
        if os.environ.get("FAKE_DOWNLOAD_FAIL"):
            return
        if os.environ.get("FAKE_LOAD_NOFILL"):
            # the loader "succeeds" but fills a cache the server's predicate does not read
            self._model = object()
            return
        root = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        os.makedirs(root, exist_ok=True)
        open(os.path.join(root, "cached-" + model_name.replace("/", "_")), "w").close()
        self._model = object()
_embedder = _Embedder()
'''


def _prewarm_venv(tmp_path, log_file, *, pip_rc=0):
    """A venv python for the pre-warm cells: pip calls are logged no-ops (exit pip_rc);
    anything else (the pre-warm snippet) runs under the real interpreter, stdin and all."""
    venv_bin = _login_home(tmp_path) / ".engram" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log_file}"\n'
        f'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit {pip_rc}; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    fake_py.chmod(0o755)
    return fake_py


def _run_prewarm(tmp_path, *, config=None, env_extra=None, pip_rc=0, with_venv=True):
    """Run the installer (--skip-build) against a fake plugin tree that carries the fake
    server/core. Returns (CompletedProcess, load_log Path)."""
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    (build_root / "server.py").write_text(_FAKE_SERVER_PY)
    (build_root / "engram_core.py").write_text(_FAKE_CORE_PY)
    (tmp_path / "config.json").write_text(
        json.dumps(config if config is not None else {"install_tier": "convenience"})
    )
    calls = tmp_path / "venv-calls.log"
    load_log = tmp_path / "load.log"
    if with_venv:
        _prewarm_venv(tmp_path, calls, pip_rc=pip_rc)
    extra = {"FAKE_LOAD_LOG": load_log, "HF_HOME": tmp_path / "hf-home"}
    extra.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code",
         "--skip-build", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=build_root, **extra),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return result, load_log


def _prewarm_lines(result):
    return [ln for ln in (result.stdout + result.stderr).splitlines() if "pre-warm:" in ln]


def test_prewarm_already_cached_logs_one_line_and_does_not_download(tmp_path):
    """A2: cached by the server's predicate -> one 'already cached' line, no load call."""
    hf = tmp_path / "hf-home"
    hf.mkdir()
    (hf / "cached-fake-default-model").write_text("")

    result, load_log = _run_prewarm(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = _prewarm_lines(result)
    assert len(lines) == 1 and "pre-warm: fake-default-model already cached" in lines[0], lines
    assert str(hf / "hub") in lines[0], lines  # the cache dir the check used is printed
    assert not load_log.exists(), "a cached model must not be loaded/downloaded again"


def test_prewarm_cache_dir_in_the_lines_follows_hf_hub_cache_first(tmp_path):
    """The printed cache dir uses the precedence HF_HUB_CACHE > HF_HOME/hub."""
    hub = tmp_path / "explicit-hub"
    result, _ = _run_prewarm(tmp_path, env_extra={"HF_HUB_CACHE": hub})

    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = _prewarm_lines(result)
    assert len(lines) == 1 and "downloaded to " + str(hub) in lines[0], lines
    assert str(tmp_path / "hf-home" / "hub") not in lines[0], lines


def test_prewarm_downloads_through_the_runtime_load_path_when_not_cached(tmp_path):
    """A3 success: not cached -> the runtime's _load_model is called with the model name
    from the runtime's own config reader (A1: the installer carries no model string)."""
    result, load_log = _run_prewarm(
        tmp_path, config={"install_tier": "convenience", "embedding": {"model": "org/custom-model"}}
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert load_log.read_text().splitlines() == ["org/custom-model"]
    lines = _prewarm_lines(result)
    assert len(lines) == 1 and "pre-warm: org/custom-model" in lines[0], lines


def test_prewarm_falls_back_to_the_runtime_default_model_when_config_names_none(tmp_path):
    """A1: with no embedding.model in config the name is the runtime's DEFAULT, not an installer literal."""
    result, load_log = _run_prewarm(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert load_log.read_text().splitlines() == ["fake-default-model"]


def test_prewarm_failure_exits_nonzero_naming_model_cache_dir_and_remedy(tmp_path):
    """A3 failure: offline / HF error -> install exits non-zero (via _venv_fail), message
    names the model, the cache dir it tried, and the exact one-line remedy."""
    result, _ = _run_prewarm(tmp_path, env_extra={"FAKE_DOWNLOAD_FAIL": "1"})

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "fake-default-model" in out
    assert str(tmp_path / "hf-home") in out  # the cache dir it tried (HF_HOME honoured)
    assert "SentenceTransformer('fake-default-model')" in out  # the remedy command
    assert str(_login_home(tmp_path) / ".engram" / "venv" / "bin" / "python") in out
    assert "ERROR: venv runtime-requirements provisioning failed" in out  # the _venv_fail trap ran


def test_prewarm_fails_when_the_model_loaded_but_the_server_predicate_is_still_false(tmp_path):
    """The success test is the SERVER's predicate after the load, not the loader's own
    _model: a load that fills a cache the server does not read (e.g. SENTENCE_TRANSFORMERS_HOME)
    exits non-zero with the same failure message plus a sentence saying so."""
    result, load_log = _run_prewarm(tmp_path, env_extra={"FAKE_LOAD_NOFILL": "1"})

    out = result.stdout + result.stderr
    assert load_log.exists(), "the loader must have been called"
    assert result.returncode != 0, out
    assert "downloaded to" not in out, out
    assert "fake-default-model" in out
    assert str(tmp_path / "hf-home") in out  # the cache dir tried
    assert "SentenceTransformer('fake-default-model')" in out  # the remedy
    assert "loaded but is not where the server looks" in out
    assert "ERROR: venv runtime-requirements provisioning failed" in out  # the _venv_fail trap ran


def test_prewarm_honours_hf_home_and_fills_the_cache_the_server_will_read(tmp_path):
    """A4: HF_HOME is passed through untouched; the cache it fills is the one the predicate reads."""
    custom = tmp_path / "shared-hf"
    result, _ = _run_prewarm(tmp_path, env_extra={"HF_HOME": custom})

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (custom / "cached-fake-default-model").exists()
    assert not (_login_home(tmp_path) / ".cache").exists()


def test_prewarm_never_downloads_a_local_path_model_that_does_not_exist(tmp_path):
    """A5: a path-shaped model name is never downloaded; a missing path fails with a message saying so."""
    missing = str(tmp_path / "no" / "such" / "model-dir")
    result, load_log = _run_prewarm(
        tmp_path, config={"install_tier": "convenience", "embedding": {"model": missing}}
    )

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert missing in out and "does not exist" in out
    assert not load_log.exists(), "a local path must never reach the download path"


def test_prewarm_is_skipped_when_the_venv_is_absent(tmp_path):
    """A6: no venv -> the block is skipped as before; no pre-warm line, exit 0."""
    result, load_log = _run_prewarm(tmp_path, with_venv=False)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _prewarm_lines(result) == []
    assert not load_log.exists()


def test_prewarm_does_not_run_after_a_failed_runtime_pip_install(tmp_path):
    """The pre-warm runs only AFTER the runtime pip install succeeds."""
    result, load_log = _run_prewarm(tmp_path, pip_rc=1)

    assert result.returncode != 0
    assert _prewarm_lines(result) == []
    assert not load_log.exists()


# ═══ #4255 slice 1 (S1.8): the installer reads neither multi_agent nor mode ════════════════════
# Forum requirements install exactly when the tier is dev, whatever either key says.
# The shipped file listing in the build log used to name the second CLAUDE.md template (slice 3),
# so look for the axis as a setting (`multi-agent: yes`, `multi_agent=true`, `--multi-agent`), not a word.
_AXIS_LOG = re.compile(r"multi[-_]agent\s*[:=]|--multi-agent", re.I)
_AXIS_KEYS = {
    "no-key": {},
    "multi_agent-true": {"multi_agent": True},
    "multi_agent-false": {"multi_agent": False},
    "mode-multi": {"mode": "multi"},
    "wrong-typed": {"multi_agent": "yes"},
}


@pytest.mark.parametrize("keys", list(_AXIS_KEYS.values()), ids=list(_AXIS_KEYS))
@pytest.mark.parametrize("tier", ["essential", "convenience", "dev"])
def test_forum_requirements_install_exactly_at_dev_tier_whatever_the_keys_say(tier, keys, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": tier, **keys}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={"LC_ALL": "C"})

    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = log_file.read_text()
    assert ("src/forum/requirements.txt" in calls) == (tier == "dev"), calls
    assert ("installing forum package dependencies" in result.stdout) == (tier == "dev")
    assert not _AXIS_LOG.search(result.stdout + result.stderr)


def test_forum_requirements_failure_at_dev_tier_warns_and_continues_like_the_dev_requirements(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file, exit_code=1)

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code", "--skip-build", "--skip-add",
         "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, ENGRAM_BUILD_DIR=_skip_build_root(tmp_path)),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )

    assert result.returncode != 0  # the failed RUNTIME install is what flags the exit
    assert "WARN: venv dev-requirements install failed" in result.stdout
    assert "WARN: venv forum-requirements install failed" in result.stdout


def _skip_build_root(tmp_path):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root, platform="claude-code")
    return build_root


def test_no_venv_means_no_forum_requirements_even_at_dev_tier_with_the_old_keys(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev", "multi_agent": True}))

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert result.returncode == 0
    assert "Provisioning venv at" not in result.stdout
    assert "forum package" not in result.stdout.lower()


@pytest.mark.parametrize("value", ["true", 1, [], {"x": 1}])
def test_a_wrong_typed_multi_agent_no_longer_fails_the_install(value, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "convenience", "multi_agent": value}))
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert result.returncode == 0
    assert "multi_agent" not in result.stderr
    assert "src/forum/requirements.txt" not in log_file.read_text()


def test_missing_forum_requirements_file_at_dev_tier_warns_naming_the_tier(tmp_path):
    real_forum_req = REPO_ROOT / "src" / "forum" / "requirements.txt"
    parked = tmp_path / "requirements.txt.parked"
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "dev"}))
    _fake_venv(tmp_path, tmp_path / "venv-calls.log")

    shutil.move(str(real_forum_req), str(parked))
    try:
        result = _run_installer_skip_build(tmp_path, env_extra={})
    finally:
        shutil.move(str(parked), str(real_forum_req))

    warn = [ln for ln in result.stdout.splitlines() if ln.startswith("[install-local-marketplace] WARN") and "forum" in ln]
    assert len(warn) == 1, result.stdout
    assert "dev tier" in warn[0] and "not found" in warn[0]
    assert "multi-agent" not in warn[0].lower()


def test_missing_forum_requirements_file_below_dev_tier_is_silent_even_with_the_old_key(tmp_path):
    real_forum_req = REPO_ROOT / "src" / "forum" / "requirements.txt"
    parked = tmp_path / "requirements.txt.parked"
    (tmp_path / "config.json").write_text(json.dumps({"install_tier": "convenience", "multi_agent": True}))
    _fake_venv(tmp_path, tmp_path / "venv-calls.log")

    shutil.move(str(real_forum_req), str(parked))
    try:
        result = _run_installer_skip_build(tmp_path, env_extra={})
    finally:
        shutil.move(str(parked), str(real_forum_req))

    assert not [ln for ln in result.stdout.splitlines() if "WARN" in ln and "forum" in ln]


@pytest.mark.parametrize("keys", [{}, {"multi_agent": True}, {"mode": "multi", "multi_agent": False}],
                         ids=["no-key", "multi_agent-true", "mode-and-false"])
def test_full_install_leaves_config_json_byte_identical_and_builds_without_the_flag(keys, tmp_path):
    raw = json.dumps({"install_tier": "dev", **keys}, indent=2) + "\n"
    (tmp_path / "config.json").write_text(raw)

    result = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT, env=_env(tmp_path, LC_ALL="C"),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )

    assert (tmp_path / "config.json").read_text() == raw
    assert not _AXIS_LOG.search(result.stdout + result.stderr)
    manifest = json.loads((tmp_path / "marketplace" / "plugins" / "engram" / ".engram-build-manifest.json").read_text())
    assert "multi_agent" not in manifest and manifest["tier"] == "dev"


def test_fresh_install_default_message_does_not_name_the_axis(tmp_path):
    log_file = tmp_path / "venv-calls.log"
    _fake_venv(tmp_path, log_file)

    result = _run_installer_skip_build(tmp_path, env_extra={})

    assert [ln for ln in result.stdout.splitlines() if "fresh-install default" in ln]
    assert "single-agent" not in result.stdout
