## Recall.select

The project aimed to be the simplest possible implementation of an agentic memory system. The core idea was to have a
single URL, by feeding which into any agent will ad him the possibility to gain long-term memory with near-zero effert
from the user.

Basic workflow:

1. User feeds the URL to the agent.
2. Agent fetches the content of the URL and gets the basic understanding of how to connect or implement the tools on top
   of recall.select
3. As the system needs an API key to work, the agent will ask the user for it, and then store it in the connector config
   or in any function implementation it decided to use.

Every memory instance - it's a Qdrant collection. Per user per project.

Registration and login: Google OAuth only.

After login, user have his default API key, default project and default collection. User can create more projects and
collections, but the system will always have a default one, which will be used if the agent doesn't specify otherwise.

The project needs it's clients implemented in Python and JavaScript:

- MCP server with config: API key, project name
- Skill with implementation of how to write tools for recall.select and how to use them in the agentic memory system.

### Infrastructure

- Qdrant for vector database
- FastMCP for MCP server
- FastAPI with Bootstrap for memory management UI

### Deployment

Everything ships as Docker containers, orchestrated with Docker Compose. No
hand-provisioning of the server.

- **Web/UI**: FastAPI app (the Bootstrap UI + later the MCP/API surface), served
  by `uvicorn` inside a container, reachable on the internal network as
  `web:8000` (also published on the LAN for debugging).
- **Shared Caddy proxy**: the server runs a single, standalone reverse proxy
  stack (`/home/setti/farm/proxy/`, `caddy:2`) that owns ports 80/443 and
  terminates TLS with automatic Let's Encrypt certs for *all* sites on the box
  (currently `startups.setti.ai` and `recall.select`). The recall stack does
  **not** run its own Caddy.
- **Per-project routing**: each fronted project owns its Caddy site block in its
  own repo under `deploy/caddy/*.caddy` (this repo:
  `deploy/caddy/recall.select.caddy` → `reverse_proxy recall-select-web:8000`).
  The proxy mounts each project's `deploy/caddy/` directory and `import`s the
  fragments, so routing config lives with the project it belongs to, not in the
  central proxy.
- **Shared network**: the proxy and every fronted app join an external Docker
  network, `caddy_net` (created once with `docker network create caddy_net`).
  The `web` service attaches to it and is reached by container name.
- **Qdrant** (later): official `qdrant/qdrant` image, persisted via a named
  volume.
- The recall containers are defined in `docker-compose.yml` at the repo root;
  the proxy + its `Caddyfile` live in `/home/setti/farm/proxy/` on the server.
- The host server runs Docker + the Compose plugin. Deployment is a `git pull`
  on the server followed by `docker compose build && docker compose up -d`,
  wrapped in `deploy/deploy.sh` (run from the dev machine over the
  `recall-server` SSH alias). `caddy_net` must exist first.
- DNS: the `recall.select` A record must point at the server's IP for Caddy to
  issue a certificate. The domain's DNS is managed in DigitalOcean.
- Bootstrap is pulled from CDN - no front-end build step.

#### Incremental build plan

1. **Step 1 (current)** - static Bootstrap landing page served by FastAPI in a
   container, plus the Docker/Compose deployment automation. No DB, no auth yet.
2. Add Qdrant service + collection/project/API-key management UI.
3. Add Google OAuth login.
4. Add MCP server (FastMCP) and the Python/JS clients + Skill.

### UI

- simple and clean. Dead simple actually. Any additional element treat es EXPENSIVE for the user, his memory and his
  cognitive load. So the UI should be as minimalistic as possible, with only necessary elements to manage projects, API
  keys and collections.
- Color scheme: near black background with beige text and orange accents.
- Main page: URL to feed the agent
- Management section: API key management, project management, collection management. Each section should be as simple as
  possible, with only necessary elements to perform the actions. For example, API key management should have only a form
  to add a new API key and a list of existing API keys with the option to delete them. Project management should have a
  form to create a new project and a list of existing projects with the option to delete them. Collection management
  should have a form to create a new collection and a list of existing collections with the option to delete them.

### Payment and tiring

- The project will have a free tier with limited number of memory sections and calls per month.
- Paid tiers will have more memory sections and calls per month

Payment is handled in-app via Monobank acquiring, reusing the **same merchant token** as the mcp-api.net platform (only
the credential is shared - recall.select creates its own invoices and owns its own redirect/webhook). A dedicated
`/plans`
page renders the tier table below; a signed-in user picks a tier, is redirected to the Monobank checkout, and a
**signature-verified** webhook grants the corresponding `tier`. See `app/services/{monobank,billing}.py` and
`app/api/payments.py`.

#### Calculations v2

| Tier    | Calls per month | Projects | Price (USD/month) |
|---------|-----------------|----------|-------------------|
| Free    | 3 000           | 1        | $0/month          |
| Paid 2x | 6 000           | 2        | $17/month         |
| Paid 5x | 20 000          | 5        | $77/month         |
| Unlim   | unlim           | unlim    | $227/month        |



#### Calculations (deprecated)

| Tier      | Memory sections | Calls per month | Price (USD/month) |
|-----------|-----------------|-----------------|-------------------|
| Free      | 1 000           | 10 000          | $0                |
| Paid 2x   | 2 000           | 20 000          | $9                |
| Paid 5x   | 5 000           | 50 000          | $19               |
| Paid 10x  | 10 000          | 100 000         | $39               |
| Paid 50x  | 50 000          | 500 000         | $99               |
| Paid 100x | 100 000         | 1 000 000       | $149              |
