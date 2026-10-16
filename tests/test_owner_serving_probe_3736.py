"""#3736: "alive" is not "serving" -- the owner-serving probe in start-engramd-owner.sh.

Contract-first: these assertions were written before the implementation (#3736, a follow-up of
#3663/U5 of #2652). The contract, per cell and per path:

  (a) a SERVING owner -> silent exit (already-alive path) / today's `UNSUPERVISED: ... launched`
      line (fresh-launch path). This is the POSITIVE CONTROL for (b): same fixture shape (a live
      pid for this home), opposite outcome, and the fixture's socket genuinely answers `ping`.
      Each (a) cell ALSO asserts the raw helper verdict itself is `serving`, so a probe that
      cannot run (no importable client -> `ambiguous:`) cannot produce the green.
  (b) process alive, socket not answering within the probe budget T -> EXACTLY ONE report naming
      the state, inside the hook's 15 s timeout, exit 0. The claim is BUDGETED ("not serving
      within T"); a bounded probe can never establish "never serving". T is the helper's NAMED
      constant, and it appears in the report. A peer that ACCEPTS the connection and then never
      answers -- the state this feature exists to detect -- is a (b) cell too, and its report must
      carry the `timeout` reason, which is what separates it from a peer with NO socket at all
      (reason `down`).
  (c) the process exits early -> today's report, byte-for-byte.
  (d) the process exits DURING the probe -> its OWN report: not (c)'s (a process that survived the
      ~1.2 s watch did NOT exit "within a second", so that sentence is false here) and not (b)'s
      (a dead process is not a not-serving owner, and the two need different remedies). Liveness is
      re-read at report time. The cell also asserts the probe was actually REACHED (the Seat's stub
      records every helper invocation), so the same green cannot come from a run in which the
      process died inside the WATCH -- where the probe is never asked at all.

Carve-out, stated because the four cells above are not symmetric across the two paths (round-2
reviewer finding F6; the earlier module docstring claimed all four on both paths, which overstated
the already-alive path):

  * (c) as stated is UNREACHABLE on the already-alive path: that path never printed `could not be
    started`, so "today's report, unchanged" has no referent there. The cell named for it asserts a
    NEIGHBOURING property instead -- a pidfile naming a process that is gone reads `none`, so the
    ordinary (launch) path runs and no not-serving claim is made about a dead pid.
  * (d) is REFRAMED there as SILENCE rather than as (c)'s report: (c)'s line says "could not be
    started", which would be false for a process this run did not start. What that path owes is
    nothing at all -- no not-serving claim about a process that is gone, and no second owner.

Harness: `tests/test_owner_launcher_3663.py`'s `Seat` (its own recording `systemctl` stub first on
a hermetic PATH, HOME in a tmp dir -- #3519) is REUSED here, not re-invented. `ProbeSeat` adds
only what the probe needs: the client module in the bundle, a short ENGRAM_HOME (the AF_UNIX
socket path lives at <ENGRAM_HOME>/run/engramd.sock, and sun_path is 107 bytes on Linux / 103 on
Darwin, while pytest's tmp_path nests deeply), and a selectable owner-launch stub.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from _fake_owner import FakeOwner  # noqa: E402  (tests/ is on sys.path, as every sibling assumes)
from test_owner_launcher_3663 import (  # noqa: E402  (the model harness: same fixtures, same rules)
    FLAG_ON,
    Seat,
    _fake_live_owner,
    _physical,
    _stub,
    context_lines,
    needs_proc,
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
CLIENT_PY = SRC / "engramd_client.py"
HOOK_IO = SRC / "hooks" / "claude" / "_engramd_owner_hook_io.py"
LAUNCHER = SRC / "hooks" / "claude" / "start-engramd-owner.sh"
TESTS_DIR = Path(__file__).resolve().parent

# The state phrase the new report carries. ONE casing across both paths, so a single assertion
# covers the report wherever it is produced.
NOT_SERVING = "NOT SERVING"

# (c), pinned byte-for-byte from the CURRENT source (start-engramd-owner.sh, the
# `_ok -eq 0` branch): never retyped from a spec. That line is UNTOUCHED by this PR and its
# byte-fidelity requirement stands.
EXITED_LEAD = "ENGRAM owner could not be started: the process exited within a second."

# (d)'s OWN report sentence (round-2 F1): it names the state it observed -- launched, alive at the
# end of the launch watch, gone at report time -- and carries NO duration, because "exited within a
# second" is false for a process that outlived the ~1.2 s watch. Distinct from (b)'s
# `UNSUPERVISED: ... NOT SERVING` line and from (c)'s above.
D_EXITED_LEAD = "ENGRAM owner exited during the serving probe:"

# The probe budget T is a NAMED constant in the helper (never an anonymous literal), and the
# report's claim is "not serving within T".
BOUND_RE = re.compile(r"^PROBE_TOTAL_BOUND_SECONDS\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.M)

# F2: the probe must spend the WHOLE window its report names. The upper slack is interpreter
# start-up + scheduling; the LOWER bound is the discriminating half -- the pre-F2 loop broke out one
# poll early, realizing ~1.29 s for a bound of 1.5 s.
PROBE_WINDOW_SLACK = (0.1, 1.0)

SERVING_OWNER_SECONDS = 20  # the launched `serving` fixture owner outlives the hook, then exits
SILENT_PEER_SECONDS = 8  # the launched `silent` fixture owner outlives the hook, then exits


def _probe_budget() -> float:
    m = BOUND_RE.search(HOOK_IO.read_text(encoding="utf-8"))
    assert m, "the probe budget must be a named constant (PROBE_TOTAL_BOUND_SECONDS) in the helper"
    return float(m.group(1))


def _assert_used_the_whole_window(elapsed: float) -> None:
    """F2: the window the report NAMES must be the window the probe USED. The lower bound is the
    discriminating half -- a loop that gives up one poll early realizes ~1.25 s for a 1.5 s bound."""
    lo, hi = PROBE_WINDOW_SLACK
    bound = _probe_budget()
    assert bound - lo <= elapsed <= bound + hi, (
        f"the probe realized {elapsed:.2f}s against a printed bound of {bound:g}s: "
        "it must keep attempting while budget remains")


def _exited_line(home) -> str:
    """(c)'s exact report (the `_ok -eq 0` branch): the pinned lead + the physical home + the log
    note. Cell (d) has its own sentence and does not go through here (round-2 F1)."""
    h = _physical(home)
    return f"{EXITED_LEAD} ENGRAM_HOME={h}. Log: {h}/engramd-owner.log"


def _cp(rc: int, out: str, err: str) -> subprocess.CompletedProcess:
    """`Seat.run_timed` returns a tuple; `context_lines` takes a CompletedProcess."""
    return subprocess.CompletedProcess([], rc, out, err)


def _hook_log_lines(seat: Seat) -> list[str]:
    """The report lines the hook itself appended (the log also carries the owner's output)."""
    return [ln for ln in seat.log_lines() if "] [start-engramd-owner]" in ln]


class ProbeSeat(Seat):
    """The #3663 `Seat`, made faithful for the serving probe:

      * `engramd_client.py` sits at the plugin root, where the plugin build ships it -- so the
        helper's probe can import it. (The base `Seat` has no client, so its probe answers
        `ambiguous:` and the pre-existing suite is untouched by the probe.)
      * ENGRAM_HOME is a SHORT path of its own: the owner socket is
        `<ENGRAM_HOME>/run/engramd.sock`, and AF_UNIX caps sun_path at 107 bytes (Linux) / 103
        (Darwin) while pytest's tmp_path nests deeply. Same workaround, same reason, as
        `tests/test_engramd_client.py`'s `sock_dir` fixture.
      * the plugin-venv python stub's owner-launch branch is selectable (`owner=`):

          "absent"  the base stub's `sleep 3` (alive through the watch, never binds)
          "wedged"  `sleep 8` (still alive when the probe gives up, never binds)
          "serving" tests/_fake_owner.py's FakeOwner -- a real socket answering `ping`
          "silent"  a socket that binds, ACCEPTS and never answers (F3: #3736's own hazard)
          "dies"    alive through the ~1.2 s launch watch, gone inside the probe window
    """

    def __init__(self, tmp_path: Path, os_name: str = "Linux", owner: str = "absent"):
        super().__init__(tmp_path, os_name=os_name)
        shutil.copy(CLIENT_PY, self.plugin / "engramd_client.py")
        self._short = Path(tempfile.mkdtemp(prefix="osp3736-"))
        self.eh = self._short / "eh"
        self.eh.mkdir()
        sock = self.eh / "run" / "engramd.sock"
        assert len(os.fsencode(sock)) <= 103, f"ENGRAM_HOME too long for an AF_UNIX socket: {sock}"
        self._stub_python(owner)

    def close(self) -> None:
        shutil.rmtree(self._short, ignore_errors=True)

    def _stub_python(self, owner: str) -> None:
        python_bin = self.home / ".engram" / "venv" / "bin"
        if owner == "serving":
            launcher = self._short / "serving_owner.py"
            launcher.write_text(textwrap.dedent(f"""\
                import sys, time
                sys.path.insert(0, {str(TESTS_DIR)!r})
                from _fake_owner import FakeOwner
                owner = FakeOwner(sys.argv[1])
                owner.start()
                try:
                    time.sleep({SERVING_OWNER_SECONDS})
                finally:
                    owner.stop()
                """), encoding="utf-8")
            tail = f'exec "{sys.executable}" "{launcher}" "{self.eh}"'
        elif owner == "silent":
            # F3: the state #3736 exists to detect -- a socket that binds, ACCEPTS, and never
            # answers. Written as a standalone script (not an import) so the launched process needs
            # nothing but the stdlib, exactly like the `serving` launcher above.
            launcher = self._short / "silent_owner.py"
            launcher.write_text(textwrap.dedent(f"""\
                import os, socket, sys, time
                run = os.path.join(sys.argv[1], "run")
                os.makedirs(run, mode=0o700, exist_ok=True)
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(os.path.join(run, "engramd.sock"))
                srv.listen(8)
                srv.settimeout(0.1)
                held = []
                end = time.monotonic() + {SILENT_PEER_SECONDS}
                while time.monotonic() < end:
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    held.append(conn)  # accepted, and NEVER answered
                """), encoding="utf-8")
            tail = f'exec "{sys.executable}" "{launcher}" "{self.eh}"'
        elif owner == "wedged":
            tail = "exec sleep 8"
        elif owner == "dies":
            tail = "sleep 1.9; exit 0"
        else:
            assert owner == "absent", owner
            tail = "exec sleep 3"
        # The base Seat's stub, with only the owner-launch branch replaced (its flag-helper and
        # I/O-helper routing must stay: the hook's own calls go through them).
        _stub(python_bin, "python3", textwrap.dedent(f"""\
            #!/usr/bin/env bash
            for a in "$@"; do case "$a" in
              *engramd_owner_flags.py) exec "{sys.executable}" "$@";;
              *_engramd_owner_hook_io.py) printf '%s\\n' "$*" >> "{self.helper_calls}"; exec "{sys.executable}" "$@";;
            esac; done
            echo "owner-stub-output"
            printf '%s\\n' "$*" >> "{self.record}"
            if [ -f "{self.pre_script}" ]; then . "{self.pre_script}"; fi
            if [ -f "{self.fail_flag}" ]; then echo "boom" >&2; exit 7; fi
            {tail}
            """))


@pytest.fixture
def probe_seat(tmp_path):
    """Factory for one `ProbeSeat` per test (two in one `tmp_path` would collide)."""
    seats: list[ProbeSeat] = []

    def _make(owner: str = "absent") -> ProbeSeat:
        assert not seats, "one ProbeSeat per test"
        seat = ProbeSeat(tmp_path, owner=owner)
        seats.append(seat)
        return seat

    yield _make
    for seat in seats:
        seat.close()


def _raw_probe(seat: Seat, home: Path | None = None) -> str:
    """The helper's OWN `serving` verdict, run directly: the shell's report is composed from
    exactly this string, so the two cannot disagree."""
    seat._ship_3709_files()  # the harness's own bundle setup (the base file's run() calls it)
    env = {"HOME": str(seat.home), "ENGRAM_HOME": str(home or seat.eh),
           "PATH": f"{seat.stubs}:{seat.tools}", "TMPDIR": str(seat.tmp)}
    r = subprocess.run(
        [sys.executable, str(seat.plugin / "hooks" / "_engramd_owner_hook_io.py"),
         "serving", str(home or seat.eh)],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
        cwd=str(seat.tmp), timeout=30,
    )
    assert r.returncode == 0, r.stderr[-300:]
    return r.stdout.strip()


def _probe_was_asked(seat: Seat) -> bool:
    """Has the hook ASKED the helper to probe? The Seat's python stub records every helper
    invocation before exec'ing it, so this is the observation a cell needs to show the probe was
    REACHED -- a green produced where the probe never ran is not evidence (F1, F4).

    The subcommand is looked for AT THE ARGV POSITION, per recorded call: the token naming the
    helper immediately followed by `serving`. Neither a substring nor a bare token will do. The
    hook's own reports are recorded as helper calls too -- `<helper> report <home> ENGRAM owner
    exited during the serving probe: ...` -- and a report's MESSAGE text carries `serving` as a
    token of its own, so both looser forms accept a run in which the helper was never asked to
    probe at all. `report` stands where the subcommand stands, and that position is what makes
    this a check on the probe rather than on the report about it (Kepler, #3825 merge seat)."""
    if not seat.helper_calls.exists():
        return False
    for line in seat.helper_calls.read_text(encoding="utf-8").splitlines():
        toks = line.split()
        for i, tok in enumerate(toks[:-1]):
            if tok.endswith("_engramd_owner_hook_io.py") and toks[i + 1] == "serving":
                return True
    return False


def _wait_for_probe_call(seat: Seat, timeout: float = 10.0) -> bool:
    """Block until the hook has ASKED the helper to probe, so a kill issued from the watcher below
    lands INSIDE the probe window rather than at a guessed wall-clock moment."""
    end = time.monotonic() + timeout
    while True:
        if _probe_was_asked(seat):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


class SilentPeer:
    """A peer that binds the owner socket, ACCEPTS the connection and then NEVER answers (F3).

    None of the earlier fixtures exercised this state -- `absent`, `wedged`, `serving` and `dies`
    either have no socket at all (the connect fails at once, so the observed reason is `down`) or
    answer properly. It is exactly the state #3736 was written to detect, and the only way to show
    that a silent peer cannot extend the call is to run the probe against one."""

    def __init__(self, home):
        self.socket_path = Path(home) / "run" / "engramd.sock"
        self._held: list = []
        self._stop = threading.Event()
        self._srv = None
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        self.socket_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.socket_path))
        self._srv.listen(8)
        self._srv.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._held.append(conn)  # accepted, and never answered

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        for conn in self._held:
            conn.close()
        if self._srv is not None:
            self._srv.close()
        self.socket_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# (a) the positive control: a SERVING owner
