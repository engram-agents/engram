"""#4313 contract: no command line in the sleep skill / dream-agent specs starts a repo tool with a bare
`python3`. On a Mac, bare `python3` is Apple's 3.9, and `engramd_client` needs 3.10+, so the owner-read CLIs
(`reconcile_lesson_demotions.py`, `dream_master_batch.py`) fail. The specs must name the venv interpreter.

Contract (stated before the edit):
  for each spec below, every place that starts one of the two tools with an interpreter names exactly
  `"$HOME/.engram/venv/bin/python3"` as that interpreter, never a bare `python3`; and each spec has at least
  the expected number of such invocations (so a deleted command cannot pass vacuously).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "src" / "engram"
VENV = '"$HOME/.engram/venv/bin/python3"'

# spec -> (tool, minimum number of command-line invocations)
SPECS = {
    "skills/claude/engram-sleep/SKILL.md": ("reconcile_lesson_demotions.py", 1),
    "agents/claude/engram-dream-master.md": ("dream_master_batch.py", 3),
    "agents/claude/engram-dream-fairy.md": ("dream_master_batch.py", 1),
}

# An invocation = an interpreter token (ending in python3, optionally closing-quoted) followed by the tool path
# on the same line, the path being quoted or not. Prose mentions of the tool file name ("from
# `tools/dream_master_batch.py`") have no interpreter right before them and are not matched.
_INVOKE = re.compile(r'(\S*python3"?)\s+"?[^\s"]*(?:/|\b)(?:reconcile_lesson_demotions|dream_master_batch)\.py'
                     r'|(\S*python3"?)\s+"[^"\n]*(?:reconcile_lesson_demotions|dream_master_batch)\.py')


def _interpreters(text: str, tool: str) -> list[str]:
    found = []
    for line in text.splitlines():
        if tool not in line:
            continue
        for m in _INVOKE.finditer(line):
            if tool in m.group(0):
                found.append((m.group(1) or m.group(2)).lstrip("`"))
    return found


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_every_tool_command_names_the_venv_interpreter(spec):
    tool, minimum = SPECS[spec]
    text = (ROOT / spec).read_text(encoding="utf-8")
    interps = _interpreters(text, tool)
    assert len(interps) >= minimum, (spec, interps)
    assert all(i == VENV for i in interps), (spec, [i for i in interps if i != VENV])


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_no_command_line_begins_with_bare_python3_for_these_tools(spec):
    tool, _ = SPECS[spec]
    text = (ROOT / spec).read_text(encoding="utf-8")
    bad = [ln for ln in text.splitlines() if tool in ln and re.match(r"\s*python3\s", ln)]
    assert not bad, bad


# ---- round 2: the path form, then the tree-wide class guard --------------------------------------------

_TOOL_PATH_ARG = re.compile(
    r'python3"?\s+(?P<path>[^\s"]*(?:reconcile_lesson_demotions|dream_master_batch)\.py)'
    r'|python3"?\s+"(?P<qpath>[^"\n]*(?:reconcile_lesson_demotions|dream_master_batch)\.py)')


def _tool_path_args(text: str, tool: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if tool in line:
            for m in _TOOL_PATH_ARG.finditer(line):
                out.append(m.group("path") or m.group("qpath"))
    return out


def _path_is_safe(path: str) -> bool:
    """Not variable-rooted, or rooted in `${CLAUDE_PLUGIN_ROOT:-...}` with a fallback. A bare
    `$CLAUDE_PLUGIN_ROOT/...` expands to `/tools/...` in an agent's Bash, where the variable is unset."""
    if "CLAUDE_PLUGIN_ROOT" not in path:
        return True
    return "${CLAUDE_PLUGIN_ROOT:-" in path


def test_path_form_self_test():
    assert not _path_is_safe("$CLAUDE_PLUGIN_ROOT/tools/reconcile_lesson_demotions.py")
    assert not _path_is_safe("${CLAUDE_PLUGIN_ROOT}/tools/reconcile_lesson_demotions.py")
    assert _path_is_safe("${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/x.py")
    assert _path_is_safe("tools/x.py")


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_every_tool_command_path_has_a_fallback_when_variable_rooted(spec):
    tool, minimum = SPECS[spec]
    text = (ROOT / spec).read_text(encoding="utf-8")
    paths = _tool_path_args(text, tool)
    assert len(paths) >= minimum, (spec, paths)
    assert all(_path_is_safe(p) for p in paths), (spec, [p for p in paths if not _path_is_safe(p)])


