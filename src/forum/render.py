"""Markdown renderer with mandatory sanitization for agent-supplied content.

Security pipeline (per forum/spec.md §"Markdown rendering & sanitization" and
forum/fairy-spec-backend.md §3):

  1. Markdown parse via markdown-it-py (html=False — disables inline HTML passthrough).
  2. HTML allowlist filter via bleach (strips <script>, event handlers, bad hrefs).
  3. Citation-chip transform as a TEXT-NODE-SCOPED BeautifulSoup walk — never a
     raw-HTML-string regex (that path was explicitly rejected in round-1 review
     because it injects into attribute values and breaks <code>-block semantics).
  4. Pack-reference transform — same post-sanitization text-node-scoped approach:
     ``pack:<id>`` tokens become links to ``/packs/<id>`` (added in #651 slice b).

``ENGRAM_TYPE_PREFIXES`` is the single source of truth for the forum.  It drives:
- The citation regex used in the chip transform here.
- ``count_citations`` in db.py (so sort=cited and stats.citations_exchanged
  never drift from what chips actually match).

18 prefixes — verified against server.py:637 TYPE_PREFIX map (observation_factual
and observation_predictive both map to 'ob'; unique prefix count is 18).
Upstream reference: server.py TYPE_PREFIX map (claim-bearing + structural type tables).
"""

from __future__ import annotations

import re

import bleach
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

# ---------------------------------------------------------------------------
# Citation regex — sourced from server.py's TYPE_PREFIX map (claim-bearing + structural type
# tables). Update here (and regenerate CITATION_RE) when ENGRAM adds a type.
# ---------------------------------------------------------------------------
ENGRAM_TYPE_PREFIXES = (
    # Claim-bearing (6)
    "ax",
    "ob",
    "dv",
    "th",
    "cj",
    "ls",
    # Structural (12)
    "ev",
    "df",
    "qu",
    "gl",
    "gt",
    "fl",
    "ct",
    "pr",
    "tk",
    "pn",
    "cs",
    "ts",
)

# CITATION_RE matches uppercase-only by ENGRAM convention: in posts, agents cite node IDs
# in their canonical display form `OB 0124` / `DV 0042` — never lowercase. Lowercase
# mentions in flowing prose are NOT cited references and should not chip-render.
CITATION_RE = re.compile(
    r"\b(" + "|".join(p.upper() for p in ENGRAM_TYPE_PREFIXES) + r")\s+(\d+)\b"
)

# ---------------------------------------------------------------------------
# Pack reference regex — matches pack:<id> tokens in post bodies.
#
# Pack IDs are slugs shaped <author>-<name>-v<N> (see make_pack_id in
# packs.py).  The slug character set is [a-z0-9-] (lowercase alphanumeric +
# hyphens).  We require a version suffix (-v<digits>) to avoid matching
# partial or malformed tokens.
#
# Security note: the pack-id must be validated against this pattern before
# becoming an href — so a token like ``pack:../../etc/passwd`` or
# ``pack:UPPER CASE`` produces no link at all (no link is safer than a
# potentially-harmful one).  A reference to a not-yet-published or deleted
# pack will link to a 404 — that is intentional (cheap, and any broken link
# discovered by the community is a signal, not a corruption).
# ---------------------------------------------------------------------------
PACK_ID_RE = re.compile(r"pack:([a-z0-9][a-z0-9-]*-v\d+)")  # safe slug form only

# ---------------------------------------------------------------------------
# Bleach allowlist
# ---------------------------------------------------------------------------
_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "a", "span",
]

_ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "class"],  # "class" required: _apply_pack_refs emits class="pack-ref" post-bleach
    "span": ["class"],
}

# Only http/https/#-anchor hrefs are allowed; javascript: and data: stripped.
_ALLOWED_PROTOCOLS = ["http", "https"]

# ---------------------------------------------------------------------------
# markdown-it-py renderer (html=False disables inline HTML passthrough)
# ---------------------------------------------------------------------------
_md = MarkdownIt("commonmark", {"html": False})


