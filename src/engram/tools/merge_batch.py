#!/usr/bin/env python3
"""merge_batch.py -- the merge seat's batched press, with its gates made mechanical.

A batch is a list of PRs merged in order onto one base, tested once as a whole
(scratch merge of the base plus every head), then pressed one by one.  Every
step that used to be done by hand and could be skipped is a refusal here.

One JSON state file (``--state PATH``) carries the batch between subcommands:

  build       Fetch the base and each refs/pull/N/head into a fresh repo under
              --scratch, merge the heads in order on a detached checkout of the
              base, record the base sha, each head sha and the tree sha after
              each step.  REFUSES (stops, names the PR) on a merge conflict.
              Never touches the caller's own clone or branch.
  record-run  Record the whole ``tests/`` run on the top tree from a JUnit XML.
              REFUSES a --tree that is not the final step tree, and REFUSES to
              overwrite an existing recorded run unless ``--replace`` is given
              (the previous record is kept under ``previous_runs``).
  window      Print the announcement line for the Office (first line, UTC);
              ``--record-seq SEQ`` stores the posted crossing's seq.  A new
              ``--minutes`` line clears the recorded seq: the new line must be
              posted and its seq recorded.  A blank seq is refused.
  press       Merge each unpressed step in order.  REFUSES unless: a window seq
              is recorded and the window's ``until`` has not passed; the recorded run is green (a red run stops and lists
              the failing cells, even in files no PR touches; only
              ``--accept-red REF``, a reference to a human ruling, proceeds --
              this tool never decides a red is a flake); the base tip is the
              recorded base (or the previous step's merge commit); the PR head
              is the recorded head; a non-author APPROVED review exists on that
              exact head; the PR is not a gh-stack member (read from
              both the REST pull object and the issue events; either saying
              member refuses); and, within ``--ceiling`` seconds,
              mergeStateStatus is CLEAN or the strict check read says
              success (UNKNOWN or pending never presses).
              A merge call that times out, cannot start, or exits non-zero is
              REFUSED as an unknown outcome (read the base before re-running).
              After each merge it records ``merged: true`` and the observed tip,
              then asserts the new tip's parent is the expected base and its
              tree is the recorded step tree; otherwise it stops and presses no
              further step.  A step left ``merged: true, pressed: false`` is
              refused on a re-run (no auto-repair).
  status      Print the state.

All GitHub access goes through one injectable client (``GhClient`` in
production); time and sleep are injectable, so the poll is testable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bare import: this file ships beside baton_gate.py (see baton.py's import note).
from baton_gate import (
    _gh_identity,
    _gh_read_json,
    _head_check_runs,
    _scrub_gh_detail,
    _strict_required_check_state,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

DEFAULT_CEILING_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_CHECK_NAME = "pr-checks"
_GIT_IDENT = ["-c", "user.name=merge-batch", "-c", "user.email=merge-batch@localhost",
              "-c", "commit.gpgsign=false"]


# Prefix of the detail ``merge`` returns when it refused before gh ran: the merge
# was NOT sent, which is a different state from "outcome unknown".
_NOT_SENT = "merge NOT sent (gh identity check refused before gh ran)"


class Refused(Exception):
    """A gate refused; the message says which and why."""


# ---------------------------------------------------------------------------
# GitHub client (production)
# ---------------------------------------------------------------------------

class GhClient:
    """Shells ``gh``; every call takes the gate's identity handling."""

    def _json(self, args):
        body, why = _gh_read_json(args)
        if why is not None:
            raise Refused(f"gh {' '.join(args[:3])} failed: {why}")
        return body

    def base_tip(self, repo, base):
        return self._json(["api", f"repos/{repo}/git/ref/heads/{base}"])["object"]["sha"]

    def pr_head(self, repo, n):
        return self._json(["api", f"repos/{repo}/pulls/{n}"])["head"]["sha"]

    def pr_author(self, repo, n):
        return self._json(["api", f"repos/{repo}/pulls/{n}"])["user"]["login"]

    def _paged(self, path, what):
        """Read every page of a list endpoint (100 per page, 10 pages at most)."""
        sep = "&" if "?" in path else "?"
        out, page = [], 1
        while True:
            rows = self._json(["api", f"{path}{sep}per_page=100&page={page}"])
            out.extend(rows)
            if len(rows) < 100:
                return out
            page += 1
            if page > 10:
                raise Refused(f"more than 1000 {what}; cannot read them whole")

    def reviews(self, repo, n):
        return self._paged(f"repos/{repo}/pulls/{n}/reviews", "reviews")

    def merge_state(self, repo, n):
        return self._json(["pr", "view", str(n), "--repo", repo,
                           "--json", "mergeStateStatus"]).get("mergeStateStatus") or ""

    def check_runs(self, repo, sha):
        return _head_check_runs(repo, sha)

    def pr_stack(self, repo, n):
        pull = self._json(["api", f"repos/{repo}/pulls/{n}"])
        return _stack_member(pull, self._paged(f"repos/{repo}/issues/{n}/events", "issue events"))

    def merge(self, repo, n, head):
        gh_kw, refusal = _gh_identity()
        if refusal is not None:
            return False, f"{_NOT_SENT}: {refusal[1]}"
        res = subprocess.run(
            ["gh", "pr", "merge", str(n), "--repo", repo, "--squash",
             "--match-head-commit", head, "--body", ""],
            capture_output=True, text=True, timeout=120, **gh_kw)
        detail = _scrub_gh_detail((res.stderr or res.stdout or "").strip())
        return res.returncode == 0, detail


