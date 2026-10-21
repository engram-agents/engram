"""The prose sweep: 23 shipped files name ENGRAM_HOME on every home line [part of #3863].

The project owner's ruling (2026-10-17): the deployment uses ENGRAM_HOME wherever possible,
because the install allows a custom engram folder. The design rule (R1): prose
counts and is fixed, with no prose exemption, because agents run skill and
template prose as commands and a gate cannot tell an instruction from a
description. So every shipped line that spells the default home also names
ENGRAM_HOME:

  * in a command (a fenced block), the canonical `${ENGRAM_HOME:-$HOME/.engram}`;
  * in a description (prose, comments, docstrings, html text, README), the
    spelling stays as the human default and the SAME line gains
    `$ENGRAM_HOME` (default `~/.engram`).

CONTRACT (each clause is a test below; the tests were written before the sweep):
  C1  same-line: in each of the 23 files, every line the gate's SPELLINGS match
      also holds the token ENGRAM_HOME.
  C2  command: in a fenced block a home spelling occurs only inside the
      canonical expression, and every ENGRAM_HOME token is inside it.
  C4  meaning: nothing else changes. Against the base commit the file has the
      same number of lines, and the changed lines are exactly the lines C1
      required, each still holding its spelling and now holding ENGRAM_HOME.
  C5  baseline: none of the 23 is in tools/home_path_baseline.json any more, and
      the 23 are exactly the baselined files at the base commit minus the files
      deliberately left to later PRs.
  C6  no behaviour change: the 3 html files keep their tags, attributes and script
      code. The sweep holds NO Python file: the code docstring/comment files are
      swept by the shell-and-docstrings PR (#3887, "PR 3b"), so there is no
      bytecode check here (a check over an empty list would pass vacuously).

The spelling table is IMPORTED from tools/home_path_gate.py, so this test and the
gate cannot drift. The base-commit comparisons (C4, C5 list, C6) are snapshot
checks pinned to the commit this sweep was cut from. They retire by CONTENT, never by
history shape (#3925): they run only when BASE and the sweep commit are both available
and the file under check still equals the file at the sweep commit; anything else is a
skip. A commit count or an ancestry test reads wrong on a partial-history checkout,
where BASE is present but the later commits are not.

RETIRED PERMANENTLY (#3925, round 3): four tests are skipped unconditionally with
RETIRED_REASON: the three scope guards (test_scope_only_the_23_..., test_disjoint_from_pr_3a_...,
test_disjoint_from_pr_3b_...) and test_c5_baseline_diff_from_dev_is_exactly_our_23. They
asserted a FROZEN fact (the sweep commit's own diff) against this file's MUTABLE constants
(FILES, COVERED_BY_PR_3A, COVERED_BY_PR_3B), so any later PR that deleted or renamed a
swept file would have turned them red, in the full suite only. Their job, stopping two PRs
from editing the same files while the sweep was in flight, ended when the sweep merged as
caebaf3c. They keep their names so test-id accounting stays clean. Delete all four, and
RETIRED_REASON with them, when the #3863 epic closes.

Every git command below whose failure can mean "the evidence is missing" (a commit or a
blob absent from a partial or blobless checkout) skips; none parses or asserts on it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = "src/engram/"
BASE = "0d5bbeba0b2fde99a6db4e3dbf1a1fb1fb8edf3f"
# The sweep commit (PR #3888), a descendant of BASE. A snapshot check runs only on
# content that is still the sweep's own; see retire_reason().
SWEEP = "caebaf3c6f237b9e727f648f41988969268efdab"
# Why the four frozen-fact tests are skipped, unconditionally (see the module docstring).
RETIRED_REASON = f"sweep merged as {SWEEP[:8]}; concurrent-edit guard no longer applies"
BASELINE_REL = "tools/home_path_baseline.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load("home_path_gate_for_prose_sweep", REPO / "tools" / "home_path_gate.py")
PRECEDENT = _load(
    "skill_home_paths_for_prose_sweep", REPO / "tests" / "test_skill_home_paths_3863.py"
)
SPELLINGS = GATE.SPELLINGS
CANONICAL = PRECEDENT._CANONICAL  # ${ENGRAM_HOME:-$HOME/.engram}
FENCE = PRECEDENT._FENCE
TOKEN = "ENGRAM_HOME"

_SKILLS = (
    "collaborating-loop curiosity-loop deep-research fairy-orchestration forum "
    "learn-from-error letter loop-diagnose loop meta-loop nap observe school-day"
).split()
SKILL_FILES = [f"skills/claude/engram-{n}/SKILL.md" for n in _SKILLS]
TEMPLATE_AGENT_HTML_FILES = [
    "agents/claude/engram-dream-fairy.md",
    "agents/claude/engram-person-node-fairy.md",
    "templates/compact-instructions.md",
    "templates/components/model-identity-gate.md",
    "templates/template.CLAUDE.multi-agent.md",
    "templates/template.warm-briefing.md",
    "viz/templates/config.html",
    "viz/templates/health_workbench.html",
    "viz/templates/stats.html",
]
README_FILES = ["tools/engram-pkg/README.md"]
FILES = SKILL_FILES + TEMPLATE_AGENT_HTML_FILES + README_FILES
HTML_FILES = [f for f in FILES if f.endswith(".html")]
TWIN = "templates/template.CLAUDE.md"

# Baselined at the base commit and deliberately NOT in this PR: viz/server.py (a
# later PR: the seat-home registry), every code-bearing tool (a later PR: the
# tools sweep with the shared resolver), and launch-engram-server.sh.
LEFT_TO_LATER_PRS = {
    "launch-engram-server.sh",
    "tools/backup_claude_projects.py",
    "tools/backup_session_logs.py",
    "tools/compute_task_time.py",
    "tools/dream_lottery.py",
    "tools/forum-mention-monitor.sh",
    "tools/forum-updates-monitor.sh",
    "tools/ghpr.sh",
    "tools/grant_viz_operator_acl.py",
    "tools/loop_prompt.py",
    "tools/migrate_person_nodes.py",
    "tools/migrate_prediction_pairing_edges.py",
    "tools/migrate_stranded_supported_by.py",
    "tools/monitor-supervisor.py",
    "tools/run-all-tests.sh",
    "tools/tripwire.py",
    "viz/server.py",
}


# Baselined at the base commit and swept by the tools-prose PR (#3883, "PR 3a"), which
# also edits them. This PR must stay disjoint from it: a file in both PRs is a content
# conflict at merge. They are named so a later edit cannot re-introduce the collision.
COVERED_BY_PR_3A = {
    "tools/config_schema.py",
    "tools/enumerate_armed_monitors.py",
    "tools/fairy_report.py",
}


# Baselined at dev and swept by the shell-and-docstrings PR (#3887, "PR 3b"): the four
# code files whose docstrings and comments carry the home spelling. Same rule as above.
COVERED_BY_PR_3B = {
    "engram_lifecycle.py",
    "engram_tasks.py",
    "health/engine.py",
    "hooks/claude/context_tracker_hook.py",
}


def _text(rel):
    return (REPO / SRC / rel).read_text(encoding="utf-8")


def _git(*args, repo=None):
    return subprocess.run(
        ["git", *args], cwd=str(repo if repo is not None else REPO), capture_output=True, text=True
    )


def _has_commit(repo, sha):
    return _git("cat-file", "-e", f"{sha}^{{commit}}", repo=repo).returncode == 0


def retire_reason(paths, repo=REPO, base=BASE, sweep=SWEEP):
    """Why a snapshot check must retire (skip), or None when it must run.

    It runs only if BASE and the sweep commit are both available in `repo` and every
    path in `paths` (repo-relative) has, in the working tree, the same bytes as at the
    sweep commit. It reads contents only: no commit count, no ancestry, no ref name.

    It compares raw bytes and assumes LF checkouts: on a CRLF checkout (core.autocrlf)
    every snapshot check retires, which is a safe skip and never a false failure.
    """
    if not _has_commit(repo, base):
        return f"base commit {base[:8]} not available"
    if not _has_commit(repo, sweep):
        return f"sweep commit {sweep[:8]} not available (partial history)"
    for p in paths:
        at_sweep = subprocess.run(
            ["git", "show", f"{sweep}:{p}"], cwd=str(repo), capture_output=True
        )
        f = Path(repo) / p
        if at_sweep.returncode != 0 or not f.is_file() or f.read_bytes() != at_sweep.stdout:
            return f"{p} differs from the sweep commit; snapshot check retired"
        # The sweep is a squash several commits above BASE, so the snapshot "BASE plus the
        # sweep's lines" holds only for a file no other commit edited between them: BASE's
        # file must equal the file just before the sweep commit.
        at_base = subprocess.run(["git", "show", f"{base}:{p}"], cwd=str(repo), capture_output=True)
        before = subprocess.run(["git", "show", f"{sweep}^:{p}"], cwd=str(repo), capture_output=True)
        if at_base.returncode != 0 or before.returncode != 0 or at_base.stdout != before.stdout:
            return f"{p} was edited between the base and the sweep commit (or its parent is unavailable); snapshot check retired"
    return None


def _base_text(rel, repo=REPO, base=BASE):
    r = _git("show", f"{base}:{SRC}{rel}", repo=repo)
    if r.returncode != 0:
        pytest.skip(f"base commit {base[:8]} or its file not available ({r.stderr.strip()[:80]})")
    return r.stdout


def _base_baseline(repo=REPO, base=BASE):
    """The baseline entries at the base commit; skips when that blob is not available
    (the commit can be present while its blob is not, in a blobless clone)."""
    r = _git("show", f"{base}:{BASELINE_REL}", repo=repo)
    if r.returncode != 0:
        pytest.skip(f"baseline at base commit {base[:8]} not available ({r.stderr.strip()[:80]})")
    return set(json.loads(r.stdout)["baseline"])


def _fresh_snapshot(rel, repo=REPO, base=BASE, sweep=SWEEP):
    """Skip unless this file is still the sweep's own (retire_reason is None)."""
    reason = retire_reason([f"{SRC}{rel}"], repo=repo, base=base, sweep=sweep)
    if reason:
        pytest.skip(reason)


