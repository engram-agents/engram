"""Contract pins for docs/VERSIONING.md and the pointers to it.

The page tells readers of old clones one remedy command, quotes one git error,
and carries a table of the 16 releases made before v0.5.6 with their
``-history-twin`` tags.  Prose does not execute, so these cells fail when the
copies of that text drift apart.  Stdlib + pytest only; no network.  Only C7
calls git, and it skips (never passes unchecked, never fails) when the tags it
needs are not in the clone.

- C1. The remedy command is the same in README.md, README.zh.md, the v0.5.6
  CHANGELOG section and (as two consecutive lines of one code block)
  VERSIONING.md.
- C2. The quoted git error is the same in VERSIONING.md and the v0.5.6
  CHANGELOG section.
- C3. Every pointer to VERSIONING.md resolves to the real file.
- C4. Every same-page ``](#anchor)`` link in VERSIONING.md matches a heading.
- C5. The table is well-formed and lists exactly the 16 releases, each with its
  own twin tag (names, 9 hex characters, distinctness, ranges; not the values).
- C6. The page's "at most two files" bound agrees with the table.
- C7. Where the 16 release tags and the 16 twin commits are present in the
  clone, the table's values match the repository: release commit, date, file
  count, twin commit, the twin's diff against the release, and the
  ``-history-twin`` tag when it exists.  Skips, naming what is missing, if not.

What these cells do NOT pin: the page's advice text.  They pin that the copies
of the command and the error agree, that pointers and anchors resolve, that the
table is well-formed and (C7, where the tags are present) that its values match
the repository.  They do not check, for example, the sentence telling readers
not to merge the two lines, or the one about naming their own commits first, so
a green run does not mean the page's advice is right.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# This file sits at tests/, so the repo root is one level up from its folder.
REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONING = REPO_ROOT / "docs" / "VERSIONING.md"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"

REMEDY = "git fetch origin && git checkout -B main origin/main"
GIT_ERROR = "fatal: Need to specify how to reconcile divergent branches."

# A closed historical list: the 16 releases made before the history change.
# It will never change.
RELEASES = [
    "0.1.0", "0.1.1", "0.1.2", "0.1.3", "0.1.4", "0.2.0", "0.3.0", "0.3.1",
    "0.3.2", "0.4.0", "0.5.0", "0.5.1", "0.5.2", "0.5.3", "0.5.4", "0.5.5",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _changelog_v056_section() -> str:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.startswith("## [v0.5.6]")), None
    )
    assert start is not None, "docs/CHANGELOG.md has no '## [v0.5.6]' heading"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _releases_table() -> list[list[str]]:
    """Data rows of the table whose header begins '| Release | Release commit date |'."""
    lines = VERSIONING.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.startswith("| Release | Release commit date |")),
        None,
    )
    assert start is not None, (
        "docs/VERSIONING.md has no '| Release | Release commit date |' table"
    )
    rows = []
    for line in lines[start + 2:]:  # skip the header and the |---| separator
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


# --- C1 ---------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["README.md", "README.zh.md"])
def test_c1_remedy_one_liner_in_readmes(rel):
    assert REMEDY in _read(rel), f"{rel} lacks the remedy line `{REMEDY}`"


def test_c1_remedy_one_liner_in_changelog_v056_section():
    assert REMEDY in _changelog_v056_section(), (
        f"docs/CHANGELOG.md, section v0.5.6, lacks the remedy line `{REMEDY}`"
    )


def test_c1_remedy_is_two_consecutive_lines_in_one_code_block_in_versioning():
    text = VERSIONING.read_text(encoding="utf-8")
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", text, flags=re.S | re.M)
    wanted = ["git fetch origin", "git checkout -B main origin/main"]
    found = any(
        [l.strip() for l in b.splitlines() if l.strip()][i:i + 2] == wanted
        for b in blocks
        for i in range(len([l for l in b.splitlines() if l.strip()]))
    )
    assert found, (
        "docs/VERSIONING.md has no fenced code block with the lines "
        f"{wanted} one right after the other, in that order"
    )


# --- C2 ---------------------------------------------------------------------

def test_c2_git_error_quoted_in_versioning_and_changelog():
    missing = []
    if GIT_ERROR not in VERSIONING.read_text(encoding="utf-8"):
        missing.append("docs/VERSIONING.md")
    if GIT_ERROR not in _changelog_v056_section():
        missing.append("docs/CHANGELOG.md (section v0.5.6)")
    assert not missing, f"the git error `{GIT_ERROR}` is missing from: {missing}"


# --- C3 ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "README.zh.md",
        "docs/README.md",
        "docs/CHANGELOG.md",
        "docs/internal/RELEASING.md",
    ],
)
def test_c3_pointer_to_versioning_resolves(rel):
    src = REPO_ROOT / rel
    targets = re.findall(r"\]\(([^)\s]+)\)", src.read_text(encoding="utf-8"))
    resolved = {
        (src.parent / t.split("#", 1)[0]).resolve()
        for t in targets
        if t.split("#", 1)[0] and "://" not in t
    }
    assert VERSIONING.resolve() in resolved and VERSIONING.is_file(), (
        f"{rel} has no Markdown link that resolves to docs/VERSIONING.md "
        "(targets are resolved relative to the linking file's folder)"
    )


# --- C4 ---------------------------------------------------------------------

def _slug(heading: str) -> str:
    h = heading.lower().replace("`", "")
    h = re.sub(r"[^a-z0-9 _-]", "", h)
    return h.replace(" ", "-")


def test_c4_slug_rule_example():
    assert _slug("If you cloned before v0.5.6") == "if-you-cloned-before-v056"


def test_c4_same_page_anchors_resolve():
    text = VERSIONING.read_text(encoding="utf-8")
    anchors = re.findall(r"\]\(#([^)\s]+)\)", text)
    assert len(anchors) >= 2, f"expected at least two same-page links, found {anchors}"
    # Headings outside code fences only: '#' lines in fenced blocks are not headings.
    prose = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    slugs = {_slug(m) for m in re.findall(r"^#{1,6}\s+(.+?)\s*$", prose, flags=re.M)}
    broken = [a for a in anchors if a not in slugs]
    assert not broken, f"same-page links in docs/VERSIONING.md with no matching heading: {broken}"


# --- C5 / C6 ----------------------------------------------------------------

def test_c5_table_is_well_formed_and_lists_the_16_releases():
    rows = _releases_table()
    assert len(rows) == 16, f"expected 16 data rows, found {len(rows)}"
    assert [r[0] for r in rows] == RELEASES
    rel_commits, twin_commits = [], []
    for r in rows:
        assert len(r) == 6, f"row {r[0]} has {len(r)} columns, expected 6"
        v = r[0]
        m = re.fullmatch(rf"`v{re.escape(v)}` → `([0-9a-f]{{9}})`", r[2])
        assert m, f"row {v}: release column {r[2]!r} is not `v{v}` → `<9 hex>`"
        rel_commits.append(m.group(1))
        m = re.fullmatch(rf"`v{re.escape(v)}-history-twin` → `([0-9a-f]{{9}})`", r[3])
        assert m, f"row {v}: twin column {r[3]!r} is not `v{v}-history-twin` → `<9 hex>`"
        twin_commits.append(m.group(1))
        assert r[4].isdigit() and int(r[4]) > 0, f"row {v}: files column {r[4]!r}"
        assert r[5].isdigit() and 0 <= int(r[5]) <= 2, f"row {v}: changed column {r[5]!r}"
    assert len(set(twin_commits)) == 16, "twin commits are not pairwise distinct"
    assert len(set(rel_commits)) == 16, "release commits are not pairwise distinct"
    assert not set(twin_commits) & set(rel_commits), "a twin commit equals a release commit"


def test_c6_stated_bound_matches_the_table():
    assert "At most two files per" in VERSIONING.read_text(encoding="utf-8"), (
        "docs/VERSIONING.md no longer states 'At most two files per'"
    )
    changed = [int(r[5]) for r in _releases_table()]
    assert max(changed) <= 2, f"table shows {max(changed)} changed files, page says at most two"


# --- C7 ---------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git against this repo root; local only, never fetches."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def test_c7_table_values_match_git():
    rows = _releases_table()
    # Preconditions: any failure SKIPS, naming what is missing.
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    if _git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        pytest.skip(f"{REPO_ROOT} is not a git checkout")
    tag_commit = {}
    for v in RELEASES:
        r = _git("rev-parse", "--verify", "-q", f"v{v}^{{commit}}")
        if r.returncode != 0:
            pytest.skip(f"release tag v{v} is not in this clone (shallow or tagless checkout)")
        tag_commit[v] = r.stdout.strip()
    twin_commit = {}
    for row in rows:
        v = row[0]
        m = re.fullmatch(r"`v[^`]+-history-twin` → `([0-9a-f]{9})`", row[3])
        if not m:
            pytest.skip(f"row {v}: twin column {row[3]!r} is unreadable (C5 is the cell to read)")
        r = _git("rev-parse", "--verify", "-q", f"{m.group(1)}^{{commit}}")
        twin_commit[v] = (m.group(1), r.stdout.strip() if r.returncode == 0 else None)
    # A clone without the development history has none of the twins: skip.  If
    # some resolve and others do not, the table is wrong: that is a row failure (4).
    if not any(full for _, full in twin_commit.values()):
        pytest.skip(
            "none of the 16 twin commits named in the table is in this clone "
            "(shallow checkout, or a clone without the development history)"
        )

    failures = []
    for row in rows:
        v = row[0]
        tag = f"v{v}"
        # (1) release commit is a prefix of what the tag peels to
        m = re.fullmatch(rf"`v{re.escape(v)}` → `([0-9a-f]{{9}})`", row[2])
        got = m.group(1) if m else row[2]
        if v not in tag_commit or not tag_commit[v].startswith(got):
            failures.append(f"{v} (1) release commit: table {got!r}, {tag} peels to {tag_commit.get(v, '?')[:9]}")
        # (2) date
        date = _git("log", "-1", "--format=%cs", tag).stdout.strip()
        if row[1] != date:
            failures.append(f"{v} (2) date: table {row[1]!r}, git {date!r}")
        # (3) file count
        files = len(_git("ls-tree", "-r", "--name-only", tag).stdout.splitlines())
        if row[4] != str(files):
            failures.append(f"{v} (3) files: table {row[4]!r}, git {files}")
        # (4) the twin value resolves to exactly one commit
        short, full = twin_commit[v]
        if full is None:
            failures.append(f"{v} (4) twin: {short} does not resolve to exactly one commit")
            continue
        # (5) release -> twin diff has only M and A; M count equals the last column
        diff = _git("diff", "--no-renames", "--name-status", tag, full).stdout.splitlines()
        letters = [ln.split("\t", 1)[0] for ln in diff if ln.strip()]
        odd = sorted({x for x in letters if x not in ("M", "A")})
        if odd:
            failures.append(f"{v} (5) diff statuses: expected only M and A, found also {odd}")
        n_m = letters.count("M")
        if row[5] != str(n_m):
            failures.append(f"{v} (5) changed files: table {row[5]!r}, git shows {n_m} M lines")
        # (6) the twin tag, only when it exists in this clone
        r = _git("rev-parse", "--verify", "-q", f"refs/tags/{tag}-history-twin^{{commit}}")
        if r.returncode == 0 and r.stdout.strip() != full:
            failures.append(
                f"{v} (6) twin tag: {tag}-history-twin peels to {r.stdout.strip()[:9]}, table says {short}"
            )
    assert not failures, "table values disagree with the repository:\n" + "\n".join(failures)
