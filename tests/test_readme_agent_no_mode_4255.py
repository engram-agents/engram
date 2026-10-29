"""README-AGENT.md carries no mode language and tells the truth about agent coordination.

engram-alpha #4255 slice 1b. After the multi-agent build flag was retired
(slice 1), README-AGENT.md was the first page a new agent reads, and it still told
that agent to pass a flag that does nothing. This file pins the replacement.

Contract (design owner: the #4255 contract, slice 1b + the vocabulary rule):

  A. README-AGENT.md contains none of the retired spellings (the word goes
     entirely from shipped files: no mode, no flag, no "single" counterpart).
  B. No fenced code block passes a retired flag, and the build example still
     exists (so deleting it cannot pass this cell vacuously).
  C. Every in-page link resolves to a real heading under GitHub's slug rule, and
     the Contents list matches the `##` sections one-to-one, in order, with the
     same text.
  D. The coordination section ("Working with other agents") names comms_surface
     and forum.url, and does NOT claim a shared /home/agents-shared/ is required.
     (The #4255 spec text said letters and batons need it. The code says
     otherwise: ia.py, baton.py and forum.py are all clients of the forum server.
     See the docstring of D2 for file:line.)
  E. The tier facts in the README agree with src/build/packaging/tiers.json:
     ia.py / baton.py / forum.py ship at the default tier, are never described
     as gated behind a flag, and agentctl is described as not in the plugin
     bundle because tiers.json lists it under `excluded`.
  G. The install-depth section does not claim `jq` is needed (the installer reads
     install_tier with python3, #2955).
  H. The pointer to USER_GUIDE.md names a heading that exists there.

Every cell is written to be able to fail; see the mutation table in the PR.
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README-AGENT.md"
USER_GUIDE = REPO / "USER_GUIDE.md"
TIERS = REPO / "src" / "build" / "packaging" / "tiers.json"

# Separator class: hyphen, underscore, any whitespace (NBSP included), soft hyphen, and the Unicode
# hyphens and dashes (U+2010 to U+2015, U+2212), so a pasted "multi\u2011agent" cannot slip through.
FORBIDDEN = re.compile("(multi|single)[-_\\s\u00ad\u2010-\u2015\u2212]agent", re.IGNORECASE)

COORD_HEADING = "Working with other agents"
INSTALL_HEADING_PREFIX = "Install depth"


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lines_with_fence_state(text: str):
    """Yield (line, in_fence) where in_fence is True for lines inside a fenced
    block (the fence lines themselves count as inside)."""
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            yield line, True
            in_fence = not in_fence
            continue
        yield line, in_fence


def _h2_headings(text: str):
    """Top-level `## ` headings outside code fences, in order."""
    out = []
    for line, fenced in _lines_with_fence_state(text):
        if not fenced and line.startswith("## "):
            out.append(line[3:].strip())
    return out


def _all_headings(text: str):
    out = []
    for line, fenced in _lines_with_fence_state(text):
        if not fenced and re.match(r"#{1,6} ", line):
            out.append(re.sub(r"^#+\s+", "", line).strip())
    return out


def gh_slug(heading: str) -> str:
    """GitHub's heading anchor rule: lowercase, drop punctuation except hyphen
    and underscore, spaces to hyphens (no collapsing)."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def _slugs(text: str):
    seen = {}
    out = []
    for h in _all_headings(text):
        base = gh_slug(h)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}-{n}")
    return out


def _section(text: str, heading_prefix: str) -> str:
    """Body of the `## ` section whose heading starts with heading_prefix."""
    lines = []
    capture = False
    for line, fenced in _lines_with_fence_state(text):
        if not fenced and line.startswith("## "):
            if capture:
                break
            capture = line[3:].strip().startswith(heading_prefix)
            continue
        if capture:
            lines.append(line)
    assert lines, f"no '## {heading_prefix}...' section in README-AGENT.md"
    return "\n".join(lines)


def _fenced_blocks(text: str):
    blocks, cur, in_fence = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if in_fence:
                blocks.append("\n".join(cur))
                cur = []
            in_fence = not in_fence
            continue
        if in_fence:
            cur.append(line)
    return blocks


def _toc_entries(text: str):
    """[(number, link_text, slug)] from the numbered list under `## Contents`."""
    body = _section(text, "Contents")
    return [
        (int(m.group(1)), m.group(2), m.group(3))
        for m in re.finditer(r"^(\d+)\. \[(.+?)\]\(#([^)]+)\)\s*$", body, re.M)
    ]


@pytest.fixture(scope="module")
def readme() -> str:
    return _read(README)


@pytest.fixture(scope="module")
def tiers() -> dict:
    return json.loads(_read(TIERS))


def _tier_of(tiers: dict, path: str):
    for m in tiers["mechanisms"]:
        if m.get("path") == path:
            return m.get("tier")
    return None


# --------------------------------------------------------------------------
# A. the retired spellings are gone, everywhere (prose, tables, code spans)
# --------------------------------------------------------------------------

