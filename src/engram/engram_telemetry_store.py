"""ENGRAM telemetry store — per-process JSONL writer + sqlite aggregator.

Spec: alpha #2574 (telemetry redesign), epic #2013, WS1 slice.
Design doc: docs/2574-telemetry-design.md (§3, as amended by this module's
own PR — see that section for the store-shape rationale).

This module is the **store mechanism only** — a new, self-contained module
that (a) lets any caller append a validated telemetry record via
``write_telemetry_record()`` and (b) lets an aggregator turn accumulated
per-process JSONL files into ``records.db`` via ``ingest()``. Nothing in the
existing codebase calls into this module yet (that is WS3's job); its own
tests exercise it directly.

Transport (settled on forum #406, 2026-09-11 — supersedes the doc's
originally-described sqlite-at-write shape): a **per-process append-only
JSONL file**, never sqlite written directly by each call site. Each process
that calls ``write_telemetry_record`` gets exactly one file,
``DATA_DIR/telemetry/<pid>-<disambiguator>.jsonl``, opened in append mode.
On clean process exit the file is renamed to the same name with a ``.done``
suffix (via ``atexit``) — the portable "no more appends coming" signal. A
process that never exits cleanly (crash, SIGKILL) leaves a bare ``.jsonl``
file behind; the aggregator's age-based sweep reclaims it once its content
has been fully ingested and it has gone stale.

Failure-mode contract (load-bearing, mirrors ``engram_log_emitter.py``):
``write_telemetry_record`` NEVER raises into its caller. Every failure path
is caught, counted, and reported via two independent, non-overlapping
mechanisms (doc §11 Q1) — see that function's own docstring.

This module is pure stdlib (``json``, ``os``, ``sqlite3``, ``sys``,
``threading``, ``time``, ``atexit``, ``re``, ``pathlib``, ``datetime``) and
does not import ``engram_core`` — it reads ``ENGRAM_HOME`` directly, the
same convention ``engram_log_emitter.py`` uses, so this store never depends
on ``knowledge.db``'s own init path (telemetry is a stated exception to
#2652's one-owner rule — see the design doc's §3).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "write_telemetry_record",
    "open_store",
    "ingest",
]

# ---------------------------------------------------------------------------
# Writer-side module state (per-process; mirrors engram_log_emitter.py's
# module-level singleton shape, but for a bare function API rather than a
# class instance).
# ---------------------------------------------------------------------------

_INIT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()
_DROP_LOCK = threading.Lock()

_PROCESS_FILE_PATH: Optional[Path] = None
_ATEXIT_REGISTERED = False

# Cumulative count of write failures THIS PROCESS has caught, since process
# start. Deliberately NEVER reset once recorded (see write_telemetry_record's
# docstring "Drop-counting" section for why a running total -- rather than a
# delta-since-last-report -- is what makes both drop-reporting mechanisms
# agree in the "N failures then one success, then exit" case).
_DROP_COUNT = 0

# Distinct-marker prefix for every stderr line this module emits -- mirrors
# engram_log_emitter.py's "[engram-emitter] " convention. The drop-flush-at-
# -exit line (mechanism 3, see write_telemetry_record docstring) is meant to
# be grep-able by future aggregator work (not WS1's job to build) hence the
# literal wording below matches the WS1 spec's own suggested phrasing.
_STDERR_PREFIX = "[engram-telemetry]"


def _stderr(msg: str) -> None:
    """Failure/diagnostic logging -- stderr only, never raises into caller."""
    try:
        print(f"{_STDERR_PREFIX} {msg}", file=sys.stderr)
    except Exception:
        # Even stderr can fail (closed fd, etc.) -- drop silently.
        pass


def _data_dir() -> Path:
    """Resolve DATA_DIR the same way engram_log_emitter.py does -- read
    ENGRAM_HOME directly rather than importing engram_core, so this store
    never depends on knowledge.db's own init/config path (design doc §3's
    #2652 exception)."""
    return Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))


def _telemetry_dir() -> Path:
    return _data_dir() / "telemetry"


