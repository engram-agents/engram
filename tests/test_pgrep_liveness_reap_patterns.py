"""Synthetic-process-table tests for #2029: pgrep-based liveness/reap probes
can be false-healthy (or over-reap) by construction, and this failure mode is
invisible to CI, diff review, AND careful reading of live pgrep output.

This is the mechanical test the issue asks for: run the REAL pattern (single-
sourced from its script/module, never re-typed) against a synthetic process
table containing a target, a co-tenant's equivalent, an invoking-process
self-quote decoy, and a wrapper — and assert the pattern matches EXACTLY the
target (modulo any documented, mitigated over-match).

Inventory covered here (see #2029 inline comments at each site for the
short "could this match the invoker / a co-tenant?" answer):

  SAFE (path-anchored or end-anchored; already fixed pre-#2029 or by #2028):
    - _hooklib.py / engram-surface-hook.py / engram-session-start-hook.py's
      resolved-absolute-path `pgrep -f <path>` server liveness check (#1839).
    - start-engram-daemon.sh's ENGRAM_HOME-end-anchored reap pattern (#1874).

  MITIGATED OVER-MATCH (candidate regex alone over-matches by design, but a
  second /proc/<pid>/cmdline verification layer + warn-not-kill action means
  the over-match can never cause a false-healthy OR a bad kill) (#1933):
    - forum-mention-monitor.sh
    - forum-updates-monitor.sh

  FIXED (#1935 — ported #1933's warn-not-kill + /proc two-layer verification,
  PLUS anchored the layer-1 candidate pattern itself on
  `^(\\S*/)?-?bash\\s+...` per #2028's boot-charge fix, so the candidate stage
  alone already excludes the self-quoting-charge-text decoy):
    - collab-baton-monitor.sh
      (#1935 originally ported the identical fix to collab-letter-monitor.sh
      too — that script was deleted as dead code in #2857 step 2, its
      watched channel retired by the 2026-06-28 UCS cutover to forum DMs —
      and its coverage removed from this file in the same PR.)

    - deploy/autostart/boot-charge-default.md / boot-charge-quiet.md's
      monitor-verify step: `pgrep -u "$USER" -af
      '^(\\S*/)?bash\\s+\\S*forum-(mention|updates)-monitor\\.sh'`. Fixed by
      #2028 (process-name-anchored `^(\\S*/)?bash`, so an unanchored
      self-quote of the pattern/monitor names in the charge text itself
      cannot match). Single-sourced from the markdown at test time below;
      both files are also asserted to carry the identical pattern.

  EXCLUDED (not a pattern-liveness probe): tools/agentctl's account-
  decommission `pkill -KILL -u "$newborn_user"` / `pgrep -u "$newborn_user"`
  (~line 1127-1129) is a uid-scoped kill-everything-for-this-account step,
  not a command-line pattern match -- there is no `-f PATTERN` to false-
  match or drift, so it is out of scope for this inventory.
"""

from __future__ import annotations

import importlib.util
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"
_HOOKS = _REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_AUTOSTART = _REPO_ROOT / "deploy" / "autostart"


# ---------------------------------------------------------------------------
# pgrep -f simulator: python re equivalent of `pgrep [-u UID] -f PATTERN`
# ---------------------------------------------------------------------------
#
# Real pgrep -f semantics being modeled:
#   - PATTERN is an extended regex, searched (not anchored) against the
#     process's full command line (argv joined by a single space, matching
#     how /proc/<pid>/cmdline is conventionally rendered for -f matching).
#   - -u UID filters the candidate set by uid BEFORE the regex is applied
#     (this is what makes -u a real security/scoping boundary and not just
#     another regex term).


def simulate_pgrep_f(pattern: str, table: list[dict], uid: int | None = None) -> list[int]:
    """Return the list of pids that `pgrep [-u uid] -f pattern` would match
    against a synthetic process table.

    table: list of {"pid": int, "uid": int, "cmdline": list[str]}
    """
    rx = re.compile(pattern)
    candidates = table if uid is None else [p for p in table if p["uid"] == uid]
    return [p["pid"] for p in candidates if rx.search(" ".join(p["cmdline"]))]


def _cmdline_str(cmdline: list[str]) -> str:
    return " ".join(cmdline)


# ---------------------------------------------------------------------------
# Pattern extraction: single-source every bash pattern from its own script
# text so this test can never silently drift from what actually ships.
# ---------------------------------------------------------------------------


def _extract_pgrep_pattern_branches(script_path: Path, var_name: str = "_pgrep_pattern") -> dict[str, str]:
    """Extract the literal patterns assigned to `_pgrep_pattern` in the
    if/else "default" vs. named-purpose branches shared by all four monitor
    scripts. Returns {"default": <pattern>, "named": <pattern-with-{purpose}>}.

    The named-purpose pattern is returned as a %-style template with
    "{purpose}" standing in for the shell `${PURPOSE}` interpolation, so the
    test can substitute a real purpose value.
    """
    text = script_path.read_text()
    single = re.findall(
        rf"{re.escape(var_name)}='([^']*)'",
        text,
    )
    double = re.findall(
        rf'{re.escape(var_name)}="([^"]*\$\{{PURPOSE\}}[^"]*)"',
        text,
    )
    assert single, f"{script_path.name}: no single-quoted {var_name} default branch found"
    assert double, f"{script_path.name}: no double-quoted {var_name} named-purpose branch found"
    named_template = double[0].replace("${PURPOSE}", "{purpose}")
    return {"default": single[0], "named": named_template}


