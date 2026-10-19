"""One owner-read helper for tools/ (U14 of #2652, #3862; extracted from U12's reconcile_lesson_demotions).

A tool that reads the live store through the engramd owner behind a flag needs the same five things:

  1. the ENGRAM_HOME resolution (one definition: `engram_home()`),
  2. the runtime-module import (engramd_client / engramd_owner_flags live in the plugin root when
     deployed and in src/engram/ in the source tree),
  3. "does the flag apply to THIS db?" (`flag_applies`: flag ON and the db is this home's store),
  4. the ping that replaces the `db_path.exists()` precondition (`ping`),
  5. a read connection: the owner shim under the flag, else a `mode=ro` sqlite connection
     (`open_reader`), with every owner failure turned into the caller's own typed error,
  6. the flag-OFF store-path precheck that replaces the same precondition (`check_store_path`, #3960): it
     classifies a path (not found, uncheckable, not a store) and never picks an exit code; each tool maps the
     classes to the codes it documents.

Each tool describes itself with one `OwnerReadSpec` (its flag key, its error class, its home, the
sentence that says what an empty answer would have meant) and calls these functions with it. The
contract the spec carries is the one design section 5.5 sets: with the flag ON and the owner down,
refusing, or answering malformed, the tool RAISES its typed error. It never answers from an empty
list and never falls back to a direct open.

Imported by an essential tool (cohort_dispatch), so this module ships at tier essential.
"""

from __future__ import annotations

import errno
import importlib
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "OwnerReadSpec",
    "engram_home",
    "default_db_path",
    "runtime_dir",
    "import_runtime",
    "flag_applies",
    "failure",
    "ping",
    "open_reader",
    "check_store_path",
    "StoreUnusable",
    "StoreUncheckable",
    "StoreNotAStore",
    "snapshot",
    "write",
]

# A stuck owner is an error and not a hang. A JUDGEMENT, not a measurement (a tool runs once per sleep,
# so latency is not gated); a spec may override.
DEFAULT_TOTAL_BUDGET_SECONDS = 60.0
DEFAULT_PER_CALL_SECONDS = 10.0
# The owner's own SNAPSHOT_REQUEST_DEADLINE is 30 s; the client must wait past it so the owner's typed timeout
# reply arrives instead of this side giving up first. A JUDGEMENT, not a measurement.
DEFAULT_SNAPSHOT_SECONDS = 35.0
# A typed owner write (U16, #3879) waits past the owner's own write-lock budget so the owner's typed answer
# (busy, readback_mismatch) arrives instead of this side giving up first. A JUDGEMENT, not a measurement.
DEFAULT_WRITE_SECONDS = 30.0


def engram_home() -> Path:
    """The ONE ENGRAM_HOME resolution for tools: $ENGRAM_HOME (default ~/.engram) when set and
    non-empty, expanded and absolute; a RELATIVE value is refused (ValueError naming it). The self-contained layers (hooks, prompt hooks, shared-bin
    flat copies) carry a copy of this body, pinned AST-identical by
    tests/test_engram_home_parity_3863.py (the #3628 pattern)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


def default_db_path() -> Path:
    return engram_home() / "knowledge.db"


@dataclass(frozen=True)
class OwnerReadSpec:
    """How one tool uses the owner.

    flag_key      the key under `engramd` in config.json (e.g. "cohort_dispatch_reads_via_owner")
    error_cls     the tool's own typed error; every owner failure is raised as this
    home          the ENGRAM_HOME the flag and the socket are resolved against
    never_empty   the sentence appended to a refusal, saying what an empty answer would have been read as
    """

    flag_key: str
    error_cls: type
    home: Path
    never_empty: str
    total_budget: float = DEFAULT_TOTAL_BUDGET_SECONDS
    per_call: float = DEFAULT_PER_CALL_SECONDS
    snapshot_timeout: float = DEFAULT_SNAPSHOT_SECONDS
    write_timeout: float = DEFAULT_WRITE_SECONDS


def runtime_dir() -> Path | None:
    """The directory holding engramd_client.py (the plugin root when deployed, src/engram/ in the source
    tree): $CLAUDE_PLUGIN_ROOT first, then a walk of ALL parent directories of this file (never a fixed
    parents[N] -- the path hazards _resolve_hook_path in reconcile_lesson_demotions documents)."""
    roots: list[Path] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        roots.append(Path(plugin_root))
    roots.extend(Path(__file__).resolve().parents)
    for root in roots:
        try:
            if (root / "engramd_client.py").exists():
                return root
        except OSError:
            continue
    return None


def import_runtime(name: str):
    """Import a runtime module (engramd_client, engramd_owner_flags) from runtime_dir()."""
    runtime = runtime_dir()
    if runtime is None:
        raise ImportError(f"{name}: no engramd_client.py found via CLAUDE_PLUGIN_ROOT or a walk of this file's parents")
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    return importlib.import_module(name)


def _report_flag_failure(spec: OwnerReadSpec, exc: BaseException) -> None:
    """Never raises: a stderr that cannot be written leaves today's silent OFF."""
    try:
        cls = "".join(ch if ch.isprintable() else "?" for ch in type(exc).__name__)[:80]
        sys.stderr.write(f"engramd.{spec.flag_key}: engramd_owner_flags failed: {cls}\n")
        sys.stderr.flush()
    except Exception:
        pass


