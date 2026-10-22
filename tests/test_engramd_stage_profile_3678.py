"""Contract tests for tools/engramd_stage_profile.py, the U7 stage profiler for
the engramd owner round trip (#3678, part of #2652).

Written BEFORE the tool (contract-first, docs/internal/CONTRACT-FIRST-TESTS.md). The
profiler ATTRIBUTES time across the stages of one `engramd_client.read`; it does
not set the level. The level -- each cut's before/after and the target -- is
judged by the UNWRAPPED A6 run (tools/engramd_a6_latency.py). Borges's design
seat ruling on #3678.

The contract:

  * the owner side is the REAL owner: a separate process whose command line
    carries `engramd_owner` (#3285), with timers wrapped around the owner's own
    callables by a launcher -- no profile mode exists in the owner;
  * every named stage reports p50 and p99 over N calls;
  * DISCRIMINATION: a delay injected into ONE named stage is attributed to that
    stage and to no other (one case per injectable stage, client and owner);
  * the stages plus an explicit `unattributed` row account for the
    independently timed end-to-end call -- the remainder is reported, never
    spread across stages or hidden;
  * the wrapped end-to-end is reported beside an UNWRAPPED end-to-end from the
    same run, so the instrument's own overhead is a visible number;
  * the profiled owner answers the same rows as a direct read of the store;
  * it never touches the live store.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from _owner_platform import requires_linux_kernel, requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_stage_profile.py"

# Injected delay and the bounds it is judged by. 2 ms stands well above the
# per-stage p50 noise measured on a WSL2 seat (sub-0.1 ms for every stage that
# is not the store read), so a wrong attribution cannot hide inside jitter.
INJECT_S = 0.002
INJECTED_RISE_MIN_MS = 1.6     # the named stage must rise by at least this much
OTHER_STAGE_RISE_MAX_MS = 0.4  # every other stage must rise by less than this


def _load_tool():
    spec = importlib.util.spec_from_file_location("engramd_stage_profile", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Loaded at collection time so the discrimination test is parametrized by stage
# NAME (a readable test id per stage, and no empty slots).
_TOOL = _load_tool() if TOOL_PATH.is_file() else None
_INJECTABLE = list(_TOOL.INJECTABLE_STAGES) if _TOOL else ["<tool missing>"]


@pytest.fixture(autouse=True, scope="module")
def _restore_sys_path():
    """#2923: tests here run the stop hook in-process (through the tool's timing
    wrappers), and the hook's `_import_engramd_client` moves src/engram to the
    front of sys.path -- right in the hook's own process, a leak in this shared
    one. Put sys.path back when the module ends (a plain restore, not
    monkeypatch.syspath_prepend, which only undoes its own prepend). Module
    scope, not per test: the module-scoped `plain` / `store` fixtures run the
    hook too, and a function-scoped snapshot is taken only AFTER them."""
    saved = list(sys.path)
    yield
    sys.path[:] = saved


@pytest.fixture(scope="module")
def tool():
    if _TOOL is None:
        pytest.fail(f"{TOOL_PATH} does not exist")
    return _TOOL


@pytest.fixture(scope="module")
def store(tool):
    return tool.make_test_store(nodes=500)


@pytest.fixture(scope="module")
def plain(tool, store):
    """One uninjected profile, shared by the shape tests and as the baseline
    the injection tests compare against."""
    return tool.profile(n=60, home=store, warmup=10)


# --- the real owner -------------------------------------------------------------

@requires_linux_owner
def test_the_profiled_owner_is_a_separate_process_carrying_the_owner_marker(tool, store):
    """#3285 obligation, read from /proc rather than from the argv we built.
    Mutation: serve the owner in-process, or launch the launcher without the
    owner module's path on its command line -> red."""
    handle = tool.launch_profiled_owner(store)
    try:
        assert handle.proc.pid != os.getpid()
        if Path("/proc").is_dir():
            cmdline = Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes()
        else:  # no /proc (macOS): the process table's own command line, same fact (#3666)
            import subprocess as _sp
            cmdline = _sp.run(["ps", "-o", "command=", "-p", str(handle.proc.pid)],
                              capture_output=True, check=True).stdout
        assert b"engramd_owner" in cmdline
    finally:
        handle.stop()
    assert handle.proc.poll() is not None


