"""The launcher, the forum monitors and two dev scripts resolve the ENGRAM home
from ENGRAM_HOME; five code files' docstrings follow [part of #3863].

Lei's ruling (2026-10-17): the deployment uses ENGRAM_HOME wherever possible,
because the install allows a custom engram folder. `tools/home_path_gate.py` is
a FILE-level ratchet (a file that mentions ENGRAM_HOME once counts clean), so
this file is the line-level control for what PR 3b of #3863 fixes.

(a) SCAN. In each of the five shell scripts, every `.engram` folder name on a
    non-comment line must sit inside the one canonical expression
    `${ENGRAM_HOME:-$HOME/.engram}` (the spelling the skills use), and every
    `ENGRAM_HOME` token must too. Bounds: only whole-line `#` comments are
    exempt; a trailing `# ...` on a code line is scanned (so is a heredoc
    body: the monitors' `--help` text carries the canonical expression too).
(b) BEHAVIOUR. `bash -n`; the launcher is run against a stub venv python at
    the path it would use; each monitor is run against a scratch config and an
    unreachable forum; the assignments in ghpr.sh / run-all-tests.sh are
    extracted and evaluated (neither the CI sim nor the suite is ever run).
(c) DOCSTRING-ONLY. The five core files carry `ENGRAM_HOME`, and no string
    constant outside a docstring holds `.engram` as a folder name.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"

SHELL_FILES = [
    "launch-engram-server.sh",
    "tools/forum-mention-monitor.sh",
    "tools/forum-updates-monitor.sh",
    "tools/ghpr.sh",
    "tools/run-all-tests.sh",
]
CORE_FILES = [
    "engram_lifecycle.py",
    "engram_tasks.py",
    "health/engine.py",
    "hooks/claude/context_tracker_hook.py",
    "viz/server.py",
]

_CANONICAL = "${ENGRAM_HOME:-$HOME/.engram}"
# `.engram` as the whole folder name (not `com.engram.x`, `.engram-gemini`,
# `.engram_home`): same lookahead as the gate's _E, plus no word char before.
_ENGRAM_DIR = re.compile(r"(?<!\w)\.engram(?![\w-])")


def _code_lines(text):
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield n, line


# The plugin venv is an install artifact at ~/.engram/venv, NOT under ENGRAM_HOME
# (README-AGENT.md:187; Borges's ruling on #3863). A code line that spells it is
# exempt from the canonical-expression rule and from the every-`.engram`-line rule,
# as the gate exempts the venv by name; a trailing `# ...` comment on such a line is
# exempt with it.
import importlib.util as _ilu

_gate_spec = _ilu.spec_from_file_location("home_path_gate", REPO / "tools" / "home_path_gate.py")
_gate_mod = _ilu.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate_mod)
# The ONE definition of the venv exemption and of the ENGRAM_HOME-derived venv class
# is the gate's (#3863 6c); this scan has no regex of its own for either.
_mask_venv = _gate_mod.mask_venv

# The relative-home guard block (#3863 6c; tests/test_launchers_relative_engram_home_3863.py pins it) must
# test and re-export the bare variable, so it cannot use the canonical expression. It is blanked out here,
# line count preserved; a bare token ANYWHERE ELSE is still flagged.
_GUARD_BLOCK = re.compile(r'^if \[ -n "\$\{ENGRAM_HOME-\}" \]; then\n.*?^fi$', re.S | re.M)


def non_canonical_lines(text):
    """[(lineno, line)] of non-comment lines where `.engram` or `ENGRAM_HOME`
    appears outside the canonical expression (the guard block excepted)."""
    text = _GUARD_BLOCK.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out = []
    for n, line in _code_lines(text):
        rest = line.replace(_CANONICAL, "")
        masked = _mask_venv(rest)
        if masked != rest:
            rest = masked.split("#", 1)[0]
        if (
            _ENGRAM_DIR.search(rest)
            or "ENGRAM_HOME" in rest
            or _gate_mod.engram_home_derived_venv(line)
        ):
            out.append((n, line.strip()))
    return out


def lines_naming_dot_engram_without_home(text):
    """[(lineno, line)] of ANY line (comments and docstrings included) that
    names `.engram` as a folder but not `ENGRAM_HOME` on the same line (the
    design seat's R1/R2 rule, #3863: a description of the default home carries
    "`$ENGRAM_HOME` (default `~/.engram`)" on one line)."""
    return [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), 1)
        if _ENGRAM_DIR.search(_mask_venv(line)) and "ENGRAM_HOME" not in line
    ]


# --- (a) scan ----------------------------------------------------------------------


@pytest.mark.parametrize("rel", SHELL_FILES + CORE_FILES)
def test_every_dot_engram_line_also_names_engram_home(rel):
    bad = lines_naming_dot_engram_without_home((SRC / rel).read_text(encoding="utf-8"))
    assert not bad, f"{rel}: line(s) name `.engram` without ENGRAM_HOME:\n" + "\n".join(
        f"  L{n}: {c}" for n, c in bad
    )


class TestPlantedPerLine:
    def test_comment_and_prose_lines_are_flagged(self):
        assert lines_naming_dot_engram_without_home("# lives in ~/.engram\n")
        assert lines_naming_dot_engram_without_home("    default is ~/.engram here\n")

    def test_same_line_pairing_passes_other_lines_do_not_count(self):
        assert lines_naming_dot_engram_without_home("`$ENGRAM_HOME` (default `~/.engram`)\n") == []
        assert lines_naming_dot_engram_without_home("ENGRAM_HOME is set\nthen ~/.engram\n")

    def test_sibling_names_are_not_flagged(self):
        assert lines_naming_dot_engram_without_home("com.engram.viz ~/.engram-gemini\n") == []


@pytest.mark.parametrize("rel", SHELL_FILES)
def test_shell_script_has_only_the_canonical_home_expression(rel):
    text = (SRC / rel).read_text(encoding="utf-8")
    bad = non_canonical_lines(text)
    assert not bad, (
        f"{rel}: {len(bad)} code line(s) name the ENGRAM home other than as "
        f"{_CANONICAL} (#3863):\n" + "\n".join(f"  L{n}: {c}" for n, c in bad)
    )


# run-all-tests.sh named the home only for its venv, which is HOME-relative now (the ruling above).
@pytest.mark.parametrize("rel", [f for f in SHELL_FILES if f != "tools/run-all-tests.sh"])
def test_shell_script_reads_engram_home_at_all(rel):
    # the file-level gate's condition, restated here so a mutation shows in both
    assert "ENGRAM_HOME" in (SRC / rel).read_text(encoding="utf-8")


class TestPlantedScan:
    @pytest.mark.parametrize(
        "spelling",
        ["$HOME/.engram/x", "${HOME}/.engram/x", "~/.engram/x", '"$HOME"/.engram/x',
         "${ENGRAM_HOME:-~/.engram}/x", "${ENGRAM_HOME:-$HOME}/.engram/x"],
    )
    def test_each_bare_spelling_in_code_is_flagged(self, spelling):
        assert non_canonical_lines(f'cat "{spelling}"\n')
        assert non_canonical_lines(f"X={spelling}\n")

    def test_canonical_is_not_flagged(self):
        assert non_canonical_lines(f'cat "{_CANONICAL}/x"\n') == []
        assert non_canonical_lines(f'X="${{FOO:-{_CANONICAL}/x}}"\n') == []

    def test_comment_mention_is_not_flagged(self):
        assert non_canonical_lines("# lives in ~/.engram by default\n") == []
        assert non_canonical_lines("    # $HOME/.engram too\n") == []

    def test_canonical_does_not_excuse_a_second_bare_path_on_the_line(self):
        assert len(non_canonical_lines(f'cp "{_CANONICAL}/a" ~/.engram/b\n')) == 1

    def test_bare_env_token_and_typos_are_flagged(self):
        assert non_canonical_lines('ls "$ENGRAM_HOME/x"\n')
        assert non_canonical_lines('ls "${ENGRAM_HOMES:-$HOME/.engram}/x"\n')

    def test_sibling_folder_names_and_labels_are_not_the_home(self):
        assert non_canonical_lines("ls ~/.engram-gemini/x ~/.engram_home com.engram.viz\n") == []

    _GUARD = 'if [ -n "${ENGRAM_HOME-}" ]; then\n    export ENGRAM_HOME\nfi\n'

    def test_the_relative_home_guard_block_is_exempt_and_nothing_else_is(self):
        assert non_canonical_lines("set -u\n" + self._GUARD) == []
        # the line right after the block is scanned again, and line numbers are preserved
        assert non_canonical_lines("set -u\n" + self._GUARD + 'ls "$ENGRAM_HOME/x"\n') == [(5, 'ls "$ENGRAM_HOME/x"')]
        # a lookalike that is not the exact opening line is not exempt
        assert non_canonical_lines('if [ -n "$ENGRAM_HOME" ]; then\n    export ENGRAM_HOME\nfi\n')


# --- the venv rule is the GATE's definition, not a second copy (#3863 6c) ---------------
# Contract (written before the implementation): the shell scan and tools/home_path_gate.py
# classify every venv spelling the same way, because the scan imports the gate's
# definition. Before this, the scan kept its own `_VENV_SPELLING` regex and the two
# disagreed: the ENGRAM_HOME-derived venv passed BOTH, and the absolute foreign venv was
# flagged by the scan and accepted by the gate.

_GATE_PATH = REPO / "tools" / "home_path_gate.py"


def _gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("home_path_gate", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G4_SPELLINGS = [
    "${ENGRAM_HOME:-$HOME/.engram}/venv/bin/python3",
    "${ENGRAM_HOME}/venv/bin/python3",
    "$ENGRAM_HOME/venv/bin/python3",
    '"$ENGRAM_HOME"/venv/bin/python3',
    'engram_home() / "venv"',
    'os.path.join(ENGRAM_HOME, "venv/bin/python3")',
    'Path(os.environ["ENGRAM_HOME"]) / "venv"',
]


@pytest.mark.parametrize("spelling", G4_SPELLINGS)
@pytest.mark.parametrize("shape", ["{s}", 'PY="{s}"', "PY={s}", "X=1 PY={s}"])
def test_the_engram_home_derived_venv_is_flagged_by_the_shell_scan(spelling, shape):
    assert non_canonical_lines(shape.format(s=spelling) + "\n"), (shape, spelling)


@pytest.mark.parametrize(
    "line",
    [
        "PY=~/.engram/venv/bin/python3",
        "PY=$HOME/.engram/venv/bin/python3",
        'PY="$HOME/.engram/venv/bin/python3"',
        "PY=${HOME}/.engram/venv/bin/python3",
        'PY="$HOME"/.engram/venv/bin/python3',
        "PY=$HOME/.engram/venv/bin/python3  # the plugin venv",
    ],
)
def test_the_home_anchored_venv_stays_exempt_in_the_shell_scan(line):
    assert non_canonical_lines(line + "\n") == [], line


@pytest.mark.parametrize(
    "line",
    [
        "PY=/home/someone/.engram/venv/bin/python3",
        'PY="/home/someone/.engram/venv/bin/python3"',
        "/home/someone/.engram/venv/bin/python3",
    ],
)
def test_the_absolute_venv_is_flagged_by_the_shell_scan(line):
    assert non_canonical_lines(line + "\n"), line


# One corpus, two verdict functions: they may not disagree. The corpus holds the
# absolute venv, the HOME-form venv in every anchor spelling (including the two the old
# private regex did not know: `${HOME}` and `"$HOME"`), the near-misses the gate refuses
# (`venvs`, a `..` traversal) and each G4 spelling.
VENV_CORPUS = (
    [
        "PY=/home/someone/.engram/venv/bin/python3",
        '"/home/someone/.engram/venv/bin/python3"',
        "PY=~/.engram/venv/bin/python3",
        "PY=$HOME/.engram/venv/bin/python3",
        "PY=${HOME}/.engram/venv/bin/python3",
        'PY="$HOME"/.engram/venv/bin/python3',
        "cat ~/.engram/venvs/bin/python3",
        "cat ~/.engram/venv-old/bin/python3",
        "cat ~/.engram/venv/../knowledge.db",
        "cat $HOME/.engram/venv/bin/../../knowledge.db",
    ]
    + [f"PY={s}" for s in G4_SPELLINGS]
    + [f'PY="{s}"' for s in G4_SPELLINGS]
)


@pytest.mark.parametrize("line", VENV_CORPUS)
def test_the_shell_scan_and_the_gate_agree_on_every_venv_line(line):
    gate_flags = bool(_gate().line_violations(line))
    shell_flags = bool(non_canonical_lines(line + "\n"))
    assert gate_flags == shell_flags, (line, gate_flags, shell_flags)


# --- (b) behaviour -----------------------------------------------------------------


def _env(**kw):
    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    env.update({k: str(v) for k, v in kw.items()})
    return env


@pytest.mark.parametrize("rel", SHELL_FILES)
def test_bash_syntax(rel):
    r = subprocess.run(["bash", "-n", str(SRC / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _stub_venv(home):
    py = home / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    # the stub says who it is and what it was asked to run, then exits 0
    py.write_text('#!/bin/sh\necho "STUB $0 $1"\n')
    py.chmod(0o755)
    return py


def _launcher_copy(tmp_path):
    """The launcher runs `exec "$VENV_PY" "$SCRIPT_DIR/server.py"`: copy it next
    to nothing so server.py is never reached (the stub ignores its args)."""
    d = tmp_path / "plugin"
    d.mkdir()
    dst = d / "launch-engram-server.sh"
    dst.write_text((SRC / "launch-engram-server.sh").read_text(encoding="utf-8"))
    return dst


class TestLauncher:
    def test_custom_home_with_space_and_quote_does_not_move_the_venv(self, tmp_path):
        """The venv lives at ~/.engram/venv whatever ENGRAM_HOME says (Borges's
        ruling on #3863, README-AGENT.md:187)."""
        home = tmp_path / "my 'custom' home"
        home.mkdir()
        user_home = tmp_path / "O'Brien home"
        py = _stub_venv(user_home / ".engram")
        r = subprocess.run(
            ["bash", str(_launcher_copy(tmp_path))],
            capture_output=True, text=True, env=_env(ENGRAM_HOME=home, HOME=user_home),
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith(f"STUB {py} "), r.stdout

    def test_unset_home_falls_back_to_dot_engram_under_home(self, tmp_path):
        user_home = tmp_path / "O'Brien home"
        py = _stub_venv(user_home / ".engram")
        r = subprocess.run(
            ["bash", str(_launcher_copy(tmp_path))],
            capture_output=True, text=True, env=_env(HOME=user_home),
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith(f"STUB {py} "), r.stdout

    def test_empty_home_variable_counts_as_unset(self, tmp_path):
        user_home = tmp_path / "u"
        py = _stub_venv(user_home / ".engram")
        r = subprocess.run(
            ["bash", str(_launcher_copy(tmp_path))],
            capture_output=True, text=True, env=_env(HOME=user_home, ENGRAM_HOME=""),
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith(f"STUB {py} "), r.stdout

    def test_missing_venv_names_the_home_venv_path_and_exits_1(self, tmp_path):
        home = tmp_path / "empty home"
        home.mkdir()
        r = subprocess.run(
            ["bash", str(_launcher_copy(tmp_path))],
            capture_output=True, text=True, env=_env(ENGRAM_HOME=home, HOME=tmp_path),
        )
        assert r.returncode == 1
        assert f"venv python not found at {tmp_path}/.engram/venv/bin/python3" in r.stderr
        assert "~/.engram/venv" in r.stderr.split("Run Phase 0 setup:")[1]
        assert f"{home}/venv" not in r.stderr  # ENGRAM_HOME does not move the venv


MONITORS = [
    ("tools/forum-mention-monitor.sh", "FORUM_MENTION_SEEN_FILE", "FORUM_MENTION_NO_REAP"),
    ("tools/forum-updates-monitor.sh", "FORUM_UPDATES_CURSOR_FILE", "FORUM_UPDATES_NO_REAP"),
]


class TestMonitorsReadThatConfig:
    """A stub `curl` first on PATH records the arguments of each monitor's
    first poll and fails like an unreachable forum. The URL carries the agent
    name and the forum base the monitor resolved, so it shows WHICH config.json
    was read (the monitor is then killed; no real forum is contacted)."""

    @staticmethod
    def _first_curl_url(tmp_path, rel, env):
        import time

        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "curl.log"
        curl = bindir / "curl"
        curl.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 22\n')
        curl.chmod(0o755)
        env = dict(env, PATH=f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}")
        p = subprocess.Popen(
            ["bash", str(SRC / rel)], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not (log.exists() and log.read_text()):
                time.sleep(0.1)
            return log.read_text() if log.exists() else ""
        finally:
            p.terminate()
            p.wait(timeout=10)

    @staticmethod
    def _config(home, agent, port):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            '{"agent_name": "%s", "forum": {"url": "http://127.0.0.1:%d"}}' % (agent, port)
        )

    @pytest.mark.parametrize("rel,state_var,noreap", MONITORS)
    def test_env_home_config_is_the_one_read(self, tmp_path, rel, state_var, noreap):
        # identity only in the custom home; HOME's own .engram holds a decoy
        # with another agent name and forum port. The home name carries a space,
        # single AND double quote and a backslash: a path spliced into the
        # monitors' python3 -c snippet (inside a double-quoted literal) is
        # broken only by the double quote / backslash, which leaves SELF empty,
        # so the agent name asserted in the first poll URL goes missing.
        custom = tmp_path / "my \"q\" 'custom' \\home"
        self._config(custom, "viaenv", 9)
        user_home = tmp_path / "uh"
        self._config(user_home / ".engram", "decoy", 8)
        env = _env(ENGRAM_HOME=custom, HOME=user_home,
                   **{noreap: "1", state_var: tmp_path / "state"})
        url = self._first_curl_url(tmp_path, rel, env)
        assert "viaenv" in url and "127.0.0.1:9" in url, url
        assert "decoy" not in url and "127.0.0.1:8" not in url, url

    @pytest.mark.parametrize("rel,state_var,noreap", MONITORS)
    def test_unset_home_reads_dot_engram_under_home(self, tmp_path, rel, state_var, noreap):
        user_home = tmp_path / "O'Brien uh"
        self._config(user_home / ".engram", "viahome", 9)
        env = _env(HOME=user_home, **{noreap: "1", state_var: tmp_path / "state"})
        url = self._first_curl_url(tmp_path, rel, env)
        assert "viahome" in url and "127.0.0.1:9" in url, url


def _assignment(rel, var):
    """The text of the first top-level `VAR=...` line of a script."""
    for line in (SRC / rel).read_text(encoding="utf-8").splitlines():
        if line.startswith(var + "="):
            return line
    raise AssertionError(f"{rel}: no top-level {var}= line")


def _eval_assignment(rel, var, env):
    r = subprocess.run(
        ["bash", "-c", f'{_assignment(rel, var)}\nprintf %s "${var}"'],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


class TestScriptPathVariables:
    @pytest.mark.parametrize(
        "rel,var,tail",
        [
            ("tools/ghpr.sh", "LOG_DIR", "ci-cost-sim"),
        ],
    )
    def test_custom_home_with_space_and_quote(self, tmp_path, rel, var, tail):
        home = tmp_path / "my 'custom' home"
        got = _eval_assignment(rel, var, _env(ENGRAM_HOME=home, HOME=tmp_path / "x"))
        assert got == f"{home}/{tail}"

    @pytest.mark.parametrize(
        "rel,var",
        [("tools/ghpr.sh", "PYTHON"), ("tools/run-all-tests.sh", "ENGRAM_PY")],
    )
    def test_the_venv_python_is_under_home_whatever_engram_home_is(self, tmp_path, rel, var):
        """Borges' ruling on #3863: the venv is always $HOME/.engram/venv."""
        home = tmp_path / "my 'custom' home"
        got = _eval_assignment(rel, var, _env(ENGRAM_HOME=home, HOME=tmp_path / "x"))
        assert got == f"{tmp_path}/x/.engram/venv/bin/python"

    @pytest.mark.parametrize(
        "rel,var,tail",
        [
            ("tools/ghpr.sh", "LOG_DIR", "ci-cost-sim"),
            ("tools/ghpr.sh", "PYTHON", "venv/bin/python"),
            ("tools/run-all-tests.sh", "ENGRAM_PY", "venv/bin/python"),
        ],
    )
    def test_unset_home_is_dot_engram_under_home(self, tmp_path, rel, var, tail):
        uh = tmp_path / "O'Brien"
        got = _eval_assignment(rel, var, _env(HOME=uh))
        assert got == f"{uh}/.engram/{tail}"

    def test_explicit_override_variables_still_win(self, tmp_path):
        env = _env(ENGRAM_HOME=tmp_path / "h", GHPR_PYTHON="/x/py", ENGRAM_PY="/y/py")
        assert _eval_assignment("tools/ghpr.sh", "PYTHON", env) == "/x/py"
        assert _eval_assignment("tools/run-all-tests.sh", "ENGRAM_PY", env) == "/y/py"


# --- (c) docstring-only files ------------------------------------------------------


def _docstring_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def engram_dir_strings_outside_docstrings(source):
    """[(lineno, string)] of string constants, docstrings excluded, holding
    `.engram` as a folder name."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and _ENGRAM_DIR.search(node.value)
        ):
            out.append((node.lineno, node.value[:80]))
    return out


@pytest.mark.parametrize("rel", CORE_FILES)
def test_core_file_names_engram_home(rel):
    assert "ENGRAM_HOME" in (SRC / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", CORE_FILES)
def test_core_file_has_no_dot_engram_string_outside_a_docstring(rel):
    bad = engram_dir_strings_outside_docstrings((SRC / rel).read_text(encoding="utf-8"))
    assert not bad, f"{rel}: code string(s) hold `.engram` (#3863): {bad}"


class TestPlantedDocstringScan:
    def test_code_string_is_flagged(self):
        assert engram_dir_strings_outside_docstrings('x = "~/.engram/y"\n')
        assert engram_dir_strings_outside_docstrings("def f():\n    return '$HOME/.engram'\n")

    def test_docstrings_are_not_flagged(self):
        src = '"""mod ~/.engram"""\nclass C:\n    """c ~/.engram"""\n    def f(self):\n        """f ~/.engram"""\n'
        assert engram_dir_strings_outside_docstrings(src) == []

    def test_a_second_string_statement_is_not_a_docstring(self):
        assert engram_dir_strings_outside_docstrings('"""doc"""\n"~/.engram"\n')

    def test_sibling_names_are_not_flagged(self):
        assert engram_dir_strings_outside_docstrings('x = "com.engram.viz ~/.engram-x"\n') == []
