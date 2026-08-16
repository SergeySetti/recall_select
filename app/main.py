import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from pymongo.database import Database

from app import auth, mcp_server
from app.api import (
    admin,
    api_keys,
    collections,
    connect,
    me,
    memory,
    payments,
    projects,
    users,
)
from app.api.deps import get_optional_user
from app.dependencies import app_container
from app.i18n import (
    DEFAULT_LOCALE,
    LOCALE_COOKIE,
    SUPPORTED_LOCALES,
    negotiate_locale,
    t,
    translator,
)
from app.services import account, billing, docs, mongo
from app.services.usage import QuotaExceeded

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Signs the session cookie. A stable secret must be set in any real deployment;
# the dev fallback keeps local runs and tests working without configuration.
SESSION_SECRET = os.getenv("SESSION_SECRET") or "dev-insecure-session-secret"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Qdrant is touched lazily: every collection (one per user+project) is
    # created on the first memory store, so startup never reaches for it.
    # Ensure the Mongo indexes the CRUD layer relies on. Tolerate a cold/remote
    # Mongo so the landing page still serves.
    try:
        mongo.ensure_indexes(app_container.get(Database))
    except Exception:  # noqa: BLE001 - startup must not crash on a cold Mongo.
        logger.warning("Mongo not reachable at startup; deferring index setup.")
    # The MCP Streamable HTTP transport (task group behind /m/{key}) runs for
    # the whole life of the app.
    async with mcp_server.session_lifespan():
        yield


# The public site owns `/docs` (user documentation), so move FastAPI's built-in
# interactive API docs out from under it to `/api/*`.
app = FastAPI(
    title="recall.select",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
# Signed-cookie sessions hold the signed-in user_id and the OAuth handshake state.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Expose the translation helper to every template so copy is authored once in
# app/translations/*.yml and reused via {{ t("rs...") }}.
templates.env.globals["t"] = t
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Auth (Google OAuth sign-in) + the signed-in user's own endpoints.
app.include_router(auth.router)
app.include_router(me.router)
# The memory link's destination: agent connection instructions (Markdown).
app.include_router(connect.router)
# Management API over the services layer (users, API keys, projects, collections).
app.include_router(users.router)
app.include_router(api_keys.router)
app.include_router(projects.router)
app.include_router(collections.router)
app.include_router(memory.router)
# Monobank checkout + the verified webhook that grants a paid tier.
app.include_router(payments.router)
# The owner's read-only view of any account. Every route 404s unless
# ADMIN_SECRET is set in the environment, so an unconfigured deployment has no
# admin area to find.
app.include_router(admin.router)
# The MCP server behind the memory link. A raw Starlette route (not a FastAPI
# handler) - the Streamable HTTP transport speaks ASGI directly. Registered
# after the routers so /m/{key}.md (instructions) keeps winning for .md URLs.
# Two ways in, same credential: the single-URL default (`/m/{key}`) and a keyless
# `/mcp` that takes the key from an `Authorization: Bearer` header - for callers
# who'd rather keep the secret out of the URL (and out of access logs).
app.router.routes.append(
    Route("/m/{key}", endpoint=mcp_server.endpoint, methods=["GET", "POST", "DELETE"])
)
app.router.routes.append(
    Route("/mcp", endpoint=mcp_server.endpoint, methods=["GET", "POST", "DELETE"])
)

@app.exception_handler(QuotaExceeded)
async def _quota_exceeded(request: Request, exc: QuotaExceeded) -> JSONResponse:
    """Map an exhausted monthly call budget to HTTP 429 for the management API.

    (The MCP transport surfaces the same exception as a tool error, so agents get
    the message inline - see ``app.mcp_server``.)
    """
    return JSONResponse(status_code=429, content={"detail": str(exc)})


# The single URL an agent is fed to gain long-term memory.
RECALL_URL = "https://recall.select"


# A year - the language choice is a low-stakes preference, remember it long.
LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # Resolve the session user directly (the dependency isn't injected on a plain
    # route handler); stays None/cold-tolerant if Mongo isn't reachable.
    try:
        user = get_optional_user(request, app_container.get(Database))
    except Exception:  # noqa: BLE001 - landing page must render without a DB.
        user = None
    # Cookie choice first, browser Accept-Language second, English otherwise.
    locale = negotiate_locale(
        request.cookies.get(LOCALE_COOKIE), request.headers.get("accept-language")
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recall_url": RECALL_URL,
            "user": user,
            # Per-request translator overrides the default `t` global so copy,
            # meta tags and JS strings all render in the negotiated locale.
            "t": translator(locale),
            "locale": locale,
            "locales": SUPPORTED_LOCALES,
        },
    )


