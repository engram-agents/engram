"""Every shipped shell script that reads ENGRAM_HOME refuses a RELATIVE one (alpha #3863, 6c).

A relative ENGRAM_HOME resolves against the CALLING process's cwd, so a hook, the owner daemon and the MCP
server would each land in a different home, silently. The Python resolvers already refuse it
(tests/test_relative_engram_home_refused_3863.py); this file is the SHELL half. The rule, one block copied
into every script (no sourced helper: the plugin build flattens hooks/claude/* -> hooks/*, so a relative
source path would break):

  * unset or empty ENGRAM_HOME: unchanged (empty means unset);
  * a leading `~` (exactly `~` or `~/...`) is expanded to $HOME and RE-EXPORTED, then the absolute check
    applies. `~user/...` is NOT expanded by the shell guard (Python's expanduser would): it is not
    absolute, so it is refused. That is the one divergence from the Python resolvers, pinned below;
  * anything else not starting with `/` is refused BEFORE any side effect;
  * the two SessionStart hook launchers fail OPEN: exit 0, nothing written, stdout empty, ONE stderr line
    `engram hook: ENGRAM_HOME must be an absolute path, got '<value>'; doing nothing`;
  * every other script exits 2 with ONE stderr line `ENGRAM_HOME must be an absolute path, got '<value>'`.

The scope is DEFAULT-DENY (L8): every tracked, shipped *.sh that mentions ENGRAM_HOME must carry the guard
as its first command or be named in ALLOWLIST with a reason. The checker is itself tested on planted files.

Mutation (see the PR body): guard removed from one script; exit 0 on a tool; `~` not expanded; expanded but
not exported; `rel` accepted but `./rel` refused; two stderr lines; guard moved after the first command;
empty treated as relative.
"""
import hashlib
import itertools
import os
import platform
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

HOOK_LAUNCHERS = {"start-engram-daemon.sh", "start-engramd-owner.sh"}
# script -> one-line reason it cannot carry the guard. Starts EMPTY: every discovered script carries it.
ALLOWLIST: dict = {}

GUARD_START = 'if [ -n "${ENGRAM_HOME-}" ]; then'
TOOL_TEXT = "ENGRAM_HOME must be an absolute path, got '%s'"
HOOK_TEXT = "engram hook: ENGRAM_HOME must be an absolute path, got '%s'; doing nothing"


# --------------------------------------------------------------------------------------------------
# discovery + the checker (L8)
# --------------------------------------------------------------------------------------------------
def shipped_scripts(root: Path = REPO):
    """Tracked *.sh that mention ENGRAM_HOME, minus tests/docs/test_* helpers and the root tools/ symlinks."""
    out = subprocess.run(["git", "ls-files", "*.sh"], cwd=str(root), capture_output=True, text=True, check=True)
    found = []
    for rel in out.stdout.splitlines():
        p = root / rel
        parts = Path(rel).parts
        if parts[0] in ("tests", "docs") or p.name.startswith("test_") or p.is_symlink() or not p.is_file():
            continue
        if "ENGRAM_HOME" in p.read_text(errors="replace"):
            found.append(rel)
    return sorted(found)


_PREAMBLE_OK = re.compile(r"^\s*(#.*|set\s+[-+].*|[A-Za-z_][A-Za-z0-9_]*=\S*)?\s*$")


def guard_problems(path: Path):
    """[] when the file carries the guard as its first command, else a list of what is wrong."""
    kind = "hook" if path.name in HOOK_LAUNCHERS else "tool"
    lines = path.read_text(errors="replace").splitlines()
    code = [(i, ln) for i, ln in enumerate(lines) if not ln.lstrip().startswith("#")]
    starts = [i for i, ln in code if ln.strip() == GUARD_START]
    if len(starts) != 1:
        return ["guard block start %r found %d times (want 1) in code lines" % (GUARD_START, len(starts))]
    start = starts[0]
    problems = []
    for i, ln in enumerate(lines[:start]):
        if not _PREAMBLE_OK.match(ln):
            problems.append("a command precedes the guard at line %d: %r" % (i + 1, ln.strip()[:60]))
            break
    end = next((i for i in range(start, len(lines)) if lines[i] == "fi"), None)
    if end is None:
        return problems + ["guard block has no closing `fi` at column 0"]
    block = "\n".join(lines[start:end + 1])
    want = HOOK_TEXT if kind == "hook" else TOOL_TEXT
    if want not in block:
        problems.append("the %s refusal text %r is not inside the guard block" % (kind, want))
    want_exit = "exit 0" if kind == "hook" else "exit 2"
    if want_exit not in block or ("exit 0" if kind == "tool" else "exit 2") in block:
        problems.append("a %s guard must end in `%s` and nowhere else" % (kind, want_exit))
    return problems


