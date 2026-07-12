"""Vector memory utility layer: semantic connections, layered relations, ontology.

Where ``memory.py`` treats each stored point as an isolated fact, this module
treats the whole ``(user, project)`` store as a *graph of meaning* and provides
the toolset for building, storing, and traversing that graph. It is a basis, not
a policy: nothing here decides *when* connections get built - callers (MCP
tools, background jobs, the connected agent) compose these primitives on demand.

Three sources of relational signal, kept honest about where each lives:

  * **Geometry** - cosine over stored vectors. Free, symmetric, topical. The
    always-available default; never more than a similarity signal.
  * **Payload** - deterministic facts attached to points: timestamps, extracted
    entities, the owner/deixis anchor. Cheap joins ("same company", "same
    week"), computed without re-embedding anything.
  * **Declared relations** - typed, possibly directed edges produced *outside*
    this service (by the client agent reasoning over memory texts: implicature,
    part-of, causality, deixis resolution...) and handed in as finished
    structures via ``upsert_relations``. This service validates and stores
    them; it never calls an LLM itself.

Everything semantic lives in a reserved ``_semantics`` namespace inside each
point's payload - Qdrant points are document-style, so the point *is* the
record: its vector, its user metadata, and its semantic annotations/edges
travel together. No second store, no cross-database invalidation.

    payload = {
        "text": ...,                     # what was embedded (memory.py)
        ...user metadata...,             # opaque, untouched
        "_semantics": {
            "owner": user_id,            # deixis anchor: whose "my"/"our"
            "stored_at": iso8601,        # deixis anchor: when "today" was
            "entities": ["acme corp"],   # extracted mentions (client-provided)
            "relations": [Edge, ...],    # declared outgoing edges (see below)
        },
    }

The common currency is the **edge** - every lens emits them, the graph and the
retrieval walk consume them:

    {"source": id, "target": id,
     "type": "topical" | "precedes" | "same_entity:acme" | "part_of" | ...,
     "layer": "topical" | "temporal" | "entity" | "declared" | ...,
     "directed": bool, "weight": float in (0, 1],
     "evidence": {...} | None}           # provenance for declared edges

Declared edges additionally carry two quality-mitigation properties (a wrong
declared relation quietly degrades every future connected recall, so declarers
must be able to hedge): ``confidence`` in (0, 1] - how sure the declaring agent
is; it scales the edge's traversal strength - and ``valid_till`` (ISO 8601,
optional) - after this moment the edge is expired and ignored by every read
path (lens, relations_of, infer_relation), so stale structure retires itself.

A **lens** is a strategy that derives edges from a set of points. Built-ins
cover the geometry and payload tiers (``topical``, ``temporal``, ``entity``)
plus ``declared`` (reads stored edges back out); new layers = new lenses in
``LENSES``, no changes to the graph/retrieval machinery.

Services-layer conventions apply: pure I/O, ``db``/``qdrant`` (and ``embed``
where free text is involved) injected, raw Qdrant access only via
``qdrant_store``. Pairwise lenses are O(n^2) and bounded by ``max_points``.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable

from pymongo.database import Database
from qdrant_client import QdrantClient

from app.services import collections, qdrant_store
from app.services.embeddings_remote import embed as default_embed

EmbedFn = Callable[[str], list[float]]
# A lens: (points with vectors+payload, params) -> edges. Registered in LENSES.
LensFn = Callable[..., list[dict]]

# Reserved payload namespace. memory.py stores {"text", **metadata}; everything
# this layer writes stays under this single key so user metadata is never touched.
SEMANTICS_KEY = "_semantics"

# Cosine similarity above which two memories are considered "connected".
# Cosine sits in [-1, 1]; 0.6 is deliberately loose so weak thematic links
# still surface - callers tighten it per use.
DEFAULT_LINK_THRESHOLD = 0.6
# At/above this the two memories are effectively the same fact.
DUPLICATE_THRESHOLD = 0.92
# Below this there is no meaningful relationship to reason about.
UNRELATED_THRESHOLD = 0.35
# Safety cap on pairwise passes (graph / clustering are O(n^2) in points).
DEFAULT_MAX_POINTS = 500
# Two memories stored within this window are "near in time" (temporal lens).
DEFAULT_TIME_WINDOW_S = 7 * 24 * 3600


# --------------------------------------------------------------------------- #
# Store-time annotation - the cheap, deterministic tier.                      #
# --------------------------------------------------------------------------- #
def annotate_store_payload(user_id: str, *, stored_at: datetime | None = None) -> dict:
    """The ``_semantics`` fragment every memory should carry from birth.

    The hybrid enrichment split: this is the "cheap now" half - just the deixis
    anchors (who "my" is, when "today" was) that are unrecoverable if not
    captured at write time. Deep extraction (entities, declared relations) comes
    later, from the client, via ``upsert_relations`` / ``annotate_memory``.
    Merge the result into the payload dict passed to ``qdrant_store.upsert_memory``.
    """
    stored_at = stored_at or datetime.now(timezone.utc)
    return {SEMANTICS_KEY: {"owner": user_id, "stored_at": stored_at.isoformat()}}


def annotate_memory(
    user_id: str,
    project_id: str,
    memory_id: str,
    annotations: dict,
    *,
    db: Database,
    qdrant: QdrantClient,
) -> bool:
    """Merge client-produced annotations into a memory's ``_semantics`` doc.

    The "deep later" half of the hybrid split: extracted entities, resolved
    times/places, aspect tags - whatever the client derived on demand. Merges
    key-by-key into the existing ``_semantics`` (``relations`` is managed by
    ``upsert_relations`` and is refused here). Returns False if the memory
    does not exist.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return False
    if "relations" in annotations:
        raise ValueError("relations are managed via upsert_relations")

    current = _semantics_of(record["name"], memory_id, qdrant=qdrant)
    if current is None:
        return False
    current.update(annotations)
    return qdrant_store.set_payload(
        record["name"], memory_id, {SEMANTICS_KEY: current}, client=qdrant
    )


