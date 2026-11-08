"""#4502: the HF_HUB_ETAG_TIMEOUT tests must leave `forum.embeddings` as they found it.

`TestHfHubEtagTimeoutEnvSet` in test_embeddings_offline_load.py deletes
`sys.modules["forum.embeddings"]` and re-imports it. If it does not undo that,
`sys.modules["forum.embeddings"]` (and the `forum` package attribute) is a NEW
module object while `forum.server.emb_mod` holds the old one, so a later
string-path patch (`monkeypatch.setattr("forum.embeddings.encode", ...)`) lands
on an object the server never calls.

Mechanism: one child pytest process runs the two real etag tests and then a
tiny helper module (written to tmp_path) in the SAME session, so the helper
sees the module state the etag tests left behind. The helper imports
`forum.server` at collection, i.e. before the etag tests run, so its
`emb_mod` is the original object. A subprocess is used so a failure here
cannot disturb this session's own module state.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
ETAG_FILE = Path(__file__).resolve().parent / "test_embeddings_offline_load.py"
ETAG_TESTS = (
    "test_hf_hub_etag_timeout_env_set",
    "test_hf_hub_etag_timeout_respects_operator_override",
)

HELPER = '''
import sys

import forum
import forum.server as server

ORIGINAL = server.emb_mod


def test_sys_modules_entry_is_servers_module():
    assert sys.modules["forum.embeddings"] is ORIGINAL


def test_forum_package_attribute_is_servers_module():
    assert forum.embeddings is ORIGINAL
'''


@pytest.fixture(scope="module")
def child_lines(tmp_path_factory):
    helper = tmp_path_factory.mktemp("helper4502") / "test_helper_4502.py"
    helper.write_text(HELPER)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "-v", "-rA", "--rootdir", str(REPO),
         f"{ETAG_FILE}::TestHfHubEtagTimeoutEnvSet", str(helper)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.stdout.splitlines(), proc.stdout


def _outcome(lines, name):
    return [l.split()[0] for l in lines
            if l.split()[:1] in (["PASSED"], ["FAILED"]) and name in l]


def test_the_two_etag_tests_really_ran_and_passed(child_lines):
    lines, out = child_lines
    for name in ETAG_TESTS:
        assert _outcome(lines, name) == ["PASSED"], out


def test_sys_modules_entry_restored_after_etag_tests(child_lines):
    lines, out = child_lines
    assert _outcome(lines, "test_sys_modules_entry_is_servers_module") == ["PASSED"], out


def test_forum_package_attribute_restored_after_etag_tests(child_lines):
    lines, out = child_lines
    assert _outcome(lines, "test_forum_package_attribute_is_servers_module") == ["PASSED"], out


def test_the_helper_ran_after_the_etag_tests(child_lines):
    """The identity check only means something if it ran AFTER the two etag tests, in the
    same session. Argument order on the child command line decides that, and a swap would
    keep every other cell green, so pin the run order from the child's verbose lines
    (the `path::test PASSED` progress lines, not the `-rA` summary lines)."""
    lines, out = child_lines
    progress = [l for l in lines if "::" in l and not l.startswith(("PASSED", "FAILED"))
                and ("PASSED" in l or "FAILED" in l)]
    def pos(name):
        hits = [i for i, l in enumerate(progress) if name in l]
        assert len(hits) == 1, (name, hits, out)
        return hits[0]
    last_etag = max(pos(n) for n in ETAG_TESTS)
    for helper_test in ("test_sys_modules_entry_is_servers_module",
                        "test_forum_package_attribute_is_servers_module"):
        assert pos(helper_test) > last_etag, out
