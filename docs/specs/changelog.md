# Changelog

Notable changes, newest first. Complements the point-in-time snapshots
(`project_state_as_for_*.md`) with a running record of what changed and why.

---

## 2026-08-17

### Hermes integration guide (YAML), and the header endpoint says what it is

A Hermes user reported that `https://recall.select/mcp` with an `Authorization`
header "returns 404, so the endpoint doesn't work", and stopped before
connecting. The endpoint is fine - a 404 there is the deliberate
`unknown memory link` auth response, identical however an invalid key is probed -
but nothing we shipped told them that, and we had no Hermes-shaped setup to
follow, so they were hand-translating the JSON config into Hermes's YAML.

- **`/docs/integrations/hermes`.** Hermes reads `~/.hermes/config.yaml` and keys
  servers under snake_case `mcp_servers`, with the HTTP transport inferred from
  `url` (no `type` field). New `docs.mcp_config_yaml` renders that from the same
  `mcp_config` entry the JSON and TOML forms use, so the three cannot drift; the
  registry gained `config_format="yaml"` and the header-auth alternative now
  renders in the page's own syntax instead of always JSON.
- **Tool names.** Hermes prefixes borrowed tools as `mcp__<server>__<tool>` and
  folds non-alphanumerics to `_`, so `store_memory` surfaces as
  `mcp__recall_select__store_memory`. Both the page and the per-key `.md` say so -
  that mismatch is the other half of "I can't see your store/recall/delete tools".
- **Secret out of the file.** `hermes mcp add recall-select --url .../mcp --auth
  header` keeps the key in the profile's `.env` as `MCP_RECALL_SELECT_API_KEY`
  and leaves `${MCP_RECALL_SELECT_API_KEY}` in `config.yaml`; `docs.hermes_env_var`
  mirrors Hermes's own derivation so the docs name the right variable.
- **`.md` instructions** now carry the Hermes YAML block (both forms) and state
  outright that a 404 from `/mcp` is an authentication failure, not a missing
  route.
- `Integration._render` raises on a TOML + header combination rather than
  silently emitting a config with the credential dropped.

Known, unfixed: `POST /mcp/` (trailing slash) 307s to a `http://` Location -
uvicorn isn't trusting the proxy's `X-Forwarded-Proto`. Caddy bounces it back to
HTTPS, but a strict client that refuses http redirects will fail there.

---

## 2026-07-02 (later still)

### Security: API keys hashed at rest, reveal-once links, Bearer-header option, log hygiene

Finished the key-handling story left open above. Keys are the whole agent
credential, and they were stored in plaintext and shown on demand.

- **Hashed at rest.** `api_keys` now stores only a SHA-256 digest (`key_hash`)
  plus a short non-secret `key_prefix` for display; the plaintext is returned
  exactly once from `add_api_key` and never persisted. `get_by_key` hashes the
  presented token and matches on the digest (a fast digest is fine - the token
  carries 128 bits of entropy). The unique index moved from `key` to `key_hash`.
- **Reveal-once links.** Because the secret can't be re-derived, "generate my
  memory link" now **rotates**: it drops the user's old default key, mints a
  fresh one, and reveals it once (old link stops working). The landing page no
  longer auto-fetches the link on load (which, with rotation, would silently
  invalidate it every visit) - it appears only on an explicit click. Key
  creation responds with `ApiKeySecretOut` (carries the token); listing responds
  with `ApiKeyOut` (prefix only, never the secret).
- **`Authorization: Bearer` option.** The single-URL model stays the default, but
  the key can now instead be sent as a header against a new key-less `/mcp`
  endpoint - for callers who'd rather keep the secret out of the URL. `/m/{key}`
  and `/mcp` resolve the same credential; the `.md` instructions document both.
- **Log hygiene.** The key rides in the URL path, so our own edge access log was
  the likeliest leak. The Caddy site now `log_skip`s `/m/*` and `/mcp` so
  key-bearing requests never hit the access log.

Threat-model note: these are complementary layers - hashing defends a DB dump;
the Bearer option and log-skip defend the secret in transit/logs.

> **Follow-up (not done):** the app's own `uvicorn` access log still records
> request paths, so `/m/{key}` can appear there. Redacting or disabling it in
> prod is the remaining log-hygiene gap.

## 2026-07-02 (later)

### Security: locked down the unauthenticated management/memory API (broken access control)

The entire `/api/**` management and memory surface was mounted with **no
authentication and no ownership checks** - every route took `user_id` /
`project_id` / `key_id` straight from the path. Anyone on the internet could:

- `GET /api/users/{user_id}/api-keys` → read any user's **plaintext** API keys
  (the whole agent credential), then use `/m/{key}` to read/write/delete all of
  that user's memories;
- read, write, or delete any user's memories, projects, and collections
  directly;
- mint or delete keys, and create/modify arbitrary users.

The UI only ever calls the session-gated `/api/me/link`, so these routes were
pre-auth scaffolding left open. Fix:

