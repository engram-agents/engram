"""No command-position hard-coded ENGRAM home path in four shipped files [part of #3863].

Lei's ruling (2026-10-17): the deployment uses ENGRAM_HOME wherever possible,
because the install allows a custom engram folder. `tools/home_path_gate.py`
is a FILE-level ratchet (a file that mentions ENGRAM_HOME once counts clean),
so it cannot see a skill that names `ENGRAM_HOME` in one place and pastes
`~/.engram/...` in another. This test is the line-level control for the three
skills PR 2 of #3863 fixes.

DEFINITION (precise). A *code line* is any line inside a fenced block (``` or
~~~ fences) plus every inline code span (`...`) on a non-fenced line. A code
line VIOLATES when it contains `~/.engram`, `$HOME/.engram`, `${HOME}/.engram`
or `"$HOME"/.engram` (with `.engram` as the whole folder name: `.engram-x` and
`.engram_x` do not count) that is NOT immediately preceded by the text
`${ENGRAM_HOME:-`. So `${ENGRAM_HOME:-$HOME/.engram}/x` passes and a bare
`~/.engram/x` is flagged.

The negative regex also knows `"${HOME}"/.engram` and an absolute
`/Users/<x>/.engram` or `/home/<x>/.engram`.

POSITIVE CHECK (PR 2b). The negative regex only knows the spellings it lists,
so a wrong-in-real-bash `ENGRAM_HOME` expression slips past it:
`${ENGRAM_HOME:-~/.engram}` (a tilde is not expanded inside double quotes),
`${ENGRAM_HOME:-$HOME}/.engram` (gives `<custom>/.engram` when the variable is
set), a bare `$ENGRAM_HOME` (empty when unset) and an absolute default. So in
every code chunk, EVERY occurrence of the text `ENGRAM_HOME` must sit inside
the one canonical expression `${ENGRAM_HOME:-$HOME/.engram}`. Anything else
(including a typo such as `ENGRAM_HOMES`) is flagged.

KNOWN BOUNDS. NOT scanned: prose outside backticks (it is not something a
reader pastes), 4-space-indented code blocks, and markdown link targets
(`[x](~/.engram/y)`). The rule for them is in the PR's decision log, not
enforced here.

RUNTIME PIN (PR 2b). The `python3 -c` snippets that need the home take it as
`sys.argv[1]`, never spliced into a Python string literal (a single quote or
backslash in the path would end the literal). `TestPythonSnippetsTakeArgv`
extracts every such snippet from the skills and RUNS it against a home whose
name holds both a single quote and a space.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "src" / "engram" / "skills" / "claude"

TARGETS = [
    "engram-health-exam",
    "engram-upgrade",
    "engram-sleep",
]

_HOME_PATH = re.compile(
    r"(?:~|\$HOME|\$\{HOME\}|\"\$HOME\"|\"\$\{HOME\}\""
    r"|/(?:Users|home)/[^/\s\"'`]+)/\.engram(?![\w-])"
)
_ALLOWED_PREFIX = "${ENGRAM_HOME:-"
_CANONICAL = "${ENGRAM_HOME:-$HOME/.engram}"
_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`([^`\n]+)`")


def _code_chunks(text):
    """Yield (lineno, chunk) for every fenced line and inline code span."""
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield n, line
        else:
            for m in _SPAN.finditer(line):
                yield n, m.group(1)


def flagged_lines(text):
    """[(lineno, chunk)] of code chunks holding a hard-coded home path."""
    out = []
    for n, chunk in _code_chunks(text):
        for m in _HOME_PATH.finditer(chunk):
            if chunk[m.end():].startswith("/venv"):
                continue  # Borges' ruling on #3863: the venv is always ~/.engram/venv
            if not chunk[: m.start()].endswith(_ALLOWED_PREFIX):
                out.append((n, chunk.strip()))
                break
    return out


def non_canonical_home_tokens(text):
    """[(lineno, chunk)] of code chunks where an `ENGRAM_HOME` token is not
    inside the canonical expression `${ENGRAM_HOME:-$HOME/.engram}`."""
    out = []
    for n, chunk in _code_chunks(text):
        covered = [
            (m.start(), m.end()) for m in re.finditer(re.escape(_CANONICAL), chunk)
        ]
        for m in re.finditer("ENGRAM_HOME", chunk):
            if not any(a <= m.start() and m.end() <= b for a, b in covered):
                out.append((n, chunk.strip()))
                break
    return out


@pytest.mark.parametrize("skill", TARGETS)
def test_skill_has_only_the_canonical_engram_home_expression(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    bad = non_canonical_home_tokens(text)
    assert not bad, (
        f"{skill}/SKILL.md has {len(bad)} code chunk(s) where ENGRAM_HOME is not "
        f"exactly {_CANONICAL} (#3863):\n" + "\n".join(f"  L{n}: {c}" for n, c in bad)
    )


@pytest.mark.parametrize("skill", TARGETS)
def test_skill_has_no_command_position_hardcoded_home(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    bad = flagged_lines(text)
    assert not bad, (
        f"{skill}/SKILL.md hard-codes the ENGRAM home in {len(bad)} code line(s); "
        f"use ${{ENGRAM_HOME:-$HOME/.engram}} (#3863):\n"
        + "\n".join(f"  L{n}: {c}" for n, c in bad)
    )


class TestPlantedDefinition:
    """The detector itself: bare spellings are flagged, the env form is not."""

    def test_bare_tilde_in_fence_is_flagged(self):
        text = "```bash\nls ~/.engram/x\n```\n"
        assert flagged_lines(text) == [(2, "ls ~/.engram/x")]

    @pytest.mark.parametrize(
        "spelling",
        ["$HOME/.engram/x", "${HOME}/.engram/x", '"$HOME"/.engram/x', "~/.engram"],
    )
    def test_each_spelling_flagged_in_fence_and_span(self, spelling):
        assert flagged_lines(f"```\ncat {spelling}\n```\n")
        assert flagged_lines(f"see `{spelling}` here\n")

    def test_env_form_is_not_flagged(self):
        text = '```bash\nls "${ENGRAM_HOME:-$HOME/.engram}/x"\n```\nuse `${ENGRAM_HOME:-$HOME/.engram}/y`\n'
        assert flagged_lines(text) == []

    def test_env_form_does_not_excuse_a_second_bare_path_on_the_line(self):
        text = '```\ncp "${ENGRAM_HOME:-$HOME/.engram}/a" ~/.engram/b\n```\n'
        assert len(flagged_lines(text)) == 1

    def test_prose_outside_backticks_is_not_scanned(self):
        assert flagged_lines("lives in ~/.engram by default\n") == []

    def test_sibling_folder_names_are_not_the_home(self):
        assert flagged_lines("```\nls ~/.engram-gemini/x ~/.engram_home\n```\n") == []

    def test_braced_quoted_home_and_absolute_paths_are_flagged(self):
        for spelling in ('"${HOME}"/.engram/x', "/Users/someone/.engram/x", "/home/someone/.engram/x"):
            assert flagged_lines(f"```\ncat {spelling}\n```\n"), spelling
            assert flagged_lines(f"see `{spelling}` here\n"), spelling

    def test_known_bounds_are_really_unscanned(self):
        assert flagged_lines("    ls ~/.engram/x\n") == []  # indented code block
        assert flagged_lines("[x](~/.engram/y)\n") == []  # link target


WRONG_FORMS = [
    "${ENGRAM_HOME:-~/.engram}",
    "${ENGRAM_HOME:-$HOME}/.engram",
    "$ENGRAM_HOME",
    "${ENGRAM_HOME:-/home/x/.engram}",
]


class TestPositiveCheckPlanted:
    @pytest.mark.parametrize("form", WRONG_FORMS)
    def test_each_wrong_form_is_flagged_in_fence_and_span(self, form):
        assert non_canonical_home_tokens(f'```bash\nls "{form}/x"\n```\n')
        assert non_canonical_home_tokens(f"use `{form}/y` here\n")

    def test_canonical_is_not_flagged(self):
        text = f'```bash\nls "{_CANONICAL}"\n```\nuse `{_CANONICAL}`\n'
        assert non_canonical_home_tokens(text) == []

    def test_canonical_followed_by_more_path_is_fine(self):
        text = f'```bash\ncat "{_CANONICAL}/a/b.json"\n```\nsee `{_CANONICAL}/knowledge.db`\n'
        assert non_canonical_home_tokens(text) == []

    def test_canonical_nested_in_another_default_is_fine(self):
        text = f'```\ncd "${{CLAUDE_PLUGIN_ROOT:-{_CANONICAL}/m}}"\n```\n'
        assert non_canonical_home_tokens(text) == []

    def test_canonical_does_not_excuse_a_second_wrong_token_on_the_line(self):
        text = f'```\ncp "{_CANONICAL}/a" "$ENGRAM_HOME/b"\n```\n'
        assert len(non_canonical_home_tokens(text)) == 1

    def test_canonical_plus_a_second_bare_path_is_still_flagged(self):
        text = f'```\ncp "{_CANONICAL}/a" ~/.engram/b\n```\n'
        assert len(flagged_lines(text)) == 1

    def test_name_typos_are_flagged(self):
        assert non_canonical_home_tokens("```\nls ${ENGRAM_HOMES:-$HOME/.engram}\n```\n")
        assert non_canonical_home_tokens("```\nls ${ENGRAM_HOME-$HOME/.engram}\n```\n")

    def test_prose_outside_backticks_is_not_scanned(self):
        assert non_canonical_home_tokens("the tool resolves ENGRAM_HOME itself\n") == []


# --- runtime pin: the python snippets take the path as argv ----------------------

_SNIPPET_START = re.compile(r"python3 -c ([\"'])")
_TRAILING_ARG = re.compile(r'[ \t]+"[^"\n]*"')


def _fenced_ranges(text):
    ranges, start, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            if start is None:
                start = pos
            else:
                ranges.append((start, pos))
                start = None
        pos += len(line)
    return ranges


def _python_c_sites(text):
    """[(lineno, code, command)] for every fenced `python3 -c <quoted>` site.

    `command` is the invocation: `python3 -c <quoted code>` plus any trailing
    double-quoted arguments on the line that closes the code. The quote that
    opens the code also closes it (these skills never escape one inside).
    """
    fences = _fenced_ranges(text)
    out = []
    for m in _SNIPPET_START.finditer(text):
        if not any(a <= m.start() < b for a, b in fences):
            continue
        end = text.index(m.group(1), m.end())
        code = text[m.end():end]
        k = end + 1
        while True:
            mm = _TRAILING_ARG.match(text, k)
            if not mm:
                break
            k = mm.end()
        out.append((text.count("\n", 0, m.start()) + 1, code, text[m.start():k]))
    return out


def _home_sites(skill):
    """The sites whose invocation carries the ENGRAM home (directly, or via the
    sleep skill's $MARKER, which is built from it)."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    return [s for s in _python_c_sites(text) if "ENGRAM_HOME" in s[2] or "$MARKER" in s[2]]


EXPECTED_HOME_SITES = {"engram-health-exam": 6, "engram-sleep": 1, "engram-upgrade": 0}


def _scratch_home(home):
    """A minimal ENGRAM home at `home` (its path may hold a quote and a space)."""
    plugin = home / "marketplace" / "plugins" / "engram"
    (plugin / "tools").mkdir(parents=True)
    (plugin / "engram_backup.py").write_text(
        "def check_fts_integrity(conn):\n"
        "    conn.execute('SELECT count(*) FROM t').fetchone()\n"
        "    return {'status': 'ok', 'probed_terms': 3}\n"
    )
    (home / "config.json").write_text(
        json.dumps({"agent_name": "zed", "mode": "single", "self_lineage": "x:y"})
    )
    (home / "forum-surfaced-cursor.txt").write_text("0")
    (home / "sessions").mkdir()
    (home / "sessions" / "last-sleep-success.json").write_text(
        json.dumps({"completed_at": "2026-10-17T00:00:00"})
    )
    c = sqlite3.connect(home / "knowledge.db")
    c.execute("CREATE TABLE t(x)")
    c.execute("PRAGMA user_version = 7")
    c.commit()
    c.close()


def _run(command, home, user_home, *, marker, export_home):
    """Run one extracted invocation under bash. tests/conftest.py puts a
    throwaway ENGRAM_HOME in every test process, so the env is built explicitly."""
    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    env["HOME"] = str(user_home)
    if export_home:
        env["ENGRAM_HOME"] = str(home)
    script = f'p=tools; MARKER="{marker}"; out=$({command}) && printf %s "$out"'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )


