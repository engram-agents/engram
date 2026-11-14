"""Contract tests for the nap-compaction backstop (#4012, part of #3640).

Contract-first (CLAUDE.md "Contract-first tests"): every assertion below was
written BEFORE the implementation and run red at base.

Mechanism (Lei, 2026-10-21: "make it mechanical instead of needing your
cognitive remember, like a step in a skill"):

  Part 1  the checkpoint persists its own verdict as
          ``$ENGRAM_HOME/compact/last-checkpoint.json``
          ``{checkpoint_at, session_id, backup_ok}``; ``compact-request.py``'s
          ``--backup-ok`` becomes optional and the record supplies it.
  Part 2  a Stop hook, ``engram-nap-compact-backstop-stop.py``, runs
          ``compact-request.py --session <id>`` (no flag) when the evidence of a
          finished nap exists and the nap skill's last step did not run.

Cells:
  B1  record absent / malformed / unparseable / not newer than the last compact -> no run
  B2  all conditions true -> exactly one request, stdout empty, one stderr line; a second Stop does not re-run
      (with the request gone: only backstop-last can say so)
  B3  a refusal is recorded and not retried; a new briefing write re-arms it
  B4  config ``compaction.nap_backstop`` false -> no run;  B4b config absent -> runs
  B5  no loop marker -> runs all the same (#4350 dropped the loop gate; was: no run)
  B6  a request since the checkpoint -> no run (pending: by requested_at; consumed tombstone: by mtime);
      older -> runs
  B10 the record's session differs from the Stop payload's -> no run; a record with no session
      (null, absent, empty, non-string) -> no run, one stderr line (#4356)
  B7  stdin without session_id -> no run, one stderr line (only when otherwise due)
  B8  exits 0 even when compact-request is missing
  B9  the deployed (flattened) layout, CLAUDE_PLUGIN_ROOT unset: found by walk-parents
  C1  the checkpoint writes the record; backup_ok false when either backup dict has "error"
  C2  compact-request without --backup-ok reads the record
  C3  a record write failure leaves the checkpoint result unchanged
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO / "src" / "engram"
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
HOOK_NAME = "engram-nap-compact-backstop-stop.py"
TOOL_PATH = SRC_ENGRAM / "tools" / "compact-request.py"

SESSION = "sess-4012"
HEADER_TS = "2026-10-21T10:00:00"  # the nap header's own form: naive, UTC
CHECKPOINT_EPOCH = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc).timestamp()
ARC = "Built the backstop today.\nSecond line of the arc."
BRIEFING = "# Warm briefing\n\n## From this session\n\n" + ARC + "\n"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def make_home(
    tmp_path: Path,
    *,
    header: str = HEADER_TS,
    record: dict | None | str = "default",
    loop: bool = True,
    briefing_after: bool = True,
) -> Path:
    """A temp ENGRAM_HOME in the state a finished nap leaves behind."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "session_log.md").write_text(
        f"# KG Memory Session Log\n\n---\n\n## Nap (turn 5) — {header}\n\nsummary\n\n---\n",
        encoding="utf-8",
    )
    wb = home / "warm-briefing.md"
    wb.write_text(BRIEFING, encoding="utf-8")
    delta = 120 if briefing_after else -120
    os.utime(wb, (CHECKPOINT_EPOCH + delta, CHECKPOINT_EPOCH + delta))
    if record == "default":
        record = {"checkpoint_at": header, "session_id": SESSION, "backup_ok": True}
    if record is not None:
        _write_json(home / "compact" / "last-checkpoint.json", record)
    # the switch is set explicitly, so only the B4b cell exercises "config absent"
    _write_json(home / "config.json", {"compaction": {"nap_backstop": True}})
    if loop:
        _write_json(home / "loop-mode.json", {"kind": "loop", "pacer": "schedule_wakeup", "cadence_seconds": 270})
    return home


