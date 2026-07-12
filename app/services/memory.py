"""Memory store & recall (services layer).

Ties the embedding model to the vector store: a memory is `text` embedded into a
vector and upserted into the (user, project) Qdrant collection, with its text and
any metadata kept as the point payload. Recall embeds a query and returns the
nearest memories. Usage counters in the Mongo collection registry are updated as
a side effect (for limits/stats).

Pure I/O - `db`, `qdrant`, and `embed` are injected so this is testable without
live backends.
"""
from __future__ import annotations

from typing import Callable
from uuid import uuid4

from pymongo.database import Database
from qdrant_client import QdrantClient

from app.services import collection_provisioning, collections, qdrant_store, usage, vector_semantics
from app.services.embeddings_remote import embed as default_embed

# The injected embedder is used as a plain callable here; the `Embedder`
# abstraction and backend selection live in app.services.embeddings / app.dependencies.
EmbedFn = Callable[[str], list[float]]


def store_memory(
    user_id: str,
    project_id: str,
    text: str,
    *,
    metadata: dict | None = None,
    db: Database,
    qdrant: QdrantClient,
    embed: EmbedFn = default_embed,
) -> dict:
    """Embed and store one memory; provisions the collection on first use.

    Gated by the tier's monthly call budget (see ``app.services.usage``); a user
    over budget is rejected here before any embedding or provisioning work.
    """
    usage.check_call_allowed(user_id, db=db)
    record = collection_provisioning.create_collection(user_id, project_id, db=db, qdrant=qdrant)
    name = record["name"]

    point_id = str(uuid4())
    # The `_semantics` anchors (owner, stored_at) must be captured at write time -
    # deixis is unrecoverable later. Merged last: the key is reserved, so a
    # metadata collision is overridden rather than trusted.
    payload = {
        "text": text,
        **(metadata or {}),
        **vector_semantics.annotate_store_payload(user_id),
    }
    qdrant_store.upsert_memory(name, point_id, embed(text), payload, client=qdrant)

    # Stats: count this write against the all-time and the monthly meters, then
    # refresh the stored-memory total.
    collections.record_call(user_id, project_id, db=db)
    usage.record_call(user_id, db=db)
    points_count = qdrant_store.count(name, client=qdrant)
    collections.set_points_count(user_id, project_id, points_count, db=db)

    return {"id": point_id, "collection": name, "points_count": points_count}


def delete_memory(
    user_id: str,
    project_id: str,
    memory_id: str,
    *,
    db: Database,
    qdrant: QdrantClient,
) -> bool:
    """Delete one stored memory by the id `store_memory`/`recall_memory` return.

    Returns False (rather than erroring) if the collection was never written to
    or the id doesn't exist.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return False

    usage.check_call_allowed(user_id, db=db)
    deleted = qdrant_store.delete_memory(record["name"], memory_id, client=qdrant)
    collections.record_call(user_id, project_id, db=db)
    usage.record_call(user_id, db=db)
    if deleted:
        # Declared relations live on their source points, so other memories may
        # still hold edges aimed at the one just removed - prune them or the
        # semantic graph keeps routing through a ghost.
        vector_semantics.prune_relations_to(user_id, project_id, memory_id, db=db, qdrant=qdrant)
        points_count = qdrant_store.count(record["name"], client=qdrant)
        collections.set_points_count(user_id, project_id, points_count, db=db)
    return deleted


def recall_memory(
    user_id: str,
    project_id: str,
    query: str,
    *,
    limit: int = 5,
    db: Database,
    qdrant: QdrantClient,
    embed: EmbedFn = default_embed,
) -> list[dict]:
    """Return the memories nearest to `query` (closest first).

    Returns an empty list if the collection has never been written to (nothing
    to recall yet) rather than erroring.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return []

    usage.check_call_allowed(user_id, db=db)
    hits = qdrant_store.search(record["name"], embed(query), limit=limit, client=qdrant)
    collections.record_call(user_id, project_id, db=db)
    usage.record_call(user_id, db=db)
    return [{"id": hit.id, "score": hit.score, "payload": hit.payload} for hit in hits]
