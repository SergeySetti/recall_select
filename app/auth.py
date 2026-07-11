"""Google OAuth (OpenID Connect) sign-in.

Three routes - ``/auth/login`` kicks off the Google consent flow, ``/auth/callback``
exchanges the code and resolves (or creates) the user, ``/auth/logout`` clears the
session. Identity is stored as ``user_id`` in the signed session cookie (see the
``SessionMiddleware`` wired in ``app.main``); ``app.api.deps.get_current_user``
reads it back.

Sign-in is the gate: a user must complete this flow before they can generate a
memory link.
"""
from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.api.deps import DbDep
from app.services import users

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Authlib client. Config is stored at import time; the OIDC metadata (and thus any
# network call) is fetched lazily on the first authorize_redirect, so importing
# this module with empty creds (e.g. under test) is harmless.
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    redirect_uri = f"{PUBLIC_BASE_URL}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request, db: DbDep):
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or await oauth.google.userinfo(token=token)
    user = users.get_or_create_google_user(
        info["sub"], info["email"], name=info.get("name"), db=db
    )
    request.session["user_id"] = user["_id"]
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