def _stack_member(pull_json, events):
    """True when EITHER source says the PR is a gh-stack member (fail closed).

    The REST pull object carries a truthy ``stack`` for members (still present
    after merge); the issue events carry ``added_to_stack`` / ``removed_from_stack``
    and the LAST such event, in API order, decides that source."""
    if pull_json.get("stack"):
        return True
    last = None
    for ev in events:
        if ev.get("event") in ("added_to_stack", "removed_from_stack"):
            last = ev["event"]
    return last == "added_to_stack"


def _default_remote(repo):
    """HTTPS, so the scratch fetch needs no SSH key on the merge seat's host."""
    return f"https://github.com/{repo}.git"


# ---------------------------------------------------------------------------
# git / state helpers
# ---------------------------------------------------------------------------

def _git(cwd, *args, check=True):
    res = subprocess.run(["git", *_GIT_IDENT, *args], cwd=str(cwd),
                         capture_output=True, text=True)
    if check and res.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])} failed: {(res.stderr or res.stdout).strip()}")
    return res


def _load(path):
    p = Path(path)
    if not p.exists():
        raise Refused(f"no state file at {p}; run `build` first")
    return json.loads(p.read_text())


def _save(path, state):
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _has_current_approval(reviews, author, head):
    """True when a non-author reviewer's latest decisive review is an APPROVED
    one on `head`.  APPROVED, CHANGES_REQUESTED and DISMISSED are decisive (a
    later one replaces an earlier one by the same reviewer, in list order);
    COMMENTED is not.  An approval of an older commit does not count."""
    decisive = {}
    for r in reviews:
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            decisive[(r.get("user") or {}).get("login")] = r
    return any(login and login != author and r.get("state") == "APPROVED"
               and r.get("commit_id") == head for login, r in decisive.items())


