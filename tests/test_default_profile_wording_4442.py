"""Conservative default authority for new installs (engram-alpha#4442, wording half).

The identity file a NEW install renders must (1) keep outward actions at
inform-before and (2) not assert the user's consent to sub-agent dispatch.
The first-session skill asks the user and records grants in their own words.
Contract cells C1-C8 of the cohort spec; C7 checks the INSTALLED artifact
(rendered through bootstrap's own substitution function), not only the template.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import template_component_render as tcr

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / "src" / "engram" / "skills" / "claude" / "engram-first-session" / "SKILL.md"
TEMPLATE = REPO_ROOT / "src" / "engram" / "templates" / "template.CLAUDE.md"

GRANT_FORMAT = "Granted by {{USER_NAME}} on <date>: <their words>"
C4_BANNED = (
    "IS the request",
    "standing-authorised",
    "standing-authorized",
    "this paragraph is that request",
    "act freely",
)


def _render() -> str:
    return tcr.render("claude-code", "CLAUDE.md")[0]


def _line(text: str, prefix: str) -> str:
    hits = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(hits) == 1, f"expected exactly one line starting {prefix!r}, got {len(hits)}"
    return hits[0]


def _check_c1(text: str) -> None:
    row = _line(text, "| **Internal — act**")
    for tok in ("GitHub", "PR", "email", "letter", "forum", "our_side", "shared space"):
        assert tok not in row, f"Internal — act row still contains {tok!r}"


def _check_c3(text: str) -> None:
    assert text.count("without consulting") == 1
    assert "without consulting" in _line(text, "| **Internal — act**")


def _check_c4(text: str) -> None:
    low = text.lower()
    for tok in C4_BANNED:
        assert tok.lower() not in low, f"pre-authorization phrase present: {tok!r}"


def test_c1_internal_act_row_has_no_outward_actions():
    _check_c1(_render())


def test_c2_inform_before_row_lists_outward_actions():
    row = _line(_render(), "| **Inform-before")
    for tok in (
        "filing GitHub issues",
        "opening PRs",
        "commenting on GitHub",
        "email, letters and forum posts to other agents",
    ):
        assert tok in row, f"Inform-before row missing {tok!r}"


def test_c3_without_consulting_only_on_internal_row():
    _check_c3(_render())


def test_c4_no_preauthorization_phrases():
    _check_c4(_render())


def test_c5_tool_policy_does_not_claim_consent():
    r = _render()
    assert "nothing in this file is that request" in r
    assert "OFF until {{USER_NAME}} says yes" in r


def test_c6_grant_format_shipped_never_a_grant():
    r = _render()
    assert r.count(GRANT_FORMAT) >= 2
    assert not [ln for ln in r.splitlines() if ln.startswith("Granted by")]


def test_c7_installed_artifact_via_bootstrap_substitution(tmp_path):
    # bootstrap.py reads required env at import and imports server.py, so call its
    # own substitute_template in a subprocess against a scratch home (never ~/.engram).
    src = REPO_ROOT / "src" / "engram"
    dst = tmp_path / "home" / ".claude" / "CLAUDE.md"
    env = {
        **os.environ,
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(src / "templates"),
        "ENGRAM_HOME": str(tmp_path / "engram"),
        "CLAUDE_HOME": str(tmp_path / "claude"),
        "TARGET": "claude",
    }
    code = (
        "import sys, pathlib; sys.path.insert(0, sys.argv[1]);"
        "import bootstrap;"
        "bootstrap.substitute_template(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]),"
        " {'{{USER_NAME}}': 'Ada'})"
    )
    # bootstrap leaves {{USER_NAME}} in place and first-session Step 6 resolves it,
    # so this test substitutes it itself (same placeholder -> value map shape).
    r = subprocess.run(
        [sys.executable, "-c", code, str(src), str(TEMPLATE), str(dst)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    text = dst.read_text()
    _check_c1(text)
    _check_c3(text)
    _check_c4(text)
    assert "Granted by Ada on <date>" in text
    assert not [ln for ln in text.splitlines() if ln.startswith("Granted by")]
    assert "{{USER_NAME}}" not in text


def test_c8_first_session_skill_has_grants_substep_inside_step6():
    text = SKILL.read_text()
    heads = list(re.finditer(r"^#{2,3} .*$", text, re.M))
    mine = [h for h in heads if h.group(0).startswith("### Step 6 — Before finalizing:")]
    assert len(mine) == 1, "grants sub-step heading must exist exactly once"
    step6 = re.search(r"^## Step 6 — Finalize", text, re.M)
    claude = re.search(r"^### Step 6 — Claude target", text, re.M)
    assert step6 and claude
    assert step6.start() < mine[0].start() < claude.start()
    nxt = [h for h in heads if h.start() > mine[0].start()][0]
    body = text[mine[0].start():nxt.start()]
    for phrase in ("in their own words", "write nothing", "Ask now, write after"):
        assert phrase in body, f"sub-step text missing {phrase!r}"
    assert (
        'On "no" or on no answer: write nothing; the default stands.'
        in body
    )


SKILLS_DIR = REPO_ROOT / "src" / "engram" / "skills" / "claude"
OUTPUT_STYLES_DIR = REPO_ROOT / "src" / "engram" / "output-styles" / "claude"
IED = SKILLS_DIR / "internal-external-decision" / "SKILL.md"
SLEEP = SKILLS_DIR / "engram-sleep" / "SKILL.md"


def test_c4_extension_no_preauthorization_phrases_in_shipped_skills_and_styles():
    files = sorted(SKILLS_DIR.glob("**/SKILL.md")) + sorted(OUTPUT_STYLES_DIR.glob("**/*.md"))
    assert files, "no skill/output-style files found"
    assert OUTPUT_STYLES_DIR.is_dir()
    hits = []
    for f in files:
        low = f.read_text().lower()
        hits += [(f.name, t) for t in C4_BANNED if t.lower() in low]
    assert not hits, hits


def test_c10_decision_skill_table_wins_and_no_outward_row_is_plain_internal():
    text = IED.read_text()
    assert "Your own CLAUDE.md table wins over every row below." in text
    pat = re.compile(r"github|\bPR\b|letter|email|forum|fairy", re.I)
    checked = 0
    for ln in text.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Case",) or set(cells[0]) <= set("-"):
            continue
        if pat.search(cells[0]):
            checked += 1
            assert cells[1] != "Internal", f"outward row still plain Internal: {cells[0]}"
    assert checked >= 10


def test_c11_sleep_skill_has_grant_precondition_before_mandatory():
    text = SLEEP.read_text()
    assert "the sub-agent grant" in text and "do NOT dispatch" in text
    assert text.index("do NOT dispatch") < text.index("MANDATORY")
    assert text.index("the sub-agent grant") < text.index("MANDATORY")
    i = text.index("One precondition comes before everything below")
    j = text.index("ALL-OR-NOTHING", i)
    assert "run Phase A, stop before Phase B" in text[i:j]


def test_c12_regression_test_sentence_says_send_anything_off_this_machine():
    assert "does it send anything off this machine, affect a human other than" in _render()


def test_c13_decision_skill_accepts_table_or_dated_grant():
    assert (
        "its authority table lists that action under Internal, a dated grant line covers it"
        in IED.read_text()
    )


def test_c14_sleep_precondition_accepts_operator_standing_request():
    text = SLEEP.read_text()
    i = text.index("One precondition comes before everything below")
    j = text.index("ALL-OR-NOTHING", i)
    assert "or a standing request already written there" in text[i:j]


def test_c15_auto_judgement_skills_carry_the_grant_precondition():
    for name in ("engram-auto-coder-fairy-judgement", "engram-auto-reviewer-fairy-judgement"):
        assert "Without it this rubric does not apply" in (SKILLS_DIR / name / "SKILL.md").read_text()


def test_c16_deference_detector_prompt_keeps_inform_before():
    hook = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-prompt.py"
    assert "an inform-before action still gets its one line to the user first" in " ".join(
        hook.read_text().replace('"', " ").split()
    )


def test_c17_sleep_precondition_still_runs_step_9_75():
    text = SLEEP.read_text()
    i = text.index("One precondition comes before everything below")
    j = text.index("ALL-OR-NOTHING", i)
    assert "still run Step 9.75" in text[i:j]


def test_c18_decision_skill_class_cell_governs():
    assert "its Class cell governs" in IED.read_text()


def test_c19_grant_line_is_a_record_no_program_parses():
    assert "no program parses it" in _render()


def test_c20_banned_lead_is_grant_or_no_grant():
    r = _render()
    assert "BANNED — grant or no grant" in r
    assert "BANNED — whatever has been granted" not in r


FIRST_SESSION = SKILLS_DIR / "engram-first-session" / "SKILL.md"
UPGRADE = SKILLS_DIR / "engram-upgrade" / "SKILL.md"


def test_c21_reading_is_not_sending():
    r = _render()
    assert "Reading is not sending" in r
    assert "Anything I send off this machine" in _line(r, "| **Inform-before")


def test_c22_default_is_not_worded_as_leaving_this_machine():
    for text in (_render(), IED.read_text(), FIRST_SESSION.read_text()):
        low = text.lower()
        assert "leaves this machine" not in low and "leave this machine" not in low


def test_c23_upgrade_skill_says_authority_narrowing_is_the_users_choice():
    assert (
        "A template change that narrows authority is the user's choice, never a silent merge"
        in UPGRADE.read_text()
    )


# --- Round 2 (wording only): the new default profile says what it means. C24-C30 ---

ORCH = SKILLS_DIR / "engram-fairy-orchestration" / "SKILL.md"

W1_REVIEWER = "reviewer-fairies (review a PR and post the review on it)"
W1_SENTENCE = (
    "Once dispatch is granted, I delegate work that doesn't need my own context; without the grant, "
    "everything below in this section describes work I do myself. A fairy acts for me: what it sends "
    "off this machine (a reviewer-fairy's posted review is a GitHub comment) falls under the authority "
    "table exactly as if I had sent it, so I tell {{USER_NAME}} first unless that channel is granted."
)
# C24 pins the clause up to "in hand" and the sentence after it; the literal between them
# (under `ask each time`) is what brings this file into C35's walk, so C35 alone owns it.
W2_LINE = "These two apply only where {{USER_NAME}} has said I may dispatch sub-agents, as a standing grant or as a yes for the task in hand"
W2_AFTER = "Without that, there is nothing to delegate: I do the work myself."
W3_CLARIFIER = (
    "That decides which skill to load, not what I may do unasked: a forum post is a message to other "
    "agents, so I tell {{USER_NAME}} first unless they have said that channel needs no telling."
)
W4_SENTENCE = (
    "If their answer is \"ask each time\", I record that too, as `{{USER_NAME}} on <date>: ask each time`. "
    "Then, before any task that needs sub-agents (tonight's consolidation, a code review), I ask, saying "
    "about how many I expect to start; their yes is the grant for that task only, and without it I do the "
    "task the no-grant way."
)
W5_ASK_EACH_TIME = (
    "On \"ask each time\": write one dated line in the tool-policy section, `<user> on <date>: ask each time`, "
    "so a later session asks before each task that needs sub-agents instead of skipping."
)
W5_NO_ANSWER = "On \"no\" or on no answer: write nothing; the default stands."
W7_SENTENCE = (
    "A reviewer-fairy posts its review on the PR, and that post is your outward action: where GitHub "
    "comments are not granted, tell the user first that the review will be posted, as you would for a "
    "comment of your own."
)


def _parallelization_section(text: str) -> str:
    i = text.index("## Parallelization and Delegation")
    j = text.find("\n## ", i + 5)
    return text[i : j if j != -1 else len(text)]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_c24_no_unconditional_delegation_in_parallelization_section():
    section = _parallelization_section(_render())
    assert "Regression tests" in section
    for phrase in ("Delegate the 4th onward", "I delegate work"):
        for ln in section.splitlines():
            if phrase in ln:
                assert "grant" in ln.lower(), f"{phrase!r} outside a granted-conditional line: {ln!r}"
    assert W2_LINE in section
    assert W2_AFTER in section
    assert "**Pattern-following (once granted):**" in section
    assert "**Spec-while-fairy-runs (once granted):**" in section
    assert section.index(W2_LINE) < section.index("**Pattern-following (once granted):**")


def test_c25_fairies_paragraph_carries_the_grant_condition_and_the_authority_pointer():
    r = _render()
    assert W1_REVIEWER in r
    assert "reviewer-fairies (read-only PR review)" not in r
    assert W1_SENTENCE in r


def test_c26_forum_pointer_says_load_the_skill_is_not_permission():
    r = _render()
    assert W3_CLARIFIER in r
    assert r.index("no trust-tier skill needed.") < r.index(W3_CLARIFIER)


def test_c27_ask_each_time_is_recorded_and_asked_before_every_dispatch():
    assert W4_SENTENCE in _render()


def test_c28_first_session_writes_the_ask_each_time_line_and_no_answer_writes_nothing():
    text = FIRST_SESSION.read_text()
    assert W5_ASK_EACH_TIME in text
    assert W5_NO_ANSWER in text
    assert 'On "no", on "ask each time", or on no answer' not in text
    assert text.index("Never pre-fill an answer") > text.index(W5_ASK_EACH_TIME)


def test_c29_first_session_writes_the_grants_before_deleting_the_marker():
    text = FIRST_SESSION.read_text()
    i = text.index("### Step 6 — Claude target (TARGET=claude)")
    j = text.index("\n---", i)
    block = text[i:j]
    items = re.findall(r"^(\d+)\. (.*)$", block, flags=re.M)
    nums = [int(n) for n, _ in items]
    assert nums == list(range(1, len(items) + 1)), "list numbering must be consecutive"
    grants = [n for n, body in items if body.startswith("**Write the grants.**")]
    marker = [n for n, body in items if body.startswith("Delete the first-run marker")]
    verify = [n for n, body in items if body.startswith("**Verify**")]
    assert len(grants) == len(marker) == len(verify) == 1
    assert int(verify[0]) < int(grants[0]) < int(marker[0])
    assert "This comes before the marker is deleted" in block


def test_c30_orchestration_skill_says_a_posted_review_is_an_outward_action():
    assert W7_SENTENCE in _flat(ORCH.read_text())


def test_c31_codex_path_cites_the_claude_item_that_calls_advance_turn():
    text = FIRST_SESSION.read_text()
    i = text.index("### Step 6 — Claude target (TARGET=claude)")
    block = text[i : text.index("\n---", i)]
    items = re.findall(r"^(\d+)\. (.*)$", block, flags=re.M)
    advance = [n for n, body in items if "engram_advance_turn(" in body]
    assert len(advance) == 1
    cited = re.findall(r"as in the claude path \(step 6\.(\d+) above\)", text)
    assert len(cited) == 1
    assert cited == advance


# --- Amendment (three states, not two): C32-C35 ---

SLEEP_SKILL = SKILLS_DIR / "engram-sleep" / "SKILL.md"
COMPONENTS_DIR = REPO_ROOT / "src" / "engram" / "templates" / "components"
W8_SENTENCE = (
    "look for an `ask each time` line there: if it is present, ask the user now, before Phase A, saying "
    "that tonight's consolidation starts a few helper sub-agents (the exact set is drawn later tonight); a yes is the grant for tonight only, "
    "and anything else, including no answer on an away night, is the no-grant path. With neither a grant "
    "nor that yes, do NOT dispatch:"
)
W9_SENTENCE = (
    "Where that section carries an `ask each time` line, ask the user before this dispatch; a yes covers "
    "this task only."
)
W10_SENTENCE = "An `ask each time` line there means ask first; a yes covers that one task."


def test_c32_sleep_precondition_asks_on_an_ask_each_time_line():
    text = SLEEP_SKILL.read_text()
    i = text.index("One precondition comes before everything below")
    j = text.index("ALL-OR-NOTHING", i)
    para = _flat(text[i:j])
    assert "which installs made before this release carry), " + W8_SENTENCE in para
    # every pre-existing clause after the insertion is kept
    assert "run Phase A, stop before Phase B" in para and "still run Step 9.75" in para


def test_c33_both_auto_judgement_skills_ask_on_an_ask_each_time_line():
    for name in ("engram-auto-coder-fairy-judgement", "engram-auto-reviewer-fairy-judgement"):
        text = (SKILLS_DIR / name / "SKILL.md").read_text()
        line = _line(text, "**Precondition**")
        assert line.endswith(W9_SENTENCE), name
        assert "do the work directly and say so." in line


def test_c34_decision_skill_asks_first_on_an_ask_each_time_line():
    text = IED.read_text()
    assert "its Tool policy section carries a standing request. " + W10_SENTENCE in text


GRANT_MARKERS = ("Granted by", "standing request", "a standing grant", "once granted", "has said I may")
SUBAGENT_WORDS = ("sub-agent", "dispatch")


def _grant_gating_population() -> dict:
    """The population the CLASS cell walks. A file is in it if it is a shipped skill
    (`src/engram/skills/**/SKILL.md`) or template component (`src/engram/templates/components/*.md`)
    with a paragraph containing one of GRANT_MARKERS and one of SUBAGENT_WORDS. Paragraphs split on
    blank lines and numbered-list starts. A file that gates in other words is outside the walk.

    Excluded surfaces: `template.CLAUDE.md`, a byte-identical render of the components (pinned by
    tests/test_template_components.py); `deploy/autostart/boot-charge-*.md`, operator files that may
    carry an operator's standing grant, asserted below to carry no `Granted by` marker today."""
    files = sorted(SKILLS_DIR.glob("**/SKILL.md")) + sorted(COMPONENTS_DIR.glob("*.md"))
    out = {}
    for f in files:
        paras = re.split(r"\n\s*\n|\n(?=\d+\. )", f.read_text())
        if any(any(m in p for m in GRANT_MARKERS) and any(w in p for w in SUBAGENT_WORDS) for p in paras):
            out[f] = f.read_text()
    return out


def test_c35_every_file_that_gates_on_the_subagent_grant_names_ask_each_time():
    pop = _grant_gating_population()
    names = {f.parent.name if f.name == "SKILL.md" else f.stem for f in pop}
    # the walk must not be vacuous: the known gating files are in it
    assert {
        "engram-sleep", "engram-first-session", "internal-external-decision",
        "engram-auto-coder-fairy-judgement", "engram-auto-reviewer-fairy-judgement",
        "fairy-tool-policy-claude-code", "engram-upgrade", "fairy-regression-tests",
    } <= names, names
    missing = sorted(str(f.relative_to(REPO_ROOT)) for f, t in pop.items() if "ask each time" not in t)
    assert not missing, f"gate on the sub-agent grant without naming `ask each time`: {missing}"
    # an operator's boot charge gaining grant language must be seen here, not missed by the walk
    charged = [p.name for p in sorted((REPO_ROOT / "deploy" / "autostart").glob("boot-charge-*.md"))
               if "Granted by" in p.read_text()]
    assert not charged, f"boot charge now carries a `Granted by` marker, decide its place in this walk: {charged}"
