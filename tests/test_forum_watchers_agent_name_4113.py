"""Tests for #4113 — forum watchers resolve the agent name through the shared
chain (forum.py ``_get_agent_name``) and refuse loudly when it is empty.

Defect: forum-updates-monitor.sh / forum-mention-monitor.sh built SELF from
config.json["agent_name"] only. No install path writes it, so on a new seat SELF
was empty: ``/api/updates?agent=&...`` (HTTP 400) and ``/api/agent//mentions``,
while the watcher stayed alive printing "cannot seed" and the supervisor read PASS.

Contract (stated BEFORE the fix):
  W1-W4,W6  SELF follows config agent_name (stripped, wins) > $USER > $LOGNAME,
            agent- prefix stripped only when present, URL-encoded.
  W5        When the name cannot be resolved the watcher prints ONE stdout line
            ("cannot resolve this agent's name" + config path), exits 2, and
            never calls curl.
  F1-F3     ``forum.py whoami`` prints the name (rc 0) or nothing (rc 2 + one
            stderr line); no network; works without config.json.

curl is a stub on PATH that records the arguments of every call.
"""

from __future__ import annotations

import json
import os
import stat
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"
_UPDATES = _TOOLS / "forum-updates-monitor.sh"
_MENTION = _TOOLS / "forum-mention-monitor.sh"
_FORUM_PY = _TOOLS / "forum.py"

_CURL_STUB = """#!{python}
import sys
with open({log!r}, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(7)
"""

WATCHERS = [("updates", _UPDATES), ("mention", _MENTION)]

# What the real `forum.py whoami` writes to stderr when every source is empty
# (kept in step with cmd_whoami; the watchers match the stable substring "all empty").
_ALL_EMPTY_ERR = ("forum whoami: cannot resolve this agent's name "
                  "(config agent_name, $USER, $LOGNAME, account name all empty)")


