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
import tempfile
import textwrap
from pathlib import Path

import pytest

from _fake_owner import FakeOwner  # noqa: E402  (tests/ is on sys.path, as every sibling assumes)

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
    p.write_text(body, encoding="utf-8")  # a stub body may carry a non-ASCII path (#3794)
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

# #3939: the installer now polls `launchctl print` after bootout and retries a failed bootstrap; keep
# every cell that reaches launchctl fast (the defaults wait up to 10 s / sleep 1 s between attempts).
_TINY_LAUNCHD_ENV = {"ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01", "ENGRAM_SETUP_LAUNCHD_RETRY_S": "0.01",
                     "ENGRAM_SETUP_LAUNCHD_WAIT_S": "1"}

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
        # #3939: `print` answers 113 (service not found = gone) so the installer's post-bootout wait ends at once
        _stub(self.stubs, "launchctl",
              f'#!/usr/bin/env bash\necho "$@" >> "{self.launchlog}"\n'
              'if [ "$1" = print ]; then exit 113; fi\nexit 0\n')

    def run(self, *args: str, engram_home: str | None = None, allow_path: bool = True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ENGRAM_HOME", "ENGRAM_HOOKS_DIR", "ENGRAM_SETUP_ALLOW_PATH")}
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.stubs}:{os.environ['PATH']}"
        env.update(_TINY_LAUNCHD_ENV)
        if engram_home is not None:
            env["ENGRAM_HOME"] = engram_home
        argv = ["bash", str(self.installer), *args]
        if allow_path:
            argv.append("--allow-path")
        return subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=str(self.tmp))

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
        return [ln.split() for ln in self.syslog.read_text(encoding="utf-8").splitlines() if ln.strip()]


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
    return unit, home, parse_unit(unit.read_text(encoding="utf-8")), sb


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


def test_c2_exit1_already_running_is_restarted_unbounded_with_backoff(rendered):
    """Exit 1: restarted by Restart=on-failure and never stopped by a start limit --
    StartLimitIntervalSec=0 disables it (#3831) -- with a bounded BACKOFF (RestartSteps /
    RestartMaxDelaySec in [Service]) capping the retry rate instead of going permanently
    `failed`. Before #3831 this was 5 starts in 300 s, after which the unit stayed failed and
    every stop blocked until a human ran `systemctl --user reset-failed`. The
    StartLimitIntervalSec=0 assertion itself lives in the C1 render cell below, so the C1
    mutation reddens exactly that cell and not this one."""
    _u, _h, ini, _s = rendered
    svc, unit = ini["Service"], ini["Unit"]
    assert svc["Restart"][0] in ("on-failure", "always")
    for key in ("RestartPreventExitStatus", "SuccessExitStatus"):
        for v in svc.get(key, []):
            assert "1" not in v.split(), f"{key}={v} makes exit 1 un-restarted"
    assert _seconds(svc["RestartSec"][0]) >= 5
    # No start limit any more, so no inert StartLimitBurst may linger: StartLimit* belongs in
    # [Unit] when it is used at all, and in [Service] systemd ignores it either way.
    assert "StartLimitBurst" not in unit and "StartLimitBurst" not in svc, \
        "an inert StartLimitBurst must not remain once StartLimitIntervalSec=0 disables the limit"
    # The backoff that replaces the limit, in [Service] where systemd reads it.
    steps = svc["RestartSteps"][0]
    assert steps.isdigit() and int(steps) >= 1, steps
    assert _seconds(svc["RestartMaxDelaySec"][0]) >= _seconds(svc["RestartSec"][0])
    assert "StartLimitIntervalSec" not in svc and "RestartSteps" not in unit \
        and "RestartMaxDelaySec" not in unit, "the restart directives belong in [Service]"


def test_c2_exit3_displaced_must_be_restarted(rendered):
    """Exit 3 is never a success status (no SuccessExitStatus, not in RestartPreventExitStatus), so
    Restart=on-failure restarts it -- unbounded, like exit 1 (#3831:
    RestartSteps/RestartMaxDelaySec backoff instead of a start limit)."""
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
    raw = [ln.strip() for ln in unit.read_text(encoding="utf-8").splitlines()
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
    rows = [ln for ln in unit.read_text(encoding="utf-8").splitlines()
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
    text1 = first.read_text(encoding="utf-8")
    second = sb.install_owner(home)
    assert first == second and len(sb.owner_units()) == 1
    assert second.read_text(encoding="utf-8") == text1


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
    out = (sb.unit_dir / "engram-surface-daemon.service").read_text(encoding="utf-8")
    hooks_dir = sb.plugin / "hooks" / "claude"
    py = subprocess.run(["python3", "-c", "import sys; print(sys.executable)"],
                        capture_output=True, encoding="utf-8", errors="replace", env={**os.environ, "HOME": str(sb.home),
                        "PATH": f"{sb.stubs}:{os.environ['PATH']}"}).stdout.strip()
    expected = (TEMPLATES / "engram-surface-daemon.service.template").read_text(encoding="utf-8") \
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
              # ---- #3831: restart-detail modes (additive; the hook reads state AND detail from
              # ONE `show` spawn -- round 2 folded NRestarts/SubState/ExecMainStatus into the one
              # `-p` list, so every mode below must be answerable by a single call) --
              running-0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=0\\nExecMainStatus=0\\n'; else nf; fi;;
              # ROUND 6 (#3835, B1) -- RE-FIXTURED. This read `ExecMainStatus=1` with no
              # `ExecMainCode`, i.e. `active/running` carrying an exit status. That record does not
              # occur on systemd 255.4: systemd ZEROES ExecMainCode/ExecMainStatus when it spawns a
              # new main process, so a running unit reads 0/0 whatever it exited with before
              # (measured: 18 long-running services on this host read a large NRestarts with 0/0).
              # The restart count is kept; the code/status pair is the measured one. The exit clause
              # is therefore NOT printworthy here, and the loud line must say so (see C4's running-3
              # cell and C8e, both rewritten this round).
              running-3) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=3\\nExecMainStatus=0\\nExecMainCode=0\\n'; else nf; fi;;
              # ROUND 6 (#3835, B1/B3): `ExecMainCode=1` ADDED -- this is `activating/auto-restart`,
              # the one state where the exit IS retained (Sol measured `code 1, status 1` on a unit
              # waiting to retry), so a fixture omitting the code models a record that does not
              # occur. Without it the B1 gate reads code 0 and drops the exit clause; with it the
              # clause and exit 5's meaning stay pinned (C4's restart-loop cell).
              restart-loop) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=auto-restart\\nNRestarts=5\\nExecMainStatus=5\\nExecMainCode=1\\n'; else nf; fi;;
              failed-4) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=2\\nExecMainStatus=4\\nExecMainCode=1\\n'; else nf; fi;;
              # ---- round 2 (#3835): the two states the round-1 fixture set could not reach ----
              # `failed` at NRestarts >= 3: exit 4 is in RestartPreventExitStatus, so systemd will
              # NOT retry it -- the unit IS permanently failed. Round 1 routed this to the LOUD
              # line, whose retry clause said the opposite. (twin: `failed-4`, one restart lower)
              failed-4-nr3) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=3\\nExecMainStatus=4\\nExecMainCode=1\\n'; else nf; fi;;
              # A manual stop: `ActiveState=inactive` + `SubState=dead` -- the REAL pair, NOT
              # `SubState=inactive`, which systemd never emits (see C7e). No failure to clear, so
              # `reset-failed` would be a no-op. `ExecMainCode=1` (CLD_EXITED) is what makes this a
              # REAL stop rather than a unit that never ran: both read `success`/`ExecMainStatus=0`,
              # and only the code separates them (#3835 round 5, C9c; see the `never-started-0`
              # twin below, which is the 70-unit population of units that never ran a process).
              stopped-0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nNRestarts=0\\nExecMainStatus=0\\nExecMainCode=1\\n'; else nf; fi;;
              # ---- round 5 (#3835): the never-started population, measured on the parent's host
              # as the `inactive`/`success` + ExecMainCode=0 row (70 of 402 units; `ExecMainStatus=0`
              # as well, which is exactly why `Result`+`ExecMainStatus` alone cannot tell it apart
              # from `stopped-0` above). LoadState/ActiveState/SubState/NRestarts/ExecMainCode/
              # ExecMainStatus/Result are the live field values.
              never-started-0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nNRestarts=0\\nExecMainStatus=0\\nExecMainCode=0\\nResult=success\\n'; else nf; fi;;
              # round 5: `auditd.service`'s shape, the live example for the `failed` arm --
              # `failed`/`failed` + `Result=exit-code` while the record holds NO process exit
              # (ExecMainCode=0) and reports status 0. The failed arm's exit clause would print
              # `(last exit 0: clean stop)` for a unit systemd calls FAILED.
              failed-0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=5\\nExecMainStatus=0\\nExecMainCode=0\\nResult=exit-code\\n'; else nf; fi;;
              # round 2: state readable, detail NOT -- the faithful single-spawn simulation of a
              # detail read that could not be completed (round 1 made the whole call exit 1; with
              # one spawn for state+detail that would read as `nobus` and launch, which is the
              # wrong degrade. The state triplet is returned and the detail fields are absent.)
              detail-broken) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=auto-restart\\n'; else nf; fi;;
              # ---- round 4 (#3835): `activating` with a substate that is NOT `auto-restart` --
              # `restart-loop` pins only `auto-restart`. `activating` also carries `start` -- a
              # unit actively starting again -- and the loud line's claims are FALSE there at
              # NRestarts=0 (it has never restarted, and systemd is STARTING it, not retrying).
              # C8a and C8b pin the two sides of that NRestarts boundary.
              activating-start-0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=start\\nNRestarts=0\\nExecMainStatus=0\\n'; else nf; fi;;
              activating-start-1) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=start\\nNRestarts=1\\nExecMainStatus=1\\n'; else nf; fi;;
              # ---- round 4: the loud threshold from below, and exits 0 and 3 through the loud line
              # ROUND 6 (#3835, B1): `running-2` re-fixtured with `running-3` (see above) -- a running
              # unit reads 0/0.
              running-2) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=2\\nExecMainStatus=0\\nExecMainCode=0\\n'; else nf; fi;;
              loud-exit0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=3\\nExecMainStatus=0\\n'; else nf; fi;;
              # ROUND 6 (#3835, B1): `ExecMainCode=1` ADDED for the same reason as `restart-loop` --
              # `activating/auto-restart` retains the exit, so omitting the code models a record that
              # does not occur. Retaining it keeps C8f's exit-3 meaning pin green under the B1 gate.
              loud-exit3) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=auto-restart\\nNRestarts=1\\nExecMainStatus=3\\nExecMainCode=1\\n'; else nf; fi;;
              # ---- round 6 (#3835, B2/B3): records Sol measured on systemd 255.4 that no fixture
              # above reaches --
              # B2: the FIRST failure, waiting for the first retry. The exit IS recorded (code 1)
              # while the counter still reads 0: `h2` in Sol's harness.
              auto-restart-nr0) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=auto-restart\\nNRestarts=0\\nExecMainStatus=1\\nExecMainCode=1\\n'; else nf; fi;;
              # B3: SIGILL on a `Type=simple` unit -- `Result=core-dump ExecMainCode=3
              # ExecMainStatus=4`. Code 3 is CLD_DUMPED, so status 4 is SIGNAL 4, not "exit 4": the
              # ungated clause printed exit 4's own meaning and its "fix the path" remedy.
              failed-killed-4) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=1\\nExecMainStatus=4\\nExecMainCode=3\\n'; else nf; fi;;
              killed-loud-4) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=activating\\nSubState=auto-restart\\nNRestarts=1\\nExecMainStatus=4\\nExecMainCode=3\\n'; else nf; fi;;
              # B3: SIGKILL is CLD_KILLED (code 2) -- a DIFFERENT si_code from SIGILL's CLD_DUMPED,
              # so the signal arm is pinned on both, not on one value.
              failed-killed-9) if [ "$unit" = "$(cat "{self.sysunit}")" ]; then
                        printf 'LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=1\\nExecMainStatus=9\\nExecMainCode=2\\n'; else nf; fi;;
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

    def run(self, extra_env: dict | None = None, script: Path | None = None, keep_plugin_root: bool = False,
            bash: str = "bash"):
        self._ship_3709_files()
        env = {"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
               "PATH": f"{self.stubs}:{self.tools}", "TMPDIR": str(self.tmp)}
        if keep_plugin_root:
            env["CLAUDE_PLUGIN_ROOT"] = str(self.plugin)
        env.update(extra_env or {})
        return subprocess.run([bash, str(script or self.hook)], capture_output=True, encoding="utf-8", errors="replace",
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
                             encoding="utf-8", errors="replace", env=env, cwd=str(self.tmp), start_new_session=True)
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
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def launched(self) -> list[str]:
        return self.record.read_text(encoding="utf-8").splitlines() if self.record.exists() else []

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
    assert "owner-stub-output" in logs[0].read_text(encoding="utf-8"), "owner stdout was not routed to the log"


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
    assert seat.unit_name() in seat.syslog.read_text(encoding="utf-8"), "asked about the wrong unit name"
    assert all(c.split()[:2] == ["--user", "show"] for c in seat.syslog.read_text(encoding="utf-8").splitlines()), \
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
    assert unit.name in seat.syslog.read_text(encoding="utf-8"), (unit.name, seat.syslog.read_text(encoding="utf-8"))


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
    data = json.loads(TIERS.read_text(encoding="utf-8"))
    out = {}
    for entry in data["mechanisms"] if "mechanisms" in data else []:
        out[entry["path"]] = entry
    return out