# Borges' ruling on #3863: the venv lives at ~/.engram/venv REGARDLESS of ENGRAM_HOME, so the
# HOME-relative venv spelling is the rule, not a leftover; it needs no ENGRAM_HOME on its line.
_VENV_HOME = re.compile(r"(?:~|\$HOME)/\.engram/venv(?![\w-])")


def has_spelling(line):
    return any(rx.search(_VENV_HOME.sub("", line)) for _, rx in SPELLINGS)


# --- the detectors (planted controls below pin them) ---------------------------


def same_line_violations(text):
    """[(lineno, line)] of lines with a home spelling and no ENGRAM_HOME (C1)."""
    return [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), 1)
        if has_spelling(line) and TOKEN not in line
    ]


def fenced_lines(text):
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield n, line


_WORD_START_BEFORE = {"", " ", "\t", "=", "(", ";", "|", "&", "<", ">"}
_TOKEN_END_AFTER = {"", " ", "\t", ")", "}", ";", "|", "&", "<", ">", "\\", "`", "/", ":", ","}


def quote_state_at(line, idx):
    """'single' | 'double' | None: the shell quoting state of line[idx], scanning
    from the start of the line (a backslash escapes outside single quotes)."""
    state, i = None, 0
    while i < idx:
        c = line[i]
        if state == "single":
            if c == "'":
                state = None
        elif c == "\\":
            i += 1
        elif state == "double":
            if c == '"':
                state = None
        elif c == "'":
            state = "single"
        elif c == '"':
            state = "double"
        i += 1
    return state


def canonical_context_problems(line):
    """[reason] for every occurrence of the canonical expression in `line` that is
    not in a context where it expands to the intended path: inside single quotes;
    glued to a word on the left (outside double quotes); or followed by something
    other than `/`, a closing quote then `/`, or the end of the token."""
    out = []
    for m in re.finditer(re.escape(CANONICAL), line):
        state = quote_state_at(line, m.start())
        if state == "single":
            out.append("inside single quotes: it never expands")
        if state is None:
            before = line[m.start() - 1] if m.start() else ""
            if before not in _WORD_START_BEFORE and not line[: m.start()].endswith(":-"):
                out.append(f"glued to {before!r} on its left, outside double quotes")
        j = m.end()
        after = line[j : j + 1]
        if after == '"':
            after = line[j + 1 : j + 2]
            if after == "'":  # `'"${..}"'/x'`: break out of single quotes, then reopen
                after = line[j + 2 : j + 3]
        if after not in _TOKEN_END_AFTER:
            out.append(f"followed by {after!r}: a join needs `/` or the end of the token")
    return out


def fenced_violations(text):
    """[(lineno, line)] of fenced lines where a home spelling or an ENGRAM_HOME
    token sits outside the canonical expression, or the canonical expression sits
    where it does not expand (C2). There is no exception: a marker JSON written with
    a single-quoted printf breaks out of the quotes around the expression."""
    out = []
    for n, line in fenced_lines(text):
        rest = line.replace(CANONICAL, "@@CANON@@")
        if has_spelling(rest) or TOKEN in rest or canonical_context_problems(line):
            out.append((n, line.strip()))
    return out


_SPAN = PRECEDENT._SPAN
_DESCRIPTION_FORMS = {"$ENGRAM_HOME", "ENGRAM_HOME"}


def prose_span_violations(text):
    """[(lineno, span)] of inline code spans (outside fences) that name ENGRAM_HOME
    in a form that is neither the description form (`$ENGRAM_HOME`, `ENGRAM_HOME`)
    nor built from the canonical expression in a context where it expands."""
    out = []
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _SPAN.finditer(line):
            span = m.group(1)
            if TOKEN not in span or span.strip() in _DESCRIPTION_FORMS:
                continue
            rest = span.replace(CANONICAL, "@@CANON@@")
            if TOKEN in rest or canonical_context_problems(span):
                out.append((n, span))
    return out


# --- C1, C2 over the 23 files --------------------------------------------------


@pytest.mark.parametrize("rel", FILES)
def test_c1_every_home_spelling_line_names_engram_home(rel):
    bad = same_line_violations(_text(rel))
    assert not bad, (
        f"{rel}: {len(bad)} line(s) spell the default home without naming ENGRAM_HOME (#3863):\n"
        + "\n".join(f"  L{n}: {c[:140]}" for n, c in bad)
    )


@pytest.mark.parametrize("rel", FILES)
def test_c2_fenced_blocks_use_only_the_canonical_expression(rel):
    bad = fenced_violations(_text(rel))
    assert not bad, (
        f"{rel}: {len(bad)} fenced line(s) outside the canonical {CANONICAL}, or where it "
        f"does not expand (#3863):\n"
        + "\n".join(f"  L{n}: {c[:140]}" for n, c in bad)
    )


@pytest.mark.parametrize("rel", FILES + [TWIN])
def test_c3_prose_spans_name_engram_home_only_in_a_form_that_works(rel):
    # No per-file retirement: unlike C4 this reads the file as it is, so a later edit
    # that breaks a span is caught however many commits touched the file since.
    text = _text(rel)
    if rel == TWIN:
        # Only the one line this PR edited: the rest of that file is text we did not write.
        text = "\n".join(ln for ln in text.splitlines() if TWIN_MENTION in ln)
        assert text, "the twin's edited line is missing"
    bad = prose_span_violations(text)
    assert not bad, (
        f"{rel}: {len(bad)} inline code span(s) with a malformed ENGRAM_HOME form (#3863):\n"
        + "\n".join(f"  L{n}: `{s[:120]}`" for n, s in bad)
    )


