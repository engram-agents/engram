"""Shared short-directory helper for tests that bind a REAL AF_UNIX socket (#3292).

macOS limits ``sun_path`` to 104 bytes (Linux: 108), and pytest's ``tmp_path``
on a Mac is long enough to exceed it, so ``sock.bind(str(tmp_path / ...))``
raises ``OSError: AF_UNIX path too long`` there. The managed-codex agentctl
tests therefore bind the fake daemon socket in a short ``/tmp/agc-*`` directory
and keep everything else (stubs, logs, cwd) under ``tmp_path``.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SOCKET_NAME = "app-server-control.sock"
# Margin under macOS's 104-byte sun_path (which counts the trailing NUL).
MAX_SOCKET_PATH_BYTES = 100


@contextmanager
def short_socket_path() -> Iterator[Path]:
    """Yield ``<short mkdtemp dir>/app-server-control.sock``; remove the dir on exit."""
    d = tempfile.mkdtemp(prefix="agc-", dir="/tmp")
    try:
        yield Path(d) / SOCKET_NAME
    finally:
        shutil.rmtree(d, ignore_errors=True)
