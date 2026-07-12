"""Basic integration tests for the memory store & recall API.

Drives the app through TestClient with Mongo (``mongomock``), a fake Qdrant that
actually retains points (so store -> recall round-trips), and a stub embedder.
"""
from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_database, get_embedder, get_qdrant
from app.main import app
from app.services import billing, mongo, projects, usage, users


class FakeQdrant:
    """In-memory stand-in: stores points per collection and returns them on query."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def collection_exists(self, name: str) -> bool:
        return name in self.data

    def create_collection(self, *, collection_name, vectors_config) -> None:
        self.data.setdefault(collection_name, {})

    def delete_collection(self, name: str) -> None:
        self.data.pop(name, None)

    def upsert(self, *, collection_name, points) -> None:
        bucket = self.data.setdefault(collection_name, {})
        for p in points:
            bucket[p.id] = p.payload

    def count(self, *, collection_name, exact=True):
        return SimpleNamespace(count=len(self.data.get(collection_name, {})))

    def retrieve(self, *, collection_name, ids):
        bucket = self.data.get(collection_name, {})
        return [SimpleNamespace(id=i) for i in ids if i in bucket]

    def delete(self, *, collection_name, points_selector):
        bucket = self.data.get(collection_name, {})
        for pid in points_selector.points:
            bucket.pop(pid, None)

    def query_points(self, *, collection_name, query, limit):
        items = list(self.data.get(collection_name, {}).items())[:limit]
        points = [SimpleNamespace(id=pid, score=1.0, payload=pl) for pid, pl in items]
        return SimpleNamespace(points=points)

    # Document-style ops used by the semantic layer (delete prunes relations).
    def scroll(self, *, collection_name, limit, offset=None, with_payload=True, with_vectors=False):
        bucket = self.data.get(collection_name, {})
        return [SimpleNamespace(id=pid, payload=pl) for pid, pl in bucket.items()], None

    def set_payload(self, *, collection_name, payload, points) -> None:
        bucket = self.data.get(collection_name, {})
        for pid in points:
            if pid in bucket:
                bucket[pid] = {**bucket[pid], **payload}


@pytest.fixture
def client():
    db = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(db)
    qdrant = FakeQdrant()
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_qdrant] = lambda: qdrant
    app.dependency_overrides[get_embedder] = lambda: (lambda text: [0.1, 0.2, 0.3])
    yield TestClient(app), db, qdrant
    app.dependency_overrides.clear()


def _make_project(db):
    """Seed a signed-in user with a project; the memory API is ownership-gated."""
    user = users.add_user("m@example.com", db=db)
    app.dependency_overrides[get_current_user] = lambda: user
    pid = projects.add_project(user["_id"], "default", db=db)["_id"]
    return user["_id"], pid


def test_store_then_recall(client):
    api, db, qdrant = client
    uid, pid = _make_project(db)
    base = f"/api/users/{uid}/projects/{pid}/memories"

    stored = api.post(base, json={"text": "the sky is blue", "metadata": {"tag": "fact"}})
    assert stored.status_code == 201
    body = stored.json()
    assert body["collection"] == f"rs_{uid}_{pid}"
    assert body["points_count"] == 1
    # Memory landed in the backing collection.
    assert body["collection"] in qdrant.data

    # A second memory bumps the stored-count stat.
    assert api.post(base, json={"text": "grass is green"}).json()["points_count"] == 2

    results = api.post(f"{base}/search", json={"query": "what colour is the sky", "limit": 5})
    assert results.status_code == 200
    hits = results.json()
    assert len(hits) == 2
    texts = {h["payload"]["text"] for h in hits}
    assert "the sky is blue" in texts


def test_delete_memory(client):
    api, db, qdrant = client
    uid, pid = _make_project(db)
    base = f"/api/users/{uid}/projects/{pid}/memories"

    memory_id = api.post(base, json={"text": "the sky is blue"}).json()["id"]
    assert api.post(base, json={"text": "grass is green"}).json()["points_count"] == 2

    assert api.delete(f"{base}/{memory_id}").status_code == 204
    # Gone from the backing collection; the stored-count stat is refreshed.
    collection = f"rs_{uid}_{pid}"
    assert memory_id not in qdrant.data[collection]
    assert len(qdrant.data[collection]) == 1
    # Deleting the same id again (or any unknown id) 404s.
    assert api.delete(f"{base}/{memory_id}").status_code == 404


def test_delete_before_any_store_404(client):
    api, db, _ = client
    uid, pid = _make_project(db)
    resp = api.delete(f"/api/users/{uid}/projects/{pid}/memories/nope")
    assert resp.status_code == 404


def test_recall_empty_when_nothing_stored(client):
    api, db, _ = client
    uid, pid = _make_project(db)
    resp = api.post(
        f"/api/users/{uid}/projects/{pid}/memories/search",
        json={"query": "anything"},
    )
    assert resp.status_code == 200 and resp.json() == []


def test_store_over_monthly_budget_429(client):
    api, db, _ = client
    uid, pid = _make_project(db)
    base = f"/api/users/{uid}/projects/{pid}/memories"

    # Spend the whole free monthly budget, then the next call is rejected.
    usage.record_call(uid, count=billing.FREE_CALLS, db=db)
    resp = api.post(base, json={"text": "one too many"})
    assert resp.status_code == 429
    assert "Monthly call limit reached" in resp.json()["detail"]


def test_calls_are_metered_monthly(client):
    api, db, _ = client
    uid, pid = _make_project(db)
    base = f"/api/users/{uid}/projects/{pid}/memories"

    # Each store + each search counts as one call against the monthly meter.
    api.post(base, json={"text": "a"})
    api.post(base, json={"text": "b"})
    api.post(f"{base}/search", json={"query": "a"})
    assert usage.calls_this_period(uid, db=db) == 3


def test_store_to_unknown_project_404(client):
    api, db, _ = client
    user = users.add_user("x@example.com", db=db)
    app.dependency_overrides[get_current_user] = lambda: user
    resp = api.post(f"/api/users/{user['_id']}/projects/nope/memories", json={"text": "hi"})
    assert resp.status_code == 404