@app.get("/plans", response_class=HTMLResponse)
async def plans(request: Request) -> HTMLResponse:
    """The plans page: a plain one-row-per-plan table (see templates/plans.html)."""
    try:
        user = get_optional_user(request, app_container.get(Database))
    except Exception:  # noqa: BLE001 - the page must render without a DB.
        user = None
    locale = negotiate_locale(
        request.cookies.get(LOCALE_COOKIE), request.headers.get("accept-language")
    )
    return templates.TemplateResponse(
        request,
        "plans.html",
        {
            "plans": billing.plans_table(),
            "user": user,
            "t": translator(locale),
            "locale": locale,
            "locales": SUPPORTED_LOCALES,
        },
    )


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    """The signed-in personal area: usage, per-project data deletion, and upgrade.

    Sign-in-gated (redirects anonymous visitors to the Google flow). Unlike the
    landing page this needs a live Mongo to render, so a cold backend falls back
    to the home page rather than a 500."""
    try:
        db = app_container.get(Database)
        user = get_optional_user(request, db)
    except Exception:  # noqa: BLE001 - a cold backend sends the visitor home.
        return RedirectResponse(url="/", status_code=303)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    locale = negotiate_locale(
        request.cookies.get(LOCALE_COOKIE), request.headers.get("accept-language")
    )
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": user,
            "user_id": user["_id"],
            "summary": account.overview(user, db=db),
            # For building the reveal-once memory link after a key is created.
            "public_base_url": me.PUBLIC_BASE_URL,
            "t": translator(locale),
            "locale": locale,
            "locales": SUPPORTED_LOCALES,
        },
    )


@app.get("/docs", response_class=HTMLResponse)
async def docs_root() -> RedirectResponse:
    """No standalone docs landing yet - send visitors to the integrations index."""
    return RedirectResponse(url="/docs/integrations", status_code=307)


@app.get("/docs/integrations", response_class=HTMLResponse)
async def docs_integrations(request: Request) -> HTMLResponse:
    """Index of integration guides. Static content - renders without a DB."""
    locale = negotiate_locale(
        request.cookies.get(LOCALE_COOKIE), request.headers.get("accept-language")
    )
    return templates.TemplateResponse(
        request,
        "docs/integrations_index.html",
        {
            "integrations": docs.list_integrations(),
            "active_slug": None,
            "t": translator(locale),
            "locale": locale,
            "locales": SUPPORTED_LOCALES,
        },
    )


@app.get("/docs/integrations/{slug}", response_class=HTMLResponse)
async def docs_integration(slug: str, request: Request) -> HTMLResponse:
    """One integration guide. 404 (not 500) on an unknown slug; no DB needed."""
    integration = docs.get_integration(slug)
    if integration is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown integration")
    locale = negotiate_locale(
        request.cookies.get(LOCALE_COOKIE), request.headers.get("accept-language")
    )
    return templates.TemplateResponse(
        request,
        "docs/integration.html",
        {
            "integration": integration,
            "integrations": docs.list_integrations(),
            "active_slug": slug,
            "t": translator(locale),
            "locale": locale,
            "locales": SUPPORTED_LOCALES,
        },
    )


@app.get("/lang/{code}")
async def set_language(code: str, request: Request) -> RedirectResponse:
    """Remember an explicit language choice in a cookie, then return to the page.

    Redirects only to the site root (never an attacker-supplied URL), so the
    switcher can't be turned into an open redirect."""
    target = code if code in SUPPORTED_LOCALES else DEFAULT_LOCALE
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        LOCALE_COOKIE,
        target,
        max_age=LOCALE_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
    )
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
