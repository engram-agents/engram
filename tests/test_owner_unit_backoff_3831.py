"""#3831: the owner unit must not go permanently failed (unlimited restarts + bounded backoff).

The unit under test is the Linux systemd USER unit RENDERED from
`templates/engramd-owner.service.template` (through the same installer path the C1 cell in
`test_owner_launcher_3663.py` reads).  The RENDER assertions for #3831 -- the template, the
launcher's loud line, and the macOS-untouched pin -- all live in that file.

THE CELLS HERE ARE OPT-IN AND DO NOT RUN IN A DEFAULT SUITE.  Set
`ENGRAM_OWNER_BACKOFF_LIVE=1` on a Linux seat with a `systemd --user` bus.  The reason is a
collision between this cell's design and a suite-wide, load-bearing discipline:

  * #3519 (repo-root `conftest.py`) shims the service-manager tools so that NO TEST MAY MUTATE THE
    REAL SERVICE MANAGER -- its origin is a test that left a launchd job respawning for three days.
    As amended by #3837 the guard is keyed on the tool CLASS, so `systemctl`, `systemd-run`,
    `loginctl` and `launchctl` are ALL shimmed: it refuses every mutating verb (`stop`,
    `reset-failed`) and, for `systemd-run`, everything but `--help`/`--version`, with exit 99.
  * A behavioural cell needs a REAL transient unit (`systemd-run --user`), so under the guard the
    cell cannot run at all -- and a run that bypassed the guard would create real units it could
    not tear down (`systemctl stop` is refused), silently accumulating them, including ones stuck
    in a restart loop.  That is precisely the harm #3519 exists to prevent, and the #3663 file's
    own stated discipline is "Nothing here reaches a real one."

  Therefore the behavioural cell is a SEAT cell, not a suite cell, and it is self-cleaning so
  that even an opted-in run leaves nothing behind: every unit is created with `--collect`, and
  every stub SELF-TERMINATES (it exits 0 -- a clean stop, which `Restart=on-failure` does not
  restart -- once it has run enough times).  No `systemctl stop` is used or needed.

CONTRACT (the two cells, and the second is the CONTROL):
  * fix     -- the rendered unit's own start limit (StartLimitIntervalSec=0) + its backoff:
               after six failures inside the rendered 300 s window the unit is NOT failed -- it is
               `activating (auto-restart)` -- and the observed gap between later restarts exceeds
               RestartSec (the backoff is real, not just a directive being present).
  * control -- StartLimitIntervalSec=300 + StartLimitBurst=5 (the OLD policy, restored
               literally): the restarts CAP at the burst and the unit ends.  Without this the fix
               cell cannot distinguish the fix from the defect.

Because the fix cell CARRIES the rendered unit's start-limit properties rather than restating
them, it is RED against the pre-fix template and green after -- a real contract-first control.

C3's behavioural half (a stub exiting 4 is not restarted) is deliberately NOT here: under
`--collect` a collected unit reads as `NRestarts=0`, so the cell could not tell "not restarted"
from "vanished".  C3's claim is pinned statically in test_owner_launcher_3663.py instead.

TIMING.  The start-limit WINDOW is the template's, verbatim (300 s): that is the policy under
test.  Only the DURATION knobs are scaled so the cell runs in seconds instead of minutes --
RestartSec 10s -> 1s and RestartMaxDelaySec 300s -> 4s; RestartSteps is the template's 5.  Six
failures in ~15 s sit well inside the 300 s window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # the Sandbox + the INI reader live in the #3663 file
    sys.path.insert(0, str(_HERE))
import test_owner_launcher_3663 as owner_launcher  # noqa: E402

LIVE_ENV = "ENGRAM_OWNER_BACKOFF_LIVE"

# The OLD policy the CONTROL restores literally: five starts inside a 300 s window trip it.
OLD_INTERVAL = "300"
OLD_BURST = "5"
# Scaled-down durations (see TIMING above); the 1 s reference RestartSec to measure gaps against.
RESTART_SEC = 1.0
RESTART_SEC_ARG = "1s"
MAX_DELAY_ARG = "4s"
# The OLD policy's observable signature under --collect, MEASURED (RestartSec 1s and 2s, poll
# 0.2 s): NRestarts climbs 0,1,2,3,4 and then the unit is GONE -- never 5, never 6. The refused
# start is the 6th (burst=5), so the last completed restart is OLD_BURST-1, and --collect removes
# the unit the moment that start is refused. The FIX cell reaches 6+ and stays; 4-and-gone versus
# 6-and-restarting is the discrimination.
CONTROL_CAP = int(OLD_BURST) - 1
# The stub self-terminates after this many runs, so no unit can outlive the cell.
STUB_RUNS = 9


def _live_run_or_skip() -> dict:
    """The environment the cells need, or a NAMED skip saying WHICH condition was missing.

    Four conditions, each named: the opt-in (the #3519 reason, see the module docstring), a real
    `systemd-run` (probed BEHAVIOURALLY -- see below), a `systemctl`, and a reachable
    `systemd --user` bus (CI containers; WSL with no runtime dir). A skip that reads like a pass is
    worse than no cell."""
    if os.environ.get(LIVE_ENV) != "1":
        pytest.skip(
            "OPT-IN seat cell, not run by default: set "
            f"{LIVE_ENV}=1 on a Linux seat with a `systemd --user` bus. It creates REAL transient "
            "units and cannot tear them down (`systemctl --user stop|reset-failed` -> exit 99), and "
            "the suite-wide #3519 guard (repo-root conftest.py, keyed on the tool CLASS since "
            "#3837) shims `systemd-run` itself -- it refuses everything but --help/--version -- so "
            "a default pytest run can neither create the unit nor clear it up. Reaching the real "
            "service manager inside the suite is what #3519 and the #3663 file's own discipline "
            "both forbid. This skip launched nothing.")
    # A PRESENCE check is dead code here: the #3519 guard puts a shimmed `systemd-run` first on PATH
    # for every test, so `shutil.which("systemd-run")` is never None (#3837 shimmed it; the guard's
    # own comment names that side effect). Probe BEHAVIOUR instead: the shim lets `--version`
    # through and execs a real tool only if one exists further down PATH, so rc 0 means a real
    # `systemd-run` and rc 99 means the guard refused us.
    try:
        probe_run = subprocess.run(["systemd-run", "--version"], capture_output=True, text=True,
                                   timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"no usable `systemd-run` on PATH ({type(exc).__name__}): this host cannot "
                    "create a transient user unit")
    if probe_run.returncode == 99 or "#3519" in (probe_run.stderr or ""):
        pytest.skip("the suite-wide #3519 guard shims `systemd-run` (everything but "
                    "--help/--version is refused): a default run cannot create a transient unit. "
                    f"Opt in with {LIVE_ENV}=1 on a seat that runs this outside the suite.")
    if probe_run.returncode != 0:
        pytest.skip(f"`systemd-run --version` exited {probe_run.returncode}: no real `systemd-run` "
                    "on PATH, so this host cannot create a transient user unit")
    if shutil.which("systemctl") is None:
        pytest.skip("no `systemctl` on PATH: a transient unit's state cannot be observed here")
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        cand = f"/run/user/{os.getuid()}"
        if os.path.isdir(cand):
            env["XDG_RUNTIME_DIR"] = cand
    try:
        probe = subprocess.run(["systemctl", "--user", "show", "-p", "Version"],
                               capture_output=True, text=True, env=env, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"no `systemd --user` bus reachable ({type(exc).__name__}): "
                    "a transient unit cannot be created here")
    if probe.returncode != 0 or not probe.stdout.startswith("Version="):
        why = (probe.stderr or probe.stdout).strip().splitlines()[:1]
        pytest.skip(f"no `systemd --user` bus reachable (systemctl said {why!r}): "
                    "a transient unit cannot be created here")
    return env


def _unit_name(tag: str) -> str:
    """A transient unit name that cannot collide with a real owner unit (`engramd-owner-<8hex>`)
    nor with a concurrent run of this cell on the host."""
    return f"engramd-owner-bk3831-{tag}-{os.getpid()}.service"


def _rendered_unit_ini(tmp_path: Path) -> dict:
    """The RENDERED owner unit, parsed: the policy the transient unit carries is read off the
    installer's own output, never restated here."""
    (tmp_path / "sb").mkdir()
    sb = owner_launcher.Sandbox(tmp_path / "sb")
    unit = sb.install_owner(str(tmp_path / "seat"))
    return owner_launcher.parse_unit(unit.read_text(encoding="utf-8"))


def _carried_properties(ini: dict) -> list[str]:
    """The rendered unit's restart policy, as systemd-run --property arguments.

    The START LIMIT (the policy #3831 changes) is carried LITERALLY, so this cell reddens against
    the pre-fix template.  RestartSteps is the template's; only RestartSec/RestartMaxDelaySec are
    scaled for runtime (TIMING above)."""
    u, svc = ini["Unit"], ini["Service"]
    props = ["Restart=on-failure", f"RestartSec={RESTART_SEC_ARG}"]
    interval = (u.get("StartLimitIntervalSec") or [None])[0]
    burst = (u.get("StartLimitBurst") or [None])[0]
    if interval is not None:  # absent -> systemd's 5-in-10s default, i.e. the bug: carried as-is
        props.append(f"StartLimitIntervalSec={interval}")
    if burst is not None:
        props.append(f"StartLimitBurst={burst}")
    # Round 2 (#3835): NO `or ["5"]` fallback. A template that DROPS RestartSteps must redden this
    # cell (KeyError) rather than being silently defaulted to a value the template does not state:
    # a default here would carry a policy the rendered unit never declared, which is the opposite
    # of the "carry the rendered unit's own literals" discipline this cell exists for.
    steps = svc["RestartSteps"][0]
    if steps.isdigit() and int(steps) >= 1:
        props.append(f"RestartSteps={steps}")
    props.append(f"RestartMaxDelaySec={MAX_DELAY_ARG}")
    return props


def _self_terminating_stub(tmp_path: Path, runs: int) -> Path:
    """A stub that exits 1 the first `runs` times it runs, then exits 0.

    Exit 0 is a clean stop, which `Restart=on-failure` does NOT restart, so the unit ends itself.
    This is the cell's ONLY cleanup path: `systemctl stop` is refused by the #3519 guard, so a
    unit that could not end itself would loop forever and leak (--collect removes a unit once it
    stops, and only then).  A regression that removes the stub's ceiling shows up as a leaked
    unit, not as a silent pass."""
    counter = tmp_path / "stub.count"
    p = tmp_path / "exit1_then_clean.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f'n="$(cat "{counter}" 2>/dev/null || echo 0)"; n=$((n+1)); echo "$n" > "{counter}"\n'
        f'[ "$n" -ge {runs} ] && exit 0\n'
        "exit 1\n",
        encoding="utf-8")
    p.chmod(0o755)
    return p


