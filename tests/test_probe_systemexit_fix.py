"""Tests for engram-alpha#2666 [part of #2652] — the daemon liveness probe's
bare `except:` swallowing its own `sys.exit(0)`.

`start-engram-daemon.sh` embeds a small Python heredoc (invoked as
`{{PYTHON}} -c "..."`) that connects to the daemon's UNIX socket and reports
liveness via exit code: 0 for a successful connect, 1 for any failure. A bare
`except:` catches *everything*, including the `SystemExit` raised by the
success path's own `sys.exit(0)` -- so the probe exited 1 unconditionally,
connect success or failure, and the surrounding `if ... ; then` in the bash
script never took the "healthy, no-op" branch. The fix narrows the clause to
`except Exception:`, which does not catch `SystemExit` (it inherits from
`BaseException`, not `Exception`), while still catching genuine connection
failures (`ConnectionRefusedError`, `FileNotFoundError`, `socket.timeout`,
...).

This file extracts the probe snippet verbatim from the shipped `.sh` file at
test time (rather than a hand-copied paraphrase) so the test is provably
exercising the same text that ships. It also pins the ORIGINAL (pre-fix,
buggy) snippet as a literal string for the falsification control below --
deliberately NOT read via a `git show <ref>:<path>` lookup, since `<ref>`
would need to be an immutable historical commit and `origin/dev` itself will
carry the fix once this PR merges, silently turning the control into a
no-op. The pinned literal is exactly the text this file's own author read
off `origin/dev` before applying the fix (verified in the PR body).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "start-engram-daemon.sh"
)

# The exact pre-fix snippet, as shipped on origin/dev immediately before this
# PR's fix commit (confirmed via `git show origin/dev:<path>` at authoring
# time -- see PR body for the literal quote). Pinned as a literal rather than
# read live so the falsification control stays meaningful even after
# origin/dev itself carries the fix.
_ORIGINAL_BUGGY_SNIPPET = (
    "import socket, sys\n"
    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "s.settimeout(2)\n"
    "try:\n"
    "    s.connect('$SOCKET_PATH')\n"
    "    s.close()\n"
    "    sys.exit(0)\n"
    "except:\n"
    "    sys.exit(1)"
)

# The heredoc-body extraction pattern: everything between the opening
# `-c "` line and the closing `"` line that precedes the invocation's stderr
# redirection. Equivalent in effect to sed-extracting the same span from the
# file; done in Python here for portability.
#
# #2676 widened this from a literal `2>/dev/null` to `2>\S+` (matches
# `2>/dev/null` AND the `2>&1` this unit introduced when it started
# capturing the probe's stderr -- the exception class name -- via command
# substitution instead of discarding it). The heredoc BODY this test cares
# about (the Python source between the quotes) is unaffected either way;
# only how the surrounding bash line disposes of stderr changed.
_HEREDOC_BODY_RE = re.compile(r'-c "\n(.*?)\n" 2>\S+', re.DOTALL)


def _extract_probe_snippet(script_text: str) -> str:
    """Pull the liveness-probe Python heredoc body verbatim out of the
    shell script's text. Raises if the shape has moved/changed (the
    spec's escape valve -- a diverged shape should fail loudly, not
    silently test the wrong thing)."""
    m = _HEREDOC_BODY_RE.search(script_text)
    if not m:
        raise AssertionError(
            "could not locate the liveness-probe heredoc in "
            "start-engram-daemon.sh -- its shape has diverged from what "
            "this test expects; do not guess, fix the extraction or "
            "escalate"
        )
    return m.group(1)


def _run_probe(snippet_template: str, socket_path: str) -> subprocess.CompletedProcess:
    """Substitute the shell-style `$SOCKET_PATH` placeholder with a real
    path and run the resulting Python snippet as a subprocess, exactly as
    the shipped bash script invokes it (`{{PYTHON}} -c "<snippet>"`)."""
    snippet = snippet_template.replace("$SOCKET_PATH", socket_path)
    return subprocess.run(
        [sys.executable, "-c", snippet], capture_output=True, text=True, timeout=10
    )


class TestProbeAgainstFixedScript(unittest.TestCase):
    """Runs the CURRENT (fixed) snippet, extracted live from the shipped
    .sh file, so this test is provably exercising the same text that
    ships."""

    def setUp(self):
        self.snippet = _extract_probe_snippet(SCRIPT_PATH.read_text())
        # #2676 changed the clause from `except Exception:` to
        # `except Exception as e:` (binding the caught exception so its
        # class name can be printed to stderr) -- still not a bare
        # `except:`, so the #2666 fix this class exists to pin is intact.
        self.assertIn(
            "except Exception as e:",
            self.snippet,
            "the live script no longer reads 'except Exception as e:' -- "
            "extraction may have grabbed stale/wrong text, or the fix "
            "was reverted",
        )

        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)

    def test_bound_and_listening_socket_exits_zero(self):
        """A real, bound, listening socket with a successful connect must
        report exit code 0 -- the case the bare `except:` broke. This
        fixture never calls accept(); the probe cannot distinguish it from
        a serving daemon, which is a known limit of connect()-based
        liveness, not a gap in this test."""
        socket_path = self.tmp / "recall-daemon.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(socket_path))
            srv.listen(1)
            result = _run_probe(self.snippet, str(socket_path))
            self.assertEqual(
                result.returncode, 0,
                f"healthy socket connect must exit 0; got {result.returncode} "
                f"(stderr: {result.stderr!r})",
            )
        finally:
            srv.close()

    def test_missing_socket_path_exits_one(self):
        """A socket path that provably does not exist must report exit
        code 1 -- a genuine failure, correctly detected."""
        socket_path = self.tmp / "no-such-socket.sock"
        self.assertFalse(socket_path.exists(), "test setup broken -- path exists")
        result = _run_probe(self.snippet, str(socket_path))
        self.assertEqual(
            result.returncode, 1,
            f"missing socket path must exit 1; got {result.returncode} "
            f"(stderr: {result.stderr!r})",
        )

    def test_bound_but_not_listening_exits_one(self):
        """Optional extra case (spec item 4): a socket that's bound but has
        no listener (connection refused) must still correctly report exit
        code 1 under the fix -- `Exception` still catches
        `ConnectionRefusedError`."""
        socket_path = self.tmp / "bound-not-listening.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(socket_path))
            # Deliberately never call .listen() -- a connect() to a bound,
            # non-listening AF_UNIX socket raises ConnectionRefusedError.
            result = _run_probe(self.snippet, str(socket_path))
            self.assertEqual(
                result.returncode, 1,
                f"bound-but-not-listening socket must exit 1; got "
                f"{result.returncode} (stderr: {result.stderr!r})",
            )
        finally:
            srv.close()


class TestFalsificationControlAgainstOriginalBuggySnippet(unittest.TestCase):
    """Runs the SAME two primary cases above against the pinned ORIGINAL
    (pre-fix, bare `except:`) snippet. Proves the test suite above is
    actually sensitive to the bug being fixed, not merely a shape that
    happens to pass regardless of the except clause.

    Expected: the healthy-connect case goes RED (exits 1 instead of the
    expected 0) against the buggy snippet, while the missing-path case
    stays GREEN either way (it was already correctly failing before the
    fix -- the bug only ever manifested on the success path).
    """

    def setUp(self):
        self.assertIn(
            "except:",
            _ORIGINAL_BUGGY_SNIPPET,
            "pinned control snippet malformed",
        )
        self.assertNotIn(
            "except Exception:",
            _ORIGINAL_BUGGY_SNIPPET,
            "pinned control snippet malformed -- must be the bare-except "
            "pre-fix form",
        )

        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)

    def test_healthy_listening_socket_goes_red_under_original_bug(self):
        """The important falsification case: against the UNFIXED snippet,
        a genuinely healthy connect must incorrectly exit 1 (not 0) --
        proving this test is sensitive to the actual bug."""
        socket_path = self.tmp / "recall-daemon.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(socket_path))
            srv.listen(1)
            result = _run_probe(_ORIGINAL_BUGGY_SNIPPET, str(socket_path))
            self.assertEqual(
                result.returncode, 1,
                "falsification control failed: the pre-fix bare `except:` "
                "snippet was expected to incorrectly exit 1 on a healthy "
                f"connect (the bug), but got {result.returncode} instead -- "
                "this test is NOT sensitive to the bug it claims to guard",
            )
        finally:
            srv.close()

    def test_missing_socket_path_stays_green_under_original_bug(self):
        """The already-correctly-failing case must remain green (exit 1)
        under the pre-fix snippet too -- the bug only manifested on the
        success path, never here."""
        socket_path = self.tmp / "no-such-socket.sock"
        self.assertFalse(socket_path.exists(), "test setup broken -- path exists")
        result = _run_probe(_ORIGINAL_BUGGY_SNIPPET, str(socket_path))
        self.assertEqual(
            result.returncode, 1,
            f"missing socket path must exit 1 under the pre-fix snippet "
            f"too; got {result.returncode} (stderr: {result.stderr!r})",
        )


if __name__ == "__main__":
    unittest.main()
