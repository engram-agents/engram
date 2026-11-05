"""engram-alpha#4323: the dream master carries an ENUMERATED tools list, not ``tools: *``.

Contract (written before the spec edit; Kepler's ruling, issue comment 6643606686, and the
evidence on the same issue):

* The list is every ENGRAM tool observed in real dream-master use on any seat, plus every
  tool the spec directs, minus exactly the eight the spec forbids; both namespaces
  (``mcp__engram__`` and ``mcp__plugin_engram_engram__``); plus ``Bash``, ``Read``,
  ``Write``, ``Edit``, ``ToolSearch``, ``Skill``. No ``Agent``, no umwelt verb, no other
  MCP server.
* Why enumerate: ``tools: *`` inherits every MCP server a seat has (a Gmail call by a dream
  master was seen in July), and the umwelt world verbs through the sleeping parent's avatar.
* Why the list is READ OFF transcripts and not guessed: a tool missing from the list fails
  SILENTLY on the first night its branch fires.

Provenance of ``OBSERVED_ENGRAM`` below: the 12 Claude Code scanner outputs posted on #4323
(``dream_master_tools_scan.py`` v1 and ``dream_master_tools_scan_v2.py``, seats Sol, Kepler,
Zephyr, Ariadne, Halley, Borges, Luria, Aleph, Clio), ``called`` plus
``loaded_by_toolsearch_select``, names normalised across both prefixes. 40 names were seen;
one of them, ``engram_add_observation``, is on the forbidden list and is removed by the
ruling's own subtraction. Every tool the spec directs was also observed (no spec-only name),
so ``OBSERVED_ENGRAM`` alone equals the allowed set today; cell
``test_every_engram_tool_the_spec_names_is_allowed_or_forbidden`` keeps it honest if the spec
later directs a new tool.

Dimensions the cells vary on purpose (a cell that holds one constant cannot catch its mutant):
the source spec, the Claude-built bundle copy, the Codex TOML conversion, the namespace pair,
and the spec BODY text that restates the grant.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "src" / "engram" / "agents" / "claude" / "engram-dream-master.md"
SERVER = REPO_ROOT / "src" / "engram" / "server.py"

NAMESPACES = ("mcp__engram__", "mcp__plugin_engram_engram__")
BASE_TOOLS = {"Bash", "Read", "Write", "Edit", "ToolSearch", "Skill"}

FORBIDDEN_ENGRAM = {
    "engram_add_axiom",
    "engram_add_conjecture",
    "engram_add_definition",
    "engram_add_goal",
    "engram_add_observation",
    "engram_add_observation_batch",
    "engram_checkpoint",
    "engram_nap",
}

# 39 names: the 40 observed minus engram_add_observation (see the module docstring).
OBSERVED_ENGRAM = {
    "engram_add_cornerstone", "engram_add_edge", "engram_add_lesson", "engram_add_person",
    "engram_add_task", "engram_advance_turn", "engram_ask", "engram_cascade_frontier",
    "engram_cascade_resolve", "engram_contradict", "engram_derive", "engram_diagnose",
    "engram_focus", "engram_focus_swap", "engram_goal_tension", "engram_history",
    "engram_inspect", "engram_lesson_register_incident", "engram_link_about", "engram_list",
    "engram_list_focused", "engram_outgrow_cornerstone", "engram_query",
    "engram_query_pattern", "engram_reflect", "engram_register_exemplar",
    "engram_remove_edge", "engram_report_feeling", "engram_resolve", "engram_retract",
    "engram_retype_edge", "engram_set_person_lineage", "engram_set_recall_summaries",
    "engram_stats", "engram_supersede", "engram_unfocus", "engram_update_lesson",
    "engram_update_person", "engram_update_task",
}
ALLOWED_ENGRAM = OBSERVED_ENGRAM - FORBIDDEN_ENGRAM

# The server tools that are neither on the list nor forbidden: not observed in any seat's dream-master use and not
# directed by the spec, so the ruled rule leaves them off. The cell below forces a decision when the server gains a
# tool (#361's "no new ENGRAM tool silently blocked from the master", kept as a mechanical gate under the list route).
# To change this set: a tool moves to OBSERVED_ENGRAM with its evidence and to the spec's tools: line, or stays here
# with a reason. Never edit it just to turn the cell green.
EXCLUDED_ENGRAM = {
    "engram_activate_goal", "engram_add_evidence", "engram_add_special_moment", "engram_add_trust_signal",
    "engram_deactivate_goal", "engram_embedding_cluster", "engram_focus_delete_set", "engram_focus_load",
    "engram_focus_save", "engram_focus_sets", "engram_get_subgraph", "engram_requote", "engram_sandbox",
    "engram_scan_emergence", "engram_set_trust_tier", "engram_sign", "engram_surface",
}


def _server_tools() -> set[str]:
    server = SERVER.read_text(encoding="utf-8")
    return set(re.findall(r"^(?:async )?def (engram_[a-z_]+)", server, re.MULTILINE))


def _split_spec(text: str) -> tuple[str, str]:
    parts = text.split("---", 2)
    assert len(parts) == 3, "agent spec lost its frontmatter"
    return parts[1], parts[2]


def _tools_line(frontmatter: str) -> str:
    m = re.search(r"^tools:[ \t]*(.*)$", frontmatter, re.MULTILINE)
    assert m, "frontmatter has no tools: line"
    return m.group(1).strip()


def _tool_entries(tools_line: str) -> list[str]:
    return [t.strip() for t in tools_line.split(",") if t.strip()]


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def entries(spec_text) -> list[str]:
    return _tool_entries(_tools_line(_split_spec(spec_text)[0]))


def _expected_entries() -> set[str]:
    return BASE_TOOLS | {ns + name for name in ALLOWED_ENGRAM for ns in NAMESPACES}


# ------------------------------------------------------------------------------ the list itself


def test_tools_is_an_enumerated_list_not_star(spec_text):
    line = _tools_line(_split_spec(spec_text)[0])
    assert line != "*", "tools: * inherits every MCP server and every umwelt verb (#4323)"
    assert "*" not in line, f"a wildcard in the tools line: {line!r}"
    assert len(_tool_entries(line)) > 1


def test_the_list_is_exactly_the_ruled_set(entries):
    got, want = set(entries), _expected_entries()
    assert got == want, (
        f"missing {sorted(want - got)} (a missing tool fails silently the first night its branch "
        f"fires); extra {sorted(got - want)}"
    )


def test_no_duplicate_entries(entries):
    dupes = sorted({e for e in entries if entries.count(e) > 1})
    assert dupes == [], f"duplicate tool entries: {dupes}"


def test_both_namespaces_for_every_engram_tool(entries):
    """The pair is symmetric: a tool in one namespace and not the other fails on the other install."""
    have = set(entries)
    for name in ALLOWED_ENGRAM:
        for ns in NAMESPACES:
            assert ns + name in have, f"{ns}{name} missing (the other namespace is present: {any((o + name) in have for o in NAMESPACES)})"


def test_the_eight_forbidden_tools_are_absent_in_both_namespaces(entries):
    have = set(entries)
    for name in sorted(FORBIDDEN_ENGRAM):
        for ns in NAMESPACES:
            assert ns + name not in have, f"{ns}{name} is forbidden by the spec's own 'No ...' lines"


def test_no_agent_no_umwelt_no_other_mcp_server(entries):
    for e in entries:
        if e in BASE_TOOLS:
            continue
        assert re.fullmatch(r"mcp__(engram|plugin_engram_engram)__engram_[a-z_]+", e), (
            f"{e!r} is neither a base tool nor an ENGRAM MCP tool (umwelt verb, other MCP server, "
            "Agent, or a wildcard)"
        )
    assert "Agent" not in entries
    assert not any("umwelt" in e for e in entries)


def test_every_listed_engram_tool_exists_on_the_server(entries):
    """A name that is not a server tool is a typo that would be a silent dead entry."""
    on_server = _server_tools()
    listed = {re.sub(r"^mcp__[a-z_]*?engram__", "", e) for e in entries if e not in BASE_TOOLS}
    assert listed - on_server == set(), f"not server tools: {sorted(listed - on_server)}"


def test_every_server_tool_is_allowed_forbidden_or_explicitly_excluded():
    """A new ENGRAM server tool must get a decision for the dream master, not be silently unreachable (#361's second reason)."""
    unclassified = sorted(_server_tools() - ALLOWED_ENGRAM - FORBIDDEN_ENGRAM - EXCLUDED_ENGRAM)
    assert unclassified == [], (
        f"ENGRAM server tool(s) with no decision for the dream master: {unclassified}. Add each to OBSERVED_ENGRAM with its "
        "evidence AND to the spec's tools: line (both namespaces), or to EXCLUDED_ENGRAM with a reason."
    )


def test_the_excluded_set_names_real_tools_and_overlaps_nothing():
    stale = sorted(EXCLUDED_ENGRAM - _server_tools())
    overlap = sorted(EXCLUDED_ENGRAM & (ALLOWED_ENGRAM | FORBIDDEN_ENGRAM))
    assert stale == [], f"EXCLUDED_ENGRAM names tools the server no longer has: {stale}"
    assert overlap == [], f"EXCLUDED_ENGRAM overlaps the allowed or forbidden set: {overlap}"


def test_every_engram_tool_the_spec_names_is_allowed_or_forbidden(spec_text):
    """A tool the spec directs must be on the list; one it forbids must not (keeps the pinned set honest)."""
    named = set(re.findall(r"\bengram_[a-z_]+", _split_spec(spec_text)[1]))
    unknown = sorted(named - ALLOWED_ENGRAM - FORBIDDEN_ENGRAM)
    assert unknown == [], f"spec body names tools that are neither allowed nor forbidden: {unknown}"


def test_the_frontmatter_has_exactly_one_tools_key(spec_text):
    """Claude Code reads one key and the Codex converter another if there are two (reviewer round 1)."""
    keys = re.findall(r"^tools[ \t]*:", _split_spec(spec_text)[0], re.MULTILINE)
    assert len(keys) == 1, f"{len(keys)} tools: keys in the frontmatter"


# ------------------------------------------------------------------------------ the spec body


def test_the_body_states_the_limits_the_list_enforces(spec_text):
    """The grant paragraph must say what the list does AND what it does not (Zephyr's security read, ruled 11-05):
    it narrows the tools callable DIRECTLY; Bash is on the list, so it is not a capability boundary."""
    paras = [p for p in _split_spec(spec_text)[1].split("\n") if p.startswith("Your grant")]
    assert len(paras) == 1, f"expected one 'Your grant' paragraph, found {len(paras)}"
    s = paras[0]
    assert "enumerated `tools:` list" in s
    assert "narrows the tools you can call directly" in s, "the paragraph must say 'narrows', not 'cannot reach'"
    assert "does not bound what `Bash` can do" in s, "the Bash bound must travel with the claim"
    assert not re.search(r"cannot (reach|call)|can(not|'t) be reached|are not there", s), "an absolute reach claim: Bash is on the list"
    assert "no other MCP server" in s and "no umwelt world verb" in s
    # every mention of Agent is the statement that it is OFF the list ("and Agent too" would widen it)
    # count the WORD, not only the backticked form: a bare "and Agent too" slipped past a backtick-only count (mutant S3)
    assert len(re.findall(r"\bAgent\b", s)) == s.count("no `Agent`"), "the grant paragraph mentions Agent other than as 'no `Agent`'"
    # Measured in a scratch session (issue comment on 4323): with ToolSearch ON the list the ENGRAM tools stay
    # deferred (first request 50,064 tokens vs 53,466 under tools: *); WITHOUT ToolSearch every schema loads at
    # spawn (76,454, +22,988). So: say deferred and that ToolSearch must stay; never "loaded when you start".
    assert "deferred" in s and "ToolSearch must stay on the list" in s, "the paragraph must say why ToolSearch stays on the list"
    assert "loaded when you start" not in s, "false for a list that contains ToolSearch: the tools are deferred"


def test_agent_stays_off_the_list_because_it_closes_the_spawn_path(entries):
    """Agent (and its alias Task) must not be re-added casually: a sub-agent the master starts is another agent type with
    its own tools, outside this list, so listing it reopens everything the list closes (design owner, ruling 11-05)."""
    bad = [e for e in entries if e in ("Agent", "Task") or e.startswith(("Agent(", "Task("))]
    assert bad == [], (
        f"{bad} on the dream master's list. Agent closes the spawn path: a sub-agent it starts carries another agent type and "
        "its own tools, so the enumerated list would no longer bound what the master can call. The master does not dispatch "
        "fairies; the parent does. Do not re-add Agent without a recorded decision from the design owner."
    )


def test_the_body_no_longer_says_the_grant_is_star(spec_text):
    body = _split_spec(spec_text)[1]
    assert not re.search(r"grant is `\*`", body), "the body still restates the retired `*` grant"
    assert "all tools, lazy-loaded" not in body


# ------------------------------------------------------------------------------ built copies


def _run_build(args: list[str], out: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "tools.engine.cli", "build", "--allow-branch", "--allow-untagged", *args, "--output", out]
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)


