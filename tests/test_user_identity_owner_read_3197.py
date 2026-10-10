"""Contract tests for engram-user-identity-hook.py's flagged read through the
owner, cells C1-C4 of U4d (#3197, part of #2652).

Written BEFORE the implementation (contract-first; project CLAUDE.md,
"Contract-first tests"). Contract: the U4d spec's C1-C4 and design decision D4
(ONE config key, `$ENGRAM_HOME/config.json`
`{"engramd": {"user_identity_reads_via_owner": true}}`, default OFF, no env
override).

  C1  differential harness (design doc 6.1): the hook's FULL stdout (and the
      exception it dies with, and the sticky marker it writes) is compared
      byte-for-byte, flag OFF (old path) vs flag ON (real owner, same store),
      INCLUDING crashes.
  C2  flag OFF is the old path: `engramd_client` is never imported or called.
  C3  owner down / refusing, flag ON: loud, never a fallback to knowledge.db,
      never `{}`, never "zero people".
  C4  the hook's owner call fits inside the hook's 3 s registration.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
_HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-user-identity-hook.py"

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

FLAG_KEY = "user_identity_reads_via_owner"


# --- fixtures / helpers --------------------------------------------------------

def make_real_store(home: Path, people, pre_metadata_guard: bool = False) -> Path:
    """knowledge.db with the REAL schema plus the given current person rows.

    `pre_metadata_guard=True` builds a store that lacks the rung-7
    guards, as a pre-rung-7 database does (#3484 / #3525): the two metadata JSON guard
    triggers (migration rung 7) are dropped BEFORE the rows go in, so a
    non-JSON `metadata` can be inserted. Same schema path, one extra step."""
    import engram_core

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        if pre_metadata_guard:
            for trig in engram_core._METADATA_GUARD_TRIGGER_NAMES:
                conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        for pid, claim, chain, meta in people:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, logical_chain, metadata, is_current) "
                "VALUES (?, 'person', ?, '2026-01-01T00:00:00', ?, ?, 1)",
                (pid, claim, chain, meta),
            )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def meta(name, aliases=(), role="r"):
    return json.dumps({"name": name, "aliases": list(aliases), "role": role})


def set_flag(home: Path, value) -> None:
    cfg = home / "config.json"
    if value is None:
        cfg.unlink(missing_ok=True)
        return
    cfg.write_text(json.dumps({"engramd": {FLAG_KEY: value}}))


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="edo-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def load_hook(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("engram_user_identity_hook_3197", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_main(mod, prompt: str):
    """-> (stdout, exception-repr-or-None, sticky-marker-text-or-None)."""
    marker = Path(mod.CURRENT_USER_PATH)
    out = io.StringIO()
    exc = None
    with patch("sys.stdin", io.StringIO(json.dumps({"prompt": prompt}))), patch("sys.stdout", out):
        try:
            mod.main()
        except SystemExit as e:
            exc = f"SystemExit({e.code})"
        except Exception as e:  # the old path's crashes are part of its behaviour
            exc = f"{type(e).__name__}: {e}"
    return out.getvalue(), exc, (marker.read_text() if marker.exists() else None)


def run_off_and_on(mod, home: Path, prompt: str, sticky=None):
    marker = Path(mod.CURRENT_USER_PATH)
    results = []
    for flag in (False, True):
        set_flag(home, flag)
        marker.unlink(missing_ok=True)
        if sticky is not None:
            marker.write_text(json.dumps(sticky))
        results.append(run_main(mod, prompt))
    return results[0], results[1]


class started_owner:
    def __init__(self, home):
        self.owner = eo.EngramdOwner(home, request_deadline=0.6)

    def __enter__(self):
        self.owner.start(install_signal_handlers=False)
        return self.owner

    def __exit__(self, *exc):
        self.owner.stop()


def additional_context(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


# --- C1: the differential harness ----------------------------------------------

ALICE = ("pn_0001", "Alice", "alice profile line", meta("Alice", ["Al"], "lead"))
BOB = ("pn_0002", "Bob", "bob profile line", meta("Bob", [], "friend"))
SAM1 = ("pn_0010", "Sam", "first sam chain", meta("Sam", ["sammy"], "one"))
SAM2 = ("pn_0011", "Sam", "second sam chain", meta("Sam", ["sammy"], "two"))
DEE = ("pn_0012", "Dee", "dee chain", meta("Dee", [], "three"))
ZOE = ("pn_0020", "Zoë 李雷", "非ASCII profile ✓", meta("Zoë 李雷", ["雷"], "ami"))
BROKEN = ("pn_0030", "Broken", "chain", "{not json")
NULLMETA = ("pn_0031", "Nully", "chain", None)

# (id, people, prompt, sticky seed, must the output be non-empty on BOTH sides)
CASES = [
    ("matching-name-prefix", [ALICE, BOB], "Alice: hello there", None, True),
    ("matching-alias", [ALICE, BOB], "al: hello there", None, True),
    ("no-prefix-no-sticky", [ALICE, BOB], "just a plain prompt", None, False),
    ("no-prefix-with-sticky", [ALICE, BOB], "just a plain prompt",
     {"person_id": "pn_0002", "name": "Bob", "role": "friend", "description": "bob profile line"}, True),
    ("prefix-matching-nobody", [ALICE, BOB], "Nobody: hello", None, False),
    ("zero-person-rows", [], "Alice: hello", None, False),
    ("several-rows-duplicate-alias-first-wins", [SAM1, SAM2, DEE], "sammy: hi", None, True),
    # A non-JSON metadata can no longer be INSERTed since rung 7 (#3484); a
    # database still at schema 6 can hold one, so this case is built with the
    # guard triggers dropped (see `pre_metadata_guard`; #3525). The refusal on
    # the current schema is pinned by test_current_schema_refuses_malformed_metadata.
    ("malformed-metadata-survived", [BROKEN, ALICE], "Alice: hello", None, True),
    ("null-metadata-survived", [NULLMETA, ALICE], "Alice: hello", None, True),
    ("non-ascii-name", [ZOE, ALICE], "雷: bonjour", None, True),
]


@requires_linux_owner
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_c1_hook_output_is_identical_flag_off_vs_on(case, home, monkeypatch):
    """Whole-stdout, exception and sticky-marker equality, off vs on, on the
    SAME store. Mutation: in the owner path drop a row / reorder rows / swap two
    columns / turn a NULL metadata into a valid, matching alias (see the controls
    below and the handoff's source-level mutations) -> red. The bad-metadata cases (malformed JSON, NULL)
    pin that BOTH paths survive them and agree (#3449): neither raises, both
    still produce Alice's identity card past the bad row. A path that crashes
    again on either side goes red."""
    _name, people, prompt, sticky, must_be_nonempty = case
    make_real_store(home, people, pre_metadata_guard=(BROKEN in people))
    mod = load_hook(home, monkeypatch)
    with started_owner(home):
        off, on = run_off_and_on(mod, home, prompt, sticky)
    if "survived" in _name:
        # Anti-vacuity for the bad-metadata cases: neither path may raise (the
        # card assert below then shows the good row after the bad one was
        # still served).
        assert off[1] is None and on[1] is None, (off[1], on[1])
    if must_be_nonempty is True:
        # Anti-vacuity: both sides produced a real identity card.
        assert additional_context(off[0]) and additional_context(on[0])
    if "survived" in _name:
        # The card names Alice (pn_0001) past the bad row, not the bad row itself.
        assert "pn_0001" in additional_context(off[0]) and "pn_0001" in additional_context(on[0])
    assert on == off


def test_current_schema_refuses_malformed_metadata(home):
    """#3525: on the CURRENT schema (rung-7 guard triggers present) the same
    malformed metadata the survived case uses is refused at INSERT, and a
    valid-JSON control goes in. Pins the constraint that makes the survived
    case need a pre-rung-7 database. Mutation: insert VALID json in the
    refusal cell, or drop the guard triggers -> red."""
    with pytest.raises(sqlite3.IntegrityError, match="valid JSON or NULL"):
        make_real_store(home, [BROKEN])
    ok_home = home / "ok"
    ok_home.mkdir()
    db = make_real_store(ok_home, [ALICE])
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT count(*) FROM nodes WHERE id='pn_0001'").fetchone()[0] == 1
    finally:
        conn.close()


def _mutating_read(mutate):
    original = ec.read

    def wrapped(*a, **k):
        result = original(*a, **k)
        if isinstance(result, ec.Ok):
            return ec.Ok(result=mutate(result.result), request_id=result.request_id)
        return result

    return wrapped


_SEVERAL = [SAM1, SAM2, DEE]
_NULL_FIRST = [NULLMETA, ALICE]

@requires_linux_owner
@pytest.mark.parametrize("name,mutate,people,prompt", [
    ("drop-one-row", lambda rows: rows[1:], _SEVERAL, "sammy: hi"),
    ("reorder-rows", lambda rows: list(reversed(rows)), _SEVERAL, "sammy: hi"),
    ("swap-two-columns", lambda rows: [[r[0], r[2], r[1], r[3]] for r in rows], _SEVERAL, "sammy: hi"),
    # A NULL metadata is survived by the hook (#3449: treated as `{}`), and so is
    # "" -- so None -> "" is invisible in the output and cannot be a control.
    # What IS visible: the NEW side turning the NULL metadata into a VALID
    # metadata whose alias matches the prompt. The NULL row comes first, so it
    # now wins the match: the card names "Nully" (pn_0031) instead of Alice
    # (pn_0001), whereas the untouched baseline skips the NULL row.
    ("none-metadata-becomes-matching-alias",
     lambda rows: [[*r[:3], meta("Nully", ["alice"])] if r[3] is None else r for r in rows],
     _NULL_FIRST, "Alice: hi"),
], ids=["drop-one-row", "reorder-rows", "swap-two-columns", "none-metadata-becomes-matching-alias"])
def test_c1_control_the_harness_goes_red_on_a_corrupted_owner_side(
        name, mutate, people, prompt, home, monkeypatch):
    """The harness can fail: the NEW side's rows are corrupted in flight (the
    OLD side is untouched) and the comparison must report a difference.
    Mutation of the harness itself (compare only `len`, or only stdout) -> this
    goes green-instead-of-red, which is the failure it exists to expose."""
    make_real_store(home, people)
    mod = load_hook(home, monkeypatch)
    with started_owner(home):
        off_clean, on_clean = run_off_and_on(mod, home, prompt)
        assert on_clean == off_clean
        assert additional_context(off_clean[0]), "baseline must agree and be non-empty"
        with patch.object(ec, "read", _mutating_read(mutate)):
            off, on = run_off_and_on(mod, home, prompt)
    assert off == off_clean, "the OLD side must be untouched by a NEW-side mutation"
    if name == "none-metadata-becomes-matching-alias":
        # The comment's claim, checked: the mutated side names the NULL row.
        assert "pn_0031" in additional_context(on[0])
        assert "pn_0001" in additional_context(off[0])
    assert on != off, f"harness failed to notice the {name} corruption"


@requires_linux_owner
def test_c1_flag_on_really_reads_through_the_owner_not_the_old_path(home, monkeypatch):
    """Without this, the differential above would pass vacuously while the flag
    is a no-op (both sides the old path). Flag ON must send exactly the hook's
    own SQL literal (D1: one source of truth) as method `read`, never call
    `get_person_nodes`, and never open the store itself. Mutation: ignore the
    flag (always the old path) -> red; send a hand-copied literal -> the
    `_PERSON_NODES_SQL` equality fails."""
    db = make_real_store(home, [ALICE, BOB])
    mod = load_hook(home, monkeypatch)
    set_flag(home, True)
    calls = []
    original = ec.read

    def spy(method, params, **kw):
        calls.append((method, params, kw))
        return original(method, params, **kw)

    monkeypatch.setattr(mod, "get_person_nodes", lambda: pytest.fail("old path used with flag ON"))
    with started_owner(home):
        seen = _spy_sqlite_connect(monkeypatch, db)
        with patch.object(ec, "read", spy):
            stdout, exc, _m = run_main(mod, "Alice: hello")
    assert [c[0] for c in calls] == ["read"]
    assert calls[0][1] == {"sql": mod._PERSON_NODES_SQL, "args": []}
    assert calls[0][2]["timeout"] == mod.OWNER_READ_TIMEOUT_S
    assert not [x for x in seen if "knowledge.db" in x], seen
    assert "Alice" in additional_context(stdout)


# --- C2: flag off is the old path exactly -----------------------------------------

@pytest.mark.parametrize("state", [
    "no-config-file",
    "config-without-engramd",
    "engramd-without-key",
    "false",
    "malformed-config-json",
    "truthy-string-is-not-true",
], ids=str)
def test_c2_flag_off_never_imports_or_calls_engramd_client(state, home, monkeypatch):
    """`sys.modules['engramd_client'] = None` makes any `import engramd_client`
    raise ImportError, so a flag-OFF hook that imports it (eagerly OR lazily)
    dies or diverges; `get_person_nodes` must be what served the read. Mutation:
    import engramd_client at module top, or branch on anything but `is True`
    -> red. ("true" as a STRING is not the boolean the key promises: off.)"""
    make_real_store(home, [ALICE, BOB])
    cfg = home / "config.json"
    cfg.unlink(missing_ok=True)
    if state == "config-without-engramd":
        cfg.write_text(json.dumps({"primary_user": "x"}))
    elif state == "engramd-without-key":
        cfg.write_text(json.dumps({"engramd": {}}))
    elif state == "false":
        cfg.write_text(json.dumps({"engramd": {FLAG_KEY: False}}))
    elif state == "malformed-config-json":
        cfg.write_text("{nope")
    elif state == "truthy-string-is-not-true":
        cfg.write_text(json.dumps({"engramd": {FLAG_KEY: "true"}}))
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    mod = load_hook(home, monkeypatch)
    served = []
    real = mod.get_person_nodes
    monkeypatch.setattr(mod, "get_person_nodes", lambda: (served.append(1), real())[1])
    stdout, exc, _marker = run_main(mod, "Alice: hello")
    assert exc is None or exc.startswith("SystemExit")
    assert served == [1]
    assert "Alice" in additional_context(stdout)


def test_c2_flag_off_keeps_the_old_paths_swallowed_failure(home, monkeypatch):
    """A migration does not fix bugs in passing: with the flag off, an
    unreadable store still yields `{}` through `except Exception: return []`.
    Mutation: let the old path raise or report -> red."""
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database" * 50)
    mod = load_hook(home, monkeypatch)
    stdout, exc, _m = run_main(mod, "Alice: hello")
    assert json.loads(stdout) == {} and exc.startswith("SystemExit")


# --- C3: owner down / refusing, flag ON ---------------------------------------------

def _spy_sqlite_connect(monkeypatch, db_path: Path):
    seen = []
    real = sqlite3.connect

    def spy(database, *a, **k):
        seen.append(str(database))
        return real(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    return seen


@pytest.mark.parametrize("prompt", ["Alice: hello there", "a plain prompt with no prefix"])
def test_c3_owner_down_is_loud_two_kinds_of_steps_and_never_opens_the_store(prompt, home, monkeypatch):
    """No owner is started. Mutation: fall back to get_person_nodes() (opens
    knowledge.db) -> the spy sees the store path; emit `{}` or treat as zero
    people -> the assertions on additionalContext fail; merge agent and human
    steps into one list -> the marker-order assertions fail."""
    db = make_real_store(home, [ALICE, BOB])
    set_flag(home, True)
    mod = load_hook(home, monkeypatch)
    expected = ec.read("read", {"sql": "x", "args": []}, timeout=mod.OWNER_READ_TIMEOUT_S,
                       socket_path=home / "run" / "engramd.sock")
    assert isinstance(expected, ec.Unavailable)
    seen = _spy_sqlite_connect(monkeypatch, db)
    stdout, exc, _m = run_main(mod, prompt)
    assert not [s for s in seen if str(db) in s or "knowledge.db" in s], seen
    out = json.loads(stdout)
    assert out != {}, "an owner outage must never read as zero people"
    ctx = additional_context(stdout)
    assert "unavailable" in ctx.lower()
    assert str(home / "run" / "engramd.sock") in ctx
    assert str(mod.OWNER_READ_TIMEOUT_S) in ctx
    a0, h0 = ctx.index("Agent steps"), ctx.index("Human steps")
    assert a0 < h0
    for step in expected.agent_steps:
        assert a0 < ctx.index(step) < h0, step
    for step in expected.human_steps:
        assert ctx.index(step) > h0, step
    assert "Alice" not in ctx, "no identity may be announced from a failed read"


@requires_linux_owner
def test_c3_rejected_by_a_real_owner_is_loud_names_reason_and_remedy(home, monkeypatch):
    """A registry drift bug for real: the store has no `nodes` table, so the
    owner's registry drops the statement and answers unregistered_template.
    Mutation: treat Rejected as an empty answer, or fall back to the store
    -> red."""
    db = home / "knowledge.db"
    sqlite3.connect(str(db)).close()
    set_flag(home, True)
    mod = load_hook(home, monkeypatch)
    with started_owner(home):
        seen = _spy_sqlite_connect(monkeypatch, db)
        stdout, exc, _m = run_main(mod, "Alice: hello")
    assert not [s for s in seen if "knowledge.db" in s], seen
    ctx = additional_context(stdout)
    assert "unregistered_template" in ctx
    assert "refused" in ctx.lower()
    # The owner's own remedy text travels verbatim (the registry's remedy
    # names the registration step).
    assert "register" in ctx.lower()


def test_c3_an_unusable_ok_result_is_loud_not_zero_people(home, monkeypatch):
    """Ok with a payload that is not rows of four cells must not be read as
    'no people'. Mutation: `rows = result.result or []` -> red."""
    make_real_store(home, [ALICE])
    set_flag(home, True)
    mod = load_hook(home, monkeypatch)
    with patch.object(ec, "read", lambda *a, **k: ec.Ok(result="garbage")):
        stdout, exc, _m = run_main(mod, "Alice: hello")
    assert json.loads(stdout) != {}
    assert "unusable" in additional_context(stdout).lower()


@pytest.mark.parametrize("exc_factory", [
    lambda: ValueError("Exceeds the limit (4300 digits) for integer string conversion"),
    lambda: TypeError("Object of type set is not JSON serializable"),
    lambda: RuntimeError("client bug"),
], ids=["ValueError", "TypeError", "RuntimeError"])
def test_c3_a_raising_client_is_a_loud_block_never_zero_people_never_the_store(exc_factory, home, monkeypatch):
    """`engramd_client.read` documents that every transport failure is returned
    as Unavailable, but nothing guarantees it never RAISES (a non-serialisable
    param, an int-digit-limit ValueError out of json.loads, a bug). The hook
    must turn that into a loud block, not die with a traceback and empty
    stdout. Mutation: drop the try/except around `ec.read` (the exception
    escapes main) -> red; emit `{}` or fall back to the store -> red."""
    db = make_real_store(home, [ALICE, BOB])
    set_flag(home, True)
    mod = load_hook(home, monkeypatch)
    seen = _spy_sqlite_connect(monkeypatch, db)

    def boom(*a, **k):
        raise exc_factory()

    with patch.object(ec, "read", boom):
        stdout, exc, marker = run_main(mod, "Alice: hello")
    assert not [s for s in seen if "knowledge.db" in s], seen
    assert exc == "SystemExit(0)", exc
    assert stdout.strip(), "a raising client must not produce empty stdout"
    assert json.loads(stdout) != {}, "must never read as zero people"
    ctx = additional_context(stdout)
    assert "unavailable" in ctx.lower() or "failed" in ctx.lower()
    assert type(exc_factory()).__name__ in ctx
    assert "Agent steps" in ctx and "Human steps" in ctx
    assert marker is None, "no identity may be switched from a failed read"
    assert "Alice" not in ctx


# --- C4: the timeout budget -----------------------------------------------------------

def _identity_hook_registration_timeout() -> int:
    hooks = json.loads((REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json").read_text())
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "engram-user-identity-hook" in str(node.get("command", "")):
                found.append(node["timeout"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(hooks)
    assert len(found) == 1, found
    return found[0]


def test_c4_owner_call_timeout_fits_inside_the_hook_registration_with_margin(home, monkeypatch):
    """Mutation: raise OWNER_READ_TIMEOUT_S to the client default or beyond
    (>= registration - 1 s margin for interpreter start, imports and the
    render) -> red."""
    mod = load_hook(home, monkeypatch)
    registration = _identity_hook_registration_timeout()
    assert registration == 3
    assert 0 < mod.OWNER_READ_TIMEOUT_S
    assert mod.OWNER_READ_TIMEOUT_S + 1.0 <= registration


def test_c4_a_trickling_peer_cannot_hold_the_hook_past_its_budget(home, monkeypatch):
    """The 5.1 trickle case through the hook: a peer that dribbles a byte every
    150 ms and never finishes a line. The client's end-to-end bound already
    handles it; this is the one integration cell. Mutation: pass the bound to a
    per-socket-op timeout only (a trickle then never trips it), or no timeout
    -> the hook overruns -> red."""
    make_real_store(home, [ALICE])
    set_flag(home, True)
    (home / "run").mkdir(mode=0o700)
    sock_path = home / "run" / "engramd.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    stop = threading.Event()

    def trickle():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            for _ in range(60):
                if stop.is_set():
                    break
                conn.sendall(b"x")
                time.sleep(0.15)
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=trickle, daemon=True)
    t.start()
    mod = load_hook(home, monkeypatch)
    try:
        start = time.monotonic()
        stdout, exc, _m = run_main(mod, "Alice: hello")
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2)
    assert elapsed < mod.OWNER_READ_TIMEOUT_S + 0.5, elapsed
    assert "unavailable" in additional_context(stdout).lower()


# --- S1 (round 2): an unimportable client is loud, never a traceback ----------------

def test_s1_flag_on_with_unimportable_client_is_loud_and_never_opens_the_store(home, monkeypatch):
    """Design 5.5. Flag ON, `engramd_client` cannot be imported (the runtime
    dir is an EMPTY dir, and the cached module is poisoned so the import must
    really fail): the hook emits the loud unavailable-style block naming the
    cause and the path searched, with agent and human steps as separate parts,
    and exits through its normal success path. Never opens the store, never
    `{}`, never zero people.
    Mutation: re-raise the ImportError in `read_people_via_owner` -> the hook
    dies with a traceback -> red. Fall back to get_person_nodes() -> the spy
    sees the store -> red. Return ([], None) -> `{}` / no block -> red."""
    db = make_real_store(home, [ALICE])
    set_flag(home, True)
    empty = home / "empty-runtime"
    empty.mkdir()
    mod = load_hook(home, monkeypatch)
    monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(empty))
    monkeypatch.setitem(sys.modules, "engramd_client", None)  # the import must raise ImportError
    seen = _spy_sqlite_connect(monkeypatch, db)
    stdout, exc, _m = run_main(mod, "Alice: hello there")
    assert exc == "SystemExit(0)", exc
    assert not [s for s in seen if "knowledge.db" in s], seen
    out = json.loads(stdout)
    assert out != {}
    ctx = additional_context(stdout)
    assert "unavailable" in ctx.lower()
    assert "engramd_client" in ctx
    assert str(empty) in ctx, "the path searched must be named"
    assert ctx.index("Agent steps") < ctx.index("Human steps")
    assert "Alice" not in ctx


def test_s1_subprocess_exit_code_is_the_normal_success_path(home):
    """The reviewer's exact reproduction, run as the real process: empty
    runtime dir, flag ON. Exit 0 with a loud block on stdout (not exit 1 with
    an empty stdout), and knowledge.db is never created or touched.
    Mutation: re-raise the ImportError -> exit 1, empty stdout -> red."""
    import subprocess

    set_flag(home, True)
    empty = home / "empty-runtime"
    empty.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env.update(ENGRAM_HOME=str(home), ENGRAM_RUNTIME_DIR=str(empty))
    r = subprocess.run([sys.executable, str(_HOOK_PATH)], input=json.dumps({"prompt": "Alice: hi"}),
                       capture_output=True, text=True, env=env, cwd=str(home))
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "unavailable" in additional_context(r.stdout).lower()
    assert not (home / "knowledge.db").exists()


# --- S2 (round 2): the DEPLOYED (flattened) layout ---------------------------------

@requires_linux_owner
def test_s2_flattened_deployed_layout_resolves_engramd_client_and_reads_through_the_owner(home, tmp_path):
    """The plugin build flattens hooks/claude/* -> hooks/* (tiers.json `dest`),
    and puts the runtime modules at the plugin root: <root>/engramd_client.py,
    <root>/hooks/{engram-user-identity-hook.py,_hooklib.py,_harness.py}.
    Recreate exactly that in a tmp dir (no src/engram ancestor anywhere on the
    path, so the repo cannot leak in), run the hook as a subprocess with
    CLAUDE_PLUGIN_ROOT and ENGRAM_RUNTIME_DIR UNSET and the flag ON against a
    real in-process owner. It must resolve the client and answer through the
    owner. Mutation: calibrate the resolver to the source layout (a fixed
    parents[N] / one level too deep) -> the client is not found -> the hook
    emits the unavailable block instead of the identity card -> red."""
    import subprocess

    make_real_store(home, [ALICE])
    set_flag(home, True)
    root = tmp_path / "engram-plugin"
    (root / "hooks").mkdir(parents=True)
    src_hooks = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
    shutil.copy(src_hooks / "engram-user-identity-hook.py", root / "hooks" / "engram-user-identity-hook.py")
    shutil.copy(src_hooks / "_hooklib.py", root / "hooks" / "_hooklib.py")
    shutil.copy(src_hooks / "_harness.py", root / "hooks" / "_harness.py")
    shutil.copy(Path(_SRC_ENGRAM) / "engramd_client.py", root / "engramd_client.py")
    # #3663: the runtime module the hook's flag read now imports lazily, at the plugin root.
    shutil.copy(Path(_SRC_ENGRAM) / "engramd_owner_flags.py", root / "engramd_owner_flags.py")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    with started_owner(home):
        r = subprocess.run([sys.executable, str(root / "hooks" / "engram-user-identity-hook.py")],
                           input=json.dumps({"prompt": "Alice: hello there"}),
                           capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, (r.returncode, r.stderr)
    ctx = additional_context(r.stdout)
    assert "ENGRAM User Identity: Alice" in ctx, ctx
    assert "unavailable" not in ctx.lower()
