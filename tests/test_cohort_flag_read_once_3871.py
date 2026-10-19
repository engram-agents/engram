"""engram-alpha#3871 -- cohort_dispatch `verify-in` reads the owner flag ONCE per run.

`cmd_verify_in` decided `owner_serves` via `owner_read.flag_applies`, then `_fetch_nodes` called
`flag_applies` again. Two reads of one decision: a broken runtime bundle printed #3869's class-only
stderr line twice, and the two reads could disagree if config.json changed between them.

Contract (each of a, c, d red at the pre-fix commit):

  (a) a broken bundle through `verify-in` -> EXACTLY ONE `engramd_owner_flags failed` line
  (b) the same through `prepare` -> one line (the control: prepare always read once)
  (c) `owner_read.flag_applies` is called exactly once per `verify-in` run, flag ON (owner up) and OFF
  (d) the flag flipping between the two would-be reads: the FIRST decision wins, in both directions
  (e) store selection stays with `flag_applies`: flag ON + `--db` naming another file -> direct
      `mode=ro`, the owner is never contacted
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from _fake_owner import FakeOwner

# Reuse the #3862 seat/fixture helpers (autouse import restore, temp home, store with 1200 nodes).
from test_cohort_owner_read_3862 import (  # noqa: F401
    FLAG_KEY,
    Opens,
    _ids,
    _restore_imports,
    home,
    load_tool,
    seat,
    set_flag,
)


def _prepare(mod, seat, ids=(1, 2, 3)):
    """Chunk a cohort with the flag OFF (direct path); returns the out dir."""
    set_flag(seat, False)
    out = seat / "cohort"
    rc = mod.main(["prepare", "--ids", ",".join(_ids(*ids)), "--out", str(out)])
    assert rc == 0
    return out


def _verify(mod, out, *extra, capsys):
    capsys.readouterr()
    rc = mod.main(["verify-in", "--out", str(out), *extra])
    cap = capsys.readouterr()
    return rc, cap


class FlagSpy:
    """Wrap `owner_read.flag_applies`: count the calls; optionally force a scripted sequence of answers
    (the last answer repeats), otherwise answer as the real function does."""

    def __init__(self, mod, monkeypatch, script=None):
        self.calls = 0
        self._script = script
        self._real = mod.owner_read.flag_applies
        monkeypatch.setattr(mod.owner_read, "flag_applies", self)

    def __call__(self, spec, db_path):
        i = self.calls
        self.calls += 1
        if self._script is None:
            return self._real(spec, db_path)
        return self._script[min(i, len(self._script) - 1)]


def _reads(owner):
    return [r for r in owner.requests if r["method"] == "read"]


# ---- (a) / (b): the broken-bundle line prints once ------------------------------------------

def test_a_verify_in_with_a_broken_bundle_prints_the_flag_line_exactly_once(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    set_flag(seat)
    monkeypatch.setitem(sys.modules, "engramd_owner_flags", None)  # `import` -> ImportError
    rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)  # the unknown flag reads OFF: the direct path still verifies
    lines = [ln for ln in cap.err.splitlines() if "engramd_owner_flags failed" in ln]
    assert lines == [f"engramd.{FLAG_KEY}: engramd_owner_flags failed: ModuleNotFoundError"], lines


def test_b_prepare_with_a_broken_bundle_prints_the_flag_line_exactly_once(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    set_flag(seat)
    monkeypatch.setitem(sys.modules, "engramd_owner_flags", None)
    capsys.readouterr()
    rc = mod.main(["prepare", "--ids", ",".join(_ids(1, 2, 3)), "--out", str(seat / "cohort")])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "engramd_owner_flags failed" in ln]
    assert len(lines) == 1, lines


# ---- (c): the read count ---------------------------------------------------------------------

def test_c_verify_in_with_the_flag_on_and_the_owner_up_reads_the_flag_once(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    set_flag(seat)
    spy = FlagSpy(mod, monkeypatch)
    with FakeOwner(seat) as owner:
        rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert json.loads(cap.out).get("passed") is True
    assert len(_reads(owner)) == 1  # it really went through the owner
    assert spy.calls == 1


def test_c_verify_in_with_the_flag_off_reads_the_flag_once(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    spy = FlagSpy(mod, monkeypatch)
    rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert spy.calls == 1


# ---- (d): the first decision is the only decision --------------------------------------------

def test_d_first_read_on_then_off_follows_the_owner_only(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    set_flag(seat)
    spy = FlagSpy(mod, monkeypatch, script=[True, False])
    opens = Opens().install(monkeypatch, raising=False)
    with FakeOwner(seat) as owner:
        rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert len(_reads(owner)) == 1 and opens.record == [], (owner.requests, opens.record)
    assert spy.calls == 1


def test_d_first_read_off_then_on_never_contacts_the_owner(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    set_flag(seat)
    spy = FlagSpy(mod, monkeypatch, script=[False, True])
    opens = Opens().install(monkeypatch, raising=False)
    with FakeOwner(seat) as owner:
        rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert _reads(owner) == [], owner.requests
    assert len(opens.record) == 1 and "mode=ro" in opens.record[0][1], opens.record
    assert spy.calls == 1


# ---- (e): store selection stays with flag_applies --------------------------------------------

def test_e_flag_on_with_another_db_stays_direct_and_never_contacts_the_owner(seat, monkeypatch, capsys):
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    other = seat / "other"
    other.mkdir()
    shutil.copy(seat / "knowledge.db", other / "k.db")
    set_flag(seat)
    opens = Opens().install(monkeypatch, raising=False)
    with FakeOwner(seat) as owner:
        rc, cap = _verify(mod, out, "--db", str(other / "k.db"), capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert _reads(owner) == []
    assert len(opens.record) == 1 and "mode=ro" in opens.record[0][1], opens.record


# ---- (f): flag ON + owner up + the local store FILE missing: the owner's answer is the precondition ----

def test_f_flag_on_owner_up_and_the_local_store_file_missing_still_verifies_via_the_owner(seat, monkeypatch, capsys):
    """Kills `use_owner=owner_serves and db_path.exists()`: the owner's answer replaces the exists() check.

    The in-process FakeOwner reads `<home>/knowledge.db` per request, so the file cannot really be deleted;
    instead THIS process's view of the store file is "missing" (Path.exists False for it), the way a seat
    whose owner serves a store the tool cannot stat would see it."""
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    set_flag(seat)
    spy = FlagSpy(mod, monkeypatch)
    store = str((seat / "knowledge.db").resolve())
    real_exists = Path.exists

    def exists(self, *a, **k):
        if str(self).endswith("knowledge.db") or str(self.resolve()) == store:
            return False
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    with FakeOwner(seat) as owner:
        rc, cap = _verify(mod, out, capsys=capsys)
    assert rc == 0, (cap.out, cap.err)
    assert json.loads(cap.out).get("passed") is True
    assert "FileNotFoundError" not in cap.err and "DB path" not in cap.err, cap.err
    assert "not found" not in cap.out, cap.out
    assert len(_reads(owner)) == 1  # the content-integrity check really ran, against the owner's rows
    assert spy.calls == 1


# ---- (g): an empty cohort never reads the flag ------------------------------------------------

def test_g_a_cohort_with_no_ids_reads_the_flag_zero_times_and_never_contacts_the_owner(seat, monkeypatch, capsys):
    """Kills dropping `bool(all_ids_seen) and`: with nothing to verify there is no decision to make."""
    mod = load_tool(seat, monkeypatch)
    out = _prepare(mod, seat)
    for payload in out.glob("**/payload.json"):
        payload.write_text("[]")
    set_flag(seat)
    spy = FlagSpy(mod, monkeypatch)
    with FakeOwner(seat) as owner:
        _rc, cap = _verify(mod, out, capsys=capsys)
    assert spy.calls == 0, (cap.out, cap.err)
    assert _reads(owner) == [], owner.requests
