"""Tests for #2652 phase-0 unit 3 -- bind the reap pattern and the
launch lines of ALL THREE surfaces that can start the surface daemon to
ONE source (start-engram-daemon.sh's own `_reap_pattern`).

Borges's framing (issue #2652, "Phase 0 -- design intent for the build
seat", section Unit 3): three surfaces should all agree on how the daemon
is scoped for the reap pattern, and before this unit two of them didn't:

    start-engram-daemon.sh   reap: engram-surface-daemon\\.py.*${ENGRAM_HOME}$
    .service.template        ExecStart: {{PYTHON}} .../engram-surface-daemon.py   <- NO ENGRAM_HOME argv
    .plist.template           ProgramArguments: [{{PYTHON}}, .../engram-surface-daemon.py]  <- NO ENGRAM_HOME argv

`pgrep -f` matches argv/cmdline text, not environment variables. Both
templates passed ENGRAM_HOME only via an env var (`Environment=` /
`EnvironmentVariables`), which `pgrep -f` cannot see -- so a daemon
launched by either supervised unit was UN-REAPABLE by
start-engram-daemon.sh's own reap pattern. This was a live incident (a
seat running the shipped `.service` unit had its daemon silently
unmatched by the reap), not a hypothetical.

The fix (this unit's Part A): add a trailing `{{ENGRAM_HOME}}` argv token
to both templates' launch lines, mirroring start-engram-daemon.sh's own
`"$DAEMON_SCRIPT" "$ENGRAM_HOME"` argv shape exactly. The env var stays in
place in both templates too -- the script still resolves ENGRAM_HOME from
the environment; the trailing argv exists solely so `pgrep -f` can scope
on it.

The binding mechanism (this unit's Part B, the actual "one source"
binding Borges asked for): a RENDERED-TEMPLATE test. Render each template
using the exact same plain-string substitution
`setup-surface-daemon-service.sh` uses, extract the actual launch-line
argv text each rendering produces, and assert it against the SAME
`_reap_pattern` regex extracted (not re-typed) from
start-engram-daemon.sh via test_pgrep_liveness_reap_patterns.py's
existing `_extract_reap_pattern()` helper. No new source-of-truth file;
reuses the existing single-sourcing helper; fails loudly the moment either
template's launch line stops matching the script's reap pattern.

Fail-then-pass control (mirrors test_start_daemon_probe_branch_logging.py's
own frozen-pre-fix-copy convention): TestPreFixTemplatesDidNotMatch below
renders BYTE-FOR-BYTE frozen copies of the pre-#2652-unit-3 templates
(captured from `git show origin/dev:...` before this unit's edit) and
asserts they do NOT match the reap pattern -- proving (a) the test's own
extraction/rendering plumbing is sound (it correctly detects absence, not
a bug that would pass regardless), and (b) the fix in this unit is what
makes the post-fix tests below pass, not an artifact of the test itself.
This was run and confirmed failing against the real (unedited) template
files before Part A's edit was applied; see this unit's PR/report for the
literal pre-edit pytest output.
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from test_pgrep_liveness_reap_patterns import _extract_reap_pattern  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_TEMPLATES = _REPO_ROOT / "src" / "engram" / "templates"

_SH_SCRIPT = _HOOKS / "start-engram-daemon.sh"
_SERVICE_TEMPLATE = _TEMPLATES / "engram-surface-daemon.service.template"
_PLIST_TEMPLATE = _TEMPLATES / "engram-surface-daemon.plist.template"

# Realistic test substitution values -- deliberately DISJOINT paths (none is
# a substring of another) so that "does the rendered argv text contain
# ENGRAM_HOME" is a clean proxy for "is the trailing argv token present",
# never a false positive from ENGRAM_HOME merely being a substring of the
# python/hooks-dir path (as it would be under a real ~/.engram install
# layout, where the venv and hooks dir both live inside ENGRAM_HOME itself).
# ENGRAM_HOME still carries a literal "." (in ".engram") to mirror the exact
# real-world shape start-engram-daemon.sh itself interpolates unescaped into
# its own `_reap_pattern` (the "." reads as a regex metachar there too; this
# test reproduces that faithfully rather than re.escape-ing it away, matching
# the existing convention in test_pgrep_liveness_reap_patterns.py's
# TestDaemonReapPatternEngramHomeAnchored).
_TEST_ENGRAM_HOME = "/home/testuser/.engram"
_TEST_HOOKS_DIR = "/opt/engram-plugin/hooks"
_TEST_PYTHON = "/opt/pyenv/versions/3.11/bin/python3"
_TEST_DAEMON_SCRIPT = f"{_TEST_HOOKS_DIR}/engram-surface-daemon.py"


def _reap_pattern() -> str:
    """Single-sourced from start-engram-daemon.sh's own `_reap_pattern` via
    the existing, reused helper -- never re-typed."""
    template = _extract_reap_pattern(_SH_SCRIPT, "_reap_pattern")
    return template.format(engram_home=_TEST_ENGRAM_HOME)


def _render(template_text: str, *, python: str = _TEST_PYTHON,
            hooks_dir: str = _TEST_HOOKS_DIR, engram_home: str = _TEST_ENGRAM_HOME) -> str:
    """Mirrors setup-surface-daemon-service.sh's rendering exactly:

        sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \\
            -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \\
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g"

    i.e. plain, global, non-regex string substitution of all three tokens.
    str.replace() replaces every occurrence by default, exactly like sed's
    trailing `g` flag.
    """
    return (
        template_text
        .replace("{{PYTHON}}", python)
        .replace("{{ENGRAM_HOOKS_DIR}}", hooks_dir)
        .replace("{{ENGRAM_HOME}}", engram_home)
    )


def _extract_service_execstart(rendered_unit_text: str) -> str:
    """The full ExecStart= value -- this literally becomes the launched
    process's argv/cmdline text that `pgrep -f` searches."""
    m = re.search(r"^ExecStart=(.*)$", rendered_unit_text, re.MULTILINE)
    assert m, "rendered .service unit: no ExecStart= line found"
    return m.group(1)


