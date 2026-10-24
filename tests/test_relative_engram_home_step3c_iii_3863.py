"""#3863 step 3c-iii: the two test-helper save/restore sites restore the WHOLE environment.

Ruling: Borges on #3863 (Square/crossing seq 16328, 2026-10-24): `engramd_a6_latency.make_test_store` and
`perf/bench_in_turn_recall._load_hook` set ENGRAM_HOME around a block and used to save/restore that ONE key
(`old = os.environ.get("ENGRAM_HOME")`), which is not a resolution but is a raw read the gate counted. The ruling
snapshots the WHOLE environment before and restores EXACTLY that in the `finally`, so the block's other writes are
undone too, a key the block REMOVED comes back, and a key that was ABSENT is absent again.

Contract, one subprocess cell per (tool x ENGRAM_HOME before x block raises or not): after the call the environment
equals the one before it, key for key. The block is made to touch THREE other keys: it adds STRAY, changes PRESET
and removes KEEP. Mutation (restore only ENGRAM_HOME, the old behaviour) -> every cell red on STRAY / PRESET / KEEP.
Each cell runs in a subprocess so a tool that fails to restore cannot poison this process's environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
A6 = REPO / "tools" / "engramd_a6_latency.py"
BENCH = REPO / "tools" / "perf" / "bench_in_turn_recall.py"
PREV = "/prev/engram-home"

# Run in the child. argv: tool path, "a6" | "bench", "raise" | "ok", scratch dir.
CHILD = r'''
import importlib.util, json, os, sys, types
tool, which, mode, scratch = sys.argv[1:5]
os.environ["PRESET"] = "original"
os.environ["KEEP"] = "kept"
os.environ.pop("STRAY", None)
spec = importlib.util.spec_from_file_location("step3c_iii_probe", tool)
mod = importlib.util.module_from_spec(spec)
sys.modules["step3c_iii_probe"] = mod
spec.loader.exec_module(mod)


def touch_env():
    os.environ["STRAY"] = "added-by-the-block"
    os.environ["PRESET"] = "changed-by-the-block"
    os.environ.pop("KEEP", None)
    if mode == "raise":
        raise RuntimeError("the block failed")


before = dict(os.environ)
raised = None
try:
    if which == "a6":
        stub = types.ModuleType("engram_core")

        def _run_db_one_time_setup(conn):
            conn.execute("CREATE TABLE nodes (id, type, claim, created_at, logical_chain, metadata, is_current)")
            touch_env()

        stub._run_db_one_time_setup = _run_db_one_time_setup
        sys.modules["engram_core"] = stub
        mod.make_test_store(nodes=0, people=0)
    else:
        hook = os.path.join(scratch, "hook.py")
        with open(hook, "w") as fh:
            fh.write("import os\nTOUCH()\n")
        mod.HOOK_PATH = hook
        import builtins
        builtins.TOUCH = touch_env
        mod._load_hook(os.path.join(scratch, "home"))
except RuntimeError as exc:
    raised = str(exc)
after = dict(os.environ)
print(json.dumps({
    "raised": raised,
    "added": sorted(set(after) - set(before)),
    "removed": sorted(set(before) - set(after)),
    "changed": sorted(k for k in before if k in after and before[k] != after[k]),
}))
'''


def _run(tool, which, mode, engram_home_before, tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ENGRAM_HOME", "PRESET", "KEEP", "STRAY", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env.update({"HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)})
    if engram_home_before is not None:
        env["ENGRAM_HOME"] = engram_home_before
    r = subprocess.run([sys.executable, "-c", CHILD, str(tool), which, mode, str(tmp_path)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", ["ok", "raise"])
@pytest.mark.parametrize("engram_home_before", [None, PREV], ids=["absent", "present"])
@pytest.mark.parametrize("which,tool", [("a6", A6), ("bench", BENCH)], ids=["a6", "bench"])
def test_the_whole_environment_is_restored_key_for_key(which, tool, engram_home_before, mode, tmp_path):
    """Added, removed and changed keys are all empty: ENGRAM_HOME that was ABSENT is absent again, a present one is
    back to its value, and the block's three other writes (STRAY added, PRESET changed, KEEP removed) are undone."""
    out = _run(tool, which, mode, engram_home_before, tmp_path)
    assert out["raised"] == ("the block failed" if mode == "raise" else None), out
    assert (out["added"], out["removed"], out["changed"]) == ([], [], []), out

