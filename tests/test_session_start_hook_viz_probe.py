"""SessionStart viz-server health probe (issue #2783, facet 1).

The viz server (health dashboard, `python3 -m viz.server`, canonically port
5001) is the one substrate component whose own absence has no other surface
to show up on -- its absence is displayed ON the dashboard. This mirrors the
#1754 MCP tri-state pattern (`_check_mcp_health`) but with a REQUIRED fourth
state: "not-configured" (silent, same as "up") is the correct, common-case
verdict for a seat with no per-account service definition at all (a shared
multi-tenant viz server, or a seat riding another account's live process) --
NOT a degraded/error case. Collapsing it into "down" or omitting it makes the
probe cry wolf on most of the fleet.

The probe never touches the network port (127.0.0.1 is not per-user
namespaced -- see _check_viz_server_health's docstring); it reads the
supervision layer instead: `launchctl list <label>` on macOS,
`systemctl --user show <unit>` on Linux (the ONE per-seat unit name this repo
actually ships, per templates/engram-viz-user.service.template +
tools/operator-setup-viz.sh -- not an invented name).

All subprocess calls are mocked -- this suite must be portable to a Linux CI
runner that has neither a real `launchctl` nor an enrolled `com.engram.viz-server`
LaunchAgent, and to a macOS dev box that has neither a real
`engram-viz-user.service` unit nor a systemd-user bus at all.
"""

import importlib.util
import io
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(monkeypatch, engram_home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)

    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", _HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_runtime_platform(monkeypatch, mod, tmp_path: Path, platform):
    """Mirrors test_session_start_hook_mcp_health.py's helper of the same
    name. Sets the CLAUDE/CODEX runtime axis (_runtime_platform / platform.json)
    -- an entirely different axis from the OS axis (sys.platform) the viz
    probe reads. Used here only to DEFER the MCP probe (platform="codex") so
    emission-block tests can isolate the viz probe's wiring without also
    mocking pgrep."""
    runtime_root = tmp_path / "plugin-runtime"
    hooks_dir = runtime_root / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / "engram-session-start-hook.py"
    hook_path.write_text("# test hook path\n")
    if platform is not None:
        (runtime_root / "platform.json").write_text(json.dumps({"platform": platform}))
    monkeypatch.setattr(mod, "__file__", str(hook_path))
    return runtime_root


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            module.main()
    return json.loads(buf.getvalue().strip())


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── _viz_supervision_layer: OS-axis platform gating ─────────────────────────


