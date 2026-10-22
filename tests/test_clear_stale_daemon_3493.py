"""Contract tests for engram-alpha#3493 -- the operator-invoked verb that clears
a crash-stale surface-daemon socket the #3361 startup gate refuses to unlink.

Contract (written before the implementation), script
src/engram/hooks/claude/clear-stale-engram-daemon.sh:

  K1 Reachable ONLY by a human: no launch path, daemon, hook registration or
     supervisor unit template mentions it.
  K2 Resolves the socket and pidfile paths EXACTLY as the daemon does
     (`--print-paths` is the read-only mode the parity test reads).
  K3 Unlinks exactly those two files, only when the socket is a real socket
     and the pidfile a regular file (symlinks refused for both). Neither
     exists -> loud, non-zero, nothing touched. One exists and is right ->
     clear it, say the other was absent, exit 0. A path of the wrong kind ->
     refuse, unlink NOTHING (all-or-nothing), non-zero.
  K4 ONE loud stderr line naming user / tty / parent process and carrying the
     warning; a second line stating which paths were removed.
  K5 No liveness heuristic decides anything; an informational line tells the
     operator to confirm no daemon runs; `--yes` is required (without it:
     print what WOULD be removed, exit 2, unlink nothing).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
SCRIPT = HOOKS_DIR / "clear-stale-engram-daemon.sh"
DAEMON_PY_PATH = HOOKS_DIR / "engram-surface-daemon.py"

WARNING_SUBSTR = (
    "no-holder safety is unproven; this is the human's deliberate recovery "
    "decision, never automatic cleanup"
)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _make_socket_file(path: Path) -> None:
    """A REAL unix socket file: bind, close; the file stays."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(path))
    finally:
        s.close()
    assert path.exists() and not path.is_symlink()