@requires_linux_owner
def test_the_owner_module_carries_no_profile_mode(tool):
    """The profiler wraps the owner from outside; the shipped owner gains
    nothing. Mutation: add a profile/timing switch to engramd_owner.py -> red."""
    src = (REPO_ROOT / "src" / "engram" / "engramd_owner.py").read_text(encoding="utf-8")
    for needle in ("stage_profile", "STAGE_PROFILE", "engramd_stage_profile"):
        assert needle not in src


@requires_linux_owner
def test_the_profiled_owner_answers_the_same_rows_as_a_direct_read(plain):
    """Mutation: the launcher serves a stub, or the profiler reads a different
    store than it reports -> red."""
    assert plain["rows_equal"] is True


# --- shape ------------------------------------------------------------------------

@requires_linux_owner
def test_every_named_stage_reports_both_percentiles(tool, plain):
    """Mutation: drop any stage from the result, or omit p99 -> red."""
    assert set(plain["stages"]) == set(tool.STAGES)
    for name in tool.STAGES:
        row = plain["stages"][name]
        assert row["n"] >= 60, name
        assert 0 <= row["p50_ms"] <= row["p99_ms"], name
        assert row["side"] in ("client", "owner", "between"), name


@requires_linux_owner
def test_stage_sides_partition_the_names(tool):
    """Every stage belongs to exactly one side. Mutation: list a stage under
    two sides, or leave one off every side -> red."""
    sides = (tool.CLIENT_STAGES, tool.OWNER_STAGES, tool.BETWEEN_STAGES)
    seen = [s for side in sides for s in side]
    assert len(seen) == len(set(seen))
    assert set(seen) == set(tool.STAGES)


# --- the remainder ------------------------------------------------------------------

@requires_linux_owner
def test_the_unattributed_remainder_is_reported_not_hidden(plain):
    """Control: with no injection the remainder is its own row, a real sample
    (n >= 60) whose p50 is not below the clock-noise floor (a small negative
    is noise; see UNATTRIBUTED_NEG_FLOOR_MS). Mutation: omit the row, or fold
    it into a stage -> red."""
    assert _remainder_row_ok(plain["unattributed"]), plain["unattributed"]


def _row(p50, n=60):
    return {"n": n, "p50_ms": p50}


def test_the_remainder_row_tolerates_clock_noise_below_zero():
    """Contract (#3784): a p50 of -0.04 ms is clock-independence noise (the CI
    runner read -0.0116 and -0.0083). Mutation: the old `>= 0` -> red."""
    assert _remainder_row_ok(_row(-0.04))
    assert _remainder_row_ok(_row(0.0))
    assert _remainder_row_ok(_row(0.02))


def test_the_remainder_row_still_refuses_real_over_attribution():
    """-0.06 ms is past the floor. Mutation: drop the floor / widen it -> red."""
    assert not _remainder_row_ok(_row(-0.06))
    assert not _remainder_row_ok(_row(-0.2))


def test_the_remainder_row_needs_a_real_sample():
    """A row with n < 60 is not a measured remainder. Mutation: drop the n
    check -> red."""
    assert not _remainder_row_ok(_row(0.02, n=59))
    assert not _remainder_row_ok(_row(0.02, n=0))


def test_the_negative_floor_is_point_zero_five_ms():
    """The literal, not the constant compared with itself."""
    assert UNATTRIBUTED_NEG_FLOOR_MS == 0.05


