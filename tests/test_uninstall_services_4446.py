"""#4446 step 2a: uninstall stops and removes THIS home's services and daemons.

Contract (the #4446 thread, Kepler's ruling + Halley's riders R1-R3, and
docs/internal/4446-install-artifacts.md section A, the legacy fixed-label units, and the
hook-started daemons of section D):

  * Ownership is the home: a unit/plist is removed only when its ENGRAM_HOME (launchctl print when
    loaded, else the plist, else the systemd unit's Environment=) normalises to the home being
    uninstalled. Another home's, or an unreadable one, is left and named `Left alone: <label> (<reason>)`.
  * Stop before delete, every unit (nothing stops itself once its tree is gone). macOS: bootout, then
    delete the plist. Linux: `disable --now`, delete the wants link and the unit, one daemon-reload.
  * Headless Mac (`launchctl print` fails: not loaded / "Domain does not support specified action"):
    a this-home plist is still deleted, the line says it was not loaded, not an error.
  * Hook-started daemons (recall-daemon.pid, engramd.pid): signalled only when
    recorded_live_pid(..., require_marker=True) passes; SIGTERM, bounded wait, never SIGKILL.
  * Viz units are not per-home: left unless --include-viz (A10 also needs its logs under this home);
    the .bak-pre-setup-* plist is always kept.
  * Every stop happens while the marketplace tree still exists; the "Nothing to uninstall" path
    handles services too; --dry-run changes nothing.

Safety (#3519): every cell runs with a FAKE HOME and recording stub `launchctl` / `systemctl` /
`uname` first on PATH; real processes are only the sleeping children this file starts, and teardown
kills every one of them.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
UNINSTALL_SH = SRC / "tools" / "uninstall-local-marketplace.sh"
HOME_KEY_SH = SRC / "tools" / "engramd-owner-home-key.sh"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"  # macOS: the stock 3.2

LEGACY_LABEL = "com.engram.surface-daemon"
LEGACY_UNIT = "engram-surface-daemon.service"
VIZ_LABEL = "com.engram.viz-server"
VIZ_UNITS = ("engram-viz-user.service", "engram-workbench-user.service")
RECALL_MARKER = "engram-surface-daemon"
OWNER_MARKER = "engramd_owner"


def _key(home: Path) -> str:
    r = subprocess.run([BASH, str(HOME_KEY_SH), str(home)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# --------------------------------------------------------------------------------------- the rig

_LAUNCHCTL_STUB = r"""#!/bin/bash
# recording launchctl stub (#4446 cells). State under STATE: loaded/<label> (content: its ENGRAM_HOME),
# nodomain (headless: every gui/ call fails), bootout_fails (bootout exits non-zero, job stays),
# print_blind (print exits non-zero although the job IS loaded; bootout still unloads it).
S="@STATE@"
H="@HOME@"
target="${2##*/}"
mkt=ABSENT; [ -d "@MARKETPLACE@" ] && mkt=present
plist=absent; [ -e "$H/Library/LaunchAgents/$target.plist" ] && plist=present
echo "$1 $2 | marketplace=$mkt plist=$plist" >> "$S/launchctl.log"
case "$1" in
  print)
    if [ -e "$S/nodomain" ]; then echo "Domain does not support specified action" >&2; exit 125; fi
    if [ -e "$S/print_blind" ]; then echo "print failed" >&2; exit 1; fi
    if [ -f "$S/loaded/$target" ]; then
      printf '%s = {\n\tenvironment = {\n\t\tENGRAM_HOME => %s\n\t}\n}\n' "$target" "$(cat "$S/loaded/$target")"
      exit 0
    fi
    echo "Could not find service \"$target\" in domain" >&2; exit 113 ;;
  bootout)
    if [ -e "$S/nodomain" ]; then echo "Domain does not support specified action" >&2; exit 125; fi
    if [ -e "$S/bootout_fails" ]; then echo "bootout failed" >&2; exit 5; fi
    rm -f "$S/loaded/$target"; exit 0 ;;
  *) exit 0 ;;
