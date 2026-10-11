"""Contract tests for serialize-once on the owner's `read` reply (#3678 U7,
cut 2; part of #2652).

Written BEFORE the change (contract-first). The owner serialized every read
row twice: one `json.dumps` per row for the response-size bound, then the whole
reply again in `_handle_connection`. The cut builds each row's JSON once, sizes
the bound on those fragments, and assembles the reply from the SAME fragments.

Borges's invariants (design seat, #3678):
  1. the bytes size-checked are the bytes sent;
  2. the wire output is byte-identical to the old two-pass `json.dumps` output
     (default separators ", " and ": ", ensure_ascii), shown by a differential
     over unicode, floats (large, negative, tiny, -0.0), None, the empty result
     and awkward strings;
  3. refusals are unchanged (the existing read and row-limit suites run
     unmodified);
  4. read-back equality is unchanged (owner rows == direct rows).

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

ROW_CASES = {
    "empty": [],
    "ints": [[0], [1], [-1], [2**62], [-(2**62)]],
    "floats": [[1.5], [-3.25], [1e308], [-1e-310], [1.5e-10], [-0.0], [123456789.125]],
    "none": [[None], [None, None], [1, None, "x"]],
    "unicode": [["é"], ["中文"], ["😀 emoji"], ["\u0000 nul"], ["  line sep"]],
    "awkward_strings": [['quote " inside'], ["back\\slash"], ["new\nline\ttab"], ["{\"json\": [1, 2]}"], [""]],
    "mixed_wide": [[i, f"name {i}", None, i / 3.0] for i in range(25)],
}


def _old_two_pass(rows) -> str:
    return json.dumps({"status": "ok", "result": rows})


@pytest.mark.parametrize("case", sorted(ROW_CASES))
def test_assembled_reply_is_byte_identical_to_the_old_two_pass_output(case):
    """Invariant 2, as a differential on the owner's own assembler. Mutation:
    assemble with "," instead of ", " (Borges's named mutation), or drop
    ensure_ascii -> red."""
    rows = ROW_CASES[case]
    frags = [json.dumps(r) for r in rows]
    assert eo._assemble_read_ok(frags) == _old_two_pass(rows)


@requires_linux_owner
def test_the_bytes_counted_for_the_size_bound_are_the_bytes_sent():
    """Invariant 1. `_render_read_rows` returns the reply it will send and the
    count it checked; the count is computed from the reply's own fragments, so
    it can exceed the reply only by the trailing separator allowance (0 for an
    empty result, 2 otherwise -- the same allowance the old per-row count had).
    Mutation: size-check a different serialization than the one sent (e.g.
    count json.dumps(cells, separators=(',', ':'))) -> red."""
    for case, rows in ROW_CASES.items():
        reply, counted = eo._render_read_rows(iter(rows))
        text = reply.wire_text
        assert text == _old_two_pass(rows), case
        slack = counted - len(text)
        assert slack == (0 if not rows else 2), (case, counted, len(text))


@requires_linux_owner
def test_the_rendered_reply_still_reads_as_the_old_dict():
    """Code that inspects `_handle_read`'s reply as a dict sees exactly what it
    saw before. Mutation: return a bare string or a dict without `result` ->
    red."""
    rows = ROW_CASES["mixed_wide"]
    reply, _ = eo._render_read_rows(iter(rows))
    assert reply == {"status": "ok", "result": rows}
    assert reply["status"] == "ok" and reply["result"] == rows


# --- end to end: the real wire --------------------------------------------------------

def _load_a6():
    spec = importlib.util.spec_from_file_location("a6_for_serialize", REPO_ROOT / "tools" / "engramd_a6_latency.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def owner_with_unicode_people(monkeypatch):
    a6 = _load_a6()
    home = a6.make_test_store(nodes=40, people=0)
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    people = [
        ("pn_9001", "Ünïcödé Person 😀", 'profile "quoted" \\ back\nslash', json.dumps({"name": "é", "n": 1.5e-10})),
        ("pn_9002", "中文 名字", "", json.dumps({"aliases": ["a", "b"], "x": None})),
        ("pn_9003", "plain", "line sep", "{}"),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, type, claim, created_at, logical_chain, metadata, is_current) "
        "VALUES (?, 'person', ?, '2026-01-01T00:00:00', ?, ?, 1)", people)
    conn.commit()
    conn.close()
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    owner = eo.EngramdOwner(home, request_deadline=2.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner, db
    finally:
        owner.stop()


IDENTITY_SQL = ("SELECT id, claim, logical_chain, metadata FROM nodes "
                "WHERE type = 'person' AND is_current = 1")


@requires_linux_owner
def test_the_wire_reply_for_a_real_read_is_the_old_json_and_matches_a_direct_read(owner_with_unicode_people):
    """Invariants 2 and 4 end to end: the raw bytes on the socket for a real
    read are exactly the old two-pass serialization of the direct-read rows,
    newline-terminated. Mutation: send the dict through json.dumps again on
    top of the assembled text, or assemble from different fragments than were
    checked -> red."""
    owner, db = owner_with_unicode_people
    direct = [list(r) for r in sqlite3.connect(str(db)).execute(IDENTITY_SQL).fetchall()]
    assert len(direct) == 3
    request = {"contract_version": ec.CONTRACT_VERSION, "method": "read",
               "params": {"sql": IDENTITY_SQL, "args": []}, "request_id": None}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(owner.socket_path))
    s.sendall((json.dumps(request) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data == (_old_two_pass(direct) + "\n").encode("utf-8")
    res = ec.read("read", {"sql": IDENTITY_SQL, "args": []}, socket_path=owner.socket_path)
    assert isinstance(res, ec.Ok) and res.result == direct


@requires_linux_owner
def test_a_read_reply_is_serialized_once_per_row_and_never_as_a_whole(owner_with_unicode_people, monkeypatch):
    """The 'once' in serialize-once, which byte identity cannot see: re-dumping
    the reply dict at the send gives the SAME bytes, so the wire test above
    stays green under it. Spy on the owner module's json.dumps across one real
    read: exactly one call per row (a list of cells), and none on a dict.
    Mutation: _handle_connection ignores wire_text and sends
    json.dumps(response) -> red here, the wire test stays green."""
    owner, db = owner_with_unicode_people
    direct = sqlite3.connect(str(db)).execute(IDENTITY_SQL).fetchall()
    real_json = eo.json
    calls = []

    class _Spy:
        def __getattr__(self, attr):
            return getattr(real_json, attr)

        @staticmethod
        def dumps(obj, *a, **k):
            calls.append(type(obj).__name__)
            return real_json.dumps(obj, *a, **k)

    monkeypatch.setattr(eo, "json", _Spy())
    res = ec.read("read", {"sql": IDENTITY_SQL, "args": []}, socket_path=owner.socket_path)
    assert isinstance(res, ec.Ok)
    assert calls.count("list") == len(direct) == 3, calls
    assert "dict" not in calls and "_RenderedReadOk" not in calls, calls
