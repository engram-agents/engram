#!/usr/bin/env python3
"""engram-generate-tool-capability-index.py — regenerate the checked-in
tool-capability-index artifact from server.py's live TOOL_CAPABILITIES dict
(issue #1958).

Usage:
    python tools/engram-generate-tool-capability-index.py
    python tools/engram-generate-tool-capability-index.py --check

When to run:
    Any time a @mcp.tool() call site's capability= text changes, or a tool
    is added to / removed from server.py. The CI drift-guard test
    (tests/test_tool_capability_index.py) fails if the checked-in artifact
    no longer matches a fresh generation — re-run this script and commit the
    regenerated artifact.

Why this exists (background):
    Claude Code defers MCP tool schemas: at decision time an agent sees only
    ENGRAM's ~58 tool NAMES, not descriptions or parameters. Learning what a
    tool CAN do costs a full schema fetch via ToolSearch. The SessionStart
    hook (src/engram/hooks/claude/engram-session-start-hook.py) injects a
    short capability-boundary line per tool to close that gap — but the hook
    is deliberately fastmcp-free (importing server.py would pull in
    fastmcp's ~150-package dependency chain into the lightweight hook
    process). This script is the maintainer/CI-time bridge: it runs in the
    full venv (fastmcp available), reads the live TOOL_CAPABILITIES dict
    that server.py populates at @mcp.tool() decoration time, and writes a
    small static text artifact the hook can read with zero heavy imports.

What it does:
    1. Imports server.py for real, from THIS repo's own source tree
       (src/engram/server.py) — not an installed ~/.engram runtime. The
       artifact this script produces IS repo source, so it must always
       reflect the tree it lives in.
    2. Reads the live, populated TOOL_CAPABILITIES dict (populated once per
       @mcp.tool() call, at decoration/import time — see server.py's
       _timing_mcp_tool, which also enforces that every call site supplies
       capability= or the import itself fails loudly).
    3. Cross-checks TOOL_CAPABILITIES against the actually-registered tool
       set via asyncio.run(mcp.list_tools()): every registered tool name
       must appear in TOOL_CAPABILITIES, and vice versa. Raises RuntimeError
       (non-zero exit) on any mismatch in either direction — this is the
       coverage guarantee, not an incidental check.
    4. Writes a small, deterministic, sorted-by-name plain-text artifact to
       src/engram/tool_capability_index.txt (ROOT-LEVEL, not a generated/
       subdirectory — build.py's plugin copier only has prefix-specific
       sections for tools/, templates/, hooks/, skills/, agents/, and
       output-styles/; a generated/ subdirectory would fall through every
       one of them silently. Root-level manifest entries — no "/" in the
       path — are the one category the copier derives automatically with
       no hardcoded list, so that's where this artifact ships from) — one
       line per tool:
           <tool_name>: <capability text>
       Plain text (not JSON): the SessionStart hook injects this near-
       verbatim with minimal parsing.

Idempotent:
    Running this twice with no source changes produces byte-identical
    output — the artifact carries no timestamps or other non-deterministic
    fields.

--check mode:
    Computes the artifact text in-memory and compares it against the
    checked-in file WITHOUT writing anything. Exits 0 if identical, 1 (with
    a diagnostic message on stderr) if the checked-in file is stale or
    missing. Used as an alternative to reimplementing the generation logic
    inline in a drift-guard test.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
ARTIFACT_PATH = SRC_ENGRAM / "tool_capability_index.txt"


def _import_server():
    """Import server.py from THIS repo's source tree (dev-repo layout).

    Mirrors tests/conftest.py's sys.path setup (src/engram, then repo root)
    rather than the ENGRAM_HOME-resolution dance the runtime data-repair
    tools (engram-regenerate-embeddings.py, engram-backfill-special-moment-
    edges.py) use — those scripts operate on an INSTALLED ~/.engram runtime;
    this script always targets the checked-in source tree it lives in.
    """
    for path in (str(SRC_ENGRAM), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    # Matches tests/conftest.py's ENGRAM_NO_DB_GUARDS bypass — this script
    # never touches a real knowledge.db, but importing server.py at module
    # level runs enough setup that the fail-loud DB guards should stay off.
    os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")

    # Purge server + engram_* family modules first so a stale import from
    # elsewhere in the process (unlikely for a standalone script invocation,
    # but keeps this function safe to call more than once, e.g. from a test)
    # can't shadow the fresh one.
    for mod_name in list(sys.modules):
        if mod_name == "server" or mod_name.startswith("engram_"):
            del sys.modules[mod_name]

    import server
    return server


def generate_index_text(server) -> str:
    """Build the deterministic artifact text from a freshly-imported server module.

    Cross-checks TOOL_CAPABILITIES against the live registered tool set
    (mcp.list_tools()) and raises RuntimeError on any mismatch in either
    direction — refuses to produce a silently incomplete or stale index.
    """
    registered_tools = asyncio.run(server.mcp.list_tools())
    registered_names = {t.name for t in registered_tools}
    capability_names = set(server.TOOL_CAPABILITIES)

    missing_capability = sorted(registered_names - capability_names)
    orphan_capability = sorted(capability_names - registered_names)
    if missing_capability or orphan_capability:
        raise RuntimeError(
            "TOOL_CAPABILITIES is out of sync with the registered tool set.\n"
            f"  Registered tools with no capability= entry: {missing_capability}\n"
            f"  TOOL_CAPABILITIES entries with no matching registered tool: "
            f"{orphan_capability}\n"
            "Every @mcp.tool() call site must carry a capability= kwarg, and "
            "every capability= entry must correspond to a real registered tool."
        )

    lines = [
        f"{name}: {server.TOOL_CAPABILITIES[name]}"
        for name in sorted(capability_names)
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate the checked-in tool-capability-index artifact from "
            "server.py's live TOOL_CAPABILITIES dict (issue #1958)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help=(
            "Verify the checked-in artifact is up to date without writing "
            "anything. Exits 1 if stale or missing."
        ),
    )
    args = parser.parse_args()

    server = _import_server()
    text = generate_index_text(server)

    if args.check:
        if not ARTIFACT_PATH.exists():
            print(f"[tool-capability-index] MISSING: {ARTIFACT_PATH}", file=sys.stderr)
            return 1
        current = ARTIFACT_PATH.read_text(encoding="utf-8")
        if current != text:
            print(
                f"[tool-capability-index] STALE: {ARTIFACT_PATH} does not "
                f"match a freshly-generated index. Re-run without --check "
                f"to regenerate, then commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"[tool-capability-index] up to date: {ARTIFACT_PATH}", file=sys.stderr)
        return 0

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(text, encoding="utf-8")
    print(
        f"[tool-capability-index] wrote {len(server.TOOL_CAPABILITIES)} "
        f"entries to {ARTIFACT_PATH}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
