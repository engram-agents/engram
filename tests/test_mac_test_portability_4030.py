"""#4030 causes 3 and 4: two test files that only passed on Linux or on a vec-capable interpreter.

B1/B2  tests/test_pr_checks_merge_group.py must resolve bash instead of hard-coding /usr/bin/bash
       (macOS has no /usr/bin/bash).
A1/A2  tests/test_relative_engram_home_step3b_3863.py::_check_surgical may ignore ONLY the #728
       sqlite-vec advisory on stderr; the prefix it filters on is pinned to the real message.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
MERGE_GROUP = TESTS / "test_pr_checks_merge_group.py"
STEP3B = TESTS / "test_relative_engram_home_step3b_3863.py"
ENGRAM_CORE = REPO / "src" / "engram" / "engram_core.py"

ADVISORY_LINE = (
    "[engram] sqlite-vec unavailable — semantic search is on the slow "
    "O(N) pure-Python cosine fallback; install sqlite-vec==0.1.9 in this "
    "interpreter to enable the vec0 KNN index (see #728).\n"
)


def _load_step3b():
    spec = importlib.util.spec_from_file_location("_step3b_3863_under_test", STEP3B)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surgical_result(stderr: str):
    # a result _check_surgical would accept apart from stderr: node looked up, exit 0
    return SimpleNamespace(stderr=stderr, stdout=json.dumps({"node": "ob_9"}), returncode=0)


def test_b1_merge_group_test_has_no_usr_bin_bash_literal():
    # a comment line is skipped; a shebang string literal ("#!/usr/bin/bash") is NOT a comment line
    hits = [
        (n, ln) for n, ln in enumerate(MERGE_GROUP.read_text().splitlines(), 1)
        if "/usr/bin/bash" in ln and not ln.lstrip().startswith("#")
    ]
    assert hits == [], hits


def test_b2_bash_resolves_on_this_host():
    found = shutil.which("bash", path="/usr/bin:/bin")
    assert found is not None
    assert Path(found).name == "bash"


def test_a1_filter_allows_only_the_advisory():
    mod = _load_step3b()
    assert mod.SQLITE_VEC_ADVISORY_PREFIX  # an empty prefix would filter every line
    mod._check_surgical(_surgical_result(ADVISORY_LINE), None, None)  # advisory alone passes
    with pytest.raises(AssertionError):
        mod._check_surgical(_surgical_result(ADVISORY_LINE + "Traceback: boom\n"), None, None)
    # A filter widened past the exact prefix must be caught: a different [engram] line, and the forum advisory.
    with pytest.raises(AssertionError):
        mod._check_surgical(_surgical_result(ADVISORY_LINE + "[engram] some other engram message\n"), None, None)
    with pytest.raises(AssertionError):
        mod._check_surgical(
            _surgical_result(ADVISORY_LINE + "[forum] sqlite-vec unavailable -- vec0 KNN index is off\n"), None, None
        )


def test_a2_advisory_prefix_matches_the_real_message():
    prefix = _load_step3b().SQLITE_VEC_ADVISORY_PREFIX
    source = ENGRAM_CORE.read_text()
    assert f'"{prefix}' in source, "the #728 message changed; update the prefix in the step-3b test"
    assert ADVISORY_LINE.startswith(prefix)
