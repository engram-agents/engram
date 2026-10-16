"""#3541 -- the approval reads work under the fine-grained service token.

Contract-first: written before the implementation.

Under the forum's fine-grained token the GraphQL field `reviews{commit{oid}}` is
refused (it needs Contents:Read); REST `GET pulls/<N>/reviews` serves
`commit_id` without it.  `_pr_approval_state` and `_reviewer_approval_state` keep
their names, signatures and state vocabularies; only HOW the rows are read
changes.  This is a MERGE GATE, so every cell here pins a fail-closed edge:
anything unestablished is `unknown` / `incomplete` / `stale`, never `fresh` /
`approved_current`.

Lettered cells follow the spec: (a) happy mapping, (b) a later
CHANGES_REQUESTED beats an earlier matching APPROVED, (c) a non-APPROVED row with
a matching commit is never fresh, (d) null commit_id, (e) the page walk,
(f) the head moved while reading, (g) invalid head.sha, (h) invalid inputs make
zero calls, (i) the identity refusal makes zero calls, (j) every failure shape at
every call kind, (k) identity kwargs and exact argv on every call, (l) the
reviewer variant's carried-over rules, (m) the repo-slug validator.

All token values are fake and built at runtime.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_ROOT = Path(__file__).parent.parent
_TOOLS_DIR = _ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton_gate  # noqa: E402

TOKEN = "ghp_fake" + "token" + "0123456789abcdef0123456789"
PR = "9001"
REPO = "example-org/example-repo"
HEAD = "a" * 40
OLD = "b" * 40
OLDER = "c" * 40
AUTHOR = "example-gh-author"
ME = "example-gh-one"
OTHER = "example-gh-two"
T0 = "2026-10-01T10:00:00Z"
T1 = "2026-10-01T11:00:00Z"
T2 = "2026-10-01T12:00:00Z"

# The head read is `gh pr view` (measured working under the fine-grained token);
# REST pulls/<N> was never measured, so it must not be used. In `paths` it shows as HEAD.
PULL_PATH = "HEAD"
HEAD_ARGV = ["gh", "pr", "view", PR, "--json", "headRefOid,author", "--repo", REPO]


def row(rid, login, state, commit=HEAD, ts=T0):
    return {
        "id": rid,
        "state": state,
        "submitted_at": ts,
        "user": ({"login": login} if login is not None else None),
        "commit_id": commit,
    }


def pull(head=HEAD, author=AUTHOR):
    return {"headRefOid": head,
            "author": ({"login": author} if author is not None else None)}


class RestGh:
    """Stands in for subprocess.run at the baton_gate boundary.

    Routes on the argv: `gh pr view ...` is a head read (the first is "head1",
    every later one "head2"), `gh api .../reviews?...&page=K` is a
    reviews page served from `reviews` (oldest-first, `page_size` per page).
    `fail` maps a call kind ("head1" | "page" | "head2") to a failure mode, and
    `fail_page` restricts a "page" failure to one page number.
    """

    def __init__(self, reviews=(), *, head=HEAD, head_after=None, author=AUTHOR,
                 page_size=100):
        self.reviews = list(reviews)
        self.head = head
        self.head_after = head if head_after is None else head_after
        self.author = author
        self.page_size = page_size
        self.calls = []          # (argv, kwargs)
        self.fail = {}
        self.fail_page = None
        self.pull_override = None   # callable(kind) -> body or None
        self.pulls_seen = 0

    # -- helpers ---------------------------------------------------------
    @property
    def argvs(self):
        return [c[0] for c in self.calls]

    @property
    def paths(self):
        return [c[0][2] if c[0][:2] == ["gh", "api"] else "HEAD" for c in self.calls]

    @property
    def page_calls(self):
        return [p for p in self.paths if "/reviews?" in p]

    def _failure(self, mode, cmd):
        if mode == "notfound":
            raise FileNotFoundError("gh")
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, 15)
        if mode == "oserror":
            raise OSError(f"cannot exec gh with {TOKEN}")
        if mode == "forbidden":
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr=f"gh: Resource not accessible by personal access token (HTTP 403) {TOKEN}")
        if mode == "exit1":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if mode == "empty":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if mode == "garbage":
            return subprocess.CompletedProcess(cmd, 0, stdout="<<not json>>", stderr="")
        return None

    def __call__(self, cmd, *a, **kw):
        self.calls.append((list(cmd), dict(kw)))
        path = cmd[2] if cmd[:2] == ["gh", "api"] else ""
        split = urlsplit(path)
        if split.path.endswith("/reviews"):
            kind = "page"
            page = int(parse_qs(split.query)["page"][0])
        else:
            self.pulls_seen += 1
            kind = "head1" if self.pulls_seen == 1 else "head2"
            page = None
        mode = self.fail.get(kind)
        if mode is not None and (kind != "page" or self.fail_page in (None, page)):
            out = self._failure(mode, cmd)
            if out is not None:
                return out
        if kind == "page":
            start = (page - 1) * self.page_size
            body = self.reviews[start:start + self.page_size]
            if mode == "obj_for_list":
                body = {"message": "not a list"}
            if mode == "bad_row":
                body = list(body) + ["not-an-object"]
            if mode == "bad_state":
                body = list(body) + [dict(row(999, ME, "APPROVED"), state=5)]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")
        body = pull(self.head if kind == "head1" else self.head_after, self.author)
        if mode == "obj_for_list":
            body = ["not", "an", "object"]
        if mode == "no_head":
            del body["headRefOid"]
        if mode == "head_not_object":
            body = "oops"
        if mode == "sha_not_string":
            body["headRefOid"] = 12345
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")


@pytest.fixture(autouse=True)
def _reset_identity():
    baton_gate._service_identity_reset()
    yield
    baton_gate._service_identity_reset()


@pytest.fixture
def gh(monkeypatch):
    def install(reviews=(), **kw):
        fake = RestGh(reviews, **kw)
        monkeypatch.setattr(baton_gate.subprocess, "run", fake)
        return fake
    return install


@pytest.fixture
def service(tmp_path):
    d = tmp_path / "gh-config"
    d.mkdir(mode=0o700)
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=str(d))
    return str(d)


def approval(pr=PR, repo=REPO):
    return baton_gate._pr_approval_state(pr, repo)


def reviewer(login=ME, pr=PR, repo=REPO):
    return baton_gate._reviewer_approval_state(pr, repo, login)


def rstate(login=ME):
    return reviewer(login)[0]


# ---------------------------------------------------------------------------
# (a) happy mapping, REST oldest-first order, "latest approval oid"
# ---------------------------------------------------------------------------

class TestHappyMapping:
    def test_approval_fresh_when_an_approved_row_covers_the_head(self, gh):
        gh([row(1, ME, "APPROVED", HEAD)])
        state, detail = approval()
        assert state == "fresh"
        assert HEAD[:8] in detail

    def test_approval_stale_when_approved_rows_exist_but_none_cover_the_head(self, gh):
        gh([row(1, ME, "APPROVED", OLDER), row(2, ME, "APPROVED", OLD)])
        state, detail = approval()
        assert state == "stale"
        assert HEAD[:8] in detail and OLD[:8] in detail
        # REST lists oldest-first: the LAST approved row is the latest approval
        assert f"latest approval oid {OLD[:8]}" in detail
        assert OLDER[:8] not in detail

    def test_approval_latest_approval_oid_skips_a_later_non_approved_row(self, gh):
        gh([row(1, ME, "APPROVED", OLDER), row(2, ME, "APPROVED", OLD),
            row(3, OTHER, "CHANGES_REQUESTED", "d" * 40)])
        state, detail = approval()
        assert state == "stale" and f"latest approval oid {OLD[:8]}" in detail

    def test_approval_no_approval_when_there_is_no_approved_row(self, gh):
        gh([row(1, ME, "COMMENTED"), row(2, ME, "CHANGES_REQUESTED"), row(3, ME, "DISMISSED")])
        assert approval()[0] == "no_approval"

    def test_approval_no_approval_for_an_empty_review_list(self, gh):
        gh([])
        assert approval()[0] == "no_approval"

    def test_approval_an_older_stale_approval_does_not_mask_a_fresh_one(self, gh):
        gh([row(1, ME, "APPROVED", HEAD), row(2, OTHER, "APPROVED", OLD)])
        assert approval()[0] == "fresh"

    def test_approval_state_word_is_case_insensitive(self, gh):
        gh([row(1, ME, "approved", HEAD)])
        assert approval()[0] == "fresh"

    def test_approval_hash_prefixed_pr_number_is_accepted(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        assert baton_gate._pr_approval_state("#9001", REPO)[0] == "fresh"
        assert fake.paths[0] == PULL_PATH

    def test_reviewer_approved_current(self, gh):
        gh([row(1, ME, "APPROVED", HEAD)])
        assert rstate() == "approved_current"

    def test_reviewer_stale_head(self, gh):
        gh([row(1, ME, "APPROVED", OLD)])
        assert rstate() == "stale_head"

    def test_reviewer_no_review(self, gh):
        gh([row(1, OTHER, "APPROVED", HEAD)])
        assert rstate() == "no_review"

    def test_reviewer_no_review_for_an_empty_list(self, gh):
        gh([])
        assert rstate() == "no_review"

    def test_reviewer_dismissed_and_commented_and_pending(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, ME, "DISMISSED", HEAD, T1)])
        assert rstate() == "dismissed"
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, ME, "COMMENTED", HEAD, T1),
            row(3, ME, "PENDING", HEAD, T2)])
        assert rstate() == "approved_current"


# ---------------------------------------------------------------------------
# (b) / (c) / (d)
# ---------------------------------------------------------------------------

class TestApprovalIsNotJustACommitMatch:
    def test_b_approved_then_later_changes_requested_is_changes_requested(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, ME, "CHANGES_REQUESTED", HEAD, T1)])
        assert rstate() == "changes_requested"

    @pytest.mark.parametrize("st", ["CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"])
    def test_c_a_non_approved_row_with_a_matching_commit_never_reads_fresh(self, gh, st):
        gh([row(1, ME, st, HEAD)])
        assert approval()[0] == "no_approval"

    def test_c_a_non_approved_matching_row_does_not_rescue_a_stale_approval(self, gh):
        gh([row(1, ME, "APPROVED", OLD), row(2, ME, "COMMENTED", HEAD)])
        assert approval()[0] == "stale"

    @pytest.mark.parametrize("missing", ["null", "absent"])
    def test_d_null_commit_id_is_non_matching_stale_and_never_an_error(self, gh, missing):
        r = row(1, ME, "APPROVED", None)
        if missing == "absent":
            del r["commit_id"]
        gh([r])
        state, detail = approval()
        assert state == "stale" and "latest approval oid unknown" in detail
        assert rstate() == "stale_head"

    def test_d_a_null_commit_row_does_not_stop_a_later_matching_row(self, gh):
        gh([row(1, ME, "APPROVED", None), row(2, ME, "APPROVED", HEAD)])
        assert approval()[0] == "fresh"


# ---------------------------------------------------------------------------
# (e) the page walk
# ---------------------------------------------------------------------------

def _filler(n, start=1000):
    return [row(start + i, OTHER, "COMMENTED", OLD, T0) for i in range(n)]


class TestPageWalk:
    def test_constants(self):
        assert baton_gate._REVIEW_PAGE_SIZE == 100
        assert baton_gate._REVIEW_PAGE_CAP == 10

    def test_99_rows_are_complete_in_one_page_call(self, gh):
        fake = gh(_filler(98) + [row(1, ME, "APPROVED", HEAD)])
        assert approval()[0] == "fresh"
        assert len(fake.page_calls) == 1
        assert len(fake.calls) == 3  # head, one page, head again

    def test_exactly_100_rows_then_an_empty_page_is_complete_after_two_page_calls(self, gh):
        fake = gh(_filler(99) + [row(1, ME, "APPROVED", HEAD)])
        assert approval()[0] == "fresh"
        assert len(fake.page_calls) == 2
        assert fake.page_calls[-1].endswith("&page=2")

    def test_exactly_100_rows_all_non_approved_is_no_approval_not_unknown(self, gh):
        fake = gh(_filler(100))
        assert approval()[0] == "no_approval"
        assert len(fake.page_calls) == 2

    def test_a_row_on_page_two_is_found(self, gh):
        fake = gh(_filler(100) + [row(1, ME, "APPROVED", HEAD)])
        assert approval()[0] == "fresh"
        assert rstate() == "approved_current"
        assert len(fake.page_calls) == 4  # two reads, two pages each

    def test_reviewer_exactly_100_rows_then_an_empty_page_is_complete(self, gh):
        fake = gh(_filler(100))
        assert rstate() == "no_review"
        assert len(fake.page_calls) == 2

    def test_nine_full_pages_then_an_empty_tenth_is_complete(self, gh):
        fake = gh(_filler(900))
        assert approval()[0] == "no_approval"
        assert len(fake.page_calls) == 10

    def test_ten_pages_with_a_short_tenth_is_complete(self, gh):
        fake = gh(_filler(999))
        assert approval()[0] == "no_approval"
        assert len(fake.page_calls) == 10

    def test_a_full_tenth_page_is_incomplete_never_none_approval(self, gh):
        fake = gh(_filler(1000))
        state, detail = approval()
        assert state == "unknown"
        assert len(fake.page_calls) == 10, "the cap is hard: no eleventh page"
        assert not any("page=11" in p for p in fake.paths)

    def test_reviewer_a_full_tenth_page_is_incomplete_never_no_review(self, gh):
        fake = gh(_filler(1000))
        state, detail = reviewer()
        assert state == "incomplete"
        assert len(fake.page_calls) == 10

    def test_incomplete_even_when_the_approval_would_have_been_on_page_eleven(self, gh):
        gh(_filler(1000) + [row(1, ME, "APPROVED", HEAD)])
        assert approval()[0] == "unknown"
        assert rstate() == "incomplete"

    def test_worst_case_is_two_head_reads_plus_ten_pages(self, gh):
        fake = gh(_filler(1000))
        approval()
        # head, 10 pages, and NO final head read once the walk is incomplete
        assert len(fake.calls) == 11
        assert len(fake.page_calls) == 10


# ---------------------------------------------------------------------------
# (f) head moved, (g) invalid head.sha
# ---------------------------------------------------------------------------

class TestHead:
    def test_f_head_moved_between_the_reads_is_unknown(self, gh):
        gh([row(1, ME, "APPROVED", HEAD)], head_after=OLD)
        state, detail = approval()
        assert state == "unknown" and "head moved" in detail
        gh([row(1, ME, "APPROVED", HEAD)], head_after=OLD)
        state, detail = reviewer()
        assert state == "unknown" and "head moved" in detail

    def test_f_the_head_is_read_before_and_after_the_reviews(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        approval()
        assert fake.paths[0] == PULL_PATH
        assert "/reviews?" in fake.paths[1]
        assert fake.paths[-1] == PULL_PATH
        assert len(fake.calls) == 3

    @pytest.mark.parametrize("bad", [
        "a" * 39, "a" * 41, "A" * 40, "a" * 39 + "&", "a" * 38 + "/x", "", "g" * 40,
        ("a" * 40) + "\n"])
    def test_g_invalid_head_sha_is_unknown_first_read(self, gh, bad):
        gh([row(1, ME, bad, bad)], head=bad, head_after=bad)
        assert approval()[0] == "unknown"
        assert rstate() == "unknown"

    @pytest.mark.parametrize("bad", ["a" * 39, "A" * 40, "a" * 39 + "&", "a" * 38 + "/x"])
    def test_g_invalid_head_sha_on_the_final_read_is_unknown(self, gh, bad):
        gh([row(1, ME, "APPROVED", HEAD)], head_after=bad)
        assert approval()[0] == "unknown"
        assert rstate() == "unknown"

    def test_g_a_bad_head_makes_no_reviews_call(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)], head="short")
        approval()
        assert len(fake.calls) == 1

    def test_the_pr_head_ref_is_never_a_substitute_for_head_sha(self, gh):
        # an approval whose commit_id equals head.ref (a branch name) is not fresh
        fake = gh([row(1, ME, "APPROVED", "feature")])
        assert approval()[0] == "stale"
        assert fake.calls


# ---------------------------------------------------------------------------
# (h) invalid inputs: unknown, ZERO calls;  (i) identity refusal: zero calls
# ---------------------------------------------------------------------------

BAD_REPOS = ["", "no-slash", None, 5, "ünï/x", "a/b\n", "a/..", "../x",
             "a/b?x=1", "a/b c", "a/b/c", "a/b#c", "a b/c", "a/b%2e"]


class TestInvalidInputs:
    @pytest.mark.parametrize("pr", ["abc", "12 34", "1; drop", "", "1/../2", "٣", "1\n"])
    def test_h_bad_pr_number_is_unknown_without_a_call(self, gh, pr):
        fake = gh([])
        assert baton_gate._pr_approval_state(pr, REPO)[0] == "unknown"
        assert baton_gate._reviewer_approval_state(pr, REPO, ME)[0] == "unknown"
        assert fake.calls == []

    @pytest.mark.parametrize("repo", BAD_REPOS, ids=repr)
    def test_h_bad_repo_is_unknown_without_a_call(self, gh, repo):
        fake = gh([])
        assert baton_gate._pr_approval_state(PR, repo)[0] == "unknown"
        assert baton_gate._reviewer_approval_state(PR, repo, ME)[0] == "unknown"
        assert fake.calls == []

    @pytest.mark.parametrize("login", ["", "   ", None, 5])
    def test_h_bad_reviewer_login_is_unknown_without_a_call(self, gh, login):
        fake = gh([])
        assert baton_gate._reviewer_approval_state(PR, REPO, login)[0] == "unknown"
        assert fake.calls == []

    def test_h_detail_names_what_was_invalid(self, gh):
        gh([])
        assert "not numeric" in baton_gate._pr_approval_state("x", REPO)[1]
        assert "repo" in baton_gate._pr_approval_state(PR, "")[1]
        assert "repo" in baton_gate._pr_approval_state(PR, "no-slash")[1]
        assert "login" in baton_gate._reviewer_approval_state(PR, REPO, "")[1]

    @pytest.mark.parametrize("environ", [{}, {"GH_TOKEN": ""}, {"GH_TOKEN": "   "}],
                             ids=["missing", "empty", "blank"])
    def test_i_identity_refusal_returns_before_any_subprocess(self, gh, tmp_path, environ):
        d = tmp_path / "cfg"
        d.mkdir(mode=0o700)
        baton_gate.configure_service_github_identity(environ=environ, config_dir=str(d))
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        a = approval()
        r = reviewer()
        assert a[0] == "unknown" and r[0] == "unknown"
        assert a == r == ("unknown", baton_gate._SERVICE_IDENTITY_MISSING_MSG)
        assert fake.calls == []


# ---------------------------------------------------------------------------
# (j) every failure shape at each of the three call kinds
# ---------------------------------------------------------------------------

COMMON = ["notfound", "timeout", "oserror", "forbidden", "exit1", "empty", "garbage",
          "obj_for_list"]
CASES = (
    [("head1", m) for m in COMMON + ["no_head", "head_not_object", "sha_not_string"]]
    + [("page", m) for m in COMMON + ["bad_row", "bad_state"]]
    + [("head2", m) for m in COMMON + ["no_head", "head_not_object", "sha_not_string"]]
)


class TestFailureShapes:
    @pytest.mark.parametrize("kind,mode", CASES, ids=[f"{k}-{m}" for k, m in CASES])
    @pytest.mark.parametrize("fn", ["approval", "reviewer"])
    def test_j_every_failure_is_unknown_scrubbed_and_never_raises(self, gh, service, kind, mode, fn):
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        fake.fail[kind] = mode
        state, detail = approval() if fn == "approval" else reviewer()
        assert state == "unknown", (state, detail)
        assert isinstance(detail, str) and detail
        assert TOKEN not in detail, "the token leaked into the detail"

    @pytest.mark.parametrize("mode", ["notfound", "timeout", "oserror", "forbidden", "exit1",
                                      "empty", "garbage", "obj_for_list", "bad_row", "bad_state"])
    @pytest.mark.parametrize("fn", ["approval", "reviewer"])
    def test_j_a_failure_on_page_two_is_unknown(self, gh, service, mode, fn):
        fake = gh(_filler(100) + [row(1, ME, "APPROVED", HEAD)])
        fake.fail["page"] = mode
        fake.fail_page = 2
        state, detail = approval() if fn == "approval" else reviewer()
        assert state == "unknown", (state, detail)
        assert TOKEN not in detail

    def test_j_the_403_detail_keeps_gh_s_reason_but_not_the_token(self, gh, service):
        fake = gh([])
        fake.fail["head1"] = "forbidden"
        detail = approval()[1]
        assert "Resource not accessible by personal access token" in detail
        assert TOKEN not in detail and "***" in detail

    def test_j_a_non_list_page_is_not_an_empty_complete_list(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        fake.fail["page"] = "obj_for_list"
        assert approval()[0] == "unknown"
        assert rstate() == "unknown"

    def test_j_no_exception_escapes_a_deeply_nested_body(self, gh, monkeypatch):
        def deep(cmd, *a, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="[" * 100000 + "]" * 100000, stderr="")
        monkeypatch.setattr(baton_gate.subprocess, "run", deep)
        assert approval()[0] == "unknown"
        assert rstate() == "unknown"


# ---------------------------------------------------------------------------
# (k) identity kwargs and exact argv on EVERY call
# ---------------------------------------------------------------------------

class TestCallShape:
    def _all_scenarios(self, gh):
        out = []
        for fn in (approval, reviewer):
            fake = gh(_filler(100) + [row(1, ME, "APPROVED", HEAD)])
            fn()
            out.append(fake)
        return out

    def test_k_every_call_carries_the_service_identity(self, gh, service):
        for fake in self._all_scenarios(gh):
            assert len(fake.calls) >= 4
            for argv, kw in fake.calls:
                env = kw.get("env")
                assert isinstance(env, dict), f"no env= on {argv}"
                assert env["GH_TOKEN"] == TOKEN and env["GH_CONFIG_DIR"] == service
                assert set(env) == {"PATH", "GH_TOKEN", "GH_CONFIG_DIR",
                                    "GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER"}

    def test_k_default_off_calls_carry_no_env(self, gh):
        for fake in self._all_scenarios(gh):
            assert len(fake.calls) >= 4
            for _argv, kw in fake.calls:
                assert "env" not in kw

    def test_k_the_head_read_argv_is_exactly_gh_pr_view_headRefOid_author(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)])
        approval()
        assert fake.calls[0][0] == HEAD_ARGV
        assert fake.calls[-1][0] == HEAD_ARGV
        assert all(a[:3] == ["gh", "pr", "view"] or "/reviews?" in a[2] for a in fake.argvs)

    def test_k_argv_is_exactly_gh_api_reviews_path_or_the_head_view_with_no_token_and_no_graphql(self, gh, service):
        allowed = re.compile(
            rf"^repos/{re.escape(REPO)}/pulls/{PR}/reviews\?per_page=100&page=[1-9][0-9]*$")
        for fake in self._all_scenarios(gh):
            for argv, kw in fake.calls:
                if argv[:3] == ["gh", "pr", "view"]:
                    assert argv == HEAD_ARGV, argv
                else:
                    assert len(argv) == 3 and argv[:2] == ["gh", "api"], argv
                    assert allowed.match(argv[2]), argv[2]
                assert TOKEN not in " ".join(argv) and "graphql" not in " ".join(argv)
                assert "--jq" not in argv and "--paginate" not in argv
                assert kw.get("capture_output") is True and kw.get("text") is True
                assert kw.get("timeout") == 15

    def test_k_pages_are_requested_in_order_from_one(self, gh):
        fake = gh(_filler(250))
        approval()
        assert fake.page_calls == [
            f"repos/{REPO}/pulls/{PR}/reviews?per_page=100&page={k}" for k in (1, 2, 3)]

    def test_k_neither_function_spawns_directly(self):
        src = (Path(baton_gate.__file__)).read_text(encoding="utf-8")
        for name in ("_pr_approval_state", "_reviewer_approval_state"):
            body = src.split(f"\ndef {name}(", 1)[1].split("\ndef ", 1)[0]
            assert "subprocess.run" not in body, f"{name} must go through the one private reader"
        assert hasattr(baton_gate, "_gh_read_json")


# ---------------------------------------------------------------------------
# (l) the reviewer variant's carried-over rules, on REST rows
# ---------------------------------------------------------------------------

class TestReviewerRules:
    def test_l_id_tie_break_is_numeric_not_lexicographic(self, gh):
        # same timestamp, same state, DIFFERENT commits: the higher NUMERIC id decides.
        gh([row(9, ME, "APPROVED", OLD, T0), row(10, ME, "APPROVED", HEAD, T0)])
        assert rstate() == "approved_current"       # string order would pick "9" -> stale
        gh([row(10, ME, "APPROVED", OLD, T0), row(9, ME, "APPROVED", HEAD, T0)])
        assert rstate() == "stale_head"             # string order would pick "9" -> current

    def test_l_ordering_follows_submitted_at_not_rest_order(self, gh):
        gh([row(2, ME, "CHANGES_REQUESTED", HEAD, T1), row(1, ME, "APPROVED", HEAD, T0)])
        assert rstate() == "changes_requested"
        gh([row(1, ME, "APPROVED", HEAD, T1), row(2, ME, "CHANGES_REQUESTED", HEAD, T0)])
        assert rstate() == "approved_current"

    def test_l_same_timestamp_different_states_is_ambiguous(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, ME, "CHANGES_REQUESTED", HEAD, T0)])
        assert rstate() == "ambiguous"
        gh([row(2, ME, "APPROVED", HEAD, T0), row(1, ME, "CHANGES_REQUESTED", HEAD, T0)])
        assert rstate() == "ambiguous"

    def test_l_ambiguity_looks_at_every_row_at_the_max_timestamp(self, gh):
        gh([row(1, ME, "CHANGES_REQUESTED", HEAD, T0), row(2, ME, "APPROVED", HEAD, T0),
            row(3, ME, "APPROVED", HEAD, T0)])
        assert rstate() == "ambiguous"

    def test_l_a_tie_below_the_max_timestamp_is_not_ambiguous(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, ME, "CHANGES_REQUESTED", HEAD, T0),
            row(3, ME, "APPROVED", HEAD, T1)])
        assert rstate() == "approved_current"

    def test_l_a_tie_between_other_logins_is_not_ambiguous(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, OTHER, "APPROVED", HEAD, T1),
            row(3, OTHER, "CHANGES_REQUESTED", HEAD, T1)])
        assert rstate() == "approved_current"

    def test_l_null_user_never_matches(self, gh):
        gh([row(1, ME, "APPROVED", HEAD, T0), row(2, None, "CHANGES_REQUESTED", HEAD, T1)])
        assert rstate() == "approved_current"
        r = row(2, None, "CHANGES_REQUESTED", HEAD, T1)
        del r["user"]
        gh([row(1, ME, "APPROVED", HEAD, T0), r])
        assert rstate() == "approved_current"

    def test_l_login_match_is_case_insensitive(self, gh):
        gh([row(1, "EXAMPLE-gh-ONE", "APPROVED", HEAD)])
        assert rstate("example-GH-one") == "approved_current"

    def test_l_self_review_uses_the_pull_user_login(self, gh):
        fake = gh([row(1, ME, "APPROVED", HEAD)], author="Example-GH-One")
        assert rstate() == "self_review"
        assert fake.page_calls == []   # decided from the head read, no reviews needed

    def test_l_a_null_pr_author_does_not_self_match(self, gh):
        gh([row(1, ME, "APPROVED", HEAD)], author=None)
        assert rstate() == "approved_current"

    @pytest.mark.parametrize("bad", [None, 123, "", "  ", ["x"], {"a": 1}, "MISSING"], ids=repr)
    def test_l_opinionated_row_without_a_usable_submitted_at_is_unknown(self, gh, bad):
        r = row(1, ME, "APPROVED", HEAD)
        if bad == "MISSING":
            del r["submitted_at"]
        else:
            r["submitted_at"] = bad
        gh([r])
        assert rstate() == "unknown"

    def test_l_unusable_submitted_at_on_an_earlier_opinion_still_refuses(self, gh):
        early = row(1, ME, "CHANGES_REQUESTED", HEAD, None)
        gh([early, row(2, ME, "APPROVED", HEAD, T1)])
        assert rstate() == "unknown"

    def test_l_commented_or_other_logins_null_submitted_at_is_ignored(self, gh):
        gh([row(1, ME, "COMMENTED", HEAD, None), row(2, OTHER, "CHANGES_REQUESTED", HEAD, None),
            row(3, ME, "APPROVED", HEAD, T0)])
        assert rstate() == "approved_current"

    @pytest.mark.parametrize("bad_id", ["R1", None, "10", 1.5, True, "MISSING"], ids=repr)
    def test_l_a_non_integer_or_missing_id_is_unknown(self, gh, bad_id):
        r = row(1, OTHER, "COMMENTED", HEAD)
        if bad_id == "MISSING":
            del r["id"]
        else:
            r["id"] = bad_id
        gh([r, row(2, ME, "APPROVED", HEAD)])
        assert rstate() == "unknown"

    def test_l_an_unrecognised_state_of_the_wanted_login_is_unknown(self, gh):
        gh([row(1, ME, "WEIRD", HEAD)])
        assert rstate() == "unknown"

    def test_l_login_is_the_same_wanted_login_for_the_approval_read_too(self, gh):
        # the approval variant is login-blind; only the reviewer variant filters
        gh([row(1, OTHER, "APPROVED", HEAD)])
        assert approval()[0] == "fresh"
        assert rstate() == "no_review"


# ---------------------------------------------------------------------------
# (m) the repo-slug validator (the shadowed-regex defect found on dev)
# ---------------------------------------------------------------------------

class TestRepoSlugValidator:
    @pytest.mark.parametrize("bad", ["ünï/x", "a/b\n", "a/..", "../x", "a/b?x=1",
                                     "a/b c", "a/b/c", "", None, 5, "a/", "/b", "./.",
                                     "a/bé"], ids=repr)
    def test_m_refuses(self, bad):
        assert baton_gate._repo_slug_is_valid(bad) is False

    @pytest.mark.parametrize("good", ["engram-agents/" + "engram-alpha", "a.b/c_d-e", "A1/b2"])
    def test_m_accepts(self, good):
        assert baton_gate._repo_slug_is_valid(good) is True

    def test_m_the_ci_validator_has_its_own_unshadowed_pattern(self):
        assert baton_gate._CI_REPO_SLUG_RE.fullmatch("a/b")
        assert not baton_gate._CI_REPO_SLUG_RE.fullmatch("ünï/x")
        assert baton_gate._REPO_SLUG_RE is not baton_gate._CI_REPO_SLUG_RE

    def test_m_the_other_users_of_the_wider_pattern_are_unchanged(self):
        assert baton_gate._REPO_SLUG_RE.match("ünï/x")


# ---------------------------------------------------------------------------
# A commit id counts as the head ONLY on exact string equality (#3541 review round)
#
# Prefix, abbreviation and case-folding comparisons all pass the fixtures above,
# because no fixture there differs from the head only after a shared prefix or
# only in case. These near-misses pin the exact-equality contract.
# ---------------------------------------------------------------------------

NEAR_HEAD = ("0123456789abcdef" * 3)[:40]
NEAR_MISSES = {
    "last_char_differs": NEAR_HEAD[:39] + ("1" if NEAR_HEAD[-1] == "0" else "0"),
    "uppercase": NEAR_HEAD.upper(),
    "abbrev_7": NEAR_HEAD[:7],
    "abbrev_8": NEAR_HEAD[:8],
}


class TestExactEqualityOnly:
    @pytest.mark.parametrize("kind", sorted(NEAR_MISSES))
    def test_approval_near_miss_commit_id_is_stale_never_fresh(self, gh, kind):
        assert NEAR_MISSES[kind] != NEAR_HEAD
        gh([row(1, ME, "APPROVED", NEAR_MISSES[kind])], head=NEAR_HEAD, head_after=NEAR_HEAD)
        assert approval()[0] == "stale"

    @pytest.mark.parametrize("kind", sorted(NEAR_MISSES))
    def test_reviewer_near_miss_commit_id_is_stale_head_never_current(self, gh, kind):
        gh([row(1, ME, "APPROVED", NEAR_MISSES[kind])], head=NEAR_HEAD, head_after=NEAR_HEAD)
        assert rstate() == "stale_head"

    def test_the_exact_head_still_counts_with_this_head_value(self, gh):
        gh([row(1, ME, "APPROVED", NEAR_HEAD)], head=NEAR_HEAD, head_after=NEAR_HEAD)
        assert approval()[0] == "fresh"
        assert rstate() == "approved_current"

    def test_after_read_differing_only_in_the_last_char_is_head_moved(self, gh):
        after = NEAR_MISSES["last_char_differs"]
        gh([row(1, ME, "APPROVED", NEAR_HEAD)], head=NEAR_HEAD, head_after=after)
        state, detail = approval()
        assert state == "unknown" and "head moved" in detail
        gh([row(1, ME, "APPROVED", NEAR_HEAD)], head=NEAR_HEAD, head_after=after)
        state, detail = reviewer()
        assert state == "unknown" and "head moved" in detail

    def test_after_read_differing_only_in_case_is_unknown(self, gh):
        # An uppercase oid is refused by the 40-lowercase-hex check before the
        # comparison, so the reason is the shape refusal, not "head moved".
        gh([row(1, ME, "APPROVED", NEAR_HEAD)], head=NEAR_HEAD, head_after=NEAR_HEAD.upper())
        assert approval()[0] == "unknown"
        gh([row(1, ME, "APPROVED", NEAR_HEAD)], head=NEAR_HEAD, head_after=NEAR_HEAD.upper())
        assert reviewer()[0] == "unknown"