# Lines that direct an agent to use the Read/Write/Edit TOOL on a home path. Those tools
# expand a leading `~` but NOT a shell expression, so `${ENGRAM_HOME:-...}` there returns
# "file does not exist". Such a line keeps the tilde path and names ENGRAM_HOME on the same
# line (the description form). Lines directing Bash keep the canonical expression. The
# lines are found by a marker that must occur exactly once in the file.
TOOL_PATH_LINES = [
    ("agents/claude/engram-dream-fairy.md", "YYYY-MM-DD-fairy-report.md"),
    ("agents/claude/engram-person-node-fairy.md", "YYYY-MM-DD-person-fairy-report.md"),
    ("agents/claude/engram-person-node-fairy.md", "person-fairy-known.json"),
    ("templates/compact-instructions.md", "warm-briefing.md"),
    ("templates/compact-instructions.md", "The newest file in"),
]


def tool_path_line_problems(line):
    """[reason] when a Read/Write/Edit-tool path line is not tilde + ENGRAM_HOME mention."""
    out = []
    if "~/.engram" not in line:
        out.append("no tilde path (`~/.engram/...`)")
    if TOKEN not in line:
        out.append("no same-line ENGRAM_HOME mention")
    if "${" in line:
        out.append("carries a shell expression the Read/Write/Edit tools do not expand")
    return out


@pytest.mark.parametrize("rel,marker", TOOL_PATH_LINES)
def test_read_write_tool_path_lines_keep_the_tilde_form_with_a_mention(rel, marker):
    lines = [ln for ln in _text(rel).splitlines() if marker in ln]
    assert len(lines) == 1, f"{rel}: marker {marker!r} found on {len(lines)} lines"
    assert not tool_path_line_problems(lines[0]), (
        f"{rel}: {tool_path_line_problems(lines[0])} -- {lines[0].strip()[:140]}"
    )


# --- run the marker-writing blocks (the quoting is what decides the written path) ----

# A fenced block that writes a marker is only right if the shell EXPANDS the home in it.
# So these tests extract the block from the SKILL.md itself and run it under a temp HOME
# and cwd, once with ENGRAM_HOME unset (the default home) and once with ENGRAM_HOME set
# to a path holding a space, and check what was written and where.

LOOP_SKILL = "skills/claude/engram-loop/SKILL.md"
SCHOOL_SKILL = "skills/claude/engram-school-day/SKILL.md"
META_SKILL = "skills/claude/engram-meta-loop/SKILL.md"


def fenced_blocks(text):
    """[(first_lineno, dedented text)] of every fenced block."""
    out, cur, start = [], None, 0
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            if cur is None:
                cur, start = [], n + 1
            else:
                out.append((start, textwrap.dedent("\n".join(cur)) + "\n"))
                cur = None
        elif cur is not None:
            cur.append(line)
    return out


def block_where(rel, predicate):
    found = [b for _, b in fenced_blocks(_text(rel)) if predicate(b)]
    assert len(found) == 1, f"{rel}: {len(found)} blocks match"
    return found[0]


MARKER_BLOCKS = {
    "school-day printf JSON": (SCHOOL_SKILL, lambda b: b.startswith("printf '%s\\n' '{\"FIRST_ACTION\"")),
    "loop heredoc JSON": (LOOP_SKILL, lambda b: "/loop-mode.json <<JSON" in b),
}
EDIT_BLOCKS = {  # edit an existing marker with jq
    "loop jq cadence edit": (LOOP_SKILL, lambda b: "cadence_seconds = $c" in b),
    "loop jq state edit": (LOOP_SKILL, lambda b: "jq --arg s" in b),
}
SCRATCH_BLOCK = (META_SKILL, lambda b: "<< 'SCRATCH'" in b)
# Blocks that need systemd/Codex to run; they are only syntax-checked here.
SYNTAX_ONLY_BLOCKS = {
    "loop codex bridge script": (LOOP_SKILL, lambda b: "<<'BASH'" in b),
    "loop codex status check": (LOOP_SKILL, lambda b: b.startswith("WAKE=") and 'list-timers "$unit"' in b),
}
SEED_MARKER = (
    'mkdir -p "${ENGRAM_HOME:-$HOME/.engram}" && '
    "echo '{\"cadence_seconds\":1800,\"state\":\"old\"}' > \"${ENGRAM_HOME:-$HOME/.engram}\"/loop-mode.json\n"
)


def run_block(script, tmp_path, mode, extra_env=None):
    """Run `script` with bash under a temp HOME/cwd. -> (CompletedProcess, home Path)."""
    user_home = tmp_path / "user"
    cwd = tmp_path / "cwd"
    user_home.mkdir()
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT")}
    env["HOME"] = str(user_home)
    env.update(extra_env or {})
    if mode == "default":
        home = user_home / ".engram"
    else:
        home = tmp_path / "my home" / "eg"
        env["ENGRAM_HOME"] = str(home)
    home.mkdir(parents=True)
    r = subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60)
    # nothing may be written outside the home (a literal `${...}` directory, a stray tmp)
    assert os.listdir(cwd) == [], f"stray files in cwd: {os.listdir(cwd)}"
    assert sorted(os.listdir(user_home)) == ([".engram"] if mode == "default" else []), os.listdir(user_home)
    return r, home


MODES = ["default", "custom-with-space"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(MARKER_BLOCKS))
def test_marker_block_writes_valid_json_with_the_real_absolute_path(tmp_path, name, mode):
    rel, pred = MARKER_BLOCKS[name]
    r, home = run_block(block_where(rel, pred), tmp_path, mode)
    assert r.returncode == 0, r.stderr
    marker = home / "loop-mode.json"
    assert marker.is_file(), f"{name}: nothing written to {marker}"
    data = json.loads(marker.read_text(encoding="utf-8"))
    leaked = {k: v[:90] for k, v in data.items() if isinstance(v, str) and "${" in v}
    assert not leaked, f"{name}: a string field holds an unexpanded shell expression: {leaked}"
    if name.startswith("school"):
        assert f"Read {home}/loop-mode.json for" in data["loop_prompt"], data["loop_prompt"][:200]
    if name.startswith("school"):
        assert f"{home}/marketplace/plugins/engram/tools/loop_prompt.py" in data["FIRST_ACTION"]
    else:
        # after #4007 the loop heredoc arms directly at cadence_seconds; no helper path in FIRST_ACTION
        assert "cadence_seconds" in data["FIRST_ACTION"], data["FIRST_ACTION"][:200]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(EDIT_BLOCKS))
def test_marker_edit_blocks_work_in_the_chosen_home(tmp_path, name, mode):
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    rel, pred = EDIT_BLOCKS[name]
    r, home = run_block(SEED_MARKER + block_where(rel, pred), tmp_path, mode)
    assert r.returncode == 0, r.stderr
    data = json.loads((home / "loop-mode.json").read_text())
    assert data["cadence_seconds"] == 3300 or data["state"] == "<new state>", data
    assert sorted(os.listdir(home)) == ["loop-mode.json"], os.listdir(home)  # tmp file was moved


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(EDIT_BLOCKS))
def test_marker_edit_blocks_path_handling_with_a_passthrough_jq(tmp_path, name, mode):
    """Path handling only (mktemp, redirect, mv), for a machine without jq: `jq` is a
    shell function that copies its last argument (the marker file) to stdout."""
    rel, pred = EDIT_BLOCKS[name]
    stub = 'jq() { local f; for f in "$@"; do :; done; cat "$f"; }\n'
    r, home = run_block(stub + SEED_MARKER + block_where(rel, pred), tmp_path, mode)
    assert r.returncode == 0, r.stderr
    assert json.loads((home / "loop-mode.json").read_text())["state"] == "old"
    assert sorted(os.listdir(home)) == ["loop-mode.json"], os.listdir(home)  # tmp file was moved


