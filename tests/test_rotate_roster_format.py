"""Contract tests for tools/rotate_roster_format.py.

Spec: the rotation helper the roster holder runs alone (format 1 -> format 2).
Cells T1-T8 pin R1-R7; two extra cells pin R2's overwrite refusal and R7's
usage-error code, which the cells below would otherwise leave to ordinary use.

Every fixture token is invented and appears nowhere else:
  ZZAlphaHolder, ZZDeltaSecret, YYBravoHandle, YYCharlie Badge.

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
import json
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
B_MULTI = "YYCharlie Badge"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(**overrides):
    """The test process environment with ENGRAM_SCANNER removed, plus overrides."""
    e = dict(os.environ)
    e.pop("ENGRAM_SCANNER", None)
    e.update(overrides)
    return e


def _run(args, *, cwd=None, env=None, attestation="auto"):
    """Run the tool. ``attestation="auto"`` appends the fixture attestation
    (``<out-dir parent>/attestation.json``) unless one is already named;
    ``None`` injects nothing (used by the derivation cell)."""
    args = list(args)
    if attestation is not None and "--attestation" not in args:
        if attestation == "auto":
            if "--out-dir" in args:
                out = Path(args[args.index("--out-dir") + 1])
                candidate = out.parent / "attestation.json"
                if candidate.exists():
                    args += ["--attestation", str(candidate)]
        else:
            args += ["--attestation", str(attestation)]
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd, env=env if env is not None else _env(),
    )


def _write_lines(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _canonical_sha(data: bytes) -> str:
    """The gate's canonical form: all trailing newlines stripped, one added."""
    return hashlib.sha256(data.rstrip(b"\n") + b"\n").hexdigest()


