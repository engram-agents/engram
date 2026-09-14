"""Tests: engram-alpha#2863 WS2 -- engram-deference-detector-prompt.py's
migration onto `_hooklib.emit_context()`.

Companion to `tests/test_hook_injection_bounds.py` (WS1's own gate) and
`tests/test_surface_hook_injection_bounds.py` (the reference WS2
migration) -- this file exercises the CALLER side: main()'s new
head/body_blocks split, wired end-to-end through a real `main()`
invocation (stdin/stdout patching, same pattern as the surface-hook
suite).

Census finding (WS2 spec): unlike engram-toolcall-repair.py's sibling in
this batch, this hook's `hits` list (and each hit's `snippet` text) is
genuinely of UNPREDICTABLE size and count -- it comes from a regex scan of
the agent's own previous free-text response (engram-deference-detector-stop.py),
with no analogue of toolcall-repair's MAX_REPAIR_PASSES ceiling. So the
worst-case fixture below is a real content-shape concern, not merely
defense-in-depth.

Three scenarios, per the WS2 spec:
  1. Worst-case: many/large hits -- TestWorstCaseManyHits
  2. Common-case sanity (one small hit) -- TestCommonCaseSanity
  3. Nothing fires (no marker / no hits) -- TestNothingFires
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-prompt.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"


def _load_hook(engram_home, monkeypatch):
    """Import the hook as a module against a scratch ENGRAM_HOME (hyphenated
    filename, not importable via normal import -- same pattern as
    test_surface_hook_injection_bounds.py's _load_surface_hook). Uses a
    unique module name per call (mirrors TestDeployedTreeResolution in
    tests/test_deference_detector_prompt.py) so each load's module-scope
    import block re-executes fresh."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_deference_detector_prompt_bounds_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_deference_detector_prompt_bounds_pt", str(_HOOK_PATH)
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
    spec = importlib.util.spec_from_file_location(
        "_harness_deference_detector_pt", _HARNESS_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _run_main(mod, payload: dict):
    """Drive main() end-to-end via stdin/stdout patching, returning the
    parsed stdout JSON (or None for empty stdout -- this hook prints
    nothing at all, not even `{}`, on every no-op path)."""
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
    "session_id": "bounds-test",
    "transcript_path": "",
    "prompt": "what is the status of issue #1594?",
}


def _seed_loop_and_marker(engram_home: Path, marker: dict) -> None:
    (engram_home / "loop-mode.json").write_text("{}")
    (engram_home / "deference-detected.json").write_text(json.dumps(marker))


class TestWorstCaseManyHits:
    """Item 1 of the WS2 spec's "New tests to add": force a large number of
    large-snippet hits through the marker (a shape genuinely reachable in
    production -- unlike engram-toolcall-repair.py's sibling in this batch,
    there is no MAX_REPAIR_PASSES-style ceiling on hit count/snippet size
    here)."""

    def test_worst_case_never_exceeds_hard_budget_and_head_stays_under_head_budget(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        budgets = _load_harness_budgets("claude-code")

        large_hits = [
            {"label": f"label-{i:04d}", "snippet": "s" * 200}
            for i in range(200)
        ]
        marker = {
            "pending": True,
            "hit_count": len(large_hits),
            "unique_labels": len(large_hits),
            "hits": large_hits,
        }
        _seed_loop_and_marker(engram_home, marker)

        mod = _load_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        real_emit_context = mod.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output, "expected a non-empty response given a non-empty hits list"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        # The hard invariant: never exceed the harness's hard budget, no
        # matter how many/large the hit snippets.
        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) under worst-case many-large-hits stacking"
        )

        # Trailer leads.
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"

        # The load-bearing property: `head` itself, BEFORE emit_context ever
        # saw it, must already be <= head_budget in this worst-case
        # fixture -- this hook's head is a fixed-format summary paragraph
        # that does not grow with hit count/snippet size, so this should
        # hold trivially; a failure here would mean a hit snippet leaked
        # into head, not an emit_context() bug.
        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) in the worst-case many-large-hits "
            f"fixture -- this is NOT an emit_context() bug."
        )

        # Marker must still be cleared after surfacing, worst-case or not.
        assert not (engram_home / "deference-detected.json").exists()


class TestCommonCaseSanity:
    """Item 2 of the WS2 spec's "New tests to add": the typical case --
    a single small hit, well under budget."""

    def test_single_small_hit_fires_output_is_trailer_head_hit_zero_cut(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        marker = {
            "pending": True,
            "hit_count": 1,
            "unique_labels": 1,
            "hits": [{"label": "want-me-to-q", "snippet": "want me to proceed?"}],
        }
        _seed_loop_and_marker(engram_home, marker)

        mod = _load_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output, "expected a non-empty response -- the hit should have fired"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for a well-under-budget hit; got trailer: {trailer!r}"

        # Load-bearing substrings preserved verbatim (same assertions as
        # tests/test_deference_detector_scope_gate.py's own coverage).
        assert "self-loop mode" in ctx
        assert "RLHF-baked" in ctx
        assert "informational, not actionable" in ctx
        assert "checking-before-executing" in ctx
        assert "want me to proceed?" in ctx

        assert not (engram_home / "deference-detected.json").exists()


class TestNothingFires:
    """Item 3 of the WS2 spec's "New tests to add": no marker present --
    the pre-migration no-op path (no print at all), preserved exactly
    (emit_context() must never be invoked)."""

    def test_no_marker_prints_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        (engram_home / "loop-mode.json").write_text("{}")
        # No deference-detected.json marker written.

        mod = _load_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        called = {"emit_context": False}
        real_emit_context = mod.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, _BASE_PAYLOAD)

        assert output is None, f"expected empty stdout when no marker is present; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called when nothing fires"
