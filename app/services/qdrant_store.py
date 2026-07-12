"""Thin wrapper around the Qdrant vector database.

Every memory instance is a Qdrant collection (per user per project). This module
centralises client creation and the handful of collection operations the app
needs so callers never touch the raw client directly. Qdrant is reached over the
internal compose network as `qdrant:6333`; never published to the host.
"""
from __future__ import annotations

import os
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
# Sent on every request when set; must match the server's QDRANT__SERVICE__API_KEY.
# None = unauthenticated (local dev against a keyless Qdrant).
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
# Embedding dimension for every collection we create. The remote embedder is
# asked to return vectors of exactly this size (via the API's `dimensions` param,
# see embeddings_remote), so the collection and the embeddings always match.
# Override via VECTOR_SIZE if the embedding model changes.
VECTOR_SIZE = int(os.getenv("VECTOR_SIZE", "768"))


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    """Return a cached Qdrant client built from ``QDRANT_URL``."""
    return QdrantClient(
        url=os.getenv("QDRANT_URL", QDRANT_URL).rstrip("/"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
    )


def ensure_collection(
    name: str,
    *,
    client: QdrantClient | None = None,
    vector_size: int | None = None,
    distance: Distance = Distance.COSINE,
) -> str:
    """Create ``name`` if it does not exist yet. Returns the collection name."""
    client = client or get_client()
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=vector_size or VECTOR_SIZE,
                distance=distance,
            ),
        )
    return name


def delete_collection(name: str, *, client: QdrantClient | None = None) -> bool:
    """Drop a collection if it exists. Returns True if one was removed."""
    client = client or get_client()
    if not client.collection_exists(name):
        return False
    client.delete_collection(name)
    return True


def count(name: str, *, client: QdrantClient | None = None) -> int:
    """Exact number of points stored in a collection."""
    client = client or get_client()
    return client.count(collection_name=name, exact=True).count


def upsert_memory(
    collection: str,
    point_id: str | int,
    vector: list[float],
    payload: dict | None = None,
    *,
    client: QdrantClient | None = None,
) -> None:
    """Insert or replace a single memory point in ``collection``."""
    client = client or get_client()
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload or {})],
    )


def delete_memory(
    collection: str,
    point_id: str | int,
    *,
    client: QdrantClient | None = None,
) -> bool:
    """Remove a single memory point. Returns True if it existed."""
    client = client or get_client()
    # The collection itself appears lazily on first store - nothing stored,
    # nothing to delete.
    if not client.collection_exists(collection):
        return False
    if not client.retrieve(collection_name=collection, ids=[point_id]):
        return False
    client.delete(
        collection_name=collection,
        points_selector=PointIdsList(points=[point_id]),
    )
    return True


def search(
    collection: str,
    vector: list[float],
    *,
    limit: int = 5,
    client: QdrantClient | None = None,
) -> list:
    """Return the nearest stored memories to ``vector`` (closest first)."""
    client = client or get_client()
    return client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
    ).points


def neighbors(
    collection: str,
    point_id: str | int,
    *,
    limit: int = 10,
    client: QdrantClient | None = None,
) -> list:
    """Nearest stored memories to an existing point, by its own vector.

    Qdrant resolves a bare point id in the ``query`` position to that point's
    stored vector, so this is "find memories like this one". The point matches
    itself, so we ask for one extra and let the caller drop the seed.
    """
    client = client or get_client()
    if not client.collection_exists(collection):
        return []
    return client.query_points(
        collection_name=collection,
        query=point_id,
        limit=limit + 1,
    ).points


def scroll_points(
    collection: str,
    *,
    with_vectors: bool = True,
    page_size: int = 256,
    limit: int | None = None,
    client: QdrantClient | None = None,
) -> list:
    """Enumerate stored points (payload, and optionally vectors) as a flat list.

    Pages through the whole collection. ``limit`` caps the total returned - the
    semantic passes are pairwise, so callers bound the work on large stores.
    """
    client = client or get_client()
    if not client.collection_exists(collection):
        return []
    points: list = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=collection,
            limit=page_size,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        points.extend(batch)
        if offset is None or (limit is not None and len(points) >= limit):
            break
    return points[:limit] if limit is not None else points


def retrieve_points(
    collection: str,
    ids: list[str | int],
    *,
    with_vectors: bool = True,
    client: QdrantClient | None = None,
) -> list:
    """Fetch specific points by id (payload, and optionally vectors)."""
    client = client or get_client()
    if not client.collection_exists(collection):
        return []
    return client.retrieve(
        collection_name=collection,
        ids=list(ids),
        with_payload=True,
        with_vectors=with_vectors,
    )


def set_payload(
    collection: str,
    point_id: str | int,
    payload: dict,
    *,
    client: QdrantClient | None = None,
) -> bool:
    """Merge ``payload`` onto one existing point (each top-level key is replaced).

    Points are document-style: this is how a stored memory gets enriched after
    the fact (semantic annotations, relations) without touching its vector.
    Returns False if the collection or the point does not exist.
    """
    client = client or get_client()
    if not client.collection_exists(collection):
        return False
    if not client.retrieve(collection_name=collection, ids=[point_id]):
        return False
    client.set_payload(
        collection_name=collection,
        payload=payload,
        points=[point_id],
    )
    return True
