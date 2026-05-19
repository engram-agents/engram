#!/usr/bin/env python3
"""Stop hook: scrape last assistant message for ENGRAM node-id mentions and
bump utility_score for IDs that were in the recall_window.

Implements alpha #177 area 4 (broader credit-assignment per Lei 2026-05-19):
the prior utility-reward mechanism only fired when a recalled node was
later cited as a `derives_from` premise in engram_derive (server.py:2694).
This misses prose-mention, inline ID-citation, and informed-but-uncited
use. The stop hook captures the prose-mention / inline-citation cases
by scanning the response text after every assistant turn and bumping
utility_score for any mentioned IDs that ALSO appear in the recall
window — i.e. IDs the substrate brought up via surface / query /
inspect / subgraph within the TTL.

Mechanism
---------
1. Read `last_assistant_message` from Claude Code's Stop-hook stdin
   payload (synchronous, not subject to the JSONL flush race that bit
   the deference-detector pre-2026-05-07 per ob_1540 / ls_0029).
2. Extract node-shaped tokens via engram_ids.find_node_ids (SSoT
   regex shipped in PR #212, supports \\d{4,} past 9999).
3. Load ~/.engram/recall_window.json and collect IDs whose window
   entry has `turn >= current_turn - RECALL_WINDOW_TTL` (TTL = 3).
4. INTERSECT mentioned IDs with the active window — only credit IDs
   the substrate brought up. IDs the agent typed without substrate
   recall (cited from training, copy-pasted, etc.) are not credited.
5. Apply Q-update: `Q_new = Q_old + α(1 - Q_old)` with ALPHA_MENTION
   = 0.12. The midpoint between surface(0.10, "passive substrate
   hint") and inspect(0.15, "deliberate full-content recall") —
   prose-mention is engagement evidence between those two.

Idempotency: a node mentioned twice in the same response receives a
single bump (find_node_ids dedups in order). Mentioned across two
turns within TTL → two bumps (matches MemRL repeated-engagement
intent).

Independent of engram_derive's existing credit path: derive scans
premise IDs, this hook scans prose. Both can fire on the same turn
for the same ID — that's the design (deliberate citation + prose
narration = stronger engagement signal than either alone).

Best-effort: any failure swallows silently. Behavioral hooks must
never block the session.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))

# Make engram_ids importable at module scope (avoids duplicate sys.path inserts
# on repeated calls to credit_mentions in test harnesses).
sys.path.insert(0, ENGRAM_HOME)
RECALL_WINDOW_PATH = os.path.join(ENGRAM_HOME, "recall_window.json")
KNOWLEDGE_DB = os.path.join(ENGRAM_HOME, "knowledge.db")
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")
LOG_PATH = os.path.join(ENGRAM_HOME, "utility-credit-mention.log")

# Q-update learning rate for "mention" credit. Midpoint between
# UTILITY_ALPHA["surface"]=0.10 and UTILITY_ALPHA["inspect"]=0.15 from
# server.py:UTILITY_ALPHA. Mention = "substrate brought it up AND agent
# engaged enough to reference it in prose."
ALPHA_MENTION = 0.12

# Must match server.py RECALL_WINDOW_TTL.
RECALL_WINDOW_TTL = 3


def get_current_turn(config_path: str = CONFIG_PATH) -> int:
    """Read current turn from config.json. 0 if unreadable or missing."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return int(config.get("memory", {}).get("current_turn", 0))
    except Exception:
        return 0


def recall_window_active_ids(
    current_turn: int,
    recall_window_path: str = RECALL_WINDOW_PATH,
    ttl: int = RECALL_WINDOW_TTL,
) -> set[str]:
    """Return IDs in the recall window whose entry-turn is within `ttl` of `current_turn`.

    Empty set if file missing, corrupt, or no entries qualify.
    """
    if not os.path.exists(recall_window_path):
        return set()
    try:
        with open(recall_window_path) as f:
            data = json.load(f)
    except Exception:
        return set()
    active: set[str] = set()
    cutoff = current_turn - ttl
    for entry in data.get("entries", []):
        if entry.get("turn", 0) >= cutoff:
            for nid in entry.get("ids", []):
                if isinstance(nid, str):
                    active.add(nid)
    return active