def flag_applies(spec: OwnerReadSpec, db_path: Path) -> bool:
    """True iff the owner flag is ON and `db_path` is THIS home's store. A flag that cannot be read is
    OFF (the U4d precedent: fail to false); an explicit --db naming another store keeps the direct path."""
    try:
        flags = import_runtime("engramd_owner_flags")
        on = flags.flag_on(spec.flag_key, spec.home / "config.json")
    except (Exception, SystemExit) as exc:
        # The flag's state is UNKNOWN (a broken runtime bundle, not a config that says off): read OFF, but
        # visibly, as U4d does (#3663): ONE class-name-only stderr line, printable-sanitised, at most 80
        # characters, on every run. An absent or malformed CONFIG is not this case: flag_on reads that as
        # OFF itself, silently. KeyboardInterrupt is deliberately not caught.
        _report_flag_failure(spec, exc)
        return False
    if not on:
        return False
    return os.path.realpath(db_path) == os.path.realpath(spec.home / "knowledge.db")


def failure(spec: OwnerReadSpec, client, failed) -> Exception:
    """The spec's error for an owner that did not give rows. Three refusals, three messages, because
    their remedies differ: down -> start it; unregistered_template -> the running owner is OLDER than this
    tool, restart it on the new build; refused_at_load -> the STORE failed classification, a restart will
    not fix it. `failed` is an OwnerReadError, an Unavailable or a Rejected."""
    err, key, home = spec.error_cls, spec.flag_key, spec.home
    result = getattr(failed, "result", failed)
    kind = getattr(failed, "kind", None)
    never_empty = f" {spec.never_empty}"
    if isinstance(result, client.Unavailable):
        why = result.reason or "unavailable"
        return err(
            f"engramd owner unavailable ({why}): start the owner (the SessionStart hook "
            f"start-engramd-owner.sh launches it), or set engramd.{key} to false in "
            f"{home / 'config.json'} to read the store directly.{never_empty}"
        )
    if isinstance(result, client.Rejected):
        if result.reason == "unregistered_template":
            return err(
                "the running engramd owner does not know this read (unregistered_template): it is older "
                "than this tool, so restart the owner on the current build, or set "
                f"engramd.{key} to false.{never_empty}"
            )
        if result.reason == "refused_at_load":
            return err(
                f"the engramd owner dropped this read at load against the store (refused_at_load: "
                f"{result.detail}). This is a store/schema problem: restarting the owner alone will not fix it, so "
                f"migrate the store (any ENGRAM session start does), then restart the owner.{never_empty}"
            )
        return err(
            f"the engramd owner refused the read ({result.reason}: {result.detail}); {result.remedy}. If that does not "
            f"help, set engramd.{key} to false to read the store directly.{never_empty}"
        )
    return err(
        f"the engramd owner read failed ({kind or 'unusable answer'}); retry once the owner is healthy, or set "
        f"engramd.{key} to false to read the store directly.{never_empty}"
    )


def _socket(client, spec: OwnerReadSpec) -> Path:
    return client.socket_path_for_home(spec.home)