# --------------------------------------------------------------------------- #
# Declared relations - client-built structures in, point payloads enriched.   #
# --------------------------------------------------------------------------- #
def upsert_relations(
    user_id: str,
    project_id: str,
    relations: list[dict],
    *,
    db: Database,
    qdrant: QdrantClient,
) -> dict:
    """Validate and store typed relations built by the client.

    The ingestion end of the client-side reasoning loop: the connected agent
    (when its setting allows, or on explicit demand) reads memory texts,
    infers relations - implicature, part-of, causal, deixis-resolved entity
    links - and hands the finished structures here. Each relation needs
    ``source``, ``target`` and ``type``; ``directed`` defaults to True (most
    declared relations are asymmetric), ``weight`` to 1.0, ``layer`` to
    "declared". Two optional hedging properties mitigate bad declarations:
    ``confidence`` in (0, 1] (default 1.0) - how sure the declarer is; scales
    the edge's traversal strength - and ``valid_till`` (ISO 8601) - the edge
    expires at that moment and stops influencing reads. Both endpoints must
    exist. Edges live on their *source* point under ``_semantics.relations``,
    deduplicated by (target, type) - re-declaring a relation updates it.

    Returns ``{"stored": n, "rejected": [{relation, reason}, ...]}``.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return {"stored": 0, "rejected": [
            {"relation": r, "reason": "no memory store"} for r in relations
        ]}
    name = record["name"]

    # One existence check for every endpoint referenced in the batch.
    ids = {str(r.get("source")) for r in relations} | {str(r.get("target")) for r in relations}
    ids.discard("None")
    existing = {
        str(p.id)
        for p in qdrant_store.retrieve_points(name, sorted(ids), with_vectors=False, client=qdrant)
    }

    stored = 0
    rejected: list[dict] = []
    by_source: dict[str, list[dict]] = {}
    for rel in relations:
        source, target, rtype = rel.get("source"), rel.get("target"), rel.get("type")
        if not source or not target or not rtype:
            rejected.append({"relation": rel, "reason": "source, target and type are required"})
            continue
        if str(source) == str(target):
            rejected.append({"relation": rel, "reason": "self-relation"})
            continue
        if str(source) not in existing or str(target) not in existing:
            rejected.append({"relation": rel, "reason": "unknown memory id"})
            continue
        weight = float(rel.get("weight", 1.0))
        if not 0.0 < weight <= 1.0:
            rejected.append({"relation": rel, "reason": "weight must be in (0, 1]"})
            continue
        confidence = float(rel.get("confidence", 1.0))
        if not 0.0 < confidence <= 1.0:
            rejected.append({"relation": rel, "reason": "confidence must be in (0, 1]"})
            continue
        valid_till = rel.get("valid_till")
        if valid_till is not None:
            parsed = _parse_ts(valid_till)
            if parsed is None:
                rejected.append(
                    {"relation": rel, "reason": "valid_till must be an ISO 8601 timestamp"}
                )
                continue
            valid_till = parsed.isoformat()
        by_source.setdefault(str(source), []).append(
            {
                "target": str(target),
                "type": str(rtype),
                "layer": str(rel.get("layer", "declared")),
                "directed": bool(rel.get("directed", True)),
                "weight": weight,
                "confidence": confidence,
                "valid_till": valid_till,
                "evidence": rel.get("evidence"),
                "declared_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    for source, new_edges in by_source.items():
        semantics = _semantics_of(name, source, qdrant=qdrant) or {}
        kept = {
            (e["target"], e["type"]): e for e in semantics.get("relations", [])
        }
        for edge in new_edges:
            kept[(edge["target"], edge["type"])] = edge
        semantics["relations"] = list(kept.values())
        qdrant_store.set_payload(name, source, {SEMANTICS_KEY: semantics}, client=qdrant)
        stored += len(new_edges)

    return {"stored": stored, "rejected": rejected}


def remove_relations(
    user_id: str,
    project_id: str,
    selectors: list[dict],
    *,
    db: Database,
    qdrant: QdrantClient,
) -> dict:
    """Remove declared relations - the corrective twin of ``upsert_relations``.

    Agents trusted to create structure need the symmetric power to correct it:
    a relation that turned out to be *wrong* (not merely stale - ``valid_till``
    handles stale) must be removable. Each selector needs ``source`` and
    ``target`` (as declared - edges live on their source point); ``type``
    narrows to one relation, omitting it removes every relation from source to
    target. Removal is physical: the edge and its provenance are gone.

    Returns ``{"removed": n, "rejected": [{relation, reason}, ...]}``.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return {"removed": 0, "rejected": [
            {"relation": s, "reason": "no memory store"} for s in selectors
        ]}
    name = record["name"]

    rejected: list[dict] = []
    by_source: dict[str, list[tuple[str, str | None]]] = {}
    for sel in selectors:
        source, target = sel.get("source"), sel.get("target")
        if not source or not target:
            rejected.append({"relation": sel, "reason": "source and target are required"})
            continue
        by_source.setdefault(str(source), []).append(
            (str(target), str(sel["type"]) if sel.get("type") else None)
        )

    removed = 0
    for source, wanted in by_source.items():
        semantics = _semantics_of(name, source, qdrant=qdrant)
        if semantics is None:
            rejected.extend(
                {"relation": {"source": source, "target": t, "type": ty}, "reason": "unknown memory id"}
                for t, ty in wanted
            )
            continue
        edges = semantics.get("relations", [])
        kept = [
            e for e in edges
            if not any(
                str(e.get("target")) == t and (ty is None or e.get("type") == ty)
                for t, ty in wanted
            )
        ]
        if len(kept) != len(edges):
            semantics["relations"] = kept
            qdrant_store.set_payload(name, source, {SEMANTICS_KEY: semantics}, client=qdrant)
            removed += len(edges) - len(kept)

    return {"removed": removed, "rejected": rejected}


