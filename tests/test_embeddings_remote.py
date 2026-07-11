"""Tests for the remote DeepInfra embedding client (app.services.embeddings_remote)."""
from __future__ import annotations

import httpx
import pytest
from dotenv import load_dotenv

from app.services import embeddings_remote
from tests.conftest import FakeResponse

load_dotenv()


def _deepinfra_payload(vector):
    return {"data": [{"embedding": vector}]}


def test_embed_posts_to_deepinfra_with_auth(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse(_deepinfra_payload([0.5, 0.6]))

    monkeypatch.setenv("EMBEDDING_API_KEY", "secret-key")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setattr(httpx, "post", fake_post)

    vector = embeddings_remote.embed("hello")

    assert vector == [0.5, 0.6]
    assert captured["url"] == "https://api.deepinfra.com/v1/embeddings"
    assert captured["headers"] == {"Authorization": "Bearer secret-key"}
    assert captured["json"] == {
        "model": embeddings_remote.EMBEDDING_MODEL,
        "input": "hello",
        "dimensions": embeddings_remote.VECTOR_SIZE,
    }
    assert captured["timeout"] == 30.0


def test_embed_requests_configured_dimensions(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["json"] = json
        return FakeResponse(_deepinfra_payload([0.1] * embeddings_remote.VECTOR_SIZE))

    monkeypatch.setattr(httpx, "post", fake_post)

    embeddings_remote.embed("hi")

    # We always pin the requested dimension to VECTOR_SIZE so the returned vector
    # matches the Qdrant collection size (the 2560-vs-1024 mismatch this fixes).
    assert captured["json"]["dimensions"] == embeddings_remote.VECTOR_SIZE


def test_embed_honours_base_url_override(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        return FakeResponse(_deepinfra_payload([1.0]))

    # Trailing slash should be stripped so we never produce `//embeddings`.
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://example.test/v2/")
    monkeypatch.setattr(httpx, "post", fake_post)

    embeddings_remote.embed("x")

    assert captured["url"] == "https://example.test/v2/embeddings"


def test_embed_raises_on_http_error(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return FakeResponse({}, status_code=401)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        embeddings_remote.embed("boom")
