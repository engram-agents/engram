"""Tests: engram-alpha#2863 WS2 -- engram-surface-hook.py's migration onto
`_hooklib.emit_context()`.

Companion to `tests/test_hook_injection_bounds.py` (WS1's own gate for the
helper + harness-adapter module themselves) -- this file exercises the
CALLER side: `main()`'s new head_blocks/body_blocks split and
`_assemble_response()`, wired end-to-end through a real `main()` invocation
(same `_load_surface_hook` + stdin/stdout patching pattern already used by
tests/test_surface_hook_mcp_liveness.py and
tests/test_principle_triggers_registry.py).

Three scenarios, per the WS2 spec:
  1. Worst-case combined-outage fixture -- TestWorstCaseCombinedOutage
  2. Common-case sanity (only the recall nudge fires) -- TestCommonCaseSanity
  3. Nothing fires -- TestNothingFires
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SURFACE_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_surface_hook(engram_home, monkeypatch):
    """Import the surface hook as a module against a scratch ENGRAM_HOME
    (hyphenated filename, not importable via normal import -- same pattern
    as test_surface_hook_mcp_liveness.py / test_principle_triggers_registry.py)."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook_bounds_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook_bounds_pt", str(_SURFACE_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" by default rather than calling
    detect_harness() for real: this dev/source checkout has no baked
    platform.json (see _harness.py's own docstring), so detect_harness()
    returns None here and get_harness_budgets(None) would silently fall
    through to UNMEASURED_HARNESS_FLOOR (4,000 / 1,200) instead of the
    measured claude-code numbers (9,500 / 1,800) the WS2 spec's own
    worst-case assertions are written against. Tests that exercise main()
    end-to-end pin mod.detect_harness the same way, so the budgets computed
    here match what main() actually used at runtime.
    """
    spec = importlib.util.spec_from_file_location("_harness_bounds_pt", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _stub_common_dependencies(mod, monkeypatch, *, nudge: str = ""):
    """Baseline stub set shared by every scenario below -- mirrors the
    existing convention in test_surface_hook_mcp_liveness.py /
    test_principle_triggers_registry.py: everything defaults to "does not
    fire", and each test overrides only what it needs on to actually fire."""
    monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(
        mod, "query_fts_fallback",
        lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []},
    )
    monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
        "idf_gate_enabled": False,
        "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0,
    })
    monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
    monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: nudge)
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))
    monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (True, "ok"))

    # Nothing fires by default -- context_tracker's real functions are
    # never reached against a fresh scratch ENGRAM_HOME (estimate_usage
    # returns None when there's no transcript), but stub explicitly so
    # this fixture set is self-contained and independent of that module's
    # own real logic (same discipline as test_principle_triggers_registry.py).
    orig_context_tracker = sys.modules.get("context_tracker")
    sys.modules["context_tracker"] = types.SimpleNamespace(
        estimate_usage=lambda *a, **kw: None,
        format_drowsiness=lambda *a, **kw: "",
    )

    def _restore_context_tracker():
        if orig_context_tracker is not None:
            sys.modules["context_tracker"] = orig_context_tracker
        else:
            sys.modules.pop("context_tracker", None)

    return _restore_context_tracker


def _run_main(mod, prompt: str = "a prompt"):
    """Drive main() end-to-end via stdin/stdout patching (same pattern as
    test_surface_hook_mcp_liveness.py), returning the parsed stdout JSON."""
    stdin_data = json.dumps({
        "prompt": prompt,
        "session_id": "bounds-test",
        "transcript_path": "",
    })
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit:
                pass
    return json.loads(buf.getvalue().strip())


