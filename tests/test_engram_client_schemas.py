"""Guard test: every TOOL_SCHEMAS entry must be on the payload_json signature.

Invariant: for every tool in TOOL_SCHEMAS, its `parameters` dict must have
EITHER zero keys (no-arg tools — engram_diagnose, engram_list_focused, etc.)
OR exactly one key named 'payload_json' (single-payload tools — all arg-bearing
tools post wave-3 migration).

No schema may carry legacy multi-arg / named-field parameters. This test fails
CI if anyone adds a non-payload_json tool schema in future.

Origin: issue #466 — engram_client.py TOOL_SCHEMAS for the 7 read tools
(engram_surface, engram_inspect, engram_query, engram_query_pattern,
engram_get_subgraph, engram_history, engram_list) were left with legacy
multi-key `parameters` after server.py completed its wave-3 payload_json
migration. EngramClient.call() only auto-wraps when the schema has exactly
["payload_json"] as its keys, so calls like
  client.call("engram_inspect", {"node_id": "ob_0001"})
were passing node_id= as a raw kwarg to the server, which rejected it with
"unexpected keyword argument 'node_id'".
"""

import sys
import os

# Ensure the project root is on the path so engram_client is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_client import TOOL_SCHEMAS


def test_all_schemas_use_payload_json_or_empty():
    """Every TOOL_SCHEMAS entry has parameters == {} or == {"payload_json": ...}.

    No legacy multi-key parameter dicts are allowed. This is the wave-3 contract:
    all arg-bearing tools use the single payload_json envelope; no-arg tools have
    empty parameters.
    """
    violations = []
    for tool_name, schema in TOOL_SCHEMAS.items():
        keys = list(schema.get("parameters", {}).keys())
        if keys == [] or keys == ["payload_json"]:
            continue  # valid: no-arg or single-payload
        violations.append(
            f"{tool_name}: parameters keys = {keys!r} "
            "(expected [] for no-arg or ['payload_json'] for single-payload)"
        )

    assert not violations, (
        "The following tools have legacy multi-arg parameter schemas — "
        "migrate them to payload_json (see issue #466 for context):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_payload_json_entries_have_string_type():
    """Every payload_json parameter entry must declare type='string'.

    The auto-wrap path in EngramClient.call() serializes the caller's dict as
    JSON and passes it as a string; the server json.loads() it. If the schema
    declared a non-string type, LLM tool-declaration generators would produce
    incorrect type annotations.
    """
    wrong_type = []
    for tool_name, schema in TOOL_SCHEMAS.items():
        pj = schema.get("parameters", {}).get("payload_json")
        if pj is None:
            continue  # no-arg tool — skip
        t = pj.get("type")
        if t != "string":
            wrong_type.append(
                f"{tool_name}: payload_json.type = {t!r} (expected 'string')"
            )

    assert not wrong_type, (
        "The following payload_json entries have wrong type declaration:\n"
        + "\n".join(f"  - {v}" for v in wrong_type)
    )