def test_A_no_retired_spellings(readme):
    hits = [
        f"line {i}: {line.strip()[:100]}"
        for i, line in enumerate(readme.splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert not hits, "retired mode wording in README-AGENT.md:\n" + "\n".join(hits)


def test_A_no_retired_spelling_split_across_a_line_break(readme):
    # The per-line scan above cannot see 'multi-' / 'agent' broken over two lines.
    flat = re.sub(r"\s+", " ", readme)
    assert not FORBIDDEN.search(flat)


# --------------------------------------------------------------------------
# B. the build example
# --------------------------------------------------------------------------

def test_B_build_example_has_no_retired_flag_and_still_exists(readme):
    builds = [b for b in _fenced_blocks(readme) if "build-plugin.sh" in b]
    assert builds, "the build-plugin.sh example was deleted; it should be rewritten"
    for b in builds:
        assert not re.search(r"--(multi|single)[-_]agent", b), b
    # the example still shows the one real choice, the depth tier
    assert any("--tier" in b for b in builds)


# --------------------------------------------------------------------------
# C. anchors and the Contents list
# --------------------------------------------------------------------------

def test_C_every_in_page_link_resolves_to_a_heading(readme):
    slugs = set(_slugs(readme))
    # drop fenced code so a shell comment like '[x](#y)' cannot count
    prose = "\n".join(l for l, fenced in _lines_with_fence_state(readme) if not fenced)
    links = re.findall(r"\]\(#([^)\s]+)\)", prose)
    assert links, "expected in-page links (Contents list)"
    dangling = sorted({l for l in links if l not in slugs})
    assert not dangling, f"in-page links with no matching heading: {dangling}"


def test_C_contents_matches_sections_one_to_one(readme):
    toc = _toc_entries(readme)
    after = _h2_headings(readme)
    after = after[after.index("Contents") + 1:]
    assert [n for n, _, _ in toc] == list(range(1, len(toc) + 1)), "Contents numbering"
    assert len(toc) == len(after), (
        f"Contents lists {len(toc)} sections, the file has {len(after)} after it"
    )
    for (n, text, slug), heading in zip(toc, after):
        assert text == heading, f"Contents #{n} text {text!r} != heading {heading!r}"
        assert slug == gh_slug(heading), (
            f"Contents #{n} links #{slug}, heading {heading!r} has slug #{gh_slug(heading)}"
        )


def test_C_renamed_sections_are_reachable_from_contents(readme):
    toc_text = [t for _, t, _ in _toc_entries(readme)]
    assert any(t.startswith(INSTALL_HEADING_PREFIX) for t in toc_text)
    assert COORD_HEADING in toc_text


# --------------------------------------------------------------------------
# D. the coordination section says only what the code does
# --------------------------------------------------------------------------

def test_D_section_names_comms_surface_and_forum_url(readme):
    sec = _section(readme, COORD_HEADING)
    assert "comms_surface" in sec
    assert "forum.url" in sec
    # the two values the code recognises (_prompthooklib.get_comms_surface)
    assert "umwelt" in sec and "forum" in sec


def test_D2_section_does_not_claim_a_shared_agents_dir_is_required(readme):
    """ia.py:13 'PURE LAN-API: no local inter-agent/ filesystem reads or writes';
    baton.py:3490 builds a ForumClient and every cmd_* takes it; forum.py:155-180
    resolves forum.url. None of the three needs /home/agents-shared/ on the
    agent's side; the shared directory belongs to the forum SERVER
    (docs/FORUM-SERVER.md). So the section must not name it as a requirement."""
    sec = _section(readme, COORD_HEADING)
    assert "/home/agents-shared" not in sec


def test_D3_section_keeps_the_forum_server_pointer_and_agentctl_list(readme):
    sec = _section(readme, COORD_HEADING)
    assert "docs/FORUM-SERVER.md" in sec
    assert "docs/baton-protocol.md" in sec
    for sub in ("spawn", "finalize-name", "session", "bash", "health", "share"):
        assert f"`{sub}`" in sec, f"agentctl subcommand {sub} dropped from the list"


# --------------------------------------------------------------------------
# E. tier facts agree with tiers.json
# --------------------------------------------------------------------------

COORD_TOOLS = ("tools/ia.py", "tools/baton.py", "tools/forum.py")
COORD_HOOKS = (
    "hooks/claude/engram-inter-agent-prompt-hook.py",
    "hooks/claude/engram-baton-prompt-hook.py",
    "hooks/claude/engram-forum-prompt-hook.py",
)
# 'only with a flag', 'only when you pass a build flag', 'only if ... flag'
FLAG_GATED = re.compile(r"only\s+(?:with|when|if)\b[^.\n]{0,60}\bflag", re.IGNORECASE)


def test_E_tiers_json_says_the_coordination_tools_ship_by_default(tiers):
    order = tiers["depth_tiers"]
    default_rank = order.index(tiers["default_tier"])
    for path in COORD_TOOLS + COORD_HOOKS:
        t = _tier_of(tiers, path)
        assert t in order, f"{path} not tiered"
        assert order.index(t) <= default_rank, f"{path} is above the default tier"


def test_E_readme_does_not_call_coordination_flag_gated(readme):
    flat = re.sub(r"\s+", " ", readme)
    hits = FLAG_GATED.findall(flat)
    assert not hits, f"README-AGENT.md still gates something behind a flag: {hits}"


def test_E_coordination_section_never_mentions_a_flag(readme):
    """The coordination tools ship by tier, not by any build flag. The phrase regex above only
    catches 'only with ... flag'; this closes the rewordings ('you must pass a build flag')."""
    sec = _section(readme, COORD_HEADING)
    assert not re.search(r"\bflags?\b", sec, re.IGNORECASE), "the coordination section talks about a flag"


def test_E_readme_names_the_tier_the_coordination_tools_ship_at(readme, tiers):
    sec = _section(readme, COORD_HEADING)
    for name in ("ia.py", "baton.py", "forum.py"):
        assert name in sec
    assert tiers["default_tier"] in sec  # 'convenience'
    # the hooks note in the file-layout section says the same
    i = readme.index("Hooks are registered in the plugin")
    note = readme[i:i + 1200]
    assert "prompt hooks" in note and tiers["default_tier"] in note


def test_E_agentctl_is_excluded_in_tiers_json_and_described_so(readme, tiers):
    excluded = {e["path"] for e in tiers["excluded"]}
    assert {"tools/agentctl", "tools/agentctl-mac", "tools/agent-bootstrap"} <= excluded
    sec = _section(readme, COORD_HEADING)
    assert re.search(r"not in the plugin bundle", sec)
    assert _tier_of(tiers, "tools/agentctl") is None  # in no tier at all


# --------------------------------------------------------------------------
# G / H. the two neighbours fixed in the same paragraphs
# --------------------------------------------------------------------------

def test_G_install_depth_section_does_not_claim_jq(readme):
    sec = _section(readme, INSTALL_HEADING_PREFIX)
    assert not re.search(r"\bjq\b", sec)
    assert "install_tier" in sec and "python3" in sec


def test_H_pointer_into_user_guide_names_a_heading_that_exists(readme):
    sec = _section(readme, COORD_HEADING)
    assert "USER_GUIDE.md" in sec
    m = re.search(r'"([^"\n]+)"\s+section\s+of\s+`?USER_GUIDE\.md', sec)
    assert m, 'the pointer should read: the "<section title>" section of USER_GUIDE.md'
    title = m.group(1)
    guide_h2 = [h for h in _h2_headings(_read(USER_GUIDE))]
    assert any(h.startswith(title) for h in guide_h2), (title, guide_h2)


# ---------------------------------------------------------------------------
# F. who falls back to $FORUM_URL (PR 4266 round 1): the sentence "the CLIs fall back to $FORUM_URL,
# then localhost:5002" was false for two of the three CLIs. Only forum.py falls back
# (forum.py _resolve_forum_url); ia exits 3 without forum.url (ia.py _check_forum_configured) and the
# baton write verbs do the same (baton.py _require_forum). A reviewer ran all three.
# ---------------------------------------------------------------------------
def _sentences(text):
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.;:])\s+(?=[A-Z`*(\[])", flat)


def test_F_the_section_does_not_say_the_cli_s_all_fall_back(readme):
    sec = _section(readme, COORD_HEADING)
    flat = re.sub(r"\s+", " ", sec)
    assert not re.search(r"\b(the )?(CLIs|clients) (all )?fall back\b", flat, re.I), \
        "an unqualified 'the CLIs fall back' is false for ia and baton"


def test_F_ia_and_baton_are_said_to_need_forum_url(readme):
    sec = _section(readme, COORD_HEADING)
    hits = [s for s in _sentences(sec)
            if "forum.url" in s and re.search(r"`?ia`?\b", s) and "baton" in s
            and re.search(r"\b(need|needs|require|requires|refuse|refuses)\b", s)]
    assert hits, "the section must say that ia and baton need forum.url (they exit 3 without it)"


def test_F_the_env_fallback_is_attributed_to_the_forum_cli_only(readme):
    sec = _section(readme, COORD_HEADING)
    hits = [s for s in _sentences(sec) if "FORUM_URL" in s]
    assert hits, "the section should still say where the forum CLI's fallback comes from"
    for s in hits:
        assert re.search(r"`forum`|forum\.py|forum CLI", s) and not re.search(r"\bia\b[^.]{0,40}fall", s), s


def test_F_the_server_is_not_said_to_be_installed_once_per_host(readme):
    sec = _section(readme, COORD_HEADING)
    assert "once per host" not in sec.lower(), \
        "agents on other hosts use the same server: it is installed once, on the machine that hosts it"
