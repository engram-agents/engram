"""#3638: the SERVER refuses `baton init` for a PR baton whose PR does not exist
on GitHub, or whose author is mapped only to agents who are not participants.

Written BEFORE the implementation (contract-first). The defect: `baton init
PR-3637 --repo ...` was a typo for 3635; the baton squatted an id a real PR got
an hour later, and the anchor-must-match-N rule means nothing can repair it.

Contract (checked at creation only; nothing stored on refusal; the check runs
AFTER every local refusal and BEFORE the write):
  C1  PR absent                          -> 422, nothing stored, names <repo>#<N>.
  C2  author maps only to non-participants -> 422, nothing stored.
  C3  author maps to a participant       -> 201 as today.
  C4  author unmappable / no map         -> 201 as today (existence alone).
  C5  GitHub unreadable ("unknown")      -> 422, FAIL CLOSED, reason + retry.
  C6  GITHUB_INTEGRATION == "off"        -> no call, 201, one warning.
  C7  a bare pr/<N> that cannot be qualified (no map / prefix unmapped) keeps the
      existing 422 and makes no GitHub call. (The server already refuses it
      before this check, so there is no "skip" branch.)
  C8  non-PR batons                      -> no call, unchanged.
  C9  locally refused first (#3366)      -> original text, zero calls.
  C10 `baton_gate._pr_identity` unit contract.

Identities and repo names are SYNTHETIC.
"""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src/forum/tests")
_TOOLS = str(Path(__file__).resolve().parents[3] / "src" / "engram" / "tools")
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE, OTHER, REPO  # noqa: E402

from forum.coordination.markdown import parse_frontmatter  # noqa: E402

PID = "PR-9001"
ANCHOR = f"pr/{REPO}/9001"
AUTHOR_LOGIN = "example-gh-author"
OTHER_LOGIN = "example-gh-other"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def world_nomap(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, with_map=False)


