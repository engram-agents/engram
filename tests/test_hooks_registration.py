"""
Hook-registration gate (issue #1059).

Every script referenced in hooks/hooks.json must exist in the source tree.
Catches: deleting a hook file without removing it from hooks.json.

Scope: source-tree check (hooks/claude/ directory against hooks/hooks.json).
The build process flattens hooks/claude/ into the plugin's hooks/ root, so
hooks.json's `${CLAUDE_PLUGIN_ROOT}/hooks/FILENAME` maps to
`src/engram/hooks/claude/FILENAME` in the source.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS_JSON = REPO_ROOT / "src/engram/hooks/hooks.json"
HOOKS_SOURCE_DIR = REPO_ROOT / "src/engram/hooks/claude"


def _referenced_filenames():
    """Return all script filenames referenced in hooks.json."""
    text = HOOKS_JSON.read_text()
    return set(
        re.findall(
            r'\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([a-zA-Z0-9_.-]+\.(?:py|sh))',
            text,
        )
    )


def test_hooks_json_exists():
    assert HOOKS_JSON.exists(), f"hooks.json not found at {HOOKS_JSON}"


def test_hooks_source_dir_exists():
    assert HOOKS_SOURCE_DIR.is_dir(), f"hooks source dir not found at {HOOKS_SOURCE_DIR}"


def test_no_dangling_hook_registration():
    """Every script referenced in hooks.json must exist in hooks/claude/."""
    referenced = _referenced_filenames()
    assert referenced, "No hook scripts found in hooks.json — check the regex"

    dangling = [f for f in sorted(referenced) if not (HOOKS_SOURCE_DIR / f).exists()]
    assert not dangling, (
        f"hooks.json references {len(dangling)} script(s) that don't exist in "
        f"src/engram/hooks/claude/:\n"
        + "\n".join(f"  {f}" for f in dangling)
    )


def test_hooks_json_is_valid_json():
    """hooks.json must be parseable JSON."""
    try:
        with open(HOOKS_JSON) as fh:
            json.load(fh)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"hooks.json is not valid JSON: {exc}") from exc