def _extract_reap_pattern(script_path: Path, var_name: str) -> str:
    """Extract a single named-variable pattern (e.g. start-engram-daemon.sh's
    `_reap_pattern`) as a %-style template with "{engram_home}" standing in
    for the shell `${ENGRAM_HOME}` interpolation.

    Bash double-quote unescaping: inside "...", a backslash is only special
    (and consumed) before $, `, ", \\, or newline -- `\\$` becomes a literal
    `$` (which the REGEX engine then reads as an end-anchor), while `\\.`
    is left untouched (both characters reach the regex engine, giving a
    literal-dot escape). Reading the raw file text skips bash's own quote
    processing, so this one substitution has to be replayed by hand or the
    extracted pattern silently keeps an inert `\\$` instead of the real `$`
    end-anchor the script actually runs with.
    """
    text = script_path.read_text()
    m = re.search(rf'{re.escape(var_name)}="([^"]*)"', text)
    assert m, f"{script_path.name}: no {var_name}=\"...\" assignment found"
    raw = m.group(1).replace("${ENGRAM_HOME}", "{engram_home}")
    return raw.replace("\\$", "$")


def _extract_boot_charge_pgrep_pattern(md_path: Path) -> str:
    """Extract the monitor-verify `pgrep -u "$USER" -af '<pattern>'` pattern
    literally from a boot-charge markdown file's prose, single-sourced so
    this test can never silently drift from what the charge text actually
    tells an agent to run. The pattern is single-quoted in the source (bash
    single-quotes suppress all interpolation/escaping), so the extracted
    text is already the literal ERE the shell would pass to `pgrep -af`,
    with no bash-quote-unescaping step required.
    """
    text = md_path.read_text()
    m = re.search(r"""pgrep -u \\?"\$USER\\?" -af '([^']*)'""", text)
    assert m, f"{md_path.name}: no monitor-verify pgrep -u \"$USER\" -af '...' pattern found"
    return m.group(1)


# ---------------------------------------------------------------------------
# _hooklib.py's resolve_own_server_py_path (imported directly, not
# re-derived) -- the pattern under test is the literal string it returns.
# ---------------------------------------------------------------------------


