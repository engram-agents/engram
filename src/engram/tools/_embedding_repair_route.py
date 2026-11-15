"""The client route for the embedding repair (#4651 part A, slice A3): `open_route` and `publish_batch`.

Part B (the repair tool) publishes already-encoded vectors through this module and through nothing else. It never reads the routing
flag itself, never opens the store to publish, and never reimplements the primitive's validation or its debt-clearer. The contract
is the declared interface note `docs/internal/4651-part-a-interface.md`, sections 3 and 3a.

    open_route(engram_home) -> Route
    publish_batch(route, items, request_id=None) -> list[PublishResult]

The routing flag is `engramd.repair_embeddings_via_owner` (JSON `true` only). It is read by `_owner_read.flag_applies`, the one
reader every owner tool shares, in exactly two places: once by `open_route`, and again by `publish_batch` before EACH batch. The
route is fixed for a run: a flag that no longer matches the route raises `RouteChanged` and never switches route.

  route "direct"  flag OFF (or absent, or unreadable). One connection to the store per batch, one `BEGIN IMMEDIATE` ... `COMMIT`
                  per row, through `engram_embedding_publish.publish_embedding`. The owner is never contacted.
  route "owner"   flag ON. The `publish_prepared_embeddings` owner op, through `_owner_read.write`. The store is NEVER opened,
                  on any path: an owner that is down, refusing, indeterminate or answering malformed raises, and nothing is
                  written directly.

Failures are typed (all subclass `RouteError`, which carries `.results`):
  RouteChanged       the flag no longer matches the route this run opened with.
  OwnerFlagOff       the owner refused `flag_off`: this caller's route is stale (the owner's route-integrity refusal).
  OwnerUnavailable   every other owner failure; `.reason`, `.commit` (None, "indeterminate" or "committed_unverified") and
                     `.request_id` (the id used, so part B retries the SAME batch with the SAME id, which the owner replays
                     instead of re-writing).
  DirectWriteError   the direct route failed mid-batch; `.results` holds the rows already committed, in order.

The owner failures are told apart by `owner_reason` and `owner_commit`, the two attributes `_owner_read.write` sets on what it
raises. Nothing here matches message text.

The direct route opens `<home>/knowledge.db` and takes the embedding epoch from the config that `engram_core` was configured with;
a caller whose home is not the one `engram_core` resolved must call `engram_core._configure_paths(home)` first (the usual case,
the process's own ENGRAM_HOME, needs nothing).

Imported by the repair tool beside `_owner_read`, so it ships at the same tier (essential).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# append (not insert(0)): same reasoning as tools/engram-regenerate-embeddings.py (#2565, the `tools` package-name clash).
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from tools import _owner_read as owner_read  # noqa: E402

__all__ = [
    "FLAG_KEY", "OP", "MAX_BATCH", "Route", "open_route", "publish_batch",
    "RouteError", "RouteChanged", "OwnerFlagOff", "OwnerUnavailable", "DirectWriteError",
]

FLAG_KEY = "repair_embeddings_via_owner"
OP = "publish_prepared_embeddings"
# Pinned equal to the owner's PUBLISH_PREPARED_MAX_ITEMS by tests/test_4651_a3_client_route.py. A starting point, not a claim.
MAX_BATCH = 50

_STATUSES = ("refreshed", "pending", "deferred")


class RouteError(Exception):
    """The base of every failure of this module. `.results` is the list of `PublishResult` already committed before the failure
    (always [] on the owner route: a batch there is one owner call, all or nothing as far as this client can tell)."""

    def __init__(self, message: str, *, results=None):
        super().__init__(message)
        self.results = list(results or [])


class RouteChanged(RouteError):
    """The flag no longer matches the route this run opened with (either direction; an unreadable flag reads OFF). Nothing was
    written for the batch."""


class OwnerFlagOff(RouteError):
    """The owner refused `flag_off`: its own config says the flag is not on, so this caller's route is stale."""


class OwnerUnavailable(RouteError):
    """Every other owner-route failure. `.reason` is the owner's reason, or "down" / "indeterminate" / "malformed"; `.commit` is
    None, "indeterminate" or "committed_unverified"; `.request_id` is the id the batch was sent with (None only when `open_route`'s
    ping failed). The message is built from `_owner_read`'s own, which `owner_read.ping` and `owner_read.write` raise as this
    class (it is the spec's `error_cls`), so a bare `OwnerUnavailable(message)` is valid."""

    def __init__(self, message: str, *, reason=None, commit=None, request_id=None, results=None):
        super().__init__(message, results=results)
        self.reason = reason
        self.commit = commit
        self.request_id = request_id


class DirectWriteError(RouteError):
    """The direct route failed mid-batch: the current row was rolled back, no later row was touched, and `.results` holds the rows
    already committed. `.cause` (also `__cause__`) is the error."""

    def __init__(self, message: str, *, results=None, cause=None):
        super().__init__(message, results=results)
        self.cause = cause