# ---------------------------------------------------------------------------

@needs_proc
def test_a_already_alive_and_serving_exits_silently(probe_seat):
    """Cell (a), already-alive path -- and cell (b)'s success control: the SAME fixture shape
    (a live pid whose argv carries the owner marker), with a socket that answers `ping`. The
    live pid alone is not the control; the answering socket is -- which is why the fixture must
    show the raw helper verdict is `serving` (F4): a probe that cannot run answers `ambiguous:`,
    and the hook then stays silent too, so the silence alone does not exclude that rival cause."""
    seat = probe_seat()
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    with FakeOwner(seat.eh):  # a genuine serving owner: an AF_UNIX socket answering `ping`
        assert _raw_probe(seat) == "serving", "the (a) control needs a probe that really ran"
        p = _fake_live_owner(seat)
        try:
            rc, out, err, elapsed = seat.run_timed(limit=15)
        finally:
            p.kill()
            p.wait()
    assert rc == 0, err[-300:]
    assert (out, err) == ("", ""), (out, err)
    assert seat.launched() == []
    assert elapsed < 15


def test_a_fresh_launch_that_serves_reports_todays_launch_line(probe_seat):
    """Cell (a), fresh-launch path: the process we launched IS serving, so today's
    `UNSUPERVISED: engramd_owner launched (pid ...)` line stands -- the opposite outcome to
    cell (b) on this path, with the same fixture but for whether the socket answers. The raw
    helper verdict is asserted after the run (F4): the launched owner keeps its socket open, so
    a probe that could not run cannot be what produced this green."""
    seat = probe_seat("serving")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert _raw_probe(seat) == "serving", "the launched owner must really answer `ping`"
    assert len(seat.launched()) == 1, seat.launched()
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    assert "UNSUPERVISED: engramd_owner launched (pid " in lines[0], lines[0]
    assert NOT_SERVING not in lines[0], lines[0]
    assert _physical(seat.eh) in lines[0], lines[0]
    assert elapsed < 15


