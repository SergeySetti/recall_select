"""User CRUD (services layer).

A user owns API keys, projects, and (transitively) collections. ``tier`` drives
the limits enforced elsewhere. Identity comes from Google OAuth (added later);
for now ``email`` is the natural key.
"""
from __future__ import annotations

from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database

from app.services.mongo import get_db, utcnow

DEFAULT_TIER = "free"


def add_user(
    email: str,
    *,
    name: str | None = None,
    tier: str = DEFAULT_TIER,
    google_sub: str | None = None,
    db: Database | None = None,
) -> dict:
    """Insert a new user and return the stored document."""
    db = db if db is not None else get_db()
    now = utcnow()
    doc = {
        "_id": uuid4().hex,
        "email": email,
        "name": name,
        "tier": tier,
        "google_sub": google_sub,
        "created_at": now,
        "updated_at": now,
    }
    db.users.insert_one(doc)
    return doc


def get_user(user_id: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.users.find_one({"_id": user_id})


def get_user_by_email(email: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.users.find_one({"email": email})


def get_user_by_google_sub(google_sub: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.users.find_one({"google_sub": google_sub})


def get_or_create_google_user(
    google_sub: str,
    email: str,
    *,
    name: str | None = None,
    db: Database | None = None,
) -> dict:
    """Resolve the user behind a Google sign-in, creating one on first login.

    Matched by Google's stable subject id (``sub``) first; failing that, by email
    - so a user who already exists (e.g. seeded via the management API) gets their
    Google identity attached rather than duplicated. The user id never changes, so
    any workspace/collection already keyed to it carries over untouched.
    """
    db = db if db is not None else get_db()

    existing = get_user_by_google_sub(google_sub, db=db)
    if existing is not None:
        return existing

    by_email = get_user_by_email(email, db=db)
    if by_email is not None:
        return update_user(by_email["_id"], db=db, google_sub=google_sub, name=name) or by_email

    return add_user(email, name=name, google_sub=google_sub, db=db)


def update_user(user_id: str, *, db: Database | None = None, **fields) -> dict | None:
    """Patch the given fields; returns the updated document (or None if absent)."""
    db = db if db is not None else get_db()
    fields.pop("_id", None)  # never let callers rewrite the id
    fields["updated_at"] = utcnow()
    return db.users.find_one_and_update(
        {"_id": user_id},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
