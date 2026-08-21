"""Owner admin (services layer): read-only access to any user's personal area.

The product owner sometimes needs to see what a user sees - which plan they are
on, how much they have used, how many memories sit in which project, whether
their memory link was ever used. That is exactly the ``/account`` snapshot
(``app.services.account.overview``), so this module does not invent a second
view of the data: it resolves *which* user to look at and reuses that snapshot.

Access is a single shared secret, ``ADMIN_SECRET``, held only in the server
environment. **Unset means the whole admin area does not exist** (the routes
404) - a deployment that never sets it cannot be probed for one. The secret is
read from the environment on every call rather than at import so tests and a
restarted process always see the current value.

What is deliberately *not* here: any way to **mutate** a user's data, and any way
to read an API key's secret (there is nothing to read - keys are stored hashed).

Memory *text* is readable, via :func:`user_memories`. That is a deliberate
widening of the earlier "shape, not contents" rule: supporting a user who says
"it didn't save anything" is impossible without seeing what their store actually
holds. It stays read-only, it is reachable only with ``ADMIN_SECRET``, and it is
scoped to one (user, project) collection per request - never a cross-user dump.

Pure I/O - ``db`` is injected so this is testable without live backends.
"""
from __future__ import annotations

import os
import re
import secrets
import time

from pymongo.database import Database
from qdrant_client import QdrantClient

from app.services import account, api_keys, billing, collections, projects, qdrant_store
from app.services.mongo import get_db
from app.services.vector_semantics import SEMANTICS_KEY

# How long an unlocked admin session stays unlocked, in seconds. Short by
# design: this is a standing key to every account, so an unattended browser
# re-locks on its own.
SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_HOURS", "12")) * 3600

# Cap on one page of the user list - the owner searches rather than scrolls.
LIST_LIMIT = 200


def secret() -> str | None:
    """The configured admin secret, or None when the feature is switched off."""
    return os.getenv("ADMIN_SECRET") or None


def is_enabled() -> bool:
    """True when ``ADMIN_SECRET`` is set, i.e. the admin area exists at all."""
    return secret() is not None


def verify(candidate: str | None) -> bool:
    """Constant-time check of a submitted secret against ``ADMIN_SECRET``.

    ``compare_digest`` so a wrong guess leaks no timing signal about how much of
    the secret was right. Always False when the feature is off, so a missing
    config can never be authenticated past.
    """
    configured = secret()
    if configured is None or not candidate:
        return False
    return secrets.compare_digest(candidate, configured)


def session_expired(unlocked_at: float | None) -> bool:
    """True when an admin session opened at ``unlocked_at`` has aged out.

    ``unlocked_at`` is epoch seconds: it round-trips through the signed *session
    cookie*, which is JSON, so it cannot be a datetime.
    """
    if not unlocked_at:
        return True
    return (time.time() - float(unlocked_at)) > SESSION_TTL_SECONDS


def list_users(*, query: str | None = None, limit: int = 50, db: Database | None = None) -> list[dict]:
    """Users, newest first, each with the size of their workspace.

    ``query`` matches an email or name substring (case-insensitive) or an exact
    user id, so the owner can jump straight to the account a support mail came
    from. Rows carry ``memories`` / ``projects_with_data`` totals summed from the
    collection registry in one pass rather than per user.
    """
    db = db if db is not None else get_db()

    criteria: dict = {}
    if query and query.strip():
        term = query.strip()
        pattern = re.escape(term)
        criteria = {
            "$or": [
                {"_id": term},
                {"email": {"$regex": pattern, "$options": "i"}},
                {"name": {"$regex": pattern, "$options": "i"}},
            ]
        }

    users = list(
        db.users.find(criteria).sort("created_at", -1).limit(min(limit, LIST_LIMIT))
    )

    # One aggregation for everyone's stored totals, then joined in memory - a
    # per-user query here would be one round trip per row.
    totals = {
        row["_id"]: row
        for row in db.collections.aggregate(
            [
                {
                    "$group": {
                        "_id": "$user_id",
                        "memories": {"$sum": "$points_count"},
                        "projects_with_data": {"$sum": 1},
                    }
                }
            ]
        )
    }

    rows = []
    for user in users:
        total = totals.get(user["_id"], {})
        rows.append(
            {
                "id": user["_id"],
                "email": user.get("email"),
                "name": user.get("name"),
                # What they are entitled to now, not what they once bought.
                "tier": billing.effective_tier(user),
                "tier_expires_at": billing.tier_expiry(user),
                "created_at": user.get("created_at"),
                "memories": total.get("memories", 0),
                "projects_with_data": total.get("projects_with_data", 0),
            }
        )
    return rows


def list_payments(*, limit: int = 100, db: Database | None = None) -> list[dict]:
    """Every payment record, newest first, joined to the payer's email.

    A row is created the moment a checkout invoice is minted, so ``created`` here
    means "started paying", not "paid" - only ``success`` grants a tier. The
    ``settled`` flag says whether the invoice reached a final state at all; a row
    that is still in flight long after ``created_at`` is one the reconciliation
    sweep has yet to settle (see ``app.services.billing.reconcile``).
    """
    db = db if db is not None else get_db()
    payments = list(db.payments.find().sort("created_at", -1).limit(limit))

    emails = {
        user["_id"]: user.get("email")
        for user in db.users.find(
            {"_id": {"$in": [p.get("user_id") for p in payments]}}, {"email": 1}
        )
    }
    return [
        {
            "invoice_id": payment["_id"],
            "user_id": payment.get("user_id"),
            "email": emails.get(payment.get("user_id")),
            "plan": payment.get("plan"),
            "tier": payment.get("tier"),
            "amount": payment.get("amount", 0) / 100,
            "currency": payment.get("ccy"),
            "status": payment.get("status"),
            "paid": payment.get("status") == billing.PAID_STATUS,
            "settled": payment.get("status") in billing.TERMINAL_STATUSES,
            "created_at": payment.get("created_at"),
            "paid_at": payment.get("paid_at"),
        }
        for payment in payments
    ]


