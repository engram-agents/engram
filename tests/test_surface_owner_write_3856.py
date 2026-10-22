"""Contract cells for U13 phase 3 (#3856, part of #2652): the surface hook's person-moment cursor advance goes
through the engramd owner behind `engramd.surface_writes_via_owner` (default OFF), the LAST hook-side direct site.

Written BEFORE the implementation (contract-first; project CLAUDE.md). The contract:

  * the flag is read ONLY through `engramd_owner_flags.flag_on` over the module-level CONFIG_PATH, like
    `_owner_reads_enabled`; anything but the JSON boolean true is OFF; a helper that raises (any Exception or
    SystemExit) reads OFF and its CLASS NAME is recorded (the existing `surface-owner-flag` notice);
    KeyboardInterrupt is not caught. It is INDEPENDENT of `engramd.surface_reads_via_owner` (four combinations).
  * flag OFF: `_advance_person_moment_cursor` is the phase-1 direct path and the owner is never contacted.
  * flag ON: the advance goes ONLY to the owner (`write("advance_moment_cursor", {person_id, from_cursor})`, params
    exactly those two keys, no request_id); ZERO direct connects to the store; NEVER a fallback to a direct write on
    any non-Ok outcome. True only for Ok with applied true; False for stale / not_person / not_object (quiet),
    Unavailable (any reason), Rejected (any reason), a client import failure, any exception.
  * the FIRST owner failure is recorded loud through the existing `surface-owner` notice kind, rate-limited by the
    shared probe-notice stamp; `Rejected(unknown_operation)` names the contract skew and says the cursor was NOT
    advanced. ONE attempt, no retry; the call gets min(remaining hook budget, OWNER_WRITE_CLIENT_TIMEOUT).

Self-contained: stores are built under tmp; nothing touches ~/.engram.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engram_moment_cursor as emc  # noqa: E402

from test_surface_owner_read_3821 import (  # noqa: E402,F401 -- fixtures and helpers reused
    PROMPT,
    StoreOpens,
    daemon_result,
    home,
    load_hook,
    make_surface_store,
    owner_of,
    run_main,
)
from test_moment_cursor_3856 import direct_write, raw_meta, whole_row  # noqa: E402

WRITE_KEY = "surface_writes_via_owner"
READ_KEY = "surface_reads_via_owner"
PID = "pn_0001"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


# =========================================================================================
# helpers
# =========================================================================================

def set_flags(home: Path, reads=..., writes=..., raw=None) -> None:
    """reads / writes = ... : key absent. raw: verbatim file text."""
    cfg = home / "config.json"
    if raw is not None:
        cfg.write_text(raw)
        return
    doc = {}
    section = {}
    if reads is not ...:
        section[READ_KEY] = reads
    if writes is not ...:
        section[WRITE_KEY] = writes
    if section:
        doc["engramd"] = section
    cfg.write_text(json.dumps(doc))


def store_with_cursor(home: Path, cursor=3) -> Path:
    db = make_surface_store(home, restore="unset")
    conn = sqlite3.connect(str(db))
    meta = json.loads(conn.execute("SELECT metadata FROM nodes WHERE id=?", (PID,)).fetchone()[0])
    meta["special_moment_cursor"] = cursor
    meta["special_moments"] = [{"description": "moment alpha"}, {"description": "moment beta"}]
    conn.execute("UPDATE nodes SET metadata=? WHERE id=?", (json.dumps(meta), PID))
    conn.commit()
    conn.close()
    return db


def hook(home, monkeypatch, writes=True, reads=False):
    mod = load_hook(home, monkeypatch, person=True)
    set_flags(home, reads=reads, writes=writes)
    mod._person_moment_in_cooldown = lambda *a, **k: False
    return mod


def hook_opens(so):
    """The store opens made BY THE HOOK (its advance function, or anything under main): the test's own helpers
    (`whole_row`, the owner fixture's start) run on the same thread and are not the hook's."""
    helpers = {"direct_write", "whole_row", "raw_meta", "cursor_of", "put"}      # the test's own opens
    return [o for o in so.opens
            if o["frames"][0] not in helpers
            and {"_advance_person_moment_cursor", "_advance_via_owner", "_main_impl"} & set(o["frames"])]


def cursor_of(db, nid=PID):
    return json.loads(raw_meta(db, nid)).get("special_moment_cursor")


class ScriptedOwner:
    """A socket server speaking the owner's wire for ONE scripted behaviour: `answer(request) -> dict` is sent
    newline-framed; `None` accepts and never replies; records every request."""

    def __init__(self, home: Path, answer):
        self.socket_path = Path(home) / "run" / "engramd.sock"
        self.answer = answer
        self.requests = []
        self._srv = None
        self._stop = threading.Event()
        self._held = []

    def __enter__(self):
        self.socket_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.socket_path))
        self._srv.listen(8)
        self._srv.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        for c in self._held:
            c.close()
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
            conn.settimeout(3)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.decode().strip())
            self.requests.append(request)
            reply = self.answer(request)
            if reply is None:
                self._held.append(conn)       # accept, never reply
                return
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        else:
            if reply is not None:
                conn.close()


def ok(applied=True, cursor=4, reason=None):
    return lambda req: {"status": "ok", "result": {"applied": applied, "cursor": cursor, "reason": reason}}


def rejected(reason, **extra):
    return lambda req: {"status": "rejected", "reason": reason, "detail": "d", "remedy": "r", **extra}


class CountingClient:
    """Counts every attempt to import/obtain the owner client and every `write`."""

    def __init__(self, mod, monkeypatch):
        self.imports = 0
        real = mod._import_engramd_client

        def counting():
            self.imports += 1
            return real()
        monkeypatch.setattr(mod, "_import_engramd_client", counting)


def advance_in_run(mod, home, frm=3, writes_on=True, reads_on=False):
    mod._OWNER_RUN = mod._new_owner_run()
    assert mod._OWNER_RUN["writes_on"] is writes_on
    return mod._advance_person_moment_cursor(PID, frm)


# =========================================================================================
# the flag: independent, default OFF, only boolean true, read only through flag_on
# =========================================================================================

@pytest.mark.parametrize("reads", [False, True])
@pytest.mark.parametrize("writes", [False, True])
def test_flag_matrix_the_write_goes_via_the_owner_only_when_the_writes_flag_is_on(reads, writes, home, monkeypatch):
    """All four combinations. Mutation: keying the write on the READS flag -> the (reads, not writes) and
    (not reads, writes) combinations go red."""
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch, writes=writes, reads=reads)
    with ScriptedOwner(home, ok(True, 4)) as owner:
        got = advance_in_run(mod, home, 3, writes_on=writes)
    assert mod._OWNER_RUN["on"] is reads and mod._OWNER_RUN["writes_on"] is writes
    if writes:
        assert len(owner.requests) == 1 and owner.requests[0]["method"] == "advance_moment_cursor"
        assert got is True and cursor_of(db) == 3          # the scripted owner wrote nothing; the hook wrote nothing
    else:
        assert owner.requests == []                         # the owner is never contacted
        assert got is True and cursor_of(db) == 4           # the direct path advanced it


def test_the_writes_flag_defaults_off_and_only_the_json_boolean_true_reads_on(home, monkeypatch):
    store_with_cursor(home)
    mod = load_hook(home, monkeypatch, person=True)
    set_flags(home, raw="")                                  # empty file
    assert mod._owner_writes_enabled() is False
    set_flags(home, raw=json.dumps({"engramd": {}}))
    assert mod._owner_writes_enabled() is False
    (home / "config.json").unlink()
    assert mod._owner_writes_enabled() is False
    for value in ("true", 1, "yes", [True], {"a": 1}, 0, None, False):
        set_flags(home, writes=value)
        assert mod._owner_writes_enabled() is False, value
    set_flags(home, writes=True)
    assert mod._owner_writes_enabled() is True


def test_the_writes_flag_is_read_only_through_flag_on_with_the_module_config_path(home, monkeypatch):
    mod = load_hook(home, monkeypatch, person=True)
    seen = []
    stub = type("F", (), {"flag_on": staticmethod(lambda key, path: seen.append((key, path)) or True)})
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: stub)
    monkeypatch.setattr(mod, "CONFIG_PATH", "/x/y/config.json")
    assert mod._owner_writes_enabled() is True
    assert seen == [(WRITE_KEY, "/x/y/config.json")]


@pytest.mark.parametrize("exc", [SystemExit(3), OSError("boom"), ImportError("nope"), RuntimeError("x")])
def test_a_flag_helper_that_raises_reads_off_records_the_class_name_and_the_direct_path_is_used(exc, home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch, writes=True)
    counting = CountingClient(mod, monkeypatch)

    def bad():
        raise exc
    monkeypatch.setattr(mod, "_import_owner_flags", bad)
    run = mod._new_owner_run()
    assert run["writes_on"] is False and run["on"] is False
    assert mod._OWNER_FLAGS_FAILURE == type(exc).__name__
    mod._OWNER_RUN = run
    assert mod._advance_person_moment_cursor(PID, 3) is True and cursor_of(db) == 4   # direct
    assert counting.imports == 0                                                      # the owner client untouched
    assert type(exc).__name__ in mod._owner_flags_failure_line()


def test_only_the_writes_helper_failing_keeps_the_reads_flag_and_records_the_class(home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch, writes=True, reads=True)

    def flag_on(key, path):
        if key == WRITE_KEY:
            raise OSError("writes only")
        return True
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: type("F", (), {"flag_on": staticmethod(flag_on)}))
    run = mod._new_owner_run()
    assert run["on"] is True and run["writes_on"] is False
    assert mod._OWNER_FLAGS_FAILURE == "OSError"


def test_a_keyboard_interrupt_in_the_flag_helper_is_not_caught(home, monkeypatch):
    mod = load_hook(home, monkeypatch, person=True)

    def boom():
        raise KeyboardInterrupt()
    monkeypatch.setattr(mod, "_import_owner_flags", boom)
    with pytest.raises(KeyboardInterrupt):
        mod._owner_writes_enabled()


# =========================================================================================
# flag OFF: today's direct path, the owner never contacted
# =========================================================================================

def test_flag_off_is_the_direct_path_with_zero_client_calls_and_the_sibling_control_sees_the_open(home, monkeypatch):
    """Also the SIBLING CONTROL: the connect counter sees this path's one RW open (so the zero-open cells below
    cannot be vacuous)."""
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch, writes=False)
    counting = CountingClient(mod, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    assert advance_in_run(mod, home, 3, writes_on=False) is True
    assert cursor_of(db) == 4
    assert counting.imports == 0
    ho = hook_opens(so)
    assert len(ho) == 1 and ho[0]["rw"] is True and "_advance_person_moment_cursor" in ho[0]["frames"]


def test_standalone_callers_outside_main_stay_on_the_direct_path(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch, writes=True)
    assert mod._OWNER_RUN is None
    counting = CountingClient(mod, monkeypatch)
    assert mod._advance_person_moment_cursor(PID, 3) is True and cursor_of(db) == 4
    assert counting.imports == 0


# =========================================================================================
# flag ON: only the owner; never a direct write
# =========================================================================================

def test_flag_on_sends_exactly_person_id_and_from_cursor_and_returns_true_on_applied(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    with ScriptedOwner(home, ok(True, 4)) as owner:
        assert advance_in_run(mod, home, 3) is True
    assert [r["params"] for r in owner.requests] == [{"person_id": PID, "from_cursor": 3}]
    assert owner.requests[0]["method"] == "advance_moment_cursor"
    assert "request_id" not in owner.requests[0]["params"]
    assert hook_opens(so) == [] and so.others == []                # zero direct connects
    assert mod._OWNER_RUN["write_failure"] is None


@pytest.mark.parametrize("reason", ["stale", "not_person", "not_object"])
def test_a_typed_not_applied_answer_is_quiet_false_with_no_failure_and_no_notice(reason, home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    with ScriptedOwner(home, ok(False, 4 if reason == "stale" else None, reason)):
        assert advance_in_run(mod, home, 3) is False
    assert mod._OWNER_RUN["write_failure"] is None
    assert mod._pending_owner_notice() == ("", None)


OUTCOMES = [
    ("busy", rejected("busy"), "rejected", "busy"),
    ("write_denied", rejected("write_denied"), "rejected", "write_denied"),
    ("readback_mismatch", rejected("readback_mismatch", commit="committed_unverified"), "rejected", "readback_mismatch"),
    ("unknown_operation", rejected("unknown_operation"), "rejected", "unknown_operation"),
    ("substrate_displaced", rejected("substrate_displaced"), "rejected", "substrate_displaced"),
    ("contract_version", rejected("contract_version"), "rejected", "contract_version"),
]


@pytest.mark.parametrize("label,answer,kind,reason", OUTCOMES, ids=[o[0] for o in OUTCOMES])
def test_every_rejected_outcome_is_false_with_zero_direct_connects_no_fallback_and_one_recorded_failure(
        label, answer, kind, reason, home, monkeypatch):
    """MUTANT: a direct-write fallback after an owner failure -> the zero-connect and DB-unchanged assertions go red."""
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    with ScriptedOwner(home, answer) as owner:
        assert advance_in_run(mod, home, 3) is False
    assert len(owner.requests) == 1                          # ONE attempt, no retry
    assert hook_opens(so) == [] and so.others == []
    assert whole_row(db, PID) == before
    f = mod._OWNER_RUN["write_failure"]
    assert f["kind"] == kind and getattr(f["result"], "reason", None) == reason


def test_owner_down_no_socket_is_false_zero_connects_db_unchanged_and_recorded(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    assert advance_in_run(mod, home, 3) is False
    assert hook_opens(so) == [] and so.others == []
    assert whole_row(db, PID) == before
    f = mod._OWNER_RUN["write_failure"]
    assert f["kind"] == "unavailable"


def test_a_client_import_failure_is_false_recorded_and_never_a_direct_write(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)

    def bad():
        raise ModuleNotFoundError("engramd_client")
    monkeypatch.setattr(mod, "_import_engramd_client", bad)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    assert advance_in_run(mod, home, 3) is False
    assert hook_opens(so) == [] and whole_row(db, PID) == before
    f = mod._OWNER_RUN["write_failure"]
    assert f["kind"] == "client_import" and f["cause"] == "ModuleNotFoundError"


def test_a_client_that_raises_is_false_recorded_never_a_direct_write(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    ecm = mod._import_engramd_client()

    def raising(*a, **k):
        raise RuntimeError("client bug")
    monkeypatch.setattr(ecm, "write", raising)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    assert advance_in_run(mod, home, 3) is False
    assert hook_opens(so) == [] and whole_row(db, PID) == before
    assert mod._OWNER_RUN["write_failure"]["kind"] == "client_failure"


def test_an_unusable_ok_answer_is_false_and_not_a_direct_write(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    with ScriptedOwner(home, lambda req: {"status": "ok", "result": "garbage"}):
        assert advance_in_run(mod, home, 3) is False
    assert hook_opens(so) == [] and cursor_of(db) == 3


# --- the timeout: one attempt, bounded -------------------------------------------------------------------


def test_a_silent_owner_returns_within_the_computed_budget_with_one_attempt(home, monkeypatch):
    """An owner that accepts and never replies: the call returns by the write timeout (here shortened), makes ONE
    attempt, and is recorded as Unavailable(timeout). Mutation: a retry loop -> two requests and a longer wait."""
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 0.6)
    so = StoreOpens(db).install(monkeypatch)
    with ScriptedOwner(home, lambda req: None) as owner:
        t0 = time.monotonic()
        got = advance_in_run(mod, home, 3)
        elapsed = time.monotonic() - t0
        time.sleep(0.3)
    assert got is False
    assert 0.5 <= elapsed < 1.2, elapsed
    assert len(owner.requests) == 1
    assert hook_opens(so) == [] and cursor_of(db) == 3
    f = mod._OWNER_RUN["write_failure"]
    assert f["kind"] == "unavailable" and f["result"].reason == "timeout"


def test_the_real_client_timeout_bounds_a_silent_owner_at_two_seconds(home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    assert ec.OWNER_WRITE_CLIENT_TIMEOUT == 2.0
    with ScriptedOwner(home, lambda req: None):
        t0 = time.monotonic()
        assert advance_in_run(mod, home, 3) is False
        elapsed = time.monotonic() - t0
    assert 1.9 <= elapsed < 2.7, elapsed


@pytest.mark.parametrize("elapsed_so_far,expected", [
    (0.0, 2.0),            # min(10 - 1.5 - 0, 2.0)
    (6.5, 2.0),            # remaining 2.0 exactly
    (7.0, 1.5),
    (8.0, 0.5),
    (8.3, None),           # remaining 0.2 < the 0.25 s floor: no owner call
    (9.9, None),
])
def test_the_write_timeout_is_min_of_the_remaining_hook_budget_and_the_client_constant(elapsed_so_far, expected, home, monkeypatch):
    mod = load_hook(home, monkeypatch, person=True)
    assert mod.HOOK_TIMEOUT_S == 10.0 and mod.OWNER_WRITE_TAIL_RESERVE_S == 1.5 and mod.OWNER_WRITE_MIN_TIMEOUT_S == 0.25
    monkeypatch.setattr(mod, "_HOOK_STARTED", time.monotonic() - elapsed_so_far)
    got = mod._owner_write_timeout(ec)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=0.05)


def test_the_hook_timeout_constant_equals_the_registered_hooks_json_timeout(home, monkeypatch):
    mod = load_hook(home, monkeypatch, person=True)
    doc = json.loads((REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json").read_text())
    timeouts = [h["timeout"] for ev in doc["hooks"].values() for grp in ev for h in grp["hooks"]
                if "engram-surface-hook.py" in h.get("command", "")]
    assert timeouts == [mod.HOOK_TIMEOUT_S]


def test_no_budget_left_means_no_owner_call_a_recorded_failure_and_no_direct_write(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_HOOK_STARTED", time.monotonic() - 9.5)
    so = StoreOpens(db).install(monkeypatch)
    with ScriptedOwner(home, ok(True, 4)) as owner:
        assert advance_in_run(mod, home, 3) is False
    assert owner.requests == [] and hook_opens(so) == []
    assert mod._OWNER_RUN["write_failure"]["kind"] == "budget_exhausted"


def test_after_one_owner_failure_the_rest_of_the_fire_makes_no_further_owner_writes(home, monkeypatch):
    """Up to three people can render a moment in one fire: one slow/failed write must not be followed by two more
    full-timeout waits inside a 10 s hook."""
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    with ScriptedOwner(home, rejected("busy")) as owner:
        mod._OWNER_RUN = mod._new_owner_run()
        assert mod._advance_person_moment_cursor(PID, 3) is False
        assert mod._advance_person_moment_cursor("pn_other", 5) is False
    assert len(owner.requests) == 1


# --- the loud notice -------------------------------------------------------------------------------------


def _fire(mod):
    return run_main(mod, PROMPT)


def test_owner_down_full_fire_renders_the_moment_adds_one_loud_notice_and_the_second_fire_adds_none(home, monkeypatch):
    db = store_with_cursor(home)
    mod = hook(home, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    r1 = _fire(mod)
    assert r1.exc == "SystemExit(0)" and "moment" in r1.ctx          # the moment still renders
    assert "could not advance" in r1.ctx and "engramd.surface_writes_via_owner" in r1.ctx
    assert not [o for o in hook_opens(so) if o["rw"]]                       # no direct WRITE connect
    assert whole_row(db, PID) == before
    r2 = _fire(mod)
    assert "could not advance" not in r2.ctx                          # rate-limited by the shared stamp
    assert "owner write failed" in r2.stderr                          # but stderr names it every run
    assert not [o for o in hook_opens(so) if o["rw"]]


def test_the_notice_uses_the_existing_surface_owner_kind_and_the_shared_stamp(home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    mod._OWNER_RUN = mod._new_owner_run()
    assert mod._advance_person_moment_cursor(PID, 3) is False
    text, key = mod._pending_owner_notice()
    assert key == mod.OWNER_NOTICE_KEY == "surface-owner" and text
    assert len(text) <= mod._OWNER_NOTICE_CAP


def test_unknown_operation_names_the_contract_skew_and_says_the_cursor_was_not_advanced(home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    with ScriptedOwner(home, rejected("unknown_operation")):
        assert advance_in_run(mod, home, 3) is False
    text, key = mod._pending_owner_notice()
    assert key == "surface-owner"
    assert "contract skew" in text and "NOT advanced" in text and "restart or upgrade the owner" in text
    assert "advance_moment_cursor" in text


def test_the_notice_wording_is_truthful_per_outcome(home, monkeypatch):
    store_with_cursor(home)
    cases = [
        (rejected("busy"), ["did NOT advance"], ["may have advanced"]),
        (rejected("readback_mismatch", commit="committed_unverified"), ["may have advanced"], ["did NOT advance"]),
        (lambda req: None, ["may or may not have advanced"], ["did NOT advance"]),
    ]
    for answer, present, absent in cases:
        mod = hook(home, monkeypatch)
        monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 0.3)
        with ScriptedOwner(home, answer):
            advance_in_run(mod, home, 3)
        text, _ = mod._pending_owner_notice()
        for p in present:
            assert p in text, (p, text)
        for a in absent:
            assert a not in text, (a, text)


def test_a_read_failure_and_a_write_failure_in_one_fire_report_the_read_failure_first(home, monkeypatch):
    store_with_cursor(home)
    mod = hook(home, monkeypatch)
    mod._OWNER_RUN = mod._new_owner_run()
    mod._OWNER_RUN["failure"] = {"site": "_check_embedding_restore_flag", "kind": "unavailable", "result": None, "cause": None}
    mod._OWNER_RUN["degraded"] = ["_check_embedding_restore_flag"]
    mod._advance_person_moment_cursor(PID, 3)
    text, _ = mod._pending_owner_notice()
    assert "owner read failed" in text


# =========================================================================================
# end to end with the REAL owner thread
# =========================================================================================

@requires_linux_owner
def test_end_to_end_the_real_owner_advances_the_cursor_once_and_a_concurrent_field_survives(home, monkeypatch):
    """A person moment renders; the cursor advances via the owner exactly once, from the value rendered at
    index from % len; `pronouns` written between the hook's read and the owner call survives; the wire params are
    exactly {person_id, from_cursor}; the hook thread makes no RW connect."""
    db = store_with_cursor(home, cursor=3)
    mod = hook(home, monkeypatch, writes=True, reads=False)
    result = daemon_result(person=True)
    result["special_nodes"][0]["special_moment_cursor"] = 3
    import copy
    mod.query_daemon = lambda *a, **k: copy.deepcopy(result)
    wire = []
    real_write = ec.write

    def spy(method, params, **kw):
        wire.append((method, dict(params), set(kw)))
        direct_write(db, PID, "pronouns", "they/them")             # a concurrent editor, just before the owner call
        return real_write(method, params, **kw)
    monkeypatch.setattr(ec, "write", spy)
    so = StoreOpens(db).install(monkeypatch)
    with owner_of("real", home) as owner:
        calls = []
        real_handler = owner._handle_advance_moment_cursor
        owner._methods["advance_moment_cursor"] = lambda p, d: calls.append(dict(p)) or real_handler(p, d)
        run = _fire(mod)
    assert run.exc == "SystemExit(0)"
    assert "moment beta" in run.ctx and "moment alpha" not in run.ctx          # 3 % 2 == 1
    assert [(m, p) for m, p, _ in wire] == [("advance_moment_cursor", {"person_id": PID, "from_cursor": 3})]
    assert calls == [{"person_id": PID, "from_cursor": 3}]                       # exactly once at the owner
    meta = json.loads(raw_meta(db, PID))
    assert meta["special_moment_cursor"] == 4 and meta["pronouns"] == "they/them"
    assert not [o for o in hook_opens(so) if o["rw"]]
    assert "could not advance" not in run.ctx


# =========================================================================================
# the hook header and the ledger say what is true
# =========================================================================================

def test_the_hook_header_no_longer_says_row_8_stays_direct_and_lists_the_write_site():
    src = (REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py").read_text()
    head = "\n".join(src.splitlines()[:260])
    assert "STAYS DIRECT on both flag values" not in head
    assert "engramd.surface_writes_via_owner" in head
    assert "advance_moment_cursor" in head
    assert "_advance_person_moment_cursor" in head


def _ledger() -> str:
    return (REPO_ROOT / "docs" / "2652-migration-ledger.md").read_text()


def test_ledger_row_37_is_behind_flag_and_names_the_writes_flag():
    row = next(ln for ln in _ledger().splitlines() if ln.startswith("| 37 |"))
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert "behind-flag" in cells
    assert "engramd.surface_writes_via_owner" in row and "advance_moment_cursor" in row
    assert "stays direct on BOTH values" not in row


def test_ledger_section_3_registered_write_sites_list_the_cursor_advance_and_the_surviving_direct_callers():
    line = next(ln for ln in _ledger().splitlines() if ln.startswith("**Write sites of the person-moment cursor"))
    assert "engramd_owner.py:_advance_moment_cursor_execute" in line
    assert "engram-surface-hook.py:_advance_person_moment_cursor" in line
    assert "engram_query.py:_select_and_render_person_moment" in line


def test_ledger_section_6_states_the_second_write_its_authorizer_and_the_stated_limits():
    text = _ledger()
    sec = text[text.index("## 6. Owner writes"):]
    for needle in (
        "advance_moment_cursor", "WRITE_METHODS",
        "{use_reward, advance_moment_cursor, fill_embeddings, finish_embedding_regen}",
        "nodes_metadata_json_guard_update", "json_valid", "WHEN clause",
        "No `request_id`", "compare-and-set", "OWNER_WRITE_LOCK_WAIT", "OWNER_WRITE_CLIENT_TIMEOUT",
        "1.3", "readback_mismatch", "between the COMMIT and the read-back",
        "allow-set", "disjointness", "engramd.surface_writes_via_owner",
    ):
        assert needle in sec, needle


def test_ledger_bounds_the_parity_claim_names_the_three_corrupt_classes_and_the_notice_priority():
    text = _ledger()
    sec = text[text.index("### The second write: `advance_moment_cursor`"):]
    for needle in ("every value `json.dumps` can emit", "duplicate keys", "escaped key spelling", "NaN",
                   "fails in the safe direction", "only the read failure reaches", "self-guarded",
                   "surface-moment-cursor", "moment cursor module unavailable"):
        assert needle in sec, needle
    row = next(ln for ln in text.splitlines() if ln.startswith("| 37 |"))
    assert "no latency claim is made" not in row and "latency reported, not gated" in row


def test_the_module_docstring_and_the_hook_notice_docstring_carry_the_same_bounds():
    import engram_moment_cursor
    doc = engram_moment_cursor.__doc__
    for needle in ("EVERY VALUE", "DUPLICATE", "ESCAPED key spelling", "NaN", "SAFE direction"):
        assert needle in doc, needle
    src = (REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py").read_text()
    assert "only the read failure reaches" in src


# --- the silent stuck rotation: the shared module failing to import in the hook is LOUD ------------------------

def _no_cursor_module(mod, monkeypatch):
    monkeypatch.setattr(mod, "_advance_moment_cursor", None)
    monkeypatch.setattr(mod, "_read_moment_cursor", None)


def test_an_unimportable_cursor_module_is_loud_once_per_window_with_a_stderr_line_every_fire(home, monkeypatch):
    """Without the module the cursor reads 0 and the advance returns False: the SAME first moment renders forever.
    That must not be silent: one stderr line on every fire that selects a person moment, and ONE notice per window
    under a NEW kind through the shared probe-notice stamp. Best effort otherwise: nothing raises, no direct write.
    Mutation: drop the notice -> red."""
    db = store_with_cursor(home, cursor=3)
    mod = hook(home, monkeypatch, writes=False)
    _no_cursor_module(mod, monkeypatch)
    so = StoreOpens(db).install(monkeypatch)
    before = whole_row(db, PID)
    r1 = _fire(mod)
    assert r1.exc == "SystemExit(0)" and "moment alpha" in r1.ctx                 # cursor read as 0: the first moment
    assert "moment cursor module unavailable" in r1.ctx and "will not advance" in r1.ctx
    assert "moment cursor module unavailable" in r1.stderr
    r2 = _fire(mod)
    assert "moment cursor module unavailable" not in r2.ctx                       # rate-limited by the shared stamp
    assert "moment cursor module unavailable" in r2.stderr                        # but stderr names it every fire
    assert not [o for o in hook_opens(so) if o["rw"]]
    assert whole_row(db, PID) == before


def test_the_unavailable_notice_uses_its_own_kind_and_the_shared_window(home, monkeypatch):
    store_with_cursor(home, cursor=3)
    mod = hook(home, monkeypatch, writes=False)
    _no_cursor_module(mod, monkeypatch)
    assert mod._pending_owner_notice() == ("", None)                                # nothing selected yet: quiet
    assert mod._select_person_moment_candidate(
        {"id": PID, "special_moments": [{"description": "m"}], "special_moment_cursor": 3}) is not None
    text, key = mod._pending_owner_notice()
    assert key == mod.MOMENT_CURSOR_NOTICE_KEY and key not in (mod.OWNER_NOTICE_KEY, mod.OWNER_FLAG_NOTICE_KEY)
    assert text and len(text) <= mod._OWNER_NOTICE_CAP


def test_a_working_cursor_module_adds_no_unavailable_notice(home, monkeypatch):
    store_with_cursor(home, cursor=3)
    mod = hook(home, monkeypatch, writes=False)
    r = _fire(mod)
    assert "moment cursor module unavailable" not in r.ctx + r.stderr
