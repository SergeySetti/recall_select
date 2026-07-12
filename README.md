# recall.select

A minimal agentic memory system - feed one URL to any agent and it gains
long-term memory with near-zero setup. Built on Qdrant + FastMCP +
FastAPI/Bootstrap.

See [`docs/specs/initial_specification.md`](docs/specs/initial_specification.md)
for the full design and the incremental build plan, and
[`docs/specs/changelog.md`](docs/specs/changelog.md) for a running record of
notable changes.

## How it works

Memory is stored as vectors. Each memory store is a **Qdrant collection**, mapped
**one-to-one** to a `(user, project)` pair. Metadata around those vectors - users,
API keys, projects, and per-collection usage/limit stats - lives in **MongoDB**.

```
agent ──▶ FastAPI (web) ──▶ Qdrant         (vectors: one collection per user+project)
                         └▶ MongoDB        (users, API keys, projects, stats/limits)
                         └▶ embedding API  (remote, text → vector)
```

Qdrant collections are created **lazily**: nothing touches Qdrant until the
first memory is stored into a `(user, project)` pair.

## Architecture

- **`app/main.py`** - FastAPI app. Serves the Bootstrap landing page and, on
  startup, ensures the Mongo indexes exist (tolerant of a cold/remote DB).
- **`app/mcp_server.py`** - the MCP server behind the memory link. An agent's MCP
  client points at `{PUBLIC_BASE_URL}/m/{key}` (Streamable HTTP, stateless, JSON
  responses); the API key in the path is the whole credential and scopes the
  tools to the key owner's default project. Basic tools: `store_memory` /
  `recall_memory` / `delete_memory`. Semantic-layer tools (see
  `vector_semantics.py`): `link_memories` / `unlink_memories` /
  `annotate_memory` / `memory_connections` / `recall_connected` - the connected agent does the
  relation reasoning client-side (only on explicit demand) and these ingest or
  traverse the result. The same key can instead be sent as `Authorization: Bearer`
  against the key-less `/mcp` endpoint, to keep the secret out of the URL/logs.
  `{...}/m/{key}.md` (in `app/api/connect.py`) serves the matching setup
  instructions (both forms).
- **`app/dependencies.py`** - the core DI container (`injector`). Constructs the
  shared singletons (Qdrant client, Mongo client/db, the remote embedder).
  FastAPI deps (`app/api/deps.py`) and startup resolve from `app_container`
  rather than building clients themselves.
