"""engram-alpha#4351: re-orient (don't rush; warm briefing slowly and in full,
first, then the newest history file), after a compaction and at every fresh start.

Contract (five cells), written before the implementation:
  1. source=compact: the re-orient block says don't rush, names the warm
     briefing (slowly and in full, before anything else), and is emitted BEFORE "[Active session:" and the
     compaction notice.
  2. source=compact: the notice keeps "stale-on-arrival" and drops the
     "full window to work" / "energy is refreshed" sentences.
  3. source=startup: the startup reading text is byte-identical to dev's.
  4. A real-seat-shaped (>=40K chars) focus block does not push the
     re-orient block out of the harness head-preview window.
  5. The rendered Claude template carries the section BEFORE "## My Goal",
     with no unresolved placeholder left in it once the install-time and
     first-session substitutions are applied.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
_TEMPLATES_DIR = _REPO_ROOT / "src" / "engram" / "templates"

# Same value as tests/test_session_start_hook_piece_c_sidecar.py's constant
# (copied, not imported: that file defines it at module level in a test module).
_HEAD_PREVIEW_CHARS = 1800

# Copied from dev's engram-session-start-hook.py (non-edited startup branch)
# before this change; the startup branch must not move.
_DEV_STARTUP_READING = (
    "[Session start reading — before responding to the first task "
    "(even if a user task is already waiting), follow your Session "
    "Start Reading list in ~/.claude/CLAUDE.md. Read first, respond "
    "second.]"
)

_SECTION_HEADING = "## Before anything else: re-orient"

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path):
    os.environ["ENGRAM_HOME"] = str(engram_home)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct
    for name in list(sys.modules):
        if name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.detect_harness = lambda: "claude-code"
    module.tool_capability_index_block = lambda: ""
    module.tool_capability_short_block = lambda: ""
    module._check_mcp_health = lambda home: (True, None)
    module._check_backup_staleness = lambda home: (True, None)
    module._check_viz_server_health = lambda home: ("not-configured", None)
    module._check_embedding_restore_flag = lambda home: (True, None)
    return module


def _mock_client_modules():
    client = MagicMock()

    def _call(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        return {"confidence": {"by_type": {}, "by_quote_type": {},
                               "by_reasoning_type": {}, "by_source_class": {}},
                "memory": {"current_turn": 1, "tier_sizes": {}}}

    client.call.side_effect = _call
    return patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(return_value=client))})


def _run(tmp_path: Path, source: str, mutate=None) -> tuple[str, Path]:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    (home / "config.json").write_text("{}", encoding="utf-8")
    mod = _load_hook_module(home)
    if mutate:
        mutate(mod)
    stdin = json.dumps({
        "session_id": f"reorient-4351-{source}",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source": source,
    })
    buf = io.StringIO()
    with _mock_client_modules():
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))):
            with redirect_stdout(buf):
                mod.main()
    ctx = json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]
    return ctx, home


def test_cell1_compact_reorient_block_leads_and_names_briefing(tmp_path):
    ctx, home = _run(tmp_path, "compact")
    assert "don't rush" in ctx
    assert "before anything else" in ctx
    assert "slowly and in full" in ctx
    assert str(home / "warm-briefing.md") in ctx
    reading = ctx.index("[Session start reading —")
    assert reading < ctx.index("[Active session:")
    assert reading < ctx.index("⚡")


def test_cell2_compact_notice_keeps_stale_fact_drops_work_pull(tmp_path):
    ctx, _ = _run(tmp_path, "compact")
    assert "stale-on-arrival" in ctx
    assert "full window to work" not in ctx
    assert "energy is refreshed" not in ctx


def test_cell3_startup_reading_text_byte_identical_to_dev(tmp_path):
    ctx, _ = _run(tmp_path, "startup")
    assert _DEV_STARTUP_READING in ctx
    assert "don't rush" not in ctx


def test_cell4_large_focus_block_keeps_reorient_in_head_preview(tmp_path):
    header = "📌 Focused nodes — the deterministic focus channel (verbatim, must survive compaction):"
    huge = "\n".join([header] + [
        f"  - [ob_9{i:03d}] (cohort test pin) Synthetic focus claim number {i} for the real-seat fixture."
        for i in range(500)
    ])
    assert len(huge) >= 40_000

    def mutate(mod):
        mod.focus_block = lambda home: huge

    ctx, _ = _run(tmp_path, "compact", mutate)
    at = ctx.find("[Session start reading — don't rush")
    assert 0 <= at < _HEAD_PREVIEW_CHARS, f"re-orient block at {at}; head: {ctx[:400]!r}"


def test_cell5_template_section_before_my_goal_and_fully_resolved():
    import template_render

    raw = template_render.render_identity_surface(_TEMPLATES_DIR)
    # install-time ({{ENGRAM_HOME}}) and first-session ({{USER_NAME}}) substitutions
    text = raw.replace("{{ENGRAM_HOME}}", "/h/.engram").replace("{{USER_NAME}}", "Pat")
    assert _SECTION_HEADING in text
    start = text.index(_SECTION_HEADING)
    assert start < text.index("## My Goal")
    section = text[start:text.index("## My Goal")]
    assert "{{" not in section
    assert "/h/.engram/warm-briefing.md" in section
    assert "/h/.engram/history/" in section


def test_surface_hook_warm_briefing_pointer_wording(tmp_path):
    """The surface hook's first-prompt pointer (the other post-compaction path)
    carries the same read-slowly-and-in-full-before-anything-else wording as the
    session-start block. Loads the REAL hook with ENGRAM_HOME on a tmp dir."""
    home = tmp_path / "engram"
    home.mkdir()
    (home / "warm-briefing.md").write_text("a note\n")
    hook_path = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_4351", hook_path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(home)}):
        spec.loader.exec_module(mod)
    assert mod.WARM_BRIEFING_PATH.startswith(str(home))
    text = mod.check_warm_briefing(1)
    assert "read it slowly and in full" in text
    assert "before anything else" in text
