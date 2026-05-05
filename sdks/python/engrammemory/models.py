"""Typed request/response models for the Engram cloud API.

Plain ``dataclasses`` — no pydantic dependency. The bridge package
already pins ``httpx>=0.25`` and ``pyyaml>=6.0`` and we're deliberately
staying within that dependency footprint so the SDK ships as a
single-file install for anyone who already has the bridge.

Every model here mirrors a real request or response shape defined in
``engram-cloud-api/api.py``. Cross-references live next to the class
so future drift is catchable by grepping for the endpoint name. When
the cloud adds a field we don't know about yet, :func:`_strip` keeps
the constructor from blowing up — unknown keys are dropped silently
on ingress so an old SDK still works against a newer API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional


def _strip(cls: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a dict containing only the fields ``cls`` knows about.

    Used by every ``from_dict`` classmethod so that a newer cloud
    response (extra fields) doesn't crash an older SDK. We deliberately
    do the filter here rather than with ``__init_subclass__`` or a
    metaclass — explicit is cheaper to debug than clever.
    """
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in raw.items() if k in known}


# ─── Store ───────────────────────────────────────────────────────────
# /v1/store → StoreRequest / StoreResponse (api.py L602-L619)


@dataclass
class StoreRequest:
    """Body for ``POST /v1/store``.

    ``text`` is the only required field. ``category`` defaults to
    ``"other"`` server-side if omitted.
    """

    text: str
    category: Optional[str] = None
    importance: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None
    collection: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Drop ``None`` fields so the server sees omission, not null.

        The cloud's pydantic model treats "missing" and "null"
        differently for a few defaulted fields (e.g. ``category``
        falls back to ``"other"`` only when truly absent), so we
        preserve that distinction here instead of shipping a flat
        ``asdict`` payload.
        """
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None}


@dataclass
class StoreResponse:
    """Response shape for ``POST /v1/store`` (api.py L614)."""

    id: str
    status: str
    category: str
    duplicate: bool
    message: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StoreResponse":
        return cls(**_strip(cls, raw))


# ─── Search ──────────────────────────────────────────────────────────
# /v1/search → SearchRequest / SearchResponse (api.py L622-L680)


@dataclass
class SearchRequest:
    """Body for ``POST /v1/search``.

    Note the server calls the result count ``limit`` — not
    ``top_k``. The SDK exposes ``top_k`` in the public method for
    ergonomics and maps it to ``limit`` here at the wire boundary.

    ``scope`` defaults to ``"personal"`` and controls which physical
    collection the search runs against. ``"hive:<hive_id>"`` searches
    a hive collection after the cloud verifies membership. Wave 3
    intentionally exposes no "merged" mode — callers wanting both
    personal and hive results must issue two searches and dedupe
    client-side, which keeps the cost model transparent.
    """

    query: str
    limit: Optional[int] = None
    category: Optional[str] = None
    min_score: Optional[float] = None
    min_importance: Optional[float] = None
    collection: Optional[str] = None
    scope: Optional[str] = None
    queries: Optional[List[str]] = None

    def to_payload(self) -> Dict[str, Any]:
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None}


@dataclass
class SearchResult:
    """One row in a SearchResponse (api.py L666-L675).

    ``tier`` is one of ``"hot"``, ``"hash"``, ``"vector"`` and is
    useful for telemetry / debugging — it reveals which recall tier
    served the hit. Newer API versions may add tiers; callers should
    treat this as an opaque string.
    """

    id: str
    text: str
    category: str
    importance: float
    score: float
    timestamp: str
    confidence: Optional[str] = None
    match_context: Optional[str] = None
    tier: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SearchResult":
        return cls(**_strip(cls, raw))


@dataclass
class SearchResponse:
    """Envelope returned by ``POST /v1/search`` (api.py L678)."""

    results: List[SearchResult] = field(default_factory=list)
    query_tokens: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SearchResponse":
        results_raw = raw.get("results") or []
        results = [
            SearchResult.from_dict(item)
            for item in results_raw
            if isinstance(item, Mapping)
        ]
        tokens = int(raw.get("query_tokens") or 0)
        return cls(results=results, query_tokens=tokens)


# ─── Forget ──────────────────────────────────────────────────────────
# /v1/forget (api.py L683-L686, L2748-L2834)
# Note: the cloud returns an ad-hoc dict, not a declared response model.
# Shape depends on whether the delete was by id, by query-match, or a
# miss. See api.py L2784 / L2828 / L2794.


@dataclass
class ForgetRequest:
    """Body for ``POST /v1/forget``. At least one of ``memory_id`` or
    ``query`` must be set or the server responds with HTTP 400. The
    SDK enforces this at the call site, not here — keeping the model
    a dumb data carrier means it stays useful for debugging."""

    memory_id: Optional[str] = None
    query: Optional[str] = None
    collection: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None}


@dataclass
class ForgetResponse:
    """Response for ``POST /v1/forget``.

    Three possible server shapes, unified here:
      - by id success: ``{"status": "deleted", "id": <id>}``
      - by query success: ``{"status": "deleted", "id": <id>, "text": <preview>}``
      - no match: ``{"status": "not_found", "message": <...>}``

    ``found`` is a derived convenience — ``True`` iff ``status ==
    "deleted"`` — so callers don't have to string-compare.
    """

    status: str
    id: Optional[str] = None
    text: Optional[str] = None
    message: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.status == "deleted"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ForgetResponse":
        return cls(**_strip(cls, raw))


# ─── Feedback ────────────────────────────────────────────────────────
# /v1/feedback (api.py L2698-L2743)


@dataclass
class FeedbackRequest:
    """Body for ``POST /v1/feedback``.

    ``selected_ids`` are memory ids the caller kept in its final
    context. ``rejected_ids`` are ones it looked at and dropped. The
    cloud engine folds both into hot-tier boosts, rejection
    penalties, and PREFERRED_OVER graph edges — it's the cheapest way
    to earn back R@1 since the judgment comes from the caller's model
    for free.
    """

    query: str
    selected_ids: List[str] = field(default_factory=list)
    rejected_ids: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "selected_ids": list(self.selected_ids),
            "rejected_ids": list(self.rejected_ids),
        }


@dataclass
class FeedbackResponse:
    """Response for ``POST /v1/feedback``.

    The cloud returns an ad-hoc dict with counters for what the engine
    did: how many ids it boosted, how many it penalized, how many
    graph edges it added. ``success`` is the server's own summary
    flag.
    """

    success: bool = True
    boosted: int = 0
    penalized: int = 0
    edges_added: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FeedbackResponse":
        return cls(
            success=bool(raw.get("success", True)),
            boosted=int(raw.get("boosted") or 0),
            penalized=int(raw.get("penalized") or 0),
            edges_added=int(raw.get("edges_added") or 0),
        )


# ─── Hives ───────────────────────────────────────────────────────────
# /v1/hives, /v1/hives/{hive_id}/members (api.py L647-L663, L2871+)


@dataclass
class HiveResponse:
    """One hive row from ``POST /v1/hives`` or ``GET /v1/hives``.

    ``role`` is only set on list responses — create returns the
    newly-authored hive without echoing the caller's role (the caller
    is always ``owner`` post-create). ``created_at`` is an ISO-8601
    string on the wire; we keep it as ``str`` so dataclasses stay free
    of ``datetime`` parsing surprises across stdlib versions.
    """

    id: str
    name: str
    slug: str
    owner_user_id: str
    created_at: Optional[str] = None
    member_count: int = 0
    role: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HiveResponse":
        return cls(**_strip(cls, raw))


# ─── Health ──────────────────────────────────────────────────────────
# /v1/health (api.py L737-L745)


@dataclass
class HealthResponse:
    """Authenticated health snapshot from ``GET /v1/health``.

    ``api`` / ``embedding`` / ``qdrant`` are short status strings
    (``"ok"``, ``"error (503)"``, ``"unreachable (...)"``). A caller
    that just wants a boolean should check ``api == "ok"`` — that's
    the value most indicative of "the SDK can continue".
    """

    api: str
    embedding: str
    qdrant: str
    qdrant_url: str
    uptime_seconds: int
    version: str
    environment: str

    @property
    def ok(self) -> bool:
        return self.api == "ok"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HealthResponse":
        return cls(**_strip(cls, raw))


__all__ = [
    "StoreRequest",
    "StoreResponse",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
    "ForgetRequest",
    "ForgetResponse",
    "FeedbackRequest",
    "FeedbackResponse",
    "HiveResponse",
    "HealthResponse",
]
