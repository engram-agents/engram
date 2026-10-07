"""Tests: engram-alpha#2863 WS2 -- engram-forum-prompt-hook.py's migration
onto `_hooklib.emit_context()`.

Companion to tests/test_hook_injection_bounds.py (WS1's own gate) and
tests/test_surface_hook_injection_bounds.py (the sibling migration this one
follows the shape of) -- this file exercises the CALLER side: main()'s new
head_blocks/body_blocks split and _assemble_response(), wired end-to-end
through a real main() invocation (same _load_hook + stdin/stdout patching
pattern already used by tests/test_forum_hook.py).

Census finding for THIS hook -- much smaller than engram-surface-hook.py's
~11 blocks: only 3 possible blocks total, across two branches main()'s own
comms_surface gate keeps mutually exclusive within a single invocation:
  - comms_surface="forum" (default): mention_line (head) + threads_line
    (body)
  - comms_surface="umwelt": the addressed-item advisory line (head) only

Four scenarios:
  1. Worst-case combined-outage fixture (forum mode: a pathologically long
     thread title forcing mention_line to grow, plus the threads line) --
     TestWorstCaseCombinedOutage
  2. Common-case sanity (forum mode, threads line only, no mentions) --
     TestCommonCaseSanity
  3. Umwelt-mode sanity (advisory line only, head-only render) --
     TestUmweltModeSanity
  4. Nothing fires -- TestNothingFires (this hook's pre-existing silent-noop
     behavior differs from engram-surface-hook.py's: NO stdout at all, not
     even "{}" -- see _assemble_response()'s own docstring)
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = (
    PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-forum-prompt-hook.py"
)
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"


def _load_hook():
    """Load the forum prompt hook as a fresh module (hyphenated filename,
    not importable via normal import -- same pattern as
    tests/test_forum_hook.py's own _load_hook())."""
    spec = importlib.util.spec_from_file_location(
        "engram_forum_prompt_hook_bounds_pt", str(_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" for the same reason
    test_surface_hook_injection_bounds.py pins it: this dev/source checkout
    has no baked platform.json, so detect_harness() returns None here and
    get_harness_budgets(None) would silently fall through to
    UNMEASURED_HARNESS_FLOOR instead of the measured claude-code numbers
    the assertions below are written against. Tests that exercise main()
    end-to-end pin mod.detect_harness the same way, so the budgets computed
    here match what main() actually used at runtime.
    """
    spec = importlib.util.spec_from_file_location("_harness_forum_bounds_pt", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _stub_config(hook, monkeypatch, agent_name: str = "testbot", comms_surface: str = "forum"):
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": agent_name,
        "forum": {"url": "http://localhost:5002"},
    })
    monkeypatch.setattr(hook, "_get_comms_surface", lambda config: comms_surface)


def _patch_paths(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_path / "forum-surfaced-cursor.txt"))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_path / "forum-hook-cache.json"))


def _make_inbox_item(tid: str = "1", title: str = "Test Thread",
                      author: str = "luria", kind: str = "at_mention") -> dict:
    return {
        "thread_id": tid,
        "thread_title": title,
        "author": author,
        "kind": kind,
        "post_id": 1,
        "created_at": "2026-01-01T12:00:00Z",
    }


def _run_main(hook) -> str:
    """Drive main() end-to-end via stdout patching (same pattern as
    tests/test_forum_hook.py's own _run_main), returning raw stdout."""
    buf = io.StringIO()
    try:
        with patch("sys.stdout", buf):
            hook.main()
    except SystemExit:
        pass
    return buf.getvalue()


class TestWorstCaseCombinedOutage:
    """Item 1 of the WS2 spec's "New tests to add": force ALL of this hook's
    optional blocks on at once in the SAME call to main() -- forum mode with
    both mention_line (head) and threads_line (body) firing, and mention_line
    stress-tested with a pathologically long (5,000-char) thread title to
    exercise the genuinely-unbounded-user-content risk this hook's head
    carries (unlike engram-surface-hook.py's own fixed-string head)."""

    def test_worst_case_never_exceeds_hard_budget_and_head_stays_under_head_budget(
        self, tmp_path, monkeypatch
    ):
        hook = _load_hook()
        budgets = _load_harness_budgets("claude-code")
        # Pin to claude-code: see _load_harness_budgets()'s own docstring.
        monkeypatch.setattr(hook, "detect_harness", lambda: "claude-code")
        # Determinism: skip the auto-publish block entirely (sanctioned
        # pattern, tests/test_forum_hook_publish.py's own convention).
        monkeypatch.setattr(hook, "derive_own_status", None)

        _stub_config(hook, monkeypatch, comms_surface="forum")
        _patch_paths(hook, monkeypatch, tmp_path)

        # -- Head: 3 mentions, each with a pathologically long (5,000-char)
        # thread title and a long author name -- forces mention_line's own
        # raw, PRE-truncation construction (were _truncate_title() not
        # applied) to run ~15,000+ chars, far past head_budget.
        huge_title = "T" * 5000
        mentions = [
            _make_inbox_item(tid=str(i), title=huge_title, author="a" * 50)
            for i in range(3)
        ]
        monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([{"id": "9", "title": "x"}] * 50, None))
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: (mentions, None))

        # Spy on emit_context so we can assert on the actual `head` string
        # main() constructed BEFORE it was passed in -- the load-bearing
        # property: the final rendered output's early portion could
        # coincidentally look short due to trailer placement even if the
        # head-construction logic has a latent bug, so this must be
        # asserted on `head` directly (same discipline as
        # test_surface_hook_injection_bounds.py's own worst-case test).
        real_emit_context = hook.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(hook, "emit_context", _spy_emit_context)

        output = _run_main(hook)
        assert output.strip(), "expected a non-empty response given multiple firing blocks"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]

        # The hard invariant: never exceed the harness's hard budget.
        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) under worst-case combined stacking"
        )

        # Trailer leads.
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"

        # The load-bearing property: `head` itself, BEFORE emit_context ever
        # saw it, must already be <= head_budget in this worst-case fixture
        # -- this is a REAL invariant here (not a canary the way it is for
        # engram-surface-hook.py's naturally-short fixed-string head):
        # _format_mention_line()'s own _truncate_title() defensive cap is
        # what makes it hold even with a 5,000-char raw title per mention.
        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) in the worst-case pathological-title "
            f"fixture -- _format_mention_line()'s per-title truncation should "
            f"have bounded this before it ever reached emit_context()."
        )

        # The mention line (bell) must still be recognizable, truncated with
        # the ellipsis marker rather than either dropped or left huge.
        assert "\U0001f514" in captured["head"]
        assert "…" in captured["head"], "expected the truncation ellipsis marker in head"


