"""Frontmatter lint gate for skills/claude/*/SKILL.md files.

For every skill directory under skills/claude/, this test asserts:
1. The SKILL.md file begins with '---\\n' and has a closing '---' delimiter.
2. The frontmatter block between the delimiters parses with yaml.safe_load
   (strict YAML — no unquoted scalars containing colons).
3. The parsed result is a dict containing non-empty string 'name' and 'description'.
4. The 'name' value equals the skill's directory name.

Parametrized per skill directory so failures name the offending skill.
Modeled on tests/test_tiers_manifest.py conventions.

This is CI gate #2 from the dual-platform conformance plan (issue #779).
"""

from __future__ import annotations

import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO_ROOT, "src", "engram", "skills", "claude")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _skill_dirs() -> list[str]:
    """Return sorted list of skill directory names under skills/claude/."""
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(
        d
        for d in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, d))
    )


def _read_skill_md(skill_name: str) -> str:
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    """Split content into (frontmatter_text, body).

    Returns (None, content) if no frontmatter block is found.
    frontmatter_text is the text between the opening and closing '---' lines
    (not including the delimiters themselves).
    """
    # Normalize CRLF so Windows-encoded SKILL.md files parse transparently;
    # without this a CRLF file would produce '---\r' != '---' mismatches.
    content = content.replace("\r\n", "\n")
    lines = content.split("\n")
    if not lines or lines[0] != "---":
        return None, content
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return None, content
    fm_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    return fm_text, body


# ---------------------------------------------------------------------------
# parametrized tests
# ---------------------------------------------------------------------------

SKILL_DIRS = _skill_dirs()


@pytest.mark.parametrize("skill_name", SKILL_DIRS)
def test_skill_has_frontmatter_delimiters(skill_name: str) -> None:
    """SKILL.md must begin with '---' and contain a closing '---' delimiter."""
    content = _read_skill_md(skill_name)
    lines = content.split("\n")

    assert lines[0] == "---", (
        f"skills/claude/{skill_name}/SKILL.md does not begin with '---\\n'. "
        f"First line is: {lines[0]!r}"
    )

    assert "---" in lines[1:], (
        f"skills/claude/{skill_name}/SKILL.md has an opening '---' but no "
        f"closing '---' delimiter — frontmatter block is unterminated."
    )


@pytest.mark.parametrize("skill_name", SKILL_DIRS)
def test_skill_frontmatter_parses(skill_name: str) -> None:
    """Frontmatter block must parse under yaml.safe_load (strict YAML)."""
    content = _read_skill_md(skill_name)
    fm_text, _ = _split_frontmatter(content)

    assert fm_text is not None, (
        f"skills/claude/{skill_name}/SKILL.md: no frontmatter block found. "
        f"(test_skill_has_frontmatter_delimiters should have failed first.)"
    )

    try:
        yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        pytest.fail(
            f"skills/claude/{skill_name}/SKILL.md: frontmatter fails yaml.safe_load: {exc}"
        )


@pytest.mark.parametrize("skill_name", SKILL_DIRS)
def test_skill_frontmatter_has_name_and_description(skill_name: str) -> None:
    """Parsed frontmatter must be a dict with non-empty 'name' and 'description'."""
    content = _read_skill_md(skill_name)
    fm_text, _ = _split_frontmatter(content)

    assert fm_text is not None, (
        f"skills/claude/{skill_name}/SKILL.md: no frontmatter block found."
    )

    parsed = yaml.safe_load(fm_text)

    assert isinstance(parsed, dict), (
        f"skills/claude/{skill_name}/SKILL.md: frontmatter parsed as "
        f"{type(parsed).__name__}, expected dict."
    )

    name = parsed.get("name")
    assert isinstance(name, str) and name.strip(), (
        f"skills/claude/{skill_name}/SKILL.md: 'name' field is missing or empty. "
        f"Got: {name!r}"
    )

    description = parsed.get("description")
    assert isinstance(description, str) and description.strip(), (
        f"skills/claude/{skill_name}/SKILL.md: 'description' field is missing or empty. "
        f"Got: {description!r}"
    )


@pytest.mark.parametrize("skill_name", SKILL_DIRS)
def test_skill_name_matches_directory(skill_name: str) -> None:
    """The 'name' field in frontmatter must equal the skill's directory name."""
    content = _read_skill_md(skill_name)
    fm_text, _ = _split_frontmatter(content)

    assert fm_text is not None, (
        f"skills/claude/{skill_name}/SKILL.md: no frontmatter block found."
    )

    parsed = yaml.safe_load(fm_text)
    assert isinstance(parsed, dict), (
        f"skills/claude/{skill_name}/SKILL.md: frontmatter is not a dict."
    )

    name = parsed.get("name", "")
    assert name == skill_name, (
        f"skills/claude/{skill_name}/SKILL.md: 'name' field {name!r} does not "
        f"match directory name {skill_name!r}."
    )


# ---------------------------------------------------------------------------
# Non-vacuousness guard
# ---------------------------------------------------------------------------


def test_skill_dirs_enumeration_nonempty() -> None:
    """The lint gate must actually find skills — guard against path drift making it vacuous."""
    assert len(SKILL_DIRS) >= 20, f"skills/claude/ enumeration found only {len(SKILL_DIRS)} dirs — gate is vacuous"


# ---------------------------------------------------------------------------
# CRLF transparency unit test
# ---------------------------------------------------------------------------


def test_split_frontmatter_crlf_transparent() -> None:
    """_split_frontmatter must parse CRLF-encoded frontmatter identically to LF."""
    lf_content = "---\nname: test-skill\ndescription: A test.\n---\nBody text.\n"
    crlf_content = lf_content.replace("\n", "\r\n")

    fm_lf, body_lf = _split_frontmatter(lf_content)
    fm_crlf, body_crlf = _split_frontmatter(crlf_content)

    assert fm_lf == fm_crlf, (
        f"CRLF frontmatter text differs from LF: {fm_crlf!r} != {fm_lf!r}"
    )
    assert body_lf == body_crlf, (
        f"CRLF body differs from LF: {body_crlf!r} != {body_lf!r}"
    )
    # Verify the CRLF result actually parses
    assert fm_crlf is not None
    parsed = yaml.safe_load(fm_crlf)
    assert parsed == {"name": "test-skill", "description": "A test."}