def ping(spec: OwnerReadSpec) -> None:
    """Flag ON replaces `db_path.exists()`: the owner must answer. Raises spec.error_cls if it does not."""
    err, key = spec.error_cls, spec.flag_key
    try:
        client = import_runtime("engramd_client")
    except Exception as exc:
        raise err(
            f"engramd.{key} is on but engramd_client could not be loaded "
            f"({type(exc).__name__}: {exc}); not reading the store directly. Fix the plugin install, or set "
            f"the flag to false."
        ) from exc
    try:
        result = client.read("ping", {}, timeout=spec.per_call, socket_path=_socket(client, spec))
    except (Exception, SystemExit) as exc:  # client.read does not raise by contract (#3717); belt, as OwnerReadConnection
        raise err(
            f"the engramd owner ping failed ({type(exc).__name__}); not reading the store directly. Start the "
            f"owner, or set engramd.{key} to false.") from exc
    if not isinstance(result, client.Ok):
        raise failure(spec, client, result)


class _OwnerReader:
    """The owner's read shim with sqlite's `execute(...).fetchone()` / `fetchall()` / `close()` face,
    turning every OwnerReadError into the spec's error for its cause."""

    def __init__(self, spec: OwnerReadSpec, client):
        self._spec = spec
        self._client = client
        self._conn = client.OwnerReadConnection(
            spec.total_budget, spec.per_call, socket_path=_socket(client, spec)
        )

    def execute(self, sql, params=()):
        try:
            return self._conn.execute(sql, params)
        except self._client.OwnerReadError as exc:
            raise failure(self._spec, self._client, exc) from exc

    def close(self):
        self._conn.close()


class StoreUnusable(Exception):
    """The base of the two classes `check_store_path` raises for a path that is not a usable store. Neither an
    OSError nor a FileNotFoundError, so no existing `except OSError` or `except FileNotFoundError` swallows
    one as something else."""


class StoreUncheckable(StoreUnusable):
    """The path exists or may exist but could not be checked (a parent that cannot be searched, a symlink loop,
    a dangling link, any other OSError). The remedy is to fix the path, never to create a store there."""


class StoreNotAStore(StoreUnusable):
    """The path resolves but is not a regular file (a FIFO, a directory, a socket, a device). The remedy is to
    fix the path, never to create a store there."""


def _errno_symbol(exc: OSError) -> str:
    code = getattr(exc, "errno", None)
    return errno.errorcode.get(code, "OSError") if code is not None else "OSError"


def _uncheckable(db_path, exc: OSError) -> StoreUncheckable:
    why = " ".join(str(exc.strerror or exc).split())
    return StoreUncheckable(
        f"cannot check the store at {db_path} ({_errno_symbol(exc)}: {why}); fix the path, do not create a "
        f"store there"
    )


def _file_type_words(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "not a regular file"


def check_store_path(db_path) -> None:
    """Flag OFF replaces `db_path.exists()`. Returns None for a usable regular file (a symlink to one counts);
    otherwise raises, classifying and NEVER choosing an exit code (each tool maps the classes itself):

      no directory entry at all                    FileNotFoundError("ENGRAM database not found: <path>"),
                                                   the type and text callers raise for a missing store
      any other lstat failure (EACCES on a parent,
        ENOTDIR, ...)                              StoreUncheckable
      lstat ok, stat fails (ELOOP, a dangling link,
        EACCES on the target)                      StoreUncheckable
      stat ok, not a regular file                  StoreNotAStore (FIFO, directory, socket, device)

    The check is `lstat` then `stat` on the resolved target and it NEVER opens the path: a plain open of a
    FIFO blocks (macOS `mode=ro`), so this is the only order in which a FIFO cannot hang the caller.
    Out of scope by ruling: a file swapped for a FIFO between this check and the open."""
    try:
        os.lstat(db_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"ENGRAM database not found: {db_path}") from None
    except OSError as exc:
        raise _uncheckable(db_path, exc) from exc
    try:
        st = os.stat(db_path)
    except OSError as exc:
        raise _uncheckable(db_path, exc) from exc
    if not stat.S_ISREG(st.st_mode):
        raise StoreNotAStore(
            f"the store path {db_path} is {_file_type_words(st.st_mode)}, not a regular file; fix the path, do "
            f"not create a store there"
        )


def open_reader(spec: OwnerReadSpec, db_path: Path, use_owner: bool):
    """The read connection: the owner shim under the flag, else a `mode=ro` sqlite connection.

    The direct path opens read-only on BOTH flag values (the uri is percent-encoded, so a `?` or `#` in
    the path cannot be read as a uri parameter). Flag OFF runs `check_store_path` first, so a FIFO (whose
    `mode=ro` open blocks) is refused before any open for every caller, including one that forgot its own
    precheck. The flag-ON branch is untouched: the owner answers for the store there."""
    if use_owner:
        return _OwnerReader(spec, import_runtime("engramd_client"))
    check_store_path(db_path)
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)


