"""Tests for the Qdrant wrapper (app.services.qdrant_store)."""
from __future__ import annotations

from app.services import qdrant_store
from qdrant_client.models import Distance


class FakeQdrantClient:
    """Records calls so we can assert on them without a running Qdrant."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.created: list[dict] = []
        self.upserts: list[dict] = []
        self.queries: list[dict] = []

    def collection_exists(self, name: str) -> bool:
        return name in self.existing

    def create_collection(self, *, collection_name, vectors_config) -> None:
        self.created.append({"name": collection_name, "config": vectors_config})
        self.existing.add(collection_name)

    def upsert(self, *, collection_name, points) -> None:
        self.upserts.append({"collection": collection_name, "points": points})

    def query_points(self, *, collection_name, query, limit):
        self.queries.append({"collection": collection_name, "query": query, "limit": limit})
        return type("Result", (), {"points": ["hit"]})()


def test_ensure_collection_creates_when_missing():
    client = FakeQdrantClient()

    name = qdrant_store.ensure_collection("memories", client=client)

    assert name == "memories"
    assert len(client.created) == 1
    config = client.created[0]["config"]
    assert config.size == qdrant_store.VECTOR_SIZE
    assert config.distance == Distance.COSINE


def test_ensure_collection_is_idempotent():
    client = FakeQdrantClient(existing={"memories"})

    qdrant_store.ensure_collection("memories", client=client)

    assert client.created == []


def test_ensure_collection_honours_custom_vector_size():
    client = FakeQdrantClient()

    qdrant_store.ensure_collection("big", client=client, vector_size=1024)

    assert client.created[0]["config"].size == 1024


def test_upsert_memory_sends_single_point():
    client = FakeQdrantClient()

    qdrant_store.upsert_memory("memories", "id-1", [0.1, 0.2], {"text": "hi"}, client=client)

    assert len(client.upserts) == 1
    call = client.upserts[0]
    assert call["collection"] == "memories"
    point = call["points"][0]
    assert point.id == "id-1"
    assert point.vector == [0.1, 0.2]
    assert point.payload == {"text": "hi"}


def test_search_returns_points():
    client = FakeQdrantClient()

    results = qdrant_store.search("memories", [0.1, 0.2], limit=3, client=client)

    assert results == ["hit"]
    assert client.queries[0] == {"collection": "memories", "query": [0.1, 0.2], "limit": 3}