# ---------------------------------------------------------------------------
# (b) alive, but not serving within the probe budget -> ONE report
# ---------------------------------------------------------------------------

@needs_proc
def test_b_already_alive_but_not_serving_reports_the_state_once(probe_seat):
    """Cell (b), already-alive path. Today this is a SILENT exit (the process exists); the probe
    must make it exactly one report naming the state -- and must launch no second owner."""
    seat = probe_seat()
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    p = _fake_live_owner(seat)  # live, marker-carrying, and NO socket at all
    try:
        rc, out, err, elapsed = seat.run_timed(limit=15)
    finally:
        p.kill()
        p.wait()
    assert rc == 0, err[-300:]
    assert elapsed < 15
    assert seat.launched() == [], "a second owner was launched on the not-serving path"
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    assert NOT_SERVING in line, line
    assert "UNSUPERVISED" not in line, "the already-alive path must not claim an unsupervised launch"
    assert "could not be started" not in line, "a live process must not be reported as exited"
    assert _physical(seat.eh) in line, line
    assert f"within {_probe_budget():g}s" in line, line  # the claim is budgeted, not absolute
    assert out.count(NOT_SERVING) == 1, out
    assert len([ln for ln in seat.log_lines() if NOT_SERVING in ln]) == 1, seat.log_lines()
    assert len(_hook_log_lines(seat)) == 1, seat.log_lines()