def test_hooks_json_registers_the_fallback_on_session_start_and_resolves_when_flattened():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
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
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", errors="replace", timeout=20)
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
        return subprocess.run([sys.executable, str(FLAGS_PY), str(home)], capture_output=True, encoding="utf-8", errors="replace")
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
    return subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", errors="replace", env=env,
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

# ---- F5: a launch that LOST a race -- silent when the winner SERVES, loud when it does not --

def _hook_lines_in_log(seat: Seat) -> list[str]:
    return [ln for ln in seat.log_lines() if "] [start-engramd-owner]" in ln]


def _race_winner(seat: Seat, order: str):
    """The stub 'owner' dies at once (exit 7), as a loser of the OwnerAlreadyRunning race does,
    while a live owner for THIS home appears -- already there, or written 0.3 s later. The
    pidfile is written by the launch stub itself, so step 3 ran with NO pidfile: the hook really
    launches and really loses, which is what makes this the race path."""
    winner = subprocess.Popen(["bash", "-c", "exec -a engramd_owner sleep 30"])
    pidfile = seat.eh / "engramd.pid"
    if order == "pidfile-already-there":
        seat.pre_script.write_text(f"printf %s {winner.pid} > '{pidfile}'\n")
    else:
        seat.pre_script.write_text(
            f"( sleep 0.3; printf %s {winner.pid} > '{pidfile}' ) >/dev/null 2>&1 &\n")
    return winner


def _short_home_for_socket(seat: Seat) -> Path:
    """#3736: an ENGRAM_HOME short enough to bind `<home>/run/engramd.sock`.

    AF_UNIX sun_path is 104 bytes on macOS / 108 on Linux, and pytest's tmp_path nests far deeper
    than that for this test's long name. Same workaround, same reason, as `sock_dir` in
    tests/test_engramd_client.py and `ProbeSeat` in tests/test_owner_serving_probe_3736.py: a short
    mkdtemp directory, never tmp_path. The client is copied into the bundle the same way those
    fixtures do it, so the hook's `serving` probe can import it. `seat.eh` is the ONE home every
    Seat path (config, pidfile, log, ENGRAM_HOME) resolves from, so reassigning it moves them all.
    """
    d = Path(tempfile.mkdtemp(prefix="f5srv-"))
    eh = d / "eh"
    eh.mkdir()
    shutil.copy(SRC / "engramd_client.py", seat.plugin / "engramd_client.py")
    shutil.copy(SRC / "engram_home_resolver.py", seat.plugin / "engram_home_resolver.py")  # #3863 step 2: the client imports it
    seat.eh = eh
    return d


def _raw_probe(seat: Seat) -> str:
    """The helper's OWN `serving` verdict, run directly: the shell's report is composed from
    exactly this string, so the two cannot disagree. Same mechanism, same reason, as `_raw_probe`
    in tests/test_owner_serving_probe_3736.py: a silent cell must prove its silence came from a
    probe that RAN and answered `serving`, not from a probe that could not run (`ambiguous:`
    also stays silent)."""
    seat._ship_3709_files()  # the harness's own bundle setup (Seat.run() calls it too)
    env = {"HOME": str(seat.home), "ENGRAM_HOME": str(seat.eh),
           "PATH": f"{seat.stubs}:{seat.tools}", "TMPDIR": str(seat.tmp)}
    r = subprocess.run(
        [sys.executable, str(seat.plugin / "hooks" / "_engramd_owner_hook_io.py"),
         "serving", str(seat.eh)],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
        cwd=str(seat.tmp), timeout=30,
    )
    assert r.returncode == 0, r.stderr[-300:]
    return r.stdout.strip()


@needs_proc
@pytest.mark.parametrize("order", ["pidfile-already-there", "pidfile-written-late"])
def test_f5_a_loser_whose_launch_died_is_silent_when_the_winner_serves(seat, order):
    """#3736 (d1): the winner owns a live pid AND answers `ping` -- the one race outcome that
    stays silent. The socket is REAL (FakeOwner), so this is not a sleep standing in for
    serving."""
    d = _short_home_for_socket(seat)
    try:
        seat.config(FLAG_ON)
        seat.systemctl("nobus")
        seat.fail_flag.write_text("x")
        winner = _race_winner(seat, order)
        try:
            with FakeOwner(seat.eh):
                # Silence is only evidence if the probe RAN and answered `serving`: a probe that
                # cannot import the client answers `ambiguous:` and also stays silent (#3736 F5).
                assert _raw_probe(seat) == "serving", "the F5 control needs a probe that really ran"
                r = seat.run()
        finally:
            winner.kill()
            winner.wait()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert len(seat.launched()) == 1, "the hook should have attempted exactly one launch"
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert _hook_lines_in_log(seat) == [], seat.log_lines()