def _extract_plist_program_arguments_joined(rendered_plist_text: str) -> str:
    """Parse the rendered plist XML properly (plistlib) rather than
    regex-scraping indentation-dependent <string> tags, and join
    ProgramArguments's values with a single space -- exactly what
    `/proc/<pid>/cmdline` (and therefore `pgrep -f`) sees once launchd
    execs the array as argv."""
    parsed = plistlib.loads(rendered_plist_text.encode("utf-8"))
    args = parsed["ProgramArguments"]
    return " ".join(args)


def _extract_sh_launch_line(script_path: Path) -> str:
    """Extract the literal `{{PYTHON}} "$DAEMON_SCRIPT" "$ENGRAM_HOME"`
    argv-shape token sequence from start-engram-daemon.sh's own launch
    line (the `env ENGRAM_HOME=... {{PYTHON}} "$DAEMON_SCRIPT"
    "$ENGRAM_HOME" ... &` line). This is the regression-guard surface
    (spec item 4): this unit does not touch the .sh script, so this must
    already pass and keep passing.

    #2652 phase-0 unit 4 FOLD: the leading `^` is now `^\\s*` -- the
    restored launch line lives inside the UNSUPERVISED `case` arm and is
    therefore indented, unlike its pre-fold column-0 position. Bash
    indentation is purely cosmetic and carries no semantic meaning, so
    tolerating leading whitespace here does not weaken what this
    regression guard actually checks (the launch line's own argv shape)."""
    text = script_path.read_text()
    m = re.search(
        r'^\s*env ENGRAM_HOME="\$ENGRAM_HOME" (\{\{PYTHON\}\} "\$DAEMON_SCRIPT" "\$ENGRAM_HOME")',
        text,
        re.MULTILINE,
    )
    assert m, (
        "start-engram-daemon.sh: expected launch-line shape "
        '`env ENGRAM_HOME="$ENGRAM_HOME" {{PYTHON}} "$DAEMON_SCRIPT" "$ENGRAM_HOME" ...` '
        "not found -- script structure changed; re-verify this unit's assumptions "
        "before trusting the regression guard below"
    )
    return m.group(1)


