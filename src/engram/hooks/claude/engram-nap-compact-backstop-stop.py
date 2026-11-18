#!/usr/bin/env python3
"""Stop hook: the mechanical backstop for the nap skill's last step (#4012, #4350, part of #3640).

The engram-nap skill ends by running ``tools/compact-request.py`` (Step 9), which leaves the request
the compaction mod acts on. That step is prose an agent can skip. Lei, 2026-10-21: "make it
mechanical instead of needing your cognitive remember, like a step in a skill" -- and a hook is
"easier to turn this feature on and off". This hook runs the same tool when the evidence of a
finished nap exists and the skill path did not run. It runs in EVERY session, loop or not (Lei,
2026-11-04: "I'd like the auto-after-nap-compaction to be a default feature and power users can opt
out", #4350): the one opt-out is the switch in condition 1, which the skill's Step 9 reads too.

It runs ``compact-request.py --session <stdin session_id>`` (no ``--backup-ok``, so the checkpoint's
own record ``compact/last-checkpoint.json`` supplies the verdict) only when ALL of these hold:

  1. the switch is on: ``$ENGRAM_HOME/config.json`` -> ``compaction.nap_backstop``; an absent or
     unreadable config is ON, only a literal ``false`` is OFF;
  2. ``compact/last-checkpoint.json`` is a record with a parseable ``checkpoint_at`` that is later
     than ``last-compact-at.json``'s ``timestamp`` (a missing compact record counts as older);
  3. no request for this checkpoint exists. A pending ``compact/request.json`` counts when its
     ``requested_at`` is at or after ``checkpoint_at``; the mod's consumed tombstone
     (``{consumed, request_id}``) carries no time, so it counts when the file's mtime is at or after
     ``checkpoint_at``. ``compact-request.py`` removes any older request before it writes, so a
     tombstone that new can only belong to this nap or a later one. This holds whether or not
     PostCompact has stamped ``last-compact-at.json`` yet, and when a compaction was vetoed and
     PostCompact never stamps (the skill path already ran, or the backstop did). A request file
     that does not parse reads as absent, as the mod reads it;
  4. this exact (``checkpoint_at``, warm-briefing mtime) pair is not the one in
     ``compact/backstop-last.json``. Every attempt, success or refusal, is recorded there, so a
     refusal is not retried on every turn; a new checkpoint or a new briefing write re-arms it;
  5. the stdin payload carries a ``session_id`` (checked last, so a Stop with nothing due is silent);
  6. the record's ``session_id`` is a non-empty string equal to the payload's (a shared ENGRAM_HOME
     must not compact a session that did not nap). A record without one (null, absent, empty,
     non-string) is refused with one stderr line, because it cannot show which session napped (#4356),
     AND recorded in ``backstop-last.json`` as a refused attempt for the Stop's own session, so the
     #4350 surface shows it once to that session. An other-session record is not written there.

Every rider (warm-briefing anchor, non-empty arc, briefing mtime at or after the checkpoint, the
backup verdict) stays in ``compact-request.py``; none is restated here. This hook never reads or
writes knowledge.db.

Output: NOTHING on stdout on any path (a Stop hook's only sanctioned stdout is ``decision: block``,
which would force another turn; ``tests/test_hooks_json_stdout_audit.py`` forbids
``hookSpecificOutput`` for Stop). On success the outcome is in ``compact/backstop-last.json`` plus ONE
stderr line, ``nap backstop: compaction requested``; the compaction itself is the visible effect. On a
refusal, timeout or error, ONE stderr line naming the exit code and the tool's first stderr line, and
the record also carries ``reason`` (that first stderr line, or a short fixed string for a timeout or
error, at most 300 characters; a success records none). Stderr is not shown to the agent, so the
UserPromptSubmit surface hook reads that record on the next prompt and says so once per refused
attempt (#4350). It ALWAYS exits 0 and never blocks the turn.

``compact-request.py`` is found via ``$CLAUDE_PLUGIN_ROOT/tools/`` first, then by walking this
file's own tree (``_hooklib.bounded_parents``, never above the tree root, #3983) for
``tools/compact-request.py`` (the build flattens ``hooks/claude`` to ``hooks``,
so a fixed ``parents[N]`` would overshoot silently). The two state files are written through
``engram_config_io.write_config_json`` (a new file gets the umask / default-ACL mode, an existing
one keeps its mode), so they never revoke an ACL'd account's access the way a mkstemp +
os.replace write does.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import bounded_parents, engram_home_or_exit, open_regular_text, resolve_runtime_dir

ENGRAM_HOME = str(engram_home_or_exit())
COMPACT_DIR = os.path.join(ENGRAM_HOME, "compact")
TOOL_NAME = "compact-request.py"
TOOL_TIMEOUT_SECONDS = 20
SUCCESS_LINE = "nap backstop: compaction requested"

try:
    _runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engram_config_io.py")
    if _runtime_dir not in sys.path:
        sys.path.insert(0, _runtime_dir)
    from engram_config_io import write_config_json
except Exception:  # noqa: BLE001 - reported in main(); the hook then does nothing
    write_config_json = None


def _say(line: str) -> None:
    sys.stderr.write("engram nap-compact-backstop: %s\n" % (line,))


def _read_json(path: str):
    """The parsed JSON file, or None when it is absent, unreadable, not regular, or not JSON."""
    try:
        with open_regular_text(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _parse_ts(value) -> float | None:
    """An ISO timestamp as epoch seconds; a naive one is UTC (the session log's own convention)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _switch_on() -> bool:
    config = _read_json(os.path.join(ENGRAM_HOME, "config.json"))
    if not isinstance(config, dict):
        return True
    section = config.get("compaction")
    if not isinstance(section, dict):
        return True
    return section.get("nap_backstop") is not False


def _session_id() -> str | None:
    try:
        payload = json.loads(sys.stdin.read() or "")
    except ValueError:
        return None
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _find_tool() -> str | None:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        cand = os.path.join(root, "tools", TOOL_NAME)
        if os.path.isfile(cand):
            return cand
    # Own tree only: this file's directory up to and including its tree root, never above it (#3983).
    # (resolve_tools_dir would also work but inserts the tools dir into sys.path; this hook only
    # runs the tool as a subprocess and must not change its own import path.)
    try:
        for parent in bounded_parents(Path(__file__).resolve()):
            cand = parent / "tools" / TOOL_NAME
            if cand.is_file():
                return str(cand)
    except OSError:
        pass
    return None


def _briefing_mtime_ns() -> int | None:
    try:
        return os.stat(os.path.join(ENGRAM_HOME, "warm-briefing.md")).st_mtime_ns
    except OSError:
        return None


REASON_MAX_CHARS = 300
REASON_NO_SESSION = (
    "the last nap's record carries no session_id, so the backstop cannot tell which session napped "
    "(a checkpoint call without session_id, or a nap skill older than this fix); nap again through the "
    "current engram-nap skill, which passes it"
)


def _record_attempt(checkpoint_at: str, mtime_ns, outcome: str, exit_code, reason: str | None = None,
                    session_id: str | None = None) -> None:
    record = {
        "checkpoint_at": checkpoint_at,
        "briefing_mtime_ns": mtime_ns,
        "outcome": outcome,
        "exit_code": exit_code,
        "session_id": session_id,  # the Stop payload's session, so the surface hook shows it to that session only
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if reason:
        record["reason"] = reason[:REASON_MAX_CHARS]
    try:
        os.makedirs(COMPACT_DIR, exist_ok=True)
        write_config_json(os.path.join(COMPACT_DIR, "backstop-last.json"), record)
    except Exception as exc:  # noqa: BLE001
        _say("could not record the attempt: %s: %s" % (type(exc).__name__, exc))


def _request_exists_for(checkpoint: float) -> bool:
    """True when compact/request.json is a request for this checkpoint or a later one: a pending one by
    its ``requested_at``, a consumed tombstone (which has no time) by the file's mtime."""
    path = os.path.join(COMPACT_DIR, "request.json")
    request = _read_json(path)
    if not isinstance(request, dict):
        return False
    requested = _parse_ts(request.get("requested_at"))
    if requested is not None:
        return requested >= checkpoint
    try:
        return os.stat(path).st_mtime >= checkpoint
    except OSError:
        return False


def _due():
    """(checkpoint_at, briefing_mtime_ns, record_session_id) when every condition but the stdin
    session_id holds, else None."""
    if not _switch_on():
        return None
    record = _read_json(os.path.join(COMPACT_DIR, "last-checkpoint.json"))
    if not isinstance(record, dict):
        return None
    checkpoint_at = record.get("checkpoint_at")
    checkpoint = _parse_ts(checkpoint_at)
    if checkpoint is None:
        return None
    compact = _read_json(os.path.join(ENGRAM_HOME, "last-compact-at.json"))
    compacted = _parse_ts(compact.get("timestamp")) if isinstance(compact, dict) else None
    if compacted is not None and checkpoint <= compacted:
        return None
    if _request_exists_for(checkpoint):
        return None
    mtime_ns = _briefing_mtime_ns()
    last = _read_json(os.path.join(COMPACT_DIR, "backstop-last.json"))
    if isinstance(last, dict) and last.get("checkpoint_at") == checkpoint_at and last.get("briefing_mtime_ns") == mtime_ns:
        return None
    return checkpoint_at, mtime_ns, record.get("session_id")


def main() -> None:
    due = _due()
    if due is None:
        return
    checkpoint_at, mtime_ns, record_session = due
    session_id = _session_id()
    if session_id is None:
        _say("the Stop payload carries no session_id; not requesting compaction")
        return
    has_record_session = isinstance(record_session, str) and bool(record_session)
    if has_record_session and record_session != session_id:
        return  # another session's nap on a shared ENGRAM_HOME: not this Stop's to compact
    if write_config_json is None:
        _say("engram_config_io is not importable; not requesting compaction (the attempt could not be recorded)")
        return
    if not has_record_session:
        # cannot show which session napped (#4356): refuse, never guess. The refusal is recorded for THIS
        # Stop's session, so guard 4 holds the pair and the #4350 surface shows it once, to that session.
        _say("the nap record carries no session_id; not requesting compaction")
        _record_attempt(checkpoint_at, mtime_ns, "refused", None, REASON_NO_SESSION, session_id=session_id)
        return
    tool = _find_tool()
    if tool is None:
        _say("%s not found under CLAUDE_PLUGIN_ROOT/tools or any parent's tools/" % (TOOL_NAME,))
        _record_attempt(checkpoint_at, mtime_ns, "error", None, "%s not found" % (TOOL_NAME,), session_id=session_id)
        return
    try:
        done = subprocess.run(
            [sys.executable, tool, "--session", session_id],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS, env=dict(os.environ, ENGRAM_HOME=ENGRAM_HOME),
        )
    except subprocess.TimeoutExpired:
        _say("%s timed out after %ds" % (TOOL_NAME, TOOL_TIMEOUT_SECONDS))
        _record_attempt(checkpoint_at, mtime_ns, "timeout", None, "%s timed out after %ds" % (TOOL_NAME, TOOL_TIMEOUT_SECONDS), session_id=session_id)
        return
    except OSError as exc:
        _say("could not run %s: %s" % (TOOL_NAME, exc))
        _record_attempt(checkpoint_at, mtime_ns, "error", None, "could not run %s" % (TOOL_NAME,), session_id=session_id)
        return
    if done.returncode == 0:
        _record_attempt(checkpoint_at, mtime_ns, "written", 0, session_id=session_id)
        sys.stderr.write(SUCCESS_LINE + "\n")
        return
    first = next((ln.strip() for ln in done.stderr.splitlines() if ln.strip()), "")
    _say("%s exited %d: %s" % (TOOL_NAME, done.returncode, first))
    _record_attempt(checkpoint_at, mtime_ns, "refused" if done.returncode == 3 else "error", done.returncode,
                    first or "%s exited %d" % (TOOL_NAME, done.returncode), session_id=session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - a Stop hook must never fail the turn
        _say("unexpected %s: %s" % (type(exc).__name__, exc))
    sys.exit(0)
