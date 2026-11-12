#!/usr/bin/env python3
"""Generate the model-family gate's write-tool list and its hooks.json matcher (#4596).

One source, two outputs.  The source is ``server.py``: every ``@mcp.tool(..., annotations={...})`` carries
FastMCP's ``readOnlyHint``.  A tool whose hint is ``False`` writes (it mutates the graph); ``True`` reads.
The generator reads that with ``ast`` (it never imports the server) and writes

  * ``model_gate_write_tools.json`` (plugin root): ``{"schema", "prefixes", "write_tools"}``, read by
    the PreToolUse hook ``engram-model-gate-pretool.py``;
  * the ``matcher`` of that hook's PreToolUse group in ``hooks/hooks.json``, enumerating the same names
    under both prefixes (``mcp__engram__`` for a bare ``.mcp.json`` install and
    ``mcp__plugin_engram_engram__`` for the plugin install), so a read tool or a non-ENGRAM tool never
    starts the hook and pays nothing.

A tool with no explicit boolean ``readOnlyHint`` is an ERROR, never a guess: an unannotated tool would be
silently ungated (or silently gated), so the generator names it and fails.

  python tools/gen_model_gate_write_tools.py --check    # exit 1 when a committed output has drifted
  python tools/gen_model_gate_write_tools.py --write    # regenerate both outputs in place

Run from a source checkout; ``tests/test_model_gate_4596.py`` (C8) runs the same check in CI.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

PREFIXES = ("mcp__engram__", "mcp__plugin_engram_engram__")
SCHEMA_VERSION = 1
HOOK_NAME = "engram-model-gate-pretool.py"

# tools/ sits directly under the tree root (src/engram/ in a checkout, the plugin root installed), and the
# build does not flatten it, so one level up is the root in both layouts.
_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER = _ROOT / "server.py"
DEFAULT_JSON = _ROOT / "model_gate_write_tools.json"
DEFAULT_HOOKS = _ROOT / "hooks" / "hooks.json"


def collect(server_src: str):
    """``(writes, reads)``: the sorted registered names of the tools whose readOnlyHint is False / True.
    Raises ``ValueError`` naming every tool without an explicit boolean ``readOnlyHint``."""
    tree = ast.parse(server_src)
    writes, reads, bad = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            target = call.func if call is not None else dec
            if not (isinstance(target, ast.Attribute) and target.attr == "tool"
                    and isinstance(target.value, ast.Name) and target.value.id == "mcp"):
                continue
            kwargs = {k.arg: k.value for k in call.keywords} if call is not None else {}
            name_node = kwargs.get("name")
            name = name_node.value if isinstance(name_node, ast.Constant) and isinstance(
                name_node.value, str) else node.name
            hint = None
            ann = kwargs.get("annotations")
            if isinstance(ann, ast.Dict):
                for k, v in zip(ann.keys, ann.values):
                    if isinstance(k, ast.Constant) and k.value == "readOnlyHint":
                        if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                            hint = v.value
                        break
            if hint is None:
                bad.append(name)
            elif hint:
                reads.append(name)
            else:
                writes.append(name)
    if bad:
        raise ValueError("tools without an explicit boolean readOnlyHint: " + ", ".join(sorted(bad)))
    return sorted(writes), sorted(reads)


def build_matcher(write_tools) -> str:
    names = "|".join(re.escape(n) for n in write_tools)
    prefixes = "|".join(re.escape(p) for p in PREFIXES)
    return f"^(?:{prefixes})(?:{names})$"


def build_outputs(server_src: str):
    """``(json_data, matcher)`` generated from ``server_src``."""
    writes, _reads = collect(server_src)
    data = {
        "schema": SCHEMA_VERSION,
        "generated_from": "server.py @mcp.tool(annotations={'readOnlyHint': False}); "
                          "regenerate with tools/gen_model_gate_write_tools.py --write",
        "prefixes": list(PREFIXES),
        "write_tools": writes,
    }
    return data, build_matcher(writes)


_MATCHER_RX = re.compile(
    r'("matcher"\s*:\s*")((?:[^"\\]|\\.)*)("\s*,\s*"hooks"\s*:\s*\[(?:(?!"matcher")[\s\S])*?' + re.escape(HOOK_NAME) + ")")


def _hooks_matcher(hooks_text: str):
    m = _MATCHER_RX.search(hooks_text)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when a committed output has drifted (default)")
    mode.add_argument("--write", action="store_true", help="regenerate both outputs in place")
    ap.add_argument("--server", default=str(DEFAULT_SERVER))
    ap.add_argument("--json", default=str(DEFAULT_JSON), dest="json_path")
    ap.add_argument("--hooks", default=str(DEFAULT_HOOKS))
    args = ap.parse_args(argv)
    try:
        data, matcher = build_outputs(Path(args.server).read_text(encoding="utf-8"))
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"gen_model_gate_write_tools: {exc}", file=sys.stderr)
        return 2
    json_path, hooks_path = Path(args.json_path), Path(args.hooks)
    want_json = json.dumps(data, indent=2) + "\n"
    hooks_text = hooks_path.read_text(encoding="utf-8")
    m = _hooks_matcher(hooks_text)
    if m is None:
        print(f"gen_model_gate_write_tools: no PreToolUse group for {HOOK_NAME} in {hooks_path}", file=sys.stderr)
        return 2
    new_hooks = hooks_text[:m.start(2)] + json.dumps(matcher)[1:-1] + hooks_text[m.end(2):]
    if args.write:
        json_path.write_text(want_json, encoding="utf-8")
        hooks_path.write_text(new_hooks, encoding="utf-8")
        print(f"wrote {json_path} and the matcher in {hooks_path} ({len(data['write_tools'])} write tools)")
        return 0
    problems = []
    try:
        have = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        have = None
        problems.append(f"{json_path}: unreadable ({exc})")
    if have is not None and have != data:
        problems.append(f"{json_path}: drifted from server.py's readOnlyHint annotations")
    if new_hooks != hooks_text:
        problems.append(f"{hooks_path}: the {HOOK_NAME} matcher drifted from server.py's annotations")
    for p in problems:
        print("gen_model_gate_write_tools: " + p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
