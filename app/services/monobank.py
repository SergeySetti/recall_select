"""Minimal Monobank Acquiring client + webhook signature verification.

recall.select reuses the **same Monobank merchant** as the sibling mcp-api.net
platform - only the merchant token (``MONOBANK_API_KEY``) is shared. recall.select
still creates its own invoices with its own ``redirectUrl``/``webHookUrl``, so no
payment traffic is proxied through the other app: funds land in the one merchant
account and are told apart by ``merchantPaymInfo.reference``.

Invoice create:
  POST https://api.monobank.ua/api/merchant/invoice/create
  Header: X-Token: <MONOBANK_API_KEY>
  Body:   { amount, ccy, merchantPaymInfo, redirectUrl, webHookUrl, ... }
  Reply:  { invoiceId, pageUrl }

Webhook auth:
  Monobank signs each webhook body with the merchant's EC key. The ``X-Sign``
  header is a base64 ECDSA-SHA256 signature over the *raw* request body; the
  matching public key comes from ``GET /api/merchant/pubkey`` (also base64). A
  webhook is only trustworthy once that signature checks out - without it anyone
  who learns the URL could POST a fake ``status:"success"`` and self-upgrade.

Docs: https://monobank.ua/api-docs/acquiring/
"""
from __future__ import annotations

import base64
import json as _json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

MONOBANK_API_BASE = "https://api.monobank.ua"
INVOICE_CREATE_PATH = "/api/merchant/invoice/create"
PUBKEY_PATH = "/api/merchant/pubkey"

# ISO 4217 numeric currency codes accepted by Monobank acquiring.
CCY = {"UAH": 980, "USD": 840, "EUR": 978}


@dataclass(frozen=True)
class MonobankConfig:
    api_key: str
    webhook_url: str
    redirect_url: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def load_config() -> MonobankConfig:
    return MonobankConfig(
        api_key=os.getenv("MONOBANK_API_KEY", ""),
        webhook_url=os.getenv("MONOBANK_WEBHOOK_URL", ""),
        redirect_url=os.getenv("MONOBANK_REDIRECT_URL", ""),
    )


def create_invoice(
    cfg: MonobankConfig,
    *,
    amount_minor: int,
    currency: str,
    reference: str,
    destination: str,
    redirect_url: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """Create a Monobank invoice and return the parsed response (`invoiceId`, `pageUrl`)."""
    if not cfg.configured:
        raise RuntimeError("MONOBANK_API_KEY is not set")
    ccy = CCY.get(currency.upper())
    if ccy is None:
        raise ValueError(f"unsupported currency: {currency}")

    payload: dict = {
        "amount": int(amount_minor),
        "ccy": ccy,
        "merchantPaymInfo": {
            "reference": reference,
            "destination": destination,
        },
        "paymentType": "debit",
    }
    redirect = redirect_url or cfg.redirect_url
    if redirect:
        payload["redirectUrl"] = redirect
    webhook = webhook_url or cfg.webhook_url
    if webhook:
        payload["webHookUrl"] = webhook

    return _post_json(cfg, INVOICE_CREATE_PATH, payload)


def fetch_pubkey(cfg: MonobankConfig) -> bytes:
    """Fetch the merchant's public key (PEM bytes) used to verify webhook signatures.

    Monobank returns it base64-encoded under ``key``; we decode to the raw PEM the
    ``cryptography`` loader expects.
    """
    if not cfg.configured:
        raise RuntimeError("MONOBANK_API_KEY is not set")
    resp = _get_json(cfg, PUBKEY_PATH)
    key_b64 = resp.get("key")
    if not key_b64:
        raise RuntimeError("Monobank pubkey response missing 'key'")
    return base64.b64decode(key_b64)


def verify_signature(pub_key_pem: bytes, body: bytes, x_sign_b64: str) -> bool:
    """True iff ``x_sign_b64`` is a valid ECDSA-SHA256 signature of ``body``.

    Pure and total: any malformed key/signature/encoding yields ``False`` rather
    than raising, so a bad webhook is simply rejected.
    """
    if not x_sign_b64:
        return False
    try:
        pub = serialization.load_pem_public_key(pub_key_pem)
        signature = base64.b64decode(x_sign_b64)
        pub.verify(signature, body, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# --- HTTP plumbing ---------------------------------------------------------
# Deliberately stdlib-only (like the mcp-api.net original) so the client has no
# runtime dependency beyond `cryptography` for the signature check.

def _post_json(cfg: MonobankConfig, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        MONOBANK_API_BASE + path,
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Token": cfg.api_key},
        method="POST",
    )
    return _send(req, path)


def _get_json(cfg: MonobankConfig, path: str) -> dict:
    req = urllib.request.Request(
        MONOBANK_API_BASE + path,
        headers={"X-Token": cfg.api_key},
        method="GET",
    )
    return _send(req, path)


def _send(req: urllib.request.Request, path: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Monobank {path} failed ({e.code}): {detail}") from e
    return _json.loads(body)