@dataclass(frozen=True)
class Route:
    """Which way this run publishes. Holds no connection. `spec` is the owner-read spec for the owner route, else None."""

    kind: str
    home: Path
    spec: "owner_read.OwnerReadSpec | None"


def _spec(home) -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=FLAG_KEY,
        error_cls=OwnerUnavailable,
        home=Path(home),
        never_empty="Did not fall back to the store directly: nothing was published.",
    )


def _store(home) -> Path:
    return Path(home) / "knowledge.db"


def _publish_module():
    """`engram_embedding_publish`, resolved at call time from the runtime directory (the plugin root when deployed, src/engram in
    the source tree), so a cell that pins `sys.modules` and the deployed layout both get the module the caller built its
    `PreparedVector`s from."""
    return owner_read.import_runtime("engram_embedding_publish")


def open_route(engram_home) -> Route:
    """Read the flag ONCE and fix the route for the run. OFF, absent, unreadable or any non-`true` value is "direct" and the owner
    is not contacted. ON pings the owner once and is "owner"; a failed ping raises `OwnerUnavailable` with nothing opened and
    nothing written, never a direct fallback."""
    home = Path(engram_home)
    spec = _spec(home)
    if not owner_read.flag_applies(spec, _store(home)):
        return Route(kind="direct", home=home, spec=None)
    owner_read.ping(spec)
    return Route(kind="owner", home=home, spec=spec)


def _validate(pub, route, items):
    """The batch's shape, checked before the flag is read or anything is opened. Returns the batch epoch."""
    if not isinstance(route, Route) or route.kind not in ("direct", "owner"):
        raise ValueError("publish_batch needs the Route that open_route returned")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH:
        raise ValueError(f"items must be a list of 1 to {MAX_BATCH} (node_id, PreparedVector) pairs")
    seen: set = set()
    epoch = None
    for entry in items:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("every item must be a (node_id, PreparedVector) pair")
        node_id, prepared = entry
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("an item's node_id must be a non-empty str")
        if node_id in seen:
            raise ValueError(f"node_id {node_id!r} appears twice in one batch")
        seen.add(node_id)
        if not isinstance(prepared, pub.PreparedVector) or not isinstance(prepared.epoch, pub.EmbeddingEpoch):
            raise ValueError(f"the item for {node_id!r} must be a PreparedVector carrying an EmbeddingEpoch")
        if epoch is None:
            epoch = prepared.epoch
        elif prepared.epoch != epoch:
            raise ValueError("every item in one batch must carry the same epoch")
    return epoch


def publish_batch(route: Route, items, request_id=None) -> list:
    """Publish one batch (1 to MAX_BATCH `(node_id, PreparedVector)` pairs) and return the `PublishResult`s in request order.

    A bad batch is a ValueError before the flag is read or anything is opened. The flag is then RE-READ: if it no longer matches
    `route.kind` (either direction; an unreadable flag reads OFF) this raises `RouteChanged`, writes nothing and never switches
    route."""
    pub = _publish_module()
    epoch = _validate(pub, route, items)
    now = "owner" if owner_read.flag_applies(_spec(route.home), _store(route.home)) else "direct"
    if now != route.kind:
        raise RouteChanged(
            f"engramd.{FLAG_KEY} no longer matches the route this run opened with (opened {route.kind!r}, now {now!r}; an "
            "unreadable flag reads OFF). Nothing was written for this batch. Stop, leave the rest pending, and start the "
            "repair again from a fresh preview."
        )
    if route.kind == "owner":
        return _publish_owner(route, items, epoch, request_id or str(uuid.uuid4()))
    return _publish_direct(route, items, pub)


# ---- route "direct" --------------------------------------------------------------------------------------------------------

def _publish_direct(route: Route, items: list, pub) -> list:
    db = _store(route.home)
    results: list = []
    try:
        owner_read.check_store_path(db)  # never creates a store: a missing one is an error, not an empty file
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
    except Exception as exc:
        raise DirectWriteError(
            f"engramd.{FLAG_KEY} is off, so the repair writes the store directly, but the store at {db} could not be opened "
            f"({type(exc).__name__}). Nothing was written. Fix the path, or run the repair from the right ENGRAM_HOME.",
            results=results, cause=exc,
        ) from exc
    try:
        pub.core._load_vec_extension(conn)
        for node_id, prepared in items:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = pub.publish_embedding(conn, node_id, prepared=prepared)
                conn.execute("COMMIT")
            except BaseException as exc:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if not isinstance(exc, Exception):
                    raise
                raise DirectWriteError(
                    f"engramd.{FLAG_KEY} is off, so the repair writes the store directly, and the write for node {node_id!r} "
                    f"failed ({type(exc).__name__}). That row was rolled back, the {len(results)} earlier row(s) stay committed "
                    "(they are in .results) and no later row was touched. Check the store, then start the repair again from a "
                    "fresh preview.",
                    results=results, cause=exc,
                ) from exc
            results.append(result)
    finally:
        conn.close()
    return results


