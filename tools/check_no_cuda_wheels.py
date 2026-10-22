#!/usr/bin/env python3
"""Fail if any CUDA wheel (nvidia-*, triton) is installed in the active env (#4004).

CPU torch is the repo rule (requirements/requirements-lock.txt header). CI install
steps install torch from the PyTorch CPU index first; this guard is the check
that the rule held, so a CUDA stack silently coming back fails the job instead of
costing ~3 GB of downloads per run.

Exit codes: 0 clean; 1 a CUDA distribution is installed; 2 `pip list` itself failed
(fail closed: an unreadable env is not a clean one).
"""

from __future__ import annotations

import re
import subprocess
import sys

# pip freeze prints the distribution name as installed; normalize `_`/`.` to `-`
# (PEP 503) before matching so an underscore spelling cannot slip through.
CUDA_RE = re.compile(r"^(nvidia-|triton==)", re.IGNORECASE)


def main() -> int:
    try:
        proc = subprocess.run(
            ["pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"::error::cannot run pip to check for CUDA wheels: {exc}")
        return 2
    if proc.returncode != 0:
        print(f"::error::`pip list` failed (exit {proc.returncode}): {proc.stderr.strip()}")
        return 2
    hits = [
        ln
        for ln in proc.stdout.splitlines()
        if CUDA_RE.match(re.sub(r"[-_.]+", "-", ln))
    ]
    if hits:
        print("\n".join(hits))
        print(
            "::error::CUDA wheels installed; CPU torch expected "
            "(see requirements-lock.txt header, #4004)"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