SCRIPTS = shipped_scripts()


def test_discovery_finds_the_shipped_scripts():
    names = {Path(s).name for s in SCRIPTS}
    assert len(SCRIPTS) >= 15, SCRIPTS
    assert HOOK_LAUNCHERS <= names, names


@pytest.mark.parametrize("rel", SCRIPTS)
def test_L8_every_shipped_script_carries_the_guard_or_is_allowlisted(rel):
    if rel in ALLOWLIST:
        assert ALLOWLIST[rel].strip(), "an allowlist entry needs a reason"
        return
    assert guard_problems(REPO / rel) == [], rel


def test_L8_the_allowlist_has_no_stale_entries():
    assert set(ALLOWLIST) <= set(SCRIPTS), set(ALLOWLIST) - set(SCRIPTS)


def test_L8_the_hook_blocks_are_identical_and_the_tool_blocks_are_identical():
    blocks = {"hook": set(), "tool": set()}
    for rel in SCRIPTS:
        if rel in ALLOWLIST:
            continue
        kind = "hook" if Path(rel).name in HOOK_LAUNCHERS else "tool"
        blocks[kind].add(_block(REPO / rel))
    assert len(blocks["hook"]) == 1 and len(blocks["tool"]) == 1, {k: len(v) for k, v in blocks.items()}


def _block(path: Path) -> str:
    lines = path.read_text().splitlines()
    start = lines.index(GUARD_START)
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1])