def snapshot(spec: OwnerReadSpec, dest_path: Path) -> Path:
    """Ask the owner for one quiesced copy of the store at `dest_path` (an absolute path whose parent
    directory already exists and is writable). Returns `dest_path` ONLY for a well-formed success; every
    other outcome RAISES spec.error_cls, so a caller can never mistake it for a snapshot.

    Indeterminate is failure. An `Unavailable` (a dropped connection) and a `Rejected` with
    `commit == "indeterminate"` both mean the destination may or may not hold a complete snapshot (the
    client's documented reading: "go look at dest_path"). This function does NOT go look: it never opens,
    reads or connects to the file on any failure path, so a possibly-partial file is never trusted.

    A well-formed success is an `Ok` whose result is a dict naming exactly `dest_path`, with a regular file
    there. An `Ok` that fails any of those (a different path, a non-dict result, no file) is the
    malformed-but-ok class and raises too."""
    err, key = spec.error_cls, spec.flag_key
    dest = str(dest_path)
    if not Path(dest_path).is_absolute():
        # The owner does not require an absolute path; a relative one would resolve against ITS cwd.
        raise err("snapshot dest_path must be absolute (a relative path would resolve against the owner's cwd)")
    try:
        client = import_runtime("engramd_client")
    except Exception as exc:
        raise err(
            f"engramd.{key} is on but engramd_client could not be loaded "
            f"({type(exc).__name__}: {exc}); not reading the store directly. Fix the plugin install, or set "
            f"the flag to false."
        ) from exc
    try:
        result = client.read(
            "snapshot", {"dest_path": dest}, timeout=spec.snapshot_timeout, socket_path=_socket(client, spec)
        )
    except (Exception, SystemExit) as exc:  # client.read does not raise by contract (#3717); belt, as ping
        raise err(
            f"the engramd owner snapshot failed ({type(exc).__name__}); the outcome is INDETERMINATE and nothing "
            f"was archived. Not reading the store directly. Start the owner, or set engramd.{key} to false."
        ) from exc
    if isinstance(result, client.Unavailable):
        if result.reason == "down":
            # "down" = no owner listening (socket missing / refused): no request reached the owner, so nothing
            # is indeterminate. Every other reason (timeout, protocol, busy, ...) may have reached it.
            raise err(
                "the engramd owner is DOWN (owner unavailable: down); no snapshot request reached it and "
                "nothing was archived. Not reading the store directly. "
                f"Start the owner and retry, or set engramd.{key} to false."
            )
        raise err(
            f"the engramd owner snapshot outcome is INDETERMINATE (owner unavailable: {result.reason or 'unavailable'}); "
            f"nothing was archived and the snapshot file was not trusted. Not reading the store directly. "
            f"Start the owner and retry, or set engramd.{key} to false."
        )
    if isinstance(result, client.Rejected):
        if getattr(result, "commit", None) == "indeterminate":
            raise err(
                f"the engramd owner snapshot outcome is INDETERMINATE ({result.reason}: {result.detail}); "
                f"nothing was archived and the snapshot file was not trusted. Not reading the store directly. "
                f"Retry once the owner is healthy, or set engramd.{key} to false."
            )
        raise failure(spec, client, result)
    if not isinstance(result, client.Ok):
        raise failure(spec, client, result)
    answer = result.result
    if not isinstance(answer, dict) or answer.get("dest_path") != dest:
        raise err(
            "the engramd owner answered the snapshot malformed (the result is not {'dest_path': <the requested "
            "path>}); nothing was archived and the snapshot file was not trusted. Not reading the store "
            f"directly. Check the owner build, or set engramd.{key} to false."
        )
    try:
        is_file = os.path.isfile(dest)
    except OSError:
        is_file = False
    if not is_file:
        raise err(
            "the engramd owner reported a snapshot but no regular file is at the requested path; nothing was "
            f"archived. Not reading the store directly. Check the owner build, or set engramd.{key} to false."
        )
    return Path(dest)


