"""Embedder abstraction.

A small interface over the concrete embedding backend so the rest of the app
depends on `Embedder`, not a specific provider. The single implementation is
the remote embedding API; the instance is provided centrally by
`app.dependencies` (the DI container) - callers never construct one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.services import embeddings_remote


class Embedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for `text`."""


class RemoteEmbedder(Embedder):
    """Remote embedding API (e.g. DeepInfra)."""

    def embed(self, text: str) -> list[float]:
        return embeddings_remote.embed(text)
