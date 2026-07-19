"""IDF-based keyword extraction for ENGRAM FTS.

Provides:
  - Vendored stopword lists (sklearn ENGLISH_STOP_WORDS + ENGRAM discourse markers)
  - tokenize(): lowercased alphanumeric + underscore tokens
  - ensure_vocab_table(): idempotent fts5vocab virtual table creation
  - idf(): per-token inverse document frequency lookup from fts5vocab
  - extract_keywords(): top-K high-IDF keywords from a text string

Intended use-cases:
  - Alpha #177 area 1: pre-surface gate / prepending refinement
  - FTS rewrite in server.py (separate PR)

All I/O is via stdlib sqlite3 only — no external ML dependencies at runtime.
The sklearn stopword list is vendored as a literal constant below.

See alpha issue #177 for full design rationale and empirical motivation.
"""

from __future__ import annotations

import math
import re
import sqlite3

# ---------------------------------------------------------------------------
# Stopword lists
# ---------------------------------------------------------------------------

# Vendored from sklearn.feature_extraction.text.ENGLISH_STOP_WORDS (sklearn 1.4+; 318 words).
STANDARD_STOPWORDS: frozenset[str] = frozenset([
    "a", "about", "above", "across", "after", "afterwards", "again", "against",
    "all", "almost", "alone", "along", "already", "also", "although", "always",
    "am", "among", "amongst", "amoungst", "amount", "an", "and", "another",
    "any", "anyhow", "anyone", "anything", "anyway", "anywhere", "are", "around",
    "as", "at", "back", "be", "became", "because", "become", "becomes",
    "becoming", "been", "before", "beforehand", "behind", "being", "below",
    "beside", "besides", "between", "beyond", "bill", "both", "bottom", "but",
    "by", "call", "can", "cannot", "cant", "co", "con", "could", "couldnt",
    "cry", "de", "describe", "detail", "do", "done", "down", "due", "during",
    "each", "eg", "eight", "either", "eleven", "else", "elsewhere", "empty",
    "enough", "etc", "even", "ever", "every", "everyone", "everything",
    "everywhere", "except", "few", "fifteen", "fifty", "fill", "find", "fire",
    "first", "five", "for", "former", "formerly", "forty", "found", "four",
    "from", "front", "full", "further", "get", "give", "go", "had", "has",
    "hasnt", "have", "he", "hence", "her", "here", "hereafter", "hereby",
    "herein", "hereupon", "hers", "herself", "him", "himself", "his", "how",
    "however", "hundred", "i", "ie", "if", "in", "inc", "indeed", "interest",
    "into", "is", "it", "its", "itself", "keep", "last", "latter", "latterly",
    "least", "less", "ltd", "made", "many", "may", "me", "meanwhile", "might",
    "mill", "mine", "more", "moreover", "most", "mostly", "move", "much",
    "must", "my", "myself", "name", "namely", "neither", "never", "nevertheless",
    "next", "nine", "no", "nobody", "none", "noone", "nor", "not", "nothing",
    "now", "nowhere", "of", "off", "often", "on", "once", "one", "only", "onto",
    "or", "other", "others", "otherwise", "our", "ours", "ourselves", "out",
    "over", "own", "part", "per", "perhaps", "please", "put", "rather", "re",
    "same", "see", "seem", "seemed", "seeming", "seems", "serious", "several",
    "she", "should", "show", "side", "since", "sincere", "six", "sixty", "so",
    "some", "somehow", "someone", "something", "sometime", "sometimes",
    "somewhere", "still", "such", "system", "take", "ten", "than", "that",
    "the", "their", "them", "themselves", "then", "thence", "there",
    "thereafter", "thereby", "therefore", "therein", "thereupon", "these",
    "they", "thick", "thin", "third", "this", "those", "though", "three",
    "through", "throughout", "thru", "thus", "to", "together", "too", "top",
    "toward", "towards", "twelve", "twenty", "two", "un", "under", "until",
    "up", "upon", "us", "very", "via", "was", "we", "well", "were", "what",
    "whatever", "when", "whence", "whenever", "where", "whereafter", "whereas",
    "whereby", "wherein", "whereupon", "wherever", "whether", "which", "while",
    "whither", "who", "whoever", "whole", "whom", "whose", "why", "will",
    "with", "within", "without", "would", "yet", "you", "your", "yours",
    "yourself", "yourselves",
])

