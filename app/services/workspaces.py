"""Workspace provisioning (services layer).

A "workspace" is what the user gets behind their memory link: a **default
project**, its one-to-one collection registration, and an **API key** the agent
authenticates with. This stitches the existing project / collection / api-key
services into the single step the "generate my memory link" button needs.

The project and collection converge idempotently (exactly one of each). The
**default key is reissued** on every call, because keys are stored hashed and can
never be re-revealed (see `app.services.api_keys`): to hand back a working link
we must mint a fresh key and show it once, invalidating any previous one. So
"generate my memory link" is a rotate-and-reveal, not an idempotent read.

Only the Mongo side is provisioned here; the backing **Qdrant collection is
created lazily on the first memory store** (see `app.services.memory`).

Pure I/O - `db` is injected so this is testable without live backends.
"""
from __future__ import annotations

from pymongo.database import Database

from app.services import api_keys, collections, projects

DEFAULT_PROJECT_NAME = "default"


def ensure_default_project(user_id: str, *, db: Database) -> dict:
    """The user's default project, created (and flagged) on first call.

    Public because the MCP server resolves every keyed request to the key
    owner's default project (the spec's "use the default unless the agent
    specifies otherwise").
    """
    existing = db.projects.find_one({"user_id": user_id, "is_default": True})
    if existing is not None:
        return existing

    project = projects.add_project(user_id, DEFAULT_PROJECT_NAME, db=db)
    db.projects.update_one({"_id": project["_id"]}, {"$set": {"is_default": True}})
    project["is_default"] = True
    return project


def _issue_default_key(user_id: str, *, db: Database) -> dict:
    """Rotate the user's default link key: drop the old one, mint and reveal a new.

    Returns the fresh key doc, which carries the one-time plaintext ``key``. Any
    link the user distributed earlier stops working - the price of never storing
    the secret and being able to show it again.
    """
    api_keys.delete_user_keys(user_id, label="default", db=db)
    return api_keys.add_api_key(user_id, label="default", db=db)


def provision_default_workspace(user_id: str, *, db: Database) -> dict:
    """Provision the user's default workspace and (re)issue their link key.

    Returns the default ``project`` and its collection ``record`` (both
    idempotent; the backing Qdrant collection appears on first store), plus a
    freshly minted ``api_key`` whose plaintext is revealed exactly once here.
    """
    project = ensure_default_project(user_id, db=db)
    record = collections.register_collection(user_id, project["_id"], db=db)
    key = _issue_default_key(user_id, db=db)
    return {"project": project, "collection": record, "api_key": key}
