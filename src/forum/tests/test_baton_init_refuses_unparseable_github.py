"""#3377: POST /api/projects refuses a PRESENT ``github`` that does not parse, for EVERY id.

Written BEFORE the implementation (contract-first). #3374 closed the hole for
PR-shaped ids (an anchorless / junk-anchored PR baton). The same junk string was
still stored verbatim as the anchor of a BUNCH-classified id (``DESIGN-x`` +
``garbage`` -> 201, phase ``pool``), where gc / flip / board_projects later read
an anchor ``_parse_github_anchor`` returns None for. Kepler's ruling (19:1xZ,
binding): one rule for all ids -- a PRESENT github must parse.

Contract (no bypass field; creation only; nothing stored on refusal):
  1. non-PR id (``DESIGN-x``, ``pool-x``, ``XPR-5``) + unparseable github
     (``garbage``, ``pr/``, ``pr/o/r/``, ``xyz/5``, ``project/``) -> 422 whose
     text names the three accepted forms; nothing stored; with
     transition_phase absent AND ``pool``.
  2. the same ids with github absent, ``''``, whitespace, null -> 201 pool
     (an anchorless bunch is legitimate).
  3. ``project/7`` / ``PROJECT/7`` -> 201 pool; ``DESIGN-x`` + ``pr/o/r/5``
     -> 201 fairy (anchor-first, unchanged).
  4. PR-shaped ids: #3374 behaviour unchanged (its own test file stays green).
  5. ordering: the refusal runs BEFORE the transition_phase allow-list.
  6. gate-unavailable branch: the same rule, with a LOCAL anchor-shape check.
  7. a non-string github keeps its existing 422.
  8. (see test_baton_migrate_unparseable_github.py) the migration tool FAILs a
     junk row and never rewrites it.
  9. one constant (``baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS``) is the wording
     shared by the server 422 and the CLI ``baton init --github`` refusal.

Environment-independent: the pool sentinel is pinned by ``World`` / monkeypatch.
Identities and repo names are SYNTHETIC.
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src/forum/tests")
# append (not insert(0)): tools/ also holds a flat forum.py that must not shadow the package.
_TOOLS = str(Path(__file__).resolve().parents[3] / "src" / "engram" / "tools")
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE, REPO  # noqa: E402

from forum.coordination.markdown import parse_frontmatter  # noqa: E402

NON_PR_IDS = ["DESIGN-x", "pool-x", "XPR-5"]
JUNK = ["garbage", "pr/", "pr/o/r/", "xyz/5", "project/"]
_ABSENT = object()


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _post(world, pid, github=_ABSENT, **extra):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR,
    }
    if github is not _ABSENT:
        body["github"] = github          # may be None -> JSON null
    body.update(extra)
    return world.client.post("/api/projects", json=body)


def _nothing_created(world, pid):
    assert world.store.read_project(pid) is None
    assert world.client.get(f"/api/projects/{pid}").status_code == 404


def _fm(world, pid):
    return parse_frontmatter(world.store.read_project(pid))[0]


def _names_three_forms(err):
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS as FORMS
    assert FORMS in err, err
    assert "pr/<owner>/<repo>/<N>" in err
    assert "pr/<N>" in err
    assert "project/<N>" in err


# 1 -----------------------------------------------------------------------
@pytest.mark.parametrize("tp", [None, "pool"])
@pytest.mark.parametrize("bad", JUNK)
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_unparseable_github_on_non_pr_id_is_422_naming_accepted_forms(world, pid, bad, tp):
    extra = {} if tp is None else {"transition_phase": tp}
    r = _post(world, pid, bad, **extra)
    assert r.status_code == 422, (pid, bad, tp, r.get_json())
    err = r.get_json()["error"]
    _names_three_forms(err)
    assert repr(bad) in err                       # names the offending value
    assert pid in err
    _nothing_created(world, pid)


def test_unparseable_value_echo_is_truncated(world):
    junk = "x" * 200
    r = _post(world, "DESIGN-x", junk)
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert junk not in err and "x" * 60 in err and "x" * 61 not in err
    _nothing_created(world, "DESIGN-x")


def test_refusal_leaves_the_id_free_for_a_correct_init(world):
    assert _post(world, "DESIGN-x", "garbage").status_code == 422
    _nothing_created(world, "DESIGN-x")
    assert _post(world, "DESIGN-x", "project/7").status_code == 201


# 2 -----------------------------------------------------------------------
@pytest.mark.parametrize("val", [_ABSENT, "", "   ", "\t", None])
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_absent_empty_blank_null_github_on_non_pr_id_still_201_pool(world, pid, val):
    r = _post(world, pid, val)
    assert r.status_code == 201, (pid, val, r.get_json())
    fm = _fm(world, pid)
    assert fm["phase"] == "pool"
    assert not fm.get("github")                   # an anchorless bunch stores no anchor


# 3 -----------------------------------------------------------------------
@pytest.mark.parametrize("good", ["project/7", "PROJECT/7"])
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_project_anchor_on_non_pr_id_is_201_pool(world, pid, good):
    r = _post(world, pid, good)
    assert r.status_code == 201, (pid, good, r.get_json())
    assert _fm(world, pid)["phase"] == "pool"


@pytest.mark.parametrize("anchor", ["pr/o/r/5", "PR/O/R/5"])
def test_pr_anchor_on_non_pr_id_is_anchor_first_fairy(world, anchor):
    r = _post(world, "DESIGN-x", anchor)
    assert r.status_code == 201, r.get_json()
    assert _fm(world, "DESIGN-x")["phase"] == "fairy"


# 5 -----------------------------------------------------------------------
@pytest.mark.parametrize("tp", ["merge", "bogus", "peer"])
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_anchor_refusal_precedes_the_phase_allow_list(world, pid, tp):
    r = _post(world, pid, "garbage", transition_phase=tp)
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    _names_three_forms(err)
    assert "allowed here" not in err, err         # not the allow-list's text
    _nothing_created(world, pid)


def test_no_caller_settable_bypass(world):
    for extra in ({"skip_anchor_check": True}, {"force": True}, {"phase": "pool"}):
        r = _post(world, "DESIGN-x", "garbage", **extra)
        assert r.status_code in (422,), (extra, r.get_json())
        assert r.get_json()["error"], extra
        _nothing_created(world, "DESIGN-x")


# 4 -----------------------------------------------------------------------
def test_pr_shaped_refusal_keeps_the_two_route_hint_not_the_bunch_text(world):
    r = _post(world, "PR-autopull2", "garbage")
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in err      # #3374 text, unchanged
    _nothing_created(world, "PR-autopull2")


# 6 gate unavailable --------------------------------------------------------
@pytest.fixture
def no_gate():
    from forum import server as server_mod
    saved = list(server_mod._BATON_GATE_MODULE_CACHE)
    server_mod._BATON_GATE_MODULE_CACHE[:] = [None]
    yield
    server_mod._BATON_GATE_MODULE_CACHE[:] = saved


@pytest.mark.parametrize("tp", ["pool", "bogus"])
@pytest.mark.parametrize("bad", JUNK)
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_gate_unavailable_junk_github_on_non_pr_id_is_422(world, no_gate, pid, bad, tp):
    r = _post(world, pid, bad, transition_phase=tp)
    assert r.status_code == 422, (pid, bad, tp, r.get_json())
    _names_three_forms(r.get_json()["error"])
    _nothing_created(world, pid)


@pytest.mark.parametrize("val", [_ABSENT, "", "   ", None])
def test_gate_unavailable_absent_github_on_non_pr_id_still_201(world, no_gate, val):
    r = _post(world, "DESIGN-x", val, transition_phase="pool")
    assert r.status_code == 201, r.get_json()


@pytest.mark.parametrize("good", ["project/7", "PROJECT/7"])
def test_gate_unavailable_parseable_github_on_non_pr_id_still_201(world, no_gate, good):
    r = _post(world, "DESIGN-x", good, transition_phase="pool")
    assert r.status_code == 201, r.get_json()


# 7 non-string github keeps its existing 422 ---------------------------------
@pytest.mark.parametrize("val", [123, True, ["pr/o/r/5"], {"a": 1}])
@pytest.mark.parametrize("pid", ["DESIGN-x", "PR-5"])
def test_non_string_github_keeps_its_existing_422(world, pid, val):
    r = _post(world, pid, val)
    assert r.status_code == 422, (pid, val, r.get_json())
    err = r.get_json()["error"]
    assert "github" in err
    assert "project/<N>" not in err                # the shared field reader's 422, not the new rule
    _nothing_created(world, pid)


def test_server_fallback_constant_equals_the_gate_constant():
    """The gate-unavailable branch carries a local copy of the wording; it must not drift."""
    from forum import server as server_mod
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS as FORMS
    assert server_mod._ACCEPTED_FORMS_FALLBACK == FORMS


def test_gate_unavailable_refusal_text_carries_the_accepted_forms(world, no_gate):
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS as FORMS
    r = _post(world, "DESIGN-x", "garbage", transition_phase="pool")
    assert r.status_code == 422
    assert FORMS in r.get_json()["error"]


# Fold (Ariadne, #3384): STORE the normalised (stripped + lowercased) anchor -------
# `_parse_github_anchor` is case-sensitive and nine readers parse the STORED value
# without lowercasing, so validating the lowercased value but storing the raw one
# let 'Project/7' through as an anchor every reader reads as "no anchor".
def _stored(world, pid):
    return _fm(world, pid).get("github")


def _reads(stored):
    from baton_gate import _parse_github_anchor
    return _parse_github_anchor(stored)


@pytest.mark.parametrize("raw", ["Project/7", "PROJECT/7", " Project/7 "])
@pytest.mark.parametrize("pid", NON_PR_IDS)
def test_mixed_case_project_anchor_is_stored_normalised_and_readers_parse_it(world, pid, raw):
    r = _post(world, pid, raw)
    assert r.status_code == 201, (pid, raw, r.get_json())
    stored = _stored(world, pid)
    assert stored == "project/7", stored
    assert _reads(stored) == ("project", None, "7")


@pytest.mark.parametrize("raw", ["Project/7", " PROJECT/7 "])
def test_gate_unavailable_mixed_case_project_anchor_is_stored_normalised(world, no_gate, raw):
    r = _post(world, "DESIGN-x", raw, transition_phase="pool")
    assert r.status_code == 201, r.get_json()
    assert _stored(world, "DESIGN-x") == "project/7"


@pytest.mark.parametrize("raw", ["PR/O/R/5", "Pr/o/R/5", " pr/O/r/5 "])
def test_mixed_case_qualified_pr_anchor_is_stored_normalised(world, raw):
    r = _post(world, "umwelt-pr-5", raw)
    assert r.status_code == 201, (raw, r.get_json())
    stored = _stored(world, "umwelt-pr-5")
    assert stored == "pr/o/r/5", stored
    assert _reads(stored) == ("pr", "o/r", "5")


def test_unqualified_mixed_case_pr_anchor_is_qualified_from_the_map_as_before(world):
    # the pre-existing qualification path (admin map by id prefix) already rewrites it
    r = _post(world, "PR-5", "PR/5")
    assert r.status_code == 201, r.get_json()
    assert _stored(world, "PR-5") == f"pr/{REPO}/5"


@pytest.mark.parametrize("val", ["project/7", "pr/o/r/5", f"pr/{REPO}/5", "pr/example-org/example-repo/12"])
def test_already_lowercase_values_are_stored_byte_identical(world, val):
    r = _post(world, "DESIGN-x", val)
    assert r.status_code == 201, r.get_json()
    assert _stored(world, "DESIGN-x") == val


def test_gate_unavailable_lowercase_value_stored_byte_identical(world, no_gate):
    assert _post(world, "DESIGN-x", "project/7", transition_phase="pool").status_code == 201
    assert _stored(world, "DESIGN-x") == "project/7"


# Parity: the gate-unavailable local regex must accept exactly what the gate parses ---
PARITY_CORPUS = [
    "pr/5", "pr/0", "pr/123456789012345", "pr/1234567890123456", "pr/o/r/5", "pr/o/r/",
    "pr/o/r", "pr/o/5", "pr/o/r/s/5", "pr//5", "pr/o//5", "pr/-1", "pr/o.x/r-y/5",
    "pr/o_x/r_y/5", "pr/o x/r/5", "pr/5/", "pr/o/r/5/", "project/7", "project/", "project",
    "project/0", "project/1234567890123456", "project/o/7", "issue/5", "xyz/5", "garbage",
    "pr", "pr/", "/pr/5", "pr/٥", "pr/5\n", "project/7 x", "pr/o/r/05", "",
]


@pytest.mark.parametrize("raw", PARITY_CORPUS)
def test_local_anchor_shape_regex_matches_the_gates_parser(raw):
    from forum import server as server_mod
    from baton_gate import _parse_github_anchor
    v = raw.strip().lower()          # exactly what the server feeds both
    if not v:
        return                       # empty is "absent", handled before either check
    assert bool(server_mod._ANCHOR_SHAPE_RE.fullmatch(v)) == (_parse_github_anchor(v) is not None), raw


# 9 one wording, both surfaces ------------------------------------------------
def test_constant_is_shared_by_server_and_cli(world, capsys, monkeypatch):
    import baton as _baton
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS as FORMS

    assert "pr/<owner>/<repo>/<N>" in FORMS and "pr/<N>" in FORMS and "project/<N>" in FORMS
    # server surface
    r = _post(world, "DESIGN-x", "garbage")
    assert r.status_code == 422 and FORMS in r.get_json()["error"]
    # CLI surface (client-side, before any POST): a junk --github on a NON-PR id
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
    args = argparse.Namespace(project_id="DESIGN-x", title="t",
                              participants="borges,ariadne", status="planning",
                              turn=None, github="garbage", repo=None, colleague=None)
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_init(args, {"mode": "multi", "agent_name": "borges", "primary_user": "casey"},
                        "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
    err = capsys.readouterr().err
    assert err.startswith("baton init: invalid --github value 'garbage'"), err
    assert FORMS in err, err


@pytest.mark.parametrize("val", ["", "   ", "pr/", "project/"])
def test_cli_refuses_empty_or_junk_github_on_non_pr_id_before_any_post(world, capsys, monkeypatch, val):
    """Measured before the change: the CLI already refuses these (an explicit empty
    --github fails the anchor regex too). Pinned so the two surfaces stay aligned."""
    import baton as _baton
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
    args = argparse.Namespace(project_id="DESIGN-x", title="t",
                              participants="borges,ariadne", status="planning",
                              turn=None, github=val, repo=None, colleague=None)
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_init(args, {"mode": "multi", "agent_name": "borges", "primary_user": "casey"},
                        "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
