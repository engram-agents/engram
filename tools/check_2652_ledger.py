#!/usr/bin/env python3
"""Completeness guard for docs/2652-migration-ledger.md [part of #2652].

The ledger's Section 2 holds one row per line matching `sqlite3\\.connect\\(` in
TRACKED files under `src/engram`. This check compares, PER FILE, the number of
Section-2 rows naming the file with the number of matching lines in it. It is
keyed on counts, not line numbers, so unrelated edits that shift line numbers
do not turn it red.

CITATIONS (#3704). The first cell of each Section-2 row cites its line as
`path` `function()` `anchor`: path relative to src/engram; `Class.method()` for
a method, `<module>` for module level, `-` (an em dash) for a non-Python file;
anchor = a verbatim substring of the cited line, with no backtick or pipe. Each
citation is verified against the working-tree file. Rows are grouped by
(path, function, anchor); for a group of N rows (N=1 usually) the anchor must
occur EXACTLY N times inside the named function (Python: the def's line span
from `ast`; `<module>`: the lines outside every def; non-Python file: the whole
file), each occurrence on a line matching `sqlite3\\.connect\\(`. N rows sharing
an anchor is how byte-identical connect lines in one function are cited; fewer
occurrences than rows ("anchor cited by N rows, found M") or more ("ambiguous
anchor") fails, so two rows can never cite one line. A citation therefore
cannot go stale silently: renaming the function, moving the line out of it,
duplicating it, or editing it away turns the guard red, while inserting lines
above it does not. The old `path:LINE` form is rejected ("line-number
citation"). A cited file that is a `.py` or has a python shebang must parse; a
parse failure fails loudly, never skips.

It also derives the per-state tally (owner, direct, behind-flag, flag-on,
retired, out of scope) from the Section-2 rows, reading the `state` column BY
HEADER NAME, and compares it with the two places Section 1 states it: the bold
"State of the N live-store lines" sentence (which must also sum to N) and the
`- **state:**` line. A state value outside the known set fails, naming the row.

What this does NOT check: a row's other contents (the `target`, `access`,
`kind`, `component` and `macOS` columns; the state column only for membership
in the known set and for the tally). It does not check line numbers in prose
outside the Section-2 first cell. A citation of an outer function also passes
when the anchor sits in a def nested inside it (an `outer()` citation covers its
closures); no current row relies on this. Line numbers follow `ast`: only
\\r\\n, \\r and \\n end a line. A same-file swap (one connect line removed
and another added in the SAME function of the same file) leaves every count
and citation valid and so passes by design.

Exit 0 when consistent. Exit 1 with one `file: ledger N rows, tree M lines`
line per differing file and a remedy line; also exit 1 ("could not parse") when
Section 2 yields no rows, so zero parsed rows can never read as consistent;
exit 1 with `section 1 <line>: <state> says N, rows give M` per disagreeing
tally, `row N: unknown state '<value>'` per unknown state, "could not parse
Section 2 states" when no row carries a recognised live-store state, and
"could not parse section 1 tally" when either tally line is missing or lacks a
number; exit 1 with one `row N: ...` line per bad citation; and
exit 1 ("could not read <path>: <error>", one per file) when a tracked file
cannot be read, so an unread file can never read as consistent. Outside a
checkout it exits 1 with the single line "not a git checkout: <root>".

Usage: check_2652_ledger.py [--root DIR]   (DIR defaults to the repo root)
"""

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

