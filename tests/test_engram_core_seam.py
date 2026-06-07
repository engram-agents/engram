"""Seam grep-gate for #872 wave 1 — spec D3.

Mechanical guards that prevent regressions of the engram_core.py extraction seam:

1. server.py must never use ``from engram_core import …`` — only the aliased
   form ``import engram_core as core`` is allowed.  Star-imports and named
   imports bypass the ``core.`` namespace and would silently re-bind mutable
   names on the server module, defeating the extraction.

2. The 14 mutable state names live exclusively in engram_core.  After the wave-1
   extraction, server.py has NO binding for them — the compat-layer block
   (located dynamically via its start/end sentinel comments) holds the
   ``import engram_core as core`` statement, set-once constant/class aliases
   (e.g. GIT_EXE, FEELING_NUDGE_TTL_TURNS), and call-time forwarder defs; the
   14 MUTABLE names are never assigned there.  Any bare word-boundary match
   outside that block is a seam regression.

Both tests read server.py from the filesystem — they are static-analysis checks
that fire without importing the module.
"""

import pathlib
import re

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# The 14 mutable state names extracted to engram_core.py.
# Mirror list from engram_core.py module docstring (HOUSE RULES section).
# If engram_core.py's docstring list changes, update this constant in sync.
# ---------------------------------------------------------------------------
MUTABLE_NAMES = [
    "DATA_DIR",
    "DB_PATH",
    "SNAPSHOT_PATH",
    "CONFIG_PATH",
    "LOG_PATH",
    "FEELING_NUDGE_MARKER",
    "ERROR_INCIDENTS_PATH",
    "_SANDBOX_ACTIVE",
    "_walguard_last_check",
    "_walguard_startup_done",
    "_walguard_disabled_logged",
    "_VEC_BACKEND_AVAILABLE",
    "GIT_TIMEOUT",
    "_git_available",
]

# ---------------------------------------------------------------------------
# Compat-layer block boundaries in server.py (1-indexed, inclusive), located
# DYNAMICALLY via the block's start/end sentinel comments so the boundary can
# never drift out of sync with the file (the hardcoded-line-number version was
# off by 4 within one review round of being written).  Any bare reference to a
# mutable name *inside* this range is expected and allowed — the compat block
# IS the seam.  Everything outside is the guard surface.  Missing sentinels
# fail loudly rather than silently guarding the wrong region.
# ---------------------------------------------------------------------------
_COMPAT_START_SENTINEL = "# ---- #872 wave-1 compat layer"
_COMPAT_END_SENTINEL = "# ---- end #872 wave-1 compat layer"


def _compat_bounds(lines):
    """Return (start_line, end_line) 1-indexed inclusive, or raise."""
    start = end = None
    for i, line in enumerate(lines, 1):
        if line.startswith(_COMPAT_START_SENTINEL) and start is None:
            start = i
        elif line.startswith(_COMPAT_END_SENTINEL):
            end = i
    if start is None or end is None or end <= start:
        raise AssertionError(
            "compat-layer sentinels missing or out of order in server.py "
            f"(start={start}, end={end}) — the seam gate cannot locate its "
            "allowed region; restore the sentinel comments."
        )
    return start, end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_server_py_no_from_engram_core_import():
    """server.py must use 'import engram_core as core', never 'from engram_core import'.

    The 'from … import' form binds names directly on the server module, bypassing
    the ``core.`` namespace prefix that the seam convention requires.  It would
    silently re-introduce module-level aliases for mutable names, allowing tests
    to reference them via the server module instead of via the live core object —
    exactly the regression this seam migration eliminates.
    """
    server_src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    # Match any variant: 'from engram_core import X', 'from engram_core import *', etc.
    pattern = re.compile(r'\bfrom\s+engram_core\s+import\b')
    matches = pattern.findall(server_src)
    assert not matches, (
        f"server.py contains forbidden 'from engram_core import' form(s); "
        f"only 'import engram_core as core' is allowed.\n"
        f"Occurrences found: {matches}"
    )


