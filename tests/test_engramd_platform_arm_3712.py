"""One platform discriminator for engramd (#3712, part of #2652).

Before: engramd_client chose the AF_UNIX path limit by `sys.platform`, a second
discriminator beside the owner's `_owner_arm()` (`platform.system()`). Now
`engramd_client.platform_arm()` -- keyed on `platform.system()` ALONE -- is the
single source: the client's SOCKET_PATH_MAX_BYTES is computed from it, and the
owner's `_owner_arm()` delegates to it (owner imports client, never the reverse).

Cell map: (a) both functions agree per patched platform.system; (a2) the owner
FOLLOWS the client (patch `ec.platform_arm`, `_owner_arm` moves -- the cell that
tells delegation from a local re-implementation); (r) rider: the import-time
constant follows the arm, shown by patching `platform.system` before a reload.
"""

from __future__ import annotations

import importlib
import platform
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402


@pytest.mark.parametrize(
    "system,arm", [("Darwin", "darwin"), ("Linux", "linux"), ("FreeBSD", None)]
)
def test_a_client_and_owner_agree_on_arm(monkeypatch, system, arm):
    monkeypatch.setattr(platform, "system", lambda: system)
    assert ec.platform_arm() == arm
    assert eo._owner_arm() == arm


def test_a2_owner_follows_the_client(monkeypatch):
    # platform.system says Linux; only the client's helper says darwin. A
    # delegating owner follows the client; a local re-implementation would not.
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(ec, "platform_arm", lambda: "darwin")
    assert eo._owner_arm() == "darwin"


@pytest.mark.parametrize("system,limit", [("Darwin", 103), ("Linux", 107)])
def test_r_import_time_constant_follows_the_arm(system, limit):
    # reload() re-executes the module IN PLACE (the owner's `ec` is the same
    # object), so the constant is patched for the owner too until restored.
    # The patch is undone BEFORE the restoring reload, in `finally`, so a
    # failing assert cannot leak a patched constant into later tests.
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(platform, "system", lambda: system)
            importlib.reload(ec)
            assert ec.SOCKET_PATH_MAX_BYTES == limit
    finally:
        importlib.reload(ec)
