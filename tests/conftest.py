"""Pytest configuration for engram-alpha tests.

Adds the repo root to sys.path so test files can `import server`.
This mirrors the `sys.path.insert(0, dirname(__file__))` pattern each
file uses when run as a script — keeping tests runnable both ways.
"""

import os
import socket
import subprocess
import sys
import tempfile
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")
if SRC_ENGRAM not in sys.path:
    sys.path.insert(0, SRC_ENGRAM)
# Phase 2: tools/engine moved to src/build/engine; tools/engine is a compat
# symlink at repo root. REPO_ROOT must come BEFORE SRC_ENGRAM in sys.path so
# Python finds tools/ at REPO_ROOT/tools/ (which has the engine symlink) rather
# than at src/engram/tools/ (which no longer has engine/ after Phase 2).
# pytest may have already added REPO_ROOT at a later position; force it to front.
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

# #2565 — sys.path-collision guard for the `tools/` package-name clash.
#
# Two genuinely different `tools` packages share the same import name:
# repo-root's own tools/ (containing tools/engine, needed by e.g.
# test_engine_phase3.py's leak-scan/manifest tests) and src/engram/tools/
# (the plugin-bundled tools, a different package). Whenever something puts
# src/engram ahead of the repo root on sys.path, `import tools` resolves to
# the wrong one and gets CACHED in sys.modules for the rest of the process
# -- silently poisoning any LATER test file in the same pytest invocation
# that needs the real tools.engine. Third recurrence (#2063, PR#2564,
# this one) -- the rule-of-three threshold for building the durable check
# rather than fixing the instance again.
#
# tools/__init__.py at the repo root is ITSELF a symlink into
# src/engram/tools/__init__.py, so Path.resolve() collapses both import
# paths to the identical real file and would defeat this check -- the
# discriminator is which sys.path ENTRY Python actually used to find the
# module, which os.path.abspath (NOT realpath/resolve) preserves.
_EXPECTED_TOOLS_DIR = os.path.join(REPO_ROOT, "tools")


def _assert_tools_package_is_repo_root() -> None:
    """Proactive, filesystem-based check -- does NOT require `tools` to
    already be imported. If `tools` is already cached, trust that (an
    import already committed to a resolution; re-deriving from sys.path
    here could disagree with what Python actually did). Otherwise, scan
    sys.path in order for the first entry that contains a same-named
    tools/__init__.py -- that is what the NEXT `import tools` would
    resolve to, whether or not it has happened yet. Checking this
    proactively (not only "is the wrong one already cached") is what lets
    the `pytest_collectstart` hook below fire BEFORE a later file's own
    `import tools...` line raises a raw, less-attributable
    ModuleNotFoundError (the actual shape of the #2564 incident: the
    victim file's own top-level import is what failed, not a fixture
    running after successful collection)."""
    mod = sys.modules.get("tools")
    if mod is not None and getattr(mod, "__file__", None) is not None:
        actual_dir = os.path.dirname(os.path.abspath(mod.__file__))
    else:
        actual_dir = None
        for entry in sys.path:
            candidate = os.path.join(entry, "tools", "__init__.py")
            if os.path.isfile(candidate):
                actual_dir = os.path.dirname(os.path.abspath(candidate))
                break
        if actual_dir is None:
            return  # no `tools` package resolvable at all -- not this guard's concern
    if actual_dir != _EXPECTED_TOOLS_DIR:
        raise RuntimeError(
            "sys.path collision (#2565): `import tools` would resolve to "
            f"{actual_dir!r}, not the repo-root package at "
            f"{_EXPECTED_TOOLS_DIR!r}. Some test file (or an env var like "
            "PYTHONPATH) put another directory ahead of the repo root on "
            "sys.path -- tools.engine and other repo-root-tools-only "
            "imports will fail or misbehave for every test module "
            f"collected after the one that caused this. Current sys.path: "
            f"{sys.path!r}"
        )