@pytest.mark.parametrize("mode", MODES)
def test_scratch_log_block_writes_into_the_chosen_home(tmp_path, mode):
    rel, pred = SCRATCH_BLOCK
    r, home = run_block(block_where(rel, pred), tmp_path, mode)
    assert r.returncode == 0, r.stderr
    text = (home / "loop-scratch.md").read_text(encoding="utf-8")
    assert "$(date" not in text and text.startswith("# Meta-Loop Scratch Log")


# --- portability: GNU-only `sed -i` / `date -I` in shipped commands (#3889) -----------
# macOS ships BSD userland: `sed -i` there takes a mandatory suffix argument, so the GNU
# form `sed -i "s/a/b/" file` fails with "invalid command code", and `date -I` is not an
# option at all. A shipped command must work on both, so it writes a temp file in the
# same directory and `mv`s it over (or passes an attached suffix, `-i.bak`).

_SED_INPLACE_BARE = re.compile(
    r"""\bsed\b[^|;&\n]*?\s(?:-[A-Za-z]*i(?=\s|$)|--in-place(?=\s|$))"""
)


def gnu_only_sed_inplace(text):
    """[(lineno, line)] of FENCED lines with `sed -i` / `--in-place` and no attached
    suffix (`-i.bak`): GNU-only, and `-i ''` breaks GNU the other way round.

    BOUNDS, so a pass is not read as more than it is. The regex is line-oriented and
    does not flag: a `sed -i` split across a backslash continuation (`sed \\` newline
    `-i`), a `-i` that follows `;`, `|` or `&` on the same line after the word `sed`
    (the scan stops at those separators), the attached-empty-suffix forms `-i''` and
    `-i""`, or a `-i` inside a quoted script (`sed -e 's/ -i /x/'` is even flagged
    wrongly, a false positive on quoted text). It scans only `skills`, `agents` and
    `templates` under src/engram/ (SHIPPED_PROSE_ROOTS); the shell text in hooks and
    tools is out of scope. So a clean scan does NOT mean no GNU-only `sed -i` is
    shipped."""
    return [(n, ln.strip()) for n, ln in fenced_lines(text) if _SED_INPLACE_BARE.search(ln)]


SHIPPED_PROSE_ROOTS = ("skills", "agents", "templates")


def _shipped_md_files():
    base = REPO / SRC
    return sorted(p for root in SHIPPED_PROSE_ROOTS for p in (base / root).rglob("*.md"))


def test_no_shipped_fenced_command_uses_gnu_only_sed_in_place():
    bad = []
    for path in _shipped_md_files():
        rel = path.relative_to(REPO)
        bad += [f"{rel}:{n}: {ln[:110]}" for n, ln in gnu_only_sed_inplace(path.read_text(encoding="utf-8"))]
    assert not bad, (
        "GNU-only `sed -i` (fails on macOS BSD sed); write a temp file in the same "
        "directory and `mv` it over (#3889):\n  " + "\n  ".join(bad)
    )


class TestSedInPlacePlanted:
    @pytest.mark.parametrize(
        "line",
        [
            'sed -i "s/a/b/" f.txt',
            "sed -i 's/a/b/' f.txt",
            "sed -ni 's/a/b/p' f.txt",
            "sed -i -e 's/a/b/' f.txt",
            "sed -i '' 's/a/b/' f.txt",          # BSD-only; breaks GNU
            "sed --in-place 's/a/b/' f.txt",
            'cd d && sed -i "s/a/b/" f.txt',
        ],
    )
    def test_gnu_only_forms_are_flagged(self, line):
        assert gnu_only_sed_inplace(f"```bash\n{line}\n```\n")

    @pytest.mark.parametrize(
        "line",
        [
            'sed -i.bak "s/a/b/" f.txt',
            "sed --in-place=.bak 's/a/b/' f.txt",
            'sed "s/a/b/" f.txt > "$tmp" && mv "$tmp" f.txt',
            "grep -i foo f.txt | sed 's/a/b/'",
            "sed -n '/x/p' f.txt",
        ],
    )
    def test_portable_forms_pass(self, line):
        assert gnu_only_sed_inplace(f"```bash\n{line}\n```\n") == []

    def test_prose_is_not_scanned(self):
        assert gnu_only_sed_inplace("run `sed -i s/a/b/ f` to edit\n") == []


BSD_SED = """#!/bin/sh
# BSD sed stand-in: -i takes a mandatory suffix argument. A bare -i swallows the next
# argument as the suffix, so the GNU form fails the way macOS does.
for a in "$@"; do
  case "$a" in
    -i|--in-place) echo "sed: 1: \\"$a\\": invalid command code ." >&2; exit 1 ;;
  esac
done
exec "$REAL_SED" "$@"
"""
BSD_DATE = """#!/bin/sh
# BSD date stand-in: no -I (ISO) option.
for a in "$@"; do
  case "$a" in
    -I*) echo "date: illegal option -- I" >&2; exit 1 ;;
  esac
done
exec "$REAL_DATE" "$@"
"""


def _bsd_userland(tmp_path):
    """-> (extra env, bin dir) putting BSD-style sed/date stand-ins first on PATH."""
    bindir = tmp_path / "bsd-bin"
    bindir.mkdir()
    for name, body in (("sed", BSD_SED), ("date", BSD_DATE)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    extra = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "REAL_SED": shutil.which("sed"),
        "REAL_DATE": shutil.which("date"),
    }
    return extra, bindir


@pytest.mark.parametrize("mode", MODES)
def test_scratch_log_block_works_on_a_bsd_style_userland(tmp_path, mode):
    """The date step must work where `sed -i` needs a suffix and `date` has no -I."""
    import datetime

    extra, _ = _bsd_userland(tmp_path)
    rel, pred = SCRATCH_BLOCK
    r, home = run_block(block_where(rel, pred), tmp_path, mode, extra_env=extra)
    assert r.returncode == 0, r.stderr
    text = (home / "loop-scratch.md").read_text(encoding="utf-8")
    today = {datetime.date.today().isoformat(), (datetime.date.today() - datetime.timedelta(days=1)).isoformat()}
    first = text.splitlines()[0]
    assert first.startswith("# Meta-Loop Scratch Log — ") and first.split("— ")[1] in today, first
    assert "$(date" not in text
    assert sorted(os.listdir(home)) == ["loop-scratch.md"], os.listdir(home)  # temp file was moved


def _failing_tool_env(tmp_path, tool):
    """PATH with a stub `tool` first that exits non-zero (sed: 3, mv: 5)."""
    bindir = tmp_path / "fail-bin"
    bindir.mkdir()
    stub = bindir / tool
    stub.write_text(f"#!/bin/sh\nexit {3 if tool == 'sed' else 5}\n")
    stub.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}


def _scratch_heredoc_body(block):
    """The log text the block's own `cat > ... << 'SCRATCH'` writes."""
    lines = block.splitlines()
    start = next(i for i, ln in enumerate(lines) if "<< 'SCRATCH'" in ln) + 1
    end = next(i for i in range(start, len(lines)) if lines[i] == "SCRATCH")
    return "\n".join(lines[start:end]) + "\n"