def prune_relations_to(
    user_id: str,
    project_id: str,
    memory_id: str,
    *,
    max_points: int = DEFAULT_MAX_POINTS,
    db: Database,
    qdrant: QdrantClient,
) -> int:
    """Drop every declared edge targeting ``memory_id``; returns how many.

    The hygiene hook for memory deletion: edges live on their *source* points,
    so deleting a memory would otherwise leave dangling relations aimed at a
    point that no longer exists. ``memory.delete_memory`` calls this after a
    successful delete. Bounded scan (see the README perf memo: a payload index
    on ``_semantics.relations[].target`` is the escalation path).
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return 0
    name = record["name"]

    pruned = 0
    for point in qdrant_store.scroll_points(
        name, with_vectors=False, limit=max_points, client=qdrant
    ):
        semantics = dict((point.payload or {}).get(SEMANTICS_KEY) or {})
        edges = semantics.get("relations", [])
        kept = [e for e in edges if str(e.get("target")) != str(memory_id)]
        if len(kept) != len(edges):
            semantics["relations"] = kept
            qdrant_store.set_payload(name, point.id, {SEMANTICS_KEY: semantics}, client=qdrant)
            pruned += len(edges) - len(kept)
    return pruned


def relations_of(
    user_id: str,
    project_id: str,
    memory_id: str,
    *,
    types: list[str] | None = None,
    include_incoming: bool = False,
    include_expired: bool = False,
    max_points: int = DEFAULT_MAX_POINTS,
    db: Database,
    qdrant: QdrantClient,
) -> list[dict]:
    """Declared relations touching one memory, as full edge dicts.

    Outgoing edges are read straight off the point; ``include_incoming`` also
    scans the store (bounded by ``max_points``) for edges pointing back at it.
    ``types`` filters by relation type. Edges past their ``valid_till`` are
    dropped unless ``include_expired`` is set (inspection/cleanup use).
    Returns ``[]`` for unknown ids.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return []
    name = record["name"]

    semantics = _semantics_of(name, memory_id, qdrant=qdrant)
    if semantics is None:
        return []
    edges = [
        {"source": str(memory_id), **e} for e in semantics.get("relations", [])
    ]

    if include_incoming:
        for point in qdrant_store.scroll_points(
            name, with_vectors=False, limit=max_points, client=qdrant
        ):
            for e in ((point.payload or {}).get(SEMANTICS_KEY) or {}).get("relations", []):
                if str(e.get("target")) == str(memory_id) and str(point.id) != str(memory_id):
                    edges.append({"source": str(point.id), **e})

    if not include_expired:
        edges = [e for e in edges if not _expired(e)]

    if types is not None:
        wanted = set(types)
        edges = [e for e in edges if e["type"] in wanted]
    return edges


