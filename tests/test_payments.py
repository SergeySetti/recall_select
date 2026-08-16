"""Payments: signature verification, idempotent tier grant, and the HTTP surface.

No live Monobank - the ECDSA signing is done locally with a throwaway EC key, and
``create_invoice`` is monkeypatched. Mongo is ``mongomock`` (via the ``mongo_db``
fixture), Qdrant is the fake from ``test_api``.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import mongomock
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.api import payments
from app.api.deps import get_current_user, get_database, get_qdrant
from app.main import app
from app.services import billing, mongo, monobank, users
from tests.test_api import FakeQdrant


@pytest.fixture
def keypair():
    """A throwaway EC keypair standing in for the Monobank merchant key."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def _sign(priv, body: bytes) -> str:
    return base64.b64encode(priv.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


# --- Signature verification ------------------------------------------------

def test_verify_signature_accepts_valid_and_rejects_tampering(keypair):
    priv, pub_pem = keypair
    body = b'{"invoiceId":"inv1","status":"success"}'
    sign = _sign(priv, body)

    assert monobank.verify_signature(pub_pem, body, sign) is True
    # Tampered body, right signature -> reject.
    assert monobank.verify_signature(pub_pem, body + b" ", sign) is False
    # Missing / garbage signature -> reject, never raise.
    assert monobank.verify_signature(pub_pem, body, "") is False
    assert monobank.verify_signature(pub_pem, body, "not-base64!!") is False


# --- Tier grant (services layer) -------------------------------------------

def test_apply_webhook_grants_tier_once(mongo_db):
    user = users.add_user("buyer@example.com", db=mongo_db)
    plan = billing.PLANS["unlim"]
    billing.record_pending("inv1", user["_id"], plan, db=mongo_db)

    # First success flips the tier.
    billing.apply_webhook("inv1", "success", db=mongo_db)
    assert users.get_user(user["_id"], db=mongo_db)["tier"] == plan.tier
    assert billing.get_payment("inv1", db=mongo_db)["status"] == "success"

    # A duplicate delivery is a no-op: still one grant, tier unchanged even if we
    # meddle with the user in between (proves we don't re-apply).
    users.update_user(user["_id"], db=mongo_db, tier="free")
    billing.apply_webhook("inv1", "success", db=mongo_db)
    assert users.get_user(user["_id"], db=mongo_db)["tier"] == "free"


def test_apply_webhook_unknown_and_failure(mongo_db):
    # Unknown invoice: ignored, no crash.
    assert billing.apply_webhook("nope", "success", db=mongo_db) is None

    user = users.add_user("b@example.com", db=mongo_db)
    billing.record_pending("inv2", user["_id"], billing.PLANS["2x"], db=mongo_db)
    billing.apply_webhook("inv2", "failure", db=mongo_db)
    assert billing.get_payment("inv2", db=mongo_db)["status"] == "failure"
    # A failure never grants a tier.
    assert users.get_user(user["_id"], db=mongo_db)["tier"] == "free"


# --- HTTP surface ----------------------------------------------------------

@pytest.fixture
def client():
    db = mongomock.MongoClient()["recall_select_test"]
    mongo.ensure_indexes(db)
    qdrant = FakeQdrant()
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_qdrant] = lambda: qdrant
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _sign_in(db, email="me@example.com"):
    user = users.add_user(email, db=db)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def test_checkout_creates_invoice_and_records_pending(client, monkeypatch):
    api, db = client
    user = _sign_in(db)
    monkeypatch.setenv("MONOBANK_API_KEY", "test-token")
    monkeypatch.setattr(
        monobank,
        "create_invoice",
        lambda cfg, **kw: {"invoiceId": "inv-xyz", "pageUrl": "https://pay.mono/inv-xyz"},
    )

    resp = api.post("/api/me/checkout", json={"plan": "unlim"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pay_url"] == "https://pay.mono/inv-xyz"
    assert body["invoice_id"] == "inv-xyz"
    assert body["tier"] == billing.PLANS["unlim"].tier

    # A pending payment now maps the invoice back to this user.
    payment = billing.get_payment("inv-xyz", db=db)
    assert payment["user_id"] == user["_id"] and payment["status"] == "created"


def test_checkout_rejects_unknown_plan(client, monkeypatch):
    api, db = client
    _sign_in(db)
    monkeypatch.setenv("MONOBANK_API_KEY", "test-token")
    assert api.post("/api/me/checkout", json={"plan": "ghost"}).status_code == 404


def test_checkout_requires_sign_in(client):
    api, _ = client
    assert api.post("/api/me/checkout", json={"plan": "unlim"}).status_code == 401


def test_webhook_verified_grants_tier(client, keypair, monkeypatch):
    api, db = client
    priv, pub_pem = keypair
    user = users.add_user("wh@example.com", db=db)
    billing.record_pending("inv-wh", user["_id"], billing.PLANS["unlim"], db=db)

    monkeypatch.setattr(payments, "_VERIFY_WEBHOOK", True)
    monkeypatch.setattr(payments, "_get_pubkey", lambda cfg: pub_pem)
    body = json.dumps({"invoiceId": "inv-wh", "status": "success"}).encode()

    # Wrong signature is refused and grants nothing.
    bad = api.post("/webhooks/monobank", content=body, headers={"X-Sign": "AAAA"})
    assert bad.status_code == 400
    assert users.get_user(user["_id"], db=db)["tier"] == "free"

    # Correct signature over the exact body is accepted and flips the tier.
    ok = api.post("/webhooks/monobank", content=body, headers={"X-Sign": _sign(priv, body)})
    assert ok.status_code == 200
    assert users.get_user(user["_id"], db=db)["tier"] == billing.PLANS["unlim"].tier


# --- Reconciliation --------------------------------------------------------
# The webhook is delivered once and never re-sent, so entitlement cannot depend
# on it alone: these cover the pull side that settles what the push side missed.


def _aged_payment(mongo_db, invoice_id: str, user_id: str, *, minutes: int, status: str = "created"):
    """A pending payment whose created_at is backdated by ``minutes``."""
    plan = billing.get_plan("2x")
    billing.record_pending(invoice_id, user_id, plan, db=mongo_db)
    stale = mongo.utcnow() - timedelta(minutes=minutes)
    mongo_db.payments.update_one(
        {"_id": invoice_id}, {"$set": {"created_at": stale, "status": status}}
    )
    return billing.get_payment(invoice_id, db=mongo_db)


def test_list_unsettled_skips_fresh_and_terminal(mongo_db):
    user = users.add_user("recon@example.com", db=mongo_db)
    uid = user["_id"]
    _aged_payment(mongo_db, "old-pending", uid, minutes=30)
    _aged_payment(mongo_db, "fresh-pending", uid, minutes=0)
    _aged_payment(mongo_db, "old-paid", uid, minutes=30, status="success")
    _aged_payment(mongo_db, "old-expired", uid, minutes=30, status="expired")

    ids = [p["_id"] for p in billing.list_unsettled(db=mongo_db)]

    # Only the one still in flight and old enough that its webhook should have come.
    assert ids == ["old-pending"]


def test_reconcile_grants_the_tier_a_lost_webhook_would_have(mongo_db):
    user = users.add_user("paid@example.com", db=mongo_db)
    uid = user["_id"]
    _aged_payment(mongo_db, "inv-paid", uid, minutes=30)

    report = billing.reconcile(fetch_status=lambda _: "success", db=mongo_db)

    assert report == {"checked": 1, "settled": 1, "granted": 1, "errors": 0}
    assert users.get_user(uid, db=mongo_db)["tier"] == "paid_2x"
    stored = billing.get_payment("inv-paid", db=mongo_db)
    assert stored["status"] == "success"
    assert stored["paid_at"] is not None


def test_reconcile_records_expiry_without_granting(mongo_db):
    user = users.add_user("expired@example.com", db=mongo_db)
    uid = user["_id"]
    _aged_payment(mongo_db, "inv-expired", uid, minutes=30)

    report = billing.reconcile(fetch_status=lambda _: "expired", db=mongo_db)

    assert report == {"checked": 1, "settled": 1, "granted": 0, "errors": 0}
    assert billing.get_payment("inv-expired", db=mongo_db)["status"] == "expired"
    assert users.get_user(uid, db=mongo_db)["tier"] == "free"


def test_reconcile_is_idempotent_with_the_webhook(mongo_db):
    """The sweep and a duplicate webhook must not grant twice or fight."""
    user = users.add_user("both@example.com", db=mongo_db)
    uid = user["_id"]
    _aged_payment(mongo_db, "inv-both", uid, minutes=30)

    billing.apply_webhook("inv-both", "success", db=mongo_db)
    paid_at = billing.get_payment("inv-both", db=mongo_db)["paid_at"]

    # Already terminal, so the sweep does not even look at it.
    report = billing.reconcile(fetch_status=lambda _: "success", db=mongo_db)

    assert report["checked"] == 0
    assert billing.get_payment("inv-both", db=mongo_db)["paid_at"] == paid_at
    assert users.get_user(uid, db=mongo_db)["tier"] == "paid_2x"


def test_reconcile_survives_a_failing_lookup(mongo_db):
    user = users.add_user("flaky@example.com", db=mongo_db)
    uid = user["_id"]
    _aged_payment(mongo_db, "inv-a", uid, minutes=30)
    _aged_payment(mongo_db, "inv-b", uid, minutes=30)

    def fetch(invoice_id: str) -> str:
        if invoice_id == "inv-a":
            raise RuntimeError("Monobank unreachable")
        return "success"

    report = billing.reconcile(fetch_status=fetch, db=mongo_db)

    # The unreachable invoice is counted and skipped; the other still settles.
    assert report["errors"] == 1
    assert report["granted"] == 1
    assert billing.get_payment("inv-a", db=mongo_db)["status"] == "created"
    assert users.get_user(uid, db=mongo_db)["tier"] == "paid_2x"


def test_reconcile_with_monobank_is_a_noop_without_a_merchant_token(mongo_db, monkeypatch):
    monkeypatch.delenv("MONOBANK_API_KEY", raising=False)

    assert billing.reconcile_with_monobank(db=mongo_db)["skipped"] is True


def test_fetch_invoice_status_calls_the_status_endpoint(monkeypatch):
    seen = {}

    def fake_get(cfg, path):
        seen["path"] = path
        return {"status": "success", "amount": 1700}

    monkeypatch.setattr(monobank, "_get_json", fake_get)
    cfg = monobank.MonobankConfig(api_key="token", webhook_url="", redirect_url="")

    assert monobank.fetch_invoice_status(cfg, "inv 42/x")["status"] == "success"
    # The id is query-escaped, never interpolated raw.
    assert seen["path"] == "/api/merchant/invoice/status?invoiceId=inv%2042/x"
