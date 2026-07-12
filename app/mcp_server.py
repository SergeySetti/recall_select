"""The MCP server behind the memory link (``/m/{key}``).

An agent's MCP client is pointed at ``{BASE}/m/{key}`` (Streamable HTTP
transport). The API key in the path is the whole credential: it scopes every
tool call to the key owner's **default project** - one URL, zero further setup.

Built on FastMCP (the official ``mcp`` SDK). The transport runs **stateless**
with plain JSON responses: every request creates a fresh transport, so no
session affinity is needed and multiple workers are safe. The tools are thin -
they resolve the workspace from the key, then delegate to ``app.services``.

Wiring lives in ``app.main``: the ``/m/{key}`` route delegates to ``endpoint``
and the app lifespan runs ``session_lifespan()`` (the transport's task group).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncIterator

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pymongo.database import Database
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import get_database, get_embedder, get_qdrant  # noqa: F401 - rebindable in tests
from app.services import api_keys, collections, memory, usage, vector_semantics, workspaces

INSTRUCTIONS = (
    "recall.select gives you persistent long-term memory. Call store_memory to "
    "save a fact worth keeping; call recall_memory to fetch the stored memories "
    "closest in meaning to a query - do this before answering when past context "
    "could matter; call delete_memory when a stored fact turns out to be wrong "
    "or stale. Memories can also be linked into a knowledge graph: when the "
    "user asks for it (or a connection is clearly durable), infer relations "
    "and entities yourself and record them with link_memories / "
    "annotate_memory; use memory_connections to inspect what one memory links "
    "to, and recall_connected for associative recall along those links. The "
    "URL you connected with already authenticates you and selects your memory "
    "workspace."
)

mcp = FastMCP("recall-select", instructions=INSTRUCTIONS)


def _extract_key(request: Request) -> str | None:
    """The API key, taken from the ``/m/{key}`` path or an ``Authorization`` header.

    Two ways to present the same credential: the single-URL default (key in the
    path) and, for callers who prefer to keep secrets out of URLs/logs, a standard
    ``Authorization: Bearer <key>`` header on a keyless ``/mcp`` endpoint.
    """
    key = request.path_params.get("key")
    if key:
        return key
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def _workspace_from_context(ctx: Context, db: Database) -> tuple[str, str]:
    """Resolve (user_id, project_id) from the request's key.

    The Starlette request travels with the MCP request context. Unknown keys are
    normally rejected at the HTTP layer already (see ``endpoint``), so this is a
    second line of defence.
    """
    key = _extract_key(ctx.request_context.request)
    key_doc = api_keys.get_by_key(key, db=db) if key else None
    if key_doc is None:
        raise ValueError("unknown memory link")
    project = workspaces.ensure_default_project(key_doc["user_id"], db=db)
    return key_doc["user_id"], project["_id"]


@mcp.tool()
async def store_memory(
    text: str, ctx: Context, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Save one memory for later recall.

    Store durable facts: user preferences, decisions, project context,
    lessons learned. `text` is what gets embedded and matched on recall, so
    make it a self-contained statement. Optional `metadata` (small JSON
    object, e.g. tags or a source) is returned alongside the text on recall.
    """
    def _store() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        return memory.store_memory(
            user_id, project_id, text,
            metadata=metadata, db=db, qdrant=get_qdrant(), embed=get_embedder(),
        )

    # Services are blocking I/O (pymongo/qdrant/httpx) - keep the event loop free.
    return await anyio.to_thread.run_sync(_store)


@mcp.tool()
async def recall_memory(query: str, ctx: Context, limit: int = 5) -> list[dict[str, Any]]:
    """Recall the stored memories closest in meaning to `query`.

    Semantic search, not keyword search - describe what you want to know
    ("user's UI preferences") rather than exact words. Returns up to `limit`
    memories, closest first, each with its similarity score and the stored
    text/metadata payload. An empty list means nothing has been stored yet.
    """
    def _recall() -> list[dict[str, Any]]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        return memory.recall_memory(
            user_id, project_id, query,
            limit=limit, db=db, qdrant=get_qdrant(), embed=get_embedder(),
        )

    return await anyio.to_thread.run_sync(_recall)