@pytest.fixture(autouse=True, scope="module")
def _sys_path_collision_guard():
    """Runs once per test MODULE, after that module's own top-level code
    (including any sys.path manipulation it does at import time) has
    already executed.

    Attribution correction (Borges's colleague-review finding on PR #2661):
    under pytest's default collect-then-run order (no xdist / pytest-randomly
    / pytest-order / --forked on this repo), EVERY module is collected --
    and therefore imported -- before ANY module-scoped fixture runs. So for
    an IMPORT-TIME collision (a module's own top-level
    `sys.path.insert(0, ...)`, the actual shape of the #2564/#2565
    incident), `pytest_collectstart` below has ALREADY caught it before
    this fixture ever gets a turn on that same module -- this fixture is
    NOT the first line of defense there, whatever an earlier version of
    this docstring implied. What this fixture actually, uniquely catches
    is a RUN-PHASE collision: one a test body introduces during test
    EXECUTION rather than at import time, which `pytest_collectstart`
    structurally cannot see (it only fires before collection, never during
    a test's own run). Keep both -- they cover genuinely different
    phases -- but don't read this fixture as redundant belt-and-braces on
    the import-time case; collectstart alone already covers that one.

    Together with `pytest_collectstart`, turns "a new test file quietly
    breaks a later, unrelated test file" into an immediate, attributable
    failure naming the actual cause, instead of a confusing
    ModuleNotFoundError surfacing three files later -- but for the
    import-time case that credit belongs to `pytest_collectstart`, per
    the attribution above; this fixture's own share of that credit is
    the run-phase case only.

    Checked in, not just asserted: the run-phase claim above is a
    falsifiable property, and
    tests/test_sys_path_collision_guard_2565.py::test_run_phase_collision_is_caught_by_the_fixture_not_collectstart
    is the regression test that verifies it (Borges's colleague-review
    finding on PR #2665: he confirmed it live before approving, but the
    docstring stated it as fact with nothing in the suite checking it --
    a hedge that did not survive compression into code).

    Unlike `pytest_collectstart` below, this fixture lets
    `_assert_tools_package_is_repo_root`'s RuntimeError propagate raw
    rather than converting it to `pytest.exit()` -- deliberately, not an
    oversight: a fixture failure is scoped and attributable per-module
    (pytest reports it as a clean "ERROR at setup of <test>", not a
    crash), unlike `pytest_collectstart`, which pytest calls with no
    surrounding try/except and would turn a bare raise into an
    INTERNALERROR (see that hook's own docstring for the confirmed
    incident). Same underlying error, two different call-sites, two
    different safe handling strategies -- not an inconsistency."""
    _assert_tools_package_is_repo_root()
    yield


def pytest_collectstart(collector) -> None:
    """Runs BEFORE a module is imported/collected -- catches a collision
    left over from an earlier file's own top-level sys.path mistake
    before THIS file's own `import tools...` line gets a chance to raise
    a raw ModuleNotFoundError instead (the fixture above alone is too
    late for that specific failure shape, since fixtures only run for
    modules that already collected successfully).

    Uses pytest.exit(), never a bare raise: pytest_collectstart is called
    directly by _pytest.runner.collect_one_node with no surrounding
    try/except (unlike the collection call that follows it), so any other
    exception raised here propagates all the way out as an INTERNALERROR
    crash trace -- confirmed empirically, not assumed, when an earlier
    draft of this hook used a bare `raise RuntimeError(...)` and a full
    suite run crashed with exactly that INTERNALERROR instead of the
    intended attributable message. pytest.exit() is pytest's own sanctioned
    mechanism for stopping the session immediately with a clear reason,
    which is also the proportionate response here: once the collision has
    happened, every module collected after it is unreliable, so continuing
    to collect would just accumulate more confusing failures."""
    try:
        _assert_tools_package_is_repo_root()
    except RuntimeError as exc:
        pytest.exit(str(exc), returncode=1)


# Test-mode bypass for the _get_db() fail-loud guards added with the plugin
# bootstrap contract. Test fixtures monkey-patch DATA_DIR/DB_PATH to a tempdir
# and let the first tool handler's _get_db() open-and-create the DB — the
# fail-loud would block the legitimate test-fixture setup otherwise. Matches
# the ENGRAM_NO_EMBEDDINGS / ENGRAM_NO_POLARITY precedent. Set early so test
# files that import server at module load time see it.
os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")

