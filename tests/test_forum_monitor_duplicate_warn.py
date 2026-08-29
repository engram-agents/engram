"""Tests for the duplicate-instance detection in forum-updates-monitor.sh,
forum-mention-monitor.sh (#1933: warn-not-kill), and — since #1935 ported the
identical structure — collab-baton-monitor.sh / collab-letter-monitor.sh.

Background: all four monitors used to SIGTERM any pgrep -f match against
their own script basename on arm ("last-arm-wins" reap). pgrep -f is an
unanchored substring match over the WHOLE cmdline, and `agentctl
--charge-file` appends the boot charge text to `claude`'s own argv — so once
a charge embedded a monitor's script path (#1865), every charge-booted
`claude` process carried that path as a substring of its own cmdline and
reaped (killed) itself, taking its whole session down (#1933, fixed for the
two forum monitors; #1935 ports the fix to the two collab monitors, which
also gained a process-name-anchored candidate pattern per #2028). Lei's
ruling: a substring match must never be an execution decision — reap NEVER
kills; it WARNS on stdout (the Monitor event stream) and lets the agent
decide.

Isolation note (load-bearing, not just style): this suite runs on a live,
shared, multi-agent host. Real production instances of all four monitors are
routinely running under this SAME uid with the DEFAULT purpose (no
--purpose flag) — pgrep -u $(id -u) -f <script-basename> would legitimately
match them too. Every test below arms its monitors with a unique, per-test
--purpose key so the pgrep candidate pattern ("script.sh.*--purpose <key>")
can never match a real production monitor's cmdline (those run under
"default" or a different named purpose). This is what makes it safe to run
this suite against the real host: it exercises the genuine reap/warn code
path without ever making a real production process a pgrep candidate. The
two collab monitors ADD a second isolation axis: each test also points their
watched directory (BATON_DIR / INTER_AGENT_DIR) at an empty per-test scratch
dir, rather than the real shared production dirs
(/home/agents-shared/forum/projects, /home/agents-shared/inter-agent) — the
duplicate-detection code path runs before directory resolution either way,
so this is belt-and-suspenders isolation, not a correctness requirement.

Verifies (per #1933's fix + the parent spec's required coverage), for all
four scripts:
  1. A decoy whose argv merely CONTAINS the script path + purpose text as a
     python3 one-liner's argument (mirrors the real boot-charge repro) is
     left alive and is NOT reported as a suspect.
  2. A genuine second `bash <script>` instance is left alive AND IS reported
     (by pid) as a suspect.
  2b. A genuine second instance launched with a login-shell-style argv0
      (`exec -a "-bash" bash <script>`, argv[0] == "-bash") is STILL
      reported — not a false negative from the interpreter-name check.
  3. $FORUM_UPDATES_NO_REAP / $FORUM_MENTION_NO_REAP / $COLLAB_MONITOR_NO_REAP
     suppresses the detection/warning pass entirely.

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
    # #1935: collab-baton-monitor.sh / collab-letter-monitor.sh gained the
    # identical warn-not-kill + /proc two-layer verification structure. Both
    # take a watched-directory env override (unlike the forum scripts, which
    # poll a URL) -- "dir_env" names it so each test below can point the
    # watched dir at an empty per-test scratch directory instead of the real
    # shared production dir (/home/agents-shared/forum/projects,
    # /home/agents-shared/inter-agent), keeping this suite's live-host
    # isolation property (see the module docstring) intact for these two too.
    "collab-baton-monitor.sh": {
        "path": _TOOLS / "collab-baton-monitor.sh",
        "no_reap_env": "COLLAB_MONITOR_NO_REAP",
        "dir_env": "BATON_DIR",
    },
    "collab-letter-monitor.sh": {
        "path": _TOOLS / "collab-letter-monitor.sh",
        "no_reap_env": "COLLAB_MONITOR_NO_REAP",
        "dir_env": "INTER_AGENT_DIR",
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


def _start_monitor(script_path: Path, home: Path, purpose: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        ["bash", str(script_path), "--purpose", purpose],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc, _LineReader(proc)


def _scratch_dir_env(meta: dict, tmp_path: Path) -> dict:
    """Build the extra_env dict pointing a collab monitor's watched directory
    at an empty per-test scratch dir (via meta["dir_env"]). Empty dict for
    the forum monitors (no "dir_env" key -- they poll a URL, not a dir)."""
    dir_env = meta.get("dir_env")
    if not dir_env:
        return {}
    scratch = tmp_path / "watched_dir"
    scratch.mkdir(exist_ok=True)
    return {dir_env: str(scratch)}


def _start_decoy(script_basename: str, purpose: str) -> subprocess.Popen:
    """A python3 process whose argv merely QUOTES the script path + purpose
    text as a single trailing argument — never actually runs the script.
    Mirrors the real #1933 repro: `python3 -c 'time.sleep(90)' "$CHARGE"`
    where $CHARGE happened to contain the monitor's invocation line.

    This decoy shape never even reaches layer-1 candidacy for the collab
    monitors: their candidate pattern is anchored on the cmdline STARTING
    with a bash-family interpreter (`^(\\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\\s+`),
    and this decoy's cmdline starts with `python3`. That makes it a
    layer-1 non-candidate, not a layer-2 rejection — it cannot exercise
    the collab scripts' /proc re-verification at all. Use
    `_start_collab_layer2_decoy` for those two scripts' decoy coverage
    instead (see that function's docstring)."""
    blob = (
        f"Automatic morning boot charge: bash /home/agent/.engram/tools/"
        f"{script_basename} --kinds dm,baton (unrelated instructions) "
        f"--purpose {purpose} (padding to resemble real charge length) "
        "continue with the rest of the day's plan"
    )
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", blob],
    )


