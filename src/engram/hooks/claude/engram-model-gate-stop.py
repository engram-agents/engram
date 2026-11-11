#!/usr/bin/env python3
"""Stop hook: record when the session's model family is not the seat's own (#4596, slice 1).

Design principle: the harness detects and code writes the text, so a session running on the wrong model
never speaks in the agent's voice.  This slice only DETECTS and RECORDS.  It never blocks, injects
nothing, prints nothing and always exits 0; what a mismatch should make the session do is the gate's
business, decided later.

Decision order (the Stop payload carries ``session_id`` and ``transcript_path``):

  1. ``self_lineage`` from ``$ENGRAM_HOME/config.json``, by the rule of ``engram_core._self_lineage()``
     (stripped; must match ``provider:family``).  Unset or malformed: do nothing, write nothing.  That is
     the gate's own dormancy.
  2. The newest model of the transcript, read from its last ``TAIL_BYTES`` only
     (``engram_signature.read_model_tail``, the same row rules as the signature's ``_read_model``).
  3. ``engram_core.model_id_to_lineage()``: the ONE family table.  This file holds no mapping of its own.
  4. MATCH writes nothing.  MISMATCH (mapped, and different) and UNKNOWN (cannot tell) write one record.
     UNKNOWN never collapses into MATCH.

UNKNOWN reasons (a closed set): ``transcript_path_missing``, ``transcript_not_found``,
``transcript_unreadable``, ``no_model_row`` (no assistant or attachment row yet, or only placeholder
ones, and the file is within the window), ``window_exceeded``, ``unmapped_model``, ``mixed_harness``,
``internal_error``, and ``records_cap_reached`` (written once, by the hard cap below).

Record file: ``$ENGRAM_HOME/model-gate/records.jsonl``, schema 1, one JSON object per line with these
keys only: ``schema``, ``at`` (UTC ISO-8601, from the clock), ``session_id``, ``outcome``
(``MISMATCH`` | ``UNKNOWN``), ``reason`` (null for MISMATCH), ``model_id``, ``measured_lineage``,
``self_lineage``, ``transcript_path``.  Append-only (``O_APPEND``); this code never rewrites, truncates
or rotates it.  Growth bounds: (1) change-only: no append when the newest record of the same session
(found in the last ``RECORDS_SCAN_BYTES``) has the same (outcome, reason, model_id), so a stuck mismatch
costs one line per session; (2) a hard cap: at ``RECORDS_MAX_BYTES`` exactly one final
``records_cap_reached`` line is appended and nothing more, so the cap is visible, never silent.

Time: the detector's own work stays under ``TIME_BUDGET_MS`` on a 50 MB transcript, measured in-process
without interpreter or first-import cost (``tests/test_model_gate_detector_4596.py``).

Siblings (``engram_core``, ``engram_signature``) are found via ``$CLAUDE_PLUGIN_ROOT`` first, then
``_hooklib.bounded_parents`` from this file, never above the tree root and never a fixed ``parents[N]``
(the build flattens ``hooks/claude`` to ``hooks``, #3983).

Planned and NOT built here (out of scope for slice 1): a gate-run fallback record for a seat whose hooks
are broken, a notice channel, Codex registration (the shared parser already reads Codex rollouts), and
any change to the autostart runner.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

TAIL_BYTES = 262144
RECORDS_SCAN_BYTES = 65536
RECORDS_MAX_BYTES = 5 * 1024 * 1024
TIME_BUDGET_MS = 100
SCHEMA_VERSION = 1
STDIN_MAX_BYTES = 1 << 20

try:
    from _hooklib import (
        bounded_parents as _bounded_parents,
        engram_home_or_exit as _engram_home_or_exit,
        open_regular_text_append as _open_append,
        _open_regular_fd,
    )
    _HOOKLIB_ERROR = None
except Exception as _exc:  # noqa: BLE001 - main() still exits 0; detect() reports internal_error
    _HOOKLIB_ERROR = type(_exc).__name__

# Filled by _load_runtime() on first use: a dormant seat never pays for the engram_core import.
_core = None
read_model_tail = None
_RUNTIME_ERROR = None


def _runtime_dir():
    """The first directory holding BOTH runtime modules: $CLAUDE_PLUGIN_ROOT, then this file's own tree
    (bounded, up to and including the tree root).  None when neither has them."""
    candidates = []
    env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.extend(_bounded_parents(Path(__file__).resolve()))
    for d in candidates:
        try:
            if (d / "engram_core.py").is_file() and (d / "engram_signature.py").is_file():
                return str(d)
        except OSError:
            continue
    return None


def _load_runtime():
    """Import engram_core and engram_signature; True when both are usable (cached)."""
    global _core, read_model_tail, _RUNTIME_ERROR
    if _core is not None:
        return True
    try:
        if _HOOKLIB_ERROR:
            raise ImportError(_HOOKLIB_ERROR)
        d = _runtime_dir()
        if d is None:
            raise ImportError("runtime modules not found")
        if d not in sys.path:
            sys.path.insert(0, d)
        import engram_core
        from engram_signature import read_model_tail as _rmt
        _core = engram_core
        if read_model_tail is None:  # (a test may have replaced it)
            read_model_tail = _rmt
        return True
    except Exception as exc:  # noqa: BLE001
        _RUNTIME_ERROR = type(exc).__name__
        return False


# ---------------------------------------------------------------- self_lineage

def _config_lineage(home):
    """The stripped ``self_lineage`` string of config.json, or "" when absent, unreadable or not a string."""
    try:
        fd = _open_regular_fd(str(Path(home) / "config.json"))
        if fd is None:
            return ""
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        val = cfg.get("self_lineage") if isinstance(cfg, dict) else None
        return val.strip() if isinstance(val, str) else ""
    except Exception:  # noqa: BLE001 - dormant, like _self_lineage()
        return ""


# --------------------------------------------------------------------- records

def _records_path(home):
    return Path(home) / "model-gate" / "records.jsonl"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _record(outcome, reason, session_id, model_id, measured, self_lineage, transcript_path):
    return {
        "schema": SCHEMA_VERSION,
        "at": _now_iso(),
        "session_id": session_id,
        "outcome": outcome,
        "reason": reason,
        "model_id": model_id,
        "measured_lineage": measured,
        "self_lineage": self_lineage,
        "transcript_path": transcript_path,
    }


def _scan_tail(path):
    """(file_size, newest-first list of parsed record dicts from the last RECORDS_SCAN_BYTES).
    (0, []) when the file does not exist; the first, possibly partial, line of a cut window is dropped."""
    fd = _open_regular_fd(str(path))
    if fd is None:
        return 0, []
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - RECORDS_SCAN_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, size - start) if size > start else b""
    finally:
        os.close(fd)
    if start > 0:
        nl = data.find(b"\n")
        data = b"" if nl < 0 else data[nl + 1:]
    out = []
    for ln in reversed(data.decode("utf-8", errors="replace").splitlines()):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return size, out


def _append_line(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _open_append(str(path), encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _write_record(home, rec):
    """Append ``rec`` under the two growth bounds.  Returns the record written, or None when skipped."""
    path = _records_path(home)
    size, newest = _scan_tail(path)
    if size >= RECORDS_MAX_BYTES:
        if newest and newest[0].get("reason") == "records_cap_reached":
            return None
        rec = _record("UNKNOWN", "records_cap_reached", rec["session_id"], None, None, rec["self_lineage"], None)
        _append_line(path, rec)
        return rec
    for prior in newest:
        if prior.get("session_id") == rec["session_id"]:
            if (prior.get("outcome"), prior.get("reason"), prior.get("model_id")) == (
                    rec["outcome"], rec["reason"], rec["model_id"]):
                return None
            break
    _append_line(path, rec)
    return rec


# -------------------------------------------------------------------- detector

def detect(payload, home):
    """One Stop: decide, and write a record when the outcome calls for one.  Returns the record written
    (a dict) or None.  Never raises."""
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    session_id = session_id if isinstance(session_id, str) else None
    tpath = payload.get("transcript_path") if isinstance(payload, dict) else None
    tpath = tpath if isinstance(tpath, str) and tpath.strip() else None
    self_lineage = _config_lineage(home)
    if not self_lineage:
        return None  # dormant: write nothing, create nothing
    try:
        if not _load_runtime():
            return _safe_write(home, "UNKNOWN", "internal_error", session_id, None, None, self_lineage, tpath)
        if not _core._LINEAGE_RE.fullmatch(self_lineage):
            return None  # malformed: dormant, as _self_lineage() treats it
        if tpath is None:
            return _safe_write(home, "UNKNOWN", "transcript_path_missing", session_id, None, None, self_lineage, None)
        res = read_model_tail(tpath, TAIL_BYTES)
        if res.reason is not None:
            return _safe_write(home, "UNKNOWN", res.reason, session_id, None, None, self_lineage, tpath)
        measured = _core.model_id_to_lineage(res.model_id)
        if not measured:
            return _safe_write(home, "UNKNOWN", "unmapped_model", session_id, res.model_id, None, self_lineage, tpath)
        if measured == self_lineage:
            return None  # MATCH
        return _safe_write(home, "MISMATCH", None, session_id, res.model_id, measured, self_lineage, tpath)
    except Exception:  # noqa: BLE001 - becomes UNKNOWN internal_error when a record can still be written
        return _safe_write(home, "UNKNOWN", "internal_error", session_id, None, None, self_lineage, tpath)


def _safe_write(home, outcome, reason, session_id, model_id, measured, self_lineage, tpath):
    try:
        return _write_record(home, _record(outcome, reason, session_id, model_id, measured, self_lineage, tpath))
    except Exception:  # noqa: BLE001 - nothing can be recorded; the Stop flow is unaffected
        return None


def main():
    try:
        raw = sys.stdin.read(STDIN_MAX_BYTES)
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    try:
        if _HOOKLIB_ERROR:
            return 0
        detect(payload if isinstance(payload, dict) else {}, _engram_home_or_exit())
    except Exception:  # noqa: BLE001 - a Stop hook never raises
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
