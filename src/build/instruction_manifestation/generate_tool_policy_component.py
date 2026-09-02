#!/usr/bin/env python3
"""generate_tool_policy_component.py -- regenerate the checked-in
``components/tool-policy.md`` BASE component from
``deploy/tool_policy_facts.py`` (PR2 of engram-alpha#2140, ruling 4 of the
design-of-record: "tool-policy component is GENERATED, never authored").

Usage:
    python3 src/build/instruction_manifestation/generate_tool_policy_component.py
    python3 src/build/instruction_manifestation/generate_tool_policy_component.py --check

Why this exists:
    ``deploy/tool_policy_facts.py`` is already the single source of truth
    for the BANNED_TOOLS / BANNED_MARKER / ALLOWED_FAIRIES facts hand-synced
    across three existing surfaces (template.CLAUDE.md, boot-charge-
    default.md, boot-charge-quiet.md -- see that module's own docstring).
    This script generates a FOURTH consumer of that same fact table -- the
    instruction-manifestation ``tool-policy`` base component -- directly
    from it, so the fact table stays the one place to edit rather than
    someone hand-typing a fourth copy that can silently drift.

Why the generated BASE body never names a literal banned tool:
    "Workflow" is both a ``BANNED_TOOLS`` fact AND a real Claude-Code
    ``tool_names`` registry entry (see harnesses/claude.yaml) -- lint (a)
    (base-never-names-a-tool) would reject a base body that spelled it out
    literally. The concrete banned-tool names therefore live in the
    claude-harness EXTENSION component (components/tool-policy-claude.md,
    hand-authored, explicitly exempt from lint (a) per the design-of-
    record), not here. This generator only emits the structural/pointer
    content that's safe in a harness-neutral base: the BANNED marker and
    the allowed-fairy id/alias list (no fairy alias collides with any
    tool_names registry entry -- they're all hyphenated lowercase, the
    registry is unhyphenated CamelCase).

--check mode:
    Computes the component text in-memory and compares it against the
    checked-in file WITHOUT writing anything. Exits 0 if identical, 1 (with
    a diagnostic on stderr) if the checked-in file is stale or missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parents[2]
COMPONENT_PATH = _HERE / "components" / "tool-policy.md"


def _import_facts():
    """Import deploy.tool_policy_facts from THIS repo's source tree.

    Mirrors tools/engram-generate-tool-capability-index.py's
    sys.path-priming pattern for a standalone-script invocation (under
    pytest, tests/conftest.py already puts REPO_ROOT on sys.path, so this
    is a no-op there).
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from deploy import tool_policy_facts

    return tool_policy_facts


def generate_component_text(facts) -> str:
    """Build the deterministic ``tool-policy.md`` text (frontmatter + body)
    from the ``tool_policy_facts`` module.

    Idempotent -- no timestamps or other non-deterministic content, so
    running this twice with no source changes in between produces
    byte-identical output.
    """
    fairy_summary = ", ".join(
        f"{fairy_id} ({'/'.join(aliases)})"
        for fairy_id, aliases in facts.ALLOWED_FAIRIES.items()
    )

    body = (
        "Tool usage policy for this harness. Certain harness-native tools "
        f"are {facts.BANNED_MARKER} outright -- the reason is recorded so "
        "the ban is never relaxed by someone who doesn't know the cost -- "
        "and a fixed set of engram-* fairies is the allowed delegation "
        f"mechanism: {fairy_summary}. The concrete tool identifiers this "
        "policy names differ per harness and are never spelled out here "
        "(this base body stays harness-neutral by construction; lint (a) "
        "enforces it) -- see this harness's own tool-policy extension "
        "component for the specifics."
    )

    frontmatter_lines = [
        "---",
        "id: tool-policy",
        "kind: base",
        "composition_mode: flat",
        "extension_required: true",
        "---",
        "",
    ]
    return "\n".join(frontmatter_lines) + body + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate the checked-in components/tool-policy.md BASE "
            "component from deploy/tool_policy_facts.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help=(
            "Verify the checked-in component is up to date without "
            "writing anything. Exits 1 if stale or missing."
        ),
    )
    args = parser.parse_args()

    facts = _import_facts()
    text = generate_component_text(facts)

    if args.check:
        if not COMPONENT_PATH.exists():
            print(f"[tool-policy-component] MISSING: {COMPONENT_PATH}", file=sys.stderr)
            return 1
        current = COMPONENT_PATH.read_text(encoding="utf-8")
        if current != text:
            print(
                f"[tool-policy-component] STALE: {COMPONENT_PATH} does not "
                f"match a fresh generation. Re-run without --check to "
                f"regenerate, then commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"[tool-policy-component] up to date: {COMPONENT_PATH}", file=sys.stderr)
        return 0

    COMPONENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPONENT_PATH.write_text(text, encoding="utf-8")
    print(f"[tool-policy-component] wrote {COMPONENT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