@needs_proc
@pytest.mark.parametrize("order", ["pidfile-already-there", "pidfile-written-late"])
def test_f5_a_loser_whose_launch_died_reports_a_live_owner_that_is_not_serving(seat, order):
    """#3736 (d2): the same lost race, but the winner never binds a socket. A live pid is not a
    serving owner, so the loser must say so ONCE instead of exiting silently -- while still
    launching no second owner. The report states the bound (the probe window), never that the
    owner does not serve."""
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    # the probe must be able to run, or its answer would be `ambiguous:` and nothing is claimed
    shutil.copy(SRC / "engramd_client.py", seat.plugin / "engramd_client.py")
    shutil.copy(SRC / "engram_home_resolver.py", seat.plugin / "engram_home_resolver.py")  # #3863 step 2: the client imports it
    seat.fail_flag.write_text("x")
    winner = _race_winner(seat, order)
    try:
        r = seat.run()
    finally:
        winner.kill()
        winner.wait()
    assert len(seat.launched()) == 1, "the hook should have attempted exactly one launch"
    assert r.returncode == 0
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    line = lines[0]
    assert "alive but NOT SERVING" in line, line
    assert "(last: " in line, line  # the helper's own observation travels with the claim
    assert "another launch for engramd.pid" in line, line
    assert "most likely another session's hook" in line, line  # INFERRED, never stated as fact
    assert "this launcher's own launch died" in line, line
    assert "within the probe window" in line, line  # BUDGETED: never "does not serve"
    assert "may still be starting" in line, line  # the winner is seconds old: a budgeted claim too
    assert _physical(seat.eh) in line, line
    assert len(_hook_lines_in_log(seat)) == 1, seat.log_lines()


def test_f5_control_no_live_owner_after_the_wait_is_still_the_loud_failure(seat):
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.fail_flag.write_text("x")
    (seat.eh / "engramd.pid").write_text("999999")  # a dead pid is not an owner
    r = seat.run()
    assert any("could not be started" in ln for ln in context_lines(r)), r.stdout


# ---- (a) no status line on every seat's session start -----------------------------------

def test_hooks_json_entry_has_no_status_message_and_keeps_its_timeout():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    (entry,) = [h for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]
                if "start-engramd-owner.sh" in h["command"]]
    assert "statusMessage" not in entry, entry
    assert entry["timeout"] == 15


# ---- (c) the helper's docstring no longer claims a reader that does not exist ---------------

def test_no_stale_claim_of_a_second_flag_reader_in_the_helper():
    text = _require(FLAGS_PY).read_text(encoding="utf-8")
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
    r = subprocess.run([sys.executable, str(FLAGS_PY), str(tmp_path)], capture_output=True, encoding="utf-8", errors="replace")
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
    # #3780: tab / LF / CR are members of the one control-byte class arm, named by their byte.
    "space": " ", "control byte 0x09": "\t", "control byte 0x0A": "\n", "control byte 0x0D": "\r",
    "ampersand": "&",
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
    assert f"ENGRAM_HOME={home}" in parse_unit(unit.read_text(encoding="utf-8"))["Service"]["Environment"]


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
    text = _require(OWNER_TEMPLATE).read_text(encoding="utf-8")
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
                       capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=60, cwd=str(home))
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
    text = _require(HOOK).read_text(encoding="utf-8")
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
    old_text = K10_PRE_IMPORT_REPORT_HOOK.read_text(encoding="utf-8")
    assert old_text, "frozen pre-import-report hook fixture is empty"
    outs = []
    for name, text in (("old", old_text), ("new", None)):
        d = tmp_path / name
        d.mkdir()
        hook = _k7_bundle(d, "real", platform=platform)
        if text is not None:
            hook.write_text(text, encoding="utf-8")
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
    return subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=str(home))


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
        # AF_UNIX paths are capped at 104 (macOS) / 108 (Linux) bytes, and the limit applies to the string
        # handed to bind(); bind by a relative name from the parent so a deep tmp prefix cannot trip it (#3808).
        sock = _socket.socket(_socket.AF_UNIX)
        cwd = os.getcwd()
        try:
            os.chdir(path.parent)
            sock.bind(path.name)
        except BaseException:
            sock.close()
            raise
        finally:
            os.chdir(cwd)
        return sock
    else:
        raise AssertionError(kind)
    return None


def test_make_special_socket_binds_a_path_longer_than_the_af_unix_limit(tmp_path):
    """#3808: the helper binds by a RELATIVE name, so a deep tmp prefix cannot trip `AF_UNIX path too long`.
    Needs no /proc, so it runs on every OS."""
    deep = tmp_path / ("d" * 20)
    while len(str(deep / "engramd.pid")) <= 110:  # > 108 (Linux) and > 104 (macOS)
        deep = deep / ("d" * 20)
    deep.mkdir(parents=True)
    path = deep / "engramd.pid"
    assert len(str(path).encode()) > 108, len(str(path).encode())
    before = os.getcwd()
    sock = _make_special("socket", path)
    try:
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        assert os.getcwd() == before, "the helper must restore the cwd"
    finally:
        sock.close()
    assert sock.fileno() == -1, "the socket must be closed at the end"


@pytest.mark.parametrize("failure", ["missing-parent", "name-already-taken"])
def test_make_special_socket_restores_the_cwd_when_it_raises(tmp_path, failure):
    """missing-parent fails at chdir; name-already-taken fails at bind AFTER the chdir succeeded (the case
    where only the `finally` restores the cwd)."""
    if failure == "missing-parent":
        path = tmp_path / "no-such-dir" / "engramd.pid"
    else:
        path = tmp_path / "engramd.pid"
        path.write_text("x")
    before = os.getcwd()
    with pytest.raises(OSError):
        _make_special("socket", path)
    assert os.getcwd() == before, "the cwd must be restored even when the helper raises"


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
    logged = (home / "engramd-owner.log").read_text(encoding="utf-8")
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
                       encoding="utf-8", errors="replace", timeout=20, cwd=str(tmp_path),
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


# ---------------------------------------------------------------------------
# #3831 -- the owner unit must not go permanently failed (restarts + bounded backoff)
# ---------------------------------------------------------------------------
#
# On a seat with any `engramd.*_via_owner` flag on, a crash loop reached systemd's start limit
# and the unit went permanently `failed` -- after which EVERY stop blocked until a human ran
# `reset-failed`. macOS has no such limit (launchd throttles and keeps trying), so this was also
# a platform-parity defect. The fix: StartLimitIntervalSec=0 (unlimited restarts) + a bounded
# exponential backoff, and a launcher line so a restart loop is LOUD rather than quiet.

# ---- C1: the rendered unit disables the start limit and adds the backoff --------------------
#
# MUTATION CONTROL for this cell: restore `StartLimitIntervalSec=300` in
# templates/engramd-owner.service.template and re-run -- THIS cell goes red
# (StartLimitIntervalSec != ["0"]) while the other render cells (exit 0/1/3/4 semantics, argv,
# ENGRAM_HOME, the log routing) stay green. A mutation that reddens everything proves nothing.

def test_c1_rendered_unit_disables_the_start_limit_and_adds_the_backoff(rendered):
    unit, _h, ini, _s = rendered
    u, svc = ini["Unit"], ini["Service"]
    # StartLimitIntervalSec=0 DISABLES the rate limit: a crash loop restarts forever instead of
    # going permanently `failed`. It belongs in [Unit]; in [Service] systemd ignores it.
    assert u.get("StartLimitIntervalSec") == ["0"], u.get("StartLimitIntervalSec")
    # An inert directive left in a file people read to learn the restart policy is the exact
    # "code should not mislead" problem this unit fixes: StartLimitBurst must not linger.
    assert "StartLimitBurst" not in u and "StartLimitBurst" not in svc, \
        "StartLimitBurst is inert under StartLimitIntervalSec=0 and must not remain"
    # The backoff that replaces the limit, in [Service] where systemd reads it.
    assert svc.get("RestartSteps") == ["5"], svc.get("RestartSteps")
    assert svc.get("RestartMaxDelaySec") == ["300s"], svc.get("RestartMaxDelaySec")
    assert "RestartSteps" not in u and "RestartMaxDelaySec" not in u
    # Unchanged by #3831.
    assert svc["Restart"] == ["on-failure"]
    assert _seconds(svc["RestartSec"][0]) == 10
    assert svc["RestartPreventExitStatus"] == ["4"]


def test_c1b_the_rendered_header_states_the_systemd_254_floor_for_the_backoff(rendered):
    """RestartSteps/RestartMaxDelaySec are systemd >= 254. Below that they are UNKNOWN KEYS
    (warned and IGNORED), so the unit degrades to "restart forever, no backoff" -- never to a
    refused unit. The header must say so, next to the existing >= 240 `append:` note, so an
    editor who deletes the two directives is made to revisit the sentence. A light prose pin, by
    design: it names the floor and the two directives together."""
    unit, _h, _ini, _s = rendered
    lines = unit.read_text(encoding="utf-8").splitlines()
    named = [i for i, ln in enumerate(lines)
             if ln.lstrip().startswith("#") and "254" in ln
             and "RestartSteps" in ln and "RestartMaxDelaySec" in ln]
    assert named, "no header comment names the >= 254 floor together with the two directives"
    steps_at = next(i for i, ln in enumerate(lines) if ln.strip().startswith("RestartSteps="))
    assert named[0] < steps_at, "the >= 254 floor note belongs in the header, before the directive"
    assert any(ln.lstrip().startswith("#") and "240" in ln for ln in lines), \
        "the pre-existing >= 240 `append:` note must stay next to it"


# ---- C3: exit 4 is still not restarted -------------------------------------------------------

def test_c3_exit4_stays_out_of_restarts_under_the_new_policy(rendered):
    """#3831 must not convert a planted-path refusal into an infinite retry loop: exit 4 is still
    the ONE status RestartPreventExitStatus lists (so the unbounded policy never retries it), and
    the file's raw line agrees."""
    unit, _h, ini, _s = rendered
    code = str(_exit_path_not_regular())
    svc = ini["Service"]
    assert svc.get("RestartPreventExitStatus") == [code], svc.get("RestartPreventExitStatus")
    raw = [ln.strip() for ln in unit.read_text(encoding="utf-8").splitlines()
           if ln.strip().startswith("RestartPreventExitStatus=")]
    assert raw == [f"RestartPreventExitStatus={code}"], raw
    assert not ({"1", "3"} & set(svc["RestartPreventExitStatus"][0].replace(",", " ").split()))
    assert svc["Restart"] == ["on-failure"]


