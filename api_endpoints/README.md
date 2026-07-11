# API endpoints

Runnable request collections for the recall.select management API, in the
JetBrains HTTP client `.http` format. Click the green ▷ gutter icon next to any
request to run it.

| File                                   | Covers                                                     |
|----------------------------------------|------------------------------------------------------------|
| [`users.http`](users.http)             | Create / get / update users (+ 409 duplicate, 404)         |
| [`api_keys.http`](api_keys.http)       | Create / list / delete user-bounded API keys               |
| [`projects.http`](projects.http)       | Create / list / get / update / delete projects             |
| [`collections.http`](collections.http) | Register / get / delete the per-(user, project) collection |
| [`memory.http`](memory.http)           | Store memories and recall them by semantic search          |

Each file is self-contained: it creates the resources it needs (a user, a
project, …) and chains ids between requests with response-handler scripts
(`> {% client.global.set("userId", response.body.id); %}`) that store ids as
global vars, referenced later as `{{userId}}`. Just run the requests
top-to-bottom.

`@baseUrl` defaults to `http://localhost:8000` - see the project README for how
to run the app locally.

> **Out of date since access control landed.** The management/memory API is now
> signed-in-only and each caller may act only on their own resources, and the
> public `POST /api/users` endpoint was removed (accounts come from Google
> sign-in). These fixtures - which self-create a user and call across accounts -
> won't run as-is; they need a session cookie from `/auth/login` and a `user_id`
> that matches the signed-in user. Treat them as a route reference until updated.

> `example.http` is the original format sample (unrelated third-party APIs) and
> can be ignored or deleted.
