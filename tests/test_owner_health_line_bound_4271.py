"""Contract tests for the session-start owner health line (#4271, part of #2652).

The line said `reason: timeout` but not WHICH timeout. Each owner read gets
min(per_call_timeout, what remains of total_budget), and the calibration read runs late
in the fire, so the real bound can be far below the 1 s per-call figure. The typed
`Unavailable` already carries it as `.bound`; the line never printed it.

  H1  `_record_owner_failure` keeps `elapsed_ms`: the fire's owner time when the FIRST
      failure was recorded (None when the fire has no start time).
  H2  `_owner_failure_text` adds, inside the parenthetical after the reason,
      `, bound N ms` (only when the result has a numeric `bound`) and then
      `; failed E ms into the B ms fire budget` (only when elapsed is known), before
      the `; waited ...` clause.
  H3  the new text sits before the block list and the remedy, so the 480 cut keeps it.
  H4  the stderr line carries the same two fields, under its [:300] cut.

Sibling hooks (surface, lesson-tripwire): see the PR body; neither keeps a fire start time
(`run["t0"]`), so the H5 condition for applying H1-H4 to them is not met and they are unchanged.
Every docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import pytest  # noqa: E402

import engramd_client as ec  # noqa: E402

from test_session_start_owner_read_3735 import Fire, load_ss  # noqa: E402,F401
from test_tripwire_owner_read_3665 import home  # noqa: E402,F401 -- fixture

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

SIX = ["_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
       "saved_focus_sets_block", "session_checkpoint_block", "sleep_status_block"]


def _unavailable(bound=0.37, reason="timeout"):
    return ec.Unavailable(socket_path="/x/sock", bound=bound, agent_steps=["a"],
                          human_steps=["h"], reason=reason)


def _fail(mod, run, site, kind, result, now, t0=1000.0):
    """Record one owner failure with the hook's clock at `now` and the fire started at `t0`."""
    if t0 is not None:
        run["t0"] = t0
    mod._mono = lambda: now
    mod._record_owner_failure(site, ec.OwnerReadError(kind, result))


def test_t10_the_line_prints_the_bound_and_the_fire_elapsed(home, monkeypatch):
    """The calibration timeout of 2026-10-29: bound 370 ms, failed 2630 ms into the 3000 ms
    budget. Mutation: drop the `, bound N ms` clause -> red; drop the `; failed ...` clause -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.37), now=1002.63)
        assert run["failure"]["elapsed_ms"] == 2630
        line = mod._owner_failure_text(run)
    assert "(unavailable, reason: timeout, bound 370 ms; failed 2630 ms into the 3000 ms fire budget)" in line, line


def test_t10b_the_budget_in_the_line_is_the_hooks_own_constant(home, monkeypatch):
    """Mutation: hard-code 3000 instead of OWNER_FIRE_BUDGET_S -> red."""
    mod = load_ss(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 7.0)
    with Fire(mod) as run:
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.37), now=1002.0)
        assert "into the 7000 ms fire budget" in mod._owner_failure_text(run)


def test_t10c_the_elapsed_is_taken_at_the_first_failure_not_the_last(home, monkeypatch):
    """Mutation: overwrite elapsed on every failure -> the later 2900 ms shows -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        _fail(mod, run, "focus_block", "unavailable", _unavailable(1.0), now=1000.5)
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.1), now=1002.9)
        line = mod._owner_failure_text(run)
    assert "failed 500 ms into" in line and "bound 1000 ms" in line, line


def test_t11_no_bound_text_without_a_bound_but_the_elapsed_stays(home, monkeypatch):
    """client_import, budget_exhausted and a Rejected carry no `.bound`. Mutation: print
    `bound None ms` / raise on a missing attribute -> red."""
    mod = load_ss(home, monkeypatch)
    rej = ec.Rejected(reason="unknown_operation", detail="d", remedy="r")
    for kind, result in (("rejected", rej), ("budget_exhausted", None), ("unusable_answer", None)):
        with Fire(mod) as run:
            _fail(mod, run, "calibration", kind, result, now=1001.0)
            line = mod._owner_failure_text(run)
        assert "bound" not in line, line
        assert "failed 1000 ms into the 3000 ms fire budget" in line, line
    with Fire(mod) as run:
        mod._mono = lambda: 1001.0
        run["t0"] = 1000.0
        mod._record_owner_failure("focus_block", mod._ClientImportFailure("ImportError"))
        line = mod._owner_failure_text(run)
    assert "bound" not in line and "failed 1000 ms" in line, line


