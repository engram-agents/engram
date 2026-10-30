"""Contract tests for the SIBLING owner health lines (#4271 round 2, part of #2652).

Round 1 gave the session-start hook's owner-failure line `, bound N ms` and
`; failed E ms into the B ms fire budget` (tests/test_owner_health_line_bound_4271.py). The surface
hook and the lesson-tripwire hook print the same shape of line and carry the same two blind spots.

  R1  each hook's `_record_owner_failure` keeps `elapsed_ms` for the FIRST failure (int, rounded),
      or None when no start time is known. Fire start (both hooks, round 3 / F2): `run["t0"]`,
      recorded where the OwnerReadConnection is created, never the process import time.
  R2  each hook's health text adds `, bound N ms` (finite numeric `result.bound` only), then
      `; failed E ms into the B ms fire budget` (B = that hook's own OWNER_FIRE_BUDGET_S).
  R3  the tripwire's socket line printed a FIXED `bound 1.0s per read`; it now prints the actual
      bound when one exists and otherwise says `configured:`.
  R4  each hook's stderr line carries the same two fields under its [:300] cut.
  R5  never raises on an odd attribute; never prints SQL, row data or a message body.

Every docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import io
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import pytest  # noqa: E402

import engramd_client as ec  # noqa: E402

from test_surface_owner_read_3821 import load_hook as load_surface_hook  # noqa: E402
from test_surface_owner_read_3821 import _restore_imports  # noqa: E402,F401 -- autouse sys.modules restore
from test_tripwire_owner_read_3665 import home, load_hook as load_tripwire_hook  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

T0 = 1000.0


def _unavailable(bound=0.37, reason="timeout"):
    return ec.Unavailable(socket_path="/x/sock", bound=bound, agent_steps=["a"],
                          human_steps=["h"], reason=reason)


def _clock(mod, now):
    """Give the hook module a `time` whose monotonic() is `now` (everything else is the real time)."""
    ns = {k: getattr(time, k) for k in dir(time) if not k.startswith("__")}
    ns["monotonic"] = lambda: now
    mod.time = types.SimpleNamespace(**ns)


class Surface:
    name = "surface"
    budget_ms = 3000
    stderr_prefix = "engram-surface-hook: owner read failed: "

    def __init__(self, home, monkeypatch):
        self.mod = load_surface_hook(home, monkeypatch)
        self.mod._OWNER_RUN = {"on": True, "conn": None, "failure": None, "degraded": []}
        self.run = self.mod._OWNER_RUN

    def fail(self, site, exc, now, started=T0):
        if started is not None:
            self.run["t0"] = started
        _clock(self.mod, now)
        assert self.mod._record_owner_failure(site, exc)

    def text(self):
        return self.mod._owner_failure_text(self.run)

    def stderr(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.mod._write_owner_stderr_lines()
        return err.getvalue()

    def head(self, text):
        """The part of the text that carries the health clause (the whole one-line notice)."""
        return text

    def set_failure(self, f):
        self.run["failure"] = f
        self.run["degraded"] = [f["site"]]


class Tripwire:
    name = "tripwire"
    budget_ms = 2000
    stderr_prefix = "engram-lesson-tripwire-hook: owner read failed: "

    def __init__(self, home, monkeypatch):
        self.mod = load_tripwire_hook(home, monkeypatch)
        self.mod._OWNER_RUN = {"on": True, "conn": None, "failure": None}
        self.run = self.mod._OWNER_RUN

    def fail(self, site, exc, now, started=T0):
        if started is not None:
            self.run["t0"] = started
        _clock(self.mod, now)
        assert self.mod._record_owner_failure(site, exc)

    def text(self):
        return self.mod._owner_failure_notice(self.run["failure"])

    def stderr(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.mod._write_owner_stderr_lines()
        return err.getvalue()

    def head(self, text):
        """The notice's first line. The Socket line below it also says `bound N ms`, so a check on
        the whole text would stay green with the head clause deleted."""
        return text.splitlines()[0]

    def set_failure(self, f):
        self.run["failure"] = f


HOOKS = [Surface, Tripwire]


@pytest.fixture(params=HOOKS, ids=lambda c: c.name)
def h(request, home, monkeypatch):
    return request.param(home, monkeypatch)


def _owner_err(kind, result):
    return ec.OwnerReadError(kind, result)


def test_t10_the_text_prints_the_bound_and_the_fire_elapsed(h):
    """Mutation: drop the `, bound N ms` clause -> red; drop the `; failed ...` clause -> red."""
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.37)), now=T0 + 2.63)
    assert h.run["failure"]["elapsed_ms"] == 2630
    text = h.head(h.text())
    assert "bound 370 ms" in text, text
    assert f"failed 2630 ms into the {h.budget_ms} ms fire budget" in text, text
    assert text.index("bound 370 ms") < text.index("failed 2630 ms")


def test_t10b_the_budget_in_the_text_is_the_hooks_own_constant(h):
    """Mutation: hard-code a budget instead of OWNER_FIRE_BUDGET_S -> red."""
    h.mod.OWNER_FIRE_BUDGET_S = 7.0
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.37)), now=T0 + 2.0)
    assert "into the 7000 ms fire budget" in h.text()


def test_t10c_the_elapsed_is_taken_at_the_first_failure_not_the_last(h):
    """Mutation: overwrite elapsed on every failure -> the later 2900 ms shows -> red."""
    h.fail("first_site", _owner_err("unavailable", _unavailable(1.0)), now=T0 + 0.5)
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.1)), now=T0 + 2.9)
    text = h.head(h.text())
    assert "failed 500 ms into" in text and "bound 1000 ms" in text, text


def test_t11_no_bound_text_without_a_bound_but_the_elapsed_stays(h):
    """budget_exhausted and a Rejected carry no `.bound`. Mutation: print `bound None ms` or
    raise on a missing attribute -> red."""
    rej = ec.Rejected(reason="unknown_operation", detail="d", remedy="r")
    for kind, result in (("rejected", rej), ("budget_exhausted", None), ("unusable_answer", None)):
        h.run["failure"] = None
        h.fail("calibration", _owner_err(kind, result), now=T0 + 1.0)
        text = h.text()
        assert "bound" not in text, (kind, text)
        assert f"failed 1000 ms into the {h.budget_ms} ms fire budget" in text, (kind, text)


def test_t11b_a_non_numeric_bound_is_ignored_and_never_raises(h):
    """Mutation: format the bound without a numeric guard -> TypeError / 'bound x ms' -> red."""
    for odd in ("0.37", None, True, [1], float("nan"), float("inf")):
        res = type("R", (), {"reason": "timeout", "bound": odd, "socket_path": "/x/sock",
                             "agent_steps": ["a"], "human_steps": ["h"]})()
        h.set_failure({"site": "calibration", "kind": "unavailable", "cause": None,
                       "result": res, "elapsed_ms": 10})
        text = h.text()
        assert "bound" not in text, (odd, text)
        assert "failed 10 ms" in text, (odd, text)


def test_t11c_an_unknown_start_gives_no_elapsed_text_and_no_exception(h):
    """Both hooks: `run["t0"]` never recorded. Mutation: compute elapsed
    from a None start (TypeError) or print `None ms` -> red."""
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.37)), now=T0 + 2.63, started=None)
    assert h.run["failure"]["elapsed_ms"] is None
    text = h.head(h.text())
    assert "bound 370 ms" in text and "fire budget" not in text and "None" not in text, text


def test_t11d_a_failure_dict_without_elapsed_still_renders(h):
    """Mutation: index the key -> KeyError -> red."""
    h.set_failure({"site": "calibration", "kind": "unavailable", "result": _unavailable(0.37), "cause": None})
    text = h.head(h.text())
    assert "bound 370 ms" in text and "fire budget" not in text, text


def test_t12_the_cap_survives_a_long_failure_and_keeps_the_new_text(h):
    """The new text sits in the head, ahead of every step and block list, so the notice cap keeps
    it. Mutation: place the new text after the steps / block list -> the cut removes it -> red."""
    res = ec.Unavailable(socket_path="/x/" + "s" * 300, bound=0.37, agent_steps=["g" * 300] * 20,
                         human_steps=["m" * 300] * 20, reason="r" * 100)
    h.fail("calibration", _owner_err("unavailable", res), now=T0 + 2.63)
    if h.name == "surface":
        h.run["degraded"] = ["a" * 30, "b" * 30, "c" * 30, "d" * 30, "e" * 30, "f" * 30]
    text = h.text()
    cap = h.mod._OWNER_NOTICE_CAP
    assert len(text) <= cap
    text = h.head(text)
    assert "bound 370 ms" in text and f"failed 2630 ms into the {h.budget_ms} ms fire budget" in text, text


def test_t13_the_stderr_line_carries_both_fields_under_its_cut(h):
    """Mutation: leave the stderr line unchanged -> red; drop the [:300] cut -> the length check is red."""
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.37)), now=T0 + 2.63)
    out = h.stderr()
    assert f"{h.stderr_prefix}unavailable at calibration" in out, out
    assert "bound 370 ms" in out and f"failed 2630 ms into the {h.budget_ms} ms fire budget" in out, out
    assert all(len(line) <= 300 for line in out.splitlines())
    # a long site cannot push the line past the cut
    h.run["failure"] = None
    h.fail("s" * 400, _owner_err("unavailable", _unavailable(0.37)), now=T0 + 2.63)
    assert all(len(line) <= 300 for line in h.stderr().splitlines())


def test_t13b_the_stderr_line_without_a_bound_or_start_has_neither_field(h):
    """The existing stderr shape is unchanged when there is nothing to add."""
    h.fail("calibration", _owner_err("budget_exhausted", None), now=T0 + 2.63, started=None)
    assert h.stderr() == f"{h.stderr_prefix}budget_exhausted at calibration\n"


# ---- the tripwire's own pieces -------------------------------------------------------------

def test_tripwire_records_t0_where_it_creates_its_connection(home, monkeypatch):
    """R1: `run["t0"]` is the monotonic time when the OwnerReadConnection is created, and only
    once. Mutation: never record it -> red; record it at every call -> the second assert is red."""
    h = Tripwire(home, monkeypatch)
    assert h.run.get("t0") is None
    _clock(h.mod, 555.0)
    conn = h.mod._owner_conn()
    assert h.run["t0"] == 555.0
    _clock(h.mod, 600.0)
    assert h.mod._owner_conn() is conn and h.run["t0"] == 555.0


def test_tripwire_import_failure_before_any_connection_has_no_elapsed(home, monkeypatch):
    """A client that cannot be imported fails before a connection (and so a t0) exists: the text
    still renders, with no elapsed. Mutation: invent a t0 at fire start in the helper -> red."""
    h = Tripwire(home, monkeypatch)
    _clock(h.mod, 9999.0)
    assert h.mod._record_owner_failure("focused_ids", h.mod._ClientImportFailure("ImportError"))
    assert h.run["failure"]["elapsed_ms"] is None
    assert "fire budget" not in h.text()


def test_tripwire_socket_line_prints_the_actual_bound_not_the_fixed_one(home, monkeypatch):
    """R3: with a real bound the line says so and the fixed `bound 1.0s per read` text is gone.
    Mutation: restore the fixed f-string -> red."""
    h = Tripwire(home, monkeypatch)
    h.fail("calibration", _owner_err("unavailable", _unavailable(0.37)), now=T0 + 1.0)
    text = h.text()
    socket_line = next(ln for ln in text.splitlines() if ln.startswith("  Socket:"))
    assert "bound 370 ms on this read" in socket_line and "2.0s per fire" in socket_line, socket_line
    assert "bound 1.0s per read" not in text and "configured" not in socket_line, text


def test_tripwire_socket_line_labels_the_configured_numbers_when_no_bound_exists(home, monkeypatch):
    """R3: no finite bound -> the configured numbers, labelled `configured:`. Mutation: drop the
    label -> red; print the configured number as if it were the read's bound -> red."""
    h = Tripwire(home, monkeypatch)
    for odd in (None, "x", float("nan")):
        res = type("R", (), {"reason": "timeout", "bound": odd, "socket_path": "/x/sock",
                             "agent_steps": ["a"], "human_steps": ["h"]})()
        h.set_failure({"site": "calibration", "kind": "unavailable", "cause": None, "result": res,
                       "elapsed_ms": None})
        socket_line = next(ln for ln in h.text().splitlines() if ln.startswith("  Socket:"))
        assert "(configured: 1.0s per read, 2.0s per fire)" in socket_line, (odd, socket_line)
    # a result with no `bound` attribute at all
    res = type("R", (), {"reason": "timeout", "socket_path": "/x/sock", "agent_steps": [], "human_steps": []})()
    h.set_failure({"site": "calibration", "kind": "unavailable", "cause": None, "result": res})
    socket_line = next(ln for ln in h.text().splitlines() if ln.startswith("  Socket:"))
    assert "(configured: 1.0s per read, 2.0s per fire)" in socket_line, socket_line


