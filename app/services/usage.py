"""Monthly API-usage metering and price-model enforcement (services layer).

The pricing model (``app.services.billing``) grants each tier a monthly call
budget. This module is the meter and the gate for it:

- every accepted ``store``/``recall``/``delete`` counts as one call, tallied into
  a per-(user, period) ``usage`` document, where ``period`` is the calendar month
  (``"YYYY-MM"``, UTC). The budget "resets" for free: each month rolls to a new
  period key, and spent months stay behind as an audit trail.
- ``check_call_allowed`` compares the running tally against the caller's tier
  allowance and raises ``QuotaExceeded`` once the month's budget is spent.

Deliberately kept apart from the per-collection counters in ``collections``
(all-time, per project - "how big/busy is this store"); this answers the
different question "has this user spent their month".

Pure I/O - ``db`` is injected so this is testable without live backends.
"""
from __future__ import annotations

from datetime import datetime

from pymongo import ReturnDocument
from pymongo.database import Database

from app.services import billing, users
from app.services.mongo import get_db, utcnow


class QuotaExceeded(Exception):
    """Raised when a call would exceed the caller's monthly tier allowance.

    Carries the context the caller needs to explain the block (and the API layer
    needs to map it to HTTP 429).
    """

    def __init__(self, user_id: str, tier: str, allowance: int, period: str) -> None:
        self.user_id = user_id
        self.tier = tier
        self.allowance = allowance
        self.period = period
        super().__init__(
            f"Monthly call limit reached: {allowance:,} calls on the '{tier}' "
            f"plan for {period}. Upgrade at /plans, or wait for next month."
        )


def current_period(now: datetime | None = None) -> str:
    """The calendar-month budgeting key, e.g. ``2026-07`` (UTC)."""
    now = now if now is not None else utcnow()
    return now.strftime("%Y-%m")


def calls_this_period(
    user_id: str, *, period: str | None = None, db: Database | None = None
) -> int:
    """How many calls the user has spent in ``period`` (defaults to this month)."""
    db = db if db is not None else get_db()
    period = period or current_period()
    doc = db.usage.find_one({"user_id": user_id, "period": period})
    return doc["calls"] if doc else 0


def record_call(
    user_id: str, *, count: int = 1, period: str | None = None, db: Database | None = None
) -> int:
    """Tally ``count`` calls into the user's current month; return the new total.

    Upserts the (user, period) row, so the first call of a new month starts the
    tally at ``count``. The unique index on (user_id, period) keeps the upsert
    from ever forking into duplicate rows.
    """
    db = db if db is not None else get_db()
    period = period or current_period()
    now = utcnow()
    doc = db.usage.find_one_and_update(
        {"user_id": user_id, "period": period},
        {
            "$inc": {"calls": count},
            "$set": {"updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["calls"]


def check_call_allowed(user_id: str, *, db: Database | None = None) -> None:
    """Raise ``QuotaExceeded`` if the user has spent this month's call budget.

    Reads the tier off the user document and the allowance off ``billing``. An
    unlimited tier (allowance ``None``) never trips; a missing user falls back to
    the free budget so an unknown caller can't outrun the meter.
    """
    db = db if db is not None else get_db()
    user = users.get_user(user_id, db=db)
    tier = user["tier"] if user else billing.FREE_TIER
    allowance = billing.call_allowance(tier)
    if allowance is None:
        return
    period = current_period()
    if calls_this_period(user_id, period=period, db=db) >= allowance:
        raise QuotaExceeded(user_id, tier, allowance, period)
