"""Tests: engram-alpha#2863 WS2 -- engram-toolcall-repair.py's migration onto
`_hooklib.emit_context()`.

Companion to `tests/test_hook_injection_bounds.py` (WS1's own gate for the
helper + harness-adapter module themselves) and
`tests/test_surface_hook_injection_bounds.py` (the reference WS2 migration)
-- this file exercises the CALLER side: `_build_context_parts()` /
`format_additional_context()`'s new head/body split, wired end-to-end
through a real `main()` invocation (stdin/stdout patching, same pattern as
`test_surface_hook_injection_bounds.py`).

Census finding (WS2 spec): unlike engram-surface-hook.py, this hook's real
content is ALREADY bounded in practice -- `attempt_repair()`'s own
`MAX_REPAIR_PASSES = 5` caps the real `repairs` list to at most 5 short,
fixed-vocabulary-parameter-name entries. The migration here is genuine
defense-in-depth (so a future loosening of MAX_REPAIR_PASSES, or a bug in
attempt_repair, can't quietly reopen an unbounded-content hole), not a fix
for a content size that is unpredictable TODAY -- hence `TestWorstCase`
below drives a SYNTHETIC oversized `repairs` list (monkeypatching
`attempt_repair` itself) rather than trying to coerce the real regex
machinery into producing 5 repairs, which would test attempt_repair's own
logic, not the emit_context migration this file is about.

Three scenarios, per the WS2 spec:
  1. Worst-case synthetic oversized repairs list -- TestWorstCaseSyntheticRepairs
  2. Common-case sanity (one real-shaped repair) -- TestCommonCaseSanity
  3. Nothing fires (no repair needed) -- TestNothingFires
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-toolcall-repair.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"


def _load_hook(engram_home, monkeypatch):
    """Import the hook as a module against a scratch ENGRAM_HOME (hyphenated
    filename, not importable via normal import -- same pattern as
    test_surface_hook_injection_bounds.py's _load_surface_hook)."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_toolcall_repair_bounds_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_toolcall_repair_bounds_pt", str(_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" by default rather than calling detect_harness()
    for real: this dev/source checkout has no baked platform.json, so
    detect_harness() returns None here and get_harness_budgets(None) would
    silently fall through to UNMEASURED_HARNESS_FLOOR instead of the
    measured claude-code numbers this file's own worst-case assertions are
    written against."""
    spec = importlib.util.spec_from_file_location("_harness_toolcall_repair_pt", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _run_main(mod, payload: dict):
    """Drive main() end-to-end via stdin/stdout patching, returning the
    parsed stdout JSON (or None for empty stdout -- this hook prints
    nothing at all, not even `{}`, when no repair is needed)."""
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit:
                pass
    out = buf.getvalue().strip()
    return json.loads(out) if out else None


_BASE_PAYLOAD = {
    "tool_name": "mcp__engram__engram_add_observation",
    "tool_input": {"quoted_text": "Q", "interpretation": "I", "claim": "C"},
}


class TestWorstCaseSyntheticRepairs:
    """Item 1 of the WS2 spec's "New tests to add": force a synthetic,
    far-oversized `repairs` list through `format_additional_context()` (via
    a monkeypatched `attempt_repair`) -- a stacking that MAX_REPAIR_PASSES
    makes structurally impossible via the real regex path, but that the
    migration must still bound defensively (see this file's own module
    docstring)."""

    def test_worst_case_never_exceeds_hard_budget_and_head_stays_under_head_budget(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook(engram_home, monkeypatch)
        budgets = _load_harness_budgets("claude-code")
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        synthetic_repairs = [
            f"split 'param{i:04d}' at bare </param{i:04d}>, recovered "
            f"'target{i:04d}' (" + ("x" * 200) + " chars)"
            for i in range(300)
        ]
        repaired_input = dict(_BASE_PAYLOAD["tool_input"])

        monkeypatch.setattr(
            mod, "attempt_repair",
            lambda tool_name, tool_input: (repaired_input, synthetic_repairs),
        )

        real_emit_context = mod.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output, "expected a non-empty response given a non-empty repairs list"
        hook_specific = output["hookSpecificOutput"]
        ctx = hook_specific["additionalContext"]

        # The hard invariant: never exceed the harness's hard budget, no
        # matter how many/large the repair descriptions.
        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) under a synthetic oversized repairs list"
        )

        # Trailer leads.
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"

        # PreToolUse contract preserved: permissionDecision + updatedInput
        # still present and correct alongside the now-bounded additionalContext.
        assert hook_specific["permissionDecision"] == "allow"
        assert hook_specific["updatedInput"] == repaired_input

        # The load-bearing property: `head` itself, BEFORE emit_context ever
        # saw it, must already be <= head_budget -- this hook's head is a
        # fixed-format single-sentence summary that does not grow with the
        # number/size of repairs, so this should hold trivially; a failure
        # here would mean the head/body split itself is wrong (e.g. a
        # repair description leaking into head).
        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) under a synthetic oversized repairs "
            f"list -- this hook's head is supposed to be a fixed-size summary "
            f"sentence independent of repair count/size; this is NOT an "
            f"emit_context() bug."
        )


class TestCommonCaseSanity:
    """Item 2 of the WS2 spec's "New tests to add": the typical repair --
    a single real-shaped swallow-pattern fix, well under budget."""

    def test_single_repair_fires_output_is_trailer_head_repair_footer_zero_cut(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        repair_desc = "split 'interpretation' at bare </interpretation>, recovered 'claim' (1 chars)"
        repaired_input = {"quoted_text": "Q", "interpretation": "I", "claim": "C"}
        monkeypatch.setattr(
            mod, "attempt_repair",
            lambda tool_name, tool_input: (repaired_input, [repair_desc]),
        )

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output, "expected a non-empty response -- a repair should have fired"
        hook_specific = output["hookSpecificOutput"]
        ctx = hook_specific["additionalContext"]

        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for a well-under-budget repair; got trailer: {trailer!r}"
        assert "1 swallow-pattern corruption(s)" in ctx
        assert repair_desc in ctx
        assert "Root cause:" in ctx
        assert hook_specific["permissionDecision"] == "allow"
        assert hook_specific["updatedInput"] == repaired_input


class TestNothingFires:
    """Item 3 of the WS2 spec's "New tests to add": no repair needed --
    the pre-migration early `sys.exit(0)` (no print at all), preserved
    exactly (emit_context() must never be invoked)."""

    def test_no_repair_needed_prints_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        monkeypatch.setattr(
            mod, "attempt_repair",
            lambda tool_name, tool_input: (dict(tool_input), []),
        )

        called = {"emit_context": False}
        real_emit_context = mod.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output is None, f"expected empty stdout when no repair is needed; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called when no repair fires"