def _render_sh_launch_line(captured: str) -> str:
    """Substitute the .sh launch line's shell tokens with the same test
    values used for the templates, simulating the actual argv `env`
    execs into once it replaces itself with {{PYTHON}}."""
    return (
        captured
        .replace("{{PYTHON}}", _TEST_PYTHON)
        .replace('"$DAEMON_SCRIPT"', _TEST_DAEMON_SCRIPT)
        .replace('"$ENGRAM_HOME"', _TEST_ENGRAM_HOME)
    )


# ===========================================================================
# Frozen pre-#2652-unit-3 template content (byte-for-byte, captured via
# `git show origin/dev:src/engram/templates/engram-surface-daemon.*.template`
# immediately before this unit's Part A edit was applied) -- used ONLY as a
# negative control proving the extraction/rendering plumbing above correctly
# detects absence, not as a live source of truth.
# ===========================================================================

_OLD_SERVICE_TEMPLATE = """[Unit]
Description=ENGRAM surface daemon (semantic recall)

[Service]
Type=simple
ExecStart={{PYTHON}} {{ENGRAM_HOOKS_DIR}}/engram-surface-daemon.py
Environment=ENGRAM_HOME={{ENGRAM_HOME}}
Environment=ENGRAM_DAEMON_IDLE_TIMEOUT=0
Restart=always
RestartSec=5s
# `append:` log-routing requires systemd >= 240 (2018-10-03).
# Pre-v240 systemd (Ubuntu 18.04 ships 237) would fail to parse this unit.
# Practical floor is fine — engram-alpha's Python 3.10+ floor already
# excludes 18.04 (3.10 not in main repos). Switch to `StandardOutput=journal`
# if a pre-v240 target ever needs support.
StandardOutput=append:{{ENGRAM_HOME}}/surface-daemon.log
StandardError=append:{{ENGRAM_HOME}}/surface-daemon.log

[Install]
WantedBy=default.target
"""

_OLD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.engram.surface-daemon</string>

    <key>ProgramArguments</key>
    <array>
        <string>{{PYTHON}}</string>
        <string>{{ENGRAM_HOOKS_DIR}}/engram-surface-daemon.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ENGRAM_HOME</key>
        <string>{{ENGRAM_HOME}}</string>
        <key>ENGRAM_DAEMON_IDLE_TIMEOUT</key>
        <string>0</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{{ENGRAM_HOME}}/surface-daemon.log</string>

    <key>StandardErrorPath</key>
    <string>{{ENGRAM_HOME}}/surface-daemon.log</string>
