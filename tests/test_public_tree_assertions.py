"""Public-tree assertions: LICENSE + README disclaimer (release-day ruling, 2026-11-10).

Why this file exists. The release-cutover tool gated a public snapshot on two
facts: a LICENSE file carrying a Copyright line, and the README's
personal-project disclaimer. Under the public-main release model there is no
snapshot step, so those two assertions would otherwise run nowhere (they are
inside the tool that builds the snapshot, which is no longer on the release
path). They live here instead: they hold for any tree, they need no secret and
no Actions, and they run in the full suite — nightly, and the release run.

Contract (each cell has a mutation that kills it, and only it):

  - LICENSE absent                       -> test_license_present_with_copyright fails
  - LICENSE present, no Copyright line   -> test_license_present_with_copyright fails
  - README absent or without the phrase  -> test_readme_carries_personal_project_disclaimer fails

The two cells are independent: mutating one leaves the other green, which is
what makes each assertion discriminating rather than decorative.
"""

from __future__ import annotations

import re
from pathlib import Path

# A test file always sits at tests/, so the fixed window to the repo root is
# exact here (and this module reads nothing but the two files below).
REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact phrase release-cutover.py required in README (README_DISCLAIMER_PHRASE).
README_DISCLAIMER_PHRASE = "This is a personal project"

# Same shape release-cutover.py parsed: "Copyright <year> <name>".
LICENSE_COPYRIGHT_RE = re.compile(r"Copyright\s+\d{4}\s+\S")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_license_present_with_copyright() -> None:
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), f"LICENSE is missing at {license_path}"
    assert LICENSE_COPYRIGHT_RE.search(_read(license_path)), (
        "LICENSE carries no 'Copyright <year> <name>' line"
    )


def test_readme_carries_personal_project_disclaimer() -> None:
    readme_path = REPO_ROOT / "README.md"
    assert readme_path.is_file(), f"README.md is missing at {readme_path}"
    assert README_DISCLAIMER_PHRASE in _read(readme_path), (
        f"README.md does not contain the required disclaimer phrase "
        f"{README_DISCLAIMER_PHRASE!r}"
    )