def test_b_fresh_launch_alive_but_not_serving_reports_the_state_once(probe_seat):
    """Cell (b), fresh-launch path: our launch is alive after the ~1.2 s watch and never binds a
    socket. Today: `UNSUPERVISED: engramd_owner launched (pid ...)` -- a false health claim."""
    seat = probe_seat("wedged")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert elapsed < 15
    assert len(seat.launched()) == 1, "exactly one launch, and no second one for the probe"
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    assert NOT_SERVING in line, line
    # The unsupervised-launch family is preserved (the reader still learns it was launched and is
    # unsupervised); what changes is the health claim.
    assert "UNSUPERVISED: engramd_owner launched (pid " in line, line
    assert "could not be started" not in line, line
    assert _physical(seat.eh) in line, line
    assert f"within {_probe_budget():g}s" in line, line
    assert str(seat.log) in line or _physical(seat.log) in line, line
    assert out.count(NOT_SERVING) == 1, out
    assert len([ln for ln in seat.log_lines() if NOT_SERVING in ln]) == 1, seat.log_lines()
    assert len(_hook_log_lines(seat)) == 1, seat.log_lines()


def test_b_fresh_launch_that_accepts_and_never_answers_reports_the_timeout_state_once(probe_seat):
    """Cell (b) for #3736's OWN hazard (F3): the owner we launched binds a socket, ACCEPTS the
    connection and never answers. Every earlier fixture either had no socket at all (the connect
    failed at once, reason `down`) or answered properly, so nothing exercised this state.

    It must produce the not-serving report exactly once, launch nothing, and stay inside the hook's
    15 s timeout. The report must name the `timeout` reason -- that is the control that excludes the
    rival cause (a socket that was never there at all would read `down` and produce the same
    sentence), so this cell cannot be satisfied by a peer the probe never really reached."""
    seat = probe_seat("silent")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert elapsed < 15
    assert len(seat.launched()) == 1, "exactly one launch, and no second one for the probe"
    assert _probe_was_asked(seat), "the probe was never asked"
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    assert NOT_SERVING in line, line
    assert "(last: timeout)" in line, f"a silent peer is `timeout`, not a missing socket: {line}"
    assert "UNSUPERVISED: engramd_owner launched (pid " in line, line
    assert _physical(seat.eh) in line, line
    assert f"within {_probe_budget():g}s" in line, line
    assert out.count(NOT_SERVING) == 1, out
    assert len([ln for ln in seat.log_lines() if NOT_SERVING in ln]) == 1, seat.log_lines()
    assert len(_hook_log_lines(seat)) == 1, seat.log_lines()


