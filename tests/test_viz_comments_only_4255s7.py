"""#4255 slice 7: the viz rewording is comments and docstrings ONLY.

Contract (written before the rewording; the cells below are the stated behaviours):

- A. For every ``src/engram/viz`` file this branch changed against its base (the merge-base with
  ``origin/dev``), the file means the same thing before and after:
  (1) a ``.py`` file has the same AST once docstrings and the literal text of argparse ``help=``
      keywords are removed (a docstring is a bare string
      expression statement at the head of a module, class or function body);
  (2) a ``.js`` file is identical once ``//`` and ``/* */`` comments are removed and whitespace is
      collapsed (a small tokenizer that knows strings, template literals and regex literals).
- B. The checker itself discriminates: code changes (a changed constant, a renamed key, a changed
  localStorage key, an added statement) are reported; a reworded comment or docstring is not.
- C. Only a comment or docstring may change: so the diff for any other viz file type is reported.

Limits stated plainly: with no ``origin/dev`` to compare to the cell skips (it is a per-PR check, and
once the branch is merged the diff is empty and it passes trivially).  The JS tokenizer is a
heuristic: a regex literal it mis-reads could hide a change.  It is a second pair of eyes beside the
diff reading, not a proof.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIZ = "src/engram/viz"


def strip_docstrings_ast(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    # an argparse help= string is user-visible words (Ariadne, #4255 slice 7): blank its LITERAL text only.
    # f-string expressions, every other keyword, string and default stay code.
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "help":
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                node.value = ast.Constant(value="")
            elif isinstance(v, ast.JoinedStr):
                node.value = ast.JoinedStr(values=[x for x in v.values if isinstance(x, ast.FormattedValue)])
    return ast.dump(tree, include_attributes=False)


def strip_js_comments(src: str) -> str:
    """Remove // and /* */ comments, keeping strings, template literals and regex literals; collapse whitespace
    OUTSIDE those literals only (the amount of whitespace inside a string is part of the string)."""
    out = []
    lits = []  # literals are set aside behind a placeholder so the whitespace collapse never touches them
    i, n = 0, len(src)
    prev_sig = ""  # last significant (non-space, non-comment) char, to tell a regex from a division
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        if c in "'\"`":
            q, j = c, i + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            out.append("\x00")
            lits.append(src[i:j + 1])
            i, prev_sig = j + 1, q
            continue
        if c == "/" and (prev_sig == "" or prev_sig in "(,=:[!&|?{};+-*%<>~^"):
            j, in_class = i + 1, False
            while j < n and (src[j] != "/" or in_class) and src[j] != "\n":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            out.append("\x00")
            lits.append(src[i:j + 1])
            i, prev_sig = j + 1, "/"
            continue
        out.append(c)
        if not c.isspace():
            prev_sig = c
        i += 1
    parts = re.sub(r"\s+", " ", "".join(out)).strip().split("\x00")
    return parts[0] + "".join(lit + part for lit, part in zip(lits, parts[1:]))


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _base():
    r = _git("merge-base", "HEAD", "origin/dev")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _changed_viz_files(base):
    r = _git("diff", "--name-only", base, "--", VIZ)
    return [p for p in r.stdout.splitlines() if p]


def _at(base, rel):
    r = _git("show", f"{base}:{rel}")
    return r.stdout if r.returncode == 0 else None


def semantic_change(rel, before, after):
    """Return a description if `after` means something different from `before` (None when the same)."""
    if rel.endswith(".py"):
        return None if strip_docstrings_ast(before) == strip_docstrings_ast(after) else "AST differs beyond docstrings"
    if rel.endswith(".js"):
        return None if strip_js_comments(before) == strip_js_comments(after) else "code differs beyond comments"
    return "not a .py or .js file: not a comment-only change"


# ============================================================================
# A. the real diff
# ============================================================================
_BASE = _base()
_FILES = _changed_viz_files(_BASE) if _BASE else []


@pytest.mark.skipif(_BASE is None, reason="no origin/dev to compare against")
@pytest.mark.parametrize("rel", _FILES or [pytest.param(None, marks=pytest.mark.skip(reason="no viz file changed"))])
def test_a_changed_viz_file_differs_only_in_comments_and_docstrings(rel):
    before = _at(_BASE, rel)
    assert before is not None, f"{rel} is new on this branch; a comment-only slice adds no viz file"
    after = (ROOT / rel).read_text(encoding="utf-8")
    assert semantic_change(rel, before, after) is None, f"{rel}: {semantic_change(rel, before, after)}"


# ============================================================================
# B, C. the checker discriminates
# ============================================================================
PY = '"""Module doc about a multi-agent config."""\nKEY = "engram-viz-lastvisit-"\n\n\ndef f(x):\n    """doc"""\n    # a comment\n    return x + 1\n'
JS = "// a comment\nvar k = 'engram-viz-lastvisit-' + name; /* c */\nif (a / b > 1) { go(/re[/]x/g); }\n"


def test_b_py_reworded_docstring_and_comment_is_not_a_change():
    after = PY.replace("about a multi-agent config", "about several agents' graphs").replace("# a comment", "# other words")
    assert semantic_change("a.py", PY, after) is None


@pytest.mark.parametrize("mutant", [
    PY.replace('"engram-viz-lastvisit-"', '"engram-viz-lastvisit2-"'),
    PY.replace("x + 1", "x + 2"),
    PY.replace("def f(x):", "def g(x):"),
    PY + "z = 1\n",
    PY.replace('    """doc"""\n', '    """doc"""\n    x = 0\n'),
])
def test_b_py_code_change_is_reported(mutant):
    assert semantic_change("a.py", PY, mutant) is not None


def test_b_py_a_string_that_is_not_a_docstring_is_code():
    assert semantic_change("a.py", "x = 1\n'not a doc'\n", "x = 1\n'changed'\n") is not None


def test_b_js_reworded_comments_are_not_a_change():
    after = JS.replace("// a comment", "// another remark, even with 'quotes' and // slashes").replace("/* c */", "/* more words */")
    assert semantic_change("a.js", JS, after) is None


@pytest.mark.parametrize("mutant", [
    JS.replace("engram-viz-lastvisit-", "engram-viz-lastvisit2-"),
    JS.replace("a / b > 1", "a / b > 2"),
    JS.replace("go(", "stop("),
    JS.replace("/re[/]x/g", "/re[/]y/g"),
    JS + "extra();\n",
])
def test_b_js_code_change_is_reported(mutant):
    assert semantic_change("a.js", JS, mutant) is not None


def test_b_js_a_comment_marker_inside_a_string_is_not_a_comment():
    a = "var u = 'http://x'; // c\n"
    assert semantic_change("a.js", a, a.replace("http://x", "http://y")) is not None


def test_c_other_file_types_are_reported():
    assert semantic_change("a.html", "x", "x") is not None


# ============================================================================
# D. widened (Ariadne, #4255 slice 7): an argparse ``help=`` string is user-visible words, not code.
#    Only the literal text of a ``help=`` keyword may change; every other string, key, default and
#    f-string expression still counts as code.
# ============================================================================
ARGP = (
    "import argparse\n"
    "DB = 'x'\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--db', default=None,\n"
    "               help='Single-agent mode: register one anonymous '\n"
    "                    f'default agent at {DB}')\n"
    "p.add_argument('--port', default='5001', help='Port to listen on')\n"
)


def test_d_a_reworded_help_string_is_not_a_change():
    after = ARGP.replace("Single-agent mode: register", "Show one graph: register").replace("Port to listen on", "TCP port")
    assert semantic_change("a.py", ARGP, after) is None


@pytest.mark.parametrize("mutant", [
    ARGP.replace("default=None", "default='/tmp/x'"),
    ARGP.replace("'--db'", "'--database'"),
    ARGP.replace("{DB}", "{DB}{DB}"),
    ARGP.replace("default='5001'", "default='5002'"),
    ARGP.replace("help='Port to listen on'", "metavar='Port to listen on'"),
    ARGP.replace("help='Port to listen on'", "help=str(1)"),
    ARGP + "p.add_argument('--x', default='y')\n",
])
def test_d_anything_but_the_help_text_is_still_a_change(mutant):
    assert semantic_change("a.py", ARGP, mutant) is not None


def test_d_a_help_that_is_not_a_string_literal_is_code():
    base = ARGP.replace("help='Port to listen on'", "help=make_help()")
    assert semantic_change("a.py", base, base.replace("make_help()", "other_help()")) is not None


def test_b_js_whitespace_inside_a_string_literal_is_code():
    """Only the AMOUNT of whitespace changes inside the literal: 'a  b' against 'a b' is a different string."""
    a = "var k = 'a b';\n"
    assert semantic_change("a.js", a, a.replace("'a b'", "'a  b'")) is not None
    t = "var t = `x\ty`;\n"
    assert semantic_change("a.js", t, t.replace("\t", " ")) is not None


def test_b_js_whitespace_outside_strings_and_comments_still_does_not_count():
    a = "var k = 'a b';\nif (x) { go(); }\n"
    assert semantic_change("a.js", a, a.replace("var k =", "var   k  =").replace("{ go(); }", "{\n  go();\n}")) is None
