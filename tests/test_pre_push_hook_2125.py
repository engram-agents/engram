"""Tests for the pre-push close-keyword hook (engram-agents/engram#2125).

The hook (``src/engram/.githooks/pre-push``) is an extensionless Python script,
so it is loaded by path.  Nothing here shells out to a real ``gh`` or touches the
network: the module's ``_run`` is monkeypatched in every test that would call it.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "engram" / ".githooks" / "pre-push"


@pytest.fixture
def hook():
    # Compile from source rather than SourceFileLoader.exec_module: that writes a .pyc next to the
    # hook, and a later same-second edit (a mutation run) would then be silently ignored.
    module = types.ModuleType("pre_push_hook_under_test")
    module.__file__ = str(HOOK_PATH)
    source = HOOK_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(HOOK_PATH), "exec"), module.__dict__)
    return module


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# _check_pr: the four verdict paths
# --------------------------------------------------------------------------


def test_check_pr_trap_blocks(hook, capsys):
    """(a) body refs a number that is not the title's target -> 1, BLOCKED, trap named."""
    rc = hook._check_pr("feat: x [closes #10]", "Closes #10.\nAlso fixes #99.")
    err = capsys.readouterr().err
    assert rc == 1
    assert "BLOCKED" in err
    assert "#99" in err
    assert capsys.readouterr().out == ""


def test_check_pr_matching_target_is_silent(hook, capsys):
    """(b) title target + matching body keyword -> 0, silent."""
    rc = hook._check_pr("feat: x [closes #10]", "Closes #10.")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == ""


def test_check_pr_title_target_without_body_keyword_warns(hook, capsys):
    """(c) title [closes #N], body has no keyword -> 0 with the warning."""
    rc = hook._check_pr("feat: x [closes #10]", "No keyword here, only #10 mentioned.")
    err = capsys.readouterr().err
    assert rc == 0
    assert "has no 'Closes #N.' keyword" in err
    assert "#10" in err
    assert "BLOCKED" not in err


def test_check_pr_body_keyword_without_title_target_warns(hook, capsys):
    """(d) body keyword, no title target -> 0 with the warning."""
    rc = hook._check_pr("feat: x", "Fixes #42.")
    err = capsys.readouterr().err
    assert rc == 0
    assert "no [closes #N] target" in err
    assert "#42" in err
    assert "BLOCKED" not in err


def test_check_pr_clean_pr_positive_control(hook, capsys):
    """A PR with neither title target nor body keyword: 0 and empty stderr."""
    rc = hook._check_pr("chore: tidy", "Just housekeeping, see #7 for context.")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == ""


# --------------------------------------------------------------------------
# _check_pr: variations
# --------------------------------------------------------------------------


def test_check_pr_multiple_title_targets_all_matched(hook, capsys):
    rc = hook._check_pr("x [closes #1] [closes #2]", "Closes #1. Closes #2.")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_pr_multiple_traps_all_named_sorted_numerically(hook, capsys):
    rc = hook._check_pr("x [closes #1]", "Closes #1. Fixes #100. Fixes #20.")
    err = capsys.readouterr().err
    assert rc == 1
    assert "#20 #100" in err  # numeric, not lexicographic, order
    assert "Title close target: #1\n" in err


def test_check_pr_one_matching_one_trap_still_blocks(hook, capsys):
    rc = hook._check_pr("x [closes #1] [closes #2]", "Closes #1. Closes #3.")
    err = capsys.readouterr().err
    assert rc == 1
    assert "#3" in err


def test_check_pr_multiple_title_targets_warning_lists_all(hook, capsys):
    rc = hook._check_pr("x [closes #3] [closes #12]", "nothing")
    err = capsys.readouterr().err
    assert rc == 0
    assert "[closes #3, #12]" in err
    assert "Add 'Closes #3.'" in err


def test_check_pr_multiple_body_refs_warning_lists_all(hook, capsys):
    rc = hook._check_pr("x", "Fixes #12. Closes #3.")
    err = capsys.readouterr().err
    assert rc == 0
    assert "[#3, #12]" in err


@pytest.mark.parametrize("word", ["Fixes", "FIXES", "fixes", "Resolved", "RESOLVED", "CLOSES", "Closed"])
def test_check_pr_keywords_case_insensitive(hook, capsys, word):
    rc = hook._check_pr("x [closes #1]", f"{word} #2.")
    assert rc == 1
    assert "#2" in capsys.readouterr().err


@pytest.mark.parametrize(
    "word", ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"]
)
def test_check_pr_every_keyword_form_is_recognised(hook, capsys, word):
    rc = hook._check_pr("x [closes #1]", f"{word} #2")
    assert rc == 1
    assert "#2" in capsys.readouterr().err


def test_check_pr_title_target_absent_from_body_is_not_a_trap(hook, capsys):
    """A title target the body does not repeat is not a trap: only body refs
    outside the title set are (kills a symmetric-difference mutant)."""
    rc = hook._check_pr("x [closes #1] [closes #2]", "Closes #1.")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""


