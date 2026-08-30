"""instruction_manifestation -- schema + builder + lints for the componentized
identity-instruction engine (PR2 of engram-alpha#2140).

This package is PR2's contribution to the "instruction-manifestation engine"
effort (issue #2140, design-of-record: engram-alpha#2140 (spec comment,
2026-08-22), Kepler): a NEW component/harness-contract schema (base + extension
components, flat composition, per-harness contracts) distinct from and
additive to #2145's earlier, differently-shaped `src/engram/templates/
components/` corpus (`kind: rule`, `harnesses: all`, `anchor`/`order`/`merge`/
`version`) -- that corpus is untouched by this package; migrating it onto this
schema is PR3's job.

Modules:
    schema.py  -- component-file and harness-contract frontmatter parsing +
                  the schema validator (collects ALL violations per pass).
    builder.py -- the flat-composition render/build logic: base body +
                  matching extension body, {{VARIABLE}} substitution,
                  build-failure raising for extension_required gaps and
                  unresolved variable tokens.
    lints.py   -- the three PR2-scope build-time lints: (a) base-never-
                  names-a-tool, (b) unresolved-variable declaration check,
                  (c) extension_required coverage. Lint (d) (no-shipped-
                  node-ids) is PR3's job -- not implemented here.
    generate_tool_policy_component.py -- generator for the one GENERATED
                  pilot component (components/tool-policy.md), sourced from
                  deploy/tool_policy_facts.py (never hand-authored -- ruling
                  4 of the design-of-record).

Install tier: Developer (T3) -- a build-time schema/lint/generator system for
authoring instruction templates, not something that runs at agent session
time. See src/build/packaging/tiers.json for the per-file tier entries and
CLAUDE.md's "Install tiers" section for the tier discipline.
"""

from __future__ import annotations
