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

import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# #1918 / #1919: there is NO default repo for an unqualified ref, deliberately
# -- do not revisit. This module used to link a bare pr/<N> (and issue/<N>,
# #<N>) to a default repo ($ENGRAM_DEFAULT_GITHUB_REPO, else
# $FORUM_GITHUB_REPO, else the public repo); such a ref now gets NO link
# (github_url returns None) rather than a link to a guessed repo.
#
# #1715: a repo-qualified anchor (pr/<owner>/<repo>/<N>) links to ITS OWN
# repo -- the naive `ref[3:]` slice (pre-fix) glued the qualified anchor's
# full "owner/repo/N" tail onto a default repo's URL, producing a broken
# link. Mirrors board_projects.py's _PR_ANCHOR_RE.
_PR_ANCHOR_RE = re.compile(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?(\d+)$", re.IGNORECASE)


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
    """Resolve a baton `github` ref to a clickable URL, or None.

    Only the repo-qualified PR form (#1715: 'pr/<owner>/<repo>/1005') links.
    #1918: a bare 'pr/1005' -- and the unqualified 'issue/<N>' / '#<N>'
    shapes -- have no repo and there is NO default repo, so they get no link
    (None), never a link to a guessed repo."""
    if not github_ref:
        return None
    ref = github_ref.strip().lower()
    if ref.startswith("pr/"):
        m = _PR_ANCHOR_RE.match(ref)
        if not m:
            return None
        owner, repo_name, number = m.groups()
        if not (owner and repo_name):
            return None
        return f"https://github.com/{owner}/{repo_name}/pull/{number}"
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