class TestCommonCaseSanity:
    """Item 2 of the WS2 spec's "New tests to add": the typical prompt --
    only the threads line fires (no pending mentions), well under budget."""

    def test_only_threads_line_fires_body_only_zero_cut(self, tmp_path, monkeypatch):
        hook = _load_hook()
        monkeypatch.setattr(hook, "detect_harness", lambda: "claude-code")
        monkeypatch.setattr(hook, "derive_own_status", None)
        _stub_config(hook, monkeypatch, comms_surface="forum")
        _patch_paths(hook, monkeypatch, tmp_path)

        monkeypatch.setattr(
            hook, "_fetch_threads",
            lambda url, since, agent, deadline: ([{"id": "1", "title": "new thread"}], None),
        )
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))

        output = _run_main(hook)
        assert output.strip(), "expected a non-empty response -- the threads line should have fired"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]

        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for a well-under-budget line; got trailer: {trailer!r}"
        assert "\U0001f4e2" in ctx
        assert "\U0001f514" not in ctx, "no mention was fetched -- bell line must not appear"


class TestUmweltModeSanity:
    """comms_surface=umwelt: the addressed-item advisory line is this hook's
    only possible block under that branch -- a head-only render."""

    def test_advisory_line_fires_head_only_zero_cut(self, tmp_path, monkeypatch):
        hook = _load_hook()
        monkeypatch.setattr(hook, "detect_harness", lambda: "claude-code")
        monkeypatch.setattr(hook, "derive_own_status", None)
        _stub_config(hook, monkeypatch, comms_surface="umwelt")
        _patch_paths(hook, monkeypatch, tmp_path)

        mention = _make_inbox_item(tid="5", title="Needs review", author="luria")
        monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([mention], None))

        output = _run_main(hook)
        assert output.strip(), "expected the umwelt advisory line to fire"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]

        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer
        assert "\U0001f4ea" in ctx
        assert "1 addressed forum item" in ctx


class TestNothingFires:
    """Item 3 of the WS2 spec's "New tests to add": nothing at all fires
    this prompt. Unlike engram-surface-hook.py, this hook's pre-existing
    silent-no-op behavior is NO stdout at all (not even "{}") -- three
    separate bare `sys.exit(0)` checks in main() run before
    _assemble_response() is ever called, and this migration preserves that
    exactly (see _assemble_response()'s own docstring)."""

    def test_nothing_fires_prints_nothing(self, tmp_path, monkeypatch):
        hook = _load_hook()
        monkeypatch.setattr(hook, "detect_harness", lambda: "claude-code")
        monkeypatch.setattr(hook, "derive_own_status", None)
        _stub_config(hook, monkeypatch, comms_surface="forum")
        _patch_paths(hook, monkeypatch, tmp_path)

        monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))

        called = {"emit_context": False}
        real_emit_context = hook.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(hook, "emit_context", _spy_emit_context)

        output = _run_main(hook)
        assert output.strip() == "", f"expected no stdout at all when nothing fires; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called when nothing fires"