def _new_disambiguator() -> str:
    """A monotonic-enough wall-clock disambiguator at file-open time (WS1
    spec: "only has to be unique enough to not collide with a different
    process that reused the same PID -- does NOT need to be the OS's real
    process-start-time"). Nanosecond wall-clock resolution, no process-table
    read, no new dependency."""
    return str(time.time_ns())


def _now_utc_iso(now: Optional[datetime] = None) -> str:
    """UTC, ISO-8601, explicit 'Z' -- millisecond precision, same rendering
    shape as engram_log_emitter.py's own `_emit_inner` (design doc §7: the
    originating incident was local-time symlink reads published as 'Z'; using
    datetime.now(timezone.utc) -- an AWARE UTC datetime built directly from
    the epoch clock -- rather than datetime.now() + a later conversion makes
    the local-timezone bug unrepresentable here regardless of the process's
    TZ environment)."""
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ensure_atexit_registered() -> None:
    """Register the rename-on-exit + stderr-drop-flush hook exactly once per
    process, on the FIRST call to write_telemetry_record -- regardless of
    whether that call (or any call) ever succeeds. This has to happen before
    any write is attempted: mechanism 3 (the stderr flush) exists precisely
    to survive a TOTAL store failure for the whole process lifetime (design
    doc §11.1), so it cannot be gated behind a successful file-open."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    with _INIT_LOCK:
        if _ATEXIT_REGISTERED:
            return
        atexit.register(_on_process_exit)
        _ATEXIT_REGISTERED = True


def _ensure_process_file() -> Path:
    """Resolve (creating the telemetry dir if needed) and cache this
    process's single telemetry file path. Raises on any failure (missing/
    unwritable DATA_DIR, etc.) -- the caller (write_telemetry_record) is
    responsible for catching, counting, and reporting; this function's job is
    only path resolution, not failure handling.

    Once resolved, the SAME path is reused for the rest of the process's
    lifetime (WS1 spec: "each process gets one file") -- a later transient
    write failure against an already-resolved path does not pick a new
    disambiguator."""
    global _PROCESS_FILE_PATH
    with _INIT_LOCK:
        if _PROCESS_FILE_PATH is not None:
            return _PROCESS_FILE_PATH
        telemetry_dir = _telemetry_dir()
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        path = telemetry_dir / f"{os.getpid()}-{_new_disambiguator()}.jsonl"
        _PROCESS_FILE_PATH = path
        return path


def _on_process_exit() -> None:
    """atexit hook. Factored into `_finalize_process` so tests can invoke the
    identical logic without needing a real process exit; the subprocess-
    based test in tests/test_telemetry_store.py is what actually confirms
    atexit fires this on its own."""
    _finalize_process()


def _finalize_process() -> None:
    """Rename this process's telemetry file to `.done` (if one was ever
    opened) and flush the cumulative drop count to stderr (if nonzero).
    Never raises -- this runs from an atexit hook, where an exception would
    print a traceback but not stop shutdown; caught anyway so behavior is
    identical whether this is invoked by atexit or directly by a test."""
    try:
        with _DROP_LOCK:
            dropped = _DROP_COUNT
        if dropped:
            _stderr(f"{dropped} records dropped this process")

        path = _PROCESS_FILE_PATH
        if path is not None:
            try:
                if path.exists():
                    os.rename(str(path), str(path) + ".done")
            except OSError as exc:
                _stderr(f"rename-on-exit failed for {path}: {exc!r}")
    except Exception as exc:  # pragma: no cover -- defense in depth only
        try:
            _stderr(f"finalize failed: {exc!r}")
        except Exception:
            pass


def _record_drop() -> None:
    global _DROP_COUNT
    with _DROP_LOCK:
        _DROP_COUNT += 1


def _current_drop_count() -> int:
    with _DROP_LOCK:
        return _DROP_COUNT


# ---------------------------------------------------------------------------
# Writer API
# ---------------------------------------------------------------------------


def write_telemetry_record(
    manifest_id: str,
    event_type: str,
    fields: dict[str, Any] | None = None,
) -> bool:
    """Append one telemetry record. Returns True on success, False on any
    failure. NEVER raises into the caller -- mirrors engram_log_emitter.py's
    Emitter.emit() never-raise discipline exactly (whole body wrapped in
    try/except; every internal failure is caught, logged to stderr, and the
    call returns False).

    Required fields, rejected (== False, no line written, NOT counted as a
    drop -- see "Rejection vs drop" below) if absent:
      - ``manifest_id``: non-empty string (caller-supplied).
      - ``event_type``: non-empty string (caller-supplied). This module does
        NOT validate against engram_log_emitter.py's `engram.<domain>.<action>`
        namespace convention -- that is a different system.
      - ``ts_utc`` is auto-stamped by this function (UTC, ISO-8601, explicit
        'Z' -- see `_now_utc_iso`), never caller-supplied.

    ``fields`` is a free-form dict merged into the record; WS4's projections
    (not built here) are what interpret event-type-specific payloads.

    Rejection vs drop
    ------------------
    A call with a missing/invalid ``manifest_id``/``event_type``/``fields``
    is a REJECTION: it returns False, writes no line, and does NOT increment
    the drop counter below. This is a judgment call (the WS1 spec offered
    either "no line written" or "a line is written recording the rejection"
    and asked for one, documented) -- rejection is a caller-input problem,
    distinct from the storage-failure class the drop-counting mechanism
    exists to survive (DATA_DIR missing, disk full, permission error).
    Conflating the two would make "drops" ambiguous between "my write
    infrastructure failed" and "a caller passed bad arguments."

    Failure contract (design doc §11 Q1; extended here to this store)
    -------------------------------------------------------------------
    On any STORAGE failure (DATA_DIR missing, disk full, permission error,
    whatever) this function returns False and:

      1. Counts the drop in-process (module-level ``_DROP_COUNT``, a
         CUMULATIVE per-process total -- see below for why it is never
         reset once recorded).
      2. Writes the accumulated (cumulative) drop count as a
         ``dropped_total`` field on the next successful write to the same
         file (self-healing -- lands in the artifact the aggregator already
         reads).
      3. Flushes the cumulative drop count to stderr, with the
         "N records dropped this process" marker, at process exit (survives
         a total store failure for the whole process lifetime).

    Both (2) and (3) are required -- they fail in opposite directions (2
    needs at least one recovered write to land; 3 assumes a reader) and
    neither covers the other's gap.

    Why the counter is cumulative, never reset (judgment call, flagged for
    review): the WS1 spec's own listed test is "force N consecutive
    failures, then one success -- assert the success's record carries a
    drop-count field/row showing N, AND assert the per-process stderr flush
    at exit shows N" -- in the SAME scenario. If the counter were reset to 0
    after being recorded in mechanism (2), a clean exit shortly after with
    no further failures would flush 0 via mechanism (3), not N, failing that
    literal test. A running (non-decreasing) per-process total, reported on
    every successful write once nonzero and flushed at exit, satisfies both
    halves of the test in the same run: mechanism 2's row is intentionally
    redundant with mechanism 3's stderr line whenever no NEW drop occurs
    between them, and mechanism 2 correctly reports a larger total if
    further drops occur between two successful writes.
    """
    try:
        # _ensure_atexit_registered() belongs INSIDE this try (round-1
        # review, #2847): the function's whole contract is "never raises
        # into the caller," and a call before the try was a hole in that
        # contract, not an exception to it -- atexit.register() essentially
        # never fails under normal conditions, but "essentially never" is
        # exactly the rare-real-failure class this module exists to survive.
        _ensure_atexit_registered()
        return _write_telemetry_record_inner(manifest_id, event_type, fields)
    except Exception as exc:
        _record_drop()
        _stderr(f"write_telemetry_record({event_type!r}) FAILED: {exc!r}; record dropped")
        return False


def _write_telemetry_record_inner(
    manifest_id: str,
    event_type: str,
    fields: dict[str, Any] | None,
) -> bool:
    if not isinstance(manifest_id, str) or not manifest_id:
        _stderr(
            f"write_telemetry_record: manifest_id must be a non-empty string, "
            f"got {manifest_id!r}; record rejected (not counted as a drop)"
        )
        return False
    if not isinstance(event_type, str) or not event_type:
        _stderr(
            f"write_telemetry_record: event_type must be a non-empty string, "
            f"got {event_type!r}; record rejected (not counted as a drop)"
        )
        return False
    if fields is not None and not isinstance(fields, dict):
        _stderr(
            f"write_telemetry_record: fields must be a dict or None, got "
            f"{type(fields).__name__}; record rejected (not counted as a drop)"
        )
        return False

    record: dict[str, Any] = {
        "ts_utc": _now_utc_iso(),
        "manifest_id": manifest_id,
        "event_type": event_type,
        "fields": dict(fields) if fields else {},
    }

    dropped_total = _current_drop_count()
    if dropped_total:
        record["dropped_total"] = dropped_total

    line = json.dumps(record, separators=(",", ":")) + "\n"

    path = _ensure_process_file()
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    return True


# ---------------------------------------------------------------------------
# Aggregator / ingest — schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS l0_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT NOT NULL,
    manifest_id   TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    session_id    TEXT,
    fields_json   TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    ingested_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_offsets (
    -- Keyed by _offset_key(path), NOT the literal filename: the same
    -- physical file's offset must resolve identically before and after its
    -- <pid>-<disamb>.jsonl -> <pid>-<disamb>.jsonl.done rename, or the
    -- rename silently re-triggers a full re-ingest (round-1 review, #2847).
    source_file   TEXT PRIMARY KEY,
    byte_offset   INTEGER NOT NULL DEFAULT 0,
    last_ingested_at TEXT NOT NULL
);
"""


