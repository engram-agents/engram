"""engram_observation — family A: evidence / observation ingestion.

Extracted from server.py as part of #872 wave 9 (the LAST extraction wave).

Family A covers the provenance-guard machinery — the substrate's honesty
layer.  Three impls ride this wave:

  _add_evidence_impl      — evidence node creation with URL guards (TLD,
                            DNS, committed-before-cite).
  _add_observation_impl   — single observation with verbatim quote check,
                            per-observation file versioning, dedup/polarity.
  _add_observation_batch_impl — bulk variant sharing one evidence node.

``engram_add_evidence`` is a NON-REGISTERED tool-shaped function that was
deliberately omitted from @mcp.tool in server.py (wave-0 D7 invariant).
Its body has been extracted here as ``_add_evidence_impl``; the
non-registered delegating shell remains in server.py.  The MCP schema must
never list ``engram_add_evidence`` — gate 1 proves this.

A-local helpers (all verified A-internal — zero consumers outside family A
on the wave-7 tip):
  _get_polarity_config
  _compute_polarity_alerts
  _extract_domain
  _check_yellow_card
  _format_yellow_warning
  _escape_for_source
  _decode_from_source
  _find_near_matches
  _capture_file_version
  _verify_quote_in_source

NOT moved (already in engram_core since wave 1):
  _git_sha_for_file  — core helper
  _infer_source_type — core helper

Constants: all dedup/polarity constants (DEDUP_TOP_K, POLARITY_DEFAULT_*,
etc.) are in engram_core; family A accesses them via ``core.X`` at call time.
VALID_QUOTE_TYPES and VALID_SOURCE_CLASSES are in engram_confidence; imported
from server.py at module level and accessible via the call-chain (they are
already on the server module's namespace — family A is called FROM server
wrappers that supply these constants locally). See NXDOMAIN_ERRNOS below for
the one A-local socket-errno constant.

NXDOMAIN_ERRNOS is an A-local constant: it is computed once at import time
from socket errno codes and used exclusively by _add_evidence_impl's DNS
guard. It is not in engram_core because it requires ``import socket`` at
module level and is consumed only within this family.

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - No ``from engram_core import`` (Rule A of the seam gate).
  - No module-level assignment of any of the 14 MUTABLE_NAMES from engram_core
    (Rule B of the seam gate).
  - Stateless module: no mutable global state beyond constants.
  - No import of server.py (acyclic: family modules must not import server).

Module / tool name collision: ``engram_add_observation`` and
``engram_add_observation_batch`` are @mcp.tool-decorated names in server.py.
Importing this module as a bare name would shadow them at delegation time.
Caller (server.py) uses the alias form::

    import engram_observation as _observation_mod

_obs_creator wiring (#918 predicted, wave 9 delivers):
  server.py's wave-6 compat forwarder for _retract_impl injects
  ``_obs_creator=_add_observation_impl``.  Before wave 9 that referenced the
  server-resident name (now a compat forwarder itself).  After wave 9 the
  wiring is updated to point at the module source of truth:
  ``_obs_creator=_observation_mod._add_observation_impl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import engram_core as core
from engram_log_emitter import emit_if_initialized

# VALID_QUOTE_TYPES and VALID_SOURCE_CLASSES come from engram_confidence via
# server.py imports.  Family A receives them as arguments or via the function
# signatures it delegates to; it does not need to import engram_confidence
# directly because the validation is done here at the impl level and the
# constants are the same objects (imported once at server startup).
# Import them directly so this module is self-contained and testable in
# isolation.
from engram_confidence import VALID_QUOTE_TYPES, VALID_SOURCE_CLASSES


# ---------------------------------------------------------------------------
# A-local constants
# ---------------------------------------------------------------------------

# DNS errno values that indicate a domain definitively does not exist.
# Computed once at import time — errno codes are fixed constants, not runtime
# state.  Used only by _add_evidence_impl's DNS guard.
NXDOMAIN_ERRNOS = {
    socket.EAI_NONAME,
    getattr(socket, "EAI_NODATA", -5),  # not defined on all platforms
}


# ---------------------------------------------------------------------------
# A-local helpers
# ---------------------------------------------------------------------------

def _get_polarity_config() -> dict:
    """Get NLI polarity-dedup config (config.json `polarity` section).

    Returns dict with keys: enabled (bool), model (str), threshold (float),
    min_similarity_for_check (float). Falls back to defaults if config
    absent.

    `enabled` defaults to FALSE as of 2026-05-15 (per Lei: polarity
    parallel-mode — feature is not fully calibrated yet, the bake-off winner is
    a 1.5GB GPU model that's slow/unavailable on Macs without GPU, and
    issue #106 documents a high false-positive "wolf-cry" rate on real
    usage. Opt-in for users who explicitly want it AND have the hardware).
    Disabled if `ENGRAM_NO_POLARITY` env var is set (test-mode escape
    hatch so tests don't have to download a 1.5GB model).

    To enable: set `polarity.enabled = true` in `~/.engram/config.json`.
    """
    defaults = {
        "enabled": False,
        "model": core.POLARITY_DEFAULT_MODEL,
        "threshold": core.POLARITY_DEFAULT_THRESHOLD,
        "min_similarity_for_check": core.POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK,
    }
    if os.environ.get("ENGRAM_NO_POLARITY"):
        defaults["enabled"] = False
        return defaults
    if core.CONFIG_PATH.exists():
        try:
            config = json.loads(core.CONFIG_PATH.read_text())
            user = config.get("polarity", {})
            if isinstance(user, dict):
                merged = dict(defaults)
                merged.update(user)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _compute_polarity_alerts(
    new_claim: str, candidates: list
) -> dict:
    """Batched polarity scoring for the action-hint loop.

    Args:
        new_claim: The claim being added.
        candidates: List of dicts with at least 'id', 'claim', 'similarity'
            (the same shape as similar_matches in _add_observation_impl).

    Returns:
        Dict mapping candidate id → POLARITY_ALERT prefix string for any
        candidate where polarity NLI fired. Candidates not in the dict had
        no alert (either filtered by sim floor, polarity disabled, or NLI
        score below threshold). All remaining candidates are scored in a
        single batched forward pass.

    Filtering preserves the same skip-on-floor optimization the per-pair
    path had: candidates with sim < min_similarity_for_check are excluded
    from the batch entirely, so we never pay NLI inference cost on truly
    unrelated pairs.

    Safe-fail: if polarity is disabled, the model fails to load, or
    score_batch returns None, returns an empty dict.
    """
    cfg = _get_polarity_config()
    # Fallback default is False to match the policy default in
    # _get_polarity_config(). In practice this is dead code because that
    # function always populates "enabled" from its defaults dict, but
    # keeping the fallback semantically aligned protects against a future
    # refactor that returns a partial dict.
    if not cfg.get("enabled", False):
        return {}
    min_sim = float(cfg.get("min_similarity_for_check", core.POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK))
    threshold = float(cfg.get("threshold", core.POLARITY_DEFAULT_THRESHOLD))
    model_name = cfg.get("model", core.POLARITY_DEFAULT_MODEL)

    eligible = []
    for c in candidates:
        sim = c.get("similarity")
        if sim is None or sim < min_sim:
            continue
        if not c.get("claim"):
            continue
        eligible.append(c)
    if not eligible:
        return {}

    pairs = [(new_claim, c["claim"]) for c in eligible]
    scores = core._nli_classifier.score_batch(pairs, model_name)
    if scores is None or len(scores) != len(eligible):
        return {}

    alerts = {}
    for c, p_contra in zip(eligible, scores):
        # score_batch returns floats only (None case caught above) — no
        # None-element check needed here.
        if p_contra < threshold:
            continue
        alerts[c["id"]] = (
            f"POLARITY_ALERT — NLI flags potential contradiction (p={p_contra:.2f}). "
            f"Consider engram_contradict if this claim genuinely contradicts. "
        )
    return alerts


def _extract_domain(url: str) -> str:
    """Extract domain from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _check_yellow_card(domain: str):
    """Return the matching yellow-card config entry (dict) or None.

    Yellow-carded domains are sources we've judged unreliable as independent
    evidence (AI-aggregators, content farms, known-hallucinating surfaces).
    Populated in ~/.engram/config.json under `yellow_domains` as a list of
    {domain, reason, engram_node} entries. Suffix-matched like trust_pool so
    subdomains inherit the flag.
    """
    if not domain:
        return None
    try:
        config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
    except Exception:
        return None
    for entry in config.get("yellow_domains", []):
        if isinstance(entry, dict) and entry.get("domain") and domain.endswith(entry["domain"]):
            return entry
    return None


def _format_yellow_warning(match: dict) -> str:
    """Format a yellow-card warning string for inclusion in tool responses."""
    d = match.get("domain", "?")
    reason = (match.get("reason", "flagged unreliable") or "").rstrip(".")
    ref = match.get("engram_node", "")
    msg = f"YELLOW-CARD SOURCE: '{d}' — {reason}."
    if ref:
        msg += f" See {ref} for rationale."
    msg += " Track down to primary sources before treating observations rooted here as independent evidence."
    return msg


def _normalize_for_equivalence(text: str) -> str:
    """Normalize text for equivalence-class quote matching (#1287).

    Applied SYMMETRICALLY to both the quote and the source, so it can only
    forgive presentational differences — never let a paraphrase pass, since
    differing words survive normalization. Folds: Unicode NFC; curly→straight
    quotes; em/en/figure-dash→hyphen; non-breaking & thin unicode spaces→space;
    zero-width space→removed; all whitespace runs (incl. newlines/tabs)→single
    space; strips ends.

    NOTE: deliberately does NOT support elision/ellipsis skipping — see #1287.
    """
    import unicodedata
    import re
    t = unicodedata.normalize("NFC", text)
    # Curly / typographic quotes → ASCII
    t = t.translate({
        0x2018: 0x27, 0x2019: 0x27, 0x201A: 0x27, 0x201B: 0x27,  # ' ' ‚ ‛ -> '
        0x2032: 0x27,                                              # prime ′ -> '
        0x201C: 0x22, 0x201D: 0x22, 0x201E: 0x22, 0x201F: 0x22,  # " " „ ‟ -> "
        0x2033: 0x22,                                              # double prime ″ -> "
    })
    # Dashes → hyphen-minus
    t = t.translate({0x2012: 0x2D, 0x2013: 0x2D, 0x2014: 0x2D, 0x2015: 0x2D})
    # Non-breaking / specialty spaces → regular space; zero-width → removed
    t = (t.replace(" ", " ")   # NO-BREAK SPACE
          .replace(" ", " ")   # NARROW NO-BREAK SPACE
          .replace(" ", " ")   # THIN SPACE
          .replace(" ", " ")   # FIGURE SPACE
          .replace("​", ""))   # ZERO WIDTH SPACE
    # Collapse all whitespace runs (incl. newlines, tabs) to a single space; strip
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _escape_for_source(text: str, file_path: str) -> str:
    """Escape text to match how it appears in a source file's raw content.

    Different file formats encode special characters differently.
    This function transforms human-readable text into the escaped form
    that would appear in the raw file, enabling direct substring search
    without parsing the entire file.

    Uses ensure_ascii=False so non-ASCII characters (em dashes, accented
    letters, CJK, etc.) are preserved as UTF-8 — matching modern JSON
    writers. The verify function tries the ASCII-escaped form as a fallback.
    """
    if file_path.endswith((".jsonl", ".json")):
        # JSON escaping: quotes become \", newlines become \\n, etc.
        # json.dumps wraps in quotes — strip them to get the inner escaped form.
        # ensure_ascii=False preserves Unicode chars as literal UTF-8.
        return json.dumps(text, ensure_ascii=False)[1:-1]
    # Future: XML/HTML escaping, etc.
    return text  # Plain text, markdown, code — no escaping needed


def _decode_from_source(text: str, file_path: str) -> str:
    """Decode escaped text from a source file back to human-readable form.

    Reverse of _escape_for_source: takes a substring extracted from a raw
    file and decodes format-specific escaping back to plain text.

    Uses regex instead of json.loads because the input is a partial fragment
    (not a valid JSON string) — it may contain structural characters like
    "},{"uuid": that would cause json.loads to fail.
    """
    if file_path.endswith((".jsonl", ".json")):
        import re
        _JSON_UNESC = {
            '"': '"', '\\': '\\', '/': '/', 'n': '\n',
            'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
        }
        return re.sub(
            r'\\(.)',
            lambda m: _JSON_UNESC.get(m.group(1), '\\' + m.group(1)),
            text,
        )
    return text


def _find_near_matches(
    content: str, escaped: str, file_path: str, max_matches: int = 3,
    context_chars: int = 150,
) -> list[dict]:
    """Find near matches for a quote in file content.

    Splits the escaped search text into words, finds the longest consecutive
    word subsequence that appears in the content, and returns surrounding
    context. Results are decoded back to human-readable form.
    """
    words = escaped.split()
    if not words:
        return []

    # Try progressively shorter consecutive word sequences
    matches = []
    for length in range(len(words), 0, -1):
        if matches:
            break
        for start in range(len(words) - length + 1):
            fragment = " ".join(words[start:start + length])
            idx = content.find(fragment)
            if idx != -1 and len(fragment) >= 15:
                # Extract context around the match
                ctx_start = max(0, idx - context_chars)
                ctx_end = min(len(content), idx + len(fragment) + context_chars)
                raw_context = content[ctx_start:ctx_end]
                # Trim to word boundaries to avoid partial escape sequences
                if ctx_start > 0:
                    space = raw_context.find(" ")
                    if space != -1 and space < context_chars:
                        raw_context = raw_context[space + 1:]
                if ctx_end < len(content):
                    space = raw_context.rfind(" ")
                    if space != -1 and space > len(raw_context) - context_chars:
                        raw_context = raw_context[:space]
                decoded = _decode_from_source(raw_context, file_path)
                matches.append({
                    "matched_words": length,
                    "total_words": len(words),
                    "context": decoded,
                })
                if len(matches) >= max_matches:
                    break
    return matches


# Content-addressed evidence archive root (#1253). Mirrors the engram-snapshot
# CLI's resolution exactly: $ENGRAM_EVIDENCE_ARCHIVE override, else
# $ENGRAM_HOME/evidence-archive. Files here are named by their sha256 content
# hash, which makes them immutable-by-construction.
EVIDENCE_ARCHIVE_ROOT = (
    os.environ.get("ENGRAM_EVIDENCE_ARCHIVE")
    or str(core.DATA_DIR / "evidence-archive")
)


def _content_addressed_archive_hash(abs_file: str) -> str:
    """If abs_file is a content-addressed evidence-archive file whose name
    matches its actual content hash, return that hash; else return "".

    Content-addressing provides the SAME immutability guarantee the
    committed-before-cite guard exists for, by a different mechanism: the
    filename IS the sha256 of the bytes, so the cited content can never change
    without the path changing too. This lets the deferred-commit path
    (#1253, Lei's ruling) cite an archive file at content_hash time — before
    the git commit that backfills its git_sha at nap — WITHOUT losing the
    immutability the guard protects. The check is scoped to the archive root,
    so all OTHER file:// citations (working-tree files whose bytes can mutate)
    keep the commit guard unchanged.
    """
    try:
        # realpath (not abspath) resolves symlinks, so the scope check enforces
        # the tighter invariant "the actual bytes live inside the archive root"
        # — a symlink planted in the archive that points outside is rejected.
        archive_root = os.path.realpath(EVIDENCE_ARCHIVE_ROOT)
        af = os.path.realpath(abs_file)
        # Must live under the archive root (reject path-traversal / symlink-out).
        if os.path.commonpath([archive_root, af]) != archive_root:
            return ""
        # Basename stem must be a 64-char lowercase-hex sha256.
        stem = os.path.basename(af).split(".", 1)[0]
        if len(stem) != 64 or any(c not in "0123456789abcdef" for c in stem):
            return ""
        # And it must actually hash to its name (cheap re-verification; this is
        # the integrity gate that replaces the commit-freeze for archive files).
        with open(af, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()
        return actual if actual == stem else ""
    except (OSError, ValueError):
        return ""


def _capture_file_version(url: str, content_hash: str = "", git_sha: str = "") -> dict:
    """Validate a file:// URL is citable and capture its version state.

    Used by both evidence creation (for the "must be committed before cite"
    guard) and observation creation (for the per-observation version metadata
    introduced by the evidence-block refactor derivation). Replaces the old inline block in engram_add_evidence.

    Behavior:
      - Non file:// URLs: returns ok=True, no version data, no validation here.
      - file:// URLs that don't exist: returns error.
      - file:// URLs outside any git repo: returns ok=True, no version data
        (versioning enforcement only applies inside git).
      - file:// URLs in a git repo with uncommitted changes (untracked,
        modified, staged): returns error explaining what's wrong.
      - file:// URLs in a git repo, clean: returns ok=True with computed
        content_hash (sha256 of working-tree file) and git_sha (HEAD if not
        provided). If git_sha was provided, verifies the file exists at that
        revision; returns error if not.

    Returns a dict with keys:
      - ok (bool): True if validation passed
      - error (str | None): error message if ok=False
      - content_hash (str): sha256 hash, or "" if not applicable
      - git_sha (str): commit SHA, or "" if not applicable
    """
    _HONESTY_REMINDER = (
        "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
        "and every future step built on it. If the real source is hard to find, "
        "raise the problem transparently — never solve it with a shortcut. (the provenance axiom)"
    )

    if not url.startswith("file://"):
        return {"ok": True, "error": None, "content_hash": "", "git_sha": ""}

    file_path = url[7:]
    if not os.path.isabs(file_path):
        return {
            "ok": False,
            "error": (
                "file:// evidence URL must be an absolute path; got relative "
                f"'{file_path}'. A relative path is cwd-dependent and not stably "
                "re-verifiable — cite the absolute path." + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }
    if not os.path.exists(file_path):
        return {
            "ok": False,
            "error": (
                f"File not found: '{file_path}'. Evidence URLs with file:// must "
                "point to existing files. If citing a conversation, use the "
                "JSONL transcript file path instead." + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }

    file_dir = os.path.dirname(os.path.abspath(file_path))
    _file_git = lambda *args: subprocess.run(
        [core.GIT_EXE, *args], cwd=file_dir, capture_output=True, text=True, timeout=core.GIT_TIMEOUT
    )
    try:
        repo_check = _file_git("rev-parse", "--is-inside-work-tree")
        file_in_git_repo = repo_check.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        file_in_git_repo = False

    if not file_in_git_repo:
        return {"ok": True, "error": None, "content_hash": "", "git_sha": ""}

    # Deferred-commit relaxation (#1253): a content-addressed evidence-archive
    # file is immutable by construction (its name IS the sha256 of its bytes),
    # so it is safe to cite regardless of git commit state — the content can't
    # change without the path changing. Accept it now at content_hash; its
    # git_sha backfills at the next nap commit. Checked BEFORE the status/HEAD
    # logic on purpose: an uncommitted archive file would otherwise be rejected
    # by the cat-file HEAD check below. Scoped to the archive root, so every
    # other file:// citation keeps the committed-before-cite guard unchanged.
    ca_hash = _content_addressed_archive_hash(file_path)
    if ca_hash:
        return {"ok": True, "error": None, "content_hash": ca_hash, "git_sha": ""}

    repo_root_result = _file_git("rev-parse", "--show-toplevel")
    repo_root = repo_root_result.stdout.strip()
    abs_file = os.path.abspath(file_path)
    rel_path = os.path.relpath(abs_file, repo_root)

    status_result = _file_git("status", "--porcelain", "--", abs_file)
    file_status = status_result.stdout.strip()
    if file_status:
        status_code = file_status[:2]
        if status_code == "??":
            status_desc = "untracked (not yet added to git)"
        elif "M" in status_code:
            status_desc = "modified but not committed"
        elif "A" in status_code:
            status_desc = "staged but not committed"
        else:
            status_desc = f"uncommitted (status: {status_code.strip()})"
        return {
            "ok": False,
            "error": (
                f"File is {status_desc}: '{rel_path}'. "
                "File-based evidence must be committed to git before it can be cited. "
                "This ensures the evidence is verifiable at the claimed revision. "
                "Commit the file first, then retry with the new git SHA."
                + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }

    if not content_hash:
        with open(abs_file, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()

    if not git_sha:
        head_result = _file_git("rev-parse", "HEAD")
        if head_result.returncode == 0:
            git_sha = head_result.stdout.strip()

    if git_sha:
        verify_result = _file_git("cat-file", "-e", f"{git_sha}:{rel_path}")
        if verify_result.returncode != 0:
            return {
                "ok": False,
                "error": (
                    f"File '{rel_path}' does not exist at git revision {git_sha[:8]}. "
                    "The git_sha must point to a commit that contains this file. "
                    "Check that you committed the file before this revision."
                    + _HONESTY_REMINDER
                ),
                "content_hash": "",
                "git_sha": "",
            }

    return {"ok": True, "error": None, "content_hash": content_hash, "git_sha": git_sha}


def _verify_quote_in_source(url: str, quoted_text: str) -> Optional[str]:
    """Verify that quoted_text exists in the evidence source file.

    For file:// URLs, reads the file and checks for exact substring match.
    The quoted_text is escaped according to the file format (e.g. JSON
    escaping for .jsonl files) before searching, so the agent can provide
    human-readable text without worrying about format-specific escaping.
    For https:// URLs, verification is skipped (no page fetch at creation time).

    On failure, attempts to find near matches and includes them in the error
    message so the agent can correct the quote in a single round-trip.

    Returns None if verification passes or is not applicable,
    or an error string if the quote is not found.
    """
    if not url.startswith("file://"):
        return None  # Cannot verify remote URLs

    file_path = url[7:]
    if not os.path.isabs(file_path):
        _HONESTY_REMINDER = (
            "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
            "and every future step built on it. If the real source is hard to find, "
            "raise the problem transparently — never solve it with a shortcut. (the provenance axiom)"
        )
        return (
            f"file:// evidence URL must be an absolute path; got relative '{file_path}'. "
            "A relative path is cwd-dependent and not stably re-verifiable — cite the absolute path."
            + _HONESTY_REMINDER
        )
    if not os.path.exists(file_path):
        return None  # File existence is checked separately

    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read evidence file for quote verification: {e}"

    # Try multiple escape strategies — different JSON writers use different
    # Unicode encoding (ensure_ascii=True → — vs False → literal —).
    escaped = _escape_for_source(quoted_text, file_path)
    if escaped in content:
        return None  # Found with primary (UTF-8) escape

    # Fallback: try ASCII-escaped form for files written with ensure_ascii=True
    if file_path.endswith((".jsonl", ".json")):
        ascii_escaped = json.dumps(quoted_text, ensure_ascii=True)[1:-1]
        if ascii_escaped != escaped and ascii_escaped in content:
            return None  # Found with ASCII escape

    # Last resort: equivalence-normalized comparison (#1287). Subsumes NFC and
    # additionally forgives presentational differences (whitespace runs, curly vs
    # straight quotes, em/en-dashes, non-breaking spaces). Symmetric — forgives
    # presentation only, cannot pass a paraphrase (differing words survive).
    if _normalize_for_equivalence(quoted_text) in _normalize_for_equivalence(content):
        return None  # Found via equivalence normalization

    # All strategies failed — try to find near matches for the agent
    near = _find_near_matches(content, escaped, file_path)
    msg = (
        f"Quoted text not found in '{os.path.basename(file_path)}'. "
        "The quoted_text must be a verbatim substring of the evidence source. "
        "Do not paraphrase, summarize, or fabricate quotes.\n\n"
        "BEFORE RETRYING: check the engram-observe skill (Step A.4) for the "
        "pre-verification script. For JSONL files, messages may not have flushed "
        "yet — verify the quote exists in the file BEFORE calling this tool. "
        "Run: ~/.engram/tools/verify_quote.py <file> <phrase>"
    )
    if near:
        suggestions = []
        for i, m in enumerate(near, 1):
            suggestions.append(
                f"  {i}. ({m['matched_words']}/{m['total_words']} words matched) "
                f"...{m['context']}..."
            )
        msg += "\n\nNearest matches in the source:\n" + "\n".join(suggestions)
        msg += (
            "\n\nCopy the exact text from a match above as your quoted_text. "
            "Trim to the relevant passage."
        )
    return msg


# ---------------------------------------------------------------------------
# A-family impls
# ---------------------------------------------------------------------------

def _add_evidence_impl(
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    content_snippet: str = "",
) -> str:
    """Internal implementation — see engram_add_evidence for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Register a new source document (webpage, article, data release) in the
    knowledge graph. Creates an Evidence node representing the raw source
    material. Evidence nodes are immutable, URL-keyed references — they record
    that a specific source exists, not what it means or what version was read.
    Per-observation versioning (git_sha + content_hash) lives on observations,
    not evidence (the evidence-block refactor derivation).

    Same URL → same evidence node, regardless of content changes. For file-based
    sources, the file must be committed to git before it can be cited (the
    "committed-before-cite" guard remains enforced); the specific revision read
    is recorded on each observation that cites this evidence.

    Args:
        url: Canonical URL of the source document. For local files, use
             'file://path/to/file' format.
        title: Article or page title.
        domain: Source domain (e.g. 'reuters.com'). Auto-extracted from URL if omitted.
        source_date: Publication or byline date of the source (ISO format, e.g. '2026-03-20').
                     Extract from the article's dateline or byline when available.
        content_snippet: Optional truncated excerpt of relevant content for offline re-reading.

    Returns:
        JSON with the evidence node ID and trust pool status.
    """
    if not url or not url.strip():
        return json.dumps({"error": "url is required and cannot be empty."})
    if not title or not title.strip():
        return json.dumps({"error": "title is required and cannot be empty."})
    conn = core._get_db()
    try:
        # Compute domain + yellow-card match early so every success return path
        # can surface the warning (short-circuit reuse returns included).
        if not domain:
            domain = _extract_domain(url)
        _yellow_match = _check_yellow_card(domain)

        # Check for existing URL — same URL → same evidence node, always.
        # Per-observation versioning (the evidence-block refactor derivation) means content changes don't fork
        # evidence; the new revision is recorded on the observation citing it.
        existing = conn.execute(
            "SELECT id FROM nodes WHERE source_url = ? AND type = 'evidence'"
            " ORDER BY created_at DESC LIMIT 1",
            (url,)
        ).fetchone()
        if existing:
            reuse = {
                "status": "already_exists",
                "evidence_id": existing["id"],
                "message": f"Evidence node {existing['id']} already registered for this URL.",
            }
            if _yellow_match:
                reuse["yellow_card_warning"] = _format_yellow_warning(_yellow_match)
            return json.dumps(reuse)

        # Validate evidence URL resolvability
        RECOGNIZED_SCHEMES = ("http://", "https://", "file://")
        _HONESTY_REMINDER = (
            "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
            "and every future step built on it. If the real source is hard to find, "
            "raise the problem transparently — never solve it with a shortcut. (the provenance axiom)"
        )
        if url.startswith("file://"):
            # File:// URLs: enforce the committed-before-cite guard. Do NOT
            # store the version state on the evidence — that lives on each
            # citing observation now.
            ver = _capture_file_version(url)
            if not ver["ok"]:
                return json.dumps({"error": ver["error"]})
        elif url.startswith("http://") or url.startswith("https://"):
            # --- Validation for web URLs ---
            parsed_url = urlparse(url)
            hostname = parsed_url.hostname or ""

            # Check 1: Structural — must have a valid hostname
            if not hostname:
                return json.dumps({
                    "error": f"Malformed URL '{url}' — no hostname found. "
                    "Evidence URLs must point to real, resolvable web addresses."
                    + _HONESTY_REMINDER
                })

            # Check 2: TLD — hostname must contain a dot (rejects fabricated
            # single-label hosts like 'session-transcript', 'conversation')
            if "." not in hostname:
                return json.dumps({
                    "error": f"Invalid hostname '{hostname}' in URL — no TLD found. "
                    "This looks like a fabricated domain. Real web URLs have a domain "
                    "with a TLD (e.g., 'arxiv.org', 'reuters.com')."
                    + _HONESTY_REMINDER
                })

            # Check 3: DNS resolution — verify the domain actually exists.
            # If the agent is citing an online source, network should be available.
            # A DNS failure almost certainly means a fabricated or hallucinated domain.
            try:
                socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except socket.gaierror as e:
                # Only block on definitive name-not-found errnos.
                # EAI_NONAME / EAI_NODATA = the domain truly doesn't exist.
                # EAI_AGAIN / EAI_FAIL = transient DNS infra; treat like timeout (pass).
                if e.errno in NXDOMAIN_ERRNOS:
                    return json.dumps({
                        "error": f"DNS resolution failed for '{hostname}' — this domain does not exist. "
                        "The URL may be fabricated or hallucinated. Verify you have the correct URL "
                        "from a real source before citing it as evidence."
                        + _HONESTY_REMINDER
                    })
                # Transient — fall through to silent pass (same as the OSError branch).
            except (socket.timeout, OSError):
                # Network issues — warn but don't block, since the domain might be real
                pass

        elif not any(url.startswith(s) for s in RECOGNIZED_SCHEMES):
            return json.dumps({
                "error": f"Unrecognized URL scheme in '{url}'. Evidence URLs must be resolvable — "
                "use file:// for local files or https:// for web sources. "
                "Do not invent synthetic schemes (e.g., conversation://, measurement://)."
                + _HONESTY_REMINDER
            })

        # Check trust pool (domain already computed above for yellow-card)
        config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
        trust_pool = config.get("trust_pool", [])
        in_trust_pool = any(domain.endswith(tp) for tp in trust_pool)

        node_id = core._next_id(conn, "evidence")
        now = core._now()

        # Auto-tag source_type from URL pattern (orthogonal to source_class).
        # Used by the dedup heuristic to grade action_hints by artifact kind.
        source_type = core._infer_source_type(url)

        conn.execute(
            """INSERT INTO nodes (id, type, created_at, source_url, source_title,
               source_domain, source_date, source_accessed, content_snippet, metadata,
               source_type)
               VALUES (?, 'evidence', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, now, url, title, domain, source_date or None, now,
             content_snippet or None, "{}",
             source_type),
        )

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        conn.commit()

        result = {
            "status": "created",
            "evidence_id": node_id,
            "domain": domain,
            "source_date": source_date or None,
            "in_trust_pool": in_trust_pool,
        }
        if trust_pool and not in_trust_pool:
            result["warning"] = (
                f"Domain '{domain}' is NOT in the trusted source pool. "
                "Consider whether this source meets your quality standards."
            )
        if _yellow_match:
            result["yellow_card_warning"] = _format_yellow_warning(_yellow_match)
        return json.dumps(result)
    finally:
        conn.close()


def _add_observation_impl(
    quoted_text: str = "",
    interpretation: str = "",
    claim: str = "",
    quote_type: str = "",
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    is_predictive: bool = False,
    predicted_event: str = "",
    resolution_timeframe: str = "",
    source_class: str = "external",
    content_hash: str = "",
    git_sha: str = "",
    standpoint_author_id: str = "",
    standpoint_collection_id: str = "",
    standpoint_override_tag: str = "",
    standpoint_lineage: str = "",
    standpoint_architecture: str = "",
    fs_class=None,
) -> str:
    """Internal implementation — see engram_add_observation MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers (engram_add_observation_batch, engram_retract replacement path).
    """
    # Per the bool-string-truthy lesson: bool-string truthy trap at wrapper boundary. JSON-string
    # "false" is truthy in Python, so an `is_predictive: "false"` payload
    # would silently create an observation_predictive node. Same fix shape
    # as PR #68 (is_self). Check here at the impl entry so both the
    # single-observation wrapper AND the batch processor get protection.
    if not isinstance(is_predictive, bool):
        return json.dumps({
            "error": (
                f"is_predictive must be a JSON boolean (true/false), got "
                f"{type(is_predictive).__name__}: {is_predictive!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })

    missing = [
        name for name, val in (
            ("quoted_text", quoted_text),
            ("interpretation", interpretation),
            ("claim", claim),
            ("quote_type", quote_type),
        ) if not val
    ]
    if missing:
        return json.dumps({
            "error": f"Missing required fields: {', '.join(missing)}"
        })
    if quote_type not in VALID_QUOTE_TYPES:
        return json.dumps(
            {
                "error": f"Invalid quote_type '{quote_type}'. Must be one of: {', '.join(sorted(VALID_QUOTE_TYPES))}"
            }
        )

    if source_class not in VALID_SOURCE_CLASSES:
        return json.dumps(
            {
                "error": f"Invalid source_class '{source_class}'. Must be one of: {', '.join(sorted(VALID_SOURCE_CLASSES))}"
            }
        )

    # standpoint_lineage format gate (v3): provider:family, lowercase. The
    # error REDIRECTS (names the expected shape + example) rather than
    # bare-rejecting; reject-never-normalize keeps the cluster hash a filing
    # convention the agent can see, not a silent transformation.
    # SSoT: uses core._LINEAGE_RE — the single compiled regex for lineage
    # validity shared with _self_lineage() (#960).
    if standpoint_lineage and not core._LINEAGE_RE.fullmatch(standpoint_lineage):
        return json.dumps(
            {
                "error": (
                    f"standpoint_lineage must match provider:family "
                    f'(e.g. "anthropic:opus"); got "{standpoint_lineage}"'
                )
            }
        )

    # standpoint_architecture enum gate: reject unknown values up-front so the
    # DB only ever holds the declared enum members. Redirecting error names the
    # accepted set so the caller knows what to fix.
    _ARCH_ENUM = {
        "transformer", "vision-spatial", "embodied-sensorimotor",
        "graph-neural", "human", "other",
    }
    if standpoint_architecture and standpoint_architecture not in _ARCH_ENUM:
        return json.dumps(
            {
                "error": (
                    f"standpoint_architecture must be one of "
                    f"{sorted(_ARCH_ENUM)}; got \"{standpoint_architecture}\""
                )
            }
        )

    # fs_class validation (Phase 2): accepted values only, per D1 filing-time contract.
    _VALID_FS_CLASSES = ("re-executable", "frozen")
    if fs_class is not None and fs_class not in _VALID_FS_CLASSES:
        return json.dumps({
            "error": (
                f"Invalid fs_class '{fs_class}'. "
                f"Accepted values: {', '.join(repr(v) for v in _VALID_FS_CLASSES)} "
                "or omit / pass null for Phase-1 proxy fallback."
            )
        })

    # Resolve evidence node: either from evidence_id or auto-create from url+title
    if not evidence_id and not url:
        return json.dumps(
            {"error": "Provide either 'url' (+ 'title') to auto-create an evidence node, or 'evidence_id' to cite an existing one."}
        )

    if not evidence_id and url and not title:
        return json.dumps(
            {"error": "'title' is required when providing 'url' for auto-creating an evidence node."}
        )

    _yellow_forward = None
    if not evidence_id and url:
        # Auto-create or reuse evidence node
        ev_result = json.loads(_add_evidence_impl(
            url=url, title=title, domain=domain, source_date=source_date,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        evidence_id = ev_result["evidence_id"]
        _yellow_forward = ev_result.get("yellow_card_warning")

    conn = core._get_db()
    try:
        # Validate evidence exists
        ev = conn.execute(
            "SELECT id, source_url FROM nodes WHERE id = ? AND type = 'evidence'",
            (evidence_id,),
        ).fetchone()
        if not ev:
            return json.dumps(
                {"error": f"Evidence node '{evidence_id}' not found."}
            )

        # Verify quoted_text exists in the evidence source (file:// only)
        ev_url = ev["source_url"] or ""
        quote_error = _verify_quote_in_source(ev_url, quoted_text)
        if quote_error:
            return json.dumps({"error": quote_error})

        # Capture per-observation file version (the evidence-block refactor derivation). For file:// URLs
        # in a git repo: enforce committed-before-cite + record the specific
        # revision read on this observation. For other URLs: no-op.
        ver = _capture_file_version(ev_url, content_hash=content_hash, git_sha=git_sha)
        if not ver["ok"]:
            return json.dumps({"error": ver["error"]})
        captured_content_hash = ver["content_hash"]
        captured_git_sha = ver["git_sha"]

        node_type = "observation_predictive" if is_predictive else "observation_factual"
        confidence = core._compute_confidence(
            conn, node_type, quote_type=quote_type, is_predictive=is_predictive,
            source_class=source_class,
        )

        # Similarity check — run BEFORE insert so the new node doesn't
        # match itself. Results are advisory (non-blocking). Helper
        # extracted 2026-05-14 (#143 §3.1) — same shape used by
        # engram_add_lesson; the importance-floor-in-FTS divergence
        # between callers was accidental drift and is now uniform.
        similar_matches = core._similar_existing_matches(
            conn, claim,
            type_filter={"observation_factual", "observation_predictive"},
            extra_columns=("evidence_id",),
        )

        node_id = core._next_id(conn, node_type)
        now = core._now()
        # quote_verified: True when quoted_text was confirmed an exact substring of
        # the file:// source; False for https://, http://, empty source_url
        # (evidence_id-only path), or any other non-file:// scheme (no fetch occurs
        # at creation time). Marks provenance quality on the node itself so
        # inspect + recall surfaces distinguish verified from unverified quotes.
        # See #1204.
        quote_verified = ev_url.startswith("file://")
        obs_metadata = {"source_class": source_class, "quote_verified": quote_verified}
        if captured_git_sha:
            obs_metadata["git_sha"] = captured_git_sha
        if captured_content_hash:
            obs_metadata["content_hash"] = captured_content_hash
        metadata = json.dumps(obs_metadata)
        conf_reason = f"Initial: {quote_type}"
        if source_class != "external":
            conf_reason += f" (source_class: {source_class})"

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, evidence_id, quoted_text,
               interpretation, quote_type, confidence, confidence_history, metadata,
               standpoint_author_id, standpoint_collection_id, standpoint_override_tag,
               standpoint_lineage, standpoint_architecture, fs_class)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                node_type,
                claim,
                now,
                evidence_id,
                quoted_text,
                interpretation,
                quote_type,
                confidence,
                json.dumps([{"timestamp": now, "value": confidence, "reason": conf_reason}]),
                metadata,
                standpoint_author_id or None,
                standpoint_collection_id or None,
                standpoint_override_tag or None,
                standpoint_lineage or None,
                standpoint_architecture or None,
                fs_class if fs_class in ("re-executable", "frozen") else None,
            ),
        )

        # Edge: observation cites evidence
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
            (node_id, evidence_id, now),
        )

        # Stamp importance
        core._stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

        result = {
            "status": "created",
            "observation_id": node_id,
            "evidence_id": evidence_id,
            "type": node_type,
            "confidence": confidence,
            "source_class": source_class,
            "quote_verified": quote_verified,
        }
        if not quote_verified:
            result["quote_provenance_warning"] = (
                "Web-sourced quoted_text is unverified at encoding — no page fetch "
                "occurs at observation creation time. The quote may not appear verbatim "
                "at the cited URL (paraphrase, page-change, paywall). For high-stakes "
                "citations, verify manually. See #1204."
            )

        # Handle prediction decomposition
        prediction_id = None
        if is_predictive and predicted_event:
            # Check if a prediction node for this event already exists
            existing_pred = conn.execute(
                """SELECT id FROM nodes WHERE type = 'prediction'
                   AND predicted_event = ? AND is_current = 1""",
                (predicted_event,),
            ).fetchone()

            if existing_pred:
                prediction_id = existing_pred["id"]
                result["prediction_status"] = "linked_to_existing"
            else:
                prediction_id = core._next_id(conn, "prediction")
                conn.execute(
                    """INSERT INTO nodes (id, type, predicted_event, resolution_timeframe,
                       status, created_at, confidence)
                       VALUES (?, 'prediction', ?, ?, 'open', ?, NULL)""",
                    (prediction_id, predicted_event, resolution_timeframe or None, now),
                )
                result["prediction_status"] = "created"
                core._log_edit(conn, "created", prediction_id, "prediction",
                          {"predicted_event": predicted_event[:200]})

            # Edge: prediction supported_by observation
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supported_by', ?)",
                    (prediction_id, node_id, now),
                )
            except sqlite3.IntegrityError:
                pass

            result["prediction_id"] = prediction_id

        core._stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

        # Attach similarity results with decision guidance.
        #
        # Action hints are graded by THREE dimensions, not just same_evidence:
        #   1. match_type — keyword (FTS, no similarity score) vs semantic (cosine sim)
        #   2. similarity score — only meaningful for semantic matches
        #   3. source_type of the shared evidence — document vs conversation/file
        #
        # Why: the old binary "same_evidence → DUPLICATE" rule caused alarm
        # fatigue because long conversation/file evidence (chat logs, edited
        # source files) legitimately produces many distinct claims from one
        # source. The new logic only cries DUPLICATE when the source is a
        # discrete document AND similarity is high — the pattern the original
        # rule was actually designed for.
        if similar_matches:
            # Look up the source_type of the current observation's evidence
            own_st_row = conn.execute(
                "SELECT source_type FROM nodes WHERE id = ?", (evidence_id,)
            ).fetchone()
            own_source_type = (
                (own_st_row["source_type"] if own_st_row and own_st_row["source_type"] else None)
                or "document"
            )
            # Hoist config out of the per-candidate loop — for K=15 candidates
            # this avoids 15x core.CONFIG_PATH.read_text() + json.loads() per write.
            _th = core._get_thresholds_config()
            # Pre-compute polarity alerts in a single batched NLI forward pass
            # for ALL eligible candidates (those above min_similarity_for_check).
            # ~33% latency win vs per-candidate single-pair calls per the
            # bake-off v2 measurement (ModernCE-large 16.6ms single → 11.1ms
            # batched on RTX 5090).
            _polarity_alerts = _compute_polarity_alerts(claim, similar_matches)

            for m in similar_matches:
                same_ev = (m.get("evidence_id") == evidence_id)
                m["same_evidence"] = same_ev
                sim = m.get("similarity")  # None for FTS keyword-only matches

                if sim is None:
                    # FTS keyword match: coarse signal, no cosine score
                    if same_ev:
                        m["action_hint"] = (
                            f"KEYWORD_OVERLAP_SAME_SOURCE — keyword match with {m['id']} "
                            f"in the same {own_source_type}. Inspect to decide if duplicate or distinct."
                        )
                    else:
                        m["action_hint"] = (
                            f"KEYWORD_OVERLAP_OTHER_SOURCE — keyword match with {m['id']} "
                            f"from a different source. Inspect to decide if corroboration."
                        )
                    continue

                # Semantic match — graded by similarity AND source_type
                #
                # Discrete artifacts (document, web_page): an author writes
                # once, so high-similarity within the same source is usually a
                # duplicate worth flagging.
                #
                # Long-running sources (conversation, file): many distinct
                # claims legitimately share one source (chat logs, edited code
                # files), so DUPLICATE only fires at very high similarity.
                if same_ev:
                    if own_source_type in ("document", "web_page") and sim >= 0.85:
                        m["action_hint"] = (
                            f"DUPLICATE — high-similarity ({sim:.2f}) claim from the same {own_source_type}. "
                            f"Likely the author repeating themselves. Consider retracting this new observation."
                        )
                    elif own_source_type in ("conversation", "file") and sim >= 0.92:
                        m["action_hint"] = (
                            f"POSSIBLE_DUPLICATE — very-high similarity ({sim:.2f}) "
                            f"with {m['id']} from the same {own_source_type}. "
                            f"Inspect both before deciding — long-running sources can repeat."
                        )
                    else:
                        m["action_hint"] = (
                            f"DISTINCT_FROM_SAME_SOURCE — same {own_source_type} source, similarity {sim:.2f}. "
                            f"Likely a different fact from a long-running source. "
                            f"Create normally if the claim adds new information."
                        )
                else:
                    if sim >= float(_th["action_hint_corroborate"]):
                        m["action_hint"] = (
                            f"CORROBORATE — high-similarity ({sim:.2f}) claim from an independent source. "
                            f"Create a corroboration derivation (inductive_generalization) "
                            f"citing both {m['id']} and {node_id}."
                        )
                    elif sim >= float(_th["action_hint_related"]):
                        m["action_hint"] = (
                            f"RELATED — moderate similarity ({sim:.2f}) from a different source. "
                            f"Inspect to decide whether corroboration is warranted."
                        )
                    else:
                        m["action_hint"] = (
                            f"WEAK_MATCH — low similarity ({sim:.2f}). "
                            f"Probably unrelated. Ignore unless inspection suggests otherwise."
                        )

                # Polarity NLI overlay — applies to BOTH same-source and
                # cross-source semantic matches (per Lei 2026-05-10:
                # "same source still worth marking contradiction"). Prepended
                # to whichever tier hint was set above. Pre-computed in a single
                # batched NLI call above (_compute_polarity_alerts); look up by
                # candidate id here. Candidates filtered out (sim below floor,
                # polarity disabled, model load failure, score below threshold)
                # are simply not in the dict.
                polarity_prefix = _polarity_alerts.get(m["id"])
                if polarity_prefix:
                    m["action_hint"] = polarity_prefix + m["action_hint"]

            result["similar_existing"] = core._strip_similar_block(similar_matches)
            result["similar_count"] = len(similar_matches)
            result["dedup_guidance"] = (
                "Similarity-graded matches. Action hints account for source type "
                "(document vs conversation/file) and similarity score. "
                "DUPLICATE only fires for high-similarity matches in discrete documents."
            )

        if _yellow_forward:
            result["yellow_card_warning"] = _yellow_forward

        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # Emit AFTER commit per the honesty axiom (structural honesty): logging an event
        # that says "this happened" before commit succeeds would risk
        # asserting a falsehood if the commit raises.
        # Decision context: similar-existing hits + dedup hint + result.
        # emit_if_initialized is a silent no-op when the server-side emitter
        # is not yet initialized (Phase 4 will add the init path).
        _similar_count = len(similar_matches)
        _highest_sim = None
        _first_hint = None
        if similar_matches:
            sims = [m.get("similarity") for m in similar_matches if m.get("similarity") is not None]
            if sims:
                _highest_sim = max(sims)
            _first_hint = similar_matches[0].get("action_hint")
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_add_observation",
                "similar_existing_returned_count": _similar_count,
                "highest_similarity": _highest_sim,
                "dedup_action_hint": _first_hint,
                "result_status": result.get("status"),
                "result_node_id": result.get("observation_id"),
                "validation_warnings": (
                    [result["yellow_card_warning"]] if result.get("yellow_card_warning") else []
                ),
            },
        )
        return json.dumps(core._strip_agent_facing(result))
    finally:
        conn.close()


def _add_observation_batch_impl(
    observations_json: str = "",
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    content_hash: str = "",
    git_sha: str = "",
) -> str:
    """Internal implementation — see engram_add_observation_batch MCP tool for
    the public payload schema. Kept callable with named kwargs for in-server
    callers.

    Extract multiple observations from a single source in one call. Use this
    after reading a source holistically — extract ALL substantive claims at
    once rather than calling engram_add_observation repeatedly. All
    observations in the batch cite the same source.

    Source identification: provide EITHER url+title (the evidence node will be
    auto-created or reused) OR evidence_id. If both are provided, evidence_id
    takes precedence.

    Args:
        observations_json: JSON array of observation objects, each with:
            - quoted_text (required): Verbatim quote from the source
            - interpretation (required): Your reasoning about what this means
            - claim (required): Atomic, falsifiable claim
            - quote_type (required): hard_data, official_statement, attributed_analysis, counterfactual_inference, unnamed_source, personal_communication, or editorial
            - is_predictive (optional): boolean, default false
            - predicted_event (optional): if predictive, the event predicted
            - resolution_timeframe (optional): if predictive, ISO date for resolution
            - source_class (optional): 'external' (default), 'introspective', or 'user_stated'
            - content_hash (optional): SHA-256 of file content for file-based evidence
            - git_sha (optional): Git commit SHA for file-based evidence
            - standpoint_author_id (optional): Persistent cross-session entity ID for who produced this observation's source claim ("who observes" axis).
            - standpoint_collection_id (optional): Corpus or work identity for this observation's source ("vantage" axis).
            - standpoint_override_tag (optional): Free-form standpoint label when the cluster key is insufficient.
            - standpoint_lineage (optional): Training lineage "provider:family" (e.g. "anthropic:opus") for the source claim's producer.
            - standpoint_architecture (optional): Cognitive architecture of the source's producer. Enum: transformer | vision-spatial | embodied-sensorimotor | graph-neural | human | other. Tracks architectural (not just training) diversity — Class A calibration exposure.
        url: URL of the source document. The evidence node is auto-created or reused.
        title: Title of the source document (required if url is provided).
        domain: Source domain (auto-extracted from URL if omitted).
        source_date: Publication date in ISO format (e.g. '2026-03-20').
        evidence_id: ID of an existing evidence node. Use this OR url+title, not both.

    Example:
        observations_json = '[{"quoted_text": "Gold fell 9.6%", "interpretation": "Worst weekly decline", "claim": "Gold declined 9.6% this week", "quote_type": "hard_data"}]'

    Returns:
        JSON with list of created observation nodes, their confidence scores, and any predictions.
    """
    try:
        observations = json.loads(observations_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(observations, list):
        return json.dumps({"error": "observations_json must be a JSON array."})

    # Resolve evidence node once for the whole batch
    resolved_evidence_id = evidence_id
    evidence_created = False
    _batch_yellow = None
    if not resolved_evidence_id and url:
        if not title:
            return json.dumps({"error": "'title' is required when providing 'url'."})
        ev_result = json.loads(_add_evidence_impl(
            url=url, title=title, domain=domain, source_date=source_date,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        resolved_evidence_id = ev_result["evidence_id"]
        evidence_created = ev_result["status"] == "created"
        _batch_yellow = ev_result.get("yellow_card_warning")
    elif not resolved_evidence_id:
        return json.dumps({"error": "Provide either 'url' (+ 'title') or 'evidence_id'."})

    results = []
    for i, obs in enumerate(observations):
        if not isinstance(obs, dict):
            results.append({"index": i, "error": "Each observation must be a JSON object."})
            continue

        required = ["quoted_text", "interpretation", "claim", "quote_type"]
        missing = [f for f in required if not obs.get(f)]
        if missing:
            results.append({"index": i, "error": f"Missing required fields: {', '.join(missing)}"})
            continue

        r = json.loads(_add_observation_impl(
            evidence_id=resolved_evidence_id,
            quoted_text=obs["quoted_text"],
            interpretation=obs["interpretation"],
            claim=obs["claim"],
            quote_type=obs["quote_type"],
            is_predictive=obs.get("is_predictive", False),
            predicted_event=obs.get("predicted_event", ""),
            resolution_timeframe=obs.get("resolution_timeframe", ""),
            source_class=obs.get("source_class", "external"),
            content_hash=content_hash,
            git_sha=git_sha,
            standpoint_author_id=obs.get("standpoint_author_id", ""),
            standpoint_collection_id=obs.get("standpoint_collection_id", ""),
            standpoint_override_tag=obs.get("standpoint_override_tag", ""),
            standpoint_lineage=obs.get("standpoint_lineage", ""),
            standpoint_architecture=obs.get("standpoint_architecture", ""),
            fs_class=obs.get("fs_class"),
        ))
        r["index"] = i
        results.append(r)

    created = [r for r in results if r.get("status") == "created"]
    batch_result = {
        "status": "batch_complete",
        "evidence_id": resolved_evidence_id,
        "evidence_created": evidence_created,
        "total": len(observations),
        "created": len(created),
        "results": results,
    }
    if _batch_yellow:
        batch_result["yellow_card_warning"] = _batch_yellow

    # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
    # Batch-level summary event — one event for the entire batch MCP call.
    # Per-observation events are already emitted by _add_observation_impl above.
    emit_if_initialized(
        event_type="engram.tool.engram_call",
        level=1,
        data={
            "tool_name": "engram_add_observation_batch",
            "result_status": "batch_complete",
            "result_node_id": None,
            "batch_total": len(observations),
            "batch_created": len(created),
            "batch_error_count": len(observations) - len(created),
            "validation_warnings": (
                [batch_result["yellow_card_warning"]]
                if batch_result.get("yellow_card_warning")
                else []
            ),
        },
    )

    return json.dumps(core._strip_agent_facing(batch_result))
