"""Collection registry CRUD (services layer).

A "collection" is the per-(user, project) memory store. The mapping is strictly
one-to-one:

    (user_id, project_id)  <->  one Qdrant collection

The Qdrant collection name follows a fixed internal standard so it can be derived
from ids alone, without a lookup - see ``collection_name``. This Mongo registry
mirrors each Qdrant collection and carries the counters used for limiting and
stats (``points_count`` for stored memories, ``calls_count`` for usage). Vectors
live in Qdrant; this is just the bookkeeping side.
"""
from __future__ import annotations

from pymongo import ReturnDocument
from pymongo.database import Database

from app.services.mongo import get_db, utcnow

# Internal naming standard. Stable + derivable from ids, and a valid Qdrant
# collection name. Keep in sync with anything that reads Qdrant directly.
COLLECTION_PREFIX = "rs"


def collection_name(user_id: str, project_id: str) -> str:
    """Deterministic Qdrant collection name for a (user, project) pair."""
    return f"{COLLECTION_PREFIX}_{user_id}_{project_id}"


def register_collection(user_id: str, project_id: str, *, db: Database | None = None) -> dict:
    """Idempotently register the collection for (user, project).

    Returns the existing record if one is already present (enforcing the
    one-to-one mapping), otherwise creates and returns a fresh one with zeroed
    counters.
    """
    db = db if db is not None else get_db()
    existing = db.collections.find_one({"user_id": user_id, "project_id": project_id})
    if existing is not None:
        return existing

    name = collection_name(user_id, project_id)
    doc = {
        "_id": name,
        "name": name,
        "user_id": user_id,
        "project_id": project_id,
        "points_count": 0,
        "calls_count": 0,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    db.collections.insert_one(doc)
    return doc


def get_collection(user_id: str, project_id: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.collections.find_one({"user_id": user_id, "project_id": project_id})


def delete_collection(user_id: str, project_id: str, *, db: Database | None = None) -> bool:
    """Remove the registry record. Returns True if one was removed."""
    db = db if db is not None else get_db()
    result = db.collections.delete_one({"user_id": user_id, "project_id": project_id})
    return result.deleted_count > 0


def record_call(user_id: str, project_id: str, *, count: int = 1, db: Database | None = None) -> dict | None:
    """Increment the usage counter (for monthly call limits/stats)."""
    return _bump(user_id, project_id, {"calls_count": count}, db=db)


def set_points_count(user_id: str, project_id: str, points: int, *, db: Database | None = None) -> dict | None:
    """Record the current number of stored memories (for size limits/stats)."""
    db = db if db is not None else get_db()
    return db.collections.find_one_and_update(
        {"user_id": user_id, "project_id": project_id},
        {"$set": {"points_count": points, "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )


def _bump(user_id: str, project_id: str, increments: dict, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.collections.find_one_and_update(
        {"user_id": user_id, "project_id": project_id},
        {"$inc": increments, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
