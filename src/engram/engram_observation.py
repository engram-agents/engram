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
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import engram_core as core
from engram_anchoring import anchor as _anchor_claim
from engram_anchoring import quote_claim_similarity as _quote_claim_similarity
from engram_log_emitter import Emitter, emit_if_initialized

# alpha #2574 WS5: engram_telemetry_store.py ships at tier=convenience (WS1's
# own tiering judgment call -- src/build/packaging/tiers.json), while THIS
# file ships at tier=essential. A plain top-level import here would make an
# essential-tier module hard-depend on a convenience-tier one, breaking an
# Essential-only build (tests/test_tiers_manifest.py's
# test_import_dep_tier_consistency enforces exactly this). Guarded import +
# no-op stub keeps this module's own tool functions gating-nothing-T1
# (docs/2574-telemetry-design.md §8) even if the store ships without it.
try:
    from engram_telemetry_store import write_telemetry_record
except Exception:  # pragma: no cover -- import-failure fallback
    def write_telemetry_record(manifest_id, event_type, fields=None):
        return False

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

# Stable-ID pattern for standpoint_author_id (#1348 convention).
# Valid forms: pn_\d+ (in-graph person node) or <prefix>:<value> (namespaced
# string such as orcid:…, doi-author:…, domain:handle).  Compiled once at
# import time — same pattern as _LINEAGE_RE in engram_core.
_STABLE_ID_RE = re.compile(r"^pn_\d+$|^[a-z][a-z0-9-]*:.+$")


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


def _get_standpoint_config() -> dict:
    """Get standpoint write-gate config (config.json `standpoint` section).

    Returns dict with key: author_id_hard_gate (bool). Falls back to the
    default if config absent. Same read-with-fallback shape as
    _get_polarity_config — read on demand (no caching) so config edits take
    effect on the next call without a server restart.

    `author_id_hard_gate` defaults to TRUE (#1827 layer 1): the #1348
    warn-not-reject advisory was measured insufficient at n=4 seats (see the
    measurements recorded on issue #1827) — the check exists, renders, and
    the mis-stamps happen anyway. Approved ruling (recorded on #1827): hard
    gate acceptable, cross-standpoint writes are rare, correctness dominates.
    Set to false as a migration-era escape hatch to restore the pre-#1827
    warn-only behavior byte-for-byte.
    """
    defaults = {"author_id_hard_gate": True}
    if core.CONFIG_PATH.exists():
        try:
            config = json.loads(core.CONFIG_PATH.read_text())
            user = config.get("standpoint", {})
            if isinstance(user, dict):
                merged = dict(defaults)
                merged.update(user)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _find_person_repair_hint(conn: sqlite3.Connection, offending: str) -> Optional[str]:
    """Fuzzy-match an offending standpoint_author_id string against current
    person nodes' name + aliases, for the #1827 reject-message repair hint.

    Reuses the same current-person-node scan _add_observation_impl already
    runs for its Stage-1 person-mention suggestion (name/aliases from
    metadata), but compares the WHOLE offending string against each
    candidate name/alias (difflib.get_close_matches) rather than searching
    for a name inside a longer claim — offending is a short standpoint-ID
    string, not a sentence.

    Returns a rendered hint string ("did you mean pn_XXXX (<name>)? Or mint
    one: engram_add_person.") or None if no person node is current, or no
    candidate is close enough to be worth suggesting.
    """
    try:
        person_rows = conn.execute(
            "SELECT id, metadata FROM nodes WHERE type = 'person' AND is_current = 1"
        ).fetchall()
    except sqlite3.Error:
        return None

    candidates: dict[str, str] = {}  # name/alias (lowercase) -> person_id
    display: dict[str, str] = {}  # name/alias (lowercase) -> original-case name for display
    for prow in person_rows:
        pmeta = core._metadata_dict(prow["metadata"])
        pname = pmeta.get("name", "")
        paliases = pmeta.get("aliases", [])
        vocab = [pname] + (paliases if isinstance(paliases, list) else [])
        for term in vocab:
            # Non-string terms (legacy/hand-edited/migration-sourced metadata
            # can carry ints/None/dicts in aliases) must degrade to the plain
            # reject message, never crash the gate (PR #1979 review).
            if not isinstance(term, str) or not term:
                continue
            candidates[term.lower()] = prow["id"]
            display[term.lower()] = pname if isinstance(pname, str) else prow["id"]

    if not candidates:
        return None

    matches = difflib.get_close_matches(
        offending.lower(), candidates.keys(), n=1, cutoff=0.6
    )
    if not matches:
        return (
            "No existing person node's name/aliases resembles this ID. Mint "
            "one: engram_add_person."
        )
    matched_term = matches[0]
    person_id = candidates[matched_term]
    person_name = display[matched_term]
    return f"Did you mean {person_id} ({person_name})? Or mint one: engram_add_person."


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


_MAX_SYMLINK_EXPANSIONS = 40


def _resolve_with_trace(path: str) -> "tuple[str, list[str]]":
    """Resolve `path` exactly as the OS does, recording every symlink crossed.

    Returns (physical_path, links). `path` is made absolute by joining the cwd
    if relative, WITHOUT normpath/abspath: `..` is applied to the PHYSICAL path
    built so far (so `repo/link/../f.md` is the parent of what `link` points at,
    not `repo/f.md`). Each component that is a symlink is recorded as the
    absolute physical path of the LINK itself, then replaced by its target and
    the walk continues (a relative target resolves against the link's
    directory). A missing FINAL component is kept as written (the caller's
    existence check reports it); anything the OS would refuse earlier raises as
    the OS does: OSError(ENOENT) when a component that must be a directory is
    missing, OSError(ENOTDIR) when it exists but is not a directory (a trailing
    slash, `/.` or `/..` after a regular file, or a path continuing past one).
    More than _MAX_SYMLINK_EXPANSIONS links raises OSError(ELOOP), so a loop is
    a clean rejection rather than a hang.
    """
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    pending = path.split(os.sep)[::-1]  # a stack: next component is pending[-1]
    resolved = os.sep
    links: list = []
    while pending:
        comp = pending.pop()
        # Consuming ANY further component (even "", "." or "..") means `resolved`
        # is being used as a directory; the OS refuses when it is not one.
        if not os.path.isdir(resolved):
            code = errno.ENOTDIR if os.path.lexists(resolved) else errno.ENOENT
            raise OSError(code, os.strerror(code), path)
        if comp in ("", "."):
            continue
        if comp == "..":
            resolved = os.path.dirname(resolved)
            continue
        candidate = os.path.join(resolved, comp)
        if os.path.islink(candidate):
            if len(links) >= _MAX_SYMLINK_EXPANSIONS:
                raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)
            links.append(candidate)
            target = os.readlink(candidate)
            if os.path.isabs(target):
                resolved = os.sep
            pending.extend(target.split(os.sep)[::-1])
        else:
            resolved = candidate
    return resolved, links


