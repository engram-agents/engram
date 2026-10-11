"""One shared skip marker for engramd-owner tests that need a RUNNING owner (#3292).

`EngramdOwner.start()` refuses on any seat that `engramd_owner.
platform_unsupported_reason()` reports as unsupported (#3194): on Linux, no
`setfacl` or `getfacl` on PATH (#3698) or no `socket.SO_PEERCRED`; on Darwin (#3666), no
`socket.LOCAL_PEERCRED`, `/bin/chmod` or `/dev/fd`; any other platform -- by
design. A test that calls `start()` (directly, or through a fixture that does)
therefore cannot pass on a seat the predicate refuses; it carries
`requires_supported_owner` (also exported under its older name,
`requires_linux_owner`, which since #3666 no longer means "Linux": a real Mac
passes the predicate and runs these tests) and SKIPS there with a reason that
quotes the owner's own refusal text.

`requires_linux_kernel` is a different marker with a different job: it is for
a test that names a LINUX-ONLY API (`/proc`, `socket.SO_PEERCRED`, `setfacl`,
`getfacl`) and so cannot pass on a Mac that the owner predicate supports. It
keys on `sys.platform`, NOT on the owner predicate -- the two answer different
questions (is the owner supported here / does this test's own mechanism exist
here).

The marker uses the owner's OWN predicate -- the very function
`_check_platform_support()` raises from -- evaluated once, at import
(collection) time. Skip and refusal therefore share one definition and cannot
disagree. That is sufficient because the seat does not change within a run; a
simulation of another seat must patch `platform.system` / `shutil.which` /
`socket.SO_PEERCRED` before collection. Tests that do not need a running owner
(pure helpers, constants, and the refusal tests themselves) must NOT carry this
marker: they run on every platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as _eo  # noqa: E402


def skip_marker_for(reason: str | None):
    """The skip marker for a given refusal text (None = supported = no skip).
    The module-level marker below is built by this, so a test can build the
    marker a hypothetical host would get from the owner's own refusal text."""
    return pytest.mark.skipif(
        reason is not None,
        reason=f"engramd owner unsupported on this seat (#3194; see #3292): {reason}",
    )


_UNSUPPORTED_REASON = _eo.platform_unsupported_reason()

requires_supported_owner = skip_marker_for(_UNSUPPORTED_REASON)
# Older name, kept so the 14 marked files do not churn (#3666): same object.
requires_linux_owner = requires_supported_owner

# For tests that name a Linux-only API (/proc, SO_PEERCRED, setfacl): skip off
# Linux whatever the owner predicate says (#3666).
requires_linux_kernel = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="uses a Linux-only API (/proc, SO_PEERCRED, setfacl/getfacl): not available on this kernel (#3666)",
)