@pytest.fixture(scope="module")
def claude_bundle(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("claude_dm")
    r = _run_build(["--target", "claude-code", "--tier", "convenience"], str(out))
    assert r.returncode == 0, f"claude-code build failed:\n{r.stdout}\n{r.stderr}"
    return out


@pytest.fixture(scope="module")
def codex_bundle(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("codex_dm")
    r = _run_build(["--target", "codex", "--tier", "convenience"], str(out))
    assert r.returncode == 0, f"codex build failed:\n{r.stdout}\n{r.stderr}"
    return out


def test_claude_built_copy_carries_the_same_list(claude_bundle, entries):
    built = claude_bundle / "agents" / "engram-dream-master.md"
    assert built.is_file(), f"no built dream-master spec at {built}"
    got = _tool_entries(_tools_line(_split_spec(built.read_text(encoding="utf-8"))[0]))
    assert got == entries, "the built (deployed-layout) copy differs from the source list"


def test_codex_toml_allowlists_exactly_the_engram_names(codex_bundle):
    path = codex_bundle / "agents" / "engram-dream-master.toml"
    assert path.is_file(), f"no codex dream-master TOML at {path}"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    policy = data["plugins"]["engram@engram-local"]["mcp_servers"]["engram"]["enabled_tools"]
    assert set(policy) == ALLOWED_ENGRAM, (
        f"codex allowlist differs: missing {sorted(ALLOWED_ENGRAM - set(policy))}, extra {sorted(set(policy) - ALLOWED_ENGRAM)}"
    )
    assert len(policy) == len(set(policy)), "duplicate names in the codex allowlist"
    assert "*" not in path.read_text(encoding="utf-8").split("developer_instructions", 1)[0]


# ------------------------------------------------------------------------------ recall-summary wording (design owner's ruling, 11-05)
# The tool is on the list by ruling and two seats' masters called it as late as 10-31, so a spec that says "must not"
# describes a rule practice does not keep. Ruled wording: the parent normally applies the batch in Step 7.5; the tool is on
# the list so a leftover payload can be applied; the master records in the dream record that it did.


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def test_the_spec_no_longer_forbids_calling_the_recall_summaries_tool(spec_text):
    body = _split_spec(spec_text)[1]
    # re.I: the spec writes "Do NOT call" and "You do NOT call"; a case-sensitive pattern let the exact old phrase through.
    bad = [s for s in _sentences(body) if re.search(r"\b(do not|never|must not)\s+call\s+`?engram_set_recall_summaries", s, re.IGNORECASE)]
    assert bad == [], f"the spec still forbids a tool its own list grants: {bad}"


def test_the_spec_says_the_tool_is_for_a_leftover_payload_and_to_record_it(spec_text):
    sents = [s for s in _sentences(_split_spec(spec_text)[1]) if "engram_set_recall_summaries" in s]
    ok = [s for s in sents if "leftover" in s and "dream record" in s]
    assert ok, f"no sentence says the tool is for a leftover payload and that applying one is recorded in the dream record; sentences naming the tool: {sents}"


def test_the_recall_summaries_wording_keeps_the_parent_step_and_the_payload_prohibition(spec_text):
    body = _split_spec(spec_text)[1]
    assert "Step 7.5" in body and "normally" in " ".join(s for s in _sentences(body) if "Step 7.5" in s and "recall summar" in s.lower())
    assert "Do NOT attempt to re-read `final_payload.json`" in body, "the unchanged prohibition on re-reading final_payload.json was lost"


# ------------------------------------------------------------------------------ the class guard (Zephyr, ruled 11-05)
# The property behind this PR is "no shipped agent spec carries a blanket tool grant without a recorded decision". Landing it
# with the instance (the dream master) keeps the next spec from reintroducing the class.

AGENTS_DIR = REPO_ROOT / "src" / "engram" / "agents"

# Recorded decisions: agent spec stem -> the reason it may carry a wildcard tools: grant. EMPTY today. An entry needs the design
# owner's decision (cite it), because a wildcard inherits every MCP server a seat has and every world verb of the parent.
RECORDED_STAR_DECISIONS: dict[str, str] = {}


def _agent_specs() -> list[Path]:
    return sorted(p for p in AGENTS_DIR.rglob("*.md") if p.name != "README.md")


def _tools_tokens(spec: Path) -> list[str] | None:
    parts = spec.read_text(encoding="utf-8").split("---", 2)
    if len(parts) < 3:
        return None
    m = re.search(r"^tools[ \t]*:[ \t]*(.*)$", parts[1], re.MULTILINE)
    if not m:
        return None
    raw = m.group(1).strip().strip("[]")
    return [tok.strip().strip("\"'") for tok in raw.split(",") if tok.strip()]


def test_no_shipped_agent_spec_carries_a_wildcard_tools_grant_without_a_recorded_decision():
    offenders = []
    for spec in _agent_specs():
        toks = _tools_tokens(spec)
        if toks and any("*" in tok for tok in toks) and spec.stem not in RECORDED_STAR_DECISIONS:
            offenders.append(f"{spec.relative_to(REPO_ROOT)}: tools: {', '.join(toks)}")
    assert offenders == [], (
        "an agent spec carries a wildcard tools grant with no recorded decision (it inherits every MCP server a seat has and the "
        f"parent's world verbs): {offenders}. Enumerate the tools, or record the decision in RECORDED_STAR_DECISIONS with its source."
    )


def test_the_class_guard_scans_the_shipped_specs_and_the_table_names_real_ones():
    """Non-vacuity: the scan must see the shipped roster, or an empty directory would pass it."""
    stems = {p.stem for p in _agent_specs()}
    assert {"engram-dream-master", "engram-dream-fairy", "engram-coder-fairy", "engram-pr-reviewer"} <= stems, f"scanned only {sorted(stems)}"
    with_tools = [p for p in _agent_specs() if _tools_tokens(p)]
    assert len(with_tools) >= 5, f"only {len(with_tools)} specs parsed a tools: line; the parser lost a form"
    assert set(RECORDED_STAR_DECISIONS) <= stems, "RECORDED_STAR_DECISIONS names a spec that does not exist"
