"""Completeness guard for docs/2652-migration-ledger.md [part of #2652].

The ledger lists every `sqlite3.connect(` line in tracked `src/engram`, one
Section-2 row per line. tools/check_2652_ledger.py compares, PER FILE, the
number of rows naming the file with the number of matching lines in it.
Per-file (not file:line) so unrelated edits that shift line numbers stay green.

Each test runs the script as a subprocess against a temp git repo; the real
ledger is only read, never edited.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "check_2652_ledger.py"

STATES = ("owner", "direct", "behind-flag", "flag-on", "retired")
OOS = "out of scope"
TABLE_HEAD = (
    "| # | site (function) | kind | target | access | component | state | A6 delta | macOS |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)
FOOTER = "\nPaths are relative to `src/engram/`.\n\n## 3. Registry\n\nnothing path-shaped here\n"


def C(path, n, fn="<module>"):
    """Section-2 first cell, new form: `path` `function()` `anchor`, anchored on
    the fixture line `c<n> = sqlite3.connect(` (see `L`)."""
    shown = fn if fn in ("<module>", "—") else fn + "()"
    return f"`{path}` `{shown}` `c{n} = sqlite3.connect(`"


def L(*ns):
    """Fixture source: one distinct connect line per number (anchors stay unique)."""
    return "".join(f"c{n} = sqlite3.connect(p)\n" for n in ns)


KINDS = ("call", "prose")
TARGETS = ("live store", "parameter", "other DB", "n/a (prose)", "unresolved")


def _target_cell(cat):
    """A realistic target cell: the category, then free text."""
    return "n/a — comment text" if cat == "n/a (prose)" else f"{cat}: free text"


def _row(n, spec, state="direct", kind="call", target="live store"):
    return f"| {n} | {spec} | {kind} | {_target_cell(target)} | rw | x | {state} | — | — |\n"


def _tally(states):
    c = {k: 0 for k in STATES + (OOS,)}
    for s in states:
        c[s] = c.get(s, 0) + 1
    return c


def _summary_line(c):
    live = sum(c[k] for k in STATES)
    parts = " · ".join(f"**{k} {c[k]}**" + (" (x)" if k == "direct" else "") for k in STATES)
    return (
        f"- **State of the {live} live-store lines** (calls): {parts}. "
        f"The other {c[OOS]} lines are out of scope (other databases, or prose).\n"
    )


def _state_line(c):
    return "- **state:** " + " · ".join(f"{k} {c[k]}" for k in STATES + (OOS,)) + "\n"


def _count(values, keys):
    return {k: sum(1 for v in values if v == k) for k in keys}


def _kind_line(c):
    return "- **kind:** " + " · ".join(f"{k} {c[k]}" for k in KINDS) + "\n"


def _target_line(c):
    return "- **target:** " + " · ".join(f"{k} {c[k]}" for k in TARGETS) + "\n"


def _unit_line(ncall, nlines=None, nconnects=None):
    nlines = ncall if nlines is None else nlines
    nconnects = ncall + 1 if nconnects is None else nconnects
    return (
        f"- **unit:** one row per matching LINE. Row 5 has two connects, so "
        f"the {ncall} `call` rows are {nlines} lines and {nconnects} connects.\n"
    )


def _ledger(rows, states=None, summary=None, state_line=None, kinds=None, targets=None,
            kind_line=None, target_line=None, unit_line=None):
    """summary/state_line/kind_line/target_line/unit_line: None = consistent
    default, "" = omit, else verbatim."""
    states = list(states) if states is not None else ["direct"] * len(rows)
    kinds = list(kinds) if kinds is not None else ["call"] * len(rows)
    targets = list(targets) if targets is not None else ["live store"] * len(rows)
    c = _tally(states)
    sm = _summary_line(c) if summary is None else summary
    sl = _state_line(c) if state_line is None else state_line
    kl = _kind_line(_count(kinds, KINDS)) if kind_line is None else kind_line
    tl = _target_line(_count(targets, TARGETS)) if target_line is None else target_line
    ul = _unit_line(kinds.count("call")) if unit_line is None else unit_line
    head = (
        f"# ledger\n\n## Summary\n\n{sm}\n## 1. Population\n\ntext\n{ul}{kl}{tl}{sl}\n"
        "## 2. The ledger\n\n" + TABLE_HEAD
    )
    body = "".join(
        _row(i + 1, r, states[i], kinds[i], targets[i]) for i, r in enumerate(rows)
    )
    return head + body + FOOTER


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _make_repo(tmp_path, files, rows, untracked=None, ledger_text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    for rel, text in files.items():
        p = tmp_path / "src" / "engram" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "2652-migration-ledger.md").write_text(
        ledger_text if ledger_text is not None else _ledger(rows), encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    for rel, text in (untracked or {}).items():
        p = tmp_path / "src" / "engram" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _run(root):
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root)],
        capture_output=True, text=True,
    )




def test_clause1_real_ledger_matches_real_tree():
    r = _run(REPO)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause2_added_connect_without_row_fails_naming_file_and_counts(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": "x\n" + L(1, 2), "b.py": L(1)},
        [C("a.py", 2), C("b.py", 1)],
    )
    r = _run(root)
    assert r.returncode == 1
    assert "a.py: ledger 1 rows, tree 2 lines" in r.stdout
    assert "b.py" not in r.stdout.split("add/remove")[0]
    assert "add/remove a Section-2 row in docs/2652-migration-ledger.md" in r.stdout


def test_clause3_row_for_file_without_connect_fails(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": L(1), "gone.py": "nothing here\n"},
        [C("a.py", 1), C("gone.py", 7)],
    )
    r = _run(root)
    assert r.returncode == 1
    assert "gone.py: ledger 1 rows, tree 0 lines" in r.stdout


def test_clause4_line_drift_only_passes(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": "\n\n\n" + L(1, 2)},
        [C("a.py", 1), C("a.py", 2)],
    )
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause5_untracked_file_not_counted(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": L(1)},
        [C("a.py", 1)],
        untracked={"scratch.py": L(1)},
    )
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause6_unparseable_section2_fails_loudly(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": L(1)},
        [],
        ledger_text="# ledger\n\n## 2. The ledger\n\nno table here\n\n## 3. x\n",
    )
    r = _run(root)
    assert r.returncode == 1
    assert "could not parse" in (r.stdout + r.stderr)


def test_clause6b_no_section2_heading_fails_loudly(tmp_path):
    root = _make_repo(tmp_path, {"a.py": L(1)}, [], ledger_text="# nothing\n")
    r = _run(root)
    assert r.returncode == 1
    assert "could not parse" in (r.stdout + r.stderr)


def test_clause7_unreadable_tracked_file_fails_loudly_naming_it(tmp_path):
    # An unreadable file may hide connect lines; skipping it would let the
    # guard report consistent. Root reads anyway, so the setup cannot hold.
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root can read a chmod 000 file")
    root = _make_repo(
        tmp_path, {"a.py": L(1), "locked.py": L(1)}, [C("a.py", 1), C("locked.py", 1)]
    )
    locked = root / "src" / "engram" / "locked.py"
    locked.chmod(0)
    try:
        r = _run(root)
    finally:
        locked.chmod(0o644)
    assert r.returncode == 1
    assert "could not read src/engram/locked.py:" in r.stdout
    assert "a.py" not in r.stdout


def test_clause8_section3_row_naming_a_file_is_not_counted(tmp_path):
    # Section 3 rows have the same `| N | `file:line` ...` shape as Section 2
    # rows. Counting them would turn a consistent ledger red (a.py would read
    # 2 rows vs 1 line; zzz.py 1 row vs 0 lines).
    text = _ledger([C("a.py", 1)]).replace(
        "nothing path-shaped here\n",
        "| 1 | `a.py:1` (x) |\n| 2 | `zzz.py:9` (x) |\n",
    )
    assert "`zzz.py:9`" in text and "## 3." in text
    root = _make_repo(tmp_path, {"a.py": L(1)}, [], ledger_text=text)
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause9_outside_a_checkout_exits_1_with_one_line(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "2652-migration-ledger.md").write_text(
        _ledger([C("a.py", 1)]), encoding="utf-8"
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert r.stdout.strip() == f"not a git checkout: {tmp_path}"
    assert "Traceback" not in r.stderr


# ---- Section-1 state tally vs the Section-2 rows (contract C1-C7) ----

FILES1 = {"a.py": L(1)}


def _run_ledger(tmp_path, text):
    return _run(_make_repo(tmp_path, FILES1, [], ledger_text=text))


def test_c1_consistent_tally_passes(tmp_path):
    text = _ledger([C("a.py", 1)], states=["behind-flag"])
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 0, r.stdout + r.stderr


def test_c1b_mixed_states_consistent_passes(tmp_path):
    files = {"a.py": L(*range(1, 6))}
    states = ["owner", "direct", "behind-flag", "flag-on", OOS]
    text = _ledger([C("a.py", i + 1) for i in range(5)], states=states)
    r = _run(_make_repo(tmp_path, files, [], ledger_text=text))
    assert r.returncode == 0, r.stdout + r.stderr


def test_c2_state_line_disagrees_names_state_numbers_and_line(tmp_path):
    c = _tally(["direct"])
    c["direct"] = 39
    text = _ledger([C("a.py", 1)], state_line=_state_line(c))
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "section 1 state line: direct says 39, rows give 1" in r.stdout
    assert "summary sentence" not in r.stdout


def test_c3_summary_sentence_disagrees_names_that_line(tmp_path):
    c = _tally(["direct"])
    c["direct"] = 38
    c["behind-flag"] = 1
    text = _ledger([C("a.py", 1)], summary=_summary_line(c))
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "section 1 summary sentence: direct says 38, rows give 1" in r.stdout
    assert "section 1 summary sentence: behind-flag says 1, rows give 0" in r.stdout
    assert "state line" not in r.stdout


def test_c3b_summary_total_must_equal_sum_of_live_states(tmp_path):
    good = _summary_line(_tally(["direct"]))
    bad = good.replace("State of the 1 live-store", "State of the 9 live-store")
    assert bad != good
    r = _run_ledger(tmp_path, _ledger([C("a.py", 1)], summary=bad))
    assert r.returncode == 1
    assert "section 1 summary sentence: total says 9, rows give 1" in r.stdout


def test_c4_missing_state_line_fails_loudly(tmp_path):
    r = _run_ledger(tmp_path, _ledger([C("a.py", 1)], state_line=""))
    assert r.returncode == 1
    assert "could not parse section 1 tally: no `- **state:**` line" in r.stdout


def test_c4b_missing_summary_sentence_fails_loudly(tmp_path):
    r = _run_ledger(tmp_path, _ledger([C("a.py", 1)], summary=""))
    assert r.returncode == 1
    assert "could not parse section 1 tally: no summary sentence" in r.stdout


def test_c4c_unparseable_tally_line_fails_loudly(tmp_path):
    r = _run_ledger(
        tmp_path, _ledger([C("a.py", 1)], state_line="- **state:** direct one · owner none\n")
    )
    assert r.returncode == 1
    assert "could not parse section 1 tally" in r.stdout


def test_c4d_tally_line_missing_a_state_key_fails_loudly(tmp_path):
    r = _run_ledger(
        tmp_path, _ledger([C("a.py", 1)], state_line="- **state:** direct 1 · out of scope 0\n")
    )
    assert r.returncode == 1
    assert "could not parse section 1 tally" in r.stdout


def test_c5_no_recognised_state_value_fails_loudly(tmp_path):
    # Every row carries a state the guard does not know: zero parsed states
    # must not read as a consistent (0 = 0) tally.
    text = _ledger([C("a.py", 1)], states=["flagon"])
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "could not parse Section 2 states" in r.stdout


def test_c5b_no_state_column_in_header_fails_loudly(tmp_path):
    text = _ledger([C("a.py", 1)]).replace(" state |", " status |", 1)
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "could not parse Section 2 states" in r.stdout


def test_c6_unknown_state_value_names_row_and_value(tmp_path):
    files = {"a.py": L(1, 2, 3)}
    rows = [C("a.py", 1), C("a.py", 2), C("a.py", 3)]
    text = _ledger(rows, states=["direct", "behind flag", "direct"])
    r = _run(_make_repo(tmp_path, files, [], ledger_text=text))
    assert r.returncode == 1
    assert "row 2: unknown state 'behind flag'" in r.stdout


def test_c6b_flagon_typo_names_row_and_value(tmp_path):
    text = _ledger([C("a.py", 1)], states=["direct"]).replace("| direct |", "| flagon |", 1)
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "row 1: unknown state 'flagon'" in r.stdout


def test_c6c_state_found_by_header_name_not_index(tmp_path):
    # State column moved to position 3 and another column holds the word
    # `retired` in free text: a fixed index would read the wrong cell.
    files = {"a.py": L(1, 2)}
    head = (
        "# l\n\n## Summary\n\n" + _summary_line(_tally(["direct", "owner"]))
        + "\n## 1. P\n\n" + _unit_line(2) + _kind_line({"call": 2, "prose": 0})
        + _target_line(_count(["live store"] * 2, TARGETS))
        + _state_line(_tally(["direct", "owner"]))
        + "\n## 2. The ledger\n\n| # | site | kind | target | state | note |\n|---|---|---|---|---|---|\n"
    )
    body = (
        "| 1 | " + C("a.py", 1) + " | call | live store | direct | retired soon |\n"
        "| 2 | " + C("a.py", 2) + " | call | live store | owner | x |\n"
    )
    r = _run(_make_repo(tmp_path, files, [], ledger_text=head + body + FOOTER))
    assert r.returncode == 0, r.stdout + r.stderr


def test_c6d_row_with_wrong_cell_count_fails_loudly(tmp_path):
    text = _ledger([C("a.py", 1)]).replace("| rw | x |", "| rw | x | y |", 1)
    r = _run_ledger(tmp_path, text)
    assert r.returncode == 1
    assert "row 1: has 10 cells, header has 9" in r.stdout


def test_c7_real_ledger_tally_matches_rows():
    r = _run(REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "section 1" not in r.stdout


# ---- kind / target / unit tallies are derived from the rows too (#2652) ----


def _two(**kw):
    return _ledger([C("a.py", 1), C("a.py", 2)], **kw)


def _run2(tmp_path, text):
    return _run(_make_repo(tmp_path, {"a.py": L(1, 2)}, [], ledger_text=text))


def test_k1_consistent_mixed_kinds_and_targets_pass(tmp_path):
    kinds = ["call", "prose", "call", "call", "prose"]
    targets = ["live store", "n/a (prose)", "parameter", "other DB", "n/a (prose)"]
    text = _ledger([C("a.py", i + 1) for i in range(5)], kinds=kinds, targets=targets)
    r = _run(_make_repo(tmp_path, {"a.py": L(*range(1, 6))}, [], ledger_text=text))
    assert r.returncode == 0, r.stdout + r.stderr


def test_k2_kind_line_number_alone_changed_fails_and_only_kind_fires(tmp_path):
    # Discriminating control: mutate ONLY the kind line's number.
    good = _two()
    bad = good.replace("- **kind:** call 2 · prose 0", "- **kind:** call 3 · prose 0")
    assert bad != good
    assert _run2(tmp_path / "good", good).returncode == 0
    r = _run2(tmp_path / "bad", bad)
    assert r.returncode == 1
    assert "section 1 kind: call says 3, rows give 2" in r.stdout
    assert "section 1 state" not in r.stdout and "row " not in r.stdout


def test_k3_prose_count_disagrees(tmp_path):
    text = _two(kinds=["call", "prose"], targets=["live store", "n/a (prose)"],
                kind_line="- **kind:** call 2 · prose 0\n")
    r = _run2(tmp_path, text)
    assert r.returncode == 1
    assert "section 1 kind: call says 2, rows give 1" in r.stdout
    assert "section 1 kind: prose says 0, rows give 1" in r.stdout


def test_k4_unknown_kind_names_row_and_value(tmp_path):
    text = _two().replace("| call |", "| calls |", 1)
    r = _run2(tmp_path, text)
    assert r.returncode == 1
    assert "row 1: unknown kind 'calls'" in r.stdout


def test_k5_kind_found_by_header_name_and_not_by_substring(tmp_path):
    # Decoy: a whole-row substring count of `call` would see the word in the
    # prose row's target and note. Columns are also reordered.
    files = {"a.py": L(1, 2)}
    head = (
        "# l\n\n## Summary\n\n" + _summary_line(_tally(["direct", "direct"]))
        + "\n## 1. P\n\n" + _unit_line(1) + _kind_line({"call": 1, "prose": 1})
        + _target_line({"live store": 1, "parameter": 0, "other DB": 0,
                        "n/a (prose)": 1, "unresolved": 0})
        + _state_line(_tally(["direct", "direct"]))
        + "\n## 2. The ledger\n\n| # | site | note | target | state | kind |\n|---|---|---|---|---|---|\n"
    )
    body = (
        "| 1 | " + C("a.py", 1) + " | call call call | live store: call | direct | call |\n"
        "| 2 | " + C("a.py", 2) + " | call it | n/a — call in a comment | direct | prose |\n"
    )
    r = _run(_make_repo(tmp_path, files, [], ledger_text=head + body + FOOTER))
    assert r.returncode == 0, r.stdout + r.stderr


def test_k6_missing_or_unparseable_kind_line_fails_loudly(tmp_path):
    r = _run2(tmp_path / "a", _two(kind_line=""))
    assert r.returncode == 1
    assert "could not parse section 1 tally: no `- **kind:**` line" in r.stdout
    r = _run2(tmp_path / "b", _two(kind_line="- **kind:** call two\n"))
    assert r.returncode == 1
    assert "could not parse section 1 tally: `- **kind:**` line lacks call, prose" in r.stdout
    r = _run2(tmp_path / "c", _two(kind_line="- **kind:** call 2 · prose 0\n" * 2))
    assert r.returncode == 1
    assert "more than one `- **kind:**` line" in r.stdout


def test_g1_target_line_disagrees_names_category_and_numbers(tmp_path):
    text = _two(targets=["live store", "parameter"],
                target_line="- **target:** live store 2 · parameter 0 · other DB 0 · "
                            "n/a (prose) 0 · unresolved 0\n")
    r = _run2(tmp_path, text)
    assert r.returncode == 1
    assert "section 1 target: live store says 2, rows give 1" in r.stdout
    assert "section 1 target: parameter says 0, rows give 1" in r.stdout


def test_g2_every_category_prefix_form_classifies(tmp_path):
    cells = {
        "live store": ["live store", "live store — free", "live store: free"],
        "parameter": ["parameter (`<db path>` placeholder)", "parameter — free"],
        "other DB": ["other DB: free", "other DB"],
        "n/a (prose)": ["n/a (site: module docstring)", "n/a — docstring", "n/a (prose)"],
        "unresolved": ["unresolved — who knows"],
    }
    flat = [(cat, cell) for cat, cs in cells.items() for cell in cs]
    n = len(flat)
    c = _count([cat for cat, _ in flat], TARGETS)
    text = _ledger([C("a.py", i + 1) for i in range(n)], target_line=_target_line(c))
    for i, (_cat, cell) in enumerate(flat):
        old = f"| {i + 1} | {C('a.py', i + 1)} | call | live store: free text |"
        assert old in text
        text = text.replace(old, f"| {i + 1} | {C('a.py', i + 1)} | call | {cell} |", 1)
    r = _run(_make_repo(tmp_path, {"a.py": L(*range(1, n + 1))}, [], ledger_text=text))
    assert r.returncode == 0, r.stdout + r.stderr


def test_g3_unknown_target_names_row_and_prefix(tmp_path):
    text = _two().replace("live store: free text", "elsewhere: free text", 1)
    r = _run2(tmp_path, text)
    assert r.returncode == 1
    assert "row 1: unknown target 'elsewhere'" in r.stdout


def test_g3b_category_must_end_at_a_word_boundary(tmp_path):
    text = _two().replace("live store: free text", "live stores: free text", 1)
    r = _run2(tmp_path, text)
    assert r.returncode == 1
    assert "row 1: unknown target 'live stores'" in r.stdout


def test_g4_missing_or_unparseable_target_line_fails_loudly(tmp_path):
    r = _run2(tmp_path / "a", _two(target_line=""))
    assert r.returncode == 1
    assert "could not parse section 1 tally: no `- **target:**` line" in r.stdout
    r = _run2(tmp_path / "b", _two(target_line="- **target:** live store 2 · parameter 0\n"))
    assert r.returncode == 1
    assert "`- **target:**` line lacks other DB, n/a (prose), unresolved" in r.stdout


def test_u1_unit_line_call_count_must_equal_derived_call_rows(tmp_path):
    r = _run2(tmp_path / "a", _two(unit_line=_unit_line(3, nlines=2)))
    assert r.returncode == 1
    assert "section 1 unit: call rows says 3, rows give 2" in r.stdout
    r = _run2(tmp_path / "b", _two(unit_line=_unit_line(2, nlines=5)))
    assert r.returncode == 1
    assert "section 1 unit: lines says 5, rows give 2" in r.stdout


def test_u2_connects_number_is_not_checked(tmp_path):
    assert _run2(tmp_path, _two(unit_line=_unit_line(2, nconnects=999))).returncode == 0


def test_u3_missing_or_unparseable_unit_line_fails_loudly(tmp_path):
    r = _run2(tmp_path / "a", _two(unit_line=""))
    assert r.returncode == 1
    assert "could not parse section 1 tally: no `- **unit:**` line" in r.stdout
    r = _run2(tmp_path / "b", _two(unit_line="- **unit:** one row per line.\n"))
    assert r.returncode == 1
    assert "`- **unit:**` line lacks 'the N `call` rows are N lines'" in r.stdout


def test_u4_unit_counts_call_rows_not_all_rows(tmp_path):
    text = _two(kinds=["call", "prose"], targets=["live store", "n/a (prose)"])
    assert _run2(tmp_path, text).returncode == 0


# ---- round 2: claimed-but-absent, every target category, failure paths ----


def _target_line_with(**override):
    base = {"live store": 2, "parameter": 0, "other DB": 0, "n/a (prose)": 0, "unresolved": 0}
    base.update({k.replace("_", " "): v for k, v in override.items()})
    return "- **target:** " + " · ".join(f"{k} {base[k]}" for k in TARGETS) + "\n"


def test_r1_claimed_but_absent_category_is_reported(tmp_path):
    # Section 1 claims a count for a category NO row has: the rows give 0.
    # Catches an implementation that iterates only over the keys the rows hold.
    r = _run2(tmp_path / "t", _two(target_line=_target_line_with(unresolved=3)))
    assert r.returncode == 1
    assert "section 1 target: unresolved says 3, rows give 0" in r.stdout
    assert r.stdout.count("section 1 target:") == 1
    r = _run2(tmp_path / "k", _two(kind_line="- **kind:** call 2 · prose 5\n"))
    assert r.returncode == 1
    assert "section 1 kind: prose says 5, rows give 0" in r.stdout
    assert r.stdout.count("section 1 kind:") == 1


def test_r2_each_target_category_is_compared_on_its_own(tmp_path):
    # Rows give live store 1 plus one row of the category under test; Section 1
    # drops that one category's count to 0 and nothing else. Catches an
    # implementation that compares only a subset of the five categories.
    for i, cat in enumerate(("parameter", "other DB", "n/a (prose)", "unresolved")):
        kinds = ["call", "prose" if cat == "n/a (prose)" else "call"]
        claimed = {k: 0 for k in TARGETS}
        claimed["live store"] = 1
        line = "- **target:** " + " · ".join(f"{k} {claimed[k]}" for k in TARGETS) + "\n"
        r = _run2(tmp_path / str(i), _two(targets=["live store", cat], kinds=kinds,
                                          target_line=line))
        assert r.returncode == 1, cat
        assert f"section 1 target: {cat} says 0, rows give 1" in r.stdout, cat
        assert r.stdout.count("section 1 target:") == 1, cat


def test_r3_missing_kind_or_target_column_fails_loudly(tmp_path):
    good = _two()
    r = _run2(tmp_path / "k", good.replace(" kind |", " type |", 1))
    assert r.returncode == 1
    assert "could not parse Section 2 kinds: no `kind` column in the table header" in r.stdout
    assert "could not parse Section 2 targets" not in r.stdout
    r = _run2(tmp_path / "t", good.replace(" target |", " dest |", 1))
    assert r.returncode == 1
    assert "could not parse Section 2 targets: no `target` column in the table header" in r.stdout
    assert "could not parse Section 2 kinds" not in r.stdout


def test_r4_repeated_target_or_unit_line_fails_loudly(tmp_path):
    r = _run2(tmp_path / "t", _two(target_line=_target_line_with() * 2))
    assert r.returncode == 1
    assert "could not parse section 1 tally: more than one `- **target:**` line" in r.stdout
    r = _run2(tmp_path / "u", _two(unit_line=_unit_line(2) * 2))
    assert r.returncode == 1
    assert "could not parse section 1 tally: more than one `- **unit:**` line" in r.stdout


# ---- Section-2 citations are function + anchor, verified (contract A1-A11, #3704) ----

SRC_A = (
    "import sqlite3\n\n"
    "def one(p):\n"
    "    first = sqlite3.connect(p)\n"
    "    return first\n\n"
    "def two(p):\n"
    "    second = sqlite3.connect(p)\n"
    "    return second\n\n"
    "class K:\n"
    "    def meth(self, p):\n"
    "        self.h = sqlite3.connect(p)\n"
)
GOOD_A = [
    ("a.py", "one()", "first = sqlite3.connect("),
    ("a.py", "two()", "second = sqlite3.connect("),
    ("a.py", "K.meth()", "self.h = sqlite3.connect("),
]


def _cite(path, fn, anchor):
    return f"`{path}` `{fn}` `{anchor}`"


def _cites_run(tmp_path, files, cites):
    return _run(_make_repo(tmp_path, files, [_cite(*c) for c in cites]))


def _with(i, cite):
    """GOOD_A with row i (0-based) replaced."""
    out = list(GOOD_A)
    out[i] = cite
    return out


def test_a1_unique_anchor_in_named_function_on_a_connect_line_passes(tmp_path):
    r = _cites_run(tmp_path, {"a.py": SRC_A}, GOOD_A)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a1b_module_level_and_non_python_citations_pass(tmp_path):
    files = {
        "a.py": "import sqlite3\nm = sqlite3.connect(p)\n",
        "d.md": "text\n`s = sqlite3.connect(x)` in a doc\n",
    }
    cites = [("a.py", "<module>", "m = sqlite3.connect("), ("d.md", "—", "s = sqlite3.connect(")]
    r = _cites_run(tmp_path, files, cites)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a2_anchor_absent_in_function_names_row_file_function_anchor(tmp_path):
    r = _cites_run(
        tmp_path, {"a.py": SRC_A}, _with(1, ("a.py", "two()", "nothere = sqlite3.connect("))
    )
    assert r.returncode == 1
    assert "row 2: a.py two(): anchor not found: `nothere = sqlite3.connect(`" in r.stdout
    assert "row 1" not in r.stdout and "row 3" not in r.stdout


def test_a3_anchor_twice_in_function_is_ambiguous(tmp_path):
    src = "import sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\n    c = sqlite3.connect(q)\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "f()", "c = sqlite3.connect(")])
    assert r.returncode == 1
    assert "row 1: a.py f(): ambiguous anchor (2 occurrences, 1 row): `c = sqlite3.connect(`" in r.stdout


def test_a4_anchor_unique_in_file_but_outside_function_fails(tmp_path):
    r = _cites_run(
        tmp_path, {"a.py": SRC_A}, _with(0, ("a.py", "one()", "second = sqlite3.connect("))
    )
    assert r.returncode == 1
    assert "row 1: a.py one(): anchor not in function: `second = sqlite3.connect(`" in r.stdout
    assert "row 2" not in r.stdout


def test_a5_named_function_does_not_exist(tmp_path):
    r = _cites_run(
        tmp_path, {"a.py": SRC_A}, _with(0, ("a.py", "nope()", "first = sqlite3.connect("))
    )
    assert r.returncode == 1
    assert "row 1: a.py nope(): function not found" in r.stdout


def test_a5b_method_needs_its_class_qualifier(tmp_path):
    r = _cites_run(
        tmp_path, {"a.py": SRC_A}, _with(2, ("a.py", "meth()", "self.h = sqlite3.connect("))
    )
    assert r.returncode == 1
    assert "row 3: a.py meth(): function not found" in r.stdout


def test_a6_anchor_line_without_connect_fails(tmp_path):
    # `return first` is unique in one() but is not a connect line: a row must
    # not be anchorable to an arbitrary unique string.
    r = _cites_run(tmp_path, {"a.py": SRC_A}, _with(0, ("a.py", "one()", "return first")))
    assert r.returncode == 1
    assert "row 1: a.py one(): anchor line has no sqlite3.connect(: `return first`" in r.stdout


def test_a7_old_line_number_form_is_rejected(tmp_path):
    text = _ledger([C("a.py", 1)]).replace(C("a.py", 1), "`a.py:1` (fn)")
    assert "`a.py:1` (fn)" in text
    r = _run(_make_repo(tmp_path, {"a.py": L(1)}, [], ledger_text=text))
    assert r.returncode == 1
    assert "row 1: line-number citation `a.py:1`" in r.stdout


def test_a7b_cell_not_in_the_three_part_form_fails(tmp_path):
    text = _ledger([C("a.py", 1)]).replace(C("a.py", 1), "`a.py` (fn)")
    r = _run(_make_repo(tmp_path, {"a.py": L(1)}, [], ledger_text=text))
    assert r.returncode == 1
    assert "row 1: citation is not `path` `function()` `anchor`" in r.stdout


def test_a8_line_drift_above_cited_line_stays_green(tmp_path):
    files = {"a.py": SRC_A, "b.py": L(1)}
    root = _make_repo(
        tmp_path, files, [_cite(*c) for c in GOOD_A + [("b.py", "<module>", "c1 = sqlite3.connect(")]]
    )
    assert _run(root).returncode == 0
    a = root / "src" / "engram" / "a.py"
    a.write_text("import os\n" * 37 + a.read_text(encoding="utf-8"), encoding="utf-8")
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a9_unparseable_python_file_fails_not_skipped(tmp_path):
    r = _cites_run(
        tmp_path,
        {"a.py": "def (:\nc = sqlite3.connect(p)\n"},
        [("a.py", "f()", "c = sqlite3.connect(")],
    )
    assert r.returncode == 1
    assert "row 1: a.py: could not parse as Python" in r.stdout


def test_a9b_python_shebang_script_without_suffix_is_parsed_as_python(tmp_path):
    src = "#!/usr/bin/env python3\nimport sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\n"
    ok = _cites_run(tmp_path / "ok", {"tool": src}, [("tool", "f()", "c = sqlite3.connect(")])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = _cites_run(tmp_path / "bad", {"tool": src}, [("tool", "g()", "c = sqlite3.connect(")])
    assert bad.returncode == 1
    assert "row 1: tool g(): function not found" in bad.stdout


def test_a9c_function_slot_must_match_file_kind(tmp_path):
    files = {"a.py": SRC_A, "d.md": "s = sqlite3.connect(x)\n"}
    cites = _with(0, ("a.py", "—", "first = sqlite3.connect(")) + [
        ("d.md", "f()", "s = sqlite3.connect(")
    ]
    r = _cites_run(tmp_path, files, cites)
    assert r.returncode == 1
    assert "row 1: a.py —: Python file needs a function or <module>" in r.stdout
    assert "row 4: d.md f(): non-Python file takes — as its function" in r.stdout


def test_a10_per_file_count_still_fails_when_citations_are_valid(tmp_path):
    src = (
        "import sqlite3\ndef f(p):\n    a = sqlite3.connect(p)\n"
        "    b = sqlite3.connect(p)\n    c = sqlite3.connect(p)\n"
    )
    r = _cites_run(
        tmp_path,
        {"a.py": src},
        [("a.py", "f()", "a = sqlite3.connect("), ("a.py", "f()", "b = sqlite3.connect(")],
    )
    assert r.returncode == 1
    assert "a.py: ledger 2 rows, tree 3 lines" in r.stdout
    assert "anchor" not in r.stdout


def test_a11_real_ledger_citations_all_verify():
    r = _run(REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "line-number citation" not in r.stdout and "anchor" not in r.stdout


# ---- shared anchors: N rows citing one (path, function, anchor) need N occurrences ----

TWICE = "import sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\n    c = sqlite3.connect(q)\n"
TWICE_CITE = ("a.py", "f()", "c = sqlite3.connect(")


def test_s1_two_rows_sharing_an_anchor_that_occurs_twice_pass(tmp_path):
    r = _cites_run(tmp_path, {"a.py": TWICE}, [TWICE_CITE] * 2)
    assert r.returncode == 0, r.stdout + r.stderr


def test_s2_two_rows_sharing_an_anchor_that_occurs_once_fail_naming_both(tmp_path):
    src = "import sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\n"
    r = _cites_run(tmp_path, {"a.py": src}, [TWICE_CITE] * 2)
    assert r.returncode == 1
    assert "rows 1, 2: a.py f(): anchor cited by 2 rows, found 1: `c = sqlite3.connect(`" in r.stdout


def test_s3_one_row_whose_anchor_occurs_twice_is_ambiguous(tmp_path):
    r = _cites_run(tmp_path, {"a.py": TWICE}, [TWICE_CITE])
    assert r.returncode == 1
    assert "row 1: a.py f(): ambiguous anchor (2 occurrences, 1 row): `c = sqlite3.connect(`" in r.stdout


def test_s4_three_rows_anchor_occurs_twice_fails(tmp_path):
    r = _cites_run(tmp_path, {"a.py": TWICE}, [TWICE_CITE] * 3)
    assert r.returncode == 1
    assert "rows 1, 2, 3: a.py f(): anchor cited by 3 rows, found 2" in r.stdout


# ---- line numbering must match ast (F) and <module> scope (M) ----


def test_f1_form_feed_between_defs_does_not_shift_the_later_function(tmp_path):
    # str.splitlines() also splits on \f, so its numbers drift from ast's.
    src = "import sqlite3\ndef a(p):\n    x = 1\n\x0c\ndef b(p):\n    c = sqlite3.connect(p)\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "b()", "c = sqlite3.connect(")])
    assert r.returncode == 0, r.stdout + r.stderr


def test_f2_drift_must_not_move_a_connect_line_into_the_wrong_function(tmp_path):
    # The connect line is in a(); with drifted numbering it lands inside b()'s
    # ast span, so a row citing b() would wrongly pass.
    src = "import sqlite3\n\x0c\ndef a(p):\n    c = sqlite3.connect(p)\ndef b(p):\n    return 1\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "b()", "c = sqlite3.connect(")])
    assert r.returncode == 1
    assert "row 1: a.py b(): anchor not in function: `c = sqlite3.connect(`" in r.stdout


def test_f3_other_unicode_line_separators_do_not_shift_numbering(tmp_path):
    src = "import sqlite3\n# a\u2028b \x85 \x1c\ndef a(p):\n    c = sqlite3.connect(p)\ndef b(p):\n    return 1\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "a()", "c = sqlite3.connect(")])
    assert r.returncode == 0, r.stdout + r.stderr


def test_m1_module_scope_excludes_defs_with_the_same_anchor(tmp_path):
    src = "import sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\nc = sqlite3.connect(p)\n"
    r = _cites_run(
        tmp_path, {"a.py": src},
        [("a.py", "f()", "c = sqlite3.connect("), ("a.py", "<module>", "c = sqlite3.connect(")],
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_m2_class_body_line_counts_as_module_level(tmp_path):
    src = "import sqlite3\nclass K:\n    c = sqlite3.connect(p)\n    def m(self):\n        return 1\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "<module>", "c = sqlite3.connect(")])
    assert r.returncode == 0, r.stdout + r.stderr


# ---- colleague-review fold: survivors of two mutations ----


def test_t1_out_of_scope_count_in_state_line_must_match_rows(tmp_path):
    c = _tally(["direct"])
    c[OOS] = 5
    r = _run_ledger(tmp_path, _ledger([C("a.py", 1)], state_line=_state_line(c)))
    assert r.returncode == 1
    assert "section 1 state line: out of scope says 5, rows give 0" in r.stdout
    assert "summary sentence" not in r.stdout


def test_t1b_out_of_scope_count_in_summary_sentence_must_match_rows(tmp_path):
    c = _tally(["direct"])
    c[OOS] = 5
    r = _run_ledger(tmp_path, _ledger([C("a.py", 1)], summary=_summary_line(c)))
    assert r.returncode == 1
    assert "section 1 summary sentence: out of scope says 5, rows give 0" in r.stdout
    assert "state line" not in r.stdout


def test_t2_function_defined_twice_cannot_be_cited(tmp_path):
    src = "import sqlite3\ndef f():\n    pass\ndef f():\n    c = sqlite3.connect(p)\n"
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "f()", "c = sqlite3.connect(")])
    assert r.returncode == 1
    assert "row 1: a.py f(): function defined 2 times, cannot pick one" in r.stdout


def test_sa_every_occurrence_of_a_shared_anchor_must_be_a_connect_line(tmp_path):
    # Two rows share the anchor, which occurs exactly twice; the FIRST is a
    # connect line, the SECOND is not (no open paren). Checking only the first
    # occurrence would pass.
    src = (
        "import sqlite3\ndef f(p):\n    c = sqlite3.connect(p)\n"
        "    note = 'c = sqlite3.connect'\n"
    )
    r = _cites_run(tmp_path, {"a.py": src}, [("a.py", "f()", "c = sqlite3.connect")] * 2)
    assert r.returncode == 1
    assert "rows 1, 2: a.py f(): anchor line has no sqlite3.connect(: `c = sqlite3.connect`" in r.stdout
