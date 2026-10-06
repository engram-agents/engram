"""Tests for #3441 — the forum monitors' "forum unreachable" edge line must
carry curl's exit code (with its meaning) and time_total.

Background: on 2026-10-05 ~22:05Z forum-mention-monitor.sh announced "forum
unreachable" and recovered a minute later, while the forum server's log showed
every poll from that client arriving on rhythm and answered 200. The failure
was client-side, and the monitor had thrown away the one datum that classifies
it: curl's exit code (28 = --max-time exceeded, 7 = connect failed, ...).

Contract (stated BEFORE the fix, see the issue):
  1. On the transition INTO unreachable the single edge line contains
     `curl <rc>: <meaning>` and time_total with units (`10.00s`). The existing
     prefix ("forum unreachable") and the "— still armed, not receiving; will
     report when it returns" tail stay byte-identical.
  2. Edge discipline (#3062/#3067) is unchanged: exactly ONE such line per
     transition, never per cycle; the recovery line prints once.
  3. The write-out used to capture time_total must never leak into the parsed
     body on success.
  4. The seed path's "cannot seed" line carries the same datum when curl
     itself failed.

curl is replaced by a stub on PATH (a Python script) that returns a scripted
(rc, body) per call and honours `-w '\\n%{time_total}'` the way real curl does
(including on failure, where real curl still prints the write-out).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UPDATES_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-updates-monitor.sh"
_MENTION_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"

_POLL = "0.2"
_TAIL = " — still armed, not receiving; will report when it returns"

_UPDATES_QUIET = json.dumps({"updates": [], "as_of": 7095})
_MENTIONS_QUIET = json.dumps({"mentions": []})

_STUB = """#!{python}
import sys, json, os
state = {state!r}
script = json.load(open({script!r}))
try:
    n = int(open(state).read())
except Exception:
    n = 0
open(state, "w").write(str(n + 1))
entry = script[min(n, len(script) - 1)]
rc, body = entry[0], entry[1]
phases = entry[2] if len(entry) > 2 else {{}}
fmt = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "-w" and i + 1 < len(args):
        fmt = args[i + 1]
out = ""
if rc == 0:
    out += body
if fmt is not None:
    for k, v in phases.items():
        fmt = fmt.replace("%{{time_" + k + "}}", v)
    fmt = fmt.replace("%{{time_total}}", "10.001234")
    out += fmt.replace("\\\\n", "\\n")