def write(spec: OwnerReadSpec, method: str, params: dict, request_id: str | None = None):
    """One typed owner WRITE (U16, #3879). Returns the owner's `result` ONLY for an `Ok` whose result is a
    dict; every other outcome RAISES spec.error_cls, so a caller can never mistake a failed or unknown write
    for a done one. Mirrors `snapshot()`: it never opens the store, on any path.

    Four outcomes are named apart because their remedies differ:
      DOWN            no owner listening: the request never reached it, nothing was written.
      INDETERMINATE   Unavailable (timeout / protocol / ...) or Rejected(commit="indeterminate"): the owner
                      may or may not have committed. The caller does not go look at the store; a re-run is
                      safe only when the op is idempotent (fill_embeddings is: a compare-and-set).
      COMMITTED, NOT VERIFIED   Rejected(commit="committed_unverified", e.g. readback_mismatch): the owner
                      committed and its own read-back failed.
      refused         any other Rejected: nothing was committed.
    An `Ok` whose result is not a dict is the malformed class and raises too."""
    err, key = spec.error_cls, spec.flag_key
    try:
        client = import_runtime("engramd_client")
    except Exception as exc:
        raise err(
            f"engramd.{key} is on but engramd_client could not be loaded "
            f"({type(exc).__name__}: {exc}); not writing the store directly. Fix the plugin install, or set "
            f"the flag to false."
        ) from exc
    try:
        result = client.write(
            method, params, request_id=request_id, timeout=spec.write_timeout, socket_path=_socket(client, spec)
        )
    except (Exception, SystemExit) as exc:  # client.write does not raise by contract (#3717); belt, as ping
        raise err(
            f"the engramd owner {method} failed ({type(exc).__name__}); the outcome is INDETERMINATE. Not "
            f"writing the store directly. Start the owner, or set engramd.{key} to false."
        ) from exc
    if isinstance(result, client.Unavailable):
        if result.reason == "down":
            raise err(
                f"the engramd owner is DOWN (owner unavailable: down); the {method} request did not reach it "
                f"and nothing was written. Not writing the store directly. Start the owner and retry, or set "
                f"engramd.{key} to false."
            )
        raise err(
            f"the engramd owner {method} outcome is INDETERMINATE (owner unavailable: "
            f"{result.reason or 'unavailable'}); the write may or may not have committed. Not writing the "
            f"store directly. Retry once the owner is healthy, or set engramd.{key} to false."
        )
    if isinstance(result, client.Rejected):
        commit = getattr(result, "commit", None)
        if commit == "indeterminate":
            raise err(
                f"the engramd owner {method} outcome is INDETERMINATE ({result.reason}: {result.detail}); the "
                f"write may or may not have committed. Not writing the store directly. Retry once the owner "
                f"is healthy, or set engramd.{key} to false."
            )
        if commit == "committed_unverified":
            raise err(
                f"the engramd owner {method} COMMITTED but could not verify it ({result.reason}: "
                f"{result.detail}); {result.remedy}. Not writing the store directly."
            )
        raise err(
            f"the engramd owner refused the {method} write ({result.reason}: {result.detail}); "
            f"{result.remedy}. Nothing was written. Not writing the store directly. If that does not help, "
            f"set engramd.{key} to false."
        )
    if not isinstance(result, client.Ok) or not isinstance(result.result, dict):
        raise err(
            f"the engramd owner answered the {method} write malformed (the result is not an object); the "
            f"outcome is INDETERMINATE. Not writing the store directly. Check the owner build, or set "
            f"engramd.{key} to false."
        )
    return result.result
