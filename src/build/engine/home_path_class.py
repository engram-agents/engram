"""The absolute-home-path leak class: ONE definition, shared.

Public text carries no host paths, usernames or seat folder names.  The class is
an absolute home path, ``/home/<name>`` or ``/Users/<name>``, where ``<name>``
is one whole path segment: it starts with a letter, digit or underscore, runs
through letters, digits, ``_``, ``.`` and ``-``, and ends at any other character
or at end of string (so a path ending right after the name, one in double quotes,
one followed by a colon, and one followed by a sentence-ending dot are all the
class; trailing dots are stripped
from the segment before it is compared).  ``/Users/`` is matched
case-insensitively (macOS file systems usually are); ``/home/`` is not.  It is
default-deny: any segment NOT declared in
ALLOWED_HOME_SEGMENTS is a finding, so the check protects every seat, not a
list of known ones.

Two consumers import this file and nothing else defines the class:
  - ``scan-leaks.py`` (the repository scanner, every git-tracked text file);
  - ``build.py`` ``scan_for_leaks`` (the foreign-bundle VALIDATE step).

Bare names (a seat name with no path around it) are NOT this class; they belong
to the name-leak gate, which scans with a roster in CI.  Known limit: a
JSON-escaped path (``\\/home\\/name\\/``) is not matched.
"""

import re

# The class: /home/<seg> or /Users/<seg> (Users case-insensitive), <seg> one
# whole path segment, ended by any non-segment character or end of string.
_PREFIX = r"/(?:home|(?i:users))/"
_SEG_START = r"[A-Za-z0-9_]"
_SEG_REST = r"[A-Za-z0-9._-]*"
_SEG_END = r"(?![A-Za-z0-9_.-])"
HOME_PATH_RE = re.compile(_PREFIX + "(" + _SEG_START + _SEG_REST + ")" + _SEG_END)

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
    # Seat-home naming convention left open by a glob or placeholder (the part
    # before the star, dollar or angle bracket): a prefix, never a name.
    "agent-": "seat-home convention prefix before a glob or placeholder (agent-*, agent-$tag)",
    "agent-NAME": "explicit placeholder for a seat name",
    "agent-newborn-": "newborn seat-home prefix before a timestamp placeholder",
    "agent-newborn-XXX": "newborn seat-home with a placeholder suffix",
    # System and shared directories.
    "Shared": "macOS system folder under /Users, not a person",
    "agents-shared": (
        "shared coordination folder the multi-agent tools use as a default; "
        "whether it should stay a public default is a separate open decision"
    ),
}


def disallowed_home_path_regex() -> "re.Pattern[str]":
    """A single compiled regex matching exactly the disallowed home paths.

    Group 1 is the segment as written; callers strip trailing dots from it
    (``seg.rstrip(".")``) to get the name.  The allowed set is applied INSIDE
    the regex as a negative lookahead, so this is the one place the verdict is
    made and a caller that takes a list of compiled patterns (a hit means a
    leak) can use it directly.  The allowed name must be followed by optional
    trailing dots and the end of the segment, so an allowed name never allows
    a longer name that merely starts with it.
    """
    allowed = "|".join(re.escape(x) for x in sorted(ALLOWED_HOME_SEGMENTS))
    return re.compile(
        _PREFIX
        + r"(?!(?:" + allowed + r")\.*" + _SEG_END + ")"
        + "(" + _SEG_START + _SEG_REST + ")" + _SEG_END
    )