# ---------------------------------------------------------------------------
# (c) the process exits early -> today's report, byte-for-byte
# ---------------------------------------------------------------------------

def test_c_process_exits_early_keeps_todays_report_byte_for_byte(probe_seat):
    """Cell (c), fresh-launch path: the launch dies inside the ~1.2 s watch, so the probe is
    never reached and today's line must be byte-identical."""
    seat = probe_seat("wedged")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    seat.fail_flag.write_text("x")  # the stub owner exits 7 immediately
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert elapsed < 15
    lines = context_lines(_cp(rc, out, err))
    assert lines == [_exited_line(seat.eh)], out
    assert NOT_SERVING not in out, "the exited state must not be reported as not-serving"
    assert len(_hook_log_lines(seat)) == 1, seat.log_lines()
    assert _hook_log_lines(seat)[0].endswith(_exited_line(seat.eh)), seat.log_lines()


def test_c_a_pidfile_naming_a_dead_process_is_not_alive(probe_seat):
    """The already-alive path's (c): a pidfile naming a process that is gone is today's `none`,
    so the ordinary path runs -- a dead pid is never reported as alive-but-not-serving."""
    seat = probe_seat("serving")  # so the only report this run can produce is a launch line
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    (seat.eh / "engramd.pid").write_text("999999")
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert len(seat.launched()) == 1, "a dead pid must not suppress the launch"
    assert NOT_SERVING not in out + err, (out, err)


