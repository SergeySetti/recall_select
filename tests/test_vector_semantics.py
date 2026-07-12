"""Unit tests for the vector memory utility layer (``vector_semantics``).

Drives the service functions directly (no HTTP) against ``mongomock`` and a
fake Qdrant that keeps whole points and ranks by real cosine, so geometry,
payload joins, and declared relations all behave as they would live.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest

from app.services import collections, vector_semantics as vs

UID, PID = "u1", "p1"


class FakeQdrant:
    """In-memory stand-in: keeps whole points (vector + payload) per collection."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def collection_exists(self, name: str) -> bool:
        return name in self.data

    def retrieve(self, *, collection_name, ids, with_payload=True, with_vectors=False):
        bucket = self.data.get(collection_name, {})
        return [bucket[i] for i in ids if i in bucket]

    def query_points(self, *, collection_name, query, limit):
        bucket = self.data.get(collection_name, {})
        # A bare id in the query position resolves to that point's own vector.
        is_id = isinstance(query, (str, int)) and query in bucket
        vector = bucket[query].vector if is_id else query
        scored = sorted(
            (SimpleNamespace(id=p.id, score=vs._cosine(vector, p.vector), payload=p.payload)
             for p in bucket.values()),
            key=lambda h: h.score,
            reverse=True,
        )
        return SimpleNamespace(points=scored[:limit])

    def scroll(self, *, collection_name, limit, offset=None, with_payload=True, with_vectors=False):
        return list(self.data.get(collection_name, {}).values()), None

    def set_payload(self, *, collection_name, payload, points) -> None:
        bucket = self.data.get(collection_name, {})
        for pid in points:
            if pid in bucket:
                bucket[pid].payload = {**(bucket[pid].payload or {}), **payload}


@pytest.fixture
def env():
    db = mongomock.MongoClient()["recall_select_test"]
    record = collections.register_collection(UID, PID, db=db)
    qdrant = FakeQdrant()
    qdrant.data[record["name"]] = {}
    return SimpleNamespace(db=db, qdrant=qdrant, name=record["name"])


def put(env, pid: str, vector: list[float], text: str = "", semantics: dict | None = None):
    payload: dict = {"text": text}
    if semantics is not None:
        payload[vs.SEMANTICS_KEY] = semantics
    env.qdrant.data[env.name][pid] = SimpleNamespace(id=pid, vector=vector, payload=payload)


def kw(env) -> dict:
    return {"db": env.db, "qdrant": env.qdrant}


# --------------------------------------------------------------------------- #
# Store-time annotation + client annotations.                                 #
# --------------------------------------------------------------------------- #
def test_annotate_store_payload_carries_deixis_anchors():
    fragment = vs.annotate_store_payload(UID)
    semantics = fragment[vs.SEMANTICS_KEY]
    assert semantics["owner"] == UID
    # stored_at is a parseable, timezone-aware ISO timestamp.
    assert datetime.fromisoformat(semantics["stored_at"]).tzinfo is not None


def test_annotate_memory_merges_and_guards(env):
    put(env, "a", [1, 0, 0], semantics={"owner": UID})

    assert vs.annotate_memory(UID, PID, "a", {"entities": ["acme"]}, **kw(env))
    semantics = env.qdrant.data[env.name]["a"].payload[vs.SEMANTICS_KEY]
    assert semantics == {"owner": UID, "entities": ["acme"]}

    # Second merge updates keys without clobbering the rest.
    assert vs.annotate_memory(UID, PID, "a", {"entities": ["acme", "anna"]}, **kw(env))
    assert env.qdrant.data[env.name]["a"].payload[vs.SEMANTICS_KEY]["owner"] == UID

    assert not vs.annotate_memory(UID, PID, "missing", {"entities": []}, **kw(env))
    with pytest.raises(ValueError):
        vs.annotate_memory(UID, PID, "a", {"relations": []}, **kw(env))