# --------------------------------------------------------------------------- #
# Lenses - one per semantic layer, all emitting the same edge shape.          #
# --------------------------------------------------------------------------- #
def topical_lens(points: list, *, min_score: float = DEFAULT_LINK_THRESHOLD) -> list[dict]:
    """Geometry tier: an undirected edge per pair above cosine ``min_score``."""
    edges: list[dict] = []
    for i in range(len(points)):
        vi = points[i].vector
        for j in range(i + 1, len(points)):
            score = _cosine(vi, points[j].vector)
            if score >= min_score:
                edges.append(_edge(points[i].id, points[j].id, "topical", "topical", score))
    return edges


def temporal_lens(points: list, *, window_seconds: int = DEFAULT_TIME_WINDOW_S) -> list[dict]:
    """Payload tier: directed ``precedes`` edges between memories stored close in time.

    Reads the ``stored_at`` anchor (see ``annotate_store_payload``); memories
    without one are skipped. Weight decays linearly across the window, so
    same-session memories bind tightly and week-apart ones barely.
    """
    stamped = []
    for p in points:
        ts = _parse_ts((((p.payload or {}).get(SEMANTICS_KEY)) or {}).get("stored_at"))
        if ts is not None:
            stamped.append((ts, p))
    stamped.sort(key=lambda pair: pair[0])

    edges: list[dict] = []
    for i, (ti, pi) in enumerate(stamped):
        for tj, pj in stamped[i + 1:]:
            gap = (tj - ti).total_seconds()
            if gap > window_seconds:
                break  # sorted: everything further is outside the window too
            weight = max(1.0 - gap / window_seconds, 1e-6)
            edges.append(_edge(pi.id, pj.id, "precedes", "temporal", weight, directed=True))
    return edges


