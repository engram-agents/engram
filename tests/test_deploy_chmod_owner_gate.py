"""Guard test: deploy.sh must owner-gate the shared-bin chmod calls (#494).

Simulating a non-owner chmod failure in CI requires root, which we don't have.
Instead this test asserts the structural invariant: both shared-bin chmod sites
use the -O owner guard, not a bare chmod that would abort a non-owner deploy
under set -e.

Root cause (issue #494): the shared CLIs are group-writable, so [[ -w ]] passes
for a group member (cp succeeds) but chmod needs ownership — bare chmod fails
with "Operation not permitted" under set -e, leaving a partial-state deploy
(code applied, marker stale).

Fix: [[ -O "$FILE" ]] && chmod ...  — skips chmod for non-owners where it is
both impossible and unnecessary (cp already refreshed content).

This grep-based test catches any regression that removes the -O gate and reverts
to a bare chmod.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_SH = REPO_ROOT / "tools" / "deploy.sh"


def _read_deploy_sh() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def test_ia_chmod_is_owner_gated() -> None:
    """The ia shared-bin chmod must be owner-gated with [[ -O ... ]].

    Bare `chmod 0755 "$SHARED_IA"` without the -O guard aborts non-owner
    deploys under set -e (issue #494).
    """
    content = _read_deploy_sh()
    # The owner-gated form: [[ -O "$SHARED_IA" ]] && chmod 0755 "$SHARED_IA"
    assert re.search(r'\[\[\s*-O\s+"\$SHARED_IA"\s*\]\]\s*&&\s*chmod\b', content), (
        "deploy.sh ia chmod must be owner-gated: "
        '[[ -O "$SHARED_IA" ]] && chmod 0755 "$SHARED_IA". '
        "Bare chmod aborts non-owner deploys under set -e (issue #494)."
    )


def test_baton_chmod_is_owner_gated() -> None:
    """The baton shared-bin chmod must be owner-gated with [[ -O ... ]].

    Bare `chmod 0755 "$SHARED_BATON"` without the -O guard aborts non-owner
    deploys under set -e (issue #494).
    """
    content = _read_deploy_sh()
    # The owner-gated form: [[ -O "$SHARED_BATON" ]] && chmod 0755 "$SHARED_BATON"
    assert re.search(r'\[\[\s*-O\s+"\$SHARED_BATON"\s*\]\]\s*&&\s*chmod\b', content), (
        "deploy.sh baton chmod must be owner-gated: "
        '[[ -O "$SHARED_BATON" ]] && chmod 0755 "$SHARED_BATON". '
        "Bare chmod aborts non-owner deploys under set -e (issue #494)."
    )


def test_no_bare_shared_ia_chmod() -> None:
    """No bare (non-owner-gated) chmod targeting $SHARED_IA must exist.

    A bare chmod line that isn't on the same line as the -O guard would
    slip past the positive assertions above. This negative check catches
    a regression where someone adds a second unconditional chmod.

    Lines inside echo strings (the not-writable operator-hint branch) are
    excluded — those aren't executed chmod calls.
    """
    content = _read_deploy_sh()
    # Find executable chmod lines referencing $SHARED_IA — skip echo lines.
    chmod_lines = [
        line.strip()
        for line in content.splitlines()
        if "chmod" in line and "$SHARED_IA" in line
        and not line.strip().startswith("echo ")
    ]
    for line in chmod_lines:
        assert "-O" in line, (
            f"Bare (non-owner-gated) chmod on $SHARED_IA found: {line!r}. "
            "All chmod calls on shared CLIs must be owner-gated (issue #494)."
        )


def test_no_bare_shared_baton_chmod() -> None:
    """No bare (non-owner-gated) chmod targeting $SHARED_BATON must exist.

    Negative counterpart of test_baton_chmod_is_owner_gated. Echo lines
    (the not-writable operator-hint branch) are excluded.
    """
    content = _read_deploy_sh()
    chmod_lines = [
        line.strip()
        for line in content.splitlines()
        if "chmod" in line and "$SHARED_BATON" in line
        and not line.strip().startswith("echo ")
    ]
    for line in chmod_lines:
        assert "-O" in line, (
            f"Bare (non-owner-gated) chmod on $SHARED_BATON found: {line!r}. "
            "All chmod calls on shared CLIs must be owner-gated (issue #494)."
        )