def test_supervision_layer_darwin_is_launchd(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mod._viz_supervision_layer() == "launchd"


def test_supervision_layer_linux_is_systemd(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(sys, "platform", "linux")
    assert mod._viz_supervision_layer() == "systemd"


def test_supervision_layer_other_is_none(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(sys, "platform", "win32")
    assert mod._viz_supervision_layer() == "none"


# ── _check_viz_server_health: dispatch ──────────────────────────────────────


def test_dispatch_routes_to_launchd_probe(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(mod, "_viz_supervision_layer", lambda: "launchd")
    monkeypatch.setattr(mod, "_check_viz_health_launchd", lambda: ("up", None))
    monkeypatch.setattr(
        mod,
        "_check_viz_health_systemd",
        lambda: (_ for _ in ()).throw(AssertionError("wrong probe called")),
    )
    assert mod._check_viz_server_health("unused") == ("up", None)


def test_dispatch_routes_to_systemd_probe(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(mod, "_viz_supervision_layer", lambda: "systemd")
    monkeypatch.setattr(
        mod,
        "_check_viz_health_launchd",
        lambda: (_ for _ in ()).throw(AssertionError("wrong probe called")),
    )
    monkeypatch.setattr(mod, "_check_viz_health_systemd", lambda: ("up", None))
    assert mod._check_viz_server_health("unused") == ("up", None)


def test_dispatch_none_layer_is_not_configured_no_probe_called(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(mod, "_viz_supervision_layer", lambda: "none")
    launchd = MagicMock()
    systemd = MagicMock()
    monkeypatch.setattr(mod, "_check_viz_health_launchd", launchd)
    monkeypatch.setattr(mod, "_check_viz_health_systemd", systemd)
    assert mod._check_viz_server_health("unused") == ("not-configured", None)
    launchd.assert_not_called()
    systemd.assert_not_called()


# ── _check_viz_health_launchd (macOS) ───────────────────────────────────────


def test_launchd_up_when_pid_present(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    stdout = (
        "{\n"
        '\t"PID" = 4242;\n'
        '\t"LastExitStatus" = 0;\n'
        "};\n"
    )
    monkeypatch.setattr(
        mod.subprocess, "run", MagicMock(return_value=_Completed(0, stdout=stdout))
    )
    assert mod._check_viz_health_launchd() == ("up", None)


def test_launchd_down_when_registered_but_no_pid(tmp_path, monkeypatch):
    """Planted-match control for the down branch: realistic dead-job
    `launchctl list <label>` output (measured shape -- a registered job with
    no "PID" key and a nonzero LastExitStatus), not merely an empty/absent
    case."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    stdout = (
        "{\n"
        '\t"Label" = "com.engram.viz-server";\n'
        '\t"OnDemand" = false;\n'
        '\t"LastExitStatus" = 512;\n'
        '\t"Program" = "/usr/bin/python3";\n'
        "};\n"
    )
    monkeypatch.setattr(
        mod.subprocess, "run", MagicMock(return_value=_Completed(0, stdout=stdout))
    )
    status, reason = mod._check_viz_health_launchd()
    assert status == "down"
    assert "not running" in reason
    assert "512" in reason


def test_launchd_not_configured_when_label_absent(tmp_path, monkeypatch):
    """Measured live: `launchctl list <absent-label>` exits 113 with
    'Could not find service ... in domain for port'."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(return_value=_Completed(113, stderr="Could not find service")),
    )
    assert mod._check_viz_health_launchd() == ("not-configured", None)


def test_launchd_indeterminate_on_timeout(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(side_effect=mod.subprocess.TimeoutExpired(["launchctl"], 1)),
    )
    status, reason = mod._check_viz_health_launchd()
    assert status == "indeterminate"
    assert "timed out" in reason


def test_launchd_indeterminate_on_unexpected_exception_never_silently_up(
    tmp_path, monkeypatch
):
    """Deliberate asymmetry vs _check_mcp_health: an unexpected exception
    here must NEVER fail open to a silent "up" -- there is a dedicated
    silent state ("not-configured") for the genuinely benign case, and an
    unexpected error is not it."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(
        mod.subprocess, "run", MagicMock(side_effect=OSError("boom"))
    )
    status, reason = mod._check_viz_health_launchd()
    assert status == "indeterminate"
    assert status != "up"
    assert "unexpectedly" in reason


def test_launchd_down_reason_includes_stale_plist_hint(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    down_stdout = '{\n\t"LastExitStatus" = 512;\n};\n'
    plutil_stdout = (
        '"ProgramArguments" => [\n'
        '  0 => "/usr/bin/python3"\n'
        '  1 => "/Users/x/.engram/marketplace/plugins/engram/viz_server.py"\n'
        "]\n"
    )

    def _run(cmd, **kwargs):
        if cmd[0] == "launchctl":
            return _Completed(0, stdout=down_stdout)
        if cmd[0] == "plutil":
            return _Completed(0, stdout=plutil_stdout)
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    status, reason = mod._check_viz_health_launchd()
    assert status == "down"
    assert "operator-setup-viz.sh" in reason


def test_viz_plist_stale_reason_none_when_plist_absent(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    run = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._viz_plist_stale_reason() is None
    run.assert_not_called()


def test_viz_plist_stale_reason_none_when_current_entrypoint(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    plutil_stdout = (
        '"ProgramArguments" => [\n'
        '  1 => "-m"\n'
        '  2 => "viz.server"\n'
        "]\n"
    )
    monkeypatch.setattr(
        mod.subprocess, "run", MagicMock(return_value=_Completed(0, stdout=plutil_stdout))
    )
    assert mod._viz_plist_stale_reason() is None


# ── _check_viz_health_systemd (Linux) ───────────────────────────────────────


def test_systemd_not_configured_when_systemctl_binary_missing(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    run = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._check_viz_health_systemd() == ("not-configured", None)
    run.assert_not_called()


def test_systemd_not_configured_when_unit_file_absent(tmp_path, monkeypatch):
    """Colleague-review blocker fix (#2795): the probe must not need a bus
    round-trip to know "nothing installed" -- a missing unit FILE is
    not-configured regardless of the bus, and no subprocess call happens at
    all on this path."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    run = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._check_viz_health_systemd() == ("not-configured", None)
    run.assert_not_called()


def test_systemd_indeterminate_when_unit_file_present_but_bus_unreachable(
    tmp_path, monkeypatch
):
    """Colleague-review blocker fix (#2795): demonstrated live on a real
    fleet seat -- XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS both unset, so
    BOTH bus-availability calls (is-system-running, then the
    status default.target fallback) exit 1 with "Failed to connect to bus:
    No medium found" -- while a genuine per-seat unit FILE sits on disk.
    Must read as "cannot tell" (indeterminate), never "nothing installed"
    (not-configured)."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(
            return_value=_Completed(
                1, stderr="Failed to connect to bus: No medium found"
            )
        ),
    )
    status, reason = mod._check_viz_health_systemd()
    assert status == "indeterminate"
    assert "XDG_RUNTIME_DIR" in reason
    assert "DBUS_SESSION_BUS_ADDRESS" in reason


def test_systemd_up_when_active(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "--quiet"]:
            return _Completed(0)
        if "show" in cmd:
            return _Completed(0, stdout="LoadState=loaded\nActiveState=active\n")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    assert mod._check_viz_health_systemd() == ("up", None)


def test_systemd_down_when_loaded_but_failed(tmp_path, monkeypatch):
    """Planted-match control for the down branch: a realistic loaded-but-failed
    unit state, not merely an empty/absent case."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "--quiet"]:
            return _Completed(0)
        if "show" in cmd:
            return _Completed(0, stdout="LoadState=loaded\nActiveState=failed\n")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    status, reason = mod._check_viz_health_systemd()
    assert status == "down"
    assert "failed" in reason
    assert mod._VIZ_SYSTEMD_UNIT in reason


def test_systemd_not_configured_when_unit_not_found(tmp_path, monkeypatch):
    """Unit FILE present on disk (past the new gate) but systemd itself
    reports LoadState=not-found -- the pre-existing fallback for this
    edge case (e.g. the file was removed between the existence check and
    the systemctl call), unchanged by the #2795 fix."""
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "--quiet"]:
            return _Completed(0)
        if "show" in cmd:
            return _Completed(0, stdout="LoadState=not-found\nActiveState=inactive\n")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    assert mod._check_viz_health_systemd() == ("not-configured", None)


def test_systemd_indeterminate_on_timeout(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "--quiet"]:
            return _Completed(0)
        raise mod.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(mod.subprocess, "run", _run)
    status, reason = mod._check_viz_health_systemd()
    assert status == "indeterminate"
    assert "timed out" in reason


def test_systemd_indeterminate_when_show_call_fails_unexpectedly(tmp_path, monkeypatch):
    mod = _load_hook_module(monkeypatch, tmp_path / "engram")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "--quiet"]:
            return _Completed(0)
        if "show" in cmd:
            return _Completed(1, stderr="unexpected systemctl failure")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    status, reason = mod._check_viz_health_systemd()
    assert status == "indeterminate"
    assert status != "up"


# ── Emission-block wiring (SessionStart main()) ─────────────────────────────


def _base_engram_home(tmp_path: Path) -> Path:
    engram_home = tmp_path / "engram"
    (engram_home / "sessions").mkdir(parents=True)
    return engram_home


def test_session_start_viz_up_emits_no_line(tmp_path, monkeypatch):
    engram_home = _base_engram_home(tmp_path)
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")  # defer MCP probe
    monkeypatch.setattr(mod, "_check_viz_server_health", lambda home: ("up", None))

    result = _run_hook_main(
        mod,
        {
            "session_id": "s1",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Viz" not in ctx
    assert "ENGRAM substrate health" not in ctx


def test_session_start_viz_not_configured_emits_no_line(tmp_path, monkeypatch):
    engram_home = _base_engram_home(tmp_path)
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")
    monkeypatch.setattr(
        mod, "_check_viz_server_health", lambda home: ("not-configured", None)
    )

    result = _run_hook_main(
        mod,
        {
            "session_id": "s2",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Viz" not in ctx
    assert "ENGRAM substrate health" not in ctx


def test_session_start_viz_down_emits_hard_alarm(tmp_path, monkeypatch):
    engram_home = _base_engram_home(tmp_path)
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")
    monkeypatch.setattr(
        mod,
        "_check_viz_server_health",
        lambda home: ("down", "launchd job registered but not running"),
    )

    result = _run_hook_main(
        mod,
        {
            "session_id": "s3",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "Viz server: DOWN (launchd job registered but not running)" in ctx
    assert "Tell the user; the health dashboard is unreachable." in ctx


def test_session_start_viz_indeterminate_emits_soft_note(tmp_path, monkeypatch):
    engram_home = _base_engram_home(tmp_path)
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")
    monkeypatch.setattr(
        mod,
        "_check_viz_server_health",
        lambda home: ("indeterminate", "launchctl list timed out"),
    )

    result = _run_hook_main(
        mod,
        {
            "session_id": "s4",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "Viz liveness: inconclusive (launchctl list timed out)" in ctx
    # Soft note only -- never the hard "tell the user immediately" framing.
    assert "Viz server: DOWN" not in ctx
