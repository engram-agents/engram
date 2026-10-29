"""#4255 slice 7, S7.6 (Ariadne, comment 6505099088 and crossing 19217): engram-collaborating-loop stops keying
its channel text on the host.

Letters (``ia``), batons and the forum are all clients of one forum server, so a counterpart on another host is
reached the same way as one on this host. The skill still said otherwise in three places (the prerequisite block,
Step 3's ``ia read <filename>`` and the forum-dimension paragraph).  The dated validation notes ("Validated ...
2026-05-30") are history and stay as written.

Contract (written before the rewording):
  1. ``ia read <filename>`` (the old file protocol) is gone; Step 3 says ``ia read <counterpart>``.
  2. "the only cross-host channel" and "watches the *same-host*" are gone.
  3. the prerequisite block no longer says letters/batons cannot reach a cross-host agent, and it names the
     forum server (``forum.url``) as what decides whether a counterpart is reachable.
  4. every remaining "same-host" in the file sits on a dated line (a validation note).
"""
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "src" / "engram" / "skills" / "claude" / "engram-collaborating-loop" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")


def _prerequisite_block():
    start = TEXT.index("**Prerequisite")
    return TEXT[start:TEXT.index("This skill is a **specialization", start)]


@pytest.mark.parametrize("gone", [
    "ia read <filename>",
    "only cross-host channel",
    "watches the *same-host*",
    "so letters/batons can't reach you",
    "Shared filesystem** (a counterpart on the same host",
])
def test_the_host_keyed_or_false_wording_is_gone(gone):
    assert gone not in TEXT, gone


def test_step_3_reads_a_dm_by_counterpart():
    step3 = TEXT[TEXT.index("## Step 3"):TEXT.index("## Step 4")]
    assert "`ia read <counterpart>`" in step3


def test_the_prerequisite_block_decides_on_reachability_through_the_forum():
    block = _prerequisite_block()
    assert "**A counterpart reachable through the forum server** (`forum.url` set in `config.json`)" in block
    assert "on your host or another one" in block


def test_the_forum_paragraph_says_letters_and_batons_already_travel_through_the_forum_server():
    para = TEXT[TEXT.index("## The forum dimension"):TEXT.index("### Mechanism 3a")]
    assert "already travel through the forum server" in para


def test_every_remaining_same_host_is_a_dated_validation_note():
    bad = [ln for ln in TEXT.splitlines() if "same-host" in ln.lower() and "2026-" not in ln]
    assert bad == [], bad


# ============================================================================
# S7.7 (Ariadne, crossing 19234): the skill no longer tells an agent to arm the deleted letter Monitor.
#   tools/collab-letter-monitor.sh was deleted in #2857 step 2.  The wake that exists today is the forum updates
#   feed (DMs and baton flips) and the forum mention feed, armed as ONE supervised Monitor.
# ============================================================================
import re
import subprocess

REPO = Path(__file__).resolve().parent.parent


def _tracked_tools_files():
    out = subprocess.run(["git", "ls-files", "--", "tools", "src/engram/tools"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    names = set()
    for p in out.stdout.split():
        names.add(p.rsplit("/", 1)[-1])
    return names


def test_s77_the_skill_names_no_tools_file_that_does_not_exist_unless_the_line_says_it_is_deleted():
    have = _tracked_tools_files()
    missing = []
    for ln in TEXT.splitlines():
        for m in re.finditer(r"tools/([A-Za-z0-9_.\-]+\.(?:sh|py))", ln):
            if m.group(1) not in have and not re.search(r"deleted|no longer exists|retired", ln, re.I):
                missing.append(f"{m.group(1)}: {ln.strip()[:90]}")
    assert missing == [], missing


@pytest.mark.parametrize("gone", [
    "bash <repo>/tools/collab-letter-monitor.sh",
    "inotifywait",
    "The to-me filter is the load-bearing line",
    "Why filename-poll, not mtime?",
    "projects/*.md",
    "polls the shared inter-agent directory",
    "lands in `/home/agents-shared/inter-agent/`",
])
def test_s77_the_retired_letter_monitor_workflow_is_gone(gone):
    assert gone not in TEXT, gone


def test_s77_inter_agent_directory_appears_only_in_retirement_lines():
    bad = [ln.strip()[:100] for ln in TEXT.splitlines()
           if "inter-agent/" in ln and not re.search(r"retired|deleted|no longer|no `/home/agents-shared/inter-agent/` watch|history|historical", ln, re.I)]
    assert bad == [], bad


def test_s77_mechanism_1_names_the_wake_that_exists_today():
    m1 = TEXT[TEXT.index("### Mechanism 1"):TEXT.index("### Mechanism 2")]
    assert "forum-updates-monitor.sh" in m1 and "monitor-supervisor.py" in m1
    assert "Step 2" in m1


def test_s77_the_dated_retirement_note_stays_as_history():
    assert "was deleted in\n> #2857 step 2" in TEXT or "was deleted in #2857 step 2" in TEXT


def test_s77_the_forum_section_heading_is_keyed_on_who_is_addressed_not_on_a_host():
    heads = [ln for ln in TEXT.splitlines() if ln.startswith("## The forum dimension")]
    assert len(heads) == 1 and "cross-host" not in heads[0] and "@-mention" in heads[0], heads


# ============================================================================
# Reviewer round 1 on PR 4274: two prose claims were unpinned and one was wider than the monitors' behaviour.
# ============================================================================
def _mechanism_1():
    return TEXT[TEXT.index("### Mechanism 1"):TEXT.index("### Mechanism 2")]


def test_mechanism_1_bounds_the_30s_claim_by_the_engaged_window_and_the_mention_coalescing():
    m1 = _mechanism_1()
    assert "within one 30s poll" in m1
    assert "while you have typed in the last 360s" in m1, "the updates monitor defers its cycle while you are engaged"
    assert "120s" in m1 and "coalesce" in m1, "the mention monitor coalesces further mentions"


def test_the_30s_wake_is_bounded_wherever_it_is_stated():
    """Each statement of the 30s wake carries its bound on the same line or within the next three."""
    lines = TEXT.splitlines()
    stated = [i for i, ln in enumerate(lines) if "30s poll" in ln and ("wakes" in ln or "| **Monitor**" in ln)]
    assert len(stated) >= 3, stated
    for i in stated:
        window = " ".join(lines[i:i + 4])
        assert "typing" in window or "typed" in window, lines[i]


def test_step_2_pins_what_the_supervisor_does_to_a_prior_instance_and_what_the_forum_monitors_do():
    step2 = TEXT[TEXT.index("## Step 2"):TEXT.index("## Step 3")]
    assert "`monitor-supervisor.py` reaps the prior supervisor's process group on start (one\n  supervisor per uid)" in step2
    assert "WARN — a suspected duplicate is left\n  alive and reported by pid on stdout" in step2


def test_mechanism_1_pins_the_monitor_timeout_it_tells_an_agent_to_pass():
    m1 = _mechanism_1()
    assert "`timeout_ms: 1800000`" in m1 and "`expires in 30m`" in m1
