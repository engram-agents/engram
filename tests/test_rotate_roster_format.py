"""Contract tests for tools/rotate_roster_format.py.

Spec: the rotation helper the roster holder runs alone (format 1 -> format 2).
Cells T1-T8 pin R1-R7; two extra cells pin R2's overwrite refusal and R7's
usage-error code, which the cells below would otherwise leave to ordinary use.

Every fixture token is invented and appears nowhere else:
  ZZAlphaHolder, ZZDeltaSecret, YYBravoHandle, Lin Example.

R1's one permitted exception is a class-B NAME (the caller's own input of
permitted names). T6 therefore uses a class-B line that declares ``(?i)`` and
whose case differs from its B entry (``(?i)\\bYYBRAVOHANDLE\\b`` vs
``yybravohandle``) so the class-B name printed in the audit is a DIFFERENT
literal string from the pattern text -- that lets the cell assert every
pattern's distinctive token is absent from stdout AND stderr without
contradicting the exception.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "rotate_roster_format.py"
SCAN_LEAKS = REPO_ROOT / "src" / "engram" / "tools" / "scan-leaks.py"

# Invented fixtures only -- never a real name or a real roster pattern.
A_ONE = "ZZAlphaHolder"
A_TWO = "ZZDeltaSecret"
B_ONE = "YYBravoHandle"
B_MULTI = "Lin Example"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(**overrides):
    """The test process environment with ENGRAM_SCANNER removed, plus overrides."""
    e = dict(os.environ)
    e.pop("ENGRAM_SCANNER", None)
    e.update(overrides)
    return e


def _run(args, *, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd, env=env if env is not None else _env(),
    )


def _write_lines(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _scratch(tmp_path: Path, roster_lines, b_lines, *, out_name="out"):
    """Lay out inputs/ and out/ under tmp_path and return (roster, bnames, out)."""
    inputs = tmp_path / "inputs"
    out = tmp_path / out_name
    out.mkdir(parents=True)
    roster = _write_lines(inputs / "roster.txt", roster_lines)
    bnames = _write_lines(inputs / "bnames.txt", b_lines)
    return roster, bnames, out


def _has_line(name: str, count: int) -> str:
    """A single text line ``line <count>: <name>`` for substring assertion."""
    return f"line {count}: {name}"


# The base fixture: two class-A lines and one class-B line.
BASE_ROSTER = [
    r"\b" + A_ONE + r"\b",
    r"\b" + A_TWO + r"\b",
    r"\b" + B_ONE + r"\b",
]
BASE_B = [B_ONE]


# ---------------------------------------------------------------------------
# T1 -- the conversion, the counts, and the 0600 mode (R2, R6.1, R6.2, R6.5)
# ---------------------------------------------------------------------------

def test_t1_converts_two_a_one_b_and_creates_a_0600_file(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)

    produced = out / "roster-format2.txt"
    assert produced.is_file()
    assert stat.S_IMODE(produced.stat().st_mode) == 0o600

    lines = produced.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "A\t" + r"\b" + A_ONE + r"\b",
        "A\t" + r"\b" + A_TWO + r"\b",
        "B\t" + r"\b" + B_ONE + r"\b",
    ]

    # R6.1 line counts.
    assert re.search(r"^total_input_lines: 3$", r.stdout, re.M), r.stdout
    assert re.search(r"^blank_or_comment_skipped: 0$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_a: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 1$", r.stdout, re.M), r.stdout
    # R6.2 every class-B name with its input line number.
    assert _has_line(B_ONE, 3) in r.stdout, r.stdout
    # R6.5 the attestation shape.
    assert re.search(r"^roster_format: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^pattern_count_a: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^pattern_count_b: 1$", r.stdout, re.M), r.stdout
    # R5 says which scanner it used.
    assert re.search(r"^scanner: .+scan-leaks\.py$", r.stdout, re.M), r.stdout


def test_t1b_blank_and_comment_lines_are_skipped_and_counted(tmp_path):
    roster, bnames, out = _scratch(
        tmp_path,
        ["# a comment", "", BASE_ROSTER[0], "   ", BASE_ROSTER[1], BASE_ROSTER[2]],
        BASE_B,
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^total_input_lines: 6$", r.stdout, re.M), r.stdout
    assert re.search(r"^blank_or_comment_skipped: 3$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_a: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 1$", r.stdout, re.M), r.stdout
    # The emitted file carries only the three real lines, in input order.
    lines = (out / "roster-format2.txt").read_text(encoding="utf-8").splitlines()
    assert [ln.split("\t", 1)[0] for ln in lines] == ["A", "A", "B"]


# ---------------------------------------------------------------------------
# T2 -- the sha256 is over the canonical text (R6.4)
# ---------------------------------------------------------------------------

def test_t2_sha256_is_over_the_canonical_file_text(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)

    data = (out / "roster-format2.txt").read_bytes()
    # Independent computation: ALL trailing newlines stripped, ONE appended.
    expected = hashlib.sha256(data.rstrip(b"\n") + b"\n").hexdigest()
    m = re.search(r"^roster_sha256: ([0-9a-f]{64})$", r.stdout, re.M)
    assert m, r.stdout
    assert m.group(1) == expected


# ---------------------------------------------------------------------------
# T3 -- an already-class-prefixed roster is refused (R3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["A\t", "B\t"])
def test_t3_already_class_prefixed_refuses_by_line_and_writes_nothing(tmp_path, prefix):
    roster, bnames, out = _scratch(
        tmp_path,
        ["# leading comment", "  " + prefix + r"\b" + A_ONE + r"\b"],
        BASE_B,
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "line 2" in r.stderr, r.stderr
    assert list(out.iterdir()) == []          # no output file, no temp left over
    combined = r.stdout + r.stderr
    assert A_ONE not in combined, combined    # R1: never echo the pattern


# ---------------------------------------------------------------------------
# T4 -- the unmatched-B report CAN fire (R4's control, R6.3)
# ---------------------------------------------------------------------------

def test_t4_unmatched_b_entries_are_all_a_and_reported(tmp_path):
    # Both B entries key-mismatch deliberately:
    #  - a SCOPED (?i:...) inside the pattern is not a leading (?i), so the key
    #    keeps it;
    #  - a multi-word name written as a regex (\s+) is not the plain name.
    roster_lines = [
        r"\bZZ(?i:" + A_ONE[2:] + r")\b",
        r"\bLin\s+Example\b",
    ]
    b_names = [A_ONE, B_MULTI]
    roster, bnames, out = _scratch(tmp_path, roster_lines, b_names)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)

    assert re.search(r"^class_a: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 0$", r.stdout, re.M), r.stdout
    assert re.search(r"^b_names_with_no_match: 2 of 2$", r.stdout, re.M), r.stdout
    assert f"unmatched_b_name: {A_ONE}" in r.stdout, r.stdout
    assert f"unmatched_b_name: {B_MULTI}" in r.stdout, r.stdout

    lines = (out / "roster-format2.txt").read_text(encoding="utf-8").splitlines()
    assert all(ln.startswith("A\t") for ln in lines), lines


# ---------------------------------------------------------------------------
# T5 -- the produced roster parses under the REAL loader (R5)
# ---------------------------------------------------------------------------

def _import_by_path(name, path):
    """Import a module by explicit file path without writing a bytecode cache."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


