"""#3863 step 3b: a RELATIVE ENGRAM_HOME on the forum host is a fail-closed 422, never a Flask 500.

tools/baton_gate.py (loaded by path by the server) carries a pinned copy of the ENGRAM-home resolver and raises
PoolSentinelUnresolved for a relative value; the server's handlers already map that type to a 422. This cell runs
the REAL resolver (nothing is monkeypatched) through one handler (a flip of a phase-less legacy baton, which needs
the sentinel) and checks the refusal text and that nothing changed.

Identities are SYNTHETIC.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import World, AUTHOR, SENTINEL  # noqa: E402
from test_baton_anchor_binding import disk, seq_state  # noqa: E402

LEGACY_PID = "PR-9401"
_GATE_FILE = Path(__file__).resolve().parents[2] / "engram" / "tools" / "baton_gate.py"


def _real_resolver_bound_to(gate):
    """The World fixture replaces gate._resolve_pool_sentinel with a stub. Rebuild the REAL function from the file
    and bind it to the server's own gate module (its globals), so it raises that module's PoolSentinelUnresolved
    (the class the handlers catch) and uses that module's cache."""
    spec = importlib.util.spec_from_file_location("_gate_real_3863", _GATE_FILE)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    f = fresh._resolve_pool_sentinel
    return types.FunctionType(f.__code__, gate.__dict__, f.__name__, f.__defaults__)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_a_relative_engram_home_is_a_422_naming_the_value_and_changes_nothing(world, monkeypatch):
    world.seed(LEGACY_PID, phase=None, github=None, turn=AUTHOR)
    before, seq0 = disk(world, LEGACY_PID), seq_state(world)
    monkeypatch.setattr(world.gate, "_resolve_pool_sentinel", _real_resolver_bound_to(world.gate))
    monkeypatch.setenv("ENGRAM_HOME", "rel/x")
    world.gate._POOL_SENTINEL_CACHE.clear()
    resp = world.flip(LEGACY_PID, agent=AUTHOR, to=SENTINEL, reason="legacy flip")
    assert resp.status_code == 422, (resp.status_code, resp.get_json())
    assert "ENGRAM_HOME must be an absolute path, got 'rel/x'" in resp.get_json()["error"], resp.get_json()
    assert disk(world, LEGACY_PID) == before and seq_state(world) == seq0