class TestWorstCaseCombinedOutage:
    """Item 1 of the WS2 spec's "New tests to add": force ALL of the head
    blocks + a large principle_triggers + a nudge + all 5 remaining body
    pointer blocks to fire in the SAME call to main() -- a combined-outage
    stacking that was structurally impossible to check for before this
    migration (there was no combined bound at all on the old
    `"\\n".join(parts)` assembly)."""

    def test_worst_case_never_exceeds_hard_budget_and_head_stays_under_head_budget(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_surface_hook(engram_home, monkeypatch)
        budgets = _load_harness_budgets("claude-code")
        # Pin to claude-code: this dev checkout has no baked platform.json,
        # so main()'s own real detect_harness() call would otherwise return
        # None and silently use the UNMEASURED_HARNESS_FLOOR -- see
        # _load_harness_budgets()'s own docstring above.
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        restore_ct = _stub_common_dependencies(mod, monkeypatch, nudge="[ENGRAM Recall: NUDGE] " + ("n" * 200))

        # -- Head: force all three head-eligible checks to fire.
        # 1. Daemon status -- CRITICAL-offline branch: query_daemon already
        #    returns None (used_daemon=False) via the common stub above, and
        #    a fresh scratch ENGRAM_HOME has neither a
        #    daemon-launch-attempt marker nor a daemon-idle-shutdown
        #    tombstone, so warming_up=False and _is_idle_shutdown=False --
        #    the CRITICAL branch fires naturally, no extra monkeypatch
        #    needed.
        # 2. MCP write-tool liveness -- UNCONFIRMED branch.
        monkeypatch.setattr(
            mod, "_check_mcp_write_tool_marker",
            lambda *a: (False, "marker PID 9999999 not running"),
        )
        # 3. DB liveness -- FAILED branch.
        monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (False, "disk I/O error"))

        # -- Body: principle_triggers at a realistic large size (measured up
        # to 8,980 chars in production per the WS2 spec), plus all 5
        # remaining pointer blocks.
        large_principle_triggers = "[ENGRAM Tripwire]: " + ("X" * 8500)
        monkeypatch.setattr(
            mod, "check_principle_triggers_full",
            lambda *a, **kw: (large_principle_triggers, []),  # rendered=[] -> no telemetry emit
        )
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "[ENGRAM: write reminder text]")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "[ENGRAM: repair notice text]")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "[ENGRAM: feeling nudge text]")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "[ENGRAM: warm briefing pointer]")
        sys.modules["context_tracker"] = types.SimpleNamespace(
            estimate_usage=lambda *a, **kw: {"prompts_since_compaction": 999},
            format_drowsiness=lambda usage: "[ENGRAM Nap URGENT: nap warning text]",
        )

        # Spy on emit_context so we can assert on the actual `head` string
        # main() constructed BEFORE it was passed in -- the load-bearing
        # property from the spec's "Load-bearing subtlety" note: the final
        # rendered output's early portion could coincidentally look short
        # due to trailer placement even if the head-construction logic has
        # a latent bug, so this must be asserted on `head` directly.
        real_emit_context = mod.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        try:
            output = _run_main(mod)
        finally:
            restore_ct()

        assert output, "expected a non-empty response given multiple firing blocks"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        # The hard invariant: never exceed the harness's hard budget, no
        # matter how many blocks stack in one prompt.
        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) under worst-case combined stacking"
        )

        # Trailer leads.
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"
        assert " shown · " in ctx.split("\n", 1)[0]

        # The load-bearing property: `head` itself, BEFORE emit_context ever
        # saw it, must already be <= head_budget in this worst-case fixture.
        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) in the worst-case combined-outage "
            f"fixture -- main()'s head-block concatenation needs its own "
            f"defensive truncation (see the WS2 spec's Load-bearing "
            f"subtlety note); this is NOT an emit_context() bug."
        )


class TestCommonCaseSanity:
    """Item 2 of the WS2 spec's "New tests to add": the typical prompt --
    only the recall nudge fires, well under budget."""

    def test_only_nudge_fires_output_is_trailer_plus_nudge_zero_cut(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_surface_hook(engram_home, monkeypatch)

        nudge = "[ENGRAM Recall: 2 matches] some ordinary recall nudge text"
        restore_ct = _stub_common_dependencies(mod, monkeypatch, nudge=nudge)
        # Daemon must be reported healthy so the head is empty this prompt.
        monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: {"matched_ids": [], "match_count": 0})

        try:
            output = _run_main(mod)
        finally:
            restore_ct()

        assert output, "expected a non-empty response -- the nudge should have fired"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for a well-under-budget nudge; got trailer: {trailer!r}"
        assert ctx == f"{trailer}\n{nudge}"


class TestNothingFires:
    """Item 3 of the WS2 spec's "New tests to add": nothing at all fires
    this prompt -- the pre-migration `if not parts:` early exit, preserved
    exactly (emit_context() must never be invoked with two empty inputs)."""

    def test_nothing_fires_prints_empty_object(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_surface_hook(engram_home, monkeypatch)
        restore_ct = _stub_common_dependencies(mod, monkeypatch, nudge="")
        # Daemon healthy, MCP/DB healthy (already the defaults in the
        # common stub) -- nothing should fire at all.
        monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: {"matched_ids": [], "match_count": 0})

        called = {"emit_context": False}
        real_emit_context = mod.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        try:
            output = _run_main(mod)
        finally:
            restore_ct()

        assert output == {}, f"expected an empty object when nothing fires; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called with two empty inputs"
