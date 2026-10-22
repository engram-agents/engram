"""#3709 part 2: the macOS (launchd) launcher for the engramd owner.  RED CELLS -- written before
the implementation.  Contract: the #3709 spec + the design-seat ruling (Q1-Q7).

Cell groups
  K1  one home key, three consumers (the installer, the SessionStart hook, the plist label)
  K2  the rendered plist, its XML refusals, and the two launchctl calls
  K4  the Darwin fallback, four outcomes, against fake launchctl built from REAL captures
  K5  liveness on engram_pidfile's named strict mode, both platforms
  K6  the owner's `--preflight`, and `--owner` on Darwin refusing before it writes anything
  K7  the #3519 guards (fake launchctl first on PATH, temp HOME, ephemeral-path refusal)
  R1  one real-launchd cell (Darwin only), which also covers K3 (restart semantics)

#3519: every cell except R1 puts its OWN recording fake `launchctl` first on a hermetic PATH and
runs with HOME in a tmp dir.  No cell registers a `com.engram.engramd-owner.*` label with a real
launchctl; an autouse guard (below) diffs the real registrations around every cell to prove it.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

# Reuse the #3663 harness by import (tests/ is on sys.path; the module imports cleanly).
from test_owner_launcher_3663 import (  # noqa: E402
    _TINY_LAUNCHD_ENV,
    FLAG_ON,
    HOOK,
    HOOK_IO,
    INSTALLER,
    REPO,
    SRC,
    TEMPLATES,
    Sandbox,
    Seat,
    _expected_id,
    _physical,
    _stub,
    context_lines,
)

import engram_pidfile as ep  # noqa: E402  (conftest puts src/engram on sys.path)

# ===========================================================================
# NAMES THE IMPLEMENTATION MUST PROVIDE (every invented name is in this block)
# ===========================================================================
# Shared home-key helper (K1).  Bash 3.2 compatible.  Run as `bash <helper> <home>` it prints the
# 8-hex key for <home> (tilde expanded, physical via `cd && pwd -P`, trailing slashes stripped,
# sha256, first 8 hex) and exits 0; SOURCED, it defines the shell function HOME_KEY_FUNC with the
# same behaviour.  It lives in tools/ next to setup-surface-daemon-service.sh, and the hook finds
# it at <plugin root>/tools/.
HOME_KEY_HELPER = SRC / "tools" / "engramd-owner-home-key.sh"
HOME_KEY_FUNC = "engramd_owner_home_key"
# The plist template (K2).  Placeholders the installer substitutes (R1 renders the same four).
PLIST_TEMPLATE = TEMPLATES / "engramd-owner.plist.template"
PLIST_PLACEHOLDERS = ("{{PYTHON}}", "{{ENGRAM_OWNER_PY}}", "{{ENGRAM_HOME}}", "{{LABEL}}")
LABEL_PREFIX = "com.engram.engramd-owner."  # label = LABEL_PREFIX + <8-hex home key>
# Hook I/O helper (_engramd_owner_hook_io.py): the new verb and the parser function it uses.
#   launchd_state <label>  -> one line: running | registered:<state>:<last exit or none> | notfound
#                             | ambiguous:<why>
#   parse_launchctl_print(rc: int, stdout: str, stderr: str) -> str   (the same verdict strings)
LAUNCHD_VERB = "launchd_state"
LAUNCHD_PARSE_FUNC = "parse_launchctl_print"
LAUNCHD_BOUND_SECONDS = 3.0  # the Python-side timeout on `launchctl print` (Linux uses 3 s)
LAUNCHD_SLACK_SECONDS = 6.0  # generous slack on top of the bound for process start-up etc.
# `live` verb: ambiguous verdict when engram_pidfile cannot be imported.
LIVE_AMBIGUOUS_PREFIX = "ambiguous"
# engram_pidfile named strict mode: marker required for ANY live pid, whatever its start time.
STRICT_KW = "require_marker"
# Owner preflight entry point: `python engramd_owner.py --preflight` (ENGRAM_HOME from env).
PREFLIGHT_FLAG = "--preflight"
# ===========================================================================

OWNER_PY = SRC / "engramd_owner.py"
PIDFILE_PY = SRC / "engram_pidfile.py"
FIXTURES = REPO / "tests" / "fixtures" / "launchctl_3709"
UID = os.getuid()
OWNER_RUN_TIMEOUT = 8.0  # a preflight must exit by itself; an owner that is SERVING never does
IS_DARWIN = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# #3519 guard: nothing here may register a production-shaped owner label with a real launchctl
# ---------------------------------------------------------------------------

def _real_owner_labels() -> set:
    if not IS_DARWIN:
        return set()
    # `list` is read-only (the conftest shim passes it through); absolute path so the shim is moot.
    r = subprocess.run(["/bin/launchctl", "list"], capture_output=True, encoding="utf-8", errors="replace", timeout=20)
    return {ln.split()[-1] for ln in r.stdout.splitlines()
            if ln.split() and ln.split()[-1].startswith(LABEL_PREFIX)}


@pytest.fixture(autouse=True)
def _no_real_owner_registration_leaks():
    before = _real_owner_labels()
    yield
    leaked = _real_owner_labels() - before
    assert not leaked, f"a cell registered production-shaped owner label(s) with REAL launchd: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# Harness extensions (copies of nothing: subclasses of the #3663 Sandbox / Seat)
# ---------------------------------------------------------------------------

def label_for(home: Path) -> str:
    return LABEL_PREFIX + _expected_id(_physical(home))


class DSandbox(Sandbox):
    """#3663 Sandbox + the shared helper copied next to the installer + a pinned venv python.
    `real_owner=True` swaps the stub owner for a symlink to the REAL owner (K6 preflight)."""

    def __init__(self, tmp: Path, os_name: str = "Darwin", real_owner: bool = False,
                 plugin_name: str = "plugin", home_name: str = "home"):
        tmp.mkdir(parents=True, exist_ok=True)
        super().__init__(tmp, os_name=os_name, plugin_name=plugin_name, home_name=home_name)
        if HOME_KEY_HELPER.exists():
            shutil.copy(HOME_KEY_HELPER, self.plugin / "tools" / HOME_KEY_HELPER.name)
        venv_bin = self.home / ".engram" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        self.python = _stub(venv_bin, "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        if real_owner:
            (self.plugin / "engramd_owner.py").unlink()
            (self.plugin / "engramd_owner.py").symlink_to(OWNER_PY)

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def launchctl_calls(self) -> list:
        if not self.launchlog.exists():
            return []
        return [ln.split() for ln in self.launchlog.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def bootstrap_rc(self, rc: int):
        """Replace the recording launchctl stub with one that fails `bootstrap` with `rc` (#3780).
        Every call is still recorded; every other verb exits 0.  No real launchctl is involved."""
        _stub(self.stubs, "launchctl",
              f'#!/usr/bin/env bash\necho "$@" >> "{self.launchlog}"\n'
              f'if [ "$1" = print ]; then exit 113; fi\n'   # #3939: "gone", so the post-bootout wait ends at once
              f'if [ "$1" = bootstrap ]; then echo "Bootstrap failed: {rc}" >&2; exit {rc}; fi\nexit 0\n')

    def run_env(self, *args: str, engram_home: str, drop: tuple = (), extra: dict | None = None,
                bash: str = "bash"):
        """Sandbox.run with the environment shaped (#3780): `drop` names removed, `extra` added.
        `bash` is the EXPLICIT interpreter (not whatever PATH finds first)."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("ENGRAM_HOME", "ENGRAM_HOOKS_DIR", "ENGRAM_SETUP_ALLOW_PATH", *drop)}
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.stubs}:{os.environ['PATH']}"
        env["ENGRAM_HOME"] = engram_home
        env.update(_TINY_LAUNCHD_ENV)   # #3939
        env.update(extra or {})
        return subprocess.run([bash, str(self.installer), *args, "--allow-path"], capture_output=True,
                              encoding="utf-8", errors="replace", env=env, cwd=str(self.tmp))

    def assert_nothing_written(self, *engram_homes: Path):
        assert not (self.home / "Library").exists(), "a LaunchAgents dir/plist was created"
        assert not self.launchlog.exists(), f"launchctl was called: {self.launchlog.read_text(encoding="utf-8")}"
        for h in engram_homes:
            assert not h.exists(), f"the home dir was created: {h}"


class DSeat(Seat):
    """#3663 Seat + the helper under <plugin>/tools/ + engram_pidfile at the plugin root + a fake
    `launchctl` driven by a mode file, whose canned outputs are the REAL captures in FIXTURES."""

    def __init__(self, tmp: Path, os_name: str = "Darwin", with_pidfile: bool = True):
        tmp.mkdir(parents=True, exist_ok=True)
        super().__init__(tmp, os_name=os_name)
        for t in ("shasum", "sha256sum", "id", "head", "tr", "cat"):  # hermetic PATH, Mac and Linux
            found = shutil.which(t)
            if found and not (self.tools / t).exists():
                (self.tools / t).symlink_to(found)
        (self.plugin / "tools").mkdir()
        if HOME_KEY_HELPER.exists():
            shutil.copy(HOME_KEY_HELPER, self.plugin / "tools" / HOME_KEY_HELPER.name)
        if with_pidfile:
            shutil.copy(PIDFILE_PY, self.plugin / "engram_pidfile.py")
            # engram_pidfile imports engram_regular_io since #3772 (dev), so a plugin root that
            # ships one ships the other
            shutil.copy(SRC / "engram_regular_io.py", self.plugin / "engram_regular_io.py")
        self.lmode = tmp / "launchctl.mode"
        self.llog = tmp / "launchctl.log"
        self.derived = tmp / "derived"
        self.derived.mkdir()
        # Derived (not hand-typed) variants of the real captures:
        running = (FIXTURES / "running.out").read_text(encoding="utf-8")
        kept = [ln for ln in running.splitlines(keepends=True) if not ln.startswith("\tstate = ")]
        assert len(kept) == len(running.splitlines(keepends=True)) - 1, "fixture has no one top-level state line"
        assert any(ln.startswith("\t\tstate = ") for ln in kept), "fixture lost its nested state lines"
        (self.derived / "nostate.out").write_text("".join(kept))
        nr = (FIXTURES / "not_running_exit0.out").read_text(encoding="utf-8")
        nolast = [ln for ln in nr.splitlines(keepends=True) if not ln.startswith("\tlast exit code = ")]
        assert len(nolast) == len(nr.splitlines(keepends=True)) - 1
        (self.derived / "not_running_nolast.out").write_text("".join(nolast))
        nf = (FIXTURES / "notfound.err").read_text(encoding="utf-8")
        (self.derived / "badrequest_only.err").write_text(nf.splitlines()[0] + "\n")

    def set_os(self, name: str):
        _stub(self.stubs, "uname", f'#!/usr/bin/env bash\necho "{name}"\n')

    def launchctl(self, mode: str):
        self.lmode.write_text(mode)
        F, D = FIXTURES, self.derived
        _stub(self.stubs, "launchctl", textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$*" >> "{self.llog}"
            case "$(cat "{self.lmode}")" in
              running) cat "{F}/running.out";;
              not_running) cat "{F}/not_running_exit0.out";;
              not_running_nolast) cat "{D}/not_running_nolast.out";;
              spawn_scheduled) cat "{F}/spawn_scheduled_exit1.out";;
              notfound) cat "{F}/notfound.err" >&2; exit "$(tr -d '[:space:]' < "{F}/notfound.rc")";;
              timeout) exec sleep 30;;
              empty) exit 0;;
              nostate) cat "{D}/nostate.out";;
              no_gui_domain) cat "{F}/no_gui_domain.err" >&2; exit "$(tr -d '[:space:]' < "{F}/no_gui_domain.rc")";;
              rc113_notext) cat "{D}/badrequest_only.err" >&2; exit "$(tr -d '[:space:]' < "{F}/notfound.rc")";;
              other_rc) cat "{F}/notfound.err" >&2; exit 2;;
              *) exit 99;;
            esac
            """))

    def launchctl_log(self) -> list:
        return self.llog.read_text(encoding="utf-8").splitlines() if self.llog.exists() else []

    def label(self) -> str:
        return label_for(self.eh)

    def expected_print(self) -> str:
        return f"print gui/{UID}/{self.label()}"

    def io(self, *args, timeout: float = 15.0) -> str:
        """Run the hook I/O helper directly (as the hook would) and return its stripped stdout."""
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:{self.tools}", "TMPDIR": str(self.tmp)}
        io_py = self.plugin / "hooks" / "_engramd_owner_hook_io.py"
        try:
            r = subprocess.run([sys.executable, str(io_py), *map(str, args)], capture_output=True, encoding="utf-8", errors="replace",
                               env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(f"hook I/O helper {args} did not return within {timeout}s (blocked)")
        return r.stdout.strip()


@pytest.fixture
def dseat(tmp_path):
    return DSeat(tmp_path / "seat")


@pytest.fixture
def short_base():
    """A SHORT physical base dir (AF_UNIX socket paths must fit): pytest's tmp_path is ~100 chars
    on macOS and would sit on the 103-byte Darwin limit by itself."""
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="k", dir="/tmp")))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _plist_of(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())


def _helper_key(arg: str, home: Path, bash: str = "bash") -> str:
    assert HOME_KEY_HELPER.exists(), f"missing (not implemented yet): {HOME_KEY_HELPER.relative_to(REPO)}"
    r = subprocess.run([bash, str(HOME_KEY_HELPER), arg], capture_output=True, encoding="utf-8", errors="replace",
                       env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return r.stdout.strip()


# ===========================================================================
# K1 -- one home key, three consumers (Q1)
# ===========================================================================

def test_k1_helper_gives_sha256_of_the_physical_home_for_every_spelling(tmp_path):
    real = tmp_path / "real-seat"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home = tmp_path / "h"
    (home / "sub").mkdir(parents=True)
    expected = _expected_id(_physical(real))
    for spelling in (str(real), str(real) + "/", str(real) + "//", str(link), str(link) + "/"):
        assert _helper_key(spelling, home) == expected, spelling
    assert _helper_key("~/sub", home) == _expected_id(_physical(home / "sub"))


def test_k1_helper_is_sourceable_and_defines_the_function(tmp_path):
    assert HOME_KEY_HELPER.exists(), f"missing (not implemented yet): {HOME_KEY_HELPER.relative_to(REPO)}"
    real = tmp_path / "real-seat"
    real.mkdir()
    r = subprocess.run(["bash", "-c", f'source "$1" && {HOME_KEY_FUNC} "$2"', "_", str(HOME_KEY_HELPER), str(real)],
                       capture_output=True, encoding="utf-8", errors="replace", env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    assert r.returncode == 0 and r.stdout.strip() == _expected_id(_physical(real)), (r.returncode, r.stdout, r.stderr)


@pytest.mark.skipif(not (IS_DARWIN and os.access("/bin/bash", os.X_OK)),
                    reason="the stock bash 3.2 is a macOS thing; elsewhere /bin/bash is already modern")
def test_k1_helper_runs_under_the_stock_bash_3_2(tmp_path):
    real = tmp_path / "real-seat"
    real.mkdir()
    assert _helper_key(str(real), tmp_path, bash="/bin/bash") == _expected_id(_physical(real))


def _code_lines(path: Path) -> str:
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("script", [INSTALLER, HOOK], ids=lambda p: p.name)
def test_k1_neither_consumer_computes_sha256_itself(script):
    code = _code_lines(script)
    assert "sha256sum" not in code and "shasum" not in code, \
        f"{script.name} still hashes ENGRAM_HOME by hand (comment lines excluded)"


def test_k1_all_consumers_agree_for_a_home_with_a_symlink_in_it(tmp_path):
    """Linux unit name, Darwin plist label + filename, the label the hook asks launchctl about, and
    the unit the hook asks systemctl about are all sha256(physical)[:8] -- for one symlinked home."""
    seat = DSeat(tmp_path / "hk")
    link = tmp_path / "link"
    link.symlink_to(seat.eh)  # macOS /var -> /private/var is the live case of this shape
    key = _expected_id(_physical(seat.eh))
    spelled = str(link) + "/"

    unit = DSandbox(tmp_path / "lin", os_name="Linux").install_owner(spelled)
    assert unit.name == f"engramd-owner-{key}.service"

    dar = DSandbox(tmp_path / "dar", os_name="Darwin")
    r = dar.run("--owner", engram_home=spelled)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    plist = dar.agents_dir / f"{LABEL_PREFIX}{key}.plist"
    assert plist.exists(), sorted(p.name for p in dar.agents_dir.glob("*")) if dar.agents_dir.exists() else "no dir"
    assert _plist_of(plist)["Label"] == f"{LABEL_PREFIX}{key}"

    seat.config(FLAG_ON)
    seat.set_os("Darwin")
    seat.launchctl("notfound")
    seat.run({"ENGRAM_HOME": str(link)})
    assert seat.launchctl_log() and seat.launchctl_log()[0] == f"print gui/{UID}/{LABEL_PREFIX}{key}", \
        seat.launchctl_log()

    seat.set_os("Linux")
    seat.systemctl("notfound")
    seat.run({"ENGRAM_HOME": str(link)})
    assert f"engramd-owner-{key}.service" in seat.syslog.read_text(encoding="utf-8")


def test_k1_tilde_spelling_gives_the_same_label_and_filename(tmp_path):
    dar = DSandbox(tmp_path / "dar", os_name="Darwin")
    r = dar.run("--owner", engram_home="~/.engram")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    key = _expected_id(_physical(dar.home / ".engram"))
    assert (dar.agents_dir / f"{LABEL_PREFIX}{key}.plist").exists()


# ===========================================================================
# K2 -- the plist (Q2), its XML refusals, the two launchctl calls
# ===========================================================================

def _install(tmp_path):
    """Run `--owner` on a faked Darwin and return (sandbox, home, plist path, parsed plist).  A
    plain function (not a fixture) so a missing feature is a FAILED cell, not a setup ERROR."""
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    home = sb.tmp / "seat-a"
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    plist = sb.agents_dir / f"{label_for(home)}.plist"
    assert plist.exists(), "no plist at $HOME/Library/LaunchAgents/<label>.plist"
    return sb, home, plist, _plist_of(plist)


def test_k2_plist_label_and_program_arguments(tmp_path):
    installed = _install(tmp_path)
    sb, home, _p, d = installed
    assert d["Label"] == label_for(home)
    py, owner = d["ProgramArguments"]
    assert py == str(sb.python)
    assert owner == _physical(sb.plugin) + "/engramd_owner.py" and "engramd_owner" in owner


def test_k2_plist_environment_and_logs(tmp_path):
    installed = _install(tmp_path)
    _sb, home, _p, d = installed
    assert d["EnvironmentVariables"]["ENGRAM_HOME"] == _physical(home)
    log = f"{_physical(home)}/engramd-owner.log"
    assert d["StandardOutPath"] == log and d["StandardErrorPath"] == log


def test_k2_plist_restart_policy_is_the_ruled_one(tmp_path):
    installed = _install(tmp_path)
    _sb, _h, _p, d = installed
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] == {"SuccessfulExit": False}, d.get("KeepAlive")
    assert d["ThrottleInterval"] == 60 and isinstance(d["ThrottleInterval"], int)


def test_k2_rendered_plist_has_no_unsubstituted_placeholder(tmp_path):
    installed = _install(tmp_path)
    _sb, _h, plist, _d = installed
    assert "{{" not in plist.read_text(encoding="utf-8") and "}}" not in plist.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil only exists on macOS")
def test_k2_rendered_plist_passes_plutil_lint(tmp_path):
    installed = _install(tmp_path)
    _sb, _h, plist, _d = installed
    r = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_k2_install_makes_exactly_bootout_then_bootstrap(tmp_path):
    installed = _install(tmp_path)
    sb, home, plist, _d = installed
    calls = sb.launchctl_calls()
    # #3939: the installer waits for the service to be gone between the two (`print` polls)
    assert [c for c in calls if c[0] != "print"] == [
        ["bootout", f"gui/{UID}/{label_for(home)}"],
        ["bootstrap", f"gui/{UID}", str(plist)],
    ], calls
    verbs = [c[0] for c in calls]
    for i, v in enumerate(verbs):
        if v == "print":
            assert verbs.index("bootout") < i < verbs.index("bootstrap"), verbs


@pytest.mark.parametrize("char", ["<", ">"])
def test_k2_xml_metacharacter_in_the_home_is_refused_before_anything_is_written(tmp_path, char):
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    home = sb.tmp / f"se{char}at"
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "unsupported in --owner mode" in r.stderr, r.stderr
    sb.assert_nothing_written(home)


def test_k2_control_existing_refusals_stay_on_darwin(tmp_path):
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    home = sb.tmp / "se at"
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode == 2 and "unsupported in --owner mode" in r.stderr, (r.returncode, r.stderr)
    sb.assert_nothing_written(home)


# ---- B (#3780): the CLASS of control bytes is refused, not a list of members -------------------
# The guard's own comment says why (the plist is XML; the unit is read by systemd); whatever a
# `case` list of members does not name falls through.  So every C0 byte 0x01..0x1F plus DEL 0x7F
# is tested, on both platform arms, under the default env and under a hostile locale env (the
# class arm pins LC_ALL=C: `[[:cntrl:]]` is locale-dependent).  NUL (0x00) cannot be passed in an
# argv/env string, so it is not here.
C0_BYTES = [*range(0x01, 0x20), 0x7F]
HOSTILE_LOCALE = {"LANG": "tr_TR.UTF-8", "LC_CTYPE": "ja_JP.eucJP", "LC_COLLATE": "tr_TR.UTF-8"}
LOCALE_ENVS = {"default-env": ((), {}), "hostile-locale": (("LC_ALL",), HOSTILE_LOCALE)}

# (#3780) The interpreter is passed EXPLICITLY, never via PATH: the stock macOS /bin/bash 3.2 mangles
# a multibyte path under a multibyte locale (its internal \001 escape byte leaks into `pwd -P`'s
# output), a modern bash does not, so a cell that takes "whatever bash PATH finds" proves nothing.
_BREW_BASH = next((b for b in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash") if os.access(b, os.X_OK)), None)
BASHES = [
    pytest.param("/bin/bash", id="bin-bash",
                 marks=pytest.mark.skipif(not os.access("/bin/bash", os.X_OK), reason="no /bin/bash")),
    pytest.param(_BREW_BASH or "/opt/homebrew/bin/bash", id="brew-bash",
                 marks=pytest.mark.skipif(_BREW_BASH is None,
                                          reason="no brew bash (/opt/homebrew/bin/bash, /usr/local/bin/bash)")),
]


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("env_id", list(LOCALE_ENVS))
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
@pytest.mark.parametrize("byte", C0_BYTES, ids=lambda b: f"0x{b:02X}")
def test_k2b_every_c0_byte_and_del_in_the_home_is_refused_before_anything_is_written(tmp_path, byte, os_name, env_id, bash):
    sb = DSandbox(tmp_path / "i", os_name=os_name)
    home = sb.tmp / f"h{chr(byte)}x"
    drop, extra = LOCALE_ENVS[env_id]
    r = sb.run_env("--owner", engram_home=str(home), drop=drop, extra=extra, bash=bash)
    assert r.returncode == 2,(r.returncode, r.stdout, r.stderr)
    assert "unsupported in --owner mode" in r.stderr, r.stderr
    assert f"control byte 0x{byte:02X}" in r.stderr, r.stderr
    assert r.stderr.count("ERROR") == 1, r.stderr
    if os_name == "Darwin":
        sb.assert_nothing_written(home)
    else:
        assert not sb.unit_dir.exists() or list(sb.unit_dir.iterdir()) == [], "a (partial) unit was written"
        assert sb.systemctl_calls() == [], "something was loaded"
        assert not home.exists(), "the bad home was created"


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_k2b_c1_char_in_the_home_is_not_a_control_byte_under_a_utf8_caller_locale(tmp_path, os_name, bash):
    """Pins the `local LC_ALL=C` in `_owner_bad_char`: U+0085 (bytes c2 85) matches `[[:cntrl:]]` under
    a UTF-8 locale but not under C.  The ruled class is 0x01-0x1F and 0x7F; U+0085 is a legal XML 1.0
    char.  The caller env is forced to a UTF-8 locale, so only the pin keeps it accepted."""
    sb = DSandbox(tmp_path / "i", os_name=os_name)
    home = sb.tmp / "a\u0085b"
    r = sb.run_env("--owner", engram_home=str(home), extra={"LC_ALL": "en_US.UTF-8"}, bash=bash)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "control byte" not in r.stderr, r.stderr


@pytest.mark.parametrize("other", ["<", "{{"])
def test_k2b_a_value_with_a_control_byte_and_a_later_arm_character_reports_the_control_byte(tmp_path, other):
    """Position: the class arm sits before the `{{` / `<` arms, as the arms it replaced did."""
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    home = sb.tmp / f"h{other}\x07x"
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode == 2 and "control byte 0x07" in r.stderr, (r.returncode, r.stderr)
    sb.assert_nothing_written(home)


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
@pytest.mark.parametrize("env_id", list(LOCALE_ENVS))
def test_k2b_control_non_ascii_and_ordinary_names_are_not_taken_for_control_bytes(tmp_path, os_name, env_id, bash):
    """Guard against an over-wide class: bytes >= 0x80 (a UTF-8 name) are not control bytes, in any
    locale; the install goes through."""
    sb = DSandbox(tmp_path / "i", os_name=os_name)
    drop, extra = LOCALE_ENVS[env_id]
    r = sb.run_env("--owner", engram_home=str(sb.tmp / "café-seat"), drop=drop, extra=extra,
                   bash=bash)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "control byte" not in r.stderr, r.stderr


# (premade dirs, ENGRAM_HOME relative to the sandbox tmp).  The installer resolves the DEEPEST EXISTING
# ancestor through `pwd -P`, so the three layouts reach that capture with a non-ASCII path in three
# different places: nowhere (only the not-yet-existing remainder is non-ASCII), the home itself, and a
# parent of the home.
LABEL_LAYOUTS = {
    "fresh-home": ((), "café-seat"),
    "existing-home": (("café-seat",), "café-seat"),
    "existing-utf8-parent": (("café-p",), "café-p/seat"),
}


@pytest.mark.parametrize("layout", list(LABEL_LAYOUTS))
@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("env_id", list(LOCALE_ENVS))
def test_k2b_utf8_home_gets_the_same_label_from_the_installer_and_the_hook(tmp_path, env_id, bash, layout):
    """(#3780) The installer and the start hook both compute the launchd label from the home's
    PHYSICAL path.  Under a multibyte locale the stock bash 3.2 mangles that path (\\001 after each byte
    of a multibyte char) unless the path step is pinned to LC_ALL=C.  Both callers must land on the
    label computed from the TRUE physical path (computed here by Python, not by bash): comparing the
    two bash sides to each other alone would pass if both were mangled the same way."""
    drop, extra = LOCALE_ENVS[env_id]
    premade, rel = LABEL_LAYOUTS[layout]
    sb = DSandbox(tmp_path / "inst", os_name="Darwin")
    for d in premade:
        (sb.tmp / d).mkdir()
    r = sb.run_env("--owner", engram_home=str(sb.tmp / rel), drop=drop, extra=extra, bash=bash)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    plists = sorted(p.name for p in sb.agents_dir.glob("*.plist"))
    assert plists == [f"{label_for(sb.tmp / rel)}.plist"], plists

    seat = DSeat(tmp_path / "hook")
    home = seat.tmp / rel
    home.mkdir(parents=True)  # the hook never creates a home: it must exist
    (home / "config.json").write_text(json.dumps({"engramd": FLAG_ON}))
    seat.set_os("Darwin")
    seat.launchctl("notfound")
    seat.run({"ENGRAM_HOME": str(home), **extra}, bash=bash)
    asked = [ln for ln in seat.launchctl_log() if ln.startswith("print ")]
    assert asked == [f"print gui/{UID}/{label_for(home)}"], (asked, label_for(home))


@pytest.mark.parametrize("mode", [(), ("--owner",)], ids=["surface-daemon", "owner"])
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_k2c_template_render_is_byte_identical_under_a_hostile_multibyte_locale(tmp_path, os_name, mode):
    """(#3794) BSD `sed` rejects bytes that are not valid in the caller's multibyte locale
    (`RE error: illegal byte sequence`) -- the Linux unit template carries 3 non-ASCII bytes, and a
    UTF-8 home / plugin path is non-ASCII too.  The render must be the same bytes under the hostile
    locale as under LC_ALL=C, on the stock /bin/bash, for both templates and both modes."""
    if not os.access("/bin/bash", os.X_OK):
        pytest.skip("no /bin/bash")
    # ONE sandbox, rendered twice in place: the owner label embeds a hash of the physical home, so two
    # homes would differ for a reason that has nothing to do with sed.
    sb = DSandbox(tmp_path / "inst", os_name=os_name, plugin_name="plügin-é", home_name="hóme-é")
    home = sb.tmp / "seat-é"
    dest = sb.agents_dir if os_name == "Darwin" else sb.unit_dir
    renders = {}
    for env_id, (drop, extra) in {"c": ((), {"LC_ALL": "C"}), "hostile": (("LC_ALL",), HOSTILE_LOCALE)}.items():
        r = sb.run_env(*mode, engram_home=str(home), drop=drop, extra=extra, bash="/bin/bash")
        assert r.returncode == 0, (env_id, r.returncode, r.stdout, r.stderr)
        files = sorted(dest.glob("*.plist" if os_name == "Darwin" else "*.service"))
        assert len(files) == 1, files
        renders[env_id] = files[0].read_bytes()
        files[0].unlink()
    assert "hóme-é".encode() in renders["c"] or "plügin-é".encode() in renders["c"], "no UTF-8 path was rendered"
    assert renders["hostile"] == renders["c"]


# ===========================================================================
# K4 -- the Darwin fallback, four outcomes (Q3), fake launchctl from REAL captures
# ===========================================================================

def _darwin_hook(seat: DSeat, mode: str | None, flags: dict | None = None):
    seat.config(flags if flags is not None else FLAG_ON)
    if mode is not None:
        seat.launchctl(mode)
    return seat.run_timed(limit=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS + 10)


def _lines_of(got, seat: DSeat) -> list:
    """The hook's additionalContext lines, with the seat's tmp path masked: the path carries the
    TEST NAME (e.g. `..._ambiguous_...`) and must never satisfy a keyword assertion by accident."""
    rc, out, _err, _t = got

    class R:
        stdout = out
    masked = []
    for ln in context_lines(R):
        for p in sorted({str(seat.tmp), _physical(seat.tmp)}, key=len, reverse=True):
            ln = ln.replace(p, "<TMP>")
        masked.append(ln)
    return masked


def test_k4_running_launches_nothing_and_says_nothing(dseat):
    got = _darwin_hook(dseat, "running")
    assert got[0] == 0 and got[1].strip() == "", got
    assert dseat.launched() == []
    assert dseat.launchctl_log() == [dseat.expected_print()], "asked launchctl about the wrong label"


@pytest.mark.parametrize("mode,state,last", [("not_running", "not running", "0"),
                                             ("spawn_scheduled", "spawn scheduled", "1")])
def test_k4_registered_launches_nothing_and_names_state_and_last_exit(dseat, mode, state, last):
    got = _darwin_hook(dseat, mode)
    lines = _lines_of(got, dseat)
    assert got[0] == 0 and dseat.launched() == []
    assert len(lines) == 1, got[1]
    assert f"registered but {state}" in lines[0], lines[0]
    assert f"last exit {last}" in lines[0], lines[0]
    assert "leaving it to launchd" in lines[0], lines[0]
    assert "UNSUPERVISED" not in got[1]
    assert dseat.launchctl_log() == [dseat.expected_print()]


def test_k4_notfound_launches_unsupervised_with_the_one_line(dseat):
    got = _darwin_hook(dseat, "notfound")
    lines = _lines_of(got, dseat)
    assert got[0] == 0
    assert len(dseat.launched()) == 1, "service-not-found must fall back to the unsupervised launch"
    assert len(lines) == 1 and "UNSUPERVISED" in lines[0], got[1]
    assert dseat.launchctl_log() == [dseat.expected_print()]


@pytest.mark.parametrize("mode", ["empty", "nostate", "rc113_notext", "other_rc"])
def test_k4_ambiguous_answer_launches_nothing_and_says_so(dseat, mode):
    got = _darwin_hook(dseat, mode)
    lines = _lines_of(got, dseat)
    assert got[0] == 0 and dseat.launched() == [], (mode, got)
    assert len(lines) == 1 and "ambiguous" in lines[0], (mode, got[1])
    assert "UNSUPERVISED" not in got[1]
    assert dseat.launchctl_log() == [dseat.expected_print()]


def test_k4_timeout_launches_nothing_and_returns_within_the_bound(dseat):
    dseat.config(FLAG_ON)
    dseat.launchctl("timeout")  # the fake `exec sleep 30`s, far past the bound
    got = dseat.run_timed(limit=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS)  # pytest.fail if it blocks
    lines = _lines_of(got, dseat)
    assert got[3] < LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS
    assert dseat.launched() == []
    assert len(lines) == 1 and "ambiguous" in lines[0], got[1]
    assert dseat.launchctl_log() == [dseat.expected_print()], "asked launchctl nothing / the wrong thing"


def test_k4_launchctl_missing_from_path_is_ambiguous_not_the_no_bus_case(dseat):
    got = _darwin_hook(dseat, None)  # no launchctl stub on the hermetic PATH
    lines = _lines_of(got, dseat)
    assert got[0] == 0 and dseat.launched() == [], got
    assert len(lines) == 1 and "ambiguous" in lines[0], got[1]
    assert "UNSUPERVISED" not in got[1]


@pytest.mark.parametrize("mode,verdict", [
    ("running", "running"),
    ("not_running", "registered:not running:0"),
    ("not_running_nolast", "registered:not running:none"),
    ("spawn_scheduled", "registered:spawn scheduled:1"),
    ("notfound", "notfound"),
])
def test_k4_verb_prints_the_verdict(dseat, mode, verdict):
    dseat.launchctl(mode)
    assert dseat.io(LAUNCHD_VERB, dseat.label()) == verdict


@pytest.mark.parametrize("mode", ["empty", "nostate", "rc113_notext", "other_rc"])
def test_k4_verb_ambiguous_verdicts(dseat, mode):
    dseat.launchctl(mode)
    assert dseat.io(LAUNCHD_VERB, dseat.label()).startswith("ambiguous:")


def test_k4_verb_timeout_is_ambiguous_within_the_bound(dseat):
    dseat.launchctl("timeout")
    t0 = time.monotonic()
    out = dseat.io(LAUNCHD_VERB, dseat.label(), timeout=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS)
    assert out.startswith("ambiguous:"), out
    assert time.monotonic() - t0 < LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS


def test_k4_verb_takes_the_top_level_state_line_not_a_nested_one(dseat):
    """The nested coalition dicts carry `state = active`; a parser that takes the LAST or ANY
    `state =` line would report that, not the job's own state."""
    dseat.launchctl("spawn_scheduled")
    out = dseat.io(LAUNCHD_VERB, dseat.label())
    assert "active" not in out and out.startswith("registered:spawn scheduled"), out


# ===========================================================================
# A (#3780) -- rc 125 is its own named state: no_gui_domain
# ===========================================================================
# rc 125 from `launchctl print gui/<uid>/...` means the DOMAIN is absent (no GUI login session), so a
# bootstrap into it cannot work.  Not `notfound` (that path launches), not a bare ambiguous (that
# neither launches nor explains).  Real capture: tests/fixtures/launchctl_3709/no_gui_domain.*

NO_GUI_ERR = (FIXTURES / "no_gui_domain.err").read_text(encoding="utf-8")
NO_GUI_RC = int((FIXTURES / "no_gui_domain.rc").read_text(encoding="utf-8"))
NO_GUI_TEXT = "Domain does not support specified action"


def test_a2_the_fixture_is_the_real_capture():
    assert NO_GUI_RC == 125, NO_GUI_RC
    assert NO_GUI_ERR == f"Could not print domain: 125: {NO_GUI_TEXT}\n", repr(NO_GUI_ERR)


def test_a1_parser_rc125_with_the_domain_text_is_no_gui_domain():
    assert _load_parser()(NO_GUI_RC, "", NO_GUI_ERR) == "no_gui_domain"


def test_a1_parser_rc125_without_the_text_stays_ambiguous():
    """Both the rc AND the text are required, as for rc 113."""
    parse = _load_parser()
    assert parse(125, "", "") == "ambiguous:launchctl print exited 125"
    assert parse(125, "", "Bad request.\n") == "ambiguous:launchctl print exited 125"


@pytest.mark.parametrize("rc,expected_prefix", [
    (113, "ambiguous:launchctl exited 113 without the not-found text"),
    (1, "ambiguous:launchctl print exited 1"),
    (2, "ambiguous:launchctl print exited 2"),
])
def test_a1_parser_the_text_with_any_other_rc_is_unchanged(rc, expected_prefix):
    assert _load_parser()(rc, "", NO_GUI_ERR) == expected_prefix


def test_a1_parser_the_text_with_rc0_is_unchanged():
    assert _load_parser()(0, "", NO_GUI_ERR) == "ambiguous:launchctl print answered with empty output"


def test_a1_notfound_is_unchanged():
    nf = (FIXTURES / "notfound.err").read_text(encoding="utf-8")
    assert _load_parser()(113, "", nf) == "notfound"


def test_a1_verb_prints_no_gui_domain(dseat):
    dseat.launchctl("no_gui_domain")
    assert dseat.io(LAUNCHD_VERB, dseat.label()) == "no_gui_domain"


# ---- #3823: a flag-ON seat with no GUI domain gets a working owner or a loud named refusal --------
# Contract C1-C4 (engram-alpha#3823).  C1(a): the unsupervised launch, as on Linux no-bus / WSL.
# C1(b): when that launch fails, one line led by OWNER REFUSED naming every ON owner flag, the exact
# recovery, and that the stop hook blocks.  "not used" alone never appears while a flag is on.

NO_GUI_REASON = "launchd: no GUI domain"
WITH_STOP = {**FLAG_ON, "stop_reads_via_owner": True}  # the stop hook's flag is ON: its sentence is true


def _only_print(dseat):
    assert dseat.launchctl_log() == [dseat.expected_print()], "something other than the one print was asked"


def test_c1a_no_gui_domain_launches_unsupervised_with_the_reason(dseat):
    got = _darwin_hook(dseat, "no_gui_domain")
    lines = _lines_of(got, dseat)
    assert got[0] == 0, got
    assert len(dseat.launched()) == 1, "a flag-ON seat with no GUI domain must get an owner launch"
    assert len(lines) == 1 and lines[0].startswith("UNSUPERVISED"), (lines, got[1])
    assert NO_GUI_REASON in lines[0], lines[0]
    assert "not used" not in got[1] and "OWNER REFUSED" not in got[1], got[1]
    _only_print(dseat)  # nothing is bootstrapped: the domain does not exist


def _fail_launch(dseat):
    dseat.fail_flag.write_text("x")


def test_c1b_failed_launch_is_a_named_refusal_that_says_the_readers_will_block(dseat):
    _fail_launch(dseat)
    got = _darwin_hook(dseat, "no_gui_domain", WITH_STOP)
    lines = _lines_of(got, dseat)
    assert got[0] == 0, got
    assert len(lines) == 1, got[1]
    line = lines[0]
    assert line.startswith("OWNER REFUSED: no launchd GUI domain"), line
    assert "user_identity_reads_via_owner" in line and "stop_reads_via_owner" in line, "the refusal names the ON flags"
    assert "stop hook blocks every stop" in line, "C4: the wording must agree with what the stop hook does"
    assert "set them false in config.json" in line, "the recovery by flag"
    assert "GUI login" in line or "GUI session" in line, "the recovery by logging in"
    assert "not used" not in got[1] and "UNSUPERVISED" not in got[1], got[1]
    assert any("OWNER REFUSED" in ln for ln in dseat.log_lines()), "the refusal is also logged"
    _only_print(dseat)


def test_c1b_the_stop_hook_sentence_is_only_said_when_the_stop_flag_is_on(dseat):
    """The stop hook is the one reader whose behaviour C4 verifies (it blocks every stop). A seat whose ON
    flags do not include `stop_reads_via_owner` must not be told its stop hook blocks."""
    _fail_launch(dseat)
    no_stop = _lines_of(_darwin_hook(dseat, "no_gui_domain", FLAG_ON), dseat)[0]
    assert "stop hook" not in no_stop and "The flagged readers have no owner until one answers." in no_stop, no_stop
    with_stop = _lines_of(_darwin_hook(dseat, "no_gui_domain", WITH_STOP), dseat)[0]
    assert "The stop hook blocks every stop until an owner answers." in with_stop, with_stop
    # a flag whose name merely CONTAINS the stop flag's must not trigger the sentence
    lookalike = {**FLAG_ON, "pre_stop_reads_via_owner": True}
    other = _lines_of(_darwin_hook(dseat, "no_gui_domain", lookalike), dseat)[0]
    assert "stop hook blocks" not in other and "pre_stop_reads_via_owner" in other, other


def test_c1b_the_refusal_names_every_on_flag_and_only_those(dseat):
    _fail_launch(dseat)
    dseat.config({"user_identity_reads_via_owner": True, "stop_reads_via_owner": True,
                  "tripwire_reads_via_owner": False, "stop_reads_via_owner_note": True,
                  "unrelated": True, "session_start_reads_via_owner": "true"})
    dseat.launchctl("no_gui_domain")
    got = dseat.run_timed(limit=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS + 10)
    line = _lines_of(got, dseat)[0]
    assert line.startswith("OWNER REFUSED"), line
    assert "stop_reads_via_owner" in line and "user_identity_reads_via_owner" in line, line
    assert "tripwire_reads_via_owner" not in line and "session_start_reads_via_owner" not in line, line
    assert "unrelated" not in line and "_note" not in line, line


def test_c1b_a_missing_owner_script_is_the_same_refusal(dseat):
    (dseat.plugin / "engramd_owner.py").unlink()
    got = _darwin_hook(dseat, "no_gui_domain", WITH_STOP)
    lines = _lines_of(got, dseat)
    assert got[0] == 0 and dseat.launched() == [], got
    assert len(lines) == 1 and lines[0].startswith("OWNER REFUSED: no launchd GUI domain"), got[1]
    assert "stop hook blocks every stop" in lines[0] and "not used" not in lines[0], lines[0]


def test_c1b_an_owner_that_dies_within_a_second_is_the_same_refusal(dseat):
    """The launch is spawned but the owner exits (the stub exits 7): not a running owner."""
    _fail_launch(dseat)
    got = _darwin_hook(dseat, "no_gui_domain")
    assert len(dseat.launched()) == 1, "the launch was attempted"
    assert _lines_of(got, dseat)[0].startswith("OWNER REFUSED"), got[1]


def test_c1b_a_launch_that_cannot_spawn_is_the_same_refusal(dseat):
    """The I/O helper's `launch` verb answers `fail` (the spawn raised OSError): the third failure site."""
    venv_bin = dseat.home / ".engram" / "venv" / "bin"
    _stub(venv_bin, "python3", textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for a in "$@"; do case "$a" in
          *engramd_owner_flags.py) exec "{sys.executable}" "$@";;
          *_engramd_owner_hook_io.py) if [ "$2" = "launch" ]; then echo fail; exit 0; fi; exec "{sys.executable}" "$@";;
        esac; done
        exit 1
        """))
    got = _darwin_hook(dseat, "no_gui_domain", WITH_STOP)
    lines = _lines_of(got, dseat)
    assert got[0] == 0, got
    assert len(lines) == 1 and lines[0].startswith("OWNER REFUSED: no launchd GUI domain"), got[1]
    assert "Owner launch failed: the launch failed." in lines[0] and "stop hook blocks every stop" in lines[0], lines[0]


def _deep_home(base: Path, chars: int = 330) -> Path:
    """A real, creatable home path of about `chars` characters (PATH_MAX-safe on macOS, unlike _long_home)."""
    home = base
    while len(str(home)) < chars:
        home = home / ("d" * 40)
    home.mkdir(parents=True)
    return home


def test_c1b_the_promises_sit_in_the_head_that_the_600_char_clip_keeps(dseat):
    """report() keeps ~298 chars at each end of a message over 600 and cuts the middle (_HEAD_CLIP). The
    promises -- flags, recovery, what the stop hook does -- must be in the first 298, so a long home or a
    long tmp path (which only ever lengthen the TAIL) cannot lose them.  Three flags is the realistic
    worst case."""
    _fail_launch(dseat)
    flags = {"stop_reads_via_owner": True, "lesson_tripwire_reads_via_owner": True,
             "session_start_reads_via_owner": True}
    home = _deep_home(dseat.tmp / "deep")
    (home / "config.json").write_text(json.dumps({"engramd": flags}))
    dseat.launchctl("no_gui_domain")
    rc, out, err, _ = dseat.run_timed({"ENGRAM_HOME": str(home)}, limit=20)
    assert rc == 0, err[-300:]
    logged = [ln for ln in (home / "engramd-owner.log").read_text(encoding="utf-8").splitlines() if "OWNER REFUSED" in ln]
    assert len(logged) == 1, logged
    msg = logged[0].split("[start-engramd-owner] ", 1)[1]
    assert len(msg) > 600, "the cell must be on the clipped side of the bound to mean anything"
    head = msg[:298]
    for flag in flags:
        assert flag in head, (flag, head)
    assert "log in to a GUI session" in head and "set them false in config.json" in head, head
    assert "stop hook blocks every stop" in head, head
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    for flag in flags:
        assert flag in ctx, "the delivered context lost a flag name"
    assert "stop hook blocks every stop" in ctx and "log in to a GUI session" in ctx, ctx


@pytest.mark.parametrize("platform", [None, "claude-code"], ids=["floor-4000", "claude-code-9500"])
def test_c1b_the_refusal_for_a_very_long_home_keeps_its_lead_words_and_its_bound(dseat, platform):
    from test_owner_launcher_3663 import _long_home
    if platform:
        (dseat.plugin / "platform.json").write_text(json.dumps({"platform": platform}))
    _fail_launch(dseat)
    home = _long_home(dseat.tmp)
    (home / "config.json").write_text(json.dumps({"engramd": WITH_STOP}))
    dseat.launchctl("no_gui_domain")
    rc, out, err, _ = dseat.run_timed({"ENGRAM_HOME": str(home)}, limit=20)
    assert rc == 0, err[-300:]
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 1000, len(ctx)
    assert "OWNER REFUSED" in ctx and "stop hook blocks every stop" in ctx and "user_identity_reads_via_owner" in ctx, ctx[:400]


def test_c1b_flags_helper_names_exactly_the_on_flags_and_agrees_with_any_on(tmp_path):
    """`--names` and `any_owner_flag_on` are one rule: the names are non-empty iff the helper says `on`."""
    helper = SRC / "engramd_owner_flags.py"
    cases = [({"b_via_owner": True, "a_via_owner": True, "c_via_owner": False}, "a_via_owner,b_via_owner", "on"),
             ({"a_via_owner": "true", "a_via_owner_x": True, "plain": True}, "", "off"),
             ({}, "", "off")]
    for i, (engramd, names, verdict) in enumerate(cases):
        home = tmp_path / f"h{i}"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"engramd": engramd}))
        got = subprocess.run([sys.executable, str(helper), str(home), "--names"], capture_output=True, encoding="utf-8")
        assert got.returncode == 0 and got.stdout.strip() == names, (engramd, got)
        v = subprocess.run([sys.executable, str(helper), str(home)], capture_output=True, encoding="utf-8")
        assert v.stdout.strip() == verdict, (engramd, v)


def test_c3_a_gui_domain_is_unchanged(dseat):
    """Control: with a GUI domain the supervised paths are exactly as before (running: silent;
    notfound: the UNSUPERVISED launch with the launchd answer, not the no-GUI reason)."""
    got = _darwin_hook(dseat, "running")
    assert got[0] == 0 and got[1].strip() == "" and dseat.launched() == [], got
    nf = _darwin_hook(dseat, "notfound")
    lines = _lines_of(nf, dseat)
    assert len(dseat.launched()) == 1 and lines[0].startswith("UNSUPERVISED"), nf
    assert NO_GUI_REASON not in lines[0], lines[0]


def test_c4_the_reader_really_blocks_when_the_flag_is_on_and_the_owner_is_down():
    """C4 drift guard: the refusal says the stop hook blocks.  The stop hook's own docstring is where
    that behaviour is stated; if it stops saying so, the refusal wording is a lie and this fails."""
    text = (SRC / "hooks" / "claude" / "engram-stop-hook.py").read_text(encoding="utf-8")
    assert "never falls back" in text, "the stop hook no longer documents the no-fallback behaviour"


def test_a3_hook_with_the_flags_off_still_says_nothing(dseat):
    """Control: the gate on the owner flags is before the launchd question; a seat that never asked
    for an owner hears nothing about its missing GUI domain."""
    dseat.launchctl("no_gui_domain")
    got = dseat.run_timed(limit=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS + 10)
    assert got[0] == 0 and got[1].strip() == "" and dseat.launchctl_log() == [], got


def _fail_bootstrap_with(tmp_path, rc: int, os_name: str = "Darwin", mode_args: tuple = ("--owner",)):
    sb = DSandbox(tmp_path / "i", os_name=os_name)
    sb.bootstrap_rc(rc)
    home = sb.tmp / "seat-a"
    r = sb.run(*mode_args, engram_home=str(home))
    return sb, home, r


def test_a4_installer_on_bootstrap_125_exits_3_with_the_named_message_and_the_plist_stays(tmp_path):
    sb, home, r = _fail_bootstrap_with(tmp_path, 125)
    plist = sb.agents_dir / f"{label_for(home)}.plist"
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (f"ERROR: launchd has no GUI domain for uid {UID} (no GUI login session). {plist} was written but "
            f"is not active now; whether launchd loads it at this account's next GUI login has not been "
            f"measured. The owner is not supervised on this seat.") in r.stderr.splitlines(), r.stderr
    assert plist.exists(), "the plist is said to be written, so it must be"
    assert "Loaded." not in r.stdout and "Setup complete" not in r.stdout, r.stdout


def test_a4_installer_on_any_other_bootstrap_failure_keeps_launchctls_rc(tmp_path):
    sb, home, r = _fail_bootstrap_with(tmp_path, 5)
    assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)
    assert "no GUI domain" not in r.stderr, r.stderr
    # #3939: retried (5 attempts in all) and the last attempt is reported loudly, with the manual command
    assert [c[0] for c in sb.launchctl_calls()].count("bootstrap") == 5, sb.launchctl_calls()
    assert "failed on all 5 attempts" in r.stderr and "Bootstrap failed: 5" in r.stderr, r.stderr


def test_a4_installer_on_bootstrap_0_is_unchanged(tmp_path):
    sb, home, r = _fail_bootstrap_with(tmp_path, 0)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Loaded." in r.stdout and "no GUI domain" not in r.stderr
    assert [c[0] for c in sb.launchctl_calls() if c[0] != "print"] == ["bootout", "bootstrap"]   # #3939: `print` polls aside


def test_a4_the_surface_daemon_path_shares_the_bootstrap_and_gets_the_same_handling_in_generic_words(tmp_path):
    """Without --owner the same bootstrap call runs.  125 there is the same absent domain; the
    message names the surface daemon, not the owner."""
    sb, _home, r = _fail_bootstrap_with(tmp_path, 125, mode_args=())
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert f"launchd has no GUI domain for uid {UID} (no GUI login session)" in r.stderr, r.stderr
    assert "The surface daemon is not supervised on this seat." in r.stderr, r.stderr
    assert "The owner" not in r.stderr, r.stderr


def test_a5_r1_skip_keys_on_the_parser_verdict_not_a_raw_125(monkeypatch):
    # (a) the real capture: skip, with the named reason
    monkeypatch.setattr(sys.modules[__name__], "_real_print", lambda label: (NO_GUI_RC, "", NO_GUI_ERR))
    reason = _no_gui_domain_skip_reason("com.engram.test.x")
    assert reason and "no GUI launchd domain" in reason and "no_gui_domain" in reason, reason
    # (b) a bare 125 (no domain text) is ambiguous, not the named state: no skip
    monkeypatch.setattr(sys.modules[__name__], "_real_print", lambda label: (125, "", ""))
    assert _no_gui_domain_skip_reason("com.engram.test.x") is None
    # (c) the normal not-found of a GUI seat: no skip
    nf = (FIXTURES / "notfound.err").read_text(encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_real_print", lambda label: (113, "", nf))
    assert _no_gui_domain_skip_reason("com.engram.test.x") is None


def test_a5_r1_leak_check_reads_the_registry_list_not_the_print_verdict():
    out = ("PID\tStatus\tLabel\n"
           "-\t0\tcom.apple.thing\n"
           "123\t0\tcom.engram.test.r1-sleep-9-abc\n"
           "-\t1\tcom.engram.test.r1-exit1-9-abc-other\n")
    mine = ["com.engram.test.r1-sleep-9-abc", "com.engram.test.r1-exit1-9-abc", "com.engram.test.r1-exit0-9-abc"]
    # exact label match: a longer label that merely starts with mine is not mine
    assert _leaked_labels(mine, out) == ["com.engram.test.r1-sleep-9-abc"]
    assert _leaked_labels(mine, "PID\tStatus\tLabel\n-\t0\tcom.apple.thing\n") == []
    assert _leaked_labels([], out) == []


# ===========================================================================
# K5 -- liveness on engram_pidfile's strict mode (Q4), both platforms
# ===========================================================================

@contextlib.contextmanager
def live_process(argv):
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    try:
        time.sleep(0.3)
        yield p
    finally:
        p.kill()
        p.wait()


NON_OWNER = ["/bin/sleep", "300"]
OWNER_LIKE = [sys.executable, "-c", "import time; time.sleep(300)", "engramd_owner"]


def write_pidfile(home: Path, pid, *, process_is_older: bool) -> Path:
    """`process_is_older`: the process started BEFORE the pidfile's mtime (the default rule then
    reads it alive without ever looking at its argv).  False: it started long AFTER."""
    p = home / "engramd.pid"
    p.write_text(f"{pid}\n")
    t = time.time() + (3600 if process_is_older else -3600)
    os.utime(p, (t, t))
    return p


def test_k5_default_rule_is_unchanged_for_existing_callers(tmp_path):
    """Control (passes before AND after): without the strict keyword, a live non-owner older than
    the pidfile reads ALIVE and a younger one reads dead."""
    with live_process(NON_OWNER) as p:
        older = write_pidfile(tmp_path, p.pid, process_is_older=True)
        assert ep.recorded_live_pid(older, "engramd_owner") == p.pid
        younger = write_pidfile(tmp_path, p.pid, process_is_older=False)
        assert ep.recorded_live_pid(younger, "engramd_owner") is None


@pytest.mark.parametrize("older", [True, False], ids=["older-than-pidfile", "younger-than-pidfile"])
def test_k5_strict_mode_live_non_owner_reads_none(tmp_path, older):
    with live_process(NON_OWNER) as p:
        pf = write_pidfile(tmp_path, p.pid, process_is_older=older)
        assert ep.recorded_live_pid(pf, "engramd_owner", **{STRICT_KW: True}) is None


@pytest.mark.parametrize("older", [True, False], ids=["older-than-pidfile", "younger-than-pidfile"])
def test_k5_strict_mode_owner_argv_reads_alive(tmp_path, older):
    with live_process(OWNER_LIKE) as p:
        pf = write_pidfile(tmp_path, p.pid, process_is_older=older)
        assert ep.recorded_live_pid(pf, "engramd_owner", **{STRICT_KW: True}) == p.pid


def test_k5_strict_mode_unreadable_cmdline_keeps_the_conservative_direction(tmp_path, monkeypatch):
    """Unreadable cmdline -> alive (the hook's rule today: never guess dead)."""
    with live_process(NON_OWNER) as p:
        monkeypatch.setattr(ep, "_read_process_cmdline", lambda pid: None)
        for older in (True, False):
            pf = write_pidfile(tmp_path, p.pid, process_is_older=older)
            assert ep.recorded_live_pid(pf, "engramd_owner", **{STRICT_KW: True}) == p.pid, older


def test_k5_strict_mode_dead_pid_and_garbage_read_none(tmp_path):
    q = subprocess.Popen(NON_OWNER)
    q.kill()
    q.wait()
    for content in (f"{q.pid}\n", "garbage", "", "0", "-5"):
        pf = tmp_path / "engramd.pid"
        pf.write_text(content)
        assert ep.recorded_live_pid(pf, "engramd_owner", **{STRICT_KW: True}) is None, content


# ---- the hook's `live` verb on top of it ----------------------------------------------

def test_k5a_live_non_owner_started_after_the_pidfile_reads_none(tmp_path):
    seat = DSeat(tmp_path / "s")
    with live_process(NON_OWNER) as p:
        write_pidfile(seat.eh, p.pid, process_is_older=False)
        assert seat.io("live", seat.eh) == "none"


def test_k5b_live_non_owner_started_BEFORE_the_pidfile_reads_none(tmp_path):
    """Stays red under engram_pidfile's DEFAULT rule (older non-owner reads alive there): only the
    named strict mode satisfies it.  The shape is reachable without malice: a plain `cp` of a home
    rewrites the pidfile's mtime."""
    seat = DSeat(tmp_path / "s")
    with live_process(NON_OWNER) as p:
        write_pidfile(seat.eh, p.pid, process_is_older=True)
        assert seat.io("live", seat.eh) == "none"


@pytest.mark.parametrize("older", [True, False], ids=["older-than-pidfile", "younger-than-pidfile"])
def test_k5c_live_owner_argv_reads_alive(tmp_path, older):
    seat = DSeat(tmp_path / "s")
    with live_process(OWNER_LIKE) as p:
        write_pidfile(seat.eh, p.pid, process_is_older=older)
        assert seat.io("live", seat.eh) == "alive"


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink-to-fifo"])
def test_k5d_non_regular_pidfile_is_reported_and_never_blocks(tmp_path, kind):
    seat = DSeat(tmp_path / "s")
    pf = seat.eh / "engramd.pid"
    if kind == "fifo":
        os.mkfifo(pf)
    elif kind == "directory":
        pf.mkdir()
    else:
        os.mkfifo(seat.eh / "elsewhere.fifo")
        pf.symlink_to(seat.eh / "elsewhere.fifo")
    assert seat.io("live", seat.eh, timeout=5) == f"notregular:{kind}"


def _load_io_module(monkeypatch, spy):
    monkeypatch.syspath_prepend(str(HOOK_IO.parent))
    monkeypatch.setitem(sys.modules, "engram_pidfile", spy)
    spec = importlib.util.spec_from_file_location("_engramd_owner_hook_io_3709", HOOK_IO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PidfileSpy:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def recorded_live_pid(self, path, program_marker=None, **kw):
        self.calls.append((str(path), program_marker, kw))
        return self.result

    def recorded_pid_is_alive(self, path, program_marker=None, **kw):
        self.calls.append((str(path), program_marker, kw))
        return self.result is not None


@contextlib.contextmanager
def alarm(seconds: int):
    def _boom(*_a):
        raise AssertionError(f"blocked for more than {seconds}s")
    old = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_k5d_engram_pidfile_is_never_called_for_a_non_regular_pidfile(tmp_path, monkeypatch, kind):
    spy = PidfileSpy(result=os.getpid())
    mod = _load_io_module(monkeypatch, spy)
    pf = tmp_path / "engramd.pid"
    os.mkfifo(pf) if kind == "fifo" else pf.mkdir()
    with alarm(5):
        verdict = mod.live(str(tmp_path))
    assert verdict == f"notregular:{kind}", verdict
    assert spy.calls == [], "the non-regular pre-check must come FIRST; engram_pidfile was reached"


def test_k5_live_delegates_to_engram_pidfile_strict_mode_and_reads_no_proc(tmp_path, monkeypatch):
    import builtins
    import inspect
    spy = PidfileSpy(result=os.getpid())
    mod = _load_io_module(monkeypatch, spy)
    (tmp_path / "engramd.pid").write_text(f"{os.getpid()}\n")
    proc_opens = []
    real_open = builtins.open

    def watching_open(f, *a, **k):
        if isinstance(f, str) and f.startswith("/proc"):
            proc_opens.append(f)
        return real_open(f, *a, **k)

    monkeypatch.setattr(builtins, "open", watching_open)
    assert mod.live(str(tmp_path)) == "alive"
    monkeypatch.undo()
    assert len(spy.calls) == 1, spy.calls
    _path, marker, kw = spy.calls[0]
    assert marker == "engramd_owner" and kw.get(STRICT_KW) is True, spy.calls
    assert proc_opens == [], f"live() still reads /proc itself: {proc_opens}"
    spy.result = None
    monkeypatch.setitem(sys.modules, "engram_pidfile", spy)
    assert mod.live(str(tmp_path)) == "none"
    assert "/proc" not in inspect.getsource(mod.live)


def test_k5_unimportable_engram_pidfile_is_ambiguous_never_none(tmp_path):
    seat = DSeat(tmp_path / "s", with_pidfile=False)  # no engram_pidfile.py anywhere reachable
    (seat.eh / "engramd.pid").write_text(f"{os.getpid()}\n")
    assert seat.io("live", seat.eh).startswith(LIVE_AMBIGUOUS_PREFIX)


def test_k5_hook_import_failure_launches_nothing_and_says_so(tmp_path):
    seat = DSeat(tmp_path / "s", with_pidfile=False)
    (seat.eh / "engramd.pid").write_text(f"{os.getpid()}\n")
    seat.config(FLAG_ON)
    seat.launchctl("notfound")
    rc, out, _e, _t = seat.run_timed(limit=30)
    assert rc == 0 and seat.launched() == [], out
    assert len(_lines_of((rc, out, _e, _t), seat)) == 1 and "ambiguous" in " ".join(_lines_of((rc, out, _e, _t), seat))


def test_k5f_darwin_hook_a_stale_pidfile_naming_a_live_non_owner_does_not_suppress_the_launch(tmp_path):
    """The headline Darwin case: today (no /proc) such a pidfile reads ALIVE and the owner is never
    launched.  Here the process is OLDER than the pidfile (the plain-`cp` shape)."""
    seat = DSeat(tmp_path / "s", os_name="Darwin")
    seat.config(FLAG_ON)
    seat.launchctl("notfound")
    with live_process(NON_OWNER) as p:
        write_pidfile(seat.eh, p.pid, process_is_older=True)
        rc, out, _e, _t = seat.run_timed(limit=60)
    assert rc == 0
    assert len(seat.launched()) == 1, "a live non-owner pid in engramd.pid suppressed the owner launch"
    assert any("UNSUPERVISED" in ln for ln in _lines_of((rc, out, _e, _t), seat))


# ===========================================================================
# K6 -- preflight from the owner's own predicates (Q5)
# ===========================================================================

def run_owner(args, *, home: Path, engram_home: Path, extra_code: str | None = None):
    """Run the REAL owner script; a preflight exits by itself.  An owner that starts SERVING
    (i.e. --preflight was ignored) is killed at the timeout and reported as the missing feature."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM_")}
    env.update({"HOME": str(home), "ENGRAM_HOME": str(engram_home), "ENGRAM_NO_EMBEDDINGS": "1"})
    argv = [sys.executable, str(OWNER_PY), *args]
    if extra_code is not None:
        argv = [sys.executable, "-c", extra_code]
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", env=env,
                         start_new_session=True, cwd=str(home))
    timed_out = False
    try:
        out, err = p.communicate(timeout=OWNER_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        pytest.fail(f"the owner did not exit within {OWNER_RUN_TIMEOUT}s with {args}: it started SERVING "
                    f"instead of running a preflight")
    return p.returncode, out, err


def tree_snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        snap[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns,
                                          p.read_bytes() if p.is_file() and not p.is_symlink() else None)
    return snap


def test_k6_healthy_home_preflight_is_silent_exit_zero_and_creates_nothing(short_base):
    home = short_base / "eh"
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=home)
    assert (rc, out, err) == (0, "", ""), (rc, out, err)
    assert not home.exists(), "a preflight created the (nonexistent) home"


def test_k6_existing_home_is_byte_identical_after_preflight(short_base):
    home = short_base / "eh"
    (home / "notes").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    (home / "notes" / "a.txt").write_text("keep")
    before = tree_snapshot(home)
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=home)
    assert rc == 0, (rc, out, err)
    assert tree_snapshot(home) == before, "preflight mutated the home"
    assert not (home / "run").exists()


@pytest.mark.parametrize("shape", ["home-is-a-file", "component-is-a-file"])
def test_k6_d_preflight_under_a_non_directory_is_a_one_line_refusal_not_a_traceback(short_base, shape):
    """#3780 D: a pidfile path under a regular file raised ENOTDIR out of the `lstat` and escaped as a
    traceback (still exit 1).  Now a one-line refusal in the style of the other preflight refusals."""
    planted = short_base / "afile"
    planted.write_text("not a directory")
    home = planted if shape == "home-is-a-file" else planted / "eh"
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=home)
    assert rc == 1, (rc, out, err)
    assert "Traceback" not in err and "NotADirectoryError" not in err, err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    assert lines[0].startswith("[engramd] "), err
    assert str(planted) in lines[0], err
    assert "not a directory" in lines[0], err
    assert out == ""


def test_k6_over_long_home_is_refused_with_the_owners_reason_and_a_healthy_control_passes(short_base):
    long_home = short_base / ("h" * 120)
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=long_home)
    assert rc != 0 and "AF_UNIX" in err and str(long_home) in err, (rc, out, err)
    assert not long_home.exists()
    # control: the same invocation on a healthy home must exit 0 (a preflight that always refuses,
    # or one that is simply the owner's start(), would not)
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=short_base / "ok")
    assert (rc, out, err) == (0, "", ""), (rc, out, err)


@pytest.mark.skipif(not IS_DARWIN, reason="the owner's TCC check keys on its Darwin arm (_owner_arm), "
                                          "which a uname stub cannot reach")
def test_k6_tcc_protected_home_is_refused_with_the_owners_reason_and_a_healthy_control_passes(short_base):
    fake_home = short_base / "u"
    fake_home.mkdir()
    home = fake_home / "Documents" / "eh"
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=fake_home, engram_home=home)
    assert rc != 0 and "TCC" in err, (rc, out, err)
    assert not (fake_home / "Documents").exists(), "the preflight touched ~/Documents"
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=fake_home, engram_home=fake_home / "eh")
    assert (rc, out, err) == (0, "", ""), (rc, out, err)


def test_k6_preflight_and_start_share_one_predicate(short_base):
    """Alter the shared check: BOTH entry points change.  (Healthy control first, so a preflight
    that does not exist cannot pass by the owner's own start() refusing.)"""
    rc, out, err = run_owner([PREFLIGHT_FLAG], home=short_base, engram_home=short_base / "ok")
    assert (rc, out, err) == (0, "", ""), (rc, out, err)
    patch = (f"import sys; sys.path.insert(0, {str(SRC)!r}); import engramd_client as ec; "
             f"ec.socket_path_too_long_reason = lambda p: 'SENTINEL-3709-REASON'; ")
    pre = patch + (f"import runpy; sys.argv = [{str(OWNER_PY)!r}, {PREFLIGHT_FLAG!r}]; "
                   f"runpy.run_path({str(OWNER_PY)!r}, run_name='__main__')")
    rc, out, err = run_owner([], home=short_base, engram_home=short_base / "ok2", extra_code=pre)
    assert rc != 0 and "SENTINEL-3709-REASON" in err, ("preflight", rc, out, err)
    start = patch + ("import engramd_owner as eo; o = eo.EngramdOwner()\n"
                     "try:\n    o.start(install_signal_handlers=False)\n"
                     "except Exception as e:\n    print('RAISED', e)\n")
    rc, out, err = run_owner([], home=short_base, engram_home=short_base / "ok3", extra_code=start)
    assert "SENTINEL-3709-REASON" in out, ("start", rc, out, err)


# ---- `--owner` on Darwin runs the preflight before it writes anything ---------------------------

def test_k6_owner_mode_over_long_home_is_refused_before_anything_is_created(tmp_path, short_base):
    sb = DSandbox(tmp_path / "i", os_name="Darwin", real_owner=True)
    home = short_base / ("h" * 120)
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "AF_UNIX" in r.stdout + r.stderr, (r.stdout, r.stderr)
    sb.assert_nothing_written(home)


def test_k6_owner_mode_checks_again_after_physical_normalization(tmp_path, short_base):
    """The tilde-expanded raw spelling is short and passes; its PHYSICAL path is longer than the
    AF_UNIX limit and must be refused by the second preflight, still before any plist."""
    target = short_base / ("a" * 55) / ("b" * 55) / "eh"
    target.mkdir(parents=True)
    link = short_base / "s"
    link.symlink_to(target)
    sb = DSandbox(tmp_path / "i", os_name="Darwin", real_owner=True)
    r = sb.run("--owner", engram_home=str(link))
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "AF_UNIX" in r.stdout + r.stderr, (r.stdout, r.stderr)
    sb.assert_nothing_written()


def test_k6x_owner_mode_new_home_under_symlinked_parent_is_refused_before_any_mkdir(tmp_path, short_base):
    """The home does not exist yet, so the physical path must be computed WITHOUT creating it: the
    short spelling passes the first preflight, the physical one (through the symlinked parent) is
    over the AF_UNIX limit.  The refusal must leave no home dir, no ~/Library, no launchctl call."""
    deep = short_base / ("a" * 55) / ("b" * 55)
    deep.mkdir(parents=True)
    link = short_base / "s"
    link.symlink_to(deep)
    sb = DSandbox(tmp_path / "i", os_name="Darwin", real_owner=True)
    r = sb.run("--owner", engram_home=str(link / "eh"))
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "AF_UNIX" in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert not (deep / "eh").exists(), "the home dir was created before the physical-path preflight"
    assert not (link / "eh").exists()
    sb.assert_nothing_written()


def _tree_listing(*roots: Path):
    return sorted(str(p) for r in roots for p in [r, *r.rglob("*")])


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_k6z_owner_mode_home_with_dotdot_after_a_symlink_is_refused_before_anything_is_created(tmp_path, short_base, os_name):
    """`lnk2 -> real/sub`, home `lnk2/../new`: a lexical plan says `.../new`, `mkdir -p` creates
    `.../real/new`.  Any `..` component in the home is refused, named, before anything is created."""
    (short_base / "real" / "sub").mkdir(parents=True)
    (short_base / "lnk2").symlink_to(short_base / "real" / "sub")
    sb = DSandbox(tmp_path / "i", os_name=os_name, real_owner=True)
    before = _tree_listing(short_base, tmp_path)
    r = sb.run("--owner", engram_home=f"{short_base}/lnk2/../new")
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "must not contain '..' components" in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert _tree_listing(short_base, tmp_path) == before, "a directory was created"
    sb.assert_nothing_written()


# Every spelling of a `..` component.  `{b}` is the (short) base dir; the two relative homes are
# resolved against the installer's cwd (the sandbox tmp) and reach the refusal as given: it runs on
# the raw value, before the home is made absolute.
DOTDOT_REFUSED = {
    "start-relative": "../x",
    "start-after-base": "{b}/../x",
    "middle": "{b}/a/../x",
    "end": "{b}/a/..",
    "trailing-slash": "{b}/a/../",
    "only-dotdot-after-base": "{b}/../..",
    "only-dotdot-relative": "..",
}
# Names that CONTAIN dots but are not a `..` component: never refused for that reason.
DOTDOT_ACCEPTED = {
    "inside": "{b}/a..b",
    "leading": "{b}/..foo",
    "trailing": "{b}/foo..",
    "three-dots-dir": "{b}/a/.../b",
}


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
@pytest.mark.parametrize("spelling", list(DOTDOT_REFUSED))
def test_k6z_owner_mode_home_with_dotdot_in_any_position_is_refused_before_anything_is_created(tmp_path, short_base, os_name, spelling):
    """A `..` as the first, a middle, the last, a trailing-slash, or the only component is refused
    (exit 2, named error), with nothing created: no dir, no ~/Library, no launchctl call."""
    sb = DSandbox(tmp_path / "i", os_name=os_name, real_owner=True)
    before = _tree_listing(short_base, tmp_path)
    r = sb.run("--owner", engram_home=DOTDOT_REFUSED[spelling].format(b=short_base))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    # a RELATIVE spelling is refused one step earlier, by the launcher's relative-ENGRAM_HOME guard (#3863 6c)
    needle = "ENGRAM_HOME must be an absolute path" if spelling.endswith("-relative") else "must not contain '..' components"
    assert needle in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert _tree_listing(short_base, tmp_path) == before, "a directory was created"
    sb.assert_nothing_written()


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
@pytest.mark.parametrize("spelling", list(DOTDOT_ACCEPTED))
def test_k6z_owner_mode_home_with_dots_in_a_name_is_not_taken_for_dotdot(tmp_path, short_base, os_name, spelling):
    """`a..b`, `..foo`, `foo..`, `a/.../b` hold dots but no `..` component.  The `..` refusal must
    not fire; the install itself may go on or stop for another reason (nothing real is registered:
    the sandbox's launchctl/systemctl are recording stubs)."""
    sb = DSandbox(tmp_path / "i", os_name=os_name, real_owner=True)
    r = sb.run("--owner", engram_home=DOTDOT_ACCEPTED[spelling].format(b=short_base))
    assert "'..' components" not in r.stdout + r.stderr, (r.returncode, r.stdout, r.stderr)


@pytest.mark.skipif(not IS_DARWIN, reason="the owner's TCC check keys on its Darwin arm (_owner_arm)")
def test_k6y_owner_mode_new_home_through_symlink_into_tcc_folder_is_refused_before_any_mkdir(short_base):
    sb = DSandbox(short_base / "i", os_name="Darwin", real_owner=True)
    link = short_base / "t"
    link.symlink_to(sb.home / "Documents" / "x")  # the target does not exist: a dangling link
    r = sb.run("--owner", engram_home=str(link / "eh"))
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "TCC" in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert not (sb.home / "Documents").exists(), "mkdir reached through the symlink into ~/Documents"
    sb.assert_nothing_written()


@pytest.mark.skipif(not IS_DARWIN, reason="the owner's TCC check keys on its Darwin arm (_owner_arm)")
def test_k6_owner_mode_tcc_home_is_refused_before_mkdir_plist_or_launchctl(short_base):
    # short base: $HOME/Documents/eh/run/engramd.sock must fit AF_UNIX, or the socket-length
    # refusal (which comes first in start()) would fire instead of the TCC one
    sb = DSandbox(short_base / "i", os_name="Darwin", real_owner=True)
    home = sb.home / "Documents" / "eh"
    r = sb.run("--owner", engram_home=str(home))
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "TCC" in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert not (sb.home / "Documents").exists(), "mkdir under ~/Documents happened before the preflight"
    sb.assert_nothing_written(home)


# A non-regular pidfile makes the owner exit 4 at start (#3768); systemd stops retrying on that code,
# launchd's KeepAlive {SuccessfulExit: false} has no per-code exclusion and would relaunch every 60 s.
# So `--owner` must refuse before registering, from the same check start() runs.
def _plant_fifo(path: Path, sentinel: Path):
    os.mkfifo(path)


def _plant_hardlink(path: Path, sentinel: Path):
    os.link(sentinel, path)


def _plant_symlink(path: Path, sentinel: Path):
    path.symlink_to(sentinel)


@pytest.mark.parametrize("plant", [_plant_fifo, _plant_hardlink, _plant_symlink],
                         ids=["fifo", "hardlink", "symlink"])
def test_k6_owner_mode_non_regular_pidfile_is_refused_before_plist_or_launchctl(short_base, plant):
    import engramd_client as ecl  # conftest puts src/engram on sys.path
    sb = DSandbox(short_base / "i", os_name="Darwin", real_owner=True)
    home = short_base / "eh"
    home.mkdir()
    sentinel = short_base / "sentinel"
    sentinel.write_bytes(b"SENTINEL-3768-DO-NOT-TOUCH")
    pid_path = ecl.pid_path_for_home(home)
    plant(pid_path, sentinel)
    before_home = sorted(p.name for p in home.iterdir())
    r = sb.run("--owner", engram_home=str(home))
    out = r.stdout + r.stderr
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "refusing to start: the pidfile path" in out and str(pid_path) in out, (r.stdout, r.stderr)
    sb.assert_nothing_written()  # no ~/Library, no plist, no launchctl call
    assert sorted(p.name for p in home.iterdir()) == before_home, "the install changed the home"
    assert sentinel.read_bytes() == b"SENTINEL-3768-DO-NOT-TOUCH"


# ===========================================================================
# K7 -- #3519 guards
# ===========================================================================

def test_k7_darwin_owner_mode_with_an_ephemeral_owner_script_is_refused_before_any_write(tmp_path):
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    home = sb.tmp / "seat-a"
    r = sb.run("--owner", engram_home=str(home), allow_path=False)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "engramd_owner.py" in r.stderr and "ephemeral" in r.stderr, r.stderr
    sb.assert_nothing_written(home)


def test_k7_darwin_owner_mode_never_touches_systemd(tmp_path):
    """Replaces the old 3663 cell's systemd half: a successful Darwin `--owner` makes no systemctl
    call (the Sandbox's recording stub is on PATH) and writes no systemd unit dir."""
    sb = DSandbox(tmp_path / "i", os_name="Darwin")
    r = sb.run("--owner", engram_home=str(sb.tmp / "seat-a"))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert sb.launchctl_calls(), "control: the Darwin install did go through launchctl"
    assert sb.systemctl_calls() == [], sb.systemctl_calls()
    assert not sb.unit_dir.exists() or list(sb.unit_dir.iterdir()) == [], sb.unit_dir


def test_k7_darwin_hook_never_calls_systemctl(tmp_path):
    """Replaces the old 3663 cell's systemd half: on Darwin the hook asks launchd only.  A RECORDING
    systemctl stub is on PATH, and the notfound path (the one that goes on to launch) is taken."""
    seat = DSeat(tmp_path / "s", os_name="Darwin")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")  # recording stub (Seat.systemctl logs every call to seat.syslog)
    seat.launchctl("notfound")
    rc, out, _e, _t = seat.run_timed(limit=LAUNCHD_BOUND_SECONDS + LAUNCHD_SLACK_SECONDS + 10)
    assert rc == 0 and len(seat.launched()) == 1, (rc, out)  # control: the launchd path ran to the end
    assert seat.launchctl_log() == [seat.expected_print()]
    assert not seat.syslog.exists(), seat.syslog.read_text(encoding="utf-8")


# ===========================================================================
# #3794 -- launcher path handling under bash 3.2 multibyte locales (after #3789)
# ===========================================================================
# Under a multibyte LC_CTYPE the stock /bin/bash 3.2 inserts \001 after each byte >= 0x80 of a `$(...)`
# capture made by the CAPTURING shell, whatever the captured function pins inside.  Only a capture made
# INSIDE a function pinned with `local LC_ALL=C`, or a `printf -v` out of one, is exact.  Every cell
# below runs under an EXPLICIT interpreter (BASHES) with the hostile locale, and compares against the
# TRUE path computed by Python, never against what the other bash side computed.
_HOSTILE_DROP, _HOSTILE_EXTRA = LOCALE_ENVS["hostile-locale"]


def _utf8_stub_python(directory: Path) -> Path:
    """A `python3` that answers `-c 'print(sys.executable)'` with ITS OWN (non-ASCII) path."""
    p = directory / "python3"
    return _stub(directory, "python3", f'#!/bin/sh\nprintf \'%s\\n\' "{p}"\n')


@pytest.mark.parametrize("python_from", ["venv", "path-fallback"])
@pytest.mark.parametrize("bash", BASHES)
def test_a4_owner_plist_content_is_byte_exact_with_utf8_home_plugin_and_python_paths(tmp_path, bash, python_from):
    """A1 + A4 (#3794): render the owner plist (fake launchctl, never a real bootstrap) with a UTF-8
    ENGRAM_HOME, a UTF-8 plugin/install path and a UTF-8 tmp tree, then parse it and compare EVERY path
    value with the true bytes.  `path-fallback` drops the venv python so the installer asks `python3`
    (a stub on a UTF-8 PATH entry) for sys.executable: that is a path capture too."""
    sb = DSandbox(tmp_path / "café-inst", os_name="Darwin", plugin_name="café-plugin")
    if python_from == "path-fallback":
        shutil.rmtree(sb.home / ".engram" / "venv")
        py = _utf8_stub_python(sb.stubs)
    else:
        py = sb.python
    home = sb.tmp / "café-seat"
    r = sb.run_env("--owner", engram_home=str(home), drop=_HOSTILE_DROP, extra=_HOSTILE_EXTRA, bash=bash)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    plist = sb.agents_dir / f"{label_for(home)}.plist"
    assert plist.exists(), sorted(p.name for p in sb.agents_dir.glob("*"))
    raw = plist.read_bytes()
    try:
        d = plistlib.loads(raw)
    except Exception as e:  # a \001 byte is not legal XML
        pytest.fail(f"the plist does not even parse ({e!r}); bytes: {raw!r}")
    log = f"{_physical(home)}/engramd-owner.log"
    assert d["ProgramArguments"] == [str(py), _physical(sb.plugin) + "/engramd_owner.py"], d["ProgramArguments"]
    assert d["EnvironmentVariables"]["ENGRAM_HOME"] == _physical(home), d["EnvironmentVariables"]
    assert d["StandardOutPath"] == log and d["StandardErrorPath"] == log, (d["StandardOutPath"], d["StandardErrorPath"])
    assert b"\x01" not in raw


@pytest.mark.parametrize("layout", ["plain", "symlink-chain"])
@pytest.mark.parametrize("bash", BASHES)
def test_a2_daemon_real_keeps_a_utf8_install_path_exact(tmp_path, bash, layout):
    """A2 (#3794): `_physical_path` pinned only AFTER its readlink loop, and its caller captured its
    stdout at top level.  The plugin root is ASCII here (so A1 is not what this cell sees); the daemon
    lives in a UTF-8 hooks dir given by ENGRAM_HOOKS_DIR.  `DAEMON_REAL` shows up verbatim in the
    ephemeral-path WARN (tmp is ephemeral).  `symlink-chain` also runs the readlink/dirname captures."""
    sb = DSandbox(tmp_path / "inst", os_name="Darwin")
    real_dir = sb.tmp / "café-real"
    real_dir.mkdir()
    (real_dir / "engram-surface-daemon.py").write_text("# stub daemon\n")
    hooks = real_dir
    if layout == "symlink-chain":
        hooks = sb.tmp / "café-hooks"
        hooks.mkdir()
        (hooks / "engram-surface-daemon.py").symlink_to("../café-real/engram-surface-daemon.py")
    r = sb.run_env(engram_home=str(sb.tmp / "seat-a"), drop=_HOSTILE_DROP, bash=bash,
                   extra={**_HOSTILE_EXTRA, "ENGRAM_HOOKS_DIR": str(hooks)})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    expected = f"{_physical(real_dir)}/engram-surface-daemon.py"
    assert f"ephemeral daemon path {expected} (" in r.stderr, (expected, r.stderr)


@pytest.mark.parametrize("python_from", ["venv", "path-fallback"])
@pytest.mark.parametrize("bash", BASHES)
def test_a1_hook_finds_its_plugin_root_and_python_with_utf8_paths_byte_exact(tmp_path, bash, python_from):
    """A1 (#3794), hook side: the plugin root / IO helper are found by walking up from BASH_SOURCE and
    the python by `command -v`, all of them path captures at the hook's top level.  The whole seat is
    under a UTF-8 dir, so the launch argv must carry the exact bytes of the plugin path."""
    seat = DSeat(tmp_path / "café-hook")
    if python_from == "path-fallback":
        venv_py = seat.home / ".engram" / "venv" / "bin" / "python3"
        body = venv_py.read_text(encoding="utf-8")
        venv_py.unlink()
        _stub(seat.stubs, "python3", body)
    seat.config(FLAG_ON)
    seat.set_os("Darwin")
    seat.launchctl("notfound")
    r = seat.run(dict(_HOSTILE_EXTRA), bash=bash)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    launched = seat.record.read_text(encoding="utf-8").splitlines() if seat.record.exists() else []
    assert launched == [_physical(seat.plugin) + "/engramd_owner.py"], (launched, r.stdout, r.stderr)


# ---- A3: the `printf -v "$name"` namespace trap ------------------------------------------------
# A function that assigns to a caller-NAMED variable with `printf -v` hits its own local when the
# caller's name equals one of its locals: rc 0, caller's variable unset (reproduced by the round-3
# reviewer).  The contract: such a name is REFUSED (rc != 0, ONE stderr line naming it, nothing
# assigned), and an ordinary name keeps working.  The locals carry a unique prefix and the guard
# refuses any name starting with it, plus `LC_ALL` (the pin, itself a local of every one of them).
def _fn_source(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"function {name} not found at the start of a line in {script.relative_to(REPO)}"
    return m.group(0)


# id -> (prefix of the function's own locals, shell definitions, call template, argument kind)
A3_TARGETS = {
    "helper-physical-path": ("_eopp_", lambda: f'source "{HOME_KEY_HELPER}"',
                             'engramd_owner_physical_path "$1" {n}', "dir"),
    "installer-physical-dir-into": ("_opdi_", lambda: _fn_source(INSTALLER, "_owner_physical_dir_into"),
                                    '_owner_physical_dir_into "$1" {n}', "dir"),
    "hook-physical-dir-into": ("_pdi_", lambda: _fn_source(HOOK, "_physical_dir_into"),
                               '_physical_dir_into "$1" {n}', "dir"),
    "installer-physical-path": ("_pp_", lambda: _fn_source(INSTALLER, "_physical_path"),
                                '_physical_path "$1" {n}', "file"),
    "installer-pinned-into": ("_pin_", lambda: _fn_source(INSTALLER, "_pinned_into"),
                              '_pinned_into {n} printf %s "$1"', "dir"),
    "hook-pinned-into": ("_pin_", lambda: _fn_source(HOOK, "_pinned_into"),
                         '_pinned_into {n} printf %s "$1"', "dir"),
}
# every local name any of these functions had before the fix, plus the pin itself
OLD_LOCAL_NAMES = ["path", "outvar", "anc", "rest", "comp", "link", "hops", "phys", "_pd", "p", "target",
                   "n", "d", "b", "LC_ALL"]


def _a3_run(tmp_path, bash, target, name):
    prefix, defs, call, kind = A3_TARGETS[target]
    d = tmp_path / "d"
    d.mkdir(exist_ok=True)
    arg = d
    if kind == "file":
        arg = d / "f.txt"
        arg.write_text("x")
    script = (f'{defs()}\nunset {name}\n{call.format(n=name)}\nrc=$?\nprintf "rc=%s\\n" "$rc"\n'
              f'if [ "${{{name}+set}}" = set ]; then printf "val=%s\\n" "${{{name}}}"; else echo UNSET; fi\n')
    r = subprocess.run([bash, "-c", script, "x", str(arg)], capture_output=True, encoding="utf-8",
                       errors="replace", env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    lines = r.stdout.splitlines()
    assert lines and lines[0].startswith("rc="), (r.stdout, r.stderr)
    rc = int(lines[0][3:])
    val = lines[1][4:] if len(lines) > 1 and lines[1].startswith("val=") else None
    expected = str(arg) if target.endswith("pinned-into") else _physical(arg)
    return rc, val, expected, r.stderr


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("target", list(A3_TARGETS))
def test_a3_a_name_the_function_cannot_assign_is_never_silently_dropped(tmp_path, bash, target):
    """Whatever name is passed, the outcome is one of two: assigned CORRECTLY (rc 0, caller's variable
    holds the value), or refused (rc != 0, the name on stderr, the variable untouched).  The bug was a
    third outcome: rc 0 with the caller's variable unset."""
    bad = []
    for name in OLD_LOCAL_NAMES + [A3_TARGETS[target][0] + s for s in ("x", "path", "phys")]:
        rc, val, expected, err = _a3_run(tmp_path, bash, target, name)
        if rc == 0 and val != expected:
            bad.append((name, "rc 0 but the caller's variable is " + ("unset" if val is None else repr(val))))
        elif rc != 0 and (val is not None or name not in err):
            bad.append((name, f"refused without naming it / after assigning: {val!r} {err!r}"))
    assert not bad, bad


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("target", list(A3_TARGETS))
def test_a3_the_functions_own_local_names_are_refused_with_one_stderr_line(tmp_path, bash, target):
    prefix = A3_TARGETS[target][0]
    for name in (prefix + "x", prefix + "phys", "LC_ALL"):
        rc, val, _exp, err = _a3_run(tmp_path, bash, target, name)
        assert rc != 0 and val is None, (name, rc, val)
        lines = [ln for ln in err.splitlines() if ln.strip()]
        assert len(lines) == 1 and name in lines[0], (name, err)


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("target", list(A3_TARGETS))
def test_a3_control_the_names_the_callers_really_pass_still_work(tmp_path, bash, target):
    for name in ("ENGRAM_HOME", "_owner_physical_planned", "PLUGIN_ROOT"):
        rc, val, expected, err = _a3_run(tmp_path, bash, target, name)
        assert (rc, val, err) == (0, expected, ""), (name, rc, val, expected, err)


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("script", [INSTALLER, HOOK], ids=["installer", "hook"])
@pytest.mark.parametrize("exit_code", [0, 3])
def test_a3_pinned_into_returns_the_commands_rc_and_assigns_its_value(tmp_path, bash, script, exit_code):
    """`_pinned_into VAR cmd...` must behave like `VAR=$(cmd...)`: the return status is the COMMAND's
    (not a constant 0), the variable still receives the command's stdout even when it failed, and the
    command's stderr passes through untouched (the caller's `|| ...` / `set -e` paths depend on the rc)."""
    defs = _fn_source(script, "_pinned_into")
    prog = (f'{defs}\n_probe() {{ printf %s "/some/path"; echo probe-stderr >&2; return {exit_code}; }}\n'
            'unset OUT\n_pinned_into OUT _probe\nrc=$?\nprintf "rc=%s\\n" "$rc"\n'
            'if [ "${OUT+set}" = set ]; then printf "val=%s\\n" "$OUT"; else echo UNSET; fi\n')
    r = subprocess.run([bash, "-c", prog], capture_output=True, encoding="utf-8", errors="replace",
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert r.stdout.splitlines() == [f"rc={exit_code}", "val=/some/path"], (r.stdout, r.stderr)
    assert r.stderr == "probe-stderr\n", r.stderr


# ===========================================================================
# #3790 -- the harness itself must not decode the scripts' UTF-8 output with the locale codec
# ===========================================================================
LAUNCHER_TEST_FILES = [Path(__file__), Path(__file__).with_name("test_owner_launcher_3663.py")]


def _locale_decode_lines(source: str) -> list:
    """Line numbers of every call that decodes with the LOCALE codec: `text=True` /
    `universal_newlines=True` on a subprocess call, or a `read_text(...)` with no `encoding`."""
    out = []
    for n in ast.walk(ast.parse(source)):
        if not isinstance(n, ast.Call):
            continue
        for kw in n.keywords:
            if (kw.arg in ("text", "universal_newlines") and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True):
                out.append(n.lineno)
        if (isinstance(n.func, ast.Attribute) and n.func.attr == "read_text"
                and not any(k.arg == "encoding" for k in n.keywords) and len(n.args) < 1):
            out.append(n.lineno)
    return sorted(out)


def test_b_the_scan_sees_a_planted_locale_decode():
    """Control for the cell below: the same scan, on planted matches, reports them (and only them)."""
    assert _locale_decode_lines("import subprocess\nsubprocess.run(['x'], capture_output=True, text=True)\n") == [2]
    assert _locale_decode_lines("p.read_text()\nq.read_text(errors='x')\n") == [1, 2]
    assert _locale_decode_lines("subprocess.run(['x'], encoding='utf-8', errors='replace')\n"
                                "p.read_text(encoding='utf-8')\n") == []


@pytest.mark.parametrize("path", LAUNCHER_TEST_FILES, ids=lambda p: p.name)
def test_b_no_launcher_test_decodes_with_the_locale_codec(path):
    """`text=True` and a bare `read_text()` decode with the locale's preferred encoding: under a
    non-UTF-8 pytest locale that raises UnicodeDecodeError on the scripts' UTF-8 output (the `→` in the
    installer's own lines) or on a UTF-8 source file, before any assertion runs (#3790).  Every such call
    states `encoding="utf-8"` (and `errors="replace"` on subprocess output)."""
    assert _locale_decode_lines(path.read_text(encoding="utf-8")) == []


def test_b_a_script_call_decodes_under_a_non_utf8_pytest_process_locale():
    """The behavioural half: a SUBPROCESS pytest (a pytest's own locale cannot change mid-session)
    under LC_CTYPE=ja_JP.eucJP runs one cell that drives the installer, whose output carries `→`."""
    env = {k: v for k, v in os.environ.items() if k != "LC_ALL"}
    env.update(HOSTILE_LOCALE)
    env["ENGRAM_NO_EMBEDDINGS"] = "1"
    probe = subprocess.run([sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
                           capture_output=True, encoding="utf-8", errors="replace", env=env)
    if probe.stdout.strip().lower().replace("-", "").replace("_", "") == "utf8":
        pytest.skip(f"the hostile locale is not available here (preferred encoding {probe.stdout.strip()!r}): "
                    "the cell would prove nothing")
    target = f"{Path(__file__).with_name('test_owner_launcher_3663.py')}::test_c1_unit_execstart_argv_contains_engramd_owner"
    r = subprocess.run([sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
                       capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=str(REPO), timeout=300)
    out = r.stdout + r.stderr
    assert "UnicodeDecodeError" not in out, out[-3000:]
    assert r.returncode == 0 and "1 passed" in out, (r.returncode, out[-3000:])


# ===========================================================================
# R1 (+ K3) -- one REAL launchd cell, Darwin only
# ===========================================================================

# SECURITY REVIEW: this is the ONE deliberate exception to the #3519 guard.  The conftest shim
# refuses `launchctl bootstrap`/`bootout` by design; this cell calls the real tool by ABSOLUTE
# path.  It is gated on (a) macOS, and (b) every label starting with TEST_LABEL_PREFIX, checked
# before the first real call.  It never touches a `com.engram.engramd-owner.*` label.
REAL_LAUNCHCTL = "/bin/launchctl"
TEST_LABEL_PREFIX = "com.engram.test."


def _load_parser():
    monkey_path = str(HOOK_IO.parent)
    if monkey_path not in sys.path:
        sys.path.insert(0, monkey_path)
    spec = importlib.util.spec_from_file_location("_engramd_owner_hook_io_r1", HOOK_IO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, LAUNCHD_PARSE_FUNC)


def _real_print(label: str):
    r = subprocess.run([REAL_LAUNCHCTL, "print", f"gui/{UID}/{label}"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    return r.returncode, r.stdout, r.stderr


def _sweep_leftover_test_jobs() -> list:
    """If an earlier run was SIGKILLed mid-R1, its `finally` never ran and its test jobs (exit1 /
    exit3 retry every ThrottleInterval, `sleep 600` lingers) are still registered.  List launchd
    (read-only), boot out every leftover label that starts with TEST_LABEL_PREFIX, and touch NOTHING
    else: any other label is left alone, and this never issues a call for it.  Returns the labels
    it booted out."""
    r = subprocess.run([REAL_LAUNCHCTL, "list"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, ("launchctl list failed", r.returncode, r.stderr)
    # `launchctl list` rows are tab-separated: PID, last exit status, label.
    found = [cols[2] for cols in (ln.split("\t") for ln in r.stdout.splitlines()) if len(cols) == 3]
    stale = [lab for lab in found if lab.startswith(TEST_LABEL_PREFIX)]
    for lab in stale:
        assert lab.startswith(TEST_LABEL_PREFIX)  # the only labels this module ever passes to bootout
        subprocess.run([REAL_LAUNCHCTL, "bootout", f"gui/{UID}/{lab}"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    again = subprocess.run([REAL_LAUNCHCTL, "list"], capture_output=True, encoding="utf-8", errors="replace", timeout=30).stdout
    still = [lab for lab in stale if f"\t{lab}" in again]
    assert not still, f"could not boot out leftover test jobs from an earlier run: {still}"
    return stale


def _no_gui_domain_skip_reason(label: str):
    """The R1 skip reason when this uid has no GUI launchd domain, else None (#3780 A5).  Keys on the
    parser's `no_gui_domain` verdict, so the rc-125 rule has ONE definition (the hook's parser) and a
    bare 125 without the domain text is not mistaken for it.  Read-only: one `launchctl print`."""
    rc, out, err = _real_print(label)
    verdict = _load_parser()(rc, out, err)
    if verdict == "no_gui_domain":
        return (f"no GUI launchd domain for uid {UID} (no logged-in GUI session): the hook's parser "
                f"says no_gui_domain for `launchctl print gui/{UID}/<label>` (rc {rc}, {err.strip()!r}); "
                f"R1 needs one")
    return None


def _leaked_labels(labels, list_output: str) -> list:
    """The labels in `labels` that are still registered according to `launchctl list` output: one row
    per job, tab-separated PID / last exit status / label.  Exact label match.  REAL state, not the
    `print` verdict (which is not `notfound` on every host where nothing leaked, e.g. a no-GUI one)."""
    registered = {cols[2] for cols in (ln.split("\t") for ln in list_output.splitlines()) if len(cols) == 3}
    return [lab for lab in labels if lab in registered]


@pytest.mark.skipif(not IS_DARWIN, reason="real launchd exists only on macOS")
def test_r1_real_launchd_restart_semantics_and_parser(tmp_path):
    # If pytest is SIGKILLed mid-cell, the `finally` below cannot run and `com.engram.test.r1-*`
    # jobs stay registered; this sweep (test-prefixed labels only) cleans them at the next start.
    _sweep_leftover_test_jobs()
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    labels = {k: f"{TEST_LABEL_PREFIX}r1-{k}-{suffix}" for k in ("sleep", "exit0", "exit1", "exit3")}
    never = f"{TEST_LABEL_PREFIX}r1-never-registered-{suffix}"
    for lab in (*labels.values(), never):
        if not lab.startswith(TEST_LABEL_PREFIX):
            pytest.fail(f"refusing to run: label {lab!r} lacks the {TEST_LABEL_PREFIX!r} test prefix")

    # No GUI launchd domain for this uid (no logged-in GUI session) means nothing can be bootstrapped
    # into gui/<uid>: a property of the host, not a defect in the code under test, so a skip.  Keyed
    # on the parser's named `no_gui_domain` verdict (#3780), not on a raw rc.  Probed before any
    # bootstrap, so a skip never registers anything.  Any other result keeps its normal path below.
    skip_reason = _no_gui_domain_skip_reason(never)
    if skip_reason:
        pytest.skip(skip_reason)

    # Policy comes from the SHIPPED template (rendered), never retyped here.
    assert PLIST_TEMPLATE.exists(), f"missing (not implemented yet): {PLIST_TEMPLATE.relative_to(REPO)}"
    text = PLIST_TEMPLATE.read_text(encoding="utf-8")
    for ph, val in zip(PLIST_PLACEHOLDERS, ("/usr/bin/python3", "/x/engramd_owner.py", "/x/home", "com.engram.test.tpl")):
        text = text.replace(ph, val)
    tpl = plistlib.loads(text.encode())
    keepalive, throttle = tpl["KeepAlive"], tpl["ThrottleInterval"]
    parse = _load_parser()

    stamps = tmp_path / "stamps"
    stamps.mkdir()
    plists = {}
    for kind, lab in labels.items():
        argv = (["/bin/sleep", "600"] if kind == "sleep" else
                ["/bin/sh", "-c", f"date +%s >> {stamps / kind}.runs; exit {kind[-1]}"])
        d = {"Label": lab, "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": keepalive,
             "ThrottleInterval": throttle, "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        plists[kind] = tmp_path / f"{lab}.plist"
        plists[kind].write_bytes(plistlib.dumps(d))

    def verdict(label):
        return parse(*_real_print(label))

    def wait_for(label, want, within=20.0):
        end = time.monotonic() + within
        while time.monotonic() < end:
            if verdict(label) == want:
                return
            time.sleep(0.5)
        pytest.fail(f"{label}: wanted {want!r}, last saw {verdict(label)!r}")

    try:
        assert verdict(never) == "notfound"
        t0 = time.monotonic()
        for kind, lab in labels.items():
            r = subprocess.run([REAL_LAUNCHCTL, "bootstrap", f"gui/{UID}", str(plists[kind])],
                               capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            assert r.returncode == 0, (kind, r.stdout, r.stderr)
        wait_for(labels["sleep"], "running")
        wait_for(labels["exit0"], "registered:not running:0")
        wait_for(labels["exit1"], "registered:spawn scheduled:1")
        wait_for(labels["exit3"], "registered:spawn scheduled:3")
        # K3: wait out the template's own ThrottleInterval, then count real runs.
        remaining = t0 + throttle + 10 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        runs = {k: len((stamps / f"{k}.runs").read_text(encoding="utf-8").split()) for k in ("exit0", "exit1", "exit3")}
        assert runs["exit0"] == 1, f"exit 0 was restarted: {runs}"
        assert runs["exit1"] >= 2 and runs["exit3"] >= 2, f"exit 1 / exit 3 were not restarted: {runs}"
        assert verdict(labels["sleep"]) == "running"
        assert verdict(labels["exit0"]) == "registered:not running:0"
    finally:
        for lab in labels.values():
            subprocess.run([REAL_LAUNCHCTL, "bootout", f"gui/{UID}/{lab}"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        # Read REAL state: a label is leaked only if `launchctl list` still shows it.
        end = time.monotonic() + 10
        while True:
            listing = subprocess.run([REAL_LAUNCHCTL, "list"], capture_output=True, encoding="utf-8", errors="replace", timeout=30).stdout
            leaked = _leaked_labels(list(labels.values()), listing)
            if not leaked or time.monotonic() >= end:
                break
            time.sleep(0.3)
        if leaked:
            pytest.fail(f"test jobs still registered after bootout: {leaked}")
