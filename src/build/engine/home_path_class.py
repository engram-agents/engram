"""The absolute-home-path leak class: ONE definition, shared.

Public text carries no host paths, usernames or seat folder names.  The class is
an absolute home path, ``/home/<name>/`` or ``/Users/<name>/``, where ``<name>``
is one whole path segment.  It is default-deny: any segment NOT declared in
ALLOWED_HOME_SEGMENTS is a finding, so the check protects every seat, not a
list of known ones.

Two consumers import this file and nothing else defines the class:
  - ``scan-leaks.py`` (the repository scanner, every git-tracked text file);
  - ``build.py`` ``scan_for_leaks`` (the foreign-bundle VALIDATE step).

Bare names (a seat name with no path around it) are NOT this class; they belong
to the name-leak gate, which scans with a roster in CI.
"""

import re

# The class: /home/<seg>/ or /Users/<seg>/, <seg> one whole path segment.
HOME_PATH_RE = re.compile(r"/(?:home|Users)/([A-Za-z][A-Za-z0-9._-]*)/")

# A segment is allowed only if declared here, each with a one-line reason.
ALLOWED_HOME_SEGMENTS = {
    # Obviously fictional placeholders.
    "someone": "fictional placeholder user",
    "someone01": "fictional placeholder user, numbered",
    "user": "generic placeholder user",
    "you": "placeholder addressing the reader",
    "x": "single-letter placeholder",
    "alice": "fictional placeholder user",
    "bob": "fictional placeholder user",
    "test": "generic test-fixture user",
    "testuser": "generic test-fixture user",
    "example-user": "fictional placeholder user",
    "dev": "generic placeholder user",
    "agent": "generic placeholder seat user",
    "g": "single-letter placeholder",
    "zzz": "obviously fictional filler",
    "other": "generic placeholder for another user",
    "seat": "generic placeholder seat user",
    "PLACEHOLDER_USER": "explicit placeholder token",
    "agent-alice": "fictional placeholder seat",
    "agent-bob": "fictional placeholder seat",
    "agent-chosenone": "fictional placeholder seat",
    # System and shared directories.
    "Shared": "macOS system folder under /Users, not a person",
    "agents-shared": (
        "shared coordination folder the multi-agent tools use as a default; "
        "whether it should stay a public default is a separate open decision"
    ),
}


def disallowed_home_path_regex() -> "re.Pattern[str]":
    """A single compiled regex matching exactly the disallowed home paths.

    Same class and same allowed set as HOME_PATH_RE + ALLOWED_HOME_SEGMENTS,
    expressed as a negative lookahead so callers that take a list of compiled
    patterns (a hit means a leak) can use it.  The lookahead includes the
    trailing slash, so an allowed name never allows a longer name that merely
    starts with it.
    """
    allowed = "|".join(re.escape(s) for s in sorted(ALLOWED_HOME_SEGMENTS))
    return re.compile(
        r"/(?:home|Users)/(?!(?:" + allowed + r")/)[A-Za-z][A-Za-z0-9._-]*/"
    )
