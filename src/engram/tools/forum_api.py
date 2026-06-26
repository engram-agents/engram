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
    EXIT_OK         = 0  — success
    EXIT_VALIDATION = 1  — bad input / HTTP 4xx
    EXIT_IO         = 2  — network unreachable / timeout
    EXIT_STATE      = 3  — state conflict
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

# Standard exit codes — mirror ia.py and baton.py so callers don't need to import both.
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ForumApiError(Exception):
    """Base class for forum API errors; carries exit_code for caller mapping."""

    def __init__(self, msg: str, exit_code: int = EXIT_IO) -> None:
        super().__init__(msg)
        self.exit_code = exit_code


class ForumNetworkError(ForumApiError):
    """Forum server unreachable — connection refused, timeout, DNS failure."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"forum unreachable — {cause}", EXIT_IO)


class ForumHttpError(ForumApiError):
    """Forum returned a non-2xx HTTP status.

    Attributes:
        status: HTTP status code.
        body: Raw response body (decoded utf-8, errors replaced).
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}", EXIT_VALIDATION)
        self.status = status
        self.body = body


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
    return (
        (config.get("forum") or {}).get("url")
        or os.environ.get("FORUM_URL")
        or "http://localhost:5002"
    )


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
            print(f"baton flip: {e}", file=sys.stderr); sys.exit(EXIT_IO)
        except ForumHttpError as e:
            print(f"baton flip: {e.status}: {e.body}", file=sys.stderr); sys.exit(EXIT_VALIDATION)
    """

    def __init__(self, base_url: str, timeout: int = 10) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

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
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
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
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        return self._execute(req)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _execute(self, req: urllib.request.Request) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ForumHttpError(e.code, body)
        except urllib.error.URLError as e:
            raise ForumNetworkError(str(e.reason))
