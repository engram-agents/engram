"""Tests: engram-alpha#2863 WS2 -- engram-stale-tree-pull-hook.py's migration
onto `_hooklib.emit_context()`.

Companion to `tests/test_hook_injection_bounds.py` (WS1's own gate for the
helper + harness-adapter module themselves) and
`tests/test_surface_hook_injection_bounds.py` (the reference migration this
one follows) -- this file exercises the CALLER side: `main()`'s new
warnings/successes split and `_assemble_response()`, wired end-to-end through
a real `main()` invocation (same importlib-load + stdin/stdout patching
pattern already used by tests/test_stale_tree_hooks.py).

Three scenarios, per the WS2 spec:
  1. Worst-case fixture -- many watched repos, forcing the body list past the
     hard budget -- TestWorstCaseManyRepos
  2. Common-case sanity (a couple of repos, well under budget) --
     TestCommonCaseSanity
  3. Nothing fires (empty watch list) -- TestNothingFires

Note on scope: engram-stale-tree-warn-hook.py (the Layer 2 sibling) is
DELIBERATELY NOT migrated in this PR -- see the PR body's escape-valve
finding. It emits at most one already-bounded, atomic message per
PreToolUse fire (a single git-path-derived sentence, no natural
head/body split), unlike this hook's config-driven, unbounded-length
per-repo list.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PULL_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-stale-tree-pull-hook.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"

_LOAD_COUNTER = 0

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_pull_hook(engram_home, monkeypatch):
    """Import the pull hook as a fresh module instance against a scratch
    ENGRAM_HOME (hyphenated filename, not importable via normal import --
    same convention as tests/test_stale_tree_hooks.py)."""
    global _LOAD_COUNTER
    _LOAD_COUNTER += 1
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    name = f"engram_stale_tree_pull_hook_bounds_pt_{_LOAD_COUNTER}"
    spec = importlib.util.spec_from_file_location(name, str(_PULL_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ENGRAM_HOME = str(engram_home)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" by default rather than calling detect_harness()
    for real: this dev/source checkout has no baked platform.json, so
    detect_harness() returns None here -- see test_surface_hook_injection_
    bounds.py's own identical helper/rationale.
    """
    spec = importlib.util.spec_from_file_location("_harness_stale_pull_bounds_pt", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _run_main(mod, stdin_payload: str = "{}"):
    """Drive main() end-to-end via stdin/stdout patching, returning the
    parsed stdout JSON (or {} if nothing was printed)."""
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload.encode()))):
        with patch("sys.stdout", buf):
            with pytest.raises(SystemExit) as exc:
                mod.main()
    assert exc.value.code == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


class TestWorstCaseManyRepos:
    """A `stale_tree_watch` config with many entries -- an unbounded,
    config-driven list -- must never blow past the hard per-harness budget,
    and the fixed head caption must stay well under head_budget."""

    def test_many_repos_never_exceeds_hard_budget(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_pull_hook(engram_home, monkeypatch)
        budgets = _load_harness_budgets("claude-code")
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        # 200 dummy watch entries -- process_repo() itself is stubbed so this
        # test forces the LIST-LENGTH overflow the migration exists to bound,
        # independent of real git behavior (already covered by
        # tests/test_stale_tree_hooks.py).
        watch_list = [{"path": f"/fake/repo-{i}", "branch": "main"} for i in range(200)]
        monkeypatch.setattr(mod, "_load_watch_list", lambda: watch_list)

        def _fake_process_repo(entry):
            i = entry["path"].rsplit("-", 1)[-1]
            # Deliberately a SUCCESS at index 0 (i=0 is even), with warnings
            # on ODD indices -- i.e. a success naturally occurs BEFORE the
            # first warning in watch-list/traversal order. This makes the
            # ordering assertions below discriminate the code's deliberate
            # warnings-first reorder from natural (unsplit) insertion order:
            # under natural order the very first item would be a success,
            # which would fail `captured["body"][0].startswith("⚠")` below.
            # (Previously this alternation put a warning at index 0, which
            # natural order also happens to satisfy trivially -- see PR
            # #2886 reviewer-fairy round 1.)
            if int(i) % 2 == 0:
                return f"↑ pulled {entry['path']} → abc123{i}"
            return f"⚠ {entry['path']} has uncommitted changes — not auto-pulling. " + ("w" * 80)

        monkeypatch.setattr(mod, "process_repo", _fake_process_repo)

        real_emit_context = mod.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod)

        assert output, "expected a non-empty response given 200 fired repo lines"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) under a 200-entry watch list"
        )
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"

        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) -- the fixed caption should never "
            f"grow with the watch list."
        )
        # The body list handed to emit_context is warnings-first (most-
        # important-first, per the WS2 spec) -- confirm the split actually
        # happened rather than merely trusting the final trailer counts.
        assert captured["body"][0].startswith("⚠")
        first_success_idx = next(
            idx for idx, item in enumerate(captured["body"]) if item.startswith("↑")
        )
        assert all(item.startswith("⚠") for item in captured["body"][:first_success_idx])


class TestCommonCaseSanity:
    """The typical case -- a couple of watched repos, well under budget."""

    def test_two_repos_one_warning_one_success_zero_cut(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_pull_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        # repo-b (success) is listed FIRST and repo-a (warning) SECOND --
        # i.e. a success naturally occurs before the warning in watch-list
        # order. This makes the warnings-first-ordering assertion below
        # discriminate the deliberate reorder from natural (unsplit)
        # insertion order, which here would emit "pulled /fake/repo-b"
        # before "uncommitted changes" (see PR #2886 reviewer-fairy round 1).
        watch_list = [
            {"path": "/fake/repo-b", "branch": "main"},
            {"path": "/fake/repo-a", "branch": "main"},
        ]
        monkeypatch.setattr(mod, "_load_watch_list", lambda: watch_list)

        def _fake_process_repo(entry):
            if entry["path"].endswith("-a"):
                return f"⚠ {entry['path']} has uncommitted changes — not auto-pulling."
            return f"↑ pulled {entry['path']} → abc123"

        monkeypatch.setattr(mod, "process_repo", _fake_process_repo)

        output = _run_main(mod)

        assert output, "expected a non-empty response -- two repos fired"
        ctx = output["hookSpecificOutput"]["additionalContext"]
        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for two short lines; got trailer: {trailer!r}"
        assert "[stale-tree auto-pull]" in ctx
        assert "uncommitted changes" in ctx
        assert "pulled /fake/repo-b" in ctx
        # Warnings-first ordering.
        assert ctx.index("uncommitted changes") < ctx.index("pulled /fake/repo-b")


class TestNothingFires:
    """An empty (or absent) watch list -- the pre-migration `if lines:`
    early-exit path, preserved exactly."""

    def test_empty_watch_list_prints_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_pull_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "_load_watch_list", lambda: [])

        called = {"emit_context": False}
        real_emit_context = mod.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod)

        assert output == {}, f"expected no output when nothing fires; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called with an empty body"
