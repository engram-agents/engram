"""Boot charges must not read `--check` exit 2 alone as "supervisor not armed" (alpha #3356).

A deployed monitor-supervisor.py older than the flag answers `--check` with an
argparse usage error: exit 2, the whole usage block (~15 lines, error LAST) -- the same exit code as the
real "not armed" verdict. The charge text must therefore (1) say exit 2 alone is
ambiguous, (2) name the stale-copy signature, (3) give the read-only dev-copy
check, and (4) trigger the three-Monitor fallback on the printed verdict only.
"""
from pathlib import Path

import pytest

AUTOSTART = Path(__file__).resolve().parent.parent / "deploy" / "autostart"
CHARGES = ["boot-charge-default-claude.md", "boot-charge-quiet-claude.md"]


@pytest.mark.parametrize("name", CHARGES)
def test_exit2_is_not_equated_with_not_armed(name):
    text = (AUTOSTART / name).read_text()
    assert "exit 2 means the supervisor is not armed at all" not in text


@pytest.mark.parametrize("name", CHARGES)
def test_stale_copy_signature_and_dev_copy_check_named(name):
    text = (AUTOSTART / name).read_text()
    assert "unrecognized arguments" in text
    assert "origin/dev:src/engram/tools/monitor-supervisor.py" in text


@pytest.mark.parametrize("name", CHARGES)
def test_fallback_keys_on_printed_verdict(name):
    text = (AUTOSTART / name).read_text()
    # the whole fallback clause, contiguous: ties the printed verdict to the
    # three-Monitor fallback sentence, not just to the explanatory sentence.
    assert ("or `--check` prints the literal line `CHECK FAIL supervisor not armed` "
            "(the printed verdict, never the exit code alone") in text
    assert "or `--check` exits 2" not in text
    # per-user path: a shared /tmp/msup.py is owned by the first seat to write it
    assert "${TMPDIR:-/tmp}/msup-$USER.py" in text
    assert "/msup.py" not in text
    # stale output shape (measured 15 lines, error last), not "fewer than five lines"
    assert "also fewer than five lines" not in text
    assert "output starts with `usage:` or its last line contains `unrecognized arguments`" in text


def test_collaborating_loop_skill_does_not_key_fallback_on_exit_code():
    skill = (Path(__file__).resolve().parent.parent / "src" / "engram" / "skills"
             / "claude" / "engram-collaborating-loop" / "SKILL.md").read_text()
    flat = " ".join(skill.replace(">", " ").split())  # wrap- and blockquote-insensitive
    assert "prints the literal line `CHECK FAIL supervisor not armed`, arm" in flat
    assert "`--check` exits 2, arm" not in flat
