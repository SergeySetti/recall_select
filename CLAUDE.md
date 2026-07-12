# Project Context for Claude

> Access & infrastructure details (SSH, server, deploy credentials) live in
> `CLAUDE.local.md`, which is gitignored and never published.

## Project
See `docs/specs/initial_specification.md` - **recall.select**, a minimal agentic
memory system (single-URL onboarding) built on Qdrant + FastMCP + FastAPI/Bootstrap.
`README.md` has the architecture + config reference; keep both in sync as the
stack grows.

### Data stores
- **Qdrant** (`qdrant/qdrant`, internal compose service `qdrant:6333`, named
  volume `qdrant_storage`) - vectors. Each memory store is one Qdrant collection,
  mapped **one-to-one** to a `(user, project)` pair. Naming standard:
  `rs_{user_id}_{project_id}` (see `app/services/collections.py:collection_name`).
- **MongoDB** - *remote, managed* (DigitalOcean). Holds users, API keys, projects,
  and per-collection usage/limit stats. Connection string is **`MONGODB_URI`**,
  supplied via a gitignored `.env` locally (template: `.env.example`) and the
  server environment in prod. DB name defaults to `recall_select` (`MONGODB_DB`).
  Not a container - nothing to run locally; tests use `mongomock`.

### Services layer (`app/services/`)
Pure I/O, no route code. Every CRUD fn takes an optional `db=`/`client=` for
testability. `mongo.py` (client + `ensure_indexes`, which enforces the
one-to-one rule via a unique compound index on `collections.(user_id,
project_id)`), `qdrant_store.py`, `users.py`, `api_keys.py`, `projects.py`,
`collections.py`, `embeddings_remote.py` (the only embedding backend - a remote
API, `EMBEDDING_API_KEY`), `vector_semantics.py` (the semantic-connections
layer over stored points: a reserved `_semantics` payload namespace per point -
deixis anchors written at store time by `memory.store_memory` - plus lens-based
typed edges and graph/activation/ontology methods; relation *reasoning* happens
client-side, the service only validates/stores declared relations and never
calls an LLM). `app/main.py` ensures the Mongo indexes on startup,
tolerant of a cold backend. Qdrant collections are created lazily, on the first
memory store into a `(user, project)` pair - never at startup or provisioning.

### MCP server (`app/mcp_server.py`)
The memory link's MCP endpoint: `/m/{key}` speaks Streamable HTTP (official
`mcp` SDK / FastMCP, stateless + JSON responses; the transport's session manager
runs inside the app lifespan). The API key in the path is the whole credential -
the tools (`store_memory` / `recall_memory` / `delete_memory`, plus the
semantic-layer tools `link_memories` / `unlink_memories` / `annotate_memory` /
`memory_connections` / `recall_connected`) resolve it to the key owner's
**default project**. `/m/{key}.md` (`app/api/connect.py`) serves the matching
setup instructions and must keep route precedence for `.md` URLs.

### Tests
`pytest` (deps: `pip install -e ".[dev]"`). No live backends - Mongo is faked
with `mongomock`, Qdrant/embedding clients with hand-rolled fakes in
`tests/conftest.py`.