def test_tripwire_budget_exhausted_sentence_is_unchanged(home, monkeypatch):
    """R3: the budget-exhausted sentence is kept as it was."""
    h = Tripwire(home, monkeypatch)
    h.fail("calibration", _owner_err("budget_exhausted", None), now=T0 + 2.0)
    assert "  The fire's owner time budget (2.0s) ran out before this read." in h.text()


def test_the_texts_never_carry_sql_or_row_data(h):
    """R5: the recorded result's `detail` (which may name a statement) never reaches the text.
    Mutation: append `result.detail` to the new clause -> red."""
    res = ec.Unavailable(socket_path="/x/sock", bound=0.37, agent_steps=["a"], human_steps=["h"],
                         reason="timeout", detail="SELECT secret FROM nodes -- SENTINEL-4271")
    h.fail("calibration", _owner_err("unavailable", res), now=T0 + 1.0)
    assert "SENTINEL-4271" not in h.text() and "SENTINEL-4271" not in h.stderr()


def test_f2_the_surface_elapsed_counts_from_the_connection_not_from_process_import(home, monkeypatch):
    """Round 3 / F2. `_HOOK_STARTED` (process import) is 100 s before the connection is created; the
    fire budget starts at the connection (`run["t0"]`). Mutation: measure from `_HOOK_STARTED` ->
    `failed 102630 ms` -> red. Also drives `_owner_conn` so the t0 recording itself is exercised."""
    s = Surface(home, monkeypatch)
    s.mod._HOOK_STARTED = T0 - 100.0
    _clock(s.mod, T0)
    fake = types.SimpleNamespace(OwnerReadConnection=lambda **kw: object())
    s.mod._owner_client = lambda: fake
    s.mod._owner_conn()
    assert s.run["t0"] == T0
    _clock(s.mod, T0 + 2.63)
    assert s.mod._record_owner_failure("calibration", _owner_err("unavailable", _unavailable(0.37)))
    assert s.run["failure"]["elapsed_ms"] == 2630
    text = s.text()
    assert "failed 2630 ms into the 3000 ms fire budget" in text, text
    assert "102630" not in text