# ---------------------------------------------------------------------------
# (d) the process exits DURING the probe -> (c)'s report, not (b)'s
# ---------------------------------------------------------------------------

def test_d_a_process_that_exits_during_the_probe_gets_its_own_report(probe_seat):
    """Cell (d), fresh-launch path: alive through the watch, gone inside the probe window. It gets
    its OWN report (F1) -- not (c)'s, whose "exited within a second" is FALSE for a process that
    outlived the ~1.2 s watch, and not (b)'s, because a dead process is not a not-serving owner.

    The cell also asserts the PROBE WAS REACHED. Without that it would pass on a run where the
    process died inside the WATCH -- the probe is then never asked, no (d) state ever arises, and
    (c)'s line is emitted: exactly the mutant that left the round-1 cell green."""
    seat = probe_seat("dies")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert elapsed < 15
    assert len(seat.launched()) == 1, seat.launched()
    assert _probe_was_asked(seat), "the probe was never asked: the process died inside the WATCH (cell c)"
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    assert line.startswith(D_EXITED_LEAD), line
    assert EXITED_LEAD not in line, "a process that survived the watch did not exit 'within a second'"
    assert "within a second" not in line, line
    assert NOT_SERVING not in line, "a process that died mid-probe is not a not-serving owner"
    assert "UNSUPERVISED" not in line, "no unsupervised-owner health claim for a process that is gone"
    assert "engramd_owner (pid " in line, line
    assert _physical(seat.eh) in line, line
    assert str(seat.log) in line or _physical(seat.log) in line, line
    assert len(_hook_log_lines(seat)) == 1, seat.log_lines()


