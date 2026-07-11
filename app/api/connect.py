"""The memory link's destination - agent connection instructions.

An agent is handed the memory link (``{BASE}/m/{key}.md``). Fetching it returns a
self-contained **Markdown** document telling the agent how to wire recall.select up
as an MCP server. The credential is the key in the URL itself: the MCP Streamable
HTTP endpoint is simply this link with the ``.md`` dropped (``{BASE}/m/{key}``,
served by ``app.mcp_server``).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.api.deps import DbDep
from app.services import api_keys, docs

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

router = APIRouter(tags=["connect"])


def _instructions_md(base_url: str, key: str) -> str:
    """Render the agent-facing setup instructions for this key."""
    mcp_url = f"{base_url}/m/{key}"
    # Same helper the public /docs pages use, so the two never drift.
    path_config = docs.mcp_config_json(mcp_url)
    header_config = docs.mcp_config_json(f"{base_url}/mcp", header_key=key)
    return f"""# recall.select - agent memory setup

You've been handed a **recall.select** memory link. recall.select gives you
persistent long-term memory: you can **store** facts now and **recall** them by
meaning later, across sessions.

Connect to it as an **MCP server** over the **Streamable HTTP** transport. Your
credential is baked into the URL below - treat the whole URL as a secret.

## MCP server URL

```
{mcp_url}
```

## Configuration

Add it to your MCP client config. For a generic Streamable HTTP MCP client:

```json
{path_config}
```

No separate API key or auth header is required - the URL itself authenticates you
and scopes every call to your own memory workspace.

## Alternative: keep the key out of the URL

Embedding the key in the URL is the simplest setup, but the secret can then land
in access logs or proxies. If you'd rather not put it in the URL, point your
client at the key-less endpoint and send the key as a header instead:

```json
{header_config}
```

Both forms are equivalent - use whichever your client supports.

Once connected, use the server's memory tools to store new memories, to recall
relevant ones before answering, and to delete memories that turn out to be wrong
or stale.
"""


@router.get("/m/{key}.md")
def connection_instructions(key: str, db: DbDep) -> Response:
    if api_keys.get_by_key(key, db=db) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown memory link")
    return Response(
        content=_instructions_md(PUBLIC_BASE_URL, key),
        media_type="text/markdown; charset=utf-8",
    )
