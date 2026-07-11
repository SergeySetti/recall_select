"""API key endpoints (user-bounded, ownership-gated).

Keys are the whole agent credential, so every route requires the caller to be
signed in and to own the addressed account/key (see `app.api.deps`).
"""
from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AccountOwner, DbDep, OwnedApiKey
from app.api.schemas import ApiKeyCreate, ApiKeyOut, ApiKeySecretOut
from app.services import api_keys

router = APIRouter(prefix="/api", tags=["api-keys"])


@router.post(
    "/users/{user_id}/api-keys",
    response_model=ApiKeySecretOut,
    status_code=status.HTTP_201_CREATED,
)
def create_api_key(user_id: str, payload: ApiKeyCreate, owner: AccountOwner, db: DbDep) -> ApiKeySecretOut:
    # The only response that carries the plaintext token - shown once, never again.
    return ApiKeySecretOut.model_validate(api_keys.add_api_key(user_id, label=payload.label, db=db))


@router.get("/users/{user_id}/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(user_id: str, owner: AccountOwner, db: DbDep) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in api_keys.list_api_keys(user_id, db=db)]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(key: OwnedApiKey, db: DbDep) -> None:
    # OwnedApiKey 404s unless the signed-in caller owns this key.
    api_keys.delete_api_key(key["_id"], db=db)
