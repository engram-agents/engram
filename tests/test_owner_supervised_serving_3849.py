"""#3849: the supervised `active` (Linux) / `running` (macOS) arms of start-engramd-owner.sh probe the
socket, in the RESIDUAL case where step 3 did not already.

Contract-first: these cells were written, and run red against unmodified dev, before the hook
changed. Why the branch is residual: `EngramdOwner.start()` writes engramd.pid after bind/listen, and
step 3 (`_live_verdict` = alive) runs before step 4, so a supervised owner that is up AND has
written its pidfile is already probed there. Step 4's `active` / `running` arm is reached only when
`_live_verdict` is NOT `alive` while the unit / job is up: the owner is still before its pidfile
write, engramd.pid is missing or stale, or the unit's process is not an `engramd_owner`.

The contract, per cell, on BOTH arms:

  C1 unit/job up AND the socket serves, with NO pidfile (the bind -> pidfile-write window)
       -> silent, nothing launched, and the probe WAS asked (so the silence is not a skipped probe).
  C2 up AND not serving AND still no live owner -> EXACTLY one line carrying the probe's own
       observation, "no second owner" and the arm's restart remedy; it never says "is alive".
  C3 up AND the probe is ambiguous (bundle without the client) -> today's behaviour (silent).
  C4 up AND not serving on the first probe, but the liveness re-read says `alive` -> today's
       behaviour (silent): the owner finished startup and wrote its pidfile during the probe.
  C5 (Linux) active + NRestarts >= 3 + serving -> today's loud line, byte for byte.
  C6 (Linux) active + NRestarts >= 3 + not serving -> exactly ONE line, the not-serving one.
  C8 up AND not serving, but the liveness re-read answers neither `alive` nor `none`
       (`notregular:*`: a non-regular engramd.pid planted during the probe) -> today's behaviour
       (silent): the line is claimed only on a POSITIVE `none` re-read.
  C7 a live recorded owner that does not serve still gets the step-3 "alive but NOT SERVING" line,
       never the new one.

Harness: `ProbeSeat` (tests/test_owner_serving_probe_3736.py) and the #3709 launchctl fixtures are
REUSED. C4's flip needs no new stub: the test starts a live owner process and writes its pidfile
only once the hook has ASKED the helper to probe, i.e. after step 3 read `none` and while the probe
is still inside its window.
"""

from __future__ import annotations

import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from _fake_owner import FakeOwner  # noqa: E402  (tests/ is on sys.path, as every sibling assumes)
from test_owner_launcher_3663 import FLAG_ON, _physical, _stub, context_lines  # noqa: E402
from test_owner_launchd_3709 import FIXTURES, UID, label_for  # noqa: E402
from test_owner_serving_probe_3736 import (  # noqa: E402
    ProbeSeat,
    _cp,
    _probe_was_asked,
    _wait_for_probe_call,
)

ARMS = ["linux", "darwin"]


class SupSeat(ProbeSeat):
    """A `ProbeSeat` whose supervisor reports the unit / job for THIS home as up.

    linux  -> the Seat's recording `systemctl` stub, mode `active` (NRestarts=0) or `running-3`
              (active/running, NRestarts=3: the #3831 loud trigger).
    darwin -> a recording `launchctl` stub serving the REAL `running` capture from #3709."""

    def __init__(self, tmp_path: Path, arm: str, owner: str = "absent"):
        super().__init__(tmp_path, os_name="Linux" if arm == "linux" else "Darwin", owner=owner)
        self.arm = arm
        self.llog = tmp_path / "launchctl-3849.log"
        self.config(FLAG_ON)
        if arm == "darwin":
            import shutil
            for t in ("shasum", "sha256sum", "id", "head", "tr", "cat"):  # hermetic PATH, Mac and Linux
                found = shutil.which(t)
                if found and not (self.tools / t).exists():
                    (self.tools / t).symlink_to(found)
            _stub(self.stubs, "launchctl", textwrap.dedent(f"""\
                #!/usr/bin/env bash
                echo "$*" >> "{self.llog}"
                cat "{FIXTURES}/running.out"
                """))
        self.up()

    def up(self, mode: str = "active") -> None:
        if self.arm == "linux":
            self.systemctl(mode, self.unit_name())

    @property
    def label(self) -> str:
        return label_for(self.eh)

    @property
    def remedy(self) -> str:
        if self.arm == "linux":
            return f"systemctl --user restart {self.unit_name()}"
        return f"launchctl kickstart -k gui/{UID}/{self.label}"

    def supervisor_calls(self) -> list[str]:
        log = self.syslog if self.arm == "linux" else self.llog
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def assert_only_asked(self) -> None:
        """Nothing but a state READ ever reached the supervisor, and no owner was launched."""
        assert self.launched() == [], "an owner was launched"
        verb = "--user show" if self.arm == "linux" else "print"
        for c in self.supervisor_calls():
            assert c.startswith(verb), f"the supervisor was asked to DO something: {c}"

    def go(self):
        rc, out, err, _ = self.run_timed(limit=20)
        assert rc == 0, err[-300:]
        return rc, out, err


