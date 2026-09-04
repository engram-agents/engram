"""Tests for tools/operator-setup-viz.sh cross-platform behavior.

Covers (per spec):
  (a) Syntax validity — bash -n exits 0.
  (b) --dry-run exits 0 with "import gate passed" when the import gate passes;
      does NOT touch ~/Library/LaunchAgents or systemd dirs.
  (c) Darwin branch renders engram-viz-server.plist.template to
      ~/Library/LaunchAgents/com.engram.viz-server.plist with all {{...}}
      placeholders substituted (no residual {{...}}).
  (d) Linux branch renders engram-viz-user.service.template to
      ~/.config/systemd/user/engram-viz-user.service with all {{...}}
      placeholders substituted (no residual {{...}}).
  (e) Import gate failure: script exits non-zero and refuses to install when
      python cannot import the health-score formula.
  (f) Darwin rendered plist carries exactly the label com.engram.viz-server.
  (g) Missing launchctl on macOS → exits non-zero with a clear message.
  (h) Missing systemctl on Linux → exits non-zero with a clear message.
  (i) Unsupported OS → exits non-zero with a clear message.

Strategy: the script resolves PLUGIN_ENGRAM_DIR from BASH_SOURCE[0] (via
readlink), so exporting it from outside doesn't override it. Each test that
needs to control the plugin layout creates a patched copy of the script where
the PLUGIN_ENGRAM_DIR assignment line is replaced with a literal path pointing
at the test's fake plugin root. This is the same patched-script technique used
by tests/test_setup_surface_daemon_service.py (TestHooksDirResolution).

All launchctl / systemctl calls are intercepted by stub binaries injected via
PATH — no real OS service manager is invoked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "operator-setup-viz.sh"
TEMPLATE_DIR = REPO_ROOT / "src" / "engram" / "templates"
# engram-alpha#2555 PR A: viz_server.py relocated into the viz/ package --
# the bundle-integrity check now looks for viz/server.py (nested), not a
# root-level viz_server.py.
VIZ_SERVER_PY = REPO_ROOT / "src" / "engram" / "viz" / "server.py"


def _write_stub(directory: Path, name: str, content: str) -> Path:
    """Write a shell stub script into directory and make it executable."""
    p = directory / name
    p.write_text(content)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _make_fake_plugin(
    plugin_engram_dir: Path,
    *,
    with_viz_server: bool = True,
    with_templates: bool = True,
) -> None:
    """Create a minimal fake plugin layout under plugin_engram_dir.

    Layout mirrors what operator-setup-viz.sh expects:
      <plugin_engram_dir>/viz/server.py         (bundle integrity check)
      <plugin_engram_dir>/templates/*.template  (service/plist templates)
    """
    plugin_engram_dir.mkdir(parents=True, exist_ok=True)
    if with_viz_server:
        viz_dir = plugin_engram_dir / "viz"
        viz_dir.mkdir(parents=True, exist_ok=True)
        vspy = viz_dir / "server.py"
        vspy.write_text("# stub viz/server.py\n")
    if with_templates:
        import shutil
        tmpl_dest = plugin_engram_dir / "templates"
        tmpl_dest.mkdir(parents=True, exist_ok=True)
        for f in TEMPLATE_DIR.iterdir():
            if f.is_file():
                shutil.copy(f, tmpl_dest / f.name)


def _make_patched_script(plugin_engram_dir: str, dest: Path) -> None:
    """Write a copy of SETUP_SCRIPT with PLUGIN_ENGRAM_DIR hard-coded.

    Replaces the computed assignment:
        PLUGIN_ENGRAM_DIR="$(dirname "$(dirname "$_self")")"
    with a fixed assignment pointing at the given fake plugin dir.
    """
    original = SETUP_SCRIPT.read_text()
    patched = original.replace(
        'PLUGIN_ENGRAM_DIR="$(dirname "$(dirname "$_self")")"',
        f'PLUGIN_ENGRAM_DIR="{plugin_engram_dir}"',
    )
    dest.write_text(patched)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _python_stub_exit0(stub_dir: Path) -> Path:
    """Write a python3 stub that always exits 0 (simulates import gate pass)."""
    return _write_stub(stub_dir, "python3", "#!/usr/bin/env bash\nexit 0\n")


def _python_stub_exit1(stub_dir: Path) -> Path:
    """Write a python3 stub that always exits 1 (simulates import gate fail)."""
    return _write_stub(stub_dir, "python3", "#!/usr/bin/env bash\nexit 1\n")


# Coreutils the setup script needs at runtime. The missing-service-manager
# tests run with PATH scrubbed down to symlinks of these — deliberately WITHOUT
# launchctl/systemctl — so "the tool is absent" can be simulated portably. (A
# naive PATH that inherits the real system dirs would always find /bin/launchctl
# on macOS, so the absence could never fire — the bug this guards against.)
_REQUIRED_TOOLS = [
    "bash", "sh", "sed", "mkdir", "cp", "rm", "ln", "chmod", "date",
    "id", "readlink", "dirname", "basename", "cat", "env", "mktemp", "grep",
]


def _make_scrubbed_bin(stub_dir: Path) -> Path:
    """Create a bin dir of symlinks to the real coreutils the script needs,
    EXCLUDING launchctl/systemctl. Returned dir is meant to be the only real-tool
    source on PATH, so a missing service manager is simulated portably."""
    realbin = stub_dir / "_realbin"
    realbin.mkdir(exist_ok=True)
    for tool in _REQUIRED_TOOLS:
        src = shutil.which(tool)
        if src:
            link = realbin / tool
            if not link.exists():
                os.symlink(src, link)
    return realbin


def _make_env(
    stub_dir: str,
    fake_home: str,
    *,
    engram_home: str | None = None,
    extra: dict | None = None,
) -> dict:
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": fake_home,
    }
    if engram_home is not None:
        env["ENGRAM_HOME"] = engram_home
    else:
        # Default: $fake_home/.engram
        env["ENGRAM_HOME"] = str(Path(fake_home) / ".engram")
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
class TestSyntax(unittest.TestCase):
    """The setup script must be syntactically valid bash."""

    def test_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bash -n failed:\n{result.stderr}",
        )


# ---------------------------------------------------------------------------
class TestDryRun(unittest.TestCase):
    """--dry-run must exit 0, print 'import gate passed', and touch nothing."""

    def test_dry_run_exits_zero_and_prints_gate_passed(self):
        """--dry-run: exit 0 and report import gate passed."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _python_stub_exit0(stub_path)

            # Fake plugin layout
            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir, with_templates=False)

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_engram_dir), patched)

            env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
            result = subprocess.run(
                ["bash", str(patched), "--dry-run"],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"--dry-run must exit 0;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            combined = result.stdout + result.stderr
            self.assertIn(
                "import gate passed",
                combined,
                "--dry-run must print 'import gate passed'",
            )

    def test_dry_run_does_not_touch_launch_agents(self):
        """--dry-run must not create any file under ~/Library/LaunchAgents."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)

            _python_stub_exit0(stub_path)

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir, with_templates=False)

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_engram_dir), patched)

            env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
            subprocess.run(
                ["bash", str(patched), "--dry-run"],
                capture_output=True,
                text=True,
                env=env,
            )

            launch_agents = Path(fake_home) / "Library" / "LaunchAgents"
            self.assertFalse(
                launch_agents.exists(),
                "--dry-run must not create ~/Library/LaunchAgents",
            )


# ---------------------------------------------------------------------------
class TestDarwinBranch(unittest.TestCase):
    """Darwin branch renders the plist to ~/Library/LaunchAgents/ correctly."""

    def _run_darwin(
        self, fake_home: str, stub_dir: str, plugin_engram_dir: Path
    ) -> subprocess.CompletedProcess:
        """Run the patched script under a Darwin-like environment."""
        fake_engram_home = Path(fake_home) / ".engram"
        fake_engram_home.mkdir(parents=True, exist_ok=True)

        stub_path = Path(stub_dir)
        _python_stub_exit0(stub_path)
        # Stub uname to report Darwin
        _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Darwin"\n')
        # launchctl stub that records calls
        log = stub_path / "launchctl-calls.log"
        _write_stub(
            stub_path,
            "launchctl",
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                echo "$@" >> "{log}"
                exit 0
                """),
        )

        patched = Path(fake_home) / "setup_patched.sh"
        _make_patched_script(str(plugin_engram_dir), patched)

        env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
        return subprocess.run(
            ["bash", str(patched)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_darwin_plist_lands_in_launch_agents(self):
        """Darwin branch must write plist to ~/Library/LaunchAgents/com.engram.viz-server.plist."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            result = self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            expected = (
                Path(fake_home) / "Library" / "LaunchAgents"
                / "com.engram.viz-server.plist"
            )
            self.assertTrue(
                expected.exists(),
                f"Darwin branch must write plist to {expected};\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_darwin_plist_label_is_exact(self):
        """Rendered plist must carry exactly the label com.engram.viz-server."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            plist = (
                Path(fake_home) / "Library" / "LaunchAgents"
                / "com.engram.viz-server.plist"
            )
            content = plist.read_text()
            self.assertIn(
                "<string>com.engram.viz-server</string>",
                content,
                "Rendered plist must contain exactly the label 'com.engram.viz-server'",
            )

    def test_darwin_plist_no_residual_placeholders(self):
        """Rendered plist must have no {{...}} placeholders left."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            plist = (
                Path(fake_home) / "Library" / "LaunchAgents"
                / "com.engram.viz-server.plist"
            )
            content = plist.read_text()
            self.assertNotIn(
                "{{",
                content,
                f"Rendered plist must have no {{{{...}}}} placeholders;\ngot:\n{content}",
            )

    def test_darwin_plist_has_keepalive_and_runatload(self):
        """Rendered plist must have KeepAlive=true and RunAtLoad=true."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            plist = (
                Path(fake_home) / "Library" / "LaunchAgents"
                / "com.engram.viz-server.plist"
            )
            content = plist.read_text()
            self.assertIn("<true/>", content, "Rendered plist must have <true/> (KeepAlive/RunAtLoad)")

    def test_darwin_invokes_launchctl_bootstrap(self):
        """Darwin branch must invoke launchctl with modern bootstrap API."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            log = Path(stub_dir) / "launchctl-calls.log"
            self.assertTrue(log.exists() and log.read_text().strip(), "launchctl must be invoked")
            calls = log.read_text()
            self.assertIn("bootstrap", calls, "launchctl must be called with 'bootstrap' (modern API)")
            self.assertIn("bootout", calls, "launchctl must be called with 'bootout' (modern cleanup)")
            self.assertIn("com.engram.viz-server", calls, "launchctl must reference the viz-server label")

    def test_darwin_backs_up_existing_plist(self):
        """Re-running over an existing plist must back it up (timestamped) before overwriting.

        This guards the safety-relevant path: an operator who already has a
        hand-maintained plist must not lose it when the script installs the
        managed one.
        """
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            # Pre-create an existing plist carrying a sentinel marker.
            agents_dir = Path(fake_home) / "Library" / "LaunchAgents"
            agents_dir.mkdir(parents=True, exist_ok=True)
            plist = agents_dir / "com.engram.viz-server.plist"
            plist.write_text("<!-- OLD HAND-MAINTAINED PLIST SENTINEL -->\n")

            result = self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            # Exactly one timestamped backup of the old plist must now exist...
            backups = list(
                agents_dir.glob("com.engram.viz-server.plist.bak-pre-setup-*")
            )
            self.assertEqual(
                len(backups), 1,
                f"exactly one timestamped backup expected, got {len(backups)};\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            # ...the backup must hold the OLD content (the sentinel)...
            self.assertIn(
                "OLD HAND-MAINTAINED PLIST SENTINEL",
                backups[0].read_text(),
                "backup must preserve the pre-existing plist content",
            )
            # ...and the live plist must be the freshly-rendered one (no sentinel).
            new_content = plist.read_text()
            self.assertNotIn(
                "OLD HAND-MAINTAINED PLIST SENTINEL", new_content,
                "live plist must be the newly-rendered one, not the old content",
            )
            self.assertIn(
                "<string>com.engram.viz-server</string>", new_content,
                "newly-rendered plist must carry the correct label",
            )

    def test_darwin_plist_references_bind_127(self):
        """Rendered plist must include --bind 127.0.0.1 (localhost-only safety)."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_darwin(fake_home, stub_dir, plugin_engram_dir)

            plist = (
                Path(fake_home) / "Library" / "LaunchAgents"
                / "com.engram.viz-server.plist"
            )
            content = plist.read_text()
            self.assertIn(
                "127.0.0.1",
                content,
                "Rendered plist must include 127.0.0.1 (--bind 127.0.0.1)",
            )


# ---------------------------------------------------------------------------
class TestLinuxBranch(unittest.TestCase):
    """Linux branch renders the systemd unit to ~/.config/systemd/user/ correctly."""

    def _run_linux(
        self, fake_home: str, stub_dir: str, plugin_engram_dir: Path
    ) -> subprocess.CompletedProcess:
        fake_engram_home = Path(fake_home) / ".engram"
        fake_engram_home.mkdir(parents=True, exist_ok=True)

        stub_path = Path(stub_dir)
        _python_stub_exit0(stub_path)
        _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
        log = stub_path / "systemctl-calls.log"
        _write_stub(
            stub_path,
            "systemctl",
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                echo "$@" >> "{log}"
                exit 0
                """),
        )

        patched = Path(fake_home) / "setup_patched.sh"
        _make_patched_script(str(plugin_engram_dir), patched)

        env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
        return subprocess.run(
            ["bash", str(patched)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_linux_unit_lands_in_systemd_user_dir(self):
        """Linux branch must write unit to ~/.config/systemd/user/engram-viz-user.service."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            result = self._run_linux(fake_home, stub_dir, plugin_engram_dir)

            expected = (
                Path(fake_home) / ".config" / "systemd" / "user"
                / "engram-viz-user.service"
            )
            self.assertTrue(
                expected.exists(),
                f"Linux branch must write unit to {expected};\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_linux_unit_no_residual_placeholders(self):
        """Rendered systemd unit must have no {{...}} placeholders left."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_linux(fake_home, stub_dir, plugin_engram_dir)

            unit = (
                Path(fake_home) / ".config" / "systemd" / "user"
                / "engram-viz-user.service"
            )
            content = unit.read_text()
            self.assertNotIn(
                "{{",
                content,
                f"Rendered unit must have no {{{{...}}}} placeholders;\ngot:\n{content}",
            )

    def test_linux_invokes_systemctl_user(self):
        """Linux branch must invoke systemctl with --user."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            self._run_linux(fake_home, stub_dir, plugin_engram_dir)

            log = Path(stub_dir) / "systemctl-calls.log"
            self.assertTrue(log.exists() and log.read_text().strip(), "systemctl must be invoked")
            calls = log.read_text()
            self.assertIn("--user", calls, "systemctl must be called with --user")
            self.assertIn("engram-viz-user.service", calls)


# ---------------------------------------------------------------------------
class TestImportGate(unittest.TestCase):
    """Import gate must block install when python cannot import the formula."""

    def test_import_gate_failure_blocks_install(self):
        """When python exits 1 (import fails), the script must exit non-zero."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            # python3 exits 1 = import fails
            _python_stub_exit1(stub_path)
            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Darwin"\n')
            _write_stub(stub_path, "launchctl", "#!/usr/bin/env bash\nexit 0\n")

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_engram_dir), patched)

            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)
            env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertNotEqual(
                result.returncode,
                0,
                f"Import gate failure must cause non-zero exit;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            combined = result.stdout + result.stderr
            self.assertIn(
                "ERROR",
                combined,
                "Import gate failure must print an ERROR message",
            )

    def test_import_gate_failure_blocks_install_linux(self):
        """Import gate blocks on Linux too when python exits 1."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            stub_path = Path(stub_dir)
            _python_stub_exit1(stub_path)
            _write_stub(stub_path, "uname", '#!/usr/bin/env bash\necho "Linux"\n')
            _write_stub(stub_path, "systemctl", "#!/usr/bin/env bash\nexit 0\n")

            plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
            _make_fake_plugin(plugin_engram_dir)

            patched = Path(fake_home) / "setup_patched.sh"
            _make_patched_script(str(plugin_engram_dir), patched)

            fake_engram_home = Path(fake_home) / ".engram"
            fake_engram_home.mkdir(parents=True, exist_ok=True)
            env = _make_env(stub_dir, fake_home, engram_home=str(fake_engram_home))
            result = subprocess.run(
                ["bash", str(patched)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertNotEqual(
                result.returncode,
                0,
                "Import gate failure must cause non-zero exit on Linux",
            )


# ---------------------------------------------------------------------------
class TestMissingToolErrors(unittest.TestCase):
    """Script must fail loud with a clear message when launchctl/systemctl is absent."""

    def _run_with_os(
        self, os_name: str, stubs: dict, fake_home: str, stub_dir: str
    ) -> subprocess.CompletedProcess:
        stub_path = Path(stub_dir)
        _python_stub_exit0(stub_path)
        _write_stub(stub_path, "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        for name, body in stubs.items():
            _write_stub(stub_path, name, body)

        plugin_engram_dir = Path(fake_home) / "plugin" / "engram"
        _make_fake_plugin(plugin_engram_dir)

        patched = Path(fake_home) / "setup_patched.sh"
        _make_patched_script(str(plugin_engram_dir), patched)

        fake_engram_home = Path(fake_home) / ".engram"
        fake_engram_home.mkdir(parents=True, exist_ok=True)
        # Scrub PATH to symlinked coreutils only (no real launchctl/systemctl),
        # so a missing service manager actually fires instead of finding the
        # real one on the inherited system PATH.
        realbin = _make_scrubbed_bin(stub_path)
        env = _make_env(
            stub_dir, fake_home, engram_home=str(fake_engram_home),
            extra={"PATH": f"{stub_dir}:{realbin}"},
        )
        return subprocess.run(
            ["bash", str(patched)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_darwin_no_launchctl_exits_nonzero(self):
        """On macOS, missing launchctl → exits non-zero with a clear message."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result = self._run_with_os("Darwin", stubs={}, fake_home=fake_home, stub_dir=stub_dir)

            self.assertNotEqual(
                result.returncode,
                0,
                f"Script must fail when launchctl is absent;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "launchctl",
                result.stderr + result.stdout,
                "Error message must mention 'launchctl'",
            )

    def test_linux_no_systemctl_exits_nonzero(self):
        """On Linux, missing systemctl → exits non-zero with a clear message."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result = self._run_with_os("Linux", stubs={}, fake_home=fake_home, stub_dir=stub_dir)

            self.assertNotEqual(
                result.returncode,
                0,
                f"Script must fail when systemctl is absent;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "systemctl",
                result.stderr + result.stdout,
                "Error message must mention 'systemctl'",
            )

    def test_unsupported_os_exits_nonzero(self):
        """On an unsupported OS (e.g. FreeBSD), script must exit non-zero with a message."""
        with tempfile.TemporaryDirectory() as stub_dir, \
             tempfile.TemporaryDirectory() as fake_home:

            result = self._run_with_os("FreeBSD", stubs={}, fake_home=fake_home, stub_dir=stub_dir)

            self.assertNotEqual(
                result.returncode,
                0,
                f"Script must fail on unsupported OS;\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "FreeBSD",
                result.stderr + result.stdout,
                "Error message must name the unsupported OS",
            )


# ---------------------------------------------------------------------------
class TestPlistTemplateRendering(unittest.TestCase):
    """Direct sed rendering of the plist template (no script invocation).

    Tests the template file itself is well-formed and renders correctly.
    """

    def test_plist_template_renders_no_residual_placeholders(self):
        """sed rendering of engram-viz-server.plist.template must leave no {{...}}."""
        plist_template = TEMPLATE_DIR / "engram-viz-server.plist.template"
        self.assertTrue(plist_template.exists(), f"Template must exist: {plist_template}")

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "com.engram.viz-server.plist"
            fake_python = "/usr/local/bin/python3"
            fake_plugin_dir = "/usr/local/lib/engram"
            fake_engram_home = "/Users/test/.engram"
            fake_viz_config = f"{fake_engram_home}/viz-agents.json"

            subprocess.run(
                [
                    "sed",
                    "-e", f"s|{{{{PYTHON}}}}|{fake_python}|g",
                    "-e", f"s|{{{{PLUGIN_ENGRAM_DIR}}}}|{fake_plugin_dir}|g",
                    "-e", f"s|{{{{ENGRAM_HOME}}}}|{fake_engram_home}|g",
                    "-e", f"s|{{{{VIZ_CONFIG}}}}|{fake_viz_config}|g",
                    str(plist_template),
                ],
                stdout=out.open("w"),
                check=True,
            )

            content = out.read_text()
            self.assertNotIn(
                "{{",
                content,
                f"Rendered plist must have no {{{{...}}}} placeholders;\ngot:\n{content}",
            )

    def test_plist_template_label(self):
        """Rendered plist must carry label com.engram.viz-server."""
        plist_template = TEMPLATE_DIR / "engram-viz-server.plist.template"
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.plist"
            subprocess.run(
                [
                    "sed",
                    "-e", "s|{{PYTHON}}|/usr/bin/python3|g",
                    "-e", "s|{{PLUGIN_ENGRAM_DIR}}|/fake/engram|g",
                    "-e", "s|{{ENGRAM_HOME}}|/fake/.engram|g",
                    "-e", "s|{{VIZ_CONFIG}}|/fake/.engram/viz-agents.json|g",
                    str(plist_template),
                ],
                stdout=out.open("w"),
                check=True,
            )
            content = out.read_text()
            self.assertIn(
                "<string>com.engram.viz-server</string>",
                content,
            )
            self.assertIn("<true/>", content)
            # Verify PYTHON and PLUGIN_ENGRAM_DIR are substituted
            self.assertIn("/usr/bin/python3", content)
            self.assertIn("/fake/engram", content)
            # VIZ_CONFIG path appears in the rendered args
            self.assertIn("/fake/.engram/viz-agents.json", content)


if __name__ == "__main__":
    unittest.main()