def test_c3_both_planted_path_refusals_map_to_exit4():
    """The issue's item 3 note: OwnerPidfileNotRegular and OwnerStorePathNotRegular are BOTH
    exit 4 (EXIT_PATH_NOT_REGULAR), so the one RestartPreventExitStatus=4 excludes both. Read off
    the owner's own shared refusal->exit table (no drift, no second copy of the mapping)."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engramd_owner
    assert engramd_owner.EXIT_PATH_NOT_REGULAR == 4
    assert engramd_owner._refusal_exit_code(engramd_owner.OwnerPidfileNotRegular()) \
        == engramd_owner.EXIT_PATH_NOT_REGULAR
    assert engramd_owner._refusal_exit_code(engramd_owner.OwnerStorePathNotRegular()) \
        == engramd_owner.EXIT_PATH_NOT_REGULAR


# ---- C4: the launcher's loud line ------------------------------------------------------------
#
# Under the new policy a persistent fault systemd RESTARTS no longer reaches `failed` -- it reads
# `activating (auto-restart)` forever, a QUIETER failure than a failed unit. Exit 4, in
# RestartPreventExitStatus, is the exception: it still reaches a permanent stop, by design, pre-PR
# and post-PR alike. So the launcher
# surfaces it: ONE bounded additionalContext object naming the restart count, the last exit
# status, the MEANING of that status, and the remedy. `_unit_detail()` returns an EMPTY detail on
# ANY failure, and an empty detail degrades to today's message exactly: a broken detail read must
# never be louder or more confident than the read it replaced.

def test_c4_a_restarting_unit_gets_exactly_one_loud_line_with_count_status_and_remedy(seat):
    seat.config(FLAG_ON)
    seat.systemctl("restart-loop", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0
    assert seat.launched() == [], "a registered unit must never get a second owner"
    assert len(r.stdout.splitlines()) == 1, r.stdout  # ONE bounded JSON object, the stdout contract
    lines = context_lines(r)
    assert len(lines) == 1, lines
    (line,) = lines
    assert seat.unit_name() in line and "auto-restart" in line, line
    assert "NRestarts=5" in line, line                      # the restart count
    assert "last exit 5" in line, line                     # the last exit status
    assert "schema" in line and "migration" in line, line  # the MEANING of status 5
    assert "reset-failed" in line, line                    # the remedy
    assert any(line in ln for ln in seat.log_lines()), seat.log_lines()


def test_c4_a_healthy_running_unit_produces_no_line(seat):
    """Silent when healthy: a running unit with NRestarts=0 produces no line at all."""
    seat.config(FLAG_ON)
    seat.systemctl("running-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert seat.launched() == []
    assert seat.log_lines() == [], seat.log_lines()


def test_c4_a_running_unit_that_has_restarted_three_times_is_loud(seat):
    """`NRestarts >= 3` is a loud trigger even while the unit is RUNNING: a unit that crashed
    three times and came back looks healthy and is not. (The healthy cell above pins the
    NRestarts=0 side of the same boundary.)

    ROUND 6 (#3835, B1) -- RE-ASSERTED, not weakened. Its two exit-clause assertions (`last exit 1`,
    `another owner holds this seat`) were true only of fixture `running-3` as it was written:
    `active/running` carrying `ExecMainStatus=1`. That record does not occur -- systemd zeroes
    ExecMainCode/ExecMainStatus when it spawns the new main process, so a running unit reads 0/0 --
    and the fixture is now the measured one, so the clause those assertions named must NOT be
    printed. The LOUDNESS claim, which is what this cell exists for, is unchanged; the exit clause's
    absence is now pinned beside it (as in the rewritten C8e)."""
    seat.config(FLAG_ON)
    seat.systemctl("running-3", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and "NRestarts=3" in lines[0], r.stdout
    assert "is now running" in lines[0], lines[0]
    assert "last exit" not in lines[0], lines[0]


def test_c4_a_planted_path_failure_says_which_it_is_and_names_the_remedy(seat):
    """`registered but failed`: the line must say WHICH failure it is (exit 4 = a planted
    non-regular path) and name the remedy, `reset-failed` (the issue's item 4)."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-4", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1 and seat.unit_name() in lines[0], r.stdout
    assert "failed" in lines[0] and "last exit 4" in lines[0], lines[0]
    assert "planted" in lines[0] and "reset-failed" in lines[0], lines[0]


def test_c4_a_broken_unit_detail_read_degrades_to_todays_message_and_launches_nothing(seat):
    """`_unit_detail()`'s failure contract: the detail FIELDS are absent from the single `show`
    record here. Round 2 put state and detail on ONE spawn, so a detail that could not be read is
    a record with no NRestarts/ExecMainStatus -- not a failing call, which would read as `nobus`
    and launch (the wrong degrade). The detail is EMPTY and the launcher falls back to exactly
    today's message -- never a wrong or partial line, and never a remedy it did not establish."""
    seat.config(FLAG_ON)
    seat.systemctl("detail-broken", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert f"ENGRAM owner unit {seat.unit_name()} is registered but activating" in line, line
    assert "leaving it to systemd, no second owner launched" in line, line
    assert _physical(seat.eh) in line, line
    assert "reset-failed" not in line, \
        "a failed detail read must not name a remedy it could not establish"
    assert "NRestarts" not in line, line


# ---- C7 (round 3, #3835): route by STATE first, count second ---------------------------------
#
# The round-1 defect. `_detail_is_loud()` was `[ "$subst" = "auto-restart" ] || [ "$nr" -ge 3 ]`
# -- it never consulted whether the unit was FAILED -- and `_owner_loud_line()` printed a
# CONSTANT retry clause:
#     "under the unbounded restart policy a restartable exit never goes permanently failed, so
#      systemd keeps retrying."
# For `detail = "3 failed 4"` that is the exact opposite of the truth: exit 4 is the one status
# in RestartPreventExitStatus=4, so systemd will NEVER retry that unit -- it IS permanently
# failed. Because NRestarts persists until `reset-failed`, ANY exit-4 refusal after three earlier
# restarts reached that branch, and it made `_owner_down_line()`, which words `failed` correctly,
# UNREACHABLE at NRestarts >= 3.
#
# Why the round-1 suite could not see it: its `failed-4` fixture sits at NRestarts=2, one below
# the `>= 3` threshold. That cell, `test_c4_a_planted_path_failure_says_which_it_is_and_names_the
# _remedy`, is NOT restated here and is left UNCHANGED by this round; its greenness beside C7b's
# is the point -- the hole lived one restart above the fixture.
#
# The round-2 fix routed by SubState. Round 3 re-keys the SAME routing to ActiveState, systemd's
# own state axis: a STOPPED unit reads `ActiveState=inactive` + `SubState=dead`, and
# `SubState=inactive` is NEVER a value systemd emits, so round 2's `inactive` arm was unreachable
# and a stopped unit fell through to today's message, which names NO remedy (C7a below is that
# fix landing; C7e is the pin that keeps it there). The routing (`_owner_verdict` in the hook) is:
#   failed      -> DOWN line, ALWAYS, whatever NRestarts; systemd will not retry it
#   inactive    -> DOWN line; not a failure, it was stopped; remedy is `start` only, never
#                  `reset-failed` (a no-op with nothing to clear)
#   activating  -> LOUD line; here systemd IS retrying (the clause is true)
#   active      -> silent unless NRestarts >= 3 -> loud; it RESTARTED and is now running, so it is
#                  NOT being retried
#   other/empty -> today's behaviour
#
# NAMING: these cells were C6a-C6e in round 2 and are renumbered to the spec's C7a-C7e, because
# the axis they pin changed from SubState to ActiveState. C7e is the vocabulary pin.

def test_c7a_a_stopped_unit_is_told_to_start_and_never_to_reset_failed(seat):
    """C7a -- `ActiveState=inactive` + `SubState=dead`, the REAL stopped pair, reaches the DOWN
    line; the text names `start` and does NOT name `reset-failed`. This is finding 3 ACTUALLY
    landing, and it is RED against round 2: round 2 keyed the arm on `SubState == "inactive"`, a
    value systemd never emits, so `dead` fell to "any other" -- today's message, which names no
    remedy at all. (Round 2's `stopped-0` fixture printed the impossible `SubState=inactive`; it is
    now the real pair.) The LOUD line must not be emitted for a stopped unit.

    ROUND 4 (#3835): the asserted clause moved from "it was stopped rather than failed" to "it is
    not running (stopped, or never started)". `ActiveState=inactive` + `SubState=dead` is emitted
    for a unit that was NEVER STARTED as well as for a manual stop, so the old clause over-stated
    the history -- the same claim-must-not-outrun-its-bound class as this round's Defect 1.

    ROUND 5 (#3835): this fixture now carries `ExecMainCode=1`, so the unit really did run and exit
    before it was stopped, and its line therefore keeps the exit clause. That is the point of the
    C9 group: an `inactive` unit with NO recorded exit gets a different line (C9a), and C9c pins this
    side of the boundary. The assertions here are the remedy and the history clause, not the exit
    clause."""
    seat.config(FLAG_ON)
    seat.systemctl("stopped-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but inactive" in line, line          # the DOWN line
    assert "it is not running (stopped, or never started)" in line, line  # exact: `inactive`
    #   covers a never-started unit too, so no over-stated history claim
    assert "systemctl --user start" in line, line              # the remedy
    assert "reset-failed" not in line, \
        "a stopped unit has nothing to clear: `reset-failed` is a no-op and must not be named"
    assert "has restarted" not in line, "the LOUD line was emitted for a stopped unit"


def test_c7b_a_failed_unit_at_three_restarts_reaches_the_down_line_not_the_loud_one(seat):
    """C7b -- `failed`/`failed` + exit 4 reaches the DOWN line at NRestarts >= 3, AND the text
    does not claim systemd is retrying. This is the reachability fix: under round 1 the count-first
    predicate sent it to the LOUD line, whose constant clause said systemd "keeps retrying" -- the
    opposite of the truth for the one status in RestartPreventExitStatus. The only difference from
    the pre-existing C4 cell is the restart count, which is exactly the axis round 1 got wrong."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-4-nr3", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but failed" in line, line                  # the DOWN line ...
    assert "systemd will not retry it" in line, line                 # ... and it says so
    assert "has restarted" not in line, "the LOUD line was emitted for a FAILED unit"
    assert "NRestarts=" not in line, line
    assert "keeps retrying" not in line, line
    assert "never goes permanently failed" not in line, line
    assert "last exit 4" in line, line                               # the status ...
    assert "planted" in line, line                                   # ... and its MEANING


def test_c7c_a_mid_restart_unit_still_gets_the_loud_line_saying_systemd_is_retrying(seat):
    """C7c -- `activating`/`auto-restart` is the ONE state where the loud clause is TRUE: the unit
    really is being retried (Restart=on-failure + StartLimitIntervalSec=0), and the line must keep
    saying so.

    DISCLOSURE (carried from round 2, #3835): this cell PASSED BEFORE its implementation. It pins
    behaviour round 1 already had right -- the old `[ "$subst" = "auto-restart" ] || [ "$nr" -ge 3 ]`
    predicate already routed `auto-restart` to the loud line -- so it is a REGRESSION GUARD, not a
    red-first cell, and is NOT evidence of a defect this round caught. (Round-2 name: C6c.)"""
    seat.config(FLAG_ON)
    seat.systemctl("restart-loop", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "auto-restart" in line and "NRestarts=5" in line, line
    assert "systemd keeps retrying" in line, line


def test_c7d_a_running_unit_that_restarted_three_times_is_not_claimed_to_be_retried(seat):
    """C7d -- `active`/`running` + NRestarts >= 3 IS loud, but the unit is RUNNING: it restarted
    and came back, so it is NOT being retried. The loud clause must not appear (round 1 emitted it
    here)."""
    seat.config(FLAG_ON)
    seat.systemctl("running-3", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "NRestarts=3" in line, line
    assert "is now running" in line, line
    assert "keeps retrying" not in line, line
    assert "never goes permanently failed" not in line, line


def test_c7e_the_vocabulary_pin_substate_inactive_is_not_a_value_systemd_emits(seat):
    """C7e (VOCABULARY PIN) -- `SubState=inactive` is NOT a value systemd emits: a STOPPED unit is
    `ActiveState=inactive` + `SubState=dead`. MEASURED on systemd 255 by planting a `Type=oneshot`
    unit (`ExecStart=/bin/true`): `systemctl --user show <u> -p ActiveState -p SubState` reads
    `inactive`/`dead` both before it has ever been started and after a clean start; only a FAILURE
    reads `failed`/`failed`. So the routing must key on ActiveState, and this cell is what stops a
    well-meaning "fix" from re-keying it back to SubState.

    SCOPE (round 4, #3835 nit): assertion (1) below reads THIS TEST FILE's own `systemctl` stub
    text, so it cannot fail as a function of any hook implementation -- it pins the FIXTURES'
    vocabulary, not the hook's behaviour, and must never be counted as evidence about the hook.
    Only assertion (2) is evidence about the hook.

    Both assertions redden if the routing is re-keyed on SubState again: (1) no `systemctl` stub
    fixture in this suite may print the impossible pair -- round 2's `stopped-0` did, which is
    exactly why its unreachable `inactive` arm LOOKED covered; and (2) the REAL stopped pair must
    reach a real verdict and not today's fallback. Under a SubState-keyed route `dead` maps to no
    arm, the fallback ("leaving it to systemd") is emitted, and (2) fires."""
    seat.config(FLAG_ON)
    seat.systemctl("stopped-0", seat.unit_name())
    stub = (seat.stubs / "systemctl").read_text(encoding="utf-8")
    # Only the EMITTED payloads count -- a comment may name the impossible value (the fixture's own
    # comment does, to say why it is not emitted), but a `printf` must never carry it.
    emitted = "\n".join(ln for ln in stub.splitlines() if "printf" in ln)
    assert "SubState=inactive" not in emitted, (
        "a stub fixture prints `SubState=inactive`, which systemd never emits: a stopped unit is "
        "`ActiveState=inactive` + `SubState=dead`. A cell built on that pair is testing an "
        "impossible input and cannot be evidence of anything (see C7a).")
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "leaving it to systemd" not in line, (
        "the REAL stopped pair fell through to today's fallback: the routing is keyed on SubState, "
        "not on ActiveState -- `dead` is not an `inactive` arm, so no remedy was named")
    assert "systemctl --user start" in line, line


def test_c7f_one_fire_makes_exactly_one_systemctl_show_call(seat):
    """C7f -- state and detail must come from ONE `systemctl --user show` spawn (round-2 finding
    2): two spawns can describe two different moments of a unit that changed between them. The
    fields are folded into one `-p` list and the call COUNT is pinned, so a future re-introduction
    of the second spawn reddens here even if both answers happen to agree. (Round-2 name: C6g;
    orthogonal to the ActiveState re-keying, so it keeps its assertions verbatim.)"""
    seat.config(FLAG_ON)
    seat.systemctl("restart-loop", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0
    raw = seat.syslog.read_text(encoding="utf-8").splitlines() if seat.syslog.exists() else []
    shows = [ln.split() for ln in raw if ln.split()[1:2] == ["show"]]
    assert len(shows) == 1, shows
    (call,) = shows
    assert call[0] == "--user", call
    joined = " ".join(call)
    # `ExecMainCode` is in the SAME `-p` list (#3835 round 5): the exit-clause gate must not have
    # bought a second spawn to read it.
    for key in ("LoadState", "ActiveState", "SubState", "NRestarts", "ExecMainStatus", "ExecMainCode"):
        assert key in joined, (key, call)


# ---- C8 (round 4, #3835): the loud line's claims must be TRUE in every state it is reached ----
#
# Round 3 re-keyed the routing onto ActiveState and that fix stands. Round 4 closes the hole one
# level down -- the SAME defect class the PR exists to fix. `_owner_verdict` sent EVERY
# `activating` substate to `loud`, so `activating` + `SubState=start` (systemd is STARTING the
# unit again, not retrying it) at `NRestarts=0` (a unit that has NEVER restarted) was handed the
# loud line, whose two claims are then false: "has restarted: NRestarts=0" is self-contradictory,
# and "systemd keeps retrying" is false for a unit being started rather than retried. `activating`
# also carries `reload`, `stop` and `final-sigterm`, and `restart-loop` pins only `auto-restart`,
# so no cell in this suite reached the other substates.
#
# The corrected contract keys the loud verdict on BOTH fields (#3835 round 4):
#   activating + auto-restart              -> loud    (systemd IS waiting to retry; clause true)
#   activating + any other substate, N >= 1 -> loud   (it HAS restarted; clause true)
#   activating + any other substate, N = 0  -> unknown -> today's claim-nothing message
# The loud line's own two-arm clause selection is untouched (the round-4 spec forbids
# restructuring it).
#
# NAMING: the older C8 (`test_c8_default_mode_...`, the `--owner`-free installer cell) is untouched;
# these round-4 cells take the letters the round-4 spec assigns (C8a, C8b, ...).


def test_c8a_activating_never_restarted_is_not_claimed_to_have_restarted(seat):
    """C8a -- `activating` + `SubState=start` + `NRestarts=0`: systemd is STARTING this unit and it
    has NEVER restarted, so the loud line's "has restarted: NRestarts=0" and "systemd keeps
    retrying" are both false. The launcher must emit today's claim-nothing message instead.

    RED against the round-3 hook, which routed every `activating` substate to loud. This is the
    round-4 Defect 1 cell; the mutation that reddens it is `_owner_verdict` routing on ActiveState
    alone again."""
    seat.config(FLAG_ON)
    seat.systemctl("activating-start-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but activating" in line, line
    assert "leaving it to systemd, no second owner launched" in line, line
    assert "has restarted" not in line, (
        "the loud line claims the unit restarted, but NRestarts=0: it never restarted")
    assert "NRestarts=" not in line, line
    assert "keeps retrying" not in line, (
        "systemd is STARTING this unit, not retrying it: the retry clause is false here")
    assert "reset-failed" not in line, (
        "a claim-nothing fallback must not name a remedy it did not establish")


def test_c8b_activating_with_a_restart_count_still_gets_the_true_loud_line(seat):
    """C8b -- C8a's other boundary: `activating` + `SubState=start` with `NRestarts=1`. The unit
    HAS restarted once and is starting again, so the loud line's claims are true by construction
    and it must still be emitted; the fix for C8a must not send every non-`auto-restart`
    `activating` state to silence.

    DISCLOSURE: this cell PASSED BEFORE its implementation -- round 3 also emitted the loud line
    here, by routing on ActiveState alone -- so it is a REGRESSION GUARD against OVER-correcting
    C8a (a fix that maps all non-`auto-restart` `activating` states to `unknown`), not evidence of
    a defect this round caught. (C7c carries the same disclosure.) Mutation: make `_owner_verdict`
    return `unknown` for every non-`auto-restart` `activating` state -> THIS cell reddens while
    C8a and C7c stay green."""
    seat.config(FLAG_ON)
    seat.systemctl("activating-start-1", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "has restarted" in line, line
    assert "NRestarts=1" in line, line
    assert "SubState=start" in line, line
    assert "systemd keeps retrying" in line, line
    assert "leaving it to systemd" not in line, line


def test_c8c_the_planted_path_remedy_text_itself_fires_the_fix_the_path_step(seat):
    """C8c -- the exit-4 remedy branch (`_owner_down_line`'s `[ "$main" = "4" ]` arm) must be
    OBSERVED, not inferred. The pre-existing C4 cell asserts `"planted" in line`, which exit 4's
    own `_owner_exit_meaning` parenthetical satisfies on its own -- so deleting the remedy branch
    leaves all thirteen c4/c7 cells green. This cell pins the remedy text itself.

    Mutation: delete the `[ "$main" = "4" ]` line (revert to the generic `reset-failed, then
    start` remedy) -> THIS cell reddens and only this cell. A COVERAGE PIN: GREEN against the
    round-3 hook, since the branch already exists and already reads correctly; it is red only
    under that mutation."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-4", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "fix the path, then run" in line, line
    assert "reset-failed" in line, line


def test_c8d_the_loud_threshold_is_silent_at_two_restarts(seat):
    """C8d -- the loud threshold for a RUNNING unit (`active` + `NRestarts >= 3`) was pinned at 0
    (C4's healthy cell) and at 3 (C4's loud cell) but never at 2, so the boundary was pinned from
    one side only. A unit that restarted twice and is running now is not the repeated-crash signal
    the loud line exists for: it stays silent.

    Mutation: change the threshold to `-ge 2` -> THIS cell reddens while C4's NRestarts=0 and
    NRestarts=3 cells both stay green. A COVERAGE PIN: GREEN against the round-3 hook."""
    seat.config(FLAG_ON)
    seat.systemctl("running-2", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    assert r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    assert seat.log_lines() == [], seat.log_lines()


def test_c8e_the_loud_line_claims_no_clean_stop_for_a_unit_whose_record_holds_no_exit(seat):
    """C8e -- REWRITTEN IN ROUND 6 (#3835, B1); it previously pinned a FALSE sentence.

    WHAT IT PINNED: fixture `loud-exit0` (`active/running NRestarts=3 ExecMainStatus=0`) is a unit
    that exited 1 three times and came back, so it reads `NRestarts=3` with `ExecMainCode=0
    ExecMainStatus=0` -- systemd zeroes both when it spawns the new main process, so those fields
    describe the CURRENT process, not the last exit. The cell asserted `last exit 0` and
    `clean stop` appear, i.e. it REQUIRED the launcher to hand a crash-looping owner a clean bill of
    health. It was described as a coverage pin for exit 0's meaning through the loud line; it was in
    fact a pin on a line that must not exist, and it is the reason the suite could not see B1.

    WHAT IT ASSERTS NOW: at `active`/`running` with `ExecMainCode=0` the loud line carries NO exit
    clause -- neither `last exit` nor `clean stop` -- while still being LOUD (it names the restart
    count, the SubState, and that a running unit is not being retried). The negative half is what
    reddens if the B1 gate is reverted; the positive half is what reddens if the unit is made silent
    instead of gated. (`loud-exit0` also has no `ExecMainCode` line, which `_unit_detail` reads as 0
    -- the same record.)

    DISCRIMINATION PARTNER: C9c (`inactive`/`dead` + ExecMainCode=1) still carries `last exit 0` and
    `clean stop`, so this cell cannot be satisfied by dropping the exit clause everywhere."""
    seat.config(FLAG_ON)
    seat.systemctl("loud-exit0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "is now running" in line, line
    assert "NRestarts=3" in line, line
    assert "last exit" not in line, (
        "the loud line asserted a last exit for a unit whose record holds none: systemd zeroes "
        "ExecMainCode/ExecMainStatus when it spawns the new main process (ExecMainCode=0)")
    assert "clean stop" not in line, (
        "a crash-looping owner that is running again was told it stopped cleanly")


def test_c8f_the_loud_line_names_the_meaning_of_a_displaced_substrate_exit_three(seat):
    """C8f -- `_owner_exit_meaning(3)` ("the owner shut down on a displaced substrate") was
    unpinned: only exits 1 (C4's running-3) and 5 (C4/C7c's restart-loop) were reached. A unit
    mid-restart whose last exit was 3 (EXIT_DISPLACED, restarted under the unbounded policy) is
    loud, and the line must name exit 3's meaning.

    Mutation: delete the `3)` arm of `_owner_exit_meaning` -> THIS cell reddens while the exit-1
    and exit-5 cells stay green. A COVERAGE PIN: GREEN against the round-3 hook."""
    seat.config(FLAG_ON)
    seat.systemctl("loud-exit3", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "auto-restart" in line and "NRestarts=1" in line, line
    assert "last exit 3" in line, line
    assert "displaced substrate" in line, line


# ---- C9 (round 5, #3835): no line may assert an exit the record does not hold -----------------
#
# THE CONTRACT (state it here, and the cells below assert it):
#   1. No printed line may assert a process exit unless `ExecMainCode != 0`. When `ExecMainCode = 0`
#      the line must say that NO exit status is recorded, and must not name an exit status or a
#      meaning for one.
#   2. Round 5 applied the gate to the DOWN arms only (`inactive`, and the `*)`/failed arm), on the
#      reasoning that the LOUD line is reachable only where an exit exists BY CONSTRUCTION --
#      `activating`/`auto-restart` (systemd is waiting to retry AFTER an exit), a non-`auto-restart`
#      `activating` at NRestarts>=1, and `active` at NRestarts>=3 (runs completed).
#      ROUND 6 (#3835, B1) FALSIFIED THAT: it is true of the HISTORY (NRestarts>=1) and false of the
#      FIELD. The `active` arm and the non-`auto-restart` `activating` arm are both reachable with
#      ExecMainCode=0, because systemd zeroes the code and status when it spawns the new main
#      process. The gate therefore applies to BOTH lines now, and the loud line is three-way on the
#      code (see the C10 group below and C8e). Round 5's `activating/auto-restart` reasoning was
#      right and is unchanged: the exit IS retained there (measured `code 1`).
#   3. Exactly one `systemctl show` spawn per fire (cell C7f pins it); `ExecMainCode` joins the one
#      `-p` list and adds no second spawn.
#   4. A failed/absent detail read still degrades to the plain message (cell C4, unchanged).
#
# `ExecMainCode` is the siginfo `si_code` of the main process: 0 = NOT SET (no process exit was
# recorded), 1 = CLD_EXITED, 2 = CLD_KILLED, 3 = CLD_DUMPED. MEASURED over 402 units on the parent's
# host: 70 read `inactive`/`success` with ExecMainCode=0 (never ran a process) against 16 with
# ExecMainCode=1 + ExecMainStatus=0 (a real stop after a clean exit) -- `Result` + `ExecMainStatus`
# alone cannot separate those two populations (both read success/0), which is why the exit clause
# was asserted of a history the record did not hold. The `failed` arm has the same hole:
# `auditd.service` is `failed`/`exit-code` with ExecMainCode=0, and the arm would have printed
# `last exit 0: clean stop` for it. Fifth recurrence of this PR's defect class, third one level down.


def test_c9a_a_never_started_unit_is_not_told_it_stopped_cleanly(seat):
    """C9a -- `inactive`/`dead` + `ExecMainCode=0`: systemd recorded NO process exit, so this is one
    of the 70 measured units that never ran a process (`ExecMainStatus=0` and `Result=success` too
    -- the reason the code field, not the status, is the discriminator). The DOWN line must print
    NO exit clause: neither `last exit` nor `clean stop`, the meaning of exit 0 that the record does
    not hold. It must still name the `start` remedy and still cover BOTH histories ("stopped, or
    never started").

    RED against `a7cef5cc`: its `inactive` arm prints `(last exit 0: clean stop)` unconditionally --
    asserting a clean stop for a unit that never ran a process, in the same sentence that admits it
    may never have started. (Discrimination partner: C9c.)"""
    seat.config(FLAG_ON)
    seat.systemctl("never-started-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but inactive" in line, line
    assert "clean stop" not in line, (
        "a never-started unit was told it stopped cleanly: ExecMainCode=0 records no exit at all")
    assert "last exit" not in line, line
    assert "systemctl --user start" in line, line
    assert "stopped, or never started" in line, line
    assert "reset-failed" not in line, line


def test_c9b_a_failed_unit_with_no_recorded_exit_does_not_claim_a_clean_stop(seat):
    """C9b -- the `*)`/failed arm's half of the same gate: `failed`/`failed` + `ExecMainCode=0` is
    `auditd.service`'s live shape -- systemd reports the unit FAILED while the record holds no
    process exit and reports status 0. The arm must not print `clean stop` (nor `last exit`), while
    the remedy still names both `reset-failed` and `start`.

    RED against `a7cef5cc`, whose failed arm prints `(last exit 0: clean stop)` for that unit."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but failed" in line, line
    assert "clean stop" not in line, (
        "a failed unit with no recorded exit was told it stopped cleanly")
    assert "last exit" not in line, line
    assert "reset-failed" in line, line
    assert "systemctl --user start" in line, line


def test_c9c_a_real_stop_still_reports_its_exit_status(seat):
    """C9c -- the DISCRIMINATION PARTNER for C9a, and the reason C9a cannot be satisfied by deleting
    the exit clause outright. `inactive`/`dead` + `ExecMainCode=1` is a unit that DID run and exit
    cleanly before it was stopped (16 of the measured 402 units). Its exit clause must survive the
    gate intact: `last exit 0` AND its meaning ("clean stop") are both present.

    GREEN against `a7cef5cc` as well (the unconditional clause happened to be right HERE) -- so it
    is a control, not evidence of the defect: it reddens only under the plausible-wrong fix that
    drops the clause from every down line instead of gating it."""
    seat.config(FLAG_ON)
    seat.systemctl("stopped-0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    (line,) = lines
    assert "is registered but inactive" in line, line
    assert "last exit 0" in line, line
    assert "clean stop" in line, line


# ---- C10 (round 6, #3835): the loud line's exit clause, and a signal is not an exit ------------
#
# Sol's colleague read (PR #3835 review 5552280898) ran this launcher's real routing code against
# real systemd 255.4 and found three members of the C9 class that five rounds of diff-reading could
# not see. Each cell below pairs the POSITIVE claim with the absence of the text that must not
# appear, so neither "delete the clause" nor "print it always" can satisfy the group.
#
#   B1 -- the loud line's exit clause was UNGATED. systemd zeroes ExecMainCode/ExecMainStatus when
#         it spawns a new main process, so `active`/`running` and non-`auto-restart` `activating`
#         are reachable with code 0 and the line asserted an exit that is not in the record.
#   B2 -- `activating`/`auto-restart` at NRestarts=0 is the FIRST failure, waiting for the first
#         retry; the leading clause nevertheless read "has restarted: NRestarts=0".
#   B3 -- ExecMainCode is the siginfo `si_code`: 1 = CLD_EXITED, 2 = CLD_KILLED, 3 = CLD_DUMPED.
#         At 2 or 3, ExecMainStatus is a SIGNAL number. Testing only `!= 0` passed signal 4 to
#         `_owner_exit_meaning`, printing exit 4's own meaning AND its "fix the path" remedy.
#
# The loud line's clause is now the SAME three-way gate as the down line's (`_owner_loud_line`),
# and `_owner_exit_meaning` is still the only place a STATUS is given a meaning -- signal arms name
# the signal and claim no meaning at all.

def test_c10a_a_loud_restarting_unit_with_no_recorded_exit_names_no_exit(seat):
    """C10a -- B1's second reachable state. `activating` + `SubState=start` at NRestarts>=1 is LOUD
    (round 4's arm: the unit has restarted) but reaches the line with `ExecMainCode=0`: systemd has
    already spawned the new main process, which is what makes the substate `start`. The loud claims
    that survive -- the restart count, the SubState, the retry clause -- must still be there, and NO
    exit clause may be named. Round 5 exempted this arm explicitly, calling the exit "by
    construction"; it is by construction in the HISTORY and absent from the FIELD.

    Mutation: restore the ungated clause in `_owner_loud_line` -> this cell and C8e redden; the
    exit-carrying cells (C4's restart-loop, C8f) stay green."""
    seat.config(FLAG_ON)
    seat.systemctl("activating-start-1", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "has restarted" in line and "NRestarts=1" in line, line
    assert "SubState=start" in line, line
    assert "systemd keeps retrying" in line, line
    assert "last exit" not in line, (
        "the loud line named a last exit for a unit whose record holds ExecMainCode=0")


def test_c10b_the_first_failure_is_not_claimed_to_have_restarted(seat):
    """C10b -- B2. `activating`/`auto-restart` at NRestarts=0 is the unit's FIRST failure, waiting
    for its first retry (Sol measured `Result=exit-code NRestarts=0 ExecMainCode=1
    ExecMainStatus=1`). The leading clause read "has restarted: NRestarts=0" -- self-contradictory.
    It must say the unit failed and is waiting for its first restart instead, while the state stays
    LOUD (round 4's "loud whatever the count" decision is deliberately unchanged: the defect is the
    sentence, not the volume) and the exit clause is retained, because `auto-restart` DOES hold the
    exit.

    Mutation: restore the constant leading clause -> THIS cell reddens and only this cell. No cell
    fed this record before this round, so it is red-first."""
    seat.config(FLAG_ON)
    seat.systemctl("auto-restart-nr0", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "has failed" in line and "first restart" in line, line
    assert "NRestarts=0" in line, line
    assert "has restarted" not in line, (
        "a unit that has restarted ZERO times was told it has restarted: NRestarts=0")
    assert "systemd keeps retrying" in line, line
    assert "last exit 1" in line and "another owner holds this seat" in line, line


def test_c10c_a_signal_killed_unit_is_not_given_an_exit_status_or_its_remedy(seat):
    """C10c -- B3 on the loud line. Sol measured SIGILL on a `Type=simple` unit as `code 3`
    (CLD_DUMPED) with `ExecMainStatus=4`: status 4 is SIGNAL 4, not "exit 4". The ungated clause
    printed exit 4's own meaning -- `a planted non-regular path (fix the path, then reset-failed)`
    -- handing the reader a WRONG REMEDY, which is an instruction to act on. The line must name the
    signal, must not name an exit, exit 4's meaning or its remedy, and must stay loud.

    Mutation: restore `2 | 3` to the exit-status clause -> THIS cell, C10d and C10e redden while the
    exit-4 control cell (C10f) stays green."""
    seat.config(FLAG_ON)
    seat.systemctl("killed-loud-4", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "killed by signal 4" in line, line
    assert "systemd keeps retrying" in line, line
    assert "last exit" not in line, line
    assert "fix the path" not in line, (
        "signal 4 is not exit 4: the exit-4 remedy must not be printed for CLD_KILLED/CLD_DUMPED")
    assert "clean stop" not in line, line


def test_c10d_a_failed_unit_killed_by_a_signal_gets_no_exit_remedy(seat):
    """C10d -- B3 on the DOWN `*)`/failed arm. This is the arm the PR's round-5 gate DID guard, but
    only on `code != 0`, so a signal number reached `_owner_exit_meaning` and the `[ "$main" = "4" ]`
    remedy arm fired on SIGNAL 4. Both halves are asserted: the clause names the signal, and the
    exit-4 remedy text is absent. The generic remedy (`reset-failed`, then `start`) survives -- it
    is the right recovery for a failed unit whatever killed it.

    Mutation: revert only the `2 | 3` arm -> HERE and C10c/C10e redden; C10f stays GREEN."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-killed-4", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "is registered but failed" in line, line
    assert "killed by signal 4" in line, line
    assert "last exit" not in line, line
    assert "fix the path" not in line, (
        "SIGILL (code 3, status 4) was given exit 4's remedy: a wrong remedy is worse than none")
    assert "planted" not in line, line          # exit 4's MEANING must not survive the signal arm
    assert "reset-failed" in line and "systemctl --user start" in line, line


def test_c10e_the_signal_arm_is_pinned_on_both_kill_codes(seat):
    """C10e -- B3, second kill code. `code 2` is CLD_KILLED (SIGKILL), a different `si_code` from
    C10d's CLD_DUMPED, and its status 9 is a signal number too. Pinned separately so an arm written
    for only one of the two values is caught: this is the same "one arm, two boundaries" shape as
    C8a/C8b."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-killed-9", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "killed by signal 9" in line, line
    assert "last exit" not in line, line
    assert "unrecognized exit status" not in line, (
        "signal 9 was passed to the exit-status table as if it were an exit status")


def test_c10f_control_exit_four_keeps_its_own_remedy_through_the_signal_gate(seat):
    """C10f -- THE CONTROL for C10c/C10d/C10e, and the reason the B3 gate cannot be satisfied by
    deleting the exit clause or the remedy. `failed`/`failed` + `ExecMainCode=1` (CLD_EXITED) +
    `ExecMainStatus=4` IS a real exit 4 -- the status in `RestartPreventExitStatus` -- so the exit
    clause keeps BOTH its status and its meaning, and the `fix the path` remedy MUST still be
    printed. It is GREEN both before and after this round; it reddens only under an over-correction
    that makes the three-way arm swallow the code-1 case."""
    seat.config(FLAG_ON)
    seat.systemctl("failed-4", seat.unit_name())
    r = seat.run()
    assert r.returncode == 0 and seat.launched() == []
    (line,) = context_lines(r)
    assert "last exit 4" in line, line
    assert "planted" in line, line
    assert "fix the path, then run" in line, line
    assert "killed by signal" not in line, line


# ---- C5: macOS untouched ---------------------------------------------------------------------
#
# macOS BEHAVIOUR is unchanged by #3831 (a Linux-only fix). Asserted BY DIFF -- a byte-for-byte
# comparison against the base commit's content -- never by reading for strings. The baselines are
# frozen here rather than read with `git show` (nightly checks out at depth 1, so a SHA-based read
# goes dead) and rather than stored as a fixture file (#3831's scope lists exactly four files).
#
# #3831 RE-BASELINED the plist cell: the guarantee is byte-identity OUTSIDE the header comment
# block, so every XML key, value and array stays pinned against the base commit while the comment
# -- which states this file's relationship to the systemd unit, the very unit #3831 changed -- is
# permitted to move (see that cell's docstring). The launcher's macOS branch has no such carve-out
# and stays whole-branch byte-identical. Re-baseline deliberately if a macOS change is intended.

PLIST_BASELINE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{{LABEL}}</string>

    <!-- Every launch path's argv MUST contain `engramd_owner` (engram_pidfile's program
         marker): the script path below carries it. ENGRAM_HOME is read from
         EnvironmentVariables, as for the surface daemon.

         Exit codes are supervised by meaning, matching engramd-owner.service.template:
           0  clean stop            -> NOT restarted. KeepAlive {SuccessfulExit: false}
                                       restarts only after a NON-zero exit.
           1  OwnerAlreadyRunning
              (and every refusal)   -> restarted, once per ThrottleInterval.
           3  EXIT_DISPLACED        -> restarted: the owner shut down on a displaced
                                       substrate without closing the store, so it must
                                       never read as a success.
         Unlike the systemd unit there is NO start limit under launchd: a persistent
         non-zero exit (a refusal the install-time preflight cannot see, or an owner
         already running unsupervised) retries once every ThrottleInterval (60 s)
         indefinitely. launchd shows that as `spawn scheduled`, and the line
         start-engramd-owner.sh prints names the last exit status. -->
    <key>ProgramArguments</key>
    <array>
        <string>{{PYTHON}}</string>
        <string>{{ENGRAM_OWNER_PY}}</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ENGRAM_HOME</key>
        <string>{{ENGRAM_HOME}}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>{{ENGRAM_HOME}}/engramd-owner.log</string>

    <key>StandardErrorPath</key>
    <string>{{ENGRAM_HOME}}/engramd-owner.log</string>
</dict>
</plist>
"""

LAUNCHER_DARWIN_BASELINE = """\
if [ "$_os" = "Darwin" ]; then
    # ---- 4a. macOS: the launchd job com.engram.engramd-owner.<key> ----------------------
    # `launchctl print gui/<uid>/<label>` is asked and parsed by the Python I/O helper (it owns
    # the timeout: stock macOS has no GNU `timeout`). It prints one of
    #   running | registered:<state>:<last exit or none> | notfound | no_gui_domain | ambiguous:<why>
    LABEL="com.engram.engramd-owner.$_home_id"
    _state="$("$PYTHON_BIN" "$IO_PY" launchd_state "$LABEL" 2>/dev/null)"
    _sup="launchd: ${_state:-no answer}"
    case "$_state" in
        running)
            # #3849: up is not serving. Probe; report ONE line only for `notserving:` with no live
            # owner recorded. Serving / ambiguous / an owner that just wrote its pidfile: silent.
            if _supervised_not_serving; then
                _report "ENGRAM owner job $LABEL is running but no live owner is recorded in engramd.pid, and its socket did not serve within the probe window: ${_serving#notserving:}; no second owner was launched. It may still be starting; if this persists across session starts, run: launchctl kickstart -k gui/$(id -u)/$LABEL. ENGRAM_HOME=$ENGRAM_HOME"
            fi
            exit 0
            ;;
        registered:*)
            _rest="${_state#registered:}"
            _report "ENGRAM owner job $LABEL is registered but ${_rest%:*} (last exit ${_rest##*:}); leaving it to launchd, no second owner launched. ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
        no_gui_domain)
            # rc 125 from the domain probe: the domain is absent (no GUI login session), so there is
            # no launchd to supervise the owner and nothing to retry (a bootstrap into an absent domain
            # cannot work). A flag is ON, so its readers WILL call an owner: the same answer as the
            # Linux no-bus / WSL path -- fall through to the unsupervised launch (#3823). If that launch
            # fails, _launch_failed prints the named refusal instead of "not used".
            _nogui=1
            _sup="launchd: no GUI domain"
            ;;
        ambiguous:*)
            _report "ENGRAM owner not launched (launchd state is ambiguous: ${_state#ambiguous:}); a duplicate is worse than a missing owner. ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
        notfound) ;;
        *)
            _report "ENGRAM owner not launched (launchd state is ambiguous: unrecognized answer). ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
    esac
else
"""


def _plist_without_header_comment(text: str) -> str:
    """`text` minus its one header comment block. The comment is prose for a human; everything
    else is what launchd actually reads, and that is the surface C5 pins."""
    start = text.index("<!--")
    end = text.index("-->", start) + len("-->")
    return text[:start] + text[end:]


def test_c5_the_launchd_plist_template_is_unchanged_outside_its_header_comment():
    """#3831 is a Linux-only fix: the macOS owner artifact's BEHAVIOUR is untouched, asserted BY
    DIFF. The guarantee here is byte-identity OUTSIDE the header comment block -- every XML key,
    value and array is pinned against the base commit, while the comment is permitted to move.

    RE-BASELINED for #3831 (2026-10-16). The comment's "Unlike the systemd unit there is NO start
    limit under launchd" sentence was a CONTRAST with the unit #3831 changed, so the change made it
    false; it was rewritten to state the new relationship (launchd: no start limit, a fixed 60 s
    `ThrottleInterval`; systemd: no permanent stop either, now by bounded exponential backoff).
    That rewrite is the ONLY permitted delta: anything outside the comment block that moves fails
    this cell, so a later XML edit cannot ride in on a comment re-baseline."""
    actual = (TEMPLATES / "engramd-owner.plist.template").read_text(encoding="utf-8")
    assert _plist_without_header_comment(actual) == _plist_without_header_comment(PLIST_BASELINE), (
        "#3831 must not change anything the launchd plist template DOES -- only its header "
        "comment may move, and only to restate the restart-policy relationship. Re-baseline "
        "deliberately if a macOS behaviour change is intended.\n--- actual ---\n" + actual)


def test_c5_the_launchers_macos_branch_is_unchanged_byte_for_byte():
    """The launcher's Darwin branch, sliced between two stable markers, asserted BY DIFF: the
    `_unit_detail()` / backoff work is the Linux branch's, and must not have leaked across.

    RE-BASELINED 2026-10-16 (round 4) because a SIBLING PR legitimately moved the slice. #3826
    (`f36c75a0`, Clio, closed #3823) rewrote the Darwin `no_gui_domain` branch: the seat no longer
    exits 0 with an "owner is not used" line; it sets `_nogui=1` and falls through to the
    unsupervised launch, then refuses loudly if that launch fails. The slice grew 33 -> 35 lines.
    The baseline is a snapshot of a SIBLING artifact's content, so a legitimate sibling change
    makes it stale -- that is a false red, a defect in neither change. It is not a hole in this
    PR's own work: the slice is byte-identical on the base and on this branch.

    WHY THIS REFRESH IS LEGITIMATE, and not a guard being silenced. A refreshed baseline is the
    exact move that hides a real leak, so it is proved rather than asserted, and the proof is
    mechanical: the new baseline's bytes are the slice derived from the BASE branch's own current
    content, and this branch's diff touches NOTHING inside the sliced region --
      * the slice re-derived from `git show f36c75a0:<the hook>` and the slice re-derived from this
        branch's working tree are byte-identical (sha256
        34a9034f861ddcc1c15116f99fa597d28c2e08795a225493bae270366d383568, 1899 bytes, 35 lines);
      * no hunk of `git diff f36c75a0...HEAD -- <the hook>` falls inside the sliced region (base
        lines 235..269): every hunk except the first sits at old line >= 272, and the first is the
        header comment ABOVE the slice at old lines 24..28.
    So the property this cell guards -- the Linux work must not leak into the Darwin branch --
    still holds, unweakened: any future edit inside the slice still reddens here. Re-baseline
    deliberately if a macOS change is intended.

    RE-BASELINED for #3849 (2026-10-16). The change IS intended and macOS-side: the supervised
    Darwin `running)` arm used to `exit 0` unconditionally, so an owner job that was up but not
    serving (the residual case) was silent. It now probes the socket (`_supervised_not_serving`)
    and reports ONE line only for `notserving:` with no live owner recorded; serving, ambiguous,
    or an owner that just wrote its pidfile stay silent, and the arm still ends in `exit 0`
    (see the hook header).

    MECHANICAL PROOF that this is the ONLY delta (computed, not estimated). Slice re-derived with
    the same two markers this cell uses; OLD = the previous `LAUNCHER_DARWIN_BASELINE` (sha256
    34a9034f861ddcc1c15116f99fa597d28c2e08795a225493bae270366d383568, 1899 bytes, 35 lines), NEW =
    the slice of this branch's hook (re-derived after the round-1 hedge/positive-`none` fixes,
    which moved the `_report` line inside the `running)` arm). `diff -u OLD NEW`, computed against
    the pre-#3849 hook (`99b85494`), is ONE hunk (`@@ -7,7 +7,14 @@`): 1 line removed
    (`        running) exit 0 ;;`), 8 added, all inside the `running)` arm; every other
    arm (`registered:*`, `no_gui_domain`, `ambiguous:*`, `notfound`, `*`) and every line outside
    the `case` is byte-identical. New slice: sha256
    0fd398c5d7d4fc850133ada94904ef800e29b5bbd37176e269faca6b524bdb3a, 2536 bytes, 42 lines.
    Any edit to another arm still reddens here."""
    text = _require(HOOK).read_text(encoding="utf-8")
    start = text.index('if [ "$_os" = "Darwin" ]; then')
    end = text.index("    # ---- 4b. Linux: the systemd --user unit")
    actual = text[start:end]
    assert actual == LAUNCHER_DARWIN_BASELINE, (
        "the launcher's macOS branch must not change except as documented in this test's "
        "RE-BASELINED paragraphs.\n--- actual ---\n" + actual)
