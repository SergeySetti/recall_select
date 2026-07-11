"""Payments: signature verification, idempotent tier grant, and the HTTP surface.

No live Monobank - the ECDSA signing is done locally with a throwaway EC key, and
``create_invoice`` is monkeypatched. Mongo is ``mongomock`` (via the ``mongo_db``
fixture), Qdrant is the fake from ``test_api``.
"""
from __future__ import annotations

import base64
import json

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