@pytest.fixture(params=ARMS)
def sup_seat(request, tmp_path):
    seat = SupSeat(tmp_path, request.param)
    yield seat
    seat.close()


def _lines(seat: SupSeat, got) -> list[str]:
    return context_lines(_cp(*got))


def _live_owner(seat: SupSeat) -> subprocess.Popen:
    """A live, marker-carrying process for engramd.pid (the 3663 harness's own recipe)."""
    p = subprocess.Popen(["bash", "-c", "exec -a engramd_owner sleep 30"])
    (seat.eh / "engramd.pid").write_text(str(p.pid))
    return p


# ---------------------------------------------------------------------------------------------
# C1: serving, no pidfile -> silent, and the probe really ran
# ---------------------------------------------------------------------------------------------

def test_c1_up_and_serving_with_no_pidfile_is_silent_and_the_probe_was_asked(sup_seat):
    seat = sup_seat
    assert not (seat.eh / "engramd.pid").exists()
    with FakeOwner(seat.eh):
        got = seat.go()
    assert got[1].strip() == "", got[1]
    seat.assert_only_asked()
    assert _probe_was_asked(seat), "the supervised arm never asked the helper to probe"


# ---------------------------------------------------------------------------------------------
# C2: not serving, no live owner -> exactly one line, carrying the observation and the remedy
# ---------------------------------------------------------------------------------------------

def test_c2_up_not_serving_and_no_live_owner_reports_one_line(sup_seat):
    seat = sup_seat
    got = seat.go()
    lines = _lines(seat, got)
    assert len(lines) == 1, got[1]
    line = lines[0]
    assert "is alive" not in line, line  # no live owner pid is established on this path
    assert "no second owner" in line, line
    assert "may still be starting" in line, line  # a budgeted claim: a cold start can miss the window
    assert "if this persists" in line, line
    assert seat.remedy in line, line
    assert _physical(seat.eh) in line, line
    # the probe's own observation, rendered by the helper (same verdict the shell composes from)
    from test_owner_serving_probe_3736 import _raw_probe
    verdict = _raw_probe(seat)
    assert verdict.startswith("notserving:"), verdict
    assert verdict[len("notserving:"):] in line, (verdict, line)
    assert "active" in line if seat.arm == "linux" else "running" in line, line
    seat.assert_only_asked()
    assert _probe_was_asked(seat)


# ---------------------------------------------------------------------------------------------
# C3: ambiguous probe -> today's behaviour (silent)
# ---------------------------------------------------------------------------------------------

def test_c3_an_ambiguous_probe_claims_nothing(sup_seat):
    seat = sup_seat
    (seat.plugin / "engramd_client.py").unlink()  # a bundle without the client
    got = seat.go()
    assert got[1].strip() == "", got[1]
    seat.assert_only_asked()
    assert _probe_was_asked(seat), "silence must come from an ambiguous answer, not a skipped probe"


# ---------------------------------------------------------------------------------------------
# C4: not serving on the first probe, but the owner wrote its pidfile during it -> silent
# ---------------------------------------------------------------------------------------------