def test_server_py_no_bare_mutable_name_outside_compat_block():
    """server.py must not reference any of the 14 mutable core names outside the compat block.

    After the #872 wave-1 extraction, server.py has no binding for any of the 14
    mutable state names.  A bare word-boundary match outside the compat-layer block
    (sentinel-delimited; located dynamically) means:
      - a direct assignment (re-binding mutable state on the server module), OR
      - a read that would silently hit the unbound name and raise NameError at
        runtime — or, if a stale module-level 'import engram_core' exists somewhere,
        would point at the WRONG (possibly stale) module object after a purge.

    Exclusions:
      - Lines inside the compat block (COMPAT_START_LINE-COMPAT_END_LINE inclusive).
      - Pure comment lines (stripped line starts with '#').
      - Occurrences immediately preceded by 'core.' (legitimate routed references
        within the same line, e.g. ``core._VEC_BACKEND_AVAILABLE``).
    """

    server_path = REPO_ROOT / "server.py"
    lines = server_path.read_text(encoding="utf-8").splitlines()
    compat_start, compat_end = _compat_bounds(lines)

    violations = []
    for name in MUTABLE_NAMES:
        pattern = re.compile(r'\b' + re.escape(name) + r'\b')
        for i, line in enumerate(lines):
            lineno = i + 1  # 1-indexed for human-readable output
            # Skip inside the compat block
            if compat_start <= lineno <= compat_end:
                continue
            # Skip pure comment lines
            if line.strip().startswith('#'):
                continue
            for m in pattern.finditer(line):
                # Allow references that are immediately prefixed by 'core.'
                start = m.start()
                preceding = line[max(0, start - 5):start]
                if 'core.' in preceding:
                    continue
                violations.append((lineno, name, line.rstrip()))

    if violations:
        formatted = "\n".join(
            f"  line {lineno}: bare {name!r} — {src!r}"
            for lineno, name, src in violations
        )
        raise AssertionError(
            f"server.py has {len(violations)} bare mutable-name reference(s) "
            f"outside the compat block (lines {compat_start}–{compat_end}):\n"
            f"{formatted}\n\n"
            f"Fix: use 'core.{violations[0][1]}' or assign via the core object, "
            f"not a bare server-module binding."
        )


def test_engram_client_reset_survives_state_extraction(tmp_path):
    """EngramClient.reset() reads DATA_DIR through the live core namespace.

    Regression guard for the #872 wave-1 consumer gap: reset() read
    server.DATA_DIR (a mutable name with deliberately NO server-side binding
    after the extraction) and had zero test coverage — the suite was green
    while reset() would AttributeError on every call. This exercises the
    real path end-to-end: configure → write marker file → reset → dir
    recreated fresh.
    """
    import sys
    repo_root = __import__("pathlib").Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from engram_client import EngramClient

    client = EngramClient(db_dir=str(tmp_path / "reset-probe"))
    probe = client._server.core.DATA_DIR / "reset-probe-marker.txt"
    probe.write_text("pre-reset")
    assert probe.exists()

    client.reset()  # raises AttributeError without the core-namespace fix

    assert client._server.core.DATA_DIR.exists(), "data dir recreated"
    assert not probe.exists(), "old contents gone after reset"


