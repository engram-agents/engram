"""Shared recall restamp + use-action table -- the single home of "this node was just used".

Two things live here so no caller can drift:

* the USE-action table (`USE_ALPHA`, `USE_ALPHA_DEFAULT`), the utility Q-update
  (`bump_utility`) and `RESTAMP_ACTIONS`, the ONE named set of actions that also
  restamp (`use_reward` owns the whole bump-then-restamp sequence).  Every
  use-site reaches it through `engram_core._utility_reward`; the mention Stop
  hook calls it directly.
* the restamp itself (below).

Deliberate recall (`engram_query._refresh_recall`) and a prose mention of a node
id (the Stop hook `engram-utility-credit-mention-stop.py`) both reset a node's
forgetting clock: `recall_turn = current_turn` and
`importance_score = importance_base * decay_base**current_turn`.  Only
deliberate recall also increments `recall_count`.  This module owns that
formula so the two callers cannot drift (Lei, 2026-10-08, engram-alpha#2005
section 3: keep the formula, let a mention refresh the time stamp).

Deliberately LIGHTWEIGHT: stdlib only.  A Stop hook imports this on every
turn, so it must never pull in engram_core, the embedding model, or the MCP
server.  engram_core imports *this* module (for `compute_importance` and
`read_memory_config`), never the reverse.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

# Use-action table: alpha of Q_new = Q_old + alpha*(1-Q_old) per USE action.
USE_ALPHA = {
    # Tier 1 -- deep cognitive load
    "derive":          0.15,
    "supersede":       0.15,
    "contradict":      0.15,
    "resolve":         0.15,
    "lesson_incident":   0.15,  # legacy key; preserved for backward-compat
    "register_exemplar": 0.15,  # unified key (used by engram_register_exemplar)
    # Tier 2 -- moderate engagement
    "inspect":         0.10,
    "subgraph":        0.10,
    "mention":         0.10,
    "focus":           0.10,  # focus_swap uses this key for the incoming set only (outgoing: "unfocus")
    "history":         0.10,
    "citation":        0.10,
    # Tier 3 -- loose touch
    "focus_load":      0.05,
    "unfocus":         0.05,
}

USE_ALPHA_DEFAULT = 0.10  # Fallback for unknown actions

# The actions whose use ALSO restamps the node (recall_turn + importance_score,
# never recall_count): the node's content is in the agent's context and it was
# shown relevant (Lei, 2026-10-08).  Deliberately NOT here: "inspect" and
# "subgraph" (they restamp AND count recall via engram_query._refresh_recall,
# gated by dream_mode -- restamping again would be redundant), "unfocus"
# (removing from mind is not use), and unknown actions (default off).
RESTAMP_ACTIONS = frozenset({
    "derive", "supersede", "contradict", "resolve",
    "register_exemplar", "lesson_incident",
    "citation", "focus", "focus_load", "history", "mention",
})

DEFAULT_DECAY_BASE = 1.014
DEFAULT_IMPORTANCE_BASE = 0.5


def default_memory_config() -> dict:
    return {"decay_base": DEFAULT_DECAY_BASE, "current_turn": 0, "tier2_max_nodes": 1000}


def read_memory_config(config_path) -> dict:
    """The `memory` block of config.json, or the defaults when the file is absent.

    A malformed file raises (json.JSONDecodeError), exactly as the original
    engram_core._get_memory_config did -- callers decide how to surface it.
    """
    path = Path(config_path)
    if path.exists():
        config = json.loads(path.read_text())
        return config.get("memory", default_memory_config())
    return default_memory_config()


def compute_importance(base: float, turn: int, decay_base: float) -> float:
    """Importance score: base * decay_base**turn."""
    return round(base * (decay_base ** turn), 6)


def restamp(
    conn: sqlite3.Connection,
    node_ids,
    *,
    count_recall: bool,
    memory_config: dict | None = None,
    config_path=None,
) -> int:
    """Restamp `node_ids`: recall_turn, importance_score, and (only when
    `count_recall`) recall_count + 1.

    `current_turn` / `decay_base` come from `memory_config` if given, else
    from `read_memory_config(config_path)`.  Ids with no row are skipped (the
    node-id shape regex can match non-node tokens).  Does NOT commit -- the
    caller owns the transaction.  Returns the number of rows restamped.
    """
    if memory_config is None:
        if config_path is None:
            raise ValueError("restamp needs memory_config or config_path")
        memory_config = read_memory_config(config_path)
    turn = memory_config.get("current_turn", 0)
    decay_base = memory_config.get("decay_base", DEFAULT_DECAY_BASE)
    restamped = 0
    for nid in node_ids:
        row = conn.execute(
            "SELECT importance_base, recall_count FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        if row is None:
            continue
        new_score = compute_importance(row[0] or DEFAULT_IMPORTANCE_BASE, turn, decay_base)
        if count_recall:
            conn.execute(
                "UPDATE nodes SET importance_score = ?, recall_turn = ?, "
                "recall_count = ? WHERE id = ?",
                (new_score, turn, (row[1] or 0) + 1, nid),
            )
        else:
            conn.execute(
                "UPDATE nodes SET importance_score = ?, recall_turn = ? WHERE id = ?",
                (new_score, turn, nid),
            )
        restamped += 1
    return restamped


def bump_utility(conn: sqlite3.Connection, node_ids, alpha: float) -> list[str]:
    """Q-update `Q_new = Q_old + alpha*(1-Q_old)` (6-dp rounding) per id.

    Duplicates collapse to one bump (first-occurrence order kept); ids with no
    row are skipped.  Does NOT commit.  Returns the ids actually updated.
    """
    updated: list[str] = []
    for nid in dict.fromkeys(node_ids):
        row = conn.execute(
            "SELECT utility_score FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        if row is None:
            continue
        q_old = row[0]
        if q_old is None:
            q_old = 0.0
        conn.execute(
            "UPDATE nodes SET utility_score = ? WHERE id = ?",
            (round(q_old + alpha * (1.0 - q_old), 6), nid),
        )
        updated.append(nid)
    return updated


class UseRewardResult(NamedTuple):
    """What `use_reward` did.  `restamp_error` is None unless the restamp was
    attempted and failed (or its config could not be read); then it is a short
    "<stage>: <Type>: <msg>" string, stage being "config" or "restamp".
    """

    bumped: int
    restamped: int
    restamp_error: str | None


def use_reward(
    conn: sqlite3.Connection,
    node_ids,
    action: str,
    *,
    memory_config=None,
    config_path=None,
    alpha: float | None = None,
) -> UseRewardResult:
    """One USE action, the single owner of "bump utility, then restamp if the
    action restamps".  Both the server (`engram_core._utility_reward`) and the
    mention Stop hook go through here.

    1. Bump utility by the action's alpha (`alpha` overrides the table; dedup,
       missing ids skipped) via `bump_utility`.  Errors here propagate: it is
       the same DB/transaction as the write it accompanies.
    2. If `action` is in RESTAMP_ACTIONS and any id was bumped, read the memory
       config (guarded) and restamp (count_recall=False) the bumped ids inside a
       SAVEPOINT on `conn`.  The restamp is decoration: on ANY config-read or
       restamp failure only the restamp is rolled back -- the bump and the
       caller's pending writes stay -- and the failure comes back as
       `restamp_error`.  A default-config fallback is deliberately NOT used: it
       would stamp turn 0, worse than skipping.
    An error while opening the SAVEPOINT itself is outside the guard and
    propagates to the caller.

    `memory_config` is a dict, or a zero-arg callable returning one (read lazily,
    inside the guard, only for a restamping action), or None to read
    `config_path`.  Never commits -- the caller owns the transaction.  Never logs
    -- each caller reports `restamp_error` through its own visible channel.
    """
    if not node_ids:
        return UseRewardResult(0, 0, None)
    if alpha is None:
        alpha = USE_ALPHA.get(action, USE_ALPHA_DEFAULT)
    updated = bump_utility(conn, node_ids, alpha)
    if not updated or action not in RESTAMP_ACTIONS:
        return UseRewardResult(len(updated), 0, None)
    try:
        if callable(memory_config):
            memory_config = memory_config()
        elif memory_config is None:
            if config_path is None:
                raise ValueError("use_reward needs memory_config or config_path")
            memory_config = read_memory_config(config_path)
    except Exception as exc:
        return UseRewardResult(
            len(updated), 0, f"config: {type(exc).__name__}: {exc}"
        )
    conn.execute("SAVEPOINT engram_use_restamp")
    try:
        restamped = restamp(
            conn, updated, count_recall=False, memory_config=memory_config
        )
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT engram_use_restamp")
        conn.execute("RELEASE SAVEPOINT engram_use_restamp")
        return UseRewardResult(
            len(updated), 0, f"restamp: {type(exc).__name__}: {exc}"
        )
    conn.execute("RELEASE SAVEPOINT engram_use_restamp")
    return UseRewardResult(len(updated), restamped, None)