# --------------------------------------------------------------------------- #
# Declared relations: ingestion + readback.                                   #
# --------------------------------------------------------------------------- #
def test_upsert_relations_validates_and_dedupes(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])

    result = vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "part_of"},
        {"source": "a", "target": "ghost", "type": "part_of"},
        {"source": "a", "target": "a", "type": "part_of"},
        {"source": "a", "target": "b"},
        {"source": "a", "target": "b", "type": "causes", "weight": 2.0},
        {"source": "a", "target": "b", "type": "causes", "confidence": 0.0},
        {"source": "a", "target": "b", "type": "causes", "valid_till": "soonish"},
    ], **kw(env))
    assert result["stored"] == 1
    reasons = [r["reason"] for r in result["rejected"]]
    assert reasons == [
        "unknown memory id", "self-relation",
        "source, target and type are required", "weight must be in (0, 1]",
        "confidence must be in (0, 1]", "valid_till must be an ISO 8601 timestamp",
    ]

    # Re-declaring the same (target, type) updates in place - no duplicate edge.
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "part_of", "weight": 0.4},
    ], **kw(env))
    edges = env.qdrant.data[env.name]["a"].payload[vs.SEMANTICS_KEY]["relations"]
    assert len(edges) == 1 and edges[0]["weight"] == 0.4


def test_relations_of_directions_and_filter(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "causes"},
        {"source": "b", "target": "a", "type": "part_of"},
    ], **kw(env))

    outgoing = vs.relations_of(UID, PID, "a", **kw(env))
    assert [(e["source"], e["target"], e["type"]) for e in outgoing] == [("a", "b", "causes")]

    both = vs.relations_of(UID, PID, "a", include_incoming=True, **kw(env))
    assert {(e["source"], e["type"]) for e in both} == {("a", "causes"), ("b", "part_of")}

    only = vs.relations_of(UID, PID, "a", include_incoming=True, types=["part_of"], **kw(env))
    assert [e["type"] for e in only] == ["part_of"]


def test_remove_relations_by_pair_and_type(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])
    put(env, "c", [0, 0, 1])
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "causes"},
        {"source": "a", "target": "b", "type": "part_of"},
        {"source": "a", "target": "c", "type": "causes"},
    ], **kw(env))

    # With a type: only that one relation goes.
    result = vs.remove_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "causes"},
    ], **kw(env))
    assert result == {"removed": 1, "rejected": []}
    assert {(e["target"], e["type"]) for e in vs.relations_of(UID, PID, "a", **kw(env))} == {
        ("b", "part_of"), ("c", "causes"),
    }

    # Without a type: everything from source to target goes; other targets stay.
    result = vs.remove_relations(UID, PID, [{"source": "a", "target": "b"}], **kw(env))
    assert result["removed"] == 1
    assert [e["target"] for e in vs.relations_of(UID, PID, "a", **kw(env))] == ["c"]

    # Bad selectors are reported, not silently ignored.
    result = vs.remove_relations(UID, PID, [
        {"source": "ghost", "target": "b"},
        {"source": "a"},
    ], **kw(env))
    assert result["removed"] == 0
    assert {r["reason"] for r in result["rejected"]} == {
        "unknown memory id", "source and target are required",
    }


def test_prune_relations_to_clears_dangling_edges(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])
    put(env, "c", [0, 0, 1])
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "c", "type": "causes"},
        {"source": "b", "target": "c", "type": "part_of"},
        {"source": "a", "target": "b", "type": "causes"},
    ], **kw(env))

    # After "c" is deleted, every edge aimed at it goes; unrelated edges stay.
    del env.qdrant.data[env.name]["c"]
    assert vs.prune_relations_to(UID, PID, "c", **kw(env)) == 2
    assert [e["target"] for e in vs.relations_of(UID, PID, "a", **kw(env))] == ["b"]
    assert vs.relations_of(UID, PID, "b", **kw(env)) == []


def test_confidence_scales_traversal_strength(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "causes", "weight": 0.8, "confidence": 0.5},
    ], **kw(env))

    [edge] = vs.declared_lens(list(env.qdrant.data[env.name].values()))
    # An unsure link conducts proportionally less: weight * confidence.
    assert edge["weight"] == pytest.approx(0.4)
    assert edge["confidence"] == 0.5

    result = vs.infer_relation(UID, PID, "a", "b", **kw(env))
    assert result["source"] == "declared" and result["confidence"] == 0.5


