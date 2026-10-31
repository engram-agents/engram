"""Contract tests for `tools/engramd_lockwait_report.py --gate` (A4 of the metricgate adoption PR). Written BEFORE
the implementation. The suffix `metricgate` stands in for an issue number (none was given at dispatch).

The contract (spec A4): with `--gate`, for each day and family, and for the window total, the report prints
beside the existing counts a Wilson 90 percent interval on waited_reads/reads and the B2 verdict:

  * `PASS` when the interval's UPPER bound is <= 0.01 AND timed_out == 0;
  * otherwise `NOT MET (upper bound x.xx% > 1%)` or `NOT MET (timed_out = k)`.

The Wilson interval is implemented locally (no metricgate dependency; it mirrors metricgate's `rate_ci`) and is
checked here against scipy's `binomtest(...).proportion_ci(method="wilson")` under importorskip("scipy").
Without `--gate` the output is unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_lockwait_report.py"
EDGE_KEYS = ["1", "2", "5", "10", "20", "50", "100", "200", "500", "1000", "2000", "inf"]


def _load():
    spec = importlib.util.spec_from_file_location("engramd_lockwait_report_gate_cells", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fam(reads, waited, timed_out=0):
    hist = {k: 0 for k in EDGE_KEYS}
    if waited:
        hist["5"] = waited
    return {"reads": reads, "waited_reads": waited, "wait_ms_sum": 1.0 * waited, "timed_out": timed_out,
            "no_time_before_acquire": 0, "wait_hist": hist, "wait_ms_p95": 5 if waited else None}


def _day(dirpath: Path, day: str, families: dict) -> None:
    (dirpath / f"lockwait-{day}.json").write_text(json.dumps({"schema": 1, "date": day, "families": families}))


def _run(tmp_path, capsys, *flags, days=3):
    mod = _load()
    rc = mod.main(["--days", str(days), "--run-dir", str(tmp_path), *flags])
    return rc, capsys.readouterr().out


def _b2_lines(out, family="tripwire"):
    return [l for l in out.splitlines() if l.startswith("B2 ") and f" {family}:" in l]


# --- the Wilson interval -------------------------------------------------------------------------------------

@pytest.mark.parametrize("k,n", [(0, 300), (4, 7704), (5, 300), (1, 7), (7, 7), (0, 1), (150, 300), (2, 600)])
def test_wilson_matches_scipy(k, n):
    """Mutation: the normal-approximation (Wald) interval, a 95 percent z, or the continuity-corrected form ->
    differs from scipy's wilson at these cells -> red."""
    stats = pytest.importorskip("scipy.stats")
    mod = _load()
    lo, hi = mod.wilson_interval(k, n, level=0.90)
    ref = stats.binomtest(k, n).proportion_ci(confidence_level=0.90, method="wilson")
    assert lo == pytest.approx(ref.low, abs=1e-9)
    assert hi == pytest.approx(ref.high, abs=1e-9)


def test_wilson_default_level_is_ninety_percent_and_zero_reads_is_refused():
    mod = _load()
    assert mod.wilson_interval(5, 300) == mod.wilson_interval(5, 300, level=0.90)
    with pytest.raises(ValueError):
        mod.wilson_interval(0, 0)


# --- the B2 verdict ------------------------------------------------------------------------------------------

def test_b2_verdict_cells():
    """Mutation: compare the point rate instead of the upper bound, use >= for the bound, or drop the
    timed_out clause -> red."""
    mod = _load()
    assert mod.b2_verdict(0, 300, 0) == "PASS"
    assert mod.b2_verdict(4, 7704, 0) == "PASS"
    assert mod.b2_verdict(5, 300, 0) == "NOT MET (upper bound 3.38% > 1%)"
    assert mod.b2_verdict(0, 300, 1) == "NOT MET (timed_out = 1)"
    both = mod.b2_verdict(5, 300, 2)
    assert "upper bound 3.38% > 1%" in both and "timed_out = 2" in both
    assert mod.b2_verdict(0, 0, 0).startswith("NO DATA")


def test_a_point_rate_under_one_percent_with_a_wide_interval_is_not_met():
    """2/300 is a 0.67 percent point rate, under the line; its 90 percent upper bound is above 1 percent. The
    gate reads the interval. Mutation: gate on the point rate -> PASS -> red."""
    mod = _load()
    assert mod.b2_verdict(2, 300, 0).startswith("NOT MET (upper bound")


# --- the report ----------------------------------------------------------------------------------------------