@mcp.tool()
async def delete_memory(memory_id: str, ctx: Context) -> dict[str, Any]:
    """Permanently delete one stored memory.

    Use this to correct the record: a fact that turned out to be wrong,
    became stale, or should never have been stored. `memory_id` is the `id`
    returned by store_memory and by each recall_memory hit. Returns
    `deleted: false` if no memory with that id exists.
    """
    def _delete() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        deleted = memory.delete_memory(
            user_id, project_id, memory_id, db=db, qdrant=get_qdrant()
        )
        return {"id": memory_id, "deleted": deleted}

    return await anyio.to_thread.run_sync(_delete)


def _record_usage(user_id: str, project_id: str, db: Database) -> None:
    """Count one call against the monthly and per-collection meters.

    The semantic tools delegate to ``vector_semantics``, which is a pure
    toolset with no metering of its own - budget checks (``check_call_allowed``
    before the work) and this recording happen here at the MCP boundary,
    mirroring what ``memory.py`` does in-service for the basic tools.
    """
    collections.record_call(user_id, project_id, db=db)
    usage.record_call(user_id, db=db)


@mcp.tool()
async def link_memories(
    relations: list[dict[str, Any]], ctx: Context
) -> dict[str, Any]:
    """Record typed relations you have inferred between stored memories.

    Do the reasoning yourself, then declare the result - use this when the
    user explicitly asks to connect memories, or when a relation is clearly
    durable (part-of, causal, same-event...). Each relation needs `source`,
    `target` (memory ids from store_memory/recall_memory) and a short `type`
    (e.g. "part_of", "causes", "about_same_event"); optional `directed`
    (default true), `weight` (0-1], and `evidence` (small JSON note on why).
    Hedge honestly: set `confidence` (0-1] below 1.0 when you are not certain -
    unsure links influence recall proportionally less - and set `valid_till`
    (ISO 8601) on relations with a shelf life ("valid until the trip", "until
    the deadline"); expired links stop affecting recall automatically.
    Declared relations power memory_connections and recall_connected, and
    re-declaring the same (source, target, type) updates it. Returns counts
    plus per-relation rejection reasons.
    """
    def _link() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        usage.check_call_allowed(user_id, db=db)
        result = vector_semantics.upsert_relations(
            user_id, project_id, relations, db=db, qdrant=get_qdrant()
        )
        _record_usage(user_id, project_id, db)
        return result

    return await anyio.to_thread.run_sync(_link)


@mcp.tool()
async def unlink_memories(
    relations: list[dict[str, Any]], ctx: Context
) -> dict[str, Any]:
    """Remove declared relations that turned out to be wrong.

    The corrective twin of link_memories - use it when a linked connection is
    incorrect or should never have been declared (for relations that merely
    expire, prefer valid_till on link_memories). Each item needs `source` and
    `target` as originally declared (relations belong to their source memory);
    add `type` to remove one specific relation, omit it to remove every
    relation from source to target. Deleting a memory already cleans up
    relations pointing at it - no need to unlink first. Returns the removed
    count plus per-item rejection reasons.
    """
    def _unlink() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        usage.check_call_allowed(user_id, db=db)
        result = vector_semantics.remove_relations(
            user_id, project_id, relations, db=db, qdrant=get_qdrant()
        )
        _record_usage(user_id, project_id, db)
        return result

    return await anyio.to_thread.run_sync(_unlink)


@mcp.tool()
async def annotate_memory(
    memory_id: str, annotations: dict[str, Any], ctx: Context
) -> dict[str, Any]:
    """Attach semantic annotations to one stored memory.

    For facts you have extracted from the memory's text on demand - most
    importantly `entities` (a list of normalised names: people, companies,
    projects, places), which lets recall_connected follow "same entity" links.
    Other keys (resolved dates, places, tags) are stored as given. Merges
    key-by-key into the memory's semantic record; relations belong in
    link_memories, not here. Returns `annotated: false` for unknown ids.
    """
    def _annotate() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        usage.check_call_allowed(user_id, db=db)
        annotated = vector_semantics.annotate_memory(
            user_id, project_id, memory_id, annotations, db=db, qdrant=get_qdrant()
        )
        _record_usage(user_id, project_id, db)
        return {"id": memory_id, "annotated": annotated}

    return await anyio.to_thread.run_sync(_annotate)


