"""#4139 shape (a): the Claude boot charges make the seat WRITE `loop_prompt` into its marker.

Contract (Step 1 / Step 2 of "Then ENTER YOUR HEARTBEAT LOOP" in the two Claude components):
  1. Step 1 names `loop_prompt` as a field and carries the exact `loop_marker_set.py --string
     loop_prompt` command with STUB inside it.
  2. The literal placeholder `<your loop stub>` appears nowhere (charges, hook source).
  3. Step 2 tells the seat to pass the marker's `loop_prompt` VERBATIM (stable phrase below).
  4. The Step 1..Step 2 span is identical across the two components.
  5. RUNTIME: the command, extracted from the charge text and run for real, writes a
     `<loop-wake>\\n`-led prompt containing STUB into a seeded marker, leaving the seed keys
     alone, and fails loudly when the marker is absent (loop_marker_set never creates one).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHARGES = {
    "default": REPO / "deploy" / "autostart" / "boot-charge-default-claude.md",
    "quiet": REPO / "deploy" / "autostart" / "boot-charge-quiet-claude.md",
}
HOOK = REPO / "src" / "engram" / "hooks" / "claude" / "engram-loop-arm-stop-hook.py"
TOOLS_DIR = REPO / "src" / "engram" / "tools"

LP_PATH = TOOLS_DIR / "loop_prompt.py"
_spec = importlib.util.spec_from_file_location("loop_prompt_4139a", LP_PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)
# The ONE owner of the stub text (#4223 round 2): tools/loop_prompt.py.
STUB = getattr(lp, "DEFAULT_STUB", "<DEFAULT_STUB missing>")
PLUGIN_TOOLS = "$HOME/.engram/marketplace/plugins/engram/tools/"
START = "Then ENTER YOUR HEARTBEAT LOOP"
END = "Relax by writing 3300"
# Stable phrase pinning Step 2 (chosen for this test).
STEP2_PHRASE = "pass the marker's `loop_prompt` field VERBATIM as the ScheduleWakeup prompt"
CMD_RE = re.compile(
    r"`(P=\"\$\(python3 \$HOME/\.engram/marketplace/plugins/engram/tools/loop_prompt\.py "
    r"--default-stub\)\" && python3 "
    r"\$HOME/\.engram/marketplace/plugins/engram/tools/loop_marker_set\.py "
    r"--string loop_prompt \"\$P\")`"
)
# Loose extractor for the pre-flag cell: any backticked Step 1 command that writes loop_prompt,
# whatever its shape, so the cell can be run against an older command form too.
LOOSE_RE = re.compile(r"`([^`]*loop_marker_set\.py --string loop_prompt[^`]*)`")


def _span(name: str) -> str:
    t = CHARGES[name].read_text(encoding="utf-8")
    return t[t.index(START):t.index(END)]


def _step1(name: str) -> str:
    s = _span(name)
    return s[:s.index("Step 2 — ARM")]


def _step2(name: str) -> str:
    s = _span(name)
    return s[s.index("Step 2 — ARM"):]


@pytest.mark.parametrize("name", CHARGES)
def test_step1_names_loop_prompt_and_exact_command(name):
    s1 = _step1(name)
    assert "four fields" in s1 and "set three fields" not in s1
    assert "`loop_prompt`" in s1
    expected = (
        f"P=\"$(python3 {PLUGIN_TOOLS}loop_prompt.py --default-stub)\" && "
        f"python3 {PLUGIN_TOOLS}loop_marker_set.py --string loop_prompt \"$P\""
    )
    assert expected in s1
    # C2 (#4223 round 3): the failure sentence rides right after the command.
    assert "If this exits non-zero, your plugin predates --default-stub: rebuild the plugin before arming." in s1
    assert "echo '" not in s1 and "Heartbeat wake" not in s1


@pytest.mark.parametrize("path", [*CHARGES.values(), HOOK], ids=[*CHARGES, "hook"])
def test_no_placeholder_stub_anywhere(path):
    assert "<your loop stub>" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", CHARGES)
def test_step2_passes_marker_loop_prompt_verbatim(name):
    s2 = _step2(name)
    assert STEP2_PHRASE in s2
    assert "your loop stub" not in s2


def test_step_span_identical_across_components():
    # May be green on dev too: the spans were already byte-identical before this change.
    assert _span("default") == _span("quiet")


def _runnable_command(name: str) -> str:
    m = CMD_RE.search(_step1(name))
    assert m, "exact loop_marker_set --string loop_prompt command not found in Step 1"
    return m.group(1).replace(PLUGIN_TOOLS, str(TOOLS_DIR) + "/")


def _run(cmd: str, home: Path):
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name", CHARGES)
def test_runtime_command_writes_loop_prompt(name, tmp_path):
    seed = {"kind": "heartbeat", "pacer": "scheduleWakeup", "cadence_seconds": 1200}
    (tmp_path / "loop-mode.json").write_text(json.dumps(seed), encoding="utf-8")
    r = _run(_runnable_command(name), tmp_path)
    assert r.returncode == 0, r.stderr
    got = json.loads((tmp_path / "loop-mode.json").read_text(encoding="utf-8"))
    assert got["loop_prompt"].startswith("<loop-wake>\n")
    assert STUB in got["loop_prompt"]
    assert {k: got[k] for k in seed} == seed


@pytest.mark.parametrize("name", CHARGES)
def test_runtime_command_fails_loudly_without_marker(name, tmp_path):
    r = _run(_runnable_command(name), tmp_path)
    assert r.returncode != 0
    assert "loop_marker_set:" in r.stderr
    assert not (tmp_path / "loop-mode.json").exists()


@pytest.mark.parametrize("name", CHARGES)
def test_c3_preflag_plugin_aborts_without_writing(name, tmp_path):
    """#4223 round 3: a plugin whose loop_prompt.py predates --default-stub must not arm.

    The stub below behaves like the old tool on an unknown flag: argparse-style usage error on
    stderr, nothing on stdout, exit 2. The charge command must exit non-zero AND leave the
    marker byte-identical (no `loop_prompt` key, in particular not an empty one).
    """
    m = LOOSE_RE.search(_step1(name))
    assert m, "no Step 1 command that writes loop_prompt found"
    old_tool = tmp_path / "old_loop_prompt.py"
    old_tool.write_text(
        "import sys\n"
        "sys.stderr.write('usage: loop_prompt.py [-h]\\n"
        "loop_prompt.py: error: unrecognized arguments: --default-stub\\n')\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    cmd = m.group(1).replace(PLUGIN_TOOLS + "loop_prompt.py", str(old_tool))
    cmd = cmd.replace(PLUGIN_TOOLS, str(TOOLS_DIR) + "/")
    assert str(old_tool) in cmd, "stub substitution did not land"
    home = tmp_path / "home"
    home.mkdir()
    seed = json.dumps({"kind": "heartbeat", "pacer": "scheduleWakeup", "cadence_seconds": 1200})
    marker = home / "loop-mode.json"
    marker.write_text(seed, encoding="utf-8")
    before = marker.read_bytes()
    r = _run(cmd, home)
    assert r.returncode != 0, "command exited 0 on a pre-flag plugin: " + r.stderr
    assert marker.read_bytes() == before
    assert "loop_prompt" not in json.loads(marker.read_text(encoding="utf-8"))


# --- #4223 round 2: one owner of the stub text -------------------------------

DISTINCTIVE = "do any planned item; otherwise re-arm at cadence_seconds"
LITERAL_STUB = (
    "Heartbeat wake: check crossings and forum items addressed to me, read my loop marker, "
    "do any planned item; otherwise re-arm at cadence_seconds from my loop marker."
)


def _lp_run(*args, stdin=None):
    kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(
        [sys.executable, str(LP_PATH), *args], capture_output=True, timeout=30, **kw,
    )


def test_c1_default_stub_constant_is_todays_text():
    assert getattr(lp, "DEFAULT_STUB", None) == LITERAL_STUB


def test_c2_default_stub_bytes_equal_stdin_path():
    via_flag = _lp_run("--default-stub")
    via_stdin = _lp_run(stdin=(lp.DEFAULT_STUB + "\n").encode("utf-8"))
    assert via_flag.returncode == 0, via_flag.stderr
    assert via_flag.stdout == via_stdin.stdout
    assert via_flag.stdout.startswith(b"<loop-wake>\n")


def test_c2_default_stub_does_not_block_on_stdin():
    # stdin is an open pipe nobody writes to or closes: a stdin read would hang to the timeout.
    p = subprocess.Popen(
        [sys.executable, str(LP_PATH), "--default-stub"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, _err = p.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        pytest.fail("--default-stub blocked waiting on stdin")
    assert p.returncode == 0 and out.startswith(b"<loop-wake>\n")


@pytest.mark.parametrize("other", [["--rearm-delay"], ["--cadence"]])
def test_c3_default_stub_is_mutually_exclusive_with_other_modes(other):
    r = _lp_run("--default-stub", *other)
    assert r.returncode == 2
    assert b"not allowed with argument" in r.stderr


def test_c6_stub_text_has_a_single_owner():
    for path in [*CHARGES.values(), HOOK]:
        text = path.read_text(encoding="utf-8")
        assert DISTINCTIVE not in text, f"{path.name} carries a copy of the stub text"
    assert DISTINCTIVE in LP_PATH.read_text(encoding="utf-8")
    offenders = [
        str(f.relative_to(REPO)) for root in (REPO / "src", REPO / "deploy")
        for f in root.rglob("*") if f.is_file() and f != LP_PATH and "__pycache__" not in f.parts
        and DISTINCTIVE in f.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []
