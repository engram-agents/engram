"""Contract tests for #3039 U2b -- the `peer -> merge` gate primitives in
tools/baton_gate.py (src/engram/tools/baton_gate.py).

Written BEFORE the implementation (contract-first). Covers:

  * the login-map loader (`_load_login_map`): schema cells, ownership / mode /
    symlink cells, the open-then-fstat (swap) cell, the unconfigured cell
    (spec cases T1, T2, T3);
  * the per-reviewer verdict (`_reviewer_approval_state`) against a fake `gh`
    at the subprocess boundary: T5-T16 plus the deterministic-ordering cells;
  * T23 (shipped-code scrub), T24 (D2/C4 limitation sentences in --help and the
    protocol doc), and the "no default map path in code" rule (D3).

The server / write-body cells (T17-T22) live in
src/forum/tests/test_baton_advance_api.py.

Identities here are SYNTHETIC. Real fleet identities must not appear in any
shipped file or test.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent
_TOOLS_DIR = _ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton_gate  # noqa: E402

PR = "9001"
REPO = "example-org/example-repo"
HEAD = "a" * 40
OLD = "b" * 40

RUNNING_AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


# ---------------------------------------------------------------------------
# Login-map loader
# ---------------------------------------------------------------------------

def _good_map() -> dict:
    return {
        "version": 1,
        "map": {
            "Example-Agent-One": ["Example-GH-One"],
            "example-agent-two": ["example-gh-two", "example-gh-two-alt"],
        },
        "repos": {"PR": "example-org/example-repo", "Example-PR": "Example-Org/Other-Repo"},
    }


def _write_map(tmp_path: Path, payload, *, mode: int = 0o644, dir_mode: int = 0o755) -> Path:
    """Write payload under tmp_path/cfg/ and return the file path."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    os.chmod(cfg, dir_mode)
    f = cfg / "baton-login-map.json"
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    os.chmod(f, mode)
    return f


def _load(path, tmp_path, *, trusted=None, **kw):
    """Load with the test user's own uid as the injected ADMIN uid, and
    tmp_path as the trust anchor (production default is '/' with root only --
    tests cannot chown to root)."""
    if trusted is None:
        trusted = (os.getuid(),)
    return baton_gate._load_login_map(
        str(path) if path is not None else None,
        extra_trusted_uids=trusted,
        trust_anchor=str(tmp_path),
        **kw,
    )


