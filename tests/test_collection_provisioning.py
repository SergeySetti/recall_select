"""Tests for the two-sided collection provisioning service.

Verifies that ``create_collection`` / ``destroy_collection`` keep the Mongo
registry and the backing Qdrant collection in step, and stay idempotent.
"""
from __future__ import annotations

import mongomock
import pytest

from app.services import collection_provisioning, collections, mongo


class FakeQdrant:
    def __init__(self) -> None:
        self.collections: set[str] = set()

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, *, collection_name, vectors_config) -> None:
        self.collections.add(collection_name)

    def delete_collection(self, name: str) -> None:
        self.collections.discard(name)


@pytest.fixture
def db():
    database = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(database)
    return database


def test_create_provisions_both_stores(db):
    qdrant = FakeQdrant()

    record = collection_provisioning.create_collection("u1", "p1", db=db, qdrant=qdrant)

    name = collections.collection_name("u1", "p1")
    assert record["name"] == name
    # Both sides exist: Mongo registry row and backing Qdrant collection.
    assert collections.get_collection("u1", "p1", db=db) is not None
    assert name in qdrant.collections


def test_create_is_idempotent(db):
    qdrant = FakeQdrant()

    first = collection_provisioning.create_collection("u1", "p1", db=db, qdrant=qdrant)
    second = collection_provisioning.create_collection("u1", "p1", db=db, qdrant=qdrant)

    assert second["name"] == first["name"]
    assert db.collections.count_documents({}) == 1
    assert qdrant.collections == {first["name"]}


def test_destroy_tears_down_both_stores(db):
    qdrant = FakeQdrant()
    name = collection_provisioning.create_collection("u1", "p1", db=db, qdrant=qdrant)["name"]

    assert collection_provisioning.destroy_collection("u1", "p1", db=db, qdrant=qdrant) is True
    assert collections.get_collection("u1", "p1", db=db) is None
    assert name not in qdrant.collections


def test_destroy_missing_is_noop(db):
    qdrant = FakeQdrant()
    assert collection_provisioning.destroy_collection("nope", "nope", db=db, qdrant=qdrant) is False
