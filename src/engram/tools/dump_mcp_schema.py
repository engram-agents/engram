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

Default invocation (no flags) prints the full JSON dump above, unchanged
since this tool's introduction — that output is the #872 diff gate and must
stay byte-identical.

--summary mode (added for the #2855 MCP-description-trim series): prints a
character-budget report instead of the JSON dump, using the SAME in-process
schema (no behavior change to the default path). This is a MEASUREMENT of
the tree this script is run from — not a claim about any other tree,
checkout, or deployed plugin copy. Run it on two trees and diff the reports
to compare them; a single run only describes its own tree.

Per tool, the report computes:
    injected_chars     = len(name) + len(description)
                          + len(json.dumps(parameters, separators=(",", ":")))
                        (the "parameters" field is the tool's full input JSON
                        schema, exactly as returned by list_tools(); this is
                        the harness-injected subset — the same three fields
                        dump_mcp_schema.py's own JSON dump already carries)
    description_chars  = len(description)          (the tool's own docstring)
    param_desc_chars   = sum of len(v) for every string found under a
                          "description" key anywhere inside "parameters",
                          searched recursively (covers nested schema shapes,
                          not just a flat top-level "description")

Tools are ranked descending by injected_chars. The report also prints the
totals across all tools (this is the "58-tool surface total" figure used in
the #2855 PR series) and the cumulative injected_chars share held by the
top-1/5/10/20 tools by that ranking.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Run from anywhere: server.py lives at the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _sum_description_lengths(obj) -> int:
    """Recursively sum len() of every string found under a "description" key.

    Walks dicts and lists. A non-string value under a "description" key
    (shouldn't occur in a JSON schema, but defensive) is skipped rather than
    raising.
    """
    total = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "description" and isinstance(value, str):
                total += len(value)
            else:
                total += _sum_description_lengths(value)
    elif isinstance(obj, list):
        for item in obj:
            total += _sum_description_lengths(item)
    return total


def _tool_metrics(entry: dict) -> dict:
    """Compute the injected-chars / description-chars / param-desc-chars
    triple for one tool's schema entry (see module docstring for the exact
    formula each field uses)."""
    name = entry.get("name") or ""
    description = entry.get("description") or ""
    parameters = entry.get("parameters")
    params_json = json.dumps(parameters, separators=(",", ":"), default=str)
    return {
        "name": name,
        "injected_chars": len(name) + len(description) + len(params_json),
        "description_chars": len(description),
        "param_desc_chars": _sum_description_lengths(parameters),
    }


def _dump_schema() -> list:
    """Import server.py and return the sorted per-tool schema-entry list —
    the shared in-process data source for both the default JSON dump and
    --summary mode."""
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
    return surface


def _print_summary(surface: list) -> None:
    metrics = [_tool_metrics(e) for e in surface]
    metrics.sort(key=lambda m: (-m["injected_chars"], m["name"]))

    print(f"# dump_mcp_schema.py --summary — measuring THIS tree ({len(metrics)} tools)")
    print("# injected_chars = len(name) + len(description) "
          '+ len(json.dumps(parameters, separators=(",", ":")))')
    print()
    print(f"{'tool':<40} {'injected':>10} {'description':>12} {'param_desc':>11}")
    for m in metrics:
        print(
            f"{m['name']:<40} {m['injected_chars']:>10} "
            f"{m['description_chars']:>12} {m['param_desc_chars']:>11}"
        )

    total_injected = sum(m["injected_chars"] for m in metrics)
    total_description = sum(m["description_chars"] for m in metrics)
    total_param_desc = sum(m["param_desc_chars"] for m in metrics)
    print()
    print(f"TOTAL across {len(metrics)} tools:")
    print(f"  injected_chars total:   {total_injected}")
    print(f"  description_chars total: {total_description}")
    print(f"  param_desc_chars total:  {total_param_desc}")

    print()
    print("Cumulative injected_chars share by top-N tools (this ranking):")
    for n in (1, 5, 10, 20):
        top_n = metrics[:n]
        share = sum(m["injected_chars"] for m in top_n)
        pct = (share / total_injected * 100) if total_injected else 0.0
        print(f"  top {n:>2}: {share:>7} chars ({pct:5.1f}% of surface total)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dump the MCP tool schema surface as JSON (default), or print a "
            "per-tool character-budget report (--summary). Both modes read "
            "the SAME in-process schema for the tree this script is run "
            "from — this is a measurement of THIS tree only, never a claim "
            "about any other checkout or a deployed plugin copy."
        ),
        epilog=(
            "--summary formula: injected_chars = len(name) + len(description) "
            '+ len(json.dumps(parameters, separators=(",", ":"))); '
            "description_chars = len(description); param_desc_chars = sum of "
            'len(v) for every string under a "description" key anywhere '
            "inside parameters, recursively. Tools are ranked descending by "
            "injected_chars; totals and top-1/5/10/20 cumulative share are "
            "computed over that ranking."
        ),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Print the character-budget report instead of the JSON dump. "
            "Does not change the default (no-flag) output."
        ),
    )
    args = parser.parse_args()

    surface = _dump_schema()

    if args.summary:
        _print_summary(surface)
        return

    json.dump(surface, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
