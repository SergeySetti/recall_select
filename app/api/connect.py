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
    toml_config = docs.mcp_config_toml(mcp_url)
    yaml_config = docs.mcp_config_yaml(mcp_url)
    header_yaml_config = docs.mcp_config_yaml(f"{base_url}/mcp", header_key=key)
    hermes_env = docs.hermes_env_var()
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

### Where that goes, per client

- **Claude Code** - `.mcp.json` in the project root (or run
  `claude mcp add --transport http recall-select {mcp_url}`).
- **Codex** - `~/.codex/config.toml`, and note it is **TOML, not JSON**:

```toml
{toml_config}
```

  Use `.codex/config.toml` inside a project to scope the memory to it. No
  `bearer_token_env_var` line is needed - the link already carries the
  credential. Restart Codex afterwards.
- **Hermes** - `~/.hermes/config.yaml`, in **YAML** under the snake_case
  `mcp_servers` key (a `url` is all it needs; the HTTP transport is inferred):

```yaml
{yaml_config}
```

  Merge the entry into an existing `mcp_servers:` section rather than adding a
  second one, or run `hermes mcp add {docs.SERVER_NAME} --url {mcp_url}`. Hermes
  prefixes borrowed tools, so `store_memory` appears as
  `mcp__recall_select__store_memory` (the hyphen in the server name becomes an
  underscore).
- **Any other MCP client** - wherever it keeps its MCP server list, in that
  client's own syntax. Full guides: {base_url}/docs/integrations

## Alternative: keep the key out of the URL

Embedding the key in the URL is the simplest setup, but the secret can then land
in access logs or proxies. If you'd rather not put it in the URL, point your
client at the key-less endpoint and send the key as a header instead:

```json
{header_config}
```

In Hermes's YAML that same alternative is:

```yaml
{header_yaml_config}
```

`hermes mcp add {docs.SERVER_NAME} --url {base_url}/mcp --auth header` writes it
for you and keeps the key in the profile's `.env` as `{hermes_env}`, leaving only
`${{{hermes_env}}}` in `config.yaml`.

Both forms are equivalent - use whichever your client supports. The key-less
`/mcp` endpoint answers **404** (`unknown memory link`) when the header is
missing or the key isn't recognised - that is an authentication failure, not a
missing endpoint.

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
