"""Slice 1 of #4842: the secrets pointer row in the locations table.

Contract: the rendered claude-code CLAUDE.md carries exactly one table row
starting with `Secrets`, naming the secrets directory and the 0700 / 0600 /
value-blind discipline; that row sits inside the infrastructure-locations
table (contiguous with its `| Component | Path |` header); and the shipped
template.CLAUDE.md carries the same row.
"""

from pathlib import Path

import template_component_render as tcr

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "src" / "engram" / "templates" / "template.CLAUDE.md"

HEADER = "| Component | Path |"


def _rendered_lines():
    rendered, _receipt = tcr.render("claude-code", "CLAUDE.md")
    return rendered.splitlines()


def _secrets_rows(lines):
    return [
        (i, ln)
        for i, ln in enumerate(lines)
        if ln.startswith("|") and ln[1:].lstrip().startswith("Secrets")
    ]


def test_exactly_one_secrets_row_with_required_terms():
    rows = _secrets_rows(_rendered_lines())
    assert len(rows) == 1, rows
    row = rows[0][1]
    for term in (
        "{{ENGRAM_HOME}}/secrets/",
        "0700",
        "keep the directory 0700",
        "0600",
        "value-blind",
        "git-ignored",
        "one file per secret",
        "{{ENGRAM_HOME}}/forum-token",
        "one known exception",
    ):
        assert term in row, term
    # Not the only credential outside secrets/ (install-backups/ can hold
    # tokens too): the row must not claim a sole exception.
    assert "the one exception" not in row


def test_secrets_row_is_inside_the_locations_table():
    lines = _rendered_lines()
    rows = _secrets_rows(lines)
    assert len(rows) == 1, rows
    idx = rows[0][0]
    header_idx = lines.index(HEADER)
    end = header_idx
    while end < len(lines) and lines[end].strip() != "":
        end += 1
    assert header_idx < idx < end, (header_idx, idx, end)


def test_template_file_carries_the_same_row():
    rows = _secrets_rows(_rendered_lines())
    assert len(rows) == 1, rows
    template_lines = TEMPLATE_PATH.read_text(encoding="utf-8").splitlines()
    assert rows[0][1] in template_lines
