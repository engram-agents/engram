from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HEALTH_EXAM_SKILL = (
    REPO_ROOT
    / "src"
    / "engram"
    / "skills"
    / "claude"
    / "engram-health-exam"
    / "SKILL.md"
)


def test_health_exam_skill_uses_supported_selftest_smoke_flag() -> None:
    content = HEALTH_EXAM_SKILL.read_text(encoding="utf-8")

    assert "python3 tools/selftest.py --smoke 2>&1 | tail -20" in content
    assert "python3 tools/selftest.py --quick" not in content
