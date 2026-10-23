"""pr-checks must report under GitHub's dev merge queue (``merge_group``).

A merge queue runs the required check on a temporary ``merge_group`` ref. A
workflow that triggers only on ``pull_request`` never reports there, so the
queue waits forever. A ``merge_group`` run also has NO pull_request payload:
``github.event.pull_request.*`` and ``github.base_ref`` are empty. These
assertions pin the wiring that makes the workflow safe on both events.

Allowed shape for a pull_request-payload reference OUTSIDE a step (workflow
``env:``, job ``env:``, ``concurrency``): only as a ``||`` fallback, i.e.
``${{ github.event.pull_request.X || <something that exists on merge_group> }}``
(today's concurrency group). The right-hand side of the LAST ``||`` must name
``github.ref`` or ``github.sha`` (a value that exists on merge_group). Anything
else fails: it would be empty on a queue run with nothing to fall back to.

A step's ``if`` counts as guarded only when it equals the guard exactly or is
``<guard> && (<ONE parenthesised group spanning the whole remainder>)`` -- a
``||`` form (``<guard> || success()``) is true on every event and guards
nothing, and so is ``<guard> && X || Y`` (``&&`` binds tighter, so the whole
thing is true on pull_request and on anything ``Y`` accepts).

``test-id-set-check`` is deliberately NOT guarded: it is half tree-level and
must run on the queue, where it aggregates Removed-test markers from the
queued PRs' bodies (``--gh-repo``) and fails loud on a commit without ``(#N)``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-checks.yml"
GUARD = "github.event_name == 'pull_request'"
RESOLVE_NAME = "resolve base ref"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(wf):
    return wf["jobs"]["pr-checks"]["steps"]


def _step_text(step):
    """Everything a step can read: run, env, with, continue-on-error."""
    parts = [str(step.get("run", ""))]
    for key in ("env", "with"):
        parts.append(json.dumps(step.get(key, {}), sort_keys=True))
    parts.append(str(step.get("continue-on-error", "")))
    return "\n".join(parts)


def _one_paren_group(text):
    """True when ``text`` is a single balanced ``( ... )`` spanning all of it."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i != len(text) - 1:
            return False  # closed early: `(a) || (b)` is two groups
        if depth < 0:
            return False
    return depth == 0


def _is_guarded(step):
    cond = str(step.get("if", "")).strip()
    if cond == GUARD:
        return True
    return cond.startswith(GUARD + " && ") and _one_paren_group(cond[len(GUARD) + 4 :])


def _arm(run, label):
    """Text of the case arm opened by ``label`` (e.g. ``*)``) up to its ``;;``."""
    m = re.search(r"^[ \t]*" + re.escape(label), run, re.M)
    assert m, f"no `{label}` arm"
    end = run.find(";;", m.end())
    assert end >= 0, f"`{label}` arm has no `;;`"
    return run[m.start() : end]


def _expressions(text):
    out, i = [], 0
    while True:
        a = text.find("${{", i)
        if a < 0:
            return out
        b = text.find("}}", a)
        out.append(text[a : b + 2])
        i = b + 2


def test_merge_group_trigger(wf):
    on = wf.get("on", wf.get(True))  # YAML 1.1 parses bare `on` as True
    assert "merge_group" in on
    assert on["merge_group"]["types"] == ["checks_requested"]
    assert "pull_request" in on


def test_pr_payload_readers_are_guarded(wf):
    bad = []
    for step in _steps(wf):
        text = _step_text(step)
        if "github.event.pull_request" not in text:
            continue
        if _is_guarded(step):
            continue
        # Unguarded step: every expression reading the payload must carry the
        # guard inline (the team-practice continue-on-error / notice case).
        for expr in _expressions(text + str(step.get("if", ""))):
            if "github.event.pull_request" in expr and GUARD not in expr:
                bad.append((step.get("name"), expr))
    assert not bad, bad


def _test_id_step(wf):
    found = [s for s in _steps(wf) if s.get("name", "").startswith("test-id-set-check — fail")]
    assert len(found) == 1
    return found[0]


def test_pr_metadata_steps_skip_on_merge_group(wf):
    prefixes = (
        "anti-dup",
        "pr-body-close-keyword",
        "name-leak-warn",
    )
    seen = 0
    for step in _steps(wf):
        name = step.get("name", "")
        if name.startswith(prefixes):
            seen += 1
            assert _is_guarded(step), (name, step.get("if"))
    assert seen == 5  # anti-dup, close-keyword, 3x name-leak
    tid = _test_id_step(wf)
    assert not _is_guarded(tid) and GUARD not in str(tid.get("if", "")), tid.get("if")
    assert str(tid.get("if", "")).strip() == "success() || failure()"
    unit = [s for s in _steps(wf) if s.get("name", "").startswith("test-id-set-check-unit-tests")]
    assert unit and all(not _is_guarded(s) for s in unit)


def test_no_step_uses_base_ref_directly(wf):
    bad = [
        s.get("name")
        for s in _steps(wf)
        if s.get("name") != RESOLVE_NAME and "github.base_ref" in _step_text(s)
    ]
    assert not bad, bad


def test_resolve_base_ref_step_precedes_first_user(wf):
    steps = _steps(wf)
    idx = [i for i, s in enumerate(steps) if s.get("name") == RESOLVE_NAME]
    assert len(idx) == 1, "exactly one `resolve base ref` step required"
    users = [
        i
        for i, s in enumerate(steps)
        if i != idx[0] and "BASE_REF" in _step_text(s) + str(s.get("if", ""))
    ]
    assert users, "no step consumes BASE_REF"
    assert idx[0] < min(users)
    text = _step_text(steps[idx[0]])
    assert "merge_group.base_sha" in text and "GITHUB_ENV" in text
    run = str(steps[idx[0]].get("run", ""))
    for needle in ("pull_request)", "merge_group)", "exit 1", "git cat-file -e"):
        assert needle in run, needle
    # the catch-all arm must fail loudly, not fall through to an empty base
    assert "exit 1" in _arm(run, "*)")


