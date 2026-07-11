# E2E basic testing

Ten manual checks covering the most important parts of recall.select as it stands
today: the app boots, the management **API** works (users → API keys → projects →
collections), the **one-to-one (user, project) ↔ Qdrant collection** mapping is
actually provisioned in Qdrant, and data **persists**. Not exhaustive - lower-value
paths (token deletion, project rename/delete, every 404) are intentionally skipped.

> These steps run the app directly with `uvicorn` against a local Qdrant and the
> remote managed Mongo. The Compose `web`/`qdrant` services only `expose` ports
> (no host publish), so `localhost` access is via the direct run. Step 10 covers
> the Compose stack + persistence.

## Prerequisites

- Docker running; Python env installed: `pip install -e ".[dev]"`.
- `.env` filled in (copy from `.env.example`). For host-run testing set Qdrant to localhost:
  - `MONGODB_URI=...` (the remote managed connection string)
  - `QDRANT_URL=http://localhost:6333`
- Use a real `curl` (`curl.exe` on Windows PowerShell, so the JSON `-d` body is sent verbatim).
- As you go, copy the `id` returned by each create call into the next command. Example placeholders below: `<USER_ID>`, `<PROJECT_ID>`, `<KEY_ID>`.

---

### 1. App boots and is healthy

Start a local Qdrant, then the app:

```bash
docker run --rm -d --name rs-qdrant -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant:v1.12.4
uvicorn app.main:app --port 8000
```

```bash
curl.exe -s http://localhost:8000/healthz
```

**Expect:** `{"status":"ok"}`. The uvicorn log shows no Mongo/Qdrant warnings (both reachable). If you see "Mongo not reachable" or "Qdrant not reachable", fix `.env` before continuing.

### 2. Landing page renders

Open <http://localhost:8000> in a browser (or `curl.exe -s http://localhost:8000`).

**Expect:** the Bootstrap landing page HTML, including the recall URL.

### 3. Create a user

```bash
curl.exe -s -X POST http://localhost:8000/api/users -H "Content-Type: application/json" -d '{"email":"e2e@example.com","name":"E2E"}'
```

**Expect:** HTTP 201 with JSON `{ "id": "...", "email": "e2e@example.com", "tier": "free", ... }`. Save the `id` as `<USER_ID>`.

_Quick uniqueness check:_ repeat the exact same POST → **409** (`email already registered`).

### 4. Read & update the user

```bash
curl.exe -s http://localhost:8000/api/users/<USER_ID>
curl.exe -s -X PATCH http://localhost:8000/api/users/<USER_ID> -H "Content-Type: application/json" -d '{"tier":"paid_5x"}'
```

**Expect:** GET returns the user; PATCH returns it with `"tier":"paid_5x"`.

### 5. Create a user-bounded API key

```bash
curl.exe -s -X POST http://localhost:8000/api/users/<USER_ID>/api-keys -H "Content-Type: application/json" -d '{"label":"laptop"}'
curl.exe -s http://localhost:8000/api/users/<USER_ID>/api-keys
```

**Expect:** 201 with a `key` starting `rs_` and `user_id` = `<USER_ID>`. The list call returns exactly that one key.

### 6. Create a project

```bash
curl.exe -s -X POST http://localhost:8000/api/users/<USER_ID>/projects -H "Content-Type: application/json" -d '{"name":"default"}'
```

**Expect:** 201 with the project. Save its `id` as `<PROJECT_ID>`.

### 7. Register the collection (the core: one-to-one provisioning)

```bash
curl.exe -s -X POST http://localhost:8000/api/users/<USER_ID>/projects/<PROJECT_ID>/collection
```

**Expect:** 201 with `name` = `rs_<USER_ID>_<PROJECT_ID>`, and `points_count` / `calls_count` both `0`.

### 8. Confirm the Qdrant collection actually exists

The registration must have provisioned the backing vector store, not just a Mongo row:

```bash
curl.exe -s http://localhost:6333/collections
```

**Expect:** the JSON `result.collections` list includes `rs_<USER_ID>_<PROJECT_ID>` (alongside `default`). This proves the Mongo registry and Qdrant are in sync.

### 9. One-to-one is enforced (idempotent re-register)

```bash
curl.exe -s -X POST http://localhost:8000/api/users/<USER_ID>/projects/<PROJECT_ID>/collection
curl.exe -s http://localhost:8000/api/users/<USER_ID>/projects/<PROJECT_ID>/collection
```

**Expect:** re-POST returns the **same** record (same `id`, no duplicate created); GET returns it with the current stats counters. Qdrant from step 8 still shows a single `rs_..._...` collection.

### 10. Data persists across a restart

Stop and restart the app and Qdrant, then re-read:

```bash
# Ctrl-C uvicorn, then:
docker restart rs-qdrant
uvicorn app.main:app --port 8000
```

```bash
curl.exe -s http://localhost:8000/api/users/<USER_ID>
curl.exe -s http://localhost:6333/collections
```

**Expect:** the user still exists (data is in the remote managed Mongo) and the `rs_<USER_ID>_<PROJECT_ID>` collection is still present (Qdrant `qdrant_storage` named volume survived the restart).

---

#### Cleanup

```bash
docker rm -f rs-qdrant
# Optional: drop the test volume so the next run starts clean
docker volume rm qdrant_storage
```

The `e2e@example.com` user and its project/key/collection records remain in the
remote Mongo - delete them via the API (`DELETE /api/projects/{id}`,
`DELETE /api/api-keys/{id}`) or directly in the database if you want a clean slate.