sys.stdout.write(out)
sys.exit(rc)
"""


class _Run:
    def __init__(self, argv, env, tmp_path, calls_file):
        self.lines: list[str] = []
        self._calls = calls_file
        self._proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env
        )
        self._t = threading.Thread(target=self._drain, daemon=True)
        self._t.start()

    def _drain(self):
        for line in self._proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def calls(self) -> int:
        try:
            return int(self._calls.read_text())
        except Exception:
            return 0

    def wait_calls(self, n: int, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calls() >= n:
                return
            time.sleep(0.02)
        raise AssertionError(f"curl stub saw {self.calls()} calls, expected >= {n}")

    def stop(self):
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._t.join(timeout=2)
        return self.lines


@pytest.fixture
def launch(tmp_path):
    runs: list[_Run] = []

    def _go(which: str, script: list[tuple], url: str = "http://127.0.0.1:9",
            env_extra: dict | None = None, stubs: dict | None = None,
            interpreter: str = "bash"):
        home = tmp_path / "home"
        (home / ".engram").mkdir(parents=True)
        (home / ".engram" / "config.json").write_text(
            json.dumps({"agent_name": "testagent",
                        "forum": {"url": url}})
        )
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script))
        calls = tmp_path / "calls"
        stub = bindir / "curl"
        stub.write_text(_STUB.format(python=sys.executable, state=str(calls),
                                     script=str(script_file)))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        if stubs:
            # Extra executables PREPENDED ahead of curl's dir (PATH is not replaced).
            stubdir = tmp_path / "stubbin"
            stubdir.mkdir()
            for name, body in stubs.items():
                f = stubdir / name
                f.write_text("#!/bin/sh\n" + body)
                f.chmod(f.stat().st_mode | stat.S_IXUSR)
            env["PATH"] = f"{stubdir}{os.pathsep}{env['PATH']}"
        env.update(env_extra or {})
        if which == "updates":
            env["FORUM_UPDATES_NO_REAP"] = "1"
            env["FORUM_UPDATES_CURSOR_FILE"] = str(tmp_path / "cursor")
            env["FORUM_UPDATES_POLL_SECONDS"] = _POLL
            argv = [interpreter, str(_UPDATES_SCRIPT)]
        else:
            env["FORUM_MENTION_NO_REAP"] = "1"
            env["FORUM_MENTION_SEEN_FILE"] = str(tmp_path / "seen")
            env["FORUM_MENTION_POLL_SECONDS"] = _POLL
            env["FORUM_MENTION_COALESCE_SECONDS"] = "1"
            argv = [interpreter, str(_MENTION_SCRIPT)]
        r = _Run(argv, env, tmp_path, calls)
        runs.append(r)
        return r

    yield _go
    for r in runs:
        r.stop()


_CASES = [
    (28, "timed out"),
    (7, "connect failed"),
    (22, "HTTP error"),
]

_MONITORS = [
    ("updates", "forum-updates-monitor.sh", _UPDATES_QUIET),
    ("mention", "forum-mention-monitor.sh", _MENTIONS_QUIET),
]


@pytest.mark.parametrize("rc,meaning", _CASES)
@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_edge_line_carries_rc_meaning_and_time(launch, which, name, quiet, rc, meaning):
    """Poll-loop edge: one line, with `curl <rc>: <meaning>` and `10.00s`, the
    byte-identical tail, once across 4 failing cycles; recovery prints once."""
    script = [(0, quiet), (0, quiet)] + [(rc, "")] * 4 + [(0, quiet)] * 3
    r = launch(which, script)
    r.wait_calls(9)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    line = unreachable[0]
    assert f"curl {rc}: {meaning}" in line, line
    assert "10.00s" in line, line
    assert line.endswith(_TAIL), line
    assert line.startswith(f"⚠️  {name}: forum unreachable"), line
    assert "\n" not in line
    assert len([l for l in lines if "forum reachable again" in l]) == 1, lines


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_unknown_code_prints_generic_meaning(launch, which, name, quiet):
    script = [(0, quiet), (0, quiet)] + [(99, "")] * 3 + [(0, quiet)] * 2
    r = launch(which, script)
    r.wait_calls(6)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    assert "curl 99: error" in unreachable[0], unreachable[0]


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_seed_failure_line_carries_rc_and_time(launch, which, name, quiet):
    """Seed path: the 'cannot seed' line names curl's rc when curl failed, and
    still fires exactly once across repeated failed seed attempts."""
    script = [(28, "")] * 3 + [(0, quiet)] * 3
    r = launch(which, script)
    r.wait_calls(5)
    lines = r.stop()
    seed = [l for l in lines if "not yet seeded" in l]
    assert len(seed) == 1, lines
    assert "curl 28: timed out" in seed[0], seed[0]
    assert "10.00s" in seed[0], seed[0]
    assert "cannot seed from the forum — not yet seeded and not receiving" in seed[0]
    assert len([l for l in lines if "seeded from the forum" in l]) == 1, lines


def test_updates_success_path_body_not_polluted(launch, tmp_path):
    """The time_total write-out must not leak into the parsed body: a real
    update still parses, is emitted, and the cursor advances to its as_of."""
    update = json.dumps(
        {"as_of": 5, "updates": [{"seq": 1, "kind": "dm", "sender": "a", "body": "hi"}]}
    )
    script = [(0, json.dumps({"updates": [], "as_of": 0})), (0, update),
              (0, '{"updates":[],"as_of":5}')]
    r = launch("updates", script)
    r.wait_calls(3)
    deadline = time.monotonic() + 5
    cursor = tmp_path / "cursor"
    while time.monotonic() < deadline:
        if cursor.is_file() and cursor.read_text().strip() == "5":
            break
        time.sleep(0.02)
    lines = r.stop()
    assert cursor.read_text().strip() == "5"
    assert any("hi" in l for l in lines), lines
    assert not [l for l in lines if "unreachable" in l], lines


_MENTION_LINE = "📣 1 new @-mention across 1 thread: #3(×1)"
_UPDATE_LINE = "✉️  new DM from a: hi"


def _wait_until(pred, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_mention_success_path_body_not_polluted(launch, tmp_path):
    """Tightened in round 2. The round-1 version waited on `"PollutionCheckThread"
    in l or "mention" in l.lower()`. The title clause can never match (the
    emitted line carries only `#<thread_id>(xN)`), and the second clause matches
    ANY line containing "mention", including a `forum-mention-monitor.sh:
    forum unreachable` warning -- so the wait could end on the wrong line. Its
    final check was `seen == ["11"] or "11" in seen_text`, whose substring arm
    passes for "110", "111", "5\\n11", i.e. a polluted file. Now: wait for the
    exact emitted line, require it to be the ONLY line, and require the seen
    file to be exactly the one id."""
    mention = json.dumps({"mentions": [
        {"post_id": 11, "thread_id": 3, "thread_title": "PollutionCheckThread"}]})
    script = [(0, _MENTIONS_QUIET), (0, mention)]
    r = launch("mention", script)
    assert _wait_until(lambda: any(l == _MENTION_LINE for l in r.lines)), r.lines
    lines = r.stop()
    assert len(lines) == 1, lines
    assert lines == [_MENTION_LINE], lines
    assert (tmp_path / "seen").read_text() == "11\tPollutionCheckThread\n"
    assert not [l for l in lines if "unreachable" in l], lines


# ---------------------------------------------------------------------------
# Round 2 (#3442 review): the ONE behaviour shift _curl_fetch introduces.
#
# `RESP="$(curl ...)"` stripped the body's trailing newlines. _curl_fetch appends
# `-w '\n%{time_total}'` and splits at the LAST newline, so on success RESP now
# KEEPS the body's own trailing newline. These tests pin that every downstream
# consumer (emit / as_of / ids, all JSON parsers fed via `printf '%s'`) treats a
# newline-terminated body exactly like a bare one, and that an empty or
# newline-only 200 body is still just a skipped cycle: no output line, no state
# movement, no "unreachable" announce, loop still alive.
# ---------------------------------------------------------------------------
_UPDATE = json.dumps(
    {"as_of": 5, "updates": [{"seq": 1, "kind": "dm", "sender": "a", "body": "hi"}]}
)
_MENTION = json.dumps({"mentions": [
    {"post_id": 11, "thread_id": 3, "thread_title": "NewlineThread"}]})


def _updates_run(launch, tmp_path, script):
    """Run the updates monitor until the cursor reaches 5, return (lines, cursor)."""
    r = launch("updates", script)
    cursor = tmp_path / "cursor"
    assert _wait_until(lambda: cursor.is_file() and cursor.read_text().strip() == "5"), (
        r.lines, cursor.read_text() if cursor.is_file() else None)
    return r.stop(), cursor.read_text().strip()


def _mention_run(launch, tmp_path, script):
    r = launch("mention", script)
    assert _wait_until(lambda: any(l == _MENTION_LINE for l in r.lines)), r.lines
    lines = r.stop()
    return lines, (tmp_path / "seen").read_text()


_UPDATES_SEED = (0, json.dumps({"updates": [], "as_of": 0}))
_UPDATES_TAIL = (0, '{"updates":[],"as_of":5}')


@pytest.mark.parametrize("trailing", ["", "\n"], ids=["bare", "trailing-newline"])
def test_updates_trailing_newline_body_processed_like_bare(launch, tmp_path, trailing):
    lines, cursor = _updates_run(
        launch, tmp_path, [_UPDATES_SEED, (0, _UPDATE + trailing), _UPDATES_TAIL])
    assert len(lines) == 1, lines
    assert lines == [_UPDATE_LINE], lines
    assert cursor == "5"


@pytest.mark.parametrize("trailing", ["", "\n"], ids=["bare", "trailing-newline"])
def test_mention_trailing_newline_body_processed_like_bare(launch, tmp_path, trailing):
    lines, seen = _mention_run(
        launch, tmp_path, [(0, _MENTIONS_QUIET), (0, _MENTION + trailing)])
    assert len(lines) == 1, lines
    assert lines == [_MENTION_LINE], lines
    assert seen == "11\tNewlineThread\n"


@pytest.mark.parametrize("bad", ["", "\n", "\n\n"], ids=["empty", "newline", "two-newlines"])
def test_updates_empty_or_newline_only_200_is_a_skipped_cycle(launch, tmp_path, bad):
    """Unchanged-by-the-PR behaviour (expected to pass at base too): an empty /
    newline-only 200 is a window the monitor could not read -> no output, cursor
    not moved by that cycle, no `unreachable`; the next good poll still lands."""
    lines, cursor = _updates_run(
        launch, tmp_path, [_UPDATES_SEED, (0, bad), (0, _UPDATE), _UPDATES_TAIL])
    assert len(lines) == 1, lines
    assert lines == [_UPDATE_LINE], lines
    assert cursor == "5"


@pytest.mark.parametrize("bad", ["", "\n", "\n\n"], ids=["empty", "newline", "two-newlines"])
def test_mention_empty_or_newline_only_200_is_a_skipped_cycle(launch, tmp_path, bad):
    """Same, for the mention monitor: `$SEEN` is preserved (not clobbered empty)
    and the next good poll's mention is still reported exactly once."""
    lines, seen = _mention_run(
        launch, tmp_path, [(0, _MENTIONS_QUIET), (0, bad), (0, _MENTION)])
    assert len(lines) == 1, lines
    assert lines == [_MENTION_LINE], lines
    assert seen == "11\tNewlineThread\n"


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_seed_200_with_unusable_body_gets_no_curl_suffix(launch, which, name, quiet):
    """The ` (last attempt: curl ...)` suffix is for curl failures only. A 200
    whose body cannot be parsed (curl rc 0) must print the bare 'cannot seed'
    line -- blaming curl for a body problem would send the operator the wrong
    way -- whereas a curl failure (rc 28) on the same path prints it."""
    r = launch(which, [(0, "<html>maintenance</html>")] * 3 + [(0, quiet)] * 3)
    r.wait_calls(5)
    lines = r.stop()
    seed = [l for l in lines if "cannot seed" in l]
    assert len(seed) == 1, lines
    assert seed[0].endswith("will report when it can"), seed[0]
    assert "last attempt" not in seed[0] and "curl" not in seed[0], seed[0]


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_seed_curl_failure_gets_the_suffix(launch, which, name, quiet):
    """Counterpart of the above: rc 28 on the seed path DOES carry the suffix."""
    r = launch(which, [(28, "")] * 3 + [(0, quiet)] * 3)
    r.wait_calls(5)
    lines = r.stop()
    seed = [l for l in lines if "cannot seed" in l]
    assert len(seed) == 1, lines
    assert seed[0].endswith(" (last attempt: curl 28: timed out, 10.00s)"), seed[0]