def _load_scanner():
    return _import_by_path("scan_leaks_for_rotation", SCAN_LEAKS)


def test_t5_produced_roster_parses_under_the_real_loader(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)

    scanner = _load_scanner()
    parsed = scanner.load_roster_file(out / "roster-format2.txt")
    assert [klass for _compiled, _label, klass in parsed] == ["A", "A", "B"]
    assert parsed[0][0].search(A_ONE)
    assert parsed[1][0].search(A_TWO)
    assert parsed[2][0].search(B_ONE)


# ---------------------------------------------------------------------------
# T6 -- R1: no roster pattern text on stdout or stderr
# ---------------------------------------------------------------------------

def test_t6_no_roster_pattern_text_reaches_stdout_or_stderr(tmp_path):
    # The class-B line declares (?i) and is case-distinct from its B entry, so
    # the printed class-B name (yybravohandle) is not the pattern's literal
    # text; that lets this cell assert every pattern token, including the
    # class-B line's, is absent without contradicting R1's class-B exception.
    roster_lines = [
        r"\b" + A_ONE + r"\b",
        r"\b" + A_TWO + r"\b",
        r"(?i)\b" + B_ONE.upper() + r"\b",
    ]
    roster, bnames, out = _scratch(tmp_path, roster_lines, [B_ONE.lower()])

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr
    assert r.returncode == 0, (r.stdout, r.stderr)

    # Control: the run really did produce a summary, so "nothing found in an
    # empty stream" cannot be what passes this cell.
    assert "total_input_lines: 3" in combined, combined
    assert B_ONE.lower() in combined, combined   # the permitted class-B name

    for token in (A_ONE, A_TWO, B_ONE.upper()):
        assert token not in combined, (token, combined)
    # The full emitted pattern lines never appear either.
    for line in roster_lines:
        assert line not in combined, (line, combined)


