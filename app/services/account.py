"""Account overview (services layer): the read-only snapshot the personal area shows.

The signed-in "personal area" (``/account``) answers four plain questions -
what plan am I on, how much have I used this month, what's stored per project
(so I can wipe it), and which API keys exist (so I can revoke them). This module
gathers that snapshot by composing the existing read services (``billing``
allowances, ``usage`` metering, ``projects`` list, ``collections`` counters,
``api_keys`` display hints) - no new storage, just a join for the page.

Pure I/O - ``db`` is injected so this is testable without live backends.
"""
from __future__ import annotations

from pymongo.database import Database

from app.services import api_keys, billing, collections, projects, usage
from app.services.mongo import get_db


def overview(user: dict, *, db: Database | None = None) -> dict:
    """A single dict describing the user's plan, monthly usage, and projects.

    ``calls_allowance`` / ``project_allowance`` are ``None`` on an unlimited tier;
    ``calls_remaining`` is ``None`` to match. Each project row carries its stored
    ``points_count`` (memories) and a ``has_data`` flag - true when a collection is
    registered, i.e. when "Delete data" has something to tear down.

    ``api_keys`` carries only display-safe fields (the secret is never at rest,
    see the ``api_keys`` service): the masked form, label, created / last-used
    timestamps, and an ``is_default`` flag marking the key behind the user's
    memory link. (Named ``api_keys``, not ``keys`` - Jinja's ``summary.keys``
    would resolve to ``dict.keys``.)
    """
    db = db if db is not None else get_db()
    user_id = user["_id"]
    tier = user.get("tier", billing.FREE_TIER)

    calls_allowance = billing.call_allowance(tier)
    calls_used = usage.calls_this_period(user_id, db=db)
    calls_remaining = (
        None if calls_allowance is None else max(calls_allowance - calls_used, 0)
    )

    project_rows: list[dict] = []
    memories_total = 0
    for project in projects.list_projects(user_id, db=db):
        record = collections.get_collection(user_id, project["_id"], db=db)
        points = record["points_count"] if record else 0
        memories_total += points
        project_rows.append(
            {
                "id": project["_id"],
                "name": project["name"],
                "is_default": project.get("is_default", False),
                "points_count": points,
                "has_data": record is not None,
            }
        )

    key_rows = [
        {
            "id": key["_id"],
            "masked": api_keys.masked(key),
            "label": key.get("label"),
            "is_default": key.get("label") == "default",
            "created_at": key["created_at"],
            "last_used_at": key.get("last_used_at"),
        }
        for key in api_keys.list_api_keys(user_id, db=db)
    ]

    return {
        "tier": tier,
        "plan_name": billing.tier_name(tier),
        "period": usage.current_period(),
        "calls_used": calls_used,
        "calls_allowance": calls_allowance,
        "calls_remaining": calls_remaining,
        "project_allowance": billing.project_allowance(tier),
        "projects_used": len(project_rows),
        "projects": project_rows,
        "memories_total": memories_total,
        "api_keys": key_rows,
    }