# Accounting is judged on the BEST of up to this many independent profiles
# (#3721, Borges's design-seat ruling). Unattributed time from a real regression
# is persistent and shows in every profile; contention from a long suite run is
# transient and inflates one. A single profile missed the 10% bound once in a
# 1h36m nightly (rest/e2e 0.116) and never in 30 standalone profiles (quiet
# worst 0.076), so the bound stays and the sample changes.
ACCOUNTING_PROFILES = 3

# The remainder's sign is clock noise near zero (#3784, Borges's ruling): it is the
# end-to-end call minus stages timed on independent clocks, so a small negative
# p50 is not over-attribution. The contract is that the row is REPORTED, never
# hidden or folded into a stage. Measured 2026-10-14: the CI runner read -0.0116
# and -0.0083 ms; Borges's Linux seat read +0.018 to +0.025 ms (n=20 per
# commit). The floor is about 4x the worst CI reading; over-attribution larger
# than it stays red.
UNATTRIBUTED_NEG_FLOOR_MS = 0.05


def _remainder_row_ok(row):
    """The reported remainder row: a real sample, p50 not below the noise floor."""
    return row["n"] >= 60 and row["p50_ms"] >= -UNATTRIBUTED_NEG_FLOOR_MS


def _accounted(profile):
    """True when this profile's unattributed p50 is within the bound."""
    e2e = profile["end_to_end"]["p50_ms"]
    rest = profile["unattributed"]["p50_ms"]
    return rest <= max(0.05, 0.10 * e2e)


def _stages_account(profiles):
    """Best of the given profiles: the stages account for the call if ANY one
    profile is within the bound. Returns (ok, [(rest, e2e), ...])."""
    seen = [(p["unattributed"]["p50_ms"], p["end_to_end"]["p50_ms"]) for p in profiles]
    return any(_accounted(p) for p in profiles), seen


def _judge_and_report(profiles, write):
    """`_stages_account(profiles)`, plus ONE line through `write` carrying every
    judged (rest, e2e) pair, on a pass as well as a fail (#3784), so a nightly
    log shows how close a passing run came."""
    ok, seen = _stages_account(profiles)
    pairs = ", ".join(f"rest={rest:g} e2e={e2e:g}" for rest, e2e in seen)
    write(f"stage accounting {'ok' if ok else 'FAILED'}: {pairs}")
    return ok, seen


def _best_of(first, take_next, cap=ACCOUNTING_PROFILES):
    """Profiles to judge: `first`, then further ones from `take_next()` only
    while none so far is within the bound, at most `cap` in all. Stopping at
    the first pass gives the same verdict as taking all `cap`. The draws are
    back to back (no spacing; #3784 re-ruled it out)."""
    profiles = [first]
    while not _stages_account(profiles)[0] and len(profiles) < cap:
        profiles.append(take_next())
    return profiles


def _synthetic(rest, e2e):
    return {"unattributed": {"p50_ms": rest}, "end_to_end": {"p50_ms": e2e}}


# The decision rule, pinned without a live owner (cells written before the
# rule; Borges's ruling on #3721). e2e = 1.0 ms, so the bound is 0.10 ms.

def test_accounting_passes_on_three_quiet_profiles():
    """Cell 1, today's behaviour. Mutation: require every profile to be
    over the bound, or invert the comparison -> red."""
    ok, _ = _stages_account([_synthetic(0.07, 1.0)] * 3)
    assert ok


def test_accounting_passes_when_one_profile_is_inflated():
    """Cell 2, the nightly shape: one transient spike at ratio 0.15 beside two
    quiet profiles. Mutation: judge on the worst profile (all() instead of
    any()), or on a single profile that happens to be the inflated one -> red."""
    ok, _ = _stages_account([_synthetic(0.15, 1.0), _synthetic(0.07, 1.0), _synthetic(0.07, 1.0)])
    assert ok