DISCOURSE_MARKERS: frozenset[str] = frozenset([
    # Positive acknowledgments
    "great", "good", "nice", "beautiful", "excellent", "awesome",
    "cool", "perfect", "wonderful", "fantastic", "amazing", "lovely",
    # Reactions / fillers
    "wow", "huh", "oh", "ah", "hmm", "ohh", "aha", "yay",
    # Greetings
    "hi", "hello", "hey", "morning", "evening",
    # Affirmation / negation
    "thanks", "thank", "ok", "okay", "alright",
    "sure", "yeah", "yep", "yes", "no", "nope",
    # Discourse openers
    "well", "so", "actually", "really", "just", "very",
    "quite", "basically", "wait", "btw",
    # Conversational reference markers
    "previous", "want", "talk", "double", "check",
    "feel", "feels", "think",
    # Politeness
    "please", "kindly",
])

# Union of both layers — the default filter applied by extract_keywords.
STOPWORDS: frozenset[str] = STANDARD_STOPWORDS | DISCOURSE_MARKERS

# Canonical junk-token stoplist (rec-3, #266 / #1784) — the SINGLE source of
# truth shared by the in-turn-recall hook (the live filter) and the rec-4
# measurement harness (the measurement). Both import this constant so they can
# never drift about "what counts as junk" on the same ledger.
#
# Deliberately CONSERVATIVE: drop only tokens that are unambiguously
# shell/code execution-noise, never topical. A zero-cooldown ledger analysis
# showed a stricter cut (e.g. a min_idf bump, or adding ambiguous tokens like
# json/os/re/cat/print/def) kills ~24% of REAL renders — so the filter is the
# floor and the measurement conforms DOWN to it, never the reverse (#1784
# decision: the filter's conservative list is canonical). Extend/tune only from
# rec-4 harness evidence. Config `auto_surface.in_turn_recall.junk_stoplist`
# overrides the whole list; `[]` disables filtering.
JUNK_STOPLIST: frozenset[str] = frozenset({
    "str", "rn", "echo", "wc", "sed", "eof",       # Kepler's named class (all-seats-confirmed)
    "sys", "stdin", "stdout", "stderr",            # python IO
    "nohup", "pkill", "sigterm", "pgrep", "grep",  # process/shell
    "awk", "xargs", "chmod", "mkdir", "rmdir", "printf",
    "sh", "bash", "zsh",
})

# Pre-compiled tokenize pattern: two-or-more letters to start, then alphanumeric +
# underscore.  The spec's stated pattern is [a-z][a-z0-9_]*, but the spec test
# assertions imply single-letter tokens (e.g. bare "s" from apostrophe-s) and
# single-letter-then-digit tokens (e.g. "c1" from "C1's") must be excluded.
# [a-z]{2,}[a-z0-9_]* is the minimal tightening that satisfies all spec examples:
#   - tokenize("Hello, EEC on C1's 'bound'.") → ['hello', 'eec', 'on', 'bound']
#   - tokenize("id_006 supersedes id_001")  → ['id_006', 'supersedes', 'id_001']
_TOKEN_RE = re.compile(r"[a-z]{2,}[a-z0-9_]*")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lowercase text and extract alphanumeric tokens that may contain underscores.

    The effective pattern is ``[a-z]{2,}[a-z0-9_]*`` applied after lowercasing:
    tokens must start with at least two letters, so pure numbers, single-letter
    fragments (e.g. bare ``s`` from apostrophe-s possessives), and
    single-letter-then-digit tokens (e.g. ``c1`` from ``C1's``) are excluded.
    Node IDs like ``the calibration axiom`` or ``dv_NNNN`` survive intact because their
    alphabetic prefix is at least two letters.

    Returns a list of tokens preserving input order.
    """
    return _TOKEN_RE.findall(text.lower())


def ensure_vocab_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``nodes_fts_vocab`` virtual table over ``nodes_fts``.

    Uses the fts5vocab extension with ``'row'`` mode so each row in the virtual
    table holds (term, doc, cnt) — *doc* is the document-frequency count used
    by :func:`idf`.

    No-op if the table already exists.  Raises ``sqlite3.OperationalError`` if
    ``nodes_fts`` is missing from the database (the caller's DB must have been
    initialized with :func:`server.ensure_db`).

    Implementation note: SQLite accepts the ``CREATE VIRTUAL TABLE`` DDL even
    when the source fts5 table does not exist yet; the error surfaces on the
    first query.  To honour the documented "raises if nodes_fts is missing"
    contract, we probe the vocab table immediately after creation.
    """
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts_vocab "
        "USING fts5vocab(nodes_fts, 'row')"
    )
    # Probe: forces sqlite to resolve the fts5 table reference now.
    # Raises sqlite3.OperationalError if nodes_fts does not exist.
    # LIMIT 0 is insufficient — fts5vocab defers resolution until rows are
    # actually requested; fetchone() forces the scan to start.
    conn.execute("SELECT term FROM nodes_fts_vocab").fetchone()


