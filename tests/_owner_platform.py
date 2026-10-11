"""One shared skip marker for engramd-owner tests that need a RUNNING owner (#3292).

`EngramdOwner.start()` refuses on any seat that `engramd_owner.
platform_unsupported_reason()` reports as unsupported (#3194): not Linux, no
`setfacl` or `getfacl` on PATH, or no `socket.SO_PEERCRED` -- by design. A test that calls
`start()` (directly, or through a fixture that does), or that reads a
Linux-only API the predicate covers (e.g. `socket.SO_PEERCRED`), therefore
cannot pass on a Mac; it carries `requires_linux_owner` and SKIPS there with
a reason that quotes the owner's own refusal text.

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

_UNSUPPORTED_REASON = _eo.platform_unsupported_reason()

requires_linux_owner = pytest.mark.skipif(
    _UNSUPPORTED_REASON is not None,
    reason=f"engramd owner unsupported on this seat (#3194; see #3292): {_UNSUPPORTED_REASON}",
)