def test_expired_relations_stop_influencing_reads(env):
    put(env, "a", [1, 0, 0])
    put(env, "b", [0, 1, 0])
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "b", "type": "stale_link", "valid_till": past},
        {"source": "a", "target": "b", "type": "live_link", "valid_till": future},
    ], **kw(env))

    # The lens and default reads see only the live edge...
    assert [e["type"] for e in vs.declared_lens(list(env.qdrant.data[env.name].values()))] == ["live_link"]
    assert [e["type"] for e in vs.relations_of(UID, PID, "a", **kw(env))] == ["live_link"]
    # ...inspection can still surface the expired one...
    both = vs.relations_of(UID, PID, "a", include_expired=True, **kw(env))
    assert {e["type"] for e in both} == {"stale_link", "live_link"}
    # ...and inference treats an expired edge as gone (live one still wins).
    assert vs.infer_relation(UID, PID, "a", "b", **kw(env))["relation"] == "live_link"


# --------------------------------------------------------------------------- #
# Lenses.                                                                     #
# --------------------------------------------------------------------------- #
def test_topical_lens_thresholds():
    points = [
        SimpleNamespace(id="a", vector=[1, 0, 0], payload={}),
        SimpleNamespace(id="a2", vector=[0.9, 0.1, 0], payload={}),
        SimpleNamespace(id="c", vector=[0, 1, 0], payload={}),
    ]
    edges = vs.topical_lens(points)
    assert [(e["source"], e["target"]) for e in edges] == [("a", "a2")]
    assert edges[0]["layer"] == "topical" and not edges[0]["directed"]


def test_temporal_lens_window_and_direction():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    def at(ts):
        return {vs.SEMANTICS_KEY: {"stored_at": ts.isoformat()}}
    points = [
        SimpleNamespace(id="late", vector=[], payload=at(t0 + timedelta(hours=2))),
        SimpleNamespace(id="early", vector=[], payload=at(t0)),
        SimpleNamespace(id="far", vector=[], payload=at(t0 + timedelta(days=30))),
        SimpleNamespace(id="unstamped", vector=[], payload={}),
    ]
    edges = vs.temporal_lens(points, window_seconds=24 * 3600)
    assert [(e["source"], e["target"], e["type"]) for e in edges] == [("early", "late", "precedes")]
    assert edges[0]["directed"] and 0 < edges[0]["weight"] < 1


def test_entity_lens_joins_on_shared_values():
    def ent(*names):
        return {vs.SEMANTICS_KEY: {"entities": list(names)}}
    points = [
        SimpleNamespace(id="a", vector=[], payload=ent("Acme", "anna")),
        SimpleNamespace(id="b", vector=[], payload=ent("acme")),
        SimpleNamespace(id="c", vector=[], payload=ent("other")),
    ]
    edges = vs.entity_lens(points)
    assert [(e["source"], e["target"], e["type"]) for e in edges] == [("a", "b", "same_entity:acme")]


# --------------------------------------------------------------------------- #
# Graph, clustering, retrieval by connection.                                 #
# --------------------------------------------------------------------------- #
def test_semantic_graph_merges_lenses(env):
    put(env, "a", [1, 0, 0], semantics={"entities": ["acme"]})
    put(env, "a2", [0.9, 0.1, 0])
    put(env, "c", [0, 1, 0], semantics={"entities": ["acme"]})

    graph = vs.semantic_graph(UID, PID, lenses=("topical", "entity"), **kw(env))
    assert {n["id"] for n in graph["nodes"]} == {"a", "a2", "c"}
    by_layer = {e["layer"] for e in graph["edges"]}
    assert by_layer == {"topical", "entity"}

    with pytest.raises(ValueError):
        vs.semantic_graph(UID, PID, lenses=("astral",), **kw(env))


def test_related_memories_drops_seed_and_weak_links(env):
    put(env, "a", [1, 0, 0])
    put(env, "a2", [0.9, 0.1, 0])
    put(env, "c", [0, 1, 0])

    hits = vs.related_memories(UID, PID, "a", **kw(env))
    assert [h["id"] for h in hits] == ["a2"]