def test_non_step_payload_references_are_fallbacks(wf):
    """Workflow env, job env and concurrency: payload refs only as `||` fallback."""
    job = wf["jobs"]["pr-checks"]
    blocks = {
        "workflow env": wf.get("env", {}),
        "job env": job.get("env", {}),
        "concurrency": wf.get("concurrency", {}),
        "job concurrency": job.get("concurrency", {}),
    }
    bad = []
    for where, block in blocks.items():
        for expr in _expressions(json.dumps(block, sort_keys=True)):
            if "github.event.pull_request" not in expr:
                continue
            rhs = expr.rsplit("||", 1)[1] if "||" in expr else ""
            if "github.ref" not in rhs and "github.sha" not in rhs:
                bad.append((where, expr))
    assert not bad, bad
    # sanity: the one known reference (concurrency group) is actually scanned
    assert "github.event.pull_request" in json.dumps(blocks["concurrency"])


def test_test_id_set_check_runs_on_the_queue_with_aggregated_markers(wf):
    step = _test_id_step(wf)
    run = str(step.get("run", ""))
    for needle in ("merge_group)", "--gh-repo", "(#", "exit 1"):
        assert needle in run, needle
    pr_arm = _arm(run, "pull_request)")
    mg_arm = _arm(run, "merge_group)")
    assert "--pr-body-file" in pr_arm
    # the queue ref carries no body: markers come from the aggregated PR bodies
    assert "--pr-body-file" not in mg_arm and "--gh-repo" in mg_arm
    # the (#N) suffix check runs BEFORE the tool and fails loud
    assert mg_arm.index("(#") < mg_arm.index("test_id_set_check.py")
    assert mg_arm.index("exit 1") < mg_arm.index("test_id_set_check.py")
    assert "exit 1" in _arm(run, "*)")
    # the subject list is captured and guarded, never read through a process
    # substitution (its exit status is invisible to `set -e`)
    assert "< <(git log" not in mg_arm
    capture = [ln for ln in mg_arm.splitlines() if "log=$(git log" in ln]
    assert len(capture) == 1 and "|| {" in capture[0] and "exit 1" in capture[0], capture
    env = step.get("env", {})
    assert "EVENT_NAME" in env and "GH_REPO" in env


# -- executing the queue arm ---------------------------------------------------
# Text pins cannot tell a loud failure from a printed one: an `exit 1` swapped
# for `true` still satisfies "exit 1 comes before the tool". These cells run the
# step's real `run:` text under bash with a stub `git` and a stub `python3`.

BARE = "bare subject without a suffix"

# macOS has no /usr/bin/bash; resolve it on the same /usr/bin:/bin the step is given (Linux: /usr/bin/bash).
_BASH = shutil.which("bash", path="/usr/bin:/bin")


def _run_queue_arm(wf, tmp_path, *, log_lines, log_exit=0):
    if _BASH is None:
        pytest.skip("no bash on /usr/bin:/bin to run the queue arm under")
    run = str(_test_id_step(wf)["run"])
    run = run.replace("${{ env.BASE_REF }}", "$BASE_REF").replace("${{ github.repository }}", "$GH_REPO")
    assert "${{" not in run, "unsubstituted expression left in the step"
    script = tmp_path / "step.sh"
    script.write_text(run)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "subjects.txt").write_text("".join(ln + "\n" for ln in log_lines))
    stub_git = bin_dir / "git"
    stub_git.write_text(
        f"#!{_BASH}\n"
        'case "$1" in\n'
        "  merge-base) echo cafe1234 ;;\n"
        f'  log) if [ {log_exit} -ne 0 ]; then echo "fatal: bad revision" >&2; exit {log_exit}; fi;'
        f' cat "{tmp_path}/subjects.txt" ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n"
    )
    stub_py = bin_dir / "python3"
    stub_py.write_text(f'#!{_BASH}\necho "$@" > "{tmp_path}/TOOL_RAN"\nexit 0\n')
    for f in (stub_git, stub_py):
        f.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "EVENT_NAME": "merge_group",
        "BASE_REF": "deadbeef",
        "GH_REPO": "x/y",
        "PR_BODY": "",
        "GH_TOKEN": "",
    }
    return subprocess.run(
        [_BASH, "-eo", "pipefail", str(script)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )


def test_queue_arm_executed_refuses_a_bare_subject_before_the_tool(wf, tmp_path):
    res = _run_queue_arm(wf, tmp_path, log_lines=["abc1234\tfeat: x (#123)", f"def5678\t{BARE}"])
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "::error::" in out and BARE in out, out
    assert not (tmp_path / "TOOL_RAN").exists(), "the tool ran despite a bare subject"


def test_queue_arm_executed_runs_the_tool_when_every_subject_is_suffixed(wf, tmp_path):
    res = _run_queue_arm(wf, tmp_path, log_lines=["abc1234\tfeat: x (#123)", "def5678\tfix: y (#124)"])
    assert res.returncode == 0, res.stdout + res.stderr
    ran = (tmp_path / "TOOL_RAN").read_text()
    assert "--gh-repo x/y" in ran and "--pr-body-file" not in ran, ran


def test_queue_arm_executed_fails_loud_when_git_log_fails(wf, tmp_path):
    res = _run_queue_arm(wf, tmp_path, log_lines=[], log_exit=128)
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "::error::" in out and "git log" in out, out
    assert not (tmp_path / "TOOL_RAN").exists()
