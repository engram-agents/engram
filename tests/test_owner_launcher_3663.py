"""#3663 (U5 of #2652): the engramd owner launcher, Linux only.

Contract-first: these assertions were written before the implementation.

Pieces under test
  * `tools/setup-surface-daemon-service.sh --owner`  -> a systemd USER unit, one per ENGRAM_HOME
  * `templates/engramd-owner.service.template`       -> the unit text (read RENDERED, never grepped raw)
  * `hooks/claude/start-engramd-owner.sh`            -> the SessionStart fallback, gated on owner flags
  * `engramd_owner_flags.py`                          -> the ONE definition of "an owner flag is on"

Test discipline (#3519): every test that reaches a service manager puts its OWN recording stub
`systemctl` first on a hermetic PATH and runs with HOME in a tmp dir. Nothing here reaches a real one.

State of the world each group pins
  C1  every launch path's argv contains `engramd_owner`
  C2  exit codes 0 / 1 / 3 are supervised by meaning (rendered unit text)
  C3  one unit per ENGRAM_HOME (four naming properties)
  C4  own user, user scope only
  C6  logs land under $ENGRAM_HOME
  C7  Darwin: nothing is written or called
  C8  the default (no --owner) installer output is unchanged
  F1-F4 the gated fallback (flags off / flag on + no unit / flag on + unit active / launch fails)
  H*  the flag helper (`flag_on`, `any_owner_flag_on`, CLI) and U4d's lazy use of it
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
INSTALLER = SRC / "tools" / "setup-surface-daemon-service.sh"
HOOK = SRC / "hooks" / "claude" / "start-engramd-owner.sh"
HOOK_IO = SRC / "hooks" / "claude" / "_engramd_owner_hook_io.py"
HOOKS_JSON = SRC / "hooks" / "hooks.json"
TEMPLATES = SRC / "templates"
OWNER_TEMPLATE = TEMPLATES / "engramd-owner.service.template"
FLAGS_PY = SRC / "engramd_owner_flags.py"
HOME_KEY_HELPER = SRC / "tools" / "engramd-owner-home-key.sh"  # #3709
U4D_HOOK_DIR = SRC / "hooks" / "claude"
TIERS = REPO / "src" / "build" / "packaging" / "tiers.json"

_TOOLS = ("bash", "env", "sha256sum", "cut", "dirname", "readlink", "timeout", "sleep", "setsid",
          "nohup", "grep", "sed", "tr", "cat", "mkdir", "date", "kill", "id", "head", "awk", "wc",
          "rm", "basename", "sort", "ls", "tail", "touch", "mktemp", "true", "false", "pwd")


def _stub(directory: Path, name: str, body: str) -> Path:
    p = directory / name
    p.write_text(body)
    p.chmod(0o755)
    return p


# Cells whose outcome depends on /proc/<pid>/cmdline (or status): the hook reads it, and macOS has
# no /proc (there an unreadable cmdline never reads as dead, by design). Skipped there, never on Linux.
needs_proc = pytest.mark.skipif(
    not os.access("/proc/self/cmdline", os.R_OK),
    reason="no readable /proc (macOS): the hook's live-owner cmdline check cannot be exercised")


def _require(path: Path) -> Path:
    assert path.exists(), f"missing (not implemented yet): {path.relative_to(REPO)}"
    return path


# ---------------------------------------------------------------------------
# INI-style reader for a rendered unit (systemd allows repeated keys)
# ---------------------------------------------------------------------------

def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        assert section is not None, f"key before any section: {raw!r}"
        key, _, value = line.partition("=")
        out[section].setdefault(key.strip(), []).append(value.strip())
    return out


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|min|m|h)?", value.strip())
    assert m, f"unparseable duration {value!r}"
    n = float(m.group(1))
    return n * {None: 1, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600}[m.group(2)]


# ---------------------------------------------------------------------------
# Installer sandbox
# ---------------------------------------------------------------------------

class Sandbox:
    """A tmp plugin layout (tools/, templates/, engramd_owner.py, hooks/claude/...) holding a COPY
    of the installer, a tmp HOME, and recording stubs for uname / systemctl / launchctl."""

    def __init__(self, tmp: Path, os_name: str = "Linux", plugin_name: str = "plugin",
                 home_name: str = "home"):
        self.tmp = tmp
        self.home = tmp / home_name
        self.home.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.plugin = tmp / plugin_name
        (self.plugin / "tools").mkdir(parents=True)
        shutil.copytree(TEMPLATES, self.plugin / "templates")
        (self.plugin / "engramd_owner.py").write_text("# stub owner\n")
        (self.plugin / "hooks" / "claude").mkdir(parents=True)
        shutil.copy(SRC / "hooks" / "claude" / "engram-surface-daemon.py",
                    self.plugin / "hooks" / "claude" / "engram-surface-daemon.py")
        self.installer = self.plugin / "tools" / "setup-surface-daemon-service.sh"
        shutil.copy(INSTALLER, self.installer)
        # #3709: the installer sources the shared home-key helper from its own tools/ directory.
        shutil.copy(HOME_KEY_HELPER, self.plugin / "tools" / HOME_KEY_HELPER.name)
        _stub(self.stubs, "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        self.syslog = tmp / "systemctl.log"
        self.launchlog = tmp / "launchctl.log"
        _stub(self.stubs, "systemctl",
              f'#!/usr/bin/env bash\necho "$@" >> "{self.syslog}"\n'
              'if [ "$2" = "is-active" ]; then echo active; fi\nexit 0\n')
        _stub(self.stubs, "launchctl",
              f'#!/usr/bin/env bash\necho "$@" >> "{self.launchlog}"\nexit 0\n')

    def run(self, *args: str, engram_home: str | None = None, allow_path: bool = True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ENGRAM_HOME", "ENGRAM_HOOKS_DIR", "ENGRAM_SETUP_ALLOW_PATH")}
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.stubs}:{os.environ['PATH']}"
        if engram_home is not None:
            env["ENGRAM_HOME"] = engram_home
        argv = ["bash", str(self.installer), *args]
        if allow_path:
            argv.append("--allow-path")
        return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(self.tmp))

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def owner_units(self) -> list[Path]:
        return sorted(self.unit_dir.glob("engramd-owner-*")) if self.unit_dir.exists() else []

    def install_owner(self, engram_home: str | None) -> Path:
        before = set(self.owner_units())
        r = self.run("--owner", engram_home=engram_home)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        new = [p for p in self.owner_units() if p not in before] or self.owner_units()
        assert len(new) >= 1, "installer wrote no engramd-owner-* unit"
        # the unit it enabled
        return new[-1]

    def systemctl_calls(self) -> list[list[str]]:
        if not self.syslog.exists():
            return []
        return [ln.split() for ln in self.syslog.read_text().splitlines() if ln.strip()]


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def _physical(p: Path) -> str:
    return os.path.realpath(str(p)).rstrip("/") or "/"


def _expected_id(engram_home: str) -> str:
    return hashlib.sha256(engram_home.encode()).hexdigest()[:8]


@pytest.fixture
def rendered(sb):
    home = sb.tmp / "seat-a"
    unit = sb.install_owner(str(home))
    return unit, home, parse_unit(unit.read_text()), sb


# ---- C1: argv carries engramd_owner (unit) ----------------------------------

def test_c1_unit_execstart_argv_contains_engramd_owner(rendered):
    unit, _home, ini, _sb = rendered
    exec_start = ini["Service"]["ExecStart"]
    assert len(exec_start) == 1, exec_start
    argv = exec_start[0].split()
    assert any(a.endswith("engramd_owner.py") for a in argv[1:]), argv
    assert "engramd_owner" in exec_start[0]


# ---- C2: exit codes supervised by meaning -----------------------------------

def test_c2_exit0_clean_stop_is_not_restarted(rendered):
    """Exit 0: `Restart=on-failure` does not restart a clean stop."""
    _u, _h, ini, _s = rendered
    svc = ini["Service"]
    assert svc["Restart"] == ["on-failure"], svc.get("Restart")
    for key in ("RestartForceExitStatus",):
        for v in svc.get(key, []):
            assert "0" not in v.split(), f"{key}={v} would restart a clean stop"


def test_c2_exit1_already_running_is_restarted_but_bounded(rendered):
    """Exit 1: restarted by Restart=on-failure, but bounded -- RestartSec plus StartLimit* in [Unit]
    (5 starts in 300 s); once the limit trips the unit stays failed until reset-failed."""
    _u, _h, ini, _s = rendered
    svc, unit = ini["Service"], ini["Unit"]
    assert svc["Restart"][0] in ("on-failure", "always")
    for key in ("RestartPreventExitStatus", "SuccessExitStatus"):
        for v in svc.get(key, []):
            assert "1" not in v.split(), f"{key}={v} makes exit 1 un-restarted"
    assert _seconds(svc["RestartSec"][0]) >= 5
    assert _seconds(unit["StartLimitIntervalSec"][0]) >= 60
    burst = unit["StartLimitBurst"][0]
    assert burst.isdigit() and 3 <= int(burst) <= 10, burst
    assert "StartLimitIntervalSec" not in svc and "StartLimitBurst" not in svc, \
        "StartLimit* belongs in [Unit]; in [Service] it is ignored"


def test_c2_exit3_displaced_must_be_restarted(rendered):
    """Exit 3 is never a success status (no SuccessExitStatus, not in RestartPreventExitStatus), so
    Restart=on-failure restarts it -- until the start limit trips, like exit 1."""
    _u, _h, ini, _s = rendered
    svc = ini["Service"]
    assert "SuccessExitStatus" not in svc, f"SuccessExitStatus={svc.get('SuccessExitStatus')}"
    for v in svc.get("RestartPreventExitStatus", []):
        assert not ({"1", "3"} & set(v.split())), v
    assert svc["Restart"] == ["on-failure"]


def _exit_path_not_regular() -> int:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engramd_owner
    return engramd_owner.EXIT_PATH_NOT_REGULAR


def test_c2_exit4_pidfile_not_regular_is_not_restarted(rendered):
    """#3768: exit 4 (EXIT_PATH_NOT_REGULAR) cannot be fixed by retrying, so the unit lists it in
    RestartPreventExitStatus -- exactly it: the value comes from the owner's constant (no drift),
    contains neither 1 nor 3 (both must still restart), and SuccessExitStatus stays absent.
    Rendered through the installer with a faked uname, so it runs on macOS and Linux."""
    unit, _h, ini, _s = rendered
    code = str(_exit_path_not_regular())
    svc = ini["Service"]
    assert svc.get("RestartPreventExitStatus") == [code], svc.get("RestartPreventExitStatus")
    raw = [ln.strip() for ln in unit.read_text().splitlines()
           if ln.strip().startswith("RestartPreventExitStatus=")]
    assert raw == [f"RestartPreventExitStatus={code}"], raw
    assert "RestartPreventExitStatus" not in ini.get("Unit", {})
    assert "SuccessExitStatus" not in svc
    assert not ({"1", "3"} & set(svc["RestartPreventExitStatus"][0].replace(",", " ").split()))
    assert svc["Restart"] == ["on-failure"]


def test_c2_exit4_is_in_the_exit_code_comment_table(rendered):
    """The 'supervised by meaning' comment table names exit 4 by its constant."""
    unit, _h, _ini, _s = rendered
    code = str(_exit_path_not_regular())
    rows = [ln for ln in unit.read_text().splitlines()
            if ln.lstrip().startswith("#") and "EXIT_PATH_NOT_REGULAR" in ln]
    assert rows, "no comment-table line names EXIT_PATH_NOT_REGULAR"
    assert any(re.match(rf"#\s+{code}\s+EXIT_PATH_NOT_REGULAR", ln.strip()) for ln in rows), rows


# ---- C3: one owner per ENGRAM_HOME -------------------------------------------

def _name(path: Path) -> str:
    return path.name


def test_c3_same_home_spelled_differently_gives_the_same_name(sb):
    real = sb.home / ".engram"
    real.mkdir()
    link = sb.tmp / "link-to-engram"
    link.symlink_to(real)
    spellings = [str(real), str(real) + "/", str(link), None, "~/.engram"]
    names = {_name(sb.install_owner(eh)) for eh in spellings}
    assert len(names) == 1, names
    assert len(sb.owner_units()) == 1


def test_c3_different_homes_give_different_non_colliding_names(sb):
    a = sb.install_owner(str(sb.tmp / "seat-a"))
    b = sb.install_owner(str(sb.tmp / "seat-b"))
    assert _name(a) != _name(b)
    assert a.exists() and b.exists(), "installing the second home clobbered the first"


def test_c3_name_is_stable_across_re_render(sb):
    home = str(sb.tmp / "seat-a")
    first = sb.install_owner(home)
    text1 = first.read_text()
    second = sb.install_owner(home)
    assert first == second and len(sb.owner_units()) == 1
    assert second.read_text() == text1


def test_c3_name_is_a_valid_unit_name_keyed_on_the_physical_home_hash(sb):
    home = sb.tmp / "seat-a"
    unit = sb.install_owner(str(home))
    assert re.fullmatch(r"engramd-owner-[0-9a-f]{8}\.service", unit.name), unit.name
    assert unit.name == f"engramd-owner-{_expected_id(_physical(home))}.service"


# ---- C4: own user, user scope only ---------------------------------------------

def test_c4_unit_has_no_user_group_and_installs_into_default_target(rendered):
    unit, _h, ini, sb = rendered
    for section, keys in ini.items():
        assert "User" not in keys and "Group" not in keys, (section, keys)
    assert ini["Install"]["WantedBy"] == ["default.target"]
    assert unit.parent == sb.home / ".config" / "systemd" / "user"


def test_c4_installer_only_ever_calls_systemctl_user(rendered):
    unit, _h, _ini, sb = rendered
    calls = sb.systemctl_calls()
    assert calls, "installer made no systemctl call"
    assert all(c[0] == "--user" for c in calls), calls
    assert any(c[1:3] == ["enable", "--now"] and c[3] == unit.name for c in calls), calls


# ---- C6: logs under $ENGRAM_HOME ------------------------------------------------

def test_c6_unit_logs_append_under_engram_home(rendered):
    _u, home, ini, _s = rendered
    prefix = f"append:{_physical(home)}/"
    for key in ("StandardOutput", "StandardError"):
        (v,) = ini["Service"][key]
        assert v.startswith(prefix) and v.endswith(".log"), (key, v)


def test_c6_unit_pins_engram_home_in_the_environment(rendered):
    _u, home, ini, _s = rendered
    assert f"ENGRAM_HOME={_physical(home)}" in ini["Service"]["Environment"]


# ---- installer: ephemeral refusal applies to the OWNER script's path -------------

def test_owner_mode_refuses_an_ephemeral_owner_script_path(sb):
    r = sb.run("--owner", engram_home=str(sb.tmp / "seat-a"), allow_path=False)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "engramd_owner.py" in r.stderr and "ephemeral" in r.stderr
    assert sb.owner_units() == [] and sb.systemctl_calls() == []


# ---- C7: Darwin ----------------------------------------------------------------

# ---- C8: default mode unchanged -------------------------------------------------

def test_c8_default_mode_renders_the_same_surface_daemon_unit(sb):
    home = sb.tmp / "seat-a"
    r = sb.run(engram_home=str(home))
    assert r.returncode == 0, (r.stdout, r.stderr)
    out = (sb.unit_dir / "engram-surface-daemon.service").read_text()
    hooks_dir = sb.plugin / "hooks" / "claude"
    py = subprocess.run(["python3", "-c", "import sys; print(sys.executable)"],
                        capture_output=True, text=True, env={**os.environ, "HOME": str(sb.home),
                        "PATH": f"{sb.stubs}:{os.environ['PATH']}"}).stdout.strip()
    expected = (TEMPLATES / "engram-surface-daemon.service.template").read_text() \
        .replace("{{PYTHON}}", py).replace("{{ENGRAM_HOOKS_DIR}}", str(hooks_dir)) \
        .replace("{{ENGRAM_HOME}}", str(home))
    assert out == expected
    assert sb.systemctl_calls() == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", "engram-surface-daemon.service"],
        ["--user", "is-active", "engram-surface-daemon.service"],
    ]
    assert sb.owner_units() == []


# ---------------------------------------------------------------------------
# The fallback hook (F1-F4 + neighbours)
# ---------------------------------------------------------------------------

class Seat:
    """A sandbox for start-engramd-owner.sh in the FLATTENED bundle layout:
    plugin/hooks/start-engramd-owner.sh, plugin/engramd_owner_flags.py, plugin/engramd_owner.py.
    PATH is hermetic (stubs + symlinks to a fixed tool list), so systemctl is absent unless stubbed."""

    def __init__(self, tmp: Path, os_name: str = "Linux", layout: str = "flat"):
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = tmp / "seat"
        self.eh.mkdir()
        self.home.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        tools = tmp / "tools"
        tools.mkdir()
        for t in _TOOLS:
            found = shutil.which(t)
            if found:
                (tools / t).symlink_to(found)
        self.tools = tools
        self.plugin = tmp / "plugin"
        (self.plugin / "hooks").mkdir(parents=True)
        if HOOK.exists():
            shutil.copy(HOOK, self.plugin / "hooks" / "start-engramd-owner.sh")
        if FLAGS_PY.exists():
            shutil.copy(FLAGS_PY, self.plugin / "engramd_owner_flags.py")
        for n in ("_engramd_owner_hook_io.py", "_hooklib.py", "_harness.py"):  # flat bundle siblings
            if (SRC / "hooks" / "claude" / n).exists():
                shutil.copy(SRC / "hooks" / "claude" / n, self.plugin / "hooks" / n)
        (self.plugin / "engramd_owner.py").write_text("# stub owner\n")
        self.hook = self.plugin / "hooks" / "start-engramd-owner.sh"
        _stub(self.stubs, "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        # the plugin venv python: real python for the flag helper, a recorder for the owner launch
        self.record = tmp / "owner-launch.argv"
        self.fail_flag = tmp / "owner-launch.fail"
        self.helper_calls = tmp / "hook-io-helper.calls"  # one line per call of the Python I/O helper
        self.pre_script = tmp / "owner-launch.pre"  # sourced by the stub owner launch (race arrangement)
        venv_bin = self.home / ".engram" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _stub(venv_bin, "python3", textwrap.dedent(f"""\
            #!/usr/bin/env bash
            for a in "$@"; do case "$a" in
              *engramd_owner_flags.py) exec "{sys.executable}" "$@";;
              *_engramd_owner_hook_io.py) printf '%s\\n' "$*" >> "{self.helper_calls}"; exec "{sys.executable}" "$@";;
            esac; done
            echo "owner-stub-output"
            printf '%s\\n' "$*" >> "{self.record}"
            if [ -f "{self.pre_script}" ]; then . "{self.pre_script}"; fi
            if [ -f "{self.fail_flag}" ]; then echo "boom" >&2; exit 7; fi
            exec sleep 3
            """))
        self.sysmode = tmp / "systemctl.mode"
        self.sysunit = tmp / "systemctl.unit"
        self.syslog = tmp / "systemctl.log"

    def systemctl(self, mode: str, unit: str | None = None):
        self.sysmode.write_text(mode)
        if unit:
            self.sysunit.write_text(unit)
        _stub(self.stubs, "systemctl", textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$*" >> "{self.syslog}"
            [ "$1" = "--user" ] || exit 99
            [ "$2" = "show" ] || exit 98
            unit="$3"; mode="$(cat "{self.sysmode}")"
            nf() {{ printf 'LoadState=not-found\\nActiveState=inactive\\nSubState=dead\\n'; }}
            case "$mode" in
              nobus) echo "Failed to connect to bus: No medium found" >&2; exit 1;;
              timeout) exit 124;;
              empty) exit 0;;
              notfound) nf;;
              active) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\n'; else nf; fi;;
              inactive) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\n'; else nf; fi;;
            esac
            """))

    def config(self, engramd: dict | None, raw: str | None = None):
        p = self.eh / "config.json"
        if raw is not None:
            p.write_text(raw)
        elif engramd is not None:
            p.write_text(json.dumps({"engramd": engramd}))

    def _ship_3709_files(self):
        """#3709: the hook sources the shared home-key helper from <plugin root>/tools/ and its `live`
        verb imports engram_pidfile from the plugin root.  A subclass that lays out tools/ itself
        (the #3709 DSeat, whose with_pidfile decides whether engram_pidfile exists) is left alone."""
        tools = self.plugin / "tools"
        if tools.exists():
            return
        tools.mkdir()
        shutil.copy(HOME_KEY_HELPER, tools / HOME_KEY_HELPER.name)
        shutil.copy(SRC / "engram_pidfile.py", self.plugin / "engram_pidfile.py")
        shutil.copy(SRC / "engram_regular_io.py", self.plugin / "engram_regular_io.py")  # engram_pidfile imports it (#3772)

    def run(self, extra_env: dict | None = None, script: Path | None = None, keep_plugin_root: bool = False):
        self._ship_3709_files()
        env = {"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
               "PATH": f"{self.stubs}:{self.tools}", "TMPDIR": str(self.tmp)}
        if keep_plugin_root:
            env["CLAUDE_PLUGIN_ROOT"] = str(self.plugin)
        env.update(extra_env or {})
        return subprocess.run(["bash", str(script or self.hook)], capture_output=True, text=True,
                              env=env, cwd=str(self.tmp), timeout=60)

    def run_timed(self, extra_env: dict | None = None, limit: float = 3.0):
        """Run the hook with a hard limit; returns (rc, stdout, stderr, elapsed). A hook that blocks is
        killed (it is our own process group) and reported as a failure, never left hanging the suite."""
        import signal
        import time
        self._ship_3709_files()
        env = {"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
               "PATH": f"{self.stubs}:{self.tools}", "TMPDIR": str(self.tmp)}
        env.update(extra_env or {})
        t0 = time.monotonic()
        p = subprocess.Popen(["bash", str(self.hook)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env, cwd=str(self.tmp), start_new_session=True)
        try:
            out, err = p.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            pytest.fail(f"the hook BLOCKED (still running after {limit}s)")
        return p.returncode, out, err, time.monotonic() - t0

    @property
    def log(self) -> Path:
        return self.eh / "engramd-owner.log"

    def log_lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def launched(self) -> list[str]:
        return self.record.read_text().splitlines() if self.record.exists() else []

    def unit_name(self) -> str:
        return f"engramd-owner-{_expected_id(_physical(self.eh))}.service"


def context_lines(result) -> list[str]:
    """The lines of the hook's one-JSON-object additionalContext ([] when stdout is empty)."""
    if not result.stdout.strip():
        return []
    obj = json.loads(result.stdout)
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    lines = ctx.splitlines()
    if lines and lines[0].startswith("▸ "):  # round 8: emit_context's trailer leads; the message follows
        lines = lines[1:]
    return lines


@pytest.fixture
def seat(tmp_path):
    return Seat(tmp_path)


FLAG_ON = {"user_identity_reads_via_owner": True}


# ---- F1: all flags off -> nothing at all ----------------------------------------

@pytest.mark.parametrize("cfg", ["absent", "false", "other-key", "string-true", "malformed"])
def test_f1_all_flags_off_launches_nothing_and_says_nothing(seat, cfg):
    if cfg == "false":
        seat.config({"user_identity_reads_via_owner": False})
    elif cfg == "other-key":
        seat.config({"foo": True})
    elif cfg == "string-true":
        seat.config({"user_identity_reads_via_owner": "true"})
    elif cfg == "malformed":
        seat.config(None, raw="{ not json")
    seat.systemctl("nobus")
    r = seat.run()
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert seat.launched() == [], "an owner was launched with every flag off"
    assert not seat.log.exists() and list(seat.eh.glob("*.log")) == [], "a log line/file appeared"
    assert not (seat.eh / "engramd.pid").exists()
    assert not (seat.eh / "engramd").exists() and not list(seat.eh.glob("*.sock"))
    assert not seat.syslog.exists(), "systemd was consulted although nothing needs an owner"


# ---- F2: flag on, no supervised unit -> launch + ONE UNSUPERVISED line -----------

@pytest.mark.parametrize("mode", ["nobus", "notfound", "absent"])
def test_f2_flag_on_without_a_supervised_unit_launches_and_reports_once(seat, mode):
    seat.config(FLAG_ON)
    if mode != "absent":
        seat.systemctl(mode)
    r = seat.run()
    assert r.returncode == 0, r.stderr
    argv = seat.launched()
    assert len(argv) == 1 and "engramd_owner" in argv[0], argv
    ctx = [ln for ln in context_lines(r) if "UNSUPERVISED" in ln]
    assert len(ctx) == 1 and _physical(seat.eh) in ctx[0], r.stdout
    assert len([ln for ln in r.stdout.splitlines() if "UNSUPERVISED" in ln]) == 1
    logged = [ln for ln in seat.log_lines() if "UNSUPERVISED" in ln]
    assert len(logged) == 1 and _physical(seat.eh) in logged[0], seat.log_lines()
    assert "UNSUPERVISED" not in r.stderr


def test_c1_fallback_launch_argv_contains_engramd_owner(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.run()
    (argv,) = seat.launched()
    assert any(a.endswith("engramd_owner.py") for a in argv.split()), argv


def test_c6_fallback_log_lives_under_engram_home_and_catches_owner_output(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.run()
    logs = list(seat.eh.glob("*.log"))
    assert len(logs) == 1, logs
    assert "owner-stub-output" in logs[0].read_text(), "owner stdout was not routed to the log"


def test_f2_launched_owner_gets_the_normalized_engram_home(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    link = seat.tmp / "seat-link"
    link.symlink_to(seat.eh)
    r = seat.run({"ENGRAM_HOME": str(link) + "/"})
    assert r.returncode == 0
    assert any(_physical(seat.eh) in ln for ln in context_lines(r))


# ---- F3: flag on, THIS home's unit is active -> no second owner -------------------

def test_f3_unit_active_for_this_home_launches_nothing(seat):
    seat.config(FLAG_ON)
    seat.systemctl("active", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0
    assert seat.launched() == []
    assert "UNSUPERVISED" not in r.stdout + r.stderr + "".join(seat.log_lines())
    assert seat.unit_name() in seat.syslog.read_text(), "asked about the wrong unit name"
    assert all(c.split()[:2] == ["--user", "show"] for c in seat.syslog.read_text().splitlines()), \
        "the fallback must only READ systemd state"


def test_f3_control_a_unit_active_for_a_DIFFERENT_home_does_not_suppress_launch(seat):
    seat.config(FLAG_ON)
    seat.systemctl("active", "engramd-owner-deadbeef.service")
    r = seat.run()
    assert len(seat.launched()) == 1, "a unit of another home suppressed this home's owner"
    assert any("UNSUPERVISED" in ln for ln in context_lines(r))


def test_f3_registered_but_not_active_unit_is_left_to_systemd_and_reported(seat):
    seat.config(FLAG_ON)
    seat.systemctl("inactive", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and seat.unit_name() in lines[0]
    assert "UNSUPERVISED" not in r.stdout


def test_hook_and_installer_agree_on_the_unit_name(tmp_path):
    (tmp_path / "i").mkdir()
    (tmp_path / "h").mkdir()
    sb = Sandbox(tmp_path / "i")
    seat = Seat(tmp_path / "h")
    unit = sb.install_owner(str(seat.eh))
    seat.config(FLAG_ON)
    seat.systemctl("notfound")
    seat.run()
    assert unit.name in seat.syslog.read_text(), (unit.name, seat.syslog.read_text())


# ---- F4: flag on and the launch fails -> loud, exit 0 ------------------------------

def test_f4_launch_failure_is_loud_names_the_log_and_exits_zero(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.fail_flag.write_text("x")
    r = seat.run()
    assert r.returncode == 0
    lines = context_lines(r)
    assert len(lines) == 1 and "could not be started" in lines[0], r.stdout
    assert str(seat.log) in lines[0] or _physical(seat.log) in lines[0]
    assert any("could not be started" in ln for ln in seat.log_lines())
    assert "UNSUPERVISED" not in r.stdout, "a failed launch must not claim a running unsupervised owner"


def test_f4_missing_owner_script_is_a_loud_failure_too(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    (seat.plugin / "engramd_owner.py").unlink()
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    assert any("could not be started" in ln for ln in context_lines(r))


# ---- neighbours: live owner, ambiguity, helper failure, Darwin ---------------------

def _fake_live_owner(seat: Seat, with_marker: bool = True):
    cmd = "exec -a engramd_owner sleep 30" if with_marker else "exec sleep 30"
    p = subprocess.Popen(["bash", "-c", cmd])
    (seat.eh / "engramd.pid").write_text(str(p.pid))
    return p


@needs_proc
def test_live_owner_already_present_launches_nothing(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    p = _fake_live_owner(seat)
    try:
        r = seat.run()
    finally:
        p.kill()
        p.wait()
    assert r.returncode == 0 and seat.launched() == []
    assert "UNSUPERVISED" not in r.stdout


@needs_proc
def test_control_a_live_pid_without_the_owner_marker_does_not_count_as_an_owner(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    p = _fake_live_owner(seat, with_marker=False)
    try:
        seat.run()
    finally:
        p.kill()
        p.wait()
    assert len(seat.launched()) == 1


@pytest.mark.parametrize("mode", ["empty", "timeout"])
def test_ambiguous_systemd_answer_launches_nothing_and_says_why(seat, mode):
    seat.config(FLAG_ON)
    seat.systemctl(mode)
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and "ambiguous" in lines[0], r.stdout
    assert "UNSUPERVISED" not in r.stdout


def test_helper_failure_is_ambiguous_launch_nothing_say_so(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    (seat.plugin / "engramd_owner_flags.py").unlink()
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and "flag" in lines[0].lower(), r.stdout


def test_other_non_linux_os_with_a_flag_on_names_that_os_not_macos(tmp_path):
    seat = Seat(tmp_path, os_name="FreeBSD")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and "FreeBSD" in lines[0] and "macOS" not in lines[0], r.stdout
    assert not seat.syslog.exists()


def test_darwin_with_all_flags_off_is_silent(tmp_path):
    seat = Seat(tmp_path, os_name="Darwin")
    r = seat.run()
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "" and seat.launched() == []


# ---- layout: flattened bundle, CLAUDE_PLUGIN_ROOT unset / decoy / source tree -----------

def test_flattened_layout_with_claude_plugin_root_unset_resolves_siblings(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    r = seat.run()  # the child env is built explicitly: CLAUDE_PLUGIN_ROOT is not in it
    assert r.returncode == 0 and len(seat.launched()) == 1


def test_claude_plugin_root_set_is_honoured(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    elsewhere = seat.tmp / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(seat.hook, elsewhere / "start-engramd-owner.sh")
    r = seat.run(script=elsewhere / "start-engramd-owner.sh", keep_plugin_root=True)
    assert len(seat.launched()) == 1 and str(seat.plugin) in seat.launched()[0]


def test_a_decoy_claude_plugin_root_falls_back_to_walking_parents(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    decoy = seat.tmp / "decoy"
    decoy.mkdir()
    r = seat.run(extra_env={"CLAUDE_PLUGIN_ROOT": str(decoy)})
    assert len(seat.launched()) == 1 and str(seat.plugin) in seat.launched()[0]


def test_source_tree_layout_resolves_the_owner_script_next_to_the_flags_module(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    r = seat.run(script=_require(HOOK))
    (argv,) = seat.launched()
    assert str(SRC / "engramd_owner.py") in argv


# ---- hooks.json / tiers ----------------------------------------------------------------

def _tiers() -> dict[str, dict]:
    data = json.loads(TIERS.read_text())
    out = {}
    for entry in data["mechanisms"] if "mechanisms" in data else []:
        out[entry["path"]] = entry
    return out


def test_hooks_json_registers_the_fallback_on_session_start_and_resolves_when_flattened():
    cfg = json.loads(HOOKS_JSON.read_text())
    cmds = [h["command"] for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]]
    mine = [c for c in cmds if "start-engramd-owner.sh" in c]
    assert len(mine) == 1, cmds
    assert mine[0].startswith('bash "${CLAUDE_PLUGIN_ROOT}/hooks/start-engramd-owner.sh"')
    entry = _tiers().get("hooks/claude/start-engramd-owner.sh")
    assert entry, "hook not tiered"
    assert entry["dest"] == "hooks/start-engramd-owner.sh" and entry["tier"] == "essential"
    assert "/hooks/start-engramd-owner.sh" in mine[0]


def test_new_shippables_are_tiered_once_each():
    t = _tiers()
    assert t["engramd_owner_flags.py"]["tier"] == "essential"
    assert t["templates/engramd-owner.service.template"]["tier"] == "convenience"
    assert t["hooks/claude/start-engramd-owner.sh"]["tier"] == "essential"


def test_scripts_parse():
    for p in (INSTALLER, _require(HOOK)):
        assert subprocess.run(["bash", "-n", str(p)], capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# The flag helper and U4d's use of it
# ---------------------------------------------------------------------------

def _flags():
    assert FLAGS_PY.is_file(), "src/engram/engramd_owner_flags.py does not exist yet"
    import importlib.util
    spec = importlib.util.spec_from_file_location("engramd_owner_flags_under_test", FLAGS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp_path: Path, engramd) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"engramd": engramd}))
    return p


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (1, False), (0, False), ("true", False), ("yes", False),
    (None, False), ([], False), ({}, False), (1.0, False),
])
def test_h_flag_on_is_true_only_for_the_boolean_true(tmp_path, value, expected):
    f = _flags()
    assert f.flag_on("user_identity_reads_via_owner",
                     str(_cfg(tmp_path, {"user_identity_reads_via_owner": value}))) is expected


def test_h_flag_on_false_for_missing_key_missing_section_bad_shapes_and_bad_files(tmp_path):
    f = _flags()
    k = "user_identity_reads_via_owner"
    assert f.flag_on(k, str(_cfg(tmp_path, {"other": True}))) is False
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"x": 1}))
    assert f.flag_on(k, str(p)) is False
    p.write_text(json.dumps({"engramd": "nope"}))
    assert f.flag_on(k, str(p)) is False
    p.write_text(json.dumps([1, 2]))
    assert f.flag_on(k, str(p)) is False
    p.write_text("{ malformed")
    assert f.flag_on(k, str(p)) is False
    assert f.flag_on(k, str(tmp_path / "nonexistent.json")) is False
    assert f.flag_on(k, str(tmp_path)) is False  # a directory


def test_h_flag_on_never_blocks_on_a_fifo(tmp_path):
    fifo = tmp_path / "config.json"
    os.mkfifo(fifo)
    code = (f"import importlib.util as u;s=u.spec_from_file_location('m',{str(FLAGS_PY)!r});"
            f"m=u.module_from_spec(s);s.loader.exec_module(m);"
            f"print(m.flag_on('user_identity_reads_via_owner',{str(fifo)!r}))")
    assert FLAGS_PY.is_file()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == "False", (r.stdout, r.stderr)


def test_h_any_owner_flag_on_matches_the_pattern_not_one_literal_key(tmp_path):
    f = _flags()
    on = lambda e: f.any_owner_flag_on(str(_cfg(tmp_path, e)))  # noqa: E731
    assert on({"user_identity_reads_via_owner": True}) is True
    assert on({"xyz_via_owner": True}) is True, "a future hook's flag must be covered"
    assert on({"stop_hook_via_owner": True, "user_identity_reads_via_owner": False}) is True
    assert on({"foo": True}) is False, "a non-owner key must not count"
    assert on({"via_owner_x": True}) is False
    assert on({"xyz_via_owner": "true"}) is False
    assert on({"xyz_via_owner": 1}) is False
    assert on({}) is False


def test_h_any_owner_flag_on_false_for_absent_malformed_and_wrong_shapes(tmp_path):
    f = _flags()
    assert f.any_owner_flag_on(str(tmp_path / "nope.json")) is False
    p = tmp_path / "c.json"
    for text in ("{ bad", "[]", '{"engramd": []}', '{"engramd": null}', "null"):
        p.write_text(text)
        assert f.any_owner_flag_on(str(p)) is False, text


def test_h_cli_prints_on_or_off_for_an_engram_home(tmp_path):
    assert FLAGS_PY.is_file()
    home = tmp_path / "h"
    home.mkdir()
    def run():
        return subprocess.run([sys.executable, str(FLAGS_PY), str(home)], capture_output=True, text=True)
    assert run().stdout.strip() == "off"
    (home / "config.json").write_text(json.dumps({"engramd": {"a_via_owner": True}}))
    r = run()
    assert r.returncode == 0 and r.stdout.strip() == "on"


def _hook_probe(root_hooks: Path, engram_home: Path, extra: str = "") -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import importlib.util, sys
        s = importlib.util.spec_from_file_location("uih", {str(root_hooks / 'engram-user-identity-hook.py')!r})
        m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
        print("imported_at_top=" + str("engramd_owner_flags" in sys.modules))
        print("result=" + repr(m._owner_reads_enabled()))
        print("imported_after_call=" + str("engramd_owner_flags" in sys.modules))
        {extra}
        """)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(engram_home)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          cwd=str(engram_home))


def _flat_bundle(tmp_path: Path, with_helper: bool) -> Path:
    root = tmp_path / "bundle"
    (root / "hooks").mkdir(parents=True)
    for n in ("engram-user-identity-hook.py", "_hooklib.py", "_harness.py"):
        shutil.copy(U4D_HOOK_DIR / n, root / "hooks" / n)
    if with_helper and FLAGS_PY.exists():
        shutil.copy(FLAGS_PY, root / "engramd_owner_flags.py")
    return root


def test_u4d_flattened_bundle_with_claude_plugin_root_unset_resolves_the_helper(tmp_path):
    root = _flat_bundle(tmp_path, with_helper=True)
    home = tmp_path / "eh"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"engramd": {"user_identity_reads_via_owner": True}}))
    r = _hook_probe(root / "hooks", home)
    assert r.returncode == 0, r.stderr
    assert "imported_at_top=False" in r.stdout, "the helper must be imported lazily"
    assert "result=True" in r.stdout, r.stdout + r.stderr
    assert "imported_after_call=True" in r.stdout, "the flag read did not go through the helper"


def test_u4d_helper_missing_from_the_bundle_fails_to_false_and_raises_nothing(tmp_path):
    root = _flat_bundle(tmp_path, with_helper=False)
    home = tmp_path / "eh"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"engramd": {"user_identity_reads_via_owner": True}}))
    r = _hook_probe(root / "hooks", home)
    assert r.returncode == 0, r.stderr
    assert "result=False" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Round 2
# ---------------------------------------------------------------------------

# ---- F5: a launch that LOST a race is silent when a live owner exists -------------------

def _hook_lines_in_log(seat: Seat) -> list[str]:
    return [ln for ln in seat.log_lines() if "] [start-engramd-owner]" in ln]


@needs_proc
@pytest.mark.parametrize("order", ["pidfile-already-there", "pidfile-written-late"])
def test_f5_a_loser_whose_launch_died_is_silent_when_a_live_owner_exists(seat, order):
    """The stub 'owner' dies at once (exit 7), as a loser of the OwnerAlreadyRunning race does,
    while a live owner for THIS home exists. Pre-check cannot see it (no pidfile yet), so the
    hook only learns of it by re-checking after its own launch died."""
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.fail_flag.write_text("x")
    winner = subprocess.Popen(["bash", "-c", "exec -a engramd_owner sleep 30"])
    try:
        pidfile = seat.eh / "engramd.pid"
        if order == "pidfile-already-there":
            seat.pre_script.write_text(f"printf %s {winner.pid} > '{pidfile}'\n")
        else:
            seat.pre_script.write_text(
                f"( sleep 0.3; printf %s {winner.pid} > '{pidfile}' ) >/dev/null 2>&1 &\n")
        r = seat.run()
    finally:
        winner.kill()
        winner.wait()
    assert len(seat.launched()) == 1, "the hook should have attempted exactly one launch"
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert _hook_lines_in_log(seat) == [], seat.log_lines()


def test_f5_control_no_live_owner_after_the_wait_is_still_the_loud_failure(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.fail_flag.write_text("x")
    (seat.eh / "engramd.pid").write_text("999999")  # a dead pid is not an owner
    r = seat.run()
    assert any("could not be started" in ln for ln in context_lines(r)), r.stdout


# ---- (a) no status line on every seat's session start -----------------------------------

def test_hooks_json_entry_has_no_status_message_and_keeps_its_timeout():
    cfg = json.loads(HOOKS_JSON.read_text())
    (entry,) = [h for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]
                if "start-engramd-owner.sh" in h["command"]]
    assert "statusMessage" not in entry, entry
    assert entry["timeout"] == 15


# ---- (c) the helper's docstring no longer claims a reader that does not exist ---------------

def test_no_stale_claim_of_a_second_flag_reader_in_the_helper():
    text = _require(FLAGS_PY).read_text()
    assert "still carries its own copy" not in text
    assert "follow-up" not in text
    assert "#3664" in text and "must call flag_on" in text


# ---- (d) the --owner banner names the owner script, not an empty hooks dir ----------------

def test_owner_mode_banner_prints_the_owner_script_not_an_empty_hooks_dir(sb):
    r = sb.run("--owner", engram_home=str(sb.tmp / "seat-a"))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert f"owner script:  {sb.plugin / 'engramd_owner.py'}" in r.stdout, r.stdout
    assert "hooks dir:" not in r.stdout


# ---------------------------------------------------------------------------
# Round 3: H9 -- a deeply nested (valid) config reads OFF and never raises
# ---------------------------------------------------------------------------

DEEP = "[" * 200000 + "]" * 200000


def _deep_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(DEEP)
    return p


def test_h9_flag_on_is_false_and_does_not_raise_on_a_deeply_nested_config(tmp_path):
    f = _flags()
    assert f.flag_on("user_identity_reads_via_owner", str(_deep_config(tmp_path))) is False


def test_h9_any_owner_flag_on_is_false_and_does_not_raise_on_a_deeply_nested_config(tmp_path):
    f = _flags()
    assert f.any_owner_flag_on(str(_deep_config(tmp_path))) is False


def test_h9_cli_prints_off_and_exits_zero_on_a_deeply_nested_config(tmp_path):
    assert FLAGS_PY.is_file()
    _deep_config(tmp_path)
    r = subprocess.run([sys.executable, str(FLAGS_PY), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "off", (r.returncode, r.stdout, r.stderr[-300:])


def test_h9_u4d_owner_reads_enabled_is_false_and_does_not_raise_on_a_deeply_nested_config(tmp_path):
    root = _flat_bundle(tmp_path, with_helper=True)
    home = tmp_path / "eh"
    home.mkdir()
    (home / "config.json").write_text(DEEP)
    r = _hook_probe(root / "hooks", home)
    assert r.returncode == 0, r.stderr[-300:]
    assert "result=False" in r.stdout, r.stdout + r.stderr[-300:]


# ---------------------------------------------------------------------------
# Round 4
# ---------------------------------------------------------------------------

# ---- I1 / K1: --owner refuses characters sed and systemd would mangle ----------------------------

BAD_CHARS = {
    "space": " ", "tab": "\t", "newline": "\n", "carriage-return": "\r", "ampersand": "&",
    "backslash": "\\", "pipe": "|", "percent": "%", "double-quote": '"', "single-quote": "'",
    "dollar": "$",
    # {{ and }}: the owner/python paths are substituted by sed BEFORE ENGRAM_HOME, so a literal
    # {{ENGRAM_HOME}} inside them is re-substituted. One uniform rule: refused in every value.
    "open-brace-pair": "{{", "close-brace-pair": "}}",
}


def _assert_refused_cleanly(sb: Sandbox, r, what: str, name: str | None = None):
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "unsupported in --owner mode" in r.stderr, r.stderr
    assert what in r.stderr, r.stderr
    if name:
        assert name in r.stderr, r.stderr
    assert r.stderr.strip().count("ERROR") == 1
    assert not sb.unit_dir.exists() or list(sb.unit_dir.iterdir()) == [], "a (partial) unit was written"
    assert sb.systemctl_calls() == [], "something was loaded"


@pytest.mark.parametrize("name", sorted(BAD_CHARS))
def test_i1_owner_mode_refuses_a_bad_character_in_engram_home(sb, name):
    home = sb.tmp / f"h{BAD_CHARS[name]}x"
    r = sb.run("--owner", engram_home=str(home))
    _assert_refused_cleanly(sb, r, "ENGRAM_HOME", name)
    assert not home.exists(), "the bad home was created"


def test_i1_a_bad_character_in_the_physical_path_behind_a_clean_symlink_is_refused(sb):
    real = sb.tmp / "real dir"
    real.mkdir()
    link = sb.tmp / "clean-link"
    link.symlink_to(real)
    r = sb.run("--owner", engram_home=str(link))
    _assert_refused_cleanly(sb, r, "ENGRAM_HOME", "space")


@pytest.mark.parametrize("name,bad", [("space", "/opt/bad path/python3"), ("ampersand", "/opt/a&b/python3")])
def test_i1_owner_mode_refuses_a_bad_character_in_the_python_path(sb, name, bad):
    _stub(sb.stubs, "python3", f'#!/usr/bin/env bash\necho "{bad}"\n')
    r = sb.run("--owner", engram_home=str(sb.tmp / "seat-a"))
    _assert_refused_cleanly(sb, r, "python path", name)
    assert not (sb.tmp / "seat-a").exists()


@pytest.mark.parametrize("name,dirname", [("space", "pl ugin"), ("pipe", "pl|ugin")])
def test_i1_owner_mode_refuses_a_bad_character_in_the_owner_script_path(tmp_path, name, dirname):
    sb = Sandbox(tmp_path, plugin_name=dirname)
    r = sb.run("--owner", engram_home=str(tmp_path / "seat-a"))
    _assert_refused_cleanly(sb, r, "owner script path", name)
    assert not (tmp_path / "seat-a").exists()


def test_i1_control_an_ordinary_home_renders_and_environment_equals_the_input_byte_for_byte(sb):
    home = _physical(sb.tmp / "seat-a_b.c-d")
    unit = sb.install_owner(home)
    assert f"ENGRAM_HOME={home}" in parse_unit(unit.read_text())["Service"]["Environment"]


def test_i1_default_mode_still_accepts_what_it_always_accepted(sb):
    """No --owner: the bad-character refusal does not apply (default mode is unchanged)."""
    r = sb.run(engram_home=str(sb.tmp / "has space"))
    assert r.returncode == 0, (r.stdout, r.stderr)


# ---- I3 / K2: a pid that is not a positive integer is not a live owner ---------------------------

@pytest.mark.parametrize("content", ["0", "00", "-1", "abc", ""])
def test_i3_a_pidfile_that_is_not_a_positive_pid_is_not_a_live_owner(seat, content):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    (seat.eh / "engramd.pid").write_text(content)
    r = seat.run()
    assert r.returncode == 0
    assert len(seat.launched()) == 1, f"pidfile {content!r} was read as a live owner"
    assert any("UNSUPERVISED" in ln for ln in context_lines(r))


# ---- K3: exit-code wording is bounded by the start limit -------------------------------------------

def test_k3_template_does_not_promise_an_unconditional_restart():
    text = _require(OWNER_TEMPLATE).read_text()
    assert "MUST be restarted" not in text
    assert "reset-failed" in text and "start limit" in text.lower()


# ---- K4(a): the hook's JSON is valid for ANY home ------------------------------------------------------

@pytest.mark.parametrize("dirname", ["sp ace", "am&per", "back\\slash", 'dq"uote'])
def test_k4a_hook_output_is_valid_json_naming_the_exact_home_for_any_home(tmp_path, dirname):
    seat = Seat(tmp_path)
    home = tmp_path / dirname
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"engramd": FLAG_ON}))
    seat.systemctl("empty")  # an ambiguous systemd answer forces the hook to emit a line
    r = seat.run({"ENGRAM_HOME": str(home)})
    assert r.returncode == 0
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _physical(home) in ctx, (ctx, r.stdout)


# ---------------------------------------------------------------------------
# Round 5
# ---------------------------------------------------------------------------

# ---- K7: a failed engramd_owner_flags import in U4d is VISIBLE (#3521's shape) ----------------------
#
# CONTRACT K7. When the U4d hook cannot import engramd_owner_flags (absent, raises on import, bad
# syntax), the flag reads OFF (the direct path still runs) AND, on every run, the hook emits:
#   1. EXACTLY ONE stderr line `engram-user-identity-hook: engramd_owner_flags failed: <Class>`
#      (class name only, never the exception message);
#   2. EXACTLY ONE injected UserPromptSubmit context line with the same text, INSIDE the hook's single
#      additionalContext payload (stdout stays one parseable JSON object);
#   3. no rate limit; 4. the healthy path is unchanged; 5. never raises.
# Each cell runs the hook as a subprocess on a scratch FLATTENED bundle with a hard timeout.

K7_SECRET = "SECRET-MESSAGE-3663-do-not-echo"
K7_HOOK = "engram-user-identity-hook.py"


def _k7_bundle(tmp_path: Path, helper_mode: str, platform: str | None = None) -> Path:
    root = tmp_path / "bundle"
    (root / "hooks").mkdir(parents=True)
    for n in (K7_HOOK, "_hooklib.py", "_harness.py"):
        shutil.copy(U4D_HOOK_DIR / n, root / "hooks" / n)
    if platform:
        (root / "platform.json").write_text(json.dumps({"platform": platform}))
    helper = root / "engramd_owner_flags.py"
    if helper_mode == "real":
        shutil.copy(FLAGS_PY, helper)
    elif helper_mode == "raise":
        helper.write_text(f"raise RuntimeError({K7_SECRET!r})\n")
    elif helper_mode == "syntax":
        helper.write_text("def broken(:\n")
    elif helper_mode == "exit0":
        helper.write_text("import sys\nsys.exit(0)\n")
    elif helper_mode == "exit3":
        helper.write_text("import sys\nsys.exit(3)\n")
    elif helper_mode == "flag_on_exit":  # imports fine, raises SystemExit (with a message) when CALLED
        helper.write_text(f"import sys\ndef flag_on(key, config_path):\n    sys.exit({K7_SECRET!r})\n")
    elif helper_mode == "kbd":
        helper.write_text("raise KeyboardInterrupt\n")
    elif helper_mode == "cls80":  # an exception class whose name is at the 80-char cap
        name = "E" * 80
        helper.write_text(f"class {name}(Exception):\n    pass\nraise {name}('x')\n")
    else:
        assert helper_mode == "absent"
    return root / "hooks" / K7_HOOK


def _k7_home(tmp_path: Path, flag, with_people: bool = True, role: str = "r",
             primary: str | None = None, sticky=None) -> Path:
    import sqlite3
    home = tmp_path / "eh"
    home.mkdir()
    cfg = {}
    if flag is not None:
        cfg["engramd"] = {"user_identity_reads_via_owner": flag}
    if primary is not None:
        cfg["primary_user"] = primary
    if cfg:
        (home / "config.json").write_text(json.dumps(cfg))
    if sticky is not None:  # a dict (valid marker) or a str (raw file content)
        (home / "current_user.json").write_text(sticky if isinstance(sticky, str) else json.dumps(sticky))
    if with_people:
        conn = sqlite3.connect(str(home / "knowledge.db"))
        conn.execute("CREATE TABLE nodes (id TEXT, type TEXT, is_current INTEGER, claim TEXT, "
                     "logical_chain TEXT, metadata TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('pn_0001','person',1,'Alice','',?)",
                     (json.dumps({"name": "Alice", "aliases": [], "role": role}),))
        conn.commit()
        conn.close()
    return home


def _k7_run(hook: Path, home: Path, prompt: str = "Alice: hello there"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    p = subprocess.run([sys.executable, str(hook)], input=json.dumps({"prompt": prompt}),
                       capture_output=True, text=True, env=env, timeout=60, cwd=str(home))
    return p.returncode, p.stdout, p.stderr


def _k7_ctx(stdout: str) -> str:
    obj = json.loads(stdout)  # one json.loads over the whole output: ONE object, no trailing text
    assert list(obj) == ["hookSpecificOutput"], obj
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return obj["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("flag", [True, None], ids=["flag-true-in-config", "flag-absent"])
@pytest.mark.parametrize("mode,cls", [("absent", "ImportError"), ("raise", "RuntimeError"),
                                      ("syntax", "SyntaxError")])
def test_k7_a_failed_helper_import_is_visible_class_only_and_the_direct_read_still_runs(
        tmp_path, mode, cls, flag):
    hook = _k7_bundle(tmp_path, mode)
    home = _k7_home(tmp_path, flag)
    rc, out, err = _k7_run(hook, home)
    line = f"engram-user-identity-hook: engramd_owner_flags failed: {cls}"
    assert rc == 0, (rc, out, err)
    assert err.splitlines() == [line], err
    ctx = _k7_ctx(out)
    assert ctx.splitlines().count(line) == 1, ctx
    assert "ENGRAM User Identity: Alice" in ctx, "the direct read (flag OFF) must still produce its normal output"
    assert K7_SECRET not in out + err


def test_k7_with_nothing_else_to_print_the_payload_is_exactly_the_line(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    home = _k7_home(tmp_path, None, with_people=False)
    rc, out, err = _k7_run(hook, home)
    assert rc == 0
    assert _k7_ctx(out) == "engram-user-identity-hook: engramd_owner_flags failed: RuntimeError"


def test_k7_there_is_no_rate_limit_both_lines_on_every_run(tmp_path):
    hook = _k7_bundle(tmp_path, "absent")
    home = _k7_home(tmp_path, None)
    for _ in range(3):
        rc, out, err = _k7_run(hook, home)
        assert rc == 0 and len(err.splitlines()) == 1
        assert "engramd_owner_flags failed: ImportError" in _k7_ctx(out)


def test_k7_healthy_helper_adds_no_line_at_all(tmp_path):
    hook = _k7_bundle(tmp_path, "real")
    home = _k7_home(tmp_path, None)
    rc, out, err = _k7_run(hook, home)
    assert rc == 0 and err == ""
    ctx = _k7_ctx(out)
    assert "engramd_owner_flags failed" not in ctx and "ENGRAM User Identity: Alice" in ctx


# ---- K8(a): the refusal message uses the right article --------------------------------------------------

@pytest.mark.parametrize("name,ch,article", [("ampersand", "&", "an"), ("pipe", "|", "a")])
def test_k8a_refusal_message_uses_the_right_article(sb, name, ch, article):
    r = sb.run("--owner", engram_home=str(sb.tmp / f"h{ch}x"))
    assert r.returncode != 0
    assert f"contains {article} {name} character" in r.stderr, r.stderr


# ---- K8(b): no long run of stray alignment spaces in the hook header ---------------------------------------

def test_k8b_live_owner_bullet_does_not_trail_off_into_a_long_run_of_spaces():
    text = _require(HOOK).read_text()
    (line,) = [ln for ln in text.splitlines() if "never counts)" in ln]
    assert line.rstrip() == line.rstrip().split("never counts)")[0] + "never counts)", line
    assert "->" not in line and not re.search(r"\) {4,}", line), line


# ---------------------------------------------------------------------------
# Round 5, K9
# ---------------------------------------------------------------------------

# ---- K9a: {{ENGRAM_HOME}} / {{PYTHON}} text inside a path is re-substituted by sed -> refuse ------------

@pytest.mark.parametrize("bad", ["/opt/p{{ENGRAM_HOME}}x/python3", "/opt/p{{PYTHON}}x/python3"])
def test_k9a_a_placeholder_inside_the_python_path_is_refused(sb, bad):
    _stub(sb.stubs, "python3", f'#!/usr/bin/env bash\necho "{bad}"\n')
    r = sb.run("--owner", engram_home=str(sb.tmp / "seat-a"))
    _assert_refused_cleanly(sb, r, "python path")


@pytest.mark.parametrize("dirname", ["p{{ENGRAM_HOME}}", "p{{PYTHON}}", "p{{ENGRAM_OWNER_PY}}"])
def test_k9a_a_placeholder_inside_the_owner_script_path_is_refused(tmp_path, dirname):
    sb = Sandbox(tmp_path, plugin_name=dirname)
    r = sb.run("--owner", engram_home=str(tmp_path / "seat-a"))
    _assert_refused_cleanly(sb, r, "owner script path")
    assert not (tmp_path / "seat-a").exists()


def test_k9a_a_placeholder_inside_the_home_is_refused_too(sb):
    home = sb.tmp / "h{{ENGRAM_HOME}}x"
    r = sb.run("--owner", engram_home=str(home))
    _assert_refused_cleanly(sb, r, "ENGRAM_HOME")
    assert not home.exists()


# ---- K9b: the refusal message only claims what holds ----------------------------------------------------------

def test_k9b_the_physical_path_refusal_does_not_over_claim_about_directories(sb):
    real = sb.tmp / "real dir"
    real.mkdir()
    (sb.tmp / "clean-link").symlink_to(real)
    r = sb.run("--owner", engram_home=str(sb.tmp / "clean-link" / "sub" / "deeper"))
    _assert_refused_cleanly(sb, r, "ENGRAM_HOME")
    assert "Nothing was written" not in r.stderr, r.stderr
    assert "No unit was written or loaded" in r.stderr, r.stderr


# ---- K9c: the ~ spelling is expanded BEFORE the raw check, and before any mkdir ---------------------------------

def test_k9c_a_tilde_home_behind_a_spaced_HOME_is_refused_before_any_mkdir(tmp_path):
    sb = Sandbox(tmp_path, home_name="home dir")
    r = sb.run("--owner", engram_home="~/x")
    _assert_refused_cleanly(sb, r, "ENGRAM_HOME", "space")
    assert not (sb.home / "x").exists(), "the home was created before the refusal"


# ---------------------------------------------------------------------------
# Round 6
# ---------------------------------------------------------------------------

K7_LINE = "engram-user-identity-hook: engramd_owner_flags failed: {cls}"
STICKY_ALICE = {"name": "Alice", "person_id": "pn_0001", "role": "r", "description": "d"}


# ---- K10: the visible line never pushes the payload past the hook's budget -----------------------------

BIG_ROLE = "R" * 12000


@pytest.mark.parametrize("platform,budget", [("claude-code", 9500), (None, 4000)],
                         ids=["claude-code-9500", "unmeasured-floor-4000"])
@pytest.mark.parametrize("mode,cls", [("raise", "RuntimeError"), ("cls80", "E" * 80)])
def test_k10_a_broken_helper_never_pushes_the_payload_past_the_budget(tmp_path, mode, cls, platform, budget):
    hook = _k7_bundle(tmp_path, mode, platform=platform)
    home = _k7_home(tmp_path, None, role=BIG_ROLE)
    rc, out, err = _k7_run(hook, home)
    assert rc == 0, err[-300:]
    ctx = _k7_ctx(out)
    assert len(ctx) <= budget, (len(ctx), budget)
    assert ctx.splitlines()[0] == K7_LINE.format(cls=cls)
    assert "ENGRAM User Identity: Alice" in ctx


# The "before" hook is a FROZEN copy, not a `git show`: the pin commit lived only on the U5 branch (the squash merge
# did not carry it to dev) and the nightly checks out at depth 1, so any SHA-based read went dead (#3684). The
# fixture is the byte-exact content of src/engram/hooks/claude/engram-user-identity-hook.py at cf86878b2fe5a17ea914aeb073744cfa4cf5ce85
# (the commit before the import report, U5 #3663); the `.txt` suffix keeps it out of collection and compile scrubs.
K10_PRE_IMPORT_REPORT_HOOK = (REPO / "tests" / "fixtures" / "owner_launcher_3663"
                              / "engram-user-identity-hook.pre-import-report.py.txt")


@pytest.mark.parametrize("platform", ["claude-code", None])
def test_k10_the_healthy_path_is_byte_identical_to_the_commit_before_the_import_report(tmp_path, platform):
    old_text = K10_PRE_IMPORT_REPORT_HOOK.read_text()
    assert old_text, "frozen pre-import-report hook fixture is empty"
    outs = []
    for name, text in (("old", old_text), ("new", None)):
        d = tmp_path / name
        d.mkdir()
        hook = _k7_bundle(d, "real", platform=platform)
        if text is not None:
            hook.write_text(text)
        home = _k7_home(d, None, role=BIG_ROLE)
        rc, out, err = _k7_run(hook, home)
        assert rc == 0
        # the overflow file path in the trailer is per-ENGRAM_HOME: normalize it
        outs.append((out.replace(str(home), "<HOME>"), err))
    assert outs[0] == outs[1]


# ---- K11: EVERY stdout site in main() after the flag read reports; the line is FIRST --------------------------
#
# Site table (src/engram/hooks/claude/engram-user-identity-hook.py main(), enumerated by grep):
#   print(json.dumps({}))   x2  -- bad stdin / empty or slash prompt: BEFORE the flag is read (no import
#                                   attempted, so no report by design)
#   _emit(failure)              -- flag ON + owner failure: reachable ONLY with a working helper (flag ON
#                                   needs the import to succeed), so it can never carry a report
#   _emit({})  no people           S1
#   _emit(card) speaker switch     S2
#   _emit(card) sticky, non-primary S3
#   _emit({})  sticky is primary   S4
#   _emit({})  sticky unreadable   S5
#   _emit({})  no sticky file      S6

def _k7_assert_first_line_report(rc, out, err, cls="RuntimeError"):
    line = K7_LINE.format(cls=cls)
    assert rc == 0, err[-300:]
    assert err.splitlines() == [line], err
    ctx = _k7_ctx(out)
    assert ctx.splitlines()[0] == line, ctx          # PLACEMENT: first
    assert ctx.splitlines().count(line) == 1, ctx
    return ctx


def test_k11_s2_speaker_switch_reports_first(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    ctx = _k7_assert_first_line_report(*_k7_run(hook, _k7_home(tmp_path, None)))
    assert "ENGRAM User Identity: Alice" in ctx


def test_k11_s1_no_people_reports(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    ctx = _k7_assert_first_line_report(*_k7_run(hook, _k7_home(tmp_path, None, with_people=False)))
    assert ctx == K7_LINE.format(cls="RuntimeError")


def test_k11_s6_unprefixed_prompt_with_no_sticky_file_reports(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    ctx = _k7_assert_first_line_report(*_k7_run(hook, _k7_home(tmp_path, None), prompt="just a question"))
    assert ctx == K7_LINE.format(cls="RuntimeError")


def test_k11_s3_sticky_marker_for_a_non_primary_user_reports_first_then_the_card(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    home = _k7_home(tmp_path, None, sticky=STICKY_ALICE)
    ctx = _k7_assert_first_line_report(*_k7_run(hook, home, prompt="just a question"))
    assert "sticky session context" in ctx


def test_k11_s4_sticky_marker_for_the_primary_user_reports(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    home = _k7_home(tmp_path, None, primary="alice", sticky=STICKY_ALICE)
    ctx = _k7_assert_first_line_report(*_k7_run(hook, home, prompt="just a question"))
    assert ctx == K7_LINE.format(cls="RuntimeError")


def test_k11_s5_unreadable_sticky_marker_reports(tmp_path):
    hook = _k7_bundle(tmp_path, "raise")
    home = _k7_home(tmp_path, None, sticky="{ this is not json")
    ctx = _k7_assert_first_line_report(*_k7_run(hook, home, prompt="just a question"))
    assert ctx == K7_LINE.format(cls="RuntimeError")


def test_k11_the_failure_object_site_never_carries_a_report_because_flag_on_needs_the_helper(tmp_path):
    """Flag ON, healthy helper, no owner: the loud failure object is printed UNCHANGED, with no
    import-failure line (the site is unreachable with a broken helper: the flag would read OFF)."""
    hook = _k7_bundle(tmp_path, "real")
    home = _k7_home(tmp_path, True)
    rc, out, err = _k7_run(hook, home)
    assert rc == 0
    assert "engramd_owner_flags failed" not in out + err


# ---- K12: a bad copy in one candidate directory does not hide a good copy in the next -----------------------------

def _k12_probe(tmp_path: Path, rt_mode: str, bundle_mode: str, rt_exc: str = "RuntimeError",
               bundle_exc: str = "ValueError"):
    root = _flat_bundle(tmp_path, with_helper=False)
    rt = tmp_path / "rt"
    rt.mkdir()
    for dirpath, mode, exc in ((rt, rt_mode, rt_exc), (root, bundle_mode, bundle_exc)):
        target = dirpath / "engramd_owner_flags.py"
        if mode == "good":
            shutil.copy(FLAGS_PY, target)
        elif mode == "bad":
            target.write_text(f"raise {exc}('boom')\n")
    home = tmp_path / "eh"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"engramd": {"user_identity_reads_via_owner": True}}))
    code = textwrap.dedent(f"""
        import importlib.util, sys
        s = importlib.util.spec_from_file_location("uih", {str(root / 'hooks' / 'engram-user-identity-hook.py')!r})
        m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
        print("result=" + repr(m._owner_reads_enabled()))
        print("error=" + repr(m._OWNER_FLAGS_IMPORT_ERROR))
        """)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env.update(ENGRAM_HOME=str(home), ENGRAM_RUNTIME_DIR=str(rt))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(home))


def test_k12_a_raising_copy_in_the_explicit_runtime_dir_does_not_hide_the_good_bundle_copy(tmp_path):
    r = _k12_probe(tmp_path, rt_mode="bad", bundle_mode="good")
    assert r.returncode == 0, r.stderr[-300:]
    assert "result=True" in r.stdout and "error=None" in r.stdout, r.stdout + r.stderr[-300:]


def test_k12_when_every_candidate_fails_the_report_names_the_class_of_the_last_failure(tmp_path):
    r = _k12_probe(tmp_path, rt_mode="bad", bundle_mode="bad", rt_exc="RuntimeError", bundle_exc="ValueError")
    assert r.returncode == 0, r.stderr[-300:]
    assert "result=False" in r.stdout and "error='ValueError'" in r.stdout, r.stdout + r.stderr[-300:]


# ---------------------------------------------------------------------------
# Round 7
# ---------------------------------------------------------------------------

# ---- K13: SystemExit from the helper (at import, or when flag_on is CALLED) is a failed helper -----------------
# SystemExit is a BaseException: `except Exception` let it escape, so the hook printed NOTHING (0 bytes
# stdout, 0 bytes stderr, rc 0 or 3) where a healthy helper prints `{}`. KeyboardInterrupt must still escape.

@pytest.mark.parametrize("flag", [True, None], ids=["flag-true-in-config", "flag-absent"])
@pytest.mark.parametrize("mode", ["exit0", "exit3", "flag_on_exit"])
def test_k13_systemexit_from_the_helper_is_a_visible_failed_helper(tmp_path, mode, flag):
    hook = _k7_bundle(tmp_path, mode)
    home = _k7_home(tmp_path, flag)
    rc, out, err = _k7_run(hook, home)
    line = K7_LINE.format(cls="SystemExit")
    assert rc == 0, (rc, out, err[-300:])
    assert err.splitlines() == [line], err
    ctx = _k7_ctx(out)
    assert ctx.splitlines()[0] == line and ctx.splitlines().count(line) == 1, ctx
    assert "ENGRAM User Identity: Alice" in ctx, "the direct read (flag OFF) must still produce its normal output"
    assert K7_SECRET not in out + err


def test_k13_keyboardinterrupt_still_escapes_the_hook(tmp_path):
    """Pins that nobody widens the net to BaseException: Ctrl-C style interrupts keep killing the hook."""
    hook = _k7_bundle(tmp_path, "kbd")
    home = _k7_home(tmp_path, None)
    rc, out, err = _k7_run(hook, home)
    assert rc != 0, (rc, out, err[-300:])
    assert out == "" and "engramd_owner_flags failed" not in out + err
    assert "KeyboardInterrupt" in err


# ---------------------------------------------------------------------------
# Round 8: no hook file open may block (#3628), and every report is bounded (emit_context)
# ---------------------------------------------------------------------------

import socket as _socket


def _make_special(kind: str, path: Path):
    """Plant a non-regular file at `path`; returns an object to keep alive until the test ends."""
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink-to-fifo":
        target = path.parent / (path.name + ".target")
        os.mkfifo(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "socket":
        sock = _socket.socket(_socket.AF_UNIX)
        sock.bind(str(path))
        return sock
    else:
        raise AssertionError(kind)
    return None


# ---- K14: a planted special file at engramd.pid never blocks the hook; nothing is launched ------------------

@needs_proc
@pytest.mark.parametrize("kind,word", [("fifo", "fifo"), ("symlink-to-fifo", "symlink"),
                                       ("socket", "socket"), ("directory", "directory")])
def test_k14_a_non_regular_pidfile_never_blocks_launches_nothing_and_says_so_once(seat, kind, word):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    keep = _make_special(kind, seat.eh / "engramd.pid")
    try:
        rc, out, err, elapsed = seat.run_timed()
    finally:
        if keep is not None:
            keep.close()
    assert rc == 0 and elapsed < 3, (rc, elapsed, err[-200:])
    assert seat.launched() == [], "an owner was launched over a planted special file"
    r = subprocess.CompletedProcess([], rc, out, err)
    lines = context_lines(r)
    assert len(lines) == 1, out
    assert "engramd.pid is not a regular file" in lines[0] and word in lines[0], lines[0]
    assert "UNSUPERVISED" not in out


# ---- K14: a planted special file at engramd-owner.log never blocks the hook ---------------------------------------

@pytest.mark.parametrize("kind", ["fifo", "symlink-to-fifo"])
def test_k14_a_non_regular_log_never_blocks_the_launch_and_the_line_says_the_log_is_unavailable(seat, kind):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    _make_special(kind, seat.log)
    rc, out, err, elapsed = seat.run_timed()
    assert rc == 0 and elapsed < 3, (rc, elapsed, err[-200:])
    assert len(seat.launched()) == 1, "the owner launch must still proceed with its output discarded"
    lines = context_lines(subprocess.CompletedProcess([], rc, out, err))
    assert len(lines) == 1 and "UNSUPERVISED" in lines[0], out
    assert "log is unavailable" in lines[0] and "not a regular file" in lines[0], lines[0]


@pytest.mark.parametrize("kind", ["fifo", "symlink-to-fifo"])
def test_k14_a_non_regular_log_does_not_block_a_report_that_launches_nothing(seat, kind):
    seat.config(FLAG_ON)
    seat.systemctl("empty")  # ambiguous: a report, no launch
    _make_special(kind, seat.log)
    rc, out, err, elapsed = seat.run_timed()
    assert rc == 0 and elapsed < 3, (rc, elapsed)
    assert seat.launched() == [] and any("ambiguous" in ln for ln in context_lines(
        subprocess.CompletedProcess([], rc, out, err)))


def test_k14_a_normal_run_logs_to_a_regular_log_and_the_launched_argv_carries_the_marker(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, elapsed = seat.run_timed()
    assert rc == 0 and seat.log.is_file() and not seat.log.is_symlink()
    assert any("UNSUPERVISED" in ln for ln in seat.log_lines())
    (argv,) = seat.launched()
    assert "engramd_owner" in argv


def test_k14_flags_off_never_calls_the_python_io_helper(seat):
    seat.systemctl("nobus")
    r = seat.run()
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not seat.helper_calls.exists(), "the I/O helper was spawned on the flags-OFF path"


def test_k14_a_missing_io_helper_is_quiet_on_stdout_loud_on_stderr_and_launches_nothing(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    (seat.plugin / "hooks" / "_engramd_owner_hook_io.py").unlink()
    r = seat.run()
    assert r.returncode == 0 and r.stdout == "" and seat.launched() == []
    assert len(r.stderr.strip().splitlines()) == 1 and "_engramd_owner_hook_io.py" in r.stderr


# ---- K15: every report is bounded and trailer-first, whatever the home's length -----------------------------------

def _long_home_path(tmp_path: Path) -> Path:
    """The 2,400+ char home as a PATH only -- nothing is created on disk."""
    home = tmp_path
    for i in range(12):
        home = home / (chr(ord("a") + i) * 200)
    assert len(str(home)) >= 2400
    return home


def _long_home(tmp_path: Path) -> Path:
    """Create the long home; where the platform's PATH_MAX forbids it (macOS: 1,024), skip -- and only for
    that errno: any other OSError still fails the cell."""
    import errno
    home = _long_home_path(tmp_path)
    try:
        home.mkdir(parents=True)
    except OSError as e:
        if e.errno != errno.ENAMETOOLONG:
            raise
        pytest.skip("a 2,400+ char home cannot exist here (PATH_MAX); the helper-level cell covers the bound")
    return home


@pytest.mark.parametrize("platform", [None, "claude-code"], ids=["floor-4000", "claude-code-9500"])
@pytest.mark.parametrize("scenario,lead", [("launch", "UNSUPERVISED"), ("fail", "could not be started"),
                                           ("ambiguous", "not launched")])
def test_k15_a_report_for_a_very_long_home_is_bounded_trailer_first_json_and_keeps_its_lead_words(
        tmp_path, scenario, lead, platform):
    seat = Seat(tmp_path)
    if platform:
        (seat.plugin / "platform.json").write_text(json.dumps({"platform": platform}))
    home = _long_home(tmp_path)
    (home / "config.json").write_text(json.dumps({"engramd": FLAG_ON}))
    seat.systemctl("empty" if scenario == "ambiguous" else "nobus")
    if scenario == "fail":
        seat.fail_flag.write_text("x")
    rc, out, err, _ = seat.run_timed({"ENGRAM_HOME": str(home)}, limit=20)
    assert rc == 0, err[-300:]
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 1000, len(ctx)
    assert ctx.splitlines()[0].startswith("▸ "), ctx[:80]
    assert lead in ctx, ctx[:300]
    logged = (home / "engramd-owner.log").read_text()
    assert _physical(home) in logged, "the full home must remain in the log line"


def _path_exists(p) -> bool:
    """Existence check that is safe on an over-long path: os.path.exists maps ENAMETOOLONG to False, whereas
    pathlib's Path.exists raises it on Python <= 3.12 (3.13+ no longer does)."""
    return os.path.exists(str(p))


def test_k15_path_exists_is_safe_on_a_path_longer_than_any_platform_limit():
    """The trap behind the helper cell's check: a path > Linux's PATH_MAX (4,096) gives ENAMETOOLONG on every
    platform, as a 2,400-char one does on macOS (PATH_MAX 1,024)."""
    import errno
    p = Path("/" + "a" * 5000)
    assert _path_exists(p) is False
    try:
        p.exists()
    except OSError as e:  # Python <= 3.12 only; recorded, never required (3.13+ returns False)
        assert e.errno == errno.ENAMETOOLONG


_HELPER_LEADS = [
    ("UNSUPERVISED", "UNSUPERVISED: engramd_owner launched (pid 1); no supervisor reachable (systemd: x). "),
    ("could not be started", "ENGRAM owner could not be started: the launch failed. "),
    ("not launched", "ENGRAM owner not launched (systemd state is ambiguous: x). "),
]


@pytest.mark.parametrize("platform", [None, "claude-code"], ids=["floor-4000", "claude-code-9500"])
@pytest.mark.parametrize("lead,prefix", _HELPER_LEADS, ids=["launch", "fail", "ambiguous"])
def test_k15_helper_report_for_a_nonexistent_very_long_home_is_bounded_and_creates_nothing(
        tmp_path, lead, prefix, platform):
    """K2: the same bound as the K15 hook cells, with no real long path -- so a Mac still checks it."""
    seat = Seat(tmp_path)
    if platform:
        (seat.plugin / "platform.json").write_text(json.dumps({"platform": platform}))
    home = _long_home_path(tmp_path)
    message = f"{prefix}ENGRAM_HOME={home}"
    helper = seat.plugin / "hooks" / "_engramd_owner_hook_io.py"
    r = subprocess.run([sys.executable, str(helper), "report", str(home), message], capture_output=True,
                       text=True, timeout=20, cwd=str(tmp_path),
                       env={"HOME": str(seat.home), "ENGRAM_HOME": str(seat.eh), "PATH": os.environ.get("PATH", "")})
    assert r.returncode == 0, r.stderr[-300:]
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 1000, len(ctx)
    assert ctx.splitlines()[0].startswith("\u25b8 "), ctx[:80]
    assert lead in ctx, ctx[:300]
    assert not _path_exists(home) and not (tmp_path / ("a" * 200)).exists(), "the report created the home on disk"


def test_k15_long_home_skips_when_the_platform_path_limit_is_hit(tmp_path, monkeypatch):
    """K3a: ENAMETOOLONG from creating the home turns the K15 cells into a skip, not a failure."""
    import errno

    def boom(self, *a, **k):
        raise OSError(errno.ENAMETOOLONG, "File name too long")
    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(pytest.skip.Exception) as ei:
        _long_home(tmp_path)
    assert "PATH_MAX" in str(ei.value)


def test_k15_long_home_does_not_swallow_any_other_oserror(tmp_path, monkeypatch):
    """K3b: only ENAMETOOLONG skips; EACCES still fails the cell."""
    import errno

    def boom(self, *a, **k):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(Path, "mkdir", boom)
    try:
        _long_home(tmp_path)
    except pytest.skip.Exception:  # a Skipped is not an OSError: it would pass through pytest.raises as a skip
        pytest.fail("EACCES must fail the cell, not skip it")
    except OSError as e:
        assert e.errno == errno.EACCES
    else:
        pytest.fail("EACCES was swallowed")


# ---- bookkeeping: the new helper is tiered like start-engramd-owner.sh -------------------------------------------------

def test_the_io_helper_is_tiered_essential_with_a_flattening_dest():
    entry = _tiers().get("hooks/claude/_engramd_owner_hook_io.py")
    assert entry and entry["tier"] == "essential" and entry["dest"] == "hooks/_engramd_owner_hook_io.py", entry
