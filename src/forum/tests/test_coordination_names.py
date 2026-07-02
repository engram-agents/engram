"""Tests for coordination.names — the agent-name validation SSoT (#1468).

Covers the validator + the InvalidAgentName type, plus the authoritative
chokepoint behavior: dm_thread_key RAISES on an invalid name (so no caller —
HTTP route, ia dm CLI, read or write — can route around the +-collision guard),
and forum.db.is_valid_agent_name is the SAME object (re-export, no drift).
"""

import pytest

from forum.coordination import dm_thread_key
from forum.coordination.names import InvalidAgentName, is_valid_agent_name


# ---------------------------------------------------------------------------
# the validator
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["ariadne", "borges", "sol", "agent-2", "luria_x", "clio9", "a"])
def test_valid_names_accepted(name):
    assert is_valid_agent_name(name)


@pytest.mark.parametrize("name", ["a+b", "a b", "a/b", "a.b", "a:b", "", "-a", "_a", "A", "ariadne+sol"])
def test_invalid_names_rejected(name):
    assert not is_valid_agent_name(name)


def test_length_cap_63():
    assert is_valid_agent_name("a" * 63)       # 1 + 62
    assert not is_valid_agent_name("a" * 64)   # over the {0,62} tail


def test_invalid_agent_name_is_valueerror():
    assert issubclass(InvalidAgentName, ValueError)


# ---------------------------------------------------------------------------
# dm_thread_key — the authoritative chokepoint raise
# ---------------------------------------------------------------------------
def test_dm_thread_key_valid_pair_ok():
    assert dm_thread_key("ariadne", "sol") == "ariadne+sol"


@pytest.mark.parametrize("a,b", [("a+b", "sol"), ("ariadne", "b+c"), ("a b", "sol"), ("ariadne", "x/y")])
def test_dm_thread_key_raises_on_invalid(a, b):
    # The collision (a+b)+(c) == (a)+(b+c) is un-formable: the key-formation
    # chokepoint raises before a bad name can ever become a thread file.
    with pytest.raises(InvalidAgentName):
        dm_thread_key(a, b)


def test_dm_thread_key_raise_is_order_independent():
    # whichever arg carries the '+', the raise fires (names are sorted internally)
    with pytest.raises(InvalidAgentName):
        dm_thread_key("sol", "a+b")


# ---------------------------------------------------------------------------
# forum.db re-export is the SAME object (one definition — no drift)
# ---------------------------------------------------------------------------
def test_db_is_a_re_export_not_a_copy():
    from forum import db
    assert db.is_valid_agent_name is is_valid_agent_name
