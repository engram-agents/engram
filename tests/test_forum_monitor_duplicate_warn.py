"""Tests for the duplicate-instance detection in forum-updates-monitor.sh
and forum-mention-monitor.sh (#1933: warn-not-kill).

Background: both monitors used to SIGTERM any pgrep -f match against their
own script basename on arm ("last-arm-wins" reap). pgrep -f is an unanchored
substring match over the WHOLE cmdline, and `agentctl --charge-file` appends
the boot charge text to `claude`'s own argv — so once a charge embedded a
monitor's script path (#1865), every charge-booted `claude` process carried
that path as a substring of its own cmdline and reaped (killed) itself,
taking its whole session down (#1933). Lei's ruling: a substring match must
never be an execution decision — reap NEVER kills; it WARNS on stdout (the
Monitor event stream) and lets the agent decide.

Isolation note (load-bearing, not just style): this suite runs on a live,
shared, multi-agent host. Real production instances of both monitors are
routinely running under this SAME uid with the DEFAULT purpose (no
--purpose flag) — pgrep -u $(id -u) -f <script-basename> would legitimately
match them too. Every test below arms its monitors with a unique, per-test
--purpose key so the pgrep candidate pattern ("script.sh.*--purpose <key>")
can never match a real production monitor's cmdline (those run under
"default" or a different named purpose). This is what makes it safe to run
this suite against the real host: it exercises the genuine reap/warn code
path without ever making a real production process a pgrep candidate.

Verifies (per #1933's fix + the parent spec's required coverage), for BOTH
scripts:
  1. A decoy whose argv merely CONTAINS the script path + purpose text as a
     python3 one-liner's argument (mirrors the real boot-charge repro) is
     left alive and is NOT reported as a suspect.
  2. A genuine second `bash <script>` instance is left alive AND IS reported
     (by pid) as a suspect.
  2b. A genuine second instance launched with a login-shell-style argv0
      (`exec -a "-bash" bash <script>`, argv[0] == "-bash") is STILL
      reported — not a false negative from the interpreter-name check.
  3. $FORUM_UPDATES_NO_REAP / $FORUM_MENTION_NO_REAP suppresses the
     detection/warning pass entirely.

Mutation-checks (run manually, not as pytest cases — see the handoff):
  - The decoy scenario (case 1), run against the pre-fix script content (git
    show HEAD~2's tree at the time this file was authored, before #1933's
    fix), kills the decoy — confirming the reap-not-warn guard is real.
  - The login-shell scenario (case 2b), run against this fix WITHOUT the
    `.lstrip("-")` on argv[0], leaves the duplicate unreported — confirming
    the login-shell fix is real, not a guard that never fires either way.
Neither is encoded as a pytest test here because each depends on a
throwaway copy of script content outside the repo tree.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"

_SCRIPTS = {
    "forum-updates-monitor.sh": {
        "path": _TOOLS / "forum-updates-monitor.sh",
        "no_reap_env": "FORUM_UPDATES_NO_REAP",
    },
    "forum-mention-monitor.sh": {
        "path": _TOOLS / "forum-mention-monitor.sh",
        "no_reap_env": "FORUM_MENTION_NO_REAP",
    },
}

# Generous but bounded: detection is a synchronous pgrep + a handful of
# python3 subprocess calls, normally well under 0.5s. This margin absorbs
# CI scheduling jitter without making a failing test take forever.
_DETECT_WINDOW = 2.0
_START_STAGGER = 0.5


def _free_port() -> int:
    """An ephemeral port nothing is listening on, so curl --fail in the
    monitor's seed-retry loop fails fast (ECONNREFUSED) instead of hanging —
    keeps the monitor process alive (blocked in its retry sleep) for the
    duration of the test without needing a mock HTTP server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_home(tmp_path: Path, tag: str, port: int) -> Path:
    home = tmp_path / f"home-{tag}"
    engram_dir = home / ".engram"
    engram_dir.mkdir(parents=True)
    config = {
        "agent_name": f"testagent-{tag}",
        "forum": {"url": f"http://127.0.0.1:{port}"},
    }
    (engram_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return home


class _LineReader:
    """Background thread draining a Popen's stdout into a list, mirroring
    the pattern in test_forum_mention_monitor_coalesce.py."""

    def __init__(self, proc: subprocess.Popen):
        self.lines: list[str] = []
        self._proc = proc
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.append(line.rstrip("\n"))


def _start_monitor(script_path: Path, home: Path, purpose: str):
    env = os.environ.copy()
    env["HOME"] = str(home)
    proc = subprocess.Popen(
        ["bash", str(script_path), "--purpose", purpose],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc, _LineReader(proc)


def _start_decoy(script_basename: str, purpose: str) -> subprocess.Popen:
    """A python3 process whose argv merely QUOTES the script path + purpose
    text as a single trailing argument — never actually runs the script.
    Mirrors the real #1933 repro: `python3 -c 'time.sleep(90)' "$CHARGE"`
    where $CHARGE happened to contain the monitor's invocation line."""
    blob = (
        f"Automatic morning boot charge: bash /home/agent/.engram/tools/"
        f"{script_basename} --kinds dm,baton (unrelated instructions) "
        f"--purpose {purpose} (padding to resemble real charge length) "
        "continue with the rest of the day's plan"
    )
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", blob],
    )


def _start_login_shell_duplicate(script_path: Path, home: Path, purpose: str) -> subprocess.Popen:
    """A genuine second instance of the monitor script, launched the way a
    login shell would: `exec -a "-bash" bash <script> ...` sets argv[0] to
    "-bash" (the leading dash is bash's own login-shell convention). This is
    real bash actually running the real script — only argv[0]'s spelling
    differs from a plain `bash <script>` launch. Built via a tiny outer `bash
    -c` wrapper because subprocess.Popen has no portable way to override
    argv[0] directly; the outer wrapper's own argv[0] is irrelevant (it never
    reaches the pgrep -f candidate stage — see the assertion in the test)."""
    return subprocess.Popen(
        ["bash", "-c", 'exec -a "-bash" bash "$0" "$@"',
         str(script_path), "--purpose", purpose],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )


def _terminate(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _unique_purpose(tag: str) -> str:
    # [a-zA-Z0-9_-] only (script-enforced) and never collides with a real
    # production --purpose key.
    return f"t1933{tag}{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 1. decoy (charge-text false positive) — alive, NOT reported
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", list(_SCRIPTS))
def test_decoy_with_script_path_in_argv_is_not_killed_or_reported(tmp_path, script_name):
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("decoy")
    port = _free_port()
    home = _make_home(tmp_path, "primary", port)

    decoy = _start_decoy(script_name, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert decoy.poll() is None, "decoy exited before the primary could even arm"

        primary, reader = _start_monitor(meta["path"], home, purpose)
        try:
            time.sleep(_DETECT_WINDOW)
            # The decoy must still be alive — the whole point of #1933.
            assert decoy.poll() is None, (
                f"decoy pid {decoy.pid} was killed — the substring-match "
                f"false positive is back. stdout so far: {reader.lines!r}"
            )
            # And must not even be REPORTED as a suspect.
            reported = [l for l in reader.lines if str(decoy.pid) in l]
            assert not reported, (
                f"decoy pid {decoy.pid} was reported as a suspect duplicate "
                f"(should be filtered by the argv[0]-interpreter check): {reported!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(decoy)


# ---------------------------------------------------------------------------
# 2. genuine duplicate — alive, IS reported (by pid)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", list(_SCRIPTS))
def test_genuine_duplicate_is_reported_but_left_alive(tmp_path, script_name):
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("dup")
    port = _free_port()
    home_a = _make_home(tmp_path, "dup-instance", port)
    home_b = _make_home(tmp_path, "primary", port)

    dup_proc, dup_reader = _start_monitor(meta["path"], home_a, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None, "the prior instance exited before it could be detected"

        primary, reader = _start_monitor(meta["path"], home_b, purpose)
        try:
            time.sleep(_DETECT_WINDOW)
            # Must still be alive — never killed.
            assert dup_proc.poll() is None, (
                f"genuine duplicate pid {dup_proc.pid} was killed; expected "
                f"warn-not-kill. stdout so far: {reader.lines!r}\n"
                f"dup stderr: {dup_reader.lines!r}"
            )
            # Must be REPORTED, by pid, on stdout.
            reported = [l for l in reader.lines if str(dup_proc.pid) in l]
            assert reported, (
                f"genuine duplicate pid {dup_proc.pid} was never reported. "
                f"stdout: {reader.lines!r}"
            )
            assert any("duplicate" in l.lower() for l in reported), (
                f"reported line(s) don't look like a duplicate warning: {reported!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(dup_proc)


# ---------------------------------------------------------------------------
# 2b. login-shell argv0 ("-bash") duplicate — still detected (not a false
#     negative). `exec -a "-bash" bash <script>` is a real, if uncommon,
#     invocation shape; a naive `basename(argv[0]) in {"bash", ...}` check
#     misses it because "-bash" isn't literally "bash".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", list(_SCRIPTS))
def test_login_shell_argv0_duplicate_is_still_reported(tmp_path, script_name):
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("loginshell")
    port = _free_port()
    home_a = _make_home(tmp_path, "dup-instance", port)
    home_b = _make_home(tmp_path, "primary", port)

    dup_proc = _start_login_shell_duplicate(meta["path"], home_a, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None, "the login-shell-argv0 instance exited before it could be detected"

        primary, reader = _start_monitor(meta["path"], home_b, purpose)
        try:
            time.sleep(_DETECT_WINDOW)
            assert dup_proc.poll() is None, (
                f"login-shell-argv0 duplicate pid {dup_proc.pid} was killed; "
                f"expected warn-not-kill. stdout: {reader.lines!r}"
            )
            reported = [l for l in reader.lines if str(dup_proc.pid) in l]
            assert reported, (
                f"login-shell-argv0 duplicate pid {dup_proc.pid} (argv[0]='-bash') "
                f"was NOT reported — the '-bash' spelling is bypassing the "
                f"bash-family interpreter check (missing lstrip('-')). "
                f"stdout: {reader.lines!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(dup_proc)


# ---------------------------------------------------------------------------
# 3. NO_REAP suppresses the warning entirely
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", list(_SCRIPTS))
def test_no_reap_env_suppresses_the_warning(tmp_path, script_name):
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("noreap")
    port = _free_port()
    home_a = _make_home(tmp_path, "dup-instance", port)
    home_b = _make_home(tmp_path, "primary", port)

    dup_proc, _dup_reader = _start_monitor(meta["path"], home_a, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None

        env = os.environ.copy()
        env["HOME"] = str(home_b)
        env[meta["no_reap_env"]] = "1"
        primary = subprocess.Popen(
            ["bash", str(meta["path"]), "--purpose", purpose],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        reader = _LineReader(primary)
        try:
            time.sleep(_DETECT_WINDOW)
            assert dup_proc.poll() is None
            assert reader.lines == [], (
                f"{meta['no_reap_env']}=1 should suppress ALL detection "
                f"output, got: {reader.lines!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(dup_proc)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
