#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect speaker identity from name-prefix convention.

Uses sticky session context: a name prefix like "<Name>:" switches
the active user, and all subsequent messages are attributed to that user
until a different prefix overrides it. The marker file (current_user.json)
is cleared on SessionStart so identity doesn't leak across sessions.

Convention: type "Alex: <message>" once to switch context to Alex.
All following messages are treated as Alex's until another "<Name>: ..."
prefix resets.

Primary-user suppression: if $ENGRAM_HOME/config.json has a `primary_user` key,
the sticky context line is suppressed when the active user matches (since
the primary speaker is the default — no need to re-announce them on every
prompt). Set via the first-session skill or by hand editing config.json.

Owner read (U4d, #3197, behind a flag, default OFF): with
`{"engramd": {"user_identity_reads_via_owner": true}}` in
$ENGRAM_HOME/config.json the person-node read goes through the engramd owner
(`engramd_client.read("read", ...)`) instead of this hook opening knowledge.db
itself. Flag ON never falls back to the store and never reads an outage as
"zero people": an owner that is down or refusing yields a loud
additionalContext block instead (docs/2652-engrammanager-design.md SS5.1, SS5.5).
So does a client module that cannot be imported.

Upgrade note: after an upgrade, RESTART the engramd owner BEFORE turning the flag
on. This hook speaks contract 1.1 (the `read` method); a still-running 1.0 owner
passes the MINOR-version check but answers `unknown_operation`, which this hook
reports loudly as a refusal (never as "zero people") -- correct, but avoidable.

Exit codes:
  0 - success (may output empty JSON if no identity detected)
  1 - non-blocking error
"""

import json
import os
import re
import sqlite3
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import atomic_write_json, emit_context
# #3628: the config / sticky-marker reads are non-blocking and regular-file-only.
from _hooklib import open_regular_text
from _harness import detect_harness, get_harness_budgets

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")

# The ONE copy of this hook's person-node statement. Both paths use it: the old
# path executes it, the owner path sends it verbatim as the `read` literal
# (U4d D1) -- so the U4c scanner's registered template and what the client sends
# cannot drift apart. A single adjacent-literal constant, which the scanner reads
# as a `module_literal`.
_PERSON_NODES_SQL = (
    "SELECT id, claim, logical_chain, metadata FROM nodes "
    "WHERE type = 'person' AND is_current = 1"
)
_PERSON_NODES_COLUMNS = ("id", "claim", "logical_chain", "metadata")

# Bound for the owner call. This hook is registered with a 3 s timeout
# (hooks/hooks.json); interpreter start, the _hooklib/_harness imports and the
# render sit inside that too, so the owner call gets half of it: 1.5 s leaves
# 1.5 s of margin. The call is end-to-end (the client re-arms the socket timeout
# before every blocking op), so a trickling peer cannot stretch it.
OWNER_READ_TIMEOUT_S = 1.5


def get_primary_user() -> str:
    """Read primary_user from $ENGRAM_HOME/config.json (lowercased; empty if unset)."""
    try:
        with open_regular_text(CONFIG_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return (config.get("primary_user") or "").lower()


def get_person_nodes() -> list[dict]:
    """Read all current person nodes from the ENGRAM database."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_PERSON_NODES_SQL).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _owner_reads_enabled() -> bool:
    """The U4d flag: `engramd.user_identity_reads_via_owner` in
    $ENGRAM_HOME/config.json. ONE source, no env override. Anything but the
    boolean `true` (absent file, absent key, a string, malformed JSON) is OFF,
    which is the old path. After an upgrade, restart the owner BEFORE turning
    this on (a running 1.0 owner answers a 1.1 client `unknown_operation`)."""
    try:
        with open_regular_text(CONFIG_PATH) as f:
            config = json.load(f)
        return config["engramd"]["user_identity_reads_via_owner"] is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _import_engramd_client():
    """Lazy import (flag OFF must never import it), resolved layout-agnostically
    via the shared resolver -- plugin root first, then a walk-parents search for
    the file; never a fixed parents[N] (project CLAUDE.md, "Plugin build
    restructures code paths")."""
    from _hooklib import resolve_runtime_dir

    runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engramd_client.py")
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    try:
        import engramd_client
    except ImportError as exc:
        # Carry the path searched to the loud block in read_people_via_owner.
        raise ImportError(
            f"engramd_client.py was not found or could not be imported "
            f"(searched: {runtime_dir}; {type(exc).__name__}: {exc})"
        ) from exc

    return engramd_client


def _lines(title: str, items) -> str:
    return "  " + title + ":\n" + "\n".join(f"    - {i}" for i in items)


def read_people_via_owner() -> tuple[list[dict] | None, dict | None]:
    """The flag-ON read. Returns (people, None) on a real answer, or
    (None, response) where `response` is the loud hook output for an owner that
    is unavailable, refusing, or answering with something unusable. NEVER opens
    knowledge.db and never returns an empty list for a failure."""
    try:
        ec = _import_engramd_client()
    except ImportError as exc:
        # Design 5.5: an unimportable client is as loud as an unreachable owner.
        # Never open the store, never `{}`, never "zero people".
        head = (
            "[ENGRAM UNAVAILABLE: the engramd client module could not be loaded, so NO "
            "identity context was loaded. This is not the same as no people being known.]"
        )
        body = [
            f"  Cause: {exc}",
            _lines("Agent steps", [
                "Do NOT read knowledge.db directly to work around this; tell the human what is shown here.",
                "Do not retry in a loop: the module is missing from the install, so a retry cannot succeed.",
            ]),
            _lines("Human steps", [
                "Check that engramd_client.py exists at the plugin root (the directory above hooks/); "
                "re-run the plugin install or upgrade if it is missing.",
                "To go back to the direct-read path meanwhile, set "
                "engramd.user_identity_reads_via_owner to false in $ENGRAM_HOME/config.json.",
            ]),
        ]
        return None, _assemble_response(head, body)
    try:
        result = ec.read(
            "read",
            {"sql": _PERSON_NODES_SQL, "args": []},
            timeout=OWNER_READ_TIMEOUT_S,
        )
    except Exception as exc:
        # The client documents transport failures as Unavailable but does not
        # guarantee it never raises (e.g. a ValueError out of json.loads). As
        # loud as every other failure: never a traceback with empty stdout,
        # never the store, never "zero people".
        head = (
            "[ENGRAM UNAVAILABLE: the engramd client failed while reading identity context, "
            "so NO identity context was loaded. This is not the same as no people being known.]"
        )
        body = [
            f"  Cause: {type(exc).__name__}: {exc}"[:300],
            _lines("Agent steps", [
                "Do NOT read knowledge.db directly to work around this; tell the human what is shown here.",
                "Do not retry in a loop: the client raised rather than reporting the owner down.",
            ]),
            _lines("Human steps", [
                "This is a client-side bug: report it with the Cause line above.",
                "To go back to the direct-read path meanwhile, set "
                "engramd.user_identity_reads_via_owner to false in $ENGRAM_HOME/config.json.",
            ]),
        ]
        return None, _assemble_response(head, body)
    if isinstance(result, ec.Unavailable):
        head = (
            "[ENGRAM UNAVAILABLE: the engramd owner did not answer the user-identity "
            "read, so NO identity context was loaded. This is not the same as no people "
            "being known.]"
        )
        body = [
            f"  Socket: {result.socket_path} (bound {OWNER_READ_TIMEOUT_S}s)",
            _lines("Agent steps", result.agent_steps),
            _lines("Human steps", result.human_steps),
        ]
        if result.detail:
            body.append(f"  Detail: {result.detail}")
        return None, _assemble_response(head, body)
    if isinstance(result, ec.Rejected):
        head = (
            f"[ENGRAM: the engramd owner REFUSED the user-identity read "
            f"(reason: {result.reason}); NO identity context was loaded and the store "
            f"was not read directly.]"
        )
        body = [
            f"  Reason: {result.reason}",
            f"  Remedy: {result.remedy}",
        ]
        if result.detail:
            body.append(f"  Detail: {result.detail}")
        return None, _assemble_response(head, body)
    rows = result.result
    usable = isinstance(rows, list) and all(
        isinstance(r, list) and len(r) == len(_PERSON_NODES_COLUMNS) for r in rows
    )
    if not usable:
        head = (
            "[ENGRAM: the engramd owner returned an unusable answer to the user-identity "
            "read; NO identity context was loaded.]"
        )
        body = [f"  Expected a list of {len(_PERSON_NODES_COLUMNS)}-cell rows, got: {str(rows)[:200]}"]
        return None, _assemble_response(head, body)
    return [dict(zip(_PERSON_NODES_COLUMNS, r)) for r in rows], None


def detect_speaker(prompt: str, people: list[dict]) -> dict | None:
    """Check if prompt starts with a known person's name prefix.

    Returns person info dict if matched, None otherwise.
    Matching is case-insensitive against name and all aliases.
    """
    # Match pattern: "Name:" or "Name :" at start of prompt
    match = re.match(r"^\s*(\w[\w\s]{0,30}?)\s*:\s*", prompt)
    if not match:
        return None

    prefix_name = match.group(1).strip().lower()

    for person in people:
        try:
            meta = json.loads(person.get("metadata") or "{}")
        except (ValueError, TypeError):  # None / invalid JSON: same as `{}` (#3425)
            meta = {}
        if not isinstance(meta, dict):  # valid JSON, not an object (#3413)
            meta = {}
        name = meta.get("name", "").lower()
        aliases = [a.lower() for a in meta.get("aliases", [])]
        role = meta.get("role", "")

        # Check against full name and each alias
        all_names = [name] + aliases
        if prefix_name in all_names:
            return {
                "person_id": person["id"],
                "name": meta.get("name", ""),
                "role": role,
                "description": (person.get("logical_chain") or "")[:200],
                "matched_prefix": match.group(1).strip(),
            }

    return None


_MAX_HEAD_NAME_LEN = 100


def _truncate_name_for_head(name: str) -> str:
    """Cap a person name before it goes into the single-line `head` string.

    `head` is one f-string line with no internal `\\n`, so
    `_hooklib._trim_to_line_boundary()` treats any head longer than the
    per-harness head budget as un-boundaried and drops it WHOLE rather than
    truncating it -- an oversized `head` isn't shortened, it's silently
    removed (engram-alpha#2887 colleague review, Ariadne: a ~1722+ char
    person name renders "an identity card with the identity removed").
    Person names carry no length cap at write time (see engram_add_person
    in server.py), so this caps defensively at the call site, comfortably
    short of the ~1800-char drop boundary.
    """
    if len(name) > _MAX_HEAD_NAME_LEN:
        return name[:_MAX_HEAD_NAME_LEN] + "…"
    return name


def _assemble_response(head: str, body_blocks: list) -> dict:
    """Build the UserPromptSubmit response dict for an identity-switch
    announcement, routed through the shared bounded renderer
    `_hooklib.emit_context()` (engram-alpha#2863 WS2).

    This hook only ever fires ONE identity card per prompt (a genuine
    single-atomic-decision block, unlike engram-surface-hook.py's stack of
    independently-firing checks) -- but the card's own `Role:` / `Profile:`
    lines are sourced from free-text person-node metadata with no length
    cap enforced at write time (see engram_add_person in server.py), so the
    render is not the "already a fixed short string" escape-valve case
    either. `head` carries the one must-not-miss fact (WHO the session
    context switched to); `body_blocks` carries the supplementary Role/
    Profile detail, most-important-first, which is what actually absorbs
    any oversized metadata under the bound.

    Returns `{}` when both are empty (mirrors the pre-migration
    early-`{}` paths in `main()` below) -- `emit_context()` is never
    called with two empty inputs.
    """
    if not head and not body_blocks:
        return {}

    harness = detect_harness()
    budgets = get_harness_budgets(harness)
    rendered = emit_context(
        head,
        body_blocks,
        budget=budgets["budget"],
        # Deliberate DEVIATION from engram-surface-hook.py's target=budget
        # choice (#2876): that hook's content is already what the agent
        # receives in full every prompt today, so its soft ceiling is the
        # hard ceiling. This hook's identity card is the opposite case --
        # in the overwhelming majority of real calls it is a few short
        # lines, already well under head_budget, so target=head_budget
        # changes nothing in practice while buying the "trailer+head
        # survives the harness's own inline preview cut" guarantee
        # _harness.py's docstring names as the reason head_budget exists
        # ("exactly what the tripwire hook does when it folds onto the
        # helper"). Only a pathologically long Role/Profile value (no
        # length cap enforced at write time, see this function's own
        # docstring) would ever make this bind.
        target=budgets["head_budget"],
        # The Role line is the more informative of the two body items;
        # letting it run past the soft head_budget (never past the hard
        # budget) beats hard-truncating a legitimately long free-text
        # value at 1,800 chars when up to 9,500 is available.
        allow_single_overflow=True,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print(json.dumps({}))
        sys.exit(0)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        print(json.dumps({}))
        sys.exit(0)

    if _owner_reads_enabled():
        people, failure = read_people_via_owner()
        if failure is not None:
            print(json.dumps(failure))
            sys.exit(0)
    else:
        people = get_person_nodes()
    if not people:
        print(json.dumps({}))
        sys.exit(0)

    speaker = detect_speaker(prompt, people)

    if speaker:
        # New prefix detected — switch active user
        try:
            atomic_write_json(CURRENT_USER_PATH, speaker)
        except OSError:
            pass
        head = (
            f"[ENGRAM User Identity: {_truncate_name_for_head(speaker['name'])} "
            f"({speaker['person_id']}) — session context switched]"
        )
        body = [
            f"  Role: {speaker['role']}",
            f"  Profile: {speaker['description']}",
        ]
        print(json.dumps(_assemble_response(head, body)))
    else:
        # No prefix — use sticky session context if available
        if os.path.exists(CURRENT_USER_PATH):
            try:
                with open_regular_text(CURRENT_USER_PATH) as f:
                    sticky = json.load(f)
                # Suppress context for the configured primary user (default
                # speaker — no need to re-announce on every prompt). If no
                # primary_user is configured, show context for all sticky users.
                name_lower = sticky.get("name", "").lower()
                primary = get_primary_user()
                is_primary = primary and name_lower == primary
                if sticky.get("person_id") and not is_primary:
                    head = (
                        f"[ENGRAM User Identity: {_truncate_name_for_head(sticky['name'])} "
                        f"({sticky['person_id']}) — sticky session context]"
                    )
                    body = [
                        f"  Role: {sticky.get('role', '')}",
                        f"  Profile: {sticky.get('description', '')}",
                    ]
                    print(json.dumps(_assemble_response(head, body)))
                else:
                    print(json.dumps({}))
            except (json.JSONDecodeError, OSError):
                print(json.dumps({}))
        else:
            print(json.dumps({}))


if __name__ == "__main__":
    main()
