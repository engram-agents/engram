"""#4305 + #2702: the surface daemon's supervisor identity is keyed on ENGRAM_HOME.

Linux unit  engram-surface-daemon-<key>.service      (was the fixed engram-surface-daemon.service)
Darwin label com.engram.surface-daemon.<key>         (was the fixed com.engram.surface-daemon)

<key> comes from the ONE helper, tools/engramd-owner-home-key.sh (8 hex over the physical home).
Contract-first: these cells were written before the implementation (Kepler with Borges' ruling,
#4305; Borges' #2702 ruling and #4305 stop-condition design).

Cells (names in the test ids)
  L1-L6   Linux, from #2702.   D1-D9  their Darwin mirrors.   P1 keyed-first precedence.   H1 no key.
  S-c     pure render: the stop condition for a deleted home is in the unit / plist, with THIS home.
  S-a/S-b real service manager (skip unless ENGRAM_REAL_SERVICE_MANAGER_4305=1; run by the parent).
  G1      no supervisor-identity literal left outside the named legacy-transition code.

Hermetic by construction (#3519): every cell that reaches a service manager runs the script with an
explicit PATH whose first entries are OUR fakes (`systemctl`, `launchctl`, `uname`, `timeout`); the
fakes keep state in a per-cell directory and record every call.  The Darwin print output is built from
the REAL capture tests/fixtures/darwin_check/launchctl_print_surface_daemon_running.txt (label and
home substituted); a capture of a KEYED job does not exist, so that part is derived, not measured.
The systemd `Environment=` line format of `systemctl show` is likewise taken from systemd.exec(5)
(space-separated assignments), not from a capture.  An autouse guard diffs the REAL registrations
around every cell.  S-a/S-b are the ONLY cells that touch a real manager and are off by default.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# #4029: in-process loads of src/engram/tools modules run inside this window (sys.path restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
TOOLS = SRC / "tools"
SETUP = TOOLS / "setup-surface-daemon-service.sh"
HOME_KEY = TOOLS / "engramd-owner-home-key.sh"
HOOK = SRC / "hooks" / "claude" / "start-engram-daemon.sh"
TEMPLATES = SRC / "templates"
CAPTURE = REPO / "tests" / "fixtures" / "darwin_check" / "launchctl_print_surface_daemon_running.txt"

LEGACY_UNIT = "engram-surface-daemon.service"
LEGACY_LABEL = "com.engram.surface-daemon"
UID = os.getuid()
IS_DARWIN = sys.platform == "darwin"
TINY = {"ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01", "ENGRAM_SETUP_LAUNCHD_RETRY_S": "0.01",
        "ENGRAM_SETUP_LAUNCHD_WAIT_S": "1"}


def unit_for(key: str) -> str:
    return f"engram-surface-daemon-{key}.service"


def label_for(key: str) -> str:
    return f"{LEGACY_LABEL}.{key}"


# ---------------------------------------------------------------------------
# #3519: nothing here may register a surface-daemon job (either shape) with the REAL manager
# ---------------------------------------------------------------------------

def _real_surface_names() -> set:
    names: set = set()
    if IS_DARWIN and os.path.exists("/bin/launchctl"):
        r = subprocess.run(["/bin/launchctl", "list"], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=20)
        names |= {ln.split()[-1] for ln in r.stdout.splitlines()
                  if ln.split() and ln.split()[-1].startswith(LEGACY_LABEL)}
    elif os.path.exists("/usr/bin/systemctl"):
        try:
            r = subprocess.run(["/usr/bin/systemctl", "--user", "list-unit-files", "--no-legend",
                                "engram-surface-daemon*"], capture_output=True, encoding="utf-8",
                               errors="replace", timeout=20)
            names |= {ln.split()[0] for ln in r.stdout.splitlines() if ln.split()}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return names


@pytest.fixture(autouse=True)
def _no_real_registration_leaks():
    before = _real_surface_names()
    yield
    leaked = _real_surface_names() - before
    # S-a / S-b register (and remove) a keyed job for a TEMP home on purpose; they clean up.
    if os.environ.get("ENGRAM_REAL_SERVICE_MANAGER_4305") != "1":
        assert not leaked, f"a cell registered surface-daemon job(s) with the REAL manager: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def key_of(home: Path) -> str:
    r = subprocess.run(["bash", str(HOME_KEY), str(home)], capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def physical_key(home: Path) -> str:
    """An independent derivation (hashlib) to pin that the helper is what everything uses."""
    return hashlib.sha256(os.path.realpath(str(home)).encode()).hexdigest()[:8]


def job_text(label: str, home: str, state: str = "running", drop_home: bool = False) -> str:
    """`launchctl print` text of a job: the REAL capture with the label and the home substituted."""
    t = CAPTURE.read_text()
    t = t.replace(LEGACY_LABEL, label).replace("\n\tstate = running\n", f"\n\tstate = {state}\n", 1)
    line = "\t\tENGRAM_HOME => /Users/seat/.engram\n"
    assert line in t
    return t.replace(line, "" if drop_home else f"\t\tENGRAM_HOME => {home}\n", 1)


_SYSTEMCTL = r"""#!/bin/bash
S="$FAKE_STATE"; echo "$*" >> "$S/systemctl.calls"
args=("$@"); n=${#args[@]}; i=0; sub=""; unit=""; props=""
while [ $i -lt $n ]; do
  a="${args[$i]}"
  case "$a" in
    --user|--now|--no-pager) ;;
    -p) i=$((i+1)); props="${args[$i]}" ;;
    *) if [ -z "$sub" ]; then sub="$a"; elif [ -z "$unit" ]; then unit="$a"; fi ;;
  esac
  i=$((i+1))
