#!/usr/bin/env python3
"""Dump the full MCP tool schema surface as deterministic JSON.

The behavior-preservation gate for the #872 modularization waves: the
published tool contract (names, descriptions/docstrings, annotations,
input/output schemas) must be byte-identical before and after each
extraction wave. Run on both sides and diff:

    ENGRAM_HOME=$(mktemp -d) ENGRAM_NO_EMBEDDINGS=1 \
        <venv-python> tools/dump_mcp_schema.py > /tmp/schema-before.json
    ... apply wave ...
    ENGRAM_HOME=$(mktemp -d) ENGRAM_NO_EMBEDDINGS=1 \
        <venv-python> tools/dump_mcp_schema.py > /tmp/schema-after.json
    diff /tmp/schema-before.json /tmp/schema-after.json   # must be empty

Importing server.py registers the tools but does not touch the data dir
(no _ensure_data_dir / _get_db at import time); ENGRAM_HOME is still
pointed at a temp dir by the invocation above as belt-and-suspenders.

Fields dumped per tool: name, title, description, annotations, parameters
(input schema), output_schema. Volatile/runtime-only fields (timeouts,
serializers, task_config, tags) are excluded — they are not part of the
published contract.
"""

import asyncio
import json
import sys
from pathlib import Path

# Run from anywhere: server.py lives at the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    import server  # noqa: E402 — registers all @mcp.tool functions on import

    tools = asyncio.run(server.mcp.list_tools())

    surface = []
    for t in tools:
        d = t.model_dump()
        surface.append(
            {
                "name": d.get("name"),
                "title": d.get("title"),
                "description": d.get("description"),
                "annotations": d.get("annotations"),
                "parameters": d.get("parameters"),
                "output_schema": d.get("output_schema"),
            }
        )

    surface.sort(key=lambda e: e["name"] or "")
    json.dump(surface, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
