"""Tests for tool_capability_index_block() wired into the SessionStart hook
(issue #1958).

Covers:
  1. Renders correctly when the artifact file exists (header + one indented
     line per entry, verbatim content).
  2. Degrades gracefully (returns "", no crash) when the artifact is
     missing.
  3. Degrades gracefully when the artifact exists but is empty.
  4. Degrades gracefully when the resolved path raises during read (e.g.
     permission error / malformed path) — the block must never propagate an
     exception into main().
  5. _resolve_tool_capability_index_path() finds the REAL checked-in
     artifact in this repo's actual dev-repo layout (sanity check against
     the real file, not just mocks).
  6. main() end-to-end: the block appears in additionalContext when the
     real checked-in artifact is present, and the hook still emits valid
     JSON when it's absent (module import is unaffected either way).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
_REAL_ARTIFACT_PATH = _REPO_ROOT / "src" / "engram" / "tool_capability_index.txt"


def _load_session_start_module(engram_home: Path):
    """Load the Claude session-start hook with ENGRAM_HOME set.

    Mirrors tests/test_focus_block.py's _load_session_start_module pattern:
    stubs context_tracker so module-level imports don't fail, forces a
    fresh import each call.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

    original_context_tracker = sys.modules.get("context_tracker")
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    stub_ct.find_active_jsonl = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    module_name = "engram_session_start_hook_for_tool_capability_block_test"
    for mod_name in list(sys.modules.keys()):
        if mod_name == module_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(module_name, _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if original_context_tracker is None:
            sys.modules.pop("context_tracker", None)
        else:
            sys.modules["context_tracker"] = original_context_tracker
    # engram-alpha#127: pin detect_harness() to "claude-code" (measured
    # budget=9,500) -- this dev/source checkout has no baked platform.json
    # (see _harness.py's own docstring), so the hook's own detect_harness()
    # would otherwise return None and get_harness_budgets(None) would fall
    # through to UNMEASURED_HARNESS_FLOOR (4,000), which is smaller than
    # this file's fixtures were written against. Same discipline as
    # tests/test_lesson_tripwire_injection_bounds.py's own
    # _load_harness_budgets() docstring.
    module.detect_harness = lambda: "claude-code"
    return module


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


# ---------------------------------------------------------------------------
# Unit tests on tool_capability_index_block() / _resolve_tool_capability_index_path()
# ---------------------------------------------------------------------------

class TestToolCapabilityIndexBlockHelper:
    def test_renders_header_and_entries_when_artifact_exists(self, engram_home, tmp_path):
        mod = _load_session_start_module(engram_home)
        artifact = tmp_path / "fake_tool_capability_index.txt"
        artifact.write_text(
            "engram_query: Semantic+keyword search; refreshes recall.\n"
            "engram_list: Mechanical filter, no aggregation.\n",
            encoding="utf-8",
        )
        with patch.object(mod, "_resolve_tool_capability_index_path", return_value=str(artifact)):
            result = mod.tool_capability_index_block()

        assert result != ""
        assert "ENGRAM tools" in result
        assert "  engram_query: Semantic+keyword search; refreshes recall." in result
        assert "  engram_list: Mechanical filter, no aggregation." in result

    def test_missing_artifact_returns_empty_string(self, engram_home, tmp_path):
        mod = _load_session_start_module(engram_home)
        missing_path = tmp_path / "does_not_exist.txt"
        with patch.object(mod, "_resolve_tool_capability_index_path", return_value=str(missing_path)):
            result = mod.tool_capability_index_block()
        assert result == ""

    def test_empty_artifact_returns_empty_string(self, engram_home, tmp_path):
        mod = _load_session_start_module(engram_home)
        empty_artifact = tmp_path / "empty.txt"
        empty_artifact.write_text("", encoding="utf-8")
        with patch.object(mod, "_resolve_tool_capability_index_path", return_value=str(empty_artifact)):
            result = mod.tool_capability_index_block()
        assert result == ""

    def test_whitespace_only_artifact_returns_empty_string(self, engram_home, tmp_path):
        mod = _load_session_start_module(engram_home)
        ws_artifact = tmp_path / "whitespace.txt"
        ws_artifact.write_text("\n\n   \n", encoding="utf-8")
        with patch.object(mod, "_resolve_tool_capability_index_path", return_value=str(ws_artifact)):
            result = mod.tool_capability_index_block()
        assert result == ""

    def test_resolver_exception_degrades_to_empty_string_not_a_raise(self, engram_home):
        """If path resolution itself blows up, the block must swallow it —
        never propagate into main() (SessionStart must never crash the
        session over this optional context surface)."""
        mod = _load_session_start_module(engram_home)
        with patch.object(
            mod, "_resolve_tool_capability_index_path", side_effect=RuntimeError("boom")
        ):
            result = mod.tool_capability_index_block()
        assert result == ""

    def test_unreadable_file_degrades_to_empty_string(self, engram_home, tmp_path):
        """A path that exists but can't be opened as text (e.g. it's a
        directory) must also degrade gracefully."""
        mod = _load_session_start_module(engram_home)
        a_directory = tmp_path / "not_a_file"
        a_directory.mkdir()
        with patch.object(mod, "_resolve_tool_capability_index_path", return_value=str(a_directory)):
            result = mod.tool_capability_index_block()
        assert result == ""

    def test_resolver_finds_the_real_checked_in_artifact_in_dev_layout(self, engram_home):
        """Sanity check against the ACTUAL repo layout — not mocked. This is
        the resolver's real job: find src/engram/tool_capability_index.txt
        from the hook's real location at
        src/engram/hooks/claude/engram-session-start-hook.py."""
        if not _REAL_ARTIFACT_PATH.exists():
            pytest.skip(
                "src/engram/tool_capability_index.txt not present — "
                "run tools/engram-generate-tool-capability-index.py first."
            )
        mod = _load_session_start_module(engram_home)
        resolved = mod._resolve_tool_capability_index_path()
        assert os.path.exists(resolved), f"resolver returned a nonexistent path: {resolved}"
        assert os.path.realpath(resolved) == os.path.realpath(str(_REAL_ARTIFACT_PATH))

    def test_block_renders_from_the_real_checked_in_artifact(self, engram_home):
        """End-to-end (no mocking) against the real checked-in artifact:
        every line should start with 'engram_' since every ENGRAM tool name
        does, and the header must be present."""
        if not _REAL_ARTIFACT_PATH.exists():
            pytest.skip(
                "src/engram/tool_capability_index.txt not present — "
                "run tools/engram-generate-tool-capability-index.py first."
            )
        mod = _load_session_start_module(engram_home)
        result = mod.tool_capability_index_block()
        assert result != ""
        assert result.startswith("ENGRAM tools — what each CAN do")
        body_lines = result.splitlines()[1:]
        assert body_lines, "expected at least one tool entry line"
        for line in body_lines:
            stripped = line.strip()
            assert stripped.startswith("engram_"), f"unexpected line shape: {line!r}"


# ---------------------------------------------------------------------------
# End-to-end: main() still emits valid JSON with the block wired in
# ---------------------------------------------------------------------------

def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


class TestToolCapabilityIndexBlockEndToEnd:
    def test_main_includes_block_when_real_artifact_present(self, engram_home, tmp_path):
        if not _REAL_ARTIFACT_PATH.exists():
            pytest.skip(
                "src/engram/tool_capability_index.txt not present — "
                "run tools/engram-generate-tool-capability-index.py first."
            )
        mod = _load_session_start_module(engram_home)
        with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(
            side_effect=RuntimeError("no client in this test")
        ))}):
            result = _run_hook_main(
                mod,
                {
                    "session_id": "tool-cap-block-e2e",
                    "transcript_path": str(tmp_path / "transcript.jsonl"),
                    "source": "startup",
                },
            )
        ctx = result["hookSpecificOutput"]["additionalContext"]
        if "ENGRAM tools — what each CAN do" in ctx:
            assert "engram_query:" in ctx
        else:
            # engram-alpha#127: the real, checked-in tool_capability_index.txt
            # artifact is a P3 (lowest-priority) block and can exceed the
            # hook's own 9,500-char TOTAL emission budget entirely on its
            # own — when that happens it is CUT, not silently dropped:
            # emit_context()'s own overflow-file contract guarantees the
            # full text is recoverable via the trailer's "-- see <path>"
            # pointer. Assert the recovery path instead of inline presence.
            match = re.search(r"— see (\S+)$", ctx.splitlines()[0])
            assert match, (
                f"block missing from ctx AND no overflow pointer in the "
                f"trailer (line 1): {ctx.splitlines()[0]!r}"
            )
            overflow_text = Path(match.group(1)).read_text(encoding="utf-8")
            assert "ENGRAM tools — what each CAN do" in overflow_text
            assert "engram_query:" in overflow_text

    def test_main_emits_valid_json_when_block_resolver_raises(self, engram_home, tmp_path):
        """Even if the block's internals explode, main() must still emit
        valid JSON — no unhandled exception escapes to the hook's stdout."""
        mod = _load_session_start_module(engram_home)
        with patch.object(
            mod, "_resolve_tool_capability_index_path", side_effect=RuntimeError("boom")
        ), patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(
            side_effect=RuntimeError("no client in this test")
        ))}):
            result = _run_hook_main(
                mod,
                {
                    "session_id": "tool-cap-block-e2e-fail",
                    "transcript_path": str(tmp_path / "transcript.jsonl"),
                    "source": "startup",
                },
            )
        assert "hookSpecificOutput" in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "ENGRAM tools — what each CAN do" not in ctx