LEDGER = "docs/2652-migration-ledger.md"
SRC = "src/engram"
CONNECT = re.compile(r"sqlite3\.connect\(")
# A Section-2 row: `| 12 | `path` `function()` `anchor` | ...`
ROW = re.compile(r"^\|\s*\d+\s*\|\s*`")
CITE = re.compile(r"^`([^`]+)`\s+`([^`]+)`\s+`([^`]+)`$")
OLD_CITE = re.compile(r"^`([^`]+:\d+)`")
MODULE = "<module>"
NO_FUNC = "—"
LIVE_STATES = ("owner", "direct", "behind-flag", "flag-on", "retired")
OUT_OF_SCOPE = "out of scope"
ALL_STATES = LIVE_STATES + (OUT_OF_SCOPE,)
STATE_LINE = re.compile(r"^\s*-\s+\*\*state:\*\*(.*)$")
SUMMARY_LINE = re.compile(r"State of the\s+(\d+)\s+live-store lines")
BOLD_STATE = re.compile(r"\*\*(owner|direct|behind-flag|flag-on|retired)\s+(\d+)\*\*")
PLAIN_STATE = re.compile(r"(owner|direct|behind-flag|flag-on|retired|out of scope)\s+(\d+)")
OTHER_OOS = re.compile(r"The other\s+(\d+)\s+lines are out of scope")
SECTION2 = re.compile(r"^##\s+2\.")
ANY_SECTION = re.compile(r"^##\s")

TALLY_REMEDY = (
    "make the Section-1 tally lines in docs/2652-migration-ledger.md equal "
    "the counts of the Section-2 `state` column (or fix the row's state)"
)
REMEDY = (
    "add/remove a Section-2 row in docs/2652-migration-ledger.md "
    "for each connect line you added/removed"
)
CITE_REMEDY = (
    "cite each row as `path` `function()` `anchor` in docs/2652-migration-ledger.md "
    "(anchor: a substring of the connect line, unique inside the function)"
)


def _lines(text):
    """Split into lines the way `ast` numbers them: only \\r\\n, \\r and \\n end a
    line. str.splitlines() also splits on \\f, \\x0b, \\x1c-\\x1e, \\x85, U+2028 and
    U+2029, which would drift every number after one of those characters."""
    parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def section2_rows(text):
    """(num, first cell) for every Section-2 row, or None when Section 2 is absent."""
    rows = []
    in_section = seen_section = False
    for line in _lines(text):
        if SECTION2.match(line):
            in_section = seen_section = True
            continue
        if in_section and ANY_SECTION.match(line):
            break
        if in_section and ROW.match(line):
            cells = _cells(line)
            rows.append((cells[0], cells[1]))
    return rows if seen_section else None


def parse_citation(cell):
    """('ok', (path, function, anchor)) | ('old', cited) | ('bad', None)."""
    m = CITE.match(cell)
    if m:
        return "ok", m.groups()
    m = OLD_CITE.match(cell)
    if m:
        return "old", m.group(1)
    return "bad", None


def ledger_counts(text):
    """Rows per file (path relative to src/engram) in Section 2, or None.
    A row whose cell has no recognisable path counts toward no file (its
    citation problem is reported separately)."""
    rows = section2_rows(text)
    counts = Counter()
    for _num, cell in rows or []:
        kind, val = parse_citation(cell)
        if kind == "ok":
            counts[val[0]] += 1
        elif kind == "old":
            counts[val.rsplit(":", 1)[0]] += 1
    if not rows:
        return None
    return counts


def row_states(text):
    """(states, problems): `states` is a list of (row number, state value) for
    every Section-2 row, located by the `state` header cell. `problems` holds
    the loud-failure lines."""
    states, problems = [], []
    in_section = False
    idx = ncols = None
    for line in _lines(text):
        if SECTION2.match(line):
            in_section = True
            continue
        if in_section and ANY_SECTION.match(line):
            break
        if not in_section:
            continue
        if idx is None and line.lstrip().startswith("|"):
            cells = [c.lower() for c in _cells(line)]
            if "state" in cells:
                idx, ncols = cells.index("state"), len(cells)
            continue
        if ROW.match(line):
            cells = _cells(line)
            num = cells[0]
            if idx is None:
                problems.append("could not parse Section 2 states: no `state` column in the table header")
                return [], problems
            if len(cells) != ncols:
                problems.append(f"row {num}: has {len(cells)} cells, header has {ncols}")
                continue
            states.append((num, cells[idx]))
    if idx is None:
        problems.append("could not parse Section 2 states: no `state` column in the table header")
    return states, problems