def _capture_file_version(url: str, content_hash: str = "", git_sha: str = "", source_repo: str = "", repo_path: str = "") -> dict:
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
    try:
        physical, links = _resolve_with_trace(file_path)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            # The OS cannot open this path either: same rejection as a missing file.
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
        return {
            "ok": False,
            "error": (
                f"Cannot resolve the symlinks in '{file_path}': {e.strerror or e} "
                "(a symlink loop, or more than "
                f"{_MAX_SYMLINK_EXPANSIONS} links on the path). Cite a path that resolves."
                + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }
    # Both halves: the cited path as the OS opens it, and the physical path the
    # walk reached. The walk fails where the OS fails; the cited-path check is
    # defense in depth, because _verify_quote_in_source skips the quote check
    # for a cited path that does not exist.
    if not (os.path.exists(file_path) and os.path.exists(physical)):
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

    # Evidence is bytes: the physical path must be a regular file. A directory
    # (or anything else that is not a file) has no content to hash or quote, and
    # `git cat-file -e <sha>:<dir>` would succeed for it (a tree object) — so this
    # fires before any content_hash / open() / git logic, for every caller (#3393).
    if not os.path.isfile(physical):
        return {
            "ok": False,
            "error": (
                f"Evidence must be a file: '{file_path}' is not a regular file "
                f"(it resolves to '{physical}'). Cite a file inside it instead."
                + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }

    cross_repo = bool(source_repo and repo_path)
    # When source_repo is provided, run git operations from that repo root
    # instead of the directory inferred from the file path. This lets the
    # caller verify a file against a secondary repo (e.g. the plugin source
    # tree) when the file's containing directory resolves to a different repo
    # (e.g. the ~/.engram data repo where plugin files are untracked).
    if source_repo:
        git_cwd = os.path.abspath(source_repo)
        if not os.path.isdir(git_cwd):
            return {
                "ok": False,
                "error": f"source_repo '{source_repo}' is not a directory." + _HONESTY_REMINDER,
                "content_hash": "",
                "git_sha": "",
            }
    else:
        # The PHYSICAL directory of the file: the one place the bytes live.
        git_cwd = os.path.dirname(physical)
    _git_at = lambda cwd, *args, env=None: subprocess.run(
        [core.GIT_EXE, *args], cwd=cwd, capture_output=True, text=True, timeout=core.GIT_TIMEOUT,
        env=env,
    )
    # Link classification matches git's English "not a git repository", so it
    # runs under the C locale; the rest of the environment is inherited.
    _c_locale_env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    _file_git = lambda *args: _git_at(git_cwd, *args)

    def _reject(message: str) -> dict:
        return {"ok": False, "error": message + _HONESTY_REMINDER, "content_hash": "", "git_sha": ""}

    def _outside_repo_rejection(real_file: str) -> dict:
        # A committed in-repo symlink whose target lies outside the repo: git
        # cannot vouch for the target, so name the cause rather than let
        # `git status` (exit 128) and cat-file fail on a '../' path.
        return _reject(
            f"File '{file_path}' resolves outside the repository "
            f"(to '{real_file}'). File-based evidence must live inside "
            "the git repository so it is verifiable at the claimed revision. "
            "Cite a file that is committed inside the repo."
        )

    # Link check (#3292). The cited path was resolved ONCE, physically, above;
    # every symlink that resolution crossed is in `links`. A link outside every
    # git work tree is environment (macOS /var -> /private/var, a ~/link) and
    # is ignored. A link inside a work tree W is part of what the citation
    # relies on, so it must be tracked in W and clean there: an untracked or
    # retargeted link could otherwise vouch for committed bytes it merely
    # points at.
    in_tree_links = []  # (link path, work tree root, link path relative to it)
    if not cross_repo:
        for link in links:
            link_dir = os.path.dirname(link)
            try:
                top = _git_at(link_dir, "rev-parse", "--show-toplevel", env=_c_locale_env)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                # Fail closed: an unanswered classification is not "environment".
                return _reject(
                    f"Cannot classify the symlink '{link}' on the cited path: git "
                    f"could not be run ({type(e).__name__}). A link that cannot be "
                    "classified cannot be trusted to vouch for the file it points "
                    "at, so the citation cannot be accepted."
                )
            if top.returncode != 0:
                # Only git's genuine "not a git repository" answer makes a link
                # environment; any other failure must not read as one.
                if "not a git repository" in (top.stderr or "").lower():
                    continue
                git_err = (top.stderr or "").strip()[:300] or "no stderr output"
                return _reject(
                    f"Cannot classify the symlink '{link}' on the cited path: "
                    f"git rev-parse --show-toplevel failed (exit {top.returncode}): "
                    f"{git_err}. A link that cannot be classified cannot be trusted "
                    "to vouch for the file it points at, so the citation cannot be "
                    "accepted."
                )
            work_tree = os.path.realpath(top.stdout.strip())
            real_dir = os.path.realpath(link_dir)
            if os.path.commonpath([work_tree, real_dir]) != work_tree:
                continue
            rel_link = os.path.relpath(os.path.join(real_dir, os.path.basename(link)), work_tree)
            tracked = _git_at(work_tree, "ls-files", "--error-unmatch", "--", rel_link)
            if tracked.returncode != 0:
                return _reject(
                    f"The cited path goes through the symlink '{link}', which is "
                    f"untracked (not yet added to git) in '{work_tree}'. An uncommitted "
                    "symlink cannot vouch for the file it points at. Commit the "
                    "symlink first, or cite the file's real path."
                )
            link_status = _git_at(work_tree, "status", "--porcelain", "--", rel_link)
            if link_status.returncode != 0:
                git_err = (link_status.stderr or "").strip()[:300] or "no stderr output"
                return _reject(
                    f"git status failed (exit {link_status.returncode}) for symlink "
                    f"'{rel_link}': {git_err}. Cannot confirm the symlink is committed, "
                    "so the citation cannot be accepted."
                )
            if link_status.stdout.strip():
                return _reject(
                    f"The cited path goes through the symlink '{link}', which is "
                    f"modified but not committed (status: {link_status.stdout[:2].strip()}). "
                    "A changed symlink cannot vouch for the file it points at. "
                    "Commit the symlink first, then retry."
                )
            in_tree_links.append((link, work_tree, rel_link))

    try:
        repo_check = _file_git("rev-parse", "--is-inside-work-tree")
        file_in_git_repo = repo_check.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        file_in_git_repo = False

    if not file_in_git_repo:
        if in_tree_links:
            # Reached through a link that lives in a work tree, but the bytes
            # are in none: no repository can vouch for them.
            return _outside_repo_rejection(physical)
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

    # Each entry is (label, repo-relative path); label "" = the only check.
    # Every path must be clean in `git status` AND present at the revision.
    if cross_repo:
        # Cross-repo citation: the cited file lives outside source_repo at abs_file,
        # but the canonical committed copy is at repo_path within source_repo.
        # Use repo_path for all git tree operations; abs_file only for content_hash.
        abs_file = os.path.abspath(file_path)
        path_checks = [("", repo_path)]
        check_git = _file_git
    else:
        # Same-repo citation (#3292): the bytes are the PHYSICAL file, checked in
        # the repository that contains it (link checks ran above).
        abs_file = physical
        real_root = os.path.realpath(repo_root)
        rel_target = os.path.relpath(physical, real_root)
        if rel_target == ".." or rel_target.startswith(".." + os.sep):
            return _outside_repo_rejection(physical)
        path_checks = [("resolved target " if links else "", rel_target)]
        # Repo-relative paths need the repo root as cwd.
        check_git = lambda *args: _git_at(real_root, *args)

    for label, rel_path in path_checks:
        status_result = check_git("status", "--porcelain", "--", rel_path)
        if status_result.returncode != 0:
            # Fail closed: an empty stdout from a failed `git status` is not "clean".
            git_err = (status_result.stderr or "").strip()[:300] or "no stderr output"
            return _reject(
                f"git status failed (exit {status_result.returncode}) for "
                f"{label}'{rel_path}': {git_err}. Cannot confirm the file is committed, "
                "so it cannot be cited."
            )
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
            return _reject(
                f"File is {status_desc}: {label}'{rel_path}'. "
                "File-based evidence must be committed to git before it can be cited. "
                "This ensures the evidence is verifiable at the claimed revision. "
                "Commit the file first, then retry with the new git SHA."
            )

    if not content_hash:
        with open(abs_file, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()

    if not git_sha:
        head_result = _file_git("rev-parse", "HEAD")
        if head_result.returncode == 0:
            git_sha = head_result.stdout.strip()

    if git_sha:
        for label, rel_path in path_checks:
            verify_result = check_git("cat-file", "-e", f"{git_sha}:{rel_path}")
            if verify_result.returncode != 0:
                return _reject(
                    f"File {label}'{rel_path}' does not exist at git revision {git_sha[:8]}. "
                    "The git_sha must point to a commit that contains this file. "
                    "Check that you committed the file before this revision."
                )
            if cross_repo:
                # The isfile check above only inspects the cited abs_file. The
                # object repo_path names in source_repo must be a blob too: for a
                # directory `cat-file -e` succeeds on the tree (a symlink entry
                # is also a blob, which is fine) (#3393).
                type_result = check_git("cat-file", "-t", f"{git_sha}:{rel_path}")
                obj_type = (type_result.stdout or "").strip()
                if type_result.returncode != 0 or obj_type != "blob":
                    return _reject(
                        f"repo_path '{rel_path}' must name a file (a blob) at git "
                        f"revision {git_sha[:8]} in source_repo, but it is "
                        f"{('a ' + obj_type) if obj_type else 'not readable'}. "
                        "Cite a committed file's path, not a directory."
                    )
        if not cross_repo:
            for link, work_tree, rel_link in in_tree_links:
                if work_tree != real_root:
                    continue
                verify_result = _git_at(real_root, "cat-file", "-e", f"{git_sha}:{rel_link}")
                if verify_result.returncode != 0:
                    return _reject(
                        f"The symlink '{link}' ('{rel_link}') does not exist at git "
                        f"revision {git_sha[:8]}. The git_sha must point to a commit "
                        "that contains every symlink the cited path goes through."
                    )

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
    # A FIFO (or any non-regular file) blocks read_text() forever; an evidence row
    # created for a regular file can have its path replaced later (#3393).
    if not os.path.isfile(file_path):
        return (
            f"Evidence must be a file: '{file_path}' is not a regular file. "
            "Cite a file inside it instead."
        )

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
    is_pdf_source = file_path.lower().endswith(".pdf")
    near = _find_near_matches(content, escaped, file_path)
    if is_pdf_source:
        # engram-alpha#2765: a PDF archive holds raw bytes, not extracted
        # text — the generic "verbatim substring" message sends the agent
        # hunting for a typo in an otherwise-correct quote. Name the format
        # gap explicitly, and say so plainly (Ariadne's amendment on #2765:
        # "Your quote may be exactly correct" is the load-bearing sentence).
        msg = (
            f"Quoted text not found in '{os.path.basename(file_path)}'. "
            "This source is a PDF: quotes cannot be verified against PDF bytes, "
            "only against extracted text. Extract first (pdftotext <f> <f>.txt), "
            "snapshot the .txt, and cite that. Your quote may be exactly correct."
        )
    else:
        msg = (
            f"Quoted text not found in '{os.path.basename(file_path)}'. "
            "The quoted_text must be a verbatim substring of the evidence source. "
            "Do not paraphrase, summarize, or fabricate quotes.\n\n"
            "BEFORE RETRYING: check the engram-observe skill (Step A.4) for the "
            "pre-verification script. For JSONL files, messages may not have flushed "
            "yet — verify the quote exists in the file BEFORE calling this tool. "
            "Run: ~/.engram/tools/verify_quote.py <file> <phrase>"
        )
    # Near-matches are computed against `content` -- for a PDF that's raw
    # bytes decoded as UTF-8 with errors="replace", i.e. noise. Suggesting
    # "copy from a match above" on garbage would contradict the PDF message's
    # own "your quote may be exactly correct" — skip the suggestion path.
    if near and not is_pdf_source:
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

def _evidence_fields_error(url: str, title: str) -> Optional[str]:
    """Required-field check of an evidence write (item-independent, no writes).
    Returns the bare error text, or None."""
    if not url or not url.strip():
        return "url is required and cannot be empty."
    if not title or not title.strip():
        return "title is required and cannot be empty."
    return None


def _evidence_row_id_for_url(url: str) -> Optional[str]:
    """Id of the evidence row already registered for this raw url, else None.
    The same lookup _add_evidence_impl's reuse short-circuit makes."""
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT id FROM nodes WHERE source_url = ? AND type = 'evidence'"
            " ORDER BY created_at DESC LIMIT 1", (url,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _evidence_url_error(url: str, source_repo: str = "", repo_path: str = "") -> Optional[str]:
    """Every rejection an evidence write makes about the url alone (#3399): the
    file:// committed-before-cite capture check, and for web urls the hostname /
    TLD / localhost / DNS checks, and the unrecognized-scheme check.

    ITEM-INDEPENDENT and write-free: no DB, no observation input. Returns the
    bare error text (no "Failed to create evidence node:" prefix), or None when
    the url may be cited. Called by _add_evidence_impl itself, after its
    existing-row reuse short-circuit, and by the batch's up-front check, so a
    url the evidence write would refuse is refused once at batch level instead
    of once per item (and one DNS lookup instead of one per item).
    """
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
        ver = _capture_file_version(url, source_repo=source_repo, repo_path=repo_path)
        if not ver["ok"]:
            return ver["error"]
    elif url.startswith("http://") or url.startswith("https://"):
        # --- Validation for web URLs ---
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname or ""

        # Check 1: Structural — must have a valid hostname
        if not hostname:
            return (
                f"Malformed URL '{url}' — no hostname found. "
                "Evidence URLs must point to real, resolvable web addresses."
                + _HONESTY_REMINDER
            )

        # Check 2: TLD — hostname must contain a dot (rejects fabricated
        # single-label hosts like 'session-transcript', 'conversation')
        if "." not in hostname:
            if hostname == "localhost":
                return (
                    f"Cannot cite a localhost URL directly — local/LAN addresses are not "
                    "stable evidence sources. To cite a forum post or local file, archive it first: "
                    "`engram-snapshot forum:<thread_id>` (forum thread) or "
                    "`engram-snapshot file:<path>` (local file), then cite the resulting "
                    "file:// evidence record returned by the snapshot tool."
                    + _HONESTY_REMINDER
                )
            return (
                f"Invalid hostname '{hostname}' in URL — no TLD found. "
                "This looks like a fabricated domain. Real web URLs have a domain "
                "with a TLD (e.g., 'arxiv.org', 'reuters.com')."
                + _HONESTY_REMINDER
            )

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
                return (
                    f"DNS resolution failed for '{hostname}' — this domain does not exist. "
                    "The URL may be fabricated or hallucinated. Verify you have the correct URL "
                    "from a real source before citing it as evidence."
                    + _HONESTY_REMINDER
                )
            # Transient — fall through to silent pass (same as the OSError branch).
        except (socket.timeout, OSError):
            # Network issues — warn but don't block, since the domain might be real
            pass

    elif not any(url.startswith(s) for s in RECOGNIZED_SCHEMES):
        return (
            f"Unrecognized URL scheme in '{url}'. Evidence URLs must be resolvable — "
            "use file:// for local files or https:// for web sources. "
            "Do not invent synthetic schemes (e.g., conversation://, measurement://)."
            + _HONESTY_REMINDER
        )
    return None


def _add_evidence_impl(
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    content_snippet: str = "",
    source_repo: str = "",
    repo_path: str = "",
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
    _fields_err = _evidence_fields_error(url, title)
    if _fields_err:
        return json.dumps({"error": _fields_err})
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

        # Item-independent url validation (no writes): one helper, shared with
        # the batch's up-front check.
        _url_err = _evidence_url_error(url, source_repo=source_repo, repo_path=repo_path)
        if _url_err:
            return json.dumps({"error": _url_err})

        # Check trust pool (domain already computed above for yellow-card)
        config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
        trust_pool = config.get("trust_pool", [])
        in_trust_pool = any(domain.endswith(tp) for tp in trust_pool)

        node_id = core._next_id(conn, "evidence")
        now = core._now()

        # Auto-tag source_type from URL pattern (orthogonal to source_class).
        # Used by the dedup heuristic to grade action_hints by artifact kind.
        source_type = core._infer_source_type(url)

        core._refuse_ncb_confidence("evidence", None)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, created_at, source_url, source_title,
               source_domain, source_date, source_accessed, content_snippet, metadata,
               source_type)
               VALUES (?, 'evidence', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, now, url, title, domain, source_date or None, now,
             content_snippet or None, "{}",
             source_type),
        )

        _emb_outcome = core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability. Evidence nodes never populate a
        # `claim` column (#2234) -- `source_url` stands in as the
        # always-populated column instead, since url is required and
        # validated non-empty before the INSERT above.
        _verdict, _durability = core.verify_write_durability(
            conn, node_id, claim_field="source_url")
        if _verdict != core.DURABILITY_DURABLE:
            return json.dumps(core.durability_failure_response(
                _verdict, _durability, "evidence_id", node_id, "#2234"))

        result = {
            "status": "created",
            "evidence_id": node_id,
            "domain": domain,
            "source_date": source_date or None,
            "in_trust_pool": in_trust_pool,
        }
        _emb_block = core._embedding_response_block(_emb_outcome)  # #345 PR3 B4
        if _emb_block:
            result["embedding_failure"] = _emb_block  # R1: renamed key
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


# Allowlist for session_id path components: UUID-ish / hyphenated hex tokens
# only. session_id is untrusted input used to build a filesystem path — this
# gate MUST run before any filesystem access to block '../' traversal (do
# NOT rely on the marker file simply not existing).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_session_transcript(session_id: str) -> dict:
    """Resolve a session_id (as injected at Claude Code session start) to a
    citable transcript file:// url + a derived title.

    Reads ``<engram_core.DATA_DIR>/sessions/<session_id>.json`` (the marker
    written by the SessionStart hook) and its ``transcript_path`` field.
    Never raises — every failure mode is returned as {"error": "..."}.

    Returns {"url": "file://...", "title": "Chat log YYYY-MM-DD"} on success.
    """
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        return {
            "error": (
                f"Invalid session_id {session_id!r}: must contain only letters, "
                "digits, '_', '-' (no '/', '\\', '.', or other path separators). "
                "Pass an explicit 'url' instead."
            )
        }

    marker_path = core.DATA_DIR / "sessions" / f"{session_id}.json"
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "error": (
                f"No session marker at {marker_path} (or unreadable); "
                "pass an explicit 'url' instead."
            )
        }

    transcript_path = marker.get("transcript_path") or ""
    if not transcript_path:
        return {
            "error": (
                f"Session marker at {marker_path} has no 'transcript_path' "
                "(or unreadable); pass an explicit 'url' instead."
            )
        }

    if not os.path.exists(transcript_path):
        return {
            "error": (
                f"Transcript file '{transcript_path}' (from session marker "
                f"{marker_path}) does not exist on disk. The session may not "
                "have flushed yet, or the marker is stale. Pass an explicit "
                "'url' instead."
            )
        }

    started_at = marker.get("started_at") or ""
    date_str = ""
    if len(started_at) >= 10 and started_at[4:5] == "-" and started_at[7:8] == "-":
        date_str = started_at[:10]
    else:
        try:
            date_str = date.fromtimestamp(os.path.getmtime(transcript_path)).isoformat()
        except OSError:
            date_str = ""

    return {
        "url": "file://" + transcript_path,
        "title": f"Chat log {date_str}".rstrip(),
    }


# Stage 1.4 (#2252) — predictive-hint affordance. Trigger A only
# (predicted_event / resolution_timeframe present without is_predictive).
# Deliberately small and boring: this is a discoverability nudge for the
# pairing invariant (SETTLED Q1), not an NLP classifier. Err toward
# silence — a missed hint costs nothing (the observation still files
# normally); a false-positive hint on every other claim would train agents
# to ignore it. The claim-text regex trigger (Trigger B) was removed
# pre-merge on measured zero precision (0/22 sampled fires were genuine
# predictions — Ariadne's #2260 colleague pass); redesign tracked in #2264.


def _predictive_hint(is_predictive: bool,
                      predicted_event: str, resolution_timeframe: str) -> Optional[str]:
    """Advisory-only nudge toward the pairing invariant (#2252 Q1) — never
    blocks, never changes what gets written. Returns the hint string when
    warranted, else None. Trigger A only: fires when predicted_event or
    resolution_timeframe is present without is_predictive=true.
    """
    if is_predictive:
        return None
    if not (predicted_event or resolution_timeframe):
        return None
    return (
        "You passed predicted_event/resolution_timeframe without "
        "is_predictive=true. If this claim predicts a checkable event, "
        "refile or follow up with is_predictive=true + predicted_event — "
        "the paired prediction node is what joins the calibration ledger "
        "(pairing invariant, #2252 Q1)."
    )


def _log_anchoring_advisory(entry: dict) -> None:
    """Append one JSONL line per observation write — the calibration input
    for #2602 facets 3/4. Not read by anything in this PR. Best-effort:
    logging failures must never block a write (same discipline as
    engram_query.py's _pattern_telemetry_log)."""
    try:
        path = Path(core.DATA_DIR) / "anchoring_advisory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _build_anchoring_advisory(
    node_id: str, claim: str, quoted_text: str, interpretation: str,
    quote_type: str, source_class: str, confidence: Optional[float],
) -> dict:
    """Facet 2 (#2602): advisory-only hard-token anchoring measurement
    attached to every observation write's response.

    Loud, never fatal — any exception here becomes an
    {"status": "unavailable", "reason": ...} advisory rather than failing
    the write (a broken advisory must never block an observation).
    """
    try:
        result = _anchor_claim(claim, quoted_text)
        similarity, similarity_reason = _quote_claim_similarity(quoted_text, claim)
        body_to_quote_ratio = (len(claim or "") + len(interpretation or "")) / max(len(quoted_text or ""), 1)

        advisory: dict = {
            "hard_tokens": result.hard_tokens,
            "anchored": result.anchored,
            "ratio": result.ratio,
            "unanchored": result.unanchored[:20],
            "body_to_quote_ratio": body_to_quote_ratio,
            "quote_claim_similarity": similarity,
        }
        if similarity is None and similarity_reason:
            advisory["similarity_status"] = f"unavailable: {similarity_reason}"
        if quote_type == "hard_data" and result.unanchored:
            advisory["hint"] = (
                f"hard_data: {len(result.unanchored)} claim token(s) absent from the "
                "quote — widen the quote, move the fact to `interpretation`, or "
                "downgrade quote_type. Advisory only (#2602 facet 2)."
            )

        _log_anchoring_advisory({
            "ts": core._now(),
            "node_id": node_id,
            "quote_type": quote_type,
            "source_class": source_class,
            "confidence": confidence,
            "hard_tokens": result.hard_tokens,
            "anchored": result.anchored,
            "ratio": result.ratio,
            "unanchored": result.unanchored[:20],
            "quote_len": len(quoted_text or ""),
            "claim_len": len(claim or ""),
            "interp_len": len(interpretation or ""),
            "body_to_quote_ratio": body_to_quote_ratio,
            "quote_claim_similarity": similarity,
        })
        return advisory
    except Exception as exc:  # noqa: BLE001 — advisory must never fail a write
        logging.warning("anchoring advisory failed for %s: %s", node_id, exc)
        return {"status": "unavailable", "reason": str(exc)[:200]}


def _evidence_capture_error(url: str, source_repo: str = "", repo_path: str = "") -> Optional[str]:
    """The item-independent first check an evidence write makes (#3399), with
    the "Failed to create evidence node:" prefix the single and batch paths
    both report. Returns the prefixed error text, or None when the url may be
    cited."""
    err = _evidence_url_error(url, source_repo=source_repo, repo_path=repo_path)
    return f"Failed to create evidence node: {err}" if err else None


def _preflight_item_checks(url: str, quoted_text: str, *, content_hash: str = "",
                           git_sha: str = "", source_repo: str = "",
                           repo_path: str = ""):
    """The per-observation rejections that depend only on the cited file (#3399).

    Returns (error_json_or_None, ver_or_None); ver is the version capture to
    reuse after the evidence write. Quote check first, then the caller
    content_hash / git_sha version check -- the order the evidence_id path uses.
    """
    quote_error = _verify_quote_in_source(url, quoted_text)
    if quote_error:
        return json.dumps({"error": quote_error}), None
    ver = _capture_file_version(
        url, content_hash=content_hash, git_sha=git_sha,
        source_repo=source_repo, repo_path=repo_path)
    if not ver["ok"]:
        return json.dumps({"error": ver["error"]}), None
    return None, ver


def _preflight_file_evidence(url: str, quoted_text: str, *, content_hash: str = "",
                             git_sha: str = "", source_repo: str = "",
                             repo_path: str = ""):
    """Every rejection decided before a file:// evidence write (#3399): the
    evidence-capture check, then the per-observation checks. The single source
    of truth for the url path of the single-observation impl, which the batch
    also goes through (it passes url+title per item).

    Returns (error_json_or_None, ver_or_None).
    """
    cap_error = _evidence_capture_error(url, source_repo=source_repo, repo_path=repo_path)
    if cap_error:
        return json.dumps({"error": cap_error}), None
    return _preflight_item_checks(
        url, quoted_text, content_hash=content_hash, git_sha=git_sha,
        source_repo=source_repo, repo_path=repo_path)


def _add_observation_impl(
    quoted_text: str = "",
    interpretation: str = "",
    claim: str = "",
    quote_type: Optional[str] = None,
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    is_predictive: bool = False,
    predicted_event: str = "",
    resolution_timeframe: str = "",
    source_class: Optional[str] = None,
    content_hash: str = "",
    git_sha: str = "",
    standpoint_author_id: str = "",
    standpoint_collection_id: str = "",
    standpoint_override_tag: str = "",
    standpoint_lineage: str = "",
    standpoint_architecture: str = "",
    fs_class=None,
    source_repo: str = "",
    repo_path: str = "",
    session_id: str = "",
    recall_summary: str = "",
    recall_keywords: Optional[list] = None,
    evidence_type: str = "",
    original_url: str = "",
    _evidence_created_out: Optional[dict] = None,
) -> str:
    """Internal implementation — see engram_add_observation MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers (engram_add_observation_batch, engram_retract replacement path).

    NOTE on source_class default: the signature default is None (sentinel for
    "caller truly omitted it"), not "external" — this lets the session_id path
    apply its own ergonomic default (user_stated) only when the caller truly
    omitted the field, without stomping an explicit choice. Using None (not
    "") matters: a caller that explicitly passes source_class="" must still
    hit the VALID_SOURCE_CLASSES rejection below, not be silently defaulted —
    only true omission (None) triggers a default. The historical "external"
    default is restored just below when session_id is not the source.
    Callers that always pass source_class explicitly (the batch impl) are
    unaffected either way.

    ``_evidence_created_out`` is a private out-parameter for the batch impl: when
    a dict is passed, ``["created"]`` is set True iff THIS call wrote a new
    evidence row (False when it reused one). It is never part of the response.
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

    # Write-time recall_summary/recall_keywords validation (#1952 PR1) — runs
    # early, before any DB/network work, so an over-long recall_summary
    # hard-rejects with zero side effects (no evidence node, no observation
    # row). Presence is soft (Resolution 1): omission normalizes to 0 chars /
    # empty list and clean-accepts; only an explicitly-provided recall_summary
    # >= 280 chars errors.
    _recall_summary, _recall_keywords, _needs_tightening, _summary_missing, _recall_error = (
        core._validate_recall_fields(recall_summary, recall_keywords)
    )
    if _recall_error:
        return json.dumps({"error": _recall_error})

    # session_id resolution — precedence evidence_id > url > session_id.
    # Runs BEFORE the required-field check below because it may supply
    # ergonomic defaults for quote_type/source_class, and before evidence
    # resolution because it may supply url/title. Feeds into the EXISTING
    # auto-create/reuse-by-url evidence path unchanged — no duplicated logic.
    if session_id:
        if evidence_id or url:
            return json.dumps({
                "error": (
                    "Provide exactly one source form: 'session_id' (cite the "
                    "current chat), 'url' (+ 'title'), or 'evidence_id' — "
                    "not more than one."
                )
            })
        _session_resolved = _resolve_session_transcript(session_id)
        if _session_resolved.get("error"):
            return json.dumps({"error": _session_resolved["error"]})
        url = _session_resolved["url"]
        title = _session_resolved["title"]
        # Ergonomic defaults — ONLY on the session_id path. The url/
        # evidence_id paths keep today's behavior (quote_type stays
        # required there; source_class stays defaulted to "external" below).
        if quote_type is None:
            quote_type = "personal_communication"
        if source_class is None:
            source_class = "user_stated"

    # Restore the historical default now that the session_id ergonomic
    # default (if any) has had its chance to apply. Only true omission
    # (None) defaults here — an explicit source_class="" from a caller
    # falls through unchanged to the VALID_SOURCE_CLASSES check below and
    # errors loudly, matching pre-session_id dev behavior.
    if source_class is None:
        source_class = "external"

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

    # standpoint_author_id stable-ID gate (#1827 layer 1 — upgraded from the
    # #1348 warn-not-reject advisory). Runs HERE, before evidence resolution,
    # so a reject leaves ZERO rows — the evidence auto-create below commits on
    # its own connection, and a gate that fired after it persisted an orphan
    # evidence node on every reject (PR #1979 review blocker). The existence
    # lookup + repair hint need a DB read; they use a tightly-scoped early
    # connection rather than the main `conn` (opened later). Two failure modes:
    #   1. display-name shape (raw strings like "John Smith", "Lei").
    #   2. dangling pn_* — well-formed pn_\d+ shape that does not resolve to
    #      an existing, current person node: the same defect wearing a
    #      valid-looking shape. This check is itself NEW (#1827) — pre-#1827
    #      code never verified pn_* existence — so it only runs when the hard
    #      gate is on; the kill-switch warn path stays byte-compatible with
    #      pre-#1827 (no dangling check, no repair hint).
    # Namespaced forms (orcid:…, domain:handle, …) pass both checks — they are
    # deliberately out-of-graph. Absent field: gate no-ops, standpoint stays
    # optional.
    _author_id_bad_shape = bool(
        standpoint_author_id and not _STABLE_ID_RE.match(standpoint_author_id)
    )
    _standpoint_cfg = _get_standpoint_config()
    _hard_gate = _standpoint_cfg.get("author_id_hard_gate", True)
    _author_id_warning: Optional[str] = None
    if not _hard_gate:
        # Legacy warn-not-reject (#1348), byte-compatible with pre-#1827:
        # format check only, no dangling-pn lookup, no repair hint.
        if _author_id_bad_shape:
            _author_id_warning = (
                f"standpoint_author_id '{standpoint_author_id}' looks like a display name "
                "rather than a stable ID. Use a pn_* node ID for in-graph entities, or a "
                "namespaced string (orcid:…, doi-author:…, domain:handle) for external "
                "authors. See #1348 convention."
            )
    elif standpoint_author_id:
        _gate_conn = core._get_db()
        try:
            if _author_id_bad_shape:
                _author_id_msg = (
                    f"standpoint_author_id '{standpoint_author_id}' looks like a display name "
                    "rather than a stable ID. Use a pn_* node ID for in-graph entities, or a "
                    "namespaced string (orcid:…, doi-author:…, domain:handle) for external "
                    "authors. See #1348 convention."
                )
                _repair_hint = _find_person_repair_hint(_gate_conn, standpoint_author_id)
                if _repair_hint:
                    _author_id_msg = f"{_author_id_msg} {_repair_hint}"
                return json.dumps({"error": _author_id_msg})
            if re.match(r"^pn_\d+$", standpoint_author_id):
                _pn_row = _gate_conn.execute(
                    "SELECT id FROM nodes WHERE id = ? AND type = 'person' AND is_current = 1",
                    (standpoint_author_id,),
                ).fetchone()
                if not _pn_row:
                    return json.dumps({"error": (
                        f"standpoint_author_id '{standpoint_author_id}' has the pn_* shape but does "
                        "not resolve to an existing, current person node — a dangling reference. "
                        "See #1827. No current person node has this ID. Check for a typo, or mint "
                        "one: engram_add_person."
                    )})
        finally:
            _gate_conn.close()

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

    # evidence_type validation (#2606 facet 1 unit 2): closed vocabulary,
    # matching engram-snapshot's EVIDENCE_TYPE_BY_SOURCE. Not inferred from
    # content or fs_class -- the capturer (engram-snapshot, or a caller citing
    # a known source kind) names it; a later reader only ever reads it back.
    _EVIDENCE_TYPE_ENUM = {
        "forum_thread", "session_transcript", "local_file", "web_page", "pdf",
    }
    if evidence_type and evidence_type not in _EVIDENCE_TYPE_ENUM:
        return json.dumps({
            "error": (
                f"Invalid evidence_type '{evidence_type}'. Must be one of: "
                f"{', '.join(sorted(_EVIDENCE_TYPE_ENUM))}"
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

    # Every rejection that depends only on the cited file is decided HERE,
    # before the evidence write (#3399). _add_evidence_impl commits its row on
    # its own connection (and reads it back on a second one), so the evidence
    # write cannot share a transaction with the observation insert below: a
    # rejection decided after it would leave an orphan evidence row. The same
    # checks used to run after it; their results are reused further down.
    #
    # Invariant this relies on: on this path ev["source_url"] == url byte for
    # byte. _add_evidence_impl's reuse lookup is `source_url = ?` on the raw
    # url, with no normalisation, and it INSERTs that same raw url when it
    # creates the row. So verifying the quote against `url` here, before the
    # write, is identical to verifying it against the row's source_url after.
    _pre_quote_checked = False
    _pre_ver = None
    if not evidence_id and url and url.startswith("file://"):
        _pre_err, _pre_ver = _preflight_file_evidence(
            url, quoted_text, content_hash=content_hash, git_sha=git_sha,
            source_repo=source_repo, repo_path=repo_path)
        if _pre_err:
            return _pre_err
        _pre_quote_checked = True

    _yellow_forward = None
    if not evidence_id and url:
        # Auto-create or reuse evidence node
        ev_result = json.loads(_add_evidence_impl(
            url=url, title=title, domain=domain, source_date=source_date,
            source_repo=source_repo, repo_path=repo_path,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        evidence_id = ev_result["evidence_id"]
        _yellow_forward = ev_result.get("yellow_card_warning")
        if _evidence_created_out is not None:
            _evidence_created_out["created"] = ev_result.get("status") == "created"

    conn = core._get_db()
    try:
        # Validate evidence exists
        ev = conn.execute(
            "SELECT id, source_url, metadata FROM nodes WHERE id = ? AND type = 'evidence'",
            (evidence_id,),
        ).fetchone()
        if not ev:
            return json.dumps(
                {"error": f"Evidence node '{evidence_id}' not found."}
            )

        # evidence_type / original_url passthrough onto the evidence node's
        # metadata (#2606 facet 1 units 2+3), whether that node was just
        # created above or is being reused (evidence_id-cite path or
        # url-match reuse inside _add_evidence_impl). First-capture-wins: a
        # value already present is never overwritten; a caller naming a
        # DIFFERING value surfaces as a *_conflict key in the response rather
        # than silently resolving one way or the other. Absent/empty
        # arguments never touch the evidence node -- an evidence node created
        # without a snapshot must not grow a spurious key.
        # #3413: parse silently here (an early return below discards the
        # write); the replacement of a non-object blob is logged and warned
        # at the WRITE, where it actually happens.
        _ev_metadata = core._metadata_dict(ev["metadata"])
        _ev_metadata_changed = False
        _evidence_type_conflict = None
        _original_url_conflict = None

        if evidence_type:
            _existing_evidence_type = _ev_metadata.get("evidence_type")
            if _existing_evidence_type is None:
                _ev_metadata["evidence_type"] = evidence_type
                _ev_metadata_changed = True
            elif _existing_evidence_type != evidence_type:
                _evidence_type_conflict = {
                    "existing": _existing_evidence_type,
                    "attempted": evidence_type,
                }

        if original_url:
            _existing_original_url = _ev_metadata.get("original_url")
            if _existing_original_url is None:
                _ev_metadata["original_url"] = original_url
                _ev_metadata_changed = True
            elif _existing_original_url != original_url:
                _original_url_conflict = {
                    "existing": _existing_original_url,
                    "attempted": original_url,
                }

        # Verify quoted_text exists in the evidence source (file:// only)
        ev_url = ev["source_url"] or ""
        # (The url path already ran both this and the version capture below
        # before the evidence write — #3399 — and reuses their results.)
        if not _pre_quote_checked:
            quote_error = _verify_quote_in_source(ev_url, quoted_text)
            if quote_error:
                return json.dumps({"error": quote_error})

        # Capture per-observation file version (the evidence-block refactor derivation). For file:// URLs
        # in a git repo: enforce committed-before-cite + record the specific
        # revision read on this observation. For other URLs: no-op.
        if _pre_quote_checked:
            ver = _pre_ver
        else:
            ver = _capture_file_version(ev_url, content_hash=content_hash, git_sha=git_sha, source_repo=source_repo, repo_path=repo_path)
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

        core._refuse_ncb_confidence(node_type, confidence)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, evidence_id, quoted_text,
               interpretation, quote_type, confidence, confidence_history, metadata,
               standpoint_author_id, standpoint_collection_id, standpoint_override_tag,
               standpoint_lineage, standpoint_architecture, fs_class, recall_summary,
               recall_keywords, needs_tightening, summary_missing)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                _recall_summary or None,
                json.dumps(_recall_keywords) if _recall_keywords else None,
                1 if _needs_tightening else 0,
                1 if _summary_missing else 0,
            ),
        )

        # Edge: observation cites evidence
        core._insert_cites_edge(conn, node_id, evidence_id, now)

        # Stamp importance
        _emb_outcome = core._stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

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
            if ev_url.lower().endswith(".pdf"):
                # engram-alpha#2765: a url:-sourced PDF observation never gets
                # a page fetch at creation time, same as any other web
                # source -- but for a PDF the check could NEVER have run
                # even with a fetch (quotes verify against extracted text,
                # not raw bytes). The generic web-source caveat reads
                # identically for "we didn't fetch" and "this format can't
                # verify"; name the PDF case so it doesn't read as an
                # ordinary, softer caveat.
                result["quote_provenance_warning"] = (
                    "This source is a PDF: the quote check could not run on it, and "
                    "could not have, even with a page fetch — PDF quotes verify only "
                    "against extracted text, never raw bytes. Extract first "
                    "(pdftotext <f> <f>.txt), snapshot the .txt, and cite that for a "
                    "verifiable quote. See #2765."
                )
            else:
                result["quote_provenance_warning"] = (
                    "Web-sourced quoted_text is unverified at encoding — no page fetch "
                    "occurs at observation creation time. The quote may not appear verbatim "
                    "at the cited URL (paraphrase, page-change, paywall). For high-stakes "
                    "citations, verify manually. See #1204."
                )

        # Advisory hard-token anchoring (#2602 facet 2) — attaches to every
        # write, all quote types; never refuses, never changes confidence.
        result["anchoring_advisory"] = _build_anchoring_advisory(
            node_id, claim, quoted_text, interpretation, quote_type,
            source_class, confidence,
        )

        # Handle prediction decomposition
        prediction_id = None
        _pred_emb_outcome = None
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
                core._refuse_ncb_confidence("prediction", None)  # #2252 Stage 1.3 write-path guard
                conn.execute(
                    """INSERT INTO nodes (id, type, predicted_event, resolution_timeframe,
                       status, created_at, confidence)
                       VALUES (?, 'prediction', ?, ?, 'open', ?, NULL)""",
                    (prediction_id, predicted_event, resolution_timeframe or None, now),
                )
                result["prediction_status"] = "created"
                # #3348: the prediction node now goes through _stamp_new_node
                # (importance stamp + write-time embed + #3343 write-path
                # contract), same as every other node-creating INSERT.
                # _stamp_new_node writes its OWN "created" edit_history row
                # (details: {"confidence": ...}, read fresh off the node
                # after its importance UPDATE) -- the manual _log_edit call
                # that used to be the ONLY "created" row here is removed so
                # there is exactly one, not two. _stamp_new_node's own row
                # does not carry predicted_event (it only ever logs
                # confidence; not touched here per #3348's DO NOT), so that
                # detail is preserved under a separate, non-"created" action
                # instead of being silently dropped.
                _pred_emb_outcome = core._stamp_new_node(
                    conn, prediction_id, confidence=0.5, surprise=0.5,
                )
                core._log_edit(conn, "prediction_decomposed", prediction_id,
                          "prediction", {"predicted_event": predicted_event[:200]})

            # Edge: prediction predicted_by observation (#2252 W6 — split
            # out of supported_by; prediction is still the SOURCE, the
            # predictive observation is still the TARGET, same direction
            # supported_by used for this pairing).
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'predicted_by', ?)",
                    (prediction_id, node_id, now),
                )
            except sqlite3.IntegrityError:
                pass

            result["prediction_id"] = prediction_id

        # #3353: the observation node is stamped exactly ONCE -- the
        # `_stamp_new_node` call right after the evidence `cites` edge
        # above (the one that feeds `_emb_outcome` into the response
        # below). A second, duplicate call used to run here, after the
        # prediction-decomposition block: nothing between the two call
        # sites reassigns `confidence` or runs `UPDATE nodes` on this
        # observation's own row, so the second call re-stamped the exact
        # same importance/embedding inputs -- one wasted embed, and a
        # second "created" edit_history row for every observation ever
        # written (issue #3353). Removed; `_emb_outcome` from the FIRST
        # call above is the one that reaches the response unchanged.

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

        if _author_id_warning:
            result["warnings"] = result.get("warnings", []) + [_author_id_warning]

        # Stage 1.4 (#2252) — predictive-hint affordance. Advisory only:
        # never blocks node creation, never changes any existing key.
        # Suppressed whenever is_predictive is true (the caller already used
        # the pairing path). See _predictive_hint for trigger definitions.
        _pred_hint = _predictive_hint(is_predictive, predicted_event, resolution_timeframe)
        if _pred_hint:
            result["predictive_hint"] = _pred_hint

        # Persist the evidence_type / original_url metadata update (if any)
        # in the SAME transaction as the observation insert above -- an
        # early return before this point (a validation failure) leaves both
        # un-committed together, so the evidence node's metadata never
        # changes for an observation that itself was never created.
        if _ev_metadata_changed:
            # A non-object blob read as {} above, so the dict being written
            # REPLACES it: the write-side helper logs that and hands back the
            # warning for this tool's `warnings` list (#3413).
            _, _ev_replaced_warning = core._metadata_dict_for_write(
                ev["metadata"], evidence_id, "engram_add_observation")
            if _ev_replaced_warning:
                result["warnings"] = result.get("warnings", []) + [_ev_replaced_warning]
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(_ev_metadata), evidence_id),
            )
        if _evidence_type_conflict:
            result["evidence_type_conflict"] = _evidence_type_conflict
        if _original_url_conflict:
            result["original_url_conflict"] = _original_url_conflict

        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            return json.dumps(core.durability_failure_response(
                _verdict, _durability, "observation_id", node_id, "#2074"))

        # --- Stage 1 auto-suggest: person-mention detection (§4 of person typed schema) ---
        # Scan the new claim text against name + aliases of every current person node.
        # On match, add an action-hint suggesting the caller wire an about-edge.
        #
        # CRITICAL CONSTRAINT: mentions ≠ about; name-match is necessary but not
        # sufficient — the caller confirms whether the claim is actually ABOUT the
        # person. Never auto-wire. This is a suggestion only.
        try:
            person_rows = conn.execute(
                "SELECT id, metadata FROM nodes WHERE type = 'person' AND is_current = 1"
            ).fetchall()
            person_mentions = []
            claim_lower = claim.lower()
            for prow in person_rows:
                pmeta = core._metadata_dict(prow["metadata"])
                pname = pmeta.get("name", "")
                paliases = pmeta.get("aliases", [])
                vocab = [pname] + (paliases if isinstance(paliases, list) else [])
                def _term_matches(term: str) -> bool:
                    t = term.lower()
                    # Plain \b is Unicode-aware and treats CJK characters as
                    # \w, so it forms no boundary between a CJK char and an
                    # adjacent ASCII term — it silently misses "Lei" in
                    # "我见到了Wei昨天" just as much as it misses "王伟" in
                    # "我和王伟讨论了" (verified empirically: branching only on
                    # term.isascii() still fails the ASCII-glued-to-CJK case,
                    # since \b itself is the thing that can't see a CJK-vs-
                    # ASCII transition as a boundary). Fix: define "boundary"
                    # in ASCII-word terms only — not preceded/followed by an
                    # ASCII letter/digit/underscore. This still blocks the
                    # "Al" ⊂ "Alex" false positive (both ASCII) while treating
                    # any CJK-adjacent position as a valid boundary.
                    pattern = r'(?<![A-Za-z0-9_])' + re.escape(t) + r'(?![A-Za-z0-9_])'
                    return re.search(pattern, claim_lower) is not None

                if any(_term_matches(term) for term in vocab if term):
                    person_mentions.append({
                        "person_id": prow["id"],
                        "person_name": pname,
                        "message": f"mentions {pname} — wire an about-edge?",
                    })
            if person_mentions:
                result["person_mentions"] = person_mentions
        except Exception:
            logging.getLogger(__name__).debug(
                "Stage-1 person-mention suggestion scan failed", exc_info=True
            )
            # Never let suggestion scan block the primary result

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
        # alpha #2574 WS5: site attestation + outcome recording +
        # protection (docs/2574-telemetry-design.md §1/§4/§5). New,
        # narrowly-scoped try/except around this new code only -- see
        # engram_derive.py's _derive_impl blocked-path site for the full
        # rationale (identical shape: this function's own try/finally has
        # no except, so an uncaught raise here would propagate straight
        # out of this MCP tool call, breaching §8).
        _site = "server.engram_add_observation"
        try:
            _had_emitter = Emitter.get() is not None
            _uid = emit_if_initialized(
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
                site="server.engram_add_observation",
            )
            write_telemetry_record(
                manifest_id=_site,
                event_type="engram.tool.engram_call",
                fields={
                    "outcome": (
                        "delivered" if _uid is not None
                        else "dropped" if _had_emitter
                        else "no_emitter"
                    )
                },
            )
        except Exception:
            try:
                write_telemetry_record(
                    manifest_id=_site,
                    event_type="engram.tool.engram_call",
                    fields={"outcome": "init_failed"},
                )
            except Exception:
                pass
        # #345 PR3 B4: R1 (round 2) renamed this block's key to
        # "embedding_failure", which never collides with _AGENT_STRIP_KEYS'
        # literal "embedding" entry (the raw vector payload strip) -- the
        # earlier strip-then-add ordering workaround is no longer needed.
        _stripped = core._strip_agent_facing(result)
        _emb_block = core._embedding_response_block(_emb_outcome)
        # #3348 round 2 (Kepler's ruling on PR #3352, option B + rider):
        # "embedding_failure" keeps EXACTLY its
        # #3343 meaning -- the OBSERVATION node's outcome, always a dict,
        # never a list. The prediction node's outcome (when the prediction
        # was CREATED this call, not linked_to_existing, and its embed
        # failed) goes in its OWN separate key, "prediction_embedding_
        # failure", same shape. A key whose type depends on how many
        # things failed is a reader hazard (Kepler's ruling); one key, one
        # meaning, one shape, per node -- the round-1 dict-or-list shape
        # this replaced is gone.
        #
        # Rider: both dicts carry "node_id" -- including "embedding_
        # failure" ALWAYS, with or without a prediction node in play (an
        # additive key, harmless to every existing dict reader that never
        # expected it). Each dict's own "message" names its own node id in
        # the text (no new top-level response line; #3343's reason for
        # nesting "message" inside the block, rather than a new top-level
        # key, still stands): message = f"{node_id}: " + <the message
        # _embedding_response_block returned>, applied uniformly to both
        # keys in every case (single-failure or both-fail).
        #
        # _pred_emb_outcome is None on the linked_to_existing path (no
        # stamp call ran) and whenever is_predictive/predicted_event are
        # absent -- _embedding_response_block requires a real
        # EmbeddingOutcome, so it must not be called with None.
        _pred_emb_block = (
            core._embedding_response_block(_pred_emb_outcome)
            if _pred_emb_outcome is not None else None
        )
        if _emb_block:
            _emb_block = dict(_emb_block)
            _emb_block["node_id"] = node_id
            _emb_block["message"] = f"{node_id}: " + _emb_block["message"]
            _stripped["embedding_failure"] = _emb_block
        if _pred_emb_block:
            _pred_emb_block = dict(_pred_emb_block)
            _pred_emb_block["node_id"] = prediction_id
            _pred_emb_block["message"] = (
                f"{prediction_id}: " + _pred_emb_block["message"]
            )
            _stripped["prediction_embedding_failure"] = _pred_emb_block
        return json.dumps(_stripped)
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
    source_repo: str = "",
    repo_path: str = "",
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
            - standpoint_lineage (optional): Training lineage "provider:family" (e.g. "anthropic:opus") for the source claim's producer. Marks the EVIDENCE SOURCE that produced the claim, not who authored this node — you always author your own nodes; lineage differs only when citing another agent's or a human's evidence.
            - standpoint_architecture (optional): Cognitive architecture of the source's producer. Enum: transformer | vision-spatial | embodied-sensorimotor | graph-neural | human | other. Tracks architectural (not just training) diversity — Class A calibration exposure.
            - recall_summary (optional but always provide when you can): ≤140-char agent-facing recall scent — compress interpretation/claim, don't start from a blank line. Not content preservation (the full claim renders on inspect); for a multi-fact observation, name the load-bearing fact or file separate observations instead. Soft-accepted up to 279 chars (flagged needs_tightening for later cleanup); rejected at 280+ chars for that one item (other items in the batch are unaffected).
            - recall_keywords (optional but always provide when you can): JSON array of 3-6 short content keywords (never rejected regardless of count).
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
    except TypeError:
        return json.dumps({
            "error": f"observations_json must be a JSON string, got "
                     f"{type(observations_json).__name__}"
        })

    if not isinstance(observations, list):
        return json.dumps({"error": "observations_json must be a JSON array."})

    # The shared evidence row is NOT written here (#3399). A url batch hands
    # url+title to the single-observation path per item; that path decides every
    # rejection before ITS evidence write and reuses the row by source_url, so
    # the only writer of the row is an item that passed. No per-item rejection,
    # present or future, can orphan it. A caller-supplied evidence_id is cited
    # as-is and never written or deleted here.
    resolved_evidence_id = evidence_id
    evidence_created = False
    _batch_yellow = None
    _use_url = False
    if not resolved_evidence_id and url:
        if not title:
            return json.dumps({"error": "'title' is required when providing 'url'."})
        # BATCH-LEVEL (item-independent, no write, decided once, one top-level
        # error with dev's text): a blank title, and everything
        # _evidence_url_error checks -- file:// capture, unrecognized scheme,
        # localhost / no-TLD host, NXDOMAIN. They depend on url/title alone, so
        # N items would only repeat them (and repeat the DNS lookup).
        # PER-ITEM (decided by the single path for each item, before ITS
        # evidence write): required fields, quote vs the file, version
        # (content_hash / git_sha), recall_summary, standpoint_*, fs_class, ...
        _fields_err = _evidence_fields_error(url, title)
        if _fields_err:
            return json.dumps({"error": f"Failed to create evidence node: {_fields_err}"})
        # The evidence write reuses an existing row for the url BEFORE it
        # validates the url, so a non-file url whose row exists is not
        # re-validated here either (same as dev). file:// is always checked: the
        # single path's preflight does the same, row or no row.
        _row_exists = (not url.startswith("file://")) and _evidence_row_id_for_url(url)
        if not _row_exists:
            _cap_error = _evidence_capture_error(url, source_repo=source_repo, repo_path=repo_path)
            if _cap_error:
                return json.dumps({"error": _cap_error})
        _use_url = True
        resolved_evidence_id = None
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

        _ev_out = {}
        r = json.loads(_add_observation_impl(
            evidence_id=resolved_evidence_id or "",
            **({"url": url, "title": title, "domain": domain,
                "source_date": source_date, "_evidence_created_out": _ev_out}
               if _use_url else {}),
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
            source_repo=source_repo,
            repo_path=repo_path,
            recall_summary=obs.get("recall_summary", ""),
            recall_keywords=obs.get("recall_keywords"),
        ))
        r["index"] = i
        if _use_url and r.get("status") == "created":
            # Every created item cites the same url-keyed row: the first one
            # wrote it (or reused a pre-existing one), the rest reuse it.
            if resolved_evidence_id and r.get("evidence_id") != resolved_evidence_id:
                raise RuntimeError(
                    f"batch items disagree on the shared evidence row: "
                    f"{resolved_evidence_id!r} vs {r.get('evidence_id')!r} (#3399)")
            resolved_evidence_id = r.get("evidence_id")
            if _ev_out.get("created"):
                evidence_created = True
        # The batch reports the evidence warning once, at batch level; the
        # per-item shape stays what the evidence_id path always returned.
        _item_yellow = r.pop("yellow_card_warning", None) if _use_url else None
        if _item_yellow and not _batch_yellow:
            _batch_yellow = _item_yellow
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
    # alpha #2574 WS5: this site sits with NO surrounding try at all (§0.1) --
    # a NEW try wraps it (§10: "a new try wrapping each of the two
    # no-try-at-all consumer sites"), same protective purpose and shape as
    # the try/finally sites above.
    _site = "server.engram_add_observation_batch"
    try:
        _had_emitter = Emitter.get() is not None
        _uid = emit_if_initialized(
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
            site="server.engram_add_observation_batch",
        )
        write_telemetry_record(
            manifest_id=_site,
            event_type="engram.tool.engram_call",
            fields={
                "outcome": (
                    "delivered" if _uid is not None
                    else "dropped" if _had_emitter
                    else "no_emitter"
                )
            },
        )
    except Exception:
        try:
            write_telemetry_record(
                manifest_id=_site,
                event_type="engram.tool.engram_call",
                fields={"outcome": "init_failed"},
            )
        except Exception:
            pass

    return json.dumps(core._strip_agent_facing(batch_result))