def test_planted_files_pass_and_fail_as_constructed(tmp_path, capsys):
    """k=0 of 300 PASS; k=4 of 7704 PASS; k=5 of 300 NOT MET with the bound printed; timed_out 1 NOT MET
    naming timed_out. One day per case, so each day's lines are the case. Mutation: a verdict that ignores
    timed_out, or one that ignores the bound -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(300, 0)})
    _day(tmp_path, "2026-10-27", {"tripwire": _fam(7704, 4)})
    _day(tmp_path, "2026-10-28", {"tripwire": _fam(300, 5)})
    _day(tmp_path, "2026-10-29", {"tripwire": _fam(300, 0, timed_out=1)})
    rc, out = _run(tmp_path, capsys, "--gate", days=4)
    assert rc == 0
    sections = out.split("== ")
    by_day = {s[:10]: s for s in sections if s[:4] == "2026"}
    assert "-> PASS" in "".join(_b2_lines(by_day["2026-10-26"]))
    assert "-> PASS" in "".join(_b2_lines(by_day["2026-10-27"]))
    l28 = "".join(_b2_lines(by_day["2026-10-28"]))
    assert "NOT MET (upper bound 3.38% > 1%)" in l28 and "5/300" in l28
    l29 = "".join(_b2_lines(by_day["2026-10-29"]))
    assert "NOT MET (timed_out = 1)" in l29


def test_each_b2_line_carries_the_counts_and_the_interval(tmp_path, capsys):
    """Mutation: print the verdict without the interval -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(7704, 4)})
    _, out = _run(tmp_path, capsys, "--gate")
    line = _b2_lines(out)[0]
    assert "waited/reads = 4/7704" in line
    assert re.search(r"Wilson 90% \[\d+\.\d\d%, \d+\.\d\d%\]", line), line


def test_every_family_gets_a_line_not_only_the_tripwire(tmp_path, capsys):
    """Mutation: report only families whose name contains 'tripwire' -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(300, 0), "surface": _fam(300, 5), "idf+tripwire": _fam(50, 0)})
    _, out = _run(tmp_path, capsys, "--gate")
    assert _b2_lines(out, "surface") and _b2_lines(out, "idf+tripwire") and _b2_lines(out, "tripwire")


def test_the_window_total_sums_the_days(tmp_path, capsys):
    """Two days that PASS alone (0/300 and 3/7704... ) are summed: the total is its own interval. Day one 0 of
    300 and day two 5 of 300 are 5 of 600 in the window: NOT MET, whatever the first day said. Mutation:
    average the daily verdicts, or sum the waited counts but not the reads -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(300, 0)})
    _day(tmp_path, "2026-10-27", {"tripwire": _fam(300, 5)})
    _, out = _run(tmp_path, capsys, "--gate", days=2)
    window = out.split("== window total")[1]
    line = _b2_lines(window)[0]
    assert "waited/reads = 5/600" in line and "NOT MET" in line


def test_the_window_total_carries_timed_out_across_days(tmp_path, capsys):
    """Mutation: take timed_out from the last day only -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(300, 0, timed_out=2)})
    _day(tmp_path, "2026-10-27", {"tripwire": _fam(300, 0)})
    _, out = _run(tmp_path, capsys, "--gate", days=2)
    window = out.split("== window total")[1]
    assert "timed_out = 2" in "".join(_b2_lines(window))


def test_a_family_with_no_reads_says_no_data_instead_of_crashing(tmp_path, capsys):
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(0, 0), "surface": _fam(300, 0)})
    rc, out = _run(tmp_path, capsys, "--gate")
    assert rc == 0 and "NO DATA" in "".join(_b2_lines(out))


def test_without_gate_the_output_is_unchanged(tmp_path, capsys):
    """Mutation: print the interval lines without the flag -> red."""
    _day(tmp_path, "2026-10-26", {"tripwire": _fam(300, 5)})
    _, plain = _run(tmp_path, capsys)
    _, gated = _run(tmp_path, capsys, "--gate")
    assert "Wilson" not in plain and "B2 " not in plain and "window total" not in plain
    assert gated.startswith(plain.rstrip("\n")), "the gated report only ADDS lines after the existing ones"


def test_the_module_docstring_says_it_mirrors_rate_ci():
    """The docstring names the library function it mirrors, so a reader knows where the reference lives."""
    mod = _load()
    assert "rate_ci" in mod.__doc__ and "mirror" in mod.__doc__
