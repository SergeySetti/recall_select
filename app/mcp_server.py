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
from app.services import api_keys, memory, workspaces

INSTRUCTIONS = (
    "recall.select gives you persistent long-term memory. Call store_memory to "
    "save a fact worth keeping; call recall_memory to fetch the stored memories "
    "closest in meaning to a query - do this before answering when past context "
    "could matter; call delete_memory when a stored fact turns out to be wrong "
    "or stale. The URL you connected with already authenticates you and selects "
    "your memory workspace."
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
