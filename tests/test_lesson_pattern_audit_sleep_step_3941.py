"""engram-alpha#3941 -- engram-sleep runs `lesson-pattern-audit.py --write-ranks` right after Step 3.9.

The step's shell block is extracted from the skill and RUN (the approach of tests/test_regen_embeddings_owner_3879.py),
so a status branch that the text only describes cannot go missing unnoticed.  Contract:

  exit 0                      -> `✓ lesson ranks written`
  exit 0 + "rank file left as is" in the tool's stdout (no transcripts) -> `✓ no transcripts found, rank file left as is`
  exit 0 + "no tool-call targets in the transcripts" / "no tripwire hook installed" -> their own `✓ ..., rank file left as is` lines
  ANY non-zero exit           ->`⚠ lesson-ranks FAILED (exit N): <last stderr line>`, loud, never skipped
  the stderr mktemp file is removed on every branch; the block itself always exits 0 (a failure never aborts sleep)
  the history line is `- lesson-fire ranks: $status`; the skill says the tool is read-only on the graph and
  the transcripts and writes only the one file.

Every cell names its mutation in the docstring.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SLEEP_SKILL = REPO_ROOT / "src" / "engram" / "skills" / "claude" / "engram-sleep" / "SKILL.md"
TOOL_TOKEN = '"$CLAUDE_PLUGIN_ROOT/tools/lesson-pattern-audit.py"'


def _skill() -> str:
    return SLEEP_SKILL.read_text(encoding="utf-8")


def _section() -> str:
    text = _skill()
    start = text.index("### Step 3.95")
    end = text.index("\n### ", start + 5)
    return text[start:end]


def step_block() -> str:
    sec = _section()
    fence = sec.index("```bash")
    body = sec.index("\n", fence) + 1
    return sec[body:sec.index("```", body)]


def test_the_step_sits_right_after_step_3_9_and_before_step_4():
    text = _skill()
    order = [text.index(h) for h in ("### Step 3.9 ", "### Step 3.95 ", "### Step 4 ")]
    assert order == sorted(order) and len(set(order)) == 3


def test_the_skill_states_the_scope_and_the_history_line_shape():
    """Mutation: drop the read-only sentence, or the history template -> red."""
    sec = _section()
    assert "read-only on the graph and on the transcripts" in sec
    assert "writes only the one rank file" in sec
    assert "- lesson-fire ranks: $lesson_ranks_status" in sec
    assert "a failure never aborts sleep" in sec


def test_the_test_map_names_this_file():
    import json
    maps = json.loads((REPO_ROOT / "src" / "engram" / "packaging" / "test-map.json").read_text())["mappings"]
    entry = [e for e in maps if e.get("path_prefix_or_glob") == "tools/lesson-pattern-audit.py"]
    assert len(entry) == 1 and "tests/test_lesson_pattern_audit_sleep_step_3941.py" in entry[0]["test_globs"]


def test_the_invocation_is_the_venv_python_the_plugin_tool_and_write_ranks():
    block = step_block()
    assert "venv/bin/python3" in block and TOOL_TOKEN in block and "--write-ranks" in block
    assert "mktemp" in block


def test_the_failed_branch_text_is_present():
    """Mutation: remove the FAILED branch text -> red."""
    assert "⚠ lesson-ranks FAILED (exit $lesson_ranks_rc): $lesson_ranks_last_line" in step_block()


def _run(tmp: Path, tool_body: str) -> tuple[str, list]:
    """Run the block with a stand-in for the tool; return ($status, leftover temp files)."""
    login = tmp / "login"
    venv = login / ".engram" / "venv" / "bin" / "python3"
    venv.parent.mkdir(parents=True)
    venv.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    venv.chmod(venv.stat().st_mode | stat.S_IXUSR)
    root = tmp / "root"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "lesson-pattern-audit.py").write_text(tool_body)
    scratch = tmp / "scratch"
    scratch.mkdir()
    env = dict(os.environ, HOME=str(login), CLAUDE_PLUGIN_ROOT=str(root), TMPDIR=str(scratch))
    env.pop("ENGRAM_HOME", None)
    done = subprocess.run(["bash", "-c", step_block() + '\nprintf "%s" "$lesson_ranks_status"'],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout, sorted(p.name for p in scratch.iterdir())


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory(prefix="lpsleep-") as d:
        yield Path(d)


def test_exit_0_is_written(tmp):
    """Mutation: record the success line regardless of the exit code -> the failure cells go red."""
    status, left = _run(tmp, "import sys\nsys.stdout.write('wrote 3 lesson rates over 4 targets to /x\\n')\n")
    assert status == "✓ lesson ranks written" and left == []


def test_exit_0_with_no_transcripts_says_so(tmp):
    status, left = _run(tmp, "import sys\nsys.stdout.write('no transcripts found; rank file left as is\\n')\n")
    assert status == "✓ no transcripts found, rank file left as is" and left == []


@pytest.mark.parametrize("rc", [1, 2, 3])
def test_any_non_zero_exit_is_loud_with_the_last_stderr_line(tmp, rc):
    """Mutation: treat exit 2 (or any non-zero) as success -> '✓' appears -> red; skip silently -> red."""
    status, left = _run(tmp, f"import sys\nsys.stderr.write('first\\nthe reason\\n')\nsys.exit({rc})\n")
    assert status == f"⚠ lesson-ranks FAILED (exit {rc}): the reason", status
    assert left == [], "the stderr mktemp file was not removed"


def test_a_traceback_tool_failure_is_recorded_not_fatal(tmp):
    status, left = _run(tmp, "raise RuntimeError('boom')\n")
    assert status.startswith("⚠ lesson-ranks FAILED (exit 1): ") and "boom" in status and left == []


# ---- round 1: distinct status lines, the temp file under TMPDIR, and the real tool through the block --------

def test_exit_0_with_no_tool_call_targets_records_its_own_line(tmp):
    """The two zero-input messages are distinct phrases and each gets its own line.  Mutation: the case
    pattern back to `*"no transcripts found"*` (which the old no-targets message also matched) -> red."""
    status, left = _run(tmp, "import sys\nsys.stdout.write("
                             "'no tool-call targets in the transcripts (3 read); rank file left as is\\n')\n")
    assert status == "✓ no tool-call targets in the transcripts, rank file left as is" and left == []


def test_exit_0_with_no_tripwire_hook_records_its_own_line(tmp):
    status, left = _run(tmp, "import sys\nsys.stdout.write('no tripwire hook installed; rank file left as is\\n')\n")
    assert status == "✓ no tripwire hook installed, rank file left as is" and left == []


def test_the_three_tool_messages_are_distinct_phrases_the_block_keys_on():
    """The block keys on one phrase per message; no phrase may be inside another message."""
    block = step_block()
    phrases = ("no transcripts found", "no tool-call targets in the transcripts", "no tripwire hook installed")
    for ph in phrases:
        assert f'*"{ph}"*' in block
    for a in phrases:
        assert [b for b in phrases if b != a and a in b] == []


def test_the_stderr_file_is_made_under_tmpdir_not_a_bare_mktemp():
    """A bare `mktemp` ignores TMPDIR on macOS, which made the removal cells vacuous there."""
    block = step_block()
    assert 'mktemp "${TMPDIR:-$lesson_ranks_tmp}/lesson-ranks-XXXXXX"' in block
    assert "$(mktemp)" not in block


SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_NAME = "engram-lesson-tripwire-hook.py"


def _run_real(tmp: Path, hook: str = "real", engram_home: str = "ok"):
    """Run the block with the REAL tool in a flattened layout.  hook: real | absent | raises."""
    import json
    import shutil
    import sqlite3
    login = tmp / "login"
    venv = login / ".engram" / "venv" / "bin" / "python3"
    venv.parent.mkdir(parents=True)
    venv.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    venv.chmod(venv.stat().st_mode | stat.S_IXUSR)
    root = tmp / "root"
    (root / "tools").mkdir(parents=True)
    (root / "hooks").mkdir()
    for name in ("lesson-pattern-audit.py", "_owner_read.py", "__init__.py"):
        shutil.copyfile(SRC_ENGRAM / "tools" / name, root / "tools" / name)
    for f in (SRC_ENGRAM / "hooks" / "claude").glob("*.py"):      # the flat hooks/ the build ships
        if f.name != HOOK_NAME:
            shutil.copyfile(f, root / "hooks" / f.name)
    if hook != "absent":
        text = (SRC_ENGRAM / "hooks" / "claude" / HOOK_NAME).read_text()
        if hook == "raises":
            text = "raise RuntimeError('boom-hook-import')\n" + text
        (root / "hooks" / HOOK_NAME).write_text(text)
    home = tmp / "home"
    home.mkdir()
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,"
                 " metadata TEXT NOT NULL DEFAULT '{}', claim TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO nodes (id, type, is_current, metadata, claim) VALUES ('lsx-001', 'lesson', 1, ?, 'c')",
                 (json.dumps({"situation_pattern": "gh pr create"}),))
    conn.commit()
    conn.close()
    transcripts = login / ".claude" / "projects" / "p"
    transcripts.mkdir(parents=True)
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}} for c in ("gh pr create a", "ls")]
    (transcripts / "t.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    scratch = tmp / "scratch"
    scratch.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env.update(HOME=str(login), CLAUDE_PLUGIN_ROOT=str(root), TMPDIR=str(scratch),
               ENGRAM_HOME=str(home) if engram_home == "ok" else engram_home)
    done = subprocess.run(["bash", "-c", step_block() + '\nprintf "%s" "$lesson_ranks_status"'],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout, sorted(p.name for p in scratch.iterdir()), home


def test_real_tool_written_leaves_the_scratch_dir_empty(tmp):
    status, left, home = _run_real(tmp)
    assert status == "✓ lesson ranks written", status
    assert left == [] and (home / "lesson-fire-ranks.json").is_file()


def test_real_tool_without_the_hook_file_records_the_no_hook_line_and_writes_nothing(tmp):
    status, left, home = _run_real(tmp, hook="absent")
    assert status == "✓ no tripwire hook installed, rank file left as is", status
    assert left == [] and not (home / "lesson-fire-ranks.json").exists()


def test_real_tool_with_a_hook_that_fails_to_import_records_FAILED_not_a_tick(tmp):
    """Mutation: treat a hook load error as 'not installed' -> a tick every night -> red."""
    status, left, home = _run_real(tmp, hook="raises")
    assert status.startswith("⚠ lesson-ranks FAILED (exit "), status
    assert "boom-hook-import" in status and "✓" not in status
    assert left == [] and not (home / "lesson-fire-ranks.json").exists()


def test_real_tool_with_a_relative_engram_home_records_FAILED_not_a_tick(tmp):
    """The hook's own guard exits 0 on a relative ENGRAM_HOME; the tool must refuse first.  Mutation: drop the
    tool's ENGRAM_HOME validation -> '✓ lesson ranks written' with nothing written -> red."""
    status, left, home = _run_real(tmp, engram_home="rel")
    assert status.startswith("⚠ lesson-ranks FAILED (exit 1): "), status
    assert "ENGRAM_HOME" in status and "✓" not in status
    assert left == []
    assert list(tmp.rglob("lesson-fire-ranks.json")) == []