done
f="$S/units/$unit"
case "$sub" in
  show)
    if [ -f "$f" ] && grep -q '^showrc=1' "$f"; then echo "Failed to connect to bus: No medium found" >&2; exit 1; fi
    if [ -f "$f" ]; then
      load=loaded; active="$(sed -n 's/^active=//p' "$f")"; home="$(sed -n 's/^home=//p' "$f")"
      if grep -q '^envmode=empty' "$f"; then home=""; fi
    else load=not-found; active=inactive; home=""; fi
    IFS=,; for p in $props; do case "$p" in
      LoadState) echo "LoadState=$load" ;;
      ActiveState) echo "ActiveState=$active" ;;
      SubState) echo "SubState=running" ;;
      Environment) if [ -n "$home" ]; then echo "Environment=ENGRAM_HOME=$home ENGRAM_DAEMON_IDLE_TIMEOUT=0"; else echo "Environment="; fi ;;
    esac; done ;;
  enable)
    mkdir -p "$S/units"
    h="$(sed -n 's/^Environment=ENGRAM_HOME=//p' "$HOME/.config/systemd/user/$unit" | head -n 1)"
    printf 'home=%s\nactive=active\n' "$h" > "$f" ;;
  disable) rm -f "$f" ;;
  is-active) echo active ;;
  *) ;;
esac
exit 0
"""

_LAUNCHCTL = r"""#!/bin/bash
S="$FAKE_STATE"; echo "$*" >> "$S/launchctl.calls"
case "$1" in
  print)
    label="${2##*/}"; dom="${2%%/*}"
    if [ -f "$S/nogui" ] && [ "$dom" = gui ]; then echo "Bad request." >&2; exit 125; fi
    if [ -f "$S/jobs/$label" ]; then cat "$S/jobs/$label"; exit 0; fi
    echo "Could not find service \"$label\" in domain for user gui: $(id -u)" >&2; exit 113 ;;
  bootout)
    label="${2##*/}"
    if [ -f "$S/jobs/$label" ]; then rm -f "$S/jobs/$label"; exit 0; fi
    echo "Boot-out failed: 3: No such process" >&2; exit 3 ;;
  bootstrap)
    label="$(basename "$3" .plist)"
    home="$(grep -A1 '<key>ENGRAM_HOME</key>' "$3" | sed -n 's|.*<string>\(.*\)</string>.*|\1|p' | head -n 1)"
    mkdir -p "$S/jobs"
    sed -e "s|__LABEL__|$label|g" -e "s|__HOME__|$home|g" "$S/job.tpl" > "$S/jobs/$label"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

_REAL_TOOLS = ("bash", "sed", "grep", "cat", "head", "cut", "tr", "dirname", "basename", "readlink", "mkdir",
               "rm", "mktemp", "date", "kill", "sleep", "id", "sha256sum", "shasum", "pgrep", "ls", "cp",
               "touch", "wc", "awk", "env", "uname_real_unused")


class Box:
    """One cell's world: a tmp plugin tree (hook, setup script, helper, templates, stub daemon), a tmp
    HOME, a state dir for the fakes, and a bin dir that is the FIRST entry of a hermetic PATH."""

    def __init__(self, tmp: Path, system: str):
        self.tmp, self.system = tmp, system
        self.home = tmp / "home"          # $HOME
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.plugin = tmp / "plugin"
        for d in (self.home, self.state / "units", self.state / "jobs", self.bin,
                  self.plugin / "tools", self.plugin / "templates", self.plugin / "hooks"):
            d.mkdir(parents=True)
        (self.state / "job.tpl").write_text(job_text("__LABEL__", "__HOME__"))
        for name, body in (("systemctl", _SYSTEMCTL), ("launchctl", _LAUNCHCTL)):
            self._exe(self.bin / name, body)
        self._exe(self.bin / "uname", f"#!/bin/bash\necho {system}\n")
        self._exe(self.bin / "timeout", '#!/bin/bash\nshift\nexec "$@"\n')   # macOS has none
        for t in _REAL_TOOLS:
            found = shutil.which(t, path="/usr/bin:/bin:/usr/local/bin")
            if found and not (self.bin / t).exists():
                (self.bin / t).symlink_to(found)
        # plugin tree, deployed (flattened) layout
        (self.plugin / "engram_core.py").write_text("# tree root marker\n")
        shutil.copy(HOME_KEY, self.plugin / "tools" / HOME_KEY.name)
        shutil.copy(SETUP, self.plugin / "tools" / SETUP.name)
        for f in TEMPLATES.iterdir():
            if f.is_file():
                shutil.copy(f, self.plugin / "templates" / f.name)
        (self.plugin / "hooks" / "engram-surface-daemon.py").write_text(
            "import sys\nprint('stub daemon started', file=sys.stderr)\n")
        self.hook = self.plugin / "hooks" / "start-engram-daemon.sh"
        self.hook.write_text(HOOK.read_text().replace("{{PYTHON}}", sys.executable))
        # the python the setup script prefers: $HOME/.engram/venv/bin/python3
        venv = self.home / ".engram" / "venv" / "bin"
        venv.mkdir(parents=True)
        self._exe(venv / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')

    @staticmethod
    def _exe(p: Path, body: str) -> Path:
        p.write_text(body)
        p.chmod(0o755)
        return p

    # ---- homes -----------------------------------------------------------------
    def new_home(self, name: str) -> Path:
        h = self.tmp / name
        h.mkdir(parents=True)
        return h

    # ---- fake registrations ------------------------------------------------------
    def unit(self, name: str, home: Path | str | None, active: str = "active", envmode: str = "ok",
             showrc: int = 0):
        (self.state / "units" / name).write_text(
            f"home={home or ''}\nactive={active}\nenvmode={envmode}\nshowrc={showrc}\n")

    def job(self, label: str, home: Path | str, state: str = "running", drop_home: bool = False):
        (self.state / "jobs" / label).write_text(job_text(label, str(home), state, drop_home))

    def unit_registered(self, name: str) -> bool:
        return (self.state / "units" / name).exists()

    def job_registered(self, label: str) -> bool:
        return (self.state / "jobs" / label).exists()

    # ---- calls ---------------------------------------------------------------------
    def calls(self, tool: str) -> list:
        p = self.state / f"{tool}.calls"
        return p.read_text().splitlines() if p.exists() else []

    # ---- running ---------------------------------------------------------------------
    def env(self, engram_home: Path | None, extra: dict | None = None) -> dict:
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "FAKE_STATE": str(self.state),
             "XDG_RUNTIME_DIR": str(self.tmp), "USER": "tester", "TMPDIR": str(self.tmp / "t")}
        (self.tmp / "t").mkdir(exist_ok=True)
        if engram_home is not None:
            e["ENGRAM_HOME"] = str(engram_home)
        e.update(TINY)
        e.update(extra or {})
        return e

    def setup(self, engram_home: Path | None, *args: str, extra: dict | None = None):
        return subprocess.run(["bash", str(self.plugin / "tools" / SETUP.name), "--allow-path", *args],
                              capture_output=True, encoding="utf-8", errors="replace",
                              env=self.env(engram_home, extra), cwd=str(self.tmp), timeout=60)

    def run_hook(self, engram_home: Path, extra: dict | None = None):
        """Run the hook with no pidfile (liveness branch 1) so the supervision decision is taken.
        Returns (state, launched) where state is one of registered-active / registered-failing /
        not-registered / unavailable, read off the hook's own supervision log line."""
        (engram_home / "surface-daemon.log").unlink(missing_ok=True)   # one decision per run
        r = subprocess.run(["bash", str(self.hook)], capture_output=True, encoding="utf-8",
                           errors="replace", env=self.env(engram_home, extra), cwd=str(self.tmp), timeout=60)
        assert r.returncode == 0, (r.stdout, r.stderr)
        deadline = time.time() + 3
        log = engram_home / "surface-daemon.log"
        while time.time() < deadline and not (log.exists() and "supervision:" in log.read_text()):
            time.sleep(0.05)
        text = log.read_text() if log.exists() else ""
        sup = [ln for ln in text.splitlines() if "supervision:" in ln]
        assert len(sup) == 1, text
        line = sup[0].split("supervision: ", 1)[1]
        if line.startswith("SUPERVISED_HEALTHY"):
            state = "registered-active"
        elif line.startswith("SUPERVISED_FAILING"):
            state = "registered-failing"
        elif line.startswith("UNSUPERVISED"):
            state = "not-registered"
        elif line.startswith("unavailable"):
            state = "unavailable"
        else:
            raise AssertionError(line)
        if state == "not-registered":   # give the background stub launch a moment
            deadline = time.time() + 3
            while time.time() < deadline and "stub daemon started" not in log.read_text():
                time.sleep(0.05)
        launched = "stub daemon started" in log.read_text()
        return state, launched, line