- **`app/services/`** - the service layer (no HTTP/route code, just I/O):
  - `qdrant_store.py` - Qdrant client + `ensure_collection`/`upsert_memory`/
    `search`/`delete_memory`, plus the point-level primitives the semantic
    layer needs (`neighbors`, `scroll_points`, `retrieve_points`, `set_payload`).
  - `vector_semantics.py` - the vector memory utility layer: treats a store as
    a graph of meaning. A reserved `_semantics` namespace in each point's
    payload holds deixis anchors (owner, stored-at; written at store time),
    client-extracted entities, and client-declared typed relations
    (`upsert_relations` validates and stores them - no LLM calls server-side).
    Declared relations carry two quality hedges: `confidence` (0-1], scales the
    edge's traversal strength) and `valid_till` (ISO 8601; expired edges are
    ignored by every read path, so stale structure retires itself). Hygiene:
    `remove_relations` deletes wrong edges (the corrective twin of
    `upsert_relations`), and `memory.delete_memory` calls `prune_relations_to`
    so no dangling edges survive a memory's deletion.
    Pluggable **lenses** (`topical`/`temporal`/`entity`/`declared`) derive
    typed edges; on top sit `semantic_graph` (multigraph), `spreading_activation`
    (retrieval by connection), `concept_clusters` (emergent ontology), and
    `infer_relation` (declared truth first, geometric heuristics after).
    Perf memo: incoming-edge lookup (`relations_of(include_incoming=True)`)
    is a bounded scroll-and-scan today. If reverse traversal becomes hot, the
    fix is a Qdrant **payload index** on `_semantics.relations[].target`
    (`create_payload_index`, keyword schema) and a filtered query instead of
    the scan - same store, just an index; nothing about the schema changes.
  - `mongo.py` - Mongo client, `get_db()`, and `ensure_indexes()` (enforces the
    one-to-one `(user, project)` rule with a unique compound index).
  - `users.py` - `add_user`, `get_user`, `get_user_by_email`, `update_user`.
  - `api_keys.py` - user-bounded keys, **stored as a SHA-256 hash** (the plaintext
    is returned once, from `add_api_key`, and never persisted): `add_api_key`,
    `delete_api_key`, `delete_user_keys`, `list_api_keys`, `get_by_key` (hashes
    the presented token and matches on the digest).
  - `projects.py` - `add_project`, `get_project`, `list_projects`,
    `update_project`, `delete_project`.
  - `collections.py` - the `(user, project) ↔ Qdrant collection` registry.
    `collection_name(user_id, project_id)` is the internal naming standard
    (`rs_{user}_{project}`); tracks `points_count`/`calls_count` for limits & stats.
  - `collection_provisioning.py` - the two-sided `create_collection` /
    `destroy_collection` step. A collection only exists once both its Mongo
    registry row **and** its backing Qdrant collection do; this composes the
    `collections` registry with `qdrant_store` into one atomic, idempotent
    operation so the two stores never fall out of step. Creation is lazy, so
    its only creating caller is the first memory write (`memory.store_memory`);
    the collection API's delete uses `destroy_collection`.
  - `embeddings.py` - the `Embedder` abstraction; `embeddings_remote.py` - the
    concrete text→vector backend (remote embedding API, e.g. DeepInfra).
  - `monobank.py` - minimal Monobank acquiring client (`create_invoice`) plus
    webhook auth (`fetch_pubkey` / `verify_signature`, ECDSA-SHA256 over the raw
    body). Reuses the mcp-api.net merchant token; recall.select owns its own
    invoice/redirect/webhook.
  - `billing.py` - the plan catalogue and the payment record keyed by Monobank's
    `invoiceId`. `record_pending` on checkout; `apply_webhook` flips the buyer's
    `tier` **once** on `success` (idempotent against retries/duplicates). Also the
    single source of truth for per-tier allowances: `call_allowance(tier)` /
    `project_allowance(tier)` (`None` = unlimited; unknown tiers fall back to free).
  - `usage.py` - the monthly call meter and the price-model gate. Every accepted
    store/recall/delete is tallied into a per-`(user, calendar-month)` `usage`
    row; `check_call_allowed` rejects a call once the tier's monthly
    `call_allowance` is spent, raising `QuotaExceeded`. Enforced in
    `memory.py` (so both the MCP tools and the HTTP memory API are covered) and
    mapped to **HTTP 429** by `app/main.py`; the MCP transport surfaces it as a
    tool error. Separate from the all-time `collections.calls_count`.
  - `account.py` - the read-only snapshot the signed-in `/account` page shows
    (plan, monthly usage, per-project stored counts), composed from
    `billing`/`usage`/`projects`/`collections`.
  - `docs.py` - content for the public `/docs` integration guides. Builds the MCP
    client config in one place (`mcp_config` / `mcp_config_json`), reused by both
    the docs pages **and** `app/api/connect.py`'s per-key `.md`, so the two never
    drift. `INTEGRATIONS` is the guide registry (add a page by adding an entry).

Public pages (served from **`app/main.py`**, Bootstrap + Jinja, i18n via
`app/translations/*.yml`): `/` landing, `/plans`, `/account` (signed-in), and the
`/docs/integrations` guides. FastAPI's built-in API docs are moved off `/docs` to
`/api/docs` (`/api/redoc`, `/api/openapi.json`) so the public site owns `/docs`.

Payments ride the HTTP layer in **`app/api/payments.py`**: `POST /api/me/checkout`
(signed-in) creates the invoice and returns the Monobank `pay_url`; the verified
`POST /webhooks/monobank` grants the tier; `GET /payment/success|fail` are the
cosmetic browser return pages (entitlement is webhook-driven, never these).

Every CRUD function takes an optional `db=`/`client=` argument so it can be driven
in tests without a live backend.

## Configuration

