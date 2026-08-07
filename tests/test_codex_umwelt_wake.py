"""Tests for tools/codex-umwelt-wake.py.

Coverage required by issue #1802:
- relevance (addressed crossings + granted mentions);
- forward-looking seed, including newly created places;
- restart-safe dedupe;
- stale-marker rejection in favor of the newest loaded live thread;
- batched wake coalescing + bounded prompt summaries;
- in-flight crash recovery / exactly-once replay suppression;
- transport selection + framing for managed unix/proxy and explicit websocket;
- pid status checks must verify watcher identity, not mere /proc existence.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "engram" / "tools" / "codex-umwelt-wake.py"

spec = importlib.util.spec_from_file_location("codex_umwelt_wake", MODULE_PATH)
assert spec and spec.loader
wake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wake
spec.loader.exec_module(wake)


class FakeWorld:
    def __init__(self, place_snapshots, responses, self_name="mira"):
        self.place_snapshots = list(place_snapshots)
        self.responses = {k: list(v) for k, v in responses.items()}
        self.place_calls = 0
        self.crossing_calls = []
        self.context = type("Ctx", (), {"self_name": self_name})()

    def places(self):
        idx = min(self.place_calls, len(self.place_snapshots) - 1)
        self.place_calls += 1
        return list(self.place_snapshots[idx])

    def crossings(self, place_id, since=None):
        self.crossing_calls.append((place_id, since))
        key = (place_id, since)
        queue = self.responses.get(key)
        assert queue, f"no fake response queued for {key!r}"
        return queue.pop(0)


class ScriptedRpc:
    def __init__(self, *, method_responses=None, notifications=None):
        self.method_responses = {k: list(v) for k, v in (method_responses or {}).items()}
        self.notifications = list(notifications or [])
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def initialize(self):
        self.calls.append(("initialize", None))

    def call(self, method, params):
        self.calls.append((method, params))
        queue = self.method_responses.get(method, [])
        if queue:
            result = queue.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if method == "turn/start":
            return {"turn": {"id": "turn_123"}}
        raise AssertionError(f"unexpected method {method!r}")

    def recv_notification(self, timeout):
        self.calls.append(("recv_notification", timeout))
        if not self.notifications:
            raise TimeoutError("no notifications queued")
        result = self.notifications.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeIo(wake.StreamIo):
    def __init__(self, reads):
        self.reads = list(reads)
        self.writes = []
        self.closed = False

    def read(self, size: int, timeout: float) -> bytes:
        if not self.reads:
            raise TimeoutError("no more fake bytes queued")
        return self.reads.pop(0)

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


class FakeStreamRpc(wake.StreamWebSocketRpc):
    def __init__(self, fake_io: FakeIo):
        super().__init__()
        self.fake_io = fake_io

    def _connect(self):
        return self.fake_io


class FakeWebsocketConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.socket = self

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self):
        if not self.messages:
            raise TimeoutError("no more fake websocket messages queued")
        return self.messages.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class FakeWebsocketRpc(wake.WebsocketRpc):
    def __init__(self, fake_conn: FakeWebsocketConnection):
        super().__init__("ws://fake")
        self.fake_conn = fake_conn

    def __enter__(self):
        self.conn = self.fake_conn
        return self


@contextlib.contextmanager
def live_app_server(listen_url: str):
    proc = subprocess.Popen(
        ["codex", "app-server", "--listen", listen_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _logger(tmp_path):
    return wake.Logger(tmp_path / "watcher.log", stdout=False)


def _transport(_app_server):
    return wake.TransportTarget(kind="websocket", value="ws://127.0.0.1:4500")


def _event(ref: str, *, kind: str = "gong", place: str = "place_square"):
    return {
        "id": f"umwelt-crossing:{place}:{ref}",
        "kind": kind,
        "source": "umwelt",
        "place_id": place,
        "sender": "sol",
        "crossing_ref": ref,
        "prompt_token": wake.stable_hash(f"{place}:{ref}", 12),
    }


def _collab_item(
    *,
    sender_thread_id: str = "thread-1",
    item_status: str = "completed",
    tool: str = "spawnAgent",
    item_id: str = "collab-1",
    agent_statuses: dict[str, str] | None = None,
):
    agent_statuses = agent_statuses or {"thread-child": "completed"}
    return {
        "type": "collabAgentToolCall",
        "id": item_id,
        "senderThreadId": sender_thread_id,
        "receiverThreadIds": list(agent_statuses),
        "agentsStates": {child_thread_id: {"status": status} for child_thread_id, status in agent_statuses.items()},
        "status": item_status,
        "tool": tool,
    }


def _collab_item_completed_notification(
    *,
    thread_id: str = "thread-1",
    sender_thread_id: str = "thread-1",
    item_status: str = "completed",
    tool: str = "spawnAgent",
    item_id: str = "collab-1",
    agent_statuses: dict[str, str] | None = None,
):
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread_id,
            "turnId": "turn-123",
            "completedAtMs": 1234567890,
            "item": _collab_item(
                sender_thread_id=sender_thread_id,
                item_status=item_status,
                tool=tool,
                item_id=item_id,
                agent_statuses=agent_statuses,
            ),
        },
    }


def _thread_read(
    status: str,
    *,
    client_id: str | None = None,
    turn_id: str | None = None,
    turn_status: str | None = None,
    turns: list[dict] | None = None,
):
    if turns is not None:
        return {"thread": {"status": {"type": status}, "turns": turns}}
    items = []
    if client_id is not None:
        items = [{"type": "userMessage", "clientId": client_id, "content": [{"type": "text", "text": "wake"}]}]
    turns = []
    if client_id is not None or turn_id is not None:
        effective_turn_id = turn_id or "turn_123"
        effective_turn_status = turn_status or ("inProgress" if status == "active" else "completed")
        turns = [{"id": effective_turn_id, "status": effective_turn_status, "items": items}]
    return {"thread": {"status": {"type": status}, "turns": turns}}


def _server_frame(opcode: int, payload: bytes) -> bytes:
    out = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        out.append(length)
    elif length < 65536:
        out.append(126)
        out.extend(length.to_bytes(2, "big"))
    else:
        out.append(127)
        out.extend(length.to_bytes(8, "big"))
    out.extend(payload)
    return bytes(out)


def _parse_client_frame(frame: bytes):
    first, second = frame[0], frame[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    idx = 2
    if length == 126:
        length = int.from_bytes(frame[idx:idx + 2], "big")
        idx += 2
    elif length == 127:
        length = int.from_bytes(frame[idx:idx + 8], "big")
        idx += 8
    mask = frame[idx:idx + 4]
    idx += 4
    payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(frame[idx:idx + length]))
    return opcode, masked, payload


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _live_turn_smoke(app_server: str) -> None:
    transport = wake.resolve_transport_target(app_server)
    with wake.open_rpc(transport) as rpc:
        rpc.initialize()
        thread_id = rpc.call("thread/start", {})["thread"]["id"]
        client_id = f"engram-umwelt-live-{int(time.time() * 1000)}"
        rpc.call(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": client_id,
                "input": [{"type": "text", "text": "Reply with one word: ack"}],
            },
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            result = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})
            if wake.thread_contains_client_message(result, client_id):
                return
            time.sleep(1)
    raise AssertionError("live smoke never observed the clientUserMessageId in thread/read")


# ---------------------------------------------------------------------------
# relevance
# ---------------------------------------------------------------------------


def test_relevant_crossing_distinguishes_gong_message_and_irrelevant():
    gong = {
        "viewer_relation": "broadcast",
        "mentions": ["mira"],
        "wakes_granted": ["mira"],
    }
    msg = {
        "viewer_relation": "addressee",
        "wake_granted": False,
        "mentions": [],
        "wakes_granted": [],
    }
    nope = {
        "viewer_relation": "broadcast",
        "mentions": ["someone-else"],
        "wakes_granted": ["someone-else"],
    }

    assert wake.relevant_crossing(gong, "mira") == "gong"
    assert wake.relevant_crossing(msg, "mira") == "message"
    assert wake.relevant_crossing(nope, "mira") is None


def test_parse_collab_item_completed_event_matches_target_thread():
    notification = _collab_item_completed_notification(agent_statuses={"thread-child": "completed", "thread-child-2": "errored"})

    event = wake.parse_collab_item_completed_event(notification, "thread-1")

    assert event is not None
    assert event["source"] == "codex"
    assert event["kind"] == "child-completed"
    assert event["target_thread_id"] == "thread-1"
    assert event["terminal_agent_count"] == 2
    assert event["status_summary"] == "completed=1,errored=1"


def test_parse_collab_item_completed_event_includes_schema_terminal_statuses_only():
    notification = _collab_item_completed_notification(
        agent_statuses={
            "thread-child-a": "completed",
            "thread-child-b": "shutdown",
            "thread-child-c": "notFound",
            "thread-child-d": "running",
            "thread-child-e": "pendingInit",
        }
    )

    event = wake.parse_collab_item_completed_event(notification, "thread-1")

    assert event is not None
    assert event["terminal_agent_count"] == 3
    assert event["status_summary"] == "completed=1,notFound=1,shutdown=1"


def test_parse_collab_item_completed_event_rejects_irrelevant_sender():
    notification = _collab_item_completed_notification(sender_thread_id="thread-else")

    assert wake.parse_collab_item_completed_event(notification, "thread-1") is None


def test_parse_collab_item_completed_event_rejects_nonterminal_status():
    notification = _collab_item_completed_notification(item_status="inProgress")

    assert wake.parse_collab_item_completed_event(notification, "thread-1") is None


def test_parse_collab_item_completed_event_rejects_non_spawnagent_tool():
    notification = _collab_item_completed_notification(tool="wait")

    assert wake.parse_collab_item_completed_event(notification, "thread-1") is None


def test_duplicate_codex_completion_event_is_not_requeued(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    notification = _collab_item_completed_notification()

    event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert event is not None
    assert wake.enqueue_event(state, event, logger) is True
    assert wake.enqueue_event(state, event, logger) is False
    assert [queued["id"] for queued in state["queue"]] == [event["id"]]


def test_active_parent_defers_codex_completion_wake(tmp_path):
    event = wake.parse_collab_item_completed_event(_collab_item_completed_notification(), "thread-1")
    assert event is not None
    state = wake.default_state()
    state["queue"] = [event]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "waiting_for_idle"
    assert result.thread_id == "thread-1"
    assert state["queue"] == [event]
    assert not [params for method, params in rpc.calls if method == "turn/start"]


def test_monitor_drain_replays_latest_idle_completion_and_live_notification(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    rpc = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-old", "status": "completed", "items": [_collab_item(item_id="old-1")]},
                        {"id": "turn-new", "status": "completed", "items": [_collab_item(item_id="latest-1")]},
                    ],
                )
            ]
        },
        notifications=[_collab_item_completed_notification(item_id="live-1")],
    )
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://127.0.0.1:4500",
        logger=logger,
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    enqueued = monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1")

    assert enqueued == 2
    assert [event["item_id"] for event in state["queue"]] == ["latest-1", "live-1"]
    assert state["codex_monitor_recovery"]["armed"] is True
    seen_ids = set(state["codex_monitor_recovery"]["seen_terminal_event_ids"])
    assert len(seen_ids) == 3
    assert {state["queue"][0]["id"], state["queue"][1]["id"]} <= seen_ids
    monitor.close()


def test_monitor_reconnect_recovers_completion_without_notification(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    rpc_round_1 = ScriptedRpc(
        method_responses={"thread/read": [_thread_read("idle", turns=[{"id": "turn-1", "status": "completed", "items": []}])]},
        notifications=[],
    )
    rpc_round_2 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-1", "status": "completed", "items": []},
                        {"id": "turn-2", "status": "completed", "items": [_collab_item(item_id="recover-1")]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpcs = iter([rpc_round_1, rpc_round_2])
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://127.0.0.1:4500",
        logger=logger,
        transport_resolver=_transport,
        rpc_opener=lambda _transport: next(rpcs),
    )

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 0
    assert state["queue"] == []

    monitor.close()
    recovered = monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1")

    assert recovered == 1
    assert [event["item_id"] for event in state["queue"]] == ["recover-1"]
    assert state["codex_monitor_recovery"]["seen_terminal_event_ids"] == [state["queue"][0]["id"]]
    monitor.close()


def test_monitor_reconnect_recovers_same_item_index_status_mutation_once(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    running_item = _collab_item(item_id="recover-1", item_status="inProgress", agent_statuses={"thread-child": "running"})
    completed_item = _collab_item(item_id="recover-1")
    rpc_round_1 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "active",
                    turns=[
                        {"id": "turn-1", "status": "completed", "items": []},
                        {"id": "turn-2", "status": "inProgress", "items": [running_item]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpc_round_2 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-1", "status": "completed", "items": []},
                        {"id": "turn-2", "status": "completed", "items": [completed_item]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpc_round_3 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-1", "status": "completed", "items": []},
                        {"id": "turn-2", "status": "completed", "items": [completed_item]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpcs = iter([rpc_round_1, rpc_round_2, rpc_round_3])
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://127.0.0.1:4500",
        logger=logger,
        transport_resolver=_transport,
        rpc_opener=lambda _transport: next(rpcs),
    )

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 0
    assert state["queue"] == []

    monitor.close()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 1
    assert [event["item_id"] for event in state["queue"]] == ["recover-1"]
    seen_after_recovery = list(state["codex_monitor_recovery"]["seen_terminal_event_ids"])

    monitor.close()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 0
    assert [event["item_id"] for event in state["queue"]] == ["recover-1"]
    assert state["codex_monitor_recovery"]["seen_terminal_event_ids"] == seen_after_recovery
    monitor.close()


def test_monitor_bootstrap_avoids_historical_flood_and_duplicate_replay(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    rpc_round_1 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-old", "status": "completed", "items": [_collab_item(item_id="old-1")]},
                        {"id": "turn-new", "status": "completed", "items": [_collab_item(item_id="latest-1")]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpc_round_2 = ScriptedRpc(
        method_responses={
            "thread/read": [
                _thread_read(
                    "idle",
                    turns=[
                        {"id": "turn-old", "status": "completed", "items": [_collab_item(item_id="old-1")]},
                        {"id": "turn-new", "status": "completed", "items": [_collab_item(item_id="latest-1")]},
                    ],
                )
            ]
        },
        notifications=[],
    )
    rpcs = iter([rpc_round_1, rpc_round_2])
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://127.0.0.1:4500",
        logger=logger,
        transport_resolver=_transport,
        rpc_opener=lambda _transport: next(rpcs),
    )

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 1
    assert [event["item_id"] for event in state["queue"]] == ["latest-1"]
    seen_after_bootstrap = set(state["codex_monitor_recovery"]["seen_terminal_event_ids"])
    assert len(seen_after_bootstrap) == 2

    monitor.close()
    assert monitor.drain(state=state, sessions_dir=sessions_dir, thread_override="thread-1") == 0
    assert [event["item_id"] for event in state["queue"]] == ["latest-1"]
    assert set(state["codex_monitor_recovery"]["seen_terminal_event_ids"]) == seen_after_bootstrap
    monitor.close()


def test_monitor_recovery_raises_when_terminal_scan_exceeds_cap(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    items = [_collab_item(item_id=f"item-{i}") for i in range(wake.MAX_MONITOR_RECOVERY_ITEMS + 1)]

    with pytest.raises(RuntimeError, match="exceeded bounded terminal-item scan"):
        wake.recover_collab_completion_events(
            _thread_read("idle", turns=[{"id": "turn-1", "status": "completed", "items": items}]),
            target_thread_id="thread-1",
            state=state,
            logger=logger,
        )


# ---------------------------------------------------------------------------
# seed/new-place behavior
# ---------------------------------------------------------------------------


def test_seed_is_forward_only_and_new_place_is_seeded_without_replay(tmp_path):
    state = wake.default_state()
    logger = _logger(tmp_path)
    world = FakeWorld(
        place_snapshots=[["place_square"], ["place_square", "place_new"]],
        responses={
            ("place_square", None): [
                {
                    "crossings": [{"seq": 10, "sender": "sol", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 10,
                }
            ],
            ("place_square", 10): [
                {
                    "crossings": [{"seq": 11, "sender": "sol", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 11,
                }
            ],
            ("place_new", None): [
                {
                    "crossings": [{"seq": 1, "sender": "kepler", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 3,
                }
            ],
        },
    )

    assert wake.poll_umwelt(world, state, logger) == "seeded"
    assert state["seeded"] is True
    assert state["queue"] == []
    assert state["cursors"]["place_square"] == 10

    assert wake.poll_umwelt(world, state, logger) == "polled"
    assert [event["id"] for event in state["queue"]] == ["umwelt-crossing:place_square:11"]
    assert state["cursors"]["place_square"] == 11
    assert state["cursors"]["place_new"] == 3
    assert ("place_new", None) in world.crossing_calls


# ---------------------------------------------------------------------------
# restart/dedupe
# ---------------------------------------------------------------------------


def test_restart_dedupe_avoids_duplicate_queue_and_requeue_after_delivery(tmp_path):
    logger = _logger(tmp_path)
    state = wake.default_state()
    state["seeded"] = True
    state["cursors"] = {"place_square": 5}

    world = FakeWorld(
        place_snapshots=[["place_square"], ["place_square"], ["place_square"]],
        responses={
            ("place_square", 5): [
                {
                    "crossings": [{"seq": 6, "sender": "sol", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 6,
                }
            ],
            ("place_square", 6): [
                {
                    "crossings": [{"seq": 6, "sender": "sol", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 6,
                },
                {
                    "crossings": [{"seq": 6, "sender": "sol", "viewer_relation": "addressee", "wake_granted": True}],
                    "to_position": 6,
                },
            ],
        },
    )

    wake.poll_umwelt(world, state, logger)
    assert [event["id"] for event in state["queue"]] == ["umwelt-crossing:place_square:6"]

    restarted_state = json.loads(json.dumps(state))
    wake.poll_umwelt(world, restarted_state, logger)
    assert [event["id"] for event in restarted_state["queue"]] == ["umwelt-crossing:place_square:6"]

    delivered = restarted_state["queue"].pop(0)
    wake.remember_delivered_id(restarted_state, delivered["id"])
    wake.poll_umwelt(world, restarted_state, logger)
    assert restarted_state["queue"] == []


# ---------------------------------------------------------------------------
# thread selection / active-idle / batching
# ---------------------------------------------------------------------------


def test_attempt_delivery_skips_newest_stale_marker_and_uses_newest_loaded_live_thread(tmp_path):
    state = wake.default_state()
    state["queue"] = [_event("9")]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "older.json").write_text(json.dumps({
        "session_id": "thread-live",
        "transcript_path": "/home/agent-mira/.codex/sessions/2026/07/21/live.jsonl",
        "role": "interactive",
        "started_at": "2026-07-21T10:00:00+00:00",
    }))
    (sessions_dir / "newer.json").write_text(json.dumps({
        "session_id": "thread-stale",
        "transcript_path": "/home/agent-mira/.codex/sessions/2026/07/22/stale.jsonl",
        "role": "interactive",
        "started_at": "2026-07-22T10:00:00+00:00",
    }))
    rpc = ScriptedRpc(method_responses={
        "thread/read": [wake.RpcError("unknown thread"), _thread_read("idle")],
    })

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override=None,
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "delivered"
    assert result.thread_id == "thread-live"
    thread_reads = [params for method, params in rpc.calls if method == "thread/read"]
    assert [call["threadId"] for call in thread_reads] == ["thread-stale", "thread-live"]
    turn_start = [params for method, params in rpc.calls if method == "turn/start"]
    assert turn_start[0]["threadId"] == "thread-live"


def test_active_turn_keeps_queue_without_starting_new_turn(tmp_path):
    state = wake.default_state()
    state["queue"] = [_event("9")]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "waiting_for_idle"
    assert len(state["queue"]) == 1
    assert state["inflight_batch"] is None
    assert [method for method, _ in rpc.calls] == ["initialize", "thread/read"]


def test_idle_turn_coalesces_current_queue_into_one_batched_wake(tmp_path):
    state = wake.default_state()
    state["queue"] = [_event("9", kind="gong", place="place_square"), _event("10", kind="message", place="place_new")]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "delivered"
    assert state["queue"] == []
    assert state["active_wake"]["thread_id"] == "thread-1"
    assert set(state["delivered_event_ids"]) >= {"umwelt-crossing:place_square:9", "umwelt-crossing:place_new:10"}
    turn_start = [params for method, params in rpc.calls if method == "turn/start"]
    assert len(turn_start) == 1
    prompt = turn_start[0]["input"][0]["text"]
    assert "count=2 events" in prompt
    assert "kinds=gong=1,message=1" in prompt
    assert "places=place_square, place_new" in prompt


def test_events_arriving_during_handoff_stay_queued_for_next_turn(tmp_path):
    state = wake.default_state()
    first = _event("9")
    second = _event("10", kind="message", place="place_new")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})

    def inject_new_event(current_state, inflight):
        assert inflight["event_ids"] == [first["id"]]
        current_state["queue"].append(second)

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
        after_inflight_persist=inject_new_event,
    )

    assert result.status == "delivered"
    assert state["queue"] == [second]
    assert state["inflight_batch"] is None
    assert state["active_wake"] is not None


def test_events_arriving_during_active_turn_are_not_replayed(tmp_path):
    state = wake.default_state()
    first = _event("9")
    second = _event("10", kind="message")
    state["queue"] = [first, second]
    state["inflight_batch"] = wake.build_inflight_batch([first], "thread-1")
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "waiting_for_idle"
    assert [event["id"] for event in state["queue"]] == [first["id"], second["id"]]
    assert state["inflight_batch"]["event_ids"] == [first["id"]]
    assert [method for method, _ in rpc.calls] == ["initialize", "thread/read"]


def test_active_wake_steers_followup_without_starting_second_turn(tmp_path):
    state = wake.default_state()
    first = _event("9", kind="gong", place="place_square")
    second = _event("10", kind="message", place="place_new")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    first_result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    assert first_result.status == "delivered"
    wake_turn_id = state["active_wake"]["wake_turn_id"]

    state["queue"] = [second]
    steer_rpc = ScriptedRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
        "turn/steer": [{"turn": {"id": wake_turn_id, "status": "inProgress"}}],
    })
    steer_result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: steer_rpc,
    )

    assert steer_result.status == "delivered"
    assert state["queue"] == []
    assert state["active_wake"]["wake_turn_id"] == wake_turn_id
    assert not [params for method, params in steer_rpc.calls if method == "turn/start"]
    steer_calls = [params for method, params in steer_rpc.calls if method == "turn/steer"]
    assert len(steer_calls) == 1
    assert steer_calls[0]["expectedTurnId"] == wake_turn_id
    assert "count=1 event" in steer_calls[0]["input"][0]["text"]

    idle_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle", turn_id=wake_turn_id, turn_status="completed")]})
    idle_result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: idle_rpc,
    )
    assert idle_result.status == "idle"
    assert state["active_wake"] is None
    assert not [params for method, params in idle_rpc.calls if method == "turn/start"]


def test_nonsteerable_active_wake_falls_back_to_single_later_turn(tmp_path):
    state = wake.default_state()
    first = _event("9")
    second = _event("10", kind="heartbeat", place="place_square")
    third = _event("11", kind="message", place="place_new")
    fourth = _event("12", kind="gong", place="place_corner")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    wake_turn_id = state["active_wake"]["wake_turn_id"]

    state["queue"] = [second, third]
    blocked_rpc = ScriptedRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
        "turn/steer": [wake.RpcError("no active turn to steer")],
    })
    blocked = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: blocked_rpc,
    )
    assert blocked.status == "waiting_for_idle"
    assert state["active_wake"]["steer_state"] == "unsupported"
    assert [event["id"] for event in state["queue"]] == [second["id"], third["id"]]

    state["queue"].append(fourth)
    still_active_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")]})
    still_active = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: still_active_rpc,
    )
    assert still_active.status == "waiting_for_idle"
    assert not [params for method, params in still_active_rpc.calls if method == "turn/steer"]
    assert not [params for method, params in still_active_rpc.calls if method == "turn/start"]

    fallback_rpc = ScriptedRpc(method_responses={
        "thread/read": [
            _thread_read("idle", turn_id=wake_turn_id, turn_status="completed"),
            _thread_read("idle"),
        ]
    })
    fallback = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: fallback_rpc,
    )
    assert fallback.status == "delivered"
    turn_start = [params for method, params in fallback_rpc.calls if method == "turn/start"]
    assert len(turn_start) == 1
    assert "count=3 events" in turn_start[0]["input"][0]["text"]
    assert "heartbeat=1" in turn_start[0]["input"][0]["text"]
    assert state["queue"] == []


def test_heartbeat_only_batch_is_subsumed_until_communication_arrives(tmp_path):
    state = wake.default_state()
    first = _event("9")
    heartbeat = _event("10", kind="heartbeat", place="place_square")
    message = _event("11", kind="message", place="place_new")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    wake_turn_id = state["active_wake"]["wake_turn_id"]

    state["queue"] = [heartbeat]
    heartbeat_only_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")]})
    heartbeat_only = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: heartbeat_only_rpc,
    )
    assert heartbeat_only.status == "waiting_for_idle"
    assert [event["id"] for event in state["queue"]] == [heartbeat["id"]]
    assert not [params for method, params in heartbeat_only_rpc.calls if method == "turn/steer"]

    state["queue"].append(message)
    followup_rpc = ScriptedRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
        "turn/steer": [{"turn": {"id": wake_turn_id, "status": "inProgress"}}],
    })
    followup = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: followup_rpc,
    )
    assert followup.status == "delivered"
    steer_call = [params for method, params in followup_rpc.calls if method == "turn/steer"][0]
    assert "count=2 events" in steer_call["input"][0]["text"]
    assert "heartbeat=1,message=1" in steer_call["input"][0]["text"]
    assert state["queue"] == []


def test_restart_during_active_wake_recovers_same_turn_and_steers(tmp_path):
    state = wake.default_state()
    first = _event("9")
    second = _event("10", kind="message", place="place_new")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    restarted_state = json.loads(json.dumps(state))
    restarted_state["queue"] = [second]
    wake_turn_id = restarted_state["active_wake"]["wake_turn_id"]

    restart_rpc = ScriptedRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
        "turn/steer": [{"turn": {"id": wake_turn_id, "status": "inProgress"}}],
    })
    restarted = wake.attempt_delivery(
        restarted_state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: restart_rpc,
    )
    assert restarted.status == "delivered"
    assert restarted_state["queue"] == []
    assert restarted_state["active_wake"]["wake_turn_id"] == wake_turn_id
    assert not [params for method, params in restart_rpc.calls if method == "turn/start"]


def test_arrival_during_steer_only_acks_snapshot_batch(tmp_path):
    state = wake.default_state()
    first = _event("9")
    second = _event("10", kind="message", place="place_new")
    third = _event("11", kind="gong", place="place_corner")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    wake_turn_id = state["active_wake"]["wake_turn_id"]
    state["queue"] = [second]

    def inject_during_steer(current_state, steer_batch):
        assert steer_batch["event_ids"] == [second["id"]]
        current_state["queue"].append(third)

    steer_rpc = ScriptedRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
        "turn/steer": [{"turn": {"id": wake_turn_id, "status": "inProgress"}}],
    })
    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: steer_rpc,
        after_steer_inflight_persist=inject_during_steer,
    )
    assert result.status == "delivered"
    assert [event["id"] for event in state["queue"]] == [third["id"]]
    assert second["id"] in state["delivered_event_ids"]
    assert third["id"] not in state["delivered_event_ids"]


def test_crash_after_accepted_steer_reconciles_without_duplicate_delivery(tmp_path):
    state = wake.default_state()
    first = _event("9")
    followup = _event("10", kind="message", place="place_new")
    state["queue"] = [first]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    first_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})
    wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: first_rpc,
    )
    wake_turn_id = state["active_wake"]["wake_turn_id"]
    state["queue"] = [followup]

    persisted = []

    def persist(current_state):
        persisted.append(json.loads(json.dumps(current_state)))

    class CrashAfterAcceptedSteerRpc(ScriptedRpc):
        def call(self, method, params):
            if method == "turn/steer":
                self.calls.append((method, params))
                raise SystemExit("simulated crash after Codex accepted turn/steer")
            return super().call(method, params)

    crashing_rpc = CrashAfterAcceptedSteerRpc(method_responses={
        "thread/read": [_thread_read("active", turn_id=wake_turn_id, turn_status="inProgress")],
    })
    with pytest.raises(SystemExit, match="after Codex accepted"):
        wake.attempt_delivery(
            state,
            app_server="ws://127.0.0.1:4500",
            sessions_dir=sessions_dir,
            thread_override="thread-1",
            logger=_logger(tmp_path),
            transport_resolver=_transport,
            rpc_opener=lambda _transport: crashing_rpc,
            persist_state=persist,
        )

    crash_state = persisted[-1]
    steer_batch = crash_state["active_wake"]["steer_inflight"]
    assert steer_batch["event_ids"] == [followup["id"]]
    assert [event["id"] for event in crash_state["queue"]] == [followup["id"]]

    restarted_state = json.loads(json.dumps(crash_state))
    reconcile_rpc = ScriptedRpc(method_responses={
        "thread/read": [
            _thread_read(
                "active",
                client_id=steer_batch["client_user_message_id"],
                turn_id=wake_turn_id,
                turn_status="inProgress",
            )
        ],
    })
    reconciled = wake.attempt_delivery(
        restarted_state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: reconcile_rpc,
        persist_state=persist,
    )

    assert reconciled.status == "waiting_for_idle"
    assert restarted_state["queue"] == []
    assert restarted_state["active_wake"]["steer_inflight"] is None
    assert followup["id"] in restarted_state["delivered_event_ids"]
    assert not [params for method, params in reconcile_rpc.calls if method == "turn/steer"]
    assert not [params for method, params in reconcile_rpc.calls if method == "turn/start"]


# ---------------------------------------------------------------------------
# prompt hygiene
# ---------------------------------------------------------------------------


def test_prompt_never_includes_crossing_body(tmp_path):
    crossing = {
        "seq": 42,
        "sender": "sol",
        "viewer_relation": "addressee",
        "wake_granted": True,
        "body": "SECRET CROSSING BODY SHOULD NEVER APPEAR",
    }
    event = wake.build_event("place_square", crossing, "mira")
    assert event is not None
    assert "body" not in event
    prompt = wake.render_batch_prompt([event])
    assert "SECRET CROSSING BODY" not in prompt
    assert "sender=sol" not in prompt
    assert "place_square" in prompt
    assert "count=1 event" in prompt


# ---------------------------------------------------------------------------
# failure recovery / graceful wait states / exactly-once crash boundaries
# ---------------------------------------------------------------------------


def test_delivery_failure_preserves_queue(tmp_path):
    state = wake.default_state()
    state["queue"] = [_event("9", kind="message")]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")], "turn/start": [RuntimeError("boom on turn/start")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=sessions_dir,
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "wake_failed"
    assert len(state["queue"]) == 1
    assert state["delivered_event_ids"] == []
    assert state["inflight_batch"] is not None


def test_crash_before_send_retries_same_client_id_once_thread_is_idle(tmp_path):
    state = wake.default_state()
    event = _event("9")
    state["queue"] = [event]
    state["inflight_batch"] = wake.build_inflight_batch([event], "thread-1")
    client_id = state["inflight_batch"]["client_user_message_id"]
    rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle")]})

    result = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: rpc,
    )

    assert result.status == "delivered"
    assert state["queue"] == []
    turn_start = [params for method, params in rpc.calls if method == "turn/start"]
    assert len(turn_start) == 1
    assert turn_start[0]["clientUserMessageId"] == client_id


def test_crash_after_accepted_response_reconciles_without_replay(tmp_path):
    state = wake.default_state()
    event = _event("9")
    state["queue"] = [event]
    state["inflight_batch"] = wake.build_inflight_batch([event], "thread-1")
    client_id = state["inflight_batch"]["client_user_message_id"]

    active_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("active")]})
    waiting = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: active_rpc,
    )
    assert waiting.status == "waiting_for_idle"
    assert not [params for method, params in active_rpc.calls if method == "turn/start"]

    recovered_rpc = ScriptedRpc(method_responses={"thread/read": [_thread_read("idle", client_id=client_id)]})
    delivered = wake.attempt_delivery(
        state,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=_transport,
        rpc_opener=lambda _transport: recovered_rpc,
    )

    assert delivered.status == "delivered"
    assert state["queue"] == []
    assert state["inflight_batch"] is None
    assert event["id"] in state["delivered_event_ids"]
    assert not [params for method, params in recovered_rpc.calls if method == "turn/start"]


def test_run_cycle_waits_gracefully_when_umwelt_is_missing(tmp_path):
    paths = wake.build_paths(tmp_path / "codex-wake")
    logger = _logger(tmp_path)

    status = wake.run_cycle(
        paths=paths,
        umwelt_config=tmp_path / "missing-umwelt.json",
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override=None,
        logger=logger,
        started_at="2026-07-22T12:00:00+00:00",
    )

    assert status["mode"] == "waiting_for_umwelt"
    on_disk = json.loads(paths.status_path.read_text())
    assert on_disk["mode"] == "waiting_for_umwelt"


def test_run_cycle_missing_umwelt_still_delivers_codex_queue(tmp_path, monkeypatch):
    paths = wake.build_paths(tmp_path / "codex-wake")
    logger = _logger(tmp_path)
    state = wake.default_state()
    event = wake.parse_collab_item_completed_event(_collab_item_completed_notification(item_id="codex-1"), "thread-1")
    assert event is not None
    state["queue"] = [event]
    wake.atomic_write_json(paths.state_path, state)

    def fake_attempt_delivery(current_state, **kwargs):
        assert current_state["queue"] == [event]
        wake.ack_event_ids(current_state, [event["id"]])
        return wake.DeliveryResult(status="delivered", detail=event["item_id"], thread_id="thread-1")

    monkeypatch.setattr(wake, "attempt_delivery", fake_attempt_delivery)

    status = wake.run_cycle(
        paths=paths,
        umwelt_config=tmp_path / "missing-umwelt.json",
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=logger,
        started_at="2026-07-22T12:00:00+00:00",
    )

    assert status["mode"] == "delivered"
    assert status["thread_id"] == "thread-1"
    persisted = json.loads(paths.state_path.read_text())
    assert persisted["queue"] == []
    assert event["id"] in persisted["delivered_event_ids"]


def test_run_cycle_poll_failure_still_delivers_codex_queue_and_upgrades_legacy_state(tmp_path, monkeypatch):
    paths = wake.build_paths(tmp_path / "codex-wake")
    logger = _logger(tmp_path)
    event = wake.parse_collab_item_completed_event(_collab_item_completed_notification(item_id="codex-legacy"), "thread-1")
    assert event is not None
    legacy_state = {
        "state_version": 3,
        "seeded": True,
        "cursors": {"place_square": 7},
        "queue": [event],
        "delivered_event_ids": [],
        "inflight_batch": None,
        "active_wake": None,
    }
    wake.atomic_write_json(paths.state_path, legacy_state)
    umwelt_config = tmp_path / "umwelt.json"
    umwelt_config.write_text(json.dumps({"agent": "mira", "world_url": "http://world.test"}))

    def fake_poll(_world, current_state, _logger):
        assert current_state["state_version"] == wake.STATE_VERSION
        assert current_state["codex_monitor_recovery"] == wake.default_monitor_recovery()
        raise RuntimeError("umwelt offline")

    def fake_attempt_delivery(current_state, **kwargs):
        assert current_state["queue"] == [event]
        assert current_state["state_version"] == wake.STATE_VERSION
        assert current_state["codex_monitor_recovery"] == wake.default_monitor_recovery()
        wake.ack_event_ids(current_state, [event["id"]])
        return wake.DeliveryResult(status="delivered", detail=event["item_id"], thread_id="thread-1")

    monkeypatch.setattr(wake, "poll_umwelt", fake_poll)
    monkeypatch.setattr(wake, "attempt_delivery", fake_attempt_delivery)

    status = wake.run_cycle(
        paths=paths,
        umwelt_config=umwelt_config,
        app_server="ws://127.0.0.1:4500",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=logger,
        started_at="2026-07-22T12:00:00+00:00",
    )

    assert status["mode"] == "delivered"
    assert status["detail"] == "codex-legacy; umwelt degraded: poll failed: umwelt offline"
    assert status["thread_id"] == "thread-1"
    persisted = json.loads(paths.state_path.read_text())
    assert persisted["state_version"] == wake.STATE_VERSION
    assert persisted["queue"] == []
    assert event["id"] in persisted["delivered_event_ids"]
    assert persisted["codex_monitor_recovery"] == wake.default_monitor_recovery()
    on_disk_status = json.loads(paths.status_path.read_text())
    assert on_disk_status["mode"] == "delivered"
    assert on_disk_status["detail"] == "codex-legacy; umwelt degraded: poll failed: umwelt offline"
    assert "umwelt poll failed: umwelt offline" in (tmp_path / "watcher.log").read_text()


# ---------------------------------------------------------------------------
# thread discovery + transport selection + framing + pid identity
# ---------------------------------------------------------------------------


def test_discover_codex_thread_id_prefers_newest_interactive_codex_marker_and_override(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    (sessions / "older.json").write_text(json.dumps({
        "session_id": "thread-old",
        "transcript_path": "/home/agent-mira/.codex/sessions/2026/07/21/old.jsonl",
        "role": "interactive",
        "started_at": "2026-07-21T10:00:00+00:00",
    }))
    (sessions / "newer.json").write_text(json.dumps({
        "session_id": "thread-new",
        "transcript_path": "/home/agent-mira/.codex/sessions/2026/07/22/new.jsonl",
        "role": "interactive",
        "started_at": "2026-07-22T10:00:00+00:00",
    }))
    (sessions / "claude.json").write_text(json.dumps({
        "session_id": "not-codex",
        "transcript_path": "/home/agent-mira/.claude/projects/ignored.jsonl",
        "role": "interactive",
        "started_at": "2026-07-22T11:00:00+00:00",
    }))

    assert wake.discover_codex_thread_id(sessions) == "thread-new"
    assert wake.discover_codex_thread_id(sessions, override="manual-thread") == "manual-thread"


def test_transport_selection_supports_managed_unix_proxy_and_explicit_ws(monkeypatch):
    monkeypatch.setattr(wake, "daemon_socket_path", lambda: "/tmp/managed.sock")

    managed_unix = wake.resolve_transport_target("unix://")
    proxy = wake.resolve_transport_target("proxy://")
    explicit_proxy = wake.resolve_transport_target("proxy:///tmp/explicit.sock")
    explicit_unix = wake.resolve_transport_target("unix:///tmp/unix.sock")
    ws = wake.resolve_transport_target("ws://127.0.0.1:4500")

    assert managed_unix == wake.TransportTarget(kind="unix", value="/tmp/managed.sock")
    assert proxy == wake.TransportTarget(kind="proxy", value="/tmp/managed.sock")
    assert explicit_proxy == wake.TransportTarget(kind="proxy", value="/tmp/explicit.sock")
    assert explicit_unix == wake.TransportTarget(kind="unix", value="/tmp/unix.sock")
    assert ws == wake.TransportTarget(kind="websocket", value="ws://127.0.0.1:4500")


def test_stream_websocket_rpc_handles_ping_and_masks_client_frames():
    handshake_key = b"AAAAAAAAAAAAAAAAAAAAAA=="
    accept = wake.base64.b64encode(wake.hashlib.sha1(handshake_key + wake.WS_GUID.encode("ascii")).digest())
    handshake = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Connection: Upgrade\r\n"
        b"Upgrade: websocket\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    response = {"id": 1, "result": {"thread": {"status": {"type": "idle"}}}}
    fake_io = FakeIo([
        handshake,
        _server_frame(0x9, b"ping"),
        _server_frame(0x1, json.dumps(response).encode("utf-8")),
    ])

    original_urandom = wake.os.urandom
    try:
        wake.os.urandom = lambda n: b"\x00" * n
        with FakeStreamRpc(fake_io) as rpc:
            result = rpc.call("thread/read", {"threadId": "thread-1", "includeTurns": False})
    finally:
        wake.os.urandom = original_urandom

    assert result == response["result"]
    assert fake_io.closed is True
    assert fake_io.writes[0].startswith(b"GET /rpc HTTP/1.1\r\n")
    req_opcode, req_masked, req_payload = _parse_client_frame(fake_io.writes[1])
    assert req_opcode == 0x1 and req_masked is True
    assert json.loads(req_payload) == {"id": 1, "method": "thread/read", "params": {"threadId": "thread-1", "includeTurns": False}}
    pong_opcode, pong_masked, pong_payload = _parse_client_frame(fake_io.writes[2])
    assert pong_opcode == 0xA and pong_masked is True and pong_payload == b"ping"


def test_websocket_rpc_preserves_completion_before_thread_read_response(tmp_path):
    notification = _collab_item_completed_notification()
    fake_conn = FakeWebsocketConnection([
        json.dumps({"id": 1, "result": {}}),
        json.dumps(notification),
        json.dumps({"id": 2, "result": _thread_read("idle")}),
    ])
    rpc = FakeWebsocketRpc(fake_conn)
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://fake",
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="websocket", value="ws://fake"),
        rpc_opener=lambda _: rpc,
    )
    state = wake.default_state()

    try:
        assert monitor.drain(state=state, sessions_dir=tmp_path / "sessions", thread_override="thread-1") == 1
    finally:
        monitor.close()

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert expected_event is not None
    assert state["queue"] == [expected_event]
    assert [json.loads(message)["method"] for message in fake_conn.sent] == [
        "initialize", "initialized", "thread/read"
    ]


def test_stream_websocket_rpc_preserves_completion_before_thread_read_response(tmp_path, monkeypatch):
    handshake_key = b"AAAAAAAAAAAAAAAAAAAAAA=="
    accept = wake.base64.b64encode(wake.hashlib.sha1(handshake_key + wake.WS_GUID.encode("ascii")).digest())
    handshake = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Connection: Upgrade\r\n"
        b"Upgrade: websocket\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    notification = _collab_item_completed_notification()
    fake_io = FakeIo([
        handshake,
        _server_frame(0x1, json.dumps({"id": 1, "result": {}}).encode("utf-8")),
        _server_frame(0x1, json.dumps(notification).encode("utf-8")),
        _server_frame(0x1, json.dumps({"id": 2, "result": _thread_read("idle")}).encode("utf-8")),
    ])
    monkeypatch.setattr(wake.os, "urandom", lambda n: b"\x00" * n)
    rpc = FakeStreamRpc(fake_io)
    monitor = wake.CodexItemCompletedMonitor(
        app_server="unix:///tmp/fake.sock",
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="unix", value="/tmp/fake.sock"),
        rpc_opener=lambda _: rpc,
    )
    state = wake.default_state()

    try:
        assert monitor.drain(state=state, sessions_dir=tmp_path / "sessions", thread_override="thread-1") == 1
    finally:
        monitor.close()

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert expected_event is not None
    assert state["queue"] == [expected_event]
    methods = [_parse_client_frame(frame)[2] for frame in fake_io.writes[1:4]]
    assert [json.loads(payload)["method"] for payload in methods] == ["initialize", "initialized", "thread/read"]


def test_monitor_initialize_error_salvages_buffered_completion_before_teardown(tmp_path):
    notification = _collab_item_completed_notification(item_id="initialize-race")
    fake_conn = FakeWebsocketConnection([
        json.dumps(notification),
        json.dumps({"id": 2, "result": {}}),
    ])
    rpc = FakeWebsocketRpc(fake_conn)
    logger = _logger(tmp_path)
    monitor = wake.CodexItemCompletedMonitor(
        app_server="ws://fake",
        logger=logger,
        transport_resolver=lambda _: wake.TransportTarget(kind="websocket", value="ws://fake"),
        rpc_opener=lambda _: rpc,
    )
    state = wake.default_state()
    persisted = []

    assert monitor.drain(
        state=state,
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        persist_state=lambda current_state: persisted.append((json.loads(json.dumps(current_state)), fake_conn.closed)),
    ) == 1

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert expected_event is not None
    assert state["queue"] == [expected_event]
    assert any(expected_event in snapshot["queue"] and not closed for snapshot, closed in persisted)
    assert fake_conn.closed is True
    assert "unexpected response id while waiting for initialize" in (tmp_path / "watcher.log").read_text(encoding="utf-8")


def test_delivery_preserves_buffered_completion_and_enqueues_it_once(tmp_path):
    notification = _collab_item_completed_notification(item_id="during-delivery")
    fake_conn = FakeWebsocketConnection([
        json.dumps({"id": 1, "result": {}}),
        json.dumps(notification),
        json.dumps(notification),
        json.dumps({"id": 2, "result": _thread_read("idle")}),
        json.dumps({"id": 3, "result": {"turn": {"id": "turn_123"}}}),
    ])
    rpc = FakeWebsocketRpc(fake_conn)
    state = wake.default_state()
    state["queue"] = [_event("delivery-trigger")]
    persisted = []

    result = wake.attempt_delivery(
        state,
        app_server="ws://fake",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-1",
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="websocket", value="ws://fake"),
        rpc_opener=lambda _: rpc,
        persist_state=lambda current_state: persisted.append((json.loads(json.dumps(current_state)), fake_conn.closed)),
    )

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert expected_event is not None
    assert result.status == "delivered"
    assert state["queue"] == [expected_event]
    assert sum(event["id"] == expected_event["id"] for event in state["queue"]) == 1
    assert any(
        expected_event in snapshot["queue"] and not closed
        for snapshot, closed in persisted
    )
    assert fake_conn.closed is True


def test_delivery_initialize_error_salvages_persisted_inflight_thread_before_override(tmp_path):
    notification = _collab_item_completed_notification(thread_id="thread-A", sender_thread_id="thread-A", item_id="inflight-race")
    fake_conn = FakeWebsocketConnection([
        json.dumps(notification),
        json.dumps({"id": 2, "result": {}}),
    ])
    rpc = FakeWebsocketRpc(fake_conn)
    state = wake.default_state()
    original_event = _event("delivery-trigger")
    state["queue"] = [original_event]
    state["inflight_batch"] = wake.build_inflight_batch([original_event], "thread-A")
    persisted = []

    result = wake.attempt_delivery(
        state,
        app_server="ws://fake",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-B",
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="websocket", value="ws://fake"),
        rpc_opener=lambda _: rpc,
        persist_state=lambda current_state: persisted.append((json.loads(json.dumps(current_state)), fake_conn.closed)),
    )

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-A")
    assert expected_event is not None
    assert result.status == "wake_failed"
    assert state["queue"] == [original_event, expected_event]
    assert sum(event["id"] == expected_event["id"] for event in state["queue"]) == 1
    assert any(expected_event in snapshot["queue"] and not closed for snapshot, closed in persisted)
    assert fake_conn.closed is True


def test_delivery_override_filters_unrelated_buffered_completion_without_persisted_work(tmp_path):
    unrelated_notification = _collab_item_completed_notification(
        thread_id="thread-A",
        sender_thread_id="thread-A",
        item_id="unrelated-race",
    )
    fake_conn = FakeWebsocketConnection([
        json.dumps(unrelated_notification),
        json.dumps({"id": 2, "result": {}}),
    ])
    rpc = FakeWebsocketRpc(fake_conn)
    state = wake.default_state()
    original_event = _event("delivery-trigger")
    state["queue"] = [original_event]

    result = wake.attempt_delivery(
        state,
        app_server="ws://fake",
        sessions_dir=tmp_path / "sessions",
        thread_override="thread-B",
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="websocket", value="ws://fake"),
        rpc_opener=lambda _: rpc,
    )

    assert result.status == "wake_failed"
    assert state["queue"] == [original_event]
    assert fake_conn.closed is True


def test_stream_delivery_self_routes_buffered_completion_without_resolved_target(tmp_path, monkeypatch):
    handshake_key = b"AAAAAAAAAAAAAAAAAAAAAA=="
    accept = wake.base64.b64encode(wake.hashlib.sha1(handshake_key + wake.WS_GUID.encode("ascii")).digest())
    handshake = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Connection: Upgrade\r\n"
        b"Upgrade: websocket\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    notification = _collab_item_completed_notification(item_id="unresolved-target")
    fake_io = FakeIo([
        handshake,
        _server_frame(0x1, json.dumps(notification).encode("utf-8")),
        _server_frame(0x1, json.dumps({"id": 1, "result": {}}).encode("utf-8")),
    ])
    monkeypatch.setattr(wake.os, "urandom", lambda n: b"\x00" * n)
    rpc = FakeStreamRpc(fake_io)
    state = wake.default_state()
    original_event = _event("delivery-trigger")
    state["queue"] = [original_event]
    persisted = []

    result = wake.attempt_delivery(
        state,
        app_server="unix:///tmp/fake.sock",
        sessions_dir=tmp_path / "sessions",
        thread_override=None,
        logger=_logger(tmp_path),
        transport_resolver=lambda _: wake.TransportTarget(kind="unix", value="/tmp/fake.sock"),
        rpc_opener=lambda _: rpc,
        persist_state=lambda current_state: persisted.append((json.loads(json.dumps(current_state)), fake_io.closed)),
    )

    expected_event = wake.parse_collab_item_completed_event(notification, "thread-1")
    assert expected_event is not None
    assert result.status == "waiting_for_thread"
    assert result.thread_id is None
    assert state["queue"] == [original_event, expected_event]
    assert any(expected_event in snapshot["queue"] and not closed for snapshot, closed in persisted)
    assert fake_io.closed is True


def test_rpc_rejects_mismatched_response_id():
    rpc = FakeWebsocketRpc(FakeWebsocketConnection([json.dumps({"id": 2, "result": {}})]))

    with rpc, pytest.raises(RuntimeError, match=r"unexpected response id.*expected 1, got 2"):
        rpc.call("thread/read", {"threadId": "thread-1"})


def test_rpc_rejects_malformed_idless_response():
    rpc = FakeWebsocketRpc(FakeWebsocketConnection([json.dumps({"result": {}})]))

    with rpc, pytest.raises(RuntimeError, match="without a valid notification method"):
        rpc.call("thread/read", {"threadId": "thread-1"})


def test_recv_notification_rejects_id_bearing_response():
    rpc = FakeWebsocketRpc(FakeWebsocketConnection([json.dumps({"id": 1, "result": {}})]))

    with rpc, pytest.raises(RuntimeError, match="unexpected response while waiting for RPC notification"):
        rpc.recv_notification(1)


def test_pending_rpc_notification_queue_capacity_and_overflow():
    rpc = FakeWebsocketRpc(FakeWebsocketConnection([]))
    notification = {"method": "item/completed", "params": {}}

    for _ in range(wake.MAX_PENDING_RPC_NOTIFICATIONS):
        rpc._preserve_notification(notification)

    assert len(rpc._pending_notifications) == wake.MAX_PENDING_RPC_NOTIFICATIONS
    with pytest.raises(RuntimeError, match="pending websocket notification queue exceeded limit"):
        rpc._preserve_notification(notification)
    assert rpc.drain_buffered_notifications() == [notification] * wake.MAX_PENDING_RPC_NOTIFICATIONS
    assert rpc.drain_buffered_notifications() == []


def test_current_status_requires_matching_watcher_cmdline(tmp_path, monkeypatch):
    paths = wake.build_paths(tmp_path / "codex-wake")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_path.write_text("12345\n", encoding="utf-8")
    wake.update_status(paths, wake.default_status(paths))

    monkeypatch.setattr(wake, "proc_cmdline", lambda pid: ("ok", "python unrelated.py"))
    status = wake.current_status(paths)
    assert status["running"] is False
    assert status["pid"] is None


live_only = pytest.mark.skipif(
    os.environ.get("CODEX_WAKE_LIVE") != "1",
    reason="set CODEX_WAKE_LIVE=1 to run live Codex app-server transport smokes",
)


@live_only
def test_live_unix_transport_smoke(tmp_path):
    sock_path = tmp_path / "app-server.sock"
    with live_app_server(f"unix://{sock_path}"):
        _wait_for_path(sock_path)
        _live_turn_smoke(f"unix://{sock_path}")


@live_only
def test_live_proxy_transport_smoke(tmp_path):
    sock_path = tmp_path / "app-server.sock"
    with live_app_server(f"unix://{sock_path}"):
        _wait_for_path(sock_path)
        _live_turn_smoke(f"proxy://{sock_path}")


@live_only
def test_live_ws_transport_smoke():
    port = _find_free_tcp_port()
    with live_app_server(f"ws://127.0.0.1:{port}"):
        time.sleep(0.5)
        _live_turn_smoke(f"ws://127.0.0.1:{port}")
