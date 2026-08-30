"""Minimal build-time renderer for the componentized identity templates.

Reads `templates/manifests/<harness>.<surface>.json` (a flat ordered list of
component ids) and `templates/components/<id>.md` (YAML frontmatter + body),
filters components by `harnesses`, and concatenates the surviving bodies in
manifest order to produce the rendered file text -- plus a build receipt
describing what went in.

This is PR1 of the instruction-manifestation migration (engram-alpha#2140):
mechanical decomposition only. No fact-table substitution (`{{fact:...}}`),
no second-harness rendering, no wiring into `bootstrap.py` -- those are PR2
and PR3. See #2140 sec5 for the full migration path.

Design notes (from #2140 sec3's six standing rulings + PR1's own local calls):

- Flat composition: no component includes another; a manifest is just an
  ordered id list. Multiple components may share one `anchor` (heading path)
  -- that co-listing IS the base+extension composition pattern (#2140's
  "Review accepted in full" ruling 1), there is no `composition_mode` field.
- Heading ownership: a component's body carries its OWN literal markdown
  heading line verbatim, when it owns one. For components that share an
  `anchor`, only the first (lowest `order`) component at that anchor
  includes the anchor's heading text in its body; later components at the
  same anchor start directly with their content (see e.g.
  `authority-structure-intro` vs `authority-structure-table-claude-code`).
  The renderer does NOT inject headings itself -- it only concatenates
  bodies in manifest order.
- Whitespace ownership: each component's body also owns the blank-line
  separator(s) that trail it in the source document, up to (not including)
  the next component's own content. This is what lets a plain single-"\n"
  join between bodies reproduce the source exactly, including the one
  boundary where the source has ZERO blank lines between two components at
  the same anchor (`loop-mode-core` -> `loop-mode-arm-claude-code`, a
  continued numbered list item, not a new block) as well as every other
  boundary (which has exactly one blank line). A component that authors a
  NEW component file for a future harness/surface must extract through its
  own trailing blank line(s) the same way, or the join at that boundary will
  be wrong.
- Dead component (#2140 sec4): a component file that exists under
  `components/` but is referenced by no manifest anywhere is a build
  FAILURE, not a silent drop -- raised, not merely noted.
- Manifest id with no matching file (the #2131 inverse-existence lesson,
  #2140 sec4): also a build FAILURE, raised immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise ImportError(
        "template_component_render requires PyYAML "
        "(see requirements/requirements-dev.txt)"
    ) from exc

_HERE = Path(__file__).resolve().parent
COMPONENTS_DIR = _HERE / "templates" / "components"
MANIFESTS_DIR = _HERE / "templates" / "manifests"


class TemplateComponentError(Exception):
    """Raised for any build-time failure while rendering template components.

    Covers both failure classes from #2140 sec4: a manifest entry naming a
    component id with no matching file, and a component file that exists
    but is excluded from every manifest (a dead component).
    """


def _manifest_path(harness: str, surface: str) -> Path:
    return MANIFESTS_DIR / f"{harness}.{surface}.json"


def _load_manifest(harness: str, surface: str) -> dict[str, Any]:
    path = _manifest_path(harness, surface)
    if not path.exists():
        raise TemplateComponentError(
            f"no manifest found for harness={harness!r} surface={surface!r} "
            f"(expected {path})"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _split_frontmatter(raw: str, component_id: str, path: Path) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise TemplateComponentError(
            f"component {component_id!r} is missing the opening YAML "
            f"frontmatter delimiter ('---') at {path}"
        )
    _, _, rest = raw.partition("---\n")
    fm_text, sep, body = rest.partition("\n---\n")
    if not sep:
        raise TemplateComponentError(
            f"component {component_id!r} has malformed frontmatter (no "
            f"closing '---' line) at {path}"
        )
    frontmatter = yaml.safe_load(fm_text) or {}
    # The writer always appends exactly one trailing "\n" past the body's own
    # text (see the whitespace-ownership note in the module docstring) --
    # strip exactly that one terminator character, not every trailing
    # newline. A component whose content ends with its own trailing blank
    # separator line (the common case) legitimately ends in "\n" *after*
    # this strip too, and that's load-bearing for render()'s join.
    if body.endswith("\n"):
        body = body[:-1]
    return frontmatter, body


def _load_component(component_id: str) -> tuple[dict[str, Any], str]:
    path = COMPONENTS_DIR / f"{component_id}.md"
    if not path.exists():
        raise TemplateComponentError(
            f"manifest references component id {component_id!r} but no "
            f"matching file exists at {path} (manifest id with no file)"
        )
    raw = path.read_text(encoding="utf-8")
    return _split_frontmatter(raw, component_id, path)


def _harness_matches(frontmatter_harnesses: Any, harness: str) -> bool:
    if frontmatter_harnesses == "all":
        return True
    if isinstance(frontmatter_harnesses, list):
        return harness in frontmatter_harnesses
    return False


def _all_referenced_component_ids() -> set[str]:
    """Component ids referenced by ANY manifest file under manifests/.

    Dead-component detection is a property of the whole manifest set, not
    just the one manifest a given render() call happens to be rendering --
    a component only used by a not-yet-written harness manifest is still
    "referenced", just not by this render.
    """
    referenced: set[str] = set()
    if not MANIFESTS_DIR.exists():
        return referenced
    for manifest_file in sorted(MANIFESTS_DIR.glob("*.json")):
        with manifest_file.open("r", encoding="utf-8") as f:
            manifest = json.load(f)
        for entry in manifest.get("components", []):
            referenced.add(entry["id"])
    return referenced


def render(harness: str, surface: str) -> tuple[str, dict[str, Any]]:
    """Render a template surface for a harness from its components.

    Returns ``(rendered_text, build_receipt)`` where ``build_receipt`` is
    ``{"components": [{"id": ..., "version": ...}], "excluded": [...]}``.

    Raises ``TemplateComponentError`` (a build failure, not a warning) if:

    - the manifest names a component id with no matching file under
      ``components/`` (the #2131 inverse-existence lesson), or
    - a component file exists under ``components/`` but is referenced by no
      manifest at all (a dead component, per #2140 sec4). The raised error's
      message reports the excluded id(s) -- there is no successful-return
      path with a non-empty ``excluded`` list, because a dead component is a
      build FAILURE, not a fact merely noted in the receipt of a return that
      otherwise succeeds.
    """
    manifest = _load_manifest(harness, surface)

    included: list[dict[str, Any]] = []
    bodies: list[str] = []
    for entry in manifest.get("components", []):
        component_id = entry["id"]
        frontmatter, body = _load_component(component_id)
        if not _harness_matches(frontmatter.get("harnesses"), harness):
            continue
        included.append({"id": component_id, "version": frontmatter.get("version")})
        bodies.append(body)

    all_component_files = (
        {p.stem for p in COMPONENTS_DIR.glob("*.md")} if COMPONENTS_DIR.exists() else set()
    )
    referenced = _all_referenced_component_ids()
    dead = sorted(all_component_files - referenced)
    if dead:
        raise TemplateComponentError(
            "dead component(s): present under components/ but excluded from "
            f"every manifest (referenced nowhere): {dead}"
        )

    # Each component owns the blank-line separator(s) that trail it in the
    # source document (see module docstring) -- so bodies partition the
    # original document's lines contiguously, and a plain single-"\n" join
    # reconstructs it exactly, including the one boundary (loop-mode-core ->
    # loop-mode-arm-claude-code) where the source has ZERO blank lines
    # because it's a continued numbered list, not a new block.
    rendered = "\n".join(bodies) + "\n"
    receipt: dict[str, Any] = {"components": included, "excluded": []}
    return rendered, receipt
