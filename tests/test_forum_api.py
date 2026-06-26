"""Tests for src/engram/tools/forum_api.py — the thin-client HTTP scaffold.

Covers:
- URL resolution (forum_url_from_config)
- ForumClient.get / .post happy path
- ForumNetworkError on connection failure
- ForumHttpError on non-2xx HTTP response
- ForumClient.from_config construction
- URL building with query params
"""
import io
import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from tools.forum_api import (
    EXIT_IO,
    EXIT_OK,
    EXIT_VALIDATION,
    ForumApiError,
    ForumClient,
    ForumHttpError,
    ForumNetworkError,
    forum_url_from_config,
)


# ---------------------------------------------------------------------------
# forum_url_from_config
# ---------------------------------------------------------------------------


def test_forum_url_from_config_uses_config_key():
    assert forum_url_from_config({"forum": {"url": "http://192.168.1.10:5002"}}) == "http://192.168.1.10:5002"


def test_forum_url_from_config_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("FORUM_URL", "http://env-host:5002")
    assert forum_url_from_config({}) == "http://env-host:5002"


def test_forum_url_from_config_falls_back_to_localhost(monkeypatch):
    monkeypatch.delenv("FORUM_URL", raising=False)
    assert forum_url_from_config({}) == "http://localhost:5002"


def test_forum_url_config_beats_env(monkeypatch):
    monkeypatch.setenv("FORUM_URL", "http://env-host:5002")
    assert forum_url_from_config({"forum": {"url": "http://config-host:5002"}}) == "http://config-host:5002"


# ---------------------------------------------------------------------------
# ForumClient construction
# ---------------------------------------------------------------------------


def test_from_config_constructs(monkeypatch):
    monkeypatch.delenv("FORUM_URL", raising=False)
    client = ForumClient.from_config({})
    assert client._base == "http://localhost:5002"


def test_trailing_slash_stripped():
    client = ForumClient("http://host:5002/")
    assert client._base == "http://host:5002"


# ---------------------------------------------------------------------------
# Helper: mock a successful urlopen response
# ---------------------------------------------------------------------------


def _mock_response(data: dict, status: int = 200):
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# ForumClient.get
# ---------------------------------------------------------------------------


def test_get_happy_path():
    client = ForumClient("http://localhost:5002")
    expected = {"threads": ["ariadne", "borges"]}
    with patch("urllib.request.urlopen", return_value=_mock_response(expected)) as mock_open:
        result = client.get("/api/dm", params={"agent": "luria"})
    assert result == expected
    called_url = mock_open.call_args[0][0].full_url
    assert "/api/dm" in called_url
    assert "agent=luria" in called_url


def test_get_builds_url_without_params():
    client = ForumClient("http://localhost:5002")
    with patch("urllib.request.urlopen", return_value=_mock_response({"ok": True})) as mock_open:
        client.get("/api/dm")
    assert mock_open.call_args[0][0].full_url == "http://localhost:5002/api/dm"


def test_get_network_error_raises_forum_network_error():
    client = ForumClient("http://localhost:5002")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(ForumNetworkError) as exc_info:
            client.get("/api/dm")
    assert exc_info.value.exit_code == EXIT_IO
    assert "unreachable" in str(exc_info.value)


def test_get_http_error_raises_forum_http_error():
    client = ForumClient("http://localhost:5002")
    err = urllib.error.HTTPError("http://localhost:5002/api/dm", 404, "Not Found", {}, io.BytesIO(b"not found"))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ForumHttpError) as exc_info:
            client.get("/api/dm")
    assert exc_info.value.status == 404
    assert exc_info.value.exit_code == EXIT_VALIDATION


# ---------------------------------------------------------------------------
# ForumClient.post
# ---------------------------------------------------------------------------


def test_post_happy_path():
    client = ForumClient("http://localhost:5002")
    expected = {"seq": 5, "ts": "2026-06-26T17:00:00Z"}
    payload = {"agent": "luria", "body": "hello borges"}
    with patch("urllib.request.urlopen", return_value=_mock_response(expected)) as mock_open:
        result = client.post("/api/dm/borges", payload)
    assert result == expected
    req = mock_open.call_args[0][0]
    assert req.method == "POST"
    assert req.data == json.dumps(payload).encode()
    assert req.get_header("Content-type") == "application/json"


def test_post_network_error_raises_forum_network_error():
    client = ForumClient("http://localhost:5002")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        with pytest.raises(ForumNetworkError):
            client.post("/api/projects/PR-1/flip", {"agent": "luria"})


def test_post_http_400_raises_forum_http_error():
    client = ForumClient("http://localhost:5002")
    err = urllib.error.HTTPError(
        "http://localhost:5002/api/projects/PR-1/flip", 400, "Bad Request", {},
        io.BytesIO(b'{"error": "invalid participant"}')
    )
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ForumHttpError) as exc_info:
            client.post("/api/projects/PR-1/flip", {})
    assert exc_info.value.status == 400
    assert "invalid participant" in exc_info.value.body


# ---------------------------------------------------------------------------
# ForumApiError hierarchy
# ---------------------------------------------------------------------------


def test_forum_network_error_exit_code():
    e = ForumNetworkError("connection refused")
    assert e.exit_code == EXIT_IO
    assert isinstance(e, ForumApiError)


def test_forum_http_error_exit_code():
    e = ForumHttpError(422, "unprocessable")
    assert e.exit_code == EXIT_VALIDATION
    assert e.status == 422
    assert isinstance(e, ForumApiError)
