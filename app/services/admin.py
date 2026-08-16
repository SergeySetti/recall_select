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

What is deliberately *not* here: any way to mutate a user's data, and any way to
read a memory's text or an API key's secret. The owner can see the shape of an
account (counts, plan, timestamps, masked keys) - not its contents. Widening
that is a product decision, not an implementation detail.

Pure I/O - ``db`` is injected so this is testable without live backends.
"""
from __future__ import annotations

import os
import re
import secrets
import time

from pymongo.database import Database

from app.services import account, api_keys, billing
from app.services.mongo import get_db

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