</dict>
</plist>
"""


class TestPreFixTemplatesDidNotMatch:
    """Negative control: byte-for-byte frozen copies of the templates as
    they existed immediately BEFORE this unit's Part A edit. Both must NOT
    match the reap pattern, and for the RIGHT reason (ENGRAM_HOME is
    genuinely absent from their rendered launch-line argv text) -- not a
    bug in the extraction/rendering code that would fail regardless.

    This is the permanent record of the fail-then-pass demonstration the
    spec requires: it was also run standalone against the real (unedited)
    template files on disk before Part A's edit was applied, confirming
    both failed for this same reason; see this unit's PR/report for that
    transcript.
    """

    def test_old_service_execstart_lacks_engram_home_argv(self):
        rendered = _render(_OLD_SERVICE_TEMPLATE)
        execstart = _extract_service_execstart(rendered)
        assert _TEST_ENGRAM_HOME not in execstart, (
            "sanity check failed: the FROZEN pre-fix .service template's "
            f"rendered ExecStart unexpectedly already contains {_TEST_ENGRAM_HOME!r} "
            f"-- got: {execstart!r}"
        )
        assert not re.search(_reap_pattern(), execstart), (
            "pre-fix .service ExecStart unexpectedly matched the reap pattern "
            f"-- got ExecStart={execstart!r}"
        )

    def test_old_plist_program_arguments_lacks_engram_home_argv(self):
        rendered = _render(_OLD_PLIST_TEMPLATE)
        joined = _extract_plist_program_arguments_joined(rendered)
        assert _TEST_ENGRAM_HOME not in joined, (
            "sanity check failed: the FROZEN pre-fix .plist template's "
            f"rendered ProgramArguments unexpectedly already contains {_TEST_ENGRAM_HOME!r} "
            f"-- got: {joined!r}"
        )
        assert not re.search(_reap_pattern(), joined), (
            "pre-fix .plist ProgramArguments unexpectedly matched the reap pattern "
            f"-- got ProgramArguments joined={joined!r}"
        )


class TestCurrentTemplatesMatchReapPattern:
    """Post-fix (this unit's Part A) confirmation: BOTH templates, as they
    ship on disk today, render a launch line whose argv text matches
    start-engram-daemon.sh's own `_reap_pattern`."""

    def test_service_execstart_matches_reap_pattern(self):
        rendered = _render(_SERVICE_TEMPLATE.read_text())
        execstart = _extract_service_execstart(rendered)
        assert re.search(_reap_pattern(), execstart), (
            ".service.template's rendered ExecStart does not match "
            f"start-engram-daemon.sh's _reap_pattern -- got ExecStart={execstart!r}, "
            f"pattern={_reap_pattern()!r}"
        )

    def test_plist_program_arguments_matches_reap_pattern(self):
        rendered = _render(_PLIST_TEMPLATE.read_text())
        joined = _extract_plist_program_arguments_joined(rendered)
        assert re.search(_reap_pattern(), joined), (
            ".plist.template's rendered, joined ProgramArguments does not match "
            f"start-engram-daemon.sh's _reap_pattern -- got joined={joined!r}, "
            f"pattern={_reap_pattern()!r}"
        )

    def test_service_execstart_actually_carries_engram_home(self):
        """Guards against a vacuous pass: confirm the match above is
        because ENGRAM_HOME is genuinely present in the argv text, not
        some other coincidental regex hit."""
        rendered = _render(_SERVICE_TEMPLATE.read_text())
        execstart = _extract_service_execstart(rendered)
        assert _TEST_ENGRAM_HOME in execstart

    def test_plist_program_arguments_actually_carries_engram_home(self):
        rendered = _render(_PLIST_TEMPLATE.read_text())
        joined = _extract_plist_program_arguments_joined(rendered)
        assert _TEST_ENGRAM_HOME in joined


class TestShScriptOwnLaunchLineStillMatches:
    """Regression guard (spec item 4, AS OF unit 3): this unit touches
    only the two templates, never start-engram-daemon.sh itself. Confirms
    its own launch line -- extracted fresh from the script text, not
    re-derived from memory -- still matches its own _reap_pattern,
    unaffected by unit 3's changes.

    #2652 phase-0 unit 4 FOLD: this class was retired (skipped) by unit
    4's own build, which removed the launch line entirely. The fold
    RESTORES it (conditionally, on the UNSUPERVISED/not-registered
    supervisor-branch path only) -- see start-engram-daemon.sh's own
    `case "$_supervisor_state"` block. `_extract_sh_launch_line` matches
    on the literal launch-line text alone, regardless of what shell
    construct now surrounds it, so this regression guard is un-skipped
    and holds again unmodified."""

    def test_sh_launch_line_matches_its_own_reap_pattern(self):
        captured = _extract_sh_launch_line(_SH_SCRIPT)
        rendered = _render_sh_launch_line(captured)
        assert re.search(_reap_pattern(), rendered), (
            "start-engram-daemon.sh's own launch line no longer matches its own "
            f"_reap_pattern -- got {rendered!r}, pattern={_reap_pattern()!r}"
        )


class TestEnvironmentVariablePathsStillPresent:
    """Part A's spec explicitly requires the pre-existing env-var
    ENGRAM_HOME lines to remain in place in both templates (the script
    still resolves ENGRAM_HOME from the environment, per
    start-engram-daemon.sh's own comment) -- the trailing argv is
    additive, not a replacement."""

    def test_service_template_keeps_environment_line(self):
        text = _SERVICE_TEMPLATE.read_text()
        assert "Environment=ENGRAM_HOME={{ENGRAM_HOME}}" in text

    def test_plist_template_keeps_environment_variables_dict(self):
        rendered = _render(_PLIST_TEMPLATE.read_text())
        parsed = plistlib.loads(rendered.encode("utf-8"))
        assert parsed["EnvironmentVariables"]["ENGRAM_HOME"] == _TEST_ENGRAM_HOME


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
