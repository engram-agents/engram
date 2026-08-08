"""Required-kwarg enforcement for @mcp.tool()'s capability= parameter
(issue #1958).

Every real @mcp.tool() call site in server.py already carries capability=
(see tests/test_tool_capability_index.py's coverage check), so this test
cannot remove it from a production tool without breaking the server. Instead
it exercises server._timing_mcp_tool directly — the actual decorator
server.py installs as mcp.tool — against THROWAWAY tool definitions that are
never registered as real ENGRAM tools. This is the isolated-harness approach
the spec calls for.

Covers:
  1. A @mcp.tool() call missing capability= entirely raises ValueError at
     decoration time (i.e. at the `def` statement, not at call time).
  2. A @mcp.tool() call with a blank/whitespace-only capability= also raises
     ValueError (empty string is not a real boundary statement).
  3. A @mcp.tool() call with a non-string capability= (e.g. None) raises
     ValueError.
  4. A well-formed capability= call succeeds, is recorded into
     TOOL_CAPABILITIES keyed by the tool's registered name, and FastMCP's
     real decorator never sees the capability kwarg (no TypeError from
     the underlying fastmcp machinery, which does not recognize it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ENGRAM = REPO_ROOT / "src" / "engram"


def _fresh_server_module():
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    for p in (str(SRC_ENGRAM), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")
    import server
    return server


_ANNOTATIONS = {
    "title": "Throwaway Test Tool",
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@pytest.fixture(scope="module")
def server_mod():
    return _fresh_server_module()


class TestCapabilityKwargRequired:
    def test_missing_capability_raises_value_error_at_decoration_time(self, server_mod):
        with pytest.raises(ValueError, match="capability"):
            @server_mod.mcp.tool(
                name="engram_test_missing_capability_1958",
                annotations=_ANNOTATIONS,
            )
            def _throwaway_missing():
                return "unused"

    def test_blank_capability_raises_value_error(self, server_mod):
        with pytest.raises(ValueError, match="non-empty"):
            @server_mod.mcp.tool(
                name="engram_test_blank_capability_1958",
                capability="   ",
                annotations=_ANNOTATIONS,
            )
            def _throwaway_blank():
                return "unused"

    def test_non_string_capability_raises_value_error(self, server_mod):
        with pytest.raises(ValueError, match="non-empty"):
            @server_mod.mcp.tool(
                name="engram_test_nonstring_capability_1958",
                capability=None,
                annotations=_ANNOTATIONS,
            )
            def _throwaway_nonstring():
                return "unused"

    def test_well_formed_capability_is_recorded_and_fastmcp_accepts_the_call(
        self, server_mod
    ):
        tool_name = "engram_test_well_formed_capability_1958"

        @server_mod.mcp.tool(
            name=tool_name,
            capability="Throwaway test tool — does X, never Y.",
            annotations=_ANNOTATIONS,
        )
        def _throwaway_ok():
            return "unused"

        assert server_mod.TOOL_CAPABILITIES.get(tool_name) == (
            "Throwaway test tool — does X, never Y."
        )
