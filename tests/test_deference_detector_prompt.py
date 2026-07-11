"""Tests for engram-deference-detector-prompt's loop-wake classification (#1594).

Behaviors covered:

  _is_cron_prompt — new SSoT marker check:
    1. format_loop_prompt body → True (the fix for #1594).
    2. <<autonomous-loop>> sentinel → still True (back-compat).
    3. <<autonomous-loop-dynamic>> sentinel → still True (back-compat).
    4. 'Loop continuation.' prefix → still True (back-compat).
    5. Plain human message → False.

  _is_cron_prompt — HTML-entity normalization (gh#1646, Layer 2):
    9. Escaped &lt;loop-wake&gt; → True (the incident case).
   10. Escaped &lt;&lt;autonomous-loop&gt;&gt; → True.
   11. Plain forms still classify (agnostic to escaped-vs-plain).

  SSoT guard:
    6. Hook's _LOOP_WAKE_MARKER_FALLBACK == loop_prompt.LOOP_WAKE_MARKER.

  Deployed-tree resolution (#1537/#1539 regression class):
    7. Case 1: CLAUDE_PLUGIN_ROOT set → resolves loop_prompt via env-var path.
    8. Case 2: CLAUDE_PLUGIN_ROOT unset → walk-parents finds loop_prompt.py.
"""

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "hooks", "claude", "engram-deference-detector-prompt.py",
)
_hook_spec = importlib.util.spec_from_file_location("deference_detector_prompt", HOOK_PATH)
ddp = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(ddp)

# Also load loop_prompt for the SSoT guard test and fixture helpers.
_LP_PATH = Path(__file__).parent.parent / "src" / "engram" / "tools" / "loop_prompt.py"
_lp_spec = importlib.util.spec_from_file_location("loop_prompt_ddp_tests", _LP_PATH)
_lp = importlib.util.module_from_spec(_lp_spec)
_lp_spec.loader.exec_module(_lp)


# ---------------------------------------------------------------------------
# _is_cron_prompt — SSoT marker + back-compat legacy sentinels
# ---------------------------------------------------------------------------

