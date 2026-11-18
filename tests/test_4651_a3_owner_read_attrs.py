"""Contract cells for the one shared change of #4651 part A slice A3 (C6): `_owner_read.write` tags what it raises.

`write` flattens every owner refusal into `spec.error_cls(<message>)`, so a caller could tell `flag_off` from the other refusals
only by matching message text. The change is MINIMAL and backwards compatible: on every raise inside `write` it sets two optional
attributes on the exception, and changes nothing else.

    owner_reason   the Rejected / Unavailable reason; "down" for a down owner; None when the failure carries no owner reason
    owner_commit   the Rejected `commit` value ("indeterminate", "committed_unverified") or None; "indeterminate" on the failures
                   whose own message says the outcome is INDETERMINATE and which carry no Rejected (an Unavailable that is not
                   "down", a client that raised, a non-object answer)

Two families: MESSAGES (every existing message byte-identical to the one the unchanged `write` produced; these were recorded from
the unchanged code and are GREEN before and after) and ATTRIBUTES (red before the change).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))

import engramd_client as ec  # noqa: E402

from tools import _owner_read as owner_read  # noqa: E402


class Err(Exception):
    pass


def spec():
    return owner_read.OwnerReadSpec(flag_key="k", error_cls=Err, home=Path("/h"), never_empty="")


def un(reason):
    return ec.Unavailable(socket_path="x", bound=1.0, agent_steps=["a"], human_steps=["h"], detail="d", reason=reason)


def rj(reason, commit=None):
    return ec.Rejected(reason=reason, detail="DET", remedy="REM", commit=commit)


M = "m"
# name -> (how the client answers, the message of the UNCHANGED write, owner_reason, owner_commit)
CASES = {
    "down": (lambda: un("down"),
             "the engramd owner is DOWN (owner unavailable: down); the m request did not reach it and nothing was written. "
             "Not writing the store directly. Start the owner and retry, or set engramd.k to false.", "down", None),
    "timeout": (lambda: un("timeout"),
                "the engramd owner m outcome is INDETERMINATE (owner unavailable: timeout); the write may or may not have "
                "committed. Not writing the store directly. Retry once the owner is healthy, or set engramd.k to false.",
                "timeout", "indeterminate"),
    "no_reason": (lambda: un(None),
                  "the engramd owner m outcome is INDETERMINATE (owner unavailable: unavailable); the write may or may not "
                  "have committed. Not writing the store directly. Retry once the owner is healthy, or set engramd.k to false.",
                  "unavailable", "indeterminate"),
    "rejected_indeterminate": (lambda: rj("publish_write_failed", "indeterminate"),
                               "the engramd owner m outcome is INDETERMINATE (publish_write_failed: DET); the write may or "
                               "may not have committed. Not writing the store directly. Retry once the owner is healthy, or "
                               "set engramd.k to false.", "publish_write_failed", "indeterminate"),
    "committed_unverified": (lambda: rj("readback_mismatch", "committed_unverified"),
                             "the engramd owner m COMMITTED but could not verify it (readback_mismatch: DET); REM. Not "
                             "writing the store directly.", "readback_mismatch", "committed_unverified"),
    "refused": (lambda: rj("invalid_params"),
                "the engramd owner refused the m write (invalid_params: DET); REM. Nothing was written. Not writing the "
                "store directly. If that does not help, set engramd.k to false.", "invalid_params", None),
    "flag_off": (lambda: rj("flag_off"),
                 "the engramd owner refused the m write (flag_off: DET); REM. Nothing was written. Not writing the store "
                 "directly. If that does not help, set engramd.k to false.", "flag_off", None),
    "ok_not_an_object": (lambda: ec.Ok(result=[1], request_id="r"),
                         "the engramd owner answered the m write malformed (the result is not an object); the outcome is "
                         "INDETERMINATE. Not writing the store directly. Check the owner build, or set engramd.k to false.",
                         None, "indeterminate"),
}
NAMES = list(CASES)


def run(monkeypatch, name):
    make, _msg, _reason, _commit = CASES[name]
    monkeypatch.setattr(ec, "write", lambda *a, **k: make())
    with pytest.raises(Err) as e:
        owner_read.write(spec(), M, {}, request_id="r")
    return e.value


def run_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(ec, "write", boom)
    with pytest.raises(Err) as e:
        owner_read.write(spec(), M, {}, request_id="r")
    return e.value


def run_import_failure(monkeypatch):
    def bad(name):
        raise ImportError("nope")

    monkeypatch.setattr(owner_read, "import_runtime", bad)
    with pytest.raises(Err) as e:
        owner_read.write(spec(), M, {}, request_id="r")
    return e.value


RAISES_MSG = ("the engramd owner m failed (OSError); the outcome is INDETERMINATE. Not writing the store directly. Start the "
              "owner, or set engramd.k to false.")
IMPORT_MSG = ("engramd.k is on but engramd_client could not be loaded (ImportError: nope); not writing the store directly. "
              "Fix the plugin install, or set the flag to false.")


# ---- MESSAGES: unchanged, green before and after ------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_c6_every_existing_message_is_byte_identical(monkeypatch, name):
    """Mutant: any edit to a message (a reworded sentence, a reason appended)."""
    assert str(run(monkeypatch, name)) == CASES[name][1]


def test_c6_the_client_raising_message_is_byte_identical(monkeypatch):
    assert str(run_raises(monkeypatch)) == RAISES_MSG


def test_c6_the_import_failure_message_is_byte_identical(monkeypatch):
    assert str(run_import_failure(monkeypatch)) == IMPORT_MSG


# ---- ATTRIBUTES: red before the change -----------------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_c6_every_raise_carries_owner_reason_and_owner_commit(monkeypatch, name):
    """Mutants: `owner_reason` unset on the Unavailable("down") raise only; one raise site untagged; the commit not carried."""
    exc = run(monkeypatch, name)
    assert exc.owner_reason == CASES[name][2]
    assert exc.owner_commit == CASES[name][3]


def test_c6_a_client_that_raises_is_tagged_indeterminate_with_no_reason(monkeypatch):
    exc = run_raises(monkeypatch)
    assert (exc.owner_reason, exc.owner_commit) == (None, "indeterminate")


def test_c6_an_import_failure_is_tagged_with_neither(monkeypatch):
    exc = run_import_failure(monkeypatch)
    assert (exc.owner_reason, exc.owner_commit) == (None, None)


def test_c6_a_successful_write_returns_the_result_untouched(monkeypatch):
    """Mutant: the success path changed (a wrapper object, a tagged dict)."""
    monkeypatch.setattr(ec, "write", lambda *a, **k: ec.Ok(result={"a": 1}, request_id="r"))
    assert owner_read.write(spec(), M, {}, request_id="r") == {"a": 1}


def test_c6_the_attributes_are_the_only_addition_to_the_raised_exception(monkeypatch):
    """The class, args and cause are what the unchanged write produced. Mutant: a different exception class or a lost cause."""
    exc = run(monkeypatch, "flag_off")
    assert type(exc) is Err and exc.args == (CASES["flag_off"][1],)
    assert sorted(k for k in vars(exc) if not k.startswith("__")) == ["owner_commit", "owner_reason"]