esac
"""

_SYSTEMCTL_STUB = r"""#!/bin/bash
# recording systemctl stub: logs argv plus the state of the unit file / wants link at call time.
S="@STATE@"
H="@HOME@"
u="${@: -1}"
mkt=ABSENT; [ -d "@MARKETPLACE@" ] && mkt=present
f=absent; [ -e "$H/.config/systemd/user/$u" ] && f=present
w=absent; { [ -e "$H/.config/systemd/user/default.target.wants/$u" ] || [ -L "$H/.config/systemd/user/default.target.wants/$u" ]; } && w=present
echo "$* | marketplace=$mkt unit=$f wants=$w" >> "$S/systemctl.log"
if [ -e "$S/disable_fails" ] && [ "$2" = "disable" ]; then exit 1; fi
exit 0
"""


class Rig:
    def __init__(self, tmp: Path, os_name: str):
        self.tmp = tmp
        self.os_name = os_name
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.pids: list[int] = []
        (self.state / "loaded").mkdir(parents=True)
        self.bin.mkdir()
        self.eh.mkdir(parents=True)
        (self.eh / "marketplace").mkdir()
        (self.eh / ".deployed-version").write_text("v0\n")
        (self.eh / "knowledge.db").write_bytes(b"graph-bytes")
        (self.eh / "config.json").write_text("{}\n")
        self.agents = self.home / "Library" / "LaunchAgents"
        self.units = self.home / ".config" / "systemd" / "user"
        self.wants = self.units / "default.target.wants"
        self.other_eh = tmp / "other" / ".engram"
        self.other_eh.mkdir(parents=True)
        self.key = _key(self.eh)
        subs = {"@STATE@": str(self.state), "@HOME@": str(self.home),
                "@MARKETPLACE@": str(self.eh / "marketplace")}
        for name, body in (("launchctl", _LAUNCHCTL_STUB), ("systemctl", _SYSTEMCTL_STUB)):
            for k, v in subs.items():
                body = body.replace(k, v)
            self._exe(name, body)
        self._exe("uname", f'#!/bin/bash\necho "{os_name}"\n')

    def _exe(self, name: str, body: str) -> None:
        p = self.bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    # ---- macOS fixtures
    def plist(self, label: str, eh: Path | str | None, logs: Path | None = None) -> Path:
        self.agents.mkdir(parents=True, exist_ok=True)
        env = ""
        if eh is not None:
            env = (f"<key>EnvironmentVariables</key>\n<dict>\n<key>ENGRAM_HOME</key>\n"
                   f"<string>{eh}</string>\n</dict>\n")
        log = ""
        if logs is not None:
            log = (f"<key>StandardOutPath</key><string>{logs}/viz-server.log</string>\n"
                   f"<key>StandardErrorPath</key><string>{logs}/viz-server.err</string>\n")
        p = self.agents / f"{label}.plist"
        p.write_text(f'<?xml version="1.0"?>\n<plist version="1.0">\n<dict>\n'
                     f"<key>Label</key><string>{label}</string>\n{env}{log}</dict>\n</plist>\n")
        return p

    def load(self, label: str, eh: Path | str) -> None:
        (self.state / "loaded" / label).write_text(str(eh))

    def headless(self) -> None:
        (self.state / "nodomain").write_text("")

    def surface_label(self) -> str:
        return f"com.engram.surface-daemon.{self.key}"

    def owner_label(self) -> str:
        return f"com.engram.engramd-owner.{self.key}"

    # ---- Linux fixtures
    def unit(self, name: str, eh: Path | str | None, exec_path: str | None = None, enable: bool = True) -> Path:
        self.units.mkdir(parents=True, exist_ok=True)
        eh_line = f"Environment=ENGRAM_HOME={eh}\n" if eh is not None else ""
        exe = exec_path or f"{self.eh}/marketplace/plugins/engram/hooks/engram-surface-daemon.py"
        p = self.units / name
        p.write_text(f"[Unit]\nDescription=x\n[Service]\nExecStart=/usr/bin/python3 {exe}\n{eh_line}"
                     "Restart=always\n[Install]\nWantedBy=default.target\n")
        if enable:
            self.wants.mkdir(parents=True, exist_ok=True)
            (self.wants / name).unlink(missing_ok=True)
            (self.wants / name).symlink_to(p)
        return p

    def surface_unit(self) -> str:
        return f"engram-surface-daemon-{self.key}.service"

    def owner_unit(self) -> str:
        return f"engramd-owner-{self.key}.service"

    # ---- daemons (real children; reparented to init so a SIGTERM leaves no zombie of ours)
    def start(self, script_name: str, ignore_term: bool = False) -> int:
        d = self.tmp / "procs"
        d.mkdir(exist_ok=True)
        script = d / script_name
        body = "import signal, time\n"
        if ignore_term:
            body += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        body += "time.sleep(600)\n"
        script.write_text(body)
        r = subprocess.run(["sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", sys.executable, str(script)],
                           capture_output=True, text=True, timeout=20)
        pid = int(r.stdout.strip())
        self.pids.append(pid)
        deadline = time.time() + 5
        while time.time() < deadline and script_name not in self.cmdline(pid):
            time.sleep(0.05)
        assert script_name in self.cmdline(pid), "child never exec'd"
        return pid

    @staticmethod
    def cmdline(pid: int) -> str:
        # -ww: procps cuts `command=` to an exported $COLUMNS (xdist exports 80) when stdout is a pipe,
        # which drops the marker off a long tmp path and makes the "marker absent" checks pass for free.
        return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(r) and not r.startswith("Z")

    def pidfile(self, name: str, pid: int, future: bool = False) -> Path:
        p = self.eh / name
        p.write_text(f"{pid}\n")
        if future:  # the process then predates the pidfile: only the marker can tell it is not ours
            t = time.time() + 120
            os.utime(p, (t, t))
        return p

    def teardown(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)  # test cleanup of OUR OWN children only
            except OSError:
                pass

    # ---- running
    def run(self, *args: str, term_wait: str = "3", extra_env: dict | None = None):
        env = {
            "HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
            "CLAUDE_HOME": str(self.home / ".claude"), "CODEX_HOME": str(self.home / ".codex"),
            "PATH": f"{self.bin}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "ENGRAM_UNINSTALL_TERM_WAIT_S": term_wait,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run([BASH, str(UNINSTALL_SH), "--skip-cli", *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def log(self, tool: str) -> list[str]:
        p = self.state / f"{tool}.log"
        return p.read_text().splitlines() if p.exists() else []

    def mutating(self, tool: str) -> list[str]:
        if tool == "launchctl":
            return [ln for ln in self.log(tool) if not ln.startswith(("print ", "list "))]
        return self.log(tool)


@pytest.fixture
def mac(tmp_path):
    r = Rig(tmp_path, "Darwin")
    yield r
    r.teardown()


@pytest.fixture
def linux(tmp_path):
    r = Rig(tmp_path, "Linux")
    yield r
    r.teardown()


def _ok(r):
    assert r.returncode == 0, f"rc={r.returncode}\n--stdout--\n{r.stdout}\n--stderr--\n{r.stderr}"
    return r.stdout


def _lines(out: str, *needles: str) -> list[str]:
    return [ln for ln in out.splitlines() if all(n in ln for n in needles)]


# ------------------------------------------------------------------------------------- macOS

def test_mac_surface_plist_loaded_this_home_is_booted_out_then_deleted(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)
    mac.load(label, mac.eh)
    out = _ok(mac.run())
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 1 and f"gui/{os.getuid()}/{label}" in boots[0], mac.log("launchctl")
    assert "plist=present" in boots[0], "the plist must still exist at the moment of bootout"
    assert not plist.exists()
    assert not (mac.state / "loaded" / label).exists()
    assert _lines(out, label)


def test_mac_surface_plist_present_not_loaded_headless_is_deleted_without_failure(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)
    mac.headless()  # `launchctl print gui/<uid>/...` answers "Domain does not support specified action"
    out = _ok(mac.run())
    assert not plist.exists()
    assert _lines(out, label, "not loaded"), out
    assert "ERROR" not in out and "WARN" not in out, out


def test_mac_surface_plist_present_not_loaded_plain_is_deleted(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)  # gui domain fine, label simply not loaded
    out = _ok(mac.run())
    assert not plist.exists()
    assert _lines(out, label, "not loaded"), out
    # a failed `print` does not prove "not loaded", so a harmless bootout still comes before the delete
    assert all("plist=present" in ln for ln in mac.log("launchctl") if ln.startswith("bootout "))


def test_mac_owner_plist_loaded_this_home_is_booted_out_then_deleted(mac):
    label = mac.owner_label()
    plist = mac.plist(label, mac.eh)
    mac.load(label, mac.eh)
    _ok(mac.run())
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 1 and label in boots[0] and "plist=present" in boots[0], mac.log("launchctl")
    assert not plist.exists()


def test_mac_keyed_label_of_another_home_is_left_alone_and_named(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.other_eh)  # this home's label name, but the job belongs elsewhere
    mac.load(label, mac.other_eh)
    out = _ok(mac.run())
    assert plist.exists()
    assert (mac.state / "loaded" / label).exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", label), out


def test_mac_a_second_homes_own_keyed_plist_survives_and_is_named(mac):
    other_label = f"com.engram.surface-daemon.{_key(mac.other_eh)}"
    other_plist = mac.plist(other_label, mac.other_eh)
    mac.load(other_label, mac.other_eh)
    mine = mac.plist(mac.surface_label(), mac.eh)
    out = _ok(mac.run())
    assert not mine.exists()
    assert other_plist.exists() and (mac.state / "loaded" / other_label).exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ") and other_label in ln]
    assert _lines(out, "Left alone:", other_label), out


def test_mac_legacy_fixed_label_this_home_is_removed(mac):
    plist = mac.plist(LEGACY_LABEL, mac.eh)
    mac.load(LEGACY_LABEL, mac.eh)
    _ok(mac.run())
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 1 and LEGACY_LABEL in boots[0] and "plist=present" in boots[0]
    assert not plist.exists()


def test_mac_legacy_fixed_label_loaded_without_plist_is_booted_out(mac):
    mac.load(LEGACY_LABEL, mac.eh)  # loaded job, plist already gone: launchctl print still names its home
    _ok(mac.run())
    assert [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]


def test_mac_legacy_fixed_label_of_another_home_is_left_alone(mac):
    plist = mac.plist(LEGACY_LABEL, mac.other_eh)
    mac.load(LEGACY_LABEL, mac.other_eh)
    out = _ok(mac.run())
    assert plist.exists() and (mac.state / "loaded" / LEGACY_LABEL).exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", LEGACY_LABEL), out


def test_mac_legacy_fixed_label_with_unreadable_home_is_left_alone(mac):
    plist = mac.plist(LEGACY_LABEL, None)  # no ENGRAM_HOME anywhere
    out = _ok(mac.run())
    assert plist.exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", LEGACY_LABEL), out


def test_mac_failed_bootout_keeps_the_plist_and_says_so(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)
    mac.load(label, mac.eh)
    (mac.state / "bootout_fails").write_text("")
    out = _ok(mac.run())
    assert plist.exists(), "a job that did not stop must keep its plist"
    assert _lines(out, label, "Still loaded") or _lines(out, label, "could not"), out


# ------------------------------------------------------------------------------------- Linux

def test_linux_surface_unit_disabled_before_delete_wants_link_gone_one_reload(linux):
    name = linux.surface_unit()
    unit = linux.unit(name, linux.eh)
    out = _ok(linux.run())
    log = linux.log("systemctl")
    dis = [ln for ln in log if ln.startswith("--user disable --now")]
    assert len(dis) == 1 and name in dis[0], log
    assert "unit=present" in dis[0] and "wants=present" in dis[0], "stop must precede every delete"
    assert not unit.exists()
    assert not (linux.wants / name).exists() and not (linux.wants / name).is_symlink()
    assert len([ln for ln in log if "daemon-reload" in ln]) == 1, log
    assert log[-1].startswith("--user daemon-reload"), log
    assert _lines(out, name)


def test_linux_owner_unit_disabled_before_delete_wants_link_gone_one_reload(linux):
    name = linux.owner_unit()
    unit = linux.unit(name, linux.eh)
    _ok(linux.run())
    log = linux.log("systemctl")
    dis = [ln for ln in log if ln.startswith("--user disable --now")]
    assert len(dis) == 1 and name in dis[0] and "unit=present" in dis[0] and "wants=present" in dis[0], log
    assert not unit.exists() and not (linux.wants / name).is_symlink()
    assert len([ln for ln in log if "daemon-reload" in ln]) == 1, log


def test_linux_two_units_still_one_reload(linux):
    linux.unit(linux.surface_unit(), linux.eh)
    linux.unit(linux.owner_unit(), linux.eh)
    _ok(linux.run())
    log = linux.log("systemctl")
    assert len([ln for ln in log if "disable --now" in ln]) == 2, log
    assert len([ln for ln in log if "daemon-reload" in ln]) == 1, log


def test_linux_unit_pointing_at_a_source_checkout_of_this_home_is_removed(linux):
    name = linux.surface_unit()
    unit = linux.unit(name, linux.eh, exec_path="/home/dev/checkout/src/engram/hooks/engram-surface-daemon.py")
    _ok(linux.run())
    assert not unit.exists()
    assert [ln for ln in linux.log("systemctl") if "disable --now" in ln and name in ln]


def test_linux_keyed_unit_of_another_home_is_left_alone_and_named(linux):
    name = linux.surface_unit()
    unit = linux.unit(name, linux.other_eh)
    out = _ok(linux.run())
    assert unit.exists() and (linux.wants / name).is_symlink()
    assert not linux.log("systemctl")
    assert _lines(out, "Left alone:", name), out


def test_linux_a_second_homes_own_keyed_unit_survives_and_is_named(linux):
    other = f"engramd-owner-{_key(linux.other_eh)}.service"
    other_unit = linux.unit(other, linux.other_eh)
    out = _ok(linux.run())
    assert other_unit.exists() and (linux.wants / other).is_symlink()
    assert not linux.log("systemctl")
    assert _lines(out, "Left alone:", other), out


def test_linux_legacy_fixed_unit_this_home_removed_other_home_left_unreadable_left(linux):
    mine = linux.unit(LEGACY_UNIT, linux.eh)
    out = _ok(linux.run())
    assert not mine.exists()
    assert [ln for ln in linux.log("systemctl") if "disable --now" in ln and LEGACY_UNIT in ln]

    theirs = linux.unit(LEGACY_UNIT, linux.other_eh)
    n = len(linux.log("systemctl"))
    out = _ok(linux.run())
    assert theirs.exists() and len(linux.log("systemctl")) == n
    assert _lines(out, "Left alone:", LEGACY_UNIT), out

    theirs.unlink()
    unreadable = linux.unit(LEGACY_UNIT, None)
    out = _ok(linux.run())
    assert unreadable.exists() and len(linux.log("systemctl")) == n
    assert _lines(out, "Left alone:", LEGACY_UNIT), out


def test_linux_failed_disable_keeps_the_unit_and_says_so(linux):
    name = linux.surface_unit()
    unit = linux.unit(name, linux.eh)
    (linux.state / "disable_fails").write_text("")
    out = _ok(linux.run())
    assert unit.exists()
    assert _lines(out, name, "could not"), out


# ------------------------------------------------------------------------- viz and the .bak

def test_linux_viz_units_left_and_named_without_the_flag(linux):
    paths = [linux.unit(n, None) for n in VIZ_UNITS]
    out = _ok(linux.run())
    for n, p in zip(VIZ_UNITS, paths):
        assert p.exists() and (linux.wants / n).is_symlink()
        assert _lines(out, "Left alone:", n, "shared by every ENGRAM home on this machine", "--include-viz"), out
    assert not linux.log("systemctl")


def test_linux_viz_units_removed_with_include_viz(linux):
    paths = [linux.unit(n, None) for n in VIZ_UNITS]
    _ok(linux.run("--include-viz"))
    log = linux.log("systemctl")
    for n, p in zip(VIZ_UNITS, paths):
        assert not p.exists() and not (linux.wants / n).is_symlink()
        dis = [ln for ln in log if "disable --now" in ln and n in ln]
        assert len(dis) == 1 and "unit=present" in dis[0] and "wants=present" in dis[0], log
    assert len([ln for ln in log if "daemon-reload" in ln]) == 1


def test_mac_viz_plist_left_without_flag_removed_with_flag_when_logs_are_in_this_home(mac):
    plist = mac.plist(VIZ_LABEL, None, logs=mac.eh)
    mac.load(VIZ_LABEL, "-")
    out = _ok(mac.run())
    assert plist.exists() and _lines(out, "Left alone:", VIZ_LABEL, "shared by every ENGRAM home on this machine", "--include-viz"), out
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    _ok(mac.run("--include-viz"))
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 1 and VIZ_LABEL in boots[0] and "plist=present" in boots[0]
    assert not plist.exists()


def test_mac_viz_plist_with_logs_elsewhere_is_left_even_with_the_flag(mac):
    plist = mac.plist(VIZ_LABEL, None, logs=mac.other_eh)
    mac.load(VIZ_LABEL, "-")
    out = _ok(mac.run("--include-viz"))
    assert plist.exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", VIZ_LABEL), out


def test_mac_bak_pre_setup_plist_is_always_kept_and_named(mac):
    mac.plist(VIZ_LABEL, None, logs=mac.eh)
    bak = mac.agents / (VIZ_LABEL + ".plist.bak-pre-setup-20261107T000000Z")  # operator-setup-viz.sh's name
    bak.write_text("<plist/>\n")
    for args in ((), ("--include-viz",)):
        out = _ok(mac.run(*args))
        assert bak.exists()
        assert _lines(out, "bak-pre-setup"), out


# ------------------------------------------------------------------- hook-started daemons (D)

def test_live_marker_daemon_in_recall_daemon_pid_is_sigtermed_and_exits(mac):
    pid = mac.start("engram-surface-daemon.py")
    mac.pidfile("recall-daemon.pid", pid)
    out = _ok(mac.run())
    deadline = time.time() + 5
    while time.time() < deadline and mac.alive(pid):
        time.sleep(0.05)
    assert not mac.alive(pid), out
    assert _lines(out, f"pid {pid}"), out


def test_live_marker_daemon_in_engramd_pid_is_sigtermed_and_exits(mac):
    pid = mac.start("engramd_owner.py")
    mac.pidfile("engramd.pid", pid)
    out = _ok(mac.run())
    deadline = time.time() + 5
    while time.time() < deadline and mac.alive(pid):
        time.sleep(0.05)
    assert not mac.alive(pid), out
    assert _lines(out, f"pid {pid}"), out


def test_decoy_live_pid_without_the_marker_is_not_signalled_and_is_reported(mac):
    pid = mac.start("unrelated_sleeper.py")
    assert RECALL_MARKER not in mac.cmdline(pid) and OWNER_MARKER not in mac.cmdline(pid)
    mac.pidfile("recall-daemon.pid", pid, future=True)
    mac.pidfile("engramd.pid", pid, future=True)
    out = _ok(mac.run())
    time.sleep(0.5)
    assert mac.alive(pid), "a pid that fails require_marker=True must never be signalled"
    assert _lines(out, "Not signalled", "recall-daemon.pid"), out
    assert _lines(out, "Not signalled", "engramd.pid"), out


def test_dead_pid_is_not_signalled_and_is_no_error(mac):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    mac.pidfile("recall-daemon.pid", p.pid)
    out = _ok(mac.run())
    assert _lines(out, "Not signalled", "recall-daemon.pid", "dead"), out
    assert "Traceback" not in out and "ERROR" not in out


def test_garbage_pidfile_is_not_signalled_and_is_no_error(mac):
    (mac.eh / "engramd.pid").write_text("not-a-pid\n")
    out = _ok(mac.run())
    assert _lines(out, "Not signalled", "engramd.pid", "garbage"), out
    assert "Traceback" not in out


def test_daemon_that_ignores_sigterm_is_reported_still_running_and_never_killed(mac):
    pid = mac.start("engram-surface-daemon.py", ignore_term=True)
    mac.pidfile("recall-daemon.pid", pid)
    t0 = time.time()
    out = _ok(mac.run(term_wait="1"))
    assert time.time() - t0 < 30
    assert mac.alive(pid), "uninstall must never SIGKILL"
    assert _lines(out, "Still running", f"pid {pid}"), out


def test_the_venv_python_is_preferred_when_present(mac):
    venv_bin = mac.home / ".engram" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = mac.tmp / "venv-python-used"
    py = venv_bin / "python3"
    py.write_text(f'#!/bin/sh\necho used >> "{marker}"\nexec "{sys.executable}" "$@"\n')
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    _ok(mac.run())
    assert marker.exists()


# ---------------------------------------------------------------------- order and the early path

def test_every_stop_happens_while_the_marketplace_tree_still_exists(mac, tmp_path):
    mac.plist(mac.surface_label(), mac.eh)
    mac.load(mac.surface_label(), mac.eh)
    mac.plist(mac.owner_label(), mac.eh)
    mac.load(mac.owner_label(), mac.eh)
    _ok(mac.run())
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 2
    assert all("marketplace=present" in ln for ln in boots), boots
    assert not (mac.eh / "marketplace").exists(), "the tree is still removed afterwards"


def test_linux_stops_happen_while_the_marketplace_tree_still_exists(linux):
    linux.unit(linux.surface_unit(), linux.eh)
    _ok(linux.run())
    dis = [ln for ln in linux.log("systemctl") if "disable --now" in ln]
    assert dis and all("marketplace=present" in ln for ln in dis), dis


def test_nothing_to_uninstall_path_still_stops_and_removes_services(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)
    mac.load(label, mac.eh)
    pid = mac.start("engram-surface-daemon.py")
    mac.pidfile("recall-daemon.pid", pid)
    import shutil
    shutil.rmtree(mac.eh / "marketplace")
    (mac.eh / ".deployed-version").unlink()
    out = _ok(mac.run())
    assert "Nothing to uninstall" in out
    assert not plist.exists()
    assert [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    deadline = time.time() + 5
    while time.time() < deadline and mac.alive(pid):
        time.sleep(0.05)
    assert not mac.alive(pid)


def test_linux_nothing_to_uninstall_path_still_stops_and_removes_services(linux):
    unit = linux.unit(linux.surface_unit(), linux.eh)
    import shutil
    shutil.rmtree(linux.eh / "marketplace")
    (linux.eh / ".deployed-version").unlink()
    out = _ok(linux.run())
    assert "Nothing to uninstall" in out
    assert not unit.exists()
    assert [ln for ln in linux.log("systemctl") if "disable --now" in ln]


# ---------------------------------------------------------------------------------- dry run

def test_dry_run_mac_prints_every_action_and_changes_nothing(mac):
    plists = [mac.plist(mac.surface_label(), mac.eh), mac.plist(mac.owner_label(), mac.eh),
              mac.plist(LEGACY_LABEL, mac.eh), mac.plist(VIZ_LABEL, None, logs=mac.eh)]
    for p in plists:
        mac.load(p.stem, mac.eh)
    headless_free = mac.plist("com.engram.surface-daemon.deadbeef", mac.eh)
    pid = mac.start("engram-surface-daemon.py")
    mac.pidfile("recall-daemon.pid", pid)
    out = _ok(mac.run("--dry-run", "--include-viz"))
    assert not mac.mutating("launchctl"), mac.log("launchctl")
    assert all(p.exists() for p in plists) and headless_free.exists()
    assert mac.alive(pid), "dry-run sends no signal"
    for p in plists:
        assert _lines(out, p.stem, "dry-run"), (p.stem, out)
    assert _lines(out, f"pid {pid}", "dry-run"), out
    assert (mac.eh / "marketplace").is_dir() and (mac.eh / ".deployed-version").exists()
    assert (mac.eh / "knowledge.db").read_bytes() == b"graph-bytes"


def test_dry_run_linux_prints_every_action_and_changes_nothing(linux):
    units = [linux.unit(linux.surface_unit(), linux.eh), linux.unit(linux.owner_unit(), linux.eh),
             linux.unit(LEGACY_UNIT, linux.eh), linux.unit(VIZ_UNITS[0], None)]
    out = _ok(linux.run("--dry-run", "--include-viz"))
    assert not linux.log("systemctl"), linux.log("systemctl")
    assert all(u.exists() for u in units)
    assert all((linux.wants / u.name).is_symlink() for u in units)
    for u in units:
        assert _lines(out, u.name, "dry-run"), (u.name, out)


# ------------------------------------------------------------------------- data is untouched

def test_data_files_untouched_and_summary_says_what_happened(mac):
    mac.plist(mac.surface_label(), mac.eh)
    mac.plist(LEGACY_LABEL, mac.other_eh)
    out = _ok(mac.run())
    assert (mac.eh / "knowledge.db").read_bytes() == b"graph-bytes"
    assert (mac.eh / "config.json").read_text() == "{}\n"
    assert _lines(out, "Left alone:", LEGACY_LABEL)
    assert "Services and daemons" in out, out


# ---- round 2 (#4446 step 2a): print fails although the job is loaded; ownership discrimination

def test_mac_print_fails_but_job_is_loaded_still_booted_out_before_the_plist_goes(mac):
    label = mac.surface_label()
    plist = mac.plist(label, mac.eh)
    mac.load(label, mac.eh)
    (mac.state / "print_blind").write_text("")  # `launchctl print` errors, yet the job is running
    _ok(mac.run())
    boots = [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert len(boots) == 1 and label in boots[0], mac.log("launchctl")
    assert "plist=present" in boots[0], "bootout must come before the plist is deleted"
    assert not (mac.state / "loaded" / label).exists(), "the job must have been unloaded"
    assert not plist.exists()


def test_mac_prefix_sibling_home_is_not_this_home(mac):
    sib = Path(str(mac.eh) + "-work")  # shares this home's string as a prefix, is another home
    sib.mkdir()
    label = mac.surface_label()
    plist = mac.plist(label, sib)
    mac.load(label, sib)
    out = _ok(mac.run())
    assert plist.exists() and (mac.state / "loaded" / label).exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", label), out


def test_linux_prefix_sibling_home_is_not_this_home(linux):
    sib = Path(str(linux.eh) + "-work")
    sib.mkdir()
    unit = linux.unit(linux.surface_unit(), sib)
    out = _ok(linux.run())
    assert unit.exists()
    assert not [ln for ln in linux.log("systemctl") if "disable" in ln]
    assert _lines(out, "Left alone:", linux.surface_unit()), out


def test_mac_viz_logs_under_a_prefix_sibling_dir_are_left_even_with_the_flag(mac):
    sib = Path(str(mac.eh) + "-work")
    sib.mkdir()
    plist = mac.plist(VIZ_LABEL, None, logs=sib)
    mac.load(VIZ_LABEL, "-")
    out = _ok(mac.run("--include-viz"))
    assert plist.exists()
    assert not [ln for ln in mac.log("launchctl") if ln.startswith("bootout ")]
    assert _lines(out, "Left alone:", VIZ_LABEL), out


def test_linux_unit_recording_a_symlink_alias_of_this_home_is_removed(linux):
    alias = linux.tmp / "alias-home"
    alias.symlink_to(linux.eh)
    name = linux.surface_unit()
    unit = linux.unit(name, alias)
    _ok(linux.run())
    assert not unit.exists()
    assert len([ln for ln in linux.log("systemctl") if "disable --now" in ln and name in ln]) == 1


def test_linux_unit_recording_a_symlink_to_another_home_is_left(linux):
    alias = linux.tmp / "alias-other"
    alias.symlink_to(linux.other_eh)
    name = linux.surface_unit()
    unit = linux.unit(name, alias)
    out = _ok(linux.run())
    assert unit.exists()
    assert not [ln for ln in linux.log("systemctl") if "disable" in ln]
    assert _lines(out, "Left alone:", name), out
