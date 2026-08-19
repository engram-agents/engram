"""Drift guard + coverage check for the generated tool-capability-index
artifact (issue #1958).

Claude Code defers MCP tool schemas: at decision time an agent has ENGRAM's
~56 tool NAMES and nothing else — no descriptions, no parameter info.
Learning what a tool CAN do costs a full schema fetch via ToolSearch. The
checked-in artifact at src/engram/tool_capability_index.txt (built
by tools/engram-generate-tool-capability-index.py from server.py's live
TOOL_CAPABILITIES dict) closes that gap; the SessionStart hook injects it
without importing server.py/fastmcp.

Two checks:
  1. Drift guard — the checked-in artifact must be byte-identical to what
     the generator would produce RIGHT NOW from server.py's live
     TOOL_CAPABILITIES. If a capability= string changes, or a tool is
     added/removed, without re-running the generator, this test fails: the
     artifact the SessionStart hook injects would otherwise silently drift
     from the real tool set.
  2. Coverage check — every tool returned by the real mcp.list_tools() API
     has an entry in the checked-in artifact. This re-asserts the
     generator's own internal cross-check directly against the checked-in
     FILE (not the live dict), so a stale commit of the artifact is caught
     even if it once passed the drift guard.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
ARTIFACT_PATH = SRC_ENGRAM / "tool_capability_index.txt"
GENERATOR_SCRIPT = REPO_ROOT / "tools" / "engram-generate-tool-capability-index.py"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "engram_generate_tool_capability_index", str(GENERATOR_SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_server_module():
    """Import server.py fresh (purges any stale server/engram_* modules
    first, mirroring tests/conftest.py's fresh_module() pattern)."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")
    import server
    return server


@pytest.fixture(scope="module")
def generator_mod():
    if not GENERATOR_SCRIPT.exists():
        pytest.skip("tools/engram-generate-tool-capability-index.py not found")
    return _load_generator_module()


@pytest.fixture(scope="module")
def fresh_server():
    return _fresh_server_module()


class TestToolCapabilityIndexDriftGuard:
    """The checked-in artifact must always reflect the live source."""

    def test_checked_in_artifact_is_byte_identical_to_fresh_generation(
        self, generator_mod, fresh_server
    ):
        assert ARTIFACT_PATH.exists(), (
            f"{ARTIFACT_PATH} is missing — run "
            f"tools/engram-generate-tool-capability-index.py and commit the "
            f"result."
        )
        fresh_text = generator_mod.generate_index_text(fresh_server)
        checked_in_text = ARTIFACT_PATH.read_text(encoding="utf-8")
        assert checked_in_text == fresh_text, (
            "Checked-in src/engram/tool_capability_index.txt is "
            "stale relative to server.py's live TOOL_CAPABILITIES (a "
            "capability= string or the registered tool set changed). "
            "Re-run tools/engram-generate-tool-capability-index.py and "
            "commit the regenerated artifact."
        )

    def test_generator_is_idempotent(self, generator_mod, fresh_server):
        """Running the generation logic twice with no source changes in
        between must produce byte-identical output (spec requirement)."""
        first = generator_mod.generate_index_text(fresh_server)
        second = generator_mod.generate_index_text(fresh_server)
        assert first == second


class TestToolCapabilityIndexCoverage:
    """Every registered tool must have a checked-in capability entry."""

    def test_every_registered_tool_has_a_checked_in_capability_entry(
        self, fresh_server
    ):
        registered = asyncio.run(fresh_server.mcp.list_tools())
        registered_names = {t.name for t in registered}
        assert registered_names, "mcp.list_tools() returned no tools — suspicious."

        artifact_lines = ARTIFACT_PATH.read_text(encoding="utf-8").splitlines()
        artifact_names = set()
        for line in artifact_lines:
            if not line.strip():
                continue
            name, sep, _rest = line.partition(":")
            assert sep, f"Malformed artifact line (no ':' separator): {line!r}"
            artifact_names.add(name.strip())

        missing = sorted(registered_names - artifact_names)
        assert not missing, (
            f"Registered tools missing a checked-in capability-index entry: "
            f"{missing}. Every @mcp.tool() call site must carry capability=, "
            f"and the artifact must be regenerated after any tool addition."
        )

    def test_no_orphan_entries_for_unregistered_tools(self, fresh_server):
        """The artifact should not carry stale entries for tools that no
        longer exist (a name-only rename would otherwise silently leave a
        dead entry behind alongside the new one)."""
        registered = asyncio.run(fresh_server.mcp.list_tools())
        registered_names = {t.name for t in registered}

        artifact_lines = ARTIFACT_PATH.read_text(encoding="utf-8").splitlines()
        artifact_names = {
            line.partition(":")[0].strip()
            for line in artifact_lines
            if line.strip()
        }

        orphans = sorted(artifact_names - registered_names)
        assert not orphans, (
            f"Checked-in artifact has entries for tools no longer "
            f"registered: {orphans}. Regenerate the artifact."
        )