def _run_unit(env: dict, name: str, stub: Path, properties: list[str]) -> subprocess.CompletedProcess:
    argv = ["systemd-run", "--user", f"--unit={name}", "--collect"]
    argv += [f"--property={p}" for p in properties]
    argv.append(str(stub))
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)


def _show(env: dict, name: str) -> dict:
    """The unit's NRestarts / ActiveState / SubState / LoadState ({'LoadState': 'not-found'} once
    the unit is gone -- which, with --collect, it is the moment it stops or fails)."""
    r = subprocess.run(
        ["systemctl", "--user", "show", name, "-p", "NRestarts,ActiveState,SubState,LoadState"],
        capture_output=True, text=True, env=env, timeout=15)
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def _int(detail: dict, key: str) -> int:
    try:
        return int(detail.get(key) or 0)
    except ValueError:
        return 0


def _watch(env: dict, name: str, deadline_s: float):
    """Poll `name` until it is gone, yielding (elapsed, NRestarts, LoadState, ActiveState,
    SubState) on every NRestarts change. Stops at the deadline or once the unit is gone."""
    t0 = time.monotonic()
    seen = 0
    while time.monotonic() - t0 < deadline_s:
        s = _show(env, name)
        if s.get("LoadState") == "not-found":
            yield time.monotonic() - t0, 0, "not-found", "", ""
            return
        nr = _int(s, "NRestarts")
        if nr != seen:
            seen = nr
            yield (time.monotonic() - t0, nr, s.get("LoadState", ""), s.get("ActiveState", ""),
                   s.get("SubState", ""))
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# C2 -- the fix: unlimited restarts with a real backoff
# ---------------------------------------------------------------------------

