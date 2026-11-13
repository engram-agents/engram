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
is a literal, token-boundary match (#4206 -- a token inside a longer number
or identifier does not count), so it under-counts a quote that carries the
same fact in a different surface form (`5,121` in the claim vs `5121` in
the quote; a spelled-out date vs ISO form). This is the issue's own
accepted bound, not a defect to route around.

Known overlap (accepted, and suppressed once by the tokenizer): the five
token classes below are independent regexes over the same text, so a single
span can match more than one class -- a date like `2026-09-06` also matches
the number class as `2026` (digit count 4 >= 3), and a hex sha with an
embedded digit run can likewise also surface as a number token. A NUMBER
token whose span lies inside another emitted token's span is dropped
(#4206): it is part of that token, not a separate count. The class list is
"exactly these five" per the issue's build spec; the containment drop is
not a sixth class.
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

# Token-boundary alphabet (#4206): a hard token anchors only when the
# characters immediately around its occurrence are each absent or outside
# this set. `#` and punctuation are boundaries; letters, digits and `_` are
# not, so `404` does not anchor inside `#4040`, `x404`, or `ob_1234`.
_WORD_CHARS = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# Numeric separators (#4206 review): a '.' or ',' that touches a digit is
# part of the number, not a boundary -- the same rule s2_clause_check's
# _token_present (M1) applies. 404 is not in 4.404, 404.5 or 5,404, but it
# is in "error 404." and "404, then".
_NUMERIC_SEPARATORS = frozenset(".,")


def _token_is_anchored(tok: str, quote: str, *, hex_class: bool) -> bool:
    """True when `tok` occurs in `quote` at a token boundary.

    Non-hex tokens need a non-word character (or the quote's edge) on both
    sides. For a token that starts with a digit, a '.' or ',' touching a
    digit is not a boundary (M1's rule): `404` is not in `4.404`, `404.5`
    or `5,404`, but is in `error 404.`. Hex tokens are matched
    case-insensitively and their right side is deliberately looser (#4206):
    a 7-character sha is usually written as the prefix of the longer sha it
    names, so an occurrence followed by more hex digits anchors -- but only
    if the whole remainder of the word is hex (`abcdef1c0ffee9` anchors
    `abcdef1`; `abcdef1zz` does not).
    """
    if not tok:
        return False
    haystack = quote.lower() if hex_class else quote
    needle = tok.lower() if hex_class else tok
    digit_leading = tok[:1].isdigit()

    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        left_ok = start == 0 or haystack[start - 1] not in _WORD_CHARS
        if (
            left_ok
            and digit_leading
            and start >= 2
            and haystack[start - 1] in _NUMERIC_SEPARATORS
            and haystack[start - 2].isdigit()
        ):
            # `4.404` / `5,404`: the separator joins the token to a number.
            left_ok = False
        if left_ok:
            right = haystack[end] if end < len(haystack) else None
            separator_tail = (
                digit_leading
                and right in _NUMERIC_SEPARATORS
                and end + 1 < len(haystack)
                and haystack[end + 1].isdigit()
            )
            if not separator_tail:
                if right is None or right not in _WORD_CHARS:
                    return True
                if hex_class:
                    # The next character is a word character: the token
                    # anchors only if what follows is a longer all-hex word
                    # (a sha prefix) and not a non-hex extension (`zz`,
                    # `x`, `_`).
                    i = end
                    while i < len(haystack) and haystack[i] in _HEX_DIGITS:
                        i += 1
                    if i == len(haystack) or haystack[i] not in _WORD_CHARS:
                        return True
        start = haystack.find(needle, start + 1)
    return False


def _digit_count(token: str) -> int:
    return sum(ch.isdigit() for ch in token)


def _hard_tokens_with_class(text: str) -> list[tuple[str, str]]:
    """Internal: hard tokens paired with their class tag, in first-
    appearance document order, de-duplicated by exact token string.

    A NUMBER token whose span lies inside another emitted token's span (the
    `1234` in `ob_1234`, the digit run in a hex sha, the year in a date) is
    part of that token, not a separate count -- the same rule
    s2_clause_check._clause_hard_tokens applies for M1. An all-digit
    7+-character token is a number, not a hex sha (#4206: decimal digits
    are hex digits, so the sha regex matches them too, and the sha class's
    loose right side would re-open the 404-in-4040 defect one digit count
    up).
    """
    if not text:
        return []

    candidates: list[tuple[int, int, str, str]] = []  # (start, end, token, class)

    for m in _NODE_ID_RE.finditer(text):
        candidates.append((m.start(), m.end(), m.group(0), "node_id"))
    for m in _ISSUE_REF_RE.finditer(text):
        candidates.append((m.start(), m.end(), m.group(0), "issue_ref"))
    for m in _HEX_SHA_RE.finditer(text):
        tok = m.group(0)
        cls = "number" if tok.isdigit() else _HEX_SHA_CLASS
        candidates.append((m.start(), m.end(), tok, cls))
    for m in _NUMBER_RE.finditer(text):
        tok = m.group(0)
        if _digit_count(tok) >= 3:
            candidates.append((m.start(), m.end(), tok, "number"))
    for m in _DATE_RE.finditer(text):
        candidates.append((m.start(), m.end(), m.group(0), "date"))
    for m in _TIME_RE.finditer(text):
        candidates.append((m.start(), m.end(), m.group(0), "time"))

    # Span containment for NUMBER tokens only; endpoints may coincide (the
    # numeric tail of `ob_1234` ends where the node id ends).
    outer_spans = [(s, e) for s, e, _tok, cls in candidates if cls != "number"]
    candidates = [
        c
        for c in candidates
        if c[3] != "number"
        or not any(
            s <= c[0] and c[1] <= e and (s, e) != (c[0], c[1])
            for s, e in outer_spans
        )
    ]

    candidates.sort(key=lambda c: c[0])

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for _start, _end, tok, cls in candidates:
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
      3. hex shas             -- 7-40 hex characters (all-decimal runs of
                                 that length are classified as numbers,
                                 so they take the strict boundary rule)
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
    """Compute how many of `claim`'s hard tokens appear in `quoted_text` at
    a token boundary (engram-alpha#4206: a token inside a longer number or
    identifier does not count).

    `ratio` is `None` when there are zero hard tokens -- "nothing to
    anchor" is a different state than "fully anchored" (which would read
    1.0), so a caller must be able to tell the two apart rather than
    treating an empty-tokens claim as trivially well-anchored.

    Matching is case-sensitive except for the hex-sha class (git renders
    the same sha in mixed case across surfaces); see `_token_is_anchored`
    for the exact boundary rule, including the hex class's looser right
    side so a short sha still anchors as the prefix of the full one.
    """
    tokens = _hard_tokens_with_class(claim)
    if not tokens:
        return AnchoringResult(hard_tokens=0, anchored=0, ratio=None, unanchored=[])

    quote = quoted_text or ""

    unanchored: list[str] = []
    anchored_count = 0
    for tok, cls in tokens:
        if _token_is_anchored(tok, quote, hex_class=cls == _HEX_SHA_CLASS):
            anchored_count += 1
        else:
            unanchored.append(tok)

    return AnchoringResult(
        hard_tokens=len(tokens),
        anchored=anchored_count,
        ratio=anchored_count / len(tokens),
        unanchored=unanchored,
    )


# Below this quote-claim cosine similarity the write-time anchoring advisory
# adds `low_similarity_hint` (#4050 part a). Advisory only; one spelling.
LOW_QUOTE_CLAIM_SIMILARITY = 0.2


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