class Identity:
    """A recording stand-in for ``baton_gate._pr_identity``."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, pr_number, repo):
        self.calls.append((pr_number, repo))
        return self.result


def _stub(world, monkeypatch, result):
    s = Identity(result)
    monkeypatch.setattr(world.gate, "_pr_identity", s, raising=False)
    return s


def _post(world, pid=PID, github=ANCHOR, **extra):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR,
    }
    if github is not None:
        body["github"] = github
    body.update(extra)
    return world.client.post("/api/projects", json=body)


def _nothing_created(world, pid=PID):
    assert world.store.read_project(pid) is None
    assert world.client.get(f"/api/projects/{pid}").status_code == 404


# C1 ------------------------------------------------------------------------
def test_c1_absent_pr_is_422_nothing_stored(world, monkeypatch):
    s = _stub(world, monkeypatch, ("absent", "Could not resolve to a PullRequest"))
    r = _post(world)
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    assert f"{REPO}#9001" in err
    assert "does not exist on GitHub" in err
    assert "check the number" in err.lower()
    assert "merge" not in err.lower()           # must not suggest the fake-sha merge route
    assert s.calls == [("9001", REPO)]
    _nothing_created(world)


def test_c1_refusal_leaves_the_id_free(world, monkeypatch):
    _stub(world, monkeypatch, ("absent", "x"))
    assert _post(world).status_code == 422
    _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    assert _post(world).status_code == 201


# C2 ------------------------------------------------------------------------
def test_c2_author_mapped_to_non_participant_is_422(world, monkeypatch):
    _stub(world, monkeypatch, ("exists", OTHER_LOGIN))
    r = _post(world)
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    assert OTHER_LOGIN in err and OTHER in err
    assert "not on the baton" in err and "check the number" in err.lower()
    _nothing_created(world)


def test_c2_reverse_lookup_is_case_insensitive(world, monkeypatch):
    _stub(world, monkeypatch, ("exists", OTHER_LOGIN.upper()))
    assert _post(world).status_code == 422
    _nothing_created(world)


def test_c2_colleague_is_in_the_checked_participant_set(world, monkeypatch):
    # COLLEAGUE is added to participants by the colleague dedup even when the
    # participants list omits it; a PR authored by the colleague is theirs.
    _stub(world, monkeypatch, ("exists", "example-gh-colleague"))
    r = _post(world, participants=[AUTHOR, SENTINEL])
    assert r.status_code == 201, r.get_json()


# C3 ------------------------------------------------------------------------
def test_c3_author_mapped_to_participant_is_201_as_today(world, monkeypatch):
    s = _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    r = _post(world)
    assert r.status_code == 201, r.get_json()
    assert s.calls == [("9001", REPO)]
    fm = parse_frontmatter(world.store.read_project(PID))[0]
    assert fm["phase"] == "fairy"
    assert fm["github"] == ANCHOR


# C4 ------------------------------------------------------------------------
def test_c4_unmapped_author_is_201(world, monkeypatch):
    _stub(world, monkeypatch, ("exists", "someone-not-in-the-map"))
    assert _post(world).status_code == 201


def test_c4_no_login_map_is_201_existence_alone(world_nomap, monkeypatch):
    s = _stub(world_nomap, monkeypatch, ("exists", OTHER_LOGIN))
    r = _post(world_nomap)          # qualified anchor: no map needed
    assert r.status_code == 201, r.get_json()
    assert len(s.calls) == 1


def test_c4_no_login_map_absent_pr_still_refused(world_nomap, monkeypatch):
    _stub(world_nomap, monkeypatch, ("absent", "x"))
    assert _post(world_nomap).status_code == 422
    _nothing_created(world_nomap)


# C5 ------------------------------------------------------------------------
def test_c5_unknown_fails_closed(world, monkeypatch):
    _stub(world, monkeypatch, ("unknown", "gh timed out after 15s"))
    r = _post(world)
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    assert "gh timed out after 15s" in err and "retry" in err.lower()
    _nothing_created(world)


# C6 ------------------------------------------------------------------------
def test_c6_integration_off_skips_with_warning(world, monkeypatch, caplog):
    world.app.config["GITHUB_INTEGRATION"] = "off"
    s = _stub(world, monkeypatch, ("absent", "would refuse"))
    with caplog.at_level(logging.WARNING):
        r = _post(world)
    assert r.status_code == 201, r.get_json()
    assert s.calls == []
    warns = [rec for rec in caplog.records if rec.levelno == logging.WARNING
             and "PR-9001" in rec.getMessage() and "off" in rec.getMessage()]
    assert len(warns) == 1, [rec.getMessage() for rec in caplog.records]


# C7 ------------------------------------------------------------------------
def test_c7_bare_anchor_no_map_keeps_existing_422_no_call(world_nomap, monkeypatch):
    s = _stub(world_nomap, monkeypatch, ("exists", AUTHOR_LOGIN))
    r = _post(world_nomap, github="pr/9001")
    assert r.status_code == 422
    assert "cannot be qualified" in r.get_json()["error"]
    assert s.calls == []
    _nothing_created(world_nomap)


def test_c7_bare_anchor_prefix_not_in_map_keeps_existing_422_no_call(world, monkeypatch):
    s = _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    r = _post(world, pid="FOO-PR-9001", github="pr/9001")
    assert r.status_code == 422
    assert "cannot be qualified" in r.get_json()["error"]
    assert s.calls == []
    _nothing_created(world, "FOO-PR-9001")


def test_c7_bare_anchor_with_bound_prefix_is_checked_against_the_bound_repo(world, monkeypatch):
    s = _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    r = _post(world, github="pr/9001")
    assert r.status_code == 201, r.get_json()
    assert s.calls == [("9001", REPO)]


# C8 ------------------------------------------------------------------------
@pytest.mark.parametrize("pid,github", [
    ("bunch-x", None),
    ("DESIGN-x", None),
    ("pool-x", None),
    ("bunch-y", "project/7"),
    ("DESIGN-y", f"project/{SENTINEL}/7"),
])
def test_c8_non_pr_batons_make_no_call(world, monkeypatch, pid, github):
    s = _stub(world, monkeypatch, ("absent", "would refuse"))
    r = _post(world, pid=pid, github=github)
    assert r.status_code == 201, r.get_json()
    assert s.calls == []


# C9 ------------------------------------------------------------------------
def test_c9_anchorless_pr_keeps_3366_text_and_makes_no_call(world, monkeypatch):
    s = _stub(world, monkeypatch, ("absent", "would refuse"))
    r = _post(world, pid="PR-autopull2", github=None)
    assert r.status_code == 422
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in r.get_json()["error"]
    assert s.calls == []


def test_c9_wrong_number_binding_refusal_makes_no_call(world, monkeypatch):
    s = _stub(world, monkeypatch, ("absent", "would refuse"))
    r = _post(world, github=f"pr/{REPO}/1234")
    assert r.status_code == 422
    assert s.calls == []


# C10 -----------------------------------------------------------------------
class _Spawns:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.result


@pytest.fixture
def gate():
    import baton_gate
    return baton_gate


def _read(gate, monkeypatch, result):
    sp = _Spawns(result)
    monkeypatch.setattr(gate, "_gh_read_json", sp)
    return sp


def test_c10_not_found_is_absent(gate, monkeypatch):
    sp = _read(gate, monkeypatch, (None, "GraphQL: Could not resolve to a PullRequest with the number of 9."))
    kind, _ = gate._pr_identity("9", REPO)
    assert kind == "absent"
    assert sp.calls == [["pr", "view", "9", "--json", "number,author", "--repo", REPO]]


@pytest.mark.parametrize("reason", [
    "gh timed out after 15s", "gh not found on PATH", "gh: authentication required",
    "gh returned unparseable JSON",
])
def test_c10_other_failures_are_unknown(gate, monkeypatch, reason):
    _read(gate, monkeypatch, (None, reason))
    kind, why = gate._pr_identity("9", REPO)
    assert kind == "unknown" and reason in why


@pytest.mark.parametrize("number,repo", [
    ("9", "not a slug"), ("9", "owner/.."), ("9", None), ("x9", REPO), ("", REPO), ("٣", REPO),
])
def test_c10_invalid_inputs_are_unknown_without_a_spawn(gate, monkeypatch, number, repo):
    sp = _read(gate, monkeypatch, ({"author": {"login": "a"}}, None))
    kind, _ = gate._pr_identity(number, repo)
    assert kind == "unknown"
    assert sp.calls == []


def test_c10_author_login_is_lowercased(gate, monkeypatch):
    _read(gate, monkeypatch, ({"number": 9, "author": {"login": "Example-GH-Author"}}, None))
    assert gate._pr_identity("9", REPO) == ("exists", "example-gh-author")


@pytest.mark.parametrize("body", [
    {"number": 9}, {"number": 9, "author": None}, {"number": 9, "author": {}},
    {"number": 9, "author": {"login": 7}}, {"number": 9, "author": "x"}, [], "str",
])
def test_c10_missing_or_ill_typed_author_is_unknown(gate, monkeypatch, body):
    _read(gate, monkeypatch, (body, None))
    assert gate._pr_identity("9", REPO)[0] == "unknown"


def test_c10_never_raises_through_the_real_reader(gate, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("gh", 15)
    monkeypatch.setattr(subprocess, "run", boom)
    assert gate._pr_identity("9", REPO)[0] == "unknown"


# Fold 1 (reviewer round 1) -------------------------------------------------
def _break_map(world):
    """The map is still CONFIGURED (cfg path set) but no longer loads."""
    world.map_path.write_text("{ this is not json")


# F1a: a configured but unloadable map refuses BEFORE any GitHub read.
def test_f1a_configured_unloadable_map_is_422_before_any_github_read(world, monkeypatch):
    s = _stub(world, monkeypatch, ("exists", OTHER_LOGIN))
    _break_map(world)
    r = _post(world)
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    assert "login map" in err.lower()
    assert s.calls == []
    _nothing_created(world)


# F1b: no map configured stays existence-only (C4); F1c: integration off needs no map.
def test_f1c_integration_off_with_unloadable_map_is_201_no_call(world, monkeypatch):
    world.app.config["GITHUB_INTEGRATION"] = "off"
    s = _stub(world, monkeypatch, ("absent", "would refuse"))
    _break_map(world)
    r = _post(world)
    assert r.status_code == 201, r.get_json()
    assert s.calls == []


# F2a: an id that already exists gets today's answer with zero GitHub reads.
@pytest.mark.parametrize("second", [("absent", "gone"), ("unknown", "gh timed out")])
def test_f2a_existing_id_is_409_with_no_github_read(world, monkeypatch, second):
    s = _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    assert _post(world).status_code == 201
    s2 = _stub(world, monkeypatch, second)
    r = _post(world)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == f"project already exists: {PID}"
    assert s2.calls == []


def test_f2a_case_variant_of_existing_id_is_409_with_no_github_read(world, monkeypatch):
    _stub(world, monkeypatch, ("exists", AUTHOR_LOGIN))
    assert _post(world).status_code == 201
    s2 = _stub(world, monkeypatch, ("unknown", "gh timed out"))
    r = _post(world, pid="pr-9001")
    assert r.status_code == 409, r.get_json()
    assert "case-insensitive" in r.get_json()["error"]
    assert s2.calls == []


# F3: the request reader does NOT lowercase `colleague` (kind "id"), so the
# refusal's own lower-casing is what makes a mixed-case colleague count.
def test_f3_mixed_case_colleague_counts_as_a_participant(world, monkeypatch):
    _stub(world, monkeypatch, ("exists", "example-gh-colleague"))
    r = _post(world, participants=[AUTHOR, SENTINEL], colleague=COLLEAGUE.upper())
    assert r.status_code == 201, r.get_json()