@needs_proc
def test_d_an_already_alive_owner_that_dies_during_the_probe_is_not_reported_as_not_serving(probe_seat):
    """Cell (d), already-alive path: the pid is live when the verdict is read, and dies while the
    probe runs. Liveness is re-read at report time, so no not-serving claim is made, and (decision
    4) no second owner is launched either."""
    seat = probe_seat()
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    p = _fake_live_owner(seat)
    watcher = threading.Thread(
        target=lambda: (_wait_for_probe_call(seat), p.kill(), p.wait()), daemon=True)
    watcher.start()
    try:
        rc, out, err, elapsed = seat.run_timed(limit=15)
    finally:
        watcher.join(timeout=10)
        if p.poll() is None:
            p.kill()
            p.wait()
    assert rc == 0, err[-300:]
    assert seat.launched() == []
    assert NOT_SERVING not in out + err, (out, err)
    assert "could not be started" not in out, out


# ---------------------------------------------------------------------------
# the predicate's own falsification: the report about the probe is not the probe
# ---------------------------------------------------------------------------

# Verbatim, from the fresh-launch branch of start-engramd-owner.sh: ask the helper to probe, then
# decide on what the probe saw. Substituted -- never retyped -- by the mutation below.
_MUTATION_TARGET = (
    '    _serving="$(_probe_serving)"\n'
    '    if ! _alive "$_pid"; then\n'
)


def _mutate_launcher_to_report_without_probing(seat: Seat) -> None:
    """Replace the fresh-launch probe call in this seat's OWN COPY of start-engramd-owner.sh with a
    blind wait of the same named window (extract verbatim, assert present, substitute, run in a
    subprocess -- the idiom `_run_supervisor_function` uses in
    tests/test_start_daemon_probe_branch_logging.py).

    The wait is the half that keeps the mutation TARGETED. Drop it and the process would still be
    alive at report time, so the launcher would take a different branch and the cell would be
    exercising a mutation of the STATE rather than of the probe; keep it, and the only thing
    removed is the call -- `helper_calls` receives no `serving` line, and (d)'s own report still
    goes out."""
    src = LAUNCHER.read_text(encoding="utf-8")
    assert src.count(_MUTATION_TARGET) == 1, (
        "mutation target snippet not found once -- start-engramd-owner.sh's shape changed")
    replacement = (
        '    _serving="ambiguous: mutation -- the helper was never asked"\n'
        f'    sleep {_probe_budget():g}\n'
        '    if ! _alive "$_pid"; then\n'
    )
    seat.hook.write_text(src.replace(_MUTATION_TARGET, replacement), encoding="utf-8")


