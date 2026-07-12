"""Basic integration tests for the HTTP API (routers over the services layer).

Drives the app through FastAPI's TestClient with the Mongo and Qdrant
dependencies overridden by an in-memory Mongo (``mongomock``) and a fake Qdrant
client. Covers the core flows, a couple of 404s, and the access-control rules:
the management/memory API is signed-in-only and each caller may only touch their
own resources.
"""
from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_database, get_qdrant
from app.main import app
from app.services import mongo, users


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
def client():
    db = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(db)
    qdrant = FakeQdrant()
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_qdrant] = lambda: qdrant
    # Note: no `with` - we don't want the lifespan to hit real backends.
    yield TestClient(app), db, qdrant
    app.dependency_overrides.clear()


def _sign_in(db, email="me@example.com"):
    """Seed a user and pin it as the signed-in caller (bypasses OAuth)."""
    user = users.add_user(email, db=db)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


# --- Access control --------------------------------------------------------

def test_management_api_requires_sign_in(client):
    api, db, _ = client
    victim = users.add_user("victim@example.com", db=db)
    uid = victim["_id"]
    # Anonymous (no get_current_user override) - every account route is 401.
    assert api.get(f"/api/users/{uid}").status_code == 401
    assert api.get(f"/api/users/{uid}/api-keys").status_code == 401
    assert api.post(f"/api/users/{uid}/api-keys", json={}).status_code == 401
    assert api.get(f"/api/users/{uid}/projects").status_code == 401
    assert api.post(f"/api/users/{uid}/projects/p/memories/search", json={"query": "x"}).status_code == 401


def test_cannot_touch_another_users_resources(client):
    api, db, _ = client
    victim = users.add_user("victim@example.com", db=db)
    vid = victim["_id"]
    # A victim key that must never leak to another signed-in user.
    from app.services import api_keys, projects
    api_keys.add_api_key(vid, db=db)
    vproj = projects.add_project(vid, "secret", db=db)["_id"]

    # Sign in as a *different* user.
    _sign_in(db, email="attacker@example.com")

    # Reading the victim's account / keys / projects is forbidden, and never
    # returns their data.
    assert api.get(f"/api/users/{vid}").status_code == 403
    assert api.get(f"/api/users/{vid}/api-keys").status_code == 403
    assert api.post(f"/api/users/{vid}/api-keys", json={}).status_code == 403
    # Project owned by the victim: 404 (existence not confirmed).
    assert api.get(f"/api/projects/{vproj}").status_code == 404
    assert api.delete(f"/api/projects/{vproj}").status_code == 404
    # Memories under the victim's namespace: forbidden.
    assert api.post(f"/api/users/{vid}/projects/{vproj}/memories", json={"text": "x"}).status_code == 403


# --- Happy paths (signed in, own resources) --------------------------------

def test_user_self_service(client):
    api, db, _ = client
    user = _sign_in(db)
    uid = user["_id"]

    assert api.get(f"/api/users/{uid}").json()["id"] == uid

    patched = api.patch(f"/api/users/{uid}", json={"name": "Ann"})
    assert patched.status_code == 200 and patched.json()["name"] == "Ann"

    # tier is billing-owned: a self-service PATCH must not change it.
    assert api.patch(f"/api/users/{uid}", json={"tier": "paid_100x"}).json()["tier"] == "free"


def test_api_key_flow(client):
    api, db, _ = client
    uid = _sign_in(db)["_id"]

    created = api.post(f"/api/users/{uid}/api-keys", json={"label": "laptop"})
    assert created.status_code == 201
    key = created.json()
    assert key["key"].startswith("rs_") and key["user_id"] == uid

    # Listing never re-exposes the secret: masked display hint only.
    listed = api.get(f"/api/users/{uid}/api-keys").json()
    assert [k["id"] for k in listed] == [key["id"]]
    row = listed[0]
    assert "key" not in row and "key_hash" not in row and "key_last4" not in row
    assert row["key_masked"].startswith("rs_") and "…" in row["key_masked"]
    assert row["key_masked"] != key["key"]
    assert row["last_used_at"] is None

    assert api.delete(f"/api/api-keys/{key['id']}").status_code == 204
    assert api.get(f"/api/users/{uid}/api-keys").json() == []
    # Deleting an unknown key 404s.
    assert api.delete("/api/api-keys/nope").status_code == 404