def open_store(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the telemetry records store at ``db_path``,
    with tables created idempotently. Plain rollback journal (not WAL) --
    mirrors health/series.py's open_store(): this store is single-writer
    (the aggregator runs periodically, offline), so WAL's concurrent-reader
    benefit isn't needed here."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Aggregator / ingest — file discovery
# ---------------------------------------------------------------------------

# <pid>-<disambiguator>.jsonl or .jsonl.done. Loosely matched (any
# non-slash disambiguator, not just the writer's own all-digit
# time_ns() shape) so a future writer using a different disambiguator
# scheme is still discoverable without a matching change here.
_TELEMETRY_FILE_RE = re.compile(r"^\d+-[^/]+\.jsonl(\.done)?$")

# Age threshold (days, not minutes) for sweeping a bare (un-renamed) .jsonl
# file once it is fully ingested -- i.e. its writer crashed or was killed
# rather than exiting cleanly through the atexit rename. Per Borges's forum
# reasoning (design doc §1's file-naming section): a per-prompt hook on an
# idle seat can go DAYS between appends, so a short threshold risks treating
# a still-legitimately-idle writer as crashed and deleting its file (silent
# DATA LOSS). A long threshold only costs disk space for an already-fully-
# ingested file (a lingering file is free once nothing further can be read
# from it). The failure direction must be "disk cost", never "data loss" --
# 5 days is the midpoint of the spec's suggested 3-7 day range: long enough
# that a hook process idling over a weekend (the common multi-day gap this
# repo's own agents produce) is never mistaken for crashed, short enough
# that a genuinely crashed writer's file doesn't linger for weeks. Flagged
# as a judgment call in the WS1 handoff -- the spec explicitly left the
# exact value to the implementer's discretion.
_STALE_FILE_AGE_DAYS = 5

_SECONDS_PER_DAY = 86400.0


def _list_telemetry_files(telemetry_dir: Path) -> list[Path]:
    if not telemetry_dir.is_dir():
        return []
    out: list[Path] = []
    for p in telemetry_dir.iterdir():
        if not p.is_file():
            continue
        if _TELEMETRY_FILE_RE.match(p.name):
            out.append(p)
    return sorted(out)


def _offset_key(path: Path) -> str:
    """The ``ingest_offsets`` identity for a telemetry file -- stable across
    its rename from ``<pid>-<disamb>.jsonl`` to ``<pid>-<disamb>.jsonl.done``
    on clean writer exit (round-1 review, #2847).

    Without this, the SAME physical file re-keys itself in ``ingest_offsets``
    the instant it is renamed: ``path.name`` changes, the offset lookup on
    the new name misses, ``start_offset`` resets to 0, and every
    already-ingested byte is re-read and re-inserted as duplicate
    ``l0_records`` rows -- silently, because ``_maybe_sweep_file`` then finds
    the (newly re-ingested) ``.done`` file fully accounted for and deletes
    it, so there is no raw JSONL left to recompute the correct count from.
    This is not a rare race: incremental offset-ingest is the NORMAL path
    for any file the aggregator has already observed once while a process
    was still writing it, and the rename can land on the very next
    ``ingest()`` call after that.
    """
    name = path.name
    if name.endswith(".done"):
        return name[: -len(".done")]
    return name


# ---------------------------------------------------------------------------
# Aggregator / ingest — per-file ingest
# ---------------------------------------------------------------------------


def _ingest_one_file(conn: sqlite3.Connection, path: Path, now: Optional[datetime] = None) -> dict:
    """Ingest new complete lines from ``path`` past its recorded byte offset.

    A line is "complete" iff it ends in ``\\n``. A complete line that fails
    to parse as JSON (or fails to shape-check as a telemetry record -- see
    below) is treated the same as a truncated trailing write: counted,
    never inserted, and the recorded byte_offset does NOT advance past its
    start -- so a subsequent ingest, once the writer finishes/fixes that
    line, picks it up cleanly. Processing of THIS file stops at the first
    such line in this ingest pass (byte_offset is a single scalar checkpoint
    per file; advancing past a bad line to ingest later good ones would mean
    never revisiting the bad line).

    Returns a dict with keys: records_ingested, truncated_lines,
    drop_count_rows.
    """
    source_file = path.name
    offset_key = _offset_key(path)
    ingested_at = _now_utc_iso(now)

    row = conn.execute(
        "SELECT byte_offset FROM ingest_offsets WHERE source_file = ?",
        (offset_key,),
    ).fetchone()
    start_offset = row["byte_offset"] if row is not None else 0

    with open(path, "rb") as f:
        f.seek(start_offset)
        new_bytes = f.read()

    records_ingested = 0
    truncated_lines = 0
    drop_count_rows = 0

    cursor = 0
    complete_end = start_offset  # byte offset AFTER the last fully-consumed line

    while True:
        newline_idx = new_bytes.find(b"\n", cursor)
        if newline_idx == -1:
            remainder = new_bytes[cursor:]
            if remainder:
                # Genuine truncated trailing write -- no \n yet. Count it,
                # do not advance past its start.
                truncated_lines += 1
            break

        raw_line = new_bytes[cursor:newline_idx]
        line_start = start_offset + cursor
        line_end = start_offset + newline_idx + 1
        cursor = newline_idx + 1

        if not raw_line.strip():
            # A blank line is "complete" but carries nothing to ingest --
            # not the truncated-trailing-write case the spec describes (this
            # writer never emits a bare newline), but also not an error;
            # skip and advance past it rather than getting permanently stuck
            # retrying a line that will never become parseable.
            complete_end = line_end
            continue

        try:
            record = json.loads(raw_line.decode("utf-8"))
            if not isinstance(record, dict):
                raise ValueError(f"telemetry record is not a JSON object: {record!r}")
            ts_utc = record["ts_utc"]
            manifest_id = record["manifest_id"]
            event_type = record["event_type"]
            if not ts_utc or not manifest_id or not event_type:
                raise ValueError("telemetry record missing a required field value")
        except (ValueError, KeyError, UnicodeDecodeError, TypeError):
            # Newline-terminated but unparseable/malformed -- per this
            # writer's single-write()-call discipline this should be rare
            # (see module docstring), but handled the same way as a
            # truncated trailing write: count it, stop here, don't advance
            # past its start.
            truncated_lines += 1
            break

        fields_for_storage = dict(record.get("fields") or {})
        if "dropped_total" in record:
            fields_for_storage["dropped_total"] = record["dropped_total"]
            drop_count_rows += 1

        conn.execute(
            "INSERT INTO l0_records "
            "(ts_utc, manifest_id, event_type, session_id, fields_json, source_file, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts_utc,
                manifest_id,
                event_type,
                record.get("session_id"),
                json.dumps(fields_for_storage, separators=(",", ":")),
                source_file,
                ingested_at,
            ),
        )
        records_ingested += 1
        complete_end = line_end

    conn.execute(
        "INSERT INTO ingest_offsets (source_file, byte_offset, last_ingested_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(source_file) DO UPDATE SET "
        "byte_offset = excluded.byte_offset, last_ingested_at = excluded.last_ingested_at",
        (offset_key, complete_end, ingested_at),
    )

    return {
        "records_ingested": records_ingested,
        "truncated_lines": truncated_lines,
        "drop_count_rows": drop_count_rows,
    }


