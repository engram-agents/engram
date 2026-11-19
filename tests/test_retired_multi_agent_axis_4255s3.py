"""Contract tests for #4255 slice 3: the CLAUDE.md template fold, the drift tool, the project
CLAUDE.md and the skills stop naming a topology mode.

Written BEFORE the implementation (contract-first). Contract: Ariadne's comment on #4255,
section "Slice 3", plus her routing note (the table is keyed on the ADDRESSEE, not the host) and
her tiers.json note (the reason string keeps the attribution to Lei's ruling).

  C1  template.CLAUDE.md holds an "Agent coordination" section and no retired text; the old
      template file is gone and no tracked file but the CHANGELOG still names it.
  C2  check-claude-md-drift.py accepts --multi-agent, ignores it, prints one retirement line;
      the folded section is checked in the default run.
  C3  config.json.example is valid JSON with none of the retired keys.
  C4  the project CLAUDE.md shrinks and holds none of the retired words.
  C5  the skills hold none of the retired words; engram-letter says what ia really does.
  C6  tiers.json has no entry for the deleted template; the reason string names Lei's ruling.
  C7  a fresh render through the real bootstrap path carries the section and no mode language.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "src" / "engram" / "templates"
TEMPLATE = TEMPLATES / "template.CLAUDE.md"
DRIFT_CLI = REPO_ROOT / "src" / "engram" / "tools" / "check-claude-md-drift.py"
SKILLS = REPO_ROOT / "src" / "engram" / "skills" / "claude"
TIERS_JSON = REPO_ROOT / "src" / "build" / "packaging" / "tiers.json"

# The name is assembled so that this file does not name the deleted template itself.
OLD_TEMPLATE = "template.CLAUDE." + "multi-agent.md"
RETIRED_WORDS = re.compile(r"multi[-_ ]agent|single[-_ ]agent", re.I)
PROJECT_CLAUDE_MD_BYTES_BEFORE = 74915

ELEVEN_SKILLS = [
    "engram-auto-reviewer-fairy-judgement", "engram-baton", "engram-collaborating-loop",
    "engram-fairy-orchestration", "engram-first-session", "engram-forum", "engram-letter",
    "engram-loop", "engram-sleep", "engram-standpoint", "engram-upgrade",
]


# ── C1: the fold ────────────────────────────────────────────────────────────────────────────
def test_template_holds_an_agent_coordination_section():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert len(re.findall(r"^#{2,3} Agent coordination\s*$", text, re.M)) == 1


def test_template_names_no_retired_topology_text():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not RETIRED_WORDS.search(text), RETIRED_WORDS.search(text).group(0)
    assert "same-host" not in text
    assert "only cross-host" not in text


def test_template_routing_table_is_keyed_on_the_addressee_and_keeps_comms_surface():
    text = TEMPLATE.read_text(encoding="utf-8")
    section = text.split("Agent coordination", 1)[1]
    assert "comms_surface" in section
    for needle in ("`ia`", "`baton`", "`forum`", "send_crossing", "Reciprocal PR review"):
        assert needle in section, needle
    assert "When other agents share this host" in section
    assert "(shared state under `/home/agents-shared/`) or a forum" in section


def test_old_template_file_is_deleted():
    assert not (TEMPLATES / OLD_TEMPLATE).exists()


def test_no_tracked_file_but_the_changelog_names_the_old_template():
    out = subprocess.run(["git", "grep", "-l", "-I", "-F", OLD_TEMPLATE], cwd=str(REPO_ROOT),
                         capture_output=True, text=True)
    hits = sorted(set(out.stdout.split()) - {"docs/CHANGELOG.md"})
    assert hits == [], hits


# ── C2: the drift tool ──────────────────────────────────────────────────────────────────────
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _fixture_repo(tmp_path, template_text):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.invalid")
    _git(repo, "config", "user.name", "T")
    d = repo / "src" / "engram" / "templates"
    d.mkdir(parents=True)
    (d / "template.CLAUDE.md").write_text(template_text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _drift(repo, live, *extra, flags=("--user-name", "U", "--agent-name", "A",
                                       "--self-node-id", "pn_0002", "--engram-home", "/h")):
    return subprocess.run(
        [sys.executable, str(DRIFT_CLI), "--repo", str(repo), "--head", "HEAD", "--live", str(live),
         *flags, *extra], capture_output=True, text=True, timeout=60)


FOLDED_TPL = ("# I'm {{AGENT_NAME}}.\n\nIntro for {{USER_NAME}}.\n\n"
              "## Agent coordination\n\nPick the channel by who you are addressing.\n\n"
              "- A hand-off to one agent: use the ia letter.\n")


def test_drift_flag_is_accepted_ignored_and_says_so_exactly_once(tmp_path):
    repo = _fixture_repo(tmp_path, FOLDED_TPL)
    live = tmp_path / "live.md"
    live.write_text(FOLDED_TPL.replace("{{AGENT_NAME}}", "A").replace("{{USER_NAME}}", "U"))
    plain = _drift(repo, live)
    flagged = _drift(repo, live, "--multi-agent")
    assert plain.returncode == 0, (plain.stdout, plain.stderr)
    assert flagged.returncode == 0, (flagged.stdout, flagged.stderr)
    assert flagged.stdout == plain.stdout
    lines = [l for l in flagged.stderr.splitlines() if "--multi-agent" in l]
    assert len(lines) == 1, flagged.stderr
    assert "retired" in lines[0] and "default" in lines[0] and "#4255" in lines[0]
    assert [l for l in plain.stderr.splitlines() if "retired" in l] == []


def test_drift_flag_given_twice_still_prints_one_line(tmp_path):
    repo = _fixture_repo(tmp_path, FOLDED_TPL)
    live = tmp_path / "live.md"
    live.write_text(FOLDED_TPL.replace("{{AGENT_NAME}}", "A").replace("{{USER_NAME}}", "U"))
    r = _drift(repo, live, "--multi-agent", "--multi-agent")
    assert r.returncode == 0, r.stderr
    assert len([l for l in r.stderr.splitlines() if "--multi-agent" in l]) == 1


def test_drift_flag_no_longer_needs_the_old_template_at_the_ref(tmp_path):
    # Before: the flag made a missing second template file exit 2. Now there is no such file.
    repo = _fixture_repo(tmp_path, FOLDED_TPL)
    live = tmp_path / "live.md"
    live.write_text(FOLDED_TPL.replace("{{AGENT_NAME}}", "A").replace("{{USER_NAME}}", "U"))
    assert _drift(repo, live, "--multi-agent").returncode == 0


def test_drift_default_run_reports_a_missing_folded_section(tmp_path):
    repo = _fixture_repo(tmp_path, FOLDED_TPL)
    live = tmp_path / "live.md"
    live.write_text("# I'm A.\n\nIntro for U.\n")
    r = _drift(repo, live)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Agent coordination" in r.stdout
    assert "Pick the channel by who you are addressing." in r.stdout


def test_drift_source_has_no_second_template_file():
    src = DRIFT_CLI.read_text(encoding="utf-8")
    assert "_MULTI_FILE" not in src
    assert OLD_TEMPLATE not in src
    help_ = subprocess.run([sys.executable, str(DRIFT_CLI), "--help"], capture_output=True, text=True)
    assert "--multi-agent" not in help_.stdout


def _seed_placeholders():
    """SEED_PLACEHOLDERS read in a child process: loading the tool in-process leaks sys.path."""
    code = ("import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "print(json.dumps(sorted(mod.SEED_PLACEHOLDERS)))\n")
    proc = subprocess.run([sys.executable, "-c", code, str(DRIFT_CLI)], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_drift_default_run_checks_the_real_templates_agent_coordination_section(tmp_path):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "Agent coordination" in text, "the fold has not happened"
    placeholders = _seed_placeholders()
    seeds = [f"{k[2:-2]}=pn_seed" for k in placeholders]
    flags = ["--user-name", "U", "--agent-name", "A", "--self-node-id", "pn_0002",
             "--engram-home", "/h"]
    for s in seeds:
        flags += ["--seed-id", s]
    repo = _fixture_repo(tmp_path, text)
    rendered = text
    for k in placeholders:
        rendered = rendered.replace(k, "pn_seed")
    rendered = (rendered.replace("{{USER_NAME}}", "U").replace("{{AGENT_NAME}}", "A")
                .replace("{{SELF_NODE_ID}}", "pn_0002").replace("{{ENGRAM_HOME}}", "/h"))
    live = tmp_path / "live.md"
    live.write_text(rendered, encoding="utf-8")
    ok = _drift(repo, live, flags=flags)
    assert ok.returncode == 0, (ok.stdout[-800:], ok.stderr)
    # Cut the section out of the seat's file: the default run must now report it.
    start = rendered.index("Agent coordination")
    start = rendered.rfind("\n#", 0, start) + 1
    nxt = re.search(r"^## ", rendered[start + 3:], re.M)
    cut = rendered[:start] + (rendered[start + 3 + nxt.start():] if nxt else "")
    live.write_text(cut, encoding="utf-8")
    bad = _drift(repo, live, flags=flags)
    assert bad.returncode == 1, (bad.stdout[-800:], bad.stderr)
    assert "MISSING" in bad.stdout and "Agent coordination" in bad.stdout


# ── C3: config.json.example ─────────────────────────────────────────────────────────────────
def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_config_example_is_valid_json_without_the_retired_keys():
    raw = (TEMPLATES / "config.json.example").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert not {"multi_agent", "mode", "counterparts"} & set(_keys(data))
    assert not RETIRED_WORDS.search(raw)
    assert data["install_tier"] == "convenience"
    comment = data["_comment_install"]
    assert "install_tier" in comment and "topology" not in comment.lower()


# ── C4: the project CLAUDE.md ───────────────────────────────────────────────────────────────
def test_project_claude_md_shrinks_and_names_no_retired_words():
    raw = (REPO_ROOT / "CLAUDE.md").read_bytes()
    assert len(raw) < PROJECT_CLAUDE_MD_BYTES_BEFORE, len(raw)
    m = RETIRED_WORDS.search(raw.decode("utf-8"))
    assert m is None, m and m.group(0)


def test_project_claude_md_moves_check_passes():
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "claude_md_moves_check.py")],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── C5: the skills ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ELEVEN_SKILLS)
def test_skill_names_no_retired_words(name):
    path = SKILLS / name / "SKILL.md"
    assert path.is_file()
    m = RETIRED_WORDS.search(path.read_text(encoding="utf-8"))
    assert m is None, m and m.group(0)


def test_no_shipped_skill_names_the_retired_words():
    bad = {}
    for p in sorted(SKILLS.glob("*/SKILL.md")):
        m = RETIRED_WORDS.search(p.read_text(encoding="utf-8"))
        if m:
            bad[p.parent.name] = m.group(0)
    assert bad == {}


def test_letter_skill_says_what_ia_does_without_a_forum_url():
    text = (SKILLS / "engram-letter" / "SKILL.md").read_text(encoding="utf-8")
    assert "single mode" not in text and "mode='multi'" not in text and "mode='single'" not in text
    assert re.search(r"exits? 3", text), "must name exit 3"
    assert "ia requires a configured forum" in text
    assert re.search(r"`ia status`[^\n]*exits? 0", text), "must name ia status exit 0"
    assert "forum.url" in text


def test_upgrade_skill_no_longer_documents_the_drift_flag():
    text = (SKILLS / "engram-upgrade" / "SKILL.md").read_text(encoding="utf-8")
    assert "--multi-agent" not in text


# ── C6: tiers.json ──────────────────────────────────────────────────────────────────────────
def test_tiers_json_has_no_entry_for_the_deleted_template():
    assert OLD_TEMPLATE not in TIERS_JSON.read_text(encoding="utf-8")


def test_agents_json_example_reason_keeps_the_ruling_and_drops_the_words():
    data = json.loads(TIERS_JSON.read_text(encoding="utf-8"))
    entry = next(e for e in data["excluded"] if e["path"] == "templates/agents.json.example")
    reason = entry["reason"]
    assert not RETIRED_WORDS.search(reason), reason
    assert "Lei's ruling" in reason and "2026-09-14" in reason and "#2857" in reason
    assert "exclude from public release" in reason


# ── C7: a fresh render through the real bootstrap path ──────────────────────────────────────
def test_bootstrap_renders_the_section_and_no_mode_language(tmp_path):
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from test_bootstrap_codex import _run_bootstrap
    result = _run_bootstrap(tmp_path, "claude")
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(re.findall(r"^#{2,3} Agent coordination\s*$", rendered, re.M)) == 1
    assert "comms_surface" in rendered
    assert not RETIRED_WORDS.search(rendered)
    assert "same-host" not in rendered and "only cross-host" not in rendered


# ── C8: the README config table rows for the retired keys (hand-off from #4266 / #4262) ──
@pytest.mark.parametrize("key", ["mode", "counterparts"])
def test_readme_config_table_rows_for_the_retired_keys_say_retired_and_ignored(key):
    text = (REPO_ROOT / "README-AGENT.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith(f"| `{key}` |")]
    assert len(rows) == 1, rows
    row = rows[0].lower()
    assert "retired" in row and "ignored" in row, rows[0]
    assert "managed by" not in row and "set by `agentctl`" not in row, rows[0]


# ── C9: folds from reviewer round 1 (each pins a sentence a mutant of the reviewer's survived) ──
HOST_KEYED = re.compile(
    r"on this host|on another host|another host|other host|same[- ]host|cross[- ]host|off[- ]host|on[- ]host",
    re.I,
)


def _routing_bullets():
    section = TEMPLATE.read_text(encoding="utf-8").split("## Agent coordination", 1)[1]
    table = section.split("Reciprocal PR review", 1)[0]
    return [ln for ln in table.splitlines() if ln.startswith("- ")]


def test_routing_bullets_are_keyed_on_who_you_address_and_never_on_a_host():
    bullets = _routing_bullets()
    assert len(bullets) >= 6, bullets
    for ln in bullets:
        assert not HOST_KEYED.search(ln), ln
    joined = " ".join(bullets)
    for keyed_on in ("to one agent", "whose-turn", "whole community", "Community-wide"):
        assert keyed_on in joined, keyed_on


def test_update_flow_names_the_one_template_file_that_carries_the_section():
    src = (REPO_ROOT / "src" / "build" / "engine" / "flows.py").read_text(encoding="utf-8")
    m = re.search(r'ctx\["_template_changed"\]\s*=\s*any\((.*?)\n\s*\)', src, re.S)
    assert m, "the _template_changed assignment moved"
    named = re.findall(r'"([^"]+)"', m.group(1))
    assert named == ["src/engram/templates/template.CLAUDE.md"], named
    assert (REPO_ROOT / named[0]).is_file()


def test_forum_skill_says_only_the_forum_cli_falls_back_to_the_env_url():
    text = (SKILLS / "engram-forum" / "SKILL.md").read_text(encoding="utf-8")
    assert "The `forum` CLI falls back to `$FORUM_URL`, then `http://localhost:5002`" in text
    assert not re.search(r"`(ia|baton)`[^\n]*fall(s)? back", text, re.I)


def test_baton_skill_says_which_verbs_need_a_forum_url():
    text = (SKILLS / "engram-baton" / "SKILL.md").read_text(encoding="utf-8")
    assert "the `baton` write verbs exit 3" in text
    assert "`baton status`, `show` and `mine` exit 0" in text


# ── C9b: the two facts Luria's mutants on #4269 left green (folded in slice 7) ──────────────────────────
def test_letter_skill_pins_the_exact_no_forum_url_sentence():
    text = (SKILLS / "engram-letter" / "SKILL.md").read_text(encoding="utf-8")
    assert ('Every `ia` verb except `ia status` exits 3 with "ia requires a configured forum"; '
            '`ia status` still exits 0') in text


def test_agents_json_example_reason_names_who_relayed_the_ruling():
    data = json.loads(TIERS_JSON.read_text(encoding="utf-8"))
    entry = next(e for e in data["excluded"] if e["path"] == "templates/agents.json.example")
    assert "relayed by Kepler, 2026-09-14" in entry["reason"]
