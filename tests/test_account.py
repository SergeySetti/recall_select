"""Account overview aggregation (``app.services.account``).

Pure services-layer tests: Mongo is ``mongomock`` (the ``mongo_db`` fixture);
the overview only reads billing/usage/projects/collections, no Qdrant or embedder.
"""
from __future__ import annotations

from app.services import account, api_keys, billing, collections, projects, usage, users


def test_overview_free_user_no_projects(mongo_db):
    user = users.add_user("free@example.com", db=mongo_db)  # defaults to free tier

    summary = account.overview(user, db=mongo_db)

    assert summary["tier"] == "free"
    assert summary["plan_name"] == "Free"
    assert summary["calls_used"] == 0
    assert summary["calls_allowance"] == billing.FREE_CALLS
    assert summary["calls_remaining"] == billing.FREE_CALLS
    assert summary["project_allowance"] == billing.FREE_PROJECTS
    assert summary["projects_used"] == 0
    assert summary["projects"] == []
    assert summary["memories_total"] == 0
    assert summary["api_keys"] == []


def test_overview_lists_keys_masked(mongo_db):
    user = users.add_user("keys@example.com", db=mongo_db)
    uid = user["_id"]
    link_key = api_keys.add_api_key(uid, label="default", db=mongo_db)
    extra_key = api_keys.add_api_key(uid, label="kitchen laptop", db=mongo_db)

    rows = account.overview(user, db=mongo_db)["api_keys"]

    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {link_key["_id"], extra_key["_id"]}
    # The memory-link key is flagged; every row is display-safe (masked, no secret).
    assert by_id[link_key["_id"]]["is_default"] is True
    assert by_id[extra_key["_id"]]["is_default"] is False
    for row, minted in ((by_id[link_key["_id"]], link_key), (by_id[extra_key["_id"]], extra_key)):
        assert row["masked"] == api_keys.masked(minted)
        assert minted["key"] not in row.values()
        assert row["last_used_at"] is None


def test_overview_counts_usage_and_project_data(mongo_db):
    user = users.add_user("busy@example.com", db=mongo_db)
    uid = user["_id"]

    usage.record_call(uid, count=1_200, db=mongo_db)

    project = projects.add_project(uid, "notes", db=mongo_db)
    collections.register_collection(uid, project["_id"], db=mongo_db)
    collections.set_points_count(uid, project["_id"], 7, db=mongo_db)
    # A second project the user made but never stored into: no collection row.
    projects.add_project(uid, "empty", db=mongo_db)

    summary = account.overview(user, db=mongo_db)

    assert summary["calls_used"] == 1_200
    assert summary["calls_remaining"] == billing.FREE_CALLS - 1_200
    assert summary["projects_used"] == 2
    assert summary["memories_total"] == 7

    by_name = {p["name"]: p for p in summary["projects"]}
    assert by_name["notes"]["points_count"] == 7
    assert by_name["notes"]["has_data"] is True
    # No collection registered -> nothing to delete, shown as empty.
    assert by_name["empty"]["points_count"] == 0
    assert by_name["empty"]["has_data"] is False


def test_overview_unlimited_tier_reports_no_ceiling(mongo_db):
    user = users.add_user("unlim@example.com", tier="unlim", db=mongo_db)
    usage.record_call(user["_id"], count=999, db=mongo_db)

    summary = account.overview(user, db=mongo_db)

    assert summary["plan_name"] == "Unlim"
    assert summary["calls_allowance"] is None
    assert summary["calls_remaining"] is None
    assert summary["project_allowance"] is None
    assert summary["calls_used"] == 999
