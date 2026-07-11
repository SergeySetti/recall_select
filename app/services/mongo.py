"""MongoDB connection helpers.

Mongo is a remote, managed instance reached via ``MONGODB_URI``. It holds the
relational-ish metadata the app needs around the vector store: users, API keys,
projects, and the per-(user, project) collection registry used for limits and
stats. Vectors themselves live in Qdrant (see ``qdrant_store``).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "recall_select")


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used for all created_at/updated_at fields."""
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    """Return a cached Mongo client built from ``MONGODB_URI``."""
    return MongoClient(os.getenv("MONGODB_URI", MONGODB_URI))


def get_db() -> Database:
    """Return the application database handle."""
    return get_client()[os.getenv("MONGODB_DB", MONGODB_DB)]


def ensure_indexes(db: Database | None = None) -> None:
    """Create the indexes the CRUD layer relies on. Safe to call repeatedly."""
    db = db if db is not None else get_db()
    db.users.create_index([("email", ASCENDING)], unique=True)
    # Google's stable subject id. Unique, but only enforced for users that have
    # signed in with Google (seeded/API-created users may have none yet).
    db.users.create_index(
        [("google_sub", ASCENDING)],
        unique=True,
        partialFilterExpression={"google_sub": {"$type": "string"}},
    )
    db.api_keys.create_index([("user_id", ASCENDING)])
    # Keys are stored hashed (see api_keys.hash_key); the unique index is on the
    # digest, never the plaintext (which is never persisted).
    db.api_keys.create_index([("key_hash", ASCENDING)], unique=True)
    db.projects.create_index([("user_id", ASCENDING)])
    # One collection per (user, project) - enforced with a unique compound index.
    db.collections.create_index(
        [("user_id", ASCENDING), ("project_id", ASCENDING)], unique=True
    )
    # Monthly usage meter: one running call tally per (user, calendar month).
    # Unique so the upserting increment can never fork into duplicate period rows.
    db.usage.create_index([("user_id", ASCENDING), ("period", ASCENDING)], unique=True)
