"""Viz quick-fixes: #2257 (edge dash owned by the schema), #2566 (nav-agent
CSS single-sourced in base.css), #2770 (Score panel never blank when a
snapshot exists but the composed score is unavailable)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz.data import health_v2 as viz_health_v2_data  # noqa: E402

VIZ = Path(viz_health_v2_data.__file__).resolve().parent.parent


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _rules(css: str):
    return re.findall(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css))


def test_2257_no_link_relation_rule_sets_dasharray():
    css = (VIZ / "static" / "graph.css").read_text(encoding="utf-8")
    bad = [sel.strip() for sel, body in _rules(css)
           if re.search(r"\.link\.[A-Za-z_]", sel) and "stroke-dasharray" in body]
    assert bad == [], f"CSS dash overrides the schema-driven dash: {bad}"


def test_2566_nav_agent_rules_only_in_base_css():
    pat = re.compile(r"(\.nav-agent-label|\.nav-agent-select|#nav-agent-wrapper)\s*[{,]")
    hits = []
    for p in list((VIZ / "static").rglob("*")) + list((VIZ / "templates").rglob("*")):
        if p.is_file() and p.suffix in (".css", ".html", ".js"):
            if pat.search(_strip_comments(p.read_text(encoding="utf-8"))):
                hits.append(p.name)
    assert hits == ["base.css"], hits


def _snapshot(composed, unscored):
    return {
        "measurements": {}, "signal_defs": {}, "config": {},
        "scores": {"composed": composed, "aspects": {}, "unscored_aspects": unscored},
    }


def _assemble(snap):
    return viz_health_v2_data._assemble_health_v2_payload(
        snap, "t-1", "2026-01-01T00:00:00Z")


def test_2770_unavailable_composed_carries_reason_null():
    p = _assemble(_snapshot(None, ["a", "b"]))
    assert p["has_snapshot"] is True
    assert p["reason"] and "a, b" in p["reason"]


def test_2770_unavailable_composed_carries_reason_sentinel_no_unscored_list():
    p = _assemble(_snapshot("UNAVAILABLE", []))
    assert p["reason"]


def test_2770_available_composed_has_no_reason():
    p = _assemble(_snapshot(88.0, []))
    assert "reason" not in p
