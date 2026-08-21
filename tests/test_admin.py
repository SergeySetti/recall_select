"""Owner admin area (``app.services.admin`` + ``app.api.admin``).

Mongo is ``mongomock``; no Qdrant or embedder is touched - the admin view only
reads the registry. The HTTP tests drive the real app with the database
dependency overridden, so routing, the secret gate and the session all run.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

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


def test_payments_page_shows_status_not_just_the_row(unlocked, mongo_db):
    """The page must distinguish "started paying" from "paid" - the row alone
    (status: created) once read as a payment that never granted a tier."""
    from app.services import billing

    user = users.add_user("payer@example.com", db=mongo_db)
    billing.record_pending("inv-admin-1", user["_id"], billing.get_plan("2x"), db=mongo_db)

    page = unlocked.get("/admin/payments")

    assert page.status_code == 200
    assert "payer@example.com" in page.text
    assert "in flight" in page.text  # created = not settled, not paid

    billing.apply_webhook("inv-admin-1", "success", db=mongo_db)
    rows = admin.list_payments(db=mongo_db)
    assert rows[0]["paid"] is True and rows[0]["settled"] is True


def test_payments_page_needs_the_unlock(client, enabled):
    response = client.get("/admin/payments", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


# --- the memory viewer -----------------------------------------------------
# The one place the area reads a user's *contents* rather than the shape of
# their account. Qdrant is faked: the viewer only scrolls.


class FakeScrollQdrant:
    """Minimal stand-in: holds points per collection and scrolls them back.

    Scrolls in insertion order deliberately - the viewer must not rely on the
    store handing memories back newest-first, because Qdrant doesn't.
    """

    def __init__(self, points: dict[str, list] | None = None) -> None:
        self.data = points or {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.data

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):
        points = self.data.get(collection_name, [])
        start = offset or 0
        batch = points[start : start + limit]
        next_offset = start + limit if start + limit < len(points) else None
        return batch, next_offset


def _point(point_id: str, text: str, stored_at: str | None, **metadata):
    payload = {"text": text, **metadata}
    if stored_at is not None:
        payload["_semantics"] = {"owner": "u1", "stored_at": stored_at}
    return SimpleNamespace(id=point_id, payload=payload, vector=None)


@pytest.fixture
def stored(mongo_db):
    """A user with one project holding three memories, stored out of order."""
    user = users.add_user("olga@example.com", db=mongo_db)
    project = projects.add_project(user["_id"], "home", db=mongo_db)
    collections.register_collection(user["_id"], project["_id"], db=mongo_db)
    collections.set_points_count(user["_id"], project["_id"], 3, db=mongo_db)
    name = collections.collection_name(user["_id"], project["_id"])
    qdrant = FakeScrollQdrant(
        {
            name: [
                _point("p-mid", "bought flour", "2026-08-15T10:00:00+00:00", tag="shopping"),
                _point("p-new", "tiramisu recipe", "2026-08-16T09:00:00+00:00"),
                _point("p-old", "dentist at four", "2026-08-14T08:00:00+00:00"),
            ]
        }
    )
    return SimpleNamespace(user=user, project=project, qdrant=qdrant, collection=name)


def test_user_memories_returns_contents_newest_first(stored, mongo_db):
    view = admin.user_memories(
        stored.user["_id"], stored.project["_id"], db=mongo_db, qdrant=stored.qdrant
    )

    assert [row["id"] for row in view["rows"]] == ["p-new", "p-mid", "p-old"]
    assert view["rows"][0]["text"] == "tiramisu recipe"
    assert view["collection"] == stored.collection
    assert view["total"] == 3  # the registry's number, the one the user sees
    assert view["truncated"] is False


def test_user_memories_splits_payload_into_text_metadata_and_semantics(stored, mongo_db):
    view = admin.user_memories(
        stored.user["_id"], stored.project["_id"], db=mongo_db, qdrant=stored.qdrant
    )
    row = next(r for r in view["rows"] if r["id"] == "p-mid")

    assert row["text"] == "bought flour"
    assert row["metadata"] == {"tag": "shopping"}  # client metadata, not the text
    assert "text" not in row["metadata"] and "_semantics" not in row["metadata"]
    # owner/stored_at are the anchors, surfaced separately - not as "semantics".
    assert row["stored_at"] == "2026-08-15T10:00:00+00:00"
    assert row["semantics"] == {}


def test_user_memories_refuses_a_project_belonging_to_someone_else(stored, mongo_db):
    """The two ids come from the URL; the pair must be checked, not trusted."""
    intruder = users.add_user("someone.else@example.com", db=mongo_db)

    assert admin.user_memories(
        intruder["_id"], stored.project["_id"], db=mongo_db, qdrant=stored.qdrant
    ) is None
    assert admin.user_memories(
        stored.user["_id"], "no-such-project", db=mongo_db, qdrant=stored.qdrant
    ) is None
    assert admin.user_memories(
        "no-such-user", stored.project["_id"], db=mongo_db, qdrant=stored.qdrant
    ) is None


def test_user_memories_of_a_never_written_project_is_empty_not_an_error(mongo_db):
    """A project nobody stored into has no collection yet - that is normal."""
    user = users.add_user("fresh@example.com", db=mongo_db)
    project = projects.add_project(user["_id"], "empty", db=mongo_db)

    view = admin.user_memories(
        user["_id"], project["_id"], db=mongo_db, qdrant=FakeScrollQdrant()
    )

    assert view is not None
    assert view["rows"] == [] and view["scanned"] == 0


def test_user_memories_paginates(stored, mongo_db):
    first = admin.user_memories(
        stored.user["_id"], stored.project["_id"], limit=2, db=mongo_db, qdrant=stored.qdrant
    )
    second = admin.user_memories(
        stored.user["_id"], stored.project["_id"], offset=2, limit=2,
        db=mongo_db, qdrant=stored.qdrant,
    )

    assert [row["id"] for row in first["rows"]] == ["p-new", "p-mid"]
    assert first["has_prev"] is False and first["has_next"] is True
    assert [row["id"] for row in second["rows"]] == ["p-old"]
    assert second["has_prev"] is True and second["has_next"] is False


def test_memories_page_renders_the_text(unlocked, stored, mongo_db):
    app.dependency_overrides[deps.get_qdrant] = lambda: stored.qdrant
    try:
        page = unlocked.get(
            f"/admin/users/{stored.user['_id']}/projects/{stored.project['_id']}/memories"
        )
    finally:
        app.dependency_overrides.pop(deps.get_qdrant, None)

    assert page.status_code == 200
    assert "tiramisu recipe" in page.text
    assert "dentist at four" in page.text
    assert stored.collection in page.text


def test_memories_page_needs_the_unlock(client, enabled, stored):
    app.dependency_overrides[deps.get_qdrant] = lambda: stored.qdrant
    try:
        response = client.get(
            f"/admin/users/{stored.user['_id']}/projects/{stored.project['_id']}/memories",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(deps.get_qdrant, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_memories_page_reports_an_unreachable_store_instead_of_500(unlocked, stored):
    """The owner is usually here because something looks wrong; "the vector
    store is down" is an answer, not a crash."""
    class DeadQdrant:
        def collection_exists(self, collection_name):
            raise RuntimeError("connection refused")

    app.dependency_overrides[deps.get_qdrant] = lambda: DeadQdrant()
    try:
        page = unlocked.get(
            f"/admin/users/{stored.user['_id']}/projects/{stored.project['_id']}/memories"
        )
    finally:
        app.dependency_overrides.pop(deps.get_qdrant, None)

    assert page.status_code == 503
    assert "Memory store unreachable" in page.text


def test_unknown_project_is_404(unlocked, stored):
    app.dependency_overrides[deps.get_qdrant] = lambda: stored.qdrant
    try:
        page = unlocked.get(f"/admin/users/{stored.user['_id']}/projects/nope/memories")
    finally:
        app.dependency_overrides.pop(deps.get_qdrant, None)

    assert page.status_code == 404
