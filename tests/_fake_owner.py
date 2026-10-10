"""An in-process FAKE engramd owner for tests that must run on every host (U6a, #3664).

The real owner refuses to start off Linux (Darwin arm is #3666), so a differential
harness written only against it cannot be shown to FAIL on a Mac. This stub speaks the
client's wire (`{"contract_version","method","params","request_id"}` newline-framed JSON
over an AF_UNIX stream socket) for exactly two methods:

  * `ping`  -> ok
  * `read`  -> the statement must equal, byte for byte, an entry of the committed
               `src/engram/engramd_read_templates.json` (else `unregistered_template`,
               the real owner's reason), and is executed against a read-only connection
               to `<home>/knowledge.db`; rows go back as JSON lists in SQLite order.

It is a stand-in for the transport and the registry gate, NOT a re-implementation of the
owner (no authorizer, no deadline, no size cap). Live-owner cells stay the Linux seat's.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "engram" / "engramd_read_templates.json"


def registered_sql() -> set:
    return {t["sql"] for t in json.loads(_TEMPLATES.read_text())["templates"]}


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

    def _answer(self, request: dict) -> dict:
        method = request.get("method")
        if method == "ping":
            return {"status": "ok", "result": "pong"}
        if method != "read":
            return {"status": "rejected", "reason": "unknown_operation",
                    "detail": f"fake owner serves ping/read only, got {method!r}", "remedy": "n/a"}
        params = request.get("params") or {}
        sql = params.get("sql")
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
