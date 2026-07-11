"""API key CRUD (services layer), scoped to a user.

Keys authenticate agents against recall.select. Each key belongs to exactly one
user. We generate an opaque token with an ``rs_`` prefix so keys are recognisable
in logs/configs.

**The token is never stored.** At rest we keep only its SHA-256 hash
(``key_hash``) plus a short, non-secret ``key_prefix`` for display. Lookups hash
the presented token and match on ``key_hash``; the plaintext is returned exactly
once, from ``add_api_key``, for the caller to show the user. A database dump
therefore leaks no usable credentials, and a lost key can only be replaced, never
recovered (see the "generate my memory link" rotation in ``workspaces``).

A fast digest (not a slow password hash) is deliberate: the token carries 128
bits of entropy, so it isn't brute-forceable, and a deterministic hash is what
lets us look it up in O(1) without storing the secret.
"""
from __future__ import annotations

import hashlib
import secrets
from uuid import uuid4

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.services.mongo import get_db, utcnow

KEY_PREFIX = "rs_"

# Chars of the token kept in the clear for display ("rs_" + 4 random chars). Long
# enough to tell keys apart in a list, far too short to be used as a credential.
_PREVIEW_LEN = len(KEY_PREFIX) + 4

# Fresh tokens to try when an insert hits the unique index on ``key_hash``. A
# collision is astronomically rare at the current token length, but when it
# happens it must cost a retry - never two users sharing one credential.
_MAX_KEY_ATTEMPTS = 3


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(16)


def hash_key(key: str) -> str:
    """The at-rest digest of a token (also used to look a presented key up)."""
    return hashlib.sha256(key.encode()).hexdigest()


def add_api_key(user_id: str, *, label: str | None = None, db: Database | None = None) -> dict:
    """Mint a new API key for ``user_id``; return the stored doc + the plaintext.

    The returned dict carries a transient ``key`` (the only time the plaintext is
    ever available) alongside the persisted fields. The unique index on
    ``key_hash`` (see ``mongo.ensure_indexes``) turns a token collision into an
    insert error instead of a silently shared credential; on one, retry.
    """
    db = db if db is not None else get_db()
    for _ in range(_MAX_KEY_ATTEMPTS):
        token = generate_key()
        doc = {
            "_id": uuid4().hex,
            "user_id": user_id,
            "key_hash": hash_key(token),
            "key_prefix": token[:_PREVIEW_LEN],
            "label": label,
            "created_at": utcnow(),
        }
        try:
            db.api_keys.insert_one(doc)
        except DuplicateKeyError as err:
            # A duplicate on key_hash is a genuine (astronomically rare) token
            # collision: a fresh token resolves it, so retry. A duplicate on ANY
            # other unique index cannot be fixed by re-minting the token - it
            # means the collection carries an index we don't expect (e.g. a
            # legacy unique index on user_id from a one-key-per-user schema).
            # Blindly retrying there just burns attempts and reports the
            # misleading "couldn't generate a unique key". Tell the two apart by
            # asking which value actually clashed (backend-agnostic: the mongomock
            # used in tests exposes no index details on the error).
            if db.api_keys.find_one({"key_hash": doc["key_hash"]}) is not None:
                continue
            raise RuntimeError(
                "api_keys insert rejected by a unique index other than "
                f"key_hash (server said: {getattr(err, 'details', None) or err}). "
                "A new token cannot resolve this - inspect the api_keys "
                "collection for an unexpected unique index, e.g. a legacy one "
                "on user_id."
            ) from err
        # Reveal the plaintext once, to this caller only - never persisted.
        return {**doc, "key": token}
    raise RuntimeError(
        f"could not generate a unique API key in {_MAX_KEY_ATTEMPTS} attempts"
    )


def get_api_key(key_id: str, *, db: Database | None = None) -> dict | None:
    """Look up a key document by its id (used for ownership checks)."""
    db = db if db is not None else get_db()
    return db.api_keys.find_one({"_id": key_id})


def delete_api_key(key_id: str, *, db: Database | None = None) -> bool:
    """Delete a key by its id. Returns True if a key was removed."""
    db = db if db is not None else get_db()
    return db.api_keys.delete_one({"_id": key_id}).deleted_count > 0


def delete_user_keys(user_id: str, *, label: str | None = None, db: Database | None = None) -> int:
    """Delete a user's keys (optionally only those with ``label``); return count.

    Used to rotate the default "memory link" key - the old one is invalidated
    before a fresh one is issued.
    """
    db = db if db is not None else get_db()
    query = {"user_id": user_id}
    if label is not None:
        query["label"] = label
    return db.api_keys.delete_many(query).deleted_count


def list_api_keys(user_id: str, *, db: Database | None = None) -> list[dict]:
    """All keys belonging to a user (newest first). No plaintext - hashes only."""
    db = db if db is not None else get_db()
    return list(db.api_keys.find({"user_id": user_id}).sort("created_at", -1))


def get_by_key(key: str, *, db: Database | None = None) -> dict | None:
    """Resolve a presented token to its stored document (authenticates requests)."""
    db = db if db is not None else get_db()
    return db.api_keys.find_one({"key_hash": hash_key(key)})