def test_spreading_activation_topical_from_text(env):
    put(env, "a", [1, 0, 0], text="alpha")
    put(env, "a2", [0.9, 0.1, 0], text="alpha too")
    put(env, "c", [0, 1, 0], text="unrelated")

    hits = vs.spreading_activation(
        UID, PID, "query text", embed=lambda _: [1, 0, 0], **kw(env)
    )
    ids = [h["id"] for h in hits]
    assert ids[0] == "a" and "a2" in ids and "c" not in ids


def test_spreading_activation_layer_weights_reach_across_meaning(env):
    # a and c are topically orthogonal; only the declared edge connects them.
    put(env, "a", [1, 0, 0], text="alpha")
    put(env, "c", [0, 1, 0], text="gamma")
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "c", "type": "causes"},
    ], **kw(env))

    hits = vs.spreading_activation(
        UID, PID, "a",
        lenses=("topical", "declared"),
        layer_weights={"topical": 0.0},  # follow explicit structure only
        embed=lambda _: [0, 0, 0],
        **kw(env),
    )
    assert {h["id"] for h in hits} == {"a", "c"}

    # Directed edges conduct forward only: walking from the target finds nothing new.
    hits = vs.spreading_activation(
        UID, PID, "c",
        lenses=("topical", "declared"),
        layer_weights={"topical": 0.0},
        embed=lambda _: [0, 0, 0],
        **kw(env),
    )
    assert {h["id"] for h in hits} == {"c"}


def test_concept_clusters_glued_by_any_lens(env):
    put(env, "a", [1, 0, 0], text="alpha", semantics={"entities": ["acme"]})
    put(env, "a2", [0.9, 0.1, 0], text="alpha too")
    put(env, "c", [0, 1, 0], text="gamma", semantics={"entities": ["acme"]})

    topical_only = vs.concept_clusters(UID, PID, **kw(env))
    assert len(topical_only) == 1 and topical_only[0]["size"] == 2

    # The shared entity pulls the orthogonal memory into the same concept.
    glued = vs.concept_clusters(UID, PID, lenses=("topical", "entity"), **kw(env))
    assert len(glued) == 1 and glued[0]["size"] == 3


# --------------------------------------------------------------------------- #
# Relation inference: declared truth first, geometry after.                   #
# --------------------------------------------------------------------------- #
def test_infer_relation_declared_wins(env):
    put(env, "a", [1, 0, 0])
    put(env, "c", [0, 1, 0])
    vs.upsert_relations(UID, PID, [
        {"source": "a", "target": "c", "type": "causes"},
    ], **kw(env))

    # Geometry says unrelated; the declared edge overrides, either way asked.
    result = vs.infer_relation(UID, PID, "c", "a", **kw(env))
    assert result["relation"] == "causes" and result["source"] == "declared"
    assert result["direction"] == {"source": "a", "target": "c"}


def test_infer_relation_geometry_fallbacks(env):
    put(env, "a", [1, 0, 0])
    put(env, "dup", [0.999, 0.001, 0])
    put(env, "c", [0, 1, 0])
    put(env, "g", [0.5, 0.5, 0])  # sits at the store's centre: the general one

    assert vs.infer_relation(UID, PID, "a", "dup", **kw(env))["relation"] == "duplicate"
    assert vs.infer_relation(UID, PID, "a", "c", **kw(env))["relation"] == "unrelated"

    result = vs.infer_relation(UID, PID, "a", "g", **kw(env))
    assert result["relation"] == "specializes" and result["source"] == "geometry"
    assert result["direction"] == {"parent": "g", "child": "a"}
    assert vs.infer_relation(UID, PID, "a", "ghost", **kw(env))["relation"] == "unknown"


def test_everything_is_empty_without_a_store(env):
    absent = {"db": env.db, "qdrant": env.qdrant}
    assert vs.related_memories("nobody", PID, "a", **absent) == []
    assert vs.semantic_graph("nobody", PID, **absent) == {"nodes": [], "edges": []}
    assert vs.concept_clusters("nobody", PID, **absent) == []
    assert vs.spreading_activation("nobody", PID, "a", embed=lambda _: [], **absent) == []
    assert vs.infer_relation("nobody", PID, "a", "b", **absent)["relation"] == "unknown"