# ---------------------------------------------------------------------------
# #3443: on rc 28 the detail names the PHASE curl ran out of time in.
#
# Contract (stated before the fix):
#   1. rc 28 -> `curl 28: timed out in <phase>, 10.00s`; other rcs get NO phase.
#      Prefix and tail stay byte-identical, one line.
#   2. http URL: time_appconnect is always 0 (no TLS), so a stall after connect
#      says `first byte` / `body`, NEVER `tls`.
#   3. Missing / unparseable phase fields -> phase omitted, rest unchanged.
#   5. Edge discipline unchanged (once per transition).
# curl reports 0 for a phase it never reached; the phase that ate the time is the
# first one still at 0. The stub emits the extra write-out fields per entry.
# ---------------------------------------------------------------------------
def _ph(dns="0.0001", tcp="0.0003", tls="0", pre="0.0004", ttfb="0.0012"):
    return {"namelookup": dns, "connect": tcp, "appconnect": tls,
            "pretransfer": pre, "starttransfer": ttfb}


_PHASE_CASES = [
    # id, url, fields, expected phase
    ("http-first-byte", "http://127.0.0.1:9", _ph(ttfb="0"), "first byte"),
    ("http-body", "http://127.0.0.1:9", _ph(ttfb="0.0009"), "body"),
    ("http-connect", "http://127.0.0.1:9", _ph(tcp="0", pre="0", ttfb="0"), "connect"),
    ("http-resolve", "http://127.0.0.1:9", _ph(dns="0", tcp="0", pre="0", ttfb="0"), "resolve"),
    # namelookup=0 but connect>0: `resolve` needs BOTH at 0. A mutant that fires
    # `resolve` on namelookup==0 alone must go red here (every other case has dns>0).
    ("http-dns-zero-connected", "http://127.0.0.1:9",
     _ph(dns="0", tcp="0.02", pre="0", ttfb="0"), "first byte"),
    # connect>0, appconnect=0: builds that record connect at TCP completion.
    ("https-tls", "https://127.0.0.1:9", _ph(tls="0", pre="0", ttfb="0"), "tls"),
    # connect=0, appconnect=0, namelookup>0: mirrors MEASURED macOS curl 8.7.1
    # (SecureTransport / LibreSSL 3.3.6), where time_connect stays 0 until the TLS
    # handshake finishes -- a silent TCP-accepting server gave tcp=0 tls=0 there.
    # A TCP stall and a TLS stall are indistinguishable, so the label says both.
    ("https-connect-or-tls", "https://127.0.0.1:9",
     _ph(tcp="0", tls="0", pre="0", ttfb="0"), "connect or tls"),
    ("https-first-byte", "https://127.0.0.1:9", _ph(tls="0.02", ttfb="0"), "first byte"),
    ("https-body", "https://127.0.0.1:9", _ph(tls="0.02", ttfb="0.03"), "body"),
]