class TestIsCronPrompt:
    """_is_cron_prompt correctly classifies loop-wake and legacy sentinels."""

    def test_format_loop_prompt_body_is_cron(self, tmp_path):
        """format_loop_prompt body → True (the #1594 fix).

        Once loops adopt format_loop_prompt(), their prompts start with the
        <loop-wake> marker.  Without this check the detector would treat them
        as real user messages and write a spurious cooldown marker.
        """
        loop_marker = str(tmp_path / "loop-mode.json")  # file need not exist
        loop_body = _lp.format_loop_prompt("Loop tick — checking for new work.")
        assert ddp._is_cron_prompt(loop_body, loop_marker) is True

    def test_format_loop_prompt_with_meta_is_cron(self, tmp_path):
        """format_loop_prompt with meta kwargs → also True."""
        loop_marker = str(tmp_path / "loop-mode.json")
        loop_body = _lp.format_loop_prompt(
            "Curiosity loop — scanning open questions.", kind="curiosity", seq=5
        )
        assert ddp._is_cron_prompt(loop_body, loop_marker) is True

    def test_legacy_autonomous_loop_is_cron(self, tmp_path):
        """<<autonomous-loop>> → True (back-compat with pre-SSoT loops)."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("<<autonomous-loop>>", loop_marker) is True

    def test_legacy_autonomous_loop_dynamic_is_cron(self, tmp_path):
        """<<autonomous-loop-dynamic>> → True (back-compat with pre-SSoT loops)."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("<<autonomous-loop-dynamic>>", loop_marker) is True

    def test_loop_continuation_prefix_is_cron(self, tmp_path):
        """'Loop continuation.' prefix → True (canonical ScheduleWakeup stub, back-compat)."""
        loop_marker = str(tmp_path / "loop-mode.json")
        stub = (
            "Loop continuation. Read `~/.engram/loop-mode.json` for the loop topic, "
            "instructions, and current state."
        )
        assert ddp._is_cron_prompt(stub, loop_marker) is True

    def test_plain_human_prompt_is_not_cron(self, tmp_path):
        """A plain human message → False (no file present → OSError swallowed → False)."""
        loop_marker = str(tmp_path / "loop-mode.json")  # deliberately absent
        assert ddp._is_cron_prompt("what is the status of issue #1594?", loop_marker) is False

    def test_empty_prompt_is_not_cron(self, tmp_path):
        """An empty prompt → False (stripped is '', no prefix matches)."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("", loop_marker) is False


# ---------------------------------------------------------------------------
# _is_cron_prompt — HTML-entity normalization (gh#1646, Layer 2)
# ---------------------------------------------------------------------------

class TestIsCronPromptHtmlEntityNormalization:
    """_is_cron_prompt must classify identically whether a marker/sentinel
    arrives plain or HTML-escaped, so an agent-construction mistake (writing
    &lt;loop-wake&gt; or &lt;&lt;autonomous-loop&gt;&gt; instead of real angle
    brackets into a ScheduleWakeup prompt) can't silently defeat detection and
    fall through to the deference-detector's presence/deference gate."""

    def test_escaped_loop_wake_marker_is_cron(self, tmp_path):
        """The incident case: &lt;loop-wake&gt; classifies as cron/loop-wake."""
        loop_marker = str(tmp_path / "loop-mode.json")
        body = "&lt;loop-wake&gt;\n...core..."
        assert ddp._is_cron_prompt(body, loop_marker) is True

    def test_plain_loop_wake_marker_is_cron(self, tmp_path):
        """Plain marker (unchanged behavior) still classifies as cron/loop-wake."""
        loop_marker = str(tmp_path / "loop-mode.json")
        body = "<loop-wake>\n...core..."
        assert ddp._is_cron_prompt(body, loop_marker) is True

    def test_escaped_autonomous_loop_sentinel_is_cron(self, tmp_path):
        """Escaped &lt;&lt;autonomous-loop&gt;&gt; → True."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("&lt;&lt;autonomous-loop&gt;&gt;", loop_marker) is True

    def test_escaped_autonomous_loop_dynamic_sentinel_is_cron(self, tmp_path):
        """Escaped &lt;&lt;autonomous-loop-dynamic&gt;&gt; → True."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("&lt;&lt;autonomous-loop-dynamic&gt;&gt;", loop_marker) is True

    def test_plain_autonomous_loop_sentinel_still_cron(self, tmp_path):
        """Plain <<autonomous-loop>> still classifies (agnostic)."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("<<autonomous-loop>>", loop_marker) is True

    def test_normal_human_prompt_is_not_cron(self, tmp_path):
        """A normal human prompt is not misclassified as cron/loop-wake."""
        loop_marker = str(tmp_path / "loop-mode.json")
        assert ddp._is_cron_prompt("a normal human prompt", loop_marker) is False


# ---------------------------------------------------------------------------
# SSoT guard — fallback constant must equal loop_prompt.LOOP_WAKE_MARKER
# ---------------------------------------------------------------------------

class TestSSoTGuard:
    """Hook's fallback marker string equals loop_prompt.LOOP_WAKE_MARKER.

    This guard prevents the two constants from diverging silently — if one is
    updated and the other is not, the test fails and the drift is caught at
    review time (#1594 spec requirement, mirroring the #1592 guard on the
    time-bar hook).
    """

    def test_fallback_equals_loop_prompt_marker(self):
        """_LOOP_WAKE_MARKER_FALLBACK == loop_prompt.LOOP_WAKE_MARKER."""
        assert ddp._LOOP_WAKE_MARKER_FALLBACK == _lp.LOOP_WAKE_MARKER

    def test_active_marker_equals_loop_prompt_marker(self):
        """In the source-tree test env, the import succeeds and the active
        marker also equals loop_prompt.LOOP_WAKE_MARKER."""
        assert ddp._LOOP_WAKE_MARKER == _lp.LOOP_WAKE_MARKER


# ---------------------------------------------------------------------------
# Deployed-tree resolution — regression guard for the #1537/#1539 class
# ---------------------------------------------------------------------------

class TestDeployedTreeResolution:
    """Regression guard for the #1537/#1539 class of deployed-tree path bugs.

    The plugin build flattens ``hooks/claude/*`` → ``hooks/*``, so a hook at
    ``<root>/hooks/<name>.py`` is one level shallower than its source at
    ``src/engram/hooks/claude/<name>.py``.  A fixed ``parents[N]`` calibrated
    against the deeper source layout overshoots in the deployed tree (same
    silent failure as #1539: the path resolves to *a* directory, just the wrong
    one).

    The deference-detector hook uses the robust resolve pattern
    (``$CLAUDE_PLUGIN_ROOT/tools`` first, then walk-parents for
    ``loop_prompt.py``) instead of a fixed ``parents[N]``.  These tests exercise
    both paths by copying the real hook and loop_prompt into a temp dir that
    mirrors the deployed (flattened) layout, then loading the hook fresh so its
    module-level import block re-executes against that layout.

    Loading uses a unique module name per case so the top-level resolution
    re-runs rather than reusing a cached module.
    """

    _SRC_ROOT = Path(__file__).parent.parent / "src" / "engram"

    def _build_deployed_layout(self, root: Path) -> None:
        """Populate ``<root>/hooks/engram-deference-detector-prompt.py`` and
        ``<root>/tools/loop_prompt.py`` — the deployed (flattened) layout."""
        (root / "hooks").mkdir(parents=True)
        (root / "tools").mkdir(parents=True)
        shutil.copy(
            self._SRC_ROOT / "hooks" / "claude" / "engram-deference-detector-prompt.py",
            root / "hooks" / "engram-deference-detector-prompt.py",
        )
        shutil.copy(
            self._SRC_ROOT / "tools" / "loop_prompt.py",
            root / "tools" / "loop_prompt.py",
        )

    def _load_hook_fresh(self, hook_path: Path, module_name: str):
        """Load the hook at hook_path under a unique module_name.

        Clears ``sys.modules['loop_prompt']`` first so the hook's top-level
        import block re-resolves loop_prompt from the newly configured sys.path
        rather than reusing a module cached from an earlier load.
        """
        sys.modules.pop("loop_prompt", None)
        spec = importlib.util.spec_from_file_location(module_name, str(hook_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_case1_plugin_root_set(self, tmp_path, monkeypatch):
        """Case 1: CLAUDE_PLUGIN_ROOT=<root> → hook resolves loop_prompt via env-var path.

        When ``$CLAUDE_PLUGIN_ROOT`` is set, ``<root>/tools`` is the first
        candidate and is found before the walk-parents search even runs.
        The hook's ``_LOOP_WAKE_MARKER`` is set from the import (not the
        hardcoded fallback), and the active marker equals the SSoT value.
        """
        root = tmp_path / "plugin"
        self._build_deployed_layout(root)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))

        saved_syspath = sys.path[:]
        try:
            hook = self._load_hook_fresh(
                root / "hooks" / "engram-deference-detector-prompt.py",
                "deference_detector_prompt_deployed_c1",
            )
            assert hook._LOOP_WAKE_MARKER == "<loop-wake>"
            assert hook._LOOP_WAKE_MARKER_FALLBACK == "<loop-wake>"
            # Verify _is_cron_prompt uses the imported marker correctly.
            loop_body = "<loop-wake>\nLoop tick — checking for new work."
            assert hook._is_cron_prompt(loop_body, str(tmp_path / "loop-mode.json")) is True
        finally:
            sys.path[:] = saved_syspath
            sys.modules.pop("loop_prompt", None)

    def test_case2_plugin_root_unset(self, tmp_path, monkeypatch):
        """Case 2: CLAUDE_PLUGIN_ROOT unset → walk-parents finds <root>/tools/loop_prompt.py.

        With the env var absent, the hook walks parents of its own ``__file__``
        (``<root>/hooks/engram-deference-detector-prompt.py``).  ``parents[1]``
        is ``<root>`` and ``<root>/tools/loop_prompt.py`` exists → found in one
        hop.  A fixed ``parents[N]`` calibrated against the source tree (two
        extra levels) would land at the *parent* of ``<root>`` and miss the
        deployed tools dir.
        """
        root = tmp_path / "plugin"
        self._build_deployed_layout(root)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        saved_syspath = sys.path[:]
        try:
            hook = self._load_hook_fresh(
                root / "hooks" / "engram-deference-detector-prompt.py",
                "deference_detector_prompt_deployed_c2",
            )
            assert hook._LOOP_WAKE_MARKER == "<loop-wake>"
            assert hook._LOOP_WAKE_MARKER_FALLBACK == "<loop-wake>"
            # Verify _is_cron_prompt uses the imported marker correctly.
            loop_body = "<loop-wake>\nLoop tick — checking for new work."
            assert hook._is_cron_prompt(loop_body, str(tmp_path / "loop-mode.json")) is True
        finally:
            sys.path[:] = saved_syspath
            sys.modules.pop("loop_prompt", None)