def _load_hooklib():
    spec = importlib.util.spec_from_file_location(
        "_hooklib_pgrep_inventory", _HOOKS / "_hooklib.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# SAFE site 1: server.py liveness (_hooklib.py / both hooks delegate to it)
# ===========================================================================


class TestServerPyLivenessPatternIsPathScoped:
    """pgrep -f <resolved-absolute-server.py-path> -- #1839's fix. The
    "pattern" here is not a hand-written regex; it IS the resolved absolute
    path, imported directly from _hooklib.py so there is nothing to
    re-derive or drift."""

    def test_matches_only_the_resolved_absolute_path(self, tmp_path):
        mod = _load_hooklib()
        engram_home = tmp_path / "agentX_home"
        engram_home.mkdir()
        server_path = engram_home / "server.py"
        server_path.write_text("# fixture\n")
        pattern = re.escape(str(server_path))  # pgrep -f receives this literally, unescaped, but as a path it contains no regex metachars in practice; re.escape makes the *test* precise regardless.

        target_pid = 100
        cotenant_pid = 200
        invoker_decoy_pid = 300
        wrapper_pid = 400

        table = [
            # target: this agent's own server.py, running directly.
            {"pid": target_pid, "uid": 1000, "cmdline": ["python3", str(server_path)]},
            # co-tenant equivalent: a DIFFERENT agent's server.py at a
            # different absolute path (their own $ENGRAM_HOME), different uid.
            {
                "pid": cotenant_pid,
                "uid": 1001,
                "cmdline": ["python3", str(tmp_path / "agentY_home" / "server.py")],
            },
            # invoking-process self-quote decoy: something that merely
            # mentions this server_path as a substring of unrelated text
            # (e.g. a boot charge or an editor process), never the real path.
            {
                "pid": invoker_decoy_pid,
                "uid": 1000,
                "cmdline": [
                    "python3", "-c", "import time; time.sleep(9)",
                    f"boot charge text mentioning {server_path} in prose, then more instructions",
                ],
            },
            # wrapper: a genuine instance launched via `bash -c '...'` --
            # still IS the target and should still match.
            {
                "pid": wrapper_pid,
                "uid": 1000,
                "cmdline": ["bash", "-c", f"exec python3 {server_path}"],
            },
        ]

        matched = simulate_pgrep_f(pattern, table)
        # The decoy DOES contain the path as a literal substring, so an
        # unescaped/unanchored match would hit it too -- that's fine and
        # expected (pgrep -f is always a substring search); the point of
        # this scoping fix is that a co-tenant's DIFFERENT path never
        # matches, which is the actual false-healthy failure mode #1839 and
        # the 07-31 incident were about.
        assert cotenant_pid not in matched
        assert target_pid in matched
        assert wrapper_pid in matched


# ===========================================================================
# SAFE site 2: start-engram-daemon.sh's ENGRAM_HOME-end-anchored reap
# ===========================================================================


class TestDaemonReapPatternEngramHomeAnchored:
    _SCRIPT = _HOOKS / "start-engram-daemon.sh"

    def _pattern_for(self, engram_home: str) -> str:
        template = _extract_reap_pattern(self._SCRIPT, "_reap_pattern")
        return template.format(engram_home=engram_home)

    def test_end_anchor_excludes_cotenant_and_charge_quoting_decoy(self):
        engram_home = "/home/agentX/.engram"
        other_home = "/home/agentY/.engram"
        pattern = self._pattern_for(engram_home)

        target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid = 10, 20, 30, 40
        table = [
            # target: genuine daemon for THIS agent's ENGRAM_HOME.
            {
                "pid": target_pid,
                "uid": 1000,
                "cmdline": ["python3", "engram-surface-daemon.py", engram_home],
            },
            # co-tenant equivalent: a DIFFERENT agent's daemon (different
            # ENGRAM_HOME string; this script has no -u scope, so the end
            # anchor on ENGRAM_HOME is what has to carry the isolation).
            {
                "pid": cotenant_pid,
                "uid": 1001,
                "cmdline": ["python3", "engram-surface-daemon.py", other_home],
            },
            # invoking-process self-quote decoy: a boot-charge-style blob
            # that quotes this exact launch line as a SUBSTRING, with more
            # trailing text after ENGRAM_HOME -- must NOT satisfy the `\$`
            # end anchor (mirrors the real #1933 repro shape, applied to
            # this script's own reap pattern).
            {
                "pid": invoker_decoy_pid,
                "uid": 1000,
                "cmdline": [
                    "claude", "-p",
                    f"Automatic morning boot: python3 engram-surface-daemon.py {engram_home} "
                    "(continue with the rest of the day's plan)",
                ],
            },
            # wrapper: a genuine instance launched via `bash -c '...'`.
            {
                "pid": wrapper_pid,
                "uid": 1000,
                "cmdline": ["bash", "-c", f"exec python3 engram-surface-daemon.py {engram_home}"],
            },
        ]

        matched = simulate_pgrep_f(pattern, table)
        assert matched == [target_pid, wrapper_pid], (
            f"expected only [{target_pid}, {wrapper_pid}] (target + wrapper), got {matched}"
        )


# ===========================================================================
# SAFE site 3: harness-specific Claude boot-charge components' monitor-verify
# pgrep -u "$USER" -af '...' step (#2029, #2028)
# ===========================================================================


class TestBootChargeMonitorVerifyPatternIsProcessNameAnchored:
    """Both Claude charge components instruct the booting agent to verify the forum
    monitors are actually running via `pgrep -u "$USER" -af
    '^(\\S*/)?bash\\s+\\S*forum-(mention|updates)-monitor\\.sh'`. The charge
    text itself QUOTES both monitor names and this very pgrep pattern as
    prose, so the invoking session's own argv is the self-match decoy this
    site has to survive -- the same trap #1933 fixed for the two monitor
    scripts, fixed here by #2028 via the leading `^(\\S*/)?bash` process-name
    anchor instead of an argv-verification second layer."""

    _DEFAULT_MD = _AUTOSTART / "boot-charge-default-claude.md"
    _QUIET_MD = _AUTOSTART / "boot-charge-quiet-claude.md"

    def test_both_boot_charge_files_carry_the_identical_pattern(self):
        default_pattern = _extract_boot_charge_pgrep_pattern(self._DEFAULT_MD)
        quiet_pattern = _extract_boot_charge_pgrep_pattern(self._QUIET_MD)
        assert default_pattern == quiet_pattern, (
            "boot-charge-default-claude.md and boot-charge-quiet-claude.md must carry the "
            "IDENTICAL monitor-verify pattern -- a hand-edit to one without "
            "the other is exactly the drift this assertion exists to catch"
        )

    def test_matches_the_monitors_excludes_cotenant_and_self_quoting_charge_text(self):
        pattern = _extract_boot_charge_pgrep_pattern(self._DEFAULT_MD)

        target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid = 1, 2, 3, 4
        table = [
            # target: a genuine running forum-mention-monitor.sh, this
            # agent's own uid.
            {
                "pid": target_pid,
                "uid": 1000,
                "cmdline": [
                    "bash",
                    "/home/agentX/.engram/marketplace/plugins/engram/tools/forum-mention-monitor.sh",
                ],
            },
            # co-tenant equivalent: same script, running for a DIFFERENT
            # agent -- must be excluded by uid-filtering BEFORE the regex is
            # even applied, modeling the real `-u "$USER"` scope.
            {
                "pid": cotenant_pid,
                "uid": 1001,
                "cmdline": [
                    "bash",
                    "/home/agentY/.engram/marketplace/plugins/engram/tools/forum-updates-monitor.sh",
                    "--kinds", "dm,baton",
                ],
            },
            # invoking-session self-match trap: this charge text's own argv
            # quotes both monitor names AND this very pgrep pattern as prose
            # (a `claude -p "<charge text>"` process) -- the ^(\S*/)?bash
            # process-name anchor is what keeps this from matching itself.
            {
                "pid": invoker_decoy_pid,
                "uid": 1000,
                "cmdline": [
                    "claude", "-p",
                    "Automatic morning boot: arm forum-mention-monitor.sh and "
                    "forum-updates-monitor.sh, then verify with pgrep -u "
                    "\"$USER\" -af '^(\\S*/)?bash\\s+\\S*forum-(mention|updates)"
                    "-monitor\\.sh' (must list both)",
                ],
            },
            # wrapper: a genuine instance launched via an absolute /bin/bash
            # path -- the leading `(\S*/)?` must still match it.
            {
                "pid": wrapper_pid,
                "uid": 1000,
                "cmdline": [
                    "/bin/bash",
                    "/home/agentX/.engram/marketplace/plugins/engram/tools/forum-updates-monitor.sh",
                    "--kinds", "dm,baton",
                ],
            },
        ]

        matched = simulate_pgrep_f(pattern, table, uid=1000)
        assert matched == [target_pid, wrapper_pid], (
            f"expected only [{target_pid}, {wrapper_pid}] (target + wrapper), got {matched}"
        )


class TestBootChargeAllPgrepInvocationsCarryTheCanonicalPattern:
    """Checks that every inline-code span in the charge naming the forum
    monitors and `pgrep` is one of the three known-good invocation spans,
    and that the selected set matches that canonical set exactly. This is
    EXACT SET MEMBERSHIP against a frozen, verbatim set of canonical spans
    -- never a PARSE of shell grammar (flag order, quote style, flag
    spelling, ...) and never mere substring CONTAINMENT. A parse-based
    check always has an axis it doesn't model, and containment alone
    misses garbage appended after the canonical text inside the same
    quotes. Exact membership sidesteps both: a selected span either IS one
    of the known strings or it is not. The set-equality half additionally
    catches a DELETED invocation, which per-span membership alone cannot
    see. See `_select_monitor_invocation_spans` below for what counts as a
    selected span and why (prior round history: commit log, #3008)."""

    _DEFAULT_MD = _AUTOSTART / "boot-charge-default-claude.md"
    _QUIET_MD = _AUTOSTART / "boot-charge-quiet-claude.md"
    _EXPECTED_MIN_COUNT = 3

    # The complete, exact, canonical invocation spans -- the full
    # inline-code span text for each of the three real pgrep
    # invocations, copied verbatim from the charge as it stood at
    # #3007's own commit (31fd8502). Frozen literals, not re-derived
    # from the file at test time, so this check can never itself become
    # a parser with its own blind spot. Membership is EXACT STRING
    # EQUALITY -- no substring, no regex, no shell-grammar parse.
    _CANONICAL_INVOCATION_SPANS = frozenset(
        {
            'pgrep -u "$USER" -af \'^(\\S*/)?bash\\s+\\S*forum-(mention|updates)-monitor\\.sh\'',
            'for pid in $(pgrep -u "$USER" -f \'^(\\S*/)?bash\\s+\\S*forum-(mention|updates)-monitor\\.sh\'); do readlink /proc/$pid/fd/1; done',
            'for pid in $(pgrep -u "$USER" -f \'^(\\S*/)?bash\\s+\\S*forum-(mention|updates)-monitor\\.sh\'); do lsof -a -p "$pid" -d 1; done',
        }
    )

    @staticmethod
    def _extract_inline_code_spans(md_path: Path) -> list[str]:
        text = md_path.read_text()
        return re.findall(r"`([^`]*)`", text)

    @classmethod
    def _select_monitor_invocation_spans(cls, md_path: Path) -> list[str]:
        """Select the inline-code spans that could plausibly be a real
        pgrep invocation targeting the forum monitors -- keyed on the
        MONITOR NAMES, never on pgrep's flag grammar, quote style, or
        flag order. `pgrep` itself is required only to separate a
        genuine invocation attempt from an unrelated mention of the
        monitor names; in this file today that excludes:

          - the two arm commands (`bash .../forum-updates-monitor*.sh`,
            `bash .../forum-mention-monitor*.sh`) -- these invoke a
            single script directly with no `pgrep` at all;
          - the bare backticked `` `pgrep` `` mention with no pattern;
          - the prose aside's bare backticked
            `` `forum-(mention|updates)-monitor` `` fragment -- this
            genuinely names both monitors (it is the illustration of
            the UNANCHORED pre-#2028 form the prose warns against) but
            carries no `pgrep` prefix, so it is correctly excluded
            rather than incorrectly required to carry the canonical
            (anchored) pattern.

        This was verified against the file rather than assumed: see
        this round's handoff for the concrete span list.
        """
        spans = cls._extract_inline_code_spans(md_path)
        return [s for s in spans if "pgrep" in s.lower() and "monitor" in s.lower()]

    def _selected_with_min_count(self, md_path: Path) -> list[str]:
        selected = self._select_monitor_invocation_spans(md_path)
        assert len(selected) >= self._EXPECTED_MIN_COUNT, (
            f"{md_path.name}: expected at least {self._EXPECTED_MIN_COUNT} "
            f"pgrep invocations naming the forum monitors (the standalone "
            f"`-af` verify line plus the Linux and Darwin loop-embedded "
            f"copies), found {len(selected)}: {selected!r} -- a lower count "
            f"means a copy was silently dropped or no longer reads as a "
            f"real invocation, not that fewer copies legitimately exist. "
            f"If a legitimate edit intentionally removed a copy, update "
            f"_EXPECTED_MIN_COUNT deliberately; a legitimate edit that adds "
            f"a fourth copy is fine here -- this is a floor, not a ceiling."
        )
        return selected

    def _assert_canonical(self, md_path: Path, label: str) -> None:
        """Shared body for the default/quiet canonical-pattern tests below --
        the two charge files differ only in which path to read and which
        filename their failure messages should name, so `label` is threaded
        through explicitly rather than derived from `md_path`."""
        selected = self._selected_with_min_count(md_path)
        for span in selected:
            assert span in self._CANONICAL_INVOCATION_SPANS, (
                f"{label}: a pgrep invocation naming "
                f"the forum monitors is not one of the canonical invocation "
                f"forms: {span!r} -- if this is a deliberate edit to a real "
                f"invocation, update _CANONICAL_INVOCATION_SPANS in this "
                f"test in the same commit."
            )
        assert set(selected) == self._CANONICAL_INVOCATION_SPANS, (
            f"{label}: the set of selected pgrep "
            f"invocations does not match the canonical set exactly -- a "
            f"canonical invocation may have been DELETED from the file "
            f"(present in _CANONICAL_INVOCATION_SPANS but not among the "
            f"selected spans). Selected: {sorted(selected)!r}; canonical: "
            f"{sorted(self._CANONICAL_INVOCATION_SPANS)!r}"
        )

    def test_default_charge_invocations_carry_the_canonical_pattern(self):
        self._assert_canonical(self._DEFAULT_MD, "boot-charge-default-claude.md")

    def test_quiet_charge_invocations_carry_the_canonical_pattern(self):
        self._assert_canonical(self._QUIET_MD, "boot-charge-quiet-claude.md")

    def test_both_charge_files_agree_on_monitor_invocation_spans(self):
        default_selected = self._select_monitor_invocation_spans(self._DEFAULT_MD)
        quiet_selected = self._select_monitor_invocation_spans(self._QUIET_MD)
        assert default_selected == quiet_selected, (
            "boot-charge-default-claude.md and boot-charge-quiet-claude.md "
            f"must carry the IDENTICAL set of pgrep invocations naming the "
            f"forum monitors, in the SAME order -- got {default_selected!r} "
            f"vs {quiet_selected!r}"
        )


class TestBootChargeMonitorStdoutTargetCheckDiscriminates:
    """pgrep proves a process exists, not that its output reaches the
    session -- a monitor armed via `nohup ... &` instead of through the
    Monitor tool passes the pgrep check identically to a correctly-armed
    one while its stdout goes to a file nothing reads (#3007). Both Claude
    charge components' monitor-verify step must therefore also instruct the
    booting agent to check the stdout target of each matched pid (Linux
    `/proc/$pid/fd/1` via `readlink`, Darwin `lsof` since macOS has no
    `/proc`).

    This class does NOT stop at a presence check. Round 1 shipped exactly
    that (asserting the paragraph's keywords were present) and a reviewer
    proved it vacuous by gutting the paragraph down to a bag of those same
    keywords with the runnable command removed -- the whole suite still
    passed. The Linux test below instead EXTRACTS the real command out of
    the charge markdown and RUNS it against two live processes whose stdout
    targets are under the test's own control (one on a socket, one on a
    regular file), asserting the command's output actually discriminates
    between them -- the same discrimination the charge's instruction exists
    to teach a booting agent to perform by hand."""

    _DEFAULT_MD = _AUTOSTART / "boot-charge-default-claude.md"
    _QUIET_MD = _AUTOSTART / "boot-charge-quiet-claude.md"

    @staticmethod
    def _extract_stdout_target_span(md_path: Path) -> str:
        text = md_path.read_text()
        m = re.search(
            r"\*\*And pgrep is not sufficient.*?had no real-time forum delivery for six hours\.",
            text,
            re.S,
        )
        assert m, f"{md_path.name}: no stdout-target-check paragraph found"
        return m.group(0)

    @staticmethod
    def _extract_linux_stdout_check_command(span: str) -> str:
        """Locate the backticked span within the paragraph that contains
        both `readlink` and `fd/1` -- the runnable Linux stdout-target
        command. If a gutting edit removes the command (while leaving the
        surrounding keyword prose intact), this extraction fails and the
        test fails with it -- this is the assertion a gutted paragraph
        trips."""
        # The `for pid in` anchor is load-bearing, not cosmetic: without it,
        # `[^`]*` happily spans the prose BETWEEN two unrelated inline-code
        # spans, so a mutation that deletes the runnable command (leaving
        # `readlink` and `fd/1` sitting in plain prose elsewhere in the
        # paragraph) still "matches" -- and the test then fails at the wrong
        # assertion downstream, reporting a reshaped loop instead of a
        # deleted command. Measured 2026-09-18.
        m = re.search(r"`(for pid in [^`]*readlink[^`]*fd/1[^`]*)`", span)
        assert m, (
            "no backticked command containing both `readlink` and `fd/1` found in "
            "the stdout-target-check paragraph -- the charge no longer carries a "
            "runnable Linux stdout-target command"
        )
        return m.group(1)

    def test_both_charge_files_carry_the_identical_stdout_target_paragraph(self):
        """A byte-identity check across the two files -- kept from round 1,
        and still not sufficient on its own: gutting both files identically
        preserves identity, which is exactly what the round-1 reviewer did.
        It is a drift guard sitting BESIDE the functional test below, never
        a substitute for it."""
        default_span = self._extract_stdout_target_span(self._DEFAULT_MD)
        quiet_span = self._extract_stdout_target_span(self._QUIET_MD)
        assert default_span == quiet_span, (
            "boot-charge-default-claude.md and boot-charge-quiet-claude.md must carry the "
            "IDENTICAL stdout-target-check paragraph -- a hand-edit to one without "
            "the other is exactly the drift this assertion exists to catch"
        )

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc/<pid>/fd is Linux-only")
    def test_linux_command_discriminates_socket_stdout_from_file_stdout(self, tmp_path):
        span = self._extract_stdout_target_span(self._DEFAULT_MD)
        cmd = self._extract_linux_stdout_check_command(span)

        sock_a, sock_b = socket.socketpair()
        log_path = tmp_path / "fum.log"
        proc_a = proc_b = None
        try:
            # pid_a: stdout on a socket -- what a correctly-armed monitor
            # (via the Monitor tool) looks like.
            proc_a = subprocess.Popen(["sleep", "30"], stdout=sock_a)
            # pid_b: stdout on a regular file -- what a `nohup ... &`
            # detached background process looks like (#3007's incident).
            with open(log_path, "w") as f:
                proc_b = subprocess.Popen(["sleep", "30"], stdout=f)

            # Repoint the pid source only: substitute the $(pgrep ...)
            # command-substitution with a literal list of the two pids just
            # spawned. Everything else -- the readlink half under test --
            # runs verbatim from the charge. Pid discovery for the
            # standalone `-af` verify line is covered by the sibling class
            # TestBootChargeMonitorVerifyPatternIsProcessNameAnchored; pid
            # discovery for THIS loop's own embedded `-f` copy (and the
            # Darwin loop's) is covered by
            # TestBootChargeAllPgrepInvocationsCarryTheCanonicalPattern's
            # content-keyed canonical-pattern assertion, not re-verified
            # here -- running the real pgrep here would pick up this seat's
            # own live monitors and make the test environment-dependent.
            repointed = re.sub(
                r"for pid in \$\(.*?\); do",
                f"for pid in {proc_a.pid} {proc_b.pid}; do",
                cmd,
            )
            assert repointed != cmd, (
                "the pid-source substitution did not change the command -- the "
                "charge's `for pid in $(...); do` loop is no longer the expected shape"
            )

            result = subprocess.run(
                ["bash", "-c", repointed], capture_output=True, text=True, timeout=10
            )
        finally:
            for proc in (proc_a, proc_b):
                if proc is not None:
                    proc.kill()
                    proc.wait()
            sock_a.close()
            sock_b.close()

        lines = result.stdout.splitlines()
        assert len(lines) == 2, (
            f"expected exactly 2 output lines (one per pid), got stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        socket_line, file_line = lines

        # This IS the discrimination the charge's instruction exists to
        # perform: a monitor whose stdout is a socket (correctly armed) vs
        # one whose stdout is a file (silently a dead end).
        assert socket_line.startswith("socket:"), (
            "the socket-backed pid's stdout-target line must START WITH `socket:` "
            "(the bracketed inode number varies per run, so this is a prefix check, "
            f"not an equality check) -- got {socket_line!r}"
        )
        assert file_line == str(log_path) and not file_line.startswith("socket:"), (
            "the file-backed pid's stdout-target line must be the log file's own "
            f"path and must NOT start with `socket:` -- got {file_line!r}"
        )

    def test_default_charge_carries_the_darwin_branch(self):
        """Darwin coverage is deliberately WEAK: CI is Linux-only, so this
        is a presence/structural check, not a functional one -- the Darwin
        `lsof` form has since been verified on two Darwin seats with both
        controls. Assert something structural rather than prose: the
        command must name `lsof` and `-d 1` (file descriptor 1 -- the
        property that makes it the right instrument), so a gutting edit
        that drops the `-d 1` is still caught even though the command
        itself cannot be run here."""
        span = self._extract_stdout_target_span(self._DEFAULT_MD)
        assert "lsof" in span
        assert "-d 1" in span
        assert "macOS has no `/proc`" in span

    def test_quiet_charge_carries_the_darwin_branch(self):
        """See test_default_charge_carries_the_darwin_branch -- same
        presence-only caveat applies here."""
        span = self._extract_stdout_target_span(self._QUIET_MD)
        assert "lsof" in span
        assert "-d 1" in span
        assert "macOS has no `/proc`" in span


# ===========================================================================
# MITIGATED OVER-MATCH: forum-mention-monitor.sh / forum-updates-monitor.sh
# ===========================================================================


class _TwoLayerMonitorFixture:
    """Shared table-building + verification-layer model for the two
    warn-not-kill, /proc-verified monitors. The candidate regex (layer 1) is
    intentionally loose (unanchored substring); the argv[0]-interpreter +
    exact-script-arg check (layer 2, mirrored here from each script's own
    _check_*_dup python heredoc -- see the script for the source of truth)
    is what actually distinguishes a genuine duplicate from a decoy.
    """

    SCRIPT: Path
    NO_REAP_ENV: str

    @classmethod
    def _patterns(cls):
        return _extract_pgrep_pattern_branches(cls.SCRIPT)

    @staticmethod
    def _layer2_is_genuine_bash_duplicate(cmdline: list[str], script_basename: str) -> bool:
        """Mirrors the embedded python in each script's _check_*_dup: argv[0]
        (dash-stripped) must be a bash-family interpreter, AND some later
        argv token must equal the script basename or end with
        "/<script_basename>"."""
        if not cmdline:
            return False
        # Mirrors the real script's `os.path.basename(argv[0]).lstrip("-")`
        # exactly -- basename FIRST, then dash-strip -- not the other order.
        interp = cmdline[0].rsplit("/", 1)[-1]
        interp = interp.lstrip("-")
        if interp not in ("bash", "sh", "dash", "ksh", "zsh", "ash"):
            return False
        return any(a == script_basename or a.endswith("/" + script_basename) for a in cmdline[1:])

    def _build_table(self, script_basename: str, purpose: str):
        target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid = 1, 2, 3, 4
        table = [
            # target: a genuine second instance of this monitor, same uid,
            # same purpose -- the thing the reap is supposed to find.
            {
                "pid": target_pid,
                "uid": 1000,
                "cmdline": ["bash", f"/home/agentX/.engram/tools/{script_basename}", "--purpose", purpose],
            },
            # co-tenant equivalent: same script, same purpose, DIFFERENT uid
            # -- must be excluded by -u before the regex is even applied.
            {
                "pid": cotenant_pid,
                "uid": 1001,
                "cmdline": ["bash", f"/home/agentY/.engram/tools/{script_basename}", "--purpose", purpose],
            },
            # invoking-process self-quote decoy: the real #1933 repro shape
            # -- a python process (e.g. standing in for `claude` with a boot
            # charge in argv) whose argv merely QUOTES this script's launch
            # line + purpose as a substring.
            {
                "pid": invoker_decoy_pid,
                "uid": 1000,
                "cmdline": [
                    "python3", "-c", "import time; time.sleep(9)",
                    f"Automatic morning boot charge: bash /home/agentX/.engram/tools/{script_basename} "
                    f"--kinds dm,baton (unrelated instructions) --purpose {purpose} "
                    "(padding to resemble real charge length) continue with the day's plan",
                ],
            },
            # wrapper: a genuine instance launched login-shell style
            # (`exec -a "-bash" bash <script>` -> argv[0] == "-bash").
            {
                "pid": wrapper_pid,
                "uid": 1000,
                "cmdline": ["-bash", f"/home/agentX/.engram/tools/{script_basename}", "--purpose", purpose],
            },
        ]
        return table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid)


class TestForumMentionMonitorCandidatePattern(_TwoLayerMonitorFixture):
    SCRIPT = _TOOLS / "forum-mention-monitor.sh"
    NO_REAP_ENV = "FORUM_MENTION_NO_REAP"

    def test_layer1_candidate_regex_over_matches_the_self_quote_decoy(self):
        """This is the documented, mitigated over-match: at the raw-regex
        candidate stage alone (uid-filtered, but before /proc verification),
        the self-quoting decoy IS a candidate. This is expected -- it is
        exactly why the second verification layer exists -- and it is safe
        only because the action is warn-not-kill (proven end-to-end by
        tests/test_forum_monitor_duplicate_warn.py, which exercises the real
        script against real processes)."""
        patterns = self._patterns()
        purpose = "unittestpurpose"
        pattern = patterns["named"].format(purpose=purpose)
        table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid) = self._build_table(
            "forum-mention-monitor.sh", purpose
        )

        matched = simulate_pgrep_f(pattern, table, uid=1000)
        assert cotenant_pid not in matched, "uid scope must exclude the co-tenant"
        assert target_pid in matched
        assert wrapper_pid in matched
        assert invoker_decoy_pid in matched, (
            "the raw candidate regex is expected to over-match the "
            "self-quoting decoy at layer 1 -- this is what layer 2 exists to filter"
        )

    def test_layer2_verification_filters_the_decoy_but_keeps_the_wrapper(self):
        """Full two-layer pipeline (layer 1 candidate + layer 2 argv
        verification) matches EXACTLY the target + the login-shell wrapper,
        excluding the co-tenant (layer 0: uid) and the self-quote decoy
        (layer 2: not a bash-family process actually running the script)."""
        patterns = self._patterns()
        purpose = "unittestpurpose2"
        pattern = patterns["named"].format(purpose=purpose)
        table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid) = self._build_table(
            "forum-mention-monitor.sh", purpose
        )

        layer1 = simulate_pgrep_f(pattern, table, uid=1000)
        by_pid = {p["pid"]: p for p in table}
        verified = [
            pid for pid in layer1
            if self._layer2_is_genuine_bash_duplicate(
                by_pid[pid]["cmdline"], "forum-mention-monitor.sh"
            )
        ]
        assert verified == [target_pid, wrapper_pid]
        assert invoker_decoy_pid not in verified
        assert cotenant_pid not in verified