def test_accounting_fails_on_persistent_unattributed_time():
    """Cell 3, the discriminating one: a stage that stopped being timed leaks
    into the remainder in EVERY profile. Mutation: widen the bound to 0.20, or
    pass unconditionally -> red, while cells 1 and 2 stay green."""
    ok, seen = _stages_account([_synthetic(0.15, 1.0), _synthetic(0.16, 1.0), _synthetic(0.15, 1.0)])
    assert not ok, seen


def test_accounting_passes_on_a_negative_remainder():
    """Cell 4: under load the stage sum can exceed end-to-end (Sol measured a
    negative remainder on #3721); that is not unattributed time. Mutation:
    compare the absolute remainder -> red."""
    ok, _ = _stages_account([_synthetic(-0.2, 1.0)] * 3)
    assert ok


# The retry that removes the transient miss, pinned with a fake producer (the
# reviewer-fairy's round-1 finding: the decision cells above stayed green with
# the retry removed).

def _producer(*profiles):
    draws = iter(profiles)
    calls = []

    def take_next():
        calls.append(1)
        return next(draws)
    return take_next, calls


def test_best_of_retries_past_an_inflated_first_profile():
    """The nightly shape end to end: the first profile is inflated, the
    second is quiet -> judged pass after exactly one more draw. Mutation:
    never retry (judge the first profile alone) -> red."""
    take_next, calls = _producer(_synthetic(0.07, 1.0), _synthetic(0.07, 1.0))
    profiles = _best_of(_synthetic(0.15, 1.0), take_next)
    assert _stages_account(profiles)[0]
    assert len(calls) == 1


def test_best_of_takes_no_extra_profile_when_the_first_passes():
    """A quiet run costs one profile, as before #3721. Mutation: always take
    `cap` profiles -> red."""
    take_next, calls = _producer(_synthetic(0.07, 1.0), _synthetic(0.07, 1.0))
    _best_of(_synthetic(0.07, 1.0), take_next)
    assert calls == []


def test_best_of_stops_at_the_cap_when_none_passes():
    """Persistent unattributed time: exactly cap - 1 extra draws, then fail.
    Mutation: cap off by one either way -> red."""
    take_next, calls = _producer(*[_synthetic(0.15, 1.0)] * 5)
    profiles = _best_of(_synthetic(0.15, 1.0), take_next)
    # Literals, not the constant compared to itself: the ruling is three
    # (Sol's colleague pass, M-c: ACCOUNTING_PROFILES = 2 stayed green).
    assert len(calls) == 2
    assert len(profiles) == 3
    assert not _stages_account(profiles)[0]


# The bound's own parameters, pinned at its edge (Sol's colleague pass: no cell
# sat at the bound, so `<` for `<=`, a 0.11 factor and a 0.0 floor all stayed
# green).

def test_accounting_bound_is_inclusive_at_ten_percent():
    """e2e 1.0 ms: rest 0.10 passes, rest 0.1001 fails. Mutation: `<` for
    `<=` -> the first red; factor 0.11 or 0.20 -> the second red."""
    assert _stages_account([_synthetic(0.10, 1.0)])[0]
    assert not _stages_account([_synthetic(0.1001, 1.0)])[0]


def test_accounting_floor_is_point_zero_five_ms_on_a_fast_call():
    """e2e 0.2 ms, where 10% is 0.02 ms, so the 0.05 ms floor decides: rest
    0.04 passes, rest 0.06 fails. Mutation: floor 0.0 -> the first red;
    floor 0.10 -> the second red."""
    assert _stages_account([_synthetic(0.04, 0.2)])[0]
    assert not _stages_account([_synthetic(0.06, 0.2)])[0]


def _live_accounting(tool, store, plain):
    """The live test's body: `plain` first, then FRESH profiles drawn from the
    real profiler on the same store. Kept apart from the test so a fake `tool`
    can pin what the retry draws (Sol's blocker, M-d: a retry that re-judges
    `plain` instead of drawing a new profile left all 52 green)."""
    return _best_of(plain, lambda: tool.profile(n=60, home=store, warmup=10))


