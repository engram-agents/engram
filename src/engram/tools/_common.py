"""Shared helpers for inter-agent tooling — used by telegram_bot.py
and telegram_dispatcher.py.

Centralized here to avoid the helper-borrowing pattern (where one tool
references helpers defined in another tool) that produced the cross-post
mirror bugs on 2026-04-29 (commits 9db8dc6 → 0dad77a after three layered
fixes for missing _channel_dir, _now_utc_filename, etc.).

If any of these helper signatures changes, search the tools/ dir for
imports — every dependent file must update together.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Read env var with optional default + required-check."""
    v = os.environ.get(key, default)
    if required and not v:
        print(f"[tools/_common] ERROR: {key} env var is required", file=sys.stderr)
        sys.exit(2)
    return v


def engram_home() -> Path:
    return Path(env("ENGRAM_HOME", required=True)).expanduser()


def channel_dir() -> Path:
    return Path(env("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")).expanduser()


def agent_name() -> str:
    return env("AGENT_NAME", required=True)


def counterpart_name() -> str:
    return env("COUNTERPART_NAME", required=True)


def now_utc_filename() -> str:
    """UTC timestamp for filenames (lexically-sortable)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def now_utc_iso() -> str:
    """UTC timestamp for frontmatter (ISO-8601)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
