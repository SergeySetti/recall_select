"""Owner admin area (``app.services.admin`` + ``app.api.admin``).

Mongo is ``mongomock``; no Qdrant or embedder is touched - the admin view only
reads the registry. The HTTP tests drive the real app with the database
dependency overridden, so routing, the secret gate and the session all run.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.services import admin, api_keys, collections, projects, users

SECRET = "test-admin-secret-value"


@pytest.fixture
def enabled(monkeypatch):
    """Configure an admin secret for the duration of one test."""
    monkeypatch.setenv("ADMIN_SECRET", SECRET)


@pytest.fixture
def client(mongo_db):
    """App client whose database dependency is the in-memory Mongo."""
    app.dependency_overrides[deps.get_database] = lambda: mongo_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def unlocked(client, enabled):
    """A client that has already unlocked the admin area."""
    client.post("/admin", data={"secret": SECRET}, follow_redirects=False)
    return client


# --- the secret gate -------------------------------------------------------


def test_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)

    assert admin.is_enabled() is False
    # Nothing authenticates past a missing configuration - not even "".
    assert admin.verify("") is False
    assert admin.verify(None) is False
    assert admin.verify("anything") is False


def test_verify_matches_only_the_configured_secret(enabled):
    assert admin.is_enabled() is True
    assert admin.verify(SECRET) is True
    assert admin.verify(SECRET + "x") is False
    assert admin.verify(SECRET[:-1]) is False


def test_session_expiry(enabled):
    assert admin.session_expired(None) is True
    assert admin.session_expired(time.time()) is False
    assert admin.session_expired(time.time() - admin.SESSION_TTL_SECONDS - 1) is True


# --- listing + per-user snapshot -------------------------------------------


def test_list_users_reports_stored_totals(mongo_db):
    alice = users.add_user("alice@example.com", name="Alice", db=mongo_db)
    project = projects.add_project(alice["_id"], "notes", db=mongo_db)
    collections.register_collection(alice["_id"], project["_id"], db=mongo_db)
    collections.set_points_count(alice["_id"], project["_id"], 9, db=mongo_db)
    users.add_user("bob@example.com", name="Bob", db=mongo_db)

    rows = {row["email"]: row for row in admin.list_users(db=mongo_db)}

    assert rows["alice@example.com"]["memories"] == 9
    assert rows["alice@example.com"]["projects_with_data"] == 1
    # A user who never stored anything still lists, with zeroes.
    assert rows["bob@example.com"]["memories"] == 0
    assert rows["bob@example.com"]["projects_with_data"] == 0
    assert admin.user_count(db=mongo_db) == 2


def test_list_users_search_by_email_name_and_id(mongo_db):
    alice = users.add_user("alice@example.com", name="Alice", db=mongo_db)
    users.add_user("bob@example.com", name="Bob", db=mongo_db)

    assert [r["id"] for r in admin.list_users(query="ALICE@", db=mongo_db)] == [alice["_id"]]
    assert [r["id"] for r in admin.list_users(query="alic", db=mongo_db)] == [alice["_id"]]
    assert [r["id"] for r in admin.list_users(query=alice["_id"], db=mongo_db)] == [alice["_id"]]
    assert admin.list_users(query="nobody", db=mongo_db) == []


def test_user_space_mirrors_the_account_snapshot(mongo_db):
    user = users.add_user("carol@example.com", db=mongo_db)
    key = api_keys.add_api_key(user["_id"], label="default", db=mongo_db)

    space = admin.user_space(user["_id"], db=mongo_db)

    assert space["user"]["email"] == "carol@example.com"
    assert space["summary"]["tier"] == "free"
    # Keys arrive display-safe: the secret is never at rest, so the owner's view
    # cannot leak it either.
    (row,) = space["summary"]["api_keys"]
    assert row["masked"] == api_keys.masked(key)
    assert key["key"] not in str(space)
    assert admin.user_space("no-such-user", db=mongo_db) is None


# --- HTTP surface ----------------------------------------------------------


def test_routes_404_when_feature_is_off(client, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)

    for path in ("/admin", "/admin/users", "/admin/users/whoever"):
        assert client.get(path).status_code == 404
    assert client.post("/admin", data={"secret": "guess"}).status_code == 404


def test_wrong_key_is_rejected_and_grants_nothing(client, enabled):
    response = client.post("/admin", data={"secret": "wrong"}, follow_redirects=False)

    assert response.status_code == 401
    # Still locked: the user list bounces back to the unlock page.
    listing = client.get("/admin/users", follow_redirects=False)
    assert listing.status_code == 303
    assert listing.headers["location"] == "/admin"


def test_unlock_then_browse_users(client, enabled, mongo_db):
    user = users.add_user("dora@example.com", name="Dora", db=mongo_db)

    unlock = client.post("/admin", data={"secret": SECRET}, follow_redirects=False)
    assert unlock.status_code == 303
    assert unlock.headers["location"] == "/admin/users"

    listing = client.get("/admin/users")
    assert listing.status_code == 200
    assert "dora@example.com" in listing.text

    detail = client.get(f"/admin/users/{user['_id']}")
    assert detail.status_code == 200
    assert "dora@example.com" in detail.text
    assert client.get("/admin/users/nope").status_code == 404


def test_lock_ends_the_admin_session(unlocked):
    unlocked.get("/admin/logout", follow_redirects=False)

    response = unlocked.get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_repeated_failures_lock_the_client_out(client, enabled):
    from app.api import admin as admin_api

    admin_api._failures.clear()
    for _ in range(admin_api.LOCKOUT_AFTER):
        assert client.post("/admin", data={"secret": "wrong"}).status_code == 401

    # Even the right key is refused while the lockout stands.
    response = client.post("/admin", data={"secret": SECRET}, follow_redirects=False)
    assert response.status_code == 429
    admin_api._failures.clear()
