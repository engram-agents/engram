"""Contract tests for the owner's per-day read-lock wait counters and their
report tool (#4234, part of #3740 / #2652).

Written BEFORE the implementation (contract-first). The contract is the issue
body (as amended by the design seat) plus its rulings comment:

  * wait = the time spent in `_client_read_lock.acquire(...)` itself, measured
    in `_run_client_read` for all four methods that take the lock;
  * counted per family: `read` -> the template's site-file family, typed
    methods -> the method name;
  * `wait_ms_sum` and the histogram `wait_hist` cover WAITED reads only
    (wait_ms > 1.0); an uncontended read leaves every bucket unchanged;
  * a timed-out acquire is a read + a waited read + `timed_out`; a request
    with no time left BEFORE the acquire is ONLY `no_time_before_acquire`;
  * a per-UTC-day file `lockwait-YYYY-MM-DD.json` in the run dir, written by a
    background thread (never the request thread) and on stop(), merged by
    addition across an owner restart, O(families x buckets).

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as eo  # noqa: E402

from test_engramd_owner_read_3197 import make_real_store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = json.loads((Path(_SRC_ENGRAM) / "engramd_read_templates.json").read_text())["templates"]
EDGE_KEYS = ["1", "2", "5", "10", "20", "50", "100", "200", "500", "1000", "2000", "inf"]
TRIPWIRE_FILE = "hooks/claude/engram-lesson-tripwire-hook.py"


def _template_with_sites(predicate):
    found = [t for t in TEMPLATES if predicate(t["sites"])]
    assert found
    return found[0]


def _files(sites):
    return {s.rsplit(":", 1)[0] for s in sites}


TRIPWIRE_SQL = _template_with_sites(lambda sites: _files(sites) == {TRIPWIRE_FILE})["sql"]
IDENTITY_SQL = _template_with_sites(lambda sites: _files(sites) == {"hooks/claude/engram-user-identity-hook.py"})["sql"]


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="lw4234-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def owner(home):
    make_real_store(home)
    o = eo.EngramdOwner(home, request_deadline=2.0)
    o.start(install_signal_handlers=False)
    try:
        yield o
    finally:
        o.stop()


def _read(o, sql=TRIPWIRE_SQL, deadline=None):
    return o._handle_read({"sql": sql, "args": []}, deadline if deadline is not None else time.monotonic() + 2.0)


def _run_dir(o) -> Path:
    return o.socket_path.parent


def _flush_and_load(o) -> dict:
    """Force a flush (the same function the background thread calls) and parse today's file."""
    o._lockwait.flush()
    files = sorted(_run_dir(o).glob("lockwait-*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def _fam(doc: dict, name: str) -> dict:
    return doc["families"][name]


def _count_keys(obj) -> int:
    if isinstance(obj, dict):
        return len(obj) + sum(_count_keys(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_keys(v) for v in obj)
    return 0


def _hold_lock(o, seconds: float) -> threading.Thread:
    """A second thread holds `_client_read_lock` for `seconds`; returns once it holds it."""
    held = threading.Event()

    def hold():
        with o._client_read_lock:
            held.set()
            time.sleep(seconds)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(2.0)
    return t


# --- the family function (pure) ----------------------------------------------

def test_family_of_a_tripwire_only_template_is_tripwire():
    """Mutation: any change to the fixed file->family table, or taking the
    directory instead of the basename -> red."""
    assert eo.lockwait_family_for_sites([f"{TRIPWIRE_FILE}:check_in_turn_recall"]) == "tripwire"
    assert eo.lockwait_family_for_sites([f"{TRIPWIRE_FILE}:a", f"{TRIPWIRE_FILE}:b"]) == "tripwire"


def test_family_of_a_template_shared_by_two_files_is_the_sorted_join():
    """Mutation (b3): a shared template takes its FIRST site's family instead of
    the join -> red. Order of `sites` must not matter."""
    a = ["hooks/claude/engram-session-start-hook.py:f", "hooks/claude/engram-surface-hook.py:g"]
    assert eo.lockwait_family_for_sites(a) == "session_start+surface"
    assert eo.lockwait_family_for_sites(list(reversed(a))) == "session_start+surface"
    assert eo.lockwait_family_for_sites(["engram_idf.py:x", f"{TRIPWIRE_FILE}:y"]) == "idf+tripwire"


def test_family_of_an_unlisted_file_is_its_stem_with_underscores():
    """Mutation: keep the hyphens / the extension -> red."""
    assert eo.lockwait_family_for_sites(["tools/dream-master-batch.py:f"]) == "dream_master_batch"
    assert eo.lockwait_family_for_sites(["engram_core.py:f"]) == "engram_core"


def test_family_table_is_a_named_constant_with_the_fixed_entries():
    """Mutation: drop or rename an entry -> red."""
    table = eo.LOCKWAIT_FAMILY_BY_FILE
    assert table["engram-lesson-tripwire-hook.py"] == "tripwire"
    assert table["engram-surface-hook.py"] == "surface"
    assert table["engram-session-start-hook.py"] == "session_start"
    assert table["engram_idf.py"] == "idf"


def test_family_for_sql_unknown_nonstr_and_no_registry_are_other():
    """Mutation: raise on a non-str / unencodable sql, or default to a template family -> red."""
    fmap = eo.lockwait_family_map(TEMPLATES)
    assert eo.lockwait_family_for_sql(fmap, TRIPWIRE_SQL) == "tripwire"
    assert eo.lockwait_family_for_sql(fmap, "SELECT 1") == "other"
    assert eo.lockwait_family_for_sql(fmap, 5) == "other"
    assert eo.lockwait_family_for_sql(fmap, None) == "other"
    assert eo.lockwait_family_for_sql(fmap, "\ud800") == "other"
    assert eo.lockwait_family_for_sql({}, TRIPWIRE_SQL) == "other"
    assert eo.lockwait_family_for_sql(None, TRIPWIRE_SQL) == "other"


def test_family_map_on_synthetic_templates():
    """Synthetic list: a tripwire-only and a session-start+surface template."""
    import engramd_read_registry as rr

    sql_a, sql_b = "SELECT 1 FROM nodes", "SELECT 2 FROM nodes"
    templates = [
        {"key": rr.template_key(sql_a), "sites": [f"{TRIPWIRE_FILE}:f"], "sql": sql_a},
        {"key": rr.template_key(sql_b),
         "sites": ["hooks/claude/engram-session-start-hook.py:f", "hooks/claude/engram-surface-hook.py:g"], "sql": sql_b},
    ]
    fmap = eo.lockwait_family_map(templates)
    assert eo.lockwait_family_for_sql(fmap, sql_a) == "tripwire"
    assert eo.lockwait_family_for_sql(fmap, sql_b) == "session_start+surface"
    assert eo.lockwait_family_for_sql(fmap, "SELECT 3") == "other"


def test_family_map_on_the_committed_file_joins_the_three_shared_templates():
    """Mutation (b3): first-site family for shared templates -> `5961b023`/`6fccf1c5`
    would be session_start or surface alone -> red. The key is the sha256 of the SQL."""
    fmap = eo.lockwait_family_map(TEMPLATES)
    by_prefix = {k[:8]: v for k, v in fmap.items()}
    assert by_prefix["5961b023"] == "session_start+surface"
    assert by_prefix["6fccf1c5"] == "session_start+surface"
    assert by_prefix["dc3d1b24"] == "idf+tripwire"
    assert "tripwire" in fmap.values()


# --- the histogram / p95 (pure) -----------------------------------------------

def test_bucket_edges_are_the_fixed_named_constant():
    """Mutation: change an edge or drop +inf -> red."""
    assert eo.LOCKWAIT_BUCKET_EDGES_MS == (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, math.inf)


@pytest.mark.parametrize("value,edge", [(1.0001, 2), (2.0, 2), (2.01, 5), (50.0, 50), (50.5, 100), (2000.0, 2000), (2000.1, math.inf), (1e9, math.inf)])
def test_a_waited_value_lands_in_the_first_bucket_whose_edge_is_at_least_the_value(value, edge):
    """Mutation: `<` instead of `<=` at the edge -> the edge-exact cases go red."""
    assert eo.LOCKWAIT_BUCKET_EDGES_MS[eo.lockwait_bucket_index(value)] == edge


def test_p95_is_nearest_rank_over_the_bucket_counts():
    """20 waited reads: ceil(0.95*20) = 19th. 18 in the 2-bucket, 1 in the 50-bucket,
    1 in the 200-bucket -> the 19th sits in the 50-bucket. Mutation: floor / round /
    max instead of nearest-rank -> 200 or 2 -> red."""
    counts = [0] * 12
    counts[1], counts[5], counts[7] = 18, 1, 1
    assert eo.lockwait_p95(counts) == 50
    only_inf = [0] * 11 + [3]
    assert eo.lockwait_p95(only_inf) == math.inf


def test_p95_is_null_with_no_waited_reads():
    """Mutation: return 0 / the first edge for an empty histogram -> red."""
    assert eo.lockwait_p95([0] * 12) is None


# --- measured at the acquire ---------------------------------------------------

@requires_linux_owner
def test_uncontended_read_counts_in_reads_only(owner):
    """Mutation (k2): an uncontended read is put in the histogram / sum / waited ->
    waited_reads, wait_ms_sum or a bucket is non-zero -> red."""
    _read(owner)
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["reads"] == 1
    assert fam["waited_reads"] == 0
    assert fam["wait_ms_sum"] == 0
    assert fam["timed_out"] == 0 and fam["no_time_before_acquire"] == 0
    assert fam["wait_hist"] == {k: 0 for k in EDGE_KEYS}
    assert fam["wait_ms_p95"] is None


@requires_linux_owner
def test_contended_read_is_a_waited_read_in_the_right_bucket(owner):
    """A second thread holds the lock ~50 ms. Mutation (m1): measure from accept
    time / not around the acquire -> the wait is missed or the bucket is wrong;
    drop the waited branch -> red."""
    holder = _hold_lock(owner, 0.05)
    _read(owner)
    holder.join()
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["reads"] == 1 and fam["waited_reads"] == 1
    assert fam["wait_ms_sum"] >= 30  # generous lower bound for a 50 ms hold
    populated = [k for k, v in fam["wait_hist"].items() if v]
    assert len(populated) == 1 and fam["wait_hist"][populated[0]] == 1
    edge = math.inf if populated[0] == "inf" else float(populated[0])
    lower = EDGE_KEYS[EDGE_KEYS.index(populated[0]) - 1]
    assert float(lower) < fam["wait_ms_sum"] <= edge
    assert fam["wait_ms_p95"] == ("inf" if populated[0] == "inf" else float(populated[0]))


@requires_linux_owner
def test_time_before_the_acquire_is_not_charged_to_the_lock(owner):
    """A client that connects, idles 150 ms, then sends its request: the time passes between
    accept and the acquire, and the lock is free. Mutation (m1): wait measured from accept
    time instead of around the acquire -> waited_reads == 1 -> red."""
    import socket as _socket
    request = {"contract_version": eo.ec.CONTRACT_VERSION, "method": "read",
               "params": {"sql": TRIPWIRE_SQL, "args": []}, "request_id": None}
    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        sock.connect(str(owner.socket_path))
        time.sleep(0.15)
        sock.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    assert json.loads(data.split(b"\n")[0])["status"] == "ok"
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["reads"] == 1 and fam["waited_reads"] == 0


@requires_linux_owner
def test_p95_over_waited_reads_only(owner):
    """50 uncontended reads + one contended: the histogram holds ONE entry and p95
    is the contended bucket, not the 1 ms edge. Mutation (k2): uncontended reads
    in the histogram -> p95 becomes 1 -> red."""
    for _ in range(50):
        _read(owner)
    holder = _hold_lock(owner, 0.05)
    _read(owner)
    holder.join()
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["reads"] == 51 and fam["waited_reads"] == 1
    assert sum(fam["wait_hist"].values()) == 1
    assert fam["wait_ms_p95"] >= 50


@requires_linux_owner
def test_timed_out_acquire_counts_in_reads_waited_histogram_and_timed_out(owner):
    """The lock is held past the request's execution deadline. Mutation: drop the
    timed-out branch (wait invisible) or count it only in timed_out -> red."""
    holder = _hold_lock(owner, 0.6)
    reply = _read(owner, deadline=time.monotonic() + eo.READ_RESPONSE_RESERVE + 0.1)
    holder.join()
    assert reply["status"] == "rejected"
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["reads"] == 1
    assert fam["waited_reads"] == 1
    assert fam["timed_out"] == 1
    assert fam["no_time_before_acquire"] == 0
    assert sum(fam["wait_hist"].values()) == 1
    assert fam["wait_ms_sum"] >= 50


@requires_linux_owner
def test_no_time_before_acquire_increments_only_that_counter(owner):
    """A request whose deadline has already passed never reaches the acquire.
    Mutation (b1): count it in `reads` (or waited / a bucket) -> red."""
    reply = _read(owner, deadline=time.monotonic() - 1.0)
    assert reply["status"] == "rejected"
    fam = _fam(_flush_and_load(owner), "tripwire")
    assert fam["no_time_before_acquire"] == 1
    assert fam["reads"] == 0 and fam["waited_reads"] == 0 and fam["timed_out"] == 0
    assert fam["wait_ms_sum"] == 0
    assert fam["wait_hist"] == {k: 0 for k in EDGE_KEYS}


@requires_linux_owner
def test_four_methods_land_in_four_families(owner):
    """Mutation: one family for every method, or the typed methods under `read`'s
    family -> red."""
    deadline = time.monotonic() + 2.0
    owner._handle_read({"sql": TRIPWIRE_SQL, "args": []}, deadline)
    owner._handle_df({"terms": ["alpha"]}, deadline)
    owner._handle_chain({"node_id": "ls_none", "max_depth": 2}, deadline)
    owner._handle_confidence_by_type({"modes": ["all"]}, deadline)
    doc = _flush_and_load(owner)
    assert set(doc["families"]) == {"tripwire", "df", "chain", "confidence_by_type"}
    assert all(f["reads"] == 1 for f in doc["families"].values())


@requires_linux_owner
def test_unregistered_sql_is_counted_under_other(owner):
    """Mutation: an unknown SQL raises in the family lookup or is dropped -> red."""
    owner._handle_read({"sql": "SELECT 1", "args": []}, time.monotonic() + 2.0)
    assert _fam(_flush_and_load(owner), "other")["reads"] == 1


# --- the file ------------------------------------------------------------------

@requires_linux_owner
def test_file_shape_name_and_mode(owner):
    """Top-level schema / date / bucket_edges_ms; name is the UTC date; mode 0600;
    p95 serialisation of an inf edge is the string "inf" (JSON has no Infinity).
    Mutation: other mode / name / keys -> red."""
    _read(owner)
    doc = _flush_and_load(owner)
    path = next(_run_dir(owner).glob("lockwait-*.json"))
    assert doc["schema"] == 1
    assert path.name == f"lockwait-{doc['date']}.json"
    assert doc["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert doc["bucket_edges_ms"] == [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, "inf"]
    assert set(_fam(doc, "tripwire")) == {"reads", "waited_reads", "wait_ms_sum", "timed_out", "no_time_before_acquire",
                                          "wait_hist", "wait_ms_p95"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(_run_dir(owner).glob("*.tmp*"))  # the temp file was replaced away


@requires_linux_owner
def test_restart_adds_to_the_same_days_counts(home):
    """Owner A counts and stops (stop() flushes); owner B starts on the same run dir and
    day and counts more: the file after B's flush is the SUM. Mutation (b2): B overwrites
    instead of adding -> reads == 1 -> red; stop() not flushing -> A's file is missing."""
    make_real_store(home)
    a = eo.EngramdOwner(home, request_deadline=2.0)
    a.start(install_signal_handlers=False)
    for _ in range(3):
        _read(a)
    holder = _hold_lock(a, 0.05)
    _read(a)
    holder.join()
    a.stop()  # flush on stop: no explicit flush call
    path = next((home / "run").glob("lockwait-*.json"))
    first = json.loads(path.read_text())
    assert _fam(first, "tripwire")["reads"] == 4 and _fam(first, "tripwire")["waited_reads"] == 1
    b = eo.EngramdOwner(home, request_deadline=2.0)
    b.start(install_signal_handlers=False)
    try:
        for _ in range(2):
            _read(b)
        doc = _flush_and_load(b)
    finally:
        b.stop()
    fam = _fam(doc, "tripwire")
    assert fam["reads"] == 6
    assert fam["waited_reads"] == 1
    assert sum(fam["wait_hist"].values()) == 1
    assert fam["wait_ms_sum"] == _fam(first, "tripwire")["wait_ms_sum"]
    assert fam["wait_ms_p95"] == _fam(first, "tripwire")["wait_ms_p95"]


@requires_linux_owner
@pytest.mark.parametrize("bad", [
    "not json {",
    json.dumps({"schema": 1, "date": "x", "bucket_edges_ms": [1, 2], "families": {}}),
    json.dumps({"schema": 2, "date": "x", "bucket_edges_ms": [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, "inf"], "families": {}}),
])
def test_corrupt_or_foreign_file_is_renamed_aside_and_counting_starts_at_zero(home, bad):
    """Mutation: the owner raises on a bad file, keeps merging it, or deletes it
    without a trace -> red. The owner keeps serving."""
    make_real_store(home)
    (home / "run").mkdir(mode=0o700, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = home / "run" / f"lockwait-{today}.json"
    target.write_text(bad)
    o = eo.EngramdOwner(home, request_deadline=2.0)
    o.start(install_signal_handlers=False)
    try:
        reply = _read(o)
        assert reply["status"] == "ok"
        doc = _flush_and_load(o)
    finally:
        o.stop()
    assert _fam(doc, "tripwire")["reads"] == 1
    aside = list((home / "run").glob(f"lockwait-{today}.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == bad


# --- off the reply path ---------------------------------------------------------

@requires_linux_owner
def test_reply_bytes_are_identical_with_and_without_the_counter(owner, monkeypatch):
    """Mutation: the counter alters the reply (or its record raises into the request)
    -> red."""
    with_counter = json.dumps(_read(owner, IDENTITY_SQL), sort_keys=True)
    monkeypatch.setattr(eo._LockwaitCounters, "record", lambda self, *a, **k: None)
    without = json.dumps(_read(owner, IDENTITY_SQL), sort_keys=True)
    assert with_counter == without
    assert json.loads(with_counter)["status"] == "ok"


@requires_linux_owner
def test_a_raising_counter_never_breaks_a_read(owner, monkeypatch):
    """Never raise out of the owner for a rollup problem. Mutation: no guard around
    record -> the read raises -> red."""
    def boom(self, *a, **k):
        raise RuntimeError("counter bug")

    monkeypatch.setattr(eo._LockwaitCounters, "record", boom)
    assert _read(owner)["status"] == "ok"


@requires_linux_owner
def test_flush_never_runs_on_the_request_path(home, monkeypatch):
    """The flush is patched to sleep 0.5 s while a flush is due every 50 ms: a read's
    wall clock stays far below 0.5 s. Mutation (k1): flush synchronously inside
    `_run_client_read` -> the read takes >= 0.5 s -> red."""
    make_real_store(home)
    calls = []

    def slow_flush(self):
        calls.append(threading.current_thread().name)
        time.sleep(0.5)

    monkeypatch.setattr(eo._LockwaitCounters, "flush", slow_flush)
    monkeypatch.setattr(eo, "LOCKWAIT_FLUSH_INTERVAL_S", 0.05)
    o = eo.EngramdOwner(home, request_deadline=2.0)
    o.start(install_signal_handlers=False)
    try:
        time.sleep(0.2)  # a flush is now in progress on the background thread
        t0 = time.monotonic()
        _read(o)
        elapsed = time.monotonic() - t0
        during_serving = list(calls)
    finally:
        o.stop()  # stop() flushes on the caller's thread by design; only the serving window is judged
    assert during_serving, "the background flush thread never ran"
    assert elapsed < 0.3, elapsed
    assert set(during_serving) == {"engramd-lockwait-flush"}


@requires_linux_owner
def test_file_size_is_independent_of_the_number_of_reads(owner):
    """10,000 reads across 3 families: the file has the same number of keys as after
    three reads. Mutation: a per-request row or a per-read key -> red."""
    deadline_for = lambda: time.monotonic() + 5.0
    def three():
        owner._handle_read({"sql": TRIPWIRE_SQL, "args": []}, deadline_for())
        owner._handle_read({"sql": IDENTITY_SQL, "args": []}, deadline_for())
        owner._handle_df({"terms": ["alpha"]}, deadline_for())
    three()
    small = _count_keys(_flush_and_load(owner))
    for _ in range(3333):
        three()
    doc = _flush_and_load(owner)
    assert sum(f["reads"] for f in doc["families"].values()) == 10_002
    assert _count_keys(doc) == small


# --- the report tool --------------------------------------------------------------

def _load_report():
    path = REPO_ROOT / "tools" / "engramd_lockwait_report.py"
    spec = importlib.util.spec_from_file_location("engramd_lockwait_report_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fam_doc(reads, waited, total, timed_out=0, no_time=0, hist_edge="5", p95=5):
    hist = {k: 0 for k in EDGE_KEYS}
    if waited:
        hist[hist_edge] = waited
    return {"reads": reads, "waited_reads": waited, "wait_ms_sum": total, "timed_out": timed_out,
            "no_time_before_acquire": no_time, "wait_hist": hist, "wait_ms_p95": p95 if waited else None}


def _write_day(dirpath: Path, day: str, families: dict) -> None:
    (dirpath / f"lockwait-{day}.json").write_text(json.dumps({
        "schema": 1, "date": day, "bucket_edges_ms": [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, "inf"],
        "families": families}))


def test_report_prints_the_table_and_the_tripwire_lines(tmp_path, capsys):
    """Mutation: omit the join families, show `reads/waited` swapped, or include a day
    outside the last N -> red."""
    mod = _load_report()
    _write_day(tmp_path, "2026-10-25", {"tripwire": _fam_doc(100, 7, 21.5)})
    _write_day(tmp_path, "2026-10-26", {"tripwire": _fam_doc(200, 10, 40.0, timed_out=1, no_time=2),
                                        "idf+tripwire": _fam_doc(50, 5, 12.0), "surface": _fam_doc(30, 3, 9.0)})
    _write_day(tmp_path, "2026-10-27", {"surface": _fam_doc(10, 0, 0.0)})
    rc = mod.main(["--days", "2", "--run-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-10-25" not in out
    assert "2026-10-26" in out and "2026-10-27" in out
    assert "tripwire waited/reads = 10/200" in out
    assert "idf+tripwire waited/reads = 5/50" in out
    assert "tripwire waited/reads = 0/0" in out  # 10-27 has no tripwire family
    surface_line = next(l for l in out.splitlines() if l.split()[:1] == ["surface"] and "30" in l)
    assert "10.0%" in surface_line  # 3/30 waited share
    for col in ("reads", "waited", "timed_out", "no_time_before_acquire", "p95"):
        assert col in out


def test_report_on_an_empty_dir_exits_zero_with_the_message(tmp_path, capsys):
    """Mutation: raise / non-zero / silence on an empty dir -> red."""
    mod = _load_report()
    assert mod.main(["--days", "3", "--run-dir", str(tmp_path)]) == 0
    assert f"no lockwait files in {tmp_path}" in capsys.readouterr().out


def test_report_on_a_missing_dir_says_no_files(tmp_path, capsys):
    mod = _load_report()
    missing = tmp_path / "nope"
    assert mod.main(["--days", "3", "--run-dir", str(missing)]) == 0
    assert f"no lockwait files in {missing}" in capsys.readouterr().out


@pytest.mark.parametrize("content,reason", [
    ("not json {", "not valid JSON"),
    (json.dumps({"schema": 9, "families": {}}), "schema"),
    (json.dumps({"schema": 1, "families": {"tripwire": {"reads": 1, "waited_reads": 0, "wait_ms_sum": 0, "timed_out": 0,
                                                       "no_time_before_acquire": 0, "wait_ms_p95": None}}}), "wait_hist"),
])
def test_report_on_a_malformed_file_is_nonzero_with_a_named_reason(tmp_path, capsys, content, reason):
    """Mutation: swallow the error (exit 0) or crash with a traceback -> red."""
    mod = _load_report()
    (tmp_path / "lockwait-2026-10-27.json").write_text(content)
    rc = mod.main(["--days", "3", "--run-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "lockwait-2026-10-27.json" in err and reason in err


def test_report_tool_is_read_only(tmp_path):
    """The tool never writes into the run dir. Mutation: any write -> the dir listing changes."""
    mod = _load_report()
    _write_day(tmp_path, "2026-10-27", {"tripwire": _fam_doc(10, 1, 3.0)})
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    mod.main(["--days", "3", "--run-dir", str(tmp_path)])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


# --- the 1.0 ms threshold, pinned on the record path (no timing) ----------------

def _recorded(outcome, wait_ms):
    counters = eo._LockwaitCounters(tempfile.gettempdir())
    counters.record("tripwire", outcome, wait_ms)
    (fams,) = counters._days.values()
    return fams["tripwire"]


def test_waited_threshold_is_the_named_one_ms_constant():
    """Mutation: change the constant / inline another value -> red."""
    assert eo.LOCKWAIT_WAITED_THRESHOLD_MS == 1.0


@pytest.mark.parametrize("outcome", ["acquired", "timed_out"])
def test_a_wait_of_exactly_one_ms_is_a_read_but_not_a_waited_read(outcome):
    """Contract: waited_reads is `wait_ms > 1.0`, strictly. Mutation: `>` -> `>=` in
    `_LockwaitCounters.record` -> waited_reads / sum / a bucket is non-zero -> red."""
    fam = _recorded(outcome, 1.0)
    assert fam["reads"] == 1
    assert fam["timed_out"] == (1 if outcome == "timed_out" else 0)
    assert fam["waited_reads"] == 0
    assert fam["wait_ms_sum"] == 0
    assert fam["hist"] == [0] * len(eo.LOCKWAIT_BUCKET_EDGES_MS)


@pytest.mark.parametrize("outcome", ["acquired", "timed_out"])
def test_a_wait_just_over_one_ms_is_a_waited_read(outcome):
    """Mutation: `>` -> `> 1.0001` or a rounded compare -> red."""
    fam = _recorded(outcome, 1.0001)
    assert fam["reads"] == 1
    assert fam["waited_reads"] == 1
    assert fam["wait_ms_sum"] == pytest.approx(1.0001)
    assert sum(fam["hist"]) == 1 and fam["hist"][1] == 1  # first edge >= 1.0001 is 2 ms