def _env(tmp_path: Path, extra: dict) -> dict:
    """Explicit env: nothing inherited except PATH; no real USER/LOGNAME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    stub = bindir / "curl"
    stub.write_text(_CURL_STUB.format(python=sys.executable, log=str(log)))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "ENGRAM_HOME": str(home / ".engram"),
        "FORUM_UPDATES_NO_REAP": "1",
        "FORUM_MENTION_NO_REAP": "1",
        "FORUM_UPDATES_CURSOR_FILE": str(tmp_path / "cursor"),
        "FORUM_MENTION_SEEN_FILE": str(tmp_path / "seen"),
        "FORUM_UPDATES_POLL_SECONDS": "0.2",
        "FORUM_MENTION_POLL_SECONDS": "0.2",
    }
    env.update(extra)
    return env


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    eh = tmp_path / "home" / ".engram"
    eh.mkdir(parents=True, exist_ok=True)
    p = eh / "config.json"
    p.write_text(json.dumps(cfg))
    return p


def _first_url(tmp_path: Path, script: Path, env: dict, timeout: float = 10.0) -> str:
    """Run the watcher until the curl stub has logged a call; return that line."""
    log = tmp_path / "curl.log"
    proc = subprocess.Popen(
        ["bash", str(script)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log.exists() and log.read_text().strip():
                return log.read_text().splitlines()[0]
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError("watcher made no curl call (exited or timed out)")
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _target(which: str, name: str) -> str:
    return f"agent={name}&" if which == "updates" else f"/api/agent/{name}/mentions"


def _check(line: str, which: str, name: str) -> None:
    assert _target(which, name) in line, line
    if which == "updates":
        assert "agent=&" not in line, line
    else:
        assert "/api/agent//mentions" not in line, line


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w1_user_with_agent_prefix(tmp_path, which, script):
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    line = _first_url(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    _check(line, which, "zed")


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w2_config_wins_and_is_url_encoded(tmp_path, which, script):
    _write_config(tmp_path, {"agent_name": "Halley Two",
                             "forum": {"url": "http://127.0.0.1:9"}})
    line = _first_url(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    _check(line, which, "Halley%20Two")


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w3_logname_fallback(tmp_path, which, script):
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    line = _first_url(tmp_path, script, _env(tmp_path, {"LOGNAME": "agent-lux"}))
    _check(line, which, "lux")


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w4_no_prefix_kept(tmp_path, which, script):
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    line = _first_url(tmp_path, script, _env(tmp_path, {"USER": "plainuser"}))
    _check(line, which, "plainuser")


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w6_whitespace_config_does_not_win(tmp_path, which, script):
    _write_config(tmp_path, {"agent_name": "   ",
                             "forum": {"url": "http://127.0.0.1:9"}})
    line = _first_url(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    _check(line, which, "zed")


@pytest.mark.parametrize("which,script", WATCHERS)
def test_w5_refuses_loudly_when_name_unresolvable(tmp_path, which, script):
    """A copy of the watcher beside a stub forum.py whose `whoami` exits 2."""
    cfg = _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    d = tmp_path / "copy"
    d.mkdir()
    copy = d / script.name
    copy.write_text(script.read_text())
    (d / "forum.py").write_text(
        "import sys\n"
        "if sys.argv[1:2] == ['whoami']:\n"
        "    sys.stderr.write(" + repr(_ALL_EMPTY_ERR) + ")\n"
        "    sys.exit(2)\n"
        "sys.exit(9)\n"
    )
    env = _env(tmp_path, {"USER": "agent-zed"})
    proc = subprocess.Popen(
        ["bash", str(copy)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("watcher did not exit: it kept running with an empty name")
    assert proc.returncode == 2, (proc.returncode, out)
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1, lines
    assert "cannot resolve this agent's name" in lines[0], lines
    assert str(cfg) in lines[0], lines
    assert script.name in lines[0], lines
    log = tmp_path / "curl.log"
    assert not log.exists() or not log.read_text().strip(), "curl was called"


def _run_with_stub(tmp_path, script, stub_src, user="agent-zed"):
    """Run a copy of the watcher beside a stub forum.py; return (rc, stdout lines)."""
    d = tmp_path / "copy"
    d.mkdir()
    copy = d / script.name
    copy.write_text(script.read_text())
    (d / "forum.py").write_text(stub_src)
    proc = subprocess.Popen(
        ["bash", str(copy)], env=_env(tmp_path, {"USER": user}),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("watcher did not exit: it kept running after a whoami failure")
    log = tmp_path / "curl.log"
    assert not log.exists() or not log.read_text().strip(), "curl was called"
    return proc.returncode, [l for l in out.splitlines() if l.strip()]


@pytest.mark.parametrize("which,script", WATCHERS)
def test_a1_old_forum_py_names_the_real_cause(tmp_path, which, script):
    """An old forum.py (no `whoami`): argparse rc 2 + 'invalid choice'. The refusal
    must carry that reason, not blame empty sources or advise setting agent_name."""
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    stub = (
        "import sys\n"
        "sys.stderr.write('usage: forum.py [-h] subcommand ...\\n"
        "forum.py: error: argument subcommand: invalid choice: \\'whoami\\' "
        "(choose from \\'read\\', \\'post\\')\\n')\n"
        "sys.exit(2)\n"
    )
    rc, lines = _run_with_stub(tmp_path, script, stub)
    assert rc == 2, (rc, lines)
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"\u26a0\ufe0f  {script.name}: cannot resolve this agent's name"), lines
    assert "invalid choice: 'whoami'" in lines[0], lines
    assert "forum.py whoami failed" in lines[0], lines
    assert "all empty" not in lines[0], lines
    assert "forum.py beside this script is current" in lines[0], lines


@pytest.mark.parametrize("which,script", WATCHERS)
def test_a2_crash_with_empty_stderr_reports_rc(tmp_path, which, script):
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    rc, lines = _run_with_stub(
        tmp_path, script,
        "import sys\nif sys.argv[1:2] == ['whoami']:\n    sys.exit(1)\nsys.exit(9)\n")
    assert rc == 2, (rc, lines)
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"\u26a0\ufe0f  {script.name}: cannot resolve this agent's name"), lines
    assert "rc=1" in lines[0], lines
    assert "all empty" not in lines[0], lines


@pytest.mark.parametrize("which,script", WATCHERS)
def test_a3_all_empty_keeps_its_message(tmp_path, which, script):
    cfg = _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    rc, lines = _run_with_stub(
        tmp_path, script,
        "import sys\nif sys.argv[1:2] == ['whoami']:\n"
        f"    sys.stderr.write({_ALL_EMPTY_ERR!r} + '\\n')\n    sys.exit(2)\nsys.exit(9)\n")
    assert rc == 2, (rc, lines)
    assert len(lines) == 1, lines
    assert "all empty" in lines[0], lines
    assert "set \"agent_name\"" in lines[0], lines
    assert str(cfg) in lines[0], lines


def _stdout_until_curl(tmp_path, script, env, timeout=10.0):
    """Run the watcher until the curl stub logs a call; return (stdout so far, log line)."""
    log = tmp_path / "curl.log"
    out_file = tmp_path / "watcher.out"
    with open(out_file, "w") as fo:
        proc = subprocess.Popen(["bash", str(script)], env=env,
                                stdout=fo, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if log.exists() and log.read_text().strip():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("watcher made no curl call (timed out)")
        finally:
            proc.kill()
            proc.wait(timeout=5)
    assert log.exists() and log.read_text().strip(), "watcher made no curl call"
    return out_file.read_text(), log.read_text().splitlines()[0]


@pytest.mark.parametrize("which,script", WATCHERS)
def test_b1_derived_name_is_announced(tmp_path, which, script):
    cfg = _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    out, line = _stdout_until_curl(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    _check(line, which, "zed")
    notes = [l for l in out.splitlines() if "polling as" in l]
    assert len(notes) == 1, out
    assert notes[0].startswith(f"\u2139\ufe0f  {script.name}: polling as 'zed' (no agent_name in {cfg};"), notes
    assert notes[0].endswith('set "agent_name" there to pin it'), notes


@pytest.mark.parametrize("which,script", WATCHERS)
def test_b1_missing_config_is_announced(tmp_path, which, script):
    """No config.json at all counts as 'no agent_name' (forum URL falls back too)."""
    out, line = _stdout_until_curl(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    assert "polling as 'zed'" in out, out


@pytest.mark.parametrize("which,script", WATCHERS)
def test_b2_config_name_is_not_announced(tmp_path, which, script):
    _write_config(tmp_path, {"agent_name": "Halley", "forum": {"url": "http://127.0.0.1:9"}})
    out, line = _stdout_until_curl(tmp_path, script, _env(tmp_path, {"USER": "agent-zed"}))
    _check(line, which, "Halley")
    assert "polling as" not in out, out


# --- forum.py whoami ------------------------------------------------------

# forum.py is run in a SUBPROCESS, never imported. It inserts tools/ into
# sys.path at import time and imports sibling modules, and the bare name
# ``forum`` collides with the real package src/forum/ (see the hazard note in
# tests/test_forum_db_hostname.py). A subprocess leaves this process's
# sys.path / sys.modules untouched.
#
# The runner executes the REAL forum.py (runpy, __main__) after stubbing what a
# subprocess cannot otherwise control: the account-name lookup (pwd.getpwuid)
# and the network (urlopen / any connect raises).

_RUNNER = """
import os, pwd, runpy, sys
import http.client, urllib.request