def _out(msg):
    print(msg, file=sys.stdout)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_build(args, client, clock, sleep):
    state_path = Path(args.state)
    if state_path.exists():
        raise Refused(f"{state_path} already exists; a batch state is never overwritten")
    prs = [int(x) for x in args.prs.split(",") if x.strip()]
    if not prs:
        raise Refused("--prs is empty")
    remote = args.remote or _default_remote(args.repo)
    scratch = Path(args.scratch)
    work = scratch / "repo"
    if work.exists():
        raise Refused(f"{work} already exists; use a fresh --scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    _git(scratch, "init", "-q", str(work))
    _git(work, "fetch", "-q", remote, f"refs/heads/{args.base}")
    base_sha = _git(work, "rev-parse", "FETCH_HEAD").stdout.strip()
    heads = {}
    for n in prs:
        _git(work, "fetch", "-q", remote, f"refs/pull/{n}/head")
        heads[n] = _git(work, "rev-parse", "FETCH_HEAD").stdout.strip()
    _git(work, "checkout", "-q", "--detach", base_sha)
    steps = []
    for n in prs:
        res = _git(work, "merge", "--no-edit", heads[n], check=False)
        if res.returncode != 0:
            _git(work, "merge", "--abort", check=False)
            raise Refused(f"merge conflict at #{n} (after {[s['pr'] for s in steps]}); "
                          f"build stopped, no state written")
        tree = _git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
        steps.append({"pr": n, "head": heads[n], "tree": tree, "pressed": False})
    _save(state_path, {"repo": args.repo, "base": args.base, "remote": remote,
                       "check_name": args.check_name, "worktree": str(work),
                       "base_sha": base_sha, "steps": steps})
    _out(f"built {len(steps)} step(s) on {args.base} {base_sha[:12]}; "
         f"top tree {steps[-1]['tree'][:12]}")
    return EXIT_OK


def _parse_junit(path):
    root = ET.parse(path).getroot()
    tests, skipped, failures, errors, failing = 0, 0, 0, 0, []
    for case in root.iter("testcase"):
        tests += 1
        kinds = [c.tag for c in case if c.tag in ("failure", "error")]
        if not kinds:
            if any(c.tag == "skipped" for c in case):
                skipped += 1
            continue
        if "error" in kinds:
            errors += 1
        else:
            failures += 1
        failing.append(f"{case.get('classname') or ''}::{case.get('name') or ''}")
    return {"tests": tests, "passed": tests - failures - errors - skipped,
            "skipped": skipped, "failures": failures, "errors": errors, "failing": failing}


def cmd_record_run(args, client, clock, sleep):
    state = _load(args.state)
    final = state["steps"][-1]["tree"]
    if args.tree != final:
        raise Refused(f"--tree {args.tree} is not the final step tree {final}; "
                      f"the run must be on the top tree of this batch")
    if state.get("run") and not args.replace:
        raise Refused("a run is already recorded; pass --replace to record a new one "
                      "(the previous record is kept under previous_runs)")
    totals = _parse_junit(args.junit)
    if totals["tests"] == 0:
        raise Refused(f"{args.junit} has no <testcase> elements; an empty report "
                      f"cannot stand for a whole run")
    if state.get("run"):
        state.setdefault("previous_runs", []).append(state["run"])
    state["run"] = {"rc": int(args.rc), "tree": args.tree, **totals}
    _save(args.state, state)
    _out(f"recorded run: rc={args.rc} tests={totals['tests']} passed={totals['passed']} "
         f"skipped={totals['skipped']} failures={totals['failures']} errors={totals['errors']}")
    return EXIT_OK


_UNTIL_FMT = "%Y-%m-%dT%H:%M:%SZ"


def cmd_window(args, client, clock, sleep):
    state = _load(args.state)
    if args.record_seq is not None:
        if not args.record_seq.strip():
            raise Refused("--record-seq is empty")
        state.setdefault("window", {})["seq"] = args.record_seq.strip()
        _save(args.state, state)
        _out(f"recorded window seq {args.record_seq.strip()}")
        return EXIT_OK
    if args.minutes is None:
        raise Refused("window needs --minutes M or --record-seq SEQ")
    # floored to the minute: the announced HH:MMZ and the enforced bound are one instant
    until = (datetime.fromtimestamp(clock(), timezone.utc)
             + timedelta(minutes=args.minutes)).replace(second=0, microsecond=0)
    nums = ", ".join(f"#{s['pr']}" for s in state["steps"])
    line = (f"{state['base']} moving, {len(state['steps'])} presses ({nums}), "
            f"until {until.strftime('%H:%M')}Z")
    # a new window line is a new announcement: its seq must be recorded again
    state["window"] = {"until": until.strftime(_UNTIL_FMT)}
    _save(args.state, state)
    _out(line)
    _out("Paste the line above into the Office, then record the crossing's seq "
         "with `window --record-seq SEQ`.")
    return EXIT_OK


def _gate_run(state, accept_red):
    if accept_red is not None and not accept_red.strip():
        raise Refused("--accept-red is blank; it must name the human ruling")
    run = state.get("run")
    if not run:
        raise Refused("no recorded run; `record-run` the whole tests/ run first")
    if run["tree"] != state["steps"][-1]["tree"]:
        raise Refused("the recorded run is not on the final step tree")
    green = run["failures"] == 0 and run["errors"] == 0 and run["rc"] == 0
    if green:
        return
    cells = "\n  ".join(run["failing"]) or "(none listed; non-zero rc)"
    if not accept_red:
        raise Refused(f"recorded run is RED (rc={run['rc']}, failures={run['failures']}, "
                      f"errors={run['errors']}); failing cells:\n  {cells}\n"
                      f"This tool never decides a red is a flake; a human ruling is "
                      f"passed as --accept-red REF")
    accept_red = accept_red.strip()
    state["accept_red"] = accept_red
    _out(f"proceeding on a red run under ruling {accept_red}; cells:\n  {cells}")


def _poll_ci(client, state, step, ceiling, interval, clock, sleep):
    start = clock()
    merge_state, strict = "(not read)", "(not read)"
    while True:
        merge_state = client.merge_state(state["repo"], step["pr"])
        if (merge_state or "").upper() == "CLEAN":
            return
        strict, _detail = _strict_required_check_state(
            client.check_runs(state["repo"], step["head"]), state["check_name"])
        if strict == "success":
            return
        if clock() - start >= ceiling:
            raise Refused(f"#{step['pr']}: not mergeable within {ceiling}s; last "
                          f"mergeStateStatus={merge_state}, last strict check state={strict}")
        sleep(interval)


def _landing(state):
    """Fetch the base and return (tip, parent, tree) of its new tip."""
    work = state["worktree"]
    _git(work, "fetch", "-q", state["remote"], f"refs/heads/{state['base']}")
    tip = _git(work, "rev-parse", "FETCH_HEAD").stdout.strip()
    parent = _git(work, "rev-parse", "FETCH_HEAD^", check=False).stdout.strip()
    tree = _git(work, "rev-parse", "FETCH_HEAD^{tree}").stdout.strip()
    return tip, parent, tree


def _check_window_open(window, clock):
    until = window.get("until")
    if not until:
        raise Refused("no window end recorded; post a new window line (`window --minutes M`) "
                      "and record its seq")
    end = datetime.strptime(until, _UNTIL_FMT).replace(tzinfo=timezone.utc)
    if datetime.fromtimestamp(clock(), timezone.utc) > end:
        raise Refused(f"the announced window ended at {until}; post a new window line "
                      f"(`window --minutes M`) and record its seq")


def cmd_press(args, client, clock, sleep):
    state = _load(args.state)
    window = state.get("window") or {}
    if not window.get("seq"):
        raise Refused("no window seq recorded; announce the window and `window --record-seq SEQ`")
    _check_window_open(window, clock)
    _gate_run(state, args.accept_red)
    _save(args.state, state)
    repo, base = state["repo"], state["base"]
    for i, step in enumerate(state["steps"]):
        if step["pressed"]:
            continue
        n = step["pr"]
        if step.get("merged"):
            raise Refused(f"#{n}: already merged but not verified as landed (merged: true, "
                          f"pressed: false; observed tip {step.get('observed_tip')}); "
                          f"read the base and repair the state by hand")
        _check_window_open(window, clock)
        expected_base = state["steps"][i - 1]["merge_sha"] if i else state["base_sha"]
        tip = client.base_tip(repo, base)
        if tip != expected_base:
            raise Refused(f"#{n}: {base} tip is {tip}, expected {expected_base}; "
                          f"the run never saw this base -- rebuild")
        head = client.pr_head(repo, n)
        if head != step["head"]:
            raise Refused(f"#{n}: head is {head}, recorded {step['head']}; rebuild")
        author = client.pr_author(repo, n)
        if not _has_current_approval(client.reviews(repo, n), author, head):
            raise Refused(f"#{n}: no current non-author APPROVED review on head {head}")
        if client.pr_stack(repo, n):
            raise Refused(f"#{n} is a gh-stack member; use `gh stack merge` by hand")
        _poll_ci(client, state, step, args.ceiling, args.interval, clock, sleep)
        unknown = f"merge outcome unknown for #{n}; read the base before re-running"
        try:
            ok, detail = client.merge(repo, n, head)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise Refused(f"{unknown} ({type(exc).__name__})") from exc
        if not ok and detail.startswith(_NOT_SENT):
            raise Refused(f"#{n}: {detail}; nothing was merged, safe to re-run once fixed")
        if not ok:
            raise Refused(f"{unknown} (merge call failed: {detail})")
        step["merged"] = True
        _save(args.state, state)
        new_tip, parent, tree = _landing(state)
        step["observed_tip"] = new_tip
        _save(args.state, state)
        if parent != expected_base or tree != step["tree"]:
            step["landed"] = {"tip": new_tip, "parent": parent, "tree": tree}
            _save(args.state, state)
            raise Refused(f"#{n}: landing mismatch -- tip {new_tip}, parent {parent} "
                          f"(expected {expected_base}), tree {tree} (expected {step['tree']}); "
                          f"no further step pressed")
        step["pressed"] = True
        step["merge_sha"] = new_tip
        _save(args.state, state)
        _out(f"pressed #{n}: {new_tip[:12]} (tree {tree[:12]})")
    return EXIT_OK


def cmd_status(args, client, clock, sleep):
    _out(json.dumps(_load(args.state), indent=2, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser():
    p = argparse.ArgumentParser(
        prog="merge_batch",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=None, help="JSON state file (required)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_, fn):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--state", default=argparse.SUPPRESS, help="JSON state file (required)")
        sp.set_defaults(fn=fn)
        return sp

    b = add("build", "scratch-merge the PRs in order; REFUSES on a conflict (names the PR)", cmd_build)
    b.add_argument("--repo", required=True)
    b.add_argument("--base", required=True)
    b.add_argument("--prs", required=True, help="comma-separated PR numbers, in merge order")
    b.add_argument("--scratch", required=True)
    b.add_argument("--remote", default=None, help="git remote URL (default https://github.com/<repo>.git)")
    b.add_argument("--check-name", default=DEFAULT_CHECK_NAME,
                   help=f"required check the strict read inspects (default {DEFAULT_CHECK_NAME})")

    r = add("record-run", "record the whole-tests run; REFUSES a --tree that is not the final step tree "
            "and an existing recorded run unless --replace", cmd_record_run)
    r.add_argument("--junit", required=True)
    r.add_argument("--rc", required=True, type=int)
    r.add_argument("--tree", required=True)
    r.add_argument("--replace", action="store_true",
                   help="replace an existing recorded run (the old one is kept under previous_runs)")

    w = add("window", "print the Office announcement line (clears the recorded seq); "
                  "--record-seq stores the posted seq, REFUSES a blank one", cmd_window)
    w.add_argument("--minutes", type=int, default=None)
    w.add_argument("--record-seq", default=None)

    pr = add("press", "merge each step in order; REFUSES without a window seq or past the window's end, on a red run "
                      "(unless --accept-red REF), a moved base or head, a missing current "
                      "non-author approval, a stack member, a step already merged but not verified, a merge call whose outcome is unknown, a CI state that is not CLEAN/strict-success "
                      "by --ceiling, or a landing whose parent/tree differs from the record", cmd_press)
    pr.add_argument("--ceiling", type=int, default=DEFAULT_CEILING_SECONDS)
    pr.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    pr.add_argument("--accept-red", default=None, metavar="REF",
                    help="reference to the human ruling that accepts a red run")

    add("status", "print the state", cmd_status)
    return p


def main(argv=None, *, client=None, clock=time.time, sleep=time.sleep):
    args = _parser().parse_args(argv)
    if not args.state:
        print("error: --state PATH is required", file=sys.stderr)
        return EXIT_USAGE
    try:
        return args.fn(args, client or GhClient(), clock, sleep)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
