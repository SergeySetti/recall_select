"""Basic integration tests for the Mongo-backed service CRUDs.

These exercise the main happy paths end-to-end against an in-memory Mongo
(``mongomock``) - not exhaustive, just enough to cover the core flows.
"""
from __future__ import annotations

import pytest

from app.services import api_keys, collections, mongo, projects, users


# --- Users -----------------------------------------------------------------

def test_user_add_get_update(mongo_db):
    user = users.add_user("a@example.com", name="Ann", db=mongo_db)

    assert user["email"] == "a@example.com"
    assert user["tier"] == "free"
    fetched = users.get_user(user["_id"], db=mongo_db)
    assert fetched["_id"] == user["_id"]
    assert fetched["email"] == "a@example.com"

    updated = users.update_user(user["_id"], tier="paid_5x", db=mongo_db)
    assert updated["tier"] == "paid_5x"


def test_get_user_by_email(mongo_db):
    users.add_user("b@example.com", db=mongo_db)
    found = users.get_user_by_email("b@example.com", db=mongo_db)
    assert found is not None and found["email"] == "b@example.com"


# --- API keys --------------------------------------------------------------

def test_api_key_add_list_delete(mongo_db):
    user = users.add_user("c@example.com", db=mongo_db)

    key = api_keys.add_api_key(user["_id"], label="laptop", db=mongo_db)
    assert key["key"].startswith("rs_")
    assert key["user_id"] == user["_id"]
    # Display hints kept at rest: head + tail of the token, never the middle.
    assert key["key_prefix"] == key["key"][: len(key["key_prefix"])]
    assert key["key_last4"] == key["key"][-4:]
    assert key["last_used_at"] is None
    assert api_keys.masked(key) == f"{key['key_prefix']}…{key['key_last4']}"
    # The masked form must not be a usable credential.
    assert api_keys.masked(key) != key["key"]

    listed = api_keys.list_api_keys(user["_id"], db=mongo_db)
    assert [k["_id"] for k in listed] == [key["_id"]]
    assert api_keys.get_by_key(key["key"], db=mongo_db)["_id"] == key["_id"]

    assert api_keys.delete_api_key(key["_id"], db=mongo_db) is True
    assert api_keys.list_api_keys(user["_id"], db=mongo_db) == []
    # Deleting again is a no-op.
    assert api_keys.delete_api_key(key["_id"], db=mongo_db) is False


def test_api_key_masked_tolerates_legacy_docs(mongo_db):
    # Keys minted before key_last4 existed still render (prefix-only fallback).
    assert api_keys.masked({"key_prefix": "rs_ab12"}) == "rs_ab12…"


def test_get_by_key_record_use_stamps_last_used(mongo_db):
    user = users.add_user("use@example.com", db=mongo_db)
    key = api_keys.add_api_key(user["_id"], db=mongo_db)

    # A plain lookup (e.g. an ownership check) leaves the stamp alone.
    api_keys.get_by_key(key["key"], db=mongo_db)
    assert api_keys.get_api_key(key["_id"], db=mongo_db)["last_used_at"] is None

    # The auth gate records the use, both in the return and at rest.
    resolved = api_keys.get_by_key(key["key"], record_use=True, db=mongo_db)
    assert resolved["last_used_at"] is not None
    assert api_keys.get_api_key(key["_id"], db=mongo_db)["last_used_at"] is not None


def test_get_labeled_key(mongo_db):
    user = users.add_user("labeled@example.com", db=mongo_db)
    assert api_keys.get_labeled_key(user["_id"], "default", db=mongo_db) is None
    key = api_keys.add_api_key(user["_id"], label="default", db=mongo_db)
    found = api_keys.get_labeled_key(user["_id"], "default", db=mongo_db)
    assert found is not None and found["_id"] == key["_id"]


def test_api_key_collision_retries_with_fresh_token(mongo_db, monkeypatch):
    # The unique index is what turns a collision into a retryable insert error.
    mongo.ensure_indexes(mongo_db)
    existing = api_keys.add_api_key("user-a", db=mongo_db)

    # First generated token collides with the existing key, second is fresh.
    tokens = iter([existing["key"], "rs_fresh-token"])
    monkeypatch.setattr(api_keys, "generate_key", lambda: next(tokens))

    created = api_keys.add_api_key("user-b", db=mongo_db)
    assert created["key"] == "rs_fresh-token"
    # Both users kept their own credential.
    assert api_keys.get_by_key(existing["key"], db=mongo_db)["user_id"] == "user-a"
    assert api_keys.get_by_key("rs_fresh-token", db=mongo_db)["user_id"] == "user-b"


def test_api_key_collision_exhaustion_raises(mongo_db, monkeypatch):
    mongo.ensure_indexes(mongo_db)
    existing = api_keys.add_api_key("user-a", db=mongo_db)

    # Every attempt yields the same colliding token.
    monkeypatch.setattr(api_keys, "generate_key", lambda: existing["key"])

    with pytest.raises(RuntimeError, match="unique API key"):
        api_keys.add_api_key("user-b", db=mongo_db)
    # The loser's inserts left no partial documents behind.
    assert api_keys.list_api_keys("user-b", db=mongo_db) == []


def test_api_key_foreign_unique_index_raises_clearly(mongo_db):
    # Simulate a stale/unexpected unique index (a legacy one-key-per-user index
    # on user_id). The insert is rejected for a reason a fresh token can't fix,
    # so we must surface *that* rather than the misleading "couldn't generate a
    # unique key" exhaustion message.
    mongo.ensure_indexes(mongo_db)
    mongo_db.api_keys.create_index("user_id", unique=True, name="legacy_user_id")
    api_keys.add_api_key("user-a", db=mongo_db)

    with pytest.raises(RuntimeError, match="index other than\\s+key_hash"):
        api_keys.add_api_key("user-a", db=mongo_db)


# --- Projects --------------------------------------------------------------

def test_project_crud(mongo_db):
    user = users.add_user("d@example.com", db=mongo_db)

    proj = projects.add_project(user["_id"], "default", db=mongo_db)
    assert projects.get_project(proj["_id"], db=mongo_db)["name"] == "default"

    renamed = projects.update_project(proj["_id"], name="notes", db=mongo_db)
    assert renamed["name"] == "notes"

    assert projects.delete_project(proj["_id"], db=mongo_db) is True
    assert projects.get_project(proj["_id"], db=mongo_db) is None


# --- Collections (one-to-one + stats) --------------------------------------

def test_collection_is_one_to_one(mongo_db):
    rec = collections.register_collection("u1", "p1", db=mongo_db)

    assert rec["name"] == collections.collection_name("u1", "p1") == "rs_u1_p1"
    assert rec["points_count"] == 0 and rec["calls_count"] == 0

    # Re-registering returns the same record rather than creating a duplicate.
    again = collections.register_collection("u1", "p1", db=mongo_db)
    assert again["_id"] == rec["_id"]
    assert mongo_db.collections.count_documents({}) == 1


def test_collection_stats_counters(mongo_db):
    collections.register_collection("u1", "p1", db=mongo_db)

    collections.record_call("u1", "p1", db=mongo_db)
    after = collections.record_call("u1", "p1", count=4, db=mongo_db)
    assert after["calls_count"] == 5

    sized = collections.set_points_count("u1", "p1", 42, db=mongo_db)
    assert sized["points_count"] == 42

    assert collections.delete_collection("u1", "p1", db=mongo_db) is True
    assert collections.get_collection("u1", "p1", db=mongo_db) is None