# ---------------------------------------------------------------------------
# T7 -- --out-dir is honoured and nothing else is written anywhere (R2)
# ---------------------------------------------------------------------------

def test_t7_nothing_is_written_outside_out_dir(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    cwd = tmp_path / "cwd"; cwd.mkdir()
    runner_temp = tmp_path / "runner-temp"; runner_temp.mkdir()

    parent_before = sorted(p.name for p in tmp_path.iterdir())
    pycache = REPO_ROOT / "src" / "engram" / "tools" / "__pycache__"
    pycache_before = sorted(p.name for p in pycache.iterdir()) if pycache.is_dir() else None
    tool_pycache = REPO_ROOT / "tools" / "__pycache__"
    tool_pycache_before = (
        sorted(p.name for p in tool_pycache.iterdir()) if tool_pycache.is_dir() else None
    )

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)],
             cwd=cwd, env=_env(RUNNER_TEMP=str(runner_temp)))
    assert r.returncode == 0, (r.stdout, r.stderr)

    assert sorted(p.name for p in tmp_path.iterdir()) == parent_before
    assert sorted(p.name for p in out.iterdir()) == ["roster-format2.txt"]
    assert list(cwd.iterdir()) == []
    assert list(runner_temp.iterdir()) == []

    pycache_after = sorted(p.name for p in pycache.iterdir()) if pycache.is_dir() else None
    assert pycache_after == pycache_before, (
        "the run wrote a bytecode cache beside the scanner (R2)")
    tool_pycache_after = (
        sorted(p.name for p in tool_pycache.iterdir()) if tool_pycache.is_dir() else None
    )
    assert tool_pycache_after == tool_pycache_before, (
        "the run wrote a bytecode cache beside the tool (R2)")


# ---------------------------------------------------------------------------
# T8 -- a scanner that cannot be found is exit 1 and writes nothing (R5, R7)
# ---------------------------------------------------------------------------

def test_t8_missing_scanner_is_exit_1_and_writes_nothing(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    missing = tmp_path / "no-such-scanner.py"

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(missing)])
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "validat" in (r.stdout + r.stderr).lower()
    assert list(out.iterdir()) == []


# ---------------------------------------------------------------------------
# R2/R7 extras: overwrite refusal, --force, and usage-error exit code
# ---------------------------------------------------------------------------

