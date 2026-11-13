"""Contract cells for #4149 (part): pre-dispatch-check is documented as a
citation check, to be run with the epic number too. Documentation only."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src/engram/tools/pre-dispatch-check.sh"
SKILL = REPO / "src/engram/skills/claude/engram-fairy-orchestration/SKILL.md"


def _header():
    lines = []
    for line in SCRIPT.read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def _bullet():
    text = SKILL.read_text()
    start = text.index("- **Pre-spec coordination check**")
    nxt = re.search(r"\n- \*\*", text[start + 5:])
    return text[start:start + 5 + nxt.start()] if nxt else text[start:]


def test_header_has_epic_rule():
    h = _header().lower()
    assert "epic" in h and "[part of #" in h


def test_header_states_bodies_not_searched():
    h = _header().lower()
    assert "bodies are not searched" in h


def test_header_states_what_it_does_not_answer():
    assert "already doing" in _header().lower()


def test_skill_bullet_has_epic_rule():
    b = _bullet().lower()
    assert "epic" in b and "[part of #" in b


def test_skill_bullet_clear_is_about_citations():
    assert "citations" in _bullet().lower()


def test_usage_error_exit_2():
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True)
    assert r.returncode == 2


def test_symlink_mode_unchanged():
    out = subprocess.check_output(
        ["git", "ls-files", "-s", "tools/pre-dispatch-check.sh"],
        cwd=str(REPO), text=True)
    assert out.startswith("120000")
