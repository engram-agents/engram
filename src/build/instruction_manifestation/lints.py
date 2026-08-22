"""The three PR2-scope build-time lints for the instruction-manifestation
engine (#2140). Lint (d) (the no-shipped-node-ids gate) is PR3's job and is
NOT implemented here.

Each lint function returns a list of violation strings (empty list = clean)
-- it does NOT raise. Raising / exiting non-zero on a non-empty result is
the caller's decision (a CI entrypoint or, for PR2, the test suite that
exercises these functions directly against both clean fixtures and
deliberately-planted violations).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import builder
from . import schema


def lint_base_never_names_tool(components_dir: Path, harnesses_dir: Path) -> list[str]:
    """Lint (a) -- base-never-names-a-tool.

    A BASE component's body must never literally name a tool from the
    UNION of every harness contract's ``tool_names`` registry. Extension
    bodies are never checked here -- they're explicitly allowed to name
    harness-specific tools (PR2 spec sec. 5, lint (a)).

    Implemented with Python's ``re`` module, deliberately NOT a shell
    ``git grep -E`` call: ``git grep -E`` with ``\\b`` word-boundary
    assertions is silently INERT on macOS (BSD's regcomp has no ``\\b``
    support -- no error, the pattern just matches nothing), which would
    defeat this lint's entire purpose on any macOS dev seat (discovered
    live 2026-08-22; see Kepler's spec-addendum comment on #2140). Python's
    ``re.search(r'\\b' + re.escape(name) + r'\\b', body)`` implements
    ``\\b`` correctly and platform-independently.
    """
    all_tool_names: set[str] = set()
    for harness_path in sorted(harnesses_dir.glob("*.yaml")):
        contract = schema.parse_harness_contract(harness_path)
        for name in contract.get("tool_names") or []:
            if isinstance(name, str):
                all_tool_names.add(name)

    violations: list[str] = []
    for path in sorted(components_dir.glob("*.md")):
        frontmatter, body = schema.parse_component_file(path)
        if frontmatter.get("kind") != "base":
            continue
        for name in sorted(all_tool_names):
            if re.search(r"\b" + re.escape(name) + r"\b", body):
                violations.append(
                    f"{path.name}: base component body names harness tool "
                    f"{name!r} (base bodies must stay harness-neutral -- "
                    f"move this content to a harness-specific extension)"
                )
    return violations


def lint_unresolved_variables(components_dir: Path, harnesses_dir: Path) -> list[str]:
    """Lint (b) -- unresolved-variable check.

    For every component body, find all ``{{...}}``-shaped tokens. Every
    token found must (1) appear in that component's own ``variables:``
    frontmatter list (a body referencing a variable it didn't declare is a
    violation -- the declaration is a real contract, not decoration), AND
    (2) be resolvable in at least one harness contract's ``variables:`` map
    (a variable name no harness actually defines is also a violation -- it
    can never render cleanly).
    """
    all_harness_vars: set[str] = set()
    for harness_path in sorted(harnesses_dir.glob("*.yaml")):
        contract = schema.parse_harness_contract(harness_path)
        variables = contract.get("variables") or {}
        if isinstance(variables, dict):
            all_harness_vars.update(variables.keys())

    violations: list[str] = []
    for path in sorted(components_dir.glob("*.md")):
        frontmatter, body = schema.parse_component_file(path)
        declared = frontmatter.get("variables") or []
        declared_set = set(declared) if isinstance(declared, list) else set()

        used_tokens = sorted(set(builder.TOKEN_RE.findall(body)))
        for token in used_tokens:
            if token not in declared_set:
                violations.append(
                    f"{path.name}: body references {{{{{token}}}}} which "
                    f"is not declared in this component's variables: "
                    f"frontmatter"
                )
            if token not in all_harness_vars:
                violations.append(
                    f"{path.name}: variable {{{{{token}}}}} is not "
                    f"defined in any harness contract's variables: map "
                    f"(can never render cleanly)"
                )
    return violations


def lint_extension_required_coverage(
    components_dir: Path, harnesses_dir: Path
) -> list[str]:
    """Lint (c) -- extension_required coverage.

    For every enabled harness and every base component with
    ``extension_required: true`` in that harness's ``components:`` list,
    confirm a matching extension component exists for that harness. Reports
    a violation naming BOTH the harness and the component id for any gap --
    mirrors the builder's own build-failure message, but fires at
    lint/CI time, before any build attempt.
    """
    components = schema.load_all_components(components_dir)
    violations: list[str] = []

    for harness_path in sorted(harnesses_dir.glob("*.yaml")):
        contract = schema.parse_harness_contract(harness_path)
        harness_name = contract.get("name", harness_path.stem)

        for entry in contract.get("components", []):
            base_id = entry["id"]
            if base_id not in components:
                # A missing component file is a harness-contract validation
                # concern (schema.validate_harness_contract), not this
                # lint's job.
                continue
            base_fm, _body, _path = components[base_id]
            if not base_fm.get("extension_required"):
                continue

            extension_spec = entry.get("extension", "auto")
            try:
                ext_id, _ext_body = builder.find_extension(
                    components, base_id, harness_name, extension_spec
                )
            except builder.BuildError:
                ext_id = None

            if ext_id is None:
                violations.append(
                    f"harness {harness_name!r} component {base_id!r}: "
                    f"extension_required: true but no matching extension "
                    f"component exists for this harness"
                )
    return violations
