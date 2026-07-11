"""Shared FastAPI dependencies for the API routers.

Thin adapters that resolve the app's singletons from the core DI container
(`app.dependencies.app_container`). Tests override these via
`app.dependency_overrides`, so the container is never touched under test.
"""
from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status
from pymongo.database import Database
from qdrant_client import QdrantClient

from app.dependencies import app_container
from app.services import api_keys as api_keys_service
from app.services import projects as projects_service
from app.services import users
from app.services.embeddings import Embedder


def get_database() -> Database:
    return app_container.get(Database)


def get_qdrant() -> QdrantClient:
    return app_container.get(QdrantClient)


def get_embedder() -> Callable[[str], list[float]]:
    # Return the bound method so callers keep a plain `embed(text)` callable.
    return app_container.get(Embedder).embed


DbDep = Annotated[Database, Depends(get_database)]
QdrantDep = Annotated[QdrantClient, Depends(get_qdrant)]
EmbedDep = Annotated[Callable[[str], list[float]], Depends(get_embedder)]


def get_optional_user(request: Request, db: DbDep) -> dict | None:
    """The signed-in user from the session cookie, or None if anonymous."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return users.get_user(user_id, db=db)


def get_current_user(user: Annotated[dict | None, Depends(get_optional_user)]) -> dict:
    """Require a signed-in user; 401 otherwise."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required")
    return user


OptionalUser = Annotated[dict | None, Depends(get_optional_user)]
CurrentUser = Annotated[dict, Depends(get_current_user)]


# --- Ownership guards ------------------------------------------------------
# The management + memory API is addressed by ids in the path (`{user_id}`,
# `{project_id}`, `{key_id}`). Without these, any signed-in request - or, before
# this, any request at all - could read or mutate another user's resources
# (including listing their API keys, which are the whole credential). Each guard
# below requires a signed-in user AND that they own the addressed resource.


def require_account_owner(user_id: str, current: CurrentUser) -> dict:
    """Authorize a `{user_id}`-scoped route: the caller must be that user."""
    if current["_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your account")
    return current


def require_own_project(project_id: str, current: CurrentUser, db: DbDep) -> dict:
    """The `{project_id}` project, only if the caller owns it (404 otherwise).

    404 rather than 403 so the endpoint never confirms the existence of a
    project belonging to someone else.
    """
    project = projects_service.get_project(project_id, db=db)
    if project is None or project["user_id"] != current["_id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


def require_own_api_key(key_id: str, current: CurrentUser, db: DbDep) -> dict:
    """The `{key_id}` API key, only if the caller owns it (404 otherwise)."""
    key = api_keys_service.get_api_key(key_id, db=db)
    if key is None or key["user_id"] != current["_id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    return key


AccountOwner = Annotated[dict, Depends(require_account_owner)]
OwnedProject = Annotated[dict, Depends(require_own_project)]
OwnedApiKey = Annotated[dict, Depends(require_own_api_key)]