class TestForumUpdatesMonitorCandidatePattern(_TwoLayerMonitorFixture):
    SCRIPT = _TOOLS / "forum-updates-monitor.sh"
    NO_REAP_ENV = "FORUM_UPDATES_NO_REAP"

    def test_layer1_candidate_regex_over_matches_the_self_quote_decoy(self):
        patterns = self._patterns()
        purpose = "unittestpurpose"
        pattern = patterns["named"].format(purpose=purpose)
        table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid) = self._build_table(
            "forum-updates-monitor.sh", purpose
        )

        matched = simulate_pgrep_f(pattern, table, uid=1000)
        assert cotenant_pid not in matched
        assert target_pid in matched
        assert wrapper_pid in matched
        assert invoker_decoy_pid in matched

    def test_layer2_verification_filters_the_decoy_but_keeps_the_wrapper(self):
        patterns = self._patterns()
        purpose = "unittestpurpose2"
        pattern = patterns["named"].format(purpose=purpose)
        table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid) = self._build_table(
            "forum-updates-monitor.sh", purpose
        )

        layer1 = simulate_pgrep_f(pattern, table, uid=1000)
        by_pid = {p["pid"]: p for p in table}
        verified = [
            pid for pid in layer1
            if self._layer2_is_genuine_bash_duplicate(
                by_pid[pid]["cmdline"], "forum-updates-monitor.sh"
            )
        ]
        assert verified == [target_pid, wrapper_pid]
        assert invoker_decoy_pid not in verified
        assert cotenant_pid not in verified