@pytest.mark.parametrize("tool", ["sed", "mv"])
@pytest.mark.parametrize("mode", MODES)
def test_scratch_log_block_leaves_no_temp_file_when_a_step_fails(tmp_path, tool, mode):
    """If the date substitution fails (sed) or the move does (mv), the exit status stays
    non-zero, no `loop-scratch.md.*` temp file is left in the home (a home with a space
    in it when mode is custom-with-space), and the log the block just wrote is untouched."""
    rel, pred = SCRATCH_BLOCK
    block = block_where(rel, pred)
    r, home = run_block(block, tmp_path, mode, extra_env=_failing_tool_env(tmp_path, tool))
    assert r.returncode != 0, "the failure must not be swallowed"
    assert sorted(os.listdir(home)) == ["loop-scratch.md"], os.listdir(home)
    assert (home / "loop-scratch.md").read_text(encoding="utf-8") == _scratch_heredoc_body(block)


def test_the_bsd_stand_ins_really_reject_the_gnu_forms(tmp_path):
    """Planted control: the stand-ins fail the old line, so the test above can fail."""
    extra, _ = _bsd_userland(tmp_path)
    old = (
        'cat > "${ENGRAM_HOME:-$HOME/.engram}"/s.md <<\'X\'\n# Log — $(date -I)\nX\n'
        'sed -i "s/\\$(date -I)/$(date -I)/" "${ENGRAM_HOME:-$HOME/.engram}"/s.md\n'
    )
    r, _home = run_block(old, tmp_path, "default", extra_env=extra)
    assert r.returncode != 0 and "invalid command code" in r.stderr + r.stdout


