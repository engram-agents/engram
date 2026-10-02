"""#3296 (part of #3039): ``baton merge --help`` states gate 2 as the maintainer-role
rule, not the stale "turn == pool sentinel".

Gate 2 in ``cmd_merge`` passes for any maintainer-role holder -- the pool sentinel,
or a named agent who is neither the registered colleague nor the PR's author (via
``_is_maintainer_flip(current_turn, "", colleague, author)``, #2913).
"""

import os
import subprocess
import sys
from pathlib import Path

CLI_PATH = Path(__file__).parent.parent / "src" / "engram" / "tools" / "baton.py"


def _merge_help(tmp_path) -> str:
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "merge", "--help"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return " ".join(proc.stdout.split())


def test_merge_help_drops_stale_sentinel_only_gate_2(tmp_path):
    out = _merge_help(tmp_path)
    assert "Gate" in out or "gates" in out or "Baton exists" in out  # instrument sanity
    assert "turn == pool sentinel" not in out


def test_merge_help_states_maintainer_role_rule_and_never_forceable(tmp_path):
    out = _merge_help(tmp_path)
    assert "turn is a maintainer-role holder" in out
    assert "neither the registered colleague nor the PR's author" in out
    # gate 2 stays never-forceable
    gate2 = out.split("2. ", 1)[1].split("3. ", 1)[0]
    assert "never forceable" in gate2


def test_merge_force_help_does_not_call_gate_2_a_sentinel_gate(tmp_path):
    out = _merge_help(tmp_path)
    assert "turn-sentinel" not in out
