"""Tests for tools/check_stubbed_signatures.py — the stubbed-signature CI gate
(#3118).

Contract-first (this repo's CLAUDE.md "Contract-first tests" convention):
written BEFORE tools/check_stubbed_signatures.py's implementation exists.

Origin: PR #3110 changed `_fetch_threads` / `_fetch_inbox`'s signatures in
src/engram/hooks/claude/engram-forum-prompt-hook.py and moved a POST call off
`urlopen` onto `http.client`. Two test files that stub those exact names by
BARE NAME (monkeypatch.setattr(hook, "_fetch_threads", ...) /
`hook.urllib.request.urlopen`) were never selected by
tools/run_touched_tests.py's import-graph + convention-map heuristic, because
that heuristic resolves by import/path, never by the bare names a stub binds.
Nobody caught it until the full suite ran on the release PR.

The primary test below is the REAL regression fixture: this repo's own
commit `40239f40` (PR #3110), diffed against its own parent `9cc9715`. The
ground-truth numbers it asserts against were independently measured (Aleph,
issue #3118 comment thread) and re-verified here directly against this
checkout — see the inline `git grep` commands in each assertion's comment.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "check_stubbed_signatures.py"

_spec = importlib.util.spec_from_file_location("check_stubbed_signatures", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

main = _mod.main
collect_narrow_names = _mod.collect_narrow_names
collect_wide_hits = _mod.collect_wide_hits
get_diff = _mod.get_diff

# The real #3110 commit pair, from this repo's own history.
PR_3110_PARENT = "9cc971591ed548f77ea8f8594963f8945c952679"
PR_3110_HEAD = "40239f40d2ffa7e46d946257bdd0dc747c29014b"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _run_main(monkeypatch, cwd: Path, argv: list[str], capsys) -> tuple[int, str, str]:
    monkeypatch.chdir(cwd)
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Primary test: the real #3110 regression fixture
# ---------------------------------------------------------------------------


def test_3110_regression_finds_the_three_missed_test_files(monkeypatch, capsys):
    """The three test files that stub #3110's renamed/removed bare names must
    all be in the output.

    Independently verified ground truth (re-run against this checkout):
      git grep -l -E -- "_fetch_threads|_fetch_inbox" 40239f40 -- tests/
        -> tests/test_forum_hook.py, tests/test_forum_hook_injection_bounds.py
      test_forum_hook_publish.py is reached via the wide "urlopen" watchlist
      name's module-scoped fallback (its raw bare-name grep is 20-21 files,
      over the noise threshold).
    """
    rc, out, err = _run_main(
        monkeypatch, REPO_ROOT, ["--base", PR_3110_PARENT, "--head", PR_3110_HEAD], capsys
    )
    assert rc == 0, f"expected exit 0, got {rc}; stderr={err!r}"
    files = set(out.splitlines())
    assert "tests/test_forum_hook.py" in files
    assert "tests/test_forum_hook_injection_bounds.py" in files
    assert "tests/test_forum_hook_publish.py" in files


def test_3110_regression_does_not_dump_the_full_urlopen_list(monkeypatch, capsys):
    """A regression to 'just report every raw urlopen hit' must be caught:
    the real urlopen-in-tests/ list is 20-21 files (measured independently,
    `git grep -l -- "urlopen" 40239f40 -- tests/ | wc -l` -> 21); this tool's
    output must stay well short of that."""
    rc, out, err = _run_main(
        monkeypatch, REPO_ROOT, ["--base", PR_3110_PARENT, "--head", PR_3110_HEAD], capsys
    )
    assert rc == 0
    files = [line for line in out.splitlines() if line]
    assert len(files) <= 15, (
        f"expected a small, targeted file list (<=15), got {len(files)}: {files}"
    )


# ---------------------------------------------------------------------------
# Synthetic fixtures — narrow-name-only case, zero-hits case, error paths
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_narrow_name_only_resolves_exactly_the_stubbing_file(tmp_path, monkeypatch, capsys):
    """A function's signature changes; exactly one of two candidate test
    files stubs it by bare name. Output must contain exactly that one file."""
    repo = _init_repo(tmp_path)

    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "widget.py").write_text(
        "def fetch_widget(url):\n    return None\n"
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_widget_stub.py").write_text(
        'def test_it(monkeypatch):\n'
        '    import src.widget as widget\n'
        '    monkeypatch.setattr(widget, "fetch_widget", lambda url, deadline: None)\n'
    )
    (tests_dir / "test_unrelated.py").write_text(
        "def test_other():\n    assert True\n"
    )
    base = _commit_all(repo, "base")

    # Change the signature (added trailing param) -- the #3110 shape.
    (src_dir / "widget.py").write_text(
        "def fetch_widget(url, deadline):\n    return None\n"
    )
    head = _commit_all(repo, "widen fetch_widget signature")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    files = set(line for line in out.splitlines() if line)
    assert files == {"tests/test_widget_stub.py"}


def test_narrow_name_resolution_excludes_non_py_files(tmp_path, monkeypatch, capsys):
    """A bare name that appears in BOTH a real test .py file AND a non-.py
    file under tests/ (e.g. a README) must resolve to ONLY the .py file.

    Contract: _git_grep_files's pathspec is scoped to TEST_ROOTS but is not
    itself restricted to *.py files within them, so a doc/fixture file that
    happens to contain the matched pattern was included in the returned file
    list and fed straight into `pytest`, which exits 4 ("no tests ran") on a
    non-Python path -- a blocking CI failure on a legitimate PR. Origin:
    Kepler's real #3090 repro (`--base 917d6835 --head face942a`), which
    returns `tests/cascade_scenarios/README.md` alongside real test files."""
    repo = _init_repo(tmp_path)

    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "widget.py").write_text(
        "def fetch_widget(url):\n    return None\n"
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_widget_stub.py").write_text(
        'def test_it(monkeypatch):\n'
        '    import src.widget as widget\n'
        '    monkeypatch.setattr(widget, "fetch_widget", lambda url, deadline: None)\n'
    )
    some_dir = tests_dir / "some_dir"
    some_dir.mkdir()
    (some_dir / "README.md").write_text(
        "See fetch_widget for details on the widget-fetching contract.\n"
    )
    base = _commit_all(repo, "base")

    # Change the signature (added trailing param) -- the #3110 shape.
    (src_dir / "widget.py").write_text(
        "def fetch_widget(url, deadline):\n    return None\n"
    )
    head = _commit_all(repo, "widen fetch_widget signature")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    files = set(line for line in out.splitlines() if line)
    assert files == {"tests/test_widget_stub.py"}, (
        f"expected only the .py stub file, got {files} "
        f"(README-shaped path must be excluded)"
    )


def test_zero_hits_case_is_empty_output_exit_0(tmp_path, monkeypatch, capsys):
    """A diff with no def-signature changes and no watchlisted-symbol
    removals must produce empty output and exit 0 -- NOT an error."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n")
    base = _commit_all(repo, "base")

    (repo / "README.md").write_text("hello again\n")
    head = _commit_all(repo, "unrelated doc change")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    assert out == ""