# ===========================================================================
# FINDING (xfail, not fixed here): collab-baton-monitor.sh --
# single-layer unanchored pgrep -f feeding
# directly into `kill`, no argv verification, no warn-not-kill guard.
# ===========================================================================


class _SingleLayerReapMonitorFixture:
    SCRIPT: Path

    def _build_table(self, script_basename: str, purpose: str):
        target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid = 1, 2, 3, 4
        table = [
            # target: a genuine second instance -- this IS meant to be
            # reaped (last-arm-wins by design).
            {
                "pid": target_pid,
                "uid": 1000,
                "cmdline": ["bash", f"/home/agentX/.engram/tools/{script_basename}", "--purpose", purpose],
            },
            # co-tenant equivalent, different uid -- must be excluded by -u.
            {
                "pid": cotenant_pid,
                "uid": 1001,
                "cmdline": ["bash", f"/home/agentY/.engram/tools/{script_basename}", "--purpose", purpose],
            },
            # invoking-process self-quote decoy -- the #1933 repro shape,
            # UNFIXED here: this script has no second layer to filter it.
            {
                "pid": invoker_decoy_pid,
                "uid": 1000,
                "cmdline": [
                    "python3", "-c", "import time; time.sleep(9)",
                    f"Automatic morning boot charge: bash /home/agentX/.engram/tools/{script_basename} "
                    f"--purpose {purpose} (unrelated instructions) continue with the day's plan",
                ],
            },
            # wrapper: a genuine second instance via login-shell argv0.
            {
                "pid": wrapper_pid,
                "uid": 1000,
                "cmdline": ["-bash", f"/home/agentX/.engram/tools/{script_basename}", "--purpose", purpose],
            },
        ]
        return table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid)

    def _pattern(self, script_basename: str, purpose: str) -> str:
        patterns = _extract_pgrep_pattern_branches(self.SCRIPT)
        return patterns["named"].format(purpose=purpose)


