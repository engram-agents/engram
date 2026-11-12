"""Create-if-absent for the forum's two private data files (#4499).

forum.db and forum-audit.jsonl used to be created by whichever code path touched them
first (the boot-verify probe, ``main()``, ``create_app``, the audit writer), each under
the process umask, so each came out 0644 or, under the old ``UMask=002``, group-writable.
Every creator now calls THIS helper first, so the mode is decided in one place.
"""

from __future__ import annotations

import os

PRIVATE_FILE_MODE = 0o640


def create_if_absent(path: str, mode: int = PRIVATE_FILE_MODE) -> bool:
    """Create *path* empty with *mode* if nothing is there. Return True iff we created it.

    - ``O_CREAT | O_EXCL``: never opens an existing file and never creates THROUGH a
      symlink (a dangling link is "something there", so ``FileExistsError``).
    - An EXISTING file or link is left exactly as it is, mode included: this never
      chmods. Changing the modes of data that is already live is the installer's job.
    - Losing a race (the file appears between the check and the create) is not an
      error: the other creator's file stands.
    - Any other ``OSError`` (missing or unwritable parent) propagates; nothing is created.

    The kernel applies the umask to *mode*, which can only narrow it (never 0o640 -> wider).
    """
    if os.path.lexists(path):
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    os.close(fd)
    return True
