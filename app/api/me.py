"""Endpoints for the signed-in user ("me").

Gated by the session cookie - `CurrentUser` 401s if the request isn't signed in.
This is where the "generate my memory link" button lands: it provisions (idempotently)
the user's default workspace and returns the link the agent is fed.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbDep
from app.api.schemas import MemoryLinkOut, MemoryLinkStatusOut, MeOut
from app.services import api_keys, workspaces

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

router = APIRouter(prefix="/api/me", tags=["me"])


def _memory_link(key: str) -> str:
    """The single URL fed to an agent - carries the API key as the path token.

    Ends in ``.md``: fetching it returns the Markdown connection instructions (see
    ``app.api.connect``). Dropping the ``.md`` yields the MCP Streamable HTTP URL.
    """
    return f"{PUBLIC_BASE_URL}/m/{key}.md"


@router.get("", response_model=MeOut)
def read_me(user: CurrentUser) -> MeOut:
    return MeOut.model_validate(user)


@router.get("/link", response_model=MemoryLinkStatusOut)
def link_status(user: CurrentUser, db: DbDep) -> MemoryLinkStatusOut:
    """Whether a memory link exists, in masked form only - never the secret.

    Lets the landing page show the state of an existing link without rotating
    it; only an explicit POST (below) mints and reveals a fresh one.
    """
    key = api_keys.get_labeled_key(user["_id"], "default", db=db)
    if key is None:
        return MemoryLinkStatusOut(exists=False)
    return MemoryLinkStatusOut(
        exists=True,
        masked_link=_memory_link(api_keys.masked(key)),
        created_at=key["created_at"],
        last_used_at=key.get("last_used_at"),
    )


@router.post("/link", response_model=MemoryLinkOut)
def generate_link(user: CurrentUser, db: DbDep) -> MemoryLinkOut:
    ws = workspaces.provision_default_workspace(user["_id"], db=db)
    key = ws["api_key"]["key"]
    return MemoryLinkOut(
        link=_memory_link(key),
        api_key=key,
        project_id=ws["project"]["_id"],
        collection=ws["collection"]["name"],
    )
