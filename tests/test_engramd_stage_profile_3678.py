"""Contract tests for tools/engramd_stage_profile.py, the U7 stage profiler for
the engramd owner round trip (#3678, part of #2652).

Written BEFORE the tool (contract-first, docs/CONTRACT-FIRST-TESTS.md). The
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

from _owner_platform import requires_linux_owner

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
    if not Path("/proc").is_dir():
        pytest.skip("needs /proc")
    handle = tool.launch_profiled_owner(store)
    try:
        assert handle.proc.pid != os.getpid()
        cmdline = Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes()
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
    """Control: with no injection the remainder is its own row. Mutation:
    omit it, or fold it into a stage -> red."""
    row = plain["unattributed"]
    assert row["n"] >= 60
    assert row["p50_ms"] >= 0


@requires_linux_owner
def test_stages_account_for_the_end_to_end_call(plain):
    """The stages tile the independently timed end-to-end call: what they do
    not cover is small. Mutation: stop timing the owner's store read (or any
    other stage of real size), so its time leaks into the remainder -> red on
    a store large enough for that read to matter; on any store, compute
    end-to-end as the stage sum instead of timing it -> red via the
    injected-remainder test below."""
    e2e = plain["end_to_end"]["p50_ms"]
    rest = plain["unattributed"]["p50_ms"]
    assert rest <= max(0.05, 0.10 * e2e), (rest, e2e)


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
