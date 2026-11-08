"""Per-test memory attribution for the nightly shards (#4420). Stdlib only.

Loaded with ``-p nightly_rss_plugin`` and ``PYTHONPATH`` pointing at this directory.

It reads ``VmRSS`` from ``/proc/self/status`` at ``pytest_runtest_logstart`` and
``pytest_runtest_logfinish``. A test's delta is the rise since the previous reading,
so growth between tests (fixture teardown, module setup) lands on the test that
follows it. Any test whose delta is >= 20 MB prints one line:

    [rss-delta] <nodeid> +<MB> MB

At session end the top 30 tests by delta and the top 20 files by summed delta are
written, as markdown tables, to the file named by ``NIGHTLY_RSS_OUT`` (nothing is
written when it is unset or when no delta was measured).

Lines are printed only from the two hooks above, never from a gc callback. Every
hook is guarded: an unreadable ``/proc`` (macOS, a sandbox) makes the plugin a
silent no-op, and no failure inside it can fail a test.

A reading rule, written before the first run: RSS deltas show where memory was
first touched, not who holds it (the allocator rarely returns pages). Deltas
concentrated in a few files point at the retained-import class (fixable per file);
deltas spread evenly over hundreds of tests point at growth of the process itself
(coverage data, the interpreter), and the split is the durable answer.
"""

import os
import sys

THRESHOLD_KB = 20 * 1024
TOP_TESTS = 30
TOP_FILES = 20
STATUS_PATH = os.environ.get("NIGHTLY_RSS_STATUS_PATH", "/proc/self/status")

_state = {"prev": None, "tests": {}, "config": None}


def _reset():
    _state["prev"] = None
    _state["tests"] = {}
    _state["config"] = None


def _read_rss_kb():
    """Current VmRSS in kB, or None when it cannot be read. Never raises."""
    try:
        with open(STATUS_PATH, encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _emit(text):
    try:
        reporter = None
        cfg = _state.get("config")
        if cfg is not None:
            reporter = cfg.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(text)
        else:
            print(text)
            sys.stdout.flush()
    except Exception:
        pass


def _take(nodeid):
    """Take one reading, add its rise over the previous reading to ``nodeid``."""
    try:
        rss = _read_rss_kb()
        if rss is None:
            return 0
        prev = _state["prev"]
        _state["prev"] = rss
        if prev is None or nodeid is None:
            return 0
        delta = rss - prev
        if delta > 0:
            _state["tests"][nodeid] = _state["tests"].get(nodeid, 0) + delta
        return delta
    except Exception:
        return 0


def pytest_configure(config):
    try:
        _state["config"] = config
    except Exception:
        pass


def pytest_runtest_logstart(nodeid, location):
    try:
        _take(nodeid)
    except Exception:
        pass


def pytest_runtest_logfinish(nodeid, location):
    try:
        _take(nodeid)
        total = _state["tests"].get(nodeid, 0)
        # The line reports the test's whole delta (start gap + run), once, at finish.
        if total >= THRESHOLD_KB and nodeid is not None:
            _emit("[rss-delta] %s +%d MB" % (nodeid, round(total / 1024.0)))
    except Exception:
        pass


def _file_of(nodeid):
    return nodeid.split("::", 1)[0]


def _report():
    tests = sorted(_state["tests"].items(), key=lambda kv: (-kv[1], kv[0]))
    per_file = {}
    for nodeid, kb in _state["tests"].items():
        per_file[_file_of(nodeid)] = per_file.get(_file_of(nodeid), 0) + kb
    files = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    out = ["#### Top %d tests by RSS delta (MB)\n" % TOP_TESTS, "| test | MB |", "|---|---|"]
    for nodeid, kb in tests[:TOP_TESTS]:
        out.append("| `%s` | %.1f |" % (nodeid, kb / 1024.0))
    out += ["", "#### Top %d files by summed RSS delta (MB)\n" % TOP_FILES, "| file | MB |", "|---|---|"]
    for path, kb in files[:TOP_FILES]:
        out.append("| `%s` | %.1f |" % (path, kb / 1024.0))
    return "\n".join(out) + "\n"


def pytest_sessionfinish(session, exitstatus):
    try:
        path = os.environ.get("NIGHTLY_RSS_OUT")
        if not path or not _state["tests"]:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_report())
    except Exception:
        pass
