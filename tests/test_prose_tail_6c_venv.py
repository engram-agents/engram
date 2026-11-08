"""#3863 PR 6c: the venv is ALWAYS ~/.engram/venv, never derived from ENGRAM_HOME.

Borges' ruling on #3863 (crossing 12498): eleven lines already on dev put the venv
under ENGRAM_HOME. They go back to the HOME form. The other ENGRAM_HOME uses on the same
lines and blocks (cursor files, the home itself) stay canonical.

  C1  each of the 11 lines holds the HOME form `$HOME/.engram/venv` and no ENGRAM_HOME in
      the venv path (asserted per file by the exact text of the line);
  C2  a repo-wide guard: no tracked non-test file under src/engram or src/build derives a
      venv path from ENGRAM_HOME (`ENGRAM_HOME[^\\n]{0,20}/venv`,
      `engram_home() / "venv"`), except the explicit allowlist below, whose counts are
      pinned so the list can only shrink;
  C3  planted violators the guard must flag (and a planted HOME form it must not).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = "src/engram/"

# file -> exact (stripped) text of the lines the ruling fixed
FIXED_LINES = {
    "skills/claude/engram-loop/SKILL.md": [
        'status=$("$HOME/.engram/venv/bin/python" "$WAKE" status)',
        'PY="$HOME/.engram/venv/bin/python"',
    ],
    "skills/claude/engram-sleep/SKILL.md": [
        '"$HOME/.engram/venv/bin/python3" "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/erase_session_block.py"',
        'session_backup_out=$("$HOME/.engram/venv/bin/python3" \\',
        'claude_backup_out=$("$HOME/.engram/venv/bin/python3" \\',
        '"$HOME/.engram/venv/bin/python3" -m tools.dream_lottery draw \\',
        'embed_backfill_json=$("$HOME/.engram/venv/bin/python3" "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/engram-regenerate-embeddings.py"',
        'backup_out=$("$HOME/.engram/venv/bin/python3" "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/backup_knowledge_db.py" \\',
    ],
    "tools/fairy_report.py": [
        '"$HOME/.engram/venv/bin/python3" tools/fairy_report.py --task <task-id> [--session <id>]',
        '"$HOME/.engram/venv/bin/python3" tools/fairy_report.py --grep <pattern> [--session <id>]',
    ],
    "tools/engram-fix-git-backup.sh": ['if [[ -x "$HOME/.engram/venv/bin/python" ]]; then', 'PYTHON_BIN="$HOME/.engram/venv/bin/python"'],
    "tools/install-local-marketplace.sh": ['_ENGRAM_VENV_PY="$HOME/.engram/venv/bin/python"', 'log "Provisioning venv at $HOME/.engram/venv (tier: $_eff_tier)"'],
    "tools/uninstall-local-marketplace.sh": ['log "   $HOME/.engram/venv/         — Python environment (removed only with --purge-venv; see Files and links)"'],
    "tools/ghpr.sh": ['PYTHON="${GHPR_PYTHON:-$HOME/.engram/venv/bin/python}"'],
    "tools/run-all-tests.sh": [
        "#      ($HOME/.engram/venv) which has sqlite-vec, anthropic, etc.",
        'ENGRAM_PY="${ENGRAM_PY:-$HOME/.engram/venv/bin/python}"',
    ],
}

_VENV_FROM_HOME = [
    re.compile(r"ENGRAM_HOME[^\n]{0,20}/venv"),
    re.compile(r"engram_home\(\)\s*/\s*['\"]venv"),
]

# Files that still derive a venv path from ENGRAM_HOME: file -> (matching lines, reason).
# Counts are pinned. Borges ruled (crossing 12515) that the venv rule covers every such line. The
# one entry this list carried (launch-engram-server.sh, "fixed on the 6b branch") is gone: 6b has
# merged and that launcher has no such line on dev, so the list is empty. "needs a ruling" must stay empty.
ALLOWLIST = {}


def venv_from_home_lines(text):
    """[(lineno, line)] of lines that derive a venv path from ENGRAM_HOME."""
    return [
        (n, line)
        for n, line in enumerate(text.split("\n"), 1)
        if any(rx.search(line) for rx in _VENV_FROM_HOME)
    ]


def _tracked_non_test_files():
    out = subprocess.run(
        ["git", "ls-files", "src/engram", "src/build"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    for rel in out:
        if not rel:
            continue
        parts = rel.split("/")
        if "tests" in parts or "docs" in parts or parts[-1].startswith("test_"):
            continue
        yield rel


def _text(rel):
    try:
        return (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return None


@pytest.mark.parametrize("rel", sorted(FIXED_LINES))
def test_c1_the_fixed_lines_use_the_home_form(rel):
    text = _text(SRC + rel)
    stripped = [ln.strip() for ln in text.split("\n")]
    for want in FIXED_LINES[rel]:
        hits = [ln for ln in stripped if want in ln]
        assert hits, f"{rel}: line not found: {want}"
        assert all("$HOME/.engram/venv" in ln for ln in hits), hits
        assert not any(rx.search(ln) for ln in hits for rx in _VENV_FROM_HOME), hits
    assert not [n for n, ln in venv_from_home_lines(text) if any(w in ln for w in FIXED_LINES[rel])]


def test_c2_no_venv_path_is_derived_from_engram_home_outside_the_allowlist():
    found = {}
    for rel in _tracked_non_test_files():
        text = _text(rel)
        if text is None:
            continue
        hits = venv_from_home_lines(text)
        if hits:
            found[rel[len(SRC):] if rel.startswith(SRC) else rel] = len(hits)
    expected = {f: n for f, (n, _why) in ALLOWLIST.items()}
    assert found == expected, (
        "a venv path derived from ENGRAM_HOME (the venv is always ~/.engram/venv, Borges' ruling "
        f"on #3863): got {found}, allowlist {expected}"
    )


def test_c3_planted_violators_are_flagged_and_the_home_form_is_not():
    assert venv_from_home_lines('PY="${ENGRAM_HOME:-$HOME/.engram}/venv/bin/python"')
    assert venv_from_home_lines('"${ENGRAM_HOME:-$HOME/.engram}"/venv/bin/python3 -m x')
    assert venv_from_home_lines('VENV_PY="$ENGRAM_HOME/venv/bin/python"')
    assert venv_from_home_lines('p = engram_home() / "venv" / "bin"')
    assert venv_from_home_lines("p = engram_home()/'venv'")
    assert venv_from_home_lines('PY="$HOME/.engram/venv/bin/python"') == []
    assert venv_from_home_lines('cat "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json') == []


def test_c4_no_allowlist_entry_waits_for_a_ruling():
    assert not [f for f, (_n, why) in ALLOWLIST.items() if "ruling" in why], ALLOWLIST


@pytest.mark.parametrize("rel", ["templates/components/infra-locations-claude-code.md", "templates/template.CLAUDE.md"])
def test_c5_the_templates_state_the_rule(rel):
    line = [ln for ln in _text(SRC + rel).split("\n") if ln.startswith("| Plugin venv")]
    assert len(line) == 1 and "~/.engram/venv/" in line[0] and "independent of `ENGRAM_HOME`" in line[0]
    assert "{{ENGRAM_HOME}}/venv" not in line[0]
