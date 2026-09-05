"""Deployed-layout regression test for gh#2570 (emitter path resolution).

Background: the emitter-import path fix (a module-scope
``resolve_runtime_dir`` call feeding ``sys.path`` before ``from
engram_log_emitter import Emitter``) existed in exactly one of the eight
emitter-importing hooks and was never propagated to the other seven. Two
opposite failure modes were both silent: a stale ENGRAM_HOME copy shadows
the plugin's live emitter (bare ``sys.path.insert(0, ENGRAM_HOME)`` puts
ENGRAM_HOME ahead of everything), or the import fails outright with nothing
on the emit-site's own sys.path pointing at the plugin root at all. Neither
raises -- ``emitter.emit()`` and its surrounding ``try/except`` never
propagate a failure to the hook's own exit code or stdout, so the only way
to know emission is alive is to read the per-session JSONL log itself. That
is what this test does: invoke each hook for real, as a subprocess, in a
synthetic recreation of the FLATTENED deployed layout (see this repo's
CLAUDE.md, "Plugin build restructures code paths" -- ``hooks/claude/*.py``
in source becomes ``hooks/*.py`` deployed, one level shallower, with the
plugin's runtime modules, including ``engram_log_emitter.py``, living at
the plugin root as siblings of ``hooks/``, not inside it), and assert a
record actually landed in the log file.

Deliberately excluded from the log-record assertion, by name (see the two
classes below): ``engram-loop-arm-stop-hook.py`` and ``engram-stop-hook.py``
place their single emit site DOWNSTREAM of the hook's own "nothing to say"
early return. On a minimal/idle payload, neither one reaches its emit call
at all -- a zero there is the hook correctly staying silent, not a broken
import. Weakening the corpus to "assert only for the hooks that emit
unconditionally" would silently drop these two hooks from the gate, which
is the exact failure this issue is about (an emitter regression in either
hook would go undetected). Instead, ``TestComplaintBranchHooksEmit``
constructs stdin/marker-file input that drives each one into its complaint
branch (the actual code path where the emit call lives), so the assertion
covers the path that matters -- see each test's docstring for exactly which
gate it clears and why that construction reaches the emit call.

Also deliberately different: ``engram-surface-hook.py`` never emits an
``engram.hook.fire`` event at all -- its three emit sites use
``engram.surface.fire``, ``engram.trigger.fire``, and
``engram.surface.render_size`` respectively (see its own module for the
full rationale). Its test asserts a record of ANY of the hook's own event
types was written, not the ``engram.hook.fire`` shape the other seven use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
EMITTER_SRC = REPO_ROOT / "src" / "engram" / "engram_log_emitter.py"
PYTHON = sys.executable

# Shared library modules every hook (or its imports) may need as a sibling
# in the flattened hooks/ directory. Copied verbatim from source -- these
# are the real modules, not stubs, so the test exercises real import
# behavior for the shared-library layer.
_SHARED_SIBLINGS = ("_hooklib.py", "_stophooklib.py", "context_tracker.py")

# All eight emitter-importing hooks (gh#2570's own enumeration, confirmed
# via `grep -l engram_log_emitter src/engram/hooks/claude/*.py`).
_ALL_EIGHT_HOOKS = (
    "engram-utility-credit-mention-stop.py",
    "engram-session-start-hook.py",
    "engram-surface-hook.py",
    "engram-stop-hook.py",
    "engram-postcompact-hook.py",
    "engram-deference-detector-prompt.py",
    "engram-deference-detector-stop.py",
    "engram-loop-arm-stop-hook.py",
)

# Stub runtime modules. Their ONLY purpose is to make
# resolve_runtime_dir(ENGRAM_HOME, sentinel)'s rung-2 plugin-root check
# (`os.path.exists(os.path.join(plugin_root, sentinel))`) succeed against
# OUR synthetic plugin root deterministically, on every machine this test
# runs on. Without them, the sentinel lookup for "engram_client.py" /
# "engram_ids.py" / "engram_idf.py" (used by call sites unrelated to the
# emitter fix under test) would fall through resolve_runtime_dir's rung 4
# (walk-parents) all the way to its rung-5 literal fallback,
# ~/engram-alpha -- which is this repo's own dev checkout and DOES exist
# on a developer seat, but must NOT exist (and reliably doesn't) on a CI
# runner. Relying on that fallback would make the test's hermeticity
# depend on which machine runs it. Stubbing pins resolution to our
# temp-dir plugin root on every machine, and keeps the test fast (no real
# EngramClient() construction, no real DB/keyword-extraction work).
_STUB_MODULES = {
    "engram_client.py": (
        "class EngramClient:\n"
        "    def __init__(self, *a, **kw):\n"
        "        pass\n"
        "    def call(self, *a, **kw):\n"
        "        return {}\n"
    ),
    "engram_ids.py": (
        "def find_node_ids(text):\n"
        "    return []\n"
    ),
    "engram_idf.py": (
        "def extract_keywords(conn, text, min_idf=None, top_k=5):\n"
        "    return []\n"
    ),
}


@pytest.fixture()
def plugin_root(tmp_path) -> Path:
    """Recreate the FLATTENED deployed layout for all eight hooks.

    ``<root>/hooks/<hook>.py`` (never ``<root>/hooks/claude/<hook>.py`` --
    the plugin build drops the ``claude/`` level), with
    ``engram_log_emitter.py`` and the stub runtime modules as SIBLINGS of
    ``hooks/`` (i.e. at the plugin root), exactly mirroring a real
    ``build-plugin.sh`` output (verified against this repo's own
    ``build/plugin/`` layout: ``engram_log_emitter.py`` sits at
    ``<plugin>/engram_log_emitter.py``, ``hooks/`` is a sibling directory).
    """
    root = tmp_path / "plugin_root"
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True)

    for name in _SHARED_SIBLINGS:
        shutil.copy(HOOKS_SRC / name, hooks_dir / name)
    for name in _ALL_EIGHT_HOOKS:
        shutil.copy(HOOKS_SRC / name, hooks_dir / name)

    shutil.copy(EMITTER_SRC, root / "engram_log_emitter.py")
    for name, content in _STUB_MODULES.items():
        (root / name).write_text(content)

    return root


@pytest.fixture()
def engram_home(tmp_path) -> Path:
    """An EMPTY temp dir standing in for ENGRAM_HOME -- no server.py, no
    stale scatter copies, no config.json, no knowledge.db. This is the
    condition gh#2570 documents as the one that most sharply exposes the
    bug: a clean plugin install where ENGRAM_HOME is data-only.
    """
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


def _run_hook(
    hook_name: str,
    stdin_payload: dict,
    plugin_root: Path,
    engram_home: Path,
) -> subprocess.CompletedProcess:
    """Invoke one hook as a real subprocess (never import -- a subprocess
    is required both because the Emitter singleton is process-global
    (importing two hooks into one interpreter would leak state between
    them) and because that is how Claude Code actually invokes hooks).

    CLAUDE_PLUGIN_ROOT and ENGRAM_RUNTIME_DIR are explicitly unset -- the
    acceptance criterion (spec §3/§4) is that rung 2 of resolve_runtime_dir
    (the plugin-root-via-__file__ computation) finds the emitter on its
    own, not that an explicit override does the job for it.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR")
    }
    env["ENGRAM_HOME"] = str(engram_home)
    hook_path = plugin_root / "hooks" / hook_name
    return subprocess.run(
        [PYTHON, str(hook_path)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _session_records(engram_home: Path, session_id: str, level: int = 1) -> list[dict]:
    log_path = engram_home / "logs" / "sessions" / f"{session_id}.l{level}.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


class TestUnconditionalEmitHooks:
    """The six hooks whose emit site is reached on ANY minimal, parseable
    stdin payload (loop-arm-stop-hook and stop-hook excluded -- see
    TestComplaintBranchHooksEmit and the module docstring)."""

    @pytest.mark.parametrize(
        "hook_name,payload",
        [
            (
                "engram-utility-credit-mention-stop.py",
                {
                    "session_id": "sid-utility-credit",
                    "transcript_path": "",
                    "last_assistant_message": "Nothing to record.",
                    "stop_hook_active": False,
                },
            ),
            (
                "engram-session-start-hook.py",
                {
                    "session_id": "sid-session-start",
                    "transcript_path": "",
                    "source": "startup",
                },
            ),
            (
                "engram-postcompact-hook.py",
                {
                    "session_id": "sid-postcompact",
                    "transcript_path": "",
                },
            ),
            (
                "engram-deference-detector-prompt.py",
                {
                    "session_id": "sid-deference-prompt",
                    "transcript_path": "",
                    "prompt": "hello",
                },
            ),
            (
                "engram-deference-detector-stop.py",
                {
                    "session_id": "sid-deference-stop",
                    "transcript_path": "",
                    "last_assistant_message": "hello",
                },
            ),
        ],
    )
    def test_emits_hook_fire_record(self, hook_name, payload, plugin_root, engram_home):
        result = _run_hook(hook_name, payload, plugin_root, engram_home)
        records = _session_records(engram_home, payload["session_id"])
        assert records, (
            f"{hook_name}: expected at least one engram.hook.fire record in "
            f"{engram_home}/logs/sessions/{payload['session_id']}.l1.jsonl "
            f"but found none.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        fire_records = [r for r in records if r.get("event_type") == "engram.hook.fire"]
        assert fire_records, (
            f"{hook_name}: found {len(records)} record(s) but none was "
            f"engram.hook.fire: {records!r}"
        )
        assert fire_records[0]["data"]["hook_name"] == hook_name.removesuffix(".py")


class TestSurfaceHookEmits:
    """engram-surface-hook.py -- a different event-type family entirely
    (engram.surface.fire / engram.trigger.fire / engram.surface.render_size,
    never engram.hook.fire). Its first emit site (engram.surface.fire) is
    reached whenever the prompt is non-empty and isn't a slash/bang command
    -- unconditional for a normal prompt, per its own main()."""

    def test_emits_surface_fire_record(self, plugin_root, engram_home):
        payload = {
            "session_id": "sid-surface",
            "transcript_path": "",
            "prompt": "what is my current status",
        }
        result = _run_hook("engram-surface-hook.py", payload, plugin_root, engram_home)
        records = _session_records(engram_home, payload["session_id"])
        assert records, (
            f"engram-surface-hook.py: expected at least one record in "
            f"{engram_home}/logs/sessions/{payload['session_id']}.l1.jsonl "
            f"but found none.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        surface_event_types = {r.get("event_type") for r in records}
        assert surface_event_types & {
            "engram.surface.fire",
            "engram.trigger.fire",
            "engram.surface.render_size",
        }, f"engram-surface-hook.py: no known surface event type in {records!r}"
        # And explicitly NOT the other seven hooks' event type -- surface-hook
        # never emits engram.hook.fire; asserting its absence here would be
        # wrong (nothing says it must be absent), so this is a documentation
        # assertion, not a behavioral one: the surface.fire record's own
        # event_type is the load-bearing check above.


class TestComplaintBranchHooksEmit:
    """loop-arm-stop-hook and stop-hook place their one emit site downstream
    of the hook's own "nothing to say" early return -- on a minimal payload
    neither reaches it, and a zero there is correct behavior, not breakage
    (see module docstring). Each test here constructs the stdin/marker-file
    input that drives the hook into its COMPLAINT branch specifically, so
    the emit call is genuinely exercised."""

    def test_loop_arm_stop_hook_emits_when_unconfirmed(self, plugin_root, engram_home):
        """Reaching the emit call requires, in order (see main()):
          1. LOOP_MARKER_PATH exists (else: silent no-op, no emit -- this
             hook never nags a non-loop session).
          2. Not a managed-Codex runtime (default here: true, no
             platform.json / CODEX_PLUGIN_ROOT in this fixture).
          3. The marker's `pacer` field reads "schedulewakeup" case-
             insensitively (_pacer_is_schedule_wakeup) -- any other value,
             or a missing/corrupt marker, silently returns without emitting.
          4. `confirmed` (a ScheduleWakeup tool_use call with the loop-wake
             marker in this turn's transcript delta) must be False --
             an empty transcript_path makes _delta_has_arm_confirmation
             return False immediately (no exception), so `confirmed` stays
             False and the complaint path (the nudge print + the emit
             call) is reached.
        """
        (engram_home / "loop-mode.json").write_text(
            json.dumps({"pacer": "scheduleWakeup"})
        )
        payload = {
            "session_id": "sid-loop-arm",
            "transcript_path": "",
        }
        result = _run_hook("engram-loop-arm-stop-hook.py", payload, plugin_root, engram_home)
        records = _session_records(engram_home, payload["session_id"])
        assert records, (
            f"engram-loop-arm-stop-hook.py: expected an engram.hook.fire "
            f"record once driven into the unconfirmed-arm complaint branch, "
            f"found none.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        fire_records = [r for r in records if r.get("event_type") == "engram.hook.fire"]
        assert fire_records, f"no engram.hook.fire record in {records!r}"
        assert fire_records[0]["data"]["hook_name"] == "engram-loop-arm-stop-hook"
        # Sanity check on the construction itself: the hook DID print the
        # blocking nudge (proves this run actually reached the complaint
        # branch, not some other path that happens to also emit).
        assert result.stdout.strip(), "expected a blocking-decision nudge on stdout"
        assert json.loads(result.stdout)["decision"] == "block"

    def test_stop_hook_emits_when_unsuppressed(self, plugin_root, engram_home):
        """Reaching the emit call requires, in order (see main()):
          1. `_maturity_muted()` is False -- with no knowledge.db present in
             ENGRAM_HOME, the DB-count query raises and the function fails
             toward "not muted" (young-agent assumption), so this holds by
             construction of the `engram_home` fixture (empty temp dir).
          2. `_should_suppress(transcript_path)` is False -- an empty
             transcript_path hits its very first check
             (`if not transcript_path: return False  # no transcript ->
             unknown -> emit`), so this also holds unconditionally for an
             empty transcript_path, with no idle-turn state file needed.
        Both conditions hold for the same minimal payload used by the
        unconditional-emit hooks above; this hook is tested separately
        (not folded into TestUnconditionalEmitHooks) because — unlike
        those six — its own main() has an early-return branch that would
        NOT reach the emit call (a mature, idle-suppressed session), and
        that branch is easy to hit by accident with a differently-shaped
        payload. Isolating it here keeps that distinction explicit rather
        than silently relying on payload shape.
        """
        payload = {
            "session_id": "sid-stop-hook",
            "transcript_path": "",
            "last_assistant_message": "Nothing to record.",
            "stop_hook_active": False,
        }
        result = _run_hook("engram-stop-hook.py", payload, plugin_root, engram_home)
        records = _session_records(engram_home, payload["session_id"])
        assert records, (
            f"engram-stop-hook.py: expected an engram.hook.fire record on "
            f"the unsuppressed/immature path, found none.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        fire_records = [r for r in records if r.get("event_type") == "engram.hook.fire"]
        assert fire_records, f"no engram.hook.fire record in {records!r}"
        assert fire_records[0]["data"]["hook_name"] == "engram-stop-hook"
        assert result.stdout.strip(), "expected a blocking-decision nudge on stdout"
        assert json.loads(result.stdout)["decision"] == "block"


class TestAllEightHooksImportCleanly:
    """Every one of the eight emitter-importing hooks must import (as a
    real subprocess, exercising the actual import machinery -- not merely
    ast.parse, which cannot see a runtime ModuleNotFoundError) without a
    traceback, in the flattened deployed layout. A hook whose module-scope
    resolve_runtime_dir call itself raises would crash the whole process
    before main() ever runs -- this is the coarsest possible regression
    check, orthogonal to whether the hook chooses to emit on this payload.
    """

    @pytest.mark.parametrize("hook_name", _ALL_EIGHT_HOOKS)
    def test_hook_process_does_not_crash(self, hook_name, plugin_root, engram_home):
        payload = {
            "session_id": f"sid-import-{hook_name}",
            "transcript_path": "",
            "prompt": "hello",
            "last_assistant_message": "hello",
            "stop_hook_active": False,
        }
        result = _run_hook(hook_name, payload, plugin_root, engram_home)
        assert result.returncode in (0, 1), (
            f"{hook_name} crashed (returncode={result.returncode}).\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "Traceback (most recent call last)" not in result.stderr, (
            f"{hook_name} raised an uncaught exception:\n{result.stderr}"
        )