class TestCollabBatonMonitorReapPattern(_SingleLayerReapMonitorFixture):
    """FIXED by #1935 (porting #1933/#2028's process-name-anchored candidate
    pattern -- collab-baton-monitor.sh no longer feeds an unanchored
    substring match straight into `kill`; the candidate pgrep pattern is now
    itself anchored on `^(\\S*/)?-?bash\\s+...` so the self-quoting-charge-text
    decoy is excluded even before any /proc/<pid>/cmdline verification runs,
    and the script also gained the /proc two-layer verification + warn-not-kill
    posture (see the script's own inline comments)."""

    SCRIPT = _TOOLS / "collab-baton-monitor.sh"

    def test_reap_pattern_matches_exactly_the_target_and_wrapper(self):
        purpose = "unittestpurpose"
        pattern = self._pattern("collab-baton-monitor.sh", purpose)
        table, (target_pid, cotenant_pid, invoker_decoy_pid, wrapper_pid) = self._build_table(
            "collab-baton-monitor.sh", purpose
        )

        matched = simulate_pgrep_f(pattern, table, uid=1000)
        # Desired safe behavior (what this asserts, and what currently
        # fails): only the target + wrapper (genuine instances meant to be
        # reaped) match -- the self-quote decoy must NOT.
        assert matched == [target_pid, wrapper_pid]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