def test_c2_a_crash_loop_never_reaches_failed_and_the_backoff_is_real(tmp_path):
    """StartLimitIntervalSec=0 + RestartSteps/RestartMaxDelaySec: after six failures inside the
    rendered unit's own 300 s window the unit is NOT failed -- it is `activating (auto-restart)`
    -- and the observed gap between later restarts exceeds RestartSec (the backoff is real, not
    just a directive being present).  The unit then ends itself, so the cell leaves nothing."""
    env = _live_run_or_skip()  # raises the NAMED skip unless the live run is opted in
    properties = _carried_properties(_rendered_unit_ini(tmp_path))
    name = _unit_name("fix")
    started = _run_unit(env, name, _self_terminating_stub(tmp_path, STUB_RUNS), properties)
    assert started.returncode == 0, (started.stdout, started.stderr)

    timeline: list[tuple[float, int]] = []
    for elapsed, nr, load, active, subst in _watch(env, name, 150.0):
        if load == "not-found":
            break
        timeline.append((elapsed, nr))
        assert active != "failed", (
            f"the rendered policy reached `failed` at NRestarts={nr}: the 5-in-300 start limit is "
            f"back, and the owner is then down for good until a human runs "
            f"`systemctl --user reset-failed` (carried properties: {properties})")
        if nr >= 6:
            # the state a persistent fault rests in under the new policy
            s = _show(env, name)
            assert s.get("ActiveState") == "activating" and s.get("SubState") == "auto-restart", (
                f"after six failures the unit is not `activating (auto-restart)`: {s}")
            break
    assert timeline and timeline[-1][1] >= 6, \
        f"only {timeline[-1][1] if timeline else 0} restarts observed; six were required"

    gaps = [b[0] - a[0] for a, b in zip(timeline, timeline[1:])]
    assert len(gaps) >= 5, timeline
    assert len([g for g in gaps if g > RESTART_SEC]) >= 2, \
        f"the gap between later restarts never exceeded RestartSec: {gaps}"
    assert max(gaps) > 2 * RESTART_SEC, f"the backoff is a directive only; gaps={gaps}"

    # No residue: the unit ends itself once the stub hits its ceiling, and --collect removes it.
    ended = [e for e, _nr, load, _a, _s in _watch(env, name, 90.0) if load == "not-found"]
    assert ended, "the transient unit never ended: it would loop forever and leak (see the docstring)"


