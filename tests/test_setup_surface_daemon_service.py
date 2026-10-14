"""Tests for tools/setup-surface-daemon-service.sh OS-detection logic.

Covers (per spec):
  (a) Linux branch targets ~/.config/systemd/user/engram-surface-daemon.service.
  (b) Darwin branch targets ~/Library/LaunchAgents/com.engram.surface-daemon.plist.
  (c) Linux branch invokes systemctl --user (mocked — no real systemctl called).
  (d) Darwin branch invokes launchctl bootstrap/bootout (modern API; mocked — no real launchctl called).
  (e) Missing systemctl on Linux → script exits non-zero with a clear message.
  (f) Missing launchctl on macOS → script exits non-zero with a clear message.
  (g) Unsupported OS → script exits non-zero with a clear message.
  (h) Script is syntactically valid (bash -n).

All tests run the script via subprocess with mocked OS-level binaries injected
via PATH; no real systemd or launchd interaction occurs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "setup-surface-daemon-service.sh"
DAEMON_PY = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
TEMPLATE_DIR = REPO_ROOT / "src" / "engram" / "templates"


def _write_stub(directory: Path, name: str, content: str) -> Path:
    """Write a shell stub script into directory and make it executable."""
    p = directory / name
    p.write_text(content)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _make_patched_script(plugin_root: str, dest: Path) -> None:
    """Write a copy of SETUP_SCRIPT with PLUGIN_ROOT hard-coded to plugin_root."""
    original = SETUP_SCRIPT.read_text()
    # Replace the PLUGIN_ROOT computation line with a fixed assignment.
    # The current line (pinned via the locale-safe helper, #3790) is:
    #   _pinned_into PLUGIN_ROOT _cd_pwd "$_script_dir/.."
    target = '_pinned_into PLUGIN_ROOT _cd_pwd "$_script_dir/.."'
    # A str.replace that matches nothing is a silent no-op: PLUGIN_ROOT
    # would then resolve from the copy's temp dir.  Fail loudly instead
    # if the line is ever reshaped (#3794).
    assert original.count(target) == 1, (
        f"setup-surface-daemon-service.sh: expected the patch target "
        f"{target!r} exactly once, found {original.count(target)}"
    )
    patched = original.replace(target, f'PLUGIN_ROOT="{plugin_root}"')
    dest.write_text(patched)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_env(stub_dir: str, fake_home: str, fake_engram_home: str,
              extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": fake_home,
        "ENGRAM_HOME": fake_engram_home,
        # #3519 item 1: these cells run from temp dirs / worktrees.
        "ENGRAM_SETUP_ALLOW_PATH": "1",
    }
    # Always clear ENGRAM_HOOKS_DIR from inherited env unless caller sets it
    env.pop("ENGRAM_HOOKS_DIR", None)
    if extra:
        env.update(extra)
    return env


class TestSetupScriptSyntax(unittest.TestCase):
    """The setup script must be syntactically valid bash."""

    def test_bash_syntax_check(self):
        """bash -n must exit 0 on the setup script."""
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bash -n failed on setup script:\n{result.stderr}",
        )


class TestOSDetection(unittest.TestCase):
    """OS-detection smoke tests — assert the right paths and commands are targeted."""

    def _systemctl_recording_stub(self, stub_dir: Path) -> list[str]:
        """Write a systemctl stub that records calls to a file; return the call-log path."""
        log = stub_dir / "systemctl-calls.log"
        body = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$@" >> "{log}"
            exit 0
            """)
        _write_stub(stub_dir, "systemctl", body)
        return str(log)

    def _launchctl_recording_stub(self, stub_dir: Path) -> list[str]:
        """Write a launchctl stub that records calls to a file; return the call-log path."""
        log = stub_dir / "launchctl-calls.log"
        body = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$@" >> "{log}"
            exit 0
            """)
        _write_stub(stub_dir, "launchctl", body)
        return str(log)

    def _drive_real_installer(self, os_name: str, stub_dir: str, fake_home: str):
        """Run the REAL installer (patched only at its PLUGIN_ROOT line, as
        TestHooksDirResolution does) for os_name against a fake plugin layout.

        Only the OS-level binaries are stubbed (uname, python3, and the
        recording systemctl/launchctl); the render, the paths and the
        service-manager calls are all the real script's (#3801). Returns
        (result, fake_engram_home, call_log_path).
        """
        stub_path = Path(stub_dir)
        fake_engram_home = Path(fake_home) / ".engram"
        fake_engram_home.mkdir(parents=True, exist_ok=True)

        _write_stub(stub_path, "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        # The installer runs `python3 -c 'import sys; print(sys.executable)'`
        # to fill PYTHON_BIN; the stub answers with that one line.
        _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
        if os_name == "Linux":
            log_path = self._systemctl_recording_stub(stub_path)
        else:
            log_path = self._launchctl_recording_stub(stub_path)

        # Fake plugin layout (mirrors production plugin)
        plugin_root = Path(fake_home) / "plugin"
        template_dir = plugin_root / "templates"
        hooks_dir = plugin_root / "hooks" / "claude"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        template_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "engram-surface-daemon.py").symlink_to(DAEMON_PY)
        for f in TEMPLATE_DIR.iterdir():
            if f.is_file():
                shutil.copy(f, template_dir / f.name)

        patched = Path(fake_home) / "setup_patched.sh"
        _make_patched_script(str(plugin_root), patched)
        env = _make_env(stub_dir, fake_home, str(fake_engram_home))
        result = subprocess.run(
            ["bash", str(patched)],
            capture_output=True,
            text=True,
            env=env,
        )
        return result, fake_engram_home, Path(log_path)

    def test_linux_targets_systemd_user_dir(self):
        """On Linux, the rendered unit file must land in ~/.config/systemd/user/."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result, fake_engram_home, log_path = self._drive_real_installer(
                "Linux", stub_dir, fake_home,
            )
            self.assertEqual(
                result.returncode, 0,
                f"Real installer must succeed on Linux;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

            # Check the unit file landed in the right place
            expected_service = Path(fake_home) / ".config" / "systemd" / "user" / "engram-surface-daemon.service"
            self.assertTrue(
                expected_service.exists(),
                f"Linux branch must write unit to {expected_service}; got:\n{result.stdout}\n{result.stderr}",
            )

            # What the real render produced: the placeholders are filled
            unit = expected_service.read_text()
            self.assertIn(
                f"Environment=ENGRAM_HOME={fake_engram_home}", unit,
                "Rendered unit must carry the substituted ENGRAM_HOME",
            )
            self.assertNotIn("{{", unit, "Rendered unit must have no unsubstituted placeholder")

            # Check systemctl was invoked
            self.assertTrue(
                log_path.exists() and log_path.read_text().strip(),
                "Linux branch must invoke systemctl",
            )
            calls = log_path.read_text()
            self.assertIn("--user", calls, "systemctl must be called with --user")
            self.assertIn("engram-surface-daemon.service", calls)
            # The recording stub logs one `"$@"` line per call.  The real enable
            # call must be present as a whole line: the `--user daemon-reload`
            # line alone satisfies the substring checks above.
            self.assertIn(
                "--user enable --now engram-surface-daemon.service",
                calls.splitlines(),
                f"systemctl must be called exactly as `--user enable --now <unit>`; calls:\n{calls}",
            )

    def test_darwin_targets_launchagents_dir(self):
        """On Darwin, the rendered plist must land in ~/Library/LaunchAgents/."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            # The recording launchctl stub exits 0, so the real script takes
            # its success path (a bootstrap rc of 125 is its no-GUI exit 3).
            result, fake_engram_home, log_path = self._drive_real_installer(
                "Darwin", stub_dir, fake_home,
            )
            self.assertEqual(
                result.returncode, 0,
                f"Real installer must succeed on Darwin;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

            plist_label = "com.engram.surface-daemon"
            plist_dest = Path(fake_home) / "Library" / "LaunchAgents" / f"{plist_label}.plist"

            # Check the plist landed in the right place
            self.assertTrue(
                plist_dest.exists(),
                f"Darwin branch must write plist to {plist_dest}; stderr:\n{result.stderr}",
            )

            # What the real render produced: the placeholders are filled
            plist = plist_dest.read_text()
            self.assertIn(
                f"<string>{fake_engram_home}</string>", plist,
                "Rendered plist must carry the substituted ENGRAM_HOME",
            )
            self.assertNotIn("{{", plist, "Rendered plist must have no unsubstituted placeholder")

            # Check launchctl was invoked with modern bootstrap API
            self.assertTrue(
                log_path.exists() and log_path.read_text().strip(),
                "Darwin branch must invoke launchctl",
            )
            calls = log_path.read_text()
            self.assertIn("bootstrap", calls, "launchctl must be called with 'bootstrap' (modern API)")
            self.assertIn("bootout", calls, "launchctl must be called with 'bootout' (modern cleanup)")
            self.assertIn("com.engram.surface-daemon", calls)
            # Whole-line pins on the real calls (the stub logs one `"$@"` line per
            # call); substrings above would also match a wrong-domain call.
            uid = os.getuid()
            call_lines = calls.splitlines()
            self.assertIn(
                f"bootout gui/{uid}/{plist_label}", call_lines,
                f"launchctl bootout must target gui/<uid>/<label>; calls:\n{calls}",
            )
            self.assertIn(
                f"bootstrap gui/{uid} {plist_dest}", call_lines,
                f"launchctl bootstrap must target gui/<uid> with the plist; calls:\n{calls}",
            )

    def test_rendered_linux_unit_contains_idle_timeout_zero(self):
        """The unit the real installer renders must contain ENGRAM_DAEMON_IDLE_TIMEOUT=0."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result, fake_engram_home, _log = self._drive_real_installer(
                "Linux", stub_dir, fake_home,
            )
            self.assertEqual(
                result.returncode, 0,
                f"Real installer must succeed on Linux;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            service_out = Path(fake_home) / ".config" / "systemd" / "user" / "engram-surface-daemon.service"
            content = service_out.read_text()
            self.assertIn(
                "ENGRAM_DAEMON_IDLE_TIMEOUT=0",
                content,
                "Rendered systemd unit must set ENGRAM_DAEMON_IDLE_TIMEOUT=0",
            )
            self.assertIn(
                "Restart=always",
                content,
                "Rendered systemd unit must have Restart=always",
            )

    def test_rendered_plist_contains_idle_timeout_zero(self):
        """The plist the real installer renders must contain ENGRAM_DAEMON_IDLE_TIMEOUT=0."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result, fake_engram_home, _log = self._drive_real_installer(
                "Darwin", stub_dir, fake_home,
            )
            self.assertEqual(
                result.returncode, 0,
                f"Real installer must succeed on Darwin;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            plist_out = Path(fake_home) / "Library" / "LaunchAgents" / "com.engram.surface-daemon.plist"
            content = plist_out.read_text()
            self.assertIn(
                "ENGRAM_DAEMON_IDLE_TIMEOUT",
                content,
                "Rendered plist must contain ENGRAM_DAEMON_IDLE_TIMEOUT key",
            )
            self.assertIn(
                "<string>0</string>",
                content,
                "Rendered plist must set ENGRAM_DAEMON_IDLE_TIMEOUT to 0",
            )
            self.assertIn(
                "com.engram.surface-daemon",
                content,
                "Rendered plist must contain the service label",
            )
            self.assertIn(
                "<true/>",
                content,
                "Rendered plist must have KeepAlive=true and RunAtLoad=true",
            )


class TestMissingToolErrors(unittest.TestCase):
    """Script must fail loud with a clear message when systemctl/launchctl is absent."""

    def _run_full_script(
        self, os_name: str, stubs: dict[str, str], *, hermetic_path: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run the actual setup script with stubbed environment.

        `hermetic_path=True` (#3333) replaces the inherited PATH with the
        stub dir plus a dir of symlinks to only the tools the script runs
        before its launchctl/systemctl check, so "launchctl absent" holds on
        every host (a real Mac has launchctl on its PATH)."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            _write_stub(stub_path, "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
            _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
            for name, body in stubs.items():
                _write_stub(stub_path, name, body)

            # Fake plugin layout so the script passes initial checks
            import shutil
            plugin_root = stub_path / "plugin"
            hooks_dir = plugin_root / "hooks" / "claude"
            template_dir = plugin_root / "templates"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            template_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / "engram-surface-daemon.py").symlink_to(DAEMON_PY)
            for f in TEMPLATE_DIR.iterdir():
                if f.is_file():
                    shutil.copy(f, template_dir / f.name)

            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            path_value = f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            if hermetic_path:
                tools = stub_path / "tools"
                tools.mkdir()
                for tool in ("bash", "env", "readlink", "dirname"):
                    found = shutil.which(tool)
                    assert found, f"hermetic PATH needs {tool} on the host"
                    (tools / tool).symlink_to(found)
                path_value = f"{stub_dir}:{tools}"

            env = {
                **os.environ,
                "PATH": path_value,
                "HOME": fake_home,
                "ENGRAM_HOME": str(fake_engram_home),
                # #3519 item 1: these cells run from temp dirs / worktrees.
                "ENGRAM_SETUP_ALLOW_PATH": "1",
            }
            return subprocess.run(
                ["bash", str(SETUP_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
            )

    def test_linux_no_systemctl_exits_nonzero(self):
        """On Linux, if systemctl is not found, script must exit non-zero."""
        # Don't provide a systemctl stub — it won't be on PATH
        result = self._run_full_script("Linux", stubs={})
        self.assertNotEqual(
            result.returncode,
            0,
            f"Script must fail when systemctl is absent; stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn(
            "systemctl",
            result.stderr + result.stdout,
            "Error message must mention 'systemctl'",
        )

    def test_darwin_no_launchctl_exits_nonzero(self):
        """On macOS, if launchctl is not found, script must exit non-zero."""
        # Don't provide a launchctl stub
        # hermetic_path: a real Mac has launchctl on PATH, so build a PATH
        # that genuinely lacks it (#3333).
        result = self._run_full_script("Darwin", stubs={}, hermetic_path=True)
        self.assertNotEqual(
            result.returncode,
            0,
            f"Script must fail when launchctl is absent; stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn(
            "launchctl",
            result.stderr + result.stdout,
            "Error message must mention 'launchctl'",
        )

    def test_unsupported_os_exits_nonzero(self):
        """On an unsupported OS (e.g. FreeBSD), script must exit non-zero with a message."""
        result = self._run_full_script("FreeBSD", stubs={})
        self.assertNotEqual(
            result.returncode,
            0,
            f"Script must fail on unsupported OS; stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn(
            "FreeBSD",
            result.stderr + result.stdout,
            "Error message must name the unsupported OS",
        )


if __name__ == "__main__":
    unittest.main()


class TestHooksDirResolution(unittest.TestCase):
    """HOOKS_DIR resolution logic: bundle-layout, source-layout fallback,
    ENGRAM_HOOKS_DIR override, and fail-loud when daemon is not found.

    Strategy: the real script computes PLUGIN_ROOT from BASH_SOURCE[0], so
    exporting PLUGIN_ROOT from outside doesn't override it.  Instead each test
    creates a patched copy of the script where the PLUGIN_ROOT assignment line
    is replaced with a literal path pointing at the test's fake plugin root.
    This lets us control the layout without modifying the real source file.
    """

    def test_bundle_layout_resolves_flat_hooks_dir(self):
        """Bundle layout (flat hooks/): script resolves HOOKS_DIR to <plugin_root>/hooks,
        not hooks/claude/, and the rendered ExecStart points at the flat daemon path."""
        import shutil

        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
            _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
            _write_stub(stub_path, "systemctl", '#!/usr/bin/env bash\nexit 0\n')

            # Bundle layout: daemon at hooks/ (flat), NO hooks/claude/ subdir
            plugin_root = Path(fake_home) / "plugin"
            template_dir = plugin_root / "templates"
            flat_hooks = plugin_root / "hooks"
            flat_hooks.mkdir(parents=True, exist_ok=True)
            template_dir.mkdir(parents=True, exist_ok=True)
            (flat_hooks / "engram-surface-daemon.py").symlink_to(DAEMON_PY)
            for f in TEMPLATE_DIR.iterdir():
                if f.is_file():
                    shutil.copy(f, template_dir / f.name)

            # Confirm fixture: no hooks/claude/
            self.assertFalse(
                (plugin_root / "hooks" / "claude").exists(),
                "Bundle-layout fixture must NOT have hooks/claude/",
            )

            # Patched script with PLUGIN_ROOT fixed to our fake plugin_root
            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_root), patched)

            env = _make_env(stub_dir, fake_home, str(fake_engram_home))
            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"Script must succeed with bundle layout;\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}",
            )

            # Rendered unit must reference the flat hooks/ daemon path
            service_dest = (
                Path(fake_home) / ".config" / "systemd" / "user"
                / "engram-surface-daemon.service"
            )
            self.assertTrue(service_dest.exists(), f"Rendered unit must exist at {service_dest}")
            unit_content = service_dest.read_text()

            flat_daemon_path = str(flat_hooks / "engram-surface-daemon.py")
            self.assertIn(
                flat_daemon_path,
                unit_content,
                f"Rendered unit must reference daemon at flat hooks/ path ({flat_daemon_path}), "
                f"not hooks/claude/;\ngot:\n{unit_content}",
            )
            self.assertNotIn(
                "hooks/claude",
                unit_content,
                "Rendered unit must NOT reference hooks/claude/ in bundle layout",
            )

    def test_source_layout_fallback_hooks_claude(self):
        """Source-tree layout (hooks/claude/): script falls back to hooks/claude/ when
        hooks/engram-surface-daemon.py does not exist at the flat location."""
        import shutil

        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
            _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
            _write_stub(stub_path, "systemctl", '#!/usr/bin/env bash\nexit 0\n')

            # Source-tree layout: daemon at hooks/claude/ only
            plugin_root = Path(fake_home) / "plugin"
            template_dir = plugin_root / "templates"
            claude_hooks = plugin_root / "hooks" / "claude"
            claude_hooks.mkdir(parents=True, exist_ok=True)
            template_dir.mkdir(parents=True, exist_ok=True)
            (claude_hooks / "engram-surface-daemon.py").symlink_to(DAEMON_PY)
            for f in TEMPLATE_DIR.iterdir():
                if f.is_file():
                    shutil.copy(f, template_dir / f.name)

            # Confirm fixture: no flat hooks/engram-surface-daemon.py
            self.assertFalse(
                (plugin_root / "hooks" / "engram-surface-daemon.py").exists(),
                "Source-layout fixture must NOT have flat hooks/engram-surface-daemon.py",
            )

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_root), patched)

            env = _make_env(stub_dir, fake_home, str(fake_engram_home))
            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"Script must succeed with source-tree layout;\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}",
            )

            # Rendered unit must reference hooks/claude/ path
            service_dest = (
                Path(fake_home) / ".config" / "systemd" / "user"
                / "engram-surface-daemon.service"
            )
            self.assertTrue(service_dest.exists(), f"Rendered unit must exist at {service_dest}")
            unit_content = service_dest.read_text()

            claude_daemon_path = str(claude_hooks / "engram-surface-daemon.py")
            self.assertIn(
                claude_daemon_path,
                unit_content,
                f"Rendered unit must reference daemon at hooks/claude/ path;\ngot:\n{unit_content}",
            )

    def test_engram_hooks_dir_override(self):
        """ENGRAM_HOOKS_DIR env var must be used directly, bypassing auto-resolution,
        even when neither hooks/ nor hooks/claude/ has the daemon."""
        import shutil

        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
            _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
            _write_stub(stub_path, "systemctl", '#!/usr/bin/env bash\nexit 0\n')

            # Plugin root with NO daemon in either auto-detection location
            plugin_root = Path(fake_home) / "plugin"
            template_dir = plugin_root / "templates"
            template_dir.mkdir(parents=True, exist_ok=True)
            for f in TEMPLATE_DIR.iterdir():
                if f.is_file():
                    shutil.copy(f, template_dir / f.name)

            # Daemon lives only at the override path
            override_dir = Path(fake_home) / "custom-hooks"
            override_dir.mkdir()
            (override_dir / "engram-surface-daemon.py").symlink_to(DAEMON_PY)

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_root), patched)

            env = _make_env(
                stub_dir, fake_home, str(fake_engram_home),
                extra={"ENGRAM_HOOKS_DIR": str(override_dir)},
            )
            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"Script must succeed when ENGRAM_HOOKS_DIR override is set;\n"
                f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}",
            )

            # Rendered unit must reference the override path
            service_dest = (
                Path(fake_home) / ".config" / "systemd" / "user"
                / "engram-surface-daemon.service"
            )
            self.assertTrue(service_dest.exists(), f"Rendered unit must exist at {service_dest}")
            unit_content = service_dest.read_text()

            override_daemon_path = str(override_dir / "engram-surface-daemon.py")
            self.assertIn(
                override_daemon_path,
                unit_content,
                f"Rendered unit must use ENGRAM_HOOKS_DIR override path;\ngot:\n{unit_content}",
            )

    def test_fail_loud_when_daemon_not_found(self):
        """Script must exit non-zero with a clear error when the daemon is absent from
        both hooks/ and hooks/claude/, and ENGRAM_HOOKS_DIR is not set."""
        import shutil

        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
            _write_stub(stub_path, "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
            _write_stub(stub_path, "systemctl", '#!/usr/bin/env bash\nexit 0\n')

            # Plugin root with NO daemon anywhere; hooks/ dir exists but is empty
            plugin_root = Path(fake_home) / "plugin"
            template_dir = plugin_root / "templates"
            hooks_dir = plugin_root / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            template_dir.mkdir(parents=True, exist_ok=True)
            for f in TEMPLATE_DIR.iterdir():
                if f.is_file():
                    shutil.copy(f, template_dir / f.name)
            # Intentionally do NOT create engram-surface-daemon.py anywhere

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_root), patched)

            # ENGRAM_HOOKS_DIR must NOT be set
            env = _make_env(stub_dir, fake_home, str(fake_engram_home))
            self.assertNotIn("ENGRAM_HOOKS_DIR", env)

            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertNotEqual(
                result.returncode,
                0,
                f"Script must fail when daemon not found in either hooks location;\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            combined = result.stderr + result.stdout
            self.assertIn(
                "ERROR",
                combined,
                "Error output must contain 'ERROR'",
            )
            self.assertIn(
                "engram-surface-daemon.py",
                combined,
                "Error output must mention 'engram-surface-daemon.py'",
            )


if __name__ == "__main__":
    unittest.main()