def bump_utility(
    node_ids: set[str],
    alpha: float,
    db_path: str = KNOWLEDGE_DB,
) -> int:
    """Apply Q-update `Q_new = Q_old + alpha * (1 - Q_old)` to each node_id.

    Returns the count of nodes actually updated. IDs not present in
    `nodes` are silently skipped (the shape-regex can match non-node
    tokens like `in_1234` per engram_ids docstring).
    """
    if not node_ids:
        return 0
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        updated = 0
        for nid in node_ids:
            row = conn.execute(
                "SELECT utility_score FROM nodes WHERE id = ?", (nid,)
            ).fetchone()
            if row is None:
                continue
            q_old = row[0] or 0.0
            q_new = q_old + alpha * (1.0 - q_old)
            conn.execute(
                "UPDATE nodes SET utility_score = ? WHERE id = ?",
                (round(q_new, 6), nid),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def _log(line: str, log_path: str = LOG_PATH) -> None:
    try:
        with open(log_path, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {line}\n")
    except Exception:
        pass


def _is_real_user_message(content) -> bool:
    """Return True if `content` represents a genuine user message (not tool_result).

    Claude Code's JSONL uses user-typed entries for both real user input AND
    for tool_result payloads (the harness's response to a tool_use block).
    Tool_result entries must NOT act as stop-markers when walking backwards
    through a turn's assistant prose.

    Real user message conditions:
      - content is a non-empty bare string (always real user text), OR
      - content is a non-empty list of blocks where NO block has type == "tool_result"

    Empty content (empty string or empty list) is treated as NOT a real user
    message — Claude Code does not emit empty-content user entries in
    practice, and treating an empty list as a turn boundary would cut off
    the backwards walk prematurely.
    """
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        if not content:
            return False
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def collect_turn_text(transcript_path: str, last_assistant_message: str) -> str:
    """Collect ALL assistant text blocks from the current turn in the JSONL.

    Walks the JSONL at `transcript_path` backwards, collecting text blocks
    from `assistant`-type entries and stopping when a real user message is
    encountered (a user entry whose content is not a tool_result payload).

    Concatenates the JSONL-collected text with `last_assistant_message`
    (the just-emitted block from stdin that hasn't flushed to the JSONL yet).

    Returns the combined string. Falls back to `last_assistant_message` alone
    on any read/parse error.
    """
    collected: list[str] = []
    if transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path) as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                entry_type = obj.get("type")
                if entry_type == "assistant":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if content is None:
                        continue
                    if isinstance(content, str):
                        if content:
                            collected.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text") or ""
                                if t:
                                    collected.append(t)
                elif entry_type == "user":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if _is_real_user_message(content):
                        break  # reached the boundary of this turn
                    # tool_result user entry — part of the same turn, keep walking
        except OSError:
            pass

    # Reverse to restore chronological order, then append stdin's last message.
    parts = list(reversed(collected))
    if last_assistant_message:
        parts.append(last_assistant_message)
    return "\n".join(parts)


def credit_mentions(
    last_message: str,
    *,
    config_path: str = CONFIG_PATH,
    recall_window_path: str = RECALL_WINDOW_PATH,
    db_path: str = KNOWLEDGE_DB,
    alpha: float = ALPHA_MENTION,
    ttl: int = RECALL_WINDOW_TTL,
) -> dict:
    """Run the credit-assignment pipeline against `last_message`.

    Returns a stats dict with mentioned_count / active_window_count /
    eligible_count / updated_count. Pure function (no I/O beyond the
    configured paths) — testable independently of stdin parsing.
    """
    stats = {
        "mentioned_count": 0,
        "active_window_count": 0,
        "eligible_count": 0,
        "updated_count": 0,
    }
    if not last_message:
        return stats

    try:
        from engram_ids import find_node_ids
    except Exception:
        return stats

    mentioned = find_node_ids(last_message)
    stats["mentioned_count"] = len(mentioned)
    if not mentioned:
        return stats

    current_turn = get_current_turn(config_path)
    active = recall_window_active_ids(current_turn, recall_window_path, ttl)
    stats["active_window_count"] = len(active)
    if not active:
        return stats

    eligible = set(mentioned) & active
    stats["eligible_count"] = len(eligible)
    if not eligible:
        return stats

    try:
        stats["updated_count"] = bump_utility(eligible, alpha, db_path)
    except Exception as e:
        _log(f"bump_utility failed: {type(e).__name__}: {e}")
    return stats


def main() -> None:
    _t0 = time.perf_counter()

    last_message = ""
    session_id = "unknown"
    transcript_path = ""
    try:
        raw = sys.stdin.read()
        if raw:
            payload = json.loads(raw)
            # Stop hooks can re-fire; guard against double credit-assignment.
            if payload.get("stop_hook_active", False):
                return
            lam = payload.get("last_assistant_message")
            if isinstance(lam, str):
                last_message = lam
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                transcript_path = tp
    except Exception:
        return

    # Combine all assistant prose blocks from the current turn (JSONL walk)
    # with the just-emitted last_assistant_message from stdin.
    turn_text = collect_turn_text(transcript_path, last_message)

    stats = credit_mentions(turn_text)
    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    if stats["mentioned_count"] > 0:
        _log(
            f"session={session_id} "
            f"mentioned={stats['mentioned_count']} "
            f"active_window={stats['active_window_count']} "
            f"eligible={stats['eligible_count']} "
            f"updated={stats['updated_count']} "
            f"duration_ms={_duration_ms}"
        )

    try:
        from engram_log_emitter import Emitter
        emitter = Emitter.init(
            session_id=session_id,
            transcript_path=transcript_path,
        )
        emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-utility-credit-mention-stop",
                "hook_type": "Stop",
                "duration_ms": _duration_ms,
                "exit_code": 0,
                "mentioned_count": stats["mentioned_count"],
                "active_window_count": stats["active_window_count"],
                "eligible_count": stats["eligible_count"],
                "updated_count": stats["updated_count"],
                "alpha_mention": ALPHA_MENTION,
                "context_mode": os.environ.get("ENGRAM_CONTEXT_MODE", "") or None,
            },
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