def _write_attestation(path: Path, roster_path: Path, *, raw_value=None) -> Path:
    """Write a fixture attestation whose roster_sha256 is ``raw_value`` (or the
    canonical hash of ``roster_path``)."""
    value = raw_value if raw_value is not None else _canonical_sha(
        roster_path.read_bytes()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "roster_format": 1,
            "pattern_count": 1,
            "roster_sha256": value,
            "updated_at": "2026-01-01T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )
    return path


def _scratch(tmp_path: Path, roster_lines, b_lines, *, out_name="out",
             roster_text=None):
    """Lay out inputs/, out/ and a matching attestation.json under tmp_path."""
    inputs = tmp_path / "inputs"
    out = tmp_path / out_name
    out.mkdir(parents=True)
    if roster_text is None:
        roster = _write_lines(inputs / "roster.txt", roster_lines)
    else:
        roster = inputs / "roster.txt"
        roster.parent.mkdir(parents=True, exist_ok=True)
        roster.write_text(roster_text, encoding="utf-8")
    bnames = _write_lines(inputs / "bnames.txt", b_lines)
    _write_attestation(tmp_path / "attestation.json", roster)
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
        r"\bYYCharlie\s+Badge\b",
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


def _load_tool():
    return _import_by_path("rotate_roster_format_mod", TOOL)


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


def test_concurrently_created_output_is_not_deleted_without_force(tmp_path):
    """Chain of custody: a file created between the pre-check and the write
    must not be unlinked when --force was not given -- destroying the
    operator's output on a path the tool was only going to refuse to use."""
    stub = tmp_path / "concurrent-scanner.py"
    stub.write_text(
        r'''
def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    if len(lines) == 3:
        (path.parent / "roster-format2.txt").write_text(
            "CONCURRENT\n", encoding="utf-8")
    return [(None, "stub", klass) for klass in ("A", "A", "B")]
'''.lstrip(),
        encoding="utf-8",
    )
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    target = out / "roster-format2.txt"

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert target.read_text(encoding="utf-8") == "CONCURRENT\n"


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


@pytest.mark.parametrize("empty_line", [r"\b\b", "(?i)"])
def test_empty_name_key_in_a_roster_line_is_refused_by_line(tmp_path, empty_line):
    """An empty name key is a prefix match on everything: a B-file ``\\b``
    keyed to ``""`` would classify the roster's ``\\b\\b`` line as class B and
    emit it report-only. Fail closed, naming the source line and never text."""
    roster, bnames, out = _scratch(tmp_path, [empty_line], [r"\b"])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "roster line 1" in combined, combined
    assert "empty name key" in combined, combined
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("empty_line", [r"\b", "(?i)"])
def test_empty_name_key_in_the_b_names_file_is_refused_by_line(tmp_path, empty_line):
    """The B side alone is enough: name its 1-based line (the caller's own
    file, safe to name) and write nothing."""
    roster, bnames, out = _scratch(tmp_path, [BASE_ROSTER[0]], [empty_line])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "B-names file line 1" in combined, combined
    assert "empty name key" in combined, combined
    assert list(out.iterdir()) == []


def test_class_a_no_boundary_refusal_says_it_will_not_rewrite_and_names_line(tmp_path):
    """The unconditional pre-check must tell the operator the tool will NOT
    rewrite the pattern, that the roster must declare the boundary itself, and
    which source line to look at (F2; the parser is not reached)."""
    roster, bnames, out = _scratch(
        tmp_path, ["# a comment", "(?i)" + A_ONE], ["YVNoMatch"]
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "declare no boundary" in combined, combined
    assert "will NOT rewrite" in combined, combined
    assert "declare the boundary itself" in combined, combined
    assert "lookaround" in combined and "^/$" in combined, combined
    assert "source roster line 2" in combined, combined
    assert list(out.iterdir()) == []
    assert A_ONE not in combined, combined   # R1: never echo the pattern


# ---------------------------------------------------------------------------
# Input identity: the canonical sha256 must match the attested live list
# ---------------------------------------------------------------------------

def test_attestation_match_proceeds(tmp_path):
    """Control: a matching fixture attestation proceeds and writes the file."""
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()
    assert "attested_roster_sha256:" in r.stdout


def test_attestation_hash_mismatch_refuses_and_names_both_hashes(tmp_path):
    """A copy that is not the live list is refused; both public hashes are
    printed so the operator can see the difference."""
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    wrong = "0" * 64
    _write_attestation(tmp_path / "attestation.json", roster, raw_value=wrong)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert wrong in combined, combined
    assert _canonical_sha(roster.read_bytes()) in combined, combined
    assert "live list" in combined, combined
    assert list(out.iterdir()) == []


def test_attestation_trailing_newline_difference_still_matches(tmp_path):
    """The canonical form strips ALL trailing newlines before hashing, so a
    copy that differs only in trailing newlines is the same live list."""
    roster_text = "\n".join(BASE_ROSTER) + "\n\n\n"
    roster, bnames, out = _scratch(
        tmp_path, [], BASE_B, roster_text=roster_text
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()


def test_attestation_placeholder_refuses_naming_the_file(tmp_path):
    """A placeholder (anything not 64 hex) is not an attestation."""
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    _write_attestation(
        tmp_path / "attestation.json", roster, raw_value="UNSET - placeholder"
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert str(tmp_path / "attestation.json") in combined, combined
    assert "cannot be confirmed as the live list" in combined, combined
    assert list(out.iterdir()) == []


def test_attestation_default_is_derived_from_the_scanner_path(tmp_path):
    """With no --attestation, the path is derived from the scanner's location:
    <repo>/src/engram/tools/scan-leaks.py -> <repo>/.github/..."""
    repo = tmp_path / "fake-repo"
    scanner = repo / "src" / "engram" / "tools" / "scan-leaks.py"
    scanner.parent.mkdir(parents=True)
    scanner.write_bytes(SCAN_LEAKS.read_bytes())
    (repo / ".github").mkdir(parents=True)

    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    (tmp_path / "attestation.json").replace(
        repo / ".github" / "name-leak-roster.attestation.json"
    )

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(scanner)],
             attestation=None)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()


# ---------------------------------------------------------------------------
# F2 -- ALL unbounded class-A lines are listed on every run, not only the first
# ---------------------------------------------------------------------------

def test_all_unbounded_class_a_lines_are_reported_in_one_run(tmp_path):
    """Three unbounded class-A lines: one run must name all three source lines
    (the parser would stop at the first)."""
    third = "ZZFoxtrotSecret"
    roster_lines = ["# comment", "(?i)" + A_ONE, "", "(?i)" + A_TWO, "",
                    "(?i)" + third]
    roster, bnames, out = _scratch(tmp_path, roster_lines, ["YVNoMatch"])

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "2, 4, 6" in combined, combined
    assert "will NOT rewrite" in combined, combined
    for token in (A_ONE, A_TWO, third):
        assert token not in combined, (token, combined)
    assert list(out.iterdir()) == []


# ---------------------------------------------------------------------------
# --dry-run: the whole pipeline, no file
# ---------------------------------------------------------------------------

def test_dry_run_clean_roster_exits_0_and_writes_nothing(tmp_path):
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, [B_ONE, B_MULTI])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--dry-run"])

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "dry_run: true" in r.stdout, r.stdout
    assert re.search(r"^total_input_lines: 3$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_a: 2$", r.stdout, re.M), r.stdout
    assert re.search(r"^class_b: 1$", r.stdout, re.M), r.stdout
    assert "unmatched_b_name: " + B_MULTI in r.stdout, r.stdout
    assert "dry_run_result: would succeed" in r.stdout, r.stdout
    assert list(out.iterdir()) == []


def test_dry_run_reports_two_unbounded_lines_then_exits_2(tmp_path):
    roster_lines = ["(?i)" + A_ONE, "", "(?i)" + A_TWO]
    roster, bnames, out = _scratch(tmp_path, roster_lines, ["YVNoMatch"])

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--dry-run"])

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "class_a_without_boundary: 2" in r.stdout, r.stdout
    assert "no_boundary_source_line: 1" in r.stdout, r.stdout
    assert "no_boundary_source_line: 3" in r.stdout, r.stdout
    assert "dry_run_result: would refuse" in r.stdout, r.stdout
    assert list(out.iterdir()) == []
    assert A_ONE not in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# F1 -- a parser that predates format 2 must not be accepted as a validator
# ---------------------------------------------------------------------------

def _stub_scanner(tmp_path: Path) -> Path:
    stub = tmp_path / "stub-scanner.py"
    stub.write_text(
        "def load_roster_file(path):\n"
        "    path.read_text(encoding='utf-8')\n"
        "    return []\n",
        encoding="utf-8",
    )
    return stub


def test_stale_format1_scanner_is_refused_and_writes_nothing(tmp_path):
    """A format-1-era load_roster_file accepts anything; the positive control
    must refuse it and write nothing."""
    stub = _stub_scanner(tmp_path)
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "predates the two-class format" in combined, combined
    assert "an unvalidated roster is never emitted" in combined, combined
    assert list(out.iterdir()) == []


def test_stale_scanner_control_real_scanner_proceeds(tmp_path):
    """Control: the same fixture against the real scanner proceeds."""
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()


# ---------------------------------------------------------------------------
# Class-B width warning (warning only, never a refusal)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("roster_line,b_name", [
    (B_ONE, B_ONE),                              # declares no boundary
    (r"\bZZAlpha.Holder\b", "ZZAlpha.Holder"),   # bounded, but '.' is a meta
])
def test_class_b_wider_than_its_name_warns_but_succeeds(tmp_path, roster_line, b_name):
    roster, bnames, out = _scratch(tmp_path, [roster_line], [b_name])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (
        f"b_wider_than_its_name: source roster line 1: {b_name}" in r.stdout
    ), r.stdout
    assert (out / "roster-format2.txt").is_file()