Set via environment (a local `.env` is auto-loaded; never commit it - see
[`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `MONGODB_URI` | _(required)_ | Remote, managed MongoDB connection string. |
| `MONGODB_DB` | `recall_select` | Database name. |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant endpoint (internal compose network). |
| `QDRANT_API_KEY` | _(none locally; required in prod)_ | Shared secret between the app and Qdrant. Compose sets Qdrant's `QDRANT__SERVICE__API_KEY` from it, and the app sends it on every request. It's the only gate on the `qdrant.recall.select` dashboard, which has no auth of its own. |
| `VECTOR_SIZE` | `768` | Vector dimension for every collection. The remote embedder is asked (via the API `dimensions` param) to return vectors of exactly this size, so the two stay in sync. |
| `EMBEDDING_API_KEY` | _(required)_ | API key for the remote embedding API. |
| `EMBEDDING_BASE_URL` | `https://api.deepinfra.com/v1` | OpenAI-compatible embeddings API base URL. |
| `GOOGLE_CLIENT_ID` | _(required for sign-in)_ | Google OAuth 2.0 Web client id. |
| `GOOGLE_CLIENT_SECRET` | _(required for sign-in)_ | Google OAuth 2.0 client secret. |
| `SESSION_SECRET` | _(dev fallback)_ | Signs the session cookie. Set a stable value in prod. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public origin; builds the memory link + OAuth redirect URI. |
| `MONOBANK_API_KEY` | _(required for payments)_ | Monobank acquiring merchant token. **Shared with the mcp-api.net platform** - same merchant, one account; invoices are told apart by `reference`. |
| `MONOBANK_REDIRECT_URL` | `{PUBLIC_BASE_URL}/payment/success` | Where the shopper's browser returns after paying. |
| `MONOBANK_WEBHOOK_URL` | `{PUBLIC_BASE_URL}/webhooks/monobank` | Server-to-server callback that grants the tier. Must be publicly reachable. |
| `MONOBANK_WEBHOOK_VERIFY` | `1` | Verify the webhook's `X-Sign` against the merchant pubkey. Keep on wherever money moves; `0` only for local dev. |

### Auth (Google sign-in)

Sign-in gates the memory link: a user signs in with Google, then clicks **Generate
my memory link** to provision their default project + collection + API key and get
the URL to feed an agent. To set up the Google credentials:

1. **Google Cloud Console → APIs & Services → OAuth consent screen** - configure
   it (External; add your email as a test user while unverified).
2. **Credentials → Create credentials → OAuth client ID → Web application.**
3. Add an **Authorized redirect URI**: `{PUBLIC_BASE_URL}/auth/callback` - e.g.
   `http://localhost:8000/auth/callback` for local dev and
   `https://recall.select/auth/callback` in prod (add both if you test locally).
4. Copy the **Client ID** and **Client secret** into `.env`
   (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`), and set a stable `SESSION_SECRET`
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).

## Run locally

The full stack (web + Qdrant) via Docker Compose:

```bash
cp .env.example .env   # then fill in MONGODB_URI
docker compose up --build
# open http://localhost:8000
```

Or just the app, against your own Qdrant/Mongo:

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

CRUD tests run against an in-memory Mongo (`mongomock`) and Qdrant/embedding
clients are faked - no live backends required.

## Deploy

```bash
./deploy/deploy.sh
```

The same command works from two places - it detects where it's run:

- **From a dev machine** (or the agent's box): pushes local commits, then runs the
  deploy on the server over the `recall-server` SSH alias.
- **On the server itself** (`setti@setti-server:~/recall_select$ ./deploy/deploy.sh`):
  deploys in place, no SSH hop.

Both paths run the same worker - [`deploy/_server_deploy.sh`](deploy/_server_deploy.sh):
`git` sync of `master`, rebuild the Compose stack (FastAPI `web` + Qdrant), reload
the shared Caddy proxy (automatic HTTPS for `recall.select`), prune old images.
MongoDB is remote/managed, so the auth/`MONGODB_URI` env (see `.env`) must be present
on the server.

Whoever runs it on the server needs **GitHub pull access** to the repo (an
authorised SSH key in their `~/.ssh`) and membership of the `docker` group - both
true for `claude-agent` and `setti`. The worker auto-registers the repo as a git
`safe.directory` so a deployer who isn't the repo's owner isn't blocked by
"dubious ownership".

### Automated deploys (CI)

Every push to `master` auto-deploys via GitHub Actions
([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) - the same flow as
above, just triggered by CI instead of a person. The job SSHes into the server and
pipes [`deploy/_server_deploy.sh`](deploy/_server_deploy.sh) over stdin, so it runs
the pushed commit's own deploy logic. Deploys are serialized (`concurrency`), and a
**Run workflow** button (`workflow_dispatch`) lets you deploy on demand.

One-time setup - add under **Settings → Secrets and variables → Actions**:

| Secret | Required | Purpose |
| --- | --- | --- |
| `DEPLOY_SSH_KEY` | yes | Private key whose public half is in the deploy user's `~/.ssh/authorized_keys`. |
| `DEPLOY_HOST` / `DEPLOY_USER` | yes | Server address and the SSH user to deploy as. |
| `DEPLOY_PORT` | no | SSH port (default `22`). |
| `DEPLOY_KNOWN_HOSTS` | no | Pin the server host key; if unset, CI trusts it on first use via `ssh-keyscan`. |

App secrets (`MONGODB_URI`, OAuth, etc.) stay in the server's `.env` - CI never sees
them.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE). If you run a
modified version as a network service, the AGPL requires you to offer its source
to your users. Copyright © 2026 Sergii Setti.