@pytest.mark.parametrize("cid,url,fields,phase", _PHASE_CASES,
                         ids=[c[0] for c in _PHASE_CASES])
@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_rc28_edge_line_names_phase(launch, which, name, quiet, cid, url, fields, phase):
    script = [(0, quiet), (0, quiet)] + [(28, "", fields)] * 3 + [(0, quiet)] * 2
    r = launch(which, script, url=url)
    r.wait_calls(7)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    # ORDER-sensitive: the whole line, so a moved/duplicated phase cannot pass.
    assert unreachable[0] == (
        f"⚠️  {name}: forum unreachable (curl 28: timed out in {phase}, 10.00s)" + _TAIL
    ), unreachable[0]


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_http_stall_after_connect_is_never_tls(launch, which, name, quiet):
    """The trap: http => appconnect 0 even on success. Must not read `tls`."""
    script = [(0, quiet), (0, quiet), (28, "", _ph(ttfb="0")), (0, quiet)]
    r = launch(which, script)
    r.wait_calls(4)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    assert "tls" not in unreachable[0], unreachable[0]
    assert "in first byte" in unreachable[0], unreachable[0]


@pytest.mark.parametrize("rc", [7, 6, 22, 56])
@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_non_28_rc_gets_no_phase(launch, which, name, quiet, rc):
    script = [(0, quiet), (0, quiet)] + [(rc, "", _ph(ttfb="0"))] * 2 + [(0, quiet)] * 2
    r = launch(which, script)
    r.wait_calls(5)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    assert " in " not in unreachable[0].split("(", 1)[1].split(")", 1)[0], unreachable[0]
    assert unreachable[0].endswith(_TAIL), unreachable[0]