# #2126 — engram_core.DATA_DIR is `Path(os.environ.get("ENGRAM_HOME") or
# "~/.engram")`, resolved ONCE at import time. On a developer clone that
# self-hosts its own ENGRAM data inside this very checkout (ENGRAM_HOME ==
# this repo), any test that reaches a git-writing code path
# (_init_git()/_git() in src/engram/engram_core.py) before rerouting DATA_DIR
# via server._configure_paths() silently commits into the AMBIENT REPO — the
# #2122 incident that #2124 only stopped from firing automatically, without
# fixing. Give every test process a throwaway ENGRAM_HOME by default
# (setdefault — a test's own explicit ENGRAM_HOME, set via monkeypatch or
# server._configure_paths(), still wins) so a fixture that forgets its own
# isolation lands in a scratch dir, never the checkout. Set at collection
# time, before any test module's own `import server` can run.
# Per-invocation private dir (0700, unique) rather than a shared named /tmp
# parent: a fixed uid-agnostic path is created 0755 by the FIRST uid to run the
# suite on a shared host, and every other uid then dies with PermissionError at
# conftest import — nothing collects, and the error names a /tmp path so it
# reads as a broken repo rather than a permissions collision (caught live by
# the merge seat on this PR's own review, reproduced cross-uid).
if "ENGRAM_HOME" not in os.environ:
    _FALLBACK_ENGRAM_HOME = tempfile.mkdtemp(
        prefix="engram-alpha-test-fallback-home-"
    )
    os.environ["ENGRAM_HOME"] = _FALLBACK_ENGRAM_HOME


@pytest.fixture(autouse=True)
def _stop_snapshot_worker():
    """Stop + reset the #1673 async snapshot worker after each test.

    engram_snapshot_worker runs a module-level singleton background thread that any
    test hitting the async _commit_snapshot path starts. Without this teardown the
    thread (and its module globals) leak across tests, polluting later ones (e.g. a
    thread still processing a torn-down tempdir's queue). Safe no-op when the module
    was never imported or no worker started."""
    yield
    mod = sys.modules.get("engram_snapshot_worker")
    if mod is not None:
        try:
            mod.stop_worker(timeout=2.0)
            # Reset singleton state so the next test starts clean.
            mod._started = False
            mod._worker_thread = None
            mod._seq_counter = 0
            mod._stop.clear()
            mod._work_event.clear()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _restore_context_tracker_in_sys_modules():
    """Restore sys.modules['context_tracker'] to its pre-test state after each test.

    Several session-start hook test helpers (_load_hook_module in
    test_session_start_hook_calibration.py, test_session_start_hook_auto_sleep_cron.py,
    and test_fairy_policy.py) install a MagicMock stub as sys.modules['context_tracker']
    so the hook's module-level `from context_tracker import write_baseline` succeeds,
    but they never restore the entry on teardown.

    A stale MagicMock in sys.modules['context_tracker'] poisons any later test that
    runs code containing `from context_tracker import estimate_usage, format_drowsiness`
    (e.g. the UserPromptSubmit hook's nap-warning path in engram-surface-hook.py):
    estimate_usage returns a truthy MagicMock, format_drowsiness returns another
    MagicMock, and that MagicMock lands in the `parts` list causing
    `TypeError: sequence item N: expected str instance, MagicMock found`.

    Root cause: test_surface_hook_mcp_liveness.py::TestMcpLivenessIntegration::
    test_main_emits_write_tool_warning_on_stale_marker fails in the full suite
    (G3a) but passes in isolation because the MagicMock stub from the session-start
    hook tests is present in sys.modules when the liveness test's main() runs.

    Fix: save the pre-test entry and restore it unconditionally on teardown.
    This is the correct scope — the stub is an implementation detail of each
    test, not a suite-wide assumption.
    """
    original = sys.modules.get("context_tracker")
    yield
    if original is None:
        sys.modules.pop("context_tracker", None)
    else:
        sys.modules["context_tracker"] = original


@pytest.fixture(autouse=True)
def mock_dns_resolution(request, monkeypatch):
    """Mock socket.getaddrinfo so tests don't depend on DNS infra.

    Tests that exercise the DNS-validation code path explicitly opt out via
    @pytest.mark.real_dns marker (test_url_validation.py's DNS-checking tests).

    Default behavior: fake hostnames with 'totally-fake-domain'
    in them raise gaierror (EAI_NONAME) to preserve fabricated-URL test coverage;
    everything else resolves to 127.0.0.1.
    """
    if request.node.get_closest_marker("real_dns"):
        return

    def fake_getaddrinfo(host, port=None, *args, **kwargs):
        if "totally-fake-domain" in host:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _no_real_git_config_leak(monkeypatch, tmp_path):
    """#2126: no `git` subprocess spawned by a test may read or write the real
    user's global/system git config.

    Every git-mutating fixture in this suite (test_engine_flows.py's
    _init_fake_git, test_backup_upstream.py's repo_pair, test_run_touched_tests.py's
    _make_tmp_repo, etc.) already sets its own LOCAL user.email/user.name via an
    explicit `cwd=` and inherits the rest of the process environment for the
    subprocess call. GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM pointed at nonexistent
    per-test paths make git behave as if neither the real global nor the real
    system config exists, regardless of what HOME resolves to — closing the
    read/write path without needing to touch HOME itself (several tests legitimately
    depend on the real HOME for unrelated path-expansion checks).
    """
    monkeypatch.setenv(
        "GIT_CONFIG_GLOBAL", str(tmp_path / "_2126_no_real_global_gitconfig")
    )
    monkeypatch.setenv(
        "GIT_CONFIG_SYSTEM", str(tmp_path / "_2126_no_real_system_gitconfig")
    )