def touch_briefing(home: Path, offset: int = 600) -> None:
    wb = home / "warm-briefing.md"
    wb.write_text(BRIEFING + "\nmore\n", encoding="utf-8")
    os.utime(wb, (CHECKPOINT_EPOCH + offset, CHECKPOINT_EPOCH + offset))


def _copy_hooks(dest: Path) -> None:
    """A flattened (deployed-shaped) hooks/ dir, plus the runtime modules the build puts at the plugin root."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in HOOK_DIR.glob("*.py"):
        shutil.copy2(f, dest / f.name)
    for name in ("engram_config_io.py", "engram_regular_io.py"):
        shutil.copy2(SRC_ENGRAM / name, dest.parent / name)


STUB_TOOL = '''#!/usr/bin/env python3
import json, os, sys
home = os.environ["ENGRAM_HOME"]
with open(os.path.join(home, "stub-calls.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
code = 0
p = os.path.join(home, "stub-exit")
if os.path.exists(p):
    code = int(open(p).read().strip() or "0")
if code:
    sys.stderr.write("stub: refused with %d\\nsecond line\\n" % code)
sys.exit(code)
'''


@pytest.fixture(scope="module")
def stub_plugin() -> Path:
    """A deployed-shaped plugin root (flattened hooks/, tools/) whose compact-request is a call-logging stub."""
    root = Path(tempfile.mkdtemp(prefix="backstop4012_stub_"))
    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    _copy_hooks(root / "hooks")
    (root / "tools").mkdir()
    (root / "tools" / "compact-request.py").write_text(STUB_TOOL, encoding="utf-8")
    return root


def run_hook(
    home: Path,
    payload,
    *,
    plugin_root: Path | None,
    hook_dir: Path | None = None,
    unset_plugin_root: bool = False,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    if plugin_root is not None and not unset_plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    hook = (hook_dir or (plugin_root / "hooks")) / HOOK_NAME
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(hook)], input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )


def stop(home, plugin_root, session_id: str | None = SESSION, **kw) -> subprocess.CompletedProcess:
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, "transcript_path": "/nonexistent.jsonl"}
    if session_id is not None:
        payload["session_id"] = session_id
    return run_hook(home, payload, plugin_root=plugin_root, **kw)


def stub_calls(home: Path) -> list:
    p = home / "stub-calls.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


def real_stop(home: Path, **kw) -> subprocess.CompletedProcess:
    """Run the hook from the source tree against the REAL compact-request."""
    return stop(home, SRC_ENGRAM, hook_dir=HOOK_DIR, **kw)


def backstop_last(home: Path) -> dict:
    return json.loads((home / "compact" / "backstop-last.json").read_text())


def request(home: Path):
    p = home / "compact" / "request.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---------------------------------------------------------------------------
# B1  each record-side precondition false on its own (stub tool: "not run" is observable)
# ---------------------------------------------------------------------------

_COMPACT_NEWER = {"timestamp": "2026-10-21T11:00:00+00:00"}
_COMPACT_EQUAL = {"timestamp": "2026-10-21T10:00:00+00:00"}


@pytest.mark.parametrize(
    "cell",
    ["record_absent", "record_not_json", "record_no_checkpoint_at", "record_unparseable_time",
     "compact_after_checkpoint", "compact_equal_checkpoint"],
)
def test_b1_each_precondition_false_on_its_own_means_no_run(cell, tmp_path, stub_plugin):
    home = make_home(tmp_path, record=None if cell == "record_absent" else "default")
    ckpt = home / "compact" / "last-checkpoint.json"
    if cell == "record_not_json":
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_text("{not json")
    elif cell == "record_no_checkpoint_at":
        _write_json(ckpt, {"session_id": SESSION, "backup_ok": True})
    elif cell == "record_unparseable_time":
        _write_json(ckpt, {"checkpoint_at": "yesterday-ish", "session_id": SESSION, "backup_ok": True})
    elif cell == "compact_after_checkpoint":
        _write_json(home / "last-compact-at.json", _COMPACT_NEWER)
    elif cell == "compact_equal_checkpoint":
        _write_json(home / "last-compact-at.json", _COMPACT_EQUAL)
    done = stop(home, stub_plugin)
    assert done.returncode == 0, done.stderr
    assert stub_calls(home) == [], cell
    assert request(home) is None
    assert not (home / "compact" / "backstop-last.json").exists(), "a precondition miss is not an attempt"


def test_b1b_compact_record_older_than_checkpoint_still_runs(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "last-compact-at.json", {"timestamp": "2026-10-21T09:00:00+00:00"})
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def test_b1c_compact_record_without_timestamp_counts_as_older(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "last-compact-at.json", {"jsonl_path": "x", "byte_offset": 3})
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def test_b1d_the_tool_is_called_with_the_session_and_no_backup_flag(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    stop(home, stub_plugin)
    assert stub_calls(home) == [["--session", SESSION]]


# ---------------------------------------------------------------------------
# B2  all true, real tool
# ---------------------------------------------------------------------------

def test_b2_all_true_writes_exactly_one_request_and_a_second_stop_does_not_rerun(tmp_path):
    home = make_home(tmp_path)
    done = real_stop(home)
    assert done.returncode == 0, done.stderr
    req = request(home)
    assert req is not None and req["session_id"] == SESSION
    assert done.stdout == ""
    rec = backstop_last(home)
    assert rec["outcome"] == "written" and rec["exit_code"] == 0
    assert rec["checkpoint_at"] == HEADER_TS
    assert rec["briefing_mtime_ns"] == (home / "warm-briefing.md").stat().st_mtime_ns

    first = (home / "compact" / "request.json").read_text()
    again = real_stop(home)
    assert again.returncode == 0 and again.stdout.strip() == ""
    assert (home / "compact" / "request.json").read_text() == first

    # the request vanishes (e.g. a mod that deletes it): only backstop-last remembers the attempt
    (home / "compact" / "request.json").unlink()
    third = real_stop(home)
    assert third.returncode == 0 and third.stdout.strip() == ""
    assert request(home) is None, "a recorded (checkpoint, briefing) pair is never retried"


def test_b2b_success_path_writes_nothing_to_stdout_and_one_stderr_line(tmp_path):
    """A Stop hook's only sanctioned stdout is decision:block, so the success path is silent on stdout."""
    home = make_home(tmp_path)
    done = real_stop(home)
    assert done.returncode == 0 and request(home) is not None
    assert done.stdout == ""
    assert done.stderr.splitlines() == ["nap backstop: compaction requested"]


