"""Tests for board_theme — the presentation SSoT + grouping abstraction.

The grouping function is the extensibility seam (status today, namespace later),
so its contract (generic ordered groups, `cards` key, robust fallbacks) is worth
locking.
"""

from forum import board_theme as t


def _items(*statuses):
    return [{"effective_status": s, "project": f"P{i}"} for i, s in enumerate(statuses)]


# --- group_board ----------------------------------------------------------

def test_group_board_orders_by_theme_then_buckets():
    groups = t.group_board(_items("done", "in-progress", "in-review", "in-progress"))
    keys = [g["key"] for g in groups]
    # theme order: in-review(0) < in-progress(1) < done(9)
    assert keys == ["in-review", "in-progress", "done"]
    counts = {g["key"]: g["count"] for g in groups}
    assert counts == {"in-review": 1, "in-progress": 2, "done": 1}


def test_group_board_uses_cards_key_not_items():
    # `items` would collide with dict.items() in Jinja — must be `cards`.
    groups = t.group_board(_items("done"))
    assert "cards" in groups[0] and "items" not in groups[0]
    assert len(groups[0]["cards"]) == 1


def test_group_board_carries_color_and_emoji_from_theme():
    g = t.group_board(_items("in-review"))[0]
    assert g["color"] == "var(--st-review)"
    assert g["label"] == "In review"
    assert g["emoji"]


def test_group_board_unknown_status_falls_back_neutral():
    groups = t.group_board(_items("brand-new-status"))
    g = groups[0]
    assert g["key"] == "brand-new-status"
    assert g["label"] == "brand-new-status"          # raw key as label
    assert g["color"] == "var(--st-default)"          # neutral slot


def test_group_board_unknown_group_by_falls_back_to_status():
    # robust default: an unknown axis groups by status, never errors
    groups = t.group_board(_items("done", "in-progress"), group_by="nonsense")
    assert [g["key"] for g in groups] == ["in-progress", "done"]


def test_group_board_empty():
    assert t.group_board([]) == []


# --- github_url -----------------------------------------------------------

def test_github_url_variants():
    base = f"https://github.com/{t.GITHUB_REPO}"
    assert t.github_url("pr/1005") == f"{base}/pull/1005"
    assert t.github_url("PR/1005") == f"{base}/pull/1005"   # case-insensitive
    assert t.github_url("issue/42") == f"{base}/issues/42"
    assert t.github_url("#42") == f"{base}/issues/42"
    assert t.github_url("") is None
    assert t.github_url("nonsense") is None


def test_github_url_repo_qualified_pr_anchor():
    """#1715 reviewer-fairy-caught: a repo-qualified anchor
    (pr/<owner>/<repo>/<N>) must link to ITS OWN repo, not GITHUB_REPO --
    the pre-fix `ref[3:]` slice glued 'owner/repo/N' onto GITHUB_REPO's URL,
    producing a broken link."""
    assert (
        t.github_url("pr/engram-agents/engram-paper/22")
        == "https://github.com/engram-agents/engram-paper/pull/22"
    )
    assert (
        t.github_url("PR/Engram-Agents/Engram-Paper/22")
        == "https://github.com/engram-agents/engram-paper/pull/22"
    )
    # Malformed qualified form (only one path segment) -- rejected, not
    # silently mis-parsed into a broken link.
    assert t.github_url("pr/only-one-segment/22") is None


# --- helpers --------------------------------------------------------------

def test_status_color_map_covers_theme():
    m = t.status_color_map()
    for status in t.STATUS_THEME:
        assert m[status] == t.STATUS_THEME[status]["color"]


def test_kind_emoji_fallback():
    assert t.kind_emoji("pr") == t.KIND_EMOJI["pr"]
    assert t.kind_emoji("unheard-of") == t.DEFAULT_KIND_EMOJI
