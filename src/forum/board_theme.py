"""Presentation theme + grouping for the project board.

SINGLE SOURCE OF TRUTH for how board items render — kept deliberately separate
from the read model (board_projects.py = data; this = presentation). The point
is extensibility: the human-visual board (color-coding, grouping, headers) reads
everything from here, so tuning the look or adding a new view is a one-place
edit, never a template rewrite.

Two extension axes are designed in from the start:

1. **Status theme** (`STATUS_THEME`) — add/recolor/relabel a status here; both the
   group headers and the cards pick it up. Colors are CSS-variable *slots*
   (`--st-*`), so the actual palette lives in CSS and the semantic mapping
   (which status uses which slot) lives here.

2. **Grouping** (`group_board`) — returns a generic ordered list of groups that
   the template renders without knowing the grouping axis. Today the only axis
   is `status`; when the board core grows namespaces, add a key-extractor to
   `_GROUPERS` (e.g. `"namespace"`) + a theme for that axis, and the SAME
   template renders it. No template change required to add a view.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Where clickable refs point. Runtime-configurable via FORUM_GITHUB_REPO so the
# shipped public snapshot carries no private dev-repo reference (the scan-leaks
# structural invariant), while the team's forum deployment points it at its
# working repo by setting the env var. Default = the public repo. This is also
# strictly more modular than a hardcoded constant: repoint with zero code change.
# ---------------------------------------------------------------------------
GITHUB_REPO = os.environ.get("FORUM_GITHUB_REPO", "engram-agents/engram")


# ---------------------------------------------------------------------------
# Kind glyphs (pr / issue / project / …) — one place, read by the card.
# ---------------------------------------------------------------------------
KIND_EMOJI: dict[str, str] = {
    "pr": "⎄",
    "issue": "◎",
    "project": "⬡",
    "task": "▪",
    "epic": "◇",
}
DEFAULT_KIND_EMOJI = "·"


# ---------------------------------------------------------------------------
# Status theme — the SSoT. `order` drives display order (low = first; terminal
# states high). `color` is a CSS-variable slot defined in the page's <style>.
# To add a status (e.g. "blocked") or recolor one, edit ONLY this table.
# ---------------------------------------------------------------------------
STATUS_THEME: dict[str, dict[str, Any]] = {
    "in-review":   {"label": "In review",   "color": "var(--st-review)",   "order": 0, "emoji": "👀"},
    "in-progress": {"label": "In progress",  "color": "var(--st-progress)", "order": 1, "emoji": "⚙"},
    "planning":    {"label": "Planning",     "color": "var(--st-planning)", "order": 2, "emoji": "✎"},
    "blocked":     {"label": "Blocked",      "color": "var(--st-blocked)",  "order": 3, "emoji": "⛔"},
    "done":        {"label": "Done",         "color": "var(--st-done)",     "order": 9, "emoji": "✓", "terminal": True},
}
# Fallback for any status not in the table (keeps the board robust to new baton
# statuses without a code change — they render with a neutral slot).
DEFAULT_STATUS_META: dict[str, Any] = {
    "label": None,            # None → fall back to the raw key
    "color": "var(--st-default)",
    "order": 5,
    "emoji": "·",
}


def status_meta(status: str) -> dict[str, Any]:
    """Resolve a status to its display metadata (label/color/emoji/order).

    Always returns a usable dict — unknown statuses get the neutral default with
    the raw key as the label, so the board never breaks on a new baton status.
    """
    m = STATUS_THEME.get(status)
    if m is None:
        return {**DEFAULT_STATUS_META, "label": status, "key": status}
    return {**m, "key": status}


def status_color_map() -> dict[str, str]:
    """status -> CSS-var slot, for the card to self-color regardless of grouping."""
    return {k: v["color"] for k, v in STATUS_THEME.items()}


def kind_emoji(kind: str) -> str:
    return KIND_EMOJI.get(kind, DEFAULT_KIND_EMOJI)


def terminal_statuses() -> set[str]:
    """Statuses rendered as 'complete' (faded card). Theme-driven, not a literal
    in the template — add `"terminal": True` to a STATUS_THEME entry to extend."""
    return {k for k, v in STATUS_THEME.items() if v.get("terminal")}


def github_url(github_ref: str) -> Optional[str]:
    """Resolve a baton `github` ref (e.g. 'pr/1005') to a clickable URL, or None."""
    if not github_ref:
        return None
    ref = github_ref.strip().lower()
    if ref.startswith("pr/"):
        return f"https://github.com/{GITHUB_REPO}/pull/{ref[3:]}"
    if ref.startswith("issue/"):
        return f"https://github.com/{GITHUB_REPO}/issues/{ref[6:]}"
    if ref.startswith("#"):
        return f"https://github.com/{GITHUB_REPO}/issues/{ref[1:]}"
    return None


# ---------------------------------------------------------------------------
# Grouping — the extensibility seam. `group_board` returns a generic ordered
# list of groups; the template renders it without knowing the axis.
# ---------------------------------------------------------------------------

# Key-extractors per grouping axis. Add an entry here (+ a theme) to introduce a
# new view — e.g. when board items gain a `namespace`, add:
#     "namespace": lambda it: it.get("namespace") or "ungrouped"
# and the same template renders a namespace-grouped board.
_GROUPERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "status": lambda it: it.get("effective_status") or "unknown",
}


def group_board(
    items: list[dict[str, Any]],
    group_by: str = "status",
) -> list[dict[str, Any]]:
    """Group board items into ordered render-groups.

    Returns a list of dicts, each:
        {key, label, color, emoji, order, count, cards}
    (`cards` not `items` — `g.items` in Jinja collides with dict.items()).
    Ordered by the axis theme's `order` then key. The template loops this
    structure generically — it never references "status" directly — so a new
    grouping axis (namespace, owner, …) is a `_GROUPERS` + theme addition, not a
    template change.

    Unknown `group_by` falls back to 'status' (robust default).
    """
    extract = _GROUPERS.get(group_by, _GROUPERS["status"])

    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(extract(it), []).append(it)

    groups: list[dict[str, Any]] = []
    for key, group_items in buckets.items():
        # Today every axis themes off status_meta; when a namespace theme lands,
        # select the theme by `group_by` here.
        meta = status_meta(key)
        groups.append({
            "key": key,
            "label": meta["label"] or key,
            "color": meta["color"],
            "emoji": meta["emoji"],
            "order": meta["order"],
            "count": len(group_items),
            "cards": group_items,
        })

    groups.sort(key=lambda g: (g["order"], g["key"]))
    return groups