@mcp.tool()
async def memory_connections(
    memory_id: str, ctx: Context, limit: int = 10
) -> dict[str, Any]:
    """Everything one memory is connected to.

    Two views in one call: `similar` - the memories nearest in meaning
    (semantic neighbours, closest first) - and `relations` - the typed links
    previously declared via link_memories, incoming and outgoing. Use it to
    explore around a memory before reasoning about it, or to check what is
    already linked before declaring new relations.
    """
    def _connections() -> dict[str, Any]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        usage.check_call_allowed(user_id, db=db)
        qdrant = get_qdrant()
        similar = vector_semantics.related_memories(
            user_id, project_id, memory_id, limit=limit, db=db, qdrant=qdrant
        )
        relations = vector_semantics.relations_of(
            user_id, project_id, memory_id, include_incoming=True, db=db, qdrant=qdrant
        )
        _record_usage(user_id, project_id, db)
        return {"id": memory_id, "similar": similar, "relations": relations}

    return await anyio.to_thread.run_sync(_connections)


@mcp.tool()
async def recall_connected(
    seed: str,
    ctx: Context,
    hops: int = 2,
    limit: int = 10,
    layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Associative recall: walk the memory graph outward from a seed.

    `seed` is a memory id or a free-text query. Where recall_memory returns a
    flat top-k, this follows connections hop by hop, so context arrives as a
    linked neighbourhood - a memory two links away still surfaces, ranked
    below its closer connectors. `layers` picks which links to follow:
    "topical" (by meaning, the default), "entity" (same annotated entity),
    "temporal" (stored close in time), "declared" (relations from
    link_memories) - e.g. ["declared", "entity"] follows only explicit
    structure. Returns memories ranked by connection strength.
    """
    def _recall() -> list[dict[str, Any]]:
        db = get_database()
        user_id, project_id = _workspace_from_context(ctx, db)
        usage.check_call_allowed(user_id, db=db)
        result = vector_semantics.spreading_activation(
            user_id, project_id, seed,
            lenses=tuple(layers) if layers else ("topical",),
            hops=hops, limit=limit,
            db=db, qdrant=get_qdrant(), embed=get_embedder(),
        )
        _record_usage(user_id, project_id, db)
        return result

    return await anyio.to_thread.run_sync(_recall)


# The running transport manager. Set for the duration of `session_lifespan()`;
# a manager instance cannot be reused after its run() exits (SDK contract), so
# each lifespan run (app start, every TestClient context) builds a fresh one.
_session_manager: StreamableHTTPSessionManager | None = None


@asynccontextmanager
async def session_lifespan() -> AsyncIterator[None]:
    """Run the Streamable HTTP transport; wrap the app lifespan's ``yield``."""
    global _session_manager
    manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,  # noqa: SLF001 - the SDK exposes no public accessor.
        json_response=True,
        stateless=True,
    )
    async with manager.run():
        _session_manager = manager
        try:
            yield
        finally:
            _session_manager = None


class _MCPEndpoint:
    """Raw ASGI endpoint for ``/m/{key}``: authenticate, then hand to the transport.

    Raw (not a FastAPI handler) because the transport wants the bare
    scope/receive/send to speak Streamable HTTP itself.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Key from the path (`/m/{key}`) or an Authorization: Bearer header
        # (keyless `/mcp`). Build a Request just to read them - it doesn't touch
        # the body, so the transport can still consume `receive` below.
        key = _extract_key(Request(scope, receive))
        # Same 404 as the .md instructions route - an invalid link should look
        # identical whichever way it is probed. Lookup is blocking pymongo.
        key_doc = await anyio.to_thread.run_sync(
            partial(api_keys.get_by_key, key, db=get_database())
        ) if key else None
        if key_doc is None:
            response = JSONResponse({"detail": "unknown memory link"}, status_code=404)
            await response(scope, receive, send)
            return
        if _session_manager is None:
            response = JSONResponse({"detail": "MCP transport not running"}, status_code=503)
            await response(scope, receive, send)
            return
        await _session_manager.handle_request(scope, receive, send)


endpoint = _MCPEndpoint()