def test_existing_output_is_refused_without_force_and_overwritten_with_it(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    target = out / "roster-format2.txt"
    target.write_text("PRE-EXISTING\n", encoding="utf-8")
    os.chmod(target, 0o644)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert target.read_text(encoding="utf-8") == "PRE-EXISTING\n"   # untouched

    r2 = _run(["--roster", str(roster), "--b-names", str(bnames),
               "--out-dir", str(out), "--force"])
    assert r2.returncode == 0, (r2.stdout, r2.stderr)
    assert target.read_text(encoding="utf-8").startswith("A\t")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_usage_error_exits_1(tmp_path):
    r = _run(["--roster", str(tmp_path / "r.txt")])   # missing required args
    assert r.returncode == 1, (r.stdout, r.stderr)


def test_missing_out_dir_exits_1(tmp_path):
    roster, bnames, _out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(tmp_path / "does-not-exist")])
    assert r.returncode == 1, (r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# R4 -- the name key: documented in --help, printed in the summary, and its
#       one-sided-boundary collapse pinned (the fail-open shape reported in the
#       ambiguous-decision log).
# ---------------------------------------------------------------------------

def test_name_key_is_documented_in_help_and_printed_in_the_summary(tmp_path):
    help_run = _run(["--help"])
    assert help_run.returncode == 0
    help_text = help_run.stdout
    for fragment in ("casefold", "(?i)", r"\b", "class B", "one-sided"):
        assert fragment in help_text, (fragment, help_text)

    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^name_key: .*one-sided.*casefold.*$", r.stdout, re.M), r.stdout


# ---------------------------------------------------------------------------
# R4 fix -- \b is a wrapper only as a MATCHED PAIR; casefold only with (?i)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("roster_line", [r"\b" + B_ONE, B_ONE + r"\b"])
def test_one_sided_boundary_is_class_a(tmp_path, roster_line):
    """A one-sided \\b is a prefix/suffix match, a different pattern from the
    bare name: it must stay class A (blocking)."""
    roster, bnames, out = _scratch(tmp_path, [roster_line], [B_ONE])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^class_a: 1$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 0$", r.stdout, re.M), r.stdout
    assert (out / "roster-format2.txt").read_text(encoding="utf-8").startswith("A\t")


@pytest.mark.parametrize("roster_line", [r"\b" + B_ONE + r"\b", B_ONE])
def test_matched_pair_or_bare_name_is_class_b(tmp_path, roster_line):
    """Both boundaries (a matched pair) or neither is the bare name: class B."""
    roster, bnames, out = _scratch(tmp_path, [roster_line], [B_ONE])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^class_a: 0$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 1$", r.stdout, re.M), r.stdout
    assert (out / "roster-format2.txt").read_text(encoding="utf-8").startswith("B\t")


def test_case_differing_without_inline_flag_is_class_a(tmp_path):
    """Without (?i) the pattern is case-sensitive, so a case-distinct name is
    not the permitted name: class A."""
    roster, bnames, out = _scratch(
        tmp_path, [r"\b" + A_ONE.upper() + r"\b"], [A_ONE]
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^class_a: 1$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 0$", r.stdout, re.M), r.stdout


def test_case_differing_with_inline_flag_is_class_b(tmp_path):
    """The same pattern with (?i) is case-insensitive: class B."""
    roster, bnames, out = _scratch(
        tmp_path, [r"(?i)\b" + A_ONE.upper() + r"\b"], [A_ONE]
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert re.search(r"^class_a: 0$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 1$", r.stdout, re.M), r.stdout


def test_class_a_no_boundary_refusal_says_it_will_not_rewrite_and_names_line(tmp_path):
    """R5's refusal message must tell the operator the tool will NOT rewrite the
    pattern, that the roster must declare the boundary itself, and which source
    line to look at -- while keeping the parser's own reason."""
    roster, bnames, out = _scratch(
        tmp_path, ["# a comment", "(?i)" + A_ONE], ["YVNoMatch"]
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    # The parser's own reason is preserved.
    assert "no declared boundary" in combined, combined
    # The operator-facing guidance.
    assert "will NOT rewrite" in combined, combined
    assert "lookaround" in combined and "^/$" in combined, combined
    assert "source roster line 2" in combined, combined
    assert "line" in combined, combined
    assert list(out.iterdir()) == []
    assert A_ONE not in combined, combined   # R1: never echo the pattern