def idf(
    conn: sqlite3.Connection,
    token: str,
    n_docs: int | None = None,
) -> float | None:
    """Return the IDF score for *token* by querying ``nodes_fts_vocab``.

    IDF is computed as ``log(n_docs / df)`` (natural log) where *df* is the
    document frequency of *token* in the fts5 index.  Returns ``None`` if the
    token is absent from the vocabulary (callers should treat this as "unknown
    to corpus" — neither noise nor signal).

    ``nodes_fts`` indexes only current (non-superseded, non-retracted) nodes —
    superseded nodes are removed from the index by the ``nodes_fts_supersede_remove``
    trigger at supersede time, and retracted nodes are removed by
    ``nodes_retract_remove_from_fts``.  This means ``nodes_fts_vocab`` df counts
    and the current-node ``n_docs`` are always coherent: df can never exceed
    n_docs for a valid corpus.

    *n_docs* is the total count of current nodes used for IDF normalisation.
    Pass it explicitly when looping over many tokens to avoid a repeated
    ``COUNT(*)`` query.  When omitted, the function queries the current-node count.
    """
    if n_docs is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
        ).fetchone()
        n_docs = row[0] if row else 0

    if n_docs == 0:
        return None

    row = conn.execute(
        "SELECT doc FROM nodes_fts_vocab WHERE term = ?",
        (token,),
    ).fetchone()
    df = row[0] if row is not None else None

    if df is None or df <= 0 or df > n_docs:
        return None

    return math.log(n_docs / df)


def extract_keywords(
    conn: sqlite3.Connection,
    text: str,
    min_idf: float = 4.0,
    top_k: int = 10,
    stopwords: frozenset[str] | None = None,
) -> list[tuple[str, float]]:
    """Extract the top-K high-IDF keywords from *text*.

    Steps:
      1. Tokenize *text* with :func:`tokenize`.
      2. Filter out tokens in *stopwords* (defaults to :data:`STOPWORDS`).
      3. Look up IDF for each remaining token; silently exclude tokens absent
         from the fts5 vocabulary (treated as unknown — not noise, not signal).
      4. Keep only tokens with ``idf >= min_idf``.
      5. Return the top *top_k* tokens sorted by IDF descending.

    To avoid repeated ``COUNT(*)`` queries, n_docs is resolved once at the top
    of the function and passed to :func:`idf` for every token.

    Returns a list of ``(token, idf_score)`` pairs.
    """
    if stopwords is None:
        stopwords = STOPWORDS

    row = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
    ).fetchone()
    n_docs = row[0] if row else 0

    if n_docs == 0:
        return []

    tokens = tokenize(text)

    scored: list[tuple[str, float]] = []
    seen: set[str] = set()

    for token in tokens:
        if token in stopwords:
            continue
        if token in seen:
            continue
        seen.add(token)

        score = idf(conn, token, n_docs=n_docs)
        if score is None:
            continue
        if score < min_idf:
            continue
        scored.append((token, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
