"""An in-process FAKE engramd owner for tests that must run on every host (U6a, #3664).

The real owner refuses to start off Linux (Darwin arm is #3666), so a differential
harness written only against it cannot be shown to FAIL on a Mac. This stub speaks the
client's wire (`{"contract_version","method","params","request_id"}` newline-framed JSON
over an AF_UNIX stream socket) for exactly four methods:

  * `ping`  -> ok
  * `chain` -> the typed generalization-chain op (#3740): `{"node_id", "max_depth"}`, a faithful
               stand-in of the real owner's (same validation order and reason strings, the
               clamp, the serialised-reply bound, and the walk RUN BY CALLING
               engram_core.generalization_chain on a read-only connection -- not a copy of it).
               tests/test_engramd_client_chain_3740.py compares it to a REAL owner.
  * `df`    -> the IDF batch statement over `{"terms": [...]}`: a faithful stand-in of the
               real owner's typed `df` op (same validation order, same reason strings, the
               byte-exact statement on a read-only connection; a store without the vocab
               table -> `vocab_table_absent`). tests/test_engramd_client_df_shim_3665.py
               compares it to a REAL owner reason-by-reason, so it cannot drift silently.
  * `read`  -> the statement must equal, byte for byte, an entry of the committed
               `src/engram/engramd_read_templates.json` (else `unregistered_template`,
               the real owner's reason), and is executed against a read-only connection
               to `<home>/knowledge.db`; rows go back as JSON lists in SQLite order. The IDF
               batch statement is refused here with `unregistered_template` (the real
               owner's answer: the committed registry still lists it, the owner drops it at
               load); it travels only as `df`.

It is a stand-in for the transport and the registry gate, NOT a re-implementation of the
owner (no authorizer, no deadline, no size cap). Live-owner cells stay the Linux seat's.
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
from pathlib import Path

# Literal copies on purpose (the fake is an independent stand-in; the pin cells in
# test_engramd_client_df_shim_3665.py assert each equals the production constant).
IDF_BATCH_SQL = "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
DF_TERMS_MAX = 1000
DF_TERM_MAX_BYTES = 256
_DF_VOCAB_DDL_SQL = "SELECT sql FROM sqlite_master WHERE name = 'nodes_fts_vocab'"
_DF_VOCAB_VARIANT_RE = re.compile(
    r"""USING\s+fts5vocab\s*\(\s*(?:"[^"]*"|'[^']*'|\[[^\]]*\]|`[^`]*`|[\w$]+)\s*,\s*["'\[`]?\s*(\w+)\s*["'\]`]?\s*\)""",
    re.IGNORECASE,
)
_DF_SQL_COMMENT_RE = re.compile(r"/\*.*?(?:\*/|\Z)|--[^\n]*", re.DOTALL)

# #3740: the typed `chain` op. Literal copies too (pinned in tests/test_engramd_client_chain_3740.py).
CHAIN_OP = "chain"
CHAIN_NODE_ID_MAX_BYTES = 256
CHAIN_MAX_DEPTH_CAP = 4
CHAIN_RESULT_MAX_BYTES = 8 * 1024 * 1024

_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "engram" / "engramd_read_templates.json"


def registered_sql() -> set:
    return {t["sql"] for t in json.loads(_TEMPLATES.read_text())["templates"]}


def statement_of(method, params):
    """The (sql, args) a request stands for: `df` is the IDF batch statement bound to the
    JSON-encoded term list; `read` is its own sql/args; `chain` (#3740) is NOT a SQL statement
    (the owner runs engram_core's walk), so it is recorded at METHOD level as
    ("chain", [node_id, max_depth]); anything else is (None, None)."""
    params = params or {}
    if method == "df":
        return IDF_BATCH_SQL, [json.dumps(params.get("terms"))]
    if method == "read":
        return params.get("sql"), params.get("args")
    if method == "chain":
        return CHAIN_OP, [params.get("node_id"), params.get("max_depth")]
    return None, None


def _rej(reason, detail, remedy="n/a"):
    return {"status": "rejected", "reason": reason, "detail": detail, "remedy": remedy}


def df_refusal(params):
    """None when valid, else the rejection -- the real owner's order: shape, list, empty,
    too many, then per element in index order not-str, empty, too long (UTF-8 bytes)."""
    if not isinstance(params, dict) or set(params) != {"terms"}:
        return _rej("df_params_shape", "df params must be a dict with exactly the key 'terms'")
    terms = params["terms"]
    if not isinstance(terms, list):
        return _rej("df_terms_not_list", "'terms' must be a list")
    if not terms:
        return _rej("df_terms_empty", "'terms' is an empty list")
    if len(terms) > DF_TERMS_MAX:
        return _rej("df_terms_too_many", f"'terms' has {len(terms)} entries")
    for i, term in enumerate(terms):
        if not isinstance(term, str):
            return _rej("df_term_not_str", f"terms[{i}] is not a string")
        if term == "":
            return _rej("df_term_empty", f"terms[{i}] is the empty string")
        if len(term.encode("utf-8", "surrogatepass")) > DF_TERM_MAX_BYTES:
            return _rej("df_term_too_long", f"terms[{i}] exceeds {DF_TERM_MAX_BYTES} UTF-8 bytes")
    return None


def chain_refusal(params):
    """None when valid, else the rejection -- the real owner's order: shape, node_id type,
    empty, too long (UTF-8 bytes, surrogatepass), not encodable, max_depth type (bool is not
    an int), max_depth negative. A max_depth above the cap is clamped by the caller."""
    if not isinstance(params, dict) or set(params) != {"node_id", "max_depth"}:
        return _rej("chain_params_shape", "chain params must be a dict with exactly 'node_id' and 'max_depth'")
    node_id, max_depth = params["node_id"], params["max_depth"]
    if not isinstance(node_id, str):
        return _rej("chain_node_id_not_str", "'node_id' must be a string")
    if node_id == "":
        return _rej("chain_node_id_empty", "'node_id' is the empty string")
    if len(node_id.encode("utf-8", "surrogatepass")) > CHAIN_NODE_ID_MAX_BYTES:
        return _rej("chain_node_id_too_long", f"'node_id' exceeds {CHAIN_NODE_ID_MAX_BYTES} UTF-8 bytes")
    try:
        node_id.encode("utf-8")
    except UnicodeEncodeError:
        return _rej("chain_node_id_not_utf8", "'node_id' contains a lone surrogate")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return _rej("chain_max_depth_not_int", "'max_depth' must be an integer")
    if max_depth < 0:
        return _rej("chain_max_depth_negative", "'max_depth' is negative")
    return None


class FakeOwner:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.socket_path = self.home / "run" / "engramd.sock"
        self.requests = []
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self._registered = registered_sql()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        self.socket_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.socket_path))
        self._srv.listen(8)
        self._srv.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._srv is not None:
            self._srv.close()
        self.socket_path.unlink(missing_ok=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(2)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.decode("utf-8"))
            self.requests.append(request)
            conn.sendall((json.dumps(self._answer(request)) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            conn.close()

    def _answer_df(self, params) -> dict:
        refusal = df_refusal(params)
        if refusal is not None:
            return refusal
        db = self.home / "knowledge.db"
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            ddl = conn.execute(_DF_VOCAB_DDL_SQL).fetchall()
            if not ddl:
                return _rej("vocab_table_absent", "nodes_fts_vocab is not in this store's sqlite_master")
            match = _DF_VOCAB_VARIANT_RE.search(_DF_SQL_COMMENT_RE.sub(" ", ddl[0][0] or ""))
            if match is None or match.group(1).lower() != "row":
                return _rej("unsupported_vocab_variant", "nodes_fts_vocab is not an fts5vocab(..., 'row') table")
            rows = [list(r) for r in conn.execute(IDF_BATCH_SQL, (json.dumps(params["terms"]),))]
        finally:
            conn.close()
        return {"status": "ok", "result": rows}

    def _answer_chain(self, params) -> dict:
        import engram_core  # lazy: the test modules put src/engram on sys.path

        refusal = chain_refusal(params)
        if refusal is not None:
            return refusal
        depth = min(params["max_depth"], CHAIN_MAX_DEPTH_CAP)
        db = self.home / "knowledge.db"
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            result = engram_core.generalization_chain(conn, params["node_id"], depth)
        except sqlite3.Error as exc:
            return _rej("read_failed", f"{type(exc).__name__}: {exc}"[:200])
        finally:
            conn.close()
        if len('{"status": "ok", "result": ' + json.dumps(result) + "}") > CHAIN_RESULT_MAX_BYTES:
            return _rej("chain_result_too_large", "the chain is over the serialised-reply bound")
        return {"status": "ok", "result": result}

    def _answer(self, request: dict) -> dict:
        method = request.get("method")
        if method == "ping":
            return {"status": "ok", "result": "pong"}
        if method == "df":
            return self._answer_df(request.get("params"))
        if method == "chain":
            return self._answer_chain(request.get("params"))
        if method != "read":
            return {"status": "rejected", "reason": "unknown_operation",
                    "detail": f"fake owner serves ping/read/df/chain only, got {method!r}", "remedy": "n/a"}
        params = request.get("params") or {}
        sql = params.get("sql")
        if sql == IDF_BATCH_SQL:
            # The real owner drops a template over a virtual table (FTS5 vocab) at load
            # (logged as virtual_table_template) although the committed JSON still lists the
            # statement, so the lookup at request time answers `unregistered_template`
            # (measured on the real owner); the IDF batch travels only as the typed `df` op.
            return {"status": "rejected", "reason": "unregistered_template",
                    "detail": "the statement is not in the committed read-template list",
                    "remedy": "use the typed df operation"}
        if sql not in self._registered:
            return {"status": "rejected", "reason": "unregistered_template",
                    "detail": "the statement is not in the committed read-template list",
                    "remedy": "register the template (tools/engramd_read_scan.py) and restart the owner"}
        db = self.home / "knowledge.db"
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = [list(r) for r in conn.execute(sql, tuple(params.get("args") or ()))]
        finally:
            conn.close()
        return {"status": "ok", "result": rows}