@pytest.mark.parametrize("escape", ["s", "d", "w", "S", "D", "W"])
def test_backslash_escape_makes_a_class_b_line_wider_than_its_name(tmp_path, escape):
    """Every backslash escape other than ``\\b`` widens the pattern exactly as
    ``.`` does, so it must draw ``b_wider_than_its_name``."""
    roster_line = "\\b" + B_ONE + "\\" + escape + "\\b"
    b_name = B_ONE + "\\" + escape
    roster, bnames, out = _scratch(tmp_path, [roster_line], [b_name])
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (
        f"b_wider_than_its_name: source roster line 1: {b_name}" in r.stdout
    ), "a backslash escape drew no width warning"
    assert (out / "roster-format2.txt").is_file()


def test_bounded_metachar_free_class_b_line_prints_no_width_warning(tmp_path):
    roster, bnames, out = _scratch(
        tmp_path, [r"\b" + B_ONE + r"\b"], [B_ONE]
    )
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "b_wider_than_its_name" not in r.stdout, r.stdout
    assert (out / "roster-format2.txt").is_file()


# ---------------------------------------------------------------------------
# R1 -- the parser's RETURN VALUE is checked, not only its exit
# ---------------------------------------------------------------------------

def test_stub_returning_right_length_all_a_classes_is_refused(tmp_path):
    """A stub that refuses the two probe strings but accepts the produced
    roster and returns the right LENGTH with every class 'A' validates
    nothing; the return-value check must refuse it."""
    stub = tmp_path / "accepting-scanner.py"
    stub.write_text(
        r'''
def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    return [(None, "stub", "A") for _ in lines]
'''.lstrip(),
        encoding="utf-8",
    )
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "predates the two-class format" in combined, combined
    assert "an unvalidated roster is never emitted" in combined, combined
    # The message names nothing but the scanner path.
    assert "\t" not in combined, combined
    assert "\\b" not in combined, combined
    assert "ZZProbeName" not in combined, combined
    assert list(out.iterdir()) == []