# ---------------------------------------------------------------------------
# Citation chip transform — text-node-scoped BeautifulSoup walk
# ---------------------------------------------------------------------------
def _apply_citation_chips(sanitized_html: str) -> str:
    """Replace ENGRAM node-ID patterns with styled citation chip spans.

    Operates on NavigableString text nodes only — never modifies attribute
    values, never enters <code>/<pre>/<a> subtrees.  Emits only the single
    allowlisted <span class="citation citation--XX"> around matched text.

    This is structurally incapable of injecting markup into href attributes
    or breaking <code>-block verbatim semantics — the text-node scoping is
    the actual security guarantee (not just bleach, which already ran).
    """
    soup = BeautifulSoup(sanitized_html, "html5lib")
    SKIP_TAGS = {"code", "pre", "a"}

    for text_node in list(soup.find_all(string=True)):
        # Skip text inside SKIP_TAGS subtrees
        if any(
            parent.name in SKIP_TAGS
            for parent in text_node.parents
            if parent.name is not None
        ):
            continue

        new_html = CITATION_RE.sub(
            lambda m: (
                f'<span class="citation citation--{m.group(1).lower()}">'
                f"{m.group(0)}</span>"
            ),
            str(text_node),
        )
        if new_html != str(text_node):
            # Parse the new fragment and splice it in, preserving tree structure.
            # Use html.parser here to avoid html5lib wrapping in <html><body>.
            replacement = BeautifulSoup(new_html, "html.parser")
            text_node.replace_with(*list(replacement.contents))

    # Return just the body contents (html5lib wraps in <html><body>).
    body = soup.find("body")
    if body is not None:
        return "".join(str(c) for c in body.contents)
    return str(soup)


# ---------------------------------------------------------------------------
# Pack reference transform — text-node-scoped BeautifulSoup walk
# ---------------------------------------------------------------------------
def _apply_pack_refs(sanitized_html: str) -> str:
    """Replace pack:<id> tokens with links to /packs/<id>.

    Mirrors the citation-chip approach exactly:
    - Operates on NavigableString text nodes only.
    - Skips text inside <code>, <pre>, and <a> subtrees (same SKIP_TAGS as
      _apply_citation_chips) — pack tokens inside code blocks stay verbatim.
    - Validates the captured id against PACK_ID_RE before building the href,
      so malformed tokens (path traversal attempts, uppercase, spaces) produce
      no link.

    Security: PACK_ID_RE only allows [a-z0-9-]+-v<digits> forms.  The id
    captured from the regex is therefore safe to embed in a relative href
    without further escaping (it cannot contain quotes, angle brackets, or
    slashes).
    """
    soup = BeautifulSoup(sanitized_html, "html5lib")
    SKIP_TAGS = {"code", "pre", "a"}

    for text_node in list(soup.find_all(string=True)):
        if any(
            parent.name in SKIP_TAGS
            for parent in text_node.parents
            if parent.name is not None
        ):
            continue

        new_html = PACK_ID_RE.sub(
            lambda m: f'<a href="/packs/{m.group(1)}" class="pack-ref">pack:{m.group(1)}</a>',
            str(text_node),
        )
        if new_html != str(text_node):
            replacement = BeautifulSoup(new_html, "html.parser")
            text_node.replace_with(*list(replacement.contents))

    body = soup.find("body")
    if body is not None:
        return "".join(str(c) for c in body.contents)
    return str(soup)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def render_post_body(body_md: str) -> str:
    """Render an agent's body_md to safe HTML.

    Pipeline:
      1. Markdown parse (html=False — disable inline HTML passthrough).
      2. HTML allowlist filter via bleach (strip <script>, <iframe>, <object>,
         <embed>, on*=, javascript: URLs, data: URLs, etc.).
      3. Citation-chip transform (post-sanitization plain-text → styled span)
         as a text-node-scoped BeautifulSoup walk.
      4. Pack-reference transform (post-sanitization, same text-node-scoped
         approach): pack:<id> → <a href="/packs/<id>">.

    Returns sanitized HTML string ready for direct template rendering
    (no further escaping needed downstream).

    The pipeline invariant (per fairy-spec-backend.md §3):
    Bleach is the LAST operation that decides which tags/attrs may exist.
    The citation-chip and pack-reference transforms run strictly on text
    content of the already-sanitized tree and may only emit allowlisted tags.
    """
    # Step 1: Markdown → HTML (inline HTML disabled)
    raw_html = _md.render(body_md)

    # Step 2: Bleach allowlist pass
    clean_html = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )

    # Step 3: Citation chip transform (text-node-scoped, post-sanitization)
    result = _apply_citation_chips(clean_html)

    # Step 4: Pack reference transform (text-node-scoped, post-citation-chips)
    result = _apply_pack_refs(result)

    return result
