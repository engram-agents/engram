"""Client side of the unblock run (#4421): how the MCP server and the derive path talk to the engramd owner about it.

The run itself (a set of premise ids per Claude Code session, held in `$ENGRAM_HOME/unblock-runs.json`) belongs to the
owner, the single writer; see `engramd_owner.UnblockRunStore`. This module only:

  * resolves the SESSION KEY, always in the server process and never from the agent: `engram_signature._resolve_session_id`
    (the shared resolver; imported, not copied). No session id resolves -> `session_key()` returns None, and the callers
    say so loudly (start refuses naming the variables; a refused derive gets one line);
  * finds the owner socket from the data dir the CALLER is configured with (never from the ambient ENGRAM_HOME, so a
    scratch data dir can never reach a live seat's owner);
  * turns every non-Ok owner answer into ONE short reason string, so a reply can say `unblock_run: {"unavailable": ...}`
    instead of `null` (null means "no run is active", and must never stand for "could not find out").

Stdlib plus `engramd_client` (imported guardedly: a build without it reads as an unreachable owner, not a crash). No
sibling-path resolution: the modules are found on sys.path, source layout and flattened plugin layout alike.
"""
from __future__ import annotations

import uuid
from pathlib import Path

try:
    import engramd_client as _ec
except Exception:  # pragma: no cover -- an install without the client: every call reports the owner unreachable
    _ec = None

GET, START, END = "cascade_unblock_get", "cascade_unblock_start", "cascade_unblock_end"
UNREACHABLE = "owner unreachable"


def session_names() -> tuple:
    """The environment variable names the shared resolver reads, for error text (never copied: read off the resolver)."""
    try:
        import engram_signature as sig
        return tuple(sig._SESSION_ENV)
    except Exception:
        return ("CLAUDE_CODE_SESSION_ID",)


def session_key():
    """The Claude Code session id as `engram_signature._resolve_session_id(None)` returns it, or None when none resolves."""
    try:
        import engram_signature as sig
    except Exception:
        return None
    try:
        return sig._resolve_session_id(None)
    except sig._Refusal:
        return None


def no_session_error() -> str:
    names = ", ".join(session_names())
    return (f"no session id: the MCP server found none in its environment (looked for {names}); the unblock run is keyed "
            "by the Claude Code session id and the agent never passes it. Use engram_cascade_frontier(node_id=...) "
            "after each resolve instead.")


def _socket(data_dir):
    return _ec.socket_path_for_home(Path(data_dir))


def reason_of(res) -> str:
    """One short reason string for any non-Ok owner answer."""
    if _ec is None or isinstance(res, _ec.Unavailable):
        return UNREACHABLE
    if isinstance(res, _ec.Rejected):
        if res.reason == "unknown_operation":
            return ("owner does not support unblock runs (it predates #4421); restart it on this build, "
                    "or use engram_cascade_frontier(node_id=...) after each resolve")
        if res.reason == "unblock_state_unreadable":
            return res.detail
        return f"owner refused ({res.reason}): {res.detail}"
    return f"owner answered unexpectedly: {type(res).__name__}"


def fetch(data_dir, session, *, touch: bool = True) -> dict:
    """The session's run, read from the owner: {"run": {...} | None, "expired": {...} | None, "unavailable": str | None}.
    `session` None (no id resolved) is "no run" with nothing unavailable: no session can hold one. `touch` slides the
    24 h idle expiry; a reply that READS the run (frontier, resolve, a refused derive) touches."""
    if session is None:
        return {"run": None, "expired": None, "unavailable": None}
    if _ec is None:
        return {"run": None, "expired": None, "unavailable": UNREACHABLE}
    try:
        res = _ec.read(GET, {"session_key": session, "touch": bool(touch)}, socket_path=_socket(data_dir))
    except Exception as exc:  # ValueError from a bad path, anything else: loud, never "no run"
        return {"run": None, "expired": None, "unavailable": f"owner call failed ({type(exc).__name__}: {exc})"}
    if not isinstance(res, _ec.Ok):
        return {"run": None, "expired": None, "unavailable": reason_of(res)}
    result = res.result if isinstance(res.result, dict) else {}
    return {"run": result.get("run"), "expired": result.get("expired"), "unavailable": None}


def _write(method: str, params: dict, data_dir):
    """One write, and on an INDETERMINATE answer (Unavailable timeout/protocol) exactly ONE retry with the SAME request id:
    the owner's dedupe store answers it with the original reply, so a lost reply never changes what the call reports."""
    if _ec is None:
        return None
    rid = str(uuid.uuid4())
    res = _ec.write(method, params, request_id=rid, socket_path=_socket(data_dir))
    if isinstance(res, _ec.Unavailable) and res.reason in ("timeout", "protocol"):
        res = _ec.write(method, params, request_id=rid, socket_path=_socket(data_dir))
    return res


def start(data_dir, session: str, targets: list, started_by: str) -> dict:
    """-> {"ok": True, "run": {...}, "replaced": {...} | None} or {"ok": False, "error": str}."""
    try:
        res = _write(START, {"session_key": session, "targets": list(targets), "started_by": started_by}, data_dir)
    except Exception as exc:
        return {"ok": False, "error": f"owner call failed ({type(exc).__name__}: {exc})"}
    if res is None or not isinstance(res, _ec.Ok):
        return {"ok": False, "error": reason_of(res)}
    return {"ok": True, "run": res.result["run"], "replaced": res.result["replaced"],
            "expired": res.result.get("expired")}


def end(data_dir, session: str) -> dict:
    """-> {"ok": True, "ended": {...} | None, "expired": {...} | None} or {"ok": False, "error": str}. `expired` is the
    notice for a run that had already expired when this end removed it (the last reply that carries it)."""
    try:
        res = _write(END, {"session_key": session}, data_dir)
    except Exception as exc:
        return {"ok": False, "error": f"owner call failed ({type(exc).__name__}: {exc})"}
    if res is None or not isinstance(res, _ec.Ok):
        return {"ok": False, "error": reason_of(res)}
    return {"ok": True, "ended": res.result["ended"], "expired": res.result.get("expired")}
