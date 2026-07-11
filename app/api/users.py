"""User endpoints - the signed-in user's own account (read/update).

Both routes are ownership-gated: you can only read or modify your own user.
There is deliberately **no public user-creation endpoint** - accounts are
created by Google sign-in (see `app.auth` / `services.users.get_or_create_google_user`),
never by an anonymous POST.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AccountOwner, DbDep
from app.api.schemas import UserOut, UserUpdate
from app.services import users

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/{user_id}", response_model=UserOut)
def read_user(user: AccountOwner) -> UserOut:
    # AccountOwner already resolved (and authorized) the signed-in user.
    return UserOut.model_validate(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: UserUpdate, owner: AccountOwner, db: DbDep) -> UserOut:
    fields = payload.model_dump(exclude_unset=True)
    # `tier` is owned by the billing flow, never self-service - dropping it here
    # stops a signed-in user upgrading their own plan for free.
    fields.pop("tier", None)
    doc = users.update_user(user_id, db=db, **fields) if fields else owner
    return UserOut.model_validate(doc)
