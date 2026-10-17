"""The walguard remedy text must not hard-code the default ENGRAM home [part of #3863].

`engram_walguard` is imported by engram_core, so it cannot import engram_core's
DATA_DIR (cycle), and the remedy is a module-level constant list rendered
before any db path is known. The steps are shell commands an operator pastes,
so they spell the home as the shell form `${ENGRAM_HOME:-$HOME/.engram}`,
which the SHELL resolves against the operator's real environment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

import engram_walguard

_BARE = re.compile(r"(?:~|\$HOME|\$\{HOME\})/\.engram(?![\w-])")
_ENV_FORM = re.compile(r'"\$\{ENGRAM_HOME:-\$HOME/\.engram\}(/[\w.-]+)"')


def _surfaces():
    return list(engram_walguard.recovery_block()["steps"]) + [
        engram_walguard.recovery_banner_text()
    ]


def test_remedy_text_has_no_bare_default_home():
    for text in _surfaces():
        stripped = text.replace("${ENGRAM_HOME:-$HOME/.engram}", "")
        assert not _BARE.search(stripped), f"hard-coded home in remedy text: {text!r}"


def test_remedy_names_the_pidfile_and_db_through_env_form():
    joined = "\n".join(engram_walguard.recovery_block()["steps"])
    assert '"${ENGRAM_HOME:-$HOME/.engram}/recall-daemon.pid"' in joined
    assert '"${ENGRAM_HOME:-$HOME/.engram}/knowledge.db"' in joined


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_env_form_resolves_to_the_custom_home_when_set(tmp_path):
    joined = "\n".join(engram_walguard.recovery_block()["steps"])
    tokens = _ENV_FORM.findall(joined)
    assert sorted(tokens) == ["/knowledge.db", "/recall-daemon.pid"]
    for suffix in tokens:
        cmd = f'echo "${{ENGRAM_HOME:-$HOME/.engram}}{suffix}"'
        env = {"PATH": os.environ["PATH"], "HOME": "/h", "ENGRAM_HOME": str(tmp_path)}
        out = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True)
        assert out.stdout.strip() == f"{tmp_path}{suffix}"
        env.pop("ENGRAM_HOME")
        out = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True)
        assert out.stdout.strip() == f"/h/.engram{suffix}"
