"""engram-alpha#3843 (part of #3039): under comms_surface != forum, a baton flip/advance to ANOTHER
agent is not delivered durably by the flip: the durable hand-off is an addressed crossing. The CLI says so, once, on stderr,
after the successful write.

Contract (tests first):
  C1  `baton flip` / `baton advance` moving the turn to an agent other than the caller, with the
      caller's config `comms_surface` set to anything but `forum`: ONE bounded hint line on stderr
      AFTER the write; stdout and exit status are exactly today's.
  C1b `forum` (also absent, unreadable, a non-string) -> no hint: the forum surface's own hook delivers.
  C2  no hint for a flip to the caller itself, to the pool sentinel, or for release / close.
  C3  control: the C1 cell is RED against the unmodified CLI (see the mutation notes in the PR).
  C4  the hint line is one line, bounded, names the recipient, names send_crossing / wake, and carries
      no reason text (the reason is the caller's free text and never rides along).
"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_baton import MULTI_CONFIG, _baton, _make_client, _make_raw_baton  # noqa: E402
from test_baton_advance_gate import _FakeClient, _args as _advance_args, _BATON_RAW  # noqa: E402

HINT_TOKEN = "durable hand-off"


def _umwelt(**extra):
    return {**MULTI_CONFIG, "comms_surface": "umwelt", **extra}


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
    monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))


def _flip(config, to="ariadne", reason="done step 1", caller="borges", project="PR-400", **fm):
    client = _make_client(post_return={"seq": 1}, get_return={"raw": _make_raw_baton(project, **fm)})
    args = argparse.Namespace(project_id=project, to=to, reason=reason, force=False)
    _baton.cmd_flip(args, config, caller, client)
    return client


def _hint_lines(err: str):
    return [ln for ln in err.splitlines() if HINT_TOKEN in ln]


# ---- C1: the hint ---------------------------------------------------------------------------

def test_c1_flip_to_another_agent_under_umwelt_prints_one_hint_on_stderr(capsys):
    _flip(_umwelt())
    out, err = capsys.readouterr()
    hints = _hint_lines(err)
    assert len(hints) == 1, err
    assert "ariadne" in hints[0] and "send_crossing" in hints[0] and "wake" in hints[0], hints[0]
    assert HINT_TOKEN not in out, "the hint is stderr only; stdout is today's"


def test_c1_the_hint_comes_after_the_write_and_stdout_is_exactly_todays(capsys):
    client = _flip(_umwelt())
    out, err = capsys.readouterr()
    assert client.post.call_count == 1
    assert out.splitlines() == ["baton flip: PR-400 → ariadne", "  from:   borges", "  reason: done step 1"], out


@pytest.mark.parametrize("surface", ["umwelt", "Umwelt", " umwelt ", "something-new", "letters"])
def test_c1_any_surface_but_forum_gets_the_hint(surface, capsys):
    _flip({**MULTI_CONFIG, "comms_surface": surface})
    assert len(_hint_lines(capsys.readouterr().err)) == 1


def test_c1_a_failed_write_prints_no_hint(capsys):
    client = _make_client(post_return={"seq": 1}, get_return={"raw": _make_raw_baton("PR-400")})
    client.post.side_effect = _baton.ForumNetworkError("down")
    args = argparse.Namespace(project_id="PR-400", to="ariadne", reason="r", force=False)
    with pytest.raises(SystemExit):
        _baton.cmd_flip(args, _umwelt(), "borges", client)
    assert HINT_TOKEN not in capsys.readouterr().err


def test_c1_advance_to_another_agent_under_umwelt_prints_one_hint(capsys):
    cfg = {"agent_name": "example-agent-colleague", "forum": {"url": "http://test-forum:5002"},
           "comms_surface": "umwelt"}
    c = _FakeClient(post_result={"advanced": True, "turn": "example-agent-seat",
                                 "ci": {"state": "green", "detail": "x"},
                                 "approval": {"state": "approved_current", "detail": "ok"}, "warnings": []})
    _baton.cmd_advance(_advance_args(), cfg, "example-agent-colleague", c)
    out, err = capsys.readouterr()
    hints = _hint_lines(err)
    assert len(hints) == 1 and "example-agent-seat" in hints[0], err
    assert "CI:" in out and "approval:" in out and HINT_TOKEN not in out


def test_c1_advance_that_did_nothing_prints_no_hint(capsys):
    cfg = {"agent_name": "example-agent-colleague", "forum": {"url": "http://test-forum:5002"},
           "comms_surface": "umwelt"}
    c = _FakeClient(post_result={"advanced": False, "reason": "already merge"})
    _baton.cmd_advance(_advance_args(), cfg, "example-agent-colleague", c)
    assert HINT_TOKEN not in capsys.readouterr().err


# ---- C1b: forum / unreadable -> silent --------------------------------------------------------

@pytest.mark.parametrize("config", [
    {**MULTI_CONFIG},                                  # key absent == forum
    {**MULTI_CONFIG, "comms_surface": "forum"},
    {**MULTI_CONFIG, "comms_surface": "FORUM"},
    {**MULTI_CONFIG, "comms_surface": None},
    {**MULTI_CONFIG, "comms_surface": 3},
    {**MULTI_CONFIG, "comms_surface": ["umwelt"]},
    {**MULTI_CONFIG, "comms_surface": ""},
], ids=["absent", "forum", "FORUM", "null", "int", "list", "empty"])
def test_c1b_forum_or_unreadable_surface_prints_no_hint(config, capsys):
    _flip(config)
    assert HINT_TOKEN not in capsys.readouterr().err


# ---- C2: self, pool sentinel, release, close --------------------------------------------------

def test_c2_flip_to_the_caller_itself_prints_no_hint(capsys):
    _flip(_umwelt(), to="borges", turn="ariadne")
    assert HINT_TOKEN not in capsys.readouterr().err


def test_c2_flip_to_the_pool_sentinel_prints_no_hint(capsys, monkeypatch):
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    _flip(_umwelt(), to="casey", participants="[borges, ariadne, casey]")
    assert HINT_TOKEN not in capsys.readouterr().err


def test_c2_close_prints_no_hint(capsys):
    client = _make_client(post_return={"seq": 1}, get_return={"raw": _make_raw_baton("PR-500", status="in-review")})
    args = argparse.Namespace(project_id="PR-500", status="merged", reason="done", force=False)
    try:
        _baton.cmd_close(args, _umwelt(), "borges", client)
    except SystemExit:
        pass
    assert HINT_TOKEN not in capsys.readouterr().err


# ---- C4: the line itself ----------------------------------------------------------------------

def test_c4_the_hint_is_one_bounded_line_without_the_reason(capsys):
    secret = "REASON-TEXT-" + "x" * 400 + "\nSECOND-LINE"
    _flip(_umwelt(), reason=secret)
    err = capsys.readouterr().err
    hints = _hint_lines(err)
    assert len(hints) == 1 and "\n" not in hints[0]
    assert len(hints[0]) <= 300, len(hints[0])
    assert "REASON-TEXT" not in hints[0] and "SECOND-LINE" not in hints[0]


def test_c4_a_huge_or_control_laden_surface_value_cannot_stretch_or_break_the_line(capsys):
    for surface in ("x" * 5000, "um\x1b[31mwelt", "line1\nline2" + "y" * 400):
        _flip({**MULTI_CONFIG, "comms_surface": surface})
        err = capsys.readouterr().err
        hints = _hint_lines(err)
        assert len(hints) == 1 and "\n" not in hints[0] and "\x1b" not in hints[0], (surface[:20], err[:200])
        assert len(hints[0]) <= 300


def test_c4_a_recipient_with_control_characters_cannot_break_the_line(capsys):
    raw = _make_raw_baton("PR-400", participants="[borges, ari\x1badne]")
    client = _make_client(post_return={"seq": 1}, get_return={"raw": raw})
    args = argparse.Namespace(project_id="PR-400", to="ari\x1badne", reason="r", force=False)
    try:
        _baton.cmd_flip(args, _umwelt(), "borges", client)
    except SystemExit:
        pass  # a handle with a control byte may be refused outright; either way no raw ESC in a hint
    err = capsys.readouterr().err
    for hint in _hint_lines(err):
        assert "\x1b" not in hint


def test_c4_the_helper_strips_control_characters_and_bounds_a_huge_recipient_handle():
    cfg = {"comms_surface": "umwelt"}
    line = _baton._handoff_hint(cfg, "borges", "ari\x1b[31madne\n")
    assert line and "\x1b" not in line and "\n" not in line and "ari" in line, repr(line)
    big = _baton._handoff_hint(cfg, "borges", "a" * 5000)
    assert big and len(big) <= 300 and "\n" not in big, len(big or "")


def test_c4_the_hint_helper_is_total_it_never_raises():
    for cfg in (None, {}, {"comms_surface": object()}, "not-a-dict", 7):
        for to in ("ariadne", "", None, 5):
            _baton._handoff_hint(cfg, "borges", to)  # must not raise

def _advance_cfg():
    return {"agent_name": "example-agent-colleague", "forum": {"url": "http://test-forum:5002"},
            "comms_surface": "umwelt"}


def _advance(server_turn, to=None, caller="example-agent-colleague"):
    c = _FakeClient(post_result={"advanced": True, "turn": server_turn,
                                 "ci": {"state": "green", "detail": "x"},
                                 "approval": {"state": "approved_current", "detail": "ok"}, "warnings": []})
    _baton.cmd_advance(_advance_args(to=to), _advance_cfg(), caller, c)


def test_c2_advance_whose_server_turn_is_the_caller_prints_no_hint(capsys):
    """The caller is blanked in the mutant `_handoff_hint(config, "", ...)`: an advance that lands the turn
    on the caller itself would then hint at itself."""
    _advance("example-agent-colleague")
    assert HINT_TOKEN not in capsys.readouterr().err


def test_c1_advance_hint_names_the_servers_turn_not_the_requested_to(capsys):
    """`out.get("turn") or to_agent`: the server's answer wins. Mutant: use the requested --to."""
    _advance("example-agent-server-chosen", to="example-agent-requested")
    hints = _hint_lines(capsys.readouterr().err)
    assert len(hints) == 1 and "example-agent-server-chosen" in hints[0] and "example-agent-requested" not in hints[0]


def test_c1_advance_hint_falls_back_to_the_requested_to_when_the_server_names_no_turn(capsys):
    c = _FakeClient(post_result={"advanced": True, "ci": {}, "approval": {}, "warnings": []})
    _baton.cmd_advance(_advance_args(to="example-agent-requested"), _advance_cfg(), "example-agent-colleague", c)
    hints = _hint_lines(capsys.readouterr().err)
    assert len(hints) == 1 and "example-agent-requested" in hints[0]
