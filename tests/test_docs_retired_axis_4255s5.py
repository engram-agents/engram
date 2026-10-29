"""#4255 slice 5: the retired multi-agent / single-agent axis is gone from the docs
and the forum server's wording, and the pages that replaced it say true things.

Contract (written before the edits; the cells below are the stated behaviours):

- A. No forbidden spelling in the in-scope living pages and the forum files this
  slice touched: the regex ``(multi|single)<sep>agent`` (separator = hyphen,
  underscore, whitespace, soft hyphen, any Unicode hyphen/dash U+2010-2015,
  minus U+2212, or nothing) case-insensitively, plus ``multi_agent``, plus the
  ``+MA`` tier-suffix of the retired flag.  The only exceptions are the entries
  of ``ALLOWED``, each justified: text naming a real artifact or a dated snapshot.
- B. Every relative markdown link in the in-scope pages resolves, and every
  ``NN-name.md`` chapter name written in the handbook names a file that exists.
- C. The chapter was renamed, not deleted: the old path is gone, the new one
  exists and carries the new title, and the handbook README table and
  FINDINGS.md name the new file.
- D. The forum server's no-colleague wording: the log line the weak flip gate
  emits says "no colleague participant; skipping colleague gate" (no mode word),
  and that is what a real flip produces (the behavioural twin of this cell is
  ``src/forum/tests/test_projects_api.py``).
- E. Two kinds of cell, with limits stated plainly. (1) The source cells check that the facts the
  rewritten pages rely on are still written in the code (a ``tiers.json`` tier, a hook's gate, the
  clients being API-only). They string-match the source, so they pin the FACTS, not the behaviour:
  the behaviour is guarded by the existing suites (``test_ia_cli.py``, ``test_forum_gate_3546.py``).
  (2) The page cells look for KNOWN phrasings of the old false claims (letters and batons need a
  shared directory, work only on one host, need ``mode = multi``, a file protocol, an opt-in
  flag). A heuristic: a sentence worded in a way not listed here still passes. A review by reading
  is what finds those; the listed phrasings are the ones reviewers have actually found.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# --- the in-scope files -----------------------------------------------------
DOC_FILES = (
    [p.relative_to(ROOT).as_posix() for p in sorted((ROOT / "docs" / "handbook").glob("*.md"))]
    + [
        "docs/baton-protocol.md",
        "docs/macos-agent-setup.md",
        "docs/os-harness-constraints.md",
        "docs/FORUM-SERVER.md",
        "USER_GUIDE.md",
    ]
)
FORUM_FILES = [
    "src/forum/server.py",
    "src/forum/tests/test_baton_advance_api.py",
    "src/forum/tests/test_baton_anchor_round6.py",
    "src/forum/tests/test_baton_phase_injection.py",
    "src/forum/tests/test_projects_api.py",
]
SCANNED = DOC_FILES + FORUM_FILES

_SEP = "-_\\s­‐‑‒–—―−"
FORBIDDEN = re.compile(rf"(multi|single)[{_SEP}]*agent", re.IGNORECASE)
# the ``+MA`` suffix the retired flag left in the handbook's tier columns
FORBIDDEN_TAG = re.compile(r"\+MA\b|\bT2\+MA\b")

# (file, substring of the line) -> why that line may keep the word.
ALLOWED = {
    ("docs/handbook/00-inventory.md", "no clean multi-agent gu)"):
        "a verbatim, truncated quote of the reason string tiers.json carries for templates/agents.json.example, in a "
        "snapshot table generated 2026-06-05; tiers.json still says it",
}


def _lines(rel):
    return (ROOT / rel).read_text(encoding="utf-8").splitlines()


def _allowed(rel, line):
    return any(f == rel and sub in line for (f, sub) in ALLOWED)


# ============================================================================
# A. the word is gone
# ============================================================================
@pytest.mark.parametrize("rel", SCANNED)
def test_a_no_forbidden_spelling(rel):
    hits = [
        f"{rel}:{i}: {line.strip()[:110]}"
        for i, line in enumerate(_lines(rel), 1)
        if (FORBIDDEN.search(line) or FORBIDDEN_TAG.search(line)) and not _allowed(rel, line)
    ]
    assert not hits, "retired axis wording still present (#4255 slice 5):\n" + "\n".join(hits)


def test_a_every_allowlist_entry_still_matches_a_line():
    """An allowance for text that is gone is a stale excuse (and a hole for the word to come back through)."""
    for (rel, sub), why in ALLOWED.items():
        assert why.strip()
        assert any(sub in line and FORBIDDEN.search(line) for line in _lines(rel)), (
            f"ALLOWED entry {(rel, sub)!r} matches no line that carries the word"
        )


def test_a_regex_catches_every_spelling_it_is_meant_to():
    for s in ("multi-agent", "Multi-Agent", "single-agent", "single_agent", "multi_agent", "multi agent",
              "single agent", "multi‑agent", "single‐agent", "multi−agent", "multiagent"):
        assert FORBIDDEN.search(s), s
    for s in ("agents", "multiple agents", "one agent", "single-threaded"):
        assert not FORBIDDEN.search(s), s


# ============================================================================
# B. links resolve
# ============================================================================
_MD_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CHAPTER = re.compile(r"(?<![\w/.-])(\d\d-[A-Za-z0-9][A-Za-z0-9-]*\.md)\b")


@pytest.mark.parametrize("rel", DOC_FILES)
def test_b_relative_links_resolve(rel):
    base = (ROOT / rel).parent
    bad = []
    for i, line in enumerate(_lines(rel), 1):
        for target in _MD_LINK.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if path and not (base / path).resolve().exists():
                bad.append(f"{rel}:{i}: ({target})")
    assert not bad, "unresolvable relative link(s):\n" + "\n".join(bad)


@pytest.mark.parametrize("rel", [r for r in DOC_FILES if r.startswith("docs/handbook/")])
def test_b_handbook_chapter_names_exist(rel):
    missing = []
    for i, line in enumerate(_lines(rel), 1):
        for name in _CHAPTER.findall(line):
            if not (ROOT / "docs" / "handbook" / name).exists():
                missing.append(f"{rel}:{i}: {name}")
    assert not missing, "chapter file(s) named but absent:\n" + "\n".join(missing)


# ============================================================================
# C. the renamed chapter
# ============================================================================
OLD = "docs/handbook/06-multi-agent.md"
NEW = "docs/handbook/06-working-with-other-agents.md"


def test_c_old_chapter_path_is_gone():
    assert not (ROOT / OLD).exists()


def test_c_new_chapter_exists_with_the_new_title():
    assert (ROOT / NEW).is_file()
    assert _lines(NEW)[0] == "# 06 — Working with other agents (L5)"
    body = "\n".join(_lines(NEW))
    assert "# Working with other agents" in body
    # rewritten, not emptied: the forum, the letters/baton channel and the packs content all survive
    for must in ("## 1. Forum server", "## 3.", "## 4. Knowledge packs", "ia.py", "baton"):
        assert must in body, must


def test_c_readme_table_and_findings_name_the_new_file():
    readme = "\n".join(_lines("docs/handbook/README.md"))
    assert "`06-working-with-other-agents.md`" in readme
    assert "06-multi-agent" not in readme
    findings = "\n".join(_lines("docs/handbook/FINDINGS.md"))
    assert "06-working-with-other-agents (L5)" in findings
    assert "06-multi-agent" not in findings


def test_c_no_tracked_file_outside_internal_and_changelog_still_names_the_old_chapter():
    out = subprocess.run(
        ["git", "grep", "-l", "-F", "06-multi-agent", "--", ".", ":!docs/internal", ":!docs/CHANGELOG.md",
         ":!tests/test_docs_retired_axis_4255s5.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"still names the old chapter path: {out.stdout}"


# ============================================================================
# D. the forum server's no-colleague wording
# ============================================================================
NEW_WARN = "no colleague participant; skipping colleague gate"


def test_d_server_log_line_says_what_is_true_without_a_mode_word():
    src = (ROOT / "src/forum/server.py").read_text(encoding="utf-8")
    assert NEW_WARN in src
    assert "single-agent mode; skipping colleague gate" not in src
    assert not FORBIDDEN.search(src)


# ============================================================================
# E. the behaviour claims the pages now make are true in the code
# ============================================================================
def _tiers():
    return json.loads((ROOT / "src/build/packaging/tiers.json").read_text(encoding="utf-8"))


def _tier_of(path):
    for m in _tiers()["mechanisms"]:
        if m.get("path") == path:
            return m.get("tier")
    return None


@pytest.mark.parametrize("path", [
    "tools/ia.py", "tools/baton.py", "tools/forum.py",
    "tools/forum-mention-monitor.sh", "tools/forum-updates-monitor.sh",
    "hooks/claude/engram-inter-agent-prompt-hook.py",
    "hooks/claude/engram-baton-prompt-hook.py",
    "hooks/claude/engram-forum-prompt-hook.py",
    "skills/claude/engram-letter", "skills/claude/engram-baton",
    "skills/claude/engram-forum", "skills/claude/engram-collaborating-loop",
])
def test_e_the_coordination_tools_ship_at_convenience_tier(path):
    """The pages say: ia, baton, forum, their hooks, monitors and skills ship at the convenience tier."""
    assert _tier_of(path) == "convenience"


@pytest.mark.parametrize("path", ["tools/agentctl", "tools/agentctl-mac", "tools/agent-bootstrap"])
def test_e_host_administration_scripts_are_excluded_from_every_build(path):
    """The pages say: agentctl, agentctl-mac and agent-bootstrap are repo-only operator tools."""
    assert path in {e.get("path") for e in _tiers()["excluded"]}
    assert _tier_of(path) is None


def test_e_no_mechanism_carries_a_flag_axis_any_more():
    """The pages say the depth tier is the only filter."""
    assert not [m["path"] for m in _tiers()["mechanisms"] if "multi_agent" in m]


@pytest.mark.parametrize("hook", [
    "engram-inter-agent-prompt-hook", "engram-baton-prompt-hook", "engram-forum-prompt-hook",
])
def test_e_the_prompt_hooks_stay_silent_until_forum_url_is_set(hook):
    src = (ROOT / "src/engram/hooks/claude" / f"{hook}.py").read_text(encoding="utf-8")
    assert re.search(r"not _is_forum_configured\(config\)", src), hook
    m = re.search(r"def _is_forum_configured\(.*?\n(?=def |\Z)", src, re.S)
    assert m and '"url"' in m.group(0), hook


def test_e_ia_and_baton_refuse_without_forum_url_and_forum_cli_does_not():
    ia = (ROOT / "src/engram/tools/ia.py").read_text(encoding="utf-8")
    baton = (ROOT / "src/engram/tools/baton.py").read_text(encoding="utf-8")
    forum = (ROOT / "src/engram/tools/forum.py").read_text(encoding="utf-8")
    assert "ia requires a configured forum." in ia and "EXIT_STATE = 3" in ia
    assert "baton requires a configured forum." in baton
    assert "def _check_forum_configured" in ia and "def _require_forum" in baton
    # forum.py falls back to $FORUM_URL and then a default URL, so it needs no forum.url
    assert "_DEFAULT_FORUM_URL" in forum and 'os.environ.get("FORUM_URL"' in forum
    assert "requires a configured forum" not in forum


def test_e_the_clients_are_pure_forum_clients_with_no_shared_directory():
    ia = (ROOT / "src/engram/tools/ia.py").read_text(encoding="utf-8")
    assert "PURE LAN-API: no local inter-agent/ filesystem reads or writes." in ia
    forum = (ROOT / "src/engram/tools/forum.py").read_text(encoding="utf-8")
    assert "agents-shared" not in ia and "agents-shared" not in forum
    coord = (ROOT / "src/forum/coordination/__init__.py").read_text(encoding="utf-8")
    assert "reachable by co-host AND cross-host agents alike" in coord


def test_e_baton_files_live_in_the_forum_servers_store_not_on_the_agents_host():
    store = (ROOT / "src/forum/coordination/store_file.py").read_text(encoding="utf-8")
    assert 'self.projects_dir = self.root / "projects"' in store
    assert 'os.environ.get("FORUM_HOME"' in store


def test_e_the_forum_server_installer_needs_linux_with_systemd_user():
    doc = (ROOT / "docs/FORUM-SERVER.md").read_text(encoding="utf-8")
    assert "**Linux with `systemd --user`.**" in doc


def test_e_comms_surface_choices_are_forum_and_umwelt():
    baton = (ROOT / "src/engram/tools/baton.py").read_text(encoding="utf-8")
    assert re.search(r"comms_surface", baton)
    assert "umwelt" in baton and "forum" in baton


# --- the false claims the old wording made must not come back -------------------------------
FALSE_CLAIMS = {
    "work only on one host": re.compile(r"only\s+[^.\n|]{0,40}\b(one|same)[- ](host|machine)", re.I),
    "opt-in flag or mode": re.compile(r"\bopt[- ]in\b[^.\n|]{0,60}\b(flag|coordination|mode)\b", re.I),
    "coordination mode or flag": re.compile(r"\bcoordination[- ](mode|flag)\b", re.I),
    # Phrasings of the same false claim that carry none of the retired words (found by the round 1 reviewer
    # and by my grep on #4265: each sat in a page of this PR while the first guard stayed green).
    "agents must share a filesystem": re.compile(r"\bshare\s+a\s+(file\s?system|director(y|ies))\b", re.I),
    "cannot leave one machine": re.compile(r"\bcannot\s+leave\s+(one|the same)\s+(machine|host)\b", re.I),
    "a coordination switch": re.compile(r"\bcoordination\s*=\s*(on|true)\b|\bmode\s+is\s+set\s+to\b", re.I),
    "a directory under agents-shared on the agent side": re.compile(
        r"\b(read|write)s?\b[^.\n|]{0,30}director(y|ies)\s+under\s+/home/agents-shared", re.I),
    "file protocol": re.compile(r"\bfile protocol\b", re.I),
    "same-host wording": re.compile(r"\bsame[- ]host\b", re.I),
    "the only cross-host channel": re.compile(r"\bonly cross-host\b|\bonly channel that spans", re.I),
    # The constant INTER_AGENT_DIR still exists in the session-start hook and a helper, so only the false
    # DESCRIPTIONS are matched: a hook that scans it, a letters directory under ~/.engram (not the
    # inter-agent-*-cursor.txt files), or the old default path.
    "a local inter-agent directory": re.compile(
        r"scans?\s+INTER_AGENT_DIR|~/\.engram/inter-agent(?![-\w])|default\s+/home/agents-shared/inter-agent", re.I),
    "needs a shared directory": re.compile(
        r"(?<!not )(?<!n't )\b(requires?|needs?|need)\b(?!\s+(no|not)\b)[^.\n|]{0,60}(shared (file ?system|dir(ectory)?)|/home/agents-shared)", re.I),
    "shared filesystem channels": re.compile(r"shared[- ]filesystem channels|same-host FS|shared dir for", re.I),
    "mode = multi": re.compile(r"\bmode\s*=\s*\"?(multi|single)\b", re.I),
}


@pytest.mark.parametrize("rel", DOC_FILES)
def test_e_pages_carry_none_of_the_known_false_claim_phrasings(rel):
    hits = []
    for i, line in enumerate(_lines(rel), 1):
        for name, rx in FALSE_CLAIMS.items():
            if rx.search(line):
                hits.append(f"{rel}:{i}: [{name}] {line.strip()[:100]}")
    assert not hits, "\n".join(hits)


def test_e_false_claim_patterns_catch_the_old_sentences():
    old = [
        "letters and batons need a shared /home/agents-shared/, so they work only between agents on the same host",
        "they work only on one host",
        "Shared-filesystem channels (`baton` / `ia`) are extra.",
        "Need a shared dir (`/Users/Shared/...`, setgid+ACL) + `mode=multi`.",
        "letters/baton = same-host FS",
        # rewordings that slipped through the first version of this list (#4255 slice 5, my mutants)
        "so only an agent on the same machine can read the batons",
        "Baton is switched on by the coordination mode in config.json.",
        "Baton is opt-in: build with the coordination flag.",
        # seven more rewordings the round 1 reviewer showed the first patterns missed
        "ia and baton work between agents on the same host.",
        "Letters and batons are same-host channels.",
        "Both CLIs read and write a directory under /home/agents-shared.",
        "Baton requires the agents to share a filesystem.",
        "ia and baton cannot leave one machine.",
        "Set coordination = on to enable the baton hook.",
        "ia refuses unless mode is set to multi.",
        # round 1 reviewer / my grep on #4265
        "| `ia.py` | `engram-letter` | file protocol: `~/.engram/inter-agent/` |",
        "CLIs (L4) carry coordination: ia letters + batons same-host, the forum (L5) cross-host",
        "Verb-first HTTP client over the forum API (the only cross-host channel)",
        "Letter discovery: scans INTER_AGENT_DIR for `*.md` files (glob at line 279)",
        "key constants — INTER_AGENT_DIR (env-override, default /home/agents-shared/inter-agent)",
    ]
    for s in old:
        assert any(rx.search(s) for rx in FALSE_CLAIMS.values()), s


def test_e_false_claim_patterns_leave_the_true_negations_alone():
    true_sentences = [
        "`baton` and `ia` need no shared directory.",
        "A letter does not need a shared directory between hosts.",
        "Baton needs a reachable forum server and nothing else.",
        "agents on other hosts use the same URL",
    ]
    for s in true_sentences:
        assert not any(rx.search(s) for rx in FALSE_CLAIMS.values()), s
