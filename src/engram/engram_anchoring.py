"""Hard-token anchoring for hard_data observations (engram-alpha#2602, facets 1-2).

Measures whether a claim's "hard" facts (node ids, issue/PR refs, hex shas,
3+-digit numbers, dates/times) are literally present in the observation's
own `quoted_text` -- the thing the verbatim-quote check actually verifies.
An observation can pass that check (the quote IS a real substring of the
source) while the claim's load-bearing facts come from somewhere else
entirely -- a short, well-provenanced quote paired with a long claim that
smuggles in numbers and IDs the quote never mentions. For `quote_type ==
"hard_data"`, "the quoted text IS the data" is the intended discipline;
this module makes that measurable.

Advisory only in this PR (facet 2 attaches the result to every observation
write's response; nothing here refuses a write or changes a confidence --
see engram_observation.py's call site and #2602's own sequencing, which
reserves refusal for facet 3 after a week of logged distribution).

Bound, stated once here rather than re-derived at each call site: anchoring
is a LITERAL SUBSTRING match, so it under-counts a quote that carries the
same fact in a different surface form (`5,121` in the claim vs `5121` in
the quote; a spelled-out date vs ISO form). This is the issue's own
accepted bound, not a defect to route around.

Known overlap (also accepted, not filtered): the five token classes below
are independent regexes over the same text, so a single span can
contribute more than one token -- a date like `2026-09-06` also matches
the number class as `2026` (digit count 4 >= 3), and a hex sha with an
embedded digit run can likewise also surface as its own number token.
Both tokens are real substrings of the claim and both get anchored (or
not) independently; the class list is "exactly these five" per the
issue's build spec, with no cross-class suppression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import engram_core as core

# Hard-token classes -- exactly five, per #2602's build spec. Order here
# is match-application order, NOT output order (output order is by first
# appearance in the text; see _hard_tokens_with_class below).
_NODE_ID_RE = re.compile(r"\b[a-z]{2}_\d{4}\b")
_ISSUE_REF_RE = re.compile(r"#\d+")
_HEX_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
# Deviates from #2602's build spec, which specifies `\d[\d,]*\.?\d*`
# literally -- that pattern swallows trailing punctuation as part of the
# number, because `[\d,]*` is happy to end on a comma and `\.?\d*` is happy
# to end on a bare dot with zero digits after it. Confirmed on real prose:
# "in #2609, sha ..." -> "2609," (comma included) and "count 247. Next"
# -> "247." (sentence period included). Both would then anchor-check
# against `quoted_text` for a token that includes stray punctuation the
# claim never "meant", silently under-counting real matches. This version
# requires the number to END on a digit before an optional comma-grouped
# tail (`(?:[\d,]*\d)?`) and requires at least one digit after a decimal
# point (`(?:\.\d+)?`) -- both changes only TIGHTEN what counts as part of
# the token; every example in the spec (`145`, `78.2`, `20.6`, `5,121`,
# `19.3s` -> `19.3`, `82089B` -> `82089`) still matches identically.
_NUMBER_RE = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?Z?")

# Class tag for tokens that need case-insensitive anchoring (hex shas only
# -- git often renders the same sha in mixed case across surfaces).
_HEX_SHA_CLASS = "hex_sha"


def _digit_count(token: str) -> int:
    return sum(ch.isdigit() for ch in token)


def _hard_tokens_with_class(text: str) -> list[tuple[str, str]]:
    """Internal: hard tokens paired with their class tag, in first-
    appearance document order, de-duplicated by exact token string."""
    if not text:
        return []

    candidates: list[tuple[int, str, str]] = []  # (start, token, class)

    for m in _NODE_ID_RE.finditer(text):
        candidates.append((m.start(), m.group(0), "node_id"))
    for m in _ISSUE_REF_RE.finditer(text):
        candidates.append((m.start(), m.group(0), "issue_ref"))
    for m in _HEX_SHA_RE.finditer(text):
        candidates.append((m.start(), m.group(0), _HEX_SHA_CLASS))
    for m in _NUMBER_RE.finditer(text):
        tok = m.group(0)
        if _digit_count(tok) >= 3:
            candidates.append((m.start(), tok, "number"))
    for m in _DATE_RE.finditer(text):
        candidates.append((m.start(), m.group(0), "date"))
    for m in _TIME_RE.finditer(text):
        candidates.append((m.start(), m.group(0), "time"))

    candidates.sort(key=lambda c: c[0])

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for _start, tok, cls in candidates:
        if tok in seen:
            continue
        seen.add(tok)
        ordered.append((tok, cls))
    return ordered


def hard_tokens(text: str) -> list[str]:
    """Extract hard tokens from `text`, ordered and de-duplicated as they
    first appear.

    Five classes (engram-alpha#2602 build spec):
      1. node ids            -- xx_NNNN
      2. issue/PR refs       -- #123 (the '#123' half of an
                                 'owner/repo#123' cross-repo form is what
                                 matches; the owner/repo prefix is not
                                 part of the token)
      3. hex shas             -- 7-40 hex characters
      4. numbers, >= 3 digits -- glued units are dropped by the regex
                                 boundary (19.3s -> 19.3, 82089B -> 82089);
                                 a comma-grouped number (5,121) counts its
                                 digits only (4), not its length
      5. dates/times          -- YYYY-MM-DD, H:MM[:SS][Z]
    """
    return [tok for tok, _cls in _hard_tokens_with_class(text)]


@dataclass
class AnchoringResult:
    hard_tokens: int
    anchored: int
    ratio: float | None
    unanchored: list[str]

    def to_dict(self) -> dict:
        return {
            "hard_tokens": self.hard_tokens,
            "anchored": self.anchored,
            "ratio": self.ratio,
            "unanchored": self.unanchored,
        }


def anchor(claim: str, quoted_text: str) -> AnchoringResult:
    """Compute how many of `claim`'s hard tokens are literal substrings of
    `quoted_text`.

    `ratio` is `None` when there are zero hard tokens -- "nothing to
    anchor" is a different state than "fully anchored" (which would read
    1.0), so a caller must be able to tell the two apart rather than
    treating an empty-tokens claim as trivially well-anchored.

    Anchoring is a literal substring check, case-insensitive only for the
    hex-sha class (git renders the same sha in mixed case across
    surfaces); every other class is matched case-sensitively.
    """
    tokens = _hard_tokens_with_class(claim)
    if not tokens:
        return AnchoringResult(hard_tokens=0, anchored=0, ratio=None, unanchored=[])

    quote = quoted_text or ""
    quote_lower = quote.lower()

    unanchored: list[str] = []
    anchored_count = 0
    for tok, cls in tokens:
        hit = tok.lower() in quote_lower if cls == _HEX_SHA_CLASS else tok in quote
        if hit:
            anchored_count += 1
        else:
            unanchored.append(tok)

    return AnchoringResult(
        hard_tokens=len(tokens),
        anchored=anchored_count,
        ratio=anchored_count / len(tokens),
        unanchored=unanchored,
    )


def quote_claim_similarity(quoted_text: str, claim: str) -> "tuple[Optional[float], Optional[str]]":
    """cosine(embed(quoted_text), embed(claim)) via the server's own
    embedder + config (engram-alpha#2602 facet 2). Returns (similarity,
    unavailable_reason) -- reason is None on success, a short string
    naming why on failure/unavailability. Never fabricates a 0.0 when the
    embedder can't run.

    The single shared home for this logic (reviewer-fairy finding on PR
    #2627, round 1): the write-time advisory in engram_observation.py and
    the read-only tools/anchoring_report.py CLI each carried their own
    byte-identical copy before this extraction -- a future fix to one
    (an embedder API change, a new failure mode) could land in one file
    and be silently missed in the other.
    """
    try:
        emb_config = core._get_embedding_config()
        if not emb_config.get("enabled", True):
            return None, "embeddings disabled in config"
        if not core._embedder.is_available():
            return None, "embedder unavailable"
        model = emb_config.get("model", core.DEFAULT_EMBEDDING_MODEL)
        vec_quote = core._embedder.embed(quoted_text or "", model)
        vec_claim = core._embedder.embed(claim or "", model)
        if not vec_quote or not vec_claim:
            return None, "embed() returned no vector"
        return core._embedder.cosine_similarity(vec_quote, vec_claim), None
    except Exception as exc:  # noqa: BLE001 — a caller-facing advisory must never raise
        return None, f"{type(exc).__name__}: {exc}"
