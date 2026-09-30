"""Contract tests for the engram-toolcall-repair.py tombstone (#3249).

#3213 deleted hooks/claude/engram-toolcall-repair.py and its two PreToolUse
registrations.  A Claude Code session started on an older release keeps those
registrations until it restarts, so after a plugin rebuild every ENGRAM tool
call would run python on a missing file (exit 2 == PreToolUse block).  The fix
is a no-op tombstone shipped for one release, NOT registered in hooks.json.

Contract:
1. The BUILT (flattened) plugin has hooks/engram-toolcall-repair.py, and it
   ships at the essential tier.
2. Run with a PreToolUse payload, empty stdin, or garbage stdin: exit 0 and
   stdout exactly empty.
3. src/engram/hooks/hooks.json does not reference it.
4. The tier manifest carries exactly one entry, essential, with a note that
   names #3249 (tombstone; delete in the first release after v0.5.2, since only
   sessions started on v0.5.2-rc4 or later carry no registration).
5. The silent-success contract holds for BOTH the src copy and the built
   (flattened) copy from the essential build.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_PLUGIN_SH = os.path.join(REPO_ROOT, "tools", "build-plugin.sh")
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")
HOOKS_JSON_SRC = os.path.join(REPO_ROOT, "src", "engram", "hooks", "hooks.json")
TOMBSTONE_SRC = os.path.join(
    REPO_ROOT, "src", "engram", "hooks", "claude", "engram-toolcall-repair.py"
)

# The path the OLD registration names, relative to the plugin root.  Copied from
# v0.5.2-rc3's src/engram/hooks/hooks.json:
#   {{PYTHON}} "${CLAUDE_PLUGIN_ROOT}/hooks/engram-toolcall-repair.py"
# Hardcoded on purpose: CI checkouts may not have release tags.
OLD_REGISTERED_RELPATH = os.path.join("hooks", "engram-toolcall-repair.py")
SRC_MANIFEST_PATH = "hooks/claude/engram-toolcall-repair.py"


def _build(tmp_path, tier: str) -> str:
    out = str(tmp_path / f"plugin-{tier}")
    proc = subprocess.run(
        ["bash", BUILD_PLUGIN_SH, "--allow-branch", "--allow-untagged",
         "--tier", tier, "--output", out],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


@pytest.fixture(scope="module")
def built_essential(tmp_path_factory) -> str:
    """One essential build per module, shared by the layout and run tests."""
    return _build(tmp_path_factory.mktemp("built"), "essential")


def _run_tombstone(path: str, stdin: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, path],
        input=stdin, capture_output=True, timeout=5,
    )


class TestPresentInBuiltLayout:
    # essential = the tier whose hooks.json carried the old registration in
    # every install; dev = the widest build, as a cumulative-tier sanity check.
    @pytest.mark.parametrize("tier", ["essential", "dev"])
    def test_file_at_old_registered_path(self, tmp_path_factory, built_essential, tier):
        root = (built_essential if tier == "essential"
                else _build(tmp_path_factory.mktemp("built-dev"), tier))
        assert os.path.isfile(os.path.join(root, OLD_REGISTERED_RELPATH)), (
            f"{OLD_REGISTERED_RELPATH} missing from the {tier} build"
        )


PAYLOAD = json.dumps({
    "session_id": "sess-3249",
    "hook_event_name": "PreToolUse",
    "tool_name": "mcp__plugin_engram_engram__engram_add_observation",
    "tool_input": {"payload_json": json.dumps({"text": "x", "source": "y"})},
}).encode()


class TestSilentSuccess:
    @pytest.mark.parametrize(
        "stdin",
        [PAYLOAD, b"", b"\xff\xfe not json {{{ \x00\x01"],
        ids=["pretooluse-payload", "empty-stdin", "garbage-stdin"],
    )
    @pytest.mark.parametrize("copy", ["src", "built"])
    def test_exit_zero_and_empty_stdout(self, stdin, copy, built_essential):
        path = (TOMBSTONE_SRC if copy == "src"
                else os.path.join(built_essential, OLD_REGISTERED_RELPATH))
        proc = _run_tombstone(path, stdin)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == b""


class TestNotRegistered:
    def test_hooks_json_has_no_reference(self):
        with open(HOOKS_JSON_SRC, encoding="utf-8") as f:
            assert "engram-toolcall-repair" not in f.read()


class TestManifestEntry:
    def test_single_essential_entry_with_note(self):
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            entries = [e for e in json.load(f)["mechanisms"]
                       if e["path"] == SRC_MANIFEST_PATH]
        assert len(entries) == 1
        e = entries[0]
        assert e["tier"] == "essential"
        assert e["dest"] == "hooks/engram-toolcall-repair.py"
        assert "3249" in e.get("note", "")
