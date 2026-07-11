"""Collection endpoints.

A collection is the per-(user, project) memory store, one-to-one with a Qdrant
collection. Registering creates only the Mongo registry record (stats/limits);
the backing Qdrant collection is created lazily on the first memory store. The
Qdrant name is derived from the ids via the internal naming standard.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DbDep, QdrantDep, require_account_owner
from app.api.schemas import CollectionOut
from app.services import collection_provisioning, collections, projects

# Every route here is `{user_id}`-scoped, so require ownership for the whole
# router: the signed-in caller must be that user.
router = APIRouter(
    prefix="/api/users/{user_id}/projects/{project_id}/collection",
    tags=["collections"],
    dependencies=[Depends(require_account_owner)],
)


def _require_project(user_id: str, project_id: str, db) -> None:
    project = projects.get_project(project_id, db=db)
    if project is None or project["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found for this user")


@router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
def register_collection(user_id: str, project_id: str, db: DbDep) -> CollectionOut:
    _require_project(user_id, project_id, db)
    # Registry row only - the backing Qdrant collection appears on first store
    # (see app.services.collection_provisioning, called from memory.store_memory).
    record = collections.register_collection(user_id, project_id, db=db)
    return CollectionOut.model_validate(record)


@router.get("", response_model=CollectionOut)
def read_collection(user_id: str, project_id: str, db: DbDep) -> CollectionOut:
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
    return CollectionOut.model_validate(record)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(user_id: str, project_id: str, db: DbDep, qdrant: QdrantDep) -> None:
    # Tears down both stores; False means there was nothing to delete.
    if not collection_provisioning.destroy_collection(user_id, project_id, db=db, qdrant=qdrant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
