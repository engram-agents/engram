"""Contract cells for the run_touched_tests selection fixes (part of #4358).

Written BEFORE the implementation (contract-first). The contract, by cell group:

  C1  --base is required for the selection modes (not for --full or the audits);
      --head is new; with --head the changed files are exactly
      `git diff --name-only base...head`, without it today's behaviour (committed
      diff plus uncommitted working-tree files) is kept.
  C2  an unknown base or head refuses: one stderr line naming the ref, no
      selection, no JSON, exit 2 (both modes).
  C3  the guard set is read from the policy document (guard_set(repo_root)):
      anchor missing or no test paths -> exit 4; any named path absent from the
      tree -> every missing path named, exit 3; the real document's guard tests
      are exactly the 21 pinned here.
  C4  no changed files -> exit 5, the guard set is not printed as a selection.
  C5  a non-full selection equal to the whole tests/ universe -> exit 6; the
      full-suite triggers stop with exit 7 (E1-E5 at the end of this file).
  C6  layer 1 identity is (import root, stem).
  C7  name rule (a) whole-name basename + (b') parent directory word when the
      basename is not unique.
  C8  a changed test file selects itself, in either universe.
  C9  the forum tests are a separate universe, reported separately.
  C10 the policy's tool checks are printed as commands.
  C11 the help text points at PR-MERGE-POLICY, not "convergence gate".
  C12 today's case on the real tree (needs commits 60d7b811 and 8488d0d4).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"
GUARD_MODULE_PATH = REPO_ROOT / "src" / "engram" / "tools" / "policy_guard_list.py"

_spec = importlib.util.spec_from_file_location("run_touched_tests_4358b", str(RUNNER_PATH))
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)

# The 21 guard tests of PR-MERGE-POLICY "Owner merge" item 4 at the time this
# contract was written. A policy edit that drops one goes red in
# test_c3_real_document_guard_tests_are_exactly_the_21.
GUARD_21 = {
    "tests/test_syspath_shadow_guard.py",
    "tests/test_tiers_manifest.py",
    "tests/test_run_touched_tests.py",
    "tests/test_ci_wiring_guard.py",
    "tests/test_engramd_read_scan.py",
    "tests/test_atomic_writer_modes_4013.py",
    "tests/test_truncating_writes_ratchet_4380.py",
    "tests/test_forum_process_spawn_ratchet_3518.py",
    "tests/test_no_db_creation_before_bootstrap_4107.py",
    "tests/test_no_unbounded_upward_walks_3983.py",
    "tests/test_prose_home_sweep_3863.py",
    "tests/test_prose_tail_6c_venv.py",
    "tests/test_raw_home_read_gate_3863.py",
    "tests/test_home_path_gate.py",
    "tests/test_lesson_pattern_audit_shipped_3941.py",
    "tests/test_docs_layout_guard.py",
    "tests/test_policy_guard_list_4487.py",
    "tests/test_metadata_parse_drift_guard_3413.py",
    "tests/test_json_extract_metadata_guard_3485.py",
    "tests/test_no_retired_cascade_store_asserts.py",
    "tests/test_forum_redirect_origin_1459.py",
}

_CONFTEST_A_ROOT = textwrap.dedent(
    """\
    import os
    import sys
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(REPO_ROOT, "a"))
    """
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    base = {"pytest.ini": "[pytest]\n", "tests/__init__.py": "", "tests/test_zz_other.py": "z = 1\n"}
    base.update(files)
    _write(tmp_path, base)
    return str(tmp_path)


def _select(changed, repo, guard=(), tools=(), detail=None):
    with mock.patch.object(_runner, "guard_set", return_value=(list(guard), list(tools))):
        return _runner.select_tests(changed, repo, detail=detail)


def _policy(item4_body: str) -> str:
    return (
        "## Owner merge (title)\n"
        "3. **Third item**\n"
        "4. **The seat's own run, on a scratch merge**\n"
        f"{item4_body}\n"
        "5. **The PR's changed or added tests**\n"
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout


def _git_repo(tmp_path: Path) -> tuple[str, str, str, str]:
    """A real git repo: base commit A, then B (adds f1), then C (adds f2).

    Returns (repo, A, B, C) with an untracked file `u.txt` left in the tree.
    """
    repo = _repo(tmp_path, {
        "tests/test_guard.py": "g = 1\n",
        "tests/test_names_f1.py": 'N = "f1.txt"\n',
        "base.txt": "base\n",
    })
    _git(repo, "init", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "A")
    a = _git(repo, "rev-parse", "HEAD").strip()
    (tmp_path / "f1.txt").write_text("1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "B")
    b = _git(repo, "rev-parse", "HEAD").strip()
    (tmp_path / "f2.txt").write_text("2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "C")
    c = _git(repo, "rev-parse", "HEAD").strip()
    (tmp_path / "u.txt").write_text("u\n")
    return repo, a, b, c


def _main(argv, capsys):
    rc = _runner.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


GUARD_PATCH = ("tests/test_guard.py",)


# ---------------------------------------------------------------------------
# C1 — --base required for selection; --head; changed-file definition
# ---------------------------------------------------------------------------


def test_c1_base_is_required_for_selection_modes(tmp_path, capsys):
    repo = _repo(tmp_path, {})
    for extra in ([], ["--json"], ["--dry-run"]):
        with pytest.raises(SystemExit) as exc:
            _runner.main([*extra, "--repo-root", repo])
        assert exc.value.code == 2
    assert "--base" in capsys.readouterr().err


def test_c1_base_not_required_for_full_or_audits(tmp_path, capsys):
    repo = _repo(tmp_path, {})
    rc, out, _err = _main(["--json", "--full", "--repo-root", repo], capsys)
    assert rc == 0 and json.loads(out)["full"] is True
    rc, _out, _err = _main(["--audit-imports", "--repo-root", repo], capsys)
    assert rc == 0
    rc, _out, _err = _main(["--audit-patches", "--repo-root", repo], capsys)
    assert rc == 0


def test_c1_head_given_is_the_pure_three_dot_diff(tmp_path):
    repo, a, b, _c = _git_repo(tmp_path)
    # base...B is only f1.txt: not C's f2.txt, not the untracked u.txt
    assert _runner._changed_files(a, repo, head=b) == ["f1.txt"]


def test_c1_head_not_given_keeps_committed_diff_plus_working_tree(tmp_path):
    repo, a, _b, _c = _git_repo(tmp_path)
    got = _runner._changed_files(a, repo)
    assert got == ["f1.txt", "f2.txt", "u.txt"]


def test_c1_head_flag_reaches_the_diff_in_json_mode(tmp_path, capsys):
    repo, a, b, _c = _git_repo(tmp_path)
    with mock.patch.object(_runner, "guard_set", return_value=(list(GUARD_PATCH), [])):
        rc, out, _e = _main(["--json", "--base", a, "--head", b, "--repo-root", repo], capsys)
    assert rc == 0
    assert json.loads(out)["changed_files"] == ["f1.txt"]


# ---------------------------------------------------------------------------
# C2 — unknown ref refuses, exit 2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [["--json"], ["--dry-run"]], ids=["json", "human"])
@pytest.mark.parametrize("which", ["base", "head"])
def test_c2_unknown_ref_refuses_exit_2(tmp_path, capsys, mode, which):
    repo, a, b, _c = _git_repo(tmp_path)
    bad = "no-such-ref-4358"
    base, head = (bad, b) if which == "base" else (a, bad)
    with mock.patch.object(_runner, "guard_set", return_value=(list(GUARD_PATCH), [])):
        rc, out, err = _main(
            [*mode, "--base", base, "--head", head, "--repo-root", repo], capsys
        )
    assert rc == 2
    assert bad in err
    assert len([ln for ln in err.splitlines() if ln.strip()]) == 1
    assert "tests/test_guard.py" not in out  # no selection printed
    assert "{" not in out  # no JSON object


# ---------------------------------------------------------------------------
# C3 — guard set from the policy document
# ---------------------------------------------------------------------------


def test_c3_the_hand_copied_constant_is_gone():
    assert not hasattr(_runner, "GUARD_SET_TESTS")


def test_c3_real_document_guard_tests_are_exactly_the_21():
    tests, tools = _runner.guard_set(str(REPO_ROOT))
    assert set(tests) == GUARD_21
    assert len(tests) == len(set(tests)) == 21
    assert "tools/scan-leaks.py" in tools and "tools/test_id_set_check.py" in tools
    assert all(t.startswith("tools/") for t in tools)


def _policy_repo(tmp_path: Path, item4_body: str, files: dict[str, str] | None = None) -> str:
    base = {"docs/internal/PR-MERGE-POLICY.md": _policy(item4_body)}
    base.update(files or {})
    return _repo(tmp_path, base)


def test_c3_guard_set_splits_tests_and_tools(tmp_path):
    repo = _policy_repo(
        tmp_path,
        "   `tests/test_a.py` and `tools/scan-leaks.py`",
        {"tests/test_a.py": "a = 1\n", "tools/scan-leaks.py": "x = 1\n"},
    )
    assert _runner.guard_set(repo) == (["tests/test_a.py"], ["tools/scan-leaks.py"])


def test_c3_missing_document_names_the_anchor_exit_4(tmp_path, capsys):
    repo = _repo(tmp_path, {})
    with pytest.raises(_runner.GuardSetError) as exc:
        _runner.guard_set(repo)
    assert exc.value.code == 4
    assert "PR-MERGE-POLICY.md" in str(exc.value)
    with mock.patch.object(_runner, "_changed_files", return_value=["notes.md"]):
        rc, out, err = _main(["--dry-run", "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 4
    assert "PR-MERGE-POLICY.md" in err


def test_c3_missing_item4_anchor_names_the_anchor_exit_4(tmp_path):
    repo = _repo(tmp_path, {"docs/internal/PR-MERGE-POLICY.md": "## Owner merge\n3. **x**\n"})
    with pytest.raises(_runner.GuardSetError) as exc:
        _runner.guard_set(repo)
    assert exc.value.code == 4
    assert "item-4 start" in str(exc.value)


def test_c3_item4_with_only_tool_paths_is_exit_4(tmp_path):
    repo = _policy_repo(tmp_path, "   `tools/scan-leaks.py`", {"tools/scan-leaks.py": "x = 1\n"})
    with pytest.raises(_runner.GuardSetError) as exc:
        _runner.guard_set(repo)
    assert exc.value.code == 4
    assert "tests/" in str(exc.value)


def test_c3_every_missing_path_is_named_exit_3(tmp_path, capsys):
    repo = _policy_repo(
        tmp_path,
        "   `tests/test_a.py` `tests/test_gone1.py` `tests/test_gone2.py` `tools/gone3.py`",
        {"tests/test_a.py": "a = 1\n"},
    )
    with pytest.raises(_runner.GuardSetError) as exc:
        _runner.guard_set(repo)
    assert exc.value.code == 3
    for gone in ("tests/test_gone1.py", "tests/test_gone2.py", "tools/gone3.py"):
        assert gone in str(exc.value)
    assert "tests/test_a.py" not in str(exc.value)
    with mock.patch.object(_runner, "_changed_files", return_value=["notes.md"]):
        rc, _out, err = _main(["--dry-run", "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 3
    for gone in ("tests/test_gone1.py", "tests/test_gone2.py", "tools/gone3.py"):
        assert gone in err


# ---------------------------------------------------------------------------
# C4 — no changed files, exit 5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [["--json"], ["--dry-run"]], ids=["json", "human"])
def test_c4_no_changed_files_exit_5_names_base_and_head(tmp_path, capsys, mode):
    repo = _repo(tmp_path, {"tests/test_guard.py": "g = 1\n"})
    with mock.patch.object(_runner, "guard_set", return_value=(list(GUARD_PATCH), [])), \
         mock.patch.object(_runner, "_changed_files", return_value=[]):
        rc, out, err = _main(
            [*mode, "--base", "origin/dev", "--head", "feat", "--repo-root", repo], capsys
        )
    assert rc == 5
    assert "origin/dev" in err and "feat" in err
    assert "tests/test_guard.py" not in out
    assert "{" not in out


# ---------------------------------------------------------------------------
# C5 — whole universe, exit 6; full triggers unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [["--json"], ["--dry-run"]], ids=["json", "human"])
def test_c5_selection_equal_to_the_whole_universe_exit_6(tmp_path, capsys, mode):
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n", "tests/test_b.py": "b = 1\n"})
    every = ["tests/test_a.py", "tests/test_b.py", "tests/test_zz_other.py"]
    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=every):
        rc, out, err = _main([*mode, "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 6
    assert err.strip()
    assert "{" not in out


def test_c5_a_proper_subset_is_not_exit_6(tmp_path, capsys):
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n"})
    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=["tests/test_a.py"]):
        rc, _out, _err = _main(["--dry-run", "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 0


@pytest.mark.parametrize("mode", [["--json"], ["--dry-run"]], ids=["json", "human"])
def test_c5_full_trigger_is_a_named_stop_with_exit_7(tmp_path, capsys, mode):
    # E1/E2 (PR #4848 review): a trigger without --full runs nothing and exits 7.
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n"})
    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=["tests/conftest.py"]):
        rc, out, _err = _main([*mode, "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 7
    assert ("FULL-SUITE TRIGGER" in out) or json.loads(out)["full"] is True


# ---------------------------------------------------------------------------
# C6 — layer 1 identity is (import root, stem)
# ---------------------------------------------------------------------------


def _c6_repo(tmp_path: Path) -> str:
    return _repo(tmp_path, {
        "tests/conftest.py": _CONFTEST_A_ROOT,
        "a/server.py": "A = 1\n",
        "b/server.py": "B = 1\n",
        "tests/test_imp.py": "import server\n",
    })


def test_c6_off_root_same_stem_does_not_select_through_layer_1(tmp_path):
    repo = _c6_repo(tmp_path)
    detail: dict = {}
    selected, is_full, _r, _m = _select(["b/server.py"], repo, detail=detail)
    assert not is_full
    assert "tests/test_imp.py" not in selected
    assert detail["layers"]["l1_import_graph"] == 0


def test_c6_on_root_module_selects_through_layer_1(tmp_path):
    repo = _c6_repo(tmp_path)
    detail: dict = {}
    selected, _f, _r, _m = _select(["a/server.py"], repo, detail=detail)
    assert "tests/test_imp.py" in selected
    assert detail["layers"]["l1_import_graph"] == 1


# ---------------------------------------------------------------------------
# C7 — name rule (a) + (b')
# ---------------------------------------------------------------------------


def test_c7_a_longer_name_ending_in_the_basename_does_not_select(tmp_path):
    repo = _repo(tmp_path, {
        "x/server.py": "S = 1\n",
        "tests/test_v.py": 'NAME = "viz_server.py"\n',
    })
    selected, _f, _r, _m = _select(["x/server.py"], repo)
    assert "tests/test_v.py" not in selected


@pytest.mark.parametrize(
    "text, selects",
    [
        ('P = "server.py"\n', True),
        ("P = root / 'server.py'\n", True),
        ("see server.py, then\n", True),
        ("P = 'a/server.py'\n", True),
        ('P = "my-server.py"\n', False),
        ('P = "viz_server.py"\n', False),
        ('P = "xserver.py"\n', False),
        ('P = ".server.py"\n', False),
        ('P = "server.pyc"\n', False),
        ('P = "server.py_old"\n', False),
        ('P = "server.pyi"\n', False),
    ],
)
def test_c7_whole_name_boundaries_of_rule_a(tmp_path, text, selects):
    repo = _repo(tmp_path, {"x/server.py": "S = 1\n", "tests/test_t.py": text})
    selected, _f, _r, _m = _select(["x/server.py"], repo)
    assert ("tests/test_t.py" in selected) is selects


def _nonunique_repo(tmp_path: Path, extra: dict[str, str]) -> str:
    files = {"alpha/server.py": "S = 1\n", "beta/server.py": "S = 2\n"}
    files.update(extra)
    return _repo(tmp_path, files)


def test_c7_nonunique_basename_selected_when_the_parent_is_named(tmp_path):
    repo = _nonunique_repo(tmp_path, {"tests/test_p.py": 'P = "alpha/server.py"\n'})
    selected, _f, _r, _m = _select(["alpha/server.py"], repo)
    assert "tests/test_p.py" in selected


def test_c7_nonunique_basename_not_selected_when_the_parent_is_absent(tmp_path):
    repo = _nonunique_repo(tmp_path, {"tests/test_q.py": 'P = "beta/server.py"\n'})
    selected, _f, _r, _m = _select(["alpha/server.py"], repo)
    assert "tests/test_q.py" not in selected


def test_c7_parent_named_only_inside_a_variable_path_selects(tmp_path):
    repo = _nonunique_repo(tmp_path, {
        "tests/test_v.py": 'alpha = HERE\nP = alpha / "server.py"\n',
    })
    selected, _f, _r, _m = _select(["alpha/server.py"], repo)
    assert "tests/test_v.py" in selected


def test_c7_parent_starting_with_a_non_word_char_is_matched(tmp_path):
    # `.claude-plugin` starts with "." so a `\b` before it never matches
    repo = _repo(tmp_path, {
        "tools/mods/m1/.claude-plugin/plugin.json": "{}\n",
        "tools/probes/p1/.claude-plugin/plugin.json": "{}\n",
        "tests/test_pl.py": 'P = ".claude-plugin" / "plugin.json"\n',
    })
    selected, _f, _r, _m = _select(["tools/mods/m1/.claude-plugin/plugin.json"], repo)
    assert "tests/test_pl.py" in selected


@pytest.mark.parametrize("text,selects", [
    ('P = "forums"\nN = "server.py"\n', False),
    ('my_forum = 1\nN = "server.py"\n', False),
    ('P = "forum"\nN = "server.py"\n', True),
])
def test_c7_the_parent_word_stays_whole(tmp_path, text, selects):
    repo = _repo(tmp_path, {
        "forum/server.py": "S = 1\n",
        "beta/server.py": "S = 2\n",
        "tests/test_pw.py": text,
    })
    selected, _f, _r, _m = _select(["forum/server.py"], repo)
    assert ("tests/test_pw.py" in selected) is selects


def test_c7_unique_basename_needs_rule_a_alone(tmp_path):
    repo = _repo(tmp_path, {
        "zeta/solo.py": "S = 1\n",
        "tests/test_s.py": 'P = "solo.py"\n',
    })
    selected, _f, _r, _m = _select(["zeta/solo.py"], repo)
    assert "tests/test_s.py" in selected


def test_c7_uniqueness_is_read_from_git_ls_files_when_the_tree_is_a_repo(tmp_path):
    # a tracked sibling with the same basename makes it non-unique; an UNtracked
    # one does not (git ls-files, not a filesystem walk)
    repo = _repo(tmp_path, {
        "alpha/server.py": "S = 1\n",
        "tests/test_q.py": 'P = "server.py"\n',
    })
    _git(repo, "init", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "x")
    _write(tmp_path, {"beta/server.py": "S = 2\n"})  # untracked
    selected, _f, _r, _m = _select(["alpha/server.py"], repo)
    assert "tests/test_q.py" in selected  # unique among tracked files: rule (a) alone
    _git(repo, "add", "beta/server.py")
    selected, _f, _r, _m = _select(["alpha/server.py"], repo)
    assert "tests/test_q.py" not in selected  # now non-unique, parent "alpha" absent


# ---------------------------------------------------------------------------
# C8 — a changed test file selects itself, in either universe
# ---------------------------------------------------------------------------


def test_c8_changed_test_file_selects_itself(tmp_path):
    repo = _repo(tmp_path, {"tests/test_self.py": "s = 1\n"})
    selected, _f, _r, _m = _select(["tests/test_self.py"], repo)
    assert "tests/test_self.py" in selected


def test_c8_changed_forum_test_file_selects_itself_in_the_forum_universe(tmp_path):
    repo = _repo(tmp_path, {"src/forum/tests/test_fself.py": "s = 1\n"})
    forum = _runner.select_forum_tests(["src/forum/tests/test_fself.py"], repo)
    assert forum == ["src/forum/tests/test_fself.py"]


# ---------------------------------------------------------------------------
# C9 — the forum universe, separate
# ---------------------------------------------------------------------------


_FORUM_CONFTEST = textwrap.dedent(
    """\
    import os
    import sys
    FORUM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(FORUM, "mods"))
    """
)


def _forum_repo(tmp_path: Path) -> str:
    return _repo(tmp_path, {
        "src/forum/tests/conftest.py": _FORUM_CONFTEST,
        "src/forum/mods/fmod.py": "F = 1\n",
        "src/forum/server.py": "S = 1\n",
        "src/forum/tests/test_uses_fmod.py": "import fmod\n",
        "src/forum/tests/test_names_server.py": 'P = "server.py"\n',
        "src/forum/tests/test_unrelated.py": "u = 1\n",
        "tests/test_main_names_server.py": 'P = "server.py"\n',
    })


def test_c9_forum_layer_1_uses_the_roots_its_conftest_puts_on_sys_path(tmp_path):
    repo = _forum_repo(tmp_path)
    got = _runner.select_forum_tests(["src/forum/mods/fmod.py"], repo)
    assert got == ["src/forum/tests/test_uses_fmod.py"]


def test_c9_forum_layer_3_name_reference(tmp_path):
    repo = _forum_repo(tmp_path)
    got = _runner.select_forum_tests(["src/forum/server.py"], repo)
    assert got == ["src/forum/tests/test_names_server.py"]


def test_c9_forum_root_falls_back_to_the_forum_dir_without_a_conftest(tmp_path):
    repo = _repo(tmp_path, {
        "src/forum/mod2.py": "M = 1\n",
        "src/forum/tests/test_uses_mod2.py": "import mod2\n",
        "src/forum/tests/test_other.py": "o = 1\n",
    })
    got = _runner.select_forum_tests(["src/forum/mod2.py"], repo)
    assert got == ["src/forum/tests/test_uses_mod2.py"]


def test_c9_forum_files_never_enter_the_tests_selection(tmp_path):
    repo = _forum_repo(tmp_path)
    selected, _f, _r, _m = _select(["src/forum/server.py"], repo)
    assert selected == ["tests/test_main_names_server.py"]
    assert not any(s.startswith("src/forum") for s in selected)


def test_c9_json_reports_the_forum_universe_separately(tmp_path, capsys):
    repo = _forum_repo(tmp_path)
    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=["src/forum/server.py"]):
        rc, out, _e = _main(["--json", "--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 0
    d = json.loads(out)
    assert d["forum_selected"] == ["src/forum/tests/test_names_server.py"]
    assert d["forum_total_test_files"] == 3
    assert d["selected"] == ["tests/test_main_names_server.py"]


def test_c9_human_output_has_a_forum_block_and_the_tests_command_excludes_forum(tmp_path, capsys):
    repo = _forum_repo(tmp_path)
    ran: list = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] == "git":  # the tool's own git reads stay real
            return real_run(cmd, **kw)
        ran.append(cmd)
        return mock.Mock(returncode=0)

    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=["src/forum/server.py"]), \
         mock.patch.object(_runner.subprocess, "run", fake_run):
        rc, out, _e = _main(["--base", "HEAD", "--repo-root", repo], capsys)
    assert rc == 0
    assert "forum tests: run in the forum's own environment" in out
    assert "src/forum/tests/test_names_server.py" in out
    assert len(ran) == 1
    assert not any("src/forum" in str(a) for a in ran[0])
    assert "tests/test_main_names_server.py" in ran[0]


def test_c9_the_old_forum_note_is_replaced(tmp_path, capsys):
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n"})
    with mock.patch.object(_runner, "guard_set", return_value=([], [])), \
         mock.patch.object(_runner, "_changed_files", return_value=["tests/conftest.py"]):
        _rc, out, _e = _main(["--dry-run", "--base", "HEAD", "--repo-root", repo], capsys)
    assert "forum/tests/ NOT included" not in out
    assert "forum tests: run in the forum's own environment" in out


# ---------------------------------------------------------------------------
# C10 — tool checks printed
# ---------------------------------------------------------------------------

_TOOLS = ["tools/scan-leaks.py", "tools/test_id_set_check.py"]


def _tool_run(tmp_path, capsys, mode, argv_extra, base="origin/dev"):
    repo = _repo(tmp_path, {"tests/test_a.py": 'D = "notes.md"\n'})
    with mock.patch.object(_runner, "guard_set", return_value=([], list(_TOOLS))), \
         mock.patch.object(_runner, "_changed_files", return_value=["notes.md"]):
        return _main([*mode, "--base", base, *argv_extra, "--repo-root", repo], capsys)


def test_c10_json_carries_tool_checks_as_commands(tmp_path, capsys):
    rc, out, _e = _tool_run(tmp_path, capsys, ["--json"], [])
    assert rc == 0
    assert json.loads(out)["tool_checks"] == [
        "python3 tools/scan-leaks.py --root .",
        "python3 tools/test_id_set_check.py --base $(git merge-base origin/dev HEAD) --head HEAD",
    ]


def test_c10_head_is_carried_into_the_test_id_set_command(tmp_path, capsys):
    rc, out, _e = _tool_run(tmp_path, capsys, ["--json"], ["--head", "feat"])
    assert rc == 0
    assert json.loads(out)["tool_checks"][1] == (
        "python3 tools/test_id_set_check.py --base $(git merge-base origin/dev feat) --head feat"
    )


def test_c10_human_output_prints_the_same_commands(tmp_path, capsys):
    rc, out, _e = _tool_run(tmp_path, capsys, ["--dry-run"], [])
    assert rc == 0
    assert "python3 tools/scan-leaks.py --root ." in out
    assert (
        "python3 tools/test_id_set_check.py --base $(git merge-base origin/dev HEAD) --head HEAD"
        in out
    )


# ---------------------------------------------------------------------------
# C11 — help text
# ---------------------------------------------------------------------------


def test_c11_help_points_at_the_policy_not_the_convergence_gate(capsys):
    with pytest.raises(SystemExit) as exc:
        _runner.main(["--help"])
    assert exc.value.code == 0
    text = re.sub(r"\s+", " ", capsys.readouterr().out)
    assert "convergence gate" not in text
    assert "PR-MERGE-POLICY.md" in text
    assert "nightly on dev" in text and "release cut" in text


# ---------------------------------------------------------------------------
# C12 — today's case on the real tree
# ---------------------------------------------------------------------------

_BASE_C12 = "60d7b811"
_HEAD_C12 = "8488d0d4"


def _have(commit: str) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=str(REPO_ROOT), capture_output=True,
    ).returncode == 0


@pytest.mark.skipif(
    not (_have(_BASE_C12) and _have(_HEAD_C12)),
    reason="commits 60d7b811 / 8488d0d4 (PR #4845 range) are not in this clone",
)
def test_c12_todays_case_selects_a_small_scoped_set(tmp_path):
    # 8488d0d4 is a merge of dev 60d7b811 into the PR branch, so its checkout IS
    # the scratch merge the merge seat runs in (checkout == head).
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt), _HEAD_C12],
        cwd=str(REPO_ROOT), check=True, capture_output=True,
    )
    try:
        env = dict(os.environ, HOME=str(tmp_path / "home"), ENGRAM_NO_EMBEDDINGS="1")
        proc = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "--repo-root", str(wt),
             "--base", _BASE_C12, "--head", _HEAD_C12, "--json"],
            cwd=str(wt), capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        guard = set(_runner.guard_set(str(wt))[0])
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=str(REPO_ROOT), capture_output=True)
    selected = set(d["selected"])
    assert not d["full"]
    assert len(selected) < 0.15 * d["total_test_files"]
    assert "tests/test_set_forum_url_3557.py" in selected
    assert guard <= selected
    for decoy in ("tests/test_focus_sets.py", "tests/test_thresholds.py"):
        assert decoy not in selected
    # SPEC GAP (flagged in the handoff): the spec also lists
    # tests/test_config_schema.py as a decoy that must NOT be selected.  Under
    # rules (a)+(b') it IS selected, and so is it in the parent's own 69-file
    # list: its docstring names server.py (rule a; server.py is not unique) and a
    # comment on its line 24 contains the whole word "forum" (rule b', "any whole
    # word anywhere in the file").  Not asserted either way until the spec says
    # which of the rule and the decoy list is right.
    changed_forum_tests = {
        c for c in d["changed_files"]
        if c.startswith("src/forum/tests/test_") and c.endswith(".py")
    }
    assert changed_forum_tests
    assert changed_forum_tests <= set(d["forum_selected"])
    assert not any(s.startswith("src/forum/") for s in selected)


# ---------------------------------------------------------------------------
# E1-E5 — a full-suite TRIGGER without --full stops with exit 7 and runs nothing
#   (code-owner review of PR #4848: the policy says the whole tests/ suite is
#   not run per PR; the tool must not start it on a conftest.py change).
#   --full stays the one explicit way to run the whole suite.
# ---------------------------------------------------------------------------

_E_GUARD = ["tests/test_guard.py"]
_E_TOOLS = ["tools/scan-leaks.py", "tools/test_id_set_check.py"]


def _assert_no_pytest(run):
    """No pytest subprocess started (git ls-files etc. may still run for layer 3)."""
    for call in run.call_args_list:
        assert "pytest" not in " ".join(str(c) for c in call[0][0]), call


def _trigger_run(tmp_path, capsys, changed, extra, guard=(_E_GUARD, _E_TOOLS)):
    """Run main on a trigger with subprocess.run patched; return (rc, out, err, run_mock)."""
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n"})
    with mock.patch.object(_runner, "guard_set", return_value=guard), \
         mock.patch.object(_runner, "_changed_files", return_value=changed), \
         mock.patch.object(_runner.subprocess, "run") as run:
        rc, out, err = _main([*extra, "--base", "HEAD", "--repo-root", repo], capsys)
    return rc, out, err, run


@pytest.mark.parametrize("extra", [[], ["--dry-run"]], ids=["real", "dry-run"])
def test_e1_trigger_without_full_runs_nothing_and_exits_7(tmp_path, capsys, extra):
    rc, out, _err, run = _trigger_run(tmp_path, capsys, ["tests/conftest.py"], extra)
    _assert_no_pytest(run)
    assert rc == 7
    assert "tests/conftest.py" in out
    assert "tests/" in out and "universe" in out
    # the guard set and the tool checks print, as on a normal selection
    assert "tests/test_guard.py" in out
    assert "[tool checks]" in out
    assert "python3 tools/scan-leaks.py --root ." in out
    assert "test_id_set_check.py --base $(git merge-base HEAD HEAD) --head HEAD" in out


def test_e1_map_full_signal_is_a_trigger_too(tmp_path, capsys):
    repo = _repo(tmp_path, {"tests/test_a.py": "a = 1\n"})
    entry = {"path_prefix_or_glob": "special/", "test_globs": [], "full_suite_fallback": True}
    with mock.patch.object(_runner, "guard_set", return_value=(_E_GUARD, _E_TOOLS)), \
         mock.patch.object(_runner, "_load_test_map",
                           return_value={"mappings": [entry]}), \
         mock.patch.object(_runner, "_changed_files", return_value=["special/x.dat"]), \
         mock.patch.object(_runner.subprocess, "run") as run:
        rc, out, _err = _main(["--base", "HEAD", "--repo-root", repo], capsys)
    _assert_no_pytest(run)
    assert rc == 7
    assert "special/x.dat" in out


def test_e2_json_trigger_prints_the_object_and_exits_7(tmp_path, capsys):
    rc, out, _err, run = _trigger_run(tmp_path, capsys, ["tests/conftest.py"], ["--json"])
    _assert_no_pytest(run)
    assert rc == 7
    d = json.loads(out)
    assert d["full"] is True
    assert any("tests/conftest.py" in r for r in d["full_reasons"])
    assert d["tool_checks"] and any("scan-leaks.py" in c for c in d["tool_checks"])


@pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--json"]], ids=["real", "dry-run", "json"])
def test_e3_guard_set_refusal_still_applies_on_the_trigger_path(tmp_path, capsys, extra):
    repo = _policy_repo(
        tmp_path, "   `tests/test_a.py` `tests/test_gone1.py`", {"tests/test_a.py": "a = 1\n"},
    )
    with mock.patch.object(_runner, "_changed_files", return_value=["tests/conftest.py"]), \
         mock.patch.object(_runner.subprocess, "run") as run:
        rc, out, err = _main([*extra, "--base", "HEAD", "--repo-root", repo], capsys)
    _assert_no_pytest(run)
    assert rc == 3
    assert "tests/test_gone1.py" in err
    assert "{" not in out


def test_e4_full_flag_still_runs_the_whole_suite_without_base(tmp_path, capsys):
    repo = _repo(tmp_path, {})
    with mock.patch.object(_runner.subprocess, "run") as run:
        run.return_value.returncode = 5
        rc, _out, _err = _main(["--full", "--repo-root", repo], capsys)
    assert rc == 5
    run.assert_called_once()
    cmd = run.call_args[0][0]
    assert cmd[-1] == "tests/" and cmd[1:3] == ["-m", "pytest"]


def test_e5_forum_trigger_is_named_with_its_universe(tmp_path, capsys):
    rc, out, _err, run = _trigger_run(
        tmp_path, capsys, ["src/forum/tests/conftest.py"], ["--dry-run"]
    )
    _assert_no_pytest(run)
    assert rc == 7
    line = next(l for l in out.splitlines() if "src/forum/tests/conftest.py" in l)
    assert "src/forum/tests/" in line
    rc2, out2, _e, _r = _trigger_run(tmp_path / "x", capsys, ["tests/conftest.py"], ["--dry-run"])
    line2 = next(l for l in out2.splitlines() if "tests/conftest.py" in l)
    assert "src/forum/tests/" not in line2