def test_stub_returning_all_a_classes_control_real_scanner_proceeds(tmp_path):
    """Control: the same fixture against the real scanner proceeds."""
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()


def test_stub_returning_too_few_entries_is_refused(tmp_path):
    """A stub that refuses both probes but returns fewer entries than there
    were produced lines has validated nothing: the entry COUNT check must
    refuse it, not let a truncated result through to be written."""
    stub = tmp_path / "short-count-scanner.py"
    stub.write_text(
        r'''
def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    return [(None, "stub", "A"), (None, "stub", "A")]
'''.lstrip(),
        encoding="utf-8",
    )
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)
    assert len(BASE_ROSTER) == 3          # the stub returns one fewer

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "predates the two-class format" in combined, combined
    assert "an unvalidated roster is never emitted" in combined, combined
    # The message names nothing but the scanner path.
    assert "\t" not in combined, combined
    assert "\\b" not in combined, combined
    assert "ZZProbeName" not in combined, combined
    assert list(out.iterdir()) == []


def test_stub_returning_entries_too_short_to_index_is_refused(tmp_path):
    """A stub returning the right NUMBER of entries whose entries are too
    short to index ``entry[2]`` is not a validator either. The assertion is
    the MESSAGE: an unguarded ``entry[2]`` raises IndexError, which also exits
    non-zero, but with the internal-error text rather than this refusal."""
    stub = tmp_path / "short-entry-scanner.py"
    stub.write_text(
        r'''
def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    return [(None, "stub"), (None, "stub"), (None, "stub")]
'''.lstrip(),
        encoding="utf-8",
    )
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "predates the two-class format" in combined, combined
    assert "an unvalidated roster is never emitted" in combined, combined
    assert list(out.iterdir()) == []


def test_stub_returning_exactly_what_was_produced_proceeds(tmp_path):
    """Control for the two cells above: a stub that refuses both probes and
    returns exactly the produced classes (A, A, B) proceeds and writes the
    file. Without it, "refused" is indistinguishable from "refuses
    everything"."""
    stub = tmp_path / "exact-scanner.py"
    stub.write_text(
        r'''
def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    return [(None, "stub", klass) for klass in ("A", "A", "B")]
'''.lstrip(),
        encoding="utf-8",
    )
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (out / "roster-format2.txt").is_file()


# ---------------------------------------------------------------------------
# R1 -- captured parser stderr: withheld when it carries a produced pattern
# ---------------------------------------------------------------------------

def _echoing_refusal_scanner(tmp_path: Path) -> Path:
    """A parser that refuses the probes and the produced roster, echoing the
    first produced line (and so its pattern) to stderr first."""
    stub = tmp_path / "echoing-refusal-scanner.py"
    stub.write_text(
        r'''
import sys


def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    sys.stderr.write("scan-leaks: roster line 1 refuses: " + lines[0] + "\n")
    raise SystemExit(2)
'''.lstrip(),
        encoding="utf-8",
    )
    return stub


def _doubled_echo_refusal_scanner(tmp_path: Path) -> Path:
    """A parser that refuses the probes and the produced roster, echoing the
    first produced line backslash-doubled (as ``repr`` renders it) to stderr."""
    stub = tmp_path / "doubled-echo-refusal-scanner.py"
    stub.write_text(
        r'''
import sys


def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    sys.stderr.write(
        "scan-leaks: roster line 1 refuses: "
        + lines[0].replace("\\", "\\\\") + "\n")
    raise SystemExit(2)
'''.lstrip(),
        encoding="utf-8",
    )
    return stub


def _bare_name_echo_refusal_scanner(tmp_path: Path) -> Path:
    """A parser that refuses the probes and the produced roster, echoing only
    the first produced line's BARE NAME KEY -- the pattern without its ``\\b``
    pair -- to stderr."""
    stub = tmp_path / "bare-name-echo-refusal-scanner.py"
    stub.write_text(
        r'''
import sys


def _declares_boundary(pattern):
    return True


def _key_parts(text):
    key = text.strip()
    if key.startswith("(?i)"):
        key = key[4:]
    if key.startswith("\\b") and key.endswith("\\b"):
        key = key[2:-2]
    return key.strip()


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    pattern = lines[0].split("\t", 1)[1]
    sys.stderr.write(
        "scan-leaks: roster line 1 refuses: " + _key_parts(pattern) + "\n")
    raise SystemExit(2)
'''.lstrip(),
        encoding="utf-8",
    )
    return stub


