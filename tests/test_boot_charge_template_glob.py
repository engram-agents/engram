"""Regression guard for engram-alpha#1998.

Background: the 08-06 fix for the self-SIGTERM incident (#1933, forum #308)
broke the boot-charge text's literal quoting of each monitor's own script
path by turning `forum-updates-monitor.sh` into the glob form
`forum-updates-monitor*.sh` (same for `forum-mention-monitor.sh`) — bash
expands the glob to the identical file, so the charge behaves the same, but
the `*` breaks the CONTIGUOUS filename so it can no longer literal-match the
monitors' own `pgrep -f` reap/duplicate-detection pattern from a
charge-booted session's argv.

That fix was applied to the DEPLOYED copies under
`/usr/local/lib/agentctl/autostart/` only — never to this repo's SOURCE
templates in `deploy/autostart/`. Every redeploy from source therefore
silently reverted the protection (measured live 2026-08-07: both deployed
templates' mtimes matched to the second, from a scripted rewrite one day
after the fix was verified).

This test is the guard: it fails loudly if either SOURCE template ever again
carries the contiguous (non-globbed) filename, so the regression can't
return silently on the next template edit. Do NOT "clean up" the `*` in
those filenames if you see this test start failing after an edit to these
templates — the `*` is load-bearing, not a stray glob.

Verification command is Ariadne's own from #308/#1933, unchanged:
    grep -cE 'forum-(updates|mention)-monitor\\.sh' <template>
Must be 0 for both templates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = [
    _REPO_ROOT / "deploy" / "autostart" / "boot-charge-default.md",
    _REPO_ROOT / "deploy" / "autostart" / "boot-charge-quiet.md",
]

# The exact regression pattern: either monitor's script name written as a
# CONTIGUOUS filename (no `*` between "-monitor" and ".sh"). This is
# Ariadne's #308 verification command, ported to Python's re.
_UNGLOBBED_MONITOR_PATH = re.compile(r"forum-(?:updates|mention)-monitor\.sh")


@pytest.mark.parametrize("template_path", _TEMPLATES, ids=lambda p: p.name)
def test_boot_charge_template_exists(template_path: Path):
    assert template_path.is_file(), (
        f"expected boot-charge source template at {template_path} — "
        "if this moved, update the path list in this test too."
    )


@pytest.mark.parametrize("template_path", _TEMPLATES, ids=lambda p: p.name)
def test_boot_charge_template_has_no_unglobbed_monitor_path(template_path: Path):
    text = template_path.read_text(encoding="utf-8")
    matches = _UNGLOBBED_MONITOR_PATH.findall(text)
    assert not matches, (
        f"{template_path} contains the monitor script path(s) as a "
        f"CONTIGUOUS filename ({matches!r}) instead of the glob form "
        "(forum-updates-monitor*.sh / forum-mention-monitor*.sh). This is "
        "the exact condition that made every charge-booted session "
        "SIGTERM itself for four days (#1933, forum #308) — the `*` must "
        "stay. See engram-alpha#1998."
    )


@pytest.mark.parametrize("template_path", _TEMPLATES, ids=lambda p: p.name)
def test_boot_charge_template_still_references_globbed_monitor_paths(template_path: Path):
    """Guards against the fix silently regressing the OTHER way — e.g. the
    monitor-arm command being deleted entirely rather than globbed. Confirms
    both globbed forms are still present."""
    text = template_path.read_text(encoding="utf-8")
    assert "forum-updates-monitor*.sh" in text, (
        f"{template_path} is missing the globbed updates-monitor arm command entirely"
    )
    assert "forum-mention-monitor*.sh" in text, (
        f"{template_path} is missing the globbed mention-monitor arm command entirely"
    )


if __name__ == "__main__":
    import subprocess
    import sys

    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))


def test_boot_charge_forum_branch_carries_verbatim_arming_text():
    """#2019 slice B pin: the conditional wrapper must not drift the forum
    branch's arming instruction — the segment from the arming order through
    the pgrep-anchor caveat is safety-relevant (the #2028 self-match fix
    lives in it). The two charges' texts legitimately differ in phrasing
    (quiet carries token-spend framing), so this pins each file's own
    segment for presence + the safety-critical tokens, not cross-file
    identity; the pgrep pattern itself is byte-pinned across both files by
    test_pgrep_liveness_reap_patterns.py."""
    import re
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    start = "Then arm your UCS forum monitors"
    end = "reports a live monitor as dead."
    for name in ("boot-charge-default.md", "boot-charge-quiet.md"):
        text = (repo / "deploy" / "autostart" / name).read_text()
        m = re.search(re.escape(start) + r".*?" + re.escape(end), text, re.DOTALL)
        assert m, f"{name}: arming segment not found — forum branch drifted or wrapper broke it"
        seg = m.group(0)
        # Monitor names are glob-form post-#2090 (forum-updates-monitor*.sh)
        for token in ("forum-updates-monitor", "forum-mention-monitor",
                      "pgrep -u", "^bash"):
            assert token in seg, f"{name}: arming segment lost token: {token}"
        assert "If `forum`" in text and "If `umwelt`" in text, (
            f"{name}: the #2019 conditional frame is missing"
        )