@pytest.fixture(params=["Linux", "Darwin"])
def box_any(request, tmp_path):
    return Box(tmp_path, request.param)


@pytest.fixture
def lin(tmp_path):
    return Box(tmp_path, "Linux")


@pytest.fixture
def mac(tmp_path):
    return Box(tmp_path, "Darwin")


def legacy_unit_file(box: Box, home: Path | str, with_env: bool = True):
    """The unit file an old setup run left in the user unit dir, rendered from the CURRENT service
    template with the old fixed name (so the Environment= line is the real one)."""
    d = box.home / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    t = (TEMPLATES / "engram-surface-daemon.service.template").read_text()
    t = (t.replace("{{PYTHON}}", "/usr/bin/python3").replace("{{ENGRAM_HOOKS_DIR}}", "/x/hooks")
          .replace("{{ENGRAM_HOME}}", str(home)))
    if not with_env:
        t = "\n".join(ln for ln in t.splitlines() if not ln.startswith("Environment=ENGRAM_HOME=")) + "\n"
    (d / LEGACY_UNIT).write_text(t)
    return d / LEGACY_UNIT


def legacy_plist_file(box: Box, home: Path | str) -> Path:
    """The plist an old setup run left, the legacy fixed label rendered with the old shape."""
    d = box.home / "Library" / "LaunchAgents"
    d.mkdir(parents=True, exist_ok=True)
    pl = {"Label": LEGACY_LABEL, "ProgramArguments": ["/usr/bin/python3", "/x/hooks/engram-surface-daemon.py", str(home)],
          "EnvironmentVariables": {"ENGRAM_HOME": str(home), "ENGRAM_DAEMON_IDLE_TIMEOUT": "0"},
          "RunAtLoad": True, "KeepAlive": True}
    p = d / f"{LEGACY_LABEL}.plist"
    p.write_bytes(plistlib.dumps(pl))
    return p


def sys_calls(box: Box) -> list:
    return [c for c in box.calls("systemctl")]


def lc_calls(box: Box) -> list:
    return box.calls("launchctl")


# ===========================================================================
# L1 / D1  two homes, two keyed jobs: each hook sees only its own
# ===========================================================================

def test_L1_two_homes_each_hook_sees_only_its_own_unit(lin):
    a, b = lin.new_home("home-a"), lin.new_home("home-b")
    ka, kb = key_of(a), key_of(b)
    assert ka != kb
    lin.unit(unit_for(ka), a, active="active")
    lin.unit(unit_for(kb), b, active="failed")
    sa, launched_a, _ = lin.run_hook(a)
    assert (sa, launched_a) == ("registered-active", False)
    sb_, launched_b, _ = lin.run_hook(b)
    assert (sb_, launched_b) == ("registered-failing", False)
    # and with ONLY A's unit registered, B's hook must not trust it
    (lin.state / "units" / unit_for(kb)).unlink()
    lin.state.joinpath("systemctl.calls").write_text("")
    s, launched, _ = lin.run_hook(b)
    assert (s, launched) == ("not-registered", True)
    asked = " ".join(sys_calls(lin))
    assert unit_for(kb) in asked and unit_for(ka) not in asked


def test_D1_two_homes_each_hook_sees_only_its_own_label(mac):
    a, b = mac.new_home("home-a"), mac.new_home("home-b")
    ka, kb = key_of(a), key_of(b)
    mac.job(label_for(ka), a)
    mac.job(label_for(kb), b, state="waiting")
    assert mac.run_hook(a)[:2] == ("registered-active", False)
    assert mac.run_hook(b)[:2] == ("registered-failing", False)
    mac.job_registered(label_for(kb)) and (mac.state / "jobs" / label_for(kb)).unlink()
    mac.state.joinpath("launchctl.calls").write_text("")
    assert mac.run_hook(b)[:2] == ("not-registered", True)
    asked = " ".join(lc_calls(mac))
    assert label_for(kb) in asked and label_for(ka) not in asked


