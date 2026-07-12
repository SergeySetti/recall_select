"""Integration tests for the MCP server behind the memory link (``/m/{key}``).

Speaks real Streamable HTTP JSON-RPC at the endpoint through TestClient. The
transport is stateless with JSON responses, so each POST is a self-contained
request-response pair - no initialize handshake or session id needed.

Unlike the other API tests, TestClient is entered as a context manager: the
lifespan must run so the MCP session manager's task group exists. The app
container is faked so startup never reaches for live backends, and the MCP
module's provider hooks are rebound to the same fakes.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.database import Database
from qdrant_client import QdrantClient

from app import main, mcp_server
from app.services import api_keys, billing, mongo, usage, users, vector_semantics

HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


class FakeQdrant:
    """In-memory stand-in: keeps whole points (vector + payload) per collection.

    Ranks queries by real cosine so the semantic tools behave; supports the
    document-style operations (scroll, set_payload) the ``_semantics`` layer
    uses. ``self.data[collection][id]`` is a point namespace, not a bare payload.
    """

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
            bucket[p.id] = SimpleNamespace(id=p.id, vector=p.vector, payload=p.payload)

    def count(self, *, collection_name, exact=True):
        return SimpleNamespace(count=len(self.data.get(collection_name, {})))

    def retrieve(self, *, collection_name, ids, with_payload=True, with_vectors=False):
        bucket = self.data.get(collection_name, {})
        return [bucket[i] for i in ids if i in bucket]

    def delete(self, *, collection_name, points_selector):
        bucket = self.data.get(collection_name, {})
        for pid in points_selector.points:
            bucket.pop(pid, None)

    def query_points(self, *, collection_name, query, limit):
        bucket = self.data.get(collection_name, {})
        # A bare id in the query position resolves to that point's own vector.
        is_id = isinstance(query, (str, int)) and query in bucket
        vector = bucket[query].vector if is_id else query
        scored = sorted(
            (SimpleNamespace(id=p.id, score=vector_semantics._cosine(vector, p.vector), payload=p.payload)
             for p in bucket.values()),
            key=lambda h: h.score,
            reverse=True,
        )
        return SimpleNamespace(points=scored[:limit])

    def scroll(self, *, collection_name, limit, offset=None, with_payload=True, with_vectors=False):
        return list(self.data.get(collection_name, {}).values()), None

    def set_payload(self, *, collection_name, payload, points) -> None:
        bucket = self.data.get(collection_name, {})
        for pid in points:
            if pid in bucket:
                bucket[pid].payload = {**(bucket[pid].payload or {}), **payload}


class FakeContainer:
    """Stands in for the injector so the lifespan touches only fakes."""

    def __init__(self, db, qdrant) -> None:
        self._by_type = {Database: db, QdrantClient: qdrant}

    def get(self, cls):
        return self._by_type[cls]


@pytest.fixture
def env(monkeypatch):
    db = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(db)
    qdrant = FakeQdrant()

    monkeypatch.setattr(main, "app_container", FakeContainer(db, qdrant))
    monkeypatch.setattr(mcp_server, "get_database", lambda: db)
    monkeypatch.setattr(mcp_server, "get_qdrant", lambda: qdrant)
    monkeypatch.setattr(mcp_server, "get_embedder", lambda: (lambda text: [0.1, 0.2, 0.3]))

    user = users.add_user("mcp@example.com", db=db)
    key = api_keys.add_api_key(user["_id"], db=db)["key"]
    return SimpleNamespace(db=db, qdrant=qdrant, user=user, key=key)


def rpc(client, key: str, method: str, params: dict | None = None, id: int = 1):
    body: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/m/{key}", json=body, headers=HEADERS)


def call_tool(client, key: str, name: str, arguments: dict) -> dict:
    resp = rpc(client, key, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result
    # Prefer structured output; fall back to the JSON-in-text content block.
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_initialize_and_list_tools(env):
    with TestClient(main.app) as client:
        resp = rpc(client, env.key, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["serverInfo"]["name"] == "recall-select"

        tools = rpc(client, env.key, "tools/list", {}).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "store_memory", "recall_memory", "delete_memory",
            "link_memories", "unlink_memories", "annotate_memory",
            "memory_connections", "recall_connected",
        }


def test_store_then_recall_roundtrip(env):
    with TestClient(main.app) as client:
        stored = call_tool(client, env.key, "store_memory", {
            "text": "The user prefers dark mode",
            "metadata": {"tag": "prefs"},
        })
        assert stored["collection"].startswith("rs_")
        assert stored["points_count"] == 1
        # The point landed in the fake vector store, in the key owner's collection.
        assert env.user["_id"] in stored["collection"]
        assert len(env.qdrant.data[stored["collection"]]) == 1

        recalled = call_tool(client, env.key, "recall_memory", {"query": "UI preferences"})
        hits = recalled["result"] if isinstance(recalled, dict) else recalled
        assert len(hits) == 1
        payload = hits[0]["payload"]
        assert payload["text"] == "The user prefers dark mode"
        assert payload["tag"] == "prefs"
        # Every memory is born with its deixis anchors (owner + stored_at).
        assert payload["_semantics"]["owner"] == env.user["_id"]
        assert "stored_at" in payload["_semantics"]


def test_delete_memory_roundtrip(env):
    with TestClient(main.app) as client:
        stored = call_tool(client, env.key, "store_memory", {"text": "obsolete fact"})

        deleted = call_tool(client, env.key, "delete_memory", {"memory_id": stored["id"]})
        assert deleted == {"id": stored["id"], "deleted": True}
        assert env.qdrant.data[stored["collection"]] == {}

        recalled = call_tool(client, env.key, "recall_memory", {"query": "obsolete"})
        hits = recalled["result"] if isinstance(recalled, dict) else recalled
        assert hits == []

        # Deleting an id that no longer exists reports deleted: false.
        again = call_tool(client, env.key, "delete_memory", {"memory_id": stored["id"]})
        assert again["deleted"] is False


def test_recall_before_any_store_is_empty(env):
    with TestClient(main.app) as client:
        recalled = call_tool(client, env.key, "recall_memory", {"query": "anything"})
        hits = recalled["result"] if isinstance(recalled, dict) else recalled
        assert hits == []


def test_store_over_monthly_budget_is_tool_error(env):
    # Spend the free monthly budget for the key owner; the next tool call must
    # come back as a tool error (not a crash) carrying the limit message.
    usage.record_call(env.user["_id"], count=billing.FREE_CALLS, db=env.db)
    with TestClient(main.app) as client:
        resp = rpc(client, env.key, "tools/call", {
            "name": "store_memory",
            "arguments": {"text": "one too many"},
        })
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert result.get("isError")
        assert "Monthly call limit reached" in result["content"][0]["text"]


def test_semantic_layer_roundtrip(env):
    """The client-side reasoning loop: annotate, declare relations, walk the graph."""
    with TestClient(main.app) as client:
        a = call_tool(client, env.key, "store_memory", {"text": "Anna plans the kitchen remodel"})
        b = call_tool(client, env.key, "store_memory", {"text": "The remodel budget is 5000"})

        # Client-derived annotations land in the reserved namespace.
        ann = call_tool(client, env.key, "annotate_memory", {
            "memory_id": a["id"], "annotations": {"entities": ["kitchen remodel"]},
        })
        assert ann == {"id": a["id"], "annotated": True}
        call_tool(client, env.key, "annotate_memory", {
            "memory_id": b["id"], "annotations": {"entities": ["kitchen remodel"]},
        })

        # Client-built relations are validated and stored on the source point.
        linked = call_tool(client, env.key, "link_memories", {"relations": [
            {"source": b["id"], "target": a["id"], "type": "part_of"},
            {"source": b["id"], "target": "nope", "type": "part_of"},
        ]})
        assert linked["stored"] == 1
        assert linked["rejected"][0]["reason"] == "unknown memory id"

        # Both views: semantic neighbours + declared edges (incoming included).
        conns = call_tool(client, env.key, "memory_connections", {"memory_id": a["id"]})
        assert {r["type"] for r in conns["relations"]} == {"part_of"}
        assert conns["relations"][0]["source"] == b["id"]

        # Associative recall over explicit structure only reaches the target.
        hits = call_tool(client, env.key, "recall_connected", {
            "seed": b["id"], "layers": ["declared", "entity"],
        })
        hits = hits["result"] if isinstance(hits, dict) else hits
        assert a["id"] in {h["id"] for h in hits}

        # A wrong relation can be corrected: unlink removes it physically.
        removed = call_tool(client, env.key, "unlink_memories", {"relations": [
            {"source": b["id"], "target": a["id"]},
        ]})
        assert removed == {"removed": 1, "rejected": []}
        conns = call_tool(client, env.key, "memory_connections", {"memory_id": a["id"]})
        assert conns["relations"] == []

        # Deleting a memory prunes the dangling edges still aimed at it.
        call_tool(client, env.key, "link_memories", {"relations": [
            {"source": b["id"], "target": a["id"], "type": "part_of"},
        ]})
        call_tool(client, env.key, "delete_memory", {"memory_id": a["id"]})
        conns_b = call_tool(client, env.key, "memory_connections", {"memory_id": b["id"]})
        assert conns_b["relations"] == []


def test_unknown_key_is_404(env):
    with TestClient(main.app) as client:
        resp = rpc(client, "rs_not_a_real_key", "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        assert resp.status_code == 404
        assert resp.json() == {"detail": "unknown memory link"}


def test_bearer_header_auth_on_mcp(env):
    """The keyless /mcp endpoint authenticates via Authorization: Bearer <key>."""
    with TestClient(main.app) as client:
        headers = {**HEADERS, "authorization": f"Bearer {env.key}"}
        init = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
            headers=headers,
        )
        assert init.status_code == 200, init.text
        assert init.json()["result"]["serverInfo"]["name"] == "recall-select"

        # A tool call over the header route hits the same workspace as /m/{key}.
        stored = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "store_memory", "arguments": {"text": "via header"}},
            },
            headers=headers,
        )
        assert stored.status_code == 200, stored.text
        assert not stored.json()["result"].get("isError")


def test_mcp_without_bearer_is_404(env):
    """/mcp with no (or a bogus) bearer token looks identical to an unknown key."""
    with TestClient(main.app) as client:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
        assert client.post("/mcp", json=body, headers=HEADERS).status_code == 404
        bogus = {**HEADERS, "authorization": "Bearer rs_not_a_real_key"}
        assert client.post("/mcp", json=body, headers=bogus).status_code == 404


def test_md_route_still_serves_instructions(env):
    """/m/{key}.md must keep routing to the Markdown onboarding, not the MCP app."""
    from app.api.deps import get_database as deps_get_database

    main.app.dependency_overrides[deps_get_database] = lambda: env.db
    try:
        with TestClient(main.app) as client:
            resp = client.get(f"/m/{env.key}.md")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/markdown")
            assert f"/m/{env.key}" in resp.text
    finally:
        main.app.dependency_overrides.clear()