def _start_collab_layer2_decoy(
    script_basename: str, purpose: str, tmp_path: Path
) -> subprocess.Popen:
    """A decoy for the two collab monitors that IS a genuine layer-1
    candidate — it starts with a bash-family interpreter and its cmdline
    contains the script's basename as a SUBSTRING — but must be REJECTED
    by layer 2's exact-script-token check.

    The collab monitors' layer-1 pgrep pattern is anchored on cmdline
    START (`^(\\S*/)?-?(bash|sh|dash|ksh|zsh|ash)\\s+\\S*<script>\\.sh...`),
    unlike the forum monitors' bare unanchored substring match — so
    `_start_decoy` above (a python3 process) can never even become a
    layer-1 candidate for these two scripts and cannot exercise their
    layer-2 rejection logic at all (confirmed: `_check_collab_*_dup`'s
    exact/suffix match on argv is never reached if pgrep never proposes
    the pid as a candidate in the first place).

    This decoy is a REAL bash process (satisfies layer 1's interpreter
    anchor) launching a SIBLING-NAMED throwaway script
    (`watch-<script>`, e.g. `watch-collab-baton-monitor.sh`) that lives
    in the per-test scratch dir. `\\S*<script>\\.sh` in the layer-1
    pattern permits an arbitrary non-space PREFIX before the script
    name, so `bash /tmp/.../watch-collab-baton-monitor.sh --purpose X`
    matches layer 1 (confirmed by regex simulation against the real
    pattern before wiring this fixture). But layer 2's
    `_check_collab_*_dup` requires an argv token that either EQUALS the
    real script's exact basename or ENDS WITH `/<real-script-basename>`
    — `.../watch-collab-baton-monitor.sh` does neither (it ends with
    `watch-collab-baton-monitor.sh`, not `/collab-baton-monitor.sh`), so
    a correct layer 2 must reject it. If layer 2 is bypassed (see the
    handoff's mutation-check), this decoy IS reported — giving the
    false-positive half of #1935 a test that can actually fail."""
    scratch = tmp_path / "layer2_decoy"
    scratch.mkdir(exist_ok=True)
    decoy_script = scratch / f"watch-{script_basename}"
    decoy_script.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
    decoy_script.chmod(0o755)
    return subprocess.Popen(
        ["bash", str(decoy_script), "--purpose", purpose],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _start_login_shell_duplicate(
    script_path: Path, home: Path, purpose: str, extra_env: dict | None = None
) -> subprocess.Popen:
    """A genuine second instance of the monitor script, launched the way a
    login shell would: `exec -a "-bash" bash <script> ...` sets argv[0] to
    "-bash" (the leading dash is bash's own login-shell convention). This is
    real bash actually running the real script — only argv[0]'s spelling
    differs from a plain `bash <script>` launch. Built via a tiny outer `bash
    -c` wrapper because subprocess.Popen has no portable way to override
    argv[0] directly; the outer wrapper's own argv[0] is irrelevant (it never
    reaches the pgrep -f candidate stage — see the assertion in the test)."""
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        ["bash", "-c", 'exec -a "-bash" bash "$0" "$@"',
         str(script_path), "--purpose", purpose],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
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
    extra_env = _scratch_dir_env(meta, tmp_path)

    # The two collab monitors have an ANCHORED layer-1 candidate pattern
    # (cmdline must START with a bash-family interpreter), so a python3
    # decoy (_start_decoy) never even becomes a layer-1 candidate for
    # them and cannot exercise their layer-2 /proc rejection logic. Use
    # a decoy that IS a genuine layer-1 candidate but must be rejected
    # by layer 2 instead — see _start_collab_layer2_decoy's docstring.
    if "dir_env" in meta:
        decoy = _start_collab_layer2_decoy(script_name, purpose, tmp_path)
    else:
        decoy = _start_decoy(script_name, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert decoy.poll() is None, "decoy exited before the primary could even arm"

        primary, reader = _start_monitor(meta["path"], home, purpose, extra_env)
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
    extra_env = _scratch_dir_env(meta, tmp_path)

    dup_proc, dup_reader = _start_monitor(meta["path"], home_a, purpose, extra_env)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None, "the prior instance exited before it could be detected"

        primary, reader = _start_monitor(meta["path"], home_b, purpose, extra_env)
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
    extra_env = _scratch_dir_env(meta, tmp_path)

    dup_proc = _start_login_shell_duplicate(meta["path"], home_a, purpose, extra_env)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None, "the login-shell-argv0 instance exited before it could be detected"

        primary, reader = _start_monitor(meta["path"], home_b, purpose, extra_env)
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
    extra_env = _scratch_dir_env(meta, tmp_path)

    dup_proc, _dup_reader = _start_monitor(meta["path"], home_a, purpose, extra_env)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None

        env = os.environ.copy()
        env["HOME"] = str(home_b)
        env.update(extra_env)
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


# ---------------------------------------------------------------------------
# 4. ps-fallback branch (round-2 fix for #2134's macOS port): ENGRAM_PROC_DIR
#    is the env-indirected override that lets this suite force the fallback
#    branch (CHANGE 2's os.path.exists(_PROC + "/self/cmdline") discriminator)
#    on this Linux test host, by pointing it at an empty directory that has
#    no self/cmdline entry. Only the two forum monitors are in scope for
#    this round's fix (the collab monitors are untouched) -- so, unlike the
#    tests above, these two are NOT parametrized over the full _SCRIPTS map.
# ---------------------------------------------------------------------------

_FORUM_SCRIPTS_ONLY = ["forum-updates-monitor.sh", "forum-mention-monitor.sh"]


def _empty_proc_dir(tmp_path: Path, tag: str) -> Path:
    """A directory with no self/cmdline entry, forcing the ps-fallback branch
    even on this Linux test host. Passed as $ENGRAM_PROC_DIR."""
    d = tmp_path / f"empty-proc-{tag}"
    d.mkdir()
    return d


@pytest.mark.parametrize("script_name", _FORUM_SCRIPTS_ONLY)
def test_ps_fallback_positive_genuine_duplicate_reported_weak(tmp_path, script_name):
    """POSITIVE control: with ENGRAM_PROC_DIR pointed at a directory with no
    self/cmdline, the platform discriminator routes to the ps fallback even
    on Linux, and a genuine duplicate is still detected -- reported via the
    rc=3 weaker-match wording, distinct from the rc=0 /proc-verified one."""
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("psfallback")
    port = _free_port()
    home_a = _make_home(tmp_path, "dup-instance", port)
    home_b = _make_home(tmp_path, "primary", port)
    proc_dir = _empty_proc_dir(tmp_path, "positive")
    extra_env = {"ENGRAM_PROC_DIR": str(proc_dir)}

    dup_proc, dup_reader = _start_monitor(meta["path"], home_a, purpose, extra_env)
    try:
        time.sleep(_START_STAGGER)
        assert dup_proc.poll() is None, "the prior instance exited before it could be detected"

        primary, reader = _start_monitor(meta["path"], home_b, purpose, extra_env)
        try:
            time.sleep(_DETECT_WINDOW)
            assert dup_proc.poll() is None, (
                f"genuine duplicate pid {dup_proc.pid} was killed under the ps "
                f"fallback; expected warn-not-kill. stdout: {reader.lines!r}\n"
                f"dup stderr: {dup_reader.lines!r}"
            )
            reported = [l for l in reader.lines if str(dup_proc.pid) in l]
            assert reported, (
                f"genuine duplicate pid {dup_proc.pid} was never reported via "
                f"the ps fallback. stdout: {reader.lines!r}"
            )
            assert any("WEAK match" in l for l in reported), (
                f"fallback report should carry the rc=3 weaker-match wording, "
                f"got: {reported!r}"
            )
            # The two confidence classes (rc=0 /proc-verified vs rc=3
            # ps-fallback) must never be conflated into one channel -- the
            # plain rc=0 wording must not appear here.
            assert not any("duplicate instance —" in l for l in reported), (
                f"fallback report should never carry the plain rc=0 "
                f"/proc-verified wording: {reported!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(dup_proc)


@pytest.mark.parametrize("script_name", _FORUM_SCRIPTS_ONLY)
def test_ps_fallback_negative_decoy_not_reported(tmp_path, script_name):
    """NEGATIVE control: with the ps fallback forced via ENGRAM_PROC_DIR, a
    non-matching decoy (argv merely quotes the script path as an argument,
    same shape as test 1 above) is still filtered out -- layer-2 verification
    (interpreter + exact-token check) rejects it regardless of which
    verification path (/proc or ps) is doing the checking."""
    meta = _SCRIPTS[script_name]
    purpose = _unique_purpose("psfallbackneg")
    port = _free_port()
    home = _make_home(tmp_path, "primary", port)
    proc_dir = _empty_proc_dir(tmp_path, "negative")
    extra_env = {"ENGRAM_PROC_DIR": str(proc_dir)}

    decoy = _start_decoy(script_name, purpose)
    try:
        time.sleep(_START_STAGGER)
        assert decoy.poll() is None, "decoy exited before the primary could even arm"

        primary, reader = _start_monitor(meta["path"], home, purpose, extra_env)
        try:
            time.sleep(_DETECT_WINDOW)
            assert decoy.poll() is None, (
                f"decoy pid {decoy.pid} was killed under the ps fallback — "
                f"the substring-match false positive is back. "
                f"stdout: {reader.lines!r}"
            )
            reported = [l for l in reader.lines if str(decoy.pid) in l]
            assert not reported, (
                f"decoy pid {decoy.pid} was reported as a suspect duplicate "
                f"via the ps fallback (should be filtered by the "
                f"argv[0]-interpreter check regardless of verification "
                f"path): {reported!r}"
            )
        finally:
            _terminate(primary)
    finally:
        _terminate(decoy)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
