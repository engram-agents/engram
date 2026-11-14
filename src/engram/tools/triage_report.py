#!/usr/bin/env python3
"""triage_report -- a read-only report over a repository's open issues, for the seat on triage duty.

It READS issues through the GitHub API and prints numbers, each stating what it ranges over. It
collects every open issue through the search API and refuses to report unless the number collected
equals the number the search said it should return. It never writes, labels, comments or closes.

Usage
    triage_report.py [--repo OWNER/REPO] [--since YYYY-MM-DD] [--now YYYY-MM-DDTHH:MM:SSZ] [--json]

Sections: S1 outside reports (daily), S2 security line, S3 weekly (1 missing labels, 2 proposed-close,
3 needs-decision, 4 priority:high, 5 totals), S4 rule 2B (deprioritized and quick-fix).

A "team account" is any account whose ``author_association`` is OWNER, MEMBER or COLLABORATOR. For an
issue that was mirrored into the repository by an account, "author" means that account, not the
original reporter.

Exit codes: 0 a report was produced (findings never change it); 2 usage error; 3 integrity error
(message on stderr starting ``ERROR:``, nothing partial on stdout).

The only network surface is the ``gh`` command-line tool, called as ``gh api -X GET`` through the one
module-level seam ``_api``. Standard library only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DEFAULT_REPO = "engram-agents/engram"
TEAM_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# A label vocabulary change is a code change: the type set is pinned by a test.
TYPE_LABELS = frozenset({"bug", "enhancement", "documentation", "research", "question", "epic"})
AREA_PREFIX = "area:"
PRIORITY_PREFIX = "priority:"
SEARCH_RESULT_CAP = 1000
PAGE_SIZE = 100
MAX_SEARCH_PAGE = SEARCH_RESULT_CAP // PAGE_SIZE
LABEL_AGE_SECONDS = 14 * 86400
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class IntegrityError(Exception):
    """What was collected cannot be shown to equal what exists. The report is refused."""


# ───────────────────────────── the one network seam ─────────────────────────────

def _run(path: str, argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise IntegrityError(f"{path}: the gh command-line tool was not found ({exc})") from exc
    if proc.returncode != 0:
        raise IntegrityError(f"{path}: gh exited {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout


def _loads(path: str, text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise IntegrityError(f"{path}: output was not JSON ({exc})") from exc


def _loads_stream(path: str, text: str) -> list:
    """``gh api --paginate`` prints one JSON array per page; concatenate them."""
    decoder = json.JSONDecoder()
    out: list = []
    pos, n = 0, len(text)
    while True:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            return out
        try:
            value, pos = decoder.raw_decode(text, pos)
        except ValueError as exc:
            raise IntegrityError(f"{path}: output was not JSON ({exc})") from exc
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)


def _api(path: str, params: dict | None = None, paginate: bool = False) -> object:
    """Run ``gh api -X GET <path>`` with ``-f key=value`` per param and return the parsed JSON.

    paginate=True: for the search endpoint, request page=1.. (per_page=100) until a page has fewer
    than 100 items and return one dict whose ``items`` are all pages' items (``total_count`` from the
    first page, ``incomplete_results`` true if any page said so); for list endpoints use ``--paginate``
    and return the concatenated list.
    """
    params = dict(params or {})

    def argv_for(extra: dict, flags: tuple = ()) -> list[str]:
        merged = {**params, **extra}
        argv = ["gh", "api", "-X", "GET", path, *flags]
        for key, value in merged.items():
            argv += ["-f", f"{key}={value}"]
        return argv

    if not paginate:
        return _loads(path, _run(path, argv_for({})))
    if path.rstrip("/").endswith("/search/issues") or path.lstrip("/").startswith("search/"):
        merged: dict = {"total_count": None, "incomplete_results": False, "items": []}
        page = 1
        while True:
            body = _loads(path, _run(path, argv_for({"per_page": PAGE_SIZE, "page": page})))
            items = body.get("items", []) if isinstance(body, dict) else []
            if merged["total_count"] is None:
                merged["total_count"] = body.get("total_count")
            merged["incomplete_results"] = merged["incomplete_results"] or bool(body.get("incomplete_results"))
            merged["items"].extend(items)
            if len(items) < PAGE_SIZE or page >= MAX_SEARCH_PAGE:
                return merged
            page += 1
    return _loads_stream(path, _run(path, argv_for({"per_page": PAGE_SIZE}, ("--paginate",))))


# ───────────────────────────── collection with the integrity check ─────────────────────────────

def collect_search(q: str) -> list[dict]:
    """Collect every result of an issue search and prove the collection is whole.

    Raises IntegrityError unless the number of items equals the first page's ``total_count``
    ("collected N, search said M"), when ``total_count`` exceeds the search API's cap of 1000
    results, when any page says ``incomplete_results``, or when the same issue appears twice.
    """
    items: list[dict] = []
    total = None
    page = 1
    while True:
        body = _api("/search/issues", {"q": q, "per_page": PAGE_SIZE, "page": page})
        if not isinstance(body, dict) or "total_count" not in body:
            raise IntegrityError(f"search {q!r} page {page}: response has no total_count")
        if total is None:
            total = body["total_count"]
            if total > SEARCH_RESULT_CAP:
                raise IntegrityError(
                    f"search {q!r} matches {total} issues; the search API returns at most "
                    f"{SEARCH_RESULT_CAP} results, so the collection would be truncated"
                )
        if body.get("incomplete_results"):
            raise IntegrityError(f"search {q!r} page {page}: the API reported incomplete_results")
        page_items = body.get("items", [])
        items.extend(page_items)
        if len(page_items) < PAGE_SIZE or page >= MAX_SEARCH_PAGE:
            break
        page += 1
    if len(items) != total:
        raise IntegrityError(f"search {q!r}: collected {len(items)}, search said {total}")
    distinct = {item.get("number") for item in items}
    if len(distinct) != len(items):
        raise IntegrityError(
            f"search {q!r}: {len(items) - len(distinct)} duplicate issue(s) among {len(items)} collected"
        )
    return items


# ───────────────────────────── the report ─────────────────────────────

@dataclass
class Counted:
    """A number, what it ranges over, and the items behind it."""
    count: int
    ranges_over: str
    items: list = field(default_factory=list)


@dataclass
class Oldest:
    number: int | None
    created_at: str | None
    ranges_over: str


@dataclass
class Report:
    repo: str
    now: str
    since: str
    open_collected: Counted
    s1_candidates: Counted
    s1_outside: Counted
    s2_since: Counted
    s2_total: Counted
    s3_missing_type: Counted
    s3_missing_area: Counted
    s3_missing_priority: Counted
    s3_proposed_close: Counted
    s3_proposed_close_unknown: Counted
    s3_needs_decision: Counted
    s3_needs_decision_unknown: Counted
    s3_priority_high: Counted
    s3_open: Counted
    s3_closed: Counted
    s3_oldest: Oldest
    s4_both: Counted
    s4_deprioritized: Counted


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _labels(issue: dict) -> set[str]:
    return {lab.get("name", "") for lab in issue.get("labels", [])}


def _is_team(association) -> bool:
    return association in TEAM_ASSOCIATIONS


def _author(issue: dict) -> str:
    return (issue.get("user") or {}).get("login", "")


def _brief(issue: dict) -> dict:
    return {"number": issue["number"], "title": issue.get("title", "")}


def _label_age_section(repo: str, now: datetime, issues: list[dict], label: str):
    """Issues carrying ``label`` whose newest ``labeled`` event is at least 14 days old, and those with no event."""
    carrying = [i for i in issues if label in _labels(i)]
    old, unknown = [], []
    for issue in carrying:
        events = _api(f"/repos/{repo}/issues/{issue['number']}/events", paginate=True)
        stamps = [parse_ts(e["created_at"]) for e in events
                  if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
        if not stamps:
            unknown.append(_brief(issue))
            continue
        age = (now - max(stamps)).total_seconds()
        if age >= LABEL_AGE_SECONDS:
            old.append({**_brief(issue), "age_days": int(age // 86400)})
    scope = (f"open issues labelled `{label}` ({len(carrying)} of them); age = now minus the newest "
             f"`labeled` event for that label, at least 14 full days")
    return (
        Counted(len(old), scope, old),
        Counted(len(unknown), f"open issues labelled `{label}` ({len(carrying)} of them) with no "
                              f"`labeled` event, so their age is unknown", unknown),
    )


def build_report(repo: str, now: datetime, since: datetime) -> Report:
    open_q = f"repo:{repo} is:issue is:open"
    closed_q = f"repo:{repo} is:issue is:closed"
    issues = collect_search(open_q)
    n_open = len(issues)
    open_scope = f"search `{open_q}`; collected equals the search total_count"

    # S1: non-team author and no team comment; comments fetched for candidates only.
    candidates = [i for i in issues if not _is_team(i.get("author_association"))]
    outside = []
    for issue in candidates:
        comments = _api(f"/repos/{repo}/issues/{issue['number']}/comments", paginate=True)
        if any(_is_team(c.get("author_association")) for c in comments):
            continue
        outside.append({"number": issue["number"], "title": issue.get("title", ""),
                        "author": _author(issue), "created_at": issue["created_at"],
                        "comments_seen": len(comments)})
    team_rule = "author_association not OWNER/MEMBER/COLLABORATOR"

    # S2: security line.
    security = [i for i in issues if "area:security" in _labels(i)]
    recent = [{"number": i["number"], "title": i.get("title", ""), "created_at": i["created_at"]}
              for i in security if parse_ts(i["created_at"]) >= since]

    # S3 (1): missing a type, an area or a priority.
    def missing(pred):
        return [_brief(i) for i in issues if not any(pred(name) for name in _labels(i))]

    miss_scope = f"the {n_open} open issues collected by `{open_q}`; "
    old_pc, unk_pc = _label_age_section(repo, now, issues, "proposed-close")
    old_nd, unk_nd = _label_age_section(repo, now, issues, "needs-decision")

    # S3 (5): totals.
    closed_body = _api("/search/issues", {"q": closed_q, "per_page": 1})
    if not isinstance(closed_body, dict) or "total_count" not in closed_body:
        raise IntegrityError(f"search {closed_q!r}: response has no total_count")
    if closed_body.get("incomplete_results"):
        raise IntegrityError(f"search {closed_q!r}: the API reported incomplete_results")
    oldest = min(issues, key=lambda i: i["created_at"]) if issues else None

    # S4: rule 2B.
    both = [_brief(i) for i in issues if {"deprioritized", "quick-fix"} <= _labels(i)]
    depr = [i for i in issues if "deprioritized" in _labels(i)]

    return Report(
        repo=repo, now=fmt_ts(now), since=fmt_ts(since),
        open_collected=Counted(n_open, open_scope),
        s1_candidates=Counted(len(candidates), f"the {n_open} open issues collected by `{open_q}` "
                                               f"whose {team_rule}"),
        s1_outside=Counted(len(outside), f"outside-author open issues with no comment from a team "
                                         f"account ({team_rule}); comments fetched for the "
                                         f"{len(candidates)} candidate issues only", outside),
        s2_since=Counted(len(recent), f"the {n_open} open issues collected by `{open_q}`, labelled "
                                      f"`area:security`, created_at on or after {fmt_ts(since)}", recent),
        s2_total=Counted(len(security), f"the {n_open} open issues collected by `{open_q}`, labelled "
                                        f"`area:security`, any age"),
        s3_missing_type=Counted(len(missing(lambda n: n in TYPE_LABELS)),
                                miss_scope + "no label among " + ", ".join(sorted(TYPE_LABELS)),
                                missing(lambda n: n in TYPE_LABELS)),
        s3_missing_area=Counted(len(missing(lambda n: n.startswith(AREA_PREFIX))),
                                miss_scope + f"no label starting `{AREA_PREFIX}`",
                                missing(lambda n: n.startswith(AREA_PREFIX))),
        s3_missing_priority=Counted(len(missing(lambda n: n.startswith(PRIORITY_PREFIX))),
                                    miss_scope + f"no label starting `{PRIORITY_PREFIX}`",
                                    missing(lambda n: n.startswith(PRIORITY_PREFIX))),
        s3_proposed_close=old_pc, s3_proposed_close_unknown=unk_pc,
        s3_needs_decision=old_nd, s3_needs_decision_unknown=unk_nd,
        s3_priority_high=Counted(
            len([i for i in issues if "priority:high" in _labels(i)]),
            f"the {n_open} open issues collected by `{open_q}`, labelled `priority:high`",
            [_brief(i) for i in issues if "priority:high" in _labels(i)]),
        s3_open=Counted(n_open, f"search `{open_q}`, total_count, equal to the collected count"),
        s3_closed=Counted(closed_body["total_count"],
                          f"search `{closed_q}`, total_count; one page of per_page=1 is enough"),
        s3_oldest=Oldest(oldest["number"] if oldest else None,
                         oldest["created_at"] if oldest else None,
                         f"the {n_open} open issues collected by `{open_q}`, smallest created_at"),
        s4_both=Counted(len(both), f"the {n_open} open issues collected by `{open_q}`, carrying both "
                                   f"`deprioritized` and `quick-fix`", both),
        s4_deprioritized=Counted(len(depr), f"the {n_open} open issues collected by `{open_q}`, "
                                            f"carrying `deprioritized`"),
    )


# ───────────────────────────── rendering ─────────────────────────────

def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _count(label: str, c: Counted) -> str:
    return f"{label}: {c.count} (ranges over: {c.ranges_over})"


def render_text(report: Report) -> str:
    r = report
    out = [f"Triage report: repo={r.repo} now={r.now} since={r.since}",
           _count("Open issues collected", r.open_collected), ""]

    out += ["== S1 OUTSIDE REPORTS (daily) ==",
            _count("Outside-author open issues (candidates)", r.s1_candidates),
            _count("Outside-author open issues with no team comment", r.s1_outside)]
    out += [f"  - #{i['number']} {_one_line(i['title'])} | by {i['author']} | created {i['created_at']} "
            f"| comments seen: {i['comments_seen']}" for i in r.s1_outside.items]

    out += ["", "== S2 SECURITY ==",
            _count("Open area:security issues created since the since value", r.s2_since)]
    out += [f"  - #{i['number']} {_one_line(i['title'])} | created {i['created_at']}" for i in r.s2_since.items]
    out += [_count("Open area:security issues in total", r.s2_total)]

    out += ["", "== S3.1 MISSING A TYPE, AN AREA OR A PRIORITY (weekly) =="]
    for label, c in (("Missing a type", r.s3_missing_type), ("Missing an area", r.s3_missing_area),
                     ("Missing a priority", r.s3_missing_priority)):
        out.append(_count(label, c))
        out += [f"  - #{i['number']} {_one_line(i['title'])}" for i in c.items]

    for heading, label, old, unknown in (
        ("S3.2 PROPOSED-CLOSE, LABEL AT LEAST 14 DAYS OLD", "proposed-close",
         r.s3_proposed_close, r.s3_proposed_close_unknown),
        ("S3.3 NEEDS-DECISION, LABEL AT LEAST 14 DAYS OLD", "needs-decision",
         r.s3_needs_decision, r.s3_needs_decision_unknown),
    ):
        out += ["", f"== {heading} ==", _count(f"{label} label at least 14 days old", old)]
        out += [f"  - #{i['number']} {_one_line(i['title'])} ({i['age_days']} days)" for i in old.items]
        out.append(_count(f"{label} label age unknown", unknown))
        out += [f"  - #{i['number']} {_one_line(i['title'])}" for i in unknown.items]

    out += ["", "== S3.4 PRIORITY:HIGH ==", _count("Open priority:high issues", r.s3_priority_high)]
    out += [f"  - #{i['number']} {_one_line(i['title'])}" for i in r.s3_priority_high.items]

    o = r.s3_oldest
    oldest = f"#{o.number}, created {o.created_at}" if o.number is not None else "none (no open issues)"
    out += ["", "== S3.5 TOTALS ==", _count("Open", r.s3_open), _count("Closed", r.s3_closed),
            f"Oldest open issue: {oldest} (ranges over: {o.ranges_over})"]

    out += ["", "== S4 RULE 2B (deprioritized and quick-fix) ==",
            _count("Open issues carrying both deprioritized and quick-fix", r.s4_both)]
    out += [f"  - #{i['number']} {_one_line(i['title'])}" for i in r.s4_both.items]
    out.append(_count("Open issues carrying deprioritized", r.s4_deprioritized))
    return "\n".join(out) + "\n"


# ───────────────────────────── command line ─────────────────────────────

def _parse_since(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")


def _parse_now(value: str) -> datetime:
    try:
        return parse_ts(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not YYYY-MM-DDTHH:MM:SSZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="triage_report", description="Read-only triage report over a repository's open issues.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="OWNER/REPO (default %(default)s)")
    parser.add_argument("--since", type=_parse_since, default=None,
                        help="YYYY-MM-DD, UTC midnight (default: now minus 7 days)")
    parser.add_argument("--now", type=_parse_now, default=None,
                        help="YYYY-MM-DDTHH:MM:SSZ (default: the current UTC time)")
    parser.add_argument("--json", action="store_true", help="print the same data as JSON")
    args = parser.parse_args(argv)

    now = args.now or datetime.now(timezone.utc).replace(microsecond=0)
    since = args.since or (now - timedelta(days=7))
    try:
        report = build_report(args.repo, now, since)
    except IntegrityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