def entity_lens(points: list) -> list[dict]:
    """Payload tier: undirected ``same_entity:<value>`` edges.

    Joins on the ``_semantics.entities`` list (client-extracted mentions,
    normalised strings - "same company", "same person", "same place" all reduce
    to sharing a value here). Deterministic; no vectors involved.
    """
    by_entity: dict[str, list] = {}
    for p in points:
        for entity in (((p.payload or {}).get(SEMANTICS_KEY)) or {}).get("entities", []):
            by_entity.setdefault(str(entity).strip().lower(), []).append(p)

    edges: list[dict] = []
    for entity, members in by_entity.items():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                edges.append(
                    _edge(members[i].id, members[j].id, f"same_entity:{entity}", "entity", 1.0)
                )
    return edges


def declared_lens(points: list) -> list[dict]:
    """Declared tier: read the client-built edges back off the points.

    A declarer's hedges are honoured here: traversal strength is
    ``weight * confidence`` (an unsure link conducts weakly), and edges past
    their ``valid_till`` are skipped entirely - expired structure stops
    influencing recall without needing deletion.
    """
    edges: list[dict] = []
    for p in points:
        for e in (((p.payload or {}).get(SEMANTICS_KEY)) or {}).get("relations", []):
            if _expired(e):
                continue
            edge = _edge(
                p.id, e["target"], e["type"], e.get("layer", "declared"),
                float(e.get("weight", 1.0)) * float(e.get("confidence", 1.0)),
                directed=bool(e.get("directed", True)),
                evidence=e.get("evidence"),
            )
            edge["confidence"] = e.get("confidence", 1.0)
            edge["valid_till"] = e.get("valid_till")
            edges.append(edge)
    return edges


# The lens registry: adding a semantic layer = registering a lens here. The
# graph, clustering, and activation machinery never changes.
LENSES: dict[str, LensFn] = {
    "topical": topical_lens,
    "temporal": temporal_lens,
    "entity": entity_lens,
    "declared": declared_lens,
}