def test_c4_a_pidfile_written_during_the_probe_keeps_todays_silence(sup_seat):
    seat = sup_seat
    procs: list[subprocess.Popen] = []

    def _flip():
        if _wait_for_probe_call(seat):
            procs.append(_live_owner(seat))

    t = threading.Thread(target=_flip, daemon=True)
    t.start()
    try:
        got = seat.go()
    finally:
        t.join(timeout=5)
        for p in procs:
            p.kill()
            p.wait()
    assert procs, "the flip never happened: the probe was not reached"
    assert got[1].strip() == "", got[1]
    seat.assert_only_asked()


# ---------------------------------------------------------------------------------------------
# C8: the liveness re-read is neither `alive` nor `none` -> claim nothing
# ---------------------------------------------------------------------------------------------

def test_c8_a_non_regular_pidfile_planted_during_the_probe_claims_nothing(sup_seat):
    """Smallest faithful way to make the re-read return something other than alive / none: step 3
    reads `none` (no pidfile), then, once the hook has ASKED the helper to probe, a FIFO is
    planted at engramd.pid, so the re-read answers `notregular:fifo`. The not-serving line must
    wait for a positive `none`."""
    import os
    seat = sup_seat
    planted: list[bool] = []

    def _flip():
        if _wait_for_probe_call(seat):
            os.mkfifo(seat.eh / "engramd.pid")
            planted.append(True)

    t = threading.Thread(target=_flip, daemon=True)
    t.start()
    try:
        got = seat.go()
    finally:
        t.join(timeout=5)
    assert planted, "the flip never happened: the probe was not reached"
    assert got[1].strip() == "", got[1]
    seat.assert_only_asked()
    assert _probe_was_asked(seat)


# ---------------------------------------------------------------------------------------------
# C5 / C6 (Linux): active + NRestarts >= 3
# ---------------------------------------------------------------------------------------------

def test_c5_linux_restarted_and_serving_keeps_the_loud_line_byte_for_byte():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sup3849-") as td:
        seat = SupSeat(Path(td), "linux")
        try:
            seat.up("running-3")
            with FakeOwner(seat.eh):
                got = seat.go()
            lines = _lines(seat, got)
            assert lines == [LOUD_LINE.format(unit=seat.unit_name(), eh=_physical(seat.eh))], lines
            seat.assert_only_asked()
            assert _probe_was_asked(seat)
        finally:
            seat.close()


def test_c6_linux_restarted_and_not_serving_gets_only_the_not_serving_line():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sup3849-") as td:
        seat = SupSeat(Path(td), "linux")
        try:
            seat.up("running-3")
            got = seat.go()
            lines = _lines(seat, got)
            assert len(lines) == 1, got[1]
            assert "no second owner" in lines[0], lines[0]
            assert "may still be starting" in lines[0], lines[0]
            assert "if this persists" in lines[0], lines[0]
            assert seat.remedy in lines[0], lines[0]
            assert "NRestarts" not in lines[0], lines[0]
            assert "is alive" not in lines[0], lines[0]
            seat.assert_only_asked()
        finally:
            seat.close()


# ---------------------------------------------------------------------------------------------
# C7: the step-3 path keeps its own line
# ---------------------------------------------------------------------------------------------

def test_c7_a_live_recorded_owner_that_does_not_serve_keeps_the_step3_line(sup_seat):
    seat = sup_seat
    p = _live_owner(seat)
    try:
        got = seat.go()
    finally:
        p.kill()
        p.wait()
    lines = _lines(seat, got)
    assert len(lines) == 1, got[1]
    assert "alive but NOT SERVING" in lines[0], lines[0]
    assert "no live owner is recorded" not in lines[0], lines[0]
    seat.assert_only_asked()


# Pinned from the CURRENT hook (`_owner_loud_line`, running-3 fixture), captured on unmodified dev
# before this change, with the unit name and home substituted.
LOUD_LINE = (
    "ENGRAM owner unit {unit} has restarted: NRestarts=3, SubState=running, no exit status recorded; "
    "it has restarted 3 times and is now running, so systemd is not retrying it. ENGRAM_HOME={eh}")
