"""ENGRAM event emitter — append-only structured event log.

Spec: alpha #175 (two-level logging architecture)
Tracking: alpha #175

This module is the single emit-side entry point for ENGRAM's append-only
event log. Hooks and server.py call Emitter.init() once per process and
emit() per interesting moment. Events are routed to per-session JSONL files
at ~/.engram/logs/sessions/<session_id>.l{1,2}.jsonl, partitioned by
privacy level (1 = stats-only / 2 = content, LOCAL ONLY).

Failure-mode contract (load-bearing): emit() NEVER raises into the caller.
All exceptions are caught, logged to stderr, and the event is dropped.
The emitter must not be a source of agent-impacting bugs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Module-level singleton — first init() call wins; subsequent calls return
# the existing instance bound to the same session context. This matches
# the "one emitter per process" semantics.
_EMITTER_INSTANCE: Optional["Emitter"] = None
_INIT_LOCK = threading.Lock()

# Turn-cache TTL: turn advances only at sleep, so refreshing from
# knowledge.db every emit() would be wasteful. Cache for 60s; lazy refresh.
_TURN_CACHE_TTL_SECONDS = 60

# Event-type namespace check. All event types must match engram.<domain>.<action>.
# Cheap regex equivalent without importing re — three dots required, all
# segments non-empty, starts with "engram.".
def _is_valid_event_type(et: str) -> bool:
    if not isinstance(et, str) or not et.startswith("engram."):
        return False
    parts = et.split(".")
    if len(parts) < 3:
        return False
    return all(parts)


class Emitter:
    """Append-only structured event emitter.

    Use Emitter.init() to construct or fetch the process singleton, then
    bind() to attach durable session context, then emit() per event.

    Thread-safety: emit() is safe to call from multiple threads. The
    underlying open-append-close per event is atomic on POSIX for line
    writes under PIPE_BUF (typically 4096 bytes); larger events use an
    OS-level lock.
    """

    def __init__(
        self,
        session_id: str,
        transcript_path: str,
        initial_turn: Optional[int],
        logs_dir: Path,
    ) -> None:
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.logs_dir = logs_dir
        self.sessions_dir = logs_dir / "sessions"
        self._bound_context: dict[str, Any] = {}
        self._write_lock = threading.Lock()
        self._engram_home = Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._stderr(f"could not create sessions dir {self.sessions_dir}: {exc}")
        # Seed turn cache. If caller passed an explicit initial_turn, honor
        # it; otherwise read from knowledge.db. Either way set cache time so
        # subsequent emits within TTL skip the db hit. Per DESIGN.md §5.
        if initial_turn is not None:
            self._turn = initial_turn
        else:
            self._turn = self._read_turn_from_db()
        self._turn_cached_at = time.time()

    @classmethod
    def init(
        cls,
        session_id: str,
        transcript_path: str,
        initial_turn: Optional[int] = None,
        logs_dir: Optional[Path] = None,
    ) -> "Emitter":
        """Idempotent: first call constructs, subsequent calls return same instance.

        If subsequent calls pass a DIFFERENT session_id, return the existing
        instance and log a warning — switching session mid-process is a bug.
        """
        global _EMITTER_INSTANCE
        with _INIT_LOCK:
            if _EMITTER_INSTANCE is None:
                if logs_dir is None:
                    engram_home = Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))
                    logs_dir = engram_home / "logs"
                _EMITTER_INSTANCE = cls(
                    session_id=session_id,
                    transcript_path=transcript_path,
                    initial_turn=initial_turn,
                    logs_dir=logs_dir,
                )
            elif _EMITTER_INSTANCE.session_id != session_id:
                _EMITTER_INSTANCE._stderr(
                    f"init() called with session_id={session_id} but instance "
                    f"already bound to {_EMITTER_INSTANCE.session_id}; "
                    f"returning existing instance"
                )
            return _EMITTER_INSTANCE

    @classmethod
    def get(cls) -> Optional["Emitter"]:
        """Return the singleton if initialized, else None. Use for opportunistic emit."""
        return _EMITTER_INSTANCE

    def bind(self, **kwargs: Any) -> None:
        """Attach durable context that auto-merges into every subsequent event's `data`.

        Typical use at session start:
            emitter.bind(model_id="claude-opus-4-7[1m]")
        """
        self._bound_context.update(kwargs)

    def emit(
        self,
        event_type: str,
        level: int,
        data: dict[str, Any],
        tool_use_id: Optional[str] = None,
        parent_uuid: Optional[str] = None,
    ) -> Optional[str]:
        """Emit one event. Returns the generated uuid for child-event linking, or None on failure.

        NEVER raises — caller code is never disrupted by emitter errors.
        """
        try:
            return self._emit_inner(event_type, level, data, tool_use_id, parent_uuid)
        except Exception as exc:
            self._stderr(f"emit({event_type}) FAILED: {exc!r}; event dropped")
            return None

    def _emit_inner(
        self,
        event_type: str,
        level: int,
        data: dict[str, Any],
        tool_use_id: Optional[str],
        parent_uuid: Optional[str],
    ) -> Optional[str]:
        # Validate
        if not _is_valid_event_type(event_type):
            self._stderr(f"invalid event_type {event_type!r}; must be engram.<domain>.<action>")
            return None
        if level not in (1, 2):
            self._stderr(f"invalid level {level}; must be 1 or 2")
            return None
        if not isinstance(data, dict):
            self._stderr(f"data must be dict, got {type(data).__name__}")
            return None

        # Enrich. Capture `now` once and derive both seconds and milliseconds
        # from the SAME datetime object — a previous version called now() twice
        # and could produce a malformed timestamp at sub-second boundaries
        # (one second ahead of the milliseconds reading) per round-1 fairy review.
        _now = datetime.now(timezone.utc)
        event_uuid = str(_uuid.uuid4())
        ts = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"
        merged_data = {**self._bound_context, **data}
        event = {
            "ts": ts,
            "sessionId": self.session_id,
            "turn": self._current_turn(),
            "uuid": event_uuid,
            "parentUuid": parent_uuid,
            "event_type": event_type,
            "tool_use_id": tool_use_id,
            "level": level,
            "data": merged_data,
        }

        # Privacy-filter: route to l1 or l2 file by level (separate files,
        # structural enforcement — see DESIGN.md §2)
        target = self.sessions_dir / f"{self.session_id}.l{level}.jsonl"

        # Serialize (compact form for efficiency)
        line = json.dumps(event, separators=(",", ":"))

        # Append (atomic on POSIX for sub-PIPE_BUF line writes; explicit
        # lock for larger lines to avoid interleave from concurrent threads)
        with self._write_lock:
            with open(target, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        return event_uuid

    def _current_turn(self) -> int:
        """Return cached turn, refreshing from knowledge.db if stale."""
        now = time.time()
        if now - self._turn_cached_at < _TURN_CACHE_TTL_SECONDS:
            return self._turn
        fresh = self._read_turn_from_db()
        if fresh is not None:
            self._turn = fresh
        self._turn_cached_at = now
        return self._turn

    def _read_turn_from_db(self) -> Optional[int]:
        """One-shot read of current_turn from ~/.engram/config.json (mirrors
        server.py:_get_current_turn — config.json is the canonical source of
        truth for the turn counter, NOT the nodes table). Returns None on
        any failure."""
        try:
            config_path = self._engram_home / "config.json"
            if not config_path.exists():
                return None
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            turn = config.get("memory", {}).get("current_turn")
            if turn is not None:
                return int(turn)
        except Exception as exc:
            self._stderr(f"turn read failed: {exc!r}")
        return None

    def _stderr(self, msg: str) -> None:
        """Failure logging — to stderr, never raise into caller."""
        try:
            print(f"[engram-emitter] {msg}", file=sys.stderr)
        except Exception:
            # Even stderr can fail (closed fd, etc.) — drop silently.
            pass


# Convenience function for opportunistic emit from sites that don't manage
# init lifecycle (e.g. server.py tool handlers). Returns None if no emitter
# is initialized — silent no-op, never raises.
def emit_if_initialized(
    event_type: str,
    level: int,
    data: dict[str, Any],
    tool_use_id: Optional[str] = None,
    parent_uuid: Optional[str] = None,
) -> Optional[str]:
    emitter = Emitter.get()
    if emitter is None:
        return None
    return emitter.emit(event_type, level, data, tool_use_id=tool_use_id, parent_uuid=parent_uuid)
