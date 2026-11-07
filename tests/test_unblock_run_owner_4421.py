"""Contract cells for the owner-held unblock run (#4421, spec section C: state, TTL, loud bad state).

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire (three owner methods):

  write("cascade_unblock_start", {"session_key", "targets", "started_by"}, request_id)
      -> Ok(result={"run": {...}, "replaced": {"targets": [...]} | None})
  write("cascade_unblock_end",   {"session_key"}, request_id)
      -> Ok(result={"ended": {"targets": [...]} | None})
  read ("cascade_unblock_get",   {"session_key", "touch"})
      -> Ok(result={"run": {...} | None, "expired": {"targets", "expired_at"} | None})

The run lives in `$ENGRAM_HOME/unblock-runs.json`, written only by the owner, atomically, mode 0600. Shape:
{"version": 1, "runs": {"<session_key>": {"targets", "started_by", "started_at", "last_used_at", "expires_at"}}}.

Self-contained: the store is built with the real schema under tmp; nothing touches ~/.engram.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

from test_moment_cursor_3856 import make_store  # noqa: E402

START, END, GET = "cascade_unblock_start", "cascade_unblock_end", "cascade_unblock_get"


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="ubr-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home):
    o = eo.EngramdOwner(home)
    o.start(install_signal_handlers=False)
    return o


@pytest.fixture
def owner(home):
    make_store(home)
    o = _start(home)
    o._clock_now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    o._unblock_now = lambda: o._clock_now
    try:
        yield o
    finally:
        o.stop()


def start(owner, session, targets, *, rid=None, by="explicit"):
    return ec.write(START, {"session_key": session, "targets": targets, "started_by": by},
                    request_id=rid, socket_path=owner.socket_path)


def end(owner, session, *, rid=None):
    return ec.write(END, {"session_key": session}, request_id=rid, socket_path=owner.socket_path)


def get(owner, session, *, touch=True):
    return ec.read(GET, {"session_key": session, "touch": touch}, socket_path=owner.socket_path)


def ok(res):
    assert isinstance(res, ec.Ok), res
    return res.result


def runs_file(owner) -> Path:
    return owner.engram_home / "unblock-runs.json"


def disk(owner) -> dict:
    return json.loads(runs_file(owner).read_text())


def iso(dt) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------- registration


def test_methods_are_registered_with_the_declared_kinds(owner):
    assert set(owner._methods) >= {START, END, GET}
    assert owner._method_kinds[START] == "write"
    assert owner._method_kinds[END] == "write"
    assert owner._method_kinds[GET] == "read"
    assert {START, END} <= eo.WRITE_METHODS and GET not in eo.WRITE_METHODS
    assert {START, END} <= eo.WIRE_REQUEST_ID_METHODS  # the handler receives the envelope's request_id
    assert {m for m, k in owner._method_kinds.items() if k == "write"} == set(eo.WRITE_METHODS)


def test_ttl_is_one_named_constant_of_24_hours():
    assert eo.UNBLOCK_RUN_TTL_SECONDS == 24 * 3600


# ---------------------------------------------------------------------------- the file


def test_start_writes_the_file_with_the_stated_shape_and_mode_0600_even_under_umask_0(owner):
    old = os.umask(0)
    try:
        res = ok(start(owner, "sess-A", ["dv_0002", "dv_0001"]))
    finally:
        os.umask(old)
    assert stat.S_IMODE(runs_file(owner).stat().st_mode) == 0o600
    state = disk(owner)
    assert set(state) == {"version", "runs"} and state["version"] == 1
    run = state["runs"]["sess-A"]
    assert set(run) == {"targets", "started_by", "started_at", "last_used_at", "expires_at"}
    assert run["targets"] == ["dv_0001", "dv_0002"]          # sorted
    assert run["started_by"] == "explicit"
    now = owner._clock_now
    assert run["started_at"] == iso(now) and run["last_used_at"] == iso(now)
    assert run["expires_at"] == iso(now + timedelta(hours=24))
    assert res["run"]["targets"] == ["dv_0001", "dv_0002"] and res["replaced"] is None


def test_write_is_atomic_leaves_no_temp_file_and_every_rewrite_keeps_0600(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    ok(start(owner, "sess-A", ["dv_2"]))
    ok(end(owner, "sess-A"))
    leftovers = [p.name for p in owner.engram_home.iterdir() if "unblock" in p.name and p.name != "unblock-runs.json"]
    assert leftovers == []
    assert stat.S_IMODE(runs_file(owner).stat().st_mode) == 0o600


# ---------------------------------------------------------------------------- start / end semantics


def test_start_always_replaces_and_names_the_replaced_run(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    res = ok(start(owner, "sess-A", ["dv_2", "dv_3"], by="derive_refusal"))
    assert res["replaced"] == {"targets": ["dv_1"]}
    assert disk(owner)["runs"]["sess-A"]["targets"] == ["dv_2", "dv_3"]
    assert disk(owner)["runs"]["sess-A"]["started_by"] == "derive_refusal"


def test_end_names_the_run_and_a_second_end_reports_ended_null(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    assert ok(end(owner, "sess-A"))["ended"] == {"targets": ["dv_1"]}
    assert "sess-A" not in disk(owner)["runs"]
    assert ok(end(owner, "sess-A"))["ended"] is None


def test_a_replayed_start_with_the_same_request_id_returns_the_ORIGINAL_reply_not_a_recomputed_one(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    first = ok(start(owner, "sess-A", ["dv_2"], rid="rid-1"))
    assert first["replaced"] == {"targets": ["dv_1"]}
    replay = ok(start(owner, "sess-A", ["dv_2"], rid="rid-1"))
    # a recomputed reply would say it replaced dv_2 (the run the first attempt created)
    assert replay["replaced"] == {"targets": ["dv_1"]}
    assert replay["run"] == first["run"]
    assert replay.get("duplicate") is True


def test_a_replayed_end_returns_the_original_reply(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    first = ok(end(owner, "sess-A", rid="rid-e"))
    assert first["ended"] == {"targets": ["dv_1"]}
    replay = ok(end(owner, "sess-A", rid="rid-e"))
    assert replay["ended"] == {"targets": ["dv_1"]}
    assert ok(end(owner, "sess-A"))["ended"] is None      # but a NEW end finds nothing


def test_a_request_id_reused_for_a_different_payload_is_refused(owner):
    ok(start(owner, "sess-A", ["dv_1"], rid="rid-x"))
    res = start(owner, "sess-A", ["dv_9"], rid="rid-x")
    assert isinstance(res, ec.Rejected) and res.reason == "request_id_reused", res
    assert disk(owner)["runs"]["sess-A"]["targets"] == ["dv_1"]


def test_sessions_hold_independent_runs_in_one_file_and_ending_one_leaves_the_other(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    ok(start(owner, "sess-B", ["dv_2"]))
    assert set(disk(owner)["runs"]) == {"sess-A", "sess-B"}
    assert ok(get(owner, "sess-A"))["run"]["targets"] == ["dv_1"]
    assert ok(get(owner, "sess-B"))["run"]["targets"] == ["dv_2"]
    assert ok(get(owner, "sess-C"))["run"] is None
    ok(end(owner, "sess-A"))
    assert ok(get(owner, "sess-B"))["run"]["targets"] == ["dv_2"]
    assert ok(get(owner, "sess-A"))["run"] is None


# ---------------------------------------------------------------------------- TTL


def test_get_slides_the_expiry_and_a_non_touching_get_does_not(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=10)
    ok(get(owner, "sess-A", touch=False))
    assert disk(owner)["runs"]["sess-A"]["expires_at"] == iso(datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc))
    res = ok(get(owner, "sess-A", touch=True))
    want = owner._clock_now + timedelta(hours=24)
    assert res["run"]["expires_at"] == iso(want) and res["run"]["last_used_at"] == iso(owner._clock_now)
    assert disk(owner)["runs"]["sess-A"]["expires_at"] == iso(want)


def test_an_expired_run_is_reported_by_every_get_until_start_or_end_and_by_neither_after(owner):
    ok(start(owner, "sess-A", ["dv_2", "dv_1"]))
    expires = owner._clock_now + timedelta(hours=24)
    owner._clock_now += timedelta(hours=24, seconds=1)
    want = {"targets": ["dv_1", "dv_2"], "expired_at": iso(expires)}
    for _ in range(2):                                   # sticky: a get does not consume the notice
        res = ok(get(owner, "sess-A"))
        assert res["run"] is None and res["expired"] == want
    assert "sess-A" in disk(owner)["runs"]
    assert ok(end(owner, "sess-A"))["expired"] == want    # `end` removes it ...
    after = ok(get(owner, "sess-A"))
    assert after["run"] is None and after["expired"] is None   # ... and neither get nor anything reports it after


def test_a_start_over_an_expired_entry_removes_the_notice_for_later_reads(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=25)
    assert ok(get(owner, "sess-A"))["expired"] is not None
    ok(start(owner, "sess-A", ["dv_2"]))                  # `start` replaces the entry
    res = ok(get(owner, "sess-A"))
    assert res["run"]["targets"] == ["dv_2"] and res["expired"] is None


def test_two_consecutive_gets_after_expiry_both_carry_the_notice_a_lost_reply_cannot_silence_it(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=25)
    first, second = ok(get(owner, "sess-A")), ok(get(owner, "sess-A"))
    assert first["expired"] is not None and first == second
    ok(end(owner, "sess-A"))
    assert ok(get(owner, "sess-A"))["expired"] is None


def test_a_get_on_an_expired_entry_neither_slides_the_expiry_nor_rewrites_the_file(owner):
    import hashlib
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=25)
    digest = lambda: hashlib.sha256(runs_file(owner).read_bytes()).hexdigest()  # noqa: E731
    before = digest()
    mtime = runs_file(owner).stat().st_mtime_ns
    res = ok(get(owner, "sess-A", touch=True))
    assert res["run"] is None and digest() == before and runs_file(owner).stat().st_mtime_ns == mtime
    assert disk(owner)["runs"]["sess-A"]["expires_at"] == iso(datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc))


def test_a_lost_reply_on_start_or_end_over_an_expired_entry_is_answered_from_the_dedupe_store(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=25)
    first = ok(start(owner, "sess-A", ["dv_2"], rid="rid-s"))
    assert first["expired"]["targets"] == ["dv_1"] and first["replaced"] is None
    replay = ok(start(owner, "sess-A", ["dv_2"], rid="rid-s"))
    assert replay["expired"] == first["expired"] and replay.get("duplicate") is True
    ok(start(owner, "sess-B", ["dv_3"]))
    owner._clock_now += timedelta(hours=25)
    e1 = ok(end(owner, "sess-B", rid="rid-e"))
    assert e1["ended"] is None and e1["expired"]["targets"] == ["dv_3"]
    e2 = ok(end(owner, "sess-B", rid="rid-e"))
    assert e2["expired"] == e1["expired"] and e2.get("duplicate") is True


def test_a_run_used_just_inside_the_window_is_alive(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=23, minutes=59)
    assert ok(get(owner, "sess-A"))["run"] is not None


# ---------------------------------------------------------------------------- pruning of abandoned sessions


def test_the_retention_is_one_named_constant_of_7_days_next_to_the_ttl():
    assert eo.UNBLOCK_RUN_EXPIRED_RETENTION_SECONDS == 7 * 24 * 3600


def _abandon_then_other_session_starts(owner, days_after_expiry):
    ok(start(owner, "sess-gone", ["dv_1"]))
    owner._clock_now += timedelta(hours=24) + timedelta(days=days_after_expiry)
    ok(start(owner, "sess-other", ["dv_2"]))


def test_an_entry_expired_8_days_ago_is_pruned_when_another_session_starts_a_run(owner):
    _abandon_then_other_session_starts(owner, 8)
    assert set(disk(owner)["runs"]) == {"sess-other"}


def test_an_entry_expired_2_days_ago_is_kept_and_still_yields_its_notice_on_every_read(owner):
    _abandon_then_other_session_starts(owner, 2)
    assert set(disk(owner)["runs"]) == {"sess-gone", "sess-other"}
    for _ in range(2):
        res = ok(get(owner, "sess-gone"))
        assert res["run"] is None and res["expired"]["targets"] == ["dv_1"]


def test_a_live_run_is_never_pruned(owner):
    ok(start(owner, "sess-live", ["dv_1"]))
    owner._clock_now += timedelta(hours=23)
    ok(start(owner, "sess-other", ["dv_2"]))
    assert "sess-live" in disk(owner)["runs"]


# ---------------------------------------------------------------------------- restart


def test_the_run_survives_an_owner_restart(home):
    make_store(home)
    o1 = _start(home)
    ok(start(o1, "sess-A", ["dv_1"]))
    o1.stop()
    o2 = _start(home)
    try:
        assert ok(get(o2, "sess-A"))["run"]["targets"] == ["dv_1"]
    finally:
        o2.stop()


# ---------------------------------------------------------------------------- loud on bad state


@pytest.mark.parametrize("bad", [
    "{not json",
    "[]",
    json.dumps({"version": 2, "runs": {}}),
    json.dumps({"version": 1, "runs": []}),
    json.dumps({"version": 1, "runs": {"s": {"targets": "dv_1"}}}),
    json.dumps({"version": 1, "runs": {"s": {"targets": ["dv_1"], "started_by": "explicit", "started_at": "yesterday",
                                             "last_used_at": "x", "expires_at": "y"}}}),
])
def test_a_malformed_file_is_loud_at_owner_start_and_on_every_run_call_and_never_read_as_no_run(home, bad, caplog):
    make_store(home)
    (home / "unblock-runs.json").write_text(bad)
    with caplog.at_level(logging.WARNING, logger="engramd_owner"):
        o = _start(home)
    try:
        start_msgs = [r.getMessage() for r in caplog.records if "unblock" in r.getMessage().lower()]
        assert start_msgs and str(home / "unblock-runs.json") in start_msgs[0]
        g = get(o, "sess-A")
        assert isinstance(g, ec.Rejected) and g.reason == "unblock_state_unreadable", g
        assert str(home / "unblock-runs.json") in g.detail
        s = start(o, "sess-A", ["dv_1"])
        assert isinstance(s, ec.Rejected) and s.reason == "unblock_state_unreadable", s
        e = end(o, "sess-A")
        assert isinstance(e, ec.Rejected) and e.reason == "unblock_state_unreadable", e
        assert (home / "unblock-runs.json").read_text() == bad      # the evidence is never overwritten
    finally:
        o.stop()


def test_a_missing_file_is_simply_no_run_and_no_start_warning(home, caplog):
    make_store(home)
    with caplog.at_level(logging.WARNING, logger="engramd_owner"):
        o = _start(home)
    try:
        assert not [r for r in caplog.records if "unblock" in r.getMessage().lower()]
        assert ok(get(o, "sess-A")) == {"run": None, "expired": None}
        assert not (home / "unblock-runs.json").exists()      # a read never creates the file
    finally:
        o.stop()


# ---------------------------------------------------------------------------- params


@pytest.mark.parametrize("params", [
    {"session_key": "s", "targets": ["dv_1"]},                                          # started_by missing
    {"session_key": "s", "targets": ["dv_1"], "started_by": "somebody"},                # not in the closed set
    {"session_key": "s", "targets": [], "started_by": "explicit"},                       # empty
    {"session_key": "s", "targets": "dv_1", "started_by": "explicit"},                   # not a list
    {"session_key": "s", "targets": [1], "started_by": "explicit"},                      # not strings
    {"session_key": "", "targets": ["dv_1"], "started_by": "explicit"},                  # empty session key
    {"session_id": "s", "targets": ["dv_1"], "started_by": "explicit"},                  # wrong key name
    {"session_key": "s", "targets": ["dv_1"], "started_by": "explicit", "extra": 1},     # unknown key
])
def test_start_params_are_validated_and_nothing_is_written_on_refusal(owner, params):
    res = ec.write(START, params, socket_path=owner.socket_path)
    assert isinstance(res, ec.Rejected), res
    assert not runs_file(owner).exists()


def test_get_and_end_params_are_validated(owner):
    for params in ({"session_key": "s"}, {"session_key": "s", "touch": "yes"}, {"session_key": 5, "touch": True},
                   {"session_key": "s", "touch": True, "x": 1}):
        assert isinstance(ec.read(GET, params, socket_path=owner.socket_path), ec.Rejected), params
    for params in ({}, {"session_key": ""}, {"session_key": "s", "x": 1}):
        assert isinstance(ec.write(END, params, socket_path=owner.socket_path), ec.Rejected), params


def test_end_on_an_expired_run_reports_ended_null_with_the_expiry_notice_once(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    expires = owner._clock_now + timedelta(hours=24)
    owner._clock_now += timedelta(hours=25)
    res = ok(end(owner, "sess-A"))
    assert res["ended"] is None and res["expired"] == {"targets": ["dv_1"], "expired_at": iso(expires)}
    assert "expired" not in ok(end(owner, "sess-A"))


def test_expiry_boundary_exactly_24h_is_not_yet_expired_and_24h_plus_one_microsecond_is(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    owner._clock_now += timedelta(hours=24)
    assert ok(get(owner, "sess-A", touch=False))["run"] is not None       # `now > expires_at`: equality is still alive
    owner._clock_now += timedelta(microseconds=1)
    res = ok(get(owner, "sess-A", touch=False))
    assert res["run"] is None and res["expired"]["targets"] == ["dv_1"]


def test_a_run_with_an_invalid_started_by_makes_the_store_unusable_loudly(home):
    make_store(home)
    good = {"targets": ["dv_1"], "started_by": "bogus", "started_at": "2026-11-07T12:00:00+00:00",
            "last_used_at": "2026-11-07T12:00:00+00:00", "expires_at": "2026-11-08T12:00:00+00:00"}
    (home / "unblock-runs.json").write_text(json.dumps({"version": 1, "runs": {"s": good}}))
    o = _start(home)
    try:
        res = get(o, "s")
        assert isinstance(res, ec.Rejected) and res.reason == "unblock_state_unreadable", res
        assert "started_by" in res.detail
    finally:
        o.stop()


def test_start_over_an_expired_entry_reports_replaced_null_and_the_expired_notice(owner):
    ok(start(owner, "sess-A", ["dv_1"]))
    expires = owner._clock_now + timedelta(hours=24)
    owner._clock_now += timedelta(hours=25)
    res = ok(start(owner, "sess-A", ["dv_2"]))
    assert res["replaced"] is None and res["expired"] == {"targets": ["dv_1"], "expired_at": iso(expires)}
    assert "expired" not in ok(start(owner, "sess-A", ["dv_3"]))        # a live run replaced: no notice