def _planted(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return p


GOOD_TOOL = (
    "#!/usr/bin/env bash\nset -euo pipefail\n"
    + GUARD_START
    + "\n    case \"$ENGRAM_HOME\" in\n        /*) ;;\n        *) printf \"" + TOOL_TEXT.replace("%s", "%s")
    + "\\n\" \"$ENGRAM_HOME\" >&2\n           return 2 2>/dev/null || exit 2 ;;\n    esac\nfi\ntouch x\n"
)


def test_L8_the_checker_accepts_a_well_formed_planted_script(tmp_path):
    assert guard_problems(_planted(tmp_path, "ok.sh", GOOD_TOOL)) == []


def test_L8_the_checker_reports_a_planted_script_with_no_guard(tmp_path):
    p = _planted(tmp_path, "bad.sh", '#!/usr/bin/env bash\nmkdir -p "${ENGRAM_HOME:-$HOME/.engram}/x"\n')
    assert guard_problems(p), "an unguarded script must be reported"


def test_L8_the_checker_reports_a_planted_script_whose_guard_comes_after_a_command(tmp_path):
    p = _planted(tmp_path, "late.sh", GOOD_TOOL.replace("touch x\n", "").replace("set -euo pipefail\n", "set -euo pipefail\ntouch x\n"))
    problems = guard_problems(p)
    assert problems and "precedes the guard" in problems[0], problems


def test_L8_the_checker_does_not_count_the_refusal_text_in_a_comment(tmp_path):
    p = _planted(tmp_path, "cmt.sh", "#!/bin/sh\n# " + GUARD_START + "\n# " + TOOL_TEXT + "\nmkdir x\n")
    assert guard_problems(p)


def test_L8_the_checker_reports_a_wrong_exit_code(tmp_path):
    p = _planted(tmp_path, "rc.sh", GOOD_TOOL.replace("exit 2", "exit 0"))
    assert guard_problems(p)


# --------------------------------------------------------------------------------------------------
# the harness: a scratch HOME and cwd, recording stubs on PATH, a before/after listing
# --------------------------------------------------------------------------------------------------
STUB_NAMES = ["systemctl", "launchctl", "git", "python3", "python", "curl", "claude", "codex", "pgrep",
              "pkill", "ps", "sudo", "gh", "loginctl", "nohup", "wget"]


class Scratch:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.cwd = tmp_path / "cwd"
        self.stubs = tmp_path / "stubs"
        self.log = tmp_path / "stubs" / "calls.log"
        for d in (self.home, self.cwd, self.stubs):
            d.mkdir()
        self.log.write_text("")
        for n in STUB_NAMES:
            f = self.stubs / n
            f.write_text('#!/bin/sh\necho "%s $* | EH=${ENGRAM_HOME-<unset>}" >> "$STUB_LOG"\nexit 0\n' % n)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        # macOS has no `timeout`; the daemon hook runs `timeout 3 systemctl ...` and bails with 127 before any
        # stubbed command runs. A transparent pass-through (no log line) keeps the fixture host-independent.
        shim = self.stubs / "timeout"
        shim.write_text('#!/bin/sh\nshift\nexec "$@"\n')
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def snapshot(self):
        """relpath -> content hash (or 'dir') for everything under HOME and cwd."""
        snap = {}
        for base in (self.home, self.cwd):
            for p in sorted(base.rglob("*")):
                rel = str(p.relative_to(self.root))
                snap[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    def calls(self):
        return self.log.read_text()


def run(scr, script_rel, value, args=(), stdin="", cmd=None, timeout=60):
    """value None = ENGRAM_HOME unset. Returns the CompletedProcess."""
    env = {"PATH": "%s:/usr/bin:/bin" % scr.stubs, "HOME": str(scr.home), "USER": "tester", "LC_ALL": "C",
           "STUB_LOG": str(scr.log)}
    if value is not None:
        env["ENGRAM_HOME"] = value
    script = str(REPO / script_rel)
    argv = cmd(script) if cmd else ["bash", script, *args]
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, cwd=str(scr.cwd),
                          timeout=timeout)


def test_the_timeout_stub_drops_the_duration_and_runs_the_rest(tmp_path):
    """The fixture ships its own `timeout` so cells do not depend on GNU coreutils (macOS has none): the
    shim drops the duration and execs the remaining argv, and stays out of the stub call log."""
    scr = Scratch(tmp_path)
    shim = str(scr.stubs / "timeout")
    r = subprocess.run([shim, "3", "/bin/echo", "hello", "world"], capture_output=True, text=True, timeout=30)
    assert (r.returncode, r.stdout) == (0, "hello world\n"), (r.returncode, r.stdout, r.stderr)
    r = subprocess.run([shim, "3", "/bin/sh", "-c", "exit 7"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 7, r.returncode  # the wrapped command's status, not a blanket 0
    assert scr.calls() == "", "the timeout shim must be transparent, not a logged command"


def is_hook(rel):
    return Path(rel).name in HOOK_LAUNCHERS


def refusal_line(rel, value):
    return (HOOK_TEXT if is_hook(rel) else TOOL_TEXT) % value


RELATIVE_VALUES = ["rel/store", "./x", "..", "x", "~user/x"]
SESSION_PAYLOAD = '{"session_id":"s1","transcript_path":"/nonexistent/t.jsonl","cwd":"/nonexistent","hook_event_name":"SessionStart","source":"startup"}'


# L1 + L5: refusal, exact rc/stdout/stderr, nothing created, no external command ran
@pytest.mark.parametrize("args", [(), ("--help",)], ids=["noargs", "help"])
@pytest.mark.parametrize("value", RELATIVE_VALUES)
@pytest.mark.parametrize("rel", SCRIPTS)
def test_L1_L5_a_relative_engram_home_is_refused_before_any_side_effect(rel, value, args, tmp_path):
    scr = Scratch(tmp_path)
    before = scr.snapshot()
    r = run(scr, rel, value, args, stdin=SESSION_PAYLOAD if is_hook(rel) else "")
    assert r.returncode == (0 if is_hook(rel) else 2), (r.returncode, r.stderr[-400:])
    assert r.stdout == "", r.stdout[:300]
    assert r.stderr == refusal_line(rel, value) + "\n", repr(r.stderr[-400:])
    assert scr.snapshot() == before, "the refusal created or changed something"
    assert scr.calls() == "", "an external command ran before the refusal: " + scr.calls()[:300]


# --------------------------------------------------------------------------------------------------
# L2/L3/L4: the guard lets a valid home through. One probe per script: what it is run with, and the
# evidence that it got past the guard (not that it succeeded).
# --------------------------------------------------------------------------------------------------
def _venv_stub(login_home):
    """The plugin venv is ~/.engram/venv whatever ENGRAM_HOME is (Borges' ruling on #3863; 6b merged
    that into launch-engram-server.sh), so the stub sits under the LOGIN home, not under ENGRAM_HOME."""
    py = login_home / ".engram" / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "venvpy $* | EH=$ENGRAM_HOME" >> "$STUB_LOG"\nexit 0\n')
    py.chmod(0o755)


def _seat_run_py_stub(login_home):
    """seat-run.sh runs $HOME/.engram/venv/bin/python (ENGRAM_PY's default) for a version and a pytest probe
    before its first `git` call; a stub that prints a 3.x version lets it reach the stubbed git."""
    py = login_home / ".engram" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "3.12.0"\nexit 0\n')
    py.chmod(0o755)


def _mk(home):
    home.mkdir(parents=True, exist_ok=True)


def _mk_marketplace(home):
    (home / "marketplace").mkdir(parents=True)


class Probe:
    def __init__(self, args=(), prep=None, evidence=None, env_proof=False, stdin="", cmd=None, login_prep=None):
        self.args, self.prep, self.evidence, self.env_proof, self.stdin, self.cmd = (
            args, prep, evidence, env_proof, stdin, cmd)
        self.login_prep = login_prep  # called with the scratch LOGIN home (HOME), not ENGRAM_HOME


def _log(r, scr):
    return scr.calls()


def _stdout(r, scr):
    return r.stdout


def _stderr(r, scr):
    return r.stderr


def _all(r, scr):
    return r.stdout + r.stderr + scr.calls()


def _service_manager():
    """The service-manager command the surface script calls on THIS host (it reads `uname -s`, which the
    harness does not stub)."""
    return "launchctl" if platform.system() == "Darwin" else "systemctl"


def _svc_log(r, scr):
    """Only the log lines the SCRIPT made to the service manager. The `python3` stub also writes a line to
    the log (its discovery probe), so the whole log proves nothing about home handling (#4349)."""
    prefix = _service_manager() + " "
    return "".join(ln + "\n" for ln in scr.calls().splitlines() if ln.startswith(prefix))


SURFACE = "src/engram/tools/setup-surface-daemon-service.sh"

PROBES = {
    "src/engram/hooks/claude/start-engram-daemon.sh": Probe(prep=_mk, evidence=_log, env_proof=True, stdin=SESSION_PAYLOAD),
    "src/engram/hooks/claude/start-engramd-owner.sh": Probe(prep=_mk, evidence=_log, env_proof=True, stdin=SESSION_PAYLOAD),
    "src/engram/launch-engram-server.sh": Probe(login_prep=_venv_stub, evidence=_log, env_proof=True),
    "src/engram/hooks/claude/clear-stale-engram-daemon.sh": Probe(evidence=_stderr),
    "src/engram/tools/engramd-owner-home-key.sh": Probe(
        prep=_mk, evidence=_all,
        cmd=lambda s: ["bash", "-c", '. "$1"; printf "%s\\n" "$ENGRAM_HOME"; engramd_owner_home_key "$ENGRAM_HOME"', "_", s]),
    # login_prep=_venv_stub: without the venv python the script's discovery runs the `python3` stub, gets an empty
    # string and exits 1 before any home handling (#4349). --allow-path: the #3519 ephemeral-path refusal runs
    # before the OS branch and fires when the repo under test sits in a temp root or `.claude/worktrees/`.
    SURFACE: Probe(prep=_mk, args=("--allow-path",), login_prep=_venv_stub, evidence=_svc_log, env_proof=True),
    "src/engram/tools/operator-setup-viz.sh": Probe(prep=_mk, evidence=_log, env_proof=True),
    "src/engram/tools/install-local-marketplace.sh": Probe(
        prep=_mk, args=("--skip-build", "--skip-add", "--allow-untagged"), evidence=_log, env_proof=True),
    "src/engram/tools/install.sh": Probe(prep=_mk, evidence=_log, env_proof=True),
    "src/engram/tools/uninstall-local-marketplace.sh": Probe(
        prep=_mk_marketplace, args=("--dry-run", "--skip-cli"), evidence=_stdout),
    "src/engram/tools/engram-fix-git-backup.sh": Probe(prep=_mk, args=("--dry-run",), evidence=_all),
    "src/engram/tools/forum-mention-monitor.sh": Probe(args=("--help",), evidence=_stdout),
    "src/engram/tools/forum-updates-monitor.sh": Probe(args=("--help",), evidence=_stdout),
    "src/engram/tools/ghpr.sh": Probe(evidence=_log, env_proof=True),
    "src/engram/tools/seat-run.sh": Probe(args=("--ref", "HEAD"), login_prep=_seat_run_py_stub, evidence=_log, env_proof=True),
    "src/engram/tools/run-all-tests.sh": Probe(evidence=_log, env_proof=True),
    "src/forum/deploy/install-forum-service.sh": Probe(args=("--help",), evidence=_stdout),
}


def test_every_discovered_script_has_a_probe():
    assert set(SCRIPTS) <= set(PROBES), sorted(set(SCRIPTS) - set(PROBES))


def _probe_run(rel, scr, value, effective_home):
    pr = PROBES[rel]
    if pr.prep and effective_home is not None:
        pr.prep(effective_home)
    if pr.login_prep and effective_home is not None:
        pr.login_prep(scr.home)
    return run(scr, rel, value, pr.args, stdin=pr.stdin, cmd=pr.cmd)


def _past_the_guard(rel, scr, r):
    refusal = [ln for ln in r.stderr.splitlines() if "must be an absolute path" in ln]
    assert refusal == [], refusal
    assert not (r.returncode == 2 and not is_hook(rel) and "ENGRAM_HOME" in r.stderr), r.stderr[-300:]
    ev = PROBES[rel].evidence(r, scr)
    assert ev.strip(), "no evidence the script got past the guard: rc=%s stderr=%r" % (r.returncode, r.stderr[-200:])
    return ev


@pytest.mark.parametrize("rel", SCRIPTS)
def test_L2_an_absolute_engram_home_proceeds_past_the_guard(rel, tmp_path):
    scr = Scratch(tmp_path)
    home = tmp_path / "abs" / "store"
    r = _probe_run(rel, scr, str(home), home)
    ev = _past_the_guard(rel, scr, r)
    if rel.endswith("clear-stale-engram-daemon.sh") or PROBES[rel].evidence in (_log, _all, _svc_log) or "uninstall" in rel:
        assert str(home) in ev, ev[:400]


@pytest.mark.parametrize("rel", SCRIPTS)
def test_L2_an_absolute_engram_home_containing_a_space_is_not_refused(rel, tmp_path):
    scr = Scratch(tmp_path)
    home = tmp_path / "x y" / "store"
    if rel == SURFACE:
        return _surface_space_cell(scr, home)
    r = _probe_run(rel, scr, str(home), home)
    _past_the_guard(rel, scr, r)


def _surface_space_cell(scr, home):
    """The surface script allows a space in ENGRAM_HOME on Darwin (the plist carries it) and refuses it on
    Linux (#4336: systemd splits on whitespace). The expectation follows the host the script will read."""
    pr = PROBES[SURFACE]
    pr.prep(home)
    if pr.login_prep:
        pr.login_prep(scr.home)
    before = scr.snapshot()
    r = run(scr, SURFACE, str(home), pr.args)
    if platform.system() == "Darwin":
        ev = _past_the_guard(SURFACE, scr, r)
        assert "launchctl " in ev, ev[:400]
        assert r.returncode == 0, (r.returncode, r.stderr[-300:])
        return
    # count the ERROR line by prefix: --allow-path adds a WARN line on stderr when the repo sits in a temp root
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("ERROR:")]
    assert r.returncode == 2, (r.returncode, r.stderr[-300:])
    assert len(lines) == 1 and "ENGRAM_HOME contains a space character" in lines[0], r.stderr[-400:]
    assert _svc_log(r, scr) == "", "a service-manager call ran before the refusal: " + scr.calls()[:300]
    assert scr.snapshot() == before, "the refusal wrote something under HOME or cwd"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
@pytest.mark.parametrize("rel", SCRIPTS)
def test_L3_unset_and_empty_behave_as_unset(rel, value, tmp_path):
    scr = Scratch(tmp_path)
    home = scr.home / ".engram"
    r = _probe_run(rel, scr, value, home)
    ev = _past_the_guard(rel, scr, r)
    assert r.returncode != 124
    # whatever ENGRAM_HOME a child saw is what it saw before the guard existed: unset, or the default home
    # (the guard exports nothing when there is nothing to expand)
    seen = set(re.findall(r"EH=(\S+)", scr.calls()))
    assert seen <= {"<unset>", str(home)}, seen
    if "uninstall" in rel:
        assert str(home) in ev, ev[:400]


@pytest.mark.parametrize("raw", ["~", "~/store"], ids=["tilde", "tilde-slash"])
@pytest.mark.parametrize("rel", SCRIPTS)
def test_L4_a_leading_tilde_expands_to_HOME_and_is_accepted(rel, raw, tmp_path):
    scr = Scratch(tmp_path)
    home = scr.home if raw == "~" else scr.home / "store"
    r = _probe_run(rel, scr, raw, home)
    ev = _past_the_guard(rel, scr, r)
    pr = PROBES[rel]
    if pr.env_proof:
        # a child process (the recording stub) saw the EXPANDED value in its environment
        assert ("EH=%s" % home) in ev, ev[:400]
        assert "EH=~" not in scr.calls()
    elif pr.evidence in (_stdout, _stderr, _all) and "monitor" not in rel and "forum-service" not in rel:
        assert str(home) in ev, ev[:400]


# the block itself, lifted out of each script and run alone: set state, export, tilde, sourced mode
_COUNTER = itertools.count()


def _run_block(rel, tmp_path, value, prefix="", suffix=""):
    sub = tmp_path / ("b%d" % next(_COUNTER))
    sub.mkdir()
    scr = Scratch(sub)
    block = _block(REPO / rel)
    code = prefix + block + "\n" + suffix
    env = {"PATH": "/usr/bin:/bin", "HOME": str(scr.home), "LC_ALL": "C"}
    if value is not None:
        env["ENGRAM_HOME"] = value
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env, cwd=str(scr.cwd), timeout=30)
    r.home = str(scr.home)
    return r


@pytest.mark.parametrize("raw,expect", [("~", "{home}"), ("~/store", "{home}/store"), ("/abs/p q", "/abs/p q")])
@pytest.mark.parametrize("rel", SCRIPTS)
def test_L4_the_block_exports_the_expanded_value_to_children(rel, raw, expect, tmp_path):
    r = _run_block(rel, tmp_path, raw, suffix='env | grep "^ENGRAM_HOME=" ')
    home = r.home
    assert r.returncode == 0, r.stderr
    assert r.stdout == "ENGRAM_HOME=" + expect.format(home=home) + "\n", r.stdout


@pytest.mark.parametrize("raw,expect", [("~", "{home}"), ("~/store", "{home}/store")])
@pytest.mark.parametrize("rel", SCRIPTS)
def test_L4_the_expanded_value_is_exported_even_when_the_caller_held_it_unexported(rel, raw, expect, tmp_path):
    """bash keeps the export attribute of a variable imported from the environment, so the `export` in the
    guard only matters when the variable is a plain shell variable (a sourced script, a caller that set it
    without exporting): then a child must still see the EXPANDED value."""
    r = _run_block(rel, tmp_path, None, prefix='ENGRAM_HOME="%s"\n' % raw, suffix='env | grep "^ENGRAM_HOME=" ')
    assert r.returncode == 0, r.stderr
    assert r.stdout == "ENGRAM_HOME=" + expect.format(home=r.home) + "\n", r.stdout


@pytest.mark.parametrize("rel", SCRIPTS)
def test_L4_the_block_leaves_the_shells_set_options_alone(rel, tmp_path):
    r = _run_block(rel, tmp_path, None, prefix="set -euo pipefail\n", suffix='echo "$-"; set -o | grep pipefail')
    assert r.returncode == 0, r.stderr
    plain = _run_block(rel, tmp_path, None, prefix="set -euo pipefail\n", suffix="").returncode
    assert plain == 0
    flags, pipefail = r.stdout.splitlines()
    assert set("eu") <= set(flags) and "on" in pipefail, r.stdout


@pytest.mark.parametrize("rel", SCRIPTS)
def test_the_block_survives_set_u_with_ENGRAM_HOME_unset_and_empty(rel, tmp_path):
    for v in (None, ""):
        r = _run_block(rel, tmp_path, v, prefix="set -u\n", suffix="echo ok")
        assert (r.returncode, r.stdout, r.stderr) == (0, "ok\n", ""), (v, r)


def test_L4_documented_divergence_tilde_user_is_not_expanded_by_the_shell_guard(tmp_path):
    """Python's os.path.expanduser would turn ~user/x into the user's home; the shell guard does not (it
    would need getent): `~user/x` is not absolute, so it is refused. A one-line divergence, on purpose."""
    scr = Scratch(tmp_path)
    r = run(scr, "src/engram/tools/forum-mention-monitor.sh", "~root/x")
    assert r.returncode == 2 and r.stderr == TOOL_TEXT % "~root/x" + "\n"


# the sourced script: it must RETURN to its caller, never exit it, and must run the same when executed
HOME_KEY = "src/engram/tools/engramd-owner-home-key.sh"


def test_sourced_a_relative_home_returns_2_to_the_caller_which_keeps_running(tmp_path):
    scr = Scratch(tmp_path)
    r = run(scr, HOME_KEY, "rel/store",
            cmd=lambda s: ["bash", "-c", '. "$1"; echo "sourced-rc=$?"; echo caller-still-running', "_", s])
    assert r.stdout == "sourced-rc=2\ncaller-still-running\n", r.stdout
    assert r.stderr == TOOL_TEXT % "rel/store" + "\n"
    assert r.returncode == 0


def test_sourced_a_valid_home_defines_the_function_and_the_caller_continues(tmp_path):
    scr = Scratch(tmp_path)
    r = run(scr, HOME_KEY, str(scr.home),
            cmd=lambda s: ["bash", "-c", '. "$1"; type -t engramd_owner_home_key', "_", s])
    assert r.stdout == "function\n", r.stdout
    assert r.stderr == ""


def test_executed_a_relative_home_exits_2(tmp_path):
    scr = Scratch(tmp_path)
    r = run(scr, HOME_KEY, "rel/store", args=(str(scr.home),))
    assert (r.returncode, r.stdout, r.stderr) == (2, "", TOOL_TEXT % "rel/store" + "\n")


def test_executed_the_key_is_still_derived_from_the_argument(tmp_path):
    scr = Scratch(tmp_path)
    r = run(scr, HOME_KEY, None, args=(str(scr.home),))
    assert r.returncode == 0 and re.fullmatch(r"[0-9a-f]{8}\n", r.stdout), (r.stdout, r.stderr)


def test_the_sourcing_callers_are_exactly_the_two_known_ones():
    """Both callers source the helper after their own guard (so the helper's `return` never meets a relative
    value there); this pins who sources it, so a third sourcer is noticed."""
    sourcers = sorted(rel for rel in SCRIPTS if re.search(r"^\s*(\.|source)\s+[\"']?\$_home_key_sh|\. \"\$HOME_KEY_SH\"|source \"\$HOME_KEY_SH\"",
                                                           (REPO / rel).read_text(), re.M))
    assert set(sourcers) == {"src/engram/hooks/claude/start-engramd-owner.sh",
                             "src/engram/tools/setup-surface-daemon-service.sh"}, sourcers


# --------------------------------------------------------------------------------------------------
# the monitors: a Monitor reads their STDOUT as events, so a refusal must leave stdout empty
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["src/engram/tools/forum-mention-monitor.sh", "src/engram/tools/forum-updates-monitor.sh"])
def test_a_monitor_refusal_is_stderr_only_and_exit_2(rel, tmp_path):
    scr = Scratch(tmp_path)
    r = run(scr, rel, "rel/store", ("--purpose", "smoke"))
    assert (r.returncode, r.stdout) == (2, "")
    assert r.stderr == TOOL_TEXT % "rel/store" + "\n"
    assert scr.calls() == ""


# the hooks are not registered as exit-2 anything: Claude Code treats a non-zero hook exit as an error
def test_the_hook_launchers_are_exactly_the_registered_shell_hooks():
    import json
    cmds = []

    def walk(x):
        if isinstance(x, dict):
            if "command" in x:
                cmds.append(x["command"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(json.loads((REPO / "src" / "engram" / "hooks" / "hooks.json").read_text()))
    registered = {Path(m.group(1)).name for c in cmds for m in [re.search(r"hooks/([\w\-\.]+\.sh)", c)] if m}
    assert registered == HOOK_LAUNCHERS, registered