@pytest.mark.parametrize("name", list(SYNTAX_ONLY_BLOCKS))
def test_codex_blocks_parse(name):
    rel, pred = SYNTAX_ONLY_BLOCKS[name]
    r = subprocess.run(["bash", "-n"], input=block_where(rel, pred), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- C4: nothing else changes ---------------------------------------------------


def assert_c4(rel, base_text, head_text):
    """The C4 assertion body: texts in, AssertionError out."""
    base_lines = base_text.splitlines()
    head_lines = head_text.splitlines()
    assert len(head_lines) == len(base_lines), (
        f"{rel}: line count {len(base_lines)} -> {len(head_lines)}; the sweep adds a "
        "mention to a line, it never adds or removes one"
    )
    required = {i for i, b in enumerate(base_lines) if has_spelling(b) and TOKEN not in b}
    changed = {i for i, (b, h) in enumerate(zip(base_lines, head_lines)) if b != h}
    assert changed == required, (
        f"{rel}: changed lines differ from the lines C1 required.\n"
        f"  changed but not required: {sorted(n + 1 for n in changed - required)}\n"
        f"  required but unchanged:   {sorted(n + 1 for n in required - changed)}"
    )
    for i in sorted(changed):
        h = head_lines[i]
        assert TOKEN in h and has_spelling(h), (
            f"{rel} L{i + 1}: a swept line keeps its spelling and gains ENGRAM_HOME: {h[:140]}"
        )
        assert words_preserved(base_lines[i], h), (
            f"{rel} L{i + 1}: a swept line lost or changed a word beyond the home spelling.\n"
            f"  base: {base_lines[i][:140]}\n  head: {h[:140]}"
        )


@pytest.mark.parametrize("rel", FILES)
def test_c4_only_the_required_lines_changed(rel):
    _fresh_snapshot(rel)
    assert_c4(rel, _base_text(rel), _text(rel))


def words_preserved(base_line, head_line):
    """Every whitespace-separated word of the base line that is not itself a home
    spelling (no `.engram` in it) survives, in order, in the head line. Edge
    punctuation is ignored: a mention may sit between a word and its comma."""
    edge = "()[]`*,;:.\"'"
    head = iter(w.strip(edge) for w in head_line.split())
    return all(
        w.strip(edge) in head for w in base_line.split() if ".engram" not in w
    )


# --- the one named exception: the twin of the model-identity component ----------

# templates/template.CLAUDE.md is NOT one of the 23 (it names ENGRAM_HOME elsewhere,
# so it was never baselined). It carries a byte-identical twin of the paragraph in
# templates/components/model-identity-gate.md, and tests/test_template_components.py
# demands that parity, so the one swept line is edited in both. That parity itself is
# covered there and is deliberately not duplicated here.
TWIN_MENTION = "in `~/.engram/config.json` (under `$ENGRAM_HOME`, default `~/.engram`) as `self_lineage`"


def test_twin_changed_exactly_one_line_and_it_is_the_component_line():
    _fresh_snapshot(TWIN)
    base_lines = _base_text(TWIN).splitlines()
    head_lines = _text(TWIN).splitlines()
    assert len(head_lines) == len(base_lines)
    changed = [i for i, (b, h) in enumerate(zip(base_lines, head_lines)) if b != h]
    assert len(changed) == 1, f"{TWIN}: changed lines {[i + 1 for i in changed]}"
    i = changed[0]
    assert TWIN_MENTION in head_lines[i]
    assert head_lines[i].replace(" (under `$ENGRAM_HOME`, default `~/.engram`)", "") == base_lines[i]
    assert words_preserved(base_lines[i], head_lines[i])


def _our_diff_files(repo=REPO, sweep=SWEEP):
    """The ONE retirement point of the scope guards: it skips, unconditionally. No git
    call, no ref, no ancestry, no content check, whatever the repo holds. The arguments
    stay so the callers and the planted tests keep their shape."""
    pytest.skip(RETIRED_REASON)


def test_scope_only_the_23_the_twin_the_baseline_and_this_test_changed():
    allowed = (
        {SRC + f for f in FILES}
        | {SRC + TWIN, "tools/home_path_baseline.json", "tests/test_prose_home_sweep_3863.py"}
    )
    extra = sorted(_our_diff_files() - allowed)
    assert not extra, f"files changed outside the sweep scope: {extra}"


def test_disjoint_from_pr_3a_none_of_its_files_is_in_this_diff():
    clash = sorted(SRC + f for f in COVERED_BY_PR_3A if SRC + f in _our_diff_files())
    assert not clash, (
        f"{clash} are swept by the tools-prose PR (#3883, PR 3a); editing them here "
        "makes the two PRs conflict at merge. Leave them to PR 3a."
    )


def test_disjoint_from_pr_3b_none_of_its_files_is_in_this_diff():
    clash = sorted(SRC + f for f in COVERED_BY_PR_3B if SRC + f in _our_diff_files())
    assert not clash, (
        f"{clash} are swept by the shell-and-docstrings PR (#3887, PR 3b); editing them "
        "here makes the two PRs conflict at merge. Leave them to PR 3b."
    )


def test_the_sweep_holds_no_python_file():
    # The Python files were swept by PR 3a and PR 3b, so no bytecode check lives here.
    assert [f for f in FILES if f.endswith(".py")] == []


def _baseline_at_head(repo=REPO):
    return set(json.loads((Path(repo) / "tools" / "home_path_baseline.json").read_text())["baseline"])


def _exact_count_gate(repo=REPO, base=BASE, sweep=SWEEP):
    """Skip unless the exact-count snapshot applies: the baseline at HEAD is still the
    sweep's own AND PR 3a's baseline shrink is not in it (the entries 3a removes are
    still present: that is what "before PR 3a lands" means, read from content)."""
    reason = retire_reason([BASELINE_REL], repo=repo, base=base, sweep=sweep)
    if reason:
        pytest.skip(reason)
    if not {SRC + f for f in COVERED_BY_PR_3A} <= _baseline_at_head(repo):
        pytest.skip("PR 3a's baseline shrink is in this baseline; the exact count no longer applies")


def assert_c5_exact(head_baseline, base_baseline, n_expected=47 - 23):
    """The exact-count assertion body: sets in, AssertionError out."""
    assert head_baseline == base_baseline - {SRC + f for f in FILES}
    assert len(head_baseline) == n_expected


def test_c5_baseline_is_exactly_the_base_minus_the_23_before_pr_3a_lands():
    _exact_count_gate()
    assert_c5_exact(_baseline_at_head(), _base_baseline())


def test_c5_baseline_diff_from_dev_is_exactly_our_23():
    pytest.skip(RETIRED_REASON)  # retired permanently; see the module docstring


# --- C5: baseline and list ------------------------------------------------------


def test_c5_none_of_the_23_is_baselined_any_more():
    still = sorted(SRC + f for f in FILES if SRC + f in _baseline_at_head())
    assert not still, f"swept files still in tools/home_path_baseline.json: {still}"


def test_c5_the_23_are_the_base_baseline_minus_the_later_prs():
    base = {p[len(SRC):] for p in _base_baseline()}
    assert len(FILES) == len(set(FILES)) == 23
    assert LEFT_TO_LATER_PRS <= base and COVERED_BY_PR_3A <= base and COVERED_BY_PR_3B <= base
    assert base - LEFT_TO_LATER_PRS - COVERED_BY_PR_3A - COVERED_BY_PR_3B == set(FILES)


def test_c5_swept_files_are_clean_under_the_file_level_gate():
    assert GATE.check(REPO) == []


# --- C6: no behaviour change ----------------------------------------------------


class _Skeleton(HTMLParser):
    """Tags with their attributes, in order, plus script code with `//` comments
    cut (the sweep may add a mention to a comment; nothing else in a script)."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.items, self._in_script = [], False

    def handle_starttag(self, tag, attrs):
        self.items.append(("start", tag, tuple(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self.items.append(("end", tag))
        self._in_script = False

    def handle_startendtag(self, tag, attrs):
        self.items.append(("startend", tag, tuple(attrs)))

    def handle_data(self, data):
        if self._in_script:
            code = "\n".join(re.sub(r"//.*$", "", ln) for ln in data.splitlines())
            self.items.append(("script", code))


def _skeleton(text):
    p = _Skeleton()
    p.feed(text)
    return p.items


@pytest.mark.parametrize("rel", HTML_FILES)
def test_c6_html_tags_attributes_and_scripts_are_unchanged(rel):
    _fresh_snapshot(rel)
    assert _skeleton(_base_text(rel)) == _skeleton(_text(rel))


# --- planted controls: the detectors themselves ---------------------------------


class TestPlanted:
    def test_bare_tilde_line_fails_c1(self):
        assert same_line_violations("cat ~/.engram/x\n") == [(1, "cat ~/.engram/x")]

    def test_prose_with_the_mention_passes_c1(self):
        line = "Lives in `~/.engram/config.json` (`$ENGRAM_HOME`, default `~/.engram`).\n"
        assert same_line_violations(line) == []

    def test_a_mention_on_a_neighbour_line_does_not_excuse_the_line(self):
        assert len(same_line_violations("uses $ENGRAM_HOME\ncat ~/.engram/x\n")) == 1

    def test_sibling_folder_is_not_a_spelling(self):
        assert same_line_violations("ls ~/.engram-gemini/x ~/.claude/y ~/engram-alpha\n") == []

    def test_wrong_default_form_in_a_fence_fails_c2(self):
        text = 'x\n```bash\nls "${ENGRAM_HOME:-~/.engram}/x"\n```\n'
        assert fenced_violations(text) == [(3, 'ls "${ENGRAM_HOME:-~/.engram}/x"')]

    def test_bare_tilde_in_a_fence_fails_c2(self):
        assert fenced_violations("```\ncat ~/.engram/x\n```\n")

    def test_canonical_expression_in_a_fence_passes_c2(self):
        text = f'```bash\ncat "{CANONICAL}"/sessions/*.json\nWAKE="${{CLAUDE_PLUGIN_ROOT:-{CANONICAL}/m}}"\n```\n'
        assert fenced_violations(text) == []

    def test_canonical_does_not_excuse_a_second_spelling_on_the_line(self):
        assert fenced_violations(f'```\ncp "{CANONICAL}/a" ~/.engram/b\n```\n')

    def test_bare_env_var_in_a_fence_fails_c2(self):
        assert fenced_violations('```\nls "$ENGRAM_HOME/x"\n```\n')

    def test_prose_is_not_a_fence(self):
        assert fenced_violations("see `~/.engram/x` and $ENGRAM_HOME\n") == []

    @pytest.mark.parametrize(
        "line",
        [
            "python3 '" + CANONICAL + "'/hooks/x.py",            # single-quoted: never expands
            'cat "' + CANONICAL + '"reports/x.md',               # join without the slash
            'ls "${CLAUDE_PLUGIN_ROOT:-' + CANONICAL + 'm}"',   # glued: no slash before 'm'
            "ls foo" + CANONICAL + "/x",                         # glued on the left, unquoted
        ],
    )
    def test_context_check_flags_a_canonical_expression_that_does_not_expand(self, line):
        assert canonical_context_problems(line)
        assert fenced_violations(f"```\n{line}\n```\n")

    @pytest.mark.parametrize(
        "line",
        [
            'cat "' + CANONICAL + '"/reports/x.md',              # closing quote then slash
            'cat "' + CANONICAL + '/reports/x.md"',
            "cat " + CANONICAL + "/x",                           # unquoted at a word start
            'WAKE="${CLAUDE_PLUGIN_ROOT:-' + CANONICAL + '/m}"/t.py',
            'x=$(stat "' + CANONICAL + '"/a || stat -c %Y "' + CANONICAL + '"/a)',
            "echo 'it'\"'\"'s' \"" + CANONICAL + '"/x',         # apostrophe idiom, then double-quoted
        ],
    )
    def test_context_check_passes_the_working_forms(self, line):
        assert canonical_context_problems(line) == []
        assert fenced_violations(f"```\n{line}\n```\n") == []

    def test_a_break_out_of_single_quotes_passes_and_a_literal_one_fails(self):
        ok = "printf '%s' '{\"a\":\"Read '\"" + CANONICAL + "\"'/m.json for\"}' > \"" + CANONICAL + "\"/m.json"
        assert fenced_violations(f"```\n{ok}\n```\n") == []
        literal = "printf '%s' '{\"a\":\"Read " + CANONICAL + "/m.json for\"}'"
        assert fenced_violations(f"```\n{literal}\n```\n")
        no_slash = "printf '%s' '{\"a\":\"Read '\"" + CANONICAL + "\"'m.json\"}'"
        assert fenced_violations(f"```\n{no_slash}\n```\n")

    @pytest.mark.parametrize(
        "span",
        [
            "${ENGRAM_HOME-$HOME/.engram}/x",          # no colon
            "${ENGRAM_HOME:-~/.engram}/x",             # tilde in the default
            "$ENGRAM_HOME}/x",
            "${ENGRAM_HOME:-$HOME/.engram/x",          # missing brace
            "cat '" + CANONICAL + "'/x",               # single-quoted in a span
        ],
    )
    def test_prose_span_check_flags_a_malformed_form(self, span):
        assert prose_span_violations(f"see `{span}` here\n")

    @pytest.mark.parametrize(
        "span",
        ["$ENGRAM_HOME", "ENGRAM_HOME", CANONICAL + "/x", 'rm "' + CANONICAL + '"/loop-mode.json'],
    )
    def test_prose_span_check_passes_the_description_and_canonical_forms(self, span):
        assert prose_span_violations(f"see `{span}` here\n") == []

    def test_prose_span_check_ignores_fenced_blocks_and_spans_without_the_token(self):
        assert prose_span_violations("```\n`${ENGRAM_HOME-x}`\n```\n`~/.engram/x`\n") == []

    def test_tool_path_line_rule_planted(self):
        write_target = "Write the report to `" + CANONICAL + "/dream/x.md`"
        assert tool_path_line_problems(write_target)
        ok = "Write the report to `~/.engram/dream/x.md` (under `$ENGRAM_HOME`, default `~/.engram`)"
        assert tool_path_line_problems(ok) == []
        assert tool_path_line_problems("Write to `~/.engram/dream/x.md`")  # no mention
        # and the description form passes the span check
        assert prose_span_violations(ok) == []

    def test_words_preserved_allows_a_mention_and_a_canonical_swap_only(self):
        base = 'mv ~/.engram/a.md "$x" # keep this'
        assert words_preserved(base, f'mv "{CANONICAL}"/a.md "$x" # keep this')
        assert words_preserved(base, "mv ~/.engram/a.md (under $ENGRAM_HOME) \"$x\" # keep this")
        assert not words_preserved(base, 'mv "{c}"/a.md "$x" # keep that'.replace("{c}", CANONICAL))
        assert not words_preserved(base, f'mv "{CANONICAL}"/a.md "$x"')

    def test_skeleton_ignores_text_and_script_comments_only(self):
        a = '<p class="x">in ~/.engram</p><script>var a = 1; // ~/.engram\n</script>'
        b = '<p class="x">in ~/.engram ($ENGRAM_HOME)</p><script>var a = 1; // ~/.engram ($ENGRAM_HOME)\n</script>'
        c = '<p class="y">in ~/.engram</p><script>var a = 1;\n</script>'
        d = '<p class="x">in ~/.engram</p><script>var a = 2; // ~/.engram\n</script>'
        assert _skeleton(a) == _skeleton(b)
        assert _skeleton(a) != _skeleton(c)
        assert _skeleton(a) != _skeleton(d)


# --- planted controls: the snapshot guards retire by CONTENT (#3925) --------------
#
# The guards used to retire on a commit COUNT (`rev-list --count BASE..HEAD`). On a
# partial-history checkout BASE is present but the later commits are not, the count
# reads <= 1, the guard does not retire, and it fails on files legitimately edited
# later. These tests build real temp git repos and pin the content-keyed rule.

_P_REL = "skills/claude/engram-x/SKILL.md"
_P_BASE_TEXT = "cat ~/.engram/x\nkeep this line\n"
_P_SWEEP_TEXT = "cat ~/.engram/x ($ENGRAM_HOME, default ~/.engram)\nkeep this line\n"
_P_LATER_TEXT = _P_SWEEP_TEXT + "an honest later edit\n"
_P_NOSHA = "f" * 40  # a sha that is in no repo: a SWEEP the checkout never fetched
_BL = "tools/home_path_baseline.json"


def _pg(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )


def _p_commit(repo, files, msg="c"):
    for rel, text in files.items():
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _pg(repo, "add", "-A")
    _pg(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _pg(repo, "rev-parse", "HEAD").stdout.strip()


def _p_repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _pg(repo, "init", "-q")
    return repo


def _p_full(tmp_path, name="r", head_text=None, between=0, sweep_text=_P_SWEEP_TEXT):
    """base -> sweep -> (between unrelated commits) -> optional later edit of the file."""
    repo = _p_repo(tmp_path, name)
    base = _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT})
    sweep = _p_commit(repo, {SRC + _P_REL: sweep_text})
    for i in range(between):
        _p_commit(repo, {f"unrelated{i}.txt": str(i)})
    if head_text is not None:
        _p_commit(repo, {SRC + _P_REL: head_text})
    return repo, base, sweep


def _must_run(fn, *args, **kw):
    """Call a guard that must RUN here; a skip would be a silent vacuity, so it fails."""
    try:
        return fn(*args, **kw)
    except pytest.skip.Exception as e:
        raise AssertionError(f"the guard skipped but must run: {e}")


def _p_shallow(tmp_path, repo):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
                   check=True, capture_output=True)
    return shallow


class TestRetireByContent:
    P = [SRC + _P_REL]

    # (a) full history, file edited after the sweep -> retire (K3)
    def test_a_full_history_file_edited_after_the_sweep_retires(self, tmp_path):
        repo, base, sweep = _p_full(tmp_path, head_text=_P_LATER_TEXT)
        assert retire_reason(self.P, repo=repo, base=base, sweep=sweep)

    # (b) the CI cell: BASE reachable, SWEEP and everything after it absent (K2)
    def test_b_partial_history_sweep_absent_retires_where_the_old_count_ran(self, tmp_path):
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT})
        _p_commit(repo, {SRC + _P_REL: _P_LATER_TEXT})  # HEAD directly on BASE
        old = _pg(repo, "rev-list", "--count", f"{base}..HEAD", "--", SRC + _P_REL).stdout.strip()
        assert int(old) <= 1, "the OLD commit-count rule would not retire here: this is the CI failure"
        reason = retire_reason(self.P, repo=repo, base=base, sweep=_P_NOSHA)
        assert reason and "sweep" in reason

    def test_b_site1_the_snapshot_helper_skips_there_instead_of_running(self, tmp_path):
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT})
        _p_commit(repo, {SRC + _P_REL: _P_LATER_TEXT})
        with pytest.raises(pytest.skip.Exception):
            _fresh_snapshot(_P_REL, repo=repo, base=base, sweep=_P_NOSHA)

    def test_k1_base_absent_retires(self, tmp_path):
        repo, base, sweep = _p_full(tmp_path)
        assert retire_reason(self.P, repo=repo, base=_P_NOSHA, sweep=sweep)

    # (c) HEAD == SWEEP content -> the guard runs (K4)
    def test_c_head_equals_sweep_content_runs(self, tmp_path):
        repo, base, sweep = _p_full(tmp_path)
        assert retire_reason(self.P, repo=repo, base=base, sweep=sweep) is None
        _must_run(_fresh_snapshot, _P_REL, repo=repo, base=base, sweep=sweep)

    # The sweep is a squash several commits above BASE: a file another commit edited
    # between BASE and the sweep cannot be judged as "base plus the sweep's lines".
    def test_a_file_edited_between_base_and_the_sweep_retires_even_at_sweep_content(self, tmp_path):
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT})
        _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT + "a foreign edit before the sweep\n"})
        sweep = _p_commit(repo, {SRC + _P_REL: _P_SWEEP_TEXT + "a foreign edit before the sweep\n"})
        assert retire_reason(self.P, repo=repo, base=base, sweep=sweep)

    def test_a_sweep_whose_parent_is_absent_retires(self, tmp_path):
        repo, base, sweep = _p_full(tmp_path)
        shallow = _p_shallow(tmp_path, repo)
        assert _has_commit(shallow, sweep) and not _has_commit(shallow, base)
        assert retire_reason(self.P, repo=shallow, base=sweep, sweep=sweep)  # base==sweep: parent absent

    # K5: unrelated commits between SWEEP and HEAD change nothing
    def test_k5_unrelated_commits_after_the_sweep_do_not_change_the_answer(self, tmp_path):
        repo, base, sweep = _p_full(tmp_path, between=3)
        assert retire_reason(self.P, repo=repo, base=base, sweep=sweep) is None
        repo2, base2, sweep2 = _p_full(tmp_path, name="r2", head_text=_P_LATER_TEXT, between=3)
        assert retire_reason(self.P, repo=repo2, base=base2, sweep=sweep2)

    # (d) vacuity: HEAD == SWEEP content with a violation planted in the swept file;
    # the guard runs (does not retire) and the real c4 body still fails.
    def test_d_c4_body_still_fails_on_a_planted_violation_when_the_guard_runs(self, tmp_path):
        bad = _P_SWEEP_TEXT + "an extra line the sweep must not add\n"
        repo, base, sweep = _p_full(tmp_path, sweep_text=bad)
        assert retire_reason(self.P, repo=repo, base=base, sweep=sweep) is None
        head = (repo / SRC / _P_REL).read_text()
        with pytest.raises(AssertionError):
            assert_c4(_P_REL, _P_BASE_TEXT, head)

    def test_d_c4_body_passes_the_correct_sweep(self):
        assert_c4(_P_REL, _P_BASE_TEXT, _P_SWEEP_TEXT)

    def test_d_c4_body_fails_a_dropped_word(self):
        with pytest.raises(AssertionError):
            assert_c4(_P_REL, _P_BASE_TEXT, _P_SWEEP_TEXT.replace("keep this line", "keep that line"))

    def test_d_c5_exact_body_fails_on_a_planted_extra_baseline_entry(self):
        base = {SRC + f for f in FILES} | {"a.py", "b.py"}
        good = {"a.py", "b.py"}
        assert_c5_exact(good, base, n_expected=2)
        with pytest.raises(AssertionError):
            assert_c5_exact(good | {"extra.py"}, base, n_expected=2)
        with pytest.raises(AssertionError):
            assert_c5_exact(good | {SRC + FILES[0]}, base, n_expected=3)

    # site 2: the exact-count gate
    def _bl_repo(self, tmp_path, head_entries, sweep_entries=None):
        repo = _p_repo(tmp_path)
        entries = sweep_entries if sweep_entries is not None else head_entries
        base = _p_commit(repo, {_BL: json.dumps({"baseline": ["x"]})})
        sweep = _p_commit(repo, {_BL: json.dumps({"baseline": entries})})
        if head_entries != entries:
            _p_commit(repo, {_BL: json.dumps({"baseline": head_entries})})
        return repo, base, sweep

    def test_site2_exact_gate_runs_on_sweep_content_with_3a_unlanded(self, tmp_path):
        ents = sorted(SRC + f for f in COVERED_BY_PR_3A)
        repo, base, sweep = self._bl_repo(tmp_path, ents)
        _must_run(_exact_count_gate, repo=repo, base=base, sweep=sweep)

    def test_site2_exact_gate_skips_when_the_sweep_is_absent(self, tmp_path):
        ents = sorted(SRC + f for f in COVERED_BY_PR_3A)
        repo, base, _ = self._bl_repo(tmp_path, ents)
        with pytest.raises(pytest.skip.Exception):
            _exact_count_gate(repo=repo, base=base, sweep=_P_NOSHA)

    def test_site2_exact_gate_skips_when_the_baseline_changed_after_the_sweep(self, tmp_path):
        ents = sorted(SRC + f for f in COVERED_BY_PR_3A)
        repo, base, sweep = self._bl_repo(tmp_path, ents + ["later.py"], sweep_entries=ents)
        with pytest.raises(pytest.skip.Exception):
            _exact_count_gate(repo=repo, base=base, sweep=sweep)

    def test_site2_exact_gate_skips_once_3a_has_shrunk_the_baseline(self, tmp_path):
        repo, base, sweep = self._bl_repo(tmp_path, ["other.py"])
        with pytest.raises(pytest.skip.Exception):
            _exact_count_gate(repo=repo, base=base, sweep=sweep)


# --- planted controls: a git failure that means "the evidence is missing" skips (S2) ---
#
# A blobless or otherwise partial checkout can hold a COMMIT whose BLOB is absent. Then
# `git show <commit>:<path>` fails and prints nothing; a guard that parses that output, or
# asserts on its return code, fails with a JSON or git error instead of skipping. These
# cells build that state for real (commit the file, delete its loose blob object) and
# prove every guard skips there.


def _p_drop_blob(repo, commit, rel):
    """Delete the loose blob of `rel` at `commit`; assert the commit survives and
    `git show commit:rel` now fails."""
    blob = _pg(repo, "rev-parse", f"{commit}:{rel}").stdout.strip()
    obj = Path(repo) / ".git" / "objects" / blob[:2] / blob[2:]
    assert obj.is_file(), "blob is not loose; the cell cannot delete it"
    obj.chmod(0o644)
    obj.unlink()
    assert _has_commit(repo, commit)
    assert _git("show", f"{commit}:{rel}", repo=repo).returncode != 0


def _p_blob_missing(tmp_path, name, files):
    """A repo whose commit holds `files` but whose blobs for them are deleted from the
    object store. Returns (repo, commit)."""
    repo = _p_repo(tmp_path, name)
    commit = _p_commit(repo, files)
    for rel in files:
        _p_drop_blob(repo, commit, rel)
    return repo, commit


class TestEvidenceMissing:
    def test_the_cell_really_is_commit_present_blob_missing(self, tmp_path):
        repo, commit = _p_blob_missing(tmp_path, "r", {_BL: json.dumps({"baseline": ["u1"]})})
        assert _has_commit(repo, commit)
        assert _git("show", f"{commit}:{_BL}", repo=repo).returncode != 0

    def test_base_text_skips_when_the_base_blob_is_missing(self, tmp_path):
        repo, base = _p_blob_missing(tmp_path, "r", {SRC + _P_REL: "unique base text 1\n"})
        with pytest.raises(pytest.skip.Exception):
            _base_text(_P_REL, repo=repo, base=base)

    def test_base_text_returns_the_text_when_the_blob_is_there(self, tmp_path):
        repo, base, _ = _p_full(tmp_path)
        assert _must_run(_base_text, _P_REL, repo=repo, base=base) == _P_BASE_TEXT

    def test_base_baseline_skips_when_the_base_blob_is_missing(self, tmp_path):
        repo, base = _p_blob_missing(tmp_path, "r", {_BL: json.dumps({"baseline": ["u2"]})})
        with pytest.raises(pytest.skip.Exception):
            _base_baseline(repo=repo, base=base)

    def test_base_baseline_returns_the_entries_when_the_blob_is_there(self, tmp_path):
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {_BL: json.dumps({"baseline": ["a", "b"]})})
        assert _must_run(_base_baseline, repo=repo, base=base) == {"a", "b"}

    def test_retire_reason_retires_when_the_base_blob_is_missing(self, tmp_path):
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {SRC + _P_REL: _P_BASE_TEXT + "unique 3\n"})
        sweep = _p_commit(repo, {SRC + _P_REL: _P_SWEEP_TEXT})
        _p_drop_blob(repo, base, SRC + _P_REL)
        assert retire_reason([SRC + _P_REL], repo=repo, base=base, sweep=sweep)

    def test_exact_count_gate_skips_when_the_base_baseline_blob_is_missing(self, tmp_path):
        ents = sorted(SRC + f for f in COVERED_BY_PR_3A)
        repo = _p_repo(tmp_path)
        base = _p_commit(repo, {_BL: json.dumps({"baseline": ["x", "unique 4"]})})
        sweep = _p_commit(repo, {_BL: json.dumps({"baseline": ents})})
        _p_drop_blob(repo, base, _BL)
        with pytest.raises(pytest.skip.Exception):
            _exact_count_gate(repo=repo, base=base, sweep=sweep)


# --- the four retired tests (the frozen-fact guards) -------------------------------


class TestRetired:
    def test_the_reason_names_what_retired_the_guards(self):
        assert RETIRED_REASON == (
            f"sweep merged as {SWEEP[:8]}; concurrent-edit guard no longer applies"
        )
        assert RETIRED_REASON == "sweep merged as caebaf3c; concurrent-edit guard no longer applies"

    def test_diff_files_skips_unconditionally_even_where_the_old_code_would_run(self, tmp_path):
        repo = _p_repo(tmp_path)
        _p_commit(repo, {SRC + FILES[0]: _P_BASE_TEXT})
        sweep = _p_commit(repo, {SRC + FILES[0]: _P_SWEEP_TEXT, _BL: "{}"})
        with pytest.raises(pytest.skip.Exception) as e:
            _our_diff_files(repo=repo, sweep=sweep)
        assert str(e.value) == RETIRED_REASON