# Tree-wide: no command line in shipped skills/agents starts a repo tool or module with a bare `python3`.
# "Repo tool" = an argument that is a `.py` path, or `-m <module>` with a module not allowlisted below.
# NOT flagged by construction: `python3 -c ...`, `python3 -` (stdin), and `$HOME/.engram/venv/bin/python3`
# (the `/` before `python3` keeps it out of the match).
ALLOWED_MODULES = {
    "venv": "creates the venv, so it must run under the system python (first-session Phase 0)",
    "json.tool": "stdlib pretty-printer, no repo import",
}
# Round 3: `python` (no 3) is bare too (no `python` on a stock Mac), and so is a quoted `"python3"`.
_BARE = re.compile(r'(?<![\w/."$-])(?:"python3?"|python3?)\s+(?:-m\s+(?P<mod>[\w.]+)|"?(?P<py>[^\s`]*\.py)\b)')


def bare_python3_tool_lines(text: str) -> list[str]:
    bad = []
    for line in text.splitlines():
        for m in _BARE.finditer(line):
            if m.group("mod") and m.group("mod") in ALLOWED_MODULES:
                continue
            bad.append(line.strip()[:160])
            break
    return bad


def test_guard_self_test_it_can_fail_and_it_can_pass():
    assert bare_python3_tool_lines('python3 "$X/tools/foo.py" --a')
    assert bare_python3_tool_lines("run `python3 -m tools.cohort_dispatch prepare` now")
    assert bare_python3_tool_lines("python3 tools/selftest.py --smoke")
    assert not bare_python3_tool_lines('"$HOME/.engram/venv/bin/python3" "$X/tools/foo.py"')
    assert not bare_python3_tool_lines('"$HOME/.engram/venv/bin/python3" -m tools.cohort_dispatch prepare')
    assert not bare_python3_tool_lines("echo x | python3 -m json.tool")          # allowlisted module
    assert not bare_python3_tool_lines("python3 -m venv ~/.engram/venv")         # allowlisted module
    assert not bare_python3_tool_lines('python3 -c "import json"')               # stdlib one-liner
    assert not bare_python3_tool_lines("python3 - <<'EOF'")                      # stdin
    # round 3: bare `python` and a quoted `"python3"` are flagged; the allowlisted forms of `python` are not
    assert bare_python3_tool_lines("python tools/x.py --apply")
    assert bare_python3_tool_lines('python "$SOURCE_DIR/tools/x.py" \\')
    assert bare_python3_tool_lines('"python3" tools/x.py')
    assert bare_python3_tool_lines('"python" -m tools.cohort_dispatch prepare')
    assert not bare_python3_tool_lines("echo x | python -m json.tool")
    assert not bare_python3_tool_lines("python -m venv ~/.engram/venv")
    assert not bare_python3_tool_lines('python -c "import json"')
    assert not bare_python3_tool_lines('PY=python3')  # the deliberate first-session fallback: no repo tool follows


def test_no_shipped_skill_or_agent_command_starts_a_repo_tool_with_bare_python3():
    offenders = {}
    for md in sorted(list((ROOT / "skills").rglob("*.md")) + list((ROOT / "agents").rglob("*.md"))):
        bad = bare_python3_tool_lines(md.read_text(encoding="utf-8"))
        if bad:
            offenders[str(md.relative_to(ROOT))] = bad
    assert not offenders, "\n" + "\n".join(f"{k}: {len(v)}  e.g. {v[0]}" for k, v in offenders.items())


# ---- round 2b: bare `$CLAUDE_PLUGIN_ROOT/...` as a path root (the variable is unset in an agent's Bash) ----
# Round 3: also a bare variable used as a command ARGUMENT with no trailing slash (`ls $CLAUDE_PLUGIN_ROOT`).
# Not matched: `${CLAUDE_PLUGIN_ROOT:-...}` / `:?` / `:+` forms, and assignments (`X=$CLAUDE_PLUGIN_ROOT`).
_BARE_VAR_ROOT = re.compile(r'(?<![=\w])\$(?:\{CLAUDE_PLUGIN_ROOT\}|CLAUDE_PLUGIN_ROOT(?![\w:]))')

