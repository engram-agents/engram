"""Component-file and harness-contract schema definitions + validators for
the instruction-manifestation engine (PR2, #2140).

Component files (``components/<id>.md``) and harness contracts
(``harnesses/<name>.yaml``) are described in full in the design-of-record
(engram-alpha#2140 (spec comment, 2026-08-22)) and in the PR2 spec. Field
set, in brief:

Component frontmatter::

    id: tool-policy            # kebab-case, must match the filename stem,
                                # must be unique across all components
    kind: base | extension     # required
    extends: <base-id>         # REQUIRED when kind=extension,
                                # FORBIDDEN when kind=base
    harness: claude | codex | dsh   # REQUIRED when kind=extension,
                                     # FORBIDDEN when kind=base
    composition_mode: flat     # required; "flat" is the only legal value
                                # in this schema version
    extension_required: true | false   # required, base components only
    variables: [NAME, ...]     # optional list of {{NAME}} tokens the body
                                # references

Harness contract::

    name: claude
    tool_names: [ToolName, ...]        # lint (a)'s registry
    variables: {NAME: "substitution", ...}
    components:
      - id: <component-id>
        extension: auto | <explicit-extension-id>

The frontmatter PARSING pattern (```---``-delimited YAML block followed by a
markdown body) mirrors ``src/engram/template_component_render.py``'s
``_split_frontmatter`` -- that module's PARSING approach only, not its field
set (that module's fields -- ``kind: rule``, ``harnesses: all``, ``anchor``,
``order``, ``merge``, ``version`` -- belong to #2145's entirely different,
untouched schema).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise ImportError(
        "instruction_manifestation.schema requires PyYAML "
        "(see requirements/requirements-dev.txt)"
    ) from exc

VALID_KINDS: tuple[str, ...] = ("base", "extension")
VALID_HARNESSES: tuple[str, ...] = ("claude", "codex", "dsh")
VALID_COMPOSITION_MODES: tuple[str, ...] = ("flat",)


class ComponentParseError(Exception):
    """Raised when a component ``.md`` file's frontmatter is structurally
    malformed (missing/unterminated ``---`` delimiters, or the frontmatter
    block doesn't parse to a YAML mapping) -- a PARSE failure, distinct from
    a semantic validation violation (see ``validate_components``, which
    collects both parse errors and semantic violations into one list)."""


class HarnessContractParseError(Exception):
    """Raised when a harness contract ``.yaml`` file cannot be parsed at all
    (invalid YAML, or the parsed value isn't a mapping)."""


def parse_component_file(path: Path) -> tuple[dict[str, Any], str]:
    """Parse a component ``.md`` file into ``(frontmatter, body)``.

    ``frontmatter`` is the parsed YAML mapping between the two ``---``
    delimiter lines. ``body`` is everything after the closing delimiter,
    taken verbatim as the component's instruction prose (the single trailing
    newline the file convention adds past the body's own content is
    stripped, matching ``template_component_render``'s whitespace-ownership
    convention -- a body that legitimately ends in its own blank line still
    ends in ``"\\n"`` after this strip).
    """
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ComponentParseError(
            f"{path}: missing opening '---' YAML frontmatter delimiter"
        )
    _, _, rest = raw.partition("---\n")
    fm_text, sep, body = rest.partition("\n---\n")
    if not sep:
        raise ComponentParseError(
            f"{path}: missing closing '---' YAML frontmatter delimiter"
        )
    frontmatter = yaml.safe_load(fm_text) or {}
    if not isinstance(frontmatter, dict):
        raise ComponentParseError(
            f"{path}: frontmatter block did not parse to a YAML mapping"
        )
    if body.endswith("\n"):
        body = body[:-1]
    return frontmatter, body


def load_all_components(components_dir: Path) -> dict[str, tuple[dict[str, Any], str, Path]]:
    """Load every ``*.md`` file in ``components_dir``, keyed by declared id.

    Falls back to the filename stem as the key when ``id`` is absent or not
    a non-empty string, so a directory containing a schema violation can
    still be loaded (the caller -- ``validate_components`` -- is what
    reports the mismatch; this loader stays permissive so the builder and
    lints can operate on a components directory that hasn't necessarily been
    validated first).
    """
    out: dict[str, tuple[dict[str, Any], str, Path]] = {}
    for path in sorted(components_dir.glob("*.md")):
        frontmatter, body = parse_component_file(path)
        key = frontmatter.get("id")
        if not isinstance(key, str) or not key:
            key = path.stem
        out[key] = (frontmatter, body, path)
    return out


def validate_components(components_dir: Path) -> list[str]:
    """Validate every component file in ``components_dir`` against the
    schema, returning ALL violations found (empty list = clean).

    Collects and reports every violation in one pass rather than
    fail-on-first -- the same discipline this repo applies to its other
    config validators (report everything wrong at once, not a
    fix-one-rerun loop).

    Rules enforced (PR2 spec sec. 2):
      1. ``id`` matches the filename stem exactly.
      2. ``id`` is globally unique across all component files in the
         directory.
      3. ``kind`` is exactly ``base`` or ``extension``.
      4. ``kind=base`` -> ``extends``/``harness`` ABSENT;
         ``kind=extension`` -> both PRESENT and non-empty.
      5. ``harness`` (when present) is one of ``claude``/``codex``/``dsh``.
      6. ``extends`` (when present) references a component id that exists
         and has ``kind=base``.
      7. ``composition_mode`` is exactly ``flat``.
      8. ``extension_required`` (when present) is a real boolean.
      9. ``variables`` (when present) is a list of strings.

    Body-vs-``variables:``-list mismatches (an undeclared ``{{NAME}}`` token,
    or a declared variable no harness ever defines) are deliberately NOT
    checked here -- that's lint (b)'s job (``lints.lint_unresolved_
    variables``), not a hard schema-validation failure (PR2 spec sec. 2,
    final bullet).
    """
    violations: list[str] = []
    paths = sorted(components_dir.glob("*.md"))

    records: list[dict[str, Any]] = []
    id_owners: dict[str, list[str]] = {}

    for path in paths:
        try:
            frontmatter, _body = parse_component_file(path)
        except ComponentParseError as exc:
            violations.append(str(exc))
            continue

        cid = frontmatter.get("id")
        stem = path.stem
        if cid != stem:
            violations.append(
                f"{path.name}: id {cid!r} does not match filename stem {stem!r}"
            )

        id_key = cid if isinstance(cid, str) and cid else f"<{path.name}>"
        id_owners.setdefault(id_key, []).append(path.name)

        kind = frontmatter.get("kind")
        if kind not in VALID_KINDS:
            violations.append(
                f"{path.name}: kind must be one of {VALID_KINDS}, got {kind!r}"
            )

        has_extends = "extends" in frontmatter
        has_harness = "harness" in frontmatter
        extends_val = frontmatter.get("extends")
        harness_val = frontmatter.get("harness")

        if kind == "base":
            if has_extends:
                violations.append(
                    f"{path.name}: kind=base but 'extends' is present "
                    f"(must be absent for a base component)"
                )
            if has_harness:
                violations.append(
                    f"{path.name}: kind=base but 'harness' is present "
                    f"(must be absent for a base component)"
                )
        elif kind == "extension":
            if not has_extends or not extends_val:
                violations.append(
                    f"{path.name}: kind=extension requires a non-empty "
                    f"'extends' field"
                )
            if not has_harness or not harness_val:
                violations.append(
                    f"{path.name}: kind=extension requires a non-empty "
                    f"'harness' field"
                )

        if has_harness and harness_val not in VALID_HARNESSES:
            violations.append(
                f"{path.name}: harness {harness_val!r} is not one of "
                f"{VALID_HARNESSES}"
            )

        cm = frontmatter.get("composition_mode")
        if cm not in VALID_COMPOSITION_MODES:
            violations.append(
                f"{path.name}: composition_mode must be 'flat' (the only "
                f"legal value in this schema version), got {cm!r}"
            )

        if "extension_required" in frontmatter:
            er = frontmatter["extension_required"]
            if not isinstance(er, bool):
                violations.append(
                    f"{path.name}: extension_required must be a real "
                    f"boolean, got {er!r} ({type(er).__name__})"
                )

        if "variables" in frontmatter:
            vs = frontmatter["variables"]
            if not isinstance(vs, list) or not all(isinstance(v, str) for v in vs):
                violations.append(
                    f"{path.name}: variables must be a list of strings, "
                    f"got {vs!r}"
                )

        records.append(
            {
                "path": path,
                "id": cid,
                "kind": kind,
                "extends": extends_val,
            }
        )

    for id_key, owners in id_owners.items():
        if len(owners) > 1:
            violations.append(
                f"id {id_key!r} is not unique -- claimed by: {', '.join(owners)}"
            )

    by_id: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if isinstance(rec["id"], str) and rec["id"]:
            by_id.setdefault(rec["id"], []).append(rec)

    for rec in records:
        if rec["kind"] != "extension":
            continue
        target = rec["extends"]
        if not target:
            continue  # already reported above as a missing-'extends' violation
        target_records = by_id.get(target)
        if not target_records:
            violations.append(
                f"{rec['path'].name}: extends {target!r} but no component "
                f"with that id exists in {components_dir}"
            )
        elif not any(t["kind"] == "base" for t in target_records):
            violations.append(
                f"{rec['path'].name}: extends {target!r} but that id is "
                f"not kind=base (an extension can't extend another "
                f"extension or a nonexistent base)"
            )

    return violations


def parse_harness_contract(path: Path) -> dict[str, Any]:
    """Parse a harness contract ``.yaml`` file into its raw dict form."""
    raw = path.read_text(encoding="utf-8")
    try:
        contract = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HarnessContractParseError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(contract, dict):
        raise HarnessContractParseError(f"{path}: did not parse to a mapping")
    return contract


def validate_harness_contract(
    contract: dict[str, Any], components_dir: Path
) -> list[str]:
    """Validate a parsed harness contract dict against its schema.

    Checks (PR2 spec sec. 3): every ``id`` under ``components:`` exists as a
    real component file; ``tool_names`` is a list of strings; ``variables``
    is a dict of string -> string.

    KNOWN SCOPE GAP (reviewer-fairy finding, flagged not fixed here): an
    explicit (non-``"auto"``) ``extension:`` override on a ``components[]``
    entry is not validated at THIS layer -- only ``lints.lint_extension_
    required_coverage`` (lint c, and only for ``extension_required: true``
    bases) and ``builder.find_extension`` (at actual render time) would
    catch a typo'd override id. A base with ``extension_required: false``
    and a typo'd explicit override would pass schema validation and all
    three PR2 lints clean, surfacing only when someone runs the builder.
    Intentional PR2 scope line, not an oversight -- extending validation or
    lint coverage to this case is left for a fast-follow or PR3.
    """
    violations: list[str] = []
    name = contract.get("name", "<unnamed>")

    tool_names = contract.get("tool_names")
    if not isinstance(tool_names, list) or not all(
        isinstance(t, str) for t in tool_names
    ):
        violations.append(
            f"harness {name!r}: tool_names must be a list of strings, "
            f"got {tool_names!r}"
        )

    variables = contract.get("variables")
    if not isinstance(variables, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in variables.items()
    ):
        violations.append(
            f"harness {name!r}: variables must be a dict of string -> "
            f"string, got {variables!r}"
        )

    components = contract.get("components")
    if not isinstance(components, list):
        violations.append(
            f"harness {name!r}: components must be a list, got {components!r}"
        )
        components = []

    for entry in components:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if not cid:
            violations.append(
                f"harness {name!r}: a components[] entry is missing 'id'"
            )
            continue
        path = components_dir / f"{cid}.md"
        if not path.exists():
            violations.append(
                f"harness {name!r}: components[].id {cid!r} has no "
                f"matching component file at {path}"
            )

    return violations