# ===========================================================================
# L2 / D2  a legacy job for home A does not suppress launch for home B
# ===========================================================================

def test_L2_legacy_unit_for_another_home_does_not_suppress_launch(lin):
    a, b = lin.new_home("home-a"), lin.new_home("home-b")
    lin.unit(LEGACY_UNIT, a)
    state, launched, _ = lin.run_hook(b)
    assert (state, launched) == ("not-registered", True)


def test_D2_legacy_job_for_another_home_does_not_suppress_launch(mac):
    a, b = mac.new_home("home-a"), mac.new_home("home-b")
    mac.job(LEGACY_LABEL, a)
    state, launched, _ = mac.run_hook(b)
    assert (state, launched) == ("not-registered", True)


# ===========================================================================
# L3 / D3  a legacy job for MY home still counts: no second daemon in the transition
# ===========================================================================

def test_L3_legacy_unit_for_my_home_counts_as_registered(lin):
    a = lin.new_home("home-a")
    lin.unit(LEGACY_UNIT, a, active="active")
    assert lin.run_hook(a)[:2] == ("registered-active", False)
    lin.unit(LEGACY_UNIT, a, active="failed")
    assert lin.run_hook(a)[:2] == ("registered-failing", False)


def test_L3_legacy_unit_home_compared_by_physical_path(lin):
    a = lin.new_home("home-a")
    link = lin.tmp / "link-to-a"
    link.symlink_to(a)
    lin.unit(LEGACY_UNIT, str(link) + "/")       # symlink spelling + trailing slash
    assert lin.run_hook(a)[:2] == ("registered-active", False)


def test_D3_legacy_job_for_my_home_counts_as_registered(mac):
    a = mac.new_home("home-a")
    mac.job(LEGACY_LABEL, a)
    assert mac.run_hook(a)[:2] == ("registered-active", False)
    mac.job(LEGACY_LABEL, a, state="waiting")
    assert mac.run_hook(a)[:2] == ("registered-failing", False)


# ===========================================================================
# P1  the keyed name is checked first and its answer stands
# ===========================================================================

def test_P1_keyed_answer_stands_over_a_legacy_one_linux(lin):
    a = lin.new_home("home-a")
    lin.unit(unit_for(key_of(a)), a, active="failed")
    lin.unit(LEGACY_UNIT, a, active="active")
    assert lin.run_hook(a)[:2] == ("registered-failing", False)
    assert LEGACY_UNIT not in " ".join(sys_calls(lin)).replace(unit_for(key_of(a)), "")


def test_P1_keyed_answer_stands_over_a_legacy_one_darwin(mac):
    a = mac.new_home("home-a")
    mac.job(label_for(key_of(a)), a, state="waiting")
    mac.job(LEGACY_LABEL, a)
    assert mac.run_hook(a)[:2] == ("registered-failing", False)
    assert not any(c.endswith("/" + LEGACY_LABEL) for c in lc_calls(mac))


# ===========================================================================
# L5 / D5  unreadable legacy home -> unavailable, never not-registered
# ===========================================================================

def test_L5_legacy_unit_whose_environment_cannot_be_read_is_unavailable(lin):
    a = lin.new_home("home-a")
    lin.unit(LEGACY_UNIT, a, envmode="empty")
    state, launched, _ = lin.run_hook(a)
    assert (state, launched) == ("unavailable", False)


def test_L5_legacy_show_failing_is_unavailable(lin):
    a = lin.new_home("home-a")
    lin.unit(LEGACY_UNIT, a, showrc=1)
    assert lin.run_hook(a)[:2] == ("unavailable", False)


def test_L5_keyed_show_failing_is_unavailable(lin):
    a = lin.new_home("home-a")
    lin.unit(unit_for(key_of(a)), a, showrc=1)
    assert lin.run_hook(a)[:2] == ("unavailable", False)


def test_D5_legacy_job_whose_home_is_unreadable_is_unavailable(mac):
    a = mac.new_home("home-a")
    mac.job(LEGACY_LABEL, a, drop_home=True)
    assert mac.run_hook(a)[:2] == ("unavailable", False)


def test_D5_no_gui_domain_asks_the_user_domain_for_the_keyed_label_first(mac):
    a = mac.new_home("home-a")
    (mac.state / "nogui").write_text("")
    assert mac.run_hook(a)[:2] == ("not-registered", True)
    asked = lc_calls(mac)
    assert any(c.startswith("print user/") and c.endswith(label_for(key_of(a))) for c in asked)


def test_D5_keyed_label_holding_another_homes_job_is_unavailable_never_not_registered(mac):
    """A keyed label (com.engram.surface-daemon.<key>) whose job's ENGRAM_HOME is another home: never trust,
    never fight -> unavailable (no launch), not not-registered."""
    a, b = mac.new_home("home-a"), mac.new_home("home-b")
    mac.job(label_for(key_of(a)), b)           # the keyed label for A, holding B's job
    state, launched = mac.run_hook(a)[:2]
    assert (state, launched) == ("unavailable", False)
    assert not any(c.startswith("bootstrap") for c in lc_calls(mac))


# ===========================================================================
# H1  no key -> unavailable (a missing helper must never read as not-registered)
# ===========================================================================

def test_H1_helper_missing_is_unavailable_not_launch(box_any):
    a = box_any.new_home("home-a")
    (box_any.plugin / "tools" / HOME_KEY.name).unlink()
    state, launched, _ = box_any.run_hook(a)
    assert (state, launched) == ("unavailable", False)


# ===========================================================================
# L4 / D4  setup: remove the legacy job ONLY for my home, name any other
# ===========================================================================

