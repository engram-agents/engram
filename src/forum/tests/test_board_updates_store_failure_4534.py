"""#4534: ``GET /api/board/updates`` must not answer a failed store read with a 200.

A poller advances its cursor to the response's ``as_of``. A 200 carrying ``updates: []``
and an ``as_of`` after a failed read therefore moves the cursor past data it never saw.
A failed read is a 503 ``{"error": ...}`` (the status and shape this route already uses
for "coordination store not configured") with no ``as_of``.
"""
import sqlite3

import pytest

from forum.coordination import FileStore, SeqAllocator
from forum.db import init_db
from forum.server import create_app


class _BrokenStore(FileStore):
    """A store whose every read raises."""

    def scan_projects(self, *a, **kw):
        raise OSError("disk went away")

    def read_projects(self, *a, **kw):
        raise OSError("disk went away")


def _app(tmp_path, store):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"))
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = SeqAllocator(recover=store.recover_max_seq)
    return app


def test_failed_store_read_is_an_error_without_as_of(tmp_path):
    app = _app(tmp_path, _BrokenStore(tmp_path / "coord"))
    resp = app.test_client().get("/api/board/updates?since=0")
    assert resp.status_code == 503
    body = resp.get_json()
    assert "as_of" not in body
    assert "updates" not in body
    assert "disk went away" in body["error"]


def test_success_path_is_unchanged(tmp_path):
    store = FileStore(tmp_path / "coord")
    app = _app(tmp_path, store)
    resp = app.test_client().get("/api/board/updates?since=0")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["updates"] == []
    assert isinstance(body["as_of"], int)
    assert body["scan_ok"] is True and body["refused"] == 0


def test_scan_failure_with_a_working_fallback_read_stays_a_200(tmp_path):
    """Only a read that yields NO data is an error: a scan failure whose fallback read
    succeeds still served real data, so it keeps the fail-soft ``scan_ok: false`` 200."""

    class _ScanOnlyBroken(FileStore):
        def scan_projects(self, *a, **kw):
            raise OSError("scan failed")

        def read_projects(self, *a, **kw):  # FileStore's own delegates to the scan
            return []

    app = _app(tmp_path, _ScanOnlyBroken(tmp_path / "coord"))
    resp = app.test_client().get("/api/board/updates?since=0")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scan_ok"] is False and body["refused"] is None
    assert isinstance(body["as_of"], int)