def test_base_equals_head_is_not_an_error(tmp_path, monkeypatch, capsys):
    """--base == --head (no diff at all) is a valid answer, not an error:
    empty output, exit 0."""
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n")
    head = _commit_all(repo, "base")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", head, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    assert out == ""


def test_invalid_base_ref_is_a_loud_nonzero_error(tmp_path, monkeypatch, capsys):
    """An unresolvable --base ref must exit non-zero with a clear stderr
    message naming the bad ref -- never a silently empty stdout list (that
    reads as 'nothing affected', the exact silent-failure shape this tool
    exists to prevent elsewhere)."""
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n")
    head = _commit_all(repo, "base")

    rc, out, err = _run_main(
        monkeypatch, repo, ["--base", "not-a-real-ref-xyz", "--head", head], capsys
    )
    assert rc != 0
    assert out == ""
    assert "not-a-real-ref-xyz" in err


def test_invalid_head_ref_is_a_loud_nonzero_error(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n")
    base = _commit_all(repo, "base")

    rc, out, err = _run_main(
        monkeypatch, repo, ["--base", base, "--head", "not-a-real-ref-xyz"], capsys
    )
    assert rc != 0
    assert out == ""
    assert "not-a-real-ref-xyz" in err


def test_no_tests_dir_is_a_loud_nonzero_error(tmp_path, monkeypatch, capsys):
    """A caller running from a repo/cwd with no tests/ directory must get a
    loud failure, not a quiet empty pass."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\n")
    base = _commit_all(repo, "base")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", base], capsys)
    assert rc != 0
    assert out == ""
    assert "tests" in err.lower()


# ---------------------------------------------------------------------------
# Unit-level checks on the pure diff-parsing helpers
# ---------------------------------------------------------------------------


def test_collect_narrow_names_matches_added_and_removed_def_lines():
    diff_text = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "-def fetch_widget(url):\n"
        "+def fetch_widget(url, deadline):\n"
    )
    assert collect_narrow_names(diff_text) == {"fetch_widget"}


def test_collect_narrow_names_excludes_added_only_and_removed_only_names():
    """A name appearing on only ONE side (brand-new def, or a def deleted
    with no replacement) is NOT a signature change and must not be
    collected -- see collect_narrow_names' own docstring for why an
    added-or-removed reading explodes on the real #3110 fixture (a new test
    file's generic added-only helper method names like `close`/`read`
    resolve to hundreds of unrelated test files)."""
    diff_text = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "+def brand_new_helper(x):\n"
        "-def only_removed(x):\n"
        "-def fetch_widget(url):\n"
        "+def fetch_widget(url, deadline):\n"
    )
    assert collect_narrow_names(diff_text) == {"fetch_widget"}


def test_collect_narrow_names_excludes_cross_hunk_name_collision():
    """A name appearing as an ADDED line in one hunk and a REMOVED line in a
    SEPARATE hunk of the same file is a coincidental collision, not a
    signature change, and must NOT be collected -- this is the same
    "218 files" cross-hunk-collision failure mode documented at length in
    collect_narrow_names' own docstring (point 2), demonstrated there only
    at the scale of the real 380-file #3110 fixture. This is the small,
    synthetic, two-`@@`-hunk version of that same property, so the
    same-hunk-boundary reset logic is exercised directly rather than only
    indirectly via the large real fixture."""
    diff_text = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def unrelated_before(x):\n"
        "+def read(self, n=-1):\n"
        "@@ -40,2 +40,2 @@\n"
        "-def read(self):\n"
        "+def unrelated_after(x):\n"
    )
    assert collect_narrow_names(diff_text) == set()


def test_collect_wide_hits_only_scans_removed_lines():
    diff_text = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "-    with _ipv4_only_urlopen(req) as resp:\n"
        "+    with http.client.HTTPConnection(host) as conn:\n"
    )
    hits = collect_wide_hits(diff_text)
    assert "urlopen" in hits
    assert hits["urlopen"] == {"src/widget.py"}
    # "http.client" appears only on an ADDED line -- must not be collected.
    assert "http.client" not in hits


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