def test_mutation_a_report_that_merely_says_serving_is_not_a_probe(probe_seat):
    """Falsification for the predicate (Kepler, #3825 merge seat): the hook's own report may not
    stand in for the probe it reports about.

    Cell (d)'s report names the state it observed in the words "exited during the serving probe",
    and the Seat's stub records that report -- message text, `serving` and all -- as a helper call.
    So a launcher that emits the report WITHOUT ever asking the helper to probe still leaves, in
    `helper_calls`, a line that both a substring match and a bare-token match read as "the probe
    was asked". This mutation is exactly that launcher: the probe call becomes a blind wait of its
    own window, the launched owner dies inside the wait, and (d)'s line goes out carrying the one
    word the predicate must not be keying on.

    The assertion is the INVERSE of cell (d)'s
    (`test_d_a_process_that_exits_during_the_probe_gets_its_own_report`), so what it guards is that
    cell going green for the wrong reason: run under this launcher, (d) fails at its
    `_probe_was_asked` line, and that is what makes (d)'s green evidence about the probe rather than
    about the report. The red-first evidence of the round-1 cell reached the same cell by a
    different lever -- TIMING, moving the owner's death into the watch -- which left the probe
    unasked but also left no report line for the predicate to be fooled by."""
    seat = probe_seat("dies")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    _mutate_launcher_to_report_without_probing(seat)
    rc, out, err, elapsed = seat.run_timed(limit=15)
    assert rc == 0, err[-300:]
    assert elapsed < 15
    lines = context_lines(_cp(rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    # The premise of the round, asserted rather than assumed: (d)'s own text carries the word.
    assert " serving " in D_EXITED_LEAD, "the report no longer carries the word the bait rides on"
    assert line.startswith(D_EXITED_LEAD), line
    recorded = seat.helper_calls.read_text(encoding="utf-8")
    assert D_EXITED_LEAD in recorded, recorded  # the report IS recorded as a helper call ...
    assert not _probe_was_asked(seat), (        # ... and must not be read as the probe having run
        "the probe was never asked: this launcher never invoked the helper's `serving` subcommand, "
        "so only the report's own wording -- `exited during the serving probe` -- could have "
        "produced a True here")


# ---------------------------------------------------------------------------
# the helper's own contract: the bound, the ping method, and the ambiguity rule
# ---------------------------------------------------------------------------

def test_the_helper_answers_serving_for_a_socket_that_answers_ping(probe_seat):
    """The helper-level positive control for the probe itself: the `ping` method really is what
    the owner serves, and a serving socket reads `serving` (never `notserving:`)."""
    seat = probe_seat()
    with FakeOwner(seat.eh):
        assert _raw_probe(seat) == "serving"


def test_the_probe_is_bounded_by_its_named_constant(probe_seat):
    """The helper owns its own deadline (stock macOS has no GNU `timeout`): with no owner at all
    the probe still answers -- inside its named bound, never an unbounded connect. F2: it also
    SPENDS that bound, so the window the report names is the window the probe used."""
    seat = probe_seat()
    t0 = time.monotonic()
    verdict = _raw_probe(seat)
    elapsed = time.monotonic() - t0
    assert verdict.startswith("notserving:"), verdict
    _assert_used_the_whole_window(elapsed)


def test_the_probe_is_bounded_with_a_peer_that_accepts_and_never_answers(probe_seat):
    """F3: the same bound, against the peer this feature exists to detect -- one that ACCEPTS the
    connection and then never answers. Nothing earlier in the suite could show that a silent peer
    cannot extend the call: the other fixtures have no socket at all, so the connect fails at once
    and the reason is `down`. This is what makes "the whole probe is bounded structurally" a
    measurement rather than a reading of the client."""
    seat = probe_seat()
    with SilentPeer(seat.eh):
        t0 = time.monotonic()
        verdict = _raw_probe(seat)
        elapsed = time.monotonic() - t0
    assert verdict.startswith("notserving:"), verdict
    assert "(last: timeout)" in verdict, verdict  # accepted and never answered, NOT a missing socket
    _assert_used_the_whole_window(elapsed)


def test_a_bundle_without_the_client_is_ambiguous_never_not_serving(tmp_path):
    """With no engramd_client.py in the bundle the helper must claim NOTHING about the owner: a
    broken bundle is `ambiguous:`, never `notserving:` (which would be a false diagnosis). The
    base `Seat` is exactly that bundle."""
    seat = Seat(tmp_path)
    seat.config(FLAG_ON)
    assert _raw_probe(seat).startswith("ambiguous:")


def test_the_report_carries_the_helpers_own_budgeted_verdict(probe_seat):
    """The shell renders the helper's verdict, so the budget T and the observed reason live in ONE
    place. Asserted against the helper's output, not against a copy of the wording."""
    seat = probe_seat()
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    verdict = _raw_probe(seat)
    assert verdict.startswith("notserving:"), verdict
    phrase = verdict[len("notserving:"):]
    assert f"within {_probe_budget():g}s" in phrase, phrase
    assert re.search(r"\(last: [A-Za-z0-9_:-]+\)$", phrase), phrase
    p = _fake_live_owner(seat)
    try:
        rc, out, err, _ = seat.run_timed(limit=15)
    finally:
        p.kill()
        p.wait()
    assert rc == 0, err[-300:]
    assert phrase in out, (phrase, out)