class TestLoginMapSchema:
    def test_good_map_loads_and_lowercases_keys_and_logins(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        m = _load(f, tmp_path)
        assert m == {
            "example-agent-one": ["example-gh-one"],
            "example-agent-two": ["example-gh-two", "example-gh-two-alt"],
        }

    @pytest.mark.parametrize(
        "payload, needle",
        [
            ("{ not json", "not valid JSON"),
            ([1, 2], "must be a JSON object"),
            ({"version": 2, "map": {}}, "version"),
            ({"map": {}}, "version"),
            ({"version": 1, "map": []}, "'map' must be an object"),
            ({"version": 1}, "'map' must be an object"),
            ({"version": 1, "map": {"example-agent-one": []}}, "empty login list"),
            ({"version": 1, "map": {"example-agent-one": [""]}}, "non-empty string"),
            ({"version": 1, "map": {"example-agent-one": [5]}}, "non-empty string"),
            ({"version": 1, "map": {"example-agent-one": "example-gh-one"}}, "list of logins"),
            (
                {"version": 1, "map": {"example-agent-one": ["shared-login"],
                                       "example-agent-two": ["Shared-Login"]}},
                "more than one agent",
            ),
        ],
    )
    def test_bad_map_content_refuses_naming_cause(self, tmp_path, payload, needle):
        f = _write_map(tmp_path, payload)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert needle in str(ei.value)

    # -- A2 (round 4): the admin-owned `repos` table is part of the map file ----

    def test_repos_is_required(self, tmp_path):
        payload = _good_map()
        del payload["repos"]
        f = _write_map(tmp_path, payload)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "repos" in str(ei.value)

    @pytest.mark.parametrize("bad", [
        [], {}, ["PR"], "PR", 5, None, {"PR": ""}, {"PR": 5}, {"PR": "no-slash"}, {"PR": "a/b/c"},
        {"": "o/r"}, {"  ": "o/r"}, {"PR": "bad repo/x"}, {"PR": "o/r", "pr": "o/other"},
    ], ids=repr)
    def test_malformed_repos_refuses_naming_repos(self, tmp_path, bad):
        payload = _good_map()
        payload["repos"] = bad
        f = _write_map(tmp_path, payload)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "repos" in str(ei.value)

    def test_repos_does_not_change_the_agent_map_result(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        m = _load(f, tmp_path)
        assert dict(m) == {
            "example-agent-one": ["example-gh-one"],
            "example-agent-two": ["example-gh-two", "example-gh-two-alt"],
        }

    def test_missing_file_refuses(self, tmp_path):
        (tmp_path / "cfg").mkdir()
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(tmp_path / "cfg" / "nope.json", tmp_path)
        assert "baton-login-map" in str(ei.value) or "nope.json" in str(ei.value)

    def test_empty_login_list_names_the_agent(self, tmp_path):
        f = _write_map(tmp_path, {"version": 1, "map": {"example-agent-one": []}})
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "example-agent-one" in str(ei.value)


class TestLoginMapUnconfigured:
    """T3 / D3: no option configured => fail closed; there is NO default path."""

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_option_fails_closed(self, value):
        with pytest.raises(baton_gate.LoginMapError) as ei:
            baton_gate._load_login_map(value)
        assert "no login map configured" in str(ei.value)

    def test_no_default_path_in_shipped_python(self):
        for rel in ("src/engram/tools/baton_gate.py", "src/forum/server.py",
                    "src/engram/tools/baton.py", "src/forum/coordination/projects.py"):
            text = (_ROOT / rel).read_text()
            assert "/etc/engram-forum" not in text, rel


class TestLoginMapIntegrity:
    """T2 -- ownership / mode / symlink cells (D3 ownership rule: root or an
    explicitly named admin uid; NEVER the server's own uid by default)."""

    def test_pass_when_owner_is_a_named_admin_uid(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        assert _load(f, tmp_path)  # does not raise

    @pytest.mark.skipif(RUNNING_AS_ROOT, reason="root is always a trusted owner")
    def test_wrong_owner_leaf_refuses_when_uid_not_named(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        # No admin uid named: the test user's own uid must NOT be trusted by
        # default (the server runs as an agent account).
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path, trusted=())
        assert "owner" in str(ei.value).lower()

    @pytest.mark.skipif(RUNNING_AS_ROOT, reason="root is always a trusted owner")
    def test_other_named_uid_does_not_trust_this_owner(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        with pytest.raises(baton_gate.LoginMapError):
            _load(f, tmp_path, trusted=(os.getuid() + 1,))

    def test_group_writable_leaf_refuses(self, tmp_path):
        f = _write_map(tmp_path, _good_map(), mode=0o664)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "writable" in str(ei.value)

    def test_world_writable_leaf_refuses(self, tmp_path):
        f = _write_map(tmp_path, _good_map(), mode=0o666)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "writable" in str(ei.value)

    def test_group_writable_parent_dir_refuses_and_names_it(self, tmp_path):
        f = _write_map(tmp_path, _good_map(), dir_mode=0o775)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        msg = str(ei.value)
        assert "writable" in msg
        assert "cfg" in msg  # names the failing component

    def test_group_writable_grandparent_refuses(self, tmp_path):
        # The directory CHAIN is checked, not just the leaf's parent.
        outer = tmp_path / "outer"
        outer.mkdir()
        os.chmod(outer, 0o775)
        inner = outer / "inner"
        inner.mkdir()
        os.chmod(inner, 0o755)
        f = inner / "m.json"
        f.write_text(json.dumps(_good_map()))
        os.chmod(f, 0o644)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path)
        assert "outer" in str(ei.value)

    def test_symlinked_leaf_refuses(self, tmp_path):
        real = _write_map(tmp_path, _good_map())
        link = real.parent / "link.json"
        link.symlink_to(real)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(link, tmp_path)
        assert "symlink" in str(ei.value).lower()

    def test_symlinked_directory_component_refuses(self, tmp_path):
        real = _write_map(tmp_path, _good_map())
        linkdir = tmp_path / "linkdir"
        linkdir.symlink_to(real.parent, target_is_directory=True)
        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(linkdir / real.name, tmp_path)
        assert "symlink" in str(ei.value).lower()

    def test_non_regular_leaf_refuses(self, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        os.chmod(cfg, 0o755)
        d = cfg / "adir"
        d.mkdir()
        with pytest.raises(baton_gate.LoginMapError):
            _load(d, tmp_path)

    def test_relative_or_dotdot_path_refuses(self, tmp_path):
        f = _write_map(tmp_path, _good_map())
        with pytest.raises(baton_gate.LoginMapError):
            baton_gate._load_login_map("cfg/baton-login-map.json",
                                       extra_trusted_uids=(os.getuid(),),
                                       trust_anchor=str(tmp_path))
        sneaky = str(tmp_path / "cfg" / ".." / "cfg" / "baton-login-map.json")
        with pytest.raises(baton_gate.LoginMapError):
            _load(sneaky, tmp_path)

    def test_swap_between_walk_and_open_is_checked_on_the_open_fd(self, tmp_path):
        """The leaf is verified by fstat on the OPEN fd, never by a path stat.
        The seam `_before_leaf_open` runs after the directory walk and before the
        leaf is opened; swapping a group-writable file in there must be caught.
        A path-stat-then-open implementation (check-then-use) verifies the good
        file and then reads the swapped one."""
        f = _write_map(tmp_path, _good_map())
        evil = f.parent / "evil.json"
        evil.write_text(json.dumps({"version": 1, "map": {"intruder": ["intruder-gh"]}}))
        os.chmod(evil, 0o666)

        def swap():
            os.replace(evil, f)

        with pytest.raises(baton_gate.LoginMapError) as ei:
            _load(f, tmp_path, _before_leaf_open=swap)
        assert "writable" in str(ei.value)


# ---------------------------------------------------------------------------
# Verdict: `_reviewer_approval_state` against a fake `gh`
# ---------------------------------------------------------------------------

def rv(rid, login, state, oid=HEAD, ts="2026-10-01T10:00:00Z"):
    return {
        "id": rid,
        "state": state,
        "submittedAt": ts,
        "author": ({"login": login} if login is not None else None),
        "commit": ({"oid": oid} if oid is not None else None),
    }


def _rest_id(rid):
    """The REST review id is an integer; the fixtures name reviews with strings."""
    if isinstance(rid, str):
        return int.from_bytes(rid.encode("utf-8"), "big")
    return rid


def to_rest(node):
    """Translate a fixture review node (the old GraphQL field names) to the REST
    row the gate reads (#3541). A non-dict node is passed through unchanged and a
    deleted key stays deleted, so each test's malformed-input intent survives."""
    if not isinstance(node, dict):
        return node
    out = {}
    if "id" in node:
        out["id"] = _rest_id(node["id"])
    if "state" in node:
        out["state"] = node["state"]
    if "submittedAt" in node:
        out["submitted_at"] = node["submittedAt"]
    if "author" in node:
        out["user"] = node["author"]
    if "commit" in node:
        c = node["commit"]
        out["commit_id"] = c.get("oid") if isinstance(c, dict) else None
    return out


class FakeGh:
    """Stands in for subprocess.run at the baton_gate boundary.

    Contract with the implementation (#3541): `gh pr view <N> --json
    headRefOid,author` serves the head + PR author, `gh api
    repos/<repo>/pulls/<N>/reviews?per_page=100&page=K` serves the rows.
    `reviews` is in REST order (oldest first); the fake serves `page_size` per page.
    """

    def __init__(self, reviews, *, head=HEAD, author="example-gh-author", page_size=100):
        self.reviews = list(reviews)
        self.head = head
        self.author = author
        self.page_size = page_size
        self.calls = []
        self.mode = None  # None | notfound | timeout | nonzero | empty | garbage | ...

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        if self.mode == "notfound":
            raise FileNotFoundError("gh")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, 15)
        if self.mode == "nonzero":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        if self.mode == "empty":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if self.mode == "garbage":
            return subprocess.CompletedProcess(cmd, 0, stdout="<<not json>>", stderr="")
        path = cmd[2]
        if cmd[:2] == ["gh", "api"] and "/reviews?" in path:
            page_no = int(path.rsplit("page=", 1)[1])
            start = (page_no - 1) * self.page_size
            page = [to_rest(n) for n in self.reviews[start:start + self.page_size]]
            body = page
            if self.mode == "nodes_not_list":
                body = "oops"
            if self.mode == "no_pageinfo":
                body = {"message": "a page that is not a list"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")
        pr = {
            "headRefOid": self.head,
            "author": ({"login": self.author} if self.author is not None else None),
        }
        if self.mode == "no_head":
            del pr["headRefOid"]
        if self.mode == "no_author":
            del pr["author"]
        body = None if self.mode == "no_pr" else pr
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")


@pytest.fixture
def gh(monkeypatch):
    def install(reviews, **kw):
        fake = FakeGh(reviews, **kw)
        monkeypatch.setattr(subprocess, "run", fake)
        return fake
    return install


def state(login="example-gh-one"):
    return baton_gate._reviewer_approval_state(PR, REPO, login)[0]


class TestReviewerVerdict:
    def test_T5_approval_on_current_head_passes(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED")])
        assert state() == "approved_current"

    def test_T6_approve_then_changes_requested_same_head_refuses(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "CHANGES_REQUESTED", ts="2026-10-01T11:00:00Z")])
        assert state() == "changes_requested"

    def test_T6b_changes_requested_then_approve_passes(self, gh):
        gh([rv("R1", "example-gh-one", "CHANGES_REQUESTED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "APPROVED", ts="2026-10-01T11:00:00Z")])
        assert state() == "approved_current"

    def test_T7_approve_then_commented_still_passes(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "COMMENTED", ts="2026-10-01T11:00:00Z")])
        assert state() == "approved_current"

    def test_pending_review_carries_no_opinion(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "PENDING", ts="2026-10-01T11:00:00Z")])
        assert state() == "approved_current"

    def test_T8_approval_on_older_head_is_stale(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", oid=OLD)])
        assert state() == "stale_head"

    def test_approval_with_null_commit_is_stale(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", oid=None)])
        assert state() == "stale_head"

    def test_T9_latest_dismissed_refuses(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "DISMISSED", ts="2026-10-01T11:00:00Z")])
        assert state() == "dismissed"

    def test_no_review_from_this_login(self, gh):
        gh([rv("R1", "example-gh-two", "APPROVED")])
        assert state("example-gh-one") == "no_review"

    def test_T10_approval_by_a_different_login_does_not_count(self, gh):
        # The colleague maps to gh-one; gh-two approved. Binds the LOGIN.
        gh([rv("R1", "example-gh-two", "APPROVED"),
            rv("R2", "example-gh-one", "COMMENTED")])
        assert state("example-gh-one") == "no_review"

    def test_T11_approval_by_unmapped_account_does_not_count(self, gh):
        gh([rv("R1", "random-stranger", "APPROVED")])
        assert state("example-gh-one") == "no_review"

    def test_T12_reviewer_is_the_pr_author_refuses(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED")], author="Example-GH-One")
        assert state("example-gh-one") == "self_review"

    def test_T13_approval_only_on_page_three_passes(self, gh):
        others = [rv(f"X{i}", "example-gh-two", "COMMENTED", ts=f"2026-10-01T09:{i % 60:02d}:00Z")
                  for i in range(120)]
        fake = gh(others + [rv("R9", "example-gh-one", "APPROVED", ts="2026-10-01T12:00:00Z")])
        assert state() == "approved_current"
        assert len(fake.calls) >= 3

    def test_T13b_page_boundary_exactly_at_cap_is_complete(self, gh):
        cap = baton_gate._REVIEW_PAGE_CAP
        n = baton_gate._REVIEW_PAGE_SIZE * cap - 2  # + the approval = 999 rows: a short tenth page
        others = [rv(f"X{i}", "example-gh-two", "COMMENTED") for i in range(n)]
        fake = gh(others + [rv("R9", "example-gh-one", "APPROVED", ts="2026-10-01T12:00:00Z")])
        assert state() == "approved_current"
        assert len([c for c in fake.calls if c[:2] == ["gh", "api"]]) == cap

    def test_T14_page_cap_exceeded_with_more_pages_is_incomplete(self, gh):
        cap = baton_gate._REVIEW_PAGE_CAP
        others = [rv(f"X{i}", "example-gh-two", "COMMENTED")
                  for i in range(baton_gate._REVIEW_PAGE_SIZE * cap + 5)]
        fake = gh(others + [rv("R9", "example-gh-one", "APPROVED", ts="2026-10-01T12:00:00Z")])
        assert state() == "incomplete"
        # hard cap: no further pages fetched
        assert len([c for c in fake.calls if c[:2] == ["gh", "api"]]) == cap

    def test_has_next_page_without_cursor_is_not_complete(self, gh, monkeypatch):
        # REST has no cursor: the same hazard is a page that never stops being
        # full. It must never read as a complete walk (#3541).
        fake = gh([rv("R1", "example-gh-one", "APPROVED")])
        real = fake.__call__
        full = [to_rest(rv(f"X{i}", "example-gh-two", "COMMENTED")) for i in range(100)]

        def broken(cmd, *a, **kw):
            if cmd[:2] == ["gh", "api"] and "/reviews?" in cmd[2]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(full), stderr="")
            return real(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", broken)
        assert state() in ("incomplete", "unknown")

    @pytest.mark.parametrize(
        "mode",
        ["notfound", "timeout", "nonzero", "empty", "garbage", "no_head", "no_pr",
         "nodes_not_list", "no_pageinfo", "no_author"],
    )
    def test_T15_gh_failure_modes_are_unknown(self, gh, mode):
        fake = gh([rv("R1", "example-gh-one", "APPROVED")])
        fake.mode = mode
        assert state() == "unknown"

    def test_malformed_review_node_is_unknown(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED"), "not-a-dict"])
        assert state() == "unknown"

    def test_review_node_missing_state_is_unknown(self, gh):
        bad = rv("R1", "example-gh-one", "APPROVED")
        del bad["state"]
        gh([bad])
        assert state() == "unknown"

    @pytest.mark.parametrize("pr_number", ["abc", "12 34", "1; drop", ""])
    def test_non_numeric_pr_is_unknown_without_calling_gh(self, gh, pr_number):
        fake = gh([])
        assert baton_gate._reviewer_approval_state(pr_number, REPO, "example-gh-one")[0] == "unknown"
        assert fake.calls == []

    @pytest.mark.parametrize("repo", ["", "no-slash", None])
    def test_bad_repo_is_unknown_without_calling_gh(self, gh, repo):
        fake = gh([])
        assert baton_gate._reviewer_approval_state(PR, repo, "example-gh-one")[0] == "unknown"
        assert fake.calls == []

    def test_empty_reviewer_login_is_unknown(self, gh):
        gh([rv("R1", "example-gh-one", "APPROVED")])
        assert baton_gate._reviewer_approval_state(PR, REPO, "")[0] == "unknown"

    def test_T16_login_comparison_is_case_insensitive(self, gh):
        gh([rv("R1", "EXAMPLE-gh-ONE", "APPROVED")])
        assert state("example-GH-one") == "approved_current"

    def test_T16b_null_author_review_is_ignored_not_fatal(self, gh):
        gh([rv("R0", None, "CHANGES_REQUESTED", ts="2026-10-01T13:00:00Z"),
            rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T10:00:00Z")])
        assert state() == "approved_current"

    def test_null_author_pr_author_does_not_self_match(self, gh):
        # PR author deleted (author: null) must not turn every reviewer into
        # a "self reviewer".
        fake = gh([rv("R1", "example-gh-one", "APPROVED")])
        fake.author = None
        assert state() == "approved_current"

    # -- deterministic ordering ------------------------------------------

    def test_order_follows_submittedAt_not_pagination_order(self, gh):
        # Pagination order says CHANGES_REQUESTED is last; time says APPROVED is.
        # Pagination order: CR@10:00 first, APPROVED@09:00 second. Time-last is CR.
        gh([rv("R1", "example-gh-one", "CHANGES_REQUESTED", ts="2026-10-01T10:00:00Z"),
            rv("R2", "example-gh-one", "APPROVED", ts="2026-10-01T09:00:00Z")])
        assert state() == "changes_requested"

    def test_order_follows_submittedAt_reverse_pagination(self, gh):
        # Pagination order puts the later APPROVED FIRST.
        gh([rv("R2", "example-gh-one", "APPROVED", ts="2026-10-01T11:00:00Z"),
            rv("R1", "example-gh-one", "CHANGES_REQUESTED", ts="2026-10-01T10:00:00Z")])
        assert state() == "approved_current"

    # -- R3 (round 4): a tie on submittedAt between DIFFERENT states is ambiguous --

    def test_same_submittedAt_different_states_is_ambiguous_in_either_id_order(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R_b", "example-gh-one", "APPROVED", ts=t),
            rv("R_a", "example-gh-one", "CHANGES_REQUESTED", ts=t)])
        assert state() == "ambiguous"
        gh([rv("R_a", "example-gh-one", "APPROVED", ts=t),
            rv("R_b", "example-gh-one", "CHANGES_REQUESTED", ts=t)])
        assert state() == "ambiguous"

    def test_ambiguous_also_covers_approved_vs_dismissed_ties(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R_a", "example-gh-one", "APPROVED", ts=t),
            rv("R_b", "example-gh-one", "DISMISSED", ts=t)])
        assert state() == "ambiguous"

    def test_same_submittedAt_same_state_is_not_ambiguous(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R_a", "example-gh-one", "APPROVED", ts=t),
            rv("R_b", "example-gh-one", "APPROVED", ts=t)])
        assert state() == "approved_current"

    def test_tie_not_among_the_last_two_is_not_ambiguous(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R_a", "example-gh-one", "APPROVED", ts=t),
            rv("R_b", "example-gh-one", "CHANGES_REQUESTED", ts=t),
            rv("R_c", "example-gh-one", "APPROVED", ts="2026-10-01T11:00:00Z")])
        assert state() == "approved_current"

    def test_tie_among_the_last_two_after_an_earlier_review_is_ambiguous(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R_z", "example-gh-one", "APPROVED", ts="2026-10-01T09:00:00Z"),
            rv("R_a", "example-gh-one", "APPROVED", ts=t),
            rv("R_b", "example-gh-one", "CHANGES_REQUESTED", ts=t)])
        assert state() == "ambiguous"

    def test_a_tie_between_other_logins_does_not_make_this_login_ambiguous(self, gh):
        t = "2026-10-01T10:00:00Z"
        gh([rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T09:00:00Z"),
            rv("R_a", "example-gh-two", "APPROVED", ts=t),
            rv("R_b", "example-gh-two", "CHANGES_REQUESTED", ts=t)])
        assert state() == "approved_current"

    @pytest.mark.parametrize("bad", [None, 123, "", ["x"], {"a": 1}, "MISSING"], ids=repr)
    def test_opinionated_review_without_a_usable_submittedAt_is_unknown(self, gh, bad):
        node = rv("R1", "example-gh-one", "APPROVED")
        if bad == "MISSING":
            del node["submittedAt"]
        else:
            node["submittedAt"] = bad
        gh([node])
        assert state() == "unknown"

    def test_unusable_submittedAt_on_an_earlier_review_still_refuses(self, gh):
        early = rv("R0", "example-gh-one", "CHANGES_REQUESTED")
        early["submittedAt"] = None
        gh([early, rv("R1", "example-gh-one", "APPROVED", ts="2026-10-01T11:00:00Z")])
        assert state() == "unknown"

    def test_commented_review_with_null_submittedAt_is_ignored(self, gh):
        c = rv("R0", "example-gh-one", "COMMENTED")
        c["submittedAt"] = None
        gh([c, rv("R1", "example-gh-one", "APPROVED")])
        assert state() == "approved_current"

    def test_other_logins_review_with_null_submittedAt_is_ignored(self, gh):
        o = rv("R0", "example-gh-two", "CHANGES_REQUESTED")
        o["submittedAt"] = None
        gh([o, rv("R1", "example-gh-one", "APPROVED")])
        assert state() == "approved_current"


# ---------------------------------------------------------------------------
# T23 / T24 -- scrub + limitation text
# ---------------------------------------------------------------------------

D2_SENTENCE = "does not prove that the assigned colleague approved"
C4_SENTENCE = "not one cross-system transaction"


class TestShippedScrubAndDocs:
    def test_T23_template_map_exists_and_is_synthetic(self):
        tmpl = json.loads((_ROOT / "deploy" / "baton-login-map.json").read_text())
        assert tmpl["version"] == 1
        assert tmpl["map"], "template must carry at least one synthetic entry"

    def test_A2_template_carries_a_synthetic_repos_table(self):
        tmpl = json.loads((_ROOT / "deploy" / "baton-login-map.json").read_text())
        repos = tmpl["repos"]
        assert isinstance(repos, dict) and repos
        for prefix, repo in repos.items():
            assert prefix and re.match(r"^[\w.-]+/[\w.-]+$", repo)
            assert repo.lower().startswith("example-org/"), repo
        # and the shipped template loads under the real validator
        baton_gate._validate_login_map(tmpl)

    def test_T23_shipped_code_carries_no_template_identity(self):
        tmpl = json.loads((_ROOT / "deploy" / "baton-login-map.json").read_text())
        names = set()
        for agent, logins in tmpl["map"].items():
            names.add(agent.lower())
            names.update(str(x).lower() for x in logins)
        for rel in ("src/engram/tools/baton_gate.py", "src/engram/tools/baton.py",
                    "src/forum/coordination/projects.py", "src/forum/server.py"):
            text = (_ROOT / rel).read_text().lower()
            for n in names:
                assert not re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", text), (rel, n)

    def test_T24_advance_help_states_d2_and_c4_limits(self):
        out = subprocess.run(
            [sys.executable, str(_TOOLS_DIR / "baton.py"), "advance", "--help"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        flat = " ".join(out.stdout.split())
        assert D2_SENTENCE in flat
        assert C4_SENTENCE in flat

    def test_T24_advance_help_has_no_force_or_skip_flag(self):
        out = subprocess.run(
            [sys.executable, str(_TOOLS_DIR / "baton.py"), "advance", "--help"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "--force" not in out.stdout
        assert "--skip" not in out.stdout

    def test_T24_protocol_doc_states_d2_and_c4_limits(self):
        flat = " ".join((_ROOT / "docs" / "baton-protocol.md").read_text().split())
        assert "baton advance" in flat
        assert D2_SENTENCE in flat
        assert C4_SENTENCE in flat
        assert "proposed" in flat.lower()  # the admin map path is only PROPOSED


# ---------------------------------------------------------------------------
# `baton advance` -- thin client (the SERVER decides; the CLI only builds the
# request, defaults the merge seat when unambiguous, and prints both verdicts)
# ---------------------------------------------------------------------------

import argparse  # noqa: E402

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError  # noqa: E402

_BATON_RAW = """---
project: PR-9001
title: advance cli
status: in-progress
turn: example-agent-colleague
turn_since: 2026-10-01T10:00:00Z
turn_reason: "start"
turn_by: ""
participants: [example-agent-author, example-agent-colleague, example-agent-seat]
github: pr/example-org/example-repo/9001
colleague: example-agent-colleague
author: example-agent-author
phase: peer
---

# PR-9001

## Turn log

- 2026-10-01T10:00:00Z initialized → example-agent-colleague: start
"""


class _FakeClient:
    def __init__(self, post_result=None, post_error=None, raw=_BATON_RAW):
        self.raw = raw
        self.post_result = post_result
        self.post_error = post_error
        self.posts = []

    def get(self, path, params=None):
        return {"raw": self.raw}

    def post(self, path, payload=None):
        self.posts.append((path, payload))
        if self.post_error is not None:
            raise self.post_error
        return self.post_result


def _args(**kw):
    ns = dict(project_id="PR-9001", to=None, reason="colleague-APPROVED; ready for merge")
    ns.update(kw)
    return argparse.Namespace(**ns)


_CFG = {"agent_name": "example-agent-colleague"}


class TestCmdAdvance:
    def test_defaults_merge_seat_to_the_one_non_author_non_colleague_participant(self, capsys):
        c = _FakeClient(post_result={
            "advanced": True, "turn": "example-agent-seat",
            "ci": {"state": "green", "detail": "mergeStateStatus=CLEAN"},
            "approval": {"state": "approved_current", "detail": "ok"}, "warnings": []})
        _baton.cmd_advance(_args(), _CFG, "example-agent-colleague", c)
        path, payload = c.posts[0]
        assert path == "/api/projects/PR-9001/advance"
        assert payload["to_agent"] == "example-agent-seat"
        assert payload["agent"] == "example-agent-colleague"
        out = capsys.readouterr().out
        assert "CI:" in out and "approval:" in out  # two separate verdict lines

    def test_ambiguous_merge_seat_asks_instead_of_guessing(self, capsys):
        raw = _BATON_RAW.replace("example-agent-seat]", "example-agent-seat, example-agent-extra]")
        c = _FakeClient(raw=raw)
        with pytest.raises(SystemExit) as ei:
            _baton.cmd_advance(_args(), _CFG, "example-agent-colleague", c)
        assert ei.value.code == _baton.EXIT_VALIDATION
        assert c.posts == []
        assert "--to" in capsys.readouterr().err

    def test_explicit_to_is_sent_lowercased(self):
        c = _FakeClient(post_result={"advanced": True, "turn": "x", "ci": {}, "approval": {}})
        _baton.cmd_advance(_args(to="Example-Agent-Seat"), _CFG, "example-agent-colleague", c)
        assert c.posts[0][1]["to_agent"] == "example-agent-seat"

    def test_server_refusal_is_surfaced_and_exits_nonzero(self, capsys):
        err = ForumHttpError(422, json.dumps({"error": "approval check refused: no_review"}))
        c = _FakeClient(post_error=err)
        with pytest.raises(SystemExit) as ei:
            _baton.cmd_advance(_args(), _CFG, "example-agent-colleague", c)
        assert ei.value.code == _baton.EXIT_VALIDATION
        assert "approval check refused: no_review" in capsys.readouterr().err

    def test_noop_is_reported_as_nothing_to_do(self, capsys):
        c = _FakeClient(post_result={"advanced": False, "reason": "already merge"})
        _baton.cmd_advance(_args(), _CFG, "example-agent-colleague", c)
        assert "nothing to do" in capsys.readouterr().out

    def test_cli_has_no_local_decision_path(self):
        # The CLI never calls the verdict primitives: the server decides.
        src = (_ROOT / "src/engram/tools/baton.py").read_text()
        start = src.index("def cmd_advance(")
        end = src.index("# Subcommand: claim", start)
        body = src[start:end]
        assert "_reviewer_approval_state" not in body
        assert "_load_login_map" not in body