def test_L4_setup_removes_legacy_unit_for_my_home_before_starting_the_keyed_one(lin):
    a = lin.new_home("home-a")
    f = legacy_unit_file(lin, a)
    lin.unit(LEGACY_UNIT, a)
    r = lin.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    ka = key_of(a)
    calls = sys_calls(lin)
    dis = [i for i, c in enumerate(calls) if LEGACY_UNIT in c and "disable" in c]
    ena = [i for i, c in enumerate(calls) if unit_for(ka) in c and "enable" in c]
    assert dis and ena and dis[0] < ena[0], calls        # legacy stopped BEFORE the keyed one starts
    assert not f.exists() and not lin.unit_registered(LEGACY_UNIT)
    assert (lin.home / ".config" / "systemd" / "user" / unit_for(ka)).exists()


def test_L4_setup_leaves_another_homes_legacy_unit_and_names_it_once(lin):
    a, b = lin.new_home("home-a"), lin.new_home("home-b")
    f = legacy_unit_file(lin, b)
    lin.unit(LEGACY_UNIT, b)
    r = lin.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert f.exists() and lin.unit_registered(LEGACY_UNIT)
    assert not any(LEGACY_UNIT in c for c in sys_calls(lin)), sys_calls(lin)   # never touched
    named = [ln for ln in r.stdout.splitlines() if str(b) in ln]
    assert len(named) == 1, r.stdout
    assert (lin.home / ".config" / "systemd" / "user" / unit_for(key_of(a))).exists()


def test_L4_setup_leaves_a_legacy_unit_whose_home_cannot_be_read(lin):
    a = lin.new_home("home-a")
    f = legacy_unit_file(lin, a, with_env=False)
    lin.unit(LEGACY_UNIT, a)
    r = lin.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert f.exists() and not any(LEGACY_UNIT in c for c in sys_calls(lin))
    assert len([ln for ln in r.stdout.splitlines() if "old fixed-name unit" in ln]) == 1, r.stdout


def test_D4_setup_removes_legacy_job_for_my_home_before_bootstrapping_the_keyed_one(mac):
    a = mac.new_home("home-a")
    p = legacy_plist_file(mac, a)
    mac.job(LEGACY_LABEL, a)
    r = mac.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    ka = key_of(a)
    calls = lc_calls(mac)
    out = [i for i, c in enumerate(calls) if c == f"bootout gui/{UID}/{LEGACY_LABEL}"]
    boot = [i for i, c in enumerate(calls) if c.startswith(f"bootstrap gui/{UID} ") and label_for(ka) in c]
    assert out and boot and out[0] < boot[0], calls
    assert not p.exists() and not mac.job_registered(LEGACY_LABEL)
    assert mac.job_registered(label_for(ka))


def test_D4_setup_leaves_another_homes_legacy_job_and_names_it_once(mac):
    a, b = mac.new_home("home-a"), mac.new_home("home-b")
    p = legacy_plist_file(mac, b)
    mac.job(LEGACY_LABEL, b)
    r = mac.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert p.exists() and mac.job_registered(LEGACY_LABEL)
    assert not any(c.endswith("/" + LEGACY_LABEL) and c.startswith("boot") for c in lc_calls(mac)), lc_calls(mac)
    assert len([ln for ln in r.stdout.splitlines() if str(b) in ln]) == 1, r.stdout


def test_D4_setup_reads_a_stale_plist_when_no_job_is_loaded(mac):
    a, b = mac.new_home("home-a"), mac.new_home("home-b")
    pa, pb = legacy_plist_file(mac, a), None
    r = mac.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not pa.exists()                       # plist file named this home: removed
    pb = legacy_plist_file(mac, b)
    r = mac.setup(a)
    assert pb.exists() and len([ln for ln in r.stdout.splitlines() if str(b) in ln]) == 1


def test_D4_setup_leaves_a_legacy_job_whose_home_cannot_be_read(mac):
    a = mac.new_home("home-a")
    mac.job(LEGACY_LABEL, a, drop_home=True)
    r = mac.setup(a)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert mac.job_registered(LEGACY_LABEL)
    assert not any(c.startswith("boot") and c.endswith("/" + LEGACY_LABEL) for c in lc_calls(mac))
    assert len([ln for ln in r.stdout.splitlines() if "old fixed-label job" in ln]) == 1, r.stdout


# ===========================================================================
# L6 / D6  every site computes the SAME name for one home
# ===========================================================================

def _load_check_stale():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_stale_processes_4305", TOOLS / "check_stale_processes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(SRC))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SRC))
    return mod


def _load_probe():
    sys.path.append(str(TOOLS))
    import supervision_probe as sp
    return sp


