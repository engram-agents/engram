"""A pytest plugin that leaks one non-daemon thread into the session, as an earlier test in a
whole-suite run does (#4163 round 6). Loaded with `-p _a6_plant_thread_plugin` by
test_a6_isolated_helper_4163.py; it is not a test module and defines no tests."""
from __future__ import annotations

import threading

_RELEASE = threading.Event()
_THREAD = threading.Thread(target=_RELEASE.wait, name="planted-stranger", daemon=False)


def pytest_sessionstart(session):
    _THREAD.start()


def pytest_unconfigure(config):
    _RELEASE.set()
    if _THREAD.is_alive():
        _THREAD.join()
