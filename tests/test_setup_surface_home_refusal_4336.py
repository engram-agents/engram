"""Surface mode of setup-surface-daemon-service.sh refuses an ENGRAM_HOME the render cannot carry (#4336).

Without --owner the script renders ENGRAM_HOME into the systemd unit / macOS plist with plain
`sed`. Only --owner had an unsafe-character refusal. Contract (the amended closing condition of
#4336, measured on the real plist template with BSD sed and plutil -lint):

  C1  surface mode only; --owner keeps its own "unsupported in --owner mode" line.
  C2  judged AFTER the home is made absolute, BEFORE `mkdir -p`; the service-manager tool
      check still comes first.
  C3  Linux refuses: space, control byte (tab and newline included), & \\ | % " ' $ {{ }}.
      NOT < or > (a unit carries them).
  C4  Darwin refuses: control byte (tab and newline included), & \\ | {{ < >.
      Darwin ALLOWS space, % $ " ' }} and renders them intact.
  C5  a value holding an allowed class AND a refused class is refused, naming the refused one
      (a first-match scan would stop at the allowed class).
  C6  a refusal: exit 2, ONE stderr line `ERROR:` naming ENGRAM_HOME, the class, the value (%q),
      and that nothing was written, loaded or created; no systemctl / launchctl call.
  C7  an ordinary path behaves as before.

The daemon script lives under a scratch dir in the REAL home (not a temp root), because the
#3519 ephemeral-path refusal would otherwise fire first. A recording spy launchctl/systemctl is
FIRST on PATH.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "setup-surface-daemon-service.sh"

_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp",
               "/var/folders", "/private/var/folders")


def _is_ephemeral(path: Path) -> bool:
    p = str(path.resolve())
    roots = list(_TEMP_ROOTS)
    if os.environ.get("TMPDIR"):
        roots.append(str(Path(os.environ["TMPDIR"]).resolve()))
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots) \
        or "/.claude/worktrees/" in p + "/"


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _q(value: str) -> str:
    """The script's own `printf '%q'` rendering of a value."""
    return subprocess.run(["bash", "-c", 'printf "%q" "$1"', "_", value],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def good_hooks():
    home = Path.home()
    if _is_ephemeral(home):
        pytest.skip("real HOME is itself ephemeral; no good base available")
    base = Path(tempfile.mkdtemp(prefix=".engram-4336-", dir=home)).resolve()
    hooks = base / "hooks"
    hooks.mkdir()
    (hooks / "engram-surface-daemon.py").write_text("# stub daemon\n")
    try:
        yield hooks
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def rig(tmp_path, good_hooks):
    stub = tmp_path / "stub"
    stub.mkdir()
    log = tmp_path / "spy.log"
    log.write_text("")
    spy = f'#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "{log}"\nexit 0\n'
    _exe(stub / "launchctl", spy)
    _exe(stub / "systemctl", spy)
    _exe(stub / "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)

    class Rig:
        pass

    r = Rig()
    r.home, r.log, r.stub = home, log, stub
    r.eh_parent = tmp_path / "eh"  # never created by the rig: a refusal must not create it

    def run(engram_home, *, os_name, args=(), cwd=None, home_env=None):
        _exe(stub / "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        env = {
            **os.environ,
            "PATH": f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": home_env if home_env is not None else str(home),
            "ENGRAM_HOOKS_DIR": str(good_hooks),
            "ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01", "ENGRAM_SETUP_LAUNCHD_WAIT_S": "0.05",
        }
        env.pop("ENGRAM_SETUP_ALLOW_PATH", None)
        env.pop("ENGRAM_HOME", None)
        if engram_home is not None:
            env["ENGRAM_HOME"] = engram_home
        return subprocess.run(["bash", str(SETUP_SCRIPT), *args], capture_output=True,
                              text=True, env=env, timeout=30, cwd=cwd)

    r.run = run
    r.spy_calls = lambda: [ln for ln in log.read_text().splitlines() if ln.strip()]
    r.written = lambda: [str(p) for p in home.rglob("*") if p.suffix in (".plist", ".service")]
    return r


# class name -> (character(s) in the value, expected wording in the message)
_LINUX_REFUSED = {
    "space": (" ", "a space character"),
    "control-tab": ("\t", "control byte 0x09"),
    "control-newline": ("\n", "control byte 0x0A"),
    "control-bel": ("\x07", "control byte 0x07"),
    "control-del": ("\x7f", "control byte 0x7F"),
    "ampersand": ("&", "an ampersand character"),
    "backslash": ("\\", "a backslash character"),
    "pipe": ("|", "a pipe character"),
    "percent": ("%", "a percent character"),
    "double-quote": ('"', "a double-quote character"),
    "single-quote": ("'", "a single-quote character"),
    "dollar": ("$", "a dollar character"),
    "open-brace-pair": ("{{", "an open-brace-pair character"),
    "close-brace-pair": ("}}", "a close-brace-pair character"),
}
_DARWIN_REFUSED = {
    "control-tab": ("\t", "control byte 0x09"),
    "control-newline": ("\n", "control byte 0x0A"),
    "control-bel": ("\x07", "control byte 0x07"),
    "control-del": ("\x7f", "control byte 0x7F"),
    "ampersand": ("&", "an ampersand character"),
    "backslash": ("\\", "a backslash character"),
    "pipe": ("|", "a pipe character"),
    "open-brace-pair": ("{{", "an open-brace-pair character"),
    "less-than": ("<", "a less-than character"),
    "greater-than": (">", "a greater-than character"),
}
_DARWIN_ALLOWED = {
    "space": " ", "percent": "%", "dollar": "$", "double-quote": '"',
    "single-quote": "'", "close-brace-pair": "}}",
}


def _assert_refused(r, res, home_value, wording):
    out = res.stdout + res.stderr
    assert res.returncode == 2, out
    errs = [ln for ln in res.stderr.splitlines() if ln.startswith("ERROR:")]
    # the value may itself hold a newline, so count the ERROR line by prefix, not by line total
    assert len(errs) == 1, res.stderr
    assert res.stderr.count("ERROR:") == 1, res.stderr
    assert "ENGRAM_HOME" in errs[0], errs[0]
    assert wording in res.stderr, res.stderr
    assert _q(home_value) in res.stderr, res.stderr
    assert "No unit or plist was written or loaded and nothing was created" in res.stderr
    assert r.spy_calls() == [], r.spy_calls()
    assert r.written() == [], r.written()


# ----------------------------------------------------------------- (a) Linux, per class


@pytest.mark.parametrize("cls", sorted(_LINUX_REFUSED))
def test_linux_refuses_each_class(rig, cls):
    ch, wording = _LINUX_REFUSED[cls]
    value = f"{rig.eh_parent}/a{ch}c"
    res = rig.run(value, os_name="Linux")
    _assert_refused(rig, res, value, wording)
    assert not rig.eh_parent.exists()


@pytest.mark.parametrize("ch", ["<", ">"])
def test_linux_allows_angle_brackets(rig, ch):
    value = f"{rig.eh_parent}/a{ch}c"
    res = rig.run(value, os_name="Linux")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ERROR" not in res.stderr
    assert any(".service" in w for w in rig.written())


# ----------------------------------------------------------------- (b) Darwin, per class


@pytest.mark.parametrize("cls", sorted(_DARWIN_REFUSED))
def test_darwin_refuses_each_class(rig, cls):
    ch, wording = _DARWIN_REFUSED[cls]
    value = f"{rig.eh_parent}/a{ch}c"
    res = rig.run(value, os_name="Darwin")
    _assert_refused(rig, res, value, wording)
    assert not rig.eh_parent.exists()


# ----------------------------------------------------------------- (c) Darwin allowed, intact


@pytest.mark.parametrize("cls", sorted(_DARWIN_ALLOWED))
def test_darwin_allowed_class_proceeds_and_renders_intact(rig, cls):
    ch = _DARWIN_ALLOWED[cls]
    value = f"{rig.eh_parent}/a{ch}c"
    res = rig.run(value, os_name="Darwin")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "ERROR" not in res.stderr, res.stderr
    plists = [Path(p) for p in rig.written()]
    assert len(plists) == 1, plists
    text = plists[0].read_text()
    assert value in text, text
    assert "{{ENGRAM_HOME}}" not in text
    assert any(c.startswith("launchctl bootstrap") for c in rig.spy_calls()), rig.spy_calls()
    if shutil.which("plutil"):
        lint = subprocess.run(["plutil", "-lint", str(plists[0])], capture_output=True, text=True)
        assert lint.returncode == 0 and "OK" in lint.stdout, lint.stdout + lint.stderr


# ----------------------------------------------------------------- (d) combination trap


@pytest.mark.parametrize("value_tail, wording", [
    ("a b&c", "an ampersand character"),   # allowed class (space) first, refused class after
    ("a&b c", "an ampersand character"),
    ("a%b|c", "a pipe character"),
    ("a$b\\c", "a backslash character"),
    ("a\"b{{c", "an open-brace-pair character"),
    ("a b<c", "a less-than character"),
    ("a'b>c", "a greater-than character"),
    ("a}}b\tc", "control byte 0x09"),
])
def test_darwin_allowed_plus_refused_is_refused_naming_the_refused(rig, value_tail, wording):
    value = f"{rig.eh_parent}/{value_tail}"
    res = rig.run(value, os_name="Darwin")
    _assert_refused(rig, res, value, wording)
    assert not rig.eh_parent.exists()


@pytest.mark.parametrize("value_tail, wordings", [
    ("a b&c", ("a space character", "an ampersand character")),
    ("a%b|c", ("a percent character", "a pipe character")),
    ("a\"b\\c", ("a double-quote character", "a backslash character")),
])
def test_linux_several_refused_names_one_of_them(rig, value_tail, wordings):
    value = f"{rig.eh_parent}/{value_tail}"
    res = rig.run(value, os_name="Linux")
    out = res.stdout + res.stderr
    assert res.returncode == 2, out
    assert res.stderr.count("ERROR:") == 1, res.stderr
    assert any(w in res.stderr for w in wordings), res.stderr
    assert _q(value) in res.stderr
    assert rig.spy_calls() == [] and rig.written() == []
    assert not rig.eh_parent.exists()


# ----------------------------------------------------------------- (e) refusal leaves nothing


@pytest.mark.parametrize("os_name, ch", [("Linux", " "), ("Linux", "&"), ("Darwin", "&"), ("Darwin", "|")])
def test_refusal_leaves_nothing_behind(rig, os_name, ch):
    value = f"{rig.eh_parent}/h{ch}x"
    before = sorted(str(p) for p in rig.home.rglob("*"))
    res = rig.run(value, os_name=os_name)
    assert res.returncode == 2, res.stdout + res.stderr
    assert not rig.eh_parent.exists()
    assert not Path(value).exists()
    assert rig.written() == []
    assert rig.spy_calls() == []
    assert sorted(str(p) for p in rig.home.rglob("*")) == before  # no LaunchAgents / systemd dir either


# ----------------------------------------------------------------- (f) relative, judged after absolutising
#
# A relative ENGRAM_HOME in the environment is refused earlier (#3863), so the only way the
# `$PWD/` prefix is reached is a relative HOME with ENGRAM_HOME unset (default $HOME/.engram).


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_relative_home_from_cwd_with_ampersand_is_refused_after_absolutising(rig, tmp_path, os_name):
    cwd = tmp_path / "work&dir"
    cwd.mkdir()
    res = rig.run(None, os_name=os_name, cwd=str(cwd), home_env="relhome")
    out = res.stdout + res.stderr
    assert res.returncode == 2, out
    assert res.stderr.count("ERROR:") == 1 and "ampersand" in res.stderr, res.stderr
    assert "ENGRAM_HOME" in res.stderr and _q("work&dir") in res.stderr, res.stderr
    assert rig.spy_calls() == []
    assert list(cwd.rglob("*")) == [], list(cwd.rglob("*"))


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_relative_home_from_clean_cwd_unchanged(rig, tmp_path, os_name):
    cwd = tmp_path / "cleandir"
    cwd.mkdir()
    res = rig.run(None, os_name=os_name, cwd=str(cwd), home_env="relhome")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (cwd / "relhome" / ".engram").is_dir()
    assert rig.spy_calls() != []


# ----------------------------------------------------------------- (g) ordinary path unchanged


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_ordinary_path_unchanged(rig, os_name):
    value = f"{rig.eh_parent}/ordinary-home"
    res = rig.run(value, os_name=os_name)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stderr == "", res.stderr
    assert "=== Setup complete ===" in res.stdout
    assert Path(value).is_dir()
    (written,) = [Path(p) for p in rig.written()]
    assert value in written.read_text()
    assert rig.spy_calls() != []


# ----------------------------------------------------------------- (h) --owner unchanged


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_owner_mode_keeps_its_own_message(rig, os_name):
    value = f"{rig.eh_parent}/a b"
    res = rig.run(value, os_name=os_name, args=["--owner", "--allow-path"])  # allow-path: the owner script sits in the checkout
    assert res.returncode == 2, res.stdout + res.stderr
    assert "unsupported in --owner mode: ENGRAM_HOME contains a space character" in res.stderr
    assert "No unit was written or loaded." in res.stderr
    assert "nothing was created" not in res.stderr
    assert rig.spy_calls() == []


# ----------------------------------------------------------------- (i) the tool check comes first (C2)
#
# The refusal runs inside _surface_compute_home_id, which each OS branch calls only AFTER its
# service-manager tool check. A host without the tool must still fail first, with the tool's own
# message, and must not reach the refusal. Moving the refusal earlier would pass every cell above
# (they all have the tool), so this pins the order. tests/conftest.py shims service-manager tools
# onto PATH, so an empty stub dir is not enough: PATH here is the stub dir plus a dir of symlinks to
# only the tools the script runs before the check (the `hermetic_path` idea of
# tests/test_setup_surface_daemon_service.py, #3333), which makes the tool truly absent.


@pytest.mark.parametrize(
    "os_name, tool, bad_home",
    [("Linux", "systemctl", "a b"), ("Darwin", "launchctl", "a&b")],
)
def test_missing_service_tool_fails_before_the_home_refusal(
    tmp_path, good_hooks, os_name, tool, bad_home
):
    stub = tmp_path / "stub"
    stub.mkdir()
    _exe(stub / "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
    _exe(stub / "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "env", "readlink", "dirname", "sed", "mkdir", "cat", "grep", "tr"):
        found = shutil.which(name)
        assert found, f"hermetic PATH needs {name} on the host"
        (tools / name).symlink_to(found)
    assert not (stub / tool).exists() and not (tools / tool).exists()
    home = tmp_path / "home"
    home.mkdir()
    engram_home = tmp_path / "eh" / bad_home
    env = {
        **os.environ,
        "PATH": f"{stub}:{tools}",
        "HOME": str(home),
        "ENGRAM_HOOKS_DIR": str(good_hooks),
        "ENGRAM_HOME": str(engram_home),
    }
    env.pop("ENGRAM_SETUP_ALLOW_PATH", None)
    res = subprocess.run(["bash", str(SETUP_SCRIPT)], capture_output=True, text=True,
                         env=env, timeout=30)
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"ERROR: {tool} not found" in res.stderr, res.stderr
    assert "ENGRAM_HOME contains" not in res.stderr, res.stderr
    assert not engram_home.exists() and not engram_home.parent.exists()
    assert [p for p in home.rglob("*") if p.suffix in (".plist", ".service")] == []