@pytest.fixture
def home(tmp_path):
    # AF_UNIX path limit (104 on macOS): keep the dir short, not under tmp_path.
    d = Path(tempfile.mkdtemp(prefix="cs3493-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _run(home_dir, *args, bash="bash", extra_env=None):
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [bash, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _both(home_dir):
    sock = home_dir / "recall-daemon.sock"
    pid = home_dir / "recall-daemon.pid"
    _make_socket_file(sock)
    pid.write_text(f"{_dead_pid()}\n")
    return sock, pid


def _out(r):
    return r.stdout + r.stderr


# --------------------------------------------------------------------------
def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_T1_yes_removes_both_prints_warning_and_invoker(home):
    sock, pid = _both(home)
    r = _run(home, "--yes")
    assert r.returncode == 0, _out(r)
    assert not os.path.lexists(sock)
    assert not os.path.lexists(pid)
    err = r.stderr
    assert WARNING_SUBSTR in err
    # invoker line: user, tty (not a tty under subprocess), parent process
    import getpass

    assert f"user={getpass.getuser()}" in err
    assert "tty=not a tty" in err
    assert "parent=" in err
    # removal line names exactly the two paths
    assert str(sock) in err and str(pid) in err
    assert re.search(r"removed", err, re.I)
    # exactly one invoker/warning line
    assert sum(1 for ln in err.splitlines() if WARNING_SUBSTR in ln) == 1
    # informational "confirm no daemon runs" line
    assert "engram-surface-daemon" in err


def test_T2_without_yes_removes_nothing_exit_2(home):
    sock, pid = _both(home)
    r = _run(home)
    assert r.returncode == 2, _out(r)
    assert os.path.exists(sock) and os.path.exists(pid)
    assert WARNING_SUBSTR in r.stderr
    assert "would" in r.stderr.lower()
    assert str(sock) in r.stderr and str(pid) in r.stderr
    assert "--yes" in r.stderr


def test_T3_neither_exists_loud_nonzero_touches_nothing(home):
    r = _run(home, "--yes")
    assert r.returncode != 0
    assert "nothing to clear" in r.stderr.lower()
    assert list(home.iterdir()) == []


def test_T4_only_socket_exists_clears_it_says_pidfile_absent(home):
    sock = home / "recall-daemon.sock"
    _make_socket_file(sock)
    r = _run(home, "--yes")
    assert r.returncode == 0, _out(r)
    assert not os.path.lexists(sock)
    assert re.search(r"pidfile.*already absent", r.stderr, re.I), r.stderr


def test_T4b_only_pidfile_exists_clears_it_says_socket_absent(home):
    pid = home / "recall-daemon.pid"
    pid.write_text("1\n")
    r = _run(home, "--yes")
    assert r.returncode == 0, _out(r)
    assert not os.path.lexists(pid)
    assert re.search(r"socket.*already absent", r.stderr, re.I), r.stderr


@pytest.mark.parametrize("kind", ["regular", "directory", "symlink", "dangling"])
def test_T5_wrong_kind_socket_path_refused_nothing_unlinked(home, kind):
    sock = home / "recall-daemon.sock"
    pid = home / "recall-daemon.pid"
    pid.write_text(f"{_dead_pid()}\n")  # a VALID pidfile: must survive
    if kind == "regular":
        sock.write_text("not a socket")
    elif kind == "directory":
        sock.mkdir()
    elif kind == "symlink":
        real = home / "real.sock"
        _make_socket_file(real)
        sock.symlink_to(real)
    else:
        sock.symlink_to(home / "nowhere")
    r = _run(home, "--yes")
    assert r.returncode != 0, _out(r)
    assert os.path.lexists(sock)
    assert os.path.exists(pid), "all-or-nothing: valid pidfile must survive"
    assert "refus" in r.stderr.lower()
    if kind == "symlink":
        assert os.path.exists(home / "real.sock")


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_T6_wrong_kind_pidfile_refused_nothing_unlinked(home, kind):
    sock = home / "recall-daemon.sock"
    pid = home / "recall-daemon.pid"
    _make_socket_file(sock)
    if kind == "symlink":
        target = home / "elsewhere.txt"
        target.write_text("precious")
        pid.symlink_to(target)
    else:
        pid.mkdir()
    r = _run(home, "--yes")
    assert r.returncode != 0, _out(r)
    assert os.path.lexists(pid)
    assert os.path.exists(sock), "all-or-nothing: valid socket must survive"
    assert "refus" in r.stderr.lower()
    if kind == "symlink":
        assert (home / "elsewhere.txt").read_text() == "precious"


def _assert_relative_refused(r, value):
    """alpha #3863 6c: a relative ENGRAM_HOME is refused by the script's launcher guard -- exit 2, the
    one guard line on stderr, nothing on stdout -- instead of being resolved against the cwd."""
    assert r.returncode == 2, _out(r)
    assert r.stdout == "", repr(r.stdout)
    assert r.stderr == "ENGRAM_HOME must be an absolute path, got '%s'\n" % value, repr(r.stderr)


def _load_daemon_for_home_value(home_value, cwd):
    """The daemon module for the home that ENGRAM_HOME=home_value names when
    run from cwd. Since alpha #3863 6b the daemon REFUSES a relative
    ENGRAM_HOME (one stderr line, exit 0) instead of resolving it against its
    cwd, so the oracle is loaded with the ABSOLUTE spelling of that value
    (os.getcwd() is the PHYSICAL cwd, as the daemon's old abspath saw it).
    Since the launcher guards (6c) the shell script under test refuses a
    relative value too (see _assert_relative_refused); the cells that still
    pin that the script agrees with the daemon about the home a value names
    use an ABSOLUTE value."""
    old = os.getcwd()
    try:
        os.chdir(cwd)
        abs_home = os.path.abspath(os.path.expanduser(home_value))
    finally:
        os.chdir(old)
    with unittest.mock.patch.dict(os.environ, {"ENGRAM_HOME": abs_home}):
        return _load_daemon_module()


def _load_daemon_module():
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_clear_3493", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_print_paths(stdout):
    got = {}
    for ln in stdout.splitlines():
        k, _, v = ln.partition("=")
        got[k] = v
    return got


def test_T7_parity_with_daemon_constants(home):
    with unittest.mock.patch.dict(os.environ, {"ENGRAM_HOME": str(home)}):
        mod = _load_daemon_module()
    r = _run(home, "--print-paths")
    assert r.returncode == 0, _out(r)
    got = _parse_print_paths(r.stdout)
    assert got.get("SOCKET_PATH") == mod.SOCKET_PATH
    assert got.get("PID_PATH") == mod.PID_PATH


def test_T7b_parity_relative_and_default_home(home):
    # relative ENGRAM_HOME: refused (alpha #3863 6c; the daemon refuses it since 6b), never resolved
    rel_parent = home
    (rel_parent / "sub").mkdir()
    env = dict(os.environ, ENGRAM_HOME="sub")
    r = subprocess.run(
        ["bash", str(SCRIPT), "--print-paths"],
        capture_output=True, text=True, env=env, cwd=str(rel_parent), timeout=60,
    )
    _assert_relative_refused(r, "sub")
    # unset ENGRAM_HOME -> ~/.engram under HOME
    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    env["HOME"] = str(home)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--print-paths"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    got = _parse_print_paths(r.stdout)
    with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
        os.environ.pop("ENGRAM_HOME", None)
        mod = _load_daemon_module()
    assert got["SOCKET_PATH"] == mod.SOCKET_PATH
    assert got["PID_PATH"] == mod.PID_PATH


def _scan_targets():
    targets = []
    for p in HOOKS_DIR.iterdir():
        if p.is_file() and p.name != SCRIPT.name:
            targets.append(p)
    targets.append(REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json")
    for root in (REPO_ROOT / "src", REPO_ROOT / "deploy"):
        for p in root.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            n = p.name
            if (
                n.endswith((".service", ".timer", ".target", ".plist"))
                or ".service." in n
                or ".plist." in n
                or n.endswith(".service.template")
                or n.endswith(".example")
            ):
                targets.append(p)
    return targets


def test_T8_no_launch_path_or_unit_references_the_script():
    targets = _scan_targets()
    names = {t.name for t in targets}
    # the scan must actually cover the files K1 names
    for must in (
        "start-engram-daemon.sh",
        "engram-surface-daemon.py",
        "hooks.json",
        "engram-surface-daemon.service.template",
        "engram-surface-daemon.plist.template",
    ):
        assert must in names, f"scan does not cover {must}"
    offenders = []
    for t in targets:
        text = t.read_text(errors="replace")
        if "clear-stale" in text or SCRIPT.name in text:
            offenders.append(str(t.relative_to(REPO_ROOT)))
    assert offenders == []


def test_T9_print_paths_prints_two_paths_touches_nothing(home):
    sock, pid = _both(home)
    before = sorted(os.listdir(home))
    r = _run(home, "--print-paths")
    assert r.returncode == 0, _out(r)
    got = _parse_print_paths(r.stdout)
    assert got == {"SOCKET_PATH": str(sock), "PID_PATH": str(pid)}
    assert r.stderr == ""
    assert sorted(os.listdir(home)) == before
    assert os.path.exists(sock) and os.path.exists(pid)
    # and --print-paths on an empty home must not create anything either
    empty = home / "fresh"
    r = _run(empty, "--print-paths")
    assert r.returncode == 0
    assert not empty.exists()


def _bin_bash_major():
    try:
        r = subprocess.run(
            ["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"],
            capture_output=True, text=True, timeout=20,
        )
        return int(r.stdout.strip())
    except Exception:
        return None


@pytest.mark.skipif(
    _bin_bash_major() != 3,
    reason="/bin/bash is not bash 3.x here (the macOS system bash); the "
    "3.2-compat cell is meaningful only where /bin/bash is 3.x",
)
def test_T10_runs_under_bin_bash_3(home):
    sock, pid = _both(home)
    r = _run(home, "--yes", bash="/bin/bash")
    assert r.returncode == 0, _out(r)
    assert not os.path.lexists(sock) and not os.path.lexists(pid)
    assert WARNING_SUBSTR in r.stderr


@pytest.mark.parametrize(
    "args",
    [("--bogus",), ("--yes", "extra"), ("extra",), ("--yes", "--bogus"), ("-y",)],
)
def test_T11_unknown_flags_and_extra_args_refused(home, args):
    sock, pid = _both(home)
    r = _run(home, *args)
    assert r.returncode == 2, _out(r)
    assert os.path.exists(sock) and os.path.exists(pid)
    assert "usage" in r.stderr.lower()


def test_T12_path_with_spaces_and_glob_chars(home):
    weird = home / "sp ace ?x *y"
    weird.mkdir()
    sock, pid = _both(weird)
    # a decoy that an unquoted glob/word-split would hit
    decoy = home / "decoy"
    decoy.write_text("keep")
    r = _run(weird, "--yes")
    assert r.returncode == 0, _out(r)
    assert not os.path.lexists(sock) and not os.path.lexists(pid)
    assert decoy.read_text() == "keep"


def test_tier_and_test_map_entries():
    tiers = json.loads(
        (REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text()
    )
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("path") == "hooks/claude/clear-stale-engram-daemon.sh":
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(tiers)
    assert len(found) == 1
    assert found[0]["tier"] == "essential"
    assert found[0]["dest"] == "hooks/clear-stale-engram-daemon.sh"


@pytest.mark.parametrize(
    "name", ["boot-charge-default-claude.md", "boot-charge-quiet-claude.md"]
)
def test_boot_charge_documents_the_remedy(name):
    text = (REPO_ROOT / "deploy" / "autostart" / name).read_text()
    assert "clear-stale-engram-daemon.sh --yes" in text
    assert "exit 75" in text and "76" in text
    assert WARNING_SUBSTR in text


def test_S1_cdpath_does_not_leak_into_path_resolution(home):
    """A relative ENGRAM_HOME plus CDPATH holding a same-named dir: `cd`
    would resolve through CDPATH and print the dir to stdout. Since 6c the relative value is refused
    before any `cd`: the refusal is the same with CDPATH set, and stdout stays empty."""
    cwd_dir = home / "A"
    cdp_dir = home / "B"
    (cwd_dir / "rel").mkdir(parents=True)
    (cdp_dir / "rel").mkdir(parents=True)
    env = dict(os.environ, ENGRAM_HOME="rel", CDPATH=str(cdp_dir))
    r = subprocess.run(
        ["bash", str(SCRIPT), "--print-paths"],
        capture_output=True, text=True, env=env, cwd=str(cwd_dir), timeout=60,
    )
    _assert_relative_refused(r, "rel")


@pytest.mark.parametrize("shape", ["trailing", "embedded"])
@pytest.mark.parametrize("args", [("--yes",), ("--print-paths",)])
def test_S3_newline_in_engram_home_refused(home, shape, args):
    sib = home / "sib"
    sib.mkdir()
    sock, pid = _both(sib)
    if shape == "trailing":
        value = str(sib) + "\n"
    else:
        value = str(sib) + "\nx"
    r = _run(value, *args)
    assert r.returncode == 2, _out(r)
    assert "newline" in r.stderr.lower()
    assert r.stdout == ""
    assert os.path.exists(sock) and os.path.exists(pid)


def _daemon_paths_for(home_value, cwd):
    """What the daemon computes for ENGRAM_HOME=home_value run from cwd
    (os.getcwd() is PHYSICAL, so abspath joins with the physical cwd)."""
    mod = _load_daemon_for_home_value(home_value, cwd)
    return mod.SOCKET_PATH, mod.PID_PATH


def _run_print_paths_from(cwd, home_value, logical_pwd=None):
    env = dict(os.environ, ENGRAM_HOME=home_value)
    # a real shell exports PWD as the LOGICAL path it cd'd through
    env["PWD"] = str(logical_pwd or cwd)
    return subprocess.run(
        ["bash", str(SCRIPT), "--print-paths"],
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=60,
    )


def _print_paths_from(cwd, home_value, logical_pwd=None):
    r = _run_print_paths_from(cwd, home_value, logical_pwd)
    assert r.returncode == 0, _out(r)
    assert len(r.stdout.splitlines()) == 2, repr(r.stdout)
    got = _parse_print_paths(r.stdout)
    return got["SOCKET_PATH"], got["PID_PATH"]


def test_A1i_relative_home_under_symlinked_cwd_is_refused(home):
    """6c: was "uses the physical cwd"; a relative value is now refused whatever the cwd is."""
    real = home / "real"
    (real / "rel").mkdir(parents=True)
    link = home / "link"
    link.symlink_to(real)
    _assert_relative_refused(_run_print_paths_from(link, "rel"), "rel")


def test_A1ii_absolute_symlinked_home_stays_the_link_path(home):
    real = home / "real2"
    real.mkdir()
    link = home / "link2"
    link.symlink_to(real)
    got = _print_paths_from(home, str(link))
    want = _daemon_paths_for(str(link), home)
    assert got == want
    assert got[0] == str(link) + "/recall-daemon.sock"


def test_A1iii_dotdot_relative_home_under_symlinked_cwd_is_refused(home):
    """6c: was "resolves ../sibling against the physical cwd"; a relative value is now refused."""
    real = home / "real3"
    (real / "sub").mkdir(parents=True)
    (real / "sibling").mkdir()
    link = home / "link3"
    link.symlink_to(real)
    _assert_relative_refused(_run_print_paths_from(link / "sub", "../sibling"), "../sibling")


_FAKE_PGREP = """#!/bin/bash
for a in "$@"; do printf '%s\\n' "$a"; done > "$FAKE_PGREP_ARGV"
printf '%s' "${FAKE_PGREP_PIDS:-}"
"""


def _fake_pgrep_env(home, pids=""):
    bindir = home / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "pgrep"
    fake.write_text(_FAKE_PGREP)
    fake.chmod(0o755)
    argv_file = home / "pgrep-argv.txt"
    if argv_file.exists():
        argv_file.unlink()
    return argv_file, {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_PGREP_ARGV": str(argv_file),
        "FAKE_PGREP_PIDS": pids,
    }


def _recorded_argv(argv_file):
    return argv_file.read_text().splitlines()


def test_A2i_pgrep_pattern_is_scoped_to_the_resolved_home(home):
    sub = home / "h1"
    sub.mkdir()
    _both(sub)
    argv_file, env = _fake_pgrep_env(home)
    r = _run(sub, "--yes", extra_env=env)
    assert r.returncode == 0, _out(r)
    argv = _recorded_argv(argv_file)
    # (ii) -u <uid> -f as before
    assert argv[:3] == ["-u", str(os.getuid()), "-f"], argv
    pattern = argv[3]
    assert pattern.endswith("$")
    assert str(sub) in pattern  # no metacharacters in this home
    assert "engram-surface-daemon" in pattern
    # the info line says which home it was scoped to
    assert str(sub) in [ln for ln in r.stderr.splitlines() if "pgrep" in ln][0]
    assert "information only" in r.stderr


def test_A2iii_metacharacters_in_home_are_escaped(home):
    weird = home / "a.b+(c d)"
    weird.mkdir()
    _both(weird)
    argv_file, env = _fake_pgrep_env(home)
    r = _run(weird, "--yes", extra_env=env)
    assert r.returncode == 0, _out(r)
    pattern = _recorded_argv(argv_file)[3]
    prefix = "python /x/engram-surface-daemon.py "
    # only . + ( ) are used: escaping them with a backslash means the same in
    # Python re and POSIX ERE
    assert re.search(pattern, prefix + str(weird))
    sibling_dot = str(weird).replace("a.b", "aXb")  # '.' would wildcard
    sibling_plus = str(weird).replace("b+(c d)", "bbbc d")  # '+' and '()' too
    sibling_plain = str(weird).replace("a.b+(c d)", "aXbc d")
    for sib in (sibling_dot, sibling_plus, sibling_plain):
        assert not re.search(pattern, prefix + sib), sib
    assert not re.search(pattern, prefix + str(weird) + "X")  # `$` anchors


def test_A2iv_info_line_never_changes_the_exit_code(home):
    codes = {}
    for label, pids in (("none", ""), ("some", "123 456")):
        sub = home / ("h-" + label)
        sub.mkdir()
        _both(sub)
        argv_file, env = _fake_pgrep_env(home, pids)
        r = _run(sub, "--yes", extra_env=env)
        codes[label] = r.returncode
        assert not os.path.lexists(sub / "recall-daemon.sock")
        info = [ln for ln in r.stderr.splitlines() if "pgrep" in ln][0]
        if pids:
            assert "123" in info and "456" in info
    assert codes["none"] == codes["some"] == 0
    # dry-run too
    sub = home / "h-dry"
    sub.mkdir()
    _both(sub)
    _argv, env = _fake_pgrep_env(home, "123")
    assert _run(sub, extra_env=env).returncode == 2


def _boot_paragraph(name):
    text = (REPO_ROOT / "deploy" / "autostart" / name).read_text()
    i = text.index("**Daemon refuses to start")
    return text[i:]


@pytest.mark.parametrize(
    "name", ["boot-charge-default-claude.md", "boot-charge-quiet-claude.md"]
)
def test_S2_boot_charge_never_tells_the_agent_to_pass_yes(name):
    """Least brittle check chosen: (1) the paragraph tells the agent to ask /
    surface to the human; (2) EVERY sentence containing `--yes` also names the
    human and none of them is addressed to the agent as "you ... run"."""
    para = _boot_paragraph(name)
    low = para.lower()
    assert ("ask" in low or "surface" in low)
    assert "human" in low and "Lei" in para
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z*`])", para)
    yes_sents = [x for x in sentences if "--yes" in x]
    assert yes_sents, "the command with --yes must still be documented"
    for sent in yes_sents:
        assert "human" in sent.lower() or "Lei" in sent, sent
        assert not re.search(r"\byou\b[^.]*\brun\b", sent, re.I), sent
        assert not re.search(r"\bafter you\b", sent, re.I), sent
    assert "yourself" in low  # the explicit "never pass --yes yourself"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