class TestPythonSnippetsTakeArgv:
    def test_site_population(self):
        got = {sk: len(_home_sites(sk)) for sk in TARGETS}
        assert got == EXPECTED_HOME_SITES, got

    @pytest.mark.parametrize("skill", TARGETS)
    def test_no_snippet_splices_the_path_into_python_code(self, skill):
        for lineno, code, _cmd in _home_sites(skill):
            assert "ENGRAM_HOME" not in code and "$MARKER" not in code, (
                f"{skill} L{lineno}: the home is spliced into the Python code; "
                "pass it as sys.argv[1]"
            )

    @pytest.mark.parametrize("export_home", [True, False], ids=["ENGRAM_HOME", "default-HOME"])
    def test_every_snippet_runs_in_a_home_with_quote_and_space(self, tmp_path, export_home):
        # export_home=False: ENGRAM_HOME unset, the default $HOME/.engram, with
        # HOME itself holding the quote (a user named O'Brien).
        user_home = tmp_path / "O'Brien home"
        user_home.mkdir()
        home = user_home / ("custom" if export_home else ".engram")
        home.mkdir()
        _scratch_home(home)
        marker = home / "sessions" / "last-sleep-success.json"
        wants = {
            "forum-surfaced-cursor": "forum-surfaced-cursor.txt",
            "user_version": "DB user_version (schema version): 7",
            "check_fts_integrity": "{'status': 'ok', 'probed_terms': 3}",
            "['agent_name']": "zed",
            "drowsiness": "agent_name   : zed",
            "realpath": str(home / "tools"),
            "completed_at": "2026-10-17T00:00:00",
        }
        seen, bad = [], []
        for skill in TARGETS:
            for lineno, code, cmd in _home_sites(skill):
                key = next(k for k in wants if k in code)
                r = _run(cmd, home, user_home, marker=marker, export_home=export_home)
                seen.append((skill, lineno))
                if r.returncode != 0 or wants[key] not in r.stdout:
                    bad.append((skill, lineno, r.returncode, r.stdout, r.stderr[-120:]))
        assert len(seen) == sum(EXPECTED_HOME_SITES.values()), seen
        assert not bad, bad
