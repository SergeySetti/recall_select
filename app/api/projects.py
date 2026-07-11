"""Project endpoints (ownership-gated).

`{user_id}`-scoped routes require the caller to be that user; `{project_id}`
routes require the caller to own the project (404 otherwise).
"""
from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AccountOwner, DbDep, OwnedProject
from app.api.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.services import projects

router = APIRouter(prefix="/api", tags=["projects"])


@router.post(
    "/users/{user_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
)
def create_project(user_id: str, payload: ProjectCreate, owner: AccountOwner, db: DbDep) -> ProjectOut:
    return ProjectOut.model_validate(projects.add_project(user_id, payload.name, db=db))


@router.get("/users/{user_id}/projects", response_model=list[ProjectOut])
def list_projects(user_id: str, owner: AccountOwner, db: DbDep) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in projects.list_projects(user_id, db=db)]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def read_project(project: OwnedProject) -> ProjectOut:
    return ProjectOut.model_validate(project)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, project: OwnedProject, db: DbDep) -> ProjectOut:
    fields = payload.model_dump(exclude_unset=True)
    doc = projects.update_project(project_id, db=db, **fields) if fields else project
    return ProjectOut.model_validate(doc)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project: OwnedProject, db: DbDep) -> None:
    projects.delete_project(project["_id"], db=db)
