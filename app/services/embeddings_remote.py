import os

import httpx

from app.services.qdrant_store import VECTOR_SIZE

EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.deepinfra.com/v1").rstrip("/")


def embed(text: str) -> list[float]:
    """Create a ``VECTOR_SIZE``-dim embedding via DeepInfra (Qwen3-Embedding-4B).

    The model's native dimension is larger (2560); we pass ``dimensions`` so the
    API returns exactly ``VECTOR_SIZE`` floats, matching the Qdrant collections
    we create. Keep the two in lockstep via the single ``VECTOR_SIZE`` knob.
    """
    api_key = os.getenv("EMBEDDING_API_KEY", "")
    base_url = os.getenv("EMBEDDING_BASE_URL", EMBEDDING_BASE_URL).rstrip("/")
    resp = httpx.post(
        f"{base_url}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": EMBEDDING_MODEL, "input": text, "dimensions": VECTOR_SIZE},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