class _FakeTool:
    def __init__(self, *profiles):
        self._draws = iter(profiles)
        self.calls = []

    def profile(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._draws)


def test_live_retry_judges_a_fresh_profile_not_plain_again():
    """Inflated `plain`, quiet fresh draw -> pass, and the judged profiles
    include the fresh one. Mutation: the retry returns `plain` again ->
    red. Also pins the retry's sample: n=60, warmup 10, same store."""
    store = object()
    fresh = _synthetic(0.07, 1.0)
    tool = _FakeTool(fresh, _synthetic(0.07, 1.0))
    plain = _synthetic(0.15, 1.0)
    profiles = _live_accounting(tool, store, plain)
    assert profiles == [plain, fresh]
    assert profiles[1] is fresh
    assert _stages_account(profiles)[0]
    assert tool.calls == [{"n": 60, "home": store, "warmup": 10}]


def test_live_retry_fails_with_every_drawn_pair_reported():
    """Inflated `plain` and inflated draws -> fail, and the message carries
    the fake's own pairs, so a failure shows what was actually judged."""
    tool = _FakeTool(_synthetic(0.16, 1.0), _synthetic(0.17, 1.0), _synthetic(0.07, 1.0))
    profiles = _live_accounting(tool, object(), _synthetic(0.15, 1.0))
    ok, seen = _stages_account(profiles)
    assert not ok
    assert seen == [(0.15, 1.0), (0.16, 1.0), (0.17, 1.0)]
    assert len(tool.calls) == 2


# Reporting (#3784). CONTRACT, written before the code: _judge_and_report writes
# exactly one line with every judged pair, on pass AND fail, and returns the same
# (ok, seen) as _stages_account.

def test_a_passing_run_reports_every_judged_pair():
    """C4: write called exactly once, the line carries each pair's values, and
    the verdict is the same as _stages_account's. Mutation: no write on a
    passing run -> red."""
    lines = []
    profiles = [_synthetic(0.15, 1.5), _synthetic(0.071, 0.987)]
    ok, seen = _judge_and_report(profiles, lines.append)
    assert (ok, seen) == _stages_account(profiles)
    assert ok
    assert len(lines) == 1
    for value in ("0.15", "1.5", "0.071", "0.987"):
        assert value in lines[0]


def test_a_failing_run_reports_every_judged_pair_once():
    """C4, failing side. Mutation: report only on pass, or once per pair ->
    red."""
    lines = []
    profiles = [_synthetic(0.15, 1.0), _synthetic(0.16, 1.0), _synthetic(0.17, 1.0)]
    ok, seen = _judge_and_report(profiles, lines.append)
    assert not ok
    assert seen == [(0.15, 1.0), (0.16, 1.0), (0.17, 1.0)]
    assert len(lines) == 1
    for value in ("0.15", "0.16", "0.17"):
        assert value in lines[0]


@pytest.mark.timing
@requires_linux_owner
def test_stages_account_for_the_end_to_end_call(tool, store, plain, request):
    """The stages tile the independently timed end-to-end call: what they do
    not cover is small. Mutation: stop timing the owner's store read (or any
    other stage of real size), so its time leaks into the remainder -> red on
    a store large enough for that read to matter; on any store, compute
    end-to-end as the stage sum instead of timing it -> red via the
    injected-remainder test below.

    Best of up to ACCOUNTING_PROFILES profiles (#3721): further profiles are
    taken only while none so far is within the bound, so a quiet run costs one
    profile as before. Stopping at the first pass gives the same verdict as
    taking all of them. What the retry draws is pinned by the fake-tool cells
    above; the retry's sample size is pinned there too, but only as the
    arguments passed, not as a measured adequacy.

    The judged pairs are written to the terminal on every run (#3784) so they
    show under `pytest -v` without `-s`."""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    write = reporter.write_line if reporter is not None else (lambda line: None)
    profiles = _live_accounting(tool, store, plain)
    ok, seen = _judge_and_report(profiles, write)
    assert ok, seen


