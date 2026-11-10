"""pr-checks' team-practice steps run on the PRIVATE repository only.

The "10. team-practice-drift-gate" block checks out a private sibling repo
(``engram-agents/team-practice``) with a secret that only the private repository
carries. On the public repository, or on any other repository, that checkout
cannot work and must not be attempted.

Contract (stated by the spec author, tested here):

1. On any repository that is not the private one, none of the three steps runs:
   no checkout, no drift check, no advisory notice.
2. On the private repository the three ``if:`` expressions evaluate exactly as
   they did before this change, in every context.
3. The condition is the repository's IDENTITY, never the presence of a secret
   (a missing secret on the private repository must still fail loudly).
4. A comment at the checkout step says the check does not apply to the public
   repository and why.

The tests evaluate each step's ``if:`` text with a small evaluator for the
expression subset in use. They cannot prove what GitHub does with a skipped
step's outcome; that needs a real run.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-checks.yml"

CHECKOUT = "team-practice-drift-gate — checkout team-practice"
CHECK = "team-practice-drift-gate — run sync_team_practice.py --check"
ADVISORY = "team-practice-drift-gate — advisory notice (checkout unavailable)"
STEP_NAMES = (CHECKOUT, CHECK, ADVISORY)

# Built from parts: tests/ is scanned by scan-leaks (git-tracked .py files), which flags the
# private repository's full name written as one literal.
PRIVATE = "engram-agents/" + "engram-" + "alpha"
NON_PRIVATE = (
    "engram-agents/engram",  # the public repository
    "someone/engram",  # a fork-shaped name
    "someone/engram-alpha-fork",  # fork-shaped and contains the private repo's name
)
REPOS = (PRIVATE,) + NON_PRIVATE
EVENTS = ("pull_request", "pull_request_fork", "push", "merge_group")
STATUSES = ("success", "failure")
OUTCOMES = ("success", "failure", "skipped")
SECRETS = ("present", "absent")

# The three `if:` expressions as they were BEFORE the change, frozen.
OLD_IF = {
    CHECKOUT: "success() || failure()",
    CHECK: "(success() || failure()) && steps.team-practice-checkout.outcome == 'success'",
    ADVISORY: "(success() || failure()) && steps.team-practice-checkout.outcome != 'success'",
}


# ── a small evaluator for the expression subset in use ──────────────────────

_TOKEN = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|\(|\)|!|,)|'(?P<str>(?:[^']|'')*)'|(?P<id>[A-Za-z_][A-Za-z0-9_.\-]*))"
)


_STATUS_CALL = re.compile(r"\b(?:success|failure|always|cancelled)\s*\(")


def _tokenize(text):
    pos, out = 0, []
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"cannot tokenize {text!r} at {pos}")
        pos = m.end()
        if m.group("op") is not None:
            out.append(("op", m.group("op")))
        elif m.group("str") is not None:
            out.append(("str", m.group("str").replace("''", "'")))
        else:
            out.append(("id", m.group("id")))
    return out


def evaluate(text, ctx):
    """Evaluate a GitHub Actions ``if:`` expression (the subset in use) to a bool.

    ``ctx`` maps dotted names (``github.repository``, ``steps.<id>.outcome``,
    ``secrets.<name>``) to strings; a name not in ``ctx`` reads as ''. The job
    status is ``ctx['_status']`` ('success' or 'failure').
    """
    if not _STATUS_CALL.search(text):
        # GitHub implicitly prepends success() to an `if:` that names no status
        # function (the workflow's own comment at the advisory step says so).
        text = f"success() && ({text})"
    toks = _tokenize(text)
    i = 0

    def peek():
        return toks[i] if i < len(toks) else (None, None)

    def take():
        nonlocal i
        tok = toks[i]
        i += 1
        return tok

    def truthy(v):
        return bool(v)

    def eq(a, b):
        if isinstance(a, str) and isinstance(b, str):
            return a.lower() == b.lower()  # GitHub compares strings case-insensitively
        return a == b

    def primary():
        kind, val = take()
        if (kind, val) == ("op", "("):
            v = or_()
            if take() != ("op", ")"):
                raise ValueError("expected )")
            return v
        if (kind, val) == ("op", "!"):
            return not truthy(primary())
        if kind == "str":
            return val
        if kind == "id":
            if peek() == ("op", "("):
                take()
                args = []
                if peek() != ("op", ")"):
                    args.append(or_())
                    while peek() == ("op", ","):
                        take()
                        args.append(or_())
                if take() != ("op", ")"):
                    raise ValueError("expected ) after call")
                if val == "success":
                    return ctx["_status"] == "success"
                if val == "failure":
                    return ctx["_status"] == "failure"
                if val == "contains":
                    return str(args[1]).lower() in str(args[0]).lower()
                raise ValueError(f"unsupported function {val}()")
            return ctx.get(val, "")
        raise ValueError(f"unexpected token {kind} {val!r}")

    def cmp_():
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            op = take()[1]
            right = primary()
            left = eq(left, right) if op == "==" else not eq(left, right)
        return left

    def and_():
        left = cmp_()
        while peek() == ("op", "&&"):
            take()
            right = cmp_()
            left = right if truthy(left) else left
        return left

    def or_():
        left = and_()
        while peek() == ("op", "||"):
            take()
            right = and_()
            left = left if truthy(left) else right
        return left

    result = or_()
    if i != len(toks):
        raise ValueError(f"trailing tokens in {text!r}")
    return truthy(result)


def make_ctx(repo, event, status, outcome, secret):
    fork = event == "pull_request_fork"
    event_name = "pull_request" if fork else event
    ctx = {
        "_status": status,
        "github.repository": repo,
        "github.event_name": event_name,
        "steps.team-practice-checkout.outcome": outcome,
        "secrets.TEAM_PRACTICE_TOKEN": "token-value" if secret == "present" else "",
    }
    if event_name == "pull_request":
        ctx["github.event.pull_request.head.repo.full_name"] = "someone/engram" if fork else repo
    return ctx


def grid():
    return itertools.product(REPOS, EVENTS, STATUSES, OUTCOMES, SECRETS)


def causal_ctx(new, repo, event, status, outcome_if_run, secret):
    """A context whose checkout outcome is consistent with the checkout's own ``if:``.

    A step whose ``if:`` is false has outcome 'skipped', whatever it would have
    done had it run. The later steps are only meaningful in such a context; the
    drift-check step relies on exactly this ("outcome == 'success'" is false when
    the checkout was skipped), so an independent outcome would test a state
    GitHub cannot produce.
    """
    ctx = make_ctx(repo, event, status, outcome_if_run, secret)
    if not evaluate(new[CHECKOUT], ctx):
        ctx["steps.team-practice-checkout.outcome"] = "skipped"
    return ctx


# ── loading the workflow ────────────────────────────────────────────────────

def _raw_lines():
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def load_steps():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    found = {}
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") in STEP_NAMES:
                found[step["name"]] = step
    missing = [n for n in STEP_NAMES if n not in found]
    assert not missing, f"steps not found by name: {missing}"
    return found


def new_if():
    steps = load_steps()
    return {name: str(steps[name]["if"]) for name in STEP_NAMES}


def _fail_cells(cells, limit=6):
    shown = "\n".join(f"  {c}" for c in cells[:limit])
    return f"{len(cells)} bad cell(s); first {min(limit, len(cells))}:\n{shown}"


# ── evaluator sanity (a vacuous evaluator would make C1 and C2 pass for free) ─

def test_c0_evaluator_reads_the_frozen_old_expressions():
    ctx = make_ctx(PRIVATE, "push", "success", "success", "present")
    assert evaluate(OLD_IF[CHECKOUT], ctx) is True
    assert evaluate(OLD_IF[CHECK], ctx) is True
    assert evaluate(OLD_IF[ADVISORY], ctx) is False
    ctx = make_ctx(PRIVATE, "push", "failure", "skipped", "present")
    assert evaluate(OLD_IF[CHECKOUT], ctx) is True
    assert evaluate(OLD_IF[CHECK], ctx) is False
    assert evaluate(OLD_IF[ADVISORY], ctx) is True
    assert evaluate("success()", make_ctx(PRIVATE, "push", "failure", "skipped", "absent")) is False
    # an `if:` naming no status function gets an implicit success() in front
    bare = f"github.repository == '{PRIVATE}'"
    assert evaluate(bare, make_ctx(PRIVATE, "push", "success", "skipped", "absent")) is True
    assert evaluate(bare, make_ctx(PRIVATE, "push", "failure", "skipped", "absent")) is False


def test_c0_private_repository_still_runs_the_checkout_after_the_change():
    new = new_if()
    ctx = make_ctx(PRIVATE, "pull_request", "success", "success", "present")
    assert evaluate(new[CHECKOUT], ctx) is True
    assert evaluate(new[CHECK], ctx) is True
    ctx = make_ctx(PRIVATE, "pull_request", "failure", "failure", "absent")
    assert evaluate(new[CHECKOUT], ctx) is True
    assert evaluate(new[ADVISORY], ctx) is True


# ── C1: contract 1 ──────────────────────────────────────────────────────────

def test_c1_no_team_practice_step_runs_off_the_private_repository():
    new = new_if()
    bad = []
    for repo, event, status, outcome, secret in grid():
        if repo == PRIVATE:
            continue
        ctx = causal_ctx(new, repo, event, status, outcome, secret)
        for name in STEP_NAMES:
            if evaluate(new[name], ctx):
                bad.append((name.split("—")[1].strip()[:12], repo, event, status, outcome, secret))
    assert not bad, _fail_cells(bad)


# ── C2: contract 2 ──────────────────────────────────────────────────────────

def test_c2_private_repository_evaluates_exactly_as_before():
    new = new_if()
    bad = []
    for repo, event, status, outcome, secret in grid():
        if repo != PRIVATE:
            continue
        ctx = make_ctx(repo, event, status, outcome, secret)
        for name in STEP_NAMES:
            if evaluate(new[name], ctx) != evaluate(OLD_IF[name], ctx):
                bad.append((name.split("—")[1].strip()[:12], event, status, outcome, secret))
    assert not bad, _fail_cells(bad)


# ── C3: contract 3 ──────────────────────────────────────────────────────────

def test_c3_no_condition_reads_a_secret_env_or_vars():
    new = new_if()
    for name, text in new.items():
        for forbidden in ("secrets.", "secrets[", "env.", "vars."):
            assert forbidden not in text, f"{name!r}: if: reads {forbidden!r}: {text}"


# ── C4: the advisory step ───────────────────────────────────────────────────

def test_c4_advisory_step_carries_the_repository_clause():
    text = new_if()[ADVISORY]
    assert f"github.repository == '{PRIVATE}'" in text, text


def test_c4_advisory_is_silent_on_public_when_the_checkout_was_skipped():
    new = new_if()
    for status, event, secret in itertools.product(STATUSES, EVENTS, SECRETS):
        ctx = make_ctx("engram-agents/engram", event, status, "skipped", secret)
        assert evaluate(new[ADVISORY], ctx) is False, (status, event, secret)


# ── C5: contract 4, the comment at the checkout step ────────────────────────

def _checkout_step_comments():
    lines = _raw_lines()
    marker = f"- name: {CHECKOUT}"
    start = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
    assert start is not None, "checkout step not found in the raw text"
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("- name:"):
        end += 1
    return [ln.strip() for ln in lines[start:end] if ln.strip().startswith("#")]


def test_c5_checkout_step_says_it_does_not_apply_to_the_public_repository():
    comment = " ".join(_checkout_step_comments()).lower()
    assert comment, "no comment inside the checkout step"
    assert "public" in comment, comment
    assert "private" in comment, comment
    assert "does not apply" in comment, comment
    assert "team-practice" in comment, comment


# C6 is tests/test_pr_checks_merge_group.py, run as its own file; it is not edited here.
