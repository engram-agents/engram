"""Thin-client HTTP scaffold for forum-native API calls.

Shared across ia.py, baton.py, and forum.py thin-client commands (UCS Slice E).
Endpoint-agnostic: each CLI defines its own routes; this module provides the
common HTTP client + error model so callers don't repeat urllib boilerplate.

Design goals:
- Zero external dependencies (stdlib urllib only — matches existing tool policy).
- Raise exceptions, not sys.exit — callers map to their own exit codes.
- Mirror the conventions already established in ia.py's cmd_dm (the first
  live thin-client proof, shipped in UCS Slice D).

Exit-code mapping (callers use these constants):
    EXIT_OK          = 0  — success
    EXIT_VALIDATION  = 2  — bad input / HTTP 4xx (mirrors forum.py)
    EXIT_UNREACHABLE = 3  — network unreachable / timeout (mirrors forum.py)
    EXIT_NOT_FOUND   = 4  — HTTP 404 (mirrors forum.py)
    EXIT_STATE       = 5  — state conflict (e.g. ColleagueGateError) [forum_api extension; not in forum.py]
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# Exit codes 0/2/3/4 mirror forum.py; EXIT_STATE=5 is a forum_api extension (ColleagueGateError).
EXIT_OK          = 0
EXIT_VALIDATION  = 2
EXIT_UNREACHABLE = 3
EXIT_NOT_FOUND   = 4
EXIT_STATE       = 5


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def engram_home() -> Path:
    """The ENGRAM home for the shared-bin flat copies (ia, forum, _status_derive, baton):
    $ENGRAM_HOME (default ~/.engram) when set and non-empty, expanded and absolute; a RELATIVE value is refused (ValueError naming it). A copy of
    tools/_owner_read.engram_home() (the canonical body): ia and forum run as flat copies in
    /home/agents-shared/bin with only this module beside them, so they cannot import _owner_read.
    tests/test_engram_home_parity_3863.py pins the copies AST-identical (the #3628 pattern)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


class ForumApiError(Exception):
    """Base class for forum API errors; carries exit_code for caller mapping."""

    def __init__(self, msg: str, exit_code: int = EXIT_UNREACHABLE) -> None:
        super().__init__(msg)
        self.exit_code = exit_code


class ForumNetworkError(ForumApiError):
    """Forum server unreachable — connection refused, timeout, DNS failure."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"forum unreachable — {cause}", EXIT_UNREACHABLE)


class ForumHttpError(ForumApiError):
    """Forum returned a non-2xx HTTP status.

    Attributes:
        status: HTTP status code.
        body: Raw response body (decoded utf-8, errors replaced).
    """

    def __init__(self, status: int, body: str) -> None:
        exit_code = EXIT_NOT_FOUND if status == 404 else EXIT_VALIDATION
        super().__init__(f"HTTP {status}: {body}", exit_code)
        self.status = status
        self.body = body


class ForumRedirectError(ForumNetworkError):
    """The forum answered with a redirect to a DIFFERENT origin, and the request carried the
    bearer token (#1459). It is not followed: the target never receives the header.

    A ForumNetworkError subclass on purpose (like ForumTokenFileError under ForumHttpError):
    every caller that already catches ForumNetworkError (baton, ia) handles it as "the forum
    as configured cannot be used", with the exit code of an unreachable forum.
    ``.target_origin`` is the redirect target's ``scheme://host[:port]``; the token is never
    part of the message.
    """

    def __init__(self, source_origin: str, target_origin: str) -> None:
        ForumApiError.__init__(
            self,
            f"forum at {source_origin} redirected to a different origin ({target_origin}); "
            f"not following it, because the request carries the forum bearer token. "
            f"Fix the forum URL to point at the server itself.",
            EXIT_UNREACHABLE,
        )
        self.source_origin = source_origin
        self.target_origin = target_origin


# ---------------------------------------------------------------------------
# Redirect policy (#1459): the bearer token never follows a redirect to another origin
# ---------------------------------------------------------------------------
# urllib's stock redirect handler re-sends the request's headers, Authorization included, to
# whatever the Location names. The forum does not redirect cross-origin today, but werkzeug does
# issue same-origin 308s (a doubled slash), and the class is every sender of the header. ONE
# policy lives here; every urllib sender opens through forum_urlopen() and none carries a copy.

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_tuple(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:  # a malformed port is its own origin, never equal to a valid one
        port = -1
    return (scheme, (parts.hostname or "").lower(), port if port is not None else _DEFAULT_PORTS.get(scheme))


def _origin_str(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{(parts.scheme or '').lower()}://{parts.netloc.rsplit('@', 1)[-1].lower()}"


def same_origin(url_a: str, url_b: str) -> bool:
    """True when both URLs have the same scheme, host and port (default ports normalised)."""
    return _origin_tuple(url_a) == _origin_tuple(url_b)


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect of a request that carries ``Authorization`` only to the SAME origin.

    A request with no Authorization header is untouched (the stock behaviour). A cross-origin
    redirect of one that has it raises ForumRedirectError before any request to the target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header("Authorization") and not same_origin(req.full_url, newurl):
            try:
                fp.close()
            except Exception:  # noqa: BLE001 - best effort, the refusal below is what matters
                pass
            raise ForumRedirectError(_origin_str(req.full_url), _origin_str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_forum_opener() -> urllib.request.OpenerDirector:
    """The one opener factory: urllib's default handlers with SameOriginRedirectHandler."""
    return urllib.request.build_opener(SameOriginRedirectHandler)


def forum_urlopen(req, timeout=None):
    """``urllib.request.urlopen`` for a forum request, under the same-origin redirect policy."""
    return build_forum_opener().open(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Forum token (#1459): ONE source for every forum client
# ---------------------------------------------------------------------------


class ForumTokenFileError(ForumHttpError):
    """The forum token file exists but is group- or world-readable (it must be 0600).

    A ForumHttpError subclass (status 0, no HTTP exchange happened) on purpose: every
    existing caller, including the prompt hooks, already catches ForumHttpError and turns it
    into one loud line, so a refused token file is never swallowed into silence.
    ``.body`` is the message; ``.path`` is the offending file.
    """

    def __init__(self, path: Path, msg: Optional[str] = None) -> None:
        if msg is None:
            msg = (f"forum token file {path} is readable by group or others; refusing to use it. "
                   f"Run: chmod 600 {path}")
        ForumApiError.__init__(self, msg, EXIT_VALIDATION)
        self.status = 0
        self.body = msg
        self.path = path


def _home_from(environ: Any) -> Path:
    """engram_home() semantics over an explicit mapping (same three rules, no os.environ)."""
    raw = environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


def forum_token_path(environ: Optional[Any] = None) -> Path:
    """Where a client's forum token file lives: ``<engram_home()>/forum-token``."""
    home = engram_home() if environ is None else _home_from(environ)
    return home / "forum-token"


def load_forum_token(environ: Optional[Any] = None) -> Optional[str]:
    """The caller's forum bearer token, or None when none is configured.

    Contract: ``ENGRAM_FORUM_TOKEN`` wins when set and non-empty; otherwise the file
    ``<engram_home()>/forum-token``, stripped of surrounding whitespace. An existing
    file that is group- or world-readable (``mode & 0o077``) is REFUSED with
    :class:`ForumTokenFileError` naming the path and ``chmod 600``. A symlinked file is
    judged by its TARGET (``os.stat`` follows the link; the monitors use ``stat -L``); a
    dangling symlink is refused too, never read as "no token". No env and no file
    (or an empty file) is None, not an error by itself: the server answers 401 and the
    caller's 401 handling says where the token belongs. The token is never logged here.
    """
    env = os.environ if environ is None else environ
    tok = (env.get("ENGRAM_FORUM_TOKEN") or "").strip()
    if tok:
        return tok
    path = forum_token_path(environ)
    try:
        st = os.stat(path)  # follows a symlink: the TARGET's mode is what is checked
    except OSError:
        if os.path.islink(path):  # a link whose target is gone is a broken setup, not "no token"
            raise ForumTokenFileError(
                path, f"forum token file {path} is a dangling symlink (its target does not "
                      f"exist); refusing to use it. Point the link at your 0600 token file or remove it")
        return None
    if st.st_mode & 0o077:
        raise ForumTokenFileError(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def auth_failure_line(status: int, body: str, environ: Optional[Any] = None) -> Optional[str]:
    """One actionable line for the forum's own auth refusals, else None.

    Recognises ONLY the guard's two shapes (401 ``authentication required``, 403
    ``identity mismatch``): any other 401/403 body (an app-level refusal such as "agent
    not authorized") keeps its own text. 401 names the token file and the env var; 403
    names the token's agent and the agent the request named.
    """
    if status not in (401, 403):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        where = str(forum_token_path(environ))
    except ValueError:
        where = "<ENGRAM_HOME>/forum-token"
    if status == 401 and data.get("error") == "authentication required":
        return (f"the forum refused the request (401): no valid forum token. Put your token in "
                f"{where} (mode 0600) or set ENGRAM_FORUM_TOKEN; the forum admin issues one "
                f"(python -m forum.admin token issue <agent>)")
    if status == 403 and data.get("error") == "identity mismatch":
        return (f"the forum refused the request (403 identity mismatch): your token (from "
                f"{where}) is for {str(data.get('token_agent', '?'))!r} but the request names "
                f"{str(data.get('named_agent', '?'))!r}")
    return None


class ForumAuthError(ForumHttpError):
    """The forum's own 401 / 403 (``authentication required`` / ``identity mismatch``).

    ``.body`` is the one actionable line (:func:`auth_failure_line`), so every caller
    that prints ``{e.status}: {e.body}`` is already loud and names the token file;
    ``.server_body`` keeps the raw response.
    """

    def __init__(self, status: int, server_body: str, line: str) -> None:
        super().__init__(status, line)
        self.server_body = server_body


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def forum_url_from_config(config: Dict[str, Any]) -> str:
    """Resolve the forum base URL.

    Precedence: config.json ``forum.url`` → ``$FORUM_URL`` → ``localhost:5002``.
    Mirrors ``ia.py._get_forum_url`` and ``forum.py._resolve_forum_url`` — the
    single definition that the module imports will replace both once the thick
    CLIs flip thin.
    """
    forum = config.get("forum")
    url = forum.get("url") if isinstance(forum, dict) else None
    # #4203: config.json is hand-edited; a non-dict ``forum`` or a non-string /
    # blank ``url`` is treated as absent, never raised on.
    if isinstance(url, str) and url.strip():
        return url
    return os.environ.get("FORUM_URL") or "http://localhost:5002"


# ---------------------------------------------------------------------------
# ForumClient
# ---------------------------------------------------------------------------


class ForumClient:
    """Thin synchronous HTTP client for the forum API.

    Usage::

        client = ForumClient.from_config(config)
        threads = client.get("/api/dm", params={"agent": name})["threads"]
        result  = client.post(f"/api/projects/{pid}/flip",
                              {"agent": name, "to_agent": "borges", "reason": "review"})

    Raises:
        ForumNetworkError: on connection failure / timeout.
        ForumHttpError:    on non-2xx HTTP status.

    Both inherit ``ForumApiError`` with ``.exit_code`` for caller mapping.

    Callers map to their own ``sys.exit`` calls:

        try:
            result = client.post(...)
        except ForumNetworkError as e:
            print(f"baton flip: {e}", file=sys.stderr); sys.exit(e.exit_code)
        except ForumHttpError as e:
            print(f"baton flip: {e.status}: {e.body}", file=sys.stderr); sys.exit(e.exit_code)
    """

    def __init__(self, base_url: str, timeout: int = 10, token: Optional[str] = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        # #1459: an explicit ``token=`` wins; else load_forum_token() runs ONCE, on the
        # first request (so a refused token file surfaces through the verbs' normal
        # ForumApiError handling, not as a constructor crash).
        self._token = token
        self._token_resolved = token is not None

    @classmethod
    def from_config(cls, config: Dict[str, Any], timeout: int = 10) -> "ForumClient":
        """Construct from a loaded config dict (the standard entry point)."""
        return cls(forum_url_from_config(config), timeout=timeout)

    # ------------------------------------------------------------------
    # Public verbs
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """HTTP GET; returns parsed JSON response dict.

        Args:
            path: API path (e.g. ``/api/dm``).
            params: Optional query-string dict — values are url-encoded.
        """
        url = self._build_url(path, params)
        req = urllib.request.Request(url, headers=self._headers({"Accept": "application/json"}))
        return self._execute(req)

    def post(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """HTTP POST with JSON body; returns parsed JSON response dict.

        Args:
            path: API path (e.g. ``/api/projects/PR-1/flip``).
            payload: Request body dict — serialized as JSON.
        """
        url = self._build_url(path)
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers=self._headers({
                "Content-Type": "application/json",
                "Accept": "application/json",
            }),
            method="POST",
        )
        return self._execute(req)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """``headers`` plus ``Authorization: Bearer <token>`` when a token is available."""
        if not self._token_resolved:
            self._token = load_forum_token()
            self._token_resolved = True
        if self._token:
            headers = dict(headers, Authorization=f"Bearer {self._token}")
        return headers

    def _build_url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _execute(self, req: urllib.request.Request) -> Dict[str, Any]:
        # Force IPv4-only resolution: example-host.local's mDNS record advertises
        # unreachable IPv6 candidates (a link-local address plus two
        # globally-scoped ones with no route on this LAN) ahead of the
        # working IPv4 address in getaddrinfo's result order. stdlib
        # socket.create_connection tries each candidate serially with the
        # full per-attempt timeout before falling through to the next, so
        # every dead IPv6 entry costs up to self._timeout seconds — enough,
        # compounded across 2-4 dead entries, to blow past the UserPromptSubmit
        # hook's outer budget (observed: 8-12s actual vs. a 5s hook budget).
        # curl doesn't hit this because it races families (Happy Eyeballs);
        # stdlib urllib has no such racing, so pin the family explicitly.
        real_getaddrinfo = socket.getaddrinfo

        def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

        socket.getaddrinfo = _ipv4_only_getaddrinfo
        try:
            with forum_urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            line = auth_failure_line(e.code, body)
            if line is not None:
                raise ForumAuthError(e.code, body, line)
            raise ForumHttpError(e.code, body)
        except urllib.error.URLError as e:
            raise ForumNetworkError(str(e.reason))
        finally:
            socket.getaddrinfo = real_getaddrinfo
