"""engram_signature -- the lineage-transparent signature line (engram-alpha#3450).

One function, ``build_signature``, shared by the ``engram_sign`` MCP tool
(``server.py``) and the ``tools/engram-sign.py`` CLI. It emits ONE predictable
line that an agent appends to every external communication:

    <emoji> <Full Name> . <marketing name> (<model id>) . ENGRAM <version> . <primary_user>'s agent

(the separator is U+00B7 MIDDLE DOT). When the harness reports no marketing
name, the middle part is just ``<model id>``. When the deployed build is
tagged and clean, the version renders as ``ENGRAM <version> (<alpha_tag>)``.

Every field is read mechanically, none is typed from memory, and an unreadable
field is a REFUSAL naming the field and how to fix it -- never a placeholder
(house rule: a default that hides a failed read is forbidden). Read-only; no
network; no writes.

Field sources:
  emoji / name / human / lineage  <data_dir>/config.json:
                                  signature_emoji, display_name, primary_user,
                                  self_lineage
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
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

SEP = " · "


@dataclass
class SignatureResult:
    ok: bool
    line: str | None = None
    field: str | None = None
    reason: str | None = None
    parts: dict = _dc_field(default_factory=dict)


class _Refusal(Exception):
    def __init__(self, field_name: str, reason: str):
        super().__init__(reason)
        self.field = field_name
        self.reason = reason


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
    rows = []
    for ln in raw.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    turns = [r for r in rows if r.get("type") == "turn_context"]
    if turns:
        if _has_claude_source(rows):
            raise _Refusal(
                "model",
                f"transcript {transcript} holds both Claude model evidence and "
                "Codex turn_context records (mixed-harness): cannot determine "
                "the running model, refusing rather than ranking the sources")
        payload = turns[-1].get("payload")
        mid = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(mid, str) and mid.strip():
            return mid.strip(), None
        raise _Refusal(
            "model",
            f"the newest turn_context record in {transcript} has no usable "
            "payload.model (missing, non-string or blank): refusing rather "
            "than falling back to an older turn or a nested model key")
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
    # Fallback: newest assistant row's message.model (id only).  Rows whose id
    # does not start with "claude-" (e.g. a harness-synthetic "<synthetic>"
    # row) are skipped HERE ONLY, so an older real assistant row is used
    # instead of refusing a healthy seat.  The attachment pass above does not
    # skip: that row is the harness's own identity statement, so a synthetic
    # id there would be a real anomaly worth refusing.
    for r in reversed(rows):
        msg = r.get("message")
        mid = msg.get("model") if isinstance(msg, dict) else None
        if (isinstance(mid, str) and mid.strip()
                and mid.strip().startswith("claude-")):
            return mid.strip(), None
    raise _Refusal(
        "model",
        f"transcript {transcript} has neither a model attachment row nor an "
        "assistant row with message.model: cannot determine the running model")


# --------------------------------------------------------------- version

def _find_plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for d in here.parents:
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
    line = SEP.join([f"{emoji} {name}", model_part, ver_part,
                     f"{human}'s agent"])
    return SignatureResult(ok=True, line=line, parts={
        "emoji": emoji, "display_name": name, "model_id": model_id,
        "marketing_name": marketing, "family": measured,
        "version": version, "alpha_tag": tag, "primary_user": human,
    })