@pytest.mark.parametrize("fields", [{}, _ph(ttfb="n/a"), _ph(dns="")],
                         ids=["absent", "garbage", "empty"])
@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_missing_or_unparseable_phase_fields_are_omitted(launch, which, name, quiet, fields):
    """No phase fields (older/odd curl) or garbage ones: the phase is omitted,
    the rest of the line is the pre-#3443 text."""
    script = [(0, quiet), (0, quiet), (28, "", fields), (0, quiet)]
    r = launch(which, script)
    r.wait_calls(4)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert unreachable == [
        f"⚠️  {name}: forum unreachable (curl 28: timed out, 10.00s)" + _TAIL
    ], unreachable


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_seed_suffix_names_phase_on_rc28(launch, which, name, quiet):
    script = [(28, "", _ph(ttfb="0"))] * 3 + [(0, quiet)] * 3
    r = launch(which, script)
    r.wait_calls(5)
    lines = r.stop()
    seed = [l for l in lines if "cannot seed" in l]
    assert len(seed) == 1, lines
    assert seed[0].endswith(" (last attempt: curl 28: timed out in first byte, 10.00s)"), seed[0]


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_success_with_phase_fields_leaves_body_clean(launch, which, name, quiet):
    """Extra write-out fields on a success must not reach the parsed body."""
    script = [(0, quiet, _ph()), (0, quiet, _ph()), (28, "", _ph(ttfb="0")),
              (0, quiet, _ph())]
    r = launch(which, script)
    # Wait for the OUTPUT the assertions need, not for the 4th curl call: the
    # stub counts a call when it STARTS, but "forum reachable again" is printed
    # only after the monitor has processed that call's response, so
    # wait_calls(4) + stop() can win the race under load.
    assert _wait_until(lambda: any("forum reachable again" in l for l in r.lines)), r.lines
    lines = r.stop()
    assert len([l for l in lines if "forum unreachable" in l]) == 1, lines
    assert len([l for l in lines if "forum reachable again" in l]) == 1, lines


# ---------------------------------------------------------------------------
# #3469: the time must format identically under a comma-decimal locale.
#
# macOS /bin/bash 3.2's printf BUILTIN ignores a per-command `LC_ALL=C` prefix
# and parses curl's dot decimal with the ambient locale: under de_DE / fr_FR it
# prints "invalid number" and exits non-zero, so the line read `?s`. The fix
# runs the EXTERNAL printf (`env LC_ALL=C printf`), which does honour LC_ALL.
# ---------------------------------------------------------------------------
def _locale_installed(name: str) -> bool:
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return False
    wanted = {name.lower(), name.lower().replace("utf-8", "utf8")}
    return any(l.strip().lower() in wanted for l in out.splitlines())