# --------------------------------------------------------------------------- #
# The multigraph - every requested layer merged over one point set.           #
# --------------------------------------------------------------------------- #
def semantic_graph(
    user_id: str,
    project_id: str,
    *,
    lenses: tuple[str, ...] = ("topical",),
    lens_params: dict[str, dict] | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
    db: Database,
    qdrant: QdrantClient,
) -> dict:
    """Build the store as a typed multigraph across the requested layers.

    Scrolls the points once, runs each named lens over them, and merges the
    edges (strongest first). ``lens_params`` passes per-lens keyword arguments,
    e.g. ``{"topical": {"min_score": 0.75}, "temporal": {"window_seconds": 3600}}``.
    Returns ``{"nodes": [{id, text}], "edges": [edge, ...]}`` - the raw material
    for visualisation, clustering, and connection-based retrieval.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return {"nodes": [], "edges": []}

    points = qdrant_store.scroll_points(record["name"], limit=max_points, client=qdrant)
    nodes = [{"id": p.id, "text": (p.payload or {}).get("text", "")} for p in points]

    params = lens_params or {}
    edges: list[dict] = []
    for lens_name in lenses:
        lens = LENSES.get(lens_name)
        if lens is None:
            raise ValueError(f"unknown lens: {lens_name!r} (have: {sorted(LENSES)})")
        edges.extend(lens(points, **params.get(lens_name, {})))
    edges.sort(key=lambda e: e["weight"], reverse=True)
    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# Retrieval by connection - activation spreading over the multigraph.         #
# --------------------------------------------------------------------------- #
def spreading_activation(
    user_id: str,
    project_id: str,
    seed: str,
    *,
    lenses: tuple[str, ...] = ("topical",),
    lens_params: dict[str, dict] | None = None,
    layer_weights: dict[str, float] | None = None,
    hops: int = 2,
    limit: int = 10,
    fanout: int = 5,
    min_score: float = DEFAULT_LINK_THRESHOLD,
    decay: float = 0.5,
    max_points: int = DEFAULT_MAX_POINTS,
    db: Database,
    qdrant: QdrantClient,
    embed: EmbedFn = default_embed,
) -> list[dict]:
    """Retrieve by connection: walk the semantic multigraph outward from a seed.

    ``seed`` is an existing memory id (its stored vector anchors the walk) or a
    free-text query (embedded on the fly). Activation spreads hop by hop along
    edges from the requested ``lenses``, multiplied by edge weight, the edge's
    layer weight, and ``decay`` per hop - so a memory two links away still
    surfaces but ranks below its closer connectors, and a query can privilege
    layers ("follow entity links strongly, topical weakly") via
    ``layer_weights``, e.g. ``{"entity": 1.0, "topical": 0.4}``. Directed edges
    conduct forward only. Returns memories ranked by accumulated activation.

    The pure-topical walk (the default) uses ANN neighbour queries and never
    scrolls the store; any other lens mix builds the multigraph first (bounded
    by ``max_points``).
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return []
    name = record["name"]

    if set(lenses) == {"topical"}:
        return _activate_ann(
            name, seed, hops=hops, limit=limit, fanout=fanout,
            min_score=min_score, decay=decay, qdrant=qdrant, embed=embed,
        )

    graph = semantic_graph(
        user_id, project_id, lenses=lenses, lens_params=lens_params,
        max_points=max_points, db=db, qdrant=qdrant,
    )
    weights = layer_weights or {}
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for e in graph["edges"]:
        conduct = e["weight"] * weights.get(e["layer"], 1.0)
        if conduct <= 0:
            continue
        adjacency.setdefault(str(e["source"]), []).append((str(e["target"]), conduct))
        if not e["directed"]:
            adjacency.setdefault(str(e["target"]), []).append((str(e["source"]), conduct))

    # Seed: an existing id, or embed the text and take the nearest points.
    known = {str(n["id"]) for n in graph["nodes"]}
    if str(seed) in known:
        frontier = {str(seed): 1.0}
    else:
        hits = qdrant_store.search(name, embed(seed), limit=fanout, client=qdrant)
        frontier = {str(h.id): h.score for h in hits if h.score >= min_score}

    activation = dict(frontier)
    for _ in range(hops):
        next_frontier: dict[str, float] = {}
        for node_id, energy in frontier.items():
            for other, conduct in adjacency.get(node_id, []):
                gained = energy * conduct * decay
                activation[other] = activation.get(other, 0.0) + gained
                next_frontier[other] = max(next_frontier.get(other, 0.0), gained)
        frontier = next_frontier
        if not frontier:
            break

    texts = {str(n["id"]): n["text"] for n in graph["nodes"]}
    ranked = sorted(activation.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"id": nid, "activation": score, "text": texts.get(nid, "")}
        for nid, score in ranked[:limit]
    ]