def user_count(*, db: Database | None = None) -> int:
    """Total registered users (the list is capped, this is not)."""
    db = db if db is not None else get_db()
    return db.users.count_documents({})


def user_space(user_id: str, *, db: Database | None = None) -> dict | None:
    """One user's personal area as they would see it, plus identity fields.

    Returns None for an unknown id. ``summary`` is the very same structure the
    user's own ``/account`` page renders (masked keys included), so the owner and
    the user are looking at one shared source of truth.
    """
    db = db if db is not None else get_db()
    user = db.users.find_one({"_id": user_id})
    if user is None:
        return None

    keys = api_keys.list_api_keys(user_id, db=db)
    used = [key["last_used_at"] for key in keys if key.get("last_used_at")]
    return {
        "user": {
            "id": user["_id"],
            "email": user.get("email"),
            "name": user.get("name"),
            "tier": billing.effective_tier(user),
            "tier_expires_at": billing.tier_expiry(user),
            "created_at": user.get("created_at"),
            "updated_at": user.get("updated_at"),
            "has_google_identity": bool(user.get("google_sub")),
        },
        "summary": account.overview(user, db=db),
        "grants": billing.list_grants(user_id, db=db),
        "last_link_use": max(used) if used else None,
    }


# One page of the owner's memory viewer. Fifty is about a screen of scanning
# without turning the page into a wall of text.
MEMORY_PAGE_SIZE = 50

# Ceiling on how many points one request pulls out of Qdrant. Scroll has no
# sort, so ordering by recency means reading the collection and sorting here;
# this bounds that work for a store far larger than anyone will page through.
# `scanned` / `truncated` in the result say when the cap actually bit.
MEMORY_SCAN_LIMIT = 2000


def _memory_row(point) -> dict:
    """One stored point as the viewer shows it.

    Splits the payload three ways: the memory ``text``, the reserved
    ``_semantics`` namespace (deixis anchors written at store time, plus whatever
    the client later declared), and everything else - metadata the client sent
    with the memory.
    """
    payload = dict(point.payload or {})
    semantics = payload.pop(SEMANTICS_KEY, None) or {}
    text = payload.pop("text", "")
    return {
        "id": str(point.id),
        "text": text,
        "stored_at": semantics.get("stored_at"),
        # `owner` is this user by construction; showing it back adds nothing.
        "semantics": {k: v for k, v in semantics.items() if k not in ("owner", "stored_at")},
        "metadata": payload,
    }


def user_memories(
    user_id: str,
    project_id: str,
    *,
    offset: int = 0,
    limit: int = MEMORY_PAGE_SIZE,
    db: Database | None = None,
    qdrant: QdrantClient | None = None,
) -> dict | None:
    """One project's stored memories, newest first - read-only.

    Returns None when the user is unknown, the project is unknown, or the project
    belongs to somebody else: the viewer is addressed by two ids from the URL, so
    it must confirm they belong together rather than trusting the pair. A
    (user, project) maps to exactly one collection (``rs_{user}_{project}``), so
    there is no way to widen this to a second user's data by editing the path.

    A never-written project has no collection yet; that reads as empty, not as an
    error - it is the normal state of a project nobody has stored into.
    """
    db = db if db is not None else get_db()
    user = db.users.find_one({"_id": user_id})
    if user is None:
        return None

    project = projects.get_project(project_id, db=db)
    if project is None or project.get("user_id") != user_id:
        return None

    name = collections.collection_name(user_id, project_id)
    points = qdrant_store.scroll_points(
        name, with_vectors=False, limit=MEMORY_SCAN_LIMIT, client=qdrant
    )
    # Qdrant scrolls in id order, which is arbitrary here (ids are uuid4). Sort
    # by when it was stored, newest first; a memory written before the
    # `_semantics` anchors existed has no timestamp and sorts last.
    rows = sorted(
        (_memory_row(point) for point in points),
        key=lambda row: row["stored_at"] or "",
        reverse=True,
    )

    offset = max(0, offset)
    registered = collections.get_collection(user_id, project_id, db=db) or {}
    return {
        "user": {"id": user_id, "email": user.get("email"), "name": user.get("name")},
        "project": {
            "id": project_id,
            "name": project.get("name"),
            "is_default": bool(project.get("is_default")),
        },
        "collection": name,
        # What the registry believes the collection holds, which is also the
        # number the user sees on their own account page.
        "total": registered.get("points_count", len(rows)),
        "scanned": len(rows),
        "truncated": len(rows) >= MEMORY_SCAN_LIMIT,
        "rows": rows[offset : offset + limit],
        "offset": offset,
        "limit": limit,
        "has_prev": offset > 0,
        "has_next": offset + limit < len(rows),
    }
