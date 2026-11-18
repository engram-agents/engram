"""Regression coverage for #4883 signal-safe status publication."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import signal
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "engram" / "tools" / "codex-umwelt-wake.py"

spec = importlib.util.spec_from_file_location("codex_umwelt_wake_signal_4883", MODULE_PATH)
assert spec and spec.loader
wake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wake
spec.loader.exec_module(wake)


@contextlib.contextmanager
def preserved_signal_handlers():
    """Always restore this test process's SIGTERM and SIGINT handlers."""
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextlib.contextmanager
def installed_signal_handlers(paths):
    """Install the watcher handlers and always restore this test process's ones."""
    wake._STOP_REQUESTED = False
    with preserved_signal_handlers():
        wake.install_signal_handlers(paths, "2026-11-18T00:00:00+00:00")
        yield


@pytest.mark.parametrize(
    "received_signals",
    [
        (signal.SIGTERM,),
        (signal.SIGINT,),
        (signal.SIGTERM, signal.SIGINT),
    ],
)
def test_signal_at_status_replace_only_requests_stop(
    tmp_path, monkeypatch, capsys, received_signals
):
    """A signal at replace cannot make a nested status write consume the temp file."""
    paths = wake.build_paths(tmp_path / "state")
    payload = {"mode": "delivered", "detail": "outer status write"}
    real_replace = wake.os.replace
    replacement_calls = 0

    with installed_signal_handlers(paths):
        def replace_at_boundary(source, destination):
            nonlocal replacement_calls
            replacement_calls += 1
            if replacement_calls == 1:
                # Status I/O remains available to the outer write, but neither a
                # read nor a write is permitted while the signal callback runs.
                with monkeypatch.context() as handler_io:
                    handler_io.setattr(
                        wake,
                        "current_status",
                        lambda *_args, **_kwargs: pytest.fail(
                            "signal handler must not read status"
                        ),
                    )
                    handler_io.setattr(
                        wake,
                        "update_status",
                        lambda *_args, **_kwargs: pytest.fail(
                            "signal handler must not write status"
                        ),
                    )
                    for signum in received_signals:
                        handler = signal.getsignal(signum)
                        assert callable(handler)
                        handler(signum, None)
            return real_replace(source, destination)

        monkeypatch.setattr(wake.os, "replace", replace_at_boundary)
        wake.update_status(paths, payload)

    assert wake._STOP_REQUESTED is True
    assert replacement_calls == 1
    assert json.loads(paths.status_path.read_text(encoding="utf-8")) == payload
    assert capsys.readouterr().err == ""


def test_cmd_run_final_status_stays_stopped_when_signals_repeat_at_replace(
    tmp_path, monkeypatch, capsys
):
    """Normal finalization owns the stopped status and removes its pidfile."""
    paths = wake.build_paths(tmp_path / "state")
    args = argparse.Namespace(
        once=False,
        state_dir=paths.state_dir,
        quiet_stdout=True,
        umwelt_config=tmp_path / "umwelt.json",
        app_server="unix:///unused.sock",
        sessions_dir=tmp_path / "sessions",
        thread_id=None,
        interval=1.0,
    )
    real_replace = wake.os.replace
    final_replacement_calls = 0

    class DummyMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    def prepare_pidfile(actual_paths, _logger):
        actual_paths.state_dir.mkdir(parents=True, exist_ok=True)
        actual_paths.pid_path.write_text(f"{wake.os.getpid()}\n", encoding="utf-8")

    def stop_after_cycle(**_kwargs):
        def replace_at_final_status(source, destination):
            nonlocal final_replacement_calls
            final_replacement_calls += 1
            if final_replacement_calls == 1:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handler = signal.getsignal(signum)
                    assert callable(handler)
                    handler(signum, None)
            return real_replace(source, destination)

        monkeypatch.setattr(wake.os, "replace", replace_at_final_status)
        wake._STOP_REQUESTED = True
        return {}

    monkeypatch.setattr(wake, "reap_prior", lambda *_args: None)
    monkeypatch.setattr(wake, "wait_for_acceptable_pidfile", prepare_pidfile)
    monkeypatch.setattr(wake, "CodexItemCompletedMonitor", DummyMonitor)
    monkeypatch.setattr(wake, "run_cycle", stop_after_cycle)

    with preserved_signal_handlers():
        assert wake.cmd_run(args) == 0

    assert wake._STOP_REQUESTED is True
    assert final_replacement_calls == 1
    assert not paths.pid_path.exists()
    stopped = json.loads(paths.status_path.read_text(encoding="utf-8"))
    assert stopped["mode"] == "stopped"
    assert stopped["running"] is False
    assert stopped["pid"] is None
    assert capsys.readouterr().err == ""
