"""Tests for Google sign-in user resolution and workspace provisioning."""
from __future__ import annotations

import mongomock
import pytest

from app.services import mongo, users, workspaces


@pytest.fixture
def db():
    database = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(database)
    return database


# --- get_or_create_google_user --------------------------------------------

def test_google_user_created_then_reused(db):
    first = users.get_or_create_google_user("sub-1", "g@example.com", name="Gina", db=db)
    assert first["email"] == "g@example.com"
    assert first["google_sub"] == "sub-1"
    assert first["tier"] == "free"

    # Same sub → same record, no duplicate.
    again = users.get_or_create_google_user("sub-1", "g@example.com", db=db)
    assert again["_id"] == first["_id"]
    assert db.users.count_documents({}) == 1


def test_google_signin_attaches_to_existing_email(db):
    seeded = users.add_user("seed@example.com", name="Seed", db=db)
    assert seeded.get("google_sub") is None

    linked = users.get_or_create_google_user("sub-9", "seed@example.com", db=db)
    # Same user id (so any workspace keyed to it carries over), now with google_sub.
    assert linked["_id"] == seeded["_id"]
    assert linked["google_sub"] == "sub-9"
    assert db.users.count_documents({}) == 1


# --- provision_default_workspace -------------------------------------------

def test_provision_is_idempotent(db):
    user = users.get_or_create_google_user("sub-w", "w@example.com", db=db)

    first = workspaces.provision_default_workspace(user["_id"], db=db)
    name = f"rs_{user['_id']}_{first['project']['_id']}"
    assert first["collection"]["name"] == name
    assert first["api_key"]["key"].startswith("rs_")
    assert first["project"]["is_default"] is True

    second = workspaces.provision_default_workspace(user["_id"], db=db)
    # Same default project and collection, but the link key is *rotated*: a fresh
    # secret is issued (reveal-once) and the old one invalidated.
    assert second["project"]["_id"] == first["project"]["_id"]
    assert second["api_key"]["key"] != first["api_key"]["key"]
    from app.services import api_keys
    assert api_keys.get_by_key(first["api_key"]["key"], db=db) is None
    assert db.projects.count_documents({"user_id": user["_id"]}) == 1
    # Still exactly one key - the old default was dropped before minting the new.
    assert db.api_keys.count_documents({"user_id": user["_id"]}) == 1
