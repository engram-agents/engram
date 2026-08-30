"""Render/composition logic for the instruction-manifestation engine (PR2,
#2140).

Flat composition only (the only ``composition_mode`` this schema version
supports): for each enabled component listed under a harness contract's
``components:``, in order, find its base body, find the matching extension
body (if any -- resolved via ``extension: auto`` or an explicit override
id), concatenate base-then-extension, and substitute ``{{VARIABLE_NAME}}``
tokens from the harness contract's ``variables:`` map.

Two build-failure conditions, both required by the design-of-record
(engram-alpha#2140 (spec comment, 2026-08-22), ruling 3):

  - a base component has ``extension_required: true`` and no matching
    extension exists for an enabled harness -> ``BuildError`` naming BOTH
    the harness and the component id;
  - after substitution, any ``{{...}}``-shaped token remains unresolved in a
    component's rendered output -> ``BuildError`` naming the component and
    the unresolved token(s).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import schema

# Matches a well-formed {{NAME}} substitution token (used for the actual
# substitution pass; public because lints.py's unresolved-variable check
# (lint b) also needs to enumerate the tokens a body references).
TOKEN_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")

# Matches ANY {{...}}-shaped token, including malformed ones (e.g. containing
# whitespace or punctuation _TOKEN_RE wouldn't capture as a NAME) -- used for
# the post-substitution "did anything unresolved survive" check, so a
# malformed token shape is caught as unresolved too, not silently ignored.
_ANY_TOKEN_RE = re.compile(r"\{\{[^{}]*\}\}")


class BuildError(Exception):
    """Raised for any build-time failure while rendering a harness's
    instruction output from its components."""


def find_extension(
    components: dict[str, tuple[dict[str, Any], str, Path]],
    base_id: str,
    harness_name: str,
    extension_spec: str,
) -> tuple[str | None, str | None]:
    """Resolve ``(extension_id, extension_body)`` for ``base_id`` under
    ``harness_name``, or ``(None, None)`` if no extension applies.

    ``extension_spec == "auto"``: find the one component whose ``extends``
    equals ``base_id`` and whose ``harness`` equals ``harness_name`` --
    "the one extension component whose extends == this base's id and
    harness == this harness's name" (PR2 spec sec. 4). More than one match
    is a ``BuildError`` (ambiguous auto-resolution), not a silent
    first-match pick.

    An explicit ``extension_spec`` (any string other than the literal
    ``"auto"``): that component's id must exist and must actually match
    (``kind=extension``, ``extends==base_id``, ``harness==harness_name``) --
    an explicit override that doesn't fit is a build failure, not a silent
    fall-through to "no extension".
    """
    if extension_spec == "auto":
        matches = [
            (cid, body)
            for cid, (fm, body, _path) in components.items()
            if fm.get("kind") == "extension"
            and fm.get("extends") == base_id
            and fm.get("harness") == harness_name
        ]
        if len(matches) > 1:
            raise BuildError(
                f"multiple extension components match base={base_id!r} "
                f"harness={harness_name!r} (auto-resolution is ambiguous): "
                f"{[m[0] for m in matches]}"
            )
        if matches:
            return matches[0]
        return None, None

    entry = components.get(extension_spec)
    if entry is None:
        raise BuildError(
            f"explicit extension id {extension_spec!r} (for base "
            f"{base_id!r}, harness {harness_name!r}) has no matching "
            f"component file"
        )
    fm, body, _path = entry
    if (
        fm.get("kind") != "extension"
        or fm.get("extends") != base_id
        or fm.get("harness") != harness_name
    ):
        raise BuildError(
            f"explicit extension id {extension_spec!r} does not extend "
            f"{base_id!r} for harness {harness_name!r} "
            f"(kind={fm.get('kind')!r}, extends={fm.get('extends')!r}, "
            f"harness={fm.get('harness')!r})"
        )
    return extension_spec, body


def _substitute(text: str, variables: dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        return variables.get(name, m.group(0))

    return TOKEN_RE.sub(repl, text)


def render_component(
    base_id: str,
    contract: dict[str, Any],
    components: dict[str, tuple[dict[str, Any], str, Path]],
    extension_spec: str = "auto",
) -> str:
    """Render one enabled component (base + matching extension, if any) for
    a harness, substituting ``{{VARIABLE}}`` tokens from the contract.

    Raises ``BuildError`` for the two build-failure conditions described in
    the module docstring, both scoped to naming THIS component.
    """
    harness_name = contract.get("name", "<unnamed>")
    entry = components.get(base_id)
    if entry is None:
        raise BuildError(
            f"harness {harness_name!r} references component id "
            f"{base_id!r} but no matching component file exists"
        )
    base_fm, base_body, _base_path = entry

    ext_id, ext_body = find_extension(components, base_id, harness_name, extension_spec)

    if ext_id is None and base_fm.get("extension_required"):
        raise BuildError(
            f"component {base_id!r} has extension_required: true but "
            f"harness {harness_name!r} has no matching extension component"
        )

    pieces = [base_body]
    if ext_body is not None:
        pieces.append(ext_body)
    combined = "\n\n".join(pieces)

    variables = contract.get("variables") or {}
    rendered = _substitute(combined, variables)

    unresolved = _ANY_TOKEN_RE.findall(rendered)
    if unresolved:
        raise BuildError(
            f"component {base_id!r} (harness {harness_name!r}) has "
            f"unresolved variable token(s) after substitution: {unresolved}"
        )

    return rendered


def render_harness(
    harness_name: str, components_dir: Path, harnesses_dir: Path
) -> tuple[str, dict[str, Any]]:
    """Render a harness's full instruction output from its enabled
    components, in the order listed under the harness contract's
    ``components:``.

    Returns ``(rendered_text, receipt)`` where ``receipt`` is
    ``{"harness": harness_name, "components": [<included base ids>]}``.
    """
    contract = schema.parse_harness_contract(harnesses_dir / f"{harness_name}.yaml")
    components = schema.load_all_components(components_dir)

    rendered_parts: list[str] = []
    included: list[str] = []

    for entry in contract.get("components", []):
        base_id = entry["id"]
        extension_spec = entry.get("extension", "auto")
        rendered_parts.append(
            render_component(base_id, contract, components, extension_spec)
        )
        included.append(base_id)

    rendered_text = "\n\n".join(rendered_parts)
    receipt: dict[str, Any] = {"harness": harness_name, "components": included}
    return rendered_text, receipt
