"""Billing (services layer): plans, payment records, and the tier flip.

A paid plan buys a **tier** on the user document (`app.services.users`). ``tier``
is the single knob the limits elsewhere read, and it is billing-owned - the
self-service user PATCH deliberately drops it (see `app.api.users`), so the only
way it changes is a *verified* Monobank webhook landing here.

Flow:
  1. ``record_pending`` writes a payment keyed by Monobank's ``invoiceId`` the
     moment we create the invoice (so the webhook, which only knows the invoice
     id, can map back to the user + plan).
  2. ``apply_webhook`` moves that record to its terminal status. On ``success``
     it flips the user's tier - exactly once, idempotently, so Monobank's retries
     (and any duplicate deliveries) can't double-apply or race.

Pure I/O - ``db`` is injected so this is testable without live backends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from pymongo import ReturnDocument
from pymongo.database import Database

from app.services import monobank, users
from app.services.mongo import get_db, utcnow

# The one webhook status that grants entitlement. Monobank also emits
# created/processing/hold/failure/reversed/expired; only this one pays out.
PAID_STATUS = "success"

# Statuses an invoice never moves on from. Anything else (created, processing,
# hold) is still in flight and worth re-checking - see ``reconcile``.
TERMINAL_STATUSES = frozenset({PAID_STATUS, "failure", "reversed", "expired"})

# How stale an in-flight payment must be before reconciliation pulls its status.
# Long enough that the ordinary webhook has had every chance to arrive first.
RECONCILE_AFTER_SECONDS = 300

# Every price in PLANS is *per month*, so a purchase buys this many days of the
# tier. Nothing renews automatically yet: when the grant runs out the account
# falls back to free and the user buys again.
SUBSCRIPTION_DAYS = 30


# The baseline every account starts on; shown (unpurchasable) on the plans page.
FREE_TIER = "free"
FREE_CALLS = 3_000
FREE_PROJECTS = 1

# Sentinel for an unmetered allowance (the Unlim tier). Rendered as "Unlimited".
UNLIMITED = None
_UNLIMITED_LABEL = "Unlimited"

_CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "UAH": "₴"}


def _allowance(value: int | None) -> str:
    """Format a monthly allowance for the table: a grouped number, or 'Unlimited'."""
    return _UNLIMITED_LABEL if value is None else f"{value:,}"


@dataclass(frozen=True)
class Plan:
    """A purchasable tier: its monthly allowances, its price, and its display name.

    ``calls`` / ``projects`` are the monthly allowances the limit checks read off
    the granted ``tier`` (see the spec's "Calculations v2" table); ``None`` means
    unmetered. Prices are per month; a purchase is charged as a single Monobank
    invoice for now.
    """

    id: str
    name: str  # display name for the plans table ("Paid 2x")
    tier: str  # the tier string the limit checks consume ("paid_2x")
    calls: int | None  # monthly call allowance; None = unlimited
    projects: int | None  # concurrent project allowance; None = unlimited
    amount_minor: int  # smallest currency unit (cents), per Monobank's `amount`
    currency: str

    @property
    def price(self) -> str:
        """Human price, e.g. ``$17`` (whole) or ``$17.50`` (with cents)."""
        symbol = _CURRENCY_SYMBOL.get(self.currency, "")
        dollars = self.amount_minor / 100
        formatted = f"{dollars:,.0f}" if self.amount_minor % 100 == 0 else f"{dollars:,.2f}"
        return f"{symbol}{formatted}"

    @property
    def checkout_label(self) -> str:
        """The `destination` shown on the Monobank checkout page + bank statement."""
        return f"recall.select {self.name}"


# Plan catalogue - the paid tiers from "Calculations v2" in
# docs/specs/initial_specification.md. Keyed by the `plan` id the checkout
# endpoint takes; the `tier` string is what the limit checks consume.
PLANS: dict[str, Plan] = {
    "2x": Plan("2x", "Paid 2x", "paid_2x", 6_000, 2, 1_700, "USD"),
    "5x": Plan("5x", "Paid 5x", "paid_5x", 30_000, 20, 6_700, "USD"),
    "unlim": Plan("unlim", "Unlim", "unlim", UNLIMITED, UNLIMITED, 22_700, "USD"),
}


def get_plan(plan_id: str) -> Plan | None:
    return PLANS.get(plan_id)


# --- Tier grants and their expiry ------------------------------------------
# A paid tier is time-limited: ``tier`` says *what* was bought and
# ``tier_expires_at`` says *until when*. Both live on the user document, and
# ``effective_tier`` is the only honest way to read them - a stored ``paid_2x``
# whose date has passed is a free account. ``None`` expiry means open-ended (a
# permanent gift or a legacy row from before this existed).


def _as_aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC.

    pymongo hands back **naive** datetimes by default while everything we write
    is aware, and comparing the two raises. Anything read out of Mongo has to
    come through here before it meets ``utcnow()``.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def tier_expiry(user: dict | None) -> datetime | None:
    """When this user's paid tier runs out (aware UTC), or None if it doesn't."""
    if not user:
        return None
    return _as_aware(user.get("tier_expires_at"))


def effective_tier(user: dict | None) -> str:
    """The tier actually in force right now - free once a grant has run out.

    Read this rather than ``user["tier"]`` anywhere entitlement is decided; the
    stored field is the *purchase*, this is the *entitlement*.
    """
    if not user:
        return FREE_TIER
    tier = user.get("tier", FREE_TIER)
    if tier == FREE_TIER:
        return FREE_TIER
    expires = _as_aware(user.get("tier_expires_at"))
    if expires is None:
        return tier
    return tier if expires > utcnow() else FREE_TIER


def grant_tier(
    user_id: str,
    tier: str,
    *,
    days: int | None = SUBSCRIPTION_DAYS,
    reason: str,
    source: str,
    db: Database | None = None,
) -> dict | None:
    """Give a user a tier for ``days`` (``None`` = open-ended) and log why.

    The single place a tier is ever granted - by a paid invoice
    (``apply_webhook``) or by hand as goodwill - so every change to entitlement
    leaves a row in ``tier_grants`` saying who, what, how long, and on what
    grounds. Buying the same tier again *extends* an unexpired grant instead of
    truncating it to a fresh window, so paying early never costs the buyer days.

    Returns the updated user document, or None if there is no such user.
    """
    db = db if db is not None else get_db()
    user = users.get_user(user_id, db=db)
    if user is None:
        return None

    now = utcnow()
    current = _as_aware(user.get("tier_expires_at"))
    extending = user.get("tier") == tier and current is not None and current > now
    expires = None if days is None else (current if extending else now) + timedelta(days=days)

    # Granting clears any "your plan ended" marker - they are current again.
    updated = users.update_user(
        user_id,
        db=db,
        tier=tier,
        tier_expires_at=expires,
        lapsed_tier=None,
        tier_lapsed_at=None,
    )
    db.tier_grants.insert_one(
        {
            "user_id": user_id,
            "tier": tier,
            "days": days,
            "expires_at": expires,
            "previous_tier": user.get("tier", FREE_TIER),
            "reason": reason,
            "source": source,
            "granted_at": now,
        }
    )
    return updated


def list_grants(user_id: str, *, db: Database | None = None) -> list[dict]:
    """Every tier grant this user has received, newest first."""
    db = db if db is not None else get_db()
    return list(db.tier_grants.find({"user_id": user_id}).sort("granted_at", -1))


def downgrade_expired(*, db: Database | None = None) -> int:
    """Drop everyone whose paid window has closed back to free. Returns the count.

    ``effective_tier`` already refuses to honour an elapsed grant, so this is
    hygiene rather than enforcement: it keeps the stored field truthful, which is
    what the account page, the admin list, and any future export read.
    """
    db = db if db is not None else get_db()
    expired = list(
        db.users.find(
            {
                "tier": {"$ne": FREE_TIER},
                "tier_expires_at": {"$ne": None, "$lte": utcnow()},
            },
            # `tier` too: it becomes the lapse marker the renewal prompt reads.
            {"_id": 1, "tier": 1},
        )
    )
    for user in expired:
        # Remember what lapsed and when: once ``tier`` is back to free the page
        # would otherwise have no way to say "your plan ended, renew it".
        users.update_user(
            user["_id"],
            db=db,
            tier=FREE_TIER,
            tier_expires_at=None,
            lapsed_tier=user["tier"],
            tier_lapsed_at=utcnow(),
        )
    return len(expired)


# --- Renewal prompts -------------------------------------------------------
# Nothing renews automatically, so the app has to *ask*. These two windows
# decide when: warn while the plan is still running but nearly out, and keep
# offering the renewal for a while after it lapses.

RENEWAL_WARNING_DAYS = 7
LAPSED_PROMPT_DAYS = 30


def renewal_state(user: dict | None) -> dict:
    """What (if anything) the account page should say about renewing.

    ``status`` is one of:

    * ``none`` - free account, or a paid grant with no expiry. Say nothing.
    * ``active`` - paid and comfortably in credit. Show the date, don't nag.
    * ``expiring`` - paid, ``RENEWAL_WARNING_DAYS`` or fewer left.
    * ``lapsed`` - ran out within the last ``LAPSED_PROMPT_DAYS``.

    ``plan_id`` is what the checkout endpoint takes, so the prompt can offer a
    one-click renewal of the plan they actually had.
    """
    quiet = {"status": "none", "plan_id": None, "plan_name": None,
             "expires_at": None, "days_left": None}
    if not user:
        return quiet

    now = utcnow()
    tier = effective_tier(user)

    if tier != FREE_TIER:
        expires = _as_aware(user.get("tier_expires_at"))
        if expires is None:
            return quiet  # open-ended grant: nothing to renew
        plan = _PLANS_BY_TIER.get(tier)
        # Round up: with 20 hours left a user should read "1 day", not "0".
        remaining = expires - now
        days_left = remaining.days + (1 if remaining.seconds else 0)
        return {
            "status": "expiring" if days_left <= RENEWAL_WARNING_DAYS else "active",
            "plan_id": plan.id if plan else None,
            "plan_name": tier_name(tier),
            "expires_at": expires,
            "days_left": days_left,
        }

    lapsed_at = _as_aware(user.get("tier_lapsed_at"))
    lapsed_tier = user.get("lapsed_tier")
    if lapsed_at and lapsed_tier and (now - lapsed_at) <= timedelta(days=LAPSED_PROMPT_DAYS):
        plan = _PLANS_BY_TIER.get(lapsed_tier)
        return {
            "status": "lapsed",
            "plan_id": plan.id if plan else None,
            "plan_name": tier_name(lapsed_tier),
            "expires_at": lapsed_at,
            "days_left": 0,
        }

    return quiet


# Reverse lookup from the granted ``tier`` string to its Plan. ``PLANS`` is keyed
# by checkout ``plan`` id ("2x"); the limit checks only know the ``tier``
# ("paid_2x"). Free is not a purchasable Plan, so the allowance helpers below
# special-case it.
_PLANS_BY_TIER: dict[str, Plan] = {plan.tier: plan for plan in PLANS.values()}


def tier_name(tier: str) -> str:
    """Human display name for a granted ``tier`` (e.g. ``paid_2x`` -> "Paid 2x").

    An unknown/legacy tier reads as "Free", matching the allowance fallbacks below.
    """
    if tier == FREE_TIER:
        return "Free"
    plan = _PLANS_BY_TIER.get(tier)
    return plan.name if plan is not None else "Free"


def call_allowance(tier: str) -> int | None:
    """Monthly call allowance for a granted ``tier``; ``None`` means unlimited.

    An unknown tier falls back to the free baseline - a mis-set or legacy tier
    must never hand out more than the free budget.
    """
    if tier == FREE_TIER:
        return FREE_CALLS
    plan = _PLANS_BY_TIER.get(tier)
    return plan.calls if plan is not None else FREE_CALLS


def project_allowance(tier: str) -> int | None:
    """Concurrent-project allowance for a granted ``tier``; ``None`` = unlimited."""
    if tier == FREE_TIER:
        return FREE_PROJECTS
    plan = _PLANS_BY_TIER.get(tier)
    return plan.projects if plan is not None else FREE_PROJECTS


def plans_table() -> list[dict]:
    """Ordered rows for the plans page: the free baseline, then every paid tier.

    Free is a display-only row (no ``id`` -> not purchasable); each paid plan
    carries the ``id`` the checkout endpoint expects. Keeping this here makes the
    catalogue - allowances, prices, tiers - a single source of truth for the page.
    """
    rows: list[dict] = [
        {
            "id": None,
            "name": "Free",
            "tier": FREE_TIER,
            "calls": _allowance(FREE_CALLS),
            "projects": _allowance(FREE_PROJECTS),
            "price": "$0",
            "purchasable": False,
        }
    ]
    for plan in PLANS.values():
        rows.append(
            {
                "id": plan.id,
                "name": plan.name,
                "tier": plan.tier,
                "calls": _allowance(plan.calls),
                "projects": _allowance(plan.projects),
                "price": plan.price,
                "purchasable": True,
            }
        )
    return rows


def record_pending(
    invoice_id: str,
    user_id: str,
    plan: Plan,
    *,
    db: Database | None = None,
) -> dict:
    """Persist a freshly created invoice as a pending payment, keyed by invoice id.

    The invoice id is the document ``_id``: it's Monobank's own unique handle and
    the only thing the webhook carries, so keying on it gives us both the reverse
    lookup and natural insert-once semantics.
    """
    db = db if db is not None else get_db()
    now = utcnow()
    doc = {
        "_id": invoice_id,
        "user_id": user_id,
        "plan": plan.id,
        "tier": plan.tier,
        "amount": plan.amount_minor,
        "ccy": plan.currency,
        "status": "created",
        "paid_at": None,
        "created_at": now,
        "updated_at": now,
    }
    db.payments.insert_one(doc)
    return doc


def get_payment(invoice_id: str, *, db: Database | None = None) -> dict | None:
    db = db if db is not None else get_db()
    return db.payments.find_one({"_id": invoice_id})


def apply_webhook(
    invoice_id: str,
    status: str,
    *,
    db: Database | None = None,
) -> dict | None:
    """Record a webhook status transition; grant the tier once on success.

    Returns the updated payment doc, or ``None`` for an unknown invoice (a webhook
    for something we never created - ignored, not an error). Idempotent on
    ``success``: the tier is flipped only on the transition *into* success, guarded
    by a conditional update so concurrent/duplicate deliveries can't double-apply.
    """
    db = db if db is not None else get_db()
    payment = db.payments.find_one({"_id": invoice_id})
    if payment is None:
        return None

    if status == PAID_STATUS:
        # Only the first delivery that flips status->success wins the update; any
        # later duplicate matches nothing here and skips the tier grant.
        updated = db.payments.find_one_and_update(
            {"_id": invoice_id, "status": {"$ne": PAID_STATUS}},
            {"$set": {"status": PAID_STATUS, "paid_at": utcnow(), "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return payment  # already paid - no double-apply
        # A month of the tier, dated from now (or from the end of an unexpired
        # grant, so buying early never burns days).
        grant_tier(
            payment["user_id"],
            payment["tier"],
            days=SUBSCRIPTION_DAYS,
            reason=f"invoice {invoice_id}",
            source="monobank",
            db=db,
        )
        return updated

    # Non-paying status (failure/expired/intermediate): just record it, never
    # touching an already-granted tier.
    return db.payments.find_one_and_update(
        {"_id": invoice_id, "status": {"$ne": PAID_STATUS}},
        {"$set": {"status": status, "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    ) or payment


# --- Reconciliation --------------------------------------------------------
# The webhook is fire-and-forget: Monobank sends a status change once, and a
# callback lost to a restart, a proxy blip, or a signature failure is never
# re-sent. Until this existed that was a silent under-entitlement - the customer
# paid and stayed on their old tier with nothing on our side to notice. So we
# also *pull*: every in-flight payment older than a few minutes gets its real
# status fetched and pushed through the very same ``apply_webhook`` transition,
# which is already idempotent - whichever of push and pull arrives first wins,
# and the other one is a no-op.


def list_unsettled(
    *,
    older_than_seconds: int = RECONCILE_AFTER_SECONDS,
    limit: int = 100,
    db: Database | None = None,
) -> list[dict]:
    """In-flight payments old enough that their webhook should have landed."""
    db = db if db is not None else get_db()
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    return list(
        db.payments.find(
            {
                "status": {"$nin": sorted(TERMINAL_STATUSES)},
                "created_at": {"$lte": cutoff},
            }
        )
        .sort("created_at", 1)
        .limit(limit)
    )


def reconcile(
    *,
    fetch_status: Callable[[str], str | None],
    older_than_seconds: int = RECONCILE_AFTER_SECONDS,
    limit: int = 100,
    db: Database | None = None,
) -> dict:
    """Pull the real status of every in-flight payment and apply what changed.

    ``fetch_status`` maps an invoice id to Monobank's current status string (it
    is injected so this is testable without the network; see
    ``reconcile_with_monobank`` for the wired-up version). A lookup that raises
    is counted and skipped - one unreachable invoice must not stop the rest.

    Returns a small report: how many were ``checked``, how many reached a
    terminal status (``settled``), how many of those were paid and therefore
    granted a tier (``granted``), and how many lookups failed (``errors``).
    """
    db = db if db is not None else get_db()
    report = {"checked": 0, "settled": 0, "granted": 0, "errors": 0}

    for payment in list_unsettled(
        older_than_seconds=older_than_seconds, limit=limit, db=db
    ):
        try:
            current = fetch_status(payment["_id"])
        except Exception:  # noqa: BLE001 - one bad lookup must not stop the sweep.
            report["errors"] += 1
            continue

        report["checked"] += 1
        if not current or current == payment["status"]:
            continue

        updated = apply_webhook(payment["_id"], current, db=db)
        if updated is None:
            continue
        if updated["status"] in TERMINAL_STATUSES:
            report["settled"] += 1
        # The tier flip happened here (not in an earlier delivery) only if this
        # record was still unpaid when the sweep picked it up.
        if current == PAID_STATUS:
            report["granted"] += 1

    return report


def reconcile_with_monobank(
    *,
    older_than_seconds: int = RECONCILE_AFTER_SECONDS,
    limit: int = 100,
    db: Database | None = None,
) -> dict:
    """``reconcile`` wired to the live Monobank API.

    A no-op when payments are unconfigured, so a deployment without a merchant
    token (local dev, tests) can run the sweep harmlessly.
    """
    cfg = monobank.load_config()
    if not cfg.configured:
        return {"checked": 0, "settled": 0, "granted": 0, "errors": 0, "skipped": True}

    def fetch(invoice_id: str) -> str | None:
        return monobank.fetch_invoice_status(cfg, invoice_id).get("status")

    return reconcile(
        fetch_status=fetch, older_than_seconds=older_than_seconds, limit=limit, db=db
    )
