"""Run a fenced shell block from a shipped SKILL.md exactly as printed [part of #3893].

A skill's marker-writing command is only right if the SHELL produces the file it
promises, so a test that checks it must run it, not read its text with a regex (a bare
double quote in the printed text, such as a quote break-out around `"${ENGRAM_HOME:-...}"`,
ends a regex match early and reads as a different document than the shell writes).

This module is the small shared place two test files import without importing each
other. `tests/test_prose_home_sweep_3863.py` carries its own earlier copies of these
three helpers; folding them into this module is a follow-up once the open change to
that file's `run_block` has landed.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap

_FENCE = re.compile(r"^\s*(```|~~~)")


def fenced_blocks(text):
    """[(first_lineno, dedented text)] of every fenced block in `text`."""
    out, cur, start = [], None, 0
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            if cur is None:
                cur, start = [], n + 1
            else:
                out.append((start, textwrap.dedent("\n".join(cur)) + "\n"))
                cur = None
        elif cur is not None:
            cur.append(line)
    return out


def block_where(text, predicate, label="text"):
    """The one fenced block of `text` for which predicate(block_text) holds."""
    found = [b for _, b in fenced_blocks(text) if predicate(b)]
    assert len(found) == 1, f"{label}: {len(found)} fenced blocks match, expected 1"
    return found[0]


def run_block(script, tmp_path, mode="default"):
    """Run `script` with bash under a temp HOME and cwd.

    mode "default": ENGRAM_HOME unset, the home is $HOME/.engram.
    mode "custom-with-space": ENGRAM_HOME set to a path holding a space.
    -> (CompletedProcess, home Path). Fails if anything is written outside the home
    (a literal `${...}` directory, a stray temp file).
    """
    user_home = tmp_path / "user"
    cwd = tmp_path / "cwd"
    user_home.mkdir()
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT")}
    env["HOME"] = str(user_home)
    if mode == "default":
        home = user_home / ".engram"
    else:
        home = tmp_path / "my home" / "eg"
        env["ENGRAM_HOME"] = str(home)
    home.mkdir(parents=True)
    r = subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60)
    assert os.listdir(cwd) == [], f"stray files in cwd: {os.listdir(cwd)}"
    assert sorted(os.listdir(user_home)) == ([".engram"] if mode == "default" else []), os.listdir(user_home)
    return r, home