def test_check_pr_keyword_needs_a_word_boundary(hook, capsys):
    """'unfixed #99' / 'disclosed #98' are not close keywords (\\b in the regex)."""
    rc = hook._check_pr("x [closes #1]", "Closes #1. Known: unfixed #99, disclosed #98.")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""


def test_check_pr_title_bracket_is_case_insensitive(hook, capsys):
    rc = hook._check_pr("x [CLOSES #5]", "Closes #5.")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_pr_title_bracket_accepts_closed_form(hook, capsys):
    rc = hook._check_pr("x [closed #5]", "Closes #5.")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_pr_empty_title_and_body(hook, capsys):
    assert hook._check_pr("", "") == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# _get_pr_info
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        _cp(returncode=1, stdout='[{"title": "t", "body": "b"}]'),
        _cp(stdout="not json at all"),
        _cp(stdout=""),
        _cp(stdout="[]"),
        _cp(stdout="{}"),
        _cp(stdout='{"title": "t", "body": "b"}'),
    ],
    ids=["gh-nonzero", "non-json", "empty-stdout", "empty-list", "json-dict-empty", "json-dict-nonempty"],
)
def test_get_pr_info_returns_none(hook, monkeypatch, result):
    monkeypatch.setattr(hook, "_run", lambda cmd, **kw: result)
    assert hook._get_pr_info("feature") is None


def test_get_pr_info_returns_first_element(hook, monkeypatch):
    payload = [{"title": "T1", "body": "B1"}, {"title": "T2", "body": "B2"}]
    monkeypatch.setattr(hook, "_run", lambda cmd, **kw: _cp(stdout=json.dumps(payload)))
    assert hook._get_pr_info("feature") == {"title": "T1", "body": "B1"}


def test_get_pr_info_one_element_list(hook, monkeypatch):
    monkeypatch.setattr(hook, "_run", lambda cmd, **kw: _cp(stdout='[{"title": "T", "body": "B"}]'))
    assert hook._get_pr_info("feature") == {"title": "T", "body": "B"}


def test_get_pr_info_exact_gh_argv(hook, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _cp(stdout="[]")

    monkeypatch.setattr(hook, "_run", fake_run)
    hook._get_pr_info("my/branch")
    assert calls == [
        ["gh", "pr", "list", "--head", "my/branch", "--state", "open",
         "--json", "title,body", "--limit", "1"]
    ]


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------


def _wire_main(hook, monkeypatch, *, gh=True, branch="feature", pr=None):
    monkeypatch.setattr(hook, "_run", lambda cmd, **kw: _cp(returncode=0 if gh else 1))
    monkeypatch.setattr(hook, "_get_branch_name", lambda: branch)
    monkeypatch.setattr(hook, "_get_pr_info", lambda b: pr)


def test_main_no_gh_skips(hook, monkeypatch):
    _wire_main(hook, monkeypatch, gh=False, pr={"title": "x [closes #1]", "body": "Closes #2."})
    assert hook.main() == 0  # would be 1 if the gh gate were absent


def test_main_which_gh_argv(hook, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _cp(returncode=1)

    monkeypatch.setattr(hook, "_run", fake_run)
    assert hook.main() == 0
    assert calls == [["which", "gh"]]


def test_main_no_branch_skips(hook, monkeypatch):
    _wire_main(hook, monkeypatch, branch=None, pr={"title": "x [closes #1]", "body": "Closes #2."})
    assert hook.main() == 0


def test_main_empty_branch_skips(hook, monkeypatch):
    _wire_main(hook, monkeypatch, branch="", pr={"title": "x [closes #1]", "body": "Closes #2."})
    assert hook.main() == 0


def test_main_no_pr_skips(hook, monkeypatch):
    _wire_main(hook, monkeypatch, pr=None)
    assert hook.main() == 0


def test_main_passes_branch_to_get_pr_info(hook, monkeypatch):
    seen = []
    monkeypatch.setattr(hook, "_run", lambda cmd, **kw: _cp())
    monkeypatch.setattr(hook, "_get_branch_name", lambda: "the-branch")
    monkeypatch.setattr(hook, "_get_pr_info", lambda b: seen.append(b))
    hook.main()
    assert seen == ["the-branch"]


def test_main_pr_with_trap_returns_1(hook, monkeypatch, capsys):
    _wire_main(hook, monkeypatch, pr={"title": "x [closes #1]", "body": "Closes #1. Fixes #2."})
    assert hook.main() == 1
    assert "BLOCKED" in capsys.readouterr().err


def test_main_clean_pr_returns_0(hook, monkeypatch, capsys):
    _wire_main(hook, monkeypatch, pr={"title": "x [closes #1]", "body": "Closes #1."})
    assert hook.main() == 0
    assert capsys.readouterr().err == ""


def test_main_null_body_treated_as_empty(hook, monkeypatch, capsys):
    _wire_main(hook, monkeypatch, pr={"title": "x [closes #1]", "body": None})
    assert hook.main() == 0  # no TypeError from re on None
    assert "has no 'Closes #N.' keyword" in capsys.readouterr().err


def test_main_missing_title_and_body_keys(hook, monkeypatch, capsys):
    _wire_main(hook, monkeypatch, pr={"number": 5})
    assert hook.main() == 0
    assert capsys.readouterr().err == ""