# ---- route "owner" ---------------------------------------------------------------------------------------------------------

def _owner_params(epoch, items: list) -> dict:
    return {
        "epoch": {"model": epoch.model, "dimension": epoch.dimension, "normalisation": epoch.normalisation},
        "items": [
            {"node_id": node_id, "vector": prepared.vector, "text_sha256": prepared.text_sha256}
            for node_id, prepared in items
        ],
    }


def _malformed(request_id: str, what: str) -> OwnerUnavailable:
    return OwnerUnavailable(
        f"the engramd owner answered the {OP} write malformed ({what}); the owner may have committed, so the outcome is "
        f"INDETERMINATE (request_id {request_id}). Not writing the store directly. Retry the SAME batch with the SAME "
        f"request_id so the owner replays it, or set engramd.{FLAG_KEY} to false and start the repair again.",
        reason="malformed", commit="indeterminate", request_id=request_id,
    )


def _results_from_answer(pub, answer: dict, items: list, request_id: str) -> list:
    """The strict check of the owner's answer. Anything else is INDETERMINATE: the owner may have committed."""
    entries = answer.get("items")
    if not isinstance(entries, list):
        raise _malformed(request_id, "'items' is not a list")
    if len(entries) != len(items):
        raise _malformed(request_id, f"it accounts for {len(entries)} of the {len(items)} items sent")
    results = []
    tally = {status: 0 for status in _STATUSES}
    for (sent_id, _prepared), entry in zip(items, entries):
        if not isinstance(entry, dict):
            raise _malformed(request_id, "an item entry is not an object")
        missing = [k for k in ("node_id", "status", "reason", "vector_changed", "debt_cleared") if k not in entry]
        if missing:
            raise _malformed(request_id, f"an item entry lacks {missing}")
        node_id, status, reason = entry["node_id"], entry["status"], entry["reason"]
        if node_id != sent_id:
            raise _malformed(request_id, f"the items are not the ones sent, in the order sent (got {node_id!r} for {sent_id!r})")
        if status not in _STATUSES:
            raise _malformed(request_id, f"an item status is not one of {list(_STATUSES)}")
        if reason is not None and not isinstance(reason, str):
            raise _malformed(request_id, "an item reason is not a string or null")
        changed = entry["vector_changed"]
        if status == "refreshed":
            if not isinstance(changed, bool):
                raise _malformed(request_id, "a refreshed item's vector_changed is not a bool")
        elif changed is not None:
            raise _malformed(request_id, "a pending or deferred item's vector_changed is not null")
        if not isinstance(entry["debt_cleared"], bool):
            raise _malformed(request_id, "an item's debt_cleared is not a bool")
        tally[status] += 1
        results.append(pub.PublishResult(
            node_id=node_id, status=status, reason=reason, vector_changed=changed,
            debt_cleared=entry["debt_cleared"], warning=None))
    counts = answer.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(_STATUSES) or any(type(v) is not int for v in counts.values()) \
            or counts != tally:
        raise _malformed(request_id, "'counts' does not equal the tally of the items")
    return results


def _from_owner(exc: Exception, request_id: str) -> RouteError:
    """Type one failure `_owner_read.write` raised, by the attributes it set (never by its text)."""
    reason = getattr(exc, "owner_reason", None)
    commit = getattr(exc, "owner_commit", None)
    if reason == "flag_off":
        return OwnerFlagOff(
            f"the engramd owner refused the {OP} write because engramd.{FLAG_KEY} is not on in its own config: this run's route "
            "is stale. Nothing was written and nothing was written directly. Stop, leave the rest pending, and start the repair "
            "again from a fresh preview."
        )
    if reason is None:
        # No owner reason: a client that raised or answered a non-object is INDETERMINATE (commit says so); anything else
        # carries nothing the owner decided (the client could not be loaded), so nothing reached it.
        reason = "indeterminate" if commit == "indeterminate" else "down"
    return OwnerUnavailable(
        f"{exc} (route engramd.{FLAG_KEY}; request_id {request_id})",
        reason=reason, commit=commit, request_id=request_id,
    )


def _publish_owner(route: Route, items: list, epoch, request_id: str) -> list:
    pub = _publish_module()
    try:
        answer = owner_read.write(route.spec, OP, _owner_params(epoch, items), request_id=request_id)
    except OwnerUnavailable as exc:
        raise _from_owner(exc, request_id) from exc
    return _results_from_answer(pub, answer, items, request_id)
