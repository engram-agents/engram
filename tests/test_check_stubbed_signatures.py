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
commit `becc164a` (PR #3110), diffed against its own parent `a7c2929`. The
ground-truth numbers it asserts against were independently measured (Aleph,
issue #3118 comment thread) and re-verified here directly against this
checkout — see the inline `git grep` commands in each assertion's comment.
"""

from __future__ import annotations

import importlib.util
import re
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
PR_3110_PARENT = "a7c2929fbbec1d34a50690939ca3f5ca913e8b04"
PR_3110_HEAD = "becc164ab9dfb0a6ce92ff4ae134902fac2a2eab"


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
      git grep -l -E -- "_fetch_threads|_fetch_inbox" becc164a -- tests/
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
    `git grep -l -- "urlopen" becc164a -- tests/ | wc -l` -> 21); this tool's
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


# ---------------------------------------------------------------------------
# NARROW-lane noise fallback (#3334) — contract-first tests
#
# Contract (stated in the #3334 spec):
#   collect_narrow_hits(diff) -> {name: {destination files of the hunks where
#     the same-hunk added+removed def pair occurs}}; collect_narrow_names is
#     exactly set(collect_narrow_hits(diff)).
#   resolve_narrow_hits(repo, hits, head): per name, raw word-bounded fixed
#     grep; if len(raw) > NARROW_RESOLVE_THRESHOLD (strictly), discard raw and
#     use the module-scoped pattern built from the hit files (None -> the name
#     contributes nothing); otherwise keep raw unchanged.
# Measured origin: PR #3321's `__init__` / `execute` -> ~255 test files.
# ---------------------------------------------------------------------------


def _hits(diff_text: str):
    return _mod.collect_narrow_hits(diff_text)


def _def_pair_hunk(path: str, name: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        f"-def {name}(x):\n"
        f"+def {name}(x, y):\n"
    )


def test_n1_collect_narrow_hits_maps_name_to_destination_file():
    assert _hits(_def_pair_hunk("src/widget.py", "fetch_widget")) == {
        "fetch_widget": {"src/widget.py"}
    }


def test_n1_two_files_with_same_name_pair_map_to_both_files():
    diff_text = _def_pair_hunk("src/a_mod.py", "execute") + _def_pair_hunk(
        "src/b_mod.py", "execute"
    )
    assert _hits(diff_text) == {"execute": {"src/a_mod.py", "src/b_mod.py"}}


def test_n1_exclusions_unchanged_added_only_removed_only_split_hunks():
    diff_text = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1,2 +1,2 @@\n"
        "+def brand_new_helper(x):\n"
        "-def only_removed(x):\n"
        "-def split_name(x):\n"
        "@@ -40,2 +40,2 @@\n"
        "+def split_name(x, y):\n"
    )
    assert _hits(diff_text) == {}


_EXISTING_FIXTURES = [
    "diff --git a/src/widget.py b/src/widget.py\n"
    "--- a/src/widget.py\n+++ b/src/widget.py\n"
    "-def fetch_widget(url):\n+def fetch_widget(url, deadline):\n",
    "diff --git a/src/widget.py b/src/widget.py\n"
    "--- a/src/widget.py\n+++ b/src/widget.py\n"
    "+def brand_new_helper(x):\n-def only_removed(x):\n"
    "-def fetch_widget(url):\n+def fetch_widget(url, deadline):\n",
    "diff --git a/src/widget.py b/src/widget.py\n"
    "--- a/src/widget.py\n+++ b/src/widget.py\n"
    "@@ -1,2 +1,2 @@\n-def unrelated_before(x):\n+def read(self, n=-1):\n"
    "@@ -40,2 +40,2 @@\n-def read(self):\n+def unrelated_after(x):\n",
]


@pytest.mark.parametrize("diff_text", _EXISTING_FIXTURES)
def test_n2_collect_narrow_names_is_set_of_narrow_hits(diff_text):
    assert collect_narrow_names(diff_text) == set(_hits(diff_text))


def _write_tests_tree(repo: Path, files: dict[str, str]) -> None:
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    for name, content in files.items():
        (tests_dir / name).write_text(content)


def _boundary_repo(tmp_path: Path, n_raw: int) -> tuple[Path, str]:
    """A repo whose tests/ has exactly n_raw files containing the word
    `thing`; exactly one of them (test_ref.py) also references the module
    `mod_alpha`. Returns (repo, head_sha)."""
    repo = _init_repo(tmp_path)
    files = {"test_ref.py": "# mod_alpha\nthing()\n"}
    for i in range(n_raw - 1):
        files[f"test_noise_{i:02d}.py"] = "thing()\n"
    _write_tests_tree(repo, files)
    head = _commit_all(repo, "tests")
    return repo, head


def test_n3_exactly_threshold_raw_files_keeps_raw_result(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    assert len(got) == n
    assert "tests/test_noise_00.py" in got and "tests/test_ref.py" in got


def test_n3_threshold_plus_one_raw_files_switches_to_module_fallback(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n + 1)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    assert got == {"tests/test_ref.py"}


def test_n3_threshold_constant_is_ten():
    assert _mod.NARROW_RESOLVE_THRESHOLD == 10


def test_n4_3321_shape_generic_names_fall_back_to_module_files(
    tmp_path, monkeypatch, capsys
):
    """PR #3321's shape: a rewritten class whose `__init__`/`execute` change
    (added + removed def in one hunk). The fixture rewrites an EXISTING
    module, src/engram/engramd_read_registry.py (a brand-new file cannot
    produce a same-hunk def pair; the real #3321 hits were `__init__` /
    `execute` in src/engram/engramd_owner.py, an existing file that was
    modified). 12+ test files mention those generic names; only the two that
    reference the module by name may be returned. A small-name control
    (`_fetch_threads`, 3 raw files) must still return all 3."""
    repo = _init_repo(tmp_path)
    (repo / "src" / "engram").mkdir(parents=True)
    reg = repo / "src" / "engram" / "engramd_read_registry.py"
    other = repo / "src" / "engram" / "forum_hook.py"
    reg.write_text(
        "class Registry:\n"
        "    def __init__(self):\n        pass\n"
        "    def execute(self, q):\n        return q\n"
    )
    other.write_text("def _fetch_threads(url):\n    return url\n")
    files = {
        "test_engramd_read_registry.py": (
            "from engram import engramd_read_registry\n"
            "def test_a():\n    r = engramd_read_registry.Registry()\n    r.execute(1)\n"
        ),
        "test_scan_registry_path.py": (
            'P = "src/engram/engramd-read-registry.py"\n'
            "class T:\n    def __init__(self):\n        pass\n"
        ),
        "test_fetch_a.py": "stub('_fetch_threads')\n",
        "test_fetch_b.py": "stub('_fetch_threads')\n",
        "test_fetch_c.py": "stub('_fetch_threads')\n",
    }
    for i in range(12):
        files[f"test_generic_{i:02d}.py"] = (
            "class T:\n    def __init__(self):\n        pass\n"
            "    def run(self):\n        self.execute()\n"
        )
    _write_tests_tree(repo, files)
    base = _commit_all(repo, "base")

    reg.write_text(
        "class Registry:\n"
        "    def __init__(self, path):\n        self.path = path\n"
        "    def execute(self, q, limit):\n        return q\n"
    )
    other.write_text("def _fetch_threads(url, deadline):\n    return url\n")
    head = _commit_all(repo, "rewrite")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    assert set(out.splitlines()) == {
        "tests/test_engramd_read_registry.py",
        "tests/test_scan_registry_path.py",
        "tests/test_fetch_a.py",
        "tests/test_fetch_b.py",
        "tests/test_fetch_c.py",
    }


def test_n5_unusable_hit_file_basename_keeps_raw_result(tmp_path):
    """No usable fallback pattern (a basename with no tokens) must NOT
    silently drop the name (F4): over-select by keeping the raw result.
    (WIDE's own behaviour for a None pattern is unchanged: it contributes
    nothing.)"""
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n + 1)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/__.py"}}, head)
    assert len(got) == n + 1
    assert "tests/test_noise_00.py" in got and "tests/test_ref.py" in got


def test_f4_empty_hit_file_set_keeps_raw_result(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n + 1)
    got = _mod.resolve_narrow_hits(repo, {"thing": set()}, head)
    assert len(got) == n + 1


def _size_repo(tmp_path: Path, n_raw: int, n_frag: int) -> tuple[Path, str]:
    """tests/ with n_raw files containing the word `thing` (and not
    `mod_alpha`) and n_frag DISJOINT files containing `mod_alpha` (and not
    `thing`), so raw and fallback results are told apart by content."""
    repo = _init_repo(tmp_path)
    files = {f"test_raw_{i:02d}.py": "thing()\n" for i in range(n_raw)}
    files.update({f"test_frag_{i:02d}.py": "# mod_alpha\n" for i in range(n_frag)})
    _write_tests_tree(repo, files)
    return repo, _commit_all(repo, "tests")


def test_f3_fallback_smaller_than_raw_is_used(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _size_repo(tmp_path, n + 1, 1)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    assert got == {"tests/test_frag_00.py"}


def test_f3_fallback_larger_than_raw_keeps_raw(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _size_repo(tmp_path, n + 1, n + 5)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    assert got == {f"tests/test_raw_{i:02d}.py" for i in range(n + 1)}


def test_f3_fallback_equal_size_keeps_raw_strictly_fewer_required(tmp_path):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _size_repo(tmp_path, n + 1, n + 1)
    got = _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    assert got == {f"tests/test_raw_{i:02d}.py" for i in range(n + 1)}


def test_f1_each_name_uses_only_its_own_hit_files_for_the_fallback(
    tmp_path, monkeypatch
):
    """Name A (changed in mod_a.py) and name B (changed in mod_b.py) are both
    over the threshold. A's fallback pattern must be built from mod_a only,
    B's from mod_b only -- never the union of all names' hit files."""
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo = _init_repo(tmp_path)
    files = {
        "test_ref_a.py": "# mod_a\n",
        "test_ref_b.py": "# mod_b\n",
    }
    for i in range(n + 1):
        files[f"test_aa_{i:02d}.py"] = "aa_fn()\n"
        files[f"test_bb_{i:02d}.py"] = "bb_fn()\n"
    _write_tests_tree(repo, files)
    head = _commit_all(repo, "tests")

    patterns: list[str] = []
    real = _mod._git_grep_files

    def spy(repo_root, pattern, rev, *, fixed, word):
        if not fixed:
            patterns.append(pattern)
        return real(repo_root, pattern, rev, fixed=fixed, word=word)

    monkeypatch.setattr(_mod, "_git_grep_files", spy)
    got = _mod.resolve_narrow_hits(
        repo, {"aa_fn": {"src/mod_a.py"}, "bb_fn": {"src/mod_b.py"}}, head
    )
    assert got == {"tests/test_ref_a.py", "tests/test_ref_b.py"}
    # Per-name, not a union: one pattern per name, each naming exactly one of
    # the two modules and containing no alternation.
    assert len(patterns) == 2
    for pattern in patterns:
        assert "|" not in pattern
        assert ("mod[-_]a" in pattern) != ("mod[-_]b" in pattern)


def test_f2_headerless_hunk_keeps_name_with_empty_file_set():
    diff_text = (
        "@@ -1,2 +1,2 @@\n"
        "-def foo(x):\n"
        "+def foo(x, y):\n"
    )
    assert collect_narrow_names(diff_text) == {"foo"}
    assert _hits(diff_text) == {"foo": set()}


def test_f5_quoted_non_ascii_header_does_not_attribute_to_neighbour():
    quoted = '"a/src/m\\303\\263dulo_\\303\\251.py" "b/src/m\\303\\263dulo_\\303\\251.py"'
    diff_text = (
        _def_pair_hunk("src/first.py", "first_fn")
        + f"diff --git {quoted}\n"
        + "--- a/x\n+++ b/x\n"
        + "@@ -1,2 +1,2 @@\n-def mid_fn(x):\n+def mid_fn(x, y):\n"
        + _def_pair_hunk("src/last.py", "last_fn")
    )
    hits = _hits(diff_text)
    assert hits["mid_fn"] == set()
    assert hits["first_fn"] == {"src/first.py"}
    assert hits["last_fn"] == {"src/last.py"}


def test_n6_union_is_sorted_and_deduplicated(tmp_path, monkeypatch, capsys):
    """Narrow fallback + raw narrow + wide fallback results merge into one
    sorted, de-duplicated list (test_both.py is reached by all three)."""
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    hookmod = repo / "src" / "hookmod.py"
    wideuser = repo / "src" / "wideuser.py"
    hookmod.write_text(
        "def generic(a):\n    return a\n"
        "def small_name(a):\n    return a\n"
    )
    wideuser.write_text("def f(req):\n    return urlopen(req)\n")
    files = {
        "test_both.py": "# hookmod wideuser\ngeneric()\nurlopen()\nsmall_name()\n",
        "test_small_two.py": "small_name()\n",
    }
    for i in range(11):
        files[f"test_noise_{i:02d}.py"] = "generic()\nurlopen()\n"
    _write_tests_tree(repo, files)
    base = _commit_all(repo, "base")

    hookmod.write_text(
        "def generic(a, b):\n    return a\n"
        "def small_name(a, b):\n    return a\n"
    )
    wideuser.write_text("def f(req):\n    return http_call(req)\n")
    head = _commit_all(repo, "change")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0, f"stderr={err!r}"
    lines = out.splitlines()
    assert lines == ["tests/test_both.py", "tests/test_small_two.py"]
    assert lines == sorted(set(lines))


_NARROW_LINE = r"check_stubbed_signatures: narrow '{name}': "


def test_diag_fallback_taken_writes_one_stderr_line(tmp_path, capsys):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _size_repo(tmp_path, n + 1, 1)
    _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert re.fullmatch(
        _NARROW_LINE.format(name="thing")
        + rf"{n + 1} raw > {n}, module fallback -> 1 files\n",
        cap.err,
    ), cap.err


def test_diag_fallback_rejected_writes_not_fewer_line(tmp_path, capsys):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _size_repo(tmp_path, n + 1, n + 5)
    _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert re.fullmatch(
        _NARROW_LINE.format(name="thing")
        + rf"{n + 1} raw > {n}, module fallback would select {n + 5} "
        r"\(not fewer\), kept raw\n",
        cap.err,
    ), cap.err


@pytest.mark.parametrize("hit_files", [{"src/__.py"}, set()])
def test_diag_no_usable_pattern_writes_line(tmp_path, capsys, hit_files):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n + 1)
    _mod.resolve_narrow_hits(repo, {"thing": hit_files}, head)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert re.fullmatch(
        _NARROW_LINE.format(name="thing")
        + rf"{n + 1} raw > {n}, no usable fallback pattern, kept raw\n",
        cap.err,
    ), cap.err


def test_diag_at_or_below_threshold_writes_nothing(tmp_path, capsys):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo, head = _boundary_repo(tmp_path, n)
    _mod.resolve_narrow_hits(repo, {"thing": {"src/mod_alpha.py"}}, head)
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


def test_diag_two_over_threshold_names_write_one_line_each(tmp_path, capsys):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo = _init_repo(tmp_path)
    files = {"test_ref_a.py": "# mod_a\n", "test_ref_b.py": "# mod_b\n"}
    for i in range(n + 1):
        files[f"test_aa_{i:02d}.py"] = "aa_fn()\n"
        files[f"test_bb_{i:02d}.py"] = "bb_fn()\n"
    _write_tests_tree(repo, files)
    head = _commit_all(repo, "tests")
    _mod.resolve_narrow_hits(
        repo, {"aa_fn": {"src/mod_a.py"}, "bb_fn": {"src/mod_b.py"}}, head
    )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(_NARROW_LINE.format(name="aa_fn"))
    assert lines[1].startswith(_NARROW_LINE.format(name="bb_fn"))


def test_diag_main_stdout_is_only_paths_diagnostics_on_stderr(
    tmp_path, monkeypatch, capsys
):
    n = _mod.NARROW_RESOLVE_THRESHOLD
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    mod = repo / "src" / "hookmod.py"
    mod.write_text("def generic(a):\n    return a\n")
    files = {"test_ref.py": "# hookmod\ngeneric()\n"}
    for i in range(n + 1):
        files[f"test_noise_{i:02d}.py"] = "generic()\n"
    _write_tests_tree(repo, files)
    base = _commit_all(repo, "base")
    mod.write_text("def generic(a, b):\n    return a\n")
    head = _commit_all(repo, "change")

    rc, out, err = _run_main(monkeypatch, repo, ["--base", base, "--head", head], capsys)
    assert rc == 0
    assert out == "tests/test_ref.py\n"
    assert err.startswith(_NARROW_LINE.format(name="generic"))
    assert err.count("\n") == 1


def test_n7_module_docstring_no_longer_claims_narrow_has_no_threshold():
    """The old sentence ("a large raw hit count is itself meaningful signal")
    was the wrong claim, falsified by PR #3321. The phrase lived in the
    comment above WIDE_RESOLVE_THRESHOLD, not the docstring, so check the
    whole source file too; and the docstring must now name the new rule."""
    phrase = "Narrow names have no such threshold"
    assert phrase not in _mod.__doc__
    assert phrase not in TOOL_PATH.read_text()
    assert "NARROW_RESOLVE_THRESHOLD" in _mod.__doc__


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