@requires_linux_owner
def test_end_to_end_is_timed_independently_of_the_stages(tool, store):
    """A delay OUTSIDE every named stage must show up in the remainder, which
    is only possible if end-to-end is its own timer. Mutation: compute
    end_to_end as the sum of the stages -> the remainder stays ~0 -> red."""
    out = tool.profile(n=30, home=store, warmup=5, inject={tool.UNNAMED_GAP: INJECT_S})
    assert out["unattributed"]["p50_ms"] >= INJECTED_RISE_MIN_MS


# --- the instrument's own overhead --------------------------------------------------

@requires_linux_owner
def test_wrapped_and_unwrapped_end_to_end_are_both_reported(plain):
    """Borges's refinement 1: the profiler attributes; the level is the
    unwrapped run. Mutation: drop the unwrapped run, or time it against the
    wrapped owner -> red."""
    wrapped = plain["end_to_end"]
    unwrapped = plain["unwrapped_end_to_end"]
    assert unwrapped["n"] >= 60
    assert 0 < unwrapped["p50_ms"]
    assert plain["unwrapped_owner_pid"] != plain["owner_pid"]
    assert wrapped["p50_ms"] > 0


# --- discrimination -------------------------------------------------------------------

@requires_linux_owner
def test_every_client_and_owner_stage_is_injectable(tool):
    """Discrimination must be checkable on both sides. Mutation: shrink
    INJECTABLE_STAGES so a side (or any client/owner stage) has no case ->
    red. `between` stages are derived from cross-process stamps and are
    checked through the remainder test instead."""
    assert set(tool.INJECTABLE_STAGES) == set(tool.CLIENT_STAGES) | set(tool.OWNER_STAGES)


@pytest.mark.timing
@requires_linux_owner
@pytest.mark.parametrize("target", _INJECTABLE)
def test_an_injected_delay_lands_in_its_own_stage_and_no_other(tool, store, plain, target):
    """The per-stage discrimination check (contract item 1 of #3678), one case
    per injectable stage. It is also what catches a wrapper that has silently
    stopped timing anything (an aliased or pre-bound call): the injection
    rides the same wrapper, so a dead wrapper means the delay never lands.
    Mutations: attribute a wrapper's time to the wrong name; wrap a module
    attribute the owner no longer reaches; record a parent stage's inclusive
    time instead of its exclusive time -> red."""
    out = tool.profile(n=30, home=store, warmup=5, inject={target: INJECT_S})
    rise = {s: out["stages"][s]["p50_ms"] - plain["stages"][s]["p50_ms"] for s in tool.STAGES}
    assert rise[target] >= INJECTED_RISE_MIN_MS, (target, rise[target])
    leaked = {s: r for s, r in rise.items() if s != target and r >= OTHER_STAGE_RISE_MAX_MS}
    assert not leaked, (target, leaked)


# The test's OWN map from a real function to the stage it must be reported under.
# Written independently of the tool's wrapper list on purpose: a stage-named
# injection rides that stage's own wrapper, so it cannot notice a wrapper put on
# the wrong function (measured: swapping the walguard and registry_check wrappers
# left every stage-named case green). This map can.
FUNCTION_TO_STAGE = [
    ("owner", "engramd_owner:EngramdOwner._check_peer_credentials", "peer_credentials"),
    ("owner", "engramd_owner:EngramdOwner._read_request_line", "request_read"),
    ("owner", "engramd_client:check_contract_version", "version_check"),
    ("owner", "engram_walguard:assert_not_displaced", "walguard"),
    ("owner", "engramd_read_registry:Registry.check", "registry_check"),
    ("owner", "engramd_owner:_arm_read_deadline", "arm_deadline"),
    ("owner", "engramd_owner:_ClientReadGate._execute_admitted", "execute"),
    ("owner", "engramd_owner:EngramdOwner._handle_read", "read_handler"),
    # Cut 2 (serialize-once) moved a read reply's whole encoding here: the row
    # checks, the one json.dumps per row, and the assembly sent as wire_text.
    ("owner", "engramd_owner:_render_read_rows", "response_encode"),
    ("client", "engramd_client:_resolve_socket_path", "resolve_path"),
    ("client", "hook:_import_engramd_client", "client_import"),
]


