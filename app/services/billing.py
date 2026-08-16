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
from datetime import timedelta
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
        users.update_user(payment["user_id"], db=db, tier=payment["tier"])
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