# (file suffix, substring of the line, reason). Everything else that matches is a defect.
ALLOWED_BARE_VAR = [
    ("engram-first-session/SKILL.md", "${CLAUDE_PLUGIN_ROOT:+",
     "guarded assignment: the `:+` form is empty when the variable is unset, and the next line handles empty"),
    ("engram-first-session/SKILL.md", "invokes a launcher script",
     "prose describing what the plugin's .mcp.json runs (hook/MCP context, where the variable is set)"),
    ("engram-collaborating-loop/SKILL.md", "grep -rln '<script-basename>'",
     "follows a `: \"${CLAUDE_PLUGIN_ROOT:?...}\"` fail-loud guard line, so it never expands empty"),
    ("engram-dream-master.md", "sys.path.insert(0, '$CLAUDE_PLUGIN_ROOT')",
     "a python string inside the block whose first line is the `: \"${CLAUDE_PLUGIN_ROOT:?...}\"` fail-loud guard"),
]
# Note on engram-loop `engram-loop-gate.py` (lines ~241/242): its path root is the fallback form; its INTERPRETER is
# deliberately untouched (a direct-exec via shebang, not a `python3` line). One measured path only: under Apple's
# /usr/bin/python3 (3.9.6) `--json` printed {"decision": "proceed", ..., "reason": "not-engaged"}, exit 0 (not-engaged
# path); nothing is claimed about the engaged path.


def bare_var_root_lines(text: str, fname: str = "") -> list[str]:
    bad = []
    for line in text.splitlines():
        if not _BARE_VAR_ROOT.search(line):
            continue
        if any(fname.endswith(f) and sub in line for f, sub, _ in ALLOWED_BARE_VAR):
            continue
        bad.append(line.strip()[:160])
    return bad


def test_bare_var_guard_self_test_both_shapes():
    # shape 1: bare variable root -> flagged (quoted, braced, and quote-closed-before-slash forms)
    assert bare_var_root_lines('python3 "$CLAUDE_PLUGIN_ROOT/tools/x.py"')
    assert bare_var_root_lines('x "${CLAUDE_PLUGIN_ROOT}/tools/x.py"')
    assert bare_var_root_lines('grep -r y "$CLAUDE_PLUGIN_ROOT"/skills')
    # round 3: no trailing slash (argument form) is flagged; assignment forms are not
    assert bare_var_root_lines("ls $CLAUDE_PLUGIN_ROOT")
    assert bare_var_root_lines('cd "${CLAUDE_PLUGIN_ROOT}" && ls')
    assert not bare_var_root_lines('ROOT=$CLAUDE_PLUGIN_ROOT')
    assert not bare_var_root_lines('export P="${CLAUDE_PLUGIN_ROOT:-/x}"')
    # shape 2: fallback / guard / assignment forms -> not flagged
    assert not bare_var_root_lines('x "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/x.py"')
    assert not bare_var_root_lines(': "${CLAUDE_PLUGIN_ROOT:?not set}"')
    # a bare variable inside quotes is now flagged too, unless the line is allowlisted with its reason
    assert bare_var_root_lines("sys.path.insert(0, '$CLAUDE_PLUGIN_ROOT')", "agents/claude/engram-loop.md")
    assert not bare_var_root_lines("sys.path.insert(0, '$CLAUDE_PLUGIN_ROOT')", "agents/claude/engram-dream-master.md")
    # an allowlisted line is not flagged, the same text in another file is
    assert not bare_var_root_lines('BOOTSTRAP="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bootstrap.py}"',
                                   "skills/claude/engram-first-session/SKILL.md")
    assert bare_var_root_lines('BOOTSTRAP="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bootstrap.py}"',
                               "skills/claude/engram-loop/SKILL.md")


def test_no_shipped_skill_or_agent_line_uses_bare_plugin_root_variable_as_a_path_root():
    offenders = {}
    for md in sorted(list((ROOT / "skills").rglob("*.md")) + list((ROOT / "agents").rglob("*.md"))):
        bad = bare_var_root_lines(md.read_text(encoding="utf-8"), str(md))
        if bad:
            offenders[str(md.relative_to(ROOT))] = bad
    assert not offenders, "\n" + "\n".join(f"{k}: {len(v)}  e.g. {v[0]}" for k, v in offenders.items())
