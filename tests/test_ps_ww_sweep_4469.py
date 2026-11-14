"""engram-agents/engram#4469: the remaining `ps -o command=` / `args=` sites carry `-ww`.

procps `ps` cuts `command=`/`args=` to 80 columns when stdout is a pipe (pytest-xdist
exports COLUMNS=80) unless `-ww` is given; #4248 fixed three sites, this pins six more.

What these cells are: source-text pins, so they go red on ANY host the moment `-ww` is
removed. They do NOT prove a long argv is read whole: BSD `ps` (macOS) already returns the
full width off a tty, so a behavioural width cell cannot discriminate there. Each pin
isolates the one `ps` call at its site, so a `-ww` elsewhere in the file cannot satisfy it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# (id, path, regex capturing the whole ps invocation at the site)
_SITES = [
    (
        "start-engram-daemon.sh",
        "src/engram/hooks/claude/start-engram-daemon.sh",
        r"subprocess\.run\(\[\s*'ps'[^\]]*'command='[^\]]*\]",
    ),
    (
        "test_engramd_a6_latency",
        "tests/test_engramd_a6_latency.py",
        r'_sp\.run\(\[\s*"ps"[^\]]*"command="[^\]]*\]',
    ),
    (
        "test_engramd_stage_profile_3678",
        "tests/test_engramd_stage_profile_3678.py",
        r'_sp\.run\(\[\s*"ps"[^\]]*"command="[^\]]*\]',
    ),
    (
        "tools/engramd_a6_latency.py",
        "tools/engramd_a6_latency.py",
        r'subprocess\.run\(\[\s*"ps"[^\]]*"command="[^\]]*\]',
    ),
    (
        "engram-health-exam SKILL.md",
        "src/engram/skills/claude/engram-health-exam/SKILL.md",
        r'ps [^\n]*args=[^\n]*',
    ),
    (
        "engram-upgrade SKILL.md",
        "src/engram/skills/claude/engram-upgrade/SKILL.md",
        r'ARGV=\$\(ps [^)]*command=[^)]*\)',
    ),
]


@pytest.mark.parametrize("site_id,rel,pattern", _SITES, ids=[s[0] for s in _SITES])
def test_ps_command_call_carries_ww_4469(site_id, rel, pattern):
    text = (_ROOT / rel).read_text()
    calls = re.findall(pattern, text)
    assert len(calls) == 1, f"{site_id}: expected exactly one ps command= call, found {calls!r}"
    call = calls[0]
    # `-ww` as its own flag (works on procps and BSD ps), not folded into other options.
    assert re.search(r"""(?<![\w-])["']?-ww["']?(?![\w-])""", call), f"{site_id}: no -ww in {call!r}"