@pytest.mark.timing
@requires_linux_owner
@pytest.mark.parametrize("side,function,stage", FUNCTION_TO_STAGE,
                         ids=[f"{st}<-{fn}" for _, fn, st in FUNCTION_TO_STAGE])
def test_a_delay_in_the_real_function_is_reported_under_its_stage(tool, store, plain, side, function, stage):
    """The independent half of discrimination: the delay is patched into the
    real function UNDER the wrappers. Mutation: put a stage's wrapper on the
    wrong function (e.g. swap the walguard and registry_check wrappers) -> red,
    where every stage-named injection stays green."""
    kw = {"inject_owner_fn" if side == "owner" else "inject_client_fn": {function: INJECT_S}}
    out = tool.profile(n=30, home=store, warmup=5, **kw)
    rise = {s: out["stages"][s]["p50_ms"] - plain["stages"][s]["p50_ms"] for s in tool.STAGES}
    assert rise[stage] >= INJECTED_RISE_MIN_MS, (function, stage, rise[stage])
    leaked = {s: r for s, r in rise.items() if s != stage and r >= OTHER_STAGE_RISE_MAX_MS}
    assert not leaked, (function, leaked)


def test_an_unknown_injection_name_is_refused(tool, store):
    """A typo must not silently inject nothing and report a clean profile.
    Mutation: ignore unknown names -> red."""
    with pytest.raises(ValueError):
        tool.profile(n=5, home=store, warmup=0, inject={"no_such_stage": INJECT_S})


# --- safety -----------------------------------------------------------------------------

def test_cli_refuses_the_seats_live_store(tool, tmp_path, monkeypatch, capsys):
    """Mutation: drop the live-store check -> the tool would copy-and-profile
    against (or worse, beside) the seat's own knowledge.db -> red."""
    home = tmp_path / "home"
    home.mkdir()
    live = home / "knowledge.db"
    live.write_bytes(b"")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    rc = tool.main(["--store", str(live), "--n", "1000"])
    assert rc == 2
    assert "live" in capsys.readouterr().err.lower()


def test_cli_enforces_the_n_floor(tool, monkeypatch):
    """#3678 asks for N >= 1000 per stage. Mutation: lower or drop the floor
    -> red."""
    rc = tool.main(["--n", "999"])
    assert rc == 2


# --- layout (#3678): the profiler gets A6's switch --------------------------------------

@requires_linux_owner
@requires_linux_kernel  # (a) Linux-only: the A6 tool reads /proc and calls os.posix_fadvise; porting the tool is out of scope (#3666)
def test_the_installed_layout_profile_runs_the_build_and_never_walks(tool, tmp_path_factory):
    """Borges's ruling: the profiler measures the installed layout too, under
    the same two runtime asserts as A6. Its client_import row then shows what
    production pays. Mutation: profile the installed layout in-process (the
    source _hooklib is already cached) or load the owner module from the source
    tree -> the assert refuses -> red."""
    a6 = tool._load_a6()
    layout = a6.build_installed_layout(tmp_path_factory.mktemp("profile-build") / "plugin")
    home = tool.make_test_store(nodes=300)
    out = tool.profile_in_layout(layout, n=30, home=home, warmup=5)
    assert out["parent_walk_calls"] == 0
    assert layout.root.resolve() in Path(out["owner_argv"][1]).resolve().parents
    src = tool.profile_in_layout(a6.source_layout(), n=30, home=home, warmup=5)
    assert src["parent_walk_calls"] > 0
    assert out["stages"]["client_import"]["p50_ms"] < src["stages"]["client_import"]["p50_ms"]
