"""Collection provisioning (services layer).

Creating a collection is inherently **two-sided**: a Mongo registry row (the
bookkeeping in ``collections.py``) *and* the backing Qdrant collection (the
vectors in ``qdrant_store.py``) have to exist together for the (user, project)
pair to be usable. This service is the single place that stitches those two core
stores into one atomic, idempotent step, so callers never open-code the pair and
never leave the two stores out of step. Creation is **lazy**: the only creating
caller is the first memory write - workspace provisioning and the collection API
register just the Mongo row and leave Qdrant untouched.

It sits one level above the core stores it composes and one level below the API:
core = ``collections`` (Mongo) + ``qdrant_store`` (Qdrant); this = the composed
create/destroy; callers = ``memory`` (create) / ``api.collections`` (destroy).

Pure I/O - ``db`` and ``qdrant`` are injected so this is testable without live
backends.
"""
from __future__ import annotations

from pymongo.database import Database
from qdrant_client import QdrantClient

from app.services import collections, qdrant_store


def create_collection(user_id: str, project_id: str, *, db: Database, qdrant: QdrantClient) -> dict:
    """Idempotently provision the (user, project) collection on both stores.

    Registers the Mongo registry record (enforcing the one-to-one mapping) and
    ensures the backing Qdrant collection exists, then returns the registry
    record. Safe to call repeatedly: it converges on exactly one row and one
    backing collection.
    """
    record = collections.register_collection(user_id, project_id, db=db)
    qdrant_store.ensure_collection(record["name"], client=qdrant)
    return record


def destroy_collection(user_id: str, project_id: str, *, db: Database, qdrant: QdrantClient) -> bool:
    """Tear the (user, project) collection down on both stores.

    Drops the backing Qdrant collection and removes the Mongo registry record.
    Returns True if a registry record was removed, False if there was nothing to
    tear down.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return False
    qdrant_store.delete_collection(record["name"], client=qdrant)
    return collections.delete_collection(user_id, project_id, db=db)
