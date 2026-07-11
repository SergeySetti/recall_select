"""Monthly usage metering + price-model enforcement (``app.services.usage``).

Pure services-layer tests: Mongo is ``mongomock`` (the ``mongo_db`` fixture),
no Qdrant or embedder needed - the meter and the gate are Mongo-only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import billing, usage, users


def test_current_period_is_utc_year_month():
    dt = datetime(2026, 7, 3, 23, 30, tzinfo=timezone.utc)
    assert usage.current_period(dt) == "2026-07"


def test_record_call_tallies_within_a_period(mongo_db):
    assert usage.calls_this_period("u1", period="2026-07", db=mongo_db) == 0

    assert usage.record_call("u1", period="2026-07", db=mongo_db) == 1
    assert usage.record_call("u1", count=4, period="2026-07", db=mongo_db) == 5
    assert usage.calls_this_period("u1", period="2026-07", db=mongo_db) == 5

    # A different month is a separate tally; a different user is isolated too.
    assert usage.calls_this_period("u1", period="2026-08", db=mongo_db) == 0
    assert usage.calls_this_period("u2", period="2026-07", db=mongo_db) == 0


def test_call_allowance_per_tier():
    assert billing.call_allowance("free") == billing.FREE_CALLS
    assert billing.call_allowance("paid_2x") == 6_000
    assert billing.call_allowance("paid_5x") == 30_000
    assert billing.call_allowance("unlim") is None
    # An unknown/legacy tier is never trusted with more than the free budget.
    assert billing.call_allowance("bogus") == billing.FREE_CALLS


def test_project_allowance_per_tier():
    assert billing.project_allowance("free") == billing.FREE_PROJECTS
    assert billing.project_allowance("paid_5x") == 20
    assert billing.project_allowance("unlim") is None


def test_check_call_allowed_passes_under_budget(mongo_db):
    user = users.add_user("free@example.com", db=mongo_db)  # defaults to the free tier
    usage.record_call(user["_id"], count=billing.FREE_CALLS - 1, db=mongo_db)
    # One call left - allowed.
    usage.check_call_allowed(user["_id"], db=mongo_db)


def test_check_call_allowed_raises_at_budget(mongo_db):
    user = users.add_user("free2@example.com", db=mongo_db)
    usage.record_call(user["_id"], count=billing.FREE_CALLS, db=mongo_db)
    with pytest.raises(usage.QuotaExceeded) as excinfo:
        usage.check_call_allowed(user["_id"], db=mongo_db)
    assert excinfo.value.tier == "free"
    assert excinfo.value.allowance == billing.FREE_CALLS


def test_unlimited_tier_never_trips(mongo_db):
    user = users.add_user("unlim@example.com", tier="unlim", db=mongo_db)
    usage.record_call(user["_id"], count=1_000_000, db=mongo_db)
    usage.check_call_allowed(user["_id"], db=mongo_db)  # no raise


def test_unknown_user_falls_back_to_free_budget(mongo_db):
    # No user document at all: default to the free allowance so an unknown
    # caller can't outrun the meter.
    usage.record_call("ghost", count=billing.FREE_CALLS, db=mongo_db)
    with pytest.raises(usage.QuotaExceeded):
        usage.check_call_allowed("ghost", db=mongo_db)
