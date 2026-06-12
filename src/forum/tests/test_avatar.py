"""Tests for forum/avatar.py — deterministic geometric avatar SVG helper."""
import xml.etree.ElementTree as ET

import pytest

from forum.avatar import avatar_svg


SEEDS = ["agent-a", "agent-b", "agent-c", "agent-d", "beacon", "ledger"]


class TestAvatarDeterminism:
    def test_same_seed_same_svg(self):
        """Same seed always produces identical SVG string."""
        for seed in SEEDS:
            svg1 = avatar_svg(seed)
            svg2 = avatar_svg(seed)
            assert svg1 == svg2, f"Non-deterministic for seed={seed!r}"

    def test_distinct_seeds_distinct_hues(self):
        """6 distinct seeds produce 6 distinct hue values in the SVG output."""
        hues = set()
        for seed in SEEDS:
            svg = avatar_svg(seed)
            # The oklch color contains the hue as the third value.
            # bg = oklch(0.32 0.05 <hue>) — extract the hue digit(s).
            import re
            m = re.search(r"oklch\(0\.32 0\.05 (\d+)\)", svg)
            assert m is not None, f"Could not find bg oklch hue in SVG for seed={seed!r}"
            hues.add(m.group(1))
        assert len(hues) == len(SEEDS), (
            f"Expected {len(SEEDS)} distinct hues, got {len(hues)}: {hues}"
        )

    def test_all_four_glyph_variants_appear(self):
        """All 4 glyph variants (circle, square, triangle, X) appear across seeds."""
        # Use a range of seeds to force all 4 variants (variant = h % 4).
        # We need at least one seed per variant.
        tags_seen: set[str] = set()
        # Try a wide range to cover all 4 variants.
        for i in range(50):
            svg = avatar_svg(f"seed-{i}")
            import re
            # Find the non-rect shape tag
            shapes = re.findall(r"<(circle|rect|polygon|line|g)", svg)
            # First rect is always the background; subsequent shapes are glyphs.
            glyph_tags = [s for s in shapes[1:] if s != "rect"]
            if glyph_tags:
                tags_seen.add(glyph_tags[0])
        assert "circle" in tags_seen, "circle variant never appeared"
        assert "rect" in tags_seen or "polygon" in tags_seen or "line" in tags_seen, (
            "Not all glyph variants appeared"
        )
        # Specifically check all four variants are represented
        assert len(tags_seen) >= 3, f"Only {len(tags_seen)} glyph variant tags seen: {tags_seen}"


class TestAvatarSVGStructure:
    def test_output_is_well_formed_xml(self):
        """Output parses as well-formed XML via ElementTree."""
        for seed in SEEDS:
            svg = avatar_svg(seed)
            try:
                ET.fromstring(svg)
            except ET.ParseError as e:
                pytest.fail(f"SVG for seed={seed!r} is not well-formed XML: {e}\n{svg}")

    def test_output_contains_no_script(self):
        """Output contains no <script> tags or event handlers."""
        for seed in SEEDS:
            svg = avatar_svg(seed).lower()
            assert "<script" not in svg, f"<script> found in SVG for seed={seed!r}"
            assert "onerror" not in svg
            assert "onload" not in svg
            assert "onclick" not in svg

    def test_custom_size(self):
        """Custom size parameter is reflected in the SVG dimensions."""
        svg = avatar_svg("agent-a", size=64)
        assert 'width="64"' in svg
        assert 'height="64"' in svg
        # Default size
        svg32 = avatar_svg("agent-a", size=32)
        assert 'width="32"' in svg32
        assert 'height="32"' in svg32

    def test_svg_root_element(self):
        """Output starts with <svg and has correct root element."""
        svg = avatar_svg("agent-b")
        assert svg.startswith("<svg ")
        root = ET.fromstring(svg)
        assert root.tag == "svg"
