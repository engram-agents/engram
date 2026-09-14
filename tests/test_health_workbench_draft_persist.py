"""Pure-data-transform parity test for the Curve Workbench's draft/edit
persistence merge (engram-alpha#2870 fix 2 item 3):
``health-workbench.js``'s ``mergeDraftsForPersist(edited, curves, drafts)``
decides what actually gets written to localStorage on every ``saveEdits()``
call.

``health-workbench.js`` (unlike ``health-workbench-curve.js``) is NOT
safely ``require()``-able whole under plain node: its IIFE unconditionally
calls ``document.addEventListener(...)`` at the top level the instant the
file loads, with no ``module.exports`` guard reached before that point --
confirmed live: ``require()``-ing the whole file under node throws
``ReferenceError: document is not defined`` before any exported function
would ever be reachable, so the naive whole-file-``require()`` convention
``tests/test_health_workbench_curve_parity.py`` uses does not transfer here.

So this test extracts JUST the ``mergeDraftsForPersist`` function's own
source text from the served file (brace-matched, not regex-truncated --
robust to the nested braces its own ``forEach`` callbacks contain) and
evaluates that extracted, self-contained function standalone under
``node -e``. ``mergeDraftsForPersist`` takes plain objects and returns a
plain object with no DOM/localStorage access inside it, so this is a
faithful run of the real function body, not a reimplementation -- same "run
the real JS under node, never fake it" discipline as the curve-parity test
above, adapted to a file that can't be required whole.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None


def _extract_function_source(js_text: str, func_name: str) -> str:
    """Brace-matched extraction of one top-level function's full source,
    from its ``function <name>(`` header through its own matching closing
    brace -- counts nesting depth rather than cutting at the first ``}``,
    so it doesn't truncate early on a function (like this one) that
    contains nested blocks of its own."""
    marker = "function " + func_name + "("
    start = js_text.index(marker)
    brace_start = js_text.index("{", start)
    depth = 0
    i = brace_start
    while i < len(js_text):
        c = js_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js_text[start : i + 1]
        i += 1
    raise AssertionError("unbalanced braces while extracting " + func_name)


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestMergeDraftsForPersist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js_text = _JS_PATH.read_text(encoding="utf-8")
        cls._func_src = _extract_function_source(js_text, "mergeDraftsForPersist")

    def _run(self, edited: dict, curves: dict, drafts: dict) -> dict:
        script = (
            self._func_src
            + "\nconsole.log(JSON.stringify(mergeDraftsForPersist("
            + json.dumps(edited)
            + ", "
            + json.dumps(curves)
            + ", "
            + json.dumps(drafts)
            + ")));"
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    def test_unloaded_draft_survives_an_unrelated_save(self):
        """The exact regression fix 2 item 3 exists to prevent: editing
        signal B (triggering markEdited(B) -> saveEdits()) must not
        silently drop signal A's still-unloaded draft from what's written
        to localStorage."""
        drafts = {"A": {"knots": [[0, 0], [1, 1]], "importance": 3}}
        curves = {"B": {"knots": [[0, 0], [0.5, 0.5], [1, 1]], "importance": 2}}
        edited = {"B": True}
        out = self._run(edited, curves, drafts)
        self.assertIn("A", out)
        self.assertEqual(out["A"], drafts["A"])
        self.assertIn("B", out)
        self.assertEqual(out["B"], curves["B"])

    def test_signal_in_both_returns_curves_value_not_drafts_value(self):
        """A signal present in both `edited` and `drafts` (a loaded-then-
        still-edited draft) must be written from the LIVE `curves` value,
        never the stale `drafts` copy."""
        drafts = {"A": {"knots": [[0, 0], [1, 1]], "importance": 1}}
        curves = {"A": {"knots": [[0, 0], [0.3, 0.9], [1, 1]], "importance": 5}}
        edited = {"A": True}
        out = self._run(edited, curves, drafts)
        self.assertEqual(out["A"], curves["A"])
        self.assertNotEqual(out["A"], drafts["A"])

    def test_signal_in_neither_is_absent_from_result(self):
        """A signal in neither `edited` nor `drafts` is correctly dropped
        -- also exercises the fully-empty case."""
        self.assertEqual(self._run({}, {}, {}), {})
        out = self._run(
            {"B": True},
            {"B": {"knots": [[0, 0], [1, 1]], "importance": 1}},
            {},
        )
        self.assertNotIn("A", out)
        self.assertIn("B", out)


if __name__ == "__main__":
    unittest.main()