def _eval_shell_word(word: str) -> str:
    r = subprocess.run(["bash", "-c", f"printf %s {word}"], capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_L6_all_sites_produce_the_same_unit_name(lin):
    a = lin.new_home("home-a")
    ka = key_of(a)
    want = unit_for(ka)
    assert ka == physical_key(a)
    # setup
    assert lin.setup(a).returncode == 0
    assert (lin.home / ".config" / "systemd" / "user" / want).exists()
    assert any(c.endswith(want) and "enable" in c for c in sys_calls(lin))
    # hook
    lin.state.joinpath("systemctl.calls").write_text("")
    lin.run_hook(a)
    assert any(c.startswith("--user show " + want) for c in sys_calls(lin)), sys_calls(lin)
    # supervision_probe
    assert _load_probe().daemon_systemd_unit(a) == want
    # check_stale_processes: the printed restart hint evaluates to the same unit
    cs = _load_check_stale()
    hint = cs._restart_command("surface-daemon", 123, a, lin.plugin, "Linux")
    m = re.search(r'systemctl --user restart (".*?\.service")', hint)
    assert m, hint
    assert _eval_shell_word(m.group(1)) == want


def test_D6_all_sites_compute_the_same_label(mac):
    a = mac.new_home("home-a")
    ka = key_of(a)
    want = label_for(ka)
    assert mac.setup(a).returncode == 0
    assert (mac.home / "Library" / "LaunchAgents" / f"{want}.plist").exists()
    assert any(c.startswith(f"bootstrap gui/{UID} ") and c.endswith(f"{want}.plist") for c in lc_calls(mac))
    mac.state.joinpath("launchctl.calls").write_text("")
    (mac.state / "jobs" / want).unlink()
    mac.run_hook(a)
    assert any(c == f"print gui/{UID}/{want}" for c in lc_calls(mac)), lc_calls(mac)
    assert _load_probe().daemon_launchd_label(a) == want
    cs = _load_check_stale()
    hint = cs._restart_command("surface-daemon", 123, a, mac.plugin, "Darwin")
    m = re.search(r'launchctl kickstart -k (gui/\$\(id -u\)/com\.engram\.surface-daemon\.\$\(bash .*?\)) \(if', hint)
    assert m, hint
    assert _eval_shell_word(m.group(1)) == f"gui/{UID}/{want}"


# ===========================================================================
# D7  a temp HOME with ENGRAM_HOME unset gets a keyed name, NEVER the fixed one
# ===========================================================================

def test_D7_temp_home_with_engram_home_unset_is_keyed_not_fixed(box_any):
    b = box_any
    r = b.setup(None)                      # ENGRAM_HOME unset: the home is $HOME/.engram
    assert r.returncode == 0, (r.stdout, r.stderr)
    key = key_of(b.home / ".engram")
    if b.system == "Linux":
        names = [p.name for p in (b.home / ".config" / "systemd" / "user").iterdir()]
        assert names == [unit_for(key)], names
        assert not any(c.endswith(LEGACY_UNIT) and "enable" in c for c in sys_calls(b))
    else:
        names = [p.name for p in (b.home / "Library" / "LaunchAgents").iterdir()]
        assert names == [f"{label_for(key)}.plist"], names
        assert not any(c.endswith(f"/{LEGACY_LABEL}") and c.startswith("boot") for c in lc_calls(b))


# ===========================================================================
# D8  the owner's names and rendering are untouched
# ===========================================================================

def test_D8_owner_unit_and_label_unchanged_linux(lin):
    a = lin.new_home("home-a")
    (lin.plugin / "engramd_owner.py").write_text("import sys\nsys.exit(0)\n")
    r = lin.setup(a, "--owner")
    assert r.returncode == 0, (r.stdout, r.stderr)
    k = hashlib.sha256(os.path.realpath(str(a)).encode()).hexdigest()[:8]
    unit = lin.home / ".config" / "systemd" / "user" / f"engramd-owner-{k}.service"
    assert unit.exists()
    t = (TEMPLATES / "engramd-owner.service.template").read_text()
    exp = (t.replace("{{PYTHON}}", str(lin.home / ".engram/venv/bin/python3"))
            .replace("{{ENGRAM_OWNER_PY}}", str(lin.plugin / "engramd_owner.py"))
            .replace("{{ENGRAM_HOME}}", os.path.realpath(str(a))))
    assert unit.read_text() == exp
    assert not any(LEGACY_UNIT in c or "surface-daemon-" in c for c in sys_calls(lin))


def test_D8_owner_unit_and_label_unchanged_darwin(mac):
    a = mac.new_home("home-a")
    (mac.plugin / "engramd_owner.py").write_text("import sys\nsys.exit(0)\n")
    r = mac.setup(a, "--owner")
    assert r.returncode == 0, (r.stdout, r.stderr)
    k = hashlib.sha256(os.path.realpath(str(a)).encode()).hexdigest()[:8]
    label = f"com.engram.engramd-owner.{k}"
    plist = mac.home / "Library" / "LaunchAgents" / f"{label}.plist"
    assert plist.exists()
    t = (TEMPLATES / "engramd-owner.plist.template").read_text()
    exp = (t.replace("{{PYTHON}}", str(mac.home / ".engram/venv/bin/python3"))
            .replace("{{ENGRAM_OWNER_PY}}", str(mac.plugin / "engramd_owner.py"))
            .replace("{{ENGRAM_HOME}}", os.path.realpath(str(a))).replace("{{LABEL}}", label))
    assert plist.read_text() == exp
    assert not any("surface-daemon" in c for c in lc_calls(mac))


# ===========================================================================
# D9  the same home twice gives the same name (idempotent re-install)
# ===========================================================================

def test_D9_same_home_twice_same_name(box_any):
    b = box_any
    a = b.new_home("home-a")
    assert b.setup(a).returncode == 0
    if b.system == "Linux":
        first = sorted(p.name for p in (b.home / ".config/systemd/user").iterdir())
        txt = (b.home / ".config/systemd/user" / first[0]).read_text()
        assert b.setup(a).returncode == 0
        assert sorted(p.name for p in (b.home / ".config/systemd/user").iterdir()) == first
        assert (b.home / ".config/systemd/user" / first[0]).read_text() == txt
    else:
        first = sorted(p.name for p in (b.home / "Library/LaunchAgents").iterdir())
        assert b.setup(a).returncode == 0
        assert sorted(p.name for p in (b.home / "Library/LaunchAgents").iterdir()) == first
    assert len(first) == 1


# ===========================================================================
# S-c  pure render: the stop condition for a deleted home, with THIS home (no service manager)
# ===========================================================================

def test_Sc_linux_unit_carries_the_condition_with_this_exact_home(lin):
    """#4305 ruling: ConditionPathExists is NOT re-checked on an automatic restart (systemd 255.4, measured), so
    the stop condition is ExecCondition=/usr/bin/test -d <home> in [Service] (re-run before every start,
    restarts included; floor 243)."""
    a = lin.new_home("home-a")
    assert lin.setup(a).returncode == 0
    text = (lin.home / ".config/systemd/user" / unit_for(key_of(a))).read_text()
    unit_part, service_part = text.split("[Service]", 1)
    live = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    svc_lines = [ln.strip() for ln in service_part.split("[Install]", 1)[0].splitlines()]
    assert f"ExecCondition=/usr/bin/test -d {a}" in svc_lines, service_part
    assert not any(ln.startswith("ExecCondition") for ln in unit_part.splitlines())      # [Service], not [Unit]
    assert sum(ln.startswith("ExecCondition=") for ln in live) == 1
    assert not any(ln.startswith("ConditionPath") for ln in live)        # exactly one mechanism, never both
    assert f"ExecCondition=/usr/bin/test -d {a.parent}" not in live


def test_Sb_render_linux_crash_is_still_restarted_while_the_home_exists(lin):
    """S-b's render half: the stop condition must not weaken supervision (a crash restarts). Independent of
    the condition cell above, so dropping or mis-pointing the condition leaves THIS cell green."""
    a = lin.new_home("home-a")
    assert lin.setup(a).returncode == 0
    svc = (lin.home / ".config/systemd/user" / unit_for(key_of(a))).read_text().split("[Service]", 1)[1]
    assert "Restart=always" in svc and "RestartSec=5s" in svc
    assert "RestartPreventExitStatus" not in svc and "SuccessExitStatus" not in svc


def test_Sb_render_darwin_crash_is_still_restarted_while_the_home_exists(mac):
    a = mac.new_home("home-a")
    assert mac.setup(a).returncode == 0
    pl = plistlib.loads((mac.home / "Library/LaunchAgents" / f"{label_for(key_of(a))}.plist").read_bytes())
    ka = pl["KeepAlive"]
    # A crash must restart: KeepAlive is true, or a dict with no exit-status exemption (SuccessfulExit /
    # Crashed would make some exits final). Deliberately silent about WHAT the condition is: that is S-c.
    assert ka is True or (isinstance(ka, dict) and not ({"SuccessfulExit", "Crashed"} & set(ka)))
    assert pl["RunAtLoad"] is True


def test_Sc_darwin_plist_carries_the_condition_with_this_exact_home(mac):
    a = mac.new_home("home-a")
    assert mac.setup(a).returncode == 0
    ka = key_of(a)
    pl = plistlib.loads((mac.home / "Library/LaunchAgents" / f"{label_for(ka)}.plist").read_bytes())
    assert pl["Label"] == label_for(ka)
    assert pl["EnvironmentVariables"]["ENGRAM_HOME"] == str(a)
    ka_dict = pl["KeepAlive"]
    # UNMEASURED candidate (Borges' memory of launchd.plist(5)): keep alive only while the home exists.
    assert isinstance(ka_dict, dict) and list(ka_dict) == ["PathState"], ka_dict
    assert ka_dict["PathState"] == {str(a): True}
    assert str(a.parent) not in ka_dict["PathState"]


def test_Sc_templates_carry_placeholders_not_literals():
    plist = (TEMPLATES / "engram-surface-daemon.plist.template").read_text()
    svc = (TEMPLATES / "engram-surface-daemon.service.template").read_text()
    assert "{{LABEL}}" in plist and LEGACY_LABEL not in plist
    live = [ln.strip() for ln in svc.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "ExecCondition=/usr/bin/test -d {{ENGRAM_HOME}}" in svc.split("[Service]", 1)[1]
    assert not any(ln.startswith("ConditionPath") for ln in live)


# ===========================================================================
# S-a / S-b  REAL service manager.  Off by default; the parent runs them, on a seat, temp homes only.
# ===========================================================================

real_manager = pytest.mark.skipif(
    os.environ.get("ENGRAM_REAL_SERVICE_MANAGER_4305") != "1",
    reason="needs a REAL systemd --user / launchd gui domain; set ENGRAM_REAL_SERVICE_MANAGER_4305=1 "
           "(the parent runs these; they register a keyed job for a TEMP home and remove it)")


def _real_names_for(key: str):
    return unit_for(key), label_for(key)


class _RealJob:
    """Registers a keyed surface-daemon job for a TEMP home with the REAL script and REAL manager.
    Safety: the only name ever passed to the manager is the one keyed on this temp home, asserted
    below; PATH has no shim, so the manager is the real one."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "engram-home"
        self.home.mkdir()
        self.key = key_of(self.home)
        assert str(self.home).startswith(str(tmp))
        self.unit, self.label = _real_names_for(self.key)
        self.plugin = tmp / "plugin"
        (self.plugin / "tools").mkdir(parents=True)
        (self.plugin / "templates").mkdir()
        (self.plugin / "hooks").mkdir()
        (self.plugin / "engram_core.py").write_text("")
        for f in (HOME_KEY, SETUP):
            shutil.copy(f, self.plugin / "tools" / f.name)
        for f in TEMPLATES.iterdir():
            if f.is_file():
                shutil.copy(f, self.plugin / "templates" / f.name)
        (self.plugin / "hooks" / "engram-surface-daemon.py").write_text(
            "import time\nwhile True:\n    time.sleep(1)\n")

    def install(self):
        env = {**os.environ, "ENGRAM_HOME": str(self.home)}
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"       # NO conftest shim: the real tools
        r = subprocess.run(["bash", str(self.plugin / "tools" / SETUP.name), "--allow-path"],
                           capture_output=True, encoding="utf-8", env=env, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)

    def remove(self):
        if IS_DARWIN:
            assert self.label.endswith(self.key)
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{UID}/{self.label}"], capture_output=True)
            (Path.home() / "Library/LaunchAgents" / f"{self.label}.plist").unlink(missing_ok=True)
        else:
            assert self.unit == unit_for(self.key)
            subprocess.run(["systemctl", "--user", "disable", "--now", self.unit], capture_output=True)
            (Path.home() / ".config/systemd/user" / self.unit).unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # ---- observations
    def main_pid(self) -> int:
        if IS_DARWIN:
            out = subprocess.run(["/bin/launchctl", "print", f"gui/{UID}/{self.label}"],
                                 capture_output=True, encoding="utf-8").stdout
            m = re.search(r"^\tpid = (\d+)$", out, re.M)
        else:
            out = subprocess.run(["systemctl", "--user", "show", self.unit, "-p", "MainPID"],
                                 capture_output=True, encoding="utf-8").stdout
            m = re.search(r"MainPID=(\d+)", out)
        return int(m.group(1)) if m and int(m.group(1)) > 0 else 0

    def restarts(self) -> int:
        if IS_DARWIN:
            out = subprocess.run(["/bin/launchctl", "print", f"gui/{UID}/{self.label}"],
                                 capture_output=True, encoding="utf-8").stdout
            m = re.search(r"^\truns = (\d+)$", out, re.M)
        else:
            out = subprocess.run(["systemctl", "--user", "show", self.unit, "-p", "NRestarts"],
                                 capture_output=True, encoding="utf-8").stdout
            m = re.search(r"NRestarts=(\d+)", out)
        return int(m.group(1)) if m else -1


def _wait_pid(job: _RealJob, timeout: float) -> int:
    end = time.time() + timeout
    while time.time() < end:
        pid = job.main_pid()
        if pid:
            return pid
        time.sleep(0.5)
    return 0


@real_manager
def test_Sb_home_present_sigkill_is_restarted(tmp_path):
    job = _RealJob(tmp_path)
    try:
        job.install()
        pid = _wait_pid(job, 20)
        assert pid, "the keyed job never started"
        os.kill(pid, signal.SIGKILL)
        end, new = time.time() + 30, 0
        while time.time() < end:
            new = job.main_pid()
            if new and new != pid:
                break
            time.sleep(0.5)
        assert new and new != pid, "supervision weakened: a SIGKILLed daemon was not restarted"
    finally:
        job.remove()


@real_manager
def test_Sa_deleted_home_no_restart_loop(tmp_path):
    """WRITTEN, NOT RUN by its author (needs a real manager).  The daemon must EXIT while the home is gone
    (a daemon that keeps running would pass vacuously), so the stand-in daemon exits after ~1s; with the home
    present it is restarted (Restart=always, RestartSec from the real template); with the home deleted the
    ExecCondition (Linux) / PathState (macOS) must stop the restarts: the counter stops growing, inactive."""
    job = _RealJob(tmp_path)
    (job.plugin / "hooks" / "engram-surface-daemon.py").write_text("import time\ntime.sleep(1)\n")
    try:
        job.install()
        end = time.time() + 40
        while time.time() < end and job.restarts() < 1:     # it exits ~1s after each start: proves restarts happen
            time.sleep(0.5)
        assert job.restarts() >= 1, "the exiting stand-in daemon was never restarted while the home existed"
        shutil.rmtree(job.home)
        time.sleep(20)                                       # let any in-flight restart settle
        n0 = job.restarts()
        time.sleep(30)                                       # >= 5 restart periods if it looped
        n1 = job.restarts()
        assert n1 == n0, f"restart loop after the home was deleted: {n0} -> {n1}"
        assert not job.main_pid(), "a daemon is running for a deleted home"
        if not IS_DARWIN:
            st = subprocess.run(["systemctl", "--user", "show", job.unit, "-p", "ActiveState"],
                                capture_output=True, encoding="utf-8").stdout
            assert "ActiveState=inactive" in st, st
    finally:
        job.remove()


# ===========================================================================
# G1  no supervisor-identity literal outside the named legacy-transition code
# ===========================================================================

_LITERAL_UNIT = re.compile(r"engram-surface-daemon\.service(?!\.template)")
# the keyed shape is `com.engram.surface-daemon.` + a `$`/`{`/`<`/`%` expansion; anything else is the literal
_LITERAL_LABEL = re.compile(r"com\.engram\.surface-daemon(?!\.[${<%])")

# THE ALLOWLIST: (path relative to the repo, enclosing function) -- the legacy-transition code and nothing
# else.  A new literal anywhere else, or in another function of these files, fails this cell.
LEGACY_ALLOWLIST = {
    ("src/engram/tools/setup-surface-daemon-service.sh", "_legacy_surface_unit"),
    ("src/engram/tools/setup-surface-daemon-service.sh", "_legacy_surface_label"),
    ("src/engram/hooks/claude/start-engram-daemon.sh", "_legacy_surface_unit"),
    ("src/engram/hooks/claude/start-engram-daemon.sh", "_legacy_surface_label"),
    # #4446: uninstall stops this home's old fixed-name units too (same ENGRAM_HOME check as the setup script).
    ("src/engram/tools/uninstall-local-marketplace.sh", "_uninst_legacy_surface_unit"),
    ("src/engram/tools/uninstall-local-marketplace.sh", "_uninst_legacy_surface_label"),
    # the same file seen through the repo-root tools/ symlink the scan also walks
    ("tools/uninstall-local-marketplace.sh", "_uninst_legacy_surface_unit"),
    ("tools/uninstall-local-marketplace.sh", "_uninst_legacy_surface_label"),
}

_FUNC_RE = {".sh": re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{"), ".py": re.compile(r"^\s*def ([A-Za-z_0-9]+)\(")}


def _scan_literals():
    hits = []
    for root in ("src", "tools"):
        for p in sorted((REPO / root).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix in {".pyc", ".db", ".png", ".gz", ".whl"}:
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            func = "<top>"
            rx = _FUNC_RE.get(p.suffix)
            for n, ln in enumerate(lines, 1):
                if rx and (m := rx.match(ln)):
                    func = m.group(1)
                if _LITERAL_UNIT.search(ln) or _LITERAL_LABEL.search(ln):
                    hits.append((str(p.relative_to(REPO)), func, n, ln.strip()))
    return hits


def test_G1_no_supervisor_identity_literal_outside_the_legacy_transition_code():
    stray = [h for h in _scan_literals() if (h[0], h[1]) not in LEGACY_ALLOWLIST]
    assert not stray, "supervisor-identity literal outside the legacy allowlist:\n" + "\n".join(
        f"  {f}:{n} ({fn}) {t[:110]}" for f, fn, n, t in stray)


def test_G1_the_allowlist_is_not_stale():
    """Every allowlisted function still holds a literal (an entry nothing needs is removed, not kept)."""
    used = {(f, fn) for f, fn, _, _ in _scan_literals()}
    assert LEGACY_ALLOWLIST <= used, sorted(LEGACY_ALLOWLIST - used)
