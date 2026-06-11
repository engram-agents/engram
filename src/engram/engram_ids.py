"""Canonical node-id matching for ENGRAM.

Single source of truth for the node-id regex used across server.py, hooks,
and tools. ENGRAM node IDs are emitted by `server._next_id` as a 2-letter
type prefix + underscore + zero-padded numeric counter:

    f"{prefix}_{num:04d}"

The :04d format guarantees a 4-digit minimum width but does NOT cap the
width: once a type-counter passes 9999 the IDs become 5-digit
(`ob_NNNN` and so on). Any regex that hard-codes `\\d{4}` will silently
miss those IDs as the graph grows. NODE_ID_RE uses `\\d{4,}` to keep
current zero-padding semantics while allowing unbounded growth.

Scope of matching
-----------------
NODE_ID_RE is a SHAPE matcher, not a semantic validator. The `[a-z]{2}`
prefix is exact two-letter (today's TYPE_PREFIX values are all 2-letter
across observation/derivation/axiom/etc.; widening to `{2,}` would let
arbitrary `xxx_NNNN` tokens through). Note the false-positive surface:
any `xx_NNNN` token matches even if `xx` isn't a real TYPE_PREFIX
(e.g. `in_NNNN` in URL fragments). Consumers that need
prefix-validation (is this a known node type?) should layer it on top
of the regex — typically by intersecting matches with `recall_window`
or by joining against the substrate's TYPE_PREFIX set.

Examples
--------
Note: doctests use ≥4-digit IDs (matching ``\\d{4,}``) — schema-illustration,
not developer-graph citations. See ``check-no-new-shipped-node-ids.yml`` EXCLUDES.

    >>> from engram_ids import NODE_ID_RE, find_node_ids
    >>> NODE_ID_RE.findall("see ob_1234 and dv_0567")
    ['ob_1234', 'dv_0567']
    >>> find_node_ids("ob_1234 then ob_1234 then ax_0001")
    ['ob_1234', 'ax_0001']

Use sites:
  - tools/mine_prompts.py — prompt-history mining
  - hooks/claude/engram-utility-credit-stop.py — response-scan utility
    credit-assignment (alpha #177 area 4, planned)
  - server.py — future call sites that need to validate or extract IDs
"""

from __future__ import annotations

import re


# Word-bounded match: 2 lowercase letters, underscore, >=4 digits.
# The `\d{4,}` (not `\d{4}`) allows the regex to keep matching once any
# type-counter passes 9999 — see module docstring for rationale.
NODE_ID_RE: re.Pattern[str] = re.compile(r"\b([a-z]{2}_\d{4,})\b")


def find_node_ids(text: str) -> list[str]:
    """Extract ENGRAM node IDs from `text`, in first-occurrence order, deduplicated.

    Returns a list of IDs; an empty list if none match. Order is preserved
    so callers can correlate with position-in-response if needed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in NODE_ID_RE.findall(text):
        if match not in seen:
            seen.add(match)
            out.append(match)
    return out
