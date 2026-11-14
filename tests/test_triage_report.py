"""Tests for tools/triage_report.py -- the read-only triage report.

Contract (each cell written before the implementation and red against the absent module):

INTEGRITY
  I1  collect_search pages until a short page, returns the items, and raises IntegrityError
      unless the count equals the first page's total_count ("collected N, search said M").
  I2  It raises (never truncates) when total_count > 1000 or any page says incomplete_results.
  I3  A second page of the wrong length, a repeated page, and duplicates at the right length
      are all refused.
  I4  Every number the text report prints carries a "ranges over:" part on its own line.
  I5  main(): 0 when a report was produced, 2 for a usage error, 3 for IntegrityError
      (stderr starts "ERROR:", nothing on stdout).
SECTIONS
  S1  outside reports (non-team author, no team comment); S2 security line with --since;
  S3  weekly sections in order; S4 rule 2B.
READ-ONLY
  The real _api builds argv starting ["gh","api","-X","GET"] for every call shape.

No test touches the network: every test patches the ``_api`` seam, and an autouse fixture makes
the real subprocess.run raise unless a test replaces it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
_TOOL = _TOOLS / "triage_report.py"
_MODNAME = "_triage_report_4779"


def _load():
    """Load the tool by file path: a rank-0 sys.path insert would outrank src/ packages."""
    spec = importlib.util.spec_from_file_location(_MODNAME, _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod  # before exec_module: dataclasses resolve their module here
    spec.loader.exec_module(mod)
    return mod


triage_report = _load()

REPO = "engram-agents/engram"
NOW_S = "2026-11-14T12:00:00Z"
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)
DAY = 86400


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: int) -> str:
    return ts(NOW - timedelta(seconds=seconds))


def mk(n, author="reporter", assoc="NONE", labels=(), created="2026-11-01T00:00:00Z", title=None):
    return {
        "number": n,
        "title": title or f"issue {n}",
        "user": {"login": author},
        "author_association": assoc,
        "created_at": created,
        "labels": [{"name": name} for name in labels],
    }


def team(n, **kw):
    kw.setdefault("assoc", "MEMBER")
    kw.setdefault("author", "teamer")
    return mk(n, **kw)


class FakeGH:
    """Stands in for triage_report._api and RECORDS every call."""

    def __init__(self, issues=(), comments=None, events=None, closed_total=0, total=None,
                 pages=None, incomplete_pages=(), closed_incomplete=False):
        self.issues = list(issues)
        self.comments = comments or {}
        self.events = events or {}
        self.closed_total = closed_total
        self.total = total
        self.pages = pages
        self.incomplete_pages = set(incomplete_pages)
        self.closed_incomplete = closed_incomplete
        self.calls = []

    def __call__(self, path, params=None, paginate=False):
        params = dict(params or {})
        self.calls.append((path, params, paginate))
        if path == "/search/issues":
            q = params["q"]
            if "is:closed" in q:
                return {"total_count": self.closed_total, "incomplete_results": self.closed_incomplete,
                        "items": []}
            page = int(params.get("page", 1))
            if self.pages is not None:
                items = self.pages.get(page, [])
            else:
                items = self.issues[(page - 1) * 100: page * 100]
            total = self.total if self.total is not None else len(self.issues)
            return {"total_count": total, "incomplete_results": page in self.incomplete_pages,
                    "items": items}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/(comments|events)", path)
        assert m, f"unexpected path {path}"
        n = int(m.group(2))
        assert paginate, "list endpoints must be fetched with paginate=True"
        return list((self.comments if m.group(3) == "comments" else self.events).get(n, []))

    def paths(self, kind):
        return [p for (p, _, _) in self.calls if p.endswith("/" + kind)]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"network/subprocess touched: {a}")
    monkeypatch.setattr(triage_report.subprocess, "run", boom)


def use(monkeypatch, fake):
    monkeypatch.setattr(triage_report, "_api", fake)
    return fake


def build(monkeypatch, fake, since=SINCE):
    use(monkeypatch, fake)
    return triage_report.build_report(REPO, NOW, since)


def numbers(counted):
    return [i["number"] for i in counted.items]


def comment(assoc, login="someone"):
    return {"user": {"login": login}, "author_association": assoc}


def labeled(label, created, event="labeled"):
    return {"event": event, "created_at": created, "label": {"name": label}}


# ───────────────────────────── I1 ─────────────────────────────

def test_i1_collects_all_items_across_pages(monkeypatch):
    fake = use(monkeypatch, FakeGH([mk(i) for i in range(1, 251)]))
    items = triage_report.collect_search("repo:x is:issue is:open")
    assert [i["number"] for i in items] == list(range(1, 251))
    pages = [str(p["page"]) for (path, p, _) in fake.calls if path == "/search/issues"]
    assert pages == ["1", "2", "3"]


def test_i1_exact_multiple_of_page_size_ends_on_empty_page(monkeypatch):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 201)]))
    assert len(triage_report.collect_search("q")) == 200


def test_i1_zero_items(monkeypatch):
    use(monkeypatch, FakeGH([]))
    assert triage_report.collect_search("q") == []


def test_i1_short_collection_raises_with_both_numbers(monkeypatch):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 131)], total=150))
    with pytest.raises(triage_report.IntegrityError, match=r"collected 130, search said 150"):
        triage_report.collect_search("q")


# ───────────────────────────── I2 ─────────────────────────────

def test_i2_total_over_1000_is_refused_not_truncated(monkeypatch):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 1001)], total=1500))
    with pytest.raises(triage_report.IntegrityError, match=r"at most 1000 results"):
        triage_report.collect_search("q")


def test_i2_total_exactly_1000_is_accepted(monkeypatch):
    fake = use(monkeypatch, FakeGH([mk(i) for i in range(1, 1001)]))
    assert len(triage_report.collect_search("q")) == 1000
    assert max(int(p["page"]) for (path, p, _) in fake.calls if path == "/search/issues") == 10


def test_i2_incomplete_results_on_first_page(monkeypatch):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 11)], incomplete_pages={1}))
    with pytest.raises(triage_report.IntegrityError, match=r"incomplete_results"):
        triage_report.collect_search("q")


def test_i2_incomplete_results_on_a_later_page(monkeypatch):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 151)], incomplete_pages={2}))
    with pytest.raises(triage_report.IntegrityError, match=r"incomplete_results"):
        triage_report.collect_search("q")


# ───────────────────────────── I3 ─────────────────────────────

def test_i3_second_page_shorter_than_total_minus_first(monkeypatch):
    pages = {1: [mk(i) for i in range(1, 101)], 2: [mk(i) for i in range(101, 131)]}
    use(monkeypatch, FakeGH(pages=pages, total=150))
    with pytest.raises(triage_report.IntegrityError, match=r"collected 130, search said 150"):
        triage_report.collect_search("q")


def test_i3_page_two_repeats_page_one(monkeypatch):
    first = [mk(i) for i in range(1, 101)]
    use(monkeypatch, FakeGH(pages={1: first, 2: list(first)}, total=100))
    with pytest.raises(triage_report.IntegrityError, match=r"collected 200, search said 100"):
        triage_report.collect_search("q")


def test_i3_duplicates_at_the_right_length_are_refused(monkeypatch):
    pages = {1: [mk(i) for i in range(1, 101)], 2: [mk(i) for i in range(1, 51)]}
    use(monkeypatch, FakeGH(pages=pages, total=150))
    with pytest.raises(triage_report.IntegrityError, match=r"duplicate"):
        triage_report.collect_search("q")


# ───────────────────────────── helpers for report cells ─────────────────────────────

def full_fixture():
    """A fixture with every section populated, so no section is vacuous."""
    issues = [
        mk(10, labels=["bug", "area:core", "priority:high"], created="2026-05-09T00:00:00Z"),
        mk(11, labels=["epic", "area:core", "priority:low"], created="2026-06-01T00:00:00Z"),
        team(12, labels=["enhancement", "priority:low"], created="2026-11-13T00:00:00Z"),
        team(13, labels=["bug", "area:security", "priority:high"], created="2026-11-10T00:00:00Z"),
        team(14, labels=["bug", "area:core"], created="2026-11-12T00:00:00Z"),
        team(15, labels=["bug", "area:core", "priority:low", "proposed-close"], created="2026-09-01T00:00:00Z"),
        team(16, labels=["bug", "area:core", "priority:low", "needs-decision"], created="2026-09-02T00:00:00Z"),
        team(17, labels=["bug", "area:core", "priority:low", "deprioritized", "quick-fix"],
             created="2026-09-03T00:00:00Z"),
        team(18, labels=["bug", "area:core", "priority:low", "deprioritized"], created="2026-09-04T00:00:00Z"),
        mk(19, labels=["bug", "area:core", "priority:low"], created="2026-11-13T09:00:00Z"),
    ]
    events = {
        15: [labeled("proposed-close", ago(20 * DAY + 5 * 3600))],
        16: [labeled("needs-decision", ago(3 * DAY))],
    }
    return FakeGH(issues, events=events, closed_total=3950, comments={19: []})


def count_lines(text):
    lines = text.splitlines()
    return [ln for ln in lines[1:] if ln and not ln[0].isspace() and not ln.startswith("== ")]


# ───────────────────────────── I4 ─────────────────────────────

def test_i4_every_count_line_states_what_it_ranges_over(monkeypatch):
    report = build(monkeypatch, full_fixture())
    lines = count_lines(triage_report.render_text(report))
    assert len(lines) >= 18, lines
    for ln in lines:
        assert "ranges over:" in ln, ln
        assert ln.split("ranges over:", 1)[1].strip(), ln


def test_i4_open_count_names_the_search_query(monkeypatch):
    report = build(monkeypatch, full_fixture())
    line = next(ln for ln in count_lines(triage_report.render_text(report)) if ln.startswith("Open issues collected"))
    assert f"repo:{REPO} is:issue is:open" in line


def test_i4_closed_count_says_one_page_of_one_suffices(monkeypatch):
    report = build(monkeypatch, full_fixture())
    assert "per_page=1" in report.s3_closed.ranges_over
    assert f"repo:{REPO} is:issue is:closed" in report.s3_closed.ranges_over


# ───────────────────────────── I5 ─────────────────────────────

def test_i5_exit_zero_with_findings_and_report_on_stdout(monkeypatch, capsys):
    use(monkeypatch, FakeGH([mk(1)], closed_total=5))
    rc = triage_report.main(["--now", NOW_S])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.startswith("Triage report:")
    assert "#1 " in out.out  # an outside issue was found, exit code unchanged


def test_i5_default_repo(monkeypatch, capsys):
    fake = use(monkeypatch, FakeGH([]))
    assert triage_report.main(["--now", NOW_S]) == 0
    assert any(p.get("q", "").startswith("repo:engram-agents/engram ") for (_, p, _) in fake.calls)


@pytest.mark.parametrize("argv", [
    ["--since", "2026-13-40"], ["--since", "yesterday"], ["--since", "2026-11-07T00:00:00Z"],
    ["--now", "2026-11-14"], ["--bogus"],
])
def test_i5_usage_error_exits_2_and_never_calls_api(monkeypatch, argv):
    fake = use(monkeypatch, FakeGH([]))
    with pytest.raises(SystemExit) as e:
        triage_report.main(argv)
    assert e.value.code == 2
    assert fake.calls == []


def test_i5_integrity_error_exits_3_with_nothing_on_stdout(monkeypatch, capsys):
    use(monkeypatch, FakeGH([mk(i) for i in range(1, 131)], total=150))
    rc = triage_report.main(["--now", NOW_S])
    out = capsys.readouterr()
    assert rc == 3
    assert out.out == ""
    assert out.err.startswith("ERROR:")
    assert "collected 130, search said 150" in out.err


def test_i5_integrity_error_midway_prints_nothing_partial(monkeypatch, capsys):
    # the search is fine, the closed-count call fails after the open set was collected
    fake = FakeGH([mk(1)], closed_incomplete=True)
    use(monkeypatch, fake)
    rc = triage_report.main(["--now", NOW_S])
    out = capsys.readouterr()
    assert rc == 3 and out.out == "" and out.err.startswith("ERROR:")


# ───────────────────────────── rendering ─────────────────────────────

def test_headings_present_in_fixed_order(monkeypatch):
    text = triage_report.render_text(build(monkeypatch, full_fixture()))
    headings = [
        "== S1 OUTSIDE REPORTS (daily) ==",
        "== S2 SECURITY ==",
        "== S3.1 MISSING A TYPE, AN AREA OR A PRIORITY (weekly) ==",
        "== S3.2 PROPOSED-CLOSE, LABEL AT LEAST 14 DAYS OLD ==",
        "== S3.3 NEEDS-DECISION, LABEL AT LEAST 14 DAYS OLD ==",
        "== S3.4 PRIORITY:HIGH ==",
        "== S3.5 TOTALS ==",
        "== S4 RULE 2B (deprioritized and quick-fix) ==",
    ]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions)
    assert text.splitlines()[0] == f"Triage report: repo={REPO} now={NOW_S} since={ts(SINCE)}"


def test_json_round_trips_the_same_counts_as_the_text(monkeypatch, capsys):
    use(monkeypatch, full_fixture())
    assert triage_report.main(["--now", NOW_S]) == 0
    text = capsys.readouterr().out
    assert triage_report.main(["--now", NOW_S, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    keys = [
        "open_collected", "s1_candidates", "s1_outside", "s2_since", "s2_total",
        "s3_missing_type", "s3_missing_area", "s3_missing_priority",
        "s3_proposed_close", "s3_proposed_close_unknown", "s3_needs_decision",
        "s3_needs_decision_unknown", "s3_priority_high", "s3_open", "s3_closed",
        "s4_both", "s4_deprioritized",
    ]
    from_json = [data[k]["count"] for k in keys]
    from_text = []
    for ln in count_lines(text):
        m = re.search(r": (\d+) \(ranges over:", ln)
        if m:
            from_text.append(int(m.group(1)))
    assert from_text == from_json
    assert data["repo"] == REPO and data["now"] == NOW_S
    assert data["s3_oldest"]["number"] == 10
    assert sum(from_json) > 20  # not vacuous


# ───────────────────────────── S1 ─────────────────────────────

def test_s1_a_outside_author_zero_comments_is_listed(monkeypatch):
    r = build(monkeypatch, FakeGH([mk(1, author="stranger", created="2026-11-05T01:02:03Z", title="T one")]))
    assert numbers(r.s1_outside) == [1]
    item = r.s1_outside.items[0]
    assert item == {"number": 1, "title": "T one", "author": "stranger",
                    "created_at": "2026-11-05T01:02:03Z", "comments_seen": 0}


def test_s1_b_outside_author_with_a_team_comment_is_not_listed(monkeypatch):
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        r = build(monkeypatch, FakeGH([mk(1)], comments={1: [comment(assoc)]}))
        assert r.s1_outside.items == [], assoc
        assert r.s1_candidates.count == 1


def test_s1_c_only_another_outside_comment_still_listed(monkeypatch):
    r = build(monkeypatch, FakeGH([mk(1)], comments={1: [comment("NONE", "other")]}))
    assert numbers(r.s1_outside) == [1]
    assert r.s1_outside.items[0]["comments_seen"] == 1


def test_s1_d_team_author_never_listed(monkeypatch):
    r = build(monkeypatch, FakeGH([team(1, assoc="OWNER"), team(2), team(3, assoc="COLLABORATOR")]))
    assert r.s1_outside.items == []
    assert r.s1_candidates.count == 0


def test_s1_e_comments_fetched_only_for_candidates(monkeypatch):
    fake = FakeGH([team(1), mk(2), team(3), mk(4)], comments={2: [comment("MEMBER")]})
    build(monkeypatch, fake)
    assert sorted(fake.paths("comments")) == [f"/repos/{REPO}/issues/2/comments",
                                              f"/repos/{REPO}/issues/4/comments"]


def test_s1_contributor_is_not_a_team_account(monkeypatch):
    r = build(monkeypatch, FakeGH([mk(1, assoc="CONTRIBUTOR"), mk(2, assoc="FIRST_TIME_CONTRIBUTOR")],
                                  comments={2: [comment("CONTRIBUTOR")]}))
    assert numbers(r.s1_outside) == [1, 2]


def test_s1_missing_association_counts_as_outside(monkeypatch):
    issue = mk(1)
    del issue["author_association"]
    r = build(monkeypatch, FakeGH([issue]))
    assert numbers(r.s1_outside) == [1]


def test_s1_ranges_over_states_the_team_rule(monkeypatch):
    r = build(monkeypatch, FakeGH([mk(1)]))
    assert "OWNER/MEMBER/COLLABORATOR" in r.s1_outside.ranges_over


# ───────────────────────────── S2 ─────────────────────────────

def test_s2_boundary_at_since_included_one_second_before_excluded(monkeypatch):
    since = datetime(2026, 11, 7, tzinfo=timezone.utc)
    issues = [
        team(1, labels=["area:security"], created="2026-11-07T00:00:00Z"),
        team(2, labels=["area:security"], created="2026-11-06T23:59:59Z"),
        team(3, labels=["bug"], created="2026-11-10T00:00:00Z"),
        team(4, labels=["area:security", "bug"], created="2026-11-13T00:00:00Z"),
    ]
    r = build(monkeypatch, FakeGH(issues), since=since)
    assert numbers(r.s2_since) == [1, 4]
    assert r.s2_since.count == 2
    assert r.s2_total.count == 3
    assert "2026-11-07T00:00:00Z" in r.s2_since.ranges_over
    assert r.s2_since.items[0] == {"number": 1, "title": "issue 1", "created_at": "2026-11-07T00:00:00Z"}


def test_s2_since_flag_is_utc_midnight_and_default_is_now_minus_7_days(monkeypatch, capsys):
    use(monkeypatch, FakeGH([]))
    triage_report.main(["--now", NOW_S, "--since", "2026-11-01"])
    assert "since=2026-11-01T00:00:00Z" in capsys.readouterr().out.splitlines()[0]
    triage_report.main(["--now", NOW_S])
    assert "since=2026-11-07T12:00:00Z" in capsys.readouterr().out.splitlines()[0]


# ───────────────────────────── S3 (1) ─────────────────────────────

def test_s3_type_set_is_pinned():
    assert triage_report.TYPE_LABELS == frozenset(
        {"bug", "enhancement", "documentation", "research", "question", "epic"})


def test_s3_1_missing_classes_listed_separately(monkeypatch):
    issues = [
        team(1, labels=["bug", "area:core", "priority:low"]),          # complete
        team(2, labels=["area:core", "priority:low"]),                  # no type
        team(3, labels=["epic", "priority:low"]),                       # epic is a type; no area
        team(4, labels=["question", "area:core"]),                      # no priority
        team(5, labels=["priority-ish", "area"]),                       # none of the three
    ]
    r = build(monkeypatch, FakeGH(issues))
    assert numbers(r.s3_missing_type) == [2, 5]
    assert numbers(r.s3_missing_area) == [3, 5]
    assert numbers(r.s3_missing_priority) == [4, 5]
    assert (r.s3_missing_type.count, r.s3_missing_area.count, r.s3_missing_priority.count) == (2, 2, 2)


def test_s3_1_every_type_label_counts_as_a_type(monkeypatch):
    issues = [team(i + 1, labels=[t, "area:x", "priority:x"]) for i, t in
              enumerate(["bug", "enhancement", "documentation", "research", "question", "epic"])]
    r = build(monkeypatch, FakeGH(issues))
    assert r.s3_missing_type.items == []


# ───────────────────────────── S3 (2) and (3) ─────────────────────────────

LABELS = ["proposed-close", "needs-decision"]


def fields(label):
    return (("s3_proposed_close", "s3_proposed_close_unknown") if label == "proposed-close"
            else ("s3_needs_decision", "s3_needs_decision_unknown"))


@pytest.mark.parametrize("label", LABELS)
def test_s3_age_exactly_14_days_included_one_second_less_excluded(monkeypatch, label):
    issues = [team(1, labels=[label]), team(2, labels=[label])]
    events = {1: [labeled(label, ago(14 * DAY))], 2: [labeled(label, ago(14 * DAY - 1))]}
    r = build(monkeypatch, FakeGH(issues, events=events))
    old, unknown = (getattr(r, f) for f in fields(label))
    assert numbers(old) == [1]
    assert unknown.items == []


@pytest.mark.parametrize("label", LABELS)
def test_s3_removed_and_readded_counts_from_the_readd(monkeypatch, label):
    events = {1: [labeled(label, ago(30 * DAY)), labeled(label, ago(25 * DAY), "unlabeled"),
                  labeled(label, ago(5 * DAY))]}
    r = build(monkeypatch, FakeGH([team(1, labels=[label])], events=events))
    old, unknown = (getattr(r, f) for f in fields(label))
    assert old.items == [] and unknown.items == []


@pytest.mark.parametrize("label", LABELS)
def test_s3_newest_labeled_event_wins_whatever_the_list_order(monkeypatch, label):
    events = {1: [labeled(label, ago(5 * DAY)), labeled(label, ago(30 * DAY))]}
    r = build(monkeypatch, FakeGH([team(1, labels=[label])], events=events))
    assert getattr(r, fields(label)[0]).items == []


@pytest.mark.parametrize("label", LABELS)
def test_s3_no_labeled_event_goes_to_age_unknown_not_dropped(monkeypatch, label):
    issues = [team(1, labels=[label]), team(2, labels=[label])]
    events = {1: [], 2: [labeled("bug", ago(90 * DAY)), labeled(label, ago(1 * DAY), "unlabeled")]}
    r = build(monkeypatch, FakeGH(issues, events=events))
    old, unknown = (getattr(r, f) for f in fields(label))
    assert old.items == []
    assert numbers(unknown) == [1, 2]
    assert unknown.count == 2
    text = triage_report.render_text(r)
    assert "age unknown" in text.lower() and "#1 " in text and "#2 " in text


@pytest.mark.parametrize("label", LABELS)
def test_s3_age_printed_in_whole_days_and_events_only_for_labelled_issues(monkeypatch, label):
    issues = [team(1, labels=[label]), team(2, labels=["bug"])]
    fake = FakeGH(issues, events={1: [labeled(label, ago(20 * DAY + 5 * 3600))]})
    r = build(monkeypatch, fake)
    old = getattr(r, fields(label)[0])
    assert old.items[0]["age_days"] == 20
    assert "(20 days)" in triage_report.render_text(r)
    assert fake.paths("events") == [f"/repos/{REPO}/issues/1/events"]


def test_s3_labels_do_not_leak_between_proposed_close_and_needs_decision(monkeypatch):
    issues = [team(1, labels=["proposed-close"]), team(2, labels=["needs-decision"])]
    events = {1: [labeled("proposed-close", ago(20 * DAY))], 2: [labeled("needs-decision", ago(20 * DAY))]}
    r = build(monkeypatch, FakeGH(issues, events=events))
    assert numbers(r.s3_proposed_close) == [1]
    assert numbers(r.s3_needs_decision) == [2]


# ───────────────────────────── S3 (4), (5) ─────────────────────────────

def test_s3_4_priority_high_listed(monkeypatch):
    issues = [team(1, labels=["priority:high"]), team(2, labels=["priority:low"]),
              team(3, labels=["bug", "priority:high"])]
    r = build(monkeypatch, FakeGH(issues))
    assert numbers(r.s3_priority_high) == [1, 3] and r.s3_priority_high.count == 2


def test_s3_5_totals_open_closed_oldest(monkeypatch):
    issues = [mk(30, created="2026-08-01T00:00:00Z"), mk(59, created="2026-05-09T10:00:00Z"),
              mk(60, created="2026-05-09T11:00:00Z")]
    fake = FakeGH(issues, closed_total=3950)
    r = build(monkeypatch, fake)
    assert r.s3_open.count == 3 and r.open_collected.count == 3
    assert r.s3_closed.count == 3950
    assert (r.s3_oldest.number, r.s3_oldest.created_at) == (59, "2026-05-09T10:00:00Z")
    closed_calls = [p for (path, p, _) in fake.calls if path == "/search/issues" and "is:closed" in p["q"]]
    assert len(closed_calls) == 1 and str(closed_calls[0]["per_page"]) == "1"


def test_s3_5_open_is_the_search_total_not_a_recount(monkeypatch):
    # collect_search guarantees collected == total; the report prints the search total
    r = build(monkeypatch, FakeGH([mk(i) for i in range(1, 8)]))
    assert r.s3_open.count == 7


def test_s3_5_no_open_issues(monkeypatch):
    r = build(monkeypatch, FakeGH([]))
    assert r.s3_oldest.number is None
    assert "none" in triage_report.render_text(r).lower()


def test_closed_count_refuses_incomplete_results(monkeypatch):
    use(monkeypatch, FakeGH([], closed_incomplete=True))
    with pytest.raises(triage_report.IntegrityError, match=r"incomplete_results"):
        triage_report.build_report(REPO, NOW, SINCE)


# ───────────────────────────── S4 ─────────────────────────────

def test_s4_both_labels_listed_one_label_not(monkeypatch):
    issues = [team(1, labels=["deprioritized", "quick-fix"]), team(2, labels=["deprioritized"]),
              team(3, labels=["quick-fix"]), team(4, labels=["deprioritized", "bug"])]
    r = build(monkeypatch, FakeGH(issues))
    assert numbers(r.s4_both) == [1] and r.s4_both.count == 1
    assert r.s4_deprioritized.count == 3


# ───────────────────────────── read-only contract ─────────────────────────────

class Capture:
    def __init__(self, stdout="[]", returncode=0, stderr=""):
        self.argvs = []
        self._out = stdout
        self._rc = returncode
        self._err = stderr

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, self._rc, stdout=self._out, stderr=self._err)


def test_readonly_every_call_shape_starts_with_gh_api_x_get(monkeypatch):
    cap = Capture(stdout=json.dumps({"total_count": 0, "incomplete_results": False, "items": []}))
    monkeypatch.setattr(triage_report.subprocess, "run", cap)
    triage_report._api("/search/issues", {"q": "repo:a/b is:issue", "per_page": "1"})
    triage_report._api("/search/issues", {"q": "repo:a/b is:issue"}, paginate=True)
    cap.__dict__["_out"] = "[]"
    triage_report._api("/repos/a/b/issues/1/comments", paginate=True)
    triage_report._api("/repos/a/b/issues/1")
    assert len(cap.argvs) >= 4
    for argv in cap.argvs:
        assert argv[:4] == ["gh", "api", "-X", "GET"], argv
        assert argv[4].startswith("/"), argv
        flags = [a for a in argv[5:] if a.startswith("-")]
        assert set(flags) <= {"-f", "--paginate"}, argv


def test_readonly_params_become_f_pairs_and_list_paginates(monkeypatch):
    cap = Capture(stdout="[]")
    monkeypatch.setattr(triage_report.subprocess, "run", cap)
    triage_report._api("/repos/a/b/issues/1/events", {"x": "y"}, paginate=True)
    argv = cap.argvs[0]
    assert "--paginate" in argv
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
    assert "x=y" in pairs and "per_page=100" in pairs


def test_search_paginate_follows_pages_until_a_short_page(monkeypatch):
    pages = {"1": [{"number": i} for i in range(100)], "2": [{"number": 100}]}

    def run(argv, **kw):
        page = next(a.split("=", 1)[1] for a in argv if a.startswith("page="))
        body = {"total_count": 101, "incomplete_results": False, "items": pages[page]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(triage_report.subprocess, "run", run)
    out = triage_report._api("/search/issues", {"q": "x"}, paginate=True)
    assert len(out["items"]) == 101 and out["total_count"] == 101


def test_list_paginate_concatenates_concatenated_json_arrays(monkeypatch):
    cap = Capture(stdout='[{"a":1},{"a":2}][{"a":3}]')
    monkeypatch.setattr(triage_report.subprocess, "run", cap)
    assert triage_report._api("/repos/a/b/issues/1/comments", paginate=True) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_nonzero_exit_raises_integrity_error_naming_path_and_stderr_prefix(monkeypatch):
    cap = Capture(returncode=1, stderr="E" * 500)
    monkeypatch.setattr(triage_report.subprocess, "run", cap)
    with pytest.raises(triage_report.IntegrityError) as e:
        triage_report._api("/repos/a/b/issues/9")
    msg = str(e.value)
    assert "/repos/a/b/issues/9" in msg and "E" * 200 in msg and "E" * 201 not in msg


def test_missing_gh_binary_is_an_integrity_error(monkeypatch):
    def run(argv, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(triage_report.subprocess, "run", run)
    with pytest.raises(triage_report.IntegrityError, match=r"gh"):
        triage_report._api("/x")


def test_unparseable_output_is_an_integrity_error(monkeypatch):
    monkeypatch.setattr(triage_report.subprocess, "run", Capture(stdout="not json"))
    with pytest.raises(triage_report.IntegrityError, match=r"/x"):
        triage_report._api("/x")


def test_source_names_no_write_verbs():
    src = (_TOOLS / "triage_report.py").read_text(encoding="utf-8")
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        assert verb not in src, verb
    assert dataclasses.is_dataclass(triage_report.Report)


def test_the_search_queries_sent_are_exactly_the_open_and_closed_queries(monkeypatch):
    # The fake ignores the query text when serving items, so the text actually SENT is asserted here.
    fake = FakeGH([mk(i) for i in range(1, 4)], closed_total=7)
    build(monkeypatch, fake)
    searches = [(p, params) for (p, params, _) in fake.calls if p == "/search/issues"]
    open_calls = [params for (_, params) in searches if "is:closed" not in params["q"]]
    assert open_calls, "no open search was sent"
    for params in open_calls:
        assert params["q"] == f"repo:{REPO} is:issue is:open"
    closed_calls = [params for (_, params) in searches
                    if params["q"] == f"repo:{REPO} is:issue is:closed"]
    assert len(closed_calls) == 1
    assert str(closed_calls[0]["per_page"]) == "1"
