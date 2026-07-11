"""Payments: Monobank checkout + the verified webhook that grants the tier.

recall.select shares only the Monobank *merchant token* with the sibling
mcp-api.net app (see `app.services.monobank`); every invoice here is created with
recall.select's own redirect/webhook URLs, so the payment round-trip stays inside
this app.

Three surfaces:
- ``POST /api/me/checkout`` - signed-in user starts a purchase; we create the
  invoice, remember it (pending) against their id, and hand back the Monobank
  ``pay_url`` for the browser to follow.
- ``POST /webhooks/monobank`` - Monobank's server-to-server callback. **Signature
  is verified before we trust a word of it** (an unverified webhook is a free
  upgrade for anyone who guesses the URL); only then is the tier granted.
- ``GET /payment/success`` / ``/payment/fail`` - where the shopper's browser
  lands afterwards. Cosmetic: entitlement is driven by the webhook, never these.
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUser, DbDep
from app.api.schemas import CheckoutIn, CheckoutOut
from app.services import billing, monobank

logger = logging.getLogger(__name__)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Verifying the webhook signature is on by default and should stay on in any
# deployment that takes real money. It exists only so local/dev runs (where the
# app can't reach Monobank to fetch the pubkey, and no real money moves) can
# exercise the flow: set MONOBANK_WEBHOOK_VERIFY=0 there, never in prod.
_VERIFY_WEBHOOK = os.getenv("MONOBANK_WEBHOOK_VERIFY", "1").lower() not in {"0", "false", "no"}

router = APIRouter(tags=["payments"])

# The merchant public key rarely changes; fetch it once and reuse.
_pubkey_cache: bytes | None = None


def _get_pubkey(cfg: monobank.MonobankConfig) -> bytes | None:
    global _pubkey_cache
    if _pubkey_cache is None:
        try:
            _pubkey_cache = monobank.fetch_pubkey(cfg)
        except Exception:  # noqa: BLE001 - a missing key must fail closed, not crash.
            logger.exception("Monobank pubkey fetch failed")
            return None
    return _pubkey_cache


@router.post("/api/me/checkout", response_model=CheckoutOut)
def checkout(payload: CheckoutIn, user: CurrentUser, db: DbDep) -> CheckoutOut:
    plan = billing.get_plan(payload.plan)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown plan")

    cfg = monobank.load_config()
    if not cfg.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "payments not configured")

    try:
        invoice = monobank.create_invoice(
            cfg,
            amount_minor=plan.amount_minor,
            currency=plan.currency,
            # Tag the payment so it's identifiable on the shared merchant account;
            # the authoritative user/plan mapping is our own payments record.
            reference=f"recall_select:{user['_id']}:{plan.id}",
            destination=plan.checkout_label,
            redirect_url=cfg.redirect_url or f"{PUBLIC_BASE_URL}/payment/success",
            webhook_url=cfg.webhook_url or f"{PUBLIC_BASE_URL}/webhooks/monobank",
        )
    except Exception as e:  # noqa: BLE001 - surface Monobank/network failure as 502.
        logger.exception("Monobank invoice create failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    invoice_id = invoice.get("invoiceId")
    pay_url = invoice.get("pageUrl")
    if not invoice_id or not pay_url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Monobank response missing invoiceId/pageUrl")

    billing.record_pending(invoice_id, user["_id"], plan, db=db)
    return CheckoutOut(pay_url=pay_url, invoice_id=invoice_id, plan=plan.id, tier=plan.tier)


@router.post("/webhooks/monobank")
async def monobank_webhook(request: Request, db: DbDep) -> dict:
    """Monobank invoice-status callback. Verifies the signature, then applies it."""
    body = await request.body()

    if _VERIFY_WEBHOOK:
        pubkey = _get_pubkey(monobank.load_config())
        x_sign = request.headers.get("X-Sign", "")
        if pubkey is None or not monobank.verify_signature(pubkey, body, x_sign):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid signature")

    try:
        data = json.loads(body or b"{}")
    except (ValueError, TypeError):
        data = {}
    invoice_id = data.get("invoiceId")
    invoice_status = data.get("status")
    if invoice_id and invoice_status:
        billing.apply_webhook(invoice_id, invoice_status, db=db)

    # Always 200 once accepted, so Monobank stops retrying a message we've handled.
    return {"received": True}


@router.get("/payment/success")
def payment_success() -> RedirectResponse:
    return RedirectResponse(url="/?payment=success", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/payment/fail")
def payment_fail() -> RedirectResponse:
    return RedirectResponse(url="/?payment=fail", status_code=status.HTTP_303_SEE_OTHER)
