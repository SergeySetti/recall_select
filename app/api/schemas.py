"""Pydantic request/response models for the API.

Mongo documents use ``_id``; the ``*Out`` models expose it as ``id`` so the JSON
surface is clean. ``model_validate(doc)`` reads each Mongo dict directly.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.services.api_keys import masked as _masked_key


class _Out(BaseModel):
    # Read Mongo's `_id` on input, but serialize it as `id` in responses.
    id: str = Field(validation_alias="_id")


# --- Users -----------------------------------------------------------------

class UserCreate(BaseModel):
    email: str
    name: str | None = None
    tier: str = "free"


class UserUpdate(BaseModel):
    email: str | None = None
    name: str | None = None
    tier: str | None = None


class UserOut(_Out):
    email: str
    name: str | None = None
    tier: str
    created_at: datetime
    updated_at: datetime


class MeOut(_Out):
    # The signed-in user's own view. `email` may be absent for seeded users.
    email: str | None = None
    name: str | None = None
    tier: str


class MemoryLinkOut(BaseModel):
    link: str
    api_key: str
    project_id: str
    collection: str


class MemoryLinkStatusOut(BaseModel):
    # Non-secret snapshot of the user's memory link: does one exist, and how does
    # it read masked. Safe to return any number of times (unlike the plaintext
    # link, which only key (re)generation reveals).
    exists: bool
    masked_link: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None


# --- API keys --------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    label: str | None = None


class ApiKeyOut(_Out):
    # Listing/reading a key never exposes the secret - only the display hints
    # (prefix + last 4), joined into `key_masked` for a "rs_ab12…wxyz" render.
    user_id: str
    key_prefix: str
    key_last4: str | None = Field(default=None, exclude=True)
    label: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key_masked(self) -> str:
        return _masked_key({"key_prefix": self.key_prefix, "key_last4": self.key_last4})


class ApiKeySecretOut(ApiKeyOut):
    # Returned only from key creation: the plaintext token, shown exactly once.
    key: str


# --- Projects --------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str | None = None


class ProjectOut(_Out):
    user_id: str
    name: str
    created_at: datetime
    updated_at: datetime


# --- Collections -----------------------------------------------------------

class CollectionOut(_Out):
    name: str
    user_id: str
    project_id: str
    points_count: int
    calls_count: int
    created_at: datetime
    updated_at: datetime


# --- Memory ----------------------------------------------------------------

class MemoryStoreIn(BaseModel):
    text: str
    metadata: dict | None = None


class MemoryStoreOut(BaseModel):
    id: str
    collection: str
    points_count: int


class MemorySearchIn(BaseModel):
    query: str
    limit: int = 5


class MemoryHit(BaseModel):
    id: str
    score: float
    payload: dict | None = None


# --- Payments --------------------------------------------------------------

class CheckoutIn(BaseModel):
    plan: str


class CheckoutOut(BaseModel):
    # The Monobank hosted page the caller redirects the browser to.
    pay_url: str
    invoice_id: str
    plan: str
    tier: str