# --------------------------------------------------------------------------- #
# Ontology: emergent concepts + relation classification.                      #
# --------------------------------------------------------------------------- #
def concept_clusters(
    user_id: str,
    project_id: str,
    *,
    lenses: tuple[str, ...] = ("topical",),
    lens_params: dict[str, dict] | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
    min_size: int = 2,
    db: Database,
    qdrant: QdrantClient,
) -> list[dict]:
    """Group memories into emergent concepts: connected components of the graph.

    Ontology from structure rather than declared categories: memories linked
    (directly or transitively) through any requested lens form one concept, so
    ``lenses=("topical", "entity")`` yields concepts glued by *either* meaning
    or shared entities. The member closest to the cluster centroid is the
    representative label. Returns clusters (largest first) as
    ``{representative, size, members}``; singletons drop out via ``min_size``.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return []

    points = qdrant_store.scroll_points(record["name"], limit=max_points, client=qdrant)
    index = {str(p.id): i for i, p in enumerate(points)}
    n = len(points)

    params = lens_params or {}
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for lens_name in lenses:
        lens = LENSES.get(lens_name)
        if lens is None:
            raise ValueError(f"unknown lens: {lens_name!r} (have: {sorted(LENSES)})")
        for e in lens(points, **params.get(lens_name, {})):
            i, j = index.get(str(e["source"])), index.get(str(e["target"]))
            if i is not None and j is not None:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        centroid = _centroid([points[m].vector for m in members])
        rep = max(members, key=lambda m: _cosine(points[m].vector, centroid))
        clusters.append(
            {
                "representative": {
                    "id": points[rep].id,
                    "text": (points[rep].payload or {}).get("text", ""),
                },
                "size": len(members),
                "members": [
                    {"id": points[m].id, "text": (points[m].payload or {}).get("text", "")}
                    for m in members
                ],
            }
        )
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


def infer_relation(
    user_id: str,
    project_id: str,
    memory_a: str,
    memory_b: str,
    *,
    max_points: int = DEFAULT_MAX_POINTS,
    db: Database,
    qdrant: QdrantClient,
) -> dict:
    """The relationship between two memories: declared truth first, geometry after.

    If the client has declared a relation between the pair (either direction),
    that edge is authoritative and returned as-is with ``source: "declared"``.
    Otherwise falls back to the geometric default (``source: "geometry"``):

      * ``duplicate``   - near-identical (>= ``DUPLICATE_THRESHOLD``).
      * ``specializes`` / ``generalizes`` - related, and one is more *general*:
        generality read as closeness to the store centroid (a broad memory sits
        nearer the centre of everything stored than a specific one) - the seed
        of an is-a hierarchy, heuristic by design.
      * ``related``     - connected but neither clearly subsumes the other.
      * ``unrelated``   - below ``UNRELATED_THRESHOLD``.

    Returns ``{relation, similarity, direction, source}``; ``relation ==
    "unknown"`` if either id is missing.
    """
    unknown = {"relation": "unknown", "similarity": 0.0, "direction": None, "source": None}
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return unknown
    name = record["name"]

    fetched = {
        str(p.id): p
        for p in qdrant_store.retrieve_points(name, [memory_a, memory_b], client=qdrant)
    }
    pa, pb = fetched.get(str(memory_a)), fetched.get(str(memory_b))
    if pa is None or pb is None:
        return unknown

    # Live declared edges outrank anything geometry can say (expired ones
    # have retired: they fall through to the geometric fallback).
    for point, other in ((pa, memory_b), (pb, memory_a)):
        for e in (((point.payload or {}).get(SEMANTICS_KEY)) or {}).get("relations", []):
            if str(e.get("target")) == str(other) and not _expired(e):
                return {
                    "relation": e["type"],
                    "similarity": _cosine(pa.vector, pb.vector),
                    "direction": (
                        {"source": str(point.id), "target": str(other)}
                        if e.get("directed", True) else None
                    ),
                    "confidence": e.get("confidence", 1.0),
                    "source": "declared",
                }

    similarity = _cosine(pa.vector, pb.vector)
    result: dict = {
        "relation": "related", "similarity": similarity,
        "direction": None, "source": "geometry",
    }
    if similarity >= DUPLICATE_THRESHOLD:
        result["relation"] = "duplicate"
        return result
    if similarity < UNRELATED_THRESHOLD:
        result["relation"] = "unrelated"
        return result

    # Related: decide subsumption by which memory is more central (== more general).
    points = qdrant_store.scroll_points(name, limit=max_points, client=qdrant)
    centroid = _centroid([p.vector for p in points]) if points else None
    if centroid is not None:
        gen_a, gen_b = _cosine(pa.vector, centroid), _cosine(pb.vector, centroid)
        if abs(gen_a - gen_b) >= 0.05:  # meaningful gap, else just "related"
            if gen_a > gen_b:
                result["relation"] = "generalizes"  # a is broader -> b specializes a
                result["direction"] = {"parent": memory_a, "child": memory_b}
            else:
                result["relation"] = "specializes"  # a is narrower than b
                result["direction"] = {"parent": memory_b, "child": memory_a}
    return result


def related_memories(
    user_id: str,
    project_id: str,
    memory_id: str,
    *,
    limit: int = 10,
    min_score: float = DEFAULT_LINK_THRESHOLD,
    db: Database,
    qdrant: QdrantClient,
) -> list[dict]:
    """Memories topically connected to one memory, closest first.

    The atomic geometric edge, answered by ANN with the memory's *own* stored
    vector (no re-embedding, no scroll). The seed itself is dropped; anything
    below ``min_score`` is filtered. Returns ``[]`` for unknown stores/ids.
    """
    record = collections.get_collection(user_id, project_id, db=db)
    if record is None:
        return []

    hits = qdrant_store.neighbors(record["name"], memory_id, limit=limit, client=qdrant)
    out: list[dict] = []
    for hit in hits:
        if str(hit.id) == str(memory_id) or hit.score < min_score:
            continue
        out.append({"id": hit.id, "score": hit.score, "payload": hit.payload})
    return out[:limit]


# --------------------------------------------------------------------------- #
# Internals.                                                                  #
# --------------------------------------------------------------------------- #
def _activate_ann(
    name: str,
    seed: str,
    *,
    hops: int,
    limit: int,
    fanout: int,
    min_score: float,
    decay: float,
    qdrant: QdrantClient,
    embed: EmbedFn,
) -> list[dict]:
    """Topical-only activation walk over live ANN queries (no store scroll)."""
    seed_hit = qdrant_store.retrieve_points(name, [seed], with_vectors=False, client=qdrant)
    if seed_hit:
        frontier = {str(seed): 1.0}
    else:
        hits = qdrant_store.search(name, embed(seed), limit=fanout, client=qdrant)
        frontier = {str(h.id): h.score for h in hits if h.score >= min_score}

    activation: dict[str, float] = dict(frontier)
    payloads: dict[str, dict] = {}
    for _ in range(hops):
        next_frontier: dict[str, float] = {}
        for node_id, energy in frontier.items():
            for hit in qdrant_store.neighbors(name, node_id, limit=fanout, client=qdrant):
                nid = str(hit.id)
                if nid == node_id or hit.score < min_score:
                    continue
                payloads[nid] = hit.payload
                gained = energy * hit.score * decay
                activation[nid] = activation.get(nid, 0.0) + gained
                next_frontier[nid] = max(next_frontier.get(nid, 0.0), gained)
        frontier = next_frontier
        if not frontier:
            break

    ranked = sorted(activation.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"id": nid, "activation": score, "payload": payloads.get(nid)}
        for nid, score in ranked[:limit]
    ]


def _semantics_of(collection: str, point_id: str, *, qdrant: QdrantClient) -> dict | None:
    """The ``_semantics`` doc of one point (empty dict if unset); None if missing."""
    fetched = qdrant_store.retrieve_points(
        collection, [point_id], with_vectors=False, client=qdrant
    )
    if not fetched:
        return None
    return dict((fetched[0].payload or {}).get(SEMANTICS_KEY) or {})


def _edge(
    source, target, rtype: str, layer: str, weight: float,
    *, directed: bool = False, evidence: dict | None = None,
) -> dict:
    return {
        "source": str(source), "target": str(target), "type": rtype,
        "layer": layer, "directed": directed, "weight": weight, "evidence": evidence,
    }


def _expired(edge: dict) -> bool:
    """True once an edge's ``valid_till`` has passed; edges without one never expire."""
    till = _parse_ts(edge.get("valid_till"))
    return till is not None and till <= datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors; 0.0 for a zero/empty vector."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _centroid(vectors: list[list[float]]) -> list[float] | None:
    """Component-wise mean of a list of equal-length vectors."""
    vectors = [v for v in vectors if v]
    if not vectors:
        return None
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    return [c / len(vectors) for c in acc]
