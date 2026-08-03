#!/usr/bin/env python3
"""codex-umwelt-wake — same-thread Umwelt wake bridge for Codex app-server.

Local watcher only: polls Umwelt for addressed crossings / granted mentions,
queues stable event IDs durably, and starts a new SAME-THREAD Codex turn over
an already-running local app-server when the target thread is idle.

Transport selection:
- `unix://` (default) — talk directly to the managed local app-server over its
  Unix domain socket, auto-discovering the daemon control socket.
- `unix:///abs/path.sock` — direct websocket-over-UDS to an explicit socket.
- `proxy://` / `proxy:///abs/path.sock` — same websocket framing, tunneled
  through `codex app-server proxy`.
- `ws://127.0.0.1:4500` / `wss://…` — explicit websocket endpoint.

Typical usage:
  python3 tools/codex-umwelt-wake.py start
  python3 tools/codex-umwelt-wake.py status
  python3 tools/codex-umwelt-wake.py stop

Environment overrides:
  ENGRAM_HOME                       default ~/.engram
  CODEX_APP_SERVER                  default unix://
  CODEX_THREAD_ID                   explicit thread override
  UMWELT_CONFIG                     default ~/.umwelt/config.json
  CODEX_UMWELT_WAKE_INTERVAL        default 2.0 seconds
  CODEX_UMWELT_WAKE_NO_REAP         non-empty => skip single-instance reap
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import select
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.sync.client import connect as ws_connect

ENGRAM_HOME = Path(os.environ.get("ENGRAM_HOME", Path.home() / ".engram"))
DEFAULT_STATE_DIR = ENGRAM_HOME / "codex-wake"
DEFAULT_APP_SERVER = os.environ.get("CODEX_APP_SERVER", "unix://")
DEFAULT_INTERVAL = float(os.environ.get("CODEX_UMWELT_WAKE_INTERVAL", "2.0"))
DEFAULT_UMWELT_CONFIG = Path(os.environ.get("UMWELT_CONFIG", Path.home() / ".umwelt" / "config.json"))
DEFAULT_THREAD_OVERRIDE = os.environ.get("CODEX_THREAD_ID")
STATUS_VERSION = 1
STATE_VERSION = 3
MAX_RECENT_DELIVERED = 256
HTTP_READ_LIMIT = 64 * 1024
HTTP_OPEN_TIMEOUT = 5
WS_OPEN_TIMEOUT = 5
REPLY_TIMEOUT = 10
WS_MESSAGE_LIMIT = 32 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_STOP_REQUESTED = False


@dataclass(frozen=True)
class RuntimePaths:
    state_dir: Path
    state_path: Path
    status_path: Path
    log_path: Path
    pid_path: Path


@dataclass(frozen=True)
class UmweltContext:
    self_name: str
    world_url: str


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    detail: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class TransportTarget:
    kind: str
    value: str


@dataclass(frozen=True)
class SessionCandidate:
    thread_id: str
    marker_path: Path | None
    transcript_path: Path | None
    recency: tuple[float, float, float]


@dataclass(frozen=True)
class ThreadResolution:
    thread_id: str
    status: str


class RpcError(RuntimeError):
    """JSON-RPC error returned by the Codex app-server."""


class Logger:
    def __init__(self, path: Path, *, stdout: bool) -> None:
        self.path = path
        self.stdout = stdout
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"{utc_now()} {message}"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.stdout:
            print(line, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def bounded_meta(value: Any, limit: int = 72) -> str:
    text = str(value or "unknown").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "unknown"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def default_state() -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "seeded": False,
        "cursors": {},
        "queue": [],
        "delivered_event_ids": [],
        "inflight_batch": None,
        "active_wake": None,
    }


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = default_state()
    if not isinstance(state, dict):
        return normalized
    normalized.update(state)
    if not isinstance(normalized.get("cursors"), dict):
        normalized["cursors"] = {}
    if not isinstance(normalized.get("queue"), list):
        normalized["queue"] = []
    if not isinstance(normalized.get("delivered_event_ids"), list):
        normalized["delivered_event_ids"] = []
    inflight = normalized.get("inflight_batch")
    if inflight is not None and not isinstance(inflight, dict):
        normalized["inflight_batch"] = None
    active_wake = normalized.get("active_wake")
    if active_wake is not None and not isinstance(active_wake, dict):
        normalized["active_wake"] = None
    normalized["state_version"] = STATE_VERSION
    return normalized


def default_status(paths: RuntimePaths) -> dict[str, Any]:
    return {
        "status_version": STATUS_VERSION,
        "running": False,
        "pid": None,
        "started_at": None,
        "last_cycle_at": None,
        "mode": "stopped",
        "detail": None,
        "queue_depth": 0,
        "seeded": False,
        "known_places": 0,
        "thread_id": None,
        "app_server": DEFAULT_APP_SERVER,
        "transport": None,
        "state_path": str(paths.state_path),
        "log_path": str(paths.log_path),
    }


def remember_delivered_id(state: dict[str, Any], event_id: str) -> None:
    delivered = [x for x in state.get("delivered_event_ids", []) if isinstance(x, str)]
    if event_id in delivered:
        delivered.remove(event_id)
    delivered.append(event_id)
    state["delivered_event_ids"] = delivered[-MAX_RECENT_DELIVERED:]


def queue_event_ids(state: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in state.get("queue", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return ids


def delivered_event_ids(state: dict[str, Any]) -> set[str]:
    return {x for x in state.get("delivered_event_ids", []) if isinstance(x, str)}


def event_ref(crossing: dict[str, Any]) -> str:
    if crossing.get("seq") is not None:
        return str(crossing["seq"])
    if crossing.get("id") is not None:
        return str(crossing["id"])
    filtered = {
        key: crossing.get(key)
        for key in (
            "sender",
            "to_agent",
            "mentions",
            "wakes_granted",
            "wake_granted",
            "viewer_relation",
            "created_at",
            "sent_at",
        )
        if key in crossing
    }
    return stable_hash(json.dumps(filtered, sort_keys=True, ensure_ascii=False), 24)


def relevant_crossing(crossing: dict[str, Any], self_name: str) -> str | None:
    mentions = {
        str(x).casefold()
        for x in (crossing.get("mentions") or [])
        if x is not None
    }
    wakes_granted = {
        str(x).casefold()
        for x in (crossing.get("wakes_granted") or [])
        if x is not None
    }
    is_addressee = crossing.get("viewer_relation") == "addressee"
    is_mentioned = self_name.casefold() in mentions
    if not (is_addressee or is_mentioned):
        return None
    granted = False
    if is_addressee:
        granted = bool(crossing.get("wake_granted"))
    if self_name.casefold() in wakes_granted:
        granted = True
    return "gong" if granted else "message"


def build_event(place_id: str, crossing: dict[str, Any], self_name: str) -> dict[str, Any] | None:
    kind = relevant_crossing(crossing, self_name)
    if kind is None:
        return None
    ref = event_ref(crossing)
    event_id = f"umwelt-crossing:{place_id}:{ref}"
    return {
        "id": event_id,
        "kind": kind,
        "place_id": bounded_meta(place_id),
        "sender": bounded_meta(crossing.get("sender")),
        "crossing_ref": bounded_meta(ref, 48),
        "prompt_token": stable_hash(event_id, 12),
    }


def enqueue_event(state: dict[str, Any], event: dict[str, Any], logger: Logger | None = None) -> bool:
    event_id = event["id"]
    if event_id in queue_event_ids(state) or event_id in delivered_event_ids(state):
        return False
    state.setdefault("queue", []).append(event)
    if logger is not None:
        logger.log(f"queued {event['kind']} {event_id}")
    return True


def _batch_places(events: list[dict[str, Any]], limit: int = 4) -> str:
    seen: list[str] = []
    for event in events:
        place_id = bounded_meta(event.get("place_id"))
        if place_id not in seen:
            seen.append(place_id)
    shown = seen[:limit]
    suffix = ""
    if len(seen) > limit:
        suffix = f", +{len(seen) - limit} more"
    return ", ".join(shown) + suffix


def _batch_kinds(events: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        kind = bounded_meta(event.get("kind"), 24)
        counts[kind] = counts.get(kind, 0) + 1
    return ",".join(f"{kind}={counts[kind]}" for kind in sorted(counts))


def render_batch_prompt(events: list[dict[str, Any]]) -> str:
    if not events:
        raise ValueError("cannot render empty wake batch")
    batch_token = stable_hash("|".join(str(event.get("id")) for event in events), 12)
    count = len(events)
    kinds = _batch_kinds(events)
    places = _batch_places(events)
    noun = "event" if count == 1 else "events"
    return (
        f"[Umwelt wake batch {batch_token}: count={count} {noun}; kinds={kinds}; places={places}. "
        "Enter the world through the umwelt-login/world skills, sweep the attested source crossings in Umwelt, "
        "and continue from there. This notification is only a bounded batch summary; do not infer or repeat "
        "message contents from it.]"
    )


def render_prompt(event: dict[str, Any]) -> str:
    return render_batch_prompt([event])


def build_inflight_batch(queue: list[dict[str, Any]], thread_id: str) -> dict[str, Any]:
    captured_events = [event for event in queue if isinstance(event, dict) and isinstance(event.get("id"), str)]
    if not captured_events:
        raise ValueError("cannot build in-flight batch from empty queue")
    event_ids = [event["id"] for event in captured_events]
    return {
        "thread_id": thread_id,
        "event_ids": event_ids,
        "client_user_message_id": f"engram-umwelt-{stable_hash('|'.join(event_ids), 24)}",
        "prompt": render_batch_prompt(captured_events),
        "captured_at": utc_now(),
    }


def build_active_steer_batch(queue: list[dict[str, Any]], active_wake: dict[str, Any]) -> dict[str, Any]:
    captured_events = [event for event in queue if isinstance(event, dict) and isinstance(event.get("id"), str)]
    if not captured_events:
        raise ValueError("cannot build active steer batch from empty queue")
    event_ids = [event["id"] for event in captured_events]
    return {
        "thread_id": active_wake["thread_id"],
        "wake_turn_id": active_wake["wake_turn_id"],
        "event_ids": event_ids,
        "client_user_message_id": f"engram-umwelt-steer-{stable_hash('|'.join(event_ids), 24)}",
        "prompt": render_active_wake_prompt(captured_events),
        "captured_at": utc_now(),
    }


def queue_without_event_ids(queue: list[dict[str, Any]], event_ids: set[str]) -> list[dict[str, Any]]:
    return [
        item
        for item in queue
        if not (isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in event_ids)
    ]


def ack_event_ids(
    state: dict[str, Any],
    event_ids: list[str] | set[str],
    *,
    logger: Logger | None = None,
    log_label: str | None = None,
) -> None:
    event_ids = {
        event_id
        for event_id in event_ids
        if isinstance(event_id, str)
    }
    state["queue"] = queue_without_event_ids(state.get("queue", []), event_ids)
    for event_id in event_ids:
        if isinstance(event_id, str):
            remember_delivered_id(state, event_id)
    if logger is not None and event_ids and log_label:
        logger.log(f"acknowledged {log_label} with {len(event_ids)} events")


def commit_inflight_batch(state: dict[str, Any], inflight: dict[str, Any], logger: Logger | None = None) -> None:
    ack_event_ids(
        state,
        inflight.get("event_ids", []),
        logger=logger,
        log_label=f"wake batch {inflight.get('client_user_message_id')}",
    )
    state["inflight_batch"] = None


def render_active_wake_prompt(events: list[dict[str, Any]]) -> str:
    if not events:
        raise ValueError("cannot render empty active-wake batch")
    batch_token = stable_hash("|".join(str(event.get("id")) for event in events), 12)
    count = len(events)
    kinds = _batch_kinds(events)
    places = _batch_places(events)
    noun = "event" if count == 1 else "events"
    return (
        f"[Umwelt wake follow-up {batch_token}: count={count} {noun}; kinds={kinds}; places={places}. "
        "Additional Umwelt wake signals arrived while you were already handling the earlier wake. "
        "Stay in the same turn, sweep the attested source crossings in Umwelt, and continue from there. "
        "This notification is only a bounded batch summary; do not infer or repeat message contents from it.]"
    )


def build_active_wake(
    inflight: dict[str, Any],
    *,
    wake_turn_id: str,
    wake_turn_status: str | None = None,
) -> dict[str, Any]:
    return {
        "thread_id": inflight["thread_id"],
        "wake_turn_id": wake_turn_id,
        "client_user_message_id": inflight["client_user_message_id"],
        "accepted_event_ids": list(inflight.get("event_ids", [])),
        "accepted_at": utc_now(),
        "accepted_batch_token": stable_hash("|".join(inflight.get("event_ids", [])), 24),
        "wake_turn_status": wake_turn_status,
        "steer_state": "ready",
        "steer_blocked_reason": None,
        "steer_inflight": None,
    }


def active_wake_from_turn(
    inflight: dict[str, Any],
    turn: dict[str, Any],
) -> dict[str, Any]:
    turn_id = str(turn.get("id") or "").strip()
    if not turn_id:
        raise ValueError("cannot recover active wake without turn id")
    return build_active_wake(inflight, wake_turn_id=turn_id, wake_turn_status=bounded_meta(turn.get("status"), 24))


def event_kind(event: dict[str, Any]) -> str:
    return bounded_meta(event.get("kind"), 24)


def has_non_heartbeat_event(events: list[dict[str, Any]]) -> bool:
    return any(event_kind(event) != "heartbeat" for event in events)


def load_umwelt_context(path: Path) -> UmweltContext | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    self_name = str(data.get("agent") or "").strip().casefold()
    world_url = str(data.get("world_url") or "").strip().rstrip("/")
    if not self_name or not world_url:
        return None
    return UmweltContext(self_name=self_name, world_url=world_url)


class WorldClient:
    def __init__(self, context: UmweltContext):
        self.context = context

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.context.world_url + path
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected Umwelt payload shape from {path}")
        return data

    def places(self) -> list[str]:
        payload = self._request("/umwelt/places")
        places = payload.get("places") or []
        return [str(p["id"]) for p in places if isinstance(p, dict) and p.get("id")]

    def crossings(self, place_id: str, since: int | str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"agent": self.context.self_name, "place": place_id, "limit": 200}
        if since is not None:
            payload["since"] = since
        return self._request("/umwelt/crossings_here", payload)


def poll_umwelt(world: WorldClient, state: dict[str, Any], logger: Logger) -> str:
    places = world.places()
    cursors = state.setdefault("cursors", {})
    if not state.get("seeded"):
        for place_id in places:
            resp = world.crossings(place_id)
            cursors[place_id] = resp.get("to_position")
        state["seeded"] = True
        logger.log(f"armed for {world.context.self_name}: seeded {len(places)} places")
        return "seeded"

    for place_id in places:
        if place_id not in cursors:
            resp = world.crossings(place_id)
            cursors[place_id] = resp.get("to_position")
            continue

        resp = world.crossings(place_id, cursors.get(place_id))
        for crossing in resp.get("crossings", []) or []:
            if not isinstance(crossing, dict):
                continue
            event = build_event(place_id, crossing, world.context.self_name)
            if event is not None:
                enqueue_event(state, event, logger)
        if "to_position" in resp:
            cursors[place_id] = resp.get("to_position")
    return "polled"


def parse_started_at(marker: dict[str, Any]) -> float:
    started_at = marker.get("started_at")
    if not isinstance(started_at, str):
        return 0.0
    try:
        return datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return 0.0


def list_session_candidates(sessions_dir: Path) -> list[SessionCandidate]:
    candidates: list[SessionCandidate] = []
    for path in sessions_dir.glob("*.json"):
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(marker, dict):
            continue
        session_id = marker.get("session_id")
        transcript_path = marker.get("transcript_path")
        if marker.get("role") != "interactive":
            continue
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(transcript_path, str) or "/.codex/sessions/" not in transcript_path.replace("\\", "/"):
            continue
        transcript = Path(transcript_path)
        try:
            transcript_mtime = transcript.stat().st_mtime
        except OSError:
            transcript_mtime = 0.0
        candidates.append(
            SessionCandidate(
                thread_id=session_id,
                marker_path=path,
                transcript_path=transcript,
                recency=(parse_started_at(marker), transcript_mtime, path.stat().st_mtime),
            )
        )
    candidates.sort(key=lambda item: item.recency, reverse=True)
    return candidates


def discover_codex_thread_id(sessions_dir: Path, override: str | None = None) -> str | None:
    if override:
        return override
    candidates = list_session_candidates(sessions_dir)
    if not candidates:
        return None
    return candidates[0].thread_id


def _thread_status(thread_read_result: dict[str, Any]) -> str:
    return str((((thread_read_result.get("thread") or {}).get("status") or {}).get("type") or "")).strip()


def resolve_live_thread(
    rpc: BaseRpc,
    sessions_dir: Path,
    override: str | None = None,
) -> ThreadResolution | None:
    candidates = (
        [SessionCandidate(thread_id=override, marker_path=None, transcript_path=None, recency=(float("inf"), float("inf"), float("inf")))]
        if override
        else list_session_candidates(sessions_dir)
    )
    for candidate in candidates:
        try:
            result = rpc.call("thread/read", {"threadId": candidate.thread_id, "includeTurns": False})
        except Exception:
            continue
        status = _thread_status(result)
        if status in {"active", "idle"}:
            return ThreadResolution(thread_id=candidate.thread_id, status=status)
    return None


def thread_contains_client_message(thread_read_result: dict[str, Any], client_user_message_id: str) -> bool:
    turns = ((thread_read_result.get("thread") or {}).get("turns") or [])
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "userMessage" and item.get("clientId") == client_user_message_id:
                return True
    return False


def thread_turns(thread_read_result: dict[str, Any]) -> list[dict[str, Any]]:
    return [turn for turn in (((thread_read_result.get("thread") or {}).get("turns") or [])) if isinstance(turn, dict)]


def turn_contains_client_message(turn: dict[str, Any], client_user_message_id: str) -> bool:
    for item in turn.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "userMessage" and item.get("clientId") == client_user_message_id:
            return True
    return False


def locate_turn(thread_read_result: dict[str, Any], *, turn_id: str | None = None, client_user_message_id: str | None = None) -> dict[str, Any] | None:
    turns = thread_turns(thread_read_result)
    if turn_id:
        for turn in turns:
            if str(turn.get("id") or "") == turn_id:
                return turn
    if client_user_message_id:
        for turn in turns:
            if turn_contains_client_message(turn, client_user_message_id):
                return turn
    return None


def turn_status_text(turn: dict[str, Any] | None) -> str | None:
    if not isinstance(turn, dict):
        return None
    status = turn.get("status")
    if isinstance(status, str):
        return status
    if status is None:
        return None
    return str(status)


def turn_is_active(turn: dict[str, Any] | None) -> bool:
    if not isinstance(turn, dict):
        return False
    status = (turn_status_text(turn) or "").casefold()
    if status in {"inprogress", "active", "queued"}:
        return True
    return turn.get("completedAt") is None and status not in {"completed", "failed", "cancelled"}


def reconcile_inflight_batch(
    rpc: BaseRpc,
    inflight: dict[str, Any],
) -> tuple[str, bool]:
    result = rpc.call("thread/read", {"threadId": inflight["thread_id"], "includeTurns": True})
    status = _thread_status(result)
    return status, thread_contains_client_message(result, inflight["client_user_message_id"])


def reconcile_inflight_batch_detail(
    rpc: BaseRpc,
    inflight: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    result = rpc.call("thread/read", {"threadId": inflight["thread_id"], "includeTurns": True})
    status = _thread_status(result)
    turn = locate_turn(result, client_user_message_id=inflight["client_user_message_id"])
    return result, status, turn


def close_active_wake_interval(
    state: dict[str, Any],
    active_wake: dict[str, Any],
    *,
    logger: Logger | None = None,
) -> None:
    state["active_wake"] = None
    if logger is not None:
        logger.log(f"closed active wake interval {active_wake.get('client_user_message_id')}")


def clear_unconfirmed_steer(
    active_wake: dict[str, Any],
    *,
    logger: Logger | None = None,
) -> None:
    steer = active_wake.get("steer_inflight")
    if not isinstance(steer, dict):
        return
    if logger is not None:
        logger.log(
            "steer acceptance not confirmed; retaining queued follow-up batch "
            f"{steer.get('client_user_message_id')} for post-idle delivery"
        )
    active_wake["steer_inflight"] = None


def active_wake_wait_detail(active_wake: dict[str, Any], *, default: str) -> str:
    if active_wake.get("steer_inflight"):
        return "active wake steer batch pending reconciliation"
    if active_wake.get("steer_state") == "unsupported":
        reason = bounded_meta(active_wake.get("steer_blocked_reason"), 120)
        return f"active wake follow-up deferred until idle ({reason})"
    return default


def mark_active_wake_nonsteerable(active_wake: dict[str, Any], detail: str) -> None:
    active_wake["steer_state"] = "unsupported"
    active_wake["steer_blocked_reason"] = detail
    active_wake["steer_inflight"] = None


def steer_error_is_nonsteerable(exc: Exception) -> bool:
    return isinstance(exc, RpcError)


def daemon_socket_path() -> str | None:
    try:
        out = subprocess.run(
            ["codex", "app-server", "daemon", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except Exception:
        return None
    socket_path = data.get("socketPath")
    return socket_path if isinstance(socket_path, str) and socket_path else None


def resolve_transport_target(app_server: str) -> TransportTarget:
    parsed = urllib.parse.urlparse(app_server)
    if parsed.scheme in {"ws", "wss"}:
        return TransportTarget(kind="websocket", value=app_server)
    if parsed.scheme in {"proxy", "unix"}:
        sock = parsed.path or daemon_socket_path()
        if not sock:
            raise RuntimeError("managed app-server socket unavailable")
        return TransportTarget(kind=parsed.scheme, value=sock)
    raise RuntimeError(f"unsupported app-server transport: {app_server}")


class BaseRpc:
    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "engram_codex_umwelt_wake",
                    "title": "ENGRAM Codex Umwelt Wake",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        raise NotImplementedError

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class WebsocketRpc(BaseRpc):
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.conn = None
        self.request_id = 0

    def __enter__(self) -> "WebsocketRpc":
        self.conn = ws_connect(
            self.endpoint,
            open_timeout=WS_OPEN_TIMEOUT,
            close_timeout=WS_OPEN_TIMEOUT,
            ping_interval=None,
            max_size=WS_MESSAGE_LIMIT,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.conn is not None
        self.conn.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.conn is not None
        self.request_id += 1
        req_id = self.request_id
        self.conn.send(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }))
        deadline = time.monotonic() + REPLY_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timeout waiting for {method} reply")
            self.conn.socket.settimeout(remaining)
            raw = self.conn.recv()
            msg = json.loads(raw)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RpcError(json.dumps(msg["error"], ensure_ascii=False, sort_keys=True))
            result = msg.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"unexpected result shape for {method}: {type(result).__name__}")
            return result


class StreamIo:
    def read(self, size: int, timeout: float) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketStreamIo(StreamIo):
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def read(self, size: int, timeout: float) -> bytes:
        self.sock.settimeout(timeout)
        return self.sock.recv(size)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


class ProcessStreamIo(StreamIo):
    def __init__(self, proc: subprocess.Popen[bytes]):
        self.proc = proc

    def read(self, size: int, timeout: float) -> bytes:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("timeout waiting for proxy stream data")
        if hasattr(self.proc.stdout, "read1"):
            return self.proc.stdout.read1(size)
        return self.proc.stdout.read(size)

    def write(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)

    def stderr_text(self) -> str:
        if self.proc.stderr is None:
            return ""
        return self.proc.stderr.read().decode("utf-8", errors="replace").strip()


class StreamWebSocketRpc(BaseRpc):
    def __init__(self):
        self.io: StreamIo | None = None
        self.request_id = 0
        self._read_buffer = bytearray()
        self._close_sent = False

    def _connect(self) -> StreamIo:
        raise NotImplementedError

    def __enter__(self):
        self.io = self._connect()
        self._handshake()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.io is not None and not self._close_sent:
                self._send_frame(0x8, b"")
        except Exception:
            pass
        if self.io is not None:
            self.io.close()
            self.io = None

    def _read_more(self, timeout: float) -> None:
        assert self.io is not None
        chunk = self.io.read(65536, timeout)
        if not chunk:
            extra = ""
            if isinstance(self.io, ProcessStreamIo):
                stderr = self.io.stderr_text()
                if stderr:
                    extra = f": {stderr}"
            raise RuntimeError(f"websocket stream closed before reply{extra}")
        self._read_buffer.extend(chunk)
        if len(self._read_buffer) > WS_MESSAGE_LIMIT:
            raise RuntimeError("websocket buffer exceeded configured limit")

    def _read_exact(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._read_buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timeout reading websocket stream")
            self._read_more(remaining)
        data = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return data

    def _handshake(self) -> None:
        assert self.io is not None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.io.write(
            (
                "GET /rpc HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        deadline = time.monotonic() + HTTP_OPEN_TIMEOUT
        while b"\r\n\r\n" not in self._read_buffer:
            if len(self._read_buffer) > HTTP_READ_LIMIT:
                raise RuntimeError("websocket handshake headers exceeded limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timeout waiting for websocket handshake")
            self._read_more(remaining)
        header_bytes, rest = bytes(self._read_buffer).split(b"\r\n\r\n", 1)
        self._read_buffer = bytearray(rest)
        lines = header_bytes.decode("latin-1").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101"):
            raise RuntimeError(f"websocket upgrade failed: {lines[0] if lines else 'missing status line'}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("websocket upgrade returned unexpected accept key")
        if headers.get("upgrade", "").casefold() != "websocket":
            raise RuntimeError("websocket upgrade missing Upgrade: websocket")
        if "upgrade" not in headers.get("connection", "").casefold():
            raise RuntimeError("websocket upgrade missing Connection: Upgrade")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.io is not None
        mask = secrets.token_bytes(4)
        out = bytearray([(0x80 | opcode), 0x80])
        length = len(payload)
        if length < 126:
            out[1] |= length
        elif length < 65536:
            out[1] |= 126
            out.extend(struct.pack("!H", length))
        else:
            out[1] |= 127
            out.extend(struct.pack("!Q", length))
        out.extend(mask)
        out.extend(bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
        if opcode == 0x8:
            self._close_sent = True
        self.io.write(bytes(out))

    def _recv_frame(self, timeout: float) -> tuple[int, bool, bytes]:
        first_two = self._read_exact(2, timeout)
        first, second = first_two[0], first_two[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, timeout))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, timeout))[0]
        masked = bool(second & 0x80)
        mask = self._read_exact(4, timeout) if masked else b""
        if length > WS_MESSAGE_LIMIT:
            raise RuntimeError("websocket frame exceeded configured limit")
        payload = self._read_exact(length, timeout)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, fin, payload

    def _recv_json(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timeout waiting for websocket message")
            opcode, fin, payload = self._recv_frame(remaining)
            if not fin:
                raise RuntimeError("fragmented websocket messages are unsupported")
            if opcode == 0x1:
                msg = json.loads(payload.decode("utf-8"))
                if not isinstance(msg, dict):
                    raise RuntimeError("unexpected websocket JSON message shape")
                return msg
            if opcode == 0x8:
                detail = payload.decode("utf-8", errors="replace")
                raise RuntimeError(f"websocket peer closed connection: {detail}")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            raise RuntimeError(f"unsupported websocket opcode: {opcode}")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps({"method": method, "params": params}, separators=(",", ":")).encode("utf-8"))

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        req_id = self.request_id
        self._send_frame(
            0x1,
            json.dumps({"id": req_id, "method": method, "params": params}, separators=(",", ":")).encode("utf-8"),
        )
        deadline = time.monotonic() + REPLY_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timeout waiting for {method} reply")
            msg = self._recv_json(remaining)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RpcError(json.dumps(msg["error"], ensure_ascii=False, sort_keys=True))
            result = msg.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"unexpected result shape for {method}: {type(result).__name__}")
            return result


class UnixSocketRpc(StreamWebSocketRpc):
    def __init__(self, socket_path: str):
        super().__init__()
        self.socket_path = socket_path

    def _connect(self) -> StreamIo:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HTTP_OPEN_TIMEOUT)
        sock.connect(self.socket_path)
        return SocketStreamIo(sock)


class ProxyRpc(StreamWebSocketRpc):
    def __init__(self, socket_path: str):
        super().__init__()
        self.socket_path = socket_path

    def _connect(self) -> StreamIo:
        proc = subprocess.Popen(
            ["codex", "app-server", "proxy", "--sock", self.socket_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        return ProcessStreamIo(proc)


def open_rpc(transport: TransportTarget):
    if transport.kind == "websocket":
        return WebsocketRpc(transport.value)
    if transport.kind == "unix":
        return UnixSocketRpc(transport.value)
    if transport.kind == "proxy":
        return ProxyRpc(transport.value)
    raise RuntimeError(f"unsupported transport kind: {transport.kind}")


def attempt_delivery(
    state: dict[str, Any],
    *,
    app_server: str,
    sessions_dir: Path,
    thread_override: str | None,
    logger: Logger,
    transport_resolver=resolve_transport_target,
    rpc_opener=open_rpc,
    persist_state=None,
    after_inflight_persist=None,
    after_active_wake_persist=None,
    after_steer_inflight_persist=None,
) -> DeliveryResult:
    queue = state.get("queue", [])
    inflight = state.get("inflight_batch")
    active_wake = state.get("active_wake")
    if not queue and not inflight and not active_wake:
        return DeliveryResult(status="idle", detail="queue empty")

    try:
        transport = transport_resolver(app_server)
    except Exception as exc:
        return DeliveryResult(status="waiting_for_app_server", detail=str(exc), thread_id=discover_codex_thread_id(sessions_dir, thread_override))

    try:
        with rpc_opener(transport) as rpc:
            rpc.initialize()
            if inflight is not None:
                try:
                    thread_result, status, found_turn = reconcile_inflight_batch_detail(rpc, inflight)
                except Exception as exc:
                    return DeliveryResult(
                        status="waiting_for_thread",
                        detail=f"in-flight batch thread unavailable: {exc}",
                        thread_id=inflight["thread_id"],
                    )
                if found_turn is not None:
                    commit_inflight_batch(state, inflight, logger)
                    state["inflight_batch"] = None
                    if turn_is_active(found_turn):
                        state["active_wake"] = active_wake_from_turn(inflight, found_turn)
                    if persist_state is not None:
                        persist_state(state)
                    return DeliveryResult(
                        status="delivered",
                        detail=inflight["client_user_message_id"],
                        thread_id=inflight["thread_id"],
                    )
                if status != "idle":
                    if status in {"active"}:
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=f"in-flight batch pending; thread status is {status}",
                            thread_id=inflight["thread_id"],
                        )
                    return DeliveryResult(
                        status="waiting_for_thread",
                        detail=f"in-flight batch pinned to unavailable thread status {status or 'unknown'}",
                        thread_id=inflight["thread_id"],
                    )
            if active_wake is not None:
                try:
                    thread_result = rpc.call("thread/read", {"threadId": active_wake["thread_id"], "includeTurns": True})
                except Exception as exc:
                    return DeliveryResult(
                        status="waiting_for_thread",
                        detail=f"active wake thread unavailable: {exc}",
                        thread_id=active_wake["thread_id"],
                    )

                thread_status = _thread_status(thread_result)
                wake_turn = locate_turn(
                    thread_result,
                    turn_id=str(active_wake.get("wake_turn_id") or ""),
                    client_user_message_id=str(active_wake.get("client_user_message_id") or ""),
                )
                steer_inflight = active_wake.get("steer_inflight")
                if isinstance(steer_inflight, dict):
                    if thread_contains_client_message(thread_result, steer_inflight["client_user_message_id"]):
                        ack_event_ids(
                            state,
                            steer_inflight.get("event_ids", []),
                            logger=logger,
                            log_label=f"active wake steer {steer_inflight.get('client_user_message_id')}",
                        )
                        active_wake["steer_inflight"] = None
                        if persist_state is not None:
                            persist_state(state)
                    elif wake_turn is None or not turn_is_active(wake_turn):
                        clear_unconfirmed_steer(active_wake, logger=logger)
                        if persist_state is not None:
                            persist_state(state)

                if wake_turn is None:
                    if thread_status == "idle":
                        close_active_wake_interval(state, active_wake, logger=logger)
                        if persist_state is not None:
                            persist_state(state)
                        active_wake = None
                    else:
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=active_wake_wait_detail(
                                active_wake,
                                default="active wake turn not recoverable while thread remains active",
                            ),
                            thread_id=active_wake["thread_id"],
                        )
                elif turn_is_active(wake_turn):
                    active_wake["wake_turn_status"] = turn_status_text(wake_turn)
                    if not queue:
                        if persist_state is not None:
                            persist_state(state)
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=active_wake_wait_detail(active_wake, default="wake-owned turn still active"),
                            thread_id=active_wake["thread_id"],
                        )
                    captured_events = [
                        event for event in state.get("queue", [])
                        if isinstance(event, dict) and isinstance(event.get("id"), str)
                    ]
                    if not captured_events:
                        if persist_state is not None:
                            persist_state(state)
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=active_wake_wait_detail(active_wake, default="wake-owned turn still active"),
                            thread_id=active_wake["thread_id"],
                        )
                    if not has_non_heartbeat_event(captured_events):
                        if persist_state is not None:
                            persist_state(state)
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail="heartbeat-only wake batch is subsumed by the active wake interval",
                            thread_id=active_wake["thread_id"],
                        )
                    if active_wake.get("steer_state") == "unsupported":
                        if persist_state is not None:
                            persist_state(state)
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=active_wake_wait_detail(active_wake, default="active wake follow-up deferred until idle"),
                            thread_id=active_wake["thread_id"],
                        )
                    if active_wake.get("steer_inflight") is not None:
                        if persist_state is not None:
                            persist_state(state)
                        return DeliveryResult(
                            status="waiting_for_idle",
                            detail=active_wake_wait_detail(active_wake, default="active wake steer batch pending reconciliation"),
                            thread_id=active_wake["thread_id"],
                        )

                    steer_batch = build_active_steer_batch(captured_events, active_wake)
                    active_wake["steer_inflight"] = steer_batch
                    if persist_state is not None:
                        persist_state(state)
                    if after_steer_inflight_persist is not None:
                        after_steer_inflight_persist(state, steer_batch)
                    try:
                        rpc.call(
                            "turn/steer",
                            {
                                "threadId": active_wake["thread_id"],
                                "expectedTurnId": active_wake["wake_turn_id"],
                                "clientUserMessageId": steer_batch["client_user_message_id"],
                                "input": [{"type": "text", "text": steer_batch["prompt"]}],
                            },
                        )
                    except Exception as exc:
                        if steer_error_is_nonsteerable(exc):
                            mark_active_wake_nonsteerable(active_wake, str(exc))
                            if persist_state is not None:
                                persist_state(state)
                            return DeliveryResult(
                                status="waiting_for_idle",
                                detail=active_wake_wait_detail(
                                    active_wake,
                                    default="active wake follow-up deferred until idle",
                                ),
                                thread_id=active_wake["thread_id"],
                            )
                        return DeliveryResult(
                            status="wake_failed",
                            detail=str(exc),
                            thread_id=active_wake["thread_id"],
                        )

                    ack_event_ids(
                        state,
                        steer_batch.get("event_ids", []),
                        logger=logger,
                        log_label=f"active wake steer {steer_batch.get('client_user_message_id')}",
                    )
                    active_wake["steer_inflight"] = None
                    if persist_state is not None:
                        persist_state(state)
                    logger.log(f"steered active wake turn for {steer_batch['client_user_message_id']}")
                    return DeliveryResult(
                        status="delivered",
                        detail=steer_batch["client_user_message_id"],
                        thread_id=active_wake["thread_id"],
                    )
                else:
                    close_active_wake_interval(state, active_wake, logger=logger)
                    if persist_state is not None:
                        persist_state(state)
                    active_wake = None

            if inflight is None and state.get("active_wake") is None and not state.get("queue"):
                return DeliveryResult(status="idle", detail="queue empty")

            if inflight is None:
                resolution = resolve_live_thread(rpc, sessions_dir, thread_override)
                if resolution is None:
                    return DeliveryResult(
                        status="waiting_for_thread",
                        detail="no loaded interactive Codex thread discovered",
                    )
                if resolution.status != "idle":
                    return DeliveryResult(
                        status="waiting_for_idle",
                        detail=f"thread status is {resolution.status or 'unknown'}",
                        thread_id=resolution.thread_id,
                    )
                inflight = build_inflight_batch(state.get("queue", []), resolution.thread_id)
                state["inflight_batch"] = inflight
                if persist_state is not None:
                    persist_state(state)
                if after_inflight_persist is not None:
                    after_inflight_persist(state, inflight)

            turn_start_result = rpc.call(
                "turn/start",
                {
                    "threadId": inflight["thread_id"],
                    "clientUserMessageId": inflight["client_user_message_id"],
                    "input": [{"type": "text", "text": inflight["prompt"]}],
                },
            )
    except Exception as exc:
        return DeliveryResult(
            status="wake_failed",
            detail=str(exc),
            thread_id=inflight["thread_id"] if inflight else discover_codex_thread_id(sessions_dir, thread_override),
        )

    assert inflight is not None
    turn_id = str(((turn_start_result.get("turn") or {}).get("id") or "")).strip()
    if not turn_id:
        return DeliveryResult(
            status="wake_failed",
            detail="turn/start response missing turn.id",
            thread_id=inflight["thread_id"],
        )
    commit_inflight_batch(state, inflight, logger)
    state["inflight_batch"] = None
    state["active_wake"] = build_active_wake(
        inflight,
        wake_turn_id=turn_id,
        wake_turn_status=turn_status_text((turn_start_result.get("turn") or {})),
    )
    if persist_state is not None:
        persist_state(state)
    if after_active_wake_persist is not None:
        after_active_wake_persist(state, state["active_wake"])
    logger.log(f"started same-thread turn for {inflight['client_user_message_id']}")
    return DeliveryResult(status="delivered", detail=inflight["client_user_message_id"], thread_id=inflight["thread_id"])


def proc_cmdline(pid: int) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ("unknown", "")
    if out.returncode != 0:
        return ("gone", "")
    return ("ok", out.stdout.strip())


def _is_our_process(cmdline: str) -> bool:
    return "codex-umwelt-wake.py" in cmdline


def reap_prior(paths: RuntimePaths, logger: Logger | None = None) -> None:
    if os.environ.get("CODEX_UMWELT_WAKE_NO_REAP"):
        return
    try:
        old = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if old == os.getpid():
        return
    status, cmd = proc_cmdline(old)
    if status == "ok" and _is_our_process(cmd):
        try:
            os.kill(old, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if logger is not None:
            logger.log(f"reaped prior instance pid={old}")
    elif status == "unknown" and logger is not None:
        logger.log(f"could not verify prior pid={old}; leaving it untouched")


def write_pid(paths: RuntimePaths) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def remove_pid(paths: RuntimePaths) -> None:
    try:
        pid = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if pid == os.getpid():
        try:
            paths.pid_path.unlink()
        except OSError:
            pass


def update_status(paths: RuntimePaths, payload: dict[str, Any]) -> None:
    atomic_write_json(paths.status_path, payload)


def current_status(paths: RuntimePaths) -> dict[str, Any]:
    status = load_json(paths.status_path, default_status(paths))
    try:
        pid = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        pid = None
    running = False
    if pid:
        proc_status, cmdline = proc_cmdline(pid)
        running = proc_status == "ok" and _is_our_process(cmdline)
    status["running"] = running
    status["pid"] = pid if running else None
    return status


def run_cycle(
    *,
    paths: RuntimePaths,
    umwelt_config: Path,
    app_server: str,
    sessions_dir: Path,
    thread_override: str | None,
    logger: Logger,
    started_at: str,
) -> dict[str, Any]:
    state = normalize_state(load_json(paths.state_path, default_state()))
    base_status = default_status(paths)
    try:
        transport = resolve_transport_target(app_server)
        transport_desc = f"{transport.kind}:{transport.value}"
    except Exception:
        transport_desc = None

    base_status.update(
        {
            "running": True,
            "pid": os.getpid(),
            "started_at": started_at,
            "last_cycle_at": utc_now(),
            "app_server": app_server,
            "transport": transport_desc,
            "queue_depth": len(state.get("queue", [])),
            "seeded": bool(state.get("seeded")),
            "known_places": len(state.get("cursors", {})),
        }
    )

    context = load_umwelt_context(umwelt_config)
    if context is None:
        base_status.update({"mode": "waiting_for_umwelt", "detail": f"missing or invalid {umwelt_config}"})
        update_status(paths, base_status)
        return base_status

    try:
        poll_umwelt(WorldClient(context), state, logger)
        atomic_write_json(paths.state_path, state)
        result = attempt_delivery(
            state,
            app_server=app_server,
            sessions_dir=sessions_dir,
            thread_override=thread_override,
            logger=logger,
            persist_state=lambda current_state: atomic_write_json(paths.state_path, current_state),
            after_inflight_persist=lambda current_state, _inflight: atomic_write_json(paths.state_path, current_state),
        )
        atomic_write_json(paths.state_path, state)
        base_status.update(
            {
                "mode": result.status,
                "detail": result.detail,
                "thread_id": result.thread_id
                or (state.get("active_wake") or {}).get("thread_id")
                or (state.get("inflight_batch") or {}).get("thread_id")
                or discover_codex_thread_id(sessions_dir, thread_override),
                "queue_depth": len(state.get("queue", [])),
                "seeded": bool(state.get("seeded")),
                "known_places": len(state.get("cursors", {})),
            }
        )
        update_status(paths, base_status)
        return base_status
    except Exception as exc:
        base_status.update(
            {
                "mode": "cycle_failed",
                "detail": str(exc),
                "queue_depth": len(state.get("queue", [])),
                "seeded": bool(state.get("seeded")),
                "known_places": len(state.get("cursors", {})),
            }
        )
        update_status(paths, base_status)
        logger.log(f"cycle failed: {exc}")
        return base_status


def install_signal_handlers(paths: RuntimePaths, started_at: str) -> None:
    def _handle(signum, _frame):
        global _STOP_REQUESTED
        _STOP_REQUESTED = True
        status = current_status(paths)
        status.update(
            {
                "status_version": STATUS_VERSION,
                "started_at": started_at,
                "last_cycle_at": utc_now(),
                "mode": "stopping",
                "detail": f"received signal {signum}",
            }
        )
        update_status(paths, status)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def build_paths(state_dir: Path) -> RuntimePaths:
    return RuntimePaths(
        state_dir=state_dir,
        state_path=state_dir / "state.json",
        status_path=state_dir / "status.json",
        log_path=state_dir / "watcher.log",
        pid_path=state_dir / "watcher.pid",
    )


def cmd_run(args: argparse.Namespace) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    paths = build_paths(args.state_dir)
    logger = Logger(paths.log_path, stdout=not args.quiet_stdout)
    started_at = utc_now()

    if not args.once:
        reap_prior(paths, logger)
        write_pid(paths)
        install_signal_handlers(paths, started_at)

    try:
        while True:
            run_cycle(
                paths=paths,
                umwelt_config=args.umwelt_config,
                app_server=args.app_server,
                sessions_dir=args.sessions_dir,
                thread_override=args.thread_id,
                logger=logger,
                started_at=started_at,
            )
            if args.once or _STOP_REQUESTED:
                break
            time.sleep(args.interval)
    finally:
        if not args.once:
            remove_pid(paths)
            stopped = current_status(paths)
            stopped.update(
                {
                    "status_version": STATUS_VERSION,
                    "running": False,
                    "pid": None,
                    "started_at": started_at,
                    "last_cycle_at": utc_now(),
                    "mode": "stopped",
                    "detail": "process exited",
                }
            )
            update_status(paths, stopped)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    paths = build_paths(args.state_dir)
    logger = Logger(paths.log_path, stdout=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--state-dir",
        str(args.state_dir),
        "--sessions-dir",
        str(args.sessions_dir),
        "--umwelt-config",
        str(args.umwelt_config),
        "--app-server",
        args.app_server,
        "--interval",
        str(args.interval),
        "--quiet-stdout",
    ]
    if args.thread_id:
        cmd.extend(["--thread-id", args.thread_id])
    log_handle = paths.log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.log("spawned background watcher")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    paths = build_paths(args.state_dir)
    try:
        pid = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        print("codex-umwelt-wake: no pidfile", file=sys.stderr)
        return 1
    status, cmdline = proc_cmdline(pid)
    if status != "ok" or not _is_our_process(cmdline):
        print("codex-umwelt-wake: pidfile is stale", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = build_paths(args.state_dir)
    print(json.dumps(current_status(paths), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
        p.add_argument("--sessions-dir", type=Path, default=ENGRAM_HOME / "sessions")
        p.add_argument("--umwelt-config", type=Path, default=DEFAULT_UMWELT_CONFIG)
        p.add_argument("--app-server", default=DEFAULT_APP_SERVER)
        p.add_argument("--thread-id", default=DEFAULT_THREAD_OVERRIDE)
        p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    run = sub.add_parser("run", help="run the wake bridge in the foreground")
    add_common(run)
    run.add_argument("--once", action="store_true", help="run one cycle and exit")
    run.add_argument("--quiet-stdout", action="store_true", help="write logs only to the log file")
    run.set_defaults(func=cmd_run)

    start = sub.add_parser("start", help="spawn the wake bridge in the background")
    add_common(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the running wake bridge")
    stop.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="print the current watcher status JSON")
    status.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
