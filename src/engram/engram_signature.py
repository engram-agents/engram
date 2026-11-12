"""engram_signature -- the lineage-transparent signature line (engram-alpha#3450).

One function, ``build_signature``, shared by the ``engram_sign`` MCP tool
(``server.py``) and the ``tools/engram-sign.py`` CLI. It emits ONE predictable
line that an agent appends to every external communication:

    <emoji> <Full Name> . <marketing name> (<model id>) . ENGRAM <version> . <primary_user>'s agent . <site>

(the separator is U+00B7 MIDDLE DOT). When the harness reports no marketing
name, the middle part is just ``<model id>``. When the deployed build is
tagged and clean, the version renders as ``ENGRAM <version> (<alpha_tag>)``.
The final ``<site>`` segment is the project website (default ``PROJECT_SITE``),
so a reader of an externally posted signature can find where to get ENGRAM; it
is omitted when ``signature_site`` is set to the empty string.

Every field is read mechanically, none is typed from memory, and an unreadable
field is a REFUSAL naming the field and how to fix it -- never a placeholder
(house rule: a default that hides a failed read is forbidden). Read-only; no
network; no writes.

Field sources:
  emoji / name / human / lineage  <data_dir>/config.json:
                                  signature_emoji, display_name, primary_user,
                                  self_lineage
  site                            config.json signature_site, else PROJECT_SITE
  model                           the session transcript: newest JSONL row with
                                  attachment.type == "model"
                                  (attachment.identity.modelId + marketingName);
                                  fallback: newest row with message.model
                                  (id only).  Codex rollout (#3922): the newest
                                  top-level type == "turn_context" record's
                                  payload.model (id only; no nested model key
                                  is read).  Claude + Codex evidence in one
                                  transcript refuses ("mixed-harness").
                                  This is a TURN claim: the newest turn record
                                  of the bound transcript, not a statement
                                  about the whole session (true of the Claude
                                  path too).
  family check                    engram_core.model_id_to_lineage(model id)
                                  must equal self_lineage
  version                         <plugin root>/plugin.json "version"; plus
                                  alpha_tag from <data_dir>/.deployed-version

Transcript discovery: the session id is the ``session_id`` argument, else
``CLAUDE_CODE_SESSION_ID``, else ``CLAUDE_SESSION_ID``, else
``ANTHROPIC_SESSION_ID`` in the environment; its marker is
``<data_dir>/sessions/<id>.json``. With no id the tool REFUSES
(``session_marker``). MEASURED (2026-10-06, two seats, Claude Code 2.1.289 and
2.1.291, Linux): the harness sets ``CLAUDE_CODE_SESSION_ID`` in the Bash tool
environment AND in the live MCP server process, so the env read inside
``build_signature`` identifies the calling session on both paths. The older
names are read as fallbacks for harnesses that use them. There is no
marker-guessing fallback (e.g. "newest marker"): with two overlapping sessions
a guess signs the wrong caller with the other session's model, a confidently
wrong line on the one field this tool exists to make trustworthy.

Plugin root: ``CLAUDE_PLUGIN_ROOT`` when set, else the nearest parent of this
file holding BOTH ``plugin.json`` and ``server.py`` (never a fixed
``parents[N]``: the plugin build flattens the source layout, see CLAUDE.md).
A source checkout has no ``plugin.json``, so it refuses -- accepted.

Trust model: the transcript is a local file the user can edit, so the signature
is DECLARED provenance, not attestation or cryptographic proof.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

SEP = " · "

# The project's own public address. A constant is honest here: it is identical
# for every install and shipped by the project, so it is not a per-seat fact and
# not a read that can fail.
#
# Optional config override ``signature_site`` in config.json. CONTRACT:
#   - key ABSENT            -> PROJECT_SITE (a true optional: the default is the
#                              project's address, not a hidden failed read)
#   - non-empty string      -> that string, verbatim after .strip()
#   - "" / whitespace only  -> segment OMITTED; the line ends at "<user>'s agent"
#   - any non-string type   -> REFUSE (field="signature_site"). House rule: a
#     wrong-type value is an unreadable field, not an absent one. This
#     deliberately differs from the config reads in #4232 (wrong-type as
#     absent); do not "fix" it toward #4232.
PROJECT_SITE = "engram-agents.org"


@dataclass
class SignatureResult:
    ok: bool
    line: str | None = None
    field: str | None = None
    reason: str | None = None
    parts: dict = _dc_field(default_factory=dict)


class _Refusal(Exception):
    def __init__(self, field_name: str, reason: str, code: str | None = None,
                 model_id: str | None = None):
        super().__init__(reason)
        self.field = field_name
        self.reason = reason
        # Machine-readable kind, set only where a caller other than the signature
        # (the #4596 detector) must tell refusals apart: "mixed_harness",
        # "unmapped_model", "no_model_row", "no_usable_model".  None elsewhere.
        self.code = code
        # The offending id of an "unmapped_model" refusal, so the #4596 gate can tell which provider
        # it belongs to; None elsewhere.
        self.model_id = model_id


# ---------------------------------------------------------------- config

def _read_config(data_dir: Path) -> dict:
    path = data_dir / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise _Refusal("config", f"cannot read {path}: {e}")
    if not isinstance(cfg, dict):
        raise _Refusal("config", f"{path} is not a JSON object")
    return cfg


def _cfg_str(cfg: dict, key: str, data_dir: Path) -> str:
    v = cfg.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _Refusal(
            key, f"{key} is missing or empty: set {key} in "
                 f"{data_dir / 'config.json'}")
    return v.strip()


def _cfg_site(cfg: dict, data_dir: Path) -> str:
    """The site segment ("" = omitted). See the PROJECT_SITE contract."""
    if "signature_site" not in cfg:
        return PROJECT_SITE
    v = cfg["signature_site"]
    if not isinstance(v, str):
        raise _Refusal(
            "signature_site",
            f"signature_site in {data_dir / 'config.json'} must be a string "
            f"(or removed), found {type(v).__name__}")
    return v.strip()


# ------------------------------------------------------------ transcript

def _load_marker(path: Path) -> dict | None:
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return m if isinstance(m, dict) else None


_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                "ANTHROPIC_SESSION_ID")


def _resolve_session_id(session_id: str | None) -> str:
    """payload/arg id, else the first non-blank env name in ``_SESSION_ENV``.
    No id is a refusal: a marker without the id is not an identity."""
    for cand in (session_id, *(os.environ.get(n) for n in _SESSION_ENV)):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    raise _Refusal(
        "session_marker",
        "no session id: pass session_id (MCP payload / --session-id) or run "
        "where the harness sets CLAUDE_CODE_SESSION_ID")


def _find_marker(data_dir: Path, session_id: str | None) -> dict:
    sdir = data_dir / "sessions"
    sid = _resolve_session_id(session_id)
    if "/" in sid or "\\" in sid or sid in (".", ".."):
        raise _Refusal("session_marker", f"invalid session_id {sid!r}")
    marker = _load_marker(sdir / f"{sid}.json")
    if marker is None:
        raise _Refusal(
            "session_marker",
            f"no readable session marker {sdir / (sid + '.json')}: pass a "
            "session_id whose marker exists")
    return marker


def _has_claude_source(rows: list) -> bool:
    """True when any row is a Claude model source (the two shapes the Claude
    path reads): an attachment model row with a non-blank ``identity.modelId``,
    or a ``message.model`` starting ``claude-``."""
    for r in rows:
        att = r.get("attachment")
        if isinstance(att, dict) and att.get("type") == "model":
            ident = att.get("identity")
            mid = ident.get("modelId") if isinstance(ident, dict) else None
            if isinstance(mid, str) and mid.strip():
                return True
        msg = r.get("message")
        mid = msg.get("model") if isinstance(msg, dict) else None
        if isinstance(mid, str) and mid.strip().startswith("claude-"):
            return True
    return False


def _lineage_of(model_id: str) -> str:
    import engram_core as core
    return core.model_id_to_lineage(model_id)


def _read_model(transcript: Path) -> tuple[str, str | None]:
    """(model_id, marketing_name|None) for the bound transcript.

    Claude transcripts: the newest model attachment row, else the newest
    assistant ``message.model`` (marketing name None).  Codex rollouts (#3922):
    the newest top-level ``type == "turn_context"`` record's ``payload.model``
    (marketing name None); ONLY that key is read -- never
    ``payload.collaboration_mode.settings.model`` or any other nested ``model``
    -- and a newest turn_context record with no usable model REFUSES rather
    than falling back to an older turn.  A transcript holding BOTH a Claude
    source and a Codex turn_context refuses ("mixed-harness"); no format is
    preferred.

    This is a TURN claim, not a session claim: the evidence is the newest turn
    record of the bound transcript (a pre-existing property of the Claude path
    too -- the newest attachment/assistant row), not a statement that the whole
    session ran on that model.  The transcript is a user-editable local file;
    the result is declared provenance, not attestation.
    """
    try:
        raw = transcript.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise _Refusal("transcript", f"cannot read transcript {transcript}: {e}")
    return _model_from_rows(_parse_rows(raw.splitlines()), transcript)


def _parse_rows(lines) -> list:
    """The JSON-object rows among ``lines``; a malformed line or a non-object is skipped."""
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def _model_from_rows(rows: list, transcript) -> tuple[str, str | None]:
    """The row-level model rules, over already-parsed rows (shared by ``_read_model``,
    which reads the whole file, and ``read_model_tail``, which reads a window).
    ``transcript`` is only the name used in refusal messages.  See ``_read_model``
    for the precedence rules; raises ``_Refusal`` exactly as it always has."""
    turns = [r for r in rows if r.get("type") == "turn_context"]
    if turns:
        if _has_claude_source(rows):
            raise _Refusal(
                "model",
                f"transcript {transcript} holds both Claude model evidence and "
                "Codex turn_context records (mixed-harness): cannot determine "
                "the running model, refusing rather than ranking the sources",
                code="mixed_harness")
        payload = turns[-1].get("payload")
        mid = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(mid, str) and mid.strip():
            return mid.strip(), None
        raise _Refusal(
            "model",
            f"the newest turn_context record in {transcript} has no usable "
            "payload.model (missing, non-string or blank): refusing rather "
            "than falling back to an older turn or a nested model key",
            code="no_usable_model")
    # Primary: harness model attachment (id + marketing name).
    for r in reversed(rows):
        att = r.get("attachment")
        if isinstance(att, dict) and att.get("type") == "model":
            ident = att.get("identity")
            mid = ident.get("modelId") if isinstance(ident, dict) else None
            if isinstance(mid, str) and mid.strip():
                name = ident.get("marketingName")
                name = name.strip() if isinstance(name, str) else ""
                return mid.strip(), (name or None)
    # Fallback: newest assistant row's message.model (id only).  Walk newest to
    # oldest and skip ONLY a non-string, blank or exactly "<synthetic>" (the
    # harness's own placeholder) model; the first remaining id is the running
    # model and decides.  An unmapped real id refuses -- an older row never
    # signs for a model that is no longer running.  The attachment pass above
    # does not skip: that row is the harness's own identity statement, so a
    # synthetic id there would be a real anomaly worth refusing.
    saw_assistant = False
    for r in reversed(rows):
        msg = r.get("message")
        if isinstance(msg, dict) and "model" in msg:
            saw_assistant = True
        mid = msg.get("model") if isinstance(msg, dict) else None
        if not isinstance(mid, str):
            continue
        mid = mid.strip()
        if not mid or mid == "<synthetic>":
            continue
        if mid.startswith("claude-") or _lineage_of(mid):
            return mid, None
        raise _Refusal(
            "model",
            f"the newest assistant row with a usable model id in {transcript} "
            f"has model id {mid!r}, "
            "which does not map to a lineage: refusing rather than signing "
            "with an older row's model", code="unmapped_model", model_id=mid)
    if saw_assistant:
        raise _Refusal(
            "model",
            f"every assistant row in {transcript} has a placeholder, blank or "
            "non-string model (no usable model id): cannot determine the "
            "running model", code="no_usable_model")
    raise _Refusal(
        "model",
        f"transcript {transcript} has neither a model attachment row nor an "
        "assistant row with message.model: cannot determine the running model",
        code="no_model_row")


# ------------------------------------------------------- bounded tail read

@dataclass
class TailResult:
    """``read_model_tail``'s answer: ``reason is None`` means ``model_id`` is the
    running model of the newest turn in the window (``marketing_name`` or None);
    otherwise ``reason`` is the UNKNOWN reason (the #4596 closed set):
    ``transcript_not_found``, ``transcript_unreadable``, ``no_model_row``,
    ``window_exceeded``, ``unmapped_model`` or ``mixed_harness``.  For ``unmapped_model``
    (and only that reason) ``model_id`` carries the offending id, so a caller can tell which
    provider it belongs to."""
    model_id: str | None = None
    marketing_name: str | None = None
    reason: str | None = None


def read_model_tail(transcript, max_bytes: int) -> TailResult:
    """``_read_model`` over at most ``max_bytes`` taken from the END of the file (#4596).

    Same row rules (``_model_from_rows``), different window.  When the read starts
    past offset 0 the first, partial line is discarded; a malformed line is
    skipped as always.  A window with no complete line, or whose rows carry no
    model while the file is larger than the window, is ``window_exceeded`` (a
    newest entry larger than the bound lands here): never an older row's id, and
    never a guess.  Never raises.  Opens non-blocking and refuses anything that is
    not a regular file, so a FIFO cannot hang a hook."""
    import errno
    import stat
    try:
        fd = os.open(os.fspath(transcript), os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return TailResult(reason="transcript_not_found")
    except NotADirectoryError:
        return TailResult(reason="transcript_not_found")
    except OSError:
        return TailResult(reason="transcript_unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return TailResult(reason="transcript_unreadable")
        size = st.st_size
        start = max(0, size - max(0, int(max_bytes)))
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], size - start
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
    except OSError as e:
        return TailResult(reason="transcript_unreadable" if e.errno != errno.ENOENT else "transcript_not_found")
    finally:
        os.close(fd)
    truncated = start > 0
    if truncated:
        nl = data.find(b"\n")
        data = b"" if nl < 0 else data[nl + 1:]
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        return TailResult(reason="window_exceeded" if truncated else "no_model_row")
    try:
        model_id, name = _model_from_rows(_parse_rows(text.splitlines()), transcript)
    except _Refusal as r:
        code = r.code
        if code in ("no_model_row", "no_usable_model"):
            code = "window_exceeded" if truncated else "no_model_row"
        elif code not in ("mixed_harness", "unmapped_model"):
            code = "internal_error"  # a refusal this reader does not recognise (or one with no code)
        return TailResult(model_id=r.model_id if code == "unmapped_model" else None, reason=code)
    return TailResult(model_id=model_id, marketing_name=name)


# ----------------------------------------------- the calling entry (#4596)

CALL_WINDOW_BYTES = 262144  # the tail window both the calling-row scan and its fallback read


def _read_window(transcript) -> tuple:
    """(data, truncated, reason): the last ``CALL_WINDOW_BYTES`` of ``transcript``, opened non-blocking
    and refused unless a regular file (a FIFO cannot hang a hook).  When ``truncated`` the first, partial
    line is already dropped.  ``reason`` is ``transcript_not_found`` / ``transcript_unreadable`` or None."""
    import errno
    import stat
    try:
        fd = os.open(os.fspath(transcript), os.O_RDONLY | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return b"", False, "transcript_not_found"
    except OSError:
        return b"", False, "transcript_unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return b"", False, "transcript_unreadable"
        size = st.st_size
        start = max(0, size - CALL_WINDOW_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], size - start
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
    except OSError as e:
        return b"", False, ("transcript_not_found" if e.errno == errno.ENOENT else "transcript_unreadable")
    finally:
        os.close(fd)
    truncated = start > 0
    if truncated:
        nl = data.find(b"\n")
        data = b"" if nl < 0 else data[nl + 1:]
    return data, truncated, None


def _calling_row_model(data: bytes, needle: bytes, tool_use_id: str):
    """(found, model_id_or_None): the newest assistant row in ``data`` holding a ``tool_use`` block whose id
    is ``tool_use_id``.  Bytes prefilter first (``needle`` is the id's bytes); only a matching line is parsed.
    ``model_id`` is None when that row carries no usable ``message.model`` (missing, blank, or the harness
    placeholder ``<synthetic>``)."""
    for line in reversed(data.split(b"\n")):
        if needle not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        if not isinstance(msg, dict) or (row.get("type") != "assistant" and msg.get("role") != "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if not any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id
                   for b in content):
            continue
        mid = msg.get("model")
        mid = mid.strip() if isinstance(mid, str) else ""
        return True, (mid if mid and mid != "<synthetic>" else None)
    return False, None


def model_of_call(transcript_path, tool_use_id, budget_s, step_s) -> tuple:
    """``(model_id | None, source, reason | None)``: the model that made one tool call (#4596).

    The harness starts a PreToolUse hook before the calling assistant row reaches the transcript, so a
    single read never sees it.  This polls every ``step_s`` seconds, for up to ``budget_s``, a seek-bounded
    read of the last ``CALL_WINDOW_BYTES`` for the assistant row holding a ``tool_use`` block with id
    ``tool_use_id`` (the id's bytes first, then parse only that line) and returns its ``message.model`` with
    source ``calling_entry``.  The scan always runs at least once.

    * A found row with no usable model returns ``(None, "calling_entry", "no_model_row")`` at once: waiting
      would not change it.
    * On budget expiry it falls back to the newest model row of the same window (``read_model_tail``) with
      source ``fallback_newest_row``: that is the PREVIOUS reply's model, not this call's.  A fallback that
      finds no model row after a poll reports ``budget_expired_no_row``.  An unmapped id is returned WITH its
      id and reason ``unmapped_model``.
    * A calling row larger than the window cannot be found by the scan; it ends in the fallback.
    * A missing or unreadable transcript returns at once (the fallback would say the same).
    * Never raises: every failure is a reason code (the closed set of ``read_model_tail``, plus
      ``transcript_path_missing``, ``budget_expired_no_row`` and ``internal_error``)."""
    try:
        return _model_of_call(transcript_path, tool_use_id, budget_s, step_s)
    except Exception:  # noqa: BLE001
        return None, "fallback_newest_row", "internal_error"


def _model_of_call(transcript_path, tool_use_id, budget_s, step_s) -> tuple:
    fb = "fallback_newest_row"
    if isinstance(transcript_path, str):
        if not transcript_path.strip():
            return None, fb, "transcript_path_missing"
    elif not isinstance(transcript_path, os.PathLike):
        return None, fb, "transcript_path_missing"
    try:
        budget = max(0.0, float(budget_s))
    except (TypeError, ValueError):
        budget = 0.0
    try:
        step = max(0.001, float(step_s))
    except (TypeError, ValueError):
        step = 0.02
    polled = False
    if isinstance(tool_use_id, str) and tool_use_id:
        needle = tool_use_id.encode("utf-8", errors="replace")
        deadline = time.monotonic() + budget
        while True:
            data, _truncated, reason = _read_window(transcript_path)
            if reason is not None:
                return None, fb, reason
            found, mid = _calling_row_model(data, needle, tool_use_id)
            if found:
                return (mid, "calling_entry", None) if mid else (None, "calling_entry", "no_model_row")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(step, remaining))
        polled = True
    res = read_model_tail(transcript_path, CALL_WINDOW_BYTES)
    if res.reason is None:
        return res.model_id, fb, None
    if res.reason == "unmapped_model":
        return res.model_id, fb, "unmapped_model"
    if res.reason == "no_model_row" and polled:
        return None, fb, "budget_expired_no_row"
    return None, fb, res.reason


# --------------------------------------------------------------- version

# Inline copy, not an import: this is a stdlib-only leaf module that server.py and the tools import, so it
# cannot depend on a sibling helper. Pinned identical to every other copy by tests/test_bounded_sibling_lookups_3983.py.
def _bounded_parents(here):
    """Directories to search for a sibling runtime file: `here`'s own directory
    up to and INCLUDING its own tree root, nearest first, never above it (#3983).
    The tree root is the nearest ancestor holding engram_core.py (the plugin root
    installed, src/engram/ in a source checkout; an essential-tier file, so every
    build ships it).  server.py is no marker: viz/server.py exists.  With no
    engram_core.py above, only the own directory and its parent are searched
    (the conservative case).  An unreadable directory is skipped
    (never taken for the root), so the walk still ends at the first readable root."""
    chain = []
    for d in here.parents:
        chain.append(d)
        try:
            if (d / "engram_core.py").exists():
                return chain
        except OSError:
            continue  # unreadable: not a root we can see; keep climbing
    return chain[:2]


def _find_plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for d in _bounded_parents(here):
        try:
            if (d / "plugin.json").is_file() and (d / "server.py").is_file():
                return d
        except OSError:
            continue
    raise _Refusal(
        "version",
        f"no plugin root found walking up from {here} (looked for a directory "
        "holding plugin.json and server.py): set CLAUDE_PLUGIN_ROOT")


def _read_plugin_version(plugin_root: Path) -> str:
    path = plugin_root / "plugin.json"
    try:
        v = json.loads(path.read_text(encoding="utf-8")).get("version")
    except Exception as e:
        raise _Refusal("version", f"cannot read ENGRAM version from {path}: {e}")
    if not isinstance(v, str) or not v.strip():
        raise _Refusal("version", f"{path} has no non-empty \"version\"")
    return v.strip()


def _alpha_tag(data_dir: Path) -> str | None:
    """The deploy tag, only when the stamp says the build was clean.

    A missing/unreadable ``.deployed-version`` is NOT a refusal: it is the
    deploy stamp, absent on a never-stamped install, and the plugin version
    alone is the identity -- a true optional, not a failed read of a required
    field.
    """
    try:
        text = (data_dir / ".deployed-version").read_text(encoding="utf-8")
    except OSError:
        return None
    kv = {}
    for ln in text.splitlines():
        k, sep, v = ln.partition("=")
        if sep and k.strip():
            kv[k.strip()] = v.strip()
    if kv.get("alpha_dirty") == "clean" and kv.get("alpha_tag"):
        return kv["alpha_tag"]
    return None


# ------------------------------------------------------------------ main

def build_signature(data_dir: Path, session_id: str | None = None,
                    plugin_root: Path | None = None) -> SignatureResult:
    """Build the signature line, or refuse naming the field to fix."""
    data_dir = Path(data_dir)
    try:
        cfg = _read_config(data_dir)
        emoji = _cfg_str(cfg, "signature_emoji", data_dir)
        name = _cfg_str(cfg, "display_name", data_dir)
        human = _cfg_str(cfg, "primary_user", data_dir)
        lineage = _cfg_str(cfg, "self_lineage", data_dir)
        site = _cfg_site(cfg, data_dir)

        marker = _find_marker(data_dir, session_id)
        tp = marker.get("transcript_path")
        if not isinstance(tp, str) or not tp.strip():
            raise _Refusal(
                "transcript_path",
                f"session marker for {marker.get('session_id')!r} has no "
                "transcript_path: the SessionStart hook did not record it")
        model_id, marketing = _read_model(Path(tp))

        import engram_core as core
        measured = core.model_id_to_lineage(model_id)
        if not measured:
            raise _Refusal(
                "model_family",
                f"model id {model_id} does not map to a provider:family lineage")
        if measured != lineage:
            raise _Refusal(
                "model_family",
                f"running model {model_id} is family {measured} but "
                f"self_lineage is {lineage}: a seat under a foreign family "
                "must not sign (model-identity gate)")

        root = Path(plugin_root) if plugin_root is not None else _find_plugin_root()
        try:
            version = _read_plugin_version(root)
        except _Refusal:
            raise
        except Exception as e:
            raise _Refusal("version", f"cannot read ENGRAM version: {e}")
        tag = _alpha_tag(data_dir)
    except _Refusal as r:
        return SignatureResult(ok=False, field=r.field, reason=r.reason)
    except Exception as e:  # never let an unforeseen read become a line
        return SignatureResult(ok=False, field="unknown", reason=str(e))

    model_part = f"{marketing} ({model_id})" if marketing else model_id
    ver_part = f"ENGRAM {version}" + (f" ({tag})" if tag else "")
    segs = [f"{emoji} {name}", model_part, ver_part, f"{human}'s agent"]
    if site:
        segs.append(site)
    line = SEP.join(segs)
    return SignatureResult(ok=True, line=line, parts={
        "emoji": emoji, "display_name": name, "model_id": model_id,
        "marketing_name": marketing, "family": measured,
        "version": version, "alpha_tag": tag, "primary_user": human,
        "site": site,
    })