def _ordinary_refusal_scanner(tmp_path: Path) -> Path:
    """A parser that refuses the probes and the produced roster with an
    ordinary message carrying no pattern text."""
    stub = tmp_path / "ordinary-refusal-scanner.py"
    stub.write_text(
        r'''
import sys


def _declares_boundary(pattern):
    return True


def load_roster_file(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        if ln.startswith("C\t") or (ln.startswith("A\t") and "\\b" not in ln):
            raise SystemExit(2)
    sys.stderr.write(
        "scan-leaks: roster line 2 has no class token; every line needs a "
        "class prefix\n")
    raise SystemExit(2)
'''.lstrip(),
        encoding="utf-8",
    )
    return stub


def test_parser_refusal_carrying_a_produced_pattern_is_withheld(tmp_path):
    """The guarantee covers text this tool emits: a parser that echoes a
    produced pattern is withheld entirely and reported by exit code only."""
    stub = _echoing_refusal_scanner(tmp_path)
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "parser refused (exit 2)" in combined, combined
    assert "withheld because it contained roster text" in combined, combined
    assert A_ONE not in combined, "a produced roster pattern reached the operator"
    assert list(out.iterdir()) == []


def test_parser_refusal_carrying_a_backslash_doubled_pattern_is_withheld(tmp_path):
    """A parser that echoes a produced pattern backslash-doubled (as ``repr``
    renders it) must be withheld whole too: a literal-only containment test
    misses the doubled text and the pattern reaches the operator."""
    stub = _doubled_echo_refusal_scanner(tmp_path)
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert A_ONE not in combined, "a produced roster pattern reached the operator"
    assert "parser refused (exit 2)" in combined, combined
    assert "withheld because it contained roster text" in combined, combined
    assert list(out.iterdir()) == []


def test_parser_refusal_carrying_only_a_bare_name_key_is_withheld(tmp_path):
    """A parser that echoes only a produced line's bare name key -- the pattern
    without its ``\\b`` pair -- still carries name text. A containment test on
    the whole pattern passes this text straight to the operator, so the
    name-key rule must withhold it whole."""
    stub = _bare_name_echo_refusal_scanner(tmp_path)
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "parser refused (exit 2)" in combined, combined
    assert "withheld because it contained roster text" in combined, combined
    assert A_ONE not in combined, "a produced name key reached the operator"
    assert list(out.iterdir()) == []


def test_parser_refusal_without_a_pattern_is_passed_through_verbatim(tmp_path):
    """Control: an ordinary refusal is re-emitted verbatim. This cell is green
    both before and after the withholding guard, so it discriminates a guard
    that simply suppresses every refusal."""
    stub = _ordinary_refusal_scanner(tmp_path)
    roster, bnames, out = _scratch(tmp_path, BASE_ROSTER, BASE_B)

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out), "--scanner", str(stub)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert (
        "scan-leaks: roster line 2 has no class token; every line needs a "
        "class prefix" in r.stderr
    ), combined
    assert "withheld" not in combined, combined
    assert list(out.iterdir()) == []


def test_real_scanner_refusal_message_reaches_the_operator(tmp_path):
    """The real scanner refuses an uncompilable produced pattern; its own
    message (line number and error class, never the pattern) still reaches the
    operator."""
    roster, bnames, out = _scratch(
        tmp_path, [r"\b" + A_ONE + r"\b("], [B_ONE]
    )

    r = _run(["--roster", str(roster), "--b-names", str(bnames),
              "--out-dir", str(out)])
    combined = r.stdout + r.stderr

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "is not a valid regular expression" in r.stderr, combined
    assert (
        "the produced roster was refused by the parser (exit 2); nothing "
        "written." in r.stderr
    ), combined
    assert "withheld" not in combined, combined
    assert A_ONE not in combined, "a produced roster pattern reached the operator"
    assert list(out.iterdir()) == []


# ---------------------------------------------------------------------------
# R2 -- the probe token is fresh per run
# ---------------------------------------------------------------------------

def test_probe_token_is_randomised_per_build():
    """Two builds in one process use different tokens; each is 16 hex chars.
    The constructed probe text is asserted on, never printed."""
    mod = _load_tool()
    first = [text for _label, text in mod._probe_rosters()]
    second = [text for _label, text in mod._probe_rosters()]

    assert len(first) == 2 and len(second) == 2
    assert first != second
    for texts in (first, second):
        for text in texts:
            token = (
                text.split("\t", 1)[1]
                .strip()
                .removeprefix("\\b")
                .removesuffix("\\b")
            )
            assert re.fullmatch(r"[0-9a-f]{16}", token)
            assert token != "ZZProbeName"
