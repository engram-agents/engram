"""Deterministic geometric avatar SVG helper.

Ported from /tmp/engram-website-skeleton/forum-section.jsx lines 162-184.
Same hash algorithm: h = (h * 31 + ord(ch)) & 0xffffff.
"""

from __future__ import annotations


def avatar_svg(seed: str, size: int = 32) -> str:
    """Return a deterministic geometric avatar SVG string for the given seed.

    Same seed always produces the same SVG.  No faces; 4 glyph variants
    (circle, square, triangle, X) x oklch hue derived from the seed hash.
    The output is server-generated markup only — no agent-supplied content
    flows through, so ``| safe`` in Jinja templates is correct.

    Args:
        seed: The seed string (typically the agent name).
        size: Pixel dimensions of the SVG (square).

    Returns:
        A complete ``<svg>`` element string.
    """
    h = 0
    for ch in seed:
        h = (h * 31 + ord(ch)) & 0xFFFFFF

    hue = h % 360
    variant = h % 4
    bg = f"oklch(0.32 0.05 {hue})"
    fg = f"oklch(0.85 0.13 {(hue + 60) % 360})"

    if variant == 0:
        # Circle
        cx = size / 2
        cy = size / 2
        r = size * 0.22
        glyph = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fg}" />'
    elif variant == 1:
        # Square
        x = size * 0.30
        y = size * 0.30
        w = size * 0.40
        h_rect = size * 0.40
        glyph = f'<rect x="{x}" y="{y}" width="{w}" height="{h_rect}" fill="{fg}" />'
    elif variant == 2:
        # Triangle
        pts = (
            f"{size / 2},{size * 0.26} "
            f"{size * 0.74},{size * 0.72} "
            f"{size * 0.26},{size * 0.72}"
        )
        glyph = f'<polygon points="{pts}" fill="{fg}" />'
    else:
        # X (two lines)
        x1a, y1a = size * 0.28, size * 0.28
        x2a, y2a = size * 0.72, size * 0.72
        x1b, y1b = size * 0.72, size * 0.28
        x2b, y2b = size * 0.28, size * 0.72
        glyph = (
            f'<line x1="{x1a}" y1="{y1a}" x2="{x2a}" y2="{y2a}" '
            f'stroke="{fg}" stroke-width="2.5" stroke-linecap="round"/>'
            f'<line x1="{x1b}" y1="{y1b}" x2="{x2b}" y2="{y2b}" '
            f'stroke="{fg}" stroke-width="2.5" stroke-linecap="round"/>'
        )

    svg = (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'style="border-radius:8px;flex-shrink:0">'
        f'<rect width="{size}" height="{size}" fill="{bg}" rx="6" />'
        f"{glyph}"
        f"</svg>"
    )
    return svg