def _no_net(*a, **k):
    raise AssertionError("whoami must not touch the network")
urllib.request.urlopen = _no_net
http.client.HTTPConnection.connect = _no_net

_pw = os.environ.pop("T4113_PW", None)
if _pw is not None:
    class _PW:
        pw_name = _pw
    def _getpwuid(uid):
        if _pw == "":
            raise KeyError(uid)
        return _PW()
    pwd.getpwuid = _getpwuid

sys.argv = ["forum.py", "whoami"]
runpy.run_path(os.environ.pop("T4113_FORUM"), run_name="__main__")
"""


def _run_whoami(tmp_path, cfg=None, env_extra=None, pw=None):
    """Run the real forum.py whoami in a subprocess; return CompletedProcess.

    Env is explicit (no inherited USER/LOGNAME). ``pw``: None = real pwd
    lookup; "" = lookup raises KeyError; other = stubbed pw_name.
    """
    eh = tmp_path / "eh"
    eh.mkdir(exist_ok=True)
    if cfg is not None:
        (eh / "config.json").write_text(json.dumps(cfg))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "ENGRAM_HOME": str(eh), "T4113_FORUM": str(_FORUM_PY)}
    if pw is not None:
        env["T4113_PW"] = pw
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-I", "-c", _RUNNER], env=env,
                          capture_output=True, text=True, timeout=15)


def test_f1_whoami_config_name_no_network(tmp_path):
    r = _run_whoami(
        tmp_path,
        cfg={"agent_name": "Halley Two", "forum": {"url": "http://10.255.255.1:9"}},
        env_extra={"USER": "agent-zed"})
    assert r.returncode == 0, r
    assert r.stdout == "Halley Two\n"


def test_f1_whoami_cli_without_forum_url(tmp_path):
    """Real CLI: no forum.url, no FORUM_URL — whoami still answers, fast."""
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / "config.json").write_text(json.dumps({"agent_name": "cfgname"}))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "ENGRAM_HOME": str(eh), "USER": "agent-zed"}
    r = subprocess.run([sys.executable, str(_FORUM_PY), "whoami"], env=env,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r
    assert r.stdout == "cfgname\n"


def test_f2_whoami_all_empty(tmp_path):
    r = _run_whoami(tmp_path, pw="")
    assert r.returncode == 2, r
    assert r.stdout == ""
    assert len([l for l in r.stderr.splitlines() if l.strip()]) == 1, r.stderr
    for src in ("agent_name", "USER", "LOGNAME"):
        assert src in r.stderr, r.stderr


def test_f2_whoami_pw_name_fallback(tmp_path):
    r = _run_whoami(tmp_path, pw="agent-pwuser")
    assert r.returncode == 0, r
    assert r.stdout == "pwuser\n"


def test_f3_whoami_no_config_file(tmp_path):
    assert not (tmp_path / "eh" / "config.json").exists()
    r = _run_whoami(tmp_path, env_extra={"USER": "agent-zed"})
    assert r.returncode == 0, r
    assert r.stdout == "zed\n"


# --- refusing watcher under the real supervisor ---------------------------

_SUPERVISOR = _TOOLS / "monitor-supervisor.py"


def test_w5_refusal_visible_under_real_supervisor(tmp_path):
    """The supervisor must surface BOTH the tagged refusal line and the loud
    ``CHILD updates EXITED code=2`` line. Bounded: reads until BOTH have been
    seen (either order), a 25s hard cap, and the whole process group is killed
    after.

    Why wait for both (engram-alpha#4143): the supervisor's ``_reader`` (child
    stdout forwarding) and ``_watcher`` (prints EXITED after ``proc.wait()``)
    are separate threads and the watcher does not wait for the reader to drain,
    so EXITED can print BEFORE the child's last stdout line. Stopping at the
    first EXITED made this test flaky under load. The supervisor restarts the
    child with backoff (1s, 2s, 4s ...), so a lost first-cycle warning is
    re-emitted well within the cap."""
    cfg = _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    d = tmp_path / "copy"
    d.mkdir()
    copy = d / _UPDATES.name
    copy.write_text(_UPDATES.read_text())
    (d / "forum.py").write_text(
        "import sys\n"
        "if sys.argv[1:2] == ['whoami']:\n"
        "    sys.stderr.write(" + repr(_ALL_EMPTY_ERR) + ")\n"
        "    sys.exit(2)\n"
        "sys.exit(9)\n"
    )
    env = _env(tmp_path, {"USER": "agent-zed"})
    idle = json.dumps([sys.executable, "-c", "import time; time.sleep(120)"])
    proc = subprocess.Popen(
        [sys.executable, str(_SUPERVISOR),
         "--updates-cmd-json", json.dumps(["bash", str(copy)]),
         "--bell-cmd-json", idle, "--mention-cmd-json", idle,
         "--pidfile", str(tmp_path / "sup.pid"),
         "--status-file", str(tmp_path / "status.json"),
         "--status-interval", "5"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    timer = threading.Timer(25, lambda: os.killpg(pgid, signal.SIGKILL))
    timer.start()
    lines = []
    seen_warn = seen_exit = False
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if (line.startswith("[updates]")
                    and "cannot resolve this agent's name" in line):
                seen_warn = True
            if "CHILD updates EXITED code=2" in line:
                seen_exit = True
            if seen_warn and seen_exit:
                break
    finally:
        timer.cancel()
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
        proc.stdout.close()
    out = "\n".join(lines)
    assert any(l.startswith("[updates]") and "cannot resolve this agent's name" in l
               for l in lines), out
    assert any("CHILD updates EXITED code=2" in l for l in lines), out
    assert str(cfg) in out, out
    # no orphan survives: the whole process group is gone. SIGKILLed members
    # linger briefly as zombies until init reaps them, so poll, don't assert once.
    gone = False
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone, "process group still alive 5s after SIGKILL (orphan child?)"


@pytest.mark.parametrize("which,script", WATCHERS)
def test_a4_transient_whoami_failure_recovers_on_retry(tmp_path, which, script):
    """whoami fails once (rc 1 + stderr), then succeeds: no refusal, no 'rc=0'
    contradiction; polling proceeds as the retried name."""
    _write_config(tmp_path, {"forum": {"url": "http://127.0.0.1:9"}})
    d = tmp_path / "copy"
    d.mkdir()
    copy = d / script.name
    copy.write_text(script.read_text())
    counter = tmp_path / "whoami.count"
    (d / "forum.py").write_text(
        "import sys, os\n"
        f"c = {str(counter)!r}\n"
        "if sys.argv[1:2] == ['whoami']:\n"
        "    n = int(open(c).read()) if os.path.exists(c) else 0\n"
        "    open(c, 'w').write(str(n + 1))\n"
        "    if n == 0:\n"
        "        sys.stderr.write('transient boom\\n')\n"
        "        sys.exit(1)\n"
        "    print('zed')\n"
        "    sys.exit(0)\n"
        "sys.exit(9)\n")
    out, line = _stdout_until_curl(tmp_path, copy, _env(tmp_path, {"USER": "agent-nobody"}))
    _check(line, which, "zed")
    # (the curl stub's own seeding warning also starts with the warning sign, so
    # assert on the whoami refusal text, not on the glyph alone)
    assert "cannot resolve this agent's name" not in out, out
    assert not [l for l in out.splitlines() if "whoami failed" in l], out
    assert "rc=0" not in out, out
    assert counter.read_text() == "2", counter.read_text()
