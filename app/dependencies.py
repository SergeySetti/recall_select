"""Core dependency-injection container.

Single place where the app's shared singletons are constructed and wired:
the Qdrant client, the Mongo client/database, and the (remote) embedder.
Everything else (FastAPI deps, startup) resolves from `app_container`
rather than constructing clients itself.

Following the `injector` pattern: a `Module` of providers + one global
`Injector`. Providers are lazy singletons, so nothing connects at import time.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from injector import Injector, Module, provider, singleton
from pymongo import MongoClient
from pymongo.database import Database
from qdrant_client import QdrantClient

from app.services.embeddings import Embedder, RemoteEmbedder

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
# Sent on every Qdrant request when set. Must match the server's
# QDRANT__SERVICE__API_KEY (see docker-compose.yml); None = unauthenticated
# (e.g. local dev against a keyless Qdrant).
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "recall_select")


class RecallSelectModule(Module):

    @provider
    @singleton
    def provide_qdrant(self) -> QdrantClient:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    @provider
    @singleton
    def provide_mongo_client(self) -> MongoClient:
        return MongoClient(MONGODB_URI)

    @provider
    @singleton
    def provide_database(self, client: MongoClient) -> Database:
        return client[MONGODB_DB]

    @provider
    @singleton
    def provide_embedder(self) -> Embedder:
        return RemoteEmbedder()


# Global container - the app's single source for shared singletons.
app_container = Injector([RecallSelectModule()])