def derived_tally(states):
    """Return (tally, problems) from (row, value) pairs."""
    tally = Counter()
    problems = []
    for num, value in states:
        if value in ALL_STATES:
            tally[value] += 1
        else:
            problems.append(f"row {num}: unknown state '{value}'")
    if not any(tally[k] for k in LIVE_STATES):
        problems.insert(
            0,
            "could not parse Section 2 states: no row carries a recognised "
            "live-store state (" + ", ".join(LIVE_STATES) + ")",
        )
    return tally, problems


def section1_tallies(text):
    """Return ({line name: {key: n}}, problems) for the two Section-1 tally lines."""
    found, problems = {}, []
    before = _lines(text.split("\n## 2.", 1)[0])
    state_lines = [m.group(1) for m in map(STATE_LINE.match, before) if m]
    summary_lines = [l for l in before if SUMMARY_LINE.search(l)]
    if len(state_lines) != 1:
        problems.append(
            "could not parse section 1 tally: no `- **state:**` line"
            if not state_lines
            else "could not parse section 1 tally: more than one `- **state:**` line"
        )
    else:
        vals = {k: int(n) for k, n in PLAIN_STATE.findall(state_lines[0])}
        missing = [k for k in ALL_STATES if k not in vals]
        if missing:
            problems.append(
                "could not parse section 1 tally: `- **state:**` line lacks "
                + ", ".join(missing)
            )
        else:
            found["state line"] = vals
    if len(summary_lines) != 1:
        problems.append(
            "could not parse section 1 tally: no summary sentence ('State of the N live-store lines')"
            if not summary_lines
            else "could not parse section 1 tally: more than one summary sentence"
        )
    else:
        line = summary_lines[0]
        vals = {k: int(n) for k, n in BOLD_STATE.findall(line)}
        other = OTHER_OOS.search(line)
        if other:
            vals[OUT_OF_SCOPE] = int(other.group(1))
        vals["total"] = int(SUMMARY_LINE.search(line).group(1))
        missing = [k for k in ALL_STATES if k not in vals]
        if missing:
            problems.append(
                "could not parse section 1 tally: summary sentence lacks "
                + ", ".join(missing)
            )
        else:
            found["summary sentence"] = vals
    return found, problems


def tally_problems(text):
    states, problems = row_states(text)
    if problems and any(p.startswith("could not parse") for p in problems):
        return problems
    tally, more = derived_tally(states)
    problems += more
    found, sec_problems = section1_tallies(text)
    problems += sec_problems
    for name, vals in found.items():
        for key in ALL_STATES:
            if vals[key] != tally[key]:
                problems.append(
                    f"section 1 {name}: {key} says {vals[key]}, rows give {tally[key]}"
                )
        if "total" in vals:
            live = sum(tally[k] for k in LIVE_STATES)
            if vals["total"] != live:
                problems.append(
                    f"section 1 {name}: total says {vals['total']}, rows give {live}"
                )
    return problems


def tree_counts(root):
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", SRC],
        cwd=root, check=True, capture_output=True,
    ).stdout.decode("utf-8", "replace")
    counts = Counter()
    unreadable = []
    for rel in filter(None, out.split("\0")):
        path = Path(root) / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            # Fail loud: an unread file may hold connect lines, and skipping
            # it would let the guard report consistent.
            unreadable.append(f"could not read {rel}: {e}")
            continue
        n = sum(1 for line in _lines(text) if CONNECT.search(line))
        if n:
            counts[rel[len(SRC) + 1:]] = n
    return counts, unreadable


def _is_python(path, lines):
    return path.endswith(".py") or (
        bool(lines) and lines[0].startswith("#!") and "python" in lines[0]
    )


def _def_spans(tree):
    """[(qualified name, first line, last line)] for every def, classes folded
    into the dotted name (`Class.method`, `outer.inner`)."""
    spans = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join(prefix + [child.name])
                spans.append((name, child.lineno, child.end_lineno))
                walk(child, prefix + [child.name])
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + [child.name])
            else:
                walk(child, prefix)

    walk(tree, [])
    return spans


def _occurrences(lines, numbers, anchor):
    """[(line number, count)] of non-overlapping `anchor` hits on the given 1-based lines."""
    return [(n, lines[n - 1].count(anchor)) for n in numbers if anchor in lines[n - 1]]