def test_t11b_a_non_numeric_bound_is_ignored_and_never_raises(home, monkeypatch):
    """Mutation: format the bound without a numeric guard -> TypeError / 'bound x ms' -> red."""
    mod = load_ss(home, monkeypatch)
    for odd in ("0.37", None, True, [1], float("nan")):
        with Fire(mod) as run:
            run["t0"] = 1000.0
            run["failure"] = {"site": "calibration", "kind": "unavailable", "cause": None,
                              "result": type("R", (), {"reason": "timeout", "bound": odd})(),
                              "elapsed_ms": 10}
            run["degraded"] = ["calibration"]
            line = mod._owner_failure_text(run)
        assert "bound" not in line and "failed 10 ms" in line, (odd, line)


def test_t11c_an_unset_t0_gives_no_elapsed_text_and_no_exception(home, monkeypatch):
    """Mutation: compute elapsed from a None t0 (TypeError) or print `None ms` -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        assert run["t0"] is None
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.37), now=1002.63, t0=None)
        assert run["failure"]["elapsed_ms"] is None
        line = mod._owner_failure_text(run)
    assert "bound 370 ms" in line and "fire budget" not in line and "None" not in line, line


def test_t11d_a_failure_dict_without_elapsed_still_renders(home, monkeypatch):
    """A failure recorded by older code (no `elapsed_ms` key). Mutation: index the key -> KeyError -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        run["failure"] = {"site": "calibration", "kind": "unavailable", "result": _unavailable(0.37),
                          "cause": None}
        run["degraded"] = ["calibration"]
        line = mod._owner_failure_text(run)
    assert "bound 370 ms" in line and "fire budget" not in line


def test_the_new_text_comes_before_the_waited_clause(home, monkeypatch):
    """Mutation: append the new text after `; waited ...` -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.37, "down"), now=1000.5)
        run["wait"] = {"ms": 250, "outcome": "still_down"}
        line = mod._owner_failure_text(run)
    assert line.index("bound 370 ms") < line.index("fire budget)") < line.index("; waited 250 ms")


def test_t12_worst_case_long_site_and_block_list_keeps_the_cap_and_the_new_text(home, monkeypatch):
    """Mutation: place the new text after the block list -> the 480 cut removes it -> red."""
    mod = load_ss(home, monkeypatch)
    with Fire(mod) as run:
        _fail(mod, run, "s" * 100, "k" * 100, _unavailable(0.37, "r" * 100), now=1002.63)
        run["failure"]["kind"] = "k" * 100
        run["wait"] = {"ms": 10 ** 9, "outcome": "still_down"}
        run["degraded"] = SIX
        line = mod._owner_failure_text(run)
    assert len(line) == mod._OWNER_NOTICE_CAP == 480
    assert "bound 370 ms" in line and "failed 2630 ms into the 3000 ms fire budget" in line, line


def test_t13_the_stderr_line_carries_both_fields_under_its_cut(home, monkeypatch):
    """Mutation: leave the stderr line unchanged -> red; drop the [:300] cut -> the length check is red."""
    mod = load_ss(home, monkeypatch)
    err = io.StringIO()
    with Fire(mod) as run:
        _fail(mod, run, "calibration", "unavailable", _unavailable(0.37), now=1002.63)
        with patch("sys.stderr", err):
            mod._write_owner_stderr_lines()
    out = err.getvalue()
    assert "owner read failed: unavailable at calibration" in out, out
    assert "bound 370 ms" in out and "failed 2630 ms into the 3000 ms fire budget" in out, out
    assert len(out.splitlines()[-1]) <= 300


def test_t13b_the_stderr_line_without_a_bound_or_t0_has_neither_field(home, monkeypatch):
    """The existing stderr shape is unchanged when there is nothing to add."""
    mod = load_ss(home, monkeypatch)
    err = io.StringIO()
    with Fire(mod) as run:
        _fail(mod, run, "calibration", "budget_exhausted", None, now=1002.63, t0=None)
        with patch("sys.stderr", err):
            mod._write_owner_stderr_lines()
    assert err.getvalue() == "engram-session-start-hook: owner read failed: budget_exhausted at calibration\n"