def _edge_line(r, calls: int = 5) -> str:
    r.wait_calls(calls)
    lines = r.stop()
    unreachable = [l for l in lines if "forum unreachable" in l]
    assert len(unreachable) == 1, lines
    return unreachable[0]


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_time_formats_under_comma_decimal_locale(launch, which, name, quiet):
    if not _locale_installed("de_DE.UTF-8"):
        pytest.skip("de_DE.UTF-8 not installed on this host")
    script = [(0, quiet), (0, quiet), (7, ""), (7, ""), (0, quiet)]
    r = launch(which, script, env_extra={"LC_ALL": "de_DE.UTF-8"})
    line = _edge_line(r)
    assert re.search(r"curl 7: connect failed, \d+\.\d\ds\)", line), line
    assert "?s" not in line, line


_BUGGY_BASH = "/bin/bash"
_BUG_LOCALE = "de_DE.UTF-8"
_bug_probe_cache: list = []


def _bug_probe_skip_reason() -> str:
    """Why the printf-builtin locale bug cannot be exhibited on this host, or
    "" if it can. Probed once per process.

    The bug: the builtin printf of macOS /bin/bash 3.2 ignores a per-command
    `LC_ALL=C` prefix, so under a comma-decimal locale `LC_ALL=C printf '%.2f'
    2.5` fails instead of printing 2.50."""
    if _bug_probe_cache:
        return _bug_probe_cache[0]
    reason = ""
    if not os.path.exists(_BUGGY_BASH):
        reason = f"no {_BUGGY_BASH} on this host"
    elif not _locale_installed(_BUG_LOCALE):
        reason = f"{_BUG_LOCALE} not installed on this host"
    else:
        try:
            env = {**os.environ, "LC_ALL": _BUG_LOCALE}
            out = subprocess.run(
                [_BUGGY_BASH, "-c", "LC_ALL=C printf '%.2f' 2.5"],
                capture_output=True, text=True, timeout=10, env=env).stdout
        except Exception as e:
            out = None
            reason = f"probe of {_BUGGY_BASH} failed to run: {e}"
        if out == "2.50":
            reason = (f"{_BUGGY_BASH} is not affected (its builtin printf honours "
                      "a per-command LC_ALL): the bug cannot occur here")
    _bug_probe_cache.append(reason)
    return reason


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_time_formats_as_dot_decimal_under_buggy_bash(launch, which, name, quiet):
    """BEHAVIOUR pin, independent of how the fix is written: run the real
    script under /bin/bash (3.2 on macOS, the bash that HAS the bug) with a
    comma-decimal locale and require the time to print as a dot-decimal number.

    Contract: this cell fails on the pre-fix code on any host that can exhibit
    the bug, and is skipped, with the reason stated, on hosts that cannot
    (no /bin/bash, no de_DE.UTF-8, or a /bin/bash whose builtin printf is not
    affected). The plain `bash` of the neighbouring cell may resolve to a
    newer bash on PATH where the bug does not exist; this one never does."""
    reason = _bug_probe_skip_reason()
    if reason:
        pytest.skip(reason)
    script = [(0, quiet), (0, quiet), (7, ""), (7, ""), (0, quiet)]
    r = launch(which, script, env_extra={"LC_ALL": _BUG_LOCALE}, interpreter=_BUGGY_BASH)
    line = _edge_line(r)
    assert re.search(r"curl 7: connect failed, \d+\.\d\ds\)", line), line
    assert "?s" not in line, line


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_non_numeric_time_total_prints_question_mark(launch, which, name, quiet):
    script = [(0, quiet), (0, quiet), (7, "", {"total": "n/a"}),
              (7, "", {"total": "n/a"}), (0, quiet)]
    line = _edge_line(launch(which, script))
    assert line.endswith("(curl 7: connect failed, ?s)" + _TAIL), line


@pytest.mark.parametrize("which,name,quiet", _MONITORS)
def test_failing_external_printf_falls_back_to_question_mark(launch, which, name, quiet):
    """A numeric time whose external printf fails (or is broken) must still
    print `?s`, not an empty field and not a crash."""
    script = [(0, quiet), (0, quiet), (7, ""), (7, ""), (0, quiet)]
    r = launch(which, script, stubs={"printf": "exit 1\n"})
    line = _edge_line(r)
    assert line.endswith("(curl 7: connect failed, ?s)" + _TAIL), line
