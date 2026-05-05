"""High-level clients for the Engram cloud API.

Two clients are exported:

- :class:`EngramClient` — blocking, suitable for scripts, CLI tools,
  jobs, and any synchronous code that just wants to call an API.
- :class:`AsyncEngramClient` — async, built on ``httpx.AsyncClient``,
  suitable for web servers, FastAPI apps, and anything already inside
  an event loop.

The two classes are intentionally mirror images. Every public method
on one has a one-for-one counterpart on the other with the same
arguments and the same return type. That keeps docs and examples
equivalent and makes it trivial for an app to swap between them (e.g.
a sync worker promoted to async FastAPI handler).

All non-trivial logic — retries, error classification, header
injection — lives in :mod:`engram._http`. The clients here are
deliberately thin glue: build a request dict, send it, wrap the
response in a dataclass. Nothing else.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ._http import (
    AsyncTransport,
    SDK_VERSION,
    SyncTransport,
    TransportConfig,
)
from .exceptions import EngramValidationError
from .models import (
    FeedbackRequest,
    FeedbackResponse,
    ForgetRequest,
    ForgetResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    StoreRequest,
    StoreResponse,
    HiveResponse,
)

DEFAULT_BASE_URL = "https://api.engrammemory.ai"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.5


def _resolve_api_key(explicit: Optional[str]) -> str:
    """Resolve the api key from explicit argument or environment.

    Explicit value wins. Empty string is treated as unset — a classic
    way users get a confusing 401 is when their CI defines the env var
    but leaves it blank.
    """
    if explicit:
        return explicit
    env = os.environ.get("ENGRAM_API_KEY", "")
    if env:
        return env
    raise EngramValidationError(
        "No API key provided. Pass api_key= to the client constructor "
        "or set the ENGRAM_API_KEY environment variable."
    )


def _build_config(
    api_key: Optional[str],
    base_url: str,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
) -> TransportConfig:
    """Normalize constructor arguments into an immutable config.

    Split out so both sync and async clients share the exact same
    validation — the cloud hive flagged a case last quarter where the
    bridge and the cloud had different ideas about what a valid
    ``base_url`` looked like, which produced great log lines and no
    useful error at the caller. Single chokepoint fixes that.
    """
    resolved_key = _resolve_api_key(api_key)
    if not base_url:
        raise EngramValidationError("base_url must be a non-empty URL")
    if timeout <= 0:
        raise EngramValidationError("timeout must be > 0")
    if max_retries < 0:
        raise EngramValidationError("max_retries must be >= 0")
    if retry_backoff < 0:
        raise EngramValidationError("retry_backoff must be >= 0")
    return TransportConfig(
        api_key=resolved_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
    )


def _coerce_store_response(raw: Any) -> StoreResponse:
    """Parse a store response or raise a useful error.

    The cloud always returns a JSON object for this endpoint, but if
    the transport ever hands us text (e.g. a proxy ate the body), we
    want a precise exception rather than an attribute error three
    frames deep.
    """
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/store, got {}".format(type(raw).__name__)
        )
    return StoreResponse.from_dict(raw)


def _coerce_search_response(raw: Any) -> SearchResponse:
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/search, got {}".format(type(raw).__name__)
        )
    return SearchResponse.from_dict(raw)


def _coerce_forget_response(raw: Any) -> ForgetResponse:
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/forget, got {}".format(type(raw).__name__)
        )
    return ForgetResponse.from_dict(raw)


def _coerce_feedback_response(raw: Any) -> FeedbackResponse:
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/feedback, got {}".format(type(raw).__name__)
        )
    return FeedbackResponse.from_dict(raw)


def _coerce_hive_response(raw: Any) -> HiveResponse:
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/hives, got {}".format(type(raw).__name__)
        )
    return HiveResponse.from_dict(raw)


def _coerce_hive_list(raw: Any) -> List[HiveResponse]:
    """The cloud returns ``{"hives": [...]}`` for GET /v1/hives.

    Unwrap the envelope here so callers get a flat list and don't
    have to reach into a dict. If the server ever returns a raw list
    (future API version), fall through and handle that too.
    """
    if isinstance(raw, dict):
        items = raw.get("hives", [])
    elif isinstance(raw, list):
        items = raw
    else:
        raise EngramValidationError(
            "Expected a JSON object or list from /v1/hives, "
            "got {}".format(type(raw).__name__)
        )
    return [HiveResponse.from_dict(item) for item in items if isinstance(item, dict)]


def _coerce_health_response(raw: Any) -> HealthResponse:
    if not isinstance(raw, dict):
        raise EngramValidationError(
            "Expected a JSON object from /v1/health, got {}".format(type(raw).__name__)
        )
    return HealthResponse.from_dict(raw)


def _validate_forget_args(memory_id: Optional[str]) -> None:
    """``/v1/forget`` requires at least one of ``memory_id`` or
    ``query`` in the body. The SDK only supports the ``memory_id``
    form in its public method (the query form is a cloud
    escape-hatch that duplicates ``search + forget``; we'd rather
    callers compose those two primitives themselves so the deletion
    target is explicit in their own code).
    """
    if not memory_id:
        raise EngramValidationError(
            "forget() requires a memory_id. Use client.search(...) "
            "first to find the id you want to delete."
        )


# ─── Sync client ─────────────────────────────────────────────────────


class EngramClient:
    """Blocking client for the Engram cloud API.

    Instances are cheap to construct but own an underlying
    :class:`httpx.Client`, so prefer building one per process and
    reusing it. Call :meth:`close` when you're done (or use it as a
    context manager) if you need deterministic connection cleanup.

    All methods raise subclasses of :class:`engram.EngramError` on
    failure. Network errors are wrapped in
    :class:`EngramConnectionError`, HTTP 401 becomes
    :class:`EngramAuthError`, 429 becomes :class:`EngramRateLimitError`
    (with the server's ``Retry-After`` in seconds when available), and
    any other non-2xx becomes :class:`EngramAPIError`.

    Example::

        from engram import EngramClient

        client = EngramClient()  # picks up ENGRAM_API_KEY
        client.store("Postgres is on port 5433 in prod", category="infra")
        hits = client.search("what port does prod postgres use")
        for hit in hits.results:
            print(hit.text, hit.score)
    """

    version = SDK_VERSION

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        config = _build_config(api_key, base_url, timeout, max_retries, retry_backoff)
        self._transport = SyncTransport(config)

    def __enter__(self) -> "EngramClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._transport.close()

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def store(
        self,
        text: str,
        category: Optional[str] = None,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoreResponse:
        """Persist a memory via ``POST /v1/store``."""
        req = StoreRequest(
            text=text,
            category=category,
            importance=importance,
            metadata=metadata,
        )
        raw = self._transport.request("POST", "/v1/store", json_body=req.to_payload())
        return _coerce_store_response(raw)

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: str = "personal",
        category: Optional[str] = None,
    ) -> SearchResponse:
        """Run a semantic search via ``POST /v1/search``.

        ``top_k`` is the SDK-facing name for the result count — the
        wire protocol calls it ``limit`` and we translate at the
        transport boundary. ``scope`` selects ``"personal"`` (default)
        or ``"hive:<hive_id>"``; an unauthorized hive scope raises
        :class:`EngramAPIError` with status 403.
        """
        req = SearchRequest(
            query=query,
            limit=top_k,
            scope=scope,
            category=category,
        )
        raw = self._transport.request("POST", "/v1/search", json_body=req.to_payload())
        return _coerce_search_response(raw)

    def forget(self, memory_id: str) -> ForgetResponse:
        """Delete a memory by id via ``POST /v1/forget``.

        The cloud endpoint also supports deletion by query match, but
        the SDK deliberately doesn't expose that form — it's too easy
        to delete the wrong thing when a search near-miss is "close
        enough" for the server but not for the caller. Compose
        :meth:`search` + :meth:`forget` in your own code instead;
        you'll see exactly which id is going away before it happens.
        """
        _validate_forget_args(memory_id)
        req = ForgetRequest(memory_id=memory_id)
        raw = self._transport.request("POST", "/v1/forget", json_body=req.to_payload())
        return _coerce_forget_response(raw)

    def feedback(
        self,
        query: str,
        selected_ids: List[str],
        rejected_ids: Optional[List[str]] = None,
    ) -> FeedbackResponse:
        """Send rerank feedback via ``POST /v1/feedback``.

        Use this right after your model decides which of the returned
        memories it actually kept in context. The cloud engine uses
        the signal to boost ids that tend to get picked and penalize
        ones that get dropped — costs nothing because the judgment
        came from your own LLM's existing pass.
        """
        req = FeedbackRequest(
            query=query,
            selected_ids=list(selected_ids),
            rejected_ids=list(rejected_ids or []),
        )
        raw = self._transport.request(
            "POST", "/v1/feedback", json_body=req.to_payload()
        )
        return _coerce_feedback_response(raw)

    # ------------------------------------------------------------------
    # Hives
    # ------------------------------------------------------------------

    def create_hive(self, name: str, slug: str) -> HiveResponse:
        """Create a new hive via ``POST /v1/hives``.

        The caller is automatically added as ``owner``, and the
        backing Qdrant collection is pre-created server-side so the
        first store/search on the new hive doesn't pay a cold-start
        roundtrip. Slug must be lowercase alphanumeric + hyphens,
        3-48 characters.
        """
        raw = self._transport.request(
            "POST",
            "/v1/hives",
            json_body={"name": name, "slug": slug},
        )
        return _coerce_hive_response(raw)

    def list_hives(self) -> List[HiveResponse]:
        """List every hive the authenticated user belongs to via
        ``GET /v1/hives``. Each row includes the caller's role on
        that hive."""
        raw = self._transport.request("GET", "/v1/hives")
        return _coerce_hive_list(raw)

    def grant_hive_access(
        self,
        hive_id: str,
        key_prefix: str,
        permission: str = "readwrite",
    ) -> Dict[str, Any]:
        """Grant an API key access to a hive via ``POST /v1/hives/{hive_id}/grants``."""
        raw = self._transport.request(
            "POST",
            "/v1/hives/{}/grants".format(hive_id),
            json_body={"key_prefix": key_prefix, "permission": permission},
        )
        return raw if isinstance(raw, dict) else {}

    def revoke_hive_access(self, hive_id: str, key_prefix: str) -> Dict[str, Any]:
        """Revoke an API key's access to a hive via
        ``DELETE /v1/hives/{hive_id}/grants/{key_prefix}``."""
        raw = self._transport.request(
            "DELETE",
            "/v1/hives/{}/grants/{}".format(hive_id, key_prefix),
        )
        return raw if isinstance(raw, dict) else {}

    def list_hive_grants(self, hive_id: str) -> List[Dict[str, Any]]:
        """List grants for a hive via ``GET /v1/hives/{hive_id}/grants``."""
        raw = self._transport.request(
            "GET",
            "/v1/hives/{}/grants".format(hive_id),
        )
        if isinstance(raw, dict):
            grants = raw.get("grants", [])
            if isinstance(grants, list):
                return [g for g in grants if isinstance(g, dict)]
        return []

    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------

    def health(self) -> HealthResponse:
        """Fetch the authenticated health snapshot via ``GET /v1/health``.

        Returns component status strings for the API, embedding
        service, and Qdrant. Useful as a smoke test at startup —
        raises :class:`EngramAuthError` if the key is invalid, which
        is usually what you want to catch early.
        """
        raw = self._transport.request("GET", "/v1/health")
        return _coerce_health_response(raw)


# ─── Async client ────────────────────────────────────────────────────


class AsyncEngramClient:
    """Async mirror of :class:`EngramClient`.

    Every method signature matches the sync class one-for-one, with
    ``async def`` in place of ``def`` and the obvious ``await``s
    inside. Use when you're already in an event loop (FastAPI, aiohttp,
    trio+anyio etc.). Like the sync client, prefer to build once and
    reuse — the underlying :class:`httpx.AsyncClient` keeps its own
    connection pool.
    """

    version = SDK_VERSION

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        config = _build_config(api_key, base_url, timeout, max_retries, retry_backoff)
        self._transport = AsyncTransport(config)

    async def __aenter__(self) -> "AsyncEngramClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP connection pool."""
        await self._transport.aclose()

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    async def store(
        self,
        text: str,
        category: Optional[str] = None,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoreResponse:
        req = StoreRequest(
            text=text,
            category=category,
            importance=importance,
            metadata=metadata,
        )
        raw = await self._transport.request(
            "POST", "/v1/store", json_body=req.to_payload()
        )
        return _coerce_store_response(raw)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        scope: str = "personal",
        category: Optional[str] = None,
    ) -> SearchResponse:
        req = SearchRequest(
            query=query,
            limit=top_k,
            scope=scope,
            category=category,
        )
        raw = await self._transport.request(
            "POST", "/v1/search", json_body=req.to_payload()
        )
        return _coerce_search_response(raw)

    async def forget(self, memory_id: str) -> ForgetResponse:
        _validate_forget_args(memory_id)
        req = ForgetRequest(memory_id=memory_id)
        raw = await self._transport.request(
            "POST", "/v1/forget", json_body=req.to_payload()
        )
        return _coerce_forget_response(raw)

    async def feedback(
        self,
        query: str,
        selected_ids: List[str],
        rejected_ids: Optional[List[str]] = None,
    ) -> FeedbackResponse:
        req = FeedbackRequest(
            query=query,
            selected_ids=list(selected_ids),
            rejected_ids=list(rejected_ids or []),
        )
        raw = await self._transport.request(
            "POST", "/v1/feedback", json_body=req.to_payload()
        )
        return _coerce_feedback_response(raw)

    # ------------------------------------------------------------------
    # Hives
    # ------------------------------------------------------------------

    async def create_hive(self, name: str, slug: str) -> HiveResponse:
        raw = await self._transport.request(
            "POST",
            "/v1/hives",
            json_body={"name": name, "slug": slug},
        )
        return _coerce_hive_response(raw)

    async def list_hives(self) -> List[HiveResponse]:
        raw = await self._transport.request("GET", "/v1/hives")
        return _coerce_hive_list(raw)

    async def grant_hive_access(
        self,
        hive_id: str,
        key_prefix: str,
        permission: str = "readwrite",
    ) -> Dict[str, Any]:
        """Grant an API key access to a hive via ``POST /v1/hives/{hive_id}/grants``."""
        raw = await self._transport.request(
            "POST",
            "/v1/hives/{}/grants".format(hive_id),
            json_body={"key_prefix": key_prefix, "permission": permission},
        )
        return raw if isinstance(raw, dict) else {}

    async def revoke_hive_access(self, hive_id: str, key_prefix: str) -> Dict[str, Any]:
        """Revoke an API key's access to a hive via
        ``DELETE /v1/hives/{hive_id}/grants/{key_prefix}``."""
        raw = await self._transport.request(
            "DELETE",
            "/v1/hives/{}/grants/{}".format(hive_id, key_prefix),
        )
        return raw if isinstance(raw, dict) else {}

    async def list_hive_grants(self, hive_id: str) -> List[Dict[str, Any]]:
        """List grants for a hive via ``GET /v1/hives/{hive_id}/grants``."""
        raw = await self._transport.request(
            "GET",
            "/v1/hives/{}/grants".format(hive_id),
        )
        if isinstance(raw, dict):
            grants = raw.get("grants", [])
            if isinstance(grants, list):
                return [g for g in grants if isinstance(g, dict)]
        return []

    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------

    async def health(self) -> HealthResponse:
        raw = await self._transport.request("GET", "/v1/health")
        return _coerce_health_response(raw)


__all__ = [
    "EngramClient",
    "AsyncEngramClient",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF",
]
