"""Contract for tripwire._infer_approver on malformed config values (#4146).

Written BEFORE the fix (contract-first). Invariant: `_infer_approver` either
returns a non-empty `str` or exits with EXIT_ERROR and a stderr message. It
never raises anything else and never returns a non-string.

Cells:
  C1 self_lineage absent / None / non-string (42, ["opus"]) -> EXIT_ERROR with
     the "cannot infer approver from self_lineage=" message. A list holding
     "opus" is NOT opus.
  C2 opus lineage, agent_name absent / None / "" / non-string -> EXIT_ERROR,
     "missing 'agent_name'" message.
  C3 sonnet lineage, peer absent / None / "" / non-string -> EXIT_ERROR,
     "no 'peer' field" message.
  C4 happy paths unchanged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools" / "tripwire.py"
_spec = importlib.util.spec_from_file_location("_tripwire_4146", _SRC)
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)

_ABSENT = object()


def _cfg(**kw):
    return {k: v for k, v in kw.items() if v is not _ABSENT}


def _exits(config, capsys, needle):
    with pytest.raises(SystemExit) as ei:
        tw._infer_approver(config)
    assert ei.value.code == tw.EXIT_ERROR
    assert needle in capsys.readouterr().err


@pytest.mark.parametrize("lineage", [_ABSENT, None, 42, ["opus"], {"opus": 1}])
def test_c1_bad_lineage_exits_cannot_infer(lineage, capsys):
    cfg = _cfg(self_lineage=lineage, agent_name="halley", peer="luria")
    _exits(cfg, capsys, "cannot infer approver from self_lineage=")


@pytest.mark.parametrize("name", [_ABSENT, None, "", 42, ["x"], True, 0])
def test_c2_opus_bad_agent_name_exits(name, capsys):
    cfg = _cfg(self_lineage="anthropic:opus", agent_name=name)
    _exits(cfg, capsys, "missing 'agent_name'")


@pytest.mark.parametrize("peer", [_ABSENT, None, "", 42, ["x"], True, 0])
def test_c3_sonnet_bad_peer_exits(peer, capsys):
    cfg = _cfg(self_lineage="anthropic:sonnet", peer=peer)
    _exits(cfg, capsys, "no 'peer' field")


def test_c4_happy_paths():
    assert tw._infer_approver({"self_lineage": "anthropic:opus", "agent_name": "halley"}) == "halley"
    assert tw._infer_approver({"self_lineage": "anthropic:sonnet", "peer": "luria"}) == "luria"
