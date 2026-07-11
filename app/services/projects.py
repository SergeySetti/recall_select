"""Project CRUD (services layer).

A project groups memory under a user. Each project maps one-to-one to a Qdrant
collection (see ``collections``). Users always have a default project; more can
be created.
"""
from __future__ import annotations

from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database

from app.services.mongo import get_db, utcnow


def add_project(user_id: str, name: str, *, db: Database | None = None) -> dict:
    """Create a project owned by ``user_id`` and return the stored document."""
    db = db if db is not None else get_db()
    now = utcnow()
    doc = {
        "_id": uuid4().hex,
        "user_id": user_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
    }
    db.projects.insert_one(doc)
    return doc


def get_project(project_id: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.projects.find_one({"_id": project_id})


def list_projects(user_id: str, *, db: Database | None = None) -> list[dict]:
    db = db if db is not None else get_db()
    return list(db.projects.find({"user_id": user_id}).sort("created_at", 1))


def update_project(project_id: str, *, db: Database | None = None, **fields) -> dict | None:
    """Patch the given fields; returns the updated document (or None if absent)."""
    db = db if db is not None else get_db()
    fields.pop("_id", None)
    fields.pop("user_id", None)  # ownership is immutable
    fields["updated_at"] = utcnow()
    return db.projects.find_one_and_update(
        {"_id": project_id},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )


def delete_project(project_id: str, *, db: Database | None = None) -> bool:
    """Delete a project by id. Returns True if one was removed."""
    db = db if db is not None else get_db()
    return db.projects.delete_one({"_id": project_id}).deleted_count > 0