def _maybe_sweep_file(conn: sqlite3.Connection, path: Path, now: Optional[datetime] = None) -> bool:
    """Delete ``path`` iff safe to do so; return True iff deleted.

    Safe iff its entire content has been ingested (byte_offset >= file size)
    AND EITHER it is a `.done` file (writer exited cleanly, nothing more
    will ever be appended) OR it is a bare `.jsonl` file whose mtime has not
    moved for at least _STALE_FILE_AGE_DAYS (crashed-writer cleanup).
    Ingestion is always checked first -- never delete-then-ingest."""
    offset_key = _offset_key(path)
    row = conn.execute(
        "SELECT byte_offset FROM ingest_offsets WHERE source_file = ?",
        (offset_key,),
    ).fetchone()
    offset = row["byte_offset"] if row is not None else 0

    try:
        st = path.stat()
    except OSError:
        return False

    if offset < st.st_size:
        return False  # not fully ingested yet -- never delete

    if path.name.endswith(".done"):
        path.unlink()
        return True

    now_ts = now.timestamp() if now is not None else time.time()
    age_days = (now_ts - st.st_mtime) / _SECONDS_PER_DAY
    if age_days >= _STALE_FILE_AGE_DAYS:
        path.unlink()
        return True
    return False


def ingest(
    db_path: str | Path,
    telemetry_dir: str | Path,
    now: Optional[datetime] = None,
) -> dict:
    """Ingest every ``<pid>-*.jsonl``/``<pid>-*.jsonl.done`` file in
    ``telemetry_dir`` into ``records.db`` (created at ``db_path`` if
    needed), then sweep files that are fully ingested and safe to delete
    (see ``_maybe_sweep_file``).

    Returns a summary dict: ``records_ingested``, ``truncated_lines``,
    ``drop_count_rows`` (records carrying a self-healing ``dropped_total``
    field), ``files_processed``, ``files_swept``, and ``errors`` (a list of
    ``{"file": ..., "error": ...}`` for any per-file OSError encountered --
    a single unreadable file never aborts the run for the rest).

    Modeled on health/series.py's ``open_store()``/``ingest()`` shape (WS1
    spec's own precedent): every path is an explicit argument, no DATA_DIR
    read here, two passes (ingest all files, THEN sweep) so a file is never
    deleted before its own content -- or a SIBLING file discovered in the
    same run -- has been fully accounted for.
    """
    conn = open_store(db_path)
    try:
        files = _list_telemetry_files(Path(telemetry_dir))

        records_ingested = 0
        truncated_lines = 0
        drop_count_rows = 0
        files_processed = 0
        files_swept = 0
        errors: list[dict[str, str]] = []

        for f in files:
            try:
                result = _ingest_one_file(conn, f, now=now)
            except OSError as exc:
                errors.append({"file": f.name, "error": repr(exc)})
                continue
            records_ingested += result["records_ingested"]
            truncated_lines += result["truncated_lines"]
            drop_count_rows += result["drop_count_rows"]
            files_processed += 1

        conn.commit()

        for f in files:
            try:
                if _maybe_sweep_file(conn, f, now=now):
                    files_swept += 1
            except OSError as exc:
                errors.append({"file": f.name, "error": repr(exc)})
                continue

        conn.commit()

        return {
            "records_ingested": records_ingested,
            "truncated_lines": truncated_lines,
            "drop_count_rows": drop_count_rows,
            "files_processed": files_processed,
            "files_swept": files_swept,
            "errors": errors,
        }
    finally:
        conn.close()