def test_family_modules_no_from_engram_core_and_no_mutable_assignments():
    """Each engram_<family>.py module must honour two house rules.

    Rule A — no 'from engram_core import': family modules must access shared
    state ONLY via 'import engram_core as core; core.NAME', never via a naked
    'from engram_core import' that binds names locally and bypasses the live
    core namespace.

    Rule B — no module-level assignment of any MUTABLE_NAMES member: family
    modules must not re-bind mutable state at module scope (e.g.
    'DATA_DIR = ...'). All mutable state lives in engram_core; a module-level
    assignment in a family module would create a stale shadow.

    Covers all engram_<family>.py files currently in the repo root (glob
    pattern 'engram_*.py', excluding engram_core.py itself).
    """
    family_modules = sorted(REPO_ROOT.glob("engram_*.py"))
    # Exclude engram_core.py itself — it IS the canonical state home
    family_modules = [p for p in family_modules if p.name != "engram_core.py"]
    assert family_modules, "No engram_*.py family modules found — glob may need adjustment"

    # Rule A pattern
    from_core_pattern = re.compile(r'\bfrom\s+engram_core\s+import\b')
    # Rule B pattern — module-level assignment: line that assigns one of the mutable names
    # A module-level assignment looks like: NAME = ... (at column 0, not inside a def/class)
    # We detect with a simple heuristic: line starts with NAME followed by space/=
    mutable_assign_pattern = re.compile(
        r'^(' + '|'.join(re.escape(n) for n in MUTABLE_NAMES) + r')\s*='
    )

    rule_a_violations = []
    rule_b_violations = []

    for module_path in family_modules:
        src = module_path.read_text(encoding="utf-8")
        lines = src.splitlines()

        # Rule A — only actual import statements at column 0 (not docstring/comment mentions)
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            # Skip pure comment lines and lines that don't start with 'from'
            if stripped.startswith('#') or not stripped.startswith('from'):
                continue
            if from_core_pattern.search(line):
                rule_a_violations.append((module_path.name, i, line.rstrip()))

        # Rule B — module-level assignments (at column 0, before first def/class)
        # KNOWN BLINDSPOT (deliberate, heuristic-scope): `inside_block` latches True
        # at the FIRST col-0 def/class and never resets, so a col-0 mutable-name
        # assignment placed AFTER the first function definition is NOT caught.
        # Family modules conventionally declare constants before any def, so the
        # heuristic covers the intended shape; a full AST walk is the upgrade path
        # if a wave ever places state below the first def. "Not caught" here is a
        # scope choice, not an impossibility.
        inside_block = False  # track whether we're past a def/class boundary
        for i, line in enumerate(lines, 1):
            stripped = line.rstrip()
            # A def or class at col 0 starts a block; subsequent lines may be indented
            if re.match(r'^(def |class )', stripped):
                inside_block = True
            # Only check col-0 lines outside function/class bodies
            if not inside_block and mutable_assign_pattern.match(stripped):
                rule_b_violations.append((module_path.name, i, stripped))

    errors = []
    if rule_a_violations:
        formatted = "\n".join(
            f"  {fname} line {lineno}: {src!r}"
            for fname, lineno, src in rule_a_violations
        )
        errors.append(
            f"Rule A — 'from engram_core import' forbidden in family modules "
            f"({len(rule_a_violations)} occurrence(s)):\n{formatted}"
        )
    if rule_b_violations:
        formatted = "\n".join(
            f"  {fname} line {lineno}: {src!r}"
            for fname, lineno, src in rule_b_violations
        )
        errors.append(
            f"Rule B — module-level mutable-name assignment in family module "
            f"({len(rule_b_violations)} occurrence(s)):\n{formatted}"
        )

    assert not errors, "\n\n".join(errors)


def test_two_clients_two_dirs_isolated(tmp_path):
    """The multi-client isolation contract: constructing client #2 with a
    different db_dir must NOT redirect client #1's live path state.

    Regression guard for the #894 colleague-round blocker: EngramClient's
    purge-and-reimport dropped only 'server' from sys.modules, so a fresh
    server re-imported the CACHED engram_core and client #2's
    _configure_paths silently re-pointed client #1's paths (the shared-
    module trap, production instance #3). The purge now drops the
    server+core pair, giving each client its own module pair.
    """
    import sys
    repo_root = __import__("pathlib").Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from engram_client import EngramClient

    dir_a = tmp_path / "client-a"
    dir_b = tmp_path / "client-b"

    client_a = EngramClient(db_dir=str(dir_a))
    a_data_dir_before = client_a._server.core.DATA_DIR
    assert str(a_data_dir_before) == str(dir_a)

    client_b = EngramClient(db_dir=str(dir_b))  # must not touch A's core
    assert str(client_b._server.core.DATA_DIR) == str(dir_b)

    # The contract: A's module pair still points at A's dir.
    assert str(client_a._server.core.DATA_DIR) == str(dir_a), (
        "client #2's construction redirected client #1's live DATA_DIR — "
        "the server+engram_core pair is being shared across clients"
    )
    # And they are genuinely distinct module objects, not one shared core.
    assert client_a._server.core is not client_b._server.core