# ---------------------------------------------------------------------------
# B3  refusal recorded, not retried; new briefing write re-arms
# ---------------------------------------------------------------------------

def test_b3_refusal_is_recorded_not_retried_and_a_new_briefing_write_rearms(tmp_path):
    home = make_home(tmp_path, briefing_after=False)  # briefing older than the checkpoint -> the tool refuses
    done = real_stop(home)
    assert done.returncode == 0
    assert request(home) is None
    assert done.stdout.strip() == ""
    err = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(err) == 1 and "3" in err[0] and "briefing" in err[0].lower(), err
    rec = backstop_last(home)
    assert rec["outcome"] == "refused" and rec["exit_code"] == 3
    stamp = (home / "compact" / "backstop-last.json").read_text()

    second = real_stop(home)
    assert second.returncode == 0 and second.stderr.strip() == "" and second.stdout.strip() == ""
    assert (home / "compact" / "backstop-last.json").read_text() == stamp, "no second attempt"

    touch_briefing(home)  # a new mtime re-arms it
    third = real_stop(home)
    assert third.returncode == 0, third.stderr
    assert request(home) is not None
    assert backstop_last(home)["outcome"] == "written"


def test_b3b_a_new_checkpoint_rearms_it(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    (home / "stub-exit").write_text("3")
    stop(home, stub_plugin)
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1
    new_header = "2026-10-21T12:00:00"
    (home / "session_log.md").write_text(
        f"## Nap (turn 5) — {new_header}\n", encoding="utf-8")
    _write_json(home / "compact" / "last-checkpoint.json",
                {"checkpoint_at": new_header, "session_id": SESSION, "backup_ok": True})
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 2


def test_b3c_a_timeout_or_crash_is_an_attempt_too(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    (home / "stub-exit").write_text("4")
    done = stop(home, stub_plugin)
    assert done.returncode == 0
    assert backstop_last(home)["exit_code"] == 4
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


# ---------------------------------------------------------------------------
# B4 / B4b  the switch
# ---------------------------------------------------------------------------

def test_b4_switch_false_means_no_run(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "config.json", {"compaction": {"nap_backstop": False}})
    done = stop(home, stub_plugin)
    assert done.returncode == 0
    assert stub_calls(home) == []
    assert not (home / "compact" / "backstop-last.json").exists()


def test_b4b_config_absent_means_on(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    (home / "config.json").unlink()
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


@pytest.mark.parametrize(
    "config_text",
    ['{"compaction": {"nap_backstop": true}}', '{}', '{"compaction": {}}', '{"compaction": "off"}',
     '{"compaction": {"nap_backstop": null}}', '{"compaction": {"nap_backstop": 0}}',
     '{"compaction": {"nap_backstop": "false"}}', '[1,2]', '{not json'],
)
def test_b4c_only_a_literal_false_turns_it_off(config_text, tmp_path, stub_plugin):
    home = make_home(tmp_path)
    (home / "config.json").write_text(config_text, encoding="utf-8")
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1, config_text


# ---------------------------------------------------------------------------
# B5 / B6 / B7 / B8 / B9
# ---------------------------------------------------------------------------

def test_b5_no_loop_marker_still_runs(tmp_path, stub_plugin):
    """#4350 (Lei, 2026-11-04): compaction after a passing nap is the default in every session. This cell
    used to assert that no loop marker meant no run; the loop-mode.json gate is gone."""
    home = make_home(tmp_path, loop=False)
    done = stop(home, stub_plugin)
    assert done.returncode == 0
    assert stub_calls(home) == [["--session", SESSION]]


@pytest.mark.parametrize("consumed", [False, True], ids=["pending", "consumed"])
def test_b6_a_request_since_the_checkpoint_means_no_run(consumed, tmp_path, stub_plugin):
    home = make_home(tmp_path)
    body = {"requested_at": "2026-10-21T10:05:00Z", "session_id": SESSION}
    if consumed:
        body["consumed"] = True
    _write_json(home / "compact" / "request.json", body)
    stop(home, stub_plugin)
    assert stub_calls(home) == []
    assert not (home / "compact" / "backstop-last.json").exists()


def test_b6b_a_request_at_exactly_the_checkpoint_instant_counts(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "compact" / "request.json", {"requested_at": "2026-10-21T10:00:00Z"})
    stop(home, stub_plugin)
    assert stub_calls(home) == []


def test_b6c_a_request_older_than_the_checkpoint_does_not_block(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "compact" / "request.json", {"requested_at": "2026-10-21T09:59:59Z"})
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def _tombstone(home: Path, mtime_offset: int, **extra) -> None:
    """The mod's consumed tombstone: {consumed, request_id}, no time of its own; only the mtime dates it."""
    path = home / "compact" / "request.json"
    _write_json(path, {"consumed": True, "request_id": "abc", **extra})
    os.utime(path, (CHECKPOINT_EPOCH + mtime_offset, CHECKPOINT_EPOCH + mtime_offset))


@pytest.mark.parametrize("offset", [60, 0], ids=["after_checkpoint", "at_checkpoint"])
def test_b6d_a_consumed_tombstone_written_since_the_checkpoint_means_no_run(offset, tmp_path, stub_plugin):
    """No last-compact-at stamp here: PostCompact has not fired yet, or the compaction was vetoed."""
    home = make_home(tmp_path)
    _tombstone(home, offset)
    stop(home, stub_plugin)
    assert stub_calls(home) == []
    assert not (home / "compact" / "backstop-last.json").exists()


def test_b6e_a_consumed_tombstone_older_than_the_checkpoint_does_not_block(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _tombstone(home, -60)
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def test_b6f_a_request_file_that_does_not_parse_reads_as_absent(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    path = home / "compact" / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{torn", encoding="utf-8")
    os.utime(path, (CHECKPOINT_EPOCH + 60, CHECKPOINT_EPOCH + 60))
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def test_b6g_a_pending_request_is_dated_by_requested_at_not_by_mtime(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    path = home / "compact" / "request.json"
    _write_json(path, {"requested_at": "2026-10-21T09:59:59Z"})  # older than the checkpoint
    os.utime(path, (CHECKPOINT_EPOCH + 60, CHECKPOINT_EPOCH + 60))  # but written "after" it
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 1


def test_b10_a_record_from_another_session_means_no_run(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    stop(home, stub_plugin, session_id="some-other-session")
    assert stub_calls(home) == []
    assert not (home / "compact" / "backstop-last.json").exists()


NO_SESSION_LINE = "engram nap-compact-backstop: the nap record carries no session_id; not requesting compaction"
_ABSENT = object()


@pytest.mark.parametrize("record_session", [None, _ABSENT, "", 5], ids=["null", "absent", "empty", "non-string"])
def test_b10b_a_record_without_a_session_means_no_run(record_session, tmp_path, stub_plugin):
    record = {"checkpoint_at": HEADER_TS, "backup_ok": True}
    if record_session is not _ABSENT:
        record["session_id"] = record_session
    home = make_home(tmp_path, record=record)
    done = stop(home, stub_plugin, session_id="whatever")
    assert done.returncode == 0
    assert stub_calls(home) == []
    assert request(home) is None
    assert done.stdout == ""
    assert done.stderr.splitlines() == [NO_SESSION_LINE]
    assert not (home / "compact" / "backstop-last.json").exists()


def test_b10d_payload_without_session_and_record_without_session_prints_only_the_payload_line(tmp_path, stub_plugin):
    home = make_home(tmp_path, record={"checkpoint_at": HEADER_TS, "session_id": None, "backup_ok": True})
    done = stop(home, stub_plugin, session_id=None)
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr.splitlines() == [
        "engram nap-compact-backstop: the Stop payload carries no session_id; not requesting compaction"
    ]
    assert stub_calls(home) == []


def test_b10c_matching_sessions_run(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    stop(home, stub_plugin, session_id=SESSION)
    assert stub_calls(home) == [["--session", SESSION]]


def test_b7_stdin_without_session_id_means_no_run_and_one_stderr_line(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    for payload in (None, "", "not json", "[]"):
        done = (stop(home, stub_plugin, session_id=None) if payload is None
                else run_hook(home, payload, plugin_root=stub_plugin))
        assert done.returncode == 0, payload
        err = [ln for ln in done.stderr.splitlines() if ln.strip()]
        assert len(err) == 1 and "session_id" in err[0], (payload, err)
    assert stub_calls(home) == []
    assert not (home / "compact" / "backstop-last.json").exists()


def test_b7b_missing_session_id_is_silent_when_nothing_was_due(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    _write_json(home / "config.json", {"compaction": {"nap_backstop": False}})  # nothing due (was: no loop marker, #4350)
    done = stop(home, stub_plugin, session_id=None)
    assert done.returncode == 0 and done.stderr.strip() == ""


def test_b8_exits_zero_even_when_compact_request_is_missing(tmp_path):
    home = make_home(tmp_path)
    root = tmp_path / "bare-plugin"
    _copy_hooks(root / "hooks")
    done = stop(home, root, unset_plugin_root=True)
    assert done.returncode == 0
    err = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(err) == 1 and "compact-request" in err[0], err
    assert request(home) is None


def test_b9_deployed_flattened_layout_finds_the_tool_by_walk_parents(tmp_path):
    root = tmp_path / "deployed"
    _copy_hooks(root / "hooks")
    (root / "tools").mkdir()
    shutil.copy2(TOOL_PATH, root / "tools" / "compact-request.py")
    # #3863 step 3b: the tool resolves the home through its sibling tools/_owner_read.py (an essential-tier file every
    # build ships beside it, with the package marker), so a deployed-shaped tools/ dir carries both.
    for sibling in ("__init__.py", "_owner_read.py"):
        shutil.copy2(SRC_ENGRAM / "tools" / sibling, root / "tools" / sibling)
    home = make_home(tmp_path)
    done = stop(home, root, unset_plugin_root=True)
    assert done.returncode == 0, done.stderr
    assert request(home) is not None, done.stderr


def test_b9d_the_walk_stops_at_the_tree_root_and_ignores_a_tool_above_it(tmp_path):
    """Deployed layout with NO tool inside the plugin root; a decoy compact-request.py sits ABOVE it
    (#3983: a sibling lookup never climbs past its own tree root). Mutation: the old unbounded
    `Path(__file__).resolve().parents` loop finds the decoy, runs it, and this goes red."""
    outer = tmp_path / "outer"
    root = outer / "nested" / "plugin"
    _copy_hooks(root / "hooks")
    shutil.copy2(SRC_ENGRAM / "engram_core.py", root / "engram_core.py")  # the tree-root marker
    (outer / "tools").mkdir(parents=True)
    (outer / "tools" / "compact-request.py").write_text(STUB_TOOL, encoding="utf-8")
    (outer / "nested" / "tools").mkdir()
    (outer / "nested" / "tools" / "compact-request.py").write_text(STUB_TOOL, encoding="utf-8")
    home = make_home(tmp_path)
    done = stop(home, root, unset_plugin_root=True)
    assert done.returncode == 0
    assert stub_calls(home) == [], "a tool above the tree root was run"
    err = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(err) == 1 and "compact-request" in err[0] and "not found" in err[0], err


def test_b9b_plugin_root_is_preferred_when_set(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    stop(home, stub_plugin)  # CLAUDE_PLUGIN_ROOT -> the stub root; the hook file there walks to the same root
    assert len(stub_calls(home)) == 1


def test_b9c_plugin_root_wins_over_the_walk(tmp_path, stub_plugin):
    """Hook run from the source tree (walk would find the REAL tool); the plugin root points at the stub."""
    home = make_home(tmp_path)
    done = stop(home, stub_plugin, hook_dir=HOOK_DIR)
    assert done.returncode == 0, done.stderr
    assert len(stub_calls(home)) == 1
    assert request(home) is None


# ---------------------------------------------------------------------------
# M1  state files keep a widened mode (ACL'd accounts must not be revoked by a 0600 rewrite)
# ---------------------------------------------------------------------------

def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_m1_backstop_last_keeps_a_widened_mode_across_a_second_write(tmp_path, stub_plugin):
    home = make_home(tmp_path)
    (home / "stub-exit").write_text("3")
    stop(home, stub_plugin)
    target = home / "compact" / "backstop-last.json"
    os.chmod(target, 0o660)
    touch_briefing(home)  # re-arm: a second attempt rewrites the file
    stop(home, stub_plugin)
    assert len(stub_calls(home)) == 2
    assert _mode(target) == 0o660


def test_m1_last_checkpoint_keeps_a_widened_mode_across_a_second_write():
    server = fresh_server()
    _checkpoint(server, OK_VC, session_id="one")
    target = Path(server.core.DATA_DIR) / "compact" / "last-checkpoint.json"
    os.chmod(target, 0o660)
    _checkpoint(server, OK_VC, session_id="two")
    assert _record(server)["session_id"] == "two"
    assert _mode(target) == 0o660


def test_m1_a_new_state_file_is_not_forced_to_0600(tmp_path, stub_plugin):
    """A new file takes what the umask (or the directory's default ACL) gives, not mkstemp's 0600."""
    home = make_home(tmp_path)
    (home / "stub-exit").write_text("3")
    old = os.umask(0o002)
    try:
        stop(home, stub_plugin)
    finally:
        os.umask(old)
    assert _mode(home / "compact" / "backstop-last.json") == 0o664


# ---------------------------------------------------------------------------
# C2  compact-request.py without --backup-ok
# ---------------------------------------------------------------------------

def run_tool(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), "--session", SESSION, *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_c2_omitted_flag_with_a_valid_record_proceeds(tmp_path):
    home = make_home(tmp_path)
    done = run_tool(home)
    assert done.returncode == 0, done.stderr
    assert request(home) is not None


def test_c2_omitted_flag_with_a_record_saying_backup_failed_refuses(tmp_path):
    home = make_home(tmp_path, record={"checkpoint_at": HEADER_TS, "session_id": SESSION, "backup_ok": False})
    done = run_tool(home)
    assert done.returncode == 3 and "backup" in done.stderr.lower()
    assert request(home) is None


def test_c2_omitted_flag_with_no_record_refuses(tmp_path):
    home = make_home(tmp_path, record=None)
    done = run_tool(home)
    assert done.returncode == 3
    assert "no checkpoint record for the latest checkpoint" in done.stderr
    assert request(home) is None


def test_c2_a_record_from_an_older_checkpoint_refuses(tmp_path):
    home = make_home(tmp_path, record={"checkpoint_at": "2026-10-21T08:00:00", "session_id": SESSION, "backup_ok": True})
    done = run_tool(home)
    assert done.returncode == 3
    assert "no checkpoint record for the latest checkpoint" in done.stderr
    assert request(home) is None


@pytest.mark.parametrize("bad", [
    {"session_id": SESSION, "backup_ok": True},
    {"checkpoint_at": HEADER_TS, "session_id": SESSION},
    {"checkpoint_at": HEADER_TS, "session_id": SESSION, "backup_ok": "true"},
    {"checkpoint_at": "garbage", "session_id": SESSION, "backup_ok": True},
])
def test_c2_a_malformed_record_is_no_record(bad, tmp_path):
    home = make_home(tmp_path, record=bad)
    done = run_tool(home)
    assert done.returncode == 3 and "no checkpoint record" in done.stderr


@pytest.mark.parametrize("flag,recorded", [("true", False), ("false", True)])
def test_c2_a_flag_disagreeing_with_a_valid_record_refuses(flag, recorded, tmp_path):
    home = make_home(tmp_path, record={"checkpoint_at": HEADER_TS, "session_id": SESSION, "backup_ok": recorded})
    done = run_tool(home, "--backup-ok", flag)
    assert done.returncode == 3 and "disagree" in done.stderr.lower(), done.stderr
    assert request(home) is None


def test_c2_a_flag_agreeing_with_the_record_proceeds(tmp_path):
    home = make_home(tmp_path)
    done = run_tool(home, "--backup-ok", "true")
    assert done.returncode == 0, done.stderr


def test_c2_flag_given_with_no_valid_record_is_todays_behaviour(tmp_path):
    home = make_home(tmp_path, record=None)
    assert run_tool(home, "--backup-ok", "true").returncode == 0
    home2_dir = tmp_path / "second"
    home2_dir.mkdir()
    home2 = make_home(home2_dir, record=None)
    refused = run_tool(home2, "--backup-ok", "false")
    assert refused.returncode == 3 and "backup is not ok" in refused.stderr


def test_c2_flag_given_with_an_older_record_is_todays_behaviour(tmp_path):
    home = make_home(tmp_path, record={"checkpoint_at": "2026-10-21T08:00:00", "session_id": SESSION, "backup_ok": False})
    done = run_tool(home, "--backup-ok", "true")
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------------
# C1 / C3  the checkpoint writes its verdict
# ---------------------------------------------------------------------------

CKPT_DIR = tempfile.mkdtemp(prefix="engram_test_4012_ckpt_")
atexit.register(lambda: shutil.rmtree(CKPT_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(CKPT_DIR):
        shutil.rmtree(CKPT_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(CKPT_DIR)
    server._ensure_data_dir()
    return server


def _header_ts(server) -> str:
    import re
    text = Path(server.core.LOG_PATH).read_text(encoding="utf-8")
    return re.findall(r"^## Nap \(turn \d+\) — (\S+)", text, re.MULTILINE)[-1]


def _checkpoint(server, vc: dict | None, session_id="sess-c1"):
    import engram_core as core
    payload = {"message": "a nap summary", "session_id": session_id}
    if vc is None:
        return json.loads(server.engram_checkpoint(json.dumps(payload)))
    with mock.patch.object(core, "_commit_snapshot", return_value=vc):
        return json.loads(server.engram_checkpoint(json.dumps(payload)))


def _record(server) -> dict:
    return json.loads((Path(server.core.DATA_DIR) / "compact" / "last-checkpoint.json").read_text())


OK_VC = {"db_backup": {"path": "x", "bytes": 3}, "binary_backup": {"path": "y"}}


def test_c1_checkpoint_writes_the_record_with_the_header_timestamp_and_session():
    server = fresh_server()
    result = _checkpoint(server, OK_VC)
    assert "error" not in result
    rec = _record(server)
    assert rec == {"checkpoint_at": _header_ts(server), "session_id": "sess-c1", "backup_ok": True}
    # the call's return exposes the same value, so the skill can tell this nap's record from the last one's
    assert result["checkpoint_at"] == rec["checkpoint_at"]


def test_c1_session_id_may_be_null():
    server = fresh_server()
    import engram_core as core
    with mock.patch.object(core, "_commit_snapshot", return_value=OK_VC):
        server.engram_checkpoint(json.dumps({"message": "m"}))
    assert _record(server)["session_id"] is None


@pytest.mark.parametrize(
    "cell,vc,expected",
    [
        ("both_ok", OK_VC, True),
        ("skipped_is_ok", {"db_backup": {"skipped": "same day", "path": "x"}, "binary_backup": {"path": "y"}}, True),
        ("db_error", {"db_backup": {"error": "disk full"}, "binary_backup": {"path": "y"}}, False),
        ("binary_error", {"db_backup": {"path": "x"}, "binary_backup": {"error": "git failed"}}, False),
        ("both_error", {"db_backup": {"error": "a"}, "binary_backup": {"error": "b"}}, False),
    ],
)
def test_c1_backup_ok_follows_the_error_key_rule(cell, vc, expected):
    server = fresh_server()
    _checkpoint(server, vc)
    assert _record(server)["backup_ok"] is expected, cell


def test_c1_record_is_replaced_by_the_next_checkpoint():
    server = fresh_server()
    _checkpoint(server, OK_VC, session_id="first")
    _checkpoint(server, {"db_backup": {"error": "x"}, "binary_backup": {}}, session_id="second")
    rec = _record(server)
    assert rec["session_id"] == "second" and rec["backup_ok"] is False
    assert not list((Path(server.core.DATA_DIR) / "compact").glob(".tmp*")), "no stray temp file"


def test_c1_a_sleep_checkpoint_does_not_write_the_nap_record():
    server = fresh_server()
    import engram_core as core
    with mock.patch.object(core, "_commit_snapshot", return_value=OK_VC):
        server.engram_advance_turn(json.dumps({"message": "sleep"}))
    assert not (Path(server.core.DATA_DIR) / "compact" / "last-checkpoint.json").exists()


def test_c3_a_record_write_failure_leaves_the_checkpoint_result_unchanged(capsys):
    server = fresh_server()
    healthy = _checkpoint(server, OK_VC)
    server = fresh_server()
    (Path(server.core.DATA_DIR) / "compact").write_text("a file where the directory should be")
    broken = _checkpoint(server, OK_VC)
    assert "error" not in broken
    assert set(broken) == set(healthy)
    assert broken["mode"] == "nap" and broken["version_control"] == OK_VC
    err = [ln for ln in capsys.readouterr().err.splitlines() if "last-checkpoint" in ln]
    assert len(err) == 1, err
    # the nap header was still written
    assert _header_ts(server)