def test_project_and_collection_flow(client):
    api, db, qdrant = client
    uid = _sign_in(db)["_id"]

    proj = api.post(f"/api/users/{uid}/projects", json={"name": "default"})
    assert proj.status_code == 201
    pid = proj.json()["id"]

    assert api.get(f"/api/projects/{pid}").json()["name"] == "default"
    assert api.patch(f"/api/projects/{pid}", json={"name": "notes"}).json()["name"] == "notes"

    # Register the collection - Mongo record only; the Qdrant collection is
    # created lazily on the first memory store.
    reg = api.post(f"/api/users/{uid}/projects/{pid}/collection")
    assert reg.status_code == 201
    record = reg.json()
    assert record["name"] == f"rs_{uid}_{pid}"
    assert record["name"] not in qdrant.collections

    # Idempotent: re-registering returns the same record.
    again = api.post(f"/api/users/{uid}/projects/{pid}/collection")
    assert again.json()["id"] == record["id"]

    # Delete drops both sides.
    assert api.delete(f"/api/users/{uid}/projects/{pid}/collection").status_code == 204
    assert record["name"] not in qdrant.collections
    assert api.get(f"/api/users/{uid}/projects/{pid}/collection").status_code == 404


def test_generate_link_requires_sign_in(client):
    api, _, _ = client
    # No session → the "me" endpoints 401.
    assert api.post("/api/me/link").status_code == 401
    assert api.get("/api/me/link").status_code == 401


def test_link_status_is_masked_and_never_rotates(client):
    api, db, _ = client
    _sign_in(db, email="status@example.com")

    # Before any link exists: a plain "no link yet" answer.
    assert api.get("/api/me/link").json() == {
        "exists": False, "masked_link": None, "created_at": None, "last_used_at": None,
    }

    key = api.post("/api/me/link").json()["api_key"]

    status = api.get("/api/me/link").json()
    assert status["exists"] is True
    # Masked form only - the plaintext must never appear in a GET.
    assert key not in status["masked_link"]
    assert "…" in status["masked_link"] and status["masked_link"].endswith(".md")
    assert status["created_at"] is not None

    # Reading the status must not rotate the key: the link still resolves.
    from app.services import api_keys
    assert api_keys.get_by_key(key, db=db) is not None


def test_generate_link_provisions_workspace(client):
    api, db, qdrant = client
    user = _sign_in(db, email="signed-in@example.com")

    resp = api.post("/api/me/link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"].startswith("rs_")
    # The link is the .md instructions URL carrying the key.
    assert body["link"].endswith(f"/m/{body['api_key']}.md")
    assert body["collection"] == f"rs_{user['_id']}_{body['project_id']}"
    # Only the Mongo registry is provisioned; the Qdrant collection is created
    # lazily on the first memory store.
    assert body["collection"] not in qdrant.collections

    # Reveal-once: clicking again rotates the key (fresh secret), and the old
    # link stops resolving.
    from app.services import api_keys
    again = api.post("/api/me/link").json()
    assert again["api_key"] != body["api_key"]
    assert api_keys.get_by_key(body["api_key"], db=db) is None


def test_connection_instructions_md(client):
    from app.services import api_keys

    api, db, _ = client
    user = users.add_user("md@example.com", db=db)
    key = api_keys.add_api_key(user["_id"], db=db)["key"]

    resp = api.get(f"/m/{key}.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    # Instructions point at the MCP URL = the link minus the .md suffix.
    assert f"/m/{key}" in body
    assert f"/m/{key}.md" not in body
    assert "mcpServers" in body

    # Unknown key 404s.
    assert api.get("/m/rs_bogus_key.md").status_code == 404
