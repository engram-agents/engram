"""Skill-text guards for #4356 and #4561.

#4356: every `engram_checkpoint` call a skill shows passes `session_id`, so
the nap record `compact/last-checkpoint.json` carries the session that took
the nap (the #4012 backstop refuses a record from another session only when
the record has one).

#4561: the health-exam skill must not direct a forum post unconditionally;
it routes a ranking-path corruption finding to the user first.
"""
import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "src" / "engram" / "skills"
HEALTH_EXAM = SKILLS / "claude" / "engram-health-exam" / "SKILL.md"

_CALL = re.compile(r"engram_checkpoint\(")
# an argument `session_id=...` or a JSON key `"session_id": ...`
_PASSES_SESSION_ID = re.compile(r"""\bsession_id\s*=|["']session_id["']\s*:""")


def _calls():
    """Yield (path, line_no, argument_text) for each call-shaped use."""
    for path in sorted(SKILLS.rglob("SKILL.md")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for m in _CALL.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            if lines[line_no - 1].lstrip().startswith("description:"):
                continue  # frontmatter prose naming the tool, not a call
            if (text[m.end()] == ")" and re.search(
                    r"^#|[Cc]all(ing)? `?engram_checkpoint", lines[line_no - 1])):
                continue  # heading/sentence naming the bare tool; any call with arguments is checked
            depth, i = 1, m.end()
            while i < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            args = text[m.end():i - 1]
            # a comment mentioning session_id must not count as passing it
            args = "\n".join(l for l in args.splitlines() if not l.lstrip().startswith("#"))
            yield path, line_no, args


def test_calls_are_found():
    names = {p.parent.name for p, _, _ in _calls()}
    assert {"engram-nap", "engram-deep-research", "engram-meta-loop",
            "engram-school-day", "engram-self-improve",
            "engram-curiosity-loop"} <= names


def test_every_skill_checkpoint_call_passes_session_id():
    missing = [f"{p.relative_to(SKILLS)}:{n}" for p, n, args in _calls()
               if not _PASSES_SESSION_ID.search(args)]
    assert not missing, f"engram_checkpoint call without session_id: {missing}"


def _corruption_paragraph():
    text = HEALTH_EXAM.read_text(encoding="utf-8")
    i = text.index("how the\ncorruption happened")
    return " ".join(text[i:i + 700].split())


def test_health_exam_does_not_direct_an_unconditional_forum_post():
    text = HEALTH_EXAM.read_text(encoding="utf-8")
    assert "worth a forum post regardless" not in text
    # every sentence that mentions the forum there is under the inform-before step
    para = _corruption_paragraph().split("\n\n")[0]
    for sentence in re.split(r"(?<=[.;])\s", para):
        if "forum" in sentence.lower():
            assert "inform-before" in sentence, sentence


def test_health_exam_routes_the_corruption_finding_to_the_user_first():
    para = _corruption_paragraph()
    assert "Report it to the user first" in para
    assert "inform-before" in para