# #2126: the exact identity-bearing / remote-bearing config keys the #2122
# incident described being corrupted ("core.bare flipped to true", "the remote
# URL rewritten to /nonexistent/no-such-repo.git", commits authored by
# `engram@local` / `test@test.com`). Deliberately NOT the full `git config
# --local --list` output: `branch.*` tracking entries live in the SAME shared
# .git as every other worktree/agent on this host, and legitimately churn from
# concurrent, unrelated git activity elsewhere in the repo while this suite is
# running (measured live: a `branch.kepler/*.merge` entry changed mid-run from
# an agent working in a different worktree, tripping a first draft of this
# guard that diffed the whole config list). Keying on the specific keys the
# incident named keeps the guard sensitive to the real defect without false
# positives from a shared, actively-multi-tenant .git.
_GUARDED_CONFIG_KEYS = (
    "core.bare",
    "user.email",
    "user.name",
    "remote.origin.url",
)


@pytest.fixture(scope="session", autouse=True)
def _ambient_repo_untouched_guard():
    """#2126 regression gate: the suite itself must never write into the repo it
    is invoked from — the exact defect that survived #2124's revert of the
    pre-push test-gate hook (#2122). Snapshot this checkout's identity-bearing
    git config, working-tree status, and HEAD before any test runs; assert
    byte-identical after the whole session. This is the mechanical version of
    "run the suite by hand in a real clone and check `git status` afterwards"
    that the #2122 incident depended on nobody doing.
    """

    def _snapshot():
        cfg = {}
        for key in _GUARDED_CONFIG_KEYS:
            r = subprocess.run(
                ["git", "-C", REPO_ROOT, "config", "--local", "--get", key],
                capture_output=True,
                text=True,
            )
            # returncode 1 == key unset (git's normal "not found" signal) —
            # record that as an explicit absence rather than folding it into
            # the same bucket as an empty string value.
            cfg[key] = r.stdout if r.returncode == 0 else None
        status = subprocess.run(
            ["git", "-C", REPO_ROOT, "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        head = subprocess.run(
            ["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        return (cfg, status.stdout, head.stdout)

    before = _snapshot()
    yield
    after = _snapshot()
    assert before == after, (
        "#2126 regression: the test suite mutated the ambient repo it was "
        f"invoked from.\nBefore: {before!r}\nAfter: {after!r}"
    )


def fresh_module(mod: types.ModuleType) -> types.ModuleType:
    """Return a freshly reloaded copy of a repo module, isolated for testing.

    R4 rationale (import-pattern contract v1.1): the caller passes the imported
    module **object** (e.g. ``fresh_module(engram_focus)``), not a string.  The
    import statement ``import engram_focus`` at the call site is the AST-visible
    dependency edge that ``run_touched_tests.py`` needs for precise selection.  A
    string-parameterised helper would be invisible to ``ast.walk`` and would
    reintroduce the opacity the contract removes.

    The purge covers ``server`` and all ``engram_*`` family modules so that
    cross-module state (e.g. ``engram_core.DATA_DIR`` shared between server and
    family modules) resets consistently.  After purge the module is re-imported
    by its ``__name__`` (the import machinery reloads all transitive deps too).

    For modules that expose ``_configure_paths`` (server-backed family modules),
    the caller is responsible for calling it with a temp dir *after* this
    function returns — ``fresh_module`` does not know which temp dir to use.
    See ``tests/IMPORT_RUBRIC.md`` §R4 for the full usage pattern.

    Args:
        mod: The imported module object whose family should be reloaded.

    Returns:
        The freshly imported module (same ``__name__`` as *mod*).
    """
    mod_name = mod.__name__
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import importlib
    return importlib.import_module(mod_name)
