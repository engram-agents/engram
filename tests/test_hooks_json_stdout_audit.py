"""Audit every hook shipped in hooks/hooks.json for Codex-safe stdout JSON.

PR #824 fixed the known laggards (time-bar, stop, user-identity).  This
regression test keeps the rest of the shipped hook set from drifting back to
plain-text or bare-Codex-incompatible stdout: if a referenced hook writes
anything to stdout, that stdout must be a JSON object.  Event hooks that surface
model context must use the event-specific envelope the hook protocol requires.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOKS_JSON = REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json"
HOOKS_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"


def _referenced_hooks() -> list[tuple[str, str, str]]:
    config = json.loads(HOOKS_JSON.read_text())
    refs: list[tuple[str, str, str]] = []
    for event_name, groups in config["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                match = re.search(r"/hooks/([^\"\s]+)", command)
                assert match, f"Could not identify hook script in command: {command!r}"
                refs.append((event_name, match.group(1), command))
    return refs


REFERENCED_HOOKS = _referenced_hooks()


def test_every_hooks_json_reference_has_a_supported_script_type():
    """Future hook script types must be added to this audit deliberately."""
    unsupported = [
        (event_name, script_name, command)
        for event_name, script_name, command in REFERENCED_HOOKS
        if not (script_name.endswith(".py") or script_name.endswith(".sh"))
    ]
    assert unsupported == []


PY_HOOK_REFS = [
    (event_name, script_name)
    for event_name, script_name, _command in REFERENCED_HOOKS
    if script_name.endswith(".py")
]

SHELL_HOOK_REFS = [
    script_name
    for _event_name, script_name, _command in REFERENCED_HOOKS
    if script_name.endswith(".sh")
]

PRETOOL_ACTIVE_FIXTURES = {
    "engram-bash-pipe-exit-warn.py",
    "engram-lesson-tripwire-hook.py",
}


# Hooks whose fixture must reach the SUCCESS path (not only the silent no-op one): the audit then
# asserts the hook did its work, so "empty stdout" is checked on the path that could have printed.
SUCCESS_PATH_FIXTURES = {"engram-nap-compact-backstop-stop.py"}


def _seed_nap_backstop_home(engram_home: Path) -> None:
    """A finished nap in a loop: checkpoint header + record, a briefing written after it, a loop marker."""
    header = "2026-10-21T10:00:00"
    epoch = 1792576800 + 0  # 2026-10-21T10:00:00Z
    (engram_home / "session_log.md").write_text(f"## Nap (turn 1) — {header}\n", encoding="utf-8")
    briefing = engram_home / "warm-briefing.md"
    briefing.write_text("# W\n\n## From this session\n\nan arc\n", encoding="utf-8")
    os.utime(briefing, (epoch + 120, epoch + 120))
    (engram_home / "loop-mode.json").write_text('{"kind": "loop"}', encoding="utf-8")
    (engram_home / "compact").mkdir()
    (engram_home / "compact" / "last-checkpoint.json").write_text(
        json.dumps({"checkpoint_at": header, "session_id": None, "backup_ok": True}), encoding="utf-8")


def _seed_lesson_tripwire_db(engram_home: Path) -> None:
    """Create the minimal active-lesson DB state needed to exercise the hook."""
    db = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            memory_status TEXT NOT NULL DEFAULT 'active',
            metadata TEXT NOT NULL DEFAULT '{}',
            claim TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, 'lesson', 1, 'active', ?, ?)",
        (
            "ls_0001",
            json.dumps({
                "situation_pattern": r"forum reply",
                "scaffolding_nudge": "Read body from stdin.",
            }),
            "forum reply lesson",
        ),
    )
    conn.commit()
    conn.close()


def _payload_for(event_name: str, script_name: str, transcript_path: Path) -> dict:
    base = {
        "session_id": "codex-json-sweep-test",
        "transcript_path": str(transcript_path),
    }
    if event_name == "UserPromptSubmit":
        return {**base, "prompt": "hello world"}
    if event_name == "PreToolUse":
        if script_name == "engram-bash-pipe-exit-warn.py":
            return {
                **base,
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/ | tail -20 && git commit -m 'wip'"},
                "input": {"command": "pytest tests/ | tail -20 && git commit -m 'wip'"},
            }
        if script_name == "engram-lesson-tripwire-hook.py":
            return {
                **base,
                "tool_name": "Bash",
                "tool_input": {"command": "forum reply 42"},
                "input": {"command": "forum reply 42"},
            }
        return {
            **base,
            "tool_name": "mcp__engram__engram_query",
            "tool_input": {"payload_json": "{}"},
        }
    if event_name == "Stop":
        return {
            **base,
            "last_assistant_message": "No ENGRAM node ids here.",
            "stop_hook_active": False,
        }
    if event_name == "SessionStart":
        return {**base, "source": "startup"}
    return base


@pytest.mark.parametrize(("event_name", "script_name"), PY_HOOK_REFS)
def test_referenced_python_hook_stdout_is_empty_or_json_object(
    event_name: str,
    script_name: str,
    tmp_path: Path,
):
    """Every Python hook in hooks/hooks.json must avoid invalid stdout.

    Empty stdout is allowed for no-op hooks. Non-empty stdout is parsed exactly
    the way Codex parses hook output: it must be a JSON object, never plain text.
    """
    hook_path = HOOKS_DIR / script_name
    assert hook_path.exists(), f"hooks.json references missing hook: {script_name}"

    engram_home = tmp_path / "engram-home"
    engram_home.mkdir()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "ENGRAM_HOME": str(engram_home),
            "HOME": str(tmp_path),
            "BATON_PROJECTS_DIR": str(tmp_path / "baton-projects"),
            "INTER_AGENT_DIR": str(tmp_path / "inter-agent"),
            "FORUM_URL": "",
            # Keep runtime imports pointed at this checkout, not a user's
            # production ENGRAM_HOME scatter copy.
            "ENGRAM_RUNTIME_DIR": str(REPO_ROOT),
        }
    )
    if script_name == "engram-lesson-tripwire-hook.py":
        _seed_lesson_tripwire_db(engram_home)
    if script_name in SUCCESS_PATH_FIXTURES:
        _seed_nap_backstop_home(engram_home)
        # The sweep's ENGRAM_RUNTIME_DIR (the repo root) does not hold the runtime modules; this hook
        # imports engram_config_io from there, so point it at the source package for its fixture.
        env["ENGRAM_RUNTIME_DIR"] = str(REPO_ROOT / "src" / "engram")

    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(_payload_for(event_name, script_name, transcript_path)),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        f"{script_name} ({event_name}) exited non-zero: {result.returncode}\n"
        f"stdout: {result.stdout[:500]!r}\n"
        f"stderr: {result.stderr[:500]!r}"
    )

    if script_name in SUCCESS_PATH_FIXTURES:
        assert (engram_home / "compact" / "request.json").exists(), (
            f"{script_name} fixture must exercise its success path; stderr: {result.stderr[:300]!r}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        assert script_name not in PRETOOL_ACTIVE_FIXTURES, (
            f"{script_name} fixture must exercise its non-empty PreToolUse "
            "output path"
        )
        return

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{script_name} ({event_name}) emitted invalid JSON stdout: {exc}\n"
            f"stdout: {stdout[:500]!r}\n"
            f"stderr: {result.stderr[:500]!r}"
        )

    assert isinstance(payload, dict), (
        f"{script_name} ({event_name}) stdout JSON must be an object, "
        f"got {type(payload).__name__}: {payload!r}"
    )
    assert "additionalContext" not in payload, (
        f"{script_name} ({event_name}) emitted bare additionalContext; "
        "Codex requires event-specific envelopes, never bare additionalContext"
    )

    if event_name == "Stop":
        assert "hookSpecificOutput" not in payload, (
            f"{script_name} ({event_name}) must not use hookSpecificOutput; "
            "Stop hooks continue the turn with decision/reason"
        )
        assert payload.get("decision") == "block", (
            f"{script_name} ({event_name}) decision must be 'block'; got {payload.get('decision')!r}"
        )
        reason = payload.get("reason")
        assert isinstance(reason, str) and reason, (
            f"{script_name} ({event_name}) reason must be a non-empty string"
        )
        return

    hook_specific = payload.get("hookSpecificOutput")
    if hook_specific is None:
        # {} is the canonical no-op JSON object.
        assert payload == {}, (
            f"{script_name} ({event_name}) emitted a non-empty JSON object "
            f"without hookSpecificOutput: {payload!r}"
        )
        return

    assert isinstance(hook_specific, dict), (
        f"{script_name} ({event_name}) hookSpecificOutput must be an object"
    )
    assert hook_specific.get("hookEventName") == event_name, (
        f"{script_name} emitted hookEventName={hook_specific.get('hookEventName')!r}, "
        f"expected {event_name!r}"
    )
    if "additionalContext" in hook_specific:
        assert isinstance(hook_specific["additionalContext"], str), (
            f"{script_name} ({event_name}) additionalContext must be a string"
        )

    if event_name == "PreToolUse":
        permission = hook_specific.get("permissionDecision")
        has_updated_input = "updatedInput" in hook_specific
        assert not (permission == "allow" and not has_updated_input), (
            f"{script_name} ({event_name}) emitted permissionDecision='allow' "
            "without updatedInput; context-only PreToolUse output must omit "
            "permissionDecision"
        )
        assert not (has_updated_input and permission != "allow"), (
            f"{script_name} ({event_name}) emitted updatedInput without "
            "permissionDecision='allow'"
        )


# --- return-by-stdout shell helper skip-list -------------------------------
#
# Some shell helpers legitimately return their value via stdout, captured by
# the caller with `$(...)`.  Their plain `echo` lines never reach the hook's
# real stdout (the Codex/Claude Code JSON channel this file's other assertion
# guards) -- only the captured variable does.  Skip-listing these lets the
# plaintext-stdout audit below keep firing on every OTHER unredirected echo
# in the shipped hook set, while not false-positiving on a helper whose whole
# contract is "print the answer, let the caller capture it".
#
# Every entry here is re-verified by the two tests that follow this one: the
# function must actually be defined in the named script (a rename or removal
# must not leave a silent, dead exemption), and every call site outside the
# function's own definition line and outside `#`-comments must be a command
# substitution -- a bare call (which WOULD leak to stdout) reddens the
# call-site test.
SHELL_STDOUT_RETURN_HELPERS: dict[str, set[str]] = {
    "start-engram-daemon.sh": {
        # $(_cgroup_supervision_state "$pid") -- reads a known live pid's
        # own cgroup; stdout is the return value.
        "_cgroup_supervision_state",
        # $(_supervisor_registered_state) -- reads whether a supervisor
        # unit is registered at all; stdout is the return value.
        "_supervisor_registered_state",
    },
}

_SHELL_FUNCTION_OPEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$")
_SHELL_FUNCTION_OPEN_RE_ANY_INDENT = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$"
)


def _nested_open_message(
    script_name: str, line_no: int, opened_fn: str, current_fn: str | None
) -> str:
    """Shared message for both the column-0 and any-indent nested-open
    asserts in `_shell_function_spans` -- the two differ only in which
    match object supplies `opened_fn`."""
    return (
        f"{script_name}:{line_no} opens function {opened_fn!r} "
        f"while {current_fn!r} is still open (nested/unclosed function "
        "body -- not supported by this audit's parser)"
    )


def _shell_function_spans(script_path: Path) -> tuple[dict[int, str], dict[str, int]]:
    """Map each line number to its enclosing function (if any), plus each
    defined function's own definition-line number.

    Parsing rule (sufficient for the current hook set -- not a general shell
    parser): a line matching `` ^fn()<ws>{<ws>$ `` (fn() then optional
    whitespace, an opening brace, optional whitespace) **at column 0**
    opens a span and records the function's definition line; a line that is
    exactly ``}`` closes it. Top-level indented function definitions (e.g.
    `start-engram-daemon.sh`'s `_stat_identity()` / `_log_socket_guard()`,
    defined inside an `if` block) are deliberately NOT recorded as spans --
    their body lines map to `None`, so the plaintext-stdout audit still
    checks them the same as any other top-level code.

    What this DOES catch, loudly, as an AssertionError rather than silently
    mis-parsing: (1) a function-open shape at column 0 while another
    function is already open; (2) a function-open shape at ANY indentation
    while another function is already open -- this closes the case an
    unindented-only check misses, where an indented nested `fn() {` inside
    an open function's body would otherwise go unnoticed and have its lines
    silently folded into the OUTER function's span; (3) a function that
    opens and never closes by EOF (unbalanced body) -- also loud rather than
    treating the rest of the file as in-function.

    What this does NOT do: a general shell parse. It has no notion of
    quoting or nesting depth, so it cannot skip content that merely LOOKS
    like a function-open line while actually living inside a heredoc, an
    embedded-interpreter block (e.g. this file's `{{PYTHON}} -c "..."`
    command substitutions), or any construct that puts the opening `{` on
    the line after the function name (`fn()` / `{` split across two
    lines). None of those shapes occur in the hook set today, so this
    line-based approximation is correct for the current file -- it is not
    proof against a future one that introduces such a shape.
    """
    lines = script_path.read_text().splitlines()
    line_to_fn: dict[int, str] = {}
    fn_def_line: dict[str, int] = {}
    current_fn: str | None = None
    for line_no, line in enumerate(lines, start=1):
        match = _SHELL_FUNCTION_OPEN_RE.match(line)
        if match:
            assert current_fn is None, _nested_open_message(
                script_path.name, line_no, match.group(1), current_fn
            )
            current_fn = match.group(1)
            fn_def_line[current_fn] = line_no
            continue
        if current_fn is not None:
            nested = _SHELL_FUNCTION_OPEN_RE_ANY_INDENT.match(line)
            assert nested is None, _nested_open_message(
                script_path.name, line_no, nested.group(1), current_fn
            )
        if line == "}" and current_fn is not None:
            current_fn = None
            continue
        if current_fn is not None:
            line_to_fn[line_no] = current_fn
    assert current_fn is None, (
        f"{script_path.name}: function {current_fn!r} opened but never closed "
        "(unbalanced function body)"
    )
    return line_to_fn, fn_def_line


# --- committed fixtures for _shell_function_spans -------------------------
#
# The real hook file (start-engram-daemon.sh) is exercised end-to-end above,
# but it contains no indented nested-function-open shape, so it never
# triggers the indent-tolerant guard's new branch. These fixtures are the
# committed control the reviewer round asked for: each one is shown (in the
# spec-driving verification step, not here) to redden under a targeted
# mutation, so a future edit that regresses the guard reddens a test rather
# than passing silently.


def test_shell_function_spans_indented_nested_open_while_open_raises(
    tmp_path: Path,
):
    """An indented `fn() {` while another function is open raises loudly.

    This is the guard round 2 added: an indented nested-function shape must
    not silently fold into the OUTER function's span.
    """
    script = tmp_path / "fixture.sh"
    script.write_text(
        "_outer() {\n"
        "    _nested() {\n"
        '        echo "leak"\n'
        "    }\n"
        "}\n"
    )
    with pytest.raises(
        AssertionError,
        match=r"opens function '_nested' while '_outer' is still open",
    ):
        _shell_function_spans(script)


def test_shell_function_spans_column0_nested_open_while_open_raises(
    tmp_path: Path,
):
    """A column-0 nested `fn() {` while another function is open also raises.

    Pins the pre-existing (round-1) branch too -- it was equally uncovered
    by any committed fixture before this round.
    """
    script = tmp_path / "fixture.sh"
    script.write_text(
        "_outer() {\n"
        "_nested() {\n"
        '    echo "leak"\n'
        "}\n"
        "}\n"
    )
    with pytest.raises(
        AssertionError,
        match=r"opens function '_nested' while '_outer' is still open",
    ):
        _shell_function_spans(script)


def test_shell_function_spans_indented_function_at_top_level_does_not_raise(
    tmp_path: Path,
):
    """An indented `fn() {` at TOP LEVEL (no function currently open) does
    not raise, and its body lines are not recorded as a span (map to
    `None` via a missing key).

    This is the real file's own shape (`_stat_identity` / `_log_socket_guard`,
    defined inside an `if` block) -- and the regression that would fire if
    the guard were "fixed" by dropping the `current_fn is not None`
    condition that scopes the nested-open check to only-while-a-function-
    is-open. This is the most important of the fixtures here: it is the
    only one that fails if the guard is over-applied, and over-application
    is the failure mode this guard's own design had to avoid.
    """
    script = tmp_path / "fixture.sh"
    script.write_text(
        "if true; then\n"
        "    _helper() {\n"
        '        echo "hi"\n'
        "    }\n"
        "fi\n"
    )
    line_to_fn, fn_def_line = _shell_function_spans(script)
    assert fn_def_line == {}
    assert line_to_fn == {}
    assert line_to_fn.get(3) is None


def test_shell_function_spans_unclosed_function_at_eof_raises(tmp_path: Path):
    """A function that opens and never closes by EOF raises loudly rather
    than silently treating the rest of the file as in-function."""
    script = tmp_path / "fixture.sh"
    script.write_text(
        "_outer() {\n"
        '    echo "never closes"\n'
    )
    with pytest.raises(
        AssertionError,
        match=r"function '_outer' opened but never closed",
    ):
        _shell_function_spans(script)


def test_shell_function_spans_two_functions_positive_control(tmp_path: Path):
    """A plain two-function file parses to the expected spans.

    A positive control: without this, the four fixtures above could all
    pass because the parser is broken outright (e.g. raises unconditionally,
    or never records any span at all).
    """
    script = tmp_path / "fixture.sh"
    script.write_text(
        "_first() {\n"
        '    echo "one"\n'
        "}\n"
        "\n"
        "_second() {\n"
        '    echo "two"\n'
        "}\n"
    )
    line_to_fn, fn_def_line = _shell_function_spans(script)
    assert fn_def_line == {"_first": 1, "_second": 5}
    assert line_to_fn == {2: "_first", 6: "_second"}


def test_referenced_shell_hooks_do_not_write_plaintext_stdout():
    """Shell hooks in hooks.json must not use stdout for human/plain text.

    Exception: functions listed in SHELL_STDOUT_RETURN_HELPERS legitimately
    return their value via stdout, captured by `$(...)` at every call site
    (verified by test_skip_listed_stdout_helpers_exist and
    test_skip_listed_stdout_helpers_call_sites_are_command_substituted
    below). Their echo lines are exempt here.
    """
    for script_name in SHELL_HOOK_REFS:
        script_path = HOOKS_DIR / script_name
        assert script_path.exists(), f"hooks.json references missing hook: {script_name}"
        line_to_fn, _fn_def_line = _shell_function_spans(script_path)
        exempt_fns = SHELL_STDOUT_RETURN_HELPERS.get(script_name, set())
        for line_no, line in enumerate(script_path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "echo" not in stripped:
                continue
            if line_to_fn.get(line_no) in exempt_fns:
                continue
            assert ">&2" in stripped or "1>&2" in stripped, (
                f"{script_name}:{line_no} writes with echo but does not redirect "
                f"to stderr: {line!r}"
            )


def test_skip_listed_stdout_helper_keys_are_referenced_hooks():
    """Every SHELL_STDOUT_RETURN_HELPERS key must still be in SHELL_HOOK_REFS.

    test_skip_listed_stdout_helpers_exist and
    test_skip_listed_stdout_helpers_call_sites_are_command_substituted both
    `continue` (skip checking) when a skip-list key is not in
    SHELL_HOOK_REFS. That `continue` is correct for a script genuinely
    removed from hooks.json -- but it also means both falsifiers go quiet
    for that key, so a lingering SHELL_STDOUT_RETURN_HELPERS entry for a
    script hooks.json no longer references would silently stop being
    checked by either test rather than failing loudly. Assert the dead-key
    case directly here instead of leaving it to two tests' shared silent
    skip.
    """
    dead_keys = set(SHELL_STDOUT_RETURN_HELPERS) - set(SHELL_HOOK_REFS)
    assert dead_keys == set(), (
        f"SHELL_STDOUT_RETURN_HELPERS has keys not in SHELL_HOOK_REFS: "
        f"{dead_keys!r} -- these scripts are no longer referenced by "
        "hooks.json, so their skip-list entries are dead and silently "
        "unchecked by the two tests below"
    )


def test_skip_listed_stdout_helpers_exist():
    """Every SHELL_STDOUT_RETURN_HELPERS entry must be a defined function.

    A rename or removal without updating the skip-list must fail loudly
    here, rather than silently leaving a dead exemption that quietly widens
    what the plaintext-stdout audit above accepts.
    """
    for script_name, exempt_fns in SHELL_STDOUT_RETURN_HELPERS.items():
        if script_name not in SHELL_HOOK_REFS:
            continue
        script_path = HOOKS_DIR / script_name
        assert script_path.exists(), f"hooks.json references missing hook: {script_name}"
        _line_to_fn, fn_def_line = _shell_function_spans(script_path)
        for fn in exempt_fns:
            assert fn in fn_def_line, (
                f"{script_name} skip-lists {fn!r} in SHELL_STDOUT_RETURN_HELPERS "
                "but no such function is defined in the script -- rename or "
                "removal without updating the skip-list"
            )


def test_skip_listed_stdout_helpers_call_sites_are_command_substituted():
    """Every call site of a skip-listed helper must be `$(...)`-captured.

    A bare call (e.g. `_cgroup_supervision_state "$pid"` with no
    surrounding `$(...)`) WOULD leak the helper's echo straight to the
    hook's real stdout -- this is the falsifier that keeps the plaintext-
    stdout exemption above honest. Every occurrence of a skip-listed
    function's name outside its own definition line and outside `#`
    comments must be immediately preceded by `$(` (optional whitespace).

    A mid-line (trailing) comment mentioning a helper name is deliberately
    NOT excluded from this scan -- over-flagging there is the safe
    direction (forces a human look) and is not worth the parsing cost of
    stripping trailing comments correctly.
    """
    for script_name, exempt_fns in SHELL_STDOUT_RETURN_HELPERS.items():
        if script_name not in SHELL_HOOK_REFS:
            continue
        script_path = HOOKS_DIR / script_name
        lines = script_path.read_text().splitlines()
        _line_to_fn, fn_def_line = _shell_function_spans(script_path)
        for fn in exempt_fns:
            def_line_no = fn_def_line.get(fn)
            name_re = re.compile(rf"\b{re.escape(fn)}\b")
            for line_no, line in enumerate(lines, start=1):
                if line_no == def_line_no:
                    continue
                if line.strip().startswith("#"):
                    continue
                for match in name_re.finditer(line):
                    prefix = line[: match.start()]
                    assert re.search(r"\$\(\s*$", prefix), (
                        f"{script_name}:{line_no} calls skip-listed helper "
                        f"{fn!r} without command substitution ($(...)) -- "
                        f"this WOULD leak its stdout to the hook's real "
                        f"stdout: {line!r}"
                    )