# ---------------------------------------------------------------------------
# C2 CONTROL -- the OLD policy caps the restarts and ends
# ---------------------------------------------------------------------------

def test_c2_control_the_old_five_in_three_hundred_policy_caps_and_ends(tmp_path):
    """The CONTROL: with StartLimitIntervalSec=300 + StartLimitBurst=5 (the policy this unit
    removes) the restarts CAP and the unit ends -- exactly the bug, whose symptom under
    `--collect` is the unit going away instead of restarting forever. The fix cell above reaches
    6+ restarts and stays; this one must stop at CONTROL_CAP and vanish. Without this cell the fix
    cell cannot distinguish the fix from the defect."""
    env = _live_run_or_skip()  # raises the NAMED skip unless the live run is opted in
    name = _unit_name("control")
    properties = [
        "Restart=on-failure",
        f"RestartSec={RESTART_SEC_ARG}",
        f"StartLimitIntervalSec={OLD_INTERVAL}",
        f"StartLimitBurst={OLD_BURST}",
    ]
    started = _run_unit(env, name, _self_terminating_stub(tmp_path, STUB_RUNS), properties)
    assert started.returncode == 0, (started.stdout, started.stderr)

    max_nr = 0
    ended = False
    for _elapsed, nr, load, active, _subst in _watch(env, name, 90.0):
        if load == "not-found":
            ended = True
            break
        max_nr = max(max_nr, nr)
        assert nr <= CONTROL_CAP, (
            f"the OLD policy ({OLD_BURST} starts in {OLD_INTERVAL} s) did not cap the restarts: "
            f"NRestarts reached {nr} (the start limit did not fire). The control cannot "
            f"distinguish the fix from the defect if it never stops the unit.")
        if active == "failed":
            ended = True
            break
    assert max_nr == CONTROL_CAP, \
        f"the burst did not cap at {CONTROL_CAP} (max NRestarts={max_nr}); the control did not " \
        f"exercise the start limit"
    assert ended, "the old policy left the unit running instead of ending it"
