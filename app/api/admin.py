"""The owner's admin area (``/admin``): look at any user's personal space.

Server-rendered, read-only, and gated by one shared secret (``ADMIN_SECRET``,
see ``app.services.admin``). Three pages: unlock, the user list, and one user's
account snapshot - the same numbers that user sees at ``/account``.

Three deliberate choices about the credential:

* **Off unless configured.** With no ``ADMIN_SECRET`` in the environment every
  route here 404s, identical to a URL that was never registered.
* **Never in a URL.** The secret is submitted by POST form, so it stays out of
  browser history, referrers, and the proxy's access log. What lands in the
  session cookie afterwards is a flag and a timestamp, never the secret itself.
* **It unlocks looking, not acting.** Nothing on these pages writes to a user's
  account; the owner cannot generate links, delete data, or read memory text or
  key secrets from here.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import DbDep
from app.i18n import t
from app.services import admin

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["t"] = t

# Session keys holding the unlocked state (signed cookie, see app.main).
SESSION_FLAG = "admin_unlocked"
SESSION_AT = "admin_unlocked_at"

# Failed-unlock throttle. The secret is long and random, so this is a brake on
# scripted guessing rather than the security boundary: after LOCKOUT_AFTER bad
# attempts, that client waits LOCKOUT_SECONDS. Per process and in memory on
# purpose - a restart clearing it is acceptable for a single-owner tool.
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 300
_failures: dict[str, tuple[int, float]] = {}

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_enabled() -> None:
    """404 the whole area when no secret is configured."""
    if not admin.is_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _is_unlocked(request: Request) -> bool:
    """True when this session unlocked the area and the unlock hasn't aged out."""
    if not request.session.get(SESSION_FLAG):
        return False
    if admin.session_expired(request.session.get(SESSION_AT)):
        _lock(request)
        return False
    return True


def _lock(request: Request) -> None:
    """Drop the admin flags, leaving any normal sign-in session intact."""
    request.session.pop(SESSION_FLAG, None)
    request.session.pop(SESSION_AT, None)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked_out(request: Request) -> int:
    """Seconds this client must wait before another unlock attempt (0 if none)."""
    count, until = _failures.get(_client_key(request), (0, 0.0))
    if count >= LOCKOUT_AFTER and time.time() < until:
        return int(until - time.time())
    return 0


def _record_failure(request: Request) -> None:
    key = _client_key(request)
    count, _ = _failures.get(key, (0, 0.0))
    _failures[key] = (count + 1, time.time() + LOCKOUT_SECONDS)


def _page(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def unlock_form(request: Request):
    """The unlock page (or straight through to the list if already unlocked)."""
    _require_enabled()
    if _is_unlocked(request):
        return RedirectResponse(url="/admin/users", status_code=303)
    return _page(request, "admin/unlock.html", {"error": None, "wait": _locked_out(request)})


@router.post("", response_class=HTMLResponse)
async def unlock(request: Request, secret: str = Form(...)):
    """Check the submitted secret and, if it matches, unlock this session."""
    _require_enabled()

    wait = _locked_out(request)
    if wait:
        return _page(
            request,
            "admin/unlock.html",
            {"error": f"Too many attempts. Try again in {wait}s.", "wait": wait},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not admin.verify(secret):
        _record_failure(request)
        return _page(
            request,
            "admin/unlock.html",
            {"error": "Wrong key.", "wait": _locked_out(request)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    _failures.pop(_client_key(request), None)
    request.session[SESSION_FLAG] = True
    request.session[SESSION_AT] = time.time()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/logout")
async def lock(request: Request):
    """Re-lock the admin area (does not sign the owner out of their own account)."""
    _require_enabled()
    _lock(request)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: DbDep, q: str | None = None):
    """Every registered user, newest first, filtered by an optional search."""
    _require_enabled()
    if not _is_unlocked(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _page(
        request,
        "admin/users.html",
        {
            "rows": admin.list_users(query=q, db=db),
            "query": q or "",
            "total": admin.user_count(db=db),
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_page(user_id: str, request: Request, db: DbDep):
    """One user's personal area, exactly as the numbers reach them at /account."""
    _require_enabled()
    if not _is_unlocked(request):
        return RedirectResponse(url="/admin", status_code=303)
    space = admin.user_space(user_id, db=db)
    if space is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return _page(request, "admin/user.html", space)
