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


# --- Tier expiry -----------------------------------------------------------
# A paid tier is bought by the month. `tier` records the purchase;
# `tier_expires_at` records how long it is good for, and entitlement must be
# read through effective_tier or an elapsed grant keeps paying out forever.


def test_effective_tier_ignores_an_elapsed_grant(mongo_db):
    user = users.add_user("lapsed@example.com", tier="paid_2x", db=mongo_db)
    mongo_db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"tier_expires_at": mongo.utcnow() - timedelta(days=1)}},
    )
    lapsed = users.get_user(user["_id"], db=mongo_db)

    assert lapsed["tier"] == "paid_2x"  # the purchase is still on record
    assert billing.effective_tier(lapsed) == "free"  # the entitlement is not
    assert billing.call_allowance(billing.effective_tier(lapsed)) == billing.FREE_CALLS


def test_effective_tier_honours_a_live_or_open_ended_grant(mongo_db):
    live = users.add_user("live@example.com", tier="paid_2x", db=mongo_db)
    mongo_db.users.update_one(
        {"_id": live["_id"]},
        {"$set": {"tier_expires_at": mongo.utcnow() + timedelta(days=5)}},
    )
    forever = users.add_user("forever@example.com", tier="unlim", db=mongo_db)

    assert billing.effective_tier(users.get_user(live["_id"], db=mongo_db)) == "paid_2x"
    # No expiry set at all = open-ended (legacy rows and permanent gifts).
    assert billing.effective_tier(forever) == "unlim"
    assert billing.effective_tier(None) == "free"


def test_effective_tier_handles_naive_timestamps_from_mongo(mongo_db):
    """pymongo returns naive datetimes; comparing them to utcnow() must not raise."""
    user = users.add_user("naive@example.com", tier="paid_2x", db=mongo_db)
    naive_future = (mongo.utcnow() + timedelta(days=3)).replace(tzinfo=None)

    assert billing.effective_tier({**user, "tier_expires_at": naive_future}) == "paid_2x"
    naive_past = (mongo.utcnow() - timedelta(days=3)).replace(tzinfo=None)
    assert billing.effective_tier({**user, "tier_expires_at": naive_past}) == "free"


def test_grant_tier_sets_a_window_and_logs_why(mongo_db):
    user = users.add_user("gift@example.com", db=mongo_db)

    granted = billing.grant_tier(
        user["_id"], "paid_2x", days=30, reason="goodwill", source="owner", db=mongo_db
    )

    assert granted["tier"] == "paid_2x"
    assert billing.effective_tier(granted) == "paid_2x"
    expires = billing.tier_expiry(granted)
    assert timedelta(days=29) < expires - mongo.utcnow() <= timedelta(days=30)
    (entry,) = billing.list_grants(user["_id"], db=mongo_db)
    assert entry["reason"] == "goodwill"
    assert entry["source"] == "owner"
    assert entry["previous_tier"] == "free"


def test_grant_tier_extends_rather_than_truncates(mongo_db):
    user = users.add_user("renewer@example.com", db=mongo_db)
    first = billing.grant_tier(
        user["_id"], "paid_2x", days=30, reason="month one", source="monobank", db=mongo_db
    )
    first_expiry = billing.tier_expiry(first)

    second = billing.grant_tier(
        user["_id"], "paid_2x", days=30, reason="month two", source="monobank", db=mongo_db
    )

    # Renewing early adds to the remaining time instead of restarting the clock.
    assert billing.tier_expiry(second) - first_expiry == timedelta(days=30)
    assert len(billing.list_grants(user["_id"], db=mongo_db)) == 2


def test_paid_webhook_grants_exactly_one_month(mongo_db):
    user = users.add_user("buyer@example.com", db=mongo_db)
    billing.record_pending("inv-month", user["_id"], billing.get_plan("2x"), db=mongo_db)

    billing.apply_webhook("inv-month", "success", db=mongo_db)

    bought = users.get_user(user["_id"], db=mongo_db)
    assert billing.effective_tier(bought) == "paid_2x"
    assert billing.tier_expiry(bought) is not None
    (entry,) = billing.list_grants(user["_id"], db=mongo_db)
    assert entry["source"] == "monobank" and entry["reason"] == "invoice inv-month"


def test_downgrade_expired_returns_lapsed_accounts_to_free(mongo_db):
    lapsed = users.add_user("out@example.com", tier="paid_2x", db=mongo_db)
    mongo_db.users.update_one(
        {"_id": lapsed["_id"]},
        {"$set": {"tier_expires_at": mongo.utcnow() - timedelta(hours=1)}},
    )
    live = users.add_user("in@example.com", db=mongo_db)
    billing.grant_tier(live["_id"], "paid_2x", days=30, reason="x", source="owner", db=mongo_db)
    forever = users.add_user("keep@example.com", tier="unlim", db=mongo_db)

    assert billing.downgrade_expired(db=mongo_db) == 1

    assert users.get_user(lapsed["_id"], db=mongo_db)["tier"] == "free"
    assert users.get_user(live["_id"], db=mongo_db)["tier"] == "paid_2x"
    # An open-ended grant has no window to fall out of.
    assert users.get_user(forever["_id"], db=mongo_db)["tier"] == "unlim"


def test_quota_check_uses_the_effective_tier(mongo_db):
    from app.services import usage
    from app.services.usage import QuotaExceeded

    user = users.add_user("overrun@example.com", tier="paid_2x", db=mongo_db)
    mongo_db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"tier_expires_at": mongo.utcnow() - timedelta(days=1)}},
    )
    # Spent more than free allows, less than the lapsed paid tier would have.
    usage.record_call(user["_id"], count=billing.FREE_CALLS, db=mongo_db)

    with pytest.raises(QuotaExceeded):
        usage.check_call_allowed(user["_id"], db=mongo_db)