- **Ownership guards** in `app/api/deps.py`: `require_account_owner` (the caller
  must be the `{user_id}` in the path), `require_own_project`, `require_own_api_key`
  (404 rather than 403 so existence isn't confirmed). Anonymous requests now 401;
  cross-user requests 403/404.
- Applied across every management/memory router (`users`, `api_keys`, `projects`,
  `collections`, `memory`).
- **Removed the public `POST /api/users`** - accounts come only from Google
  sign-in, never an anonymous POST.
- **`tier` is no longer self-service** on `PATCH /api/users/{id}` (it's owned by
  the billing flow), closing a free self-upgrade.

Regression tests assert 401 for anonymous callers and 403/404 across accounts,
plus the happy paths signed in. The manual `.http` fixtures in `api_endpoints/`
pre-date auth and are flagged as out of date.

> **Still open (follow-up):** API keys are stored in plaintext at rest. With the
> listing endpoint now locked down, a leak requires DB access rather than an
> anonymous GET, but hashing keys at rest is still worth doing.

## 2026-07-02

### Collection provisioning is its own service (`app/services/collection_provisioning.py`)

A collection only exists once **both** its Mongo registry row (bookkeeping in
`collections.py`) and its backing Qdrant collection (`qdrant_store.py`) do. That
"register + ensure" pair used to be open-coded in several callers, each free to
drift or leave the two stores out of step.

It now lives in one place:

- `create_collection(user_id, project_id, *, db, qdrant)` - idempotently
  registers the Mongo row and ensures the backing Qdrant collection, returning
  the registry record.
- `destroy_collection(user_id, project_id, *, db, qdrant)` - drops the Qdrant
  collection and removes the Mongo row; returns whether anything was torn down.

The service sits between the core stores it composes (`collections` + `qdrant_store`)
and the callers above it. Creation is **lazy**: the only caller that creates the
Qdrant side is the first memory write (`memory.store_memory`). Workspace
provisioning (the "generate my memory link" flow) and the collection API register
just the Mongo row and leave Qdrant untouched; the collection API's `DELETE`
routes through `destroy_collection`.

Covered by `tests/test_collection_provisioning.py` (both-stores creation,
idempotency, teardown, missing-collection no-op).

### Fixed embedding/collection dimension mismatch → standardised on 768

`store_memory` was failing with a Qdrant `400 Bad Request`:
`Vector dimension error: expected dim: 1024, got 2560`. The remote embedder
(`Qwen/Qwen3-Embedding-4B`) returns its native 2560-dim vectors, which don't fit
the collections we create.

Fix: one knob, `VECTOR_SIZE = 768`, drives both sides so they can't drift.

- Collections are created at `VECTOR_SIZE` (default `1024 → 768`).
- `embeddings_remote.embed()` now sends `"dimensions": VECTOR_SIZE` to the
  DeepInfra API, so the returned vector is truncated (Matryoshka) to exactly the
  collection size.
- Config surfaces updated to 768: `.env.example`, `docker-compose.yml`,
  `docker-compose.local.yml`, README.
- Tests assert the `dimensions` field is sent; the base-url-override test was
  restored to a mocked call (it had been commented out and was hitting the
  network).

> **Ops note:** this only changes *newly created* collections. A collection that
> already exists at the old dimension is not resized by `ensure_collection`. Drop
> the stale prod collection (or wipe the `qdrant_storage` volume) so it is
> recreated at 768 on the next store - otherwise the error just becomes
> `768 vs 1024`. Safe to drop: no store ever succeeded, so there is no vector
> data to lose.

### Exposed the Qdrant dashboard at `qdrant.recall.select`, behind an API key

The Qdrant web dashboard is now fronted by the shared Caddy proxy over HTTPS.

- `deploy/caddy/recall.select.caddy` gains a `qdrant.recall.select` site block
  reverse-proxying to `recall-select-qdrant:6333`.
- The `qdrant` service joins `caddy_net` (in addition to `internal`) so the proxy
  can reach it by container name.
- The dashboard has **no auth of its own**, so access is gated by Qdrant's API
  key: the service reads `QDRANT__SERVICE__API_KEY` from a new `QDRANT_API_KEY`
  env (required in prod). Because a keyed Qdrant then rejects *every*
  unauthenticated request - including the web app's own internal calls - the app
  threads the key into both `QdrantClient` constructions (the DI singleton in
  `app/dependencies.py` and the `qdrant_store` fallback client). Unset = keyless,
  so local dev is unchanged.
- `QDRANT_API_KEY` documented in `.env.example` and the README config table.

> **Ops steps to go live** (outside the repo):
> 1. Set `QDRANT_API_KEY` in the server env / `.env` (now a required var - Compose
>    won't start without it). Generate one:
>    `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
> 2. Add a DNS **A record** for `qdrant.recall.select` → the server's IP, or a
>    `*.recall.select` wildcard, so Caddy can issue the certificate.
>
> Then open `https://qdrant.recall.select/dashboard` and paste the same key when
> the UI prompts for it.
