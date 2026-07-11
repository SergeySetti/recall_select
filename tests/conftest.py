"""Shared test helpers and fixtures."""
from __future__ import annotations

import httpx
import mongomock
import pytest


@pytest.fixture
def mongo_db():
    """A fresh in-memory Mongo database for integration-style CRUD tests."""
    client = mongomock.MongoClient()
    return client["recall_select_test"]


class FakeResponse:
    """Minimal stand-in for httpx.Response used to drive embed() in tests."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://test/"),
                response=httpx.Response(self.status_code),
            )