def verify_citation(root, nums, path, func, anchor):
    """Return a problem string for the rows sharing one parsed citation, or None
    when it holds: the anchor must occur exactly len(nums) times in the scope,
    every occurrence on a line matching sqlite3.connect(."""
    label = ("row " if len(nums) == 1 else "rows ") + ", ".join(nums)
    who = f"{label}: {path} {func}"
    try:
        text = (Path(root) / SRC / path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"{label}: could not read {path}: {e}"
    lines = _lines(text)
    everywhere = range(1, len(lines) + 1)
    if _is_python(path, lines):
        if func == NO_FUNC:
            return f"{who}: Python file needs a function or {MODULE}"
        try:
            spans = _def_spans(ast.parse(text))
        except (SyntaxError, ValueError) as e:
            return f"{label}: {path}: could not parse as Python: {e}"
        if func == MODULE:
            inside = set()
            for _name, a, b in spans:
                inside.update(range(a, b + 1))
            scope = [n for n in everywhere if n not in inside]
        else:
            want = func[:-2] if func.endswith("()") else func
            hits = [(a, b) for name, a, b in spans if name == want]
            if not hits:
                return f"{who}: function not found"
            if len(hits) > 1:
                return f"{who}: function defined {len(hits)} times, cannot pick one"
            scope = list(range(hits[0][0], hits[0][1] + 1))
    else:
        if func != NO_FUNC:
            return f"{who}: non-Python file takes {NO_FUNC} as its function"
        scope = list(everywhere)
    found = _occurrences(lines, scope, anchor)
    total = sum(c for _n, c in found)
    if total == 0:
        if any(anchor in l for l in lines):
            return f"{who}: anchor not in function: `{anchor}`"
        return f"{who}: anchor not found: `{anchor}`"
    n = len(nums)
    if total > n:
        return (
            f"{who}: ambiguous anchor ({total} occurrences, {n} "
            f"{'row' if n == 1 else 'rows'}): `{anchor}`"
        )
    if total < n:
        return f"{who}: anchor cited by {n} rows, found {total}: `{anchor}`"
    for line_no, _count in found:
        if not CONNECT.search(lines[line_no - 1]):
            return f"{who}: anchor line has no sqlite3.connect(: `{anchor}`"
    return None


def citation_problems(root, text):
    problems = []
    groups = {}  # (path, function, anchor) -> row numbers, in ledger order
    for num, cell in section2_rows(text) or []:
        kind, val = parse_citation(cell)
        if kind == "old":
            problems.append(f"row {num}: line-number citation `{val}`")
        elif kind == "bad":
            problems.append(f"row {num}: citation is not `path` `function()` `anchor`")
        else:
            groups.setdefault(val, []).append(num)
    for key, nums in groups.items():
        problem = verify_citation(root, nums, *key)
        if problem:
            problems.append(problem)
    return problems


def check(root):
    """Return (exit_code, output_lines)."""
    ledger = Path(root) / LEDGER
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError as e:
        return 1, [f"could not parse {LEDGER}: {e}"]
    rows = ledger_counts(text)
    if rows is None:
        return 1, [f"could not parse Section 2 of {LEDGER}: no ledger rows found"]
    tally_bad = tally_problems(text)
    try:
        tree, unreadable = tree_counts(root)
    except subprocess.CalledProcessError:
        return 1, [f"not a git checkout: {root}"]
    if unreadable:
        return 1, unreadable
    bad = [
        f"{f}: ledger {rows.get(f, 0)} rows, tree {tree.get(f, 0)} lines"
        for f in sorted(set(rows) | set(tree))
        if rows.get(f, 0) != tree.get(f, 0)
    ]
    cite_bad = citation_problems(root, text)
    out = list(tally_bad)
    if tally_bad:
        out.append(TALLY_REMEDY)
    if bad:
        out += bad + [REMEDY]
    if cite_bad:
        out += cite_bad + [CITE_REMEDY]
    return (1, out) if out else (0, [])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    code, lines = check(args.root)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
