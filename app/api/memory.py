"""Memory endpoints - store, recall, and delete, per (user, project).

Thin: validate the project, then delegate to `app.services.memory`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DbDep, EmbedDep, QdrantDep, require_account_owner
from app.api.schemas import MemoryHit, MemorySearchIn, MemoryStoreIn, MemoryStoreOut
from app.services import memory, projects

# Every route here is `{user_id}`-scoped: require the caller to be that user, so
# nobody can store into or read another user's memories.
router = APIRouter(
    prefix="/api/users/{user_id}/projects/{project_id}/memories",
    tags=["memory"],
    dependencies=[Depends(require_account_owner)],
)


def _require_project(user_id: str, project_id: str, db) -> None:
    project = projects.get_project(project_id, db=db)
    if project is None or project["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found for this user")


@router.post("", response_model=MemoryStoreOut, status_code=status.HTTP_201_CREATED)
def store_memory(
    user_id: str,
    project_id: str,
    payload: MemoryStoreIn,
    db: DbDep,
    qdrant: QdrantDep,
    embed: EmbedDep,
) -> MemoryStoreOut:
    _require_project(user_id, project_id, db)
    return MemoryStoreOut(**memory.store_memory(
        user_id, project_id, payload.text,
        metadata=payload.metadata, db=db, qdrant=qdrant, embed=embed,
    ))


@router.post("/search", response_model=list[MemoryHit])
def recall_memory(
    user_id: str,
    project_id: str,
    payload: MemorySearchIn,
    db: DbDep,
    qdrant: QdrantDep,
    embed: EmbedDep,
) -> list[MemoryHit]:
    _require_project(user_id, project_id, db)
    return memory.recall_memory(
        user_id, project_id, payload.query,
        limit=payload.limit, db=db, qdrant=qdrant, embed=embed,
    )


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    user_id: str,
    project_id: str,
    memory_id: str,
    db: DbDep,
    qdrant: QdrantDep,
) -> None:
    _require_project(user_id, project_id, db)
    if not memory.delete_memory(user_id, project_id, memory_id, db=db, qdrant=qdrant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
