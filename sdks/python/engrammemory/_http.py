"""HTTP transport for the Engram Python SDK.

Kept deliberately isolated so the public ``client.py`` can stay
method-shaped — one function per endpoint — while all the messy
concerns (auth injection, retry loop, error classification, request id
propagation, user-agent) live here.

Two transports are exported: :class:`SyncTransport` wraps
``httpx.Client`` and :class:`AsyncTransport` wraps ``httpx.AsyncClient``.
They share logic via :class:`_BaseTransport` which only defines pure
functions (header construction, status code classification, backoff
scheduling) — no I/O at all. That keeps the blocking and async code
paths bit-for-bit identical without resorting to ``asyncio.run`` tricks
or a sans-I/O state machine.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from .exceptions import (
    EngramAPIError,
    EngramAuthError,
    EngramConnectionError,
    EngramRateLimitError,
    EngramValidationError,
)

# Version is hand-maintained for now — kept in sync with pyproject.toml
# on every release. Surfacing it in the User-Agent header lets the
# cloud distinguish SDK traffic from bridge traffic and from raw curl,
# which matters for release telemetry and for targeting deprecation
# warnings.
SDK_VERSION = "0.1.0"

DEFAULT_USER_AGENT = "engram-py/{} httpx/{}".format(SDK_VERSION, httpx.__version__)


@dataclass(frozen=True)
class TransportConfig:
    """Immutable configuration snapshot used by both transports.

    ``max_retries`` counts *additional* attempts after the first — i.e.
    ``max_retries=3`` means up to 4 total tries. ``retry_backoff`` is
    the base for the exponential curve: attempt ``n`` sleeps for
    ``retry_backoff * 2**(n-1)`` seconds plus jitter.
    """

    api_key: str
    base_url: str
    timeout: float
    max_retries: int
    retry_backoff: float
    user_agent: str = DEFAULT_USER_AGENT


class _BaseTransport:
    """Shared logic between sync and async transports.

    This class never touches the network. Subclasses wire its helpers
    to either ``httpx.Client`` or ``httpx.AsyncClient``. Every method
    here is a pure function of its arguments, so they're trivially
    unit-testable without mocking httpx.
    """

    # These methods get retried on 5xx / network errors. POST is
    # included because every mutating endpoint in the Engram API is
    # idempotent from a user's point of view — a second /v1/store of
    # the same text is deduped server-side, and /v1/forget by id is
    # safely re-runnable. If the cloud ever adds a non-idempotent
    # mutation we'll flip this to allow-list only the safe methods.
    _RETRY_METHODS = frozenset({"GET", "POST", "DELETE", "PUT", "PATCH"})

    def __init__(self, config: TransportConfig) -> None:
        if not config.api_key:
            raise EngramValidationError(
                "api_key is required — pass it to EngramClient(api_key=...) "
                "or set the ENGRAM_API_KEY environment variable."
            )
        if not config.base_url:
            raise EngramValidationError("base_url must be a non-empty URL")
        self._config = config

    # ------------------------------------------------------------------
    # Pure helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Stitch the base URL and endpoint path together.

        Tolerates a trailing slash on the base and/or a leading slash
        on the path — both are common ways callers get tripped up.
        """
        return "{}/{}".format(
            self._config.base_url.rstrip("/"),
            path.lstrip("/"),
        )

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """Build the header dict for a single request.

        We set ``Accept: application/json`` so the cloud never falls
        back to HTML error pages (the FastAPI default for a few paths
        on the auth layer returns HTML when ``Accept: */*``), which
        would confuse our JSON decoder and turn a clean 401 into a
        useless ``EngramAPIError``.
        """
        headers: Dict[str, str] = {
            "Authorization": "Bearer {}".format(self._config.api_key),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
            "X-SDK-Name": "engram-py",
            "X-SDK-Version": SDK_VERSION,
        }
        if extra:
            headers.update(extra)
        return headers

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter.

        ``attempt`` is 1-indexed. The jitter is a small random
        multiplier on the base delay so that a cluster of clients
        retrying the same 5xx don't thundering-herd the cloud when it
        recovers.
        """
        base = self._config.retry_backoff * (2 ** (attempt - 1))
        return base * (1.0 + random.random() * 0.25)

    def _parse_body(self, response: httpx.Response) -> Any:
        """Best-effort decode of a response body.

        Returns parsed JSON when the content-type claims JSON,
        otherwise returns the raw text. Never raises — a malformed
        JSON body from the server becomes a string on the returned
        exception, which is more useful than a
        ``JSONDecodeError`` bubbling out of the SDK.
        """
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type.lower():
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                return response.text
        return response.text or None

    def _retry_after(self, response: httpx.Response) -> Optional[float]:
        """Pull the Retry-After header as a float number of seconds.

        The header is either seconds (integer string) or an HTTP-date
        (RFC 1123). We support the seconds form only — HTTP-date is
        rare in practice and the cloud doesn't emit it. If the header
        is missing or unparseable, return ``None`` and let the caller
        fall back to exponential backoff.
        """
        raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _classify(self, response: httpx.Response) -> Tuple[bool, Optional[Exception]]:
        """Decide what a given HTTP status means.

        Returns ``(should_retry, exc_to_raise_if_giving_up)``. When
        ``should_retry`` is ``True``, the caller bumps the attempt
        counter; if it runs out of attempts, it raises the exception
        returned here.

        This deliberately does not look at the body — body decoding is
        deferred to :meth:`_parse_body` and happens once, at the final
        raise site, so we don't burn CPU decoding JSON for a response
        we're about to discard and retry.
        """
        status = response.status_code
        if 200 <= status < 300:
            return (False, None)
        body = self._parse_body(response)
        message = self._format_error(status, body)
        if status == 401:
            return (False, EngramAuthError(message, status_code=status, body=body))
        if status == 429:
            retry_after = self._retry_after(response)
            return (
                False,
                EngramRateLimitError(
                    message,
                    status_code=status,
                    body=body,
                    retry_after=retry_after,
                ),
            )
        if 500 <= status < 600:
            return (True, EngramAPIError(message, status_code=status, body=body))
        # Other 4xx: client bug, no retry.
        return (False, EngramAPIError(message, status_code=status, body=body))

    def _format_error(self, status: int, body: Any) -> str:
        """Produce the human message attached to an exception.

        Tries hard to pull a useful string out of the cloud's error
        envelope — FastAPI wraps HTTPException detail in ``{"detail":
        ...}`` sometimes as a string and sometimes as a dict. We
        unwrap both cases so exceptions print ``Invalid API key``
        instead of ``{'detail': {'error': 'Invalid API key', ...}}``.
        """
        if isinstance(body, dict):
            detail = body.get("detail", body)
            if isinstance(detail, dict):
                detail = (
                    detail.get("message")
                    or detail.get("error")
                    or detail.get("detail")
                    or json.dumps(detail, sort_keys=True)
                )
            return "HTTP {} — {}".format(status, detail)
        if isinstance(body, str) and body:
            return "HTTP {} — {}".format(status, body)
        return "HTTP {}".format(status)


class SyncTransport(_BaseTransport):
    """Blocking transport built on :class:`httpx.Client`.

    Kept as a short-lived object: one client, reused across every
    request made by a single :class:`engram.EngramClient`. We expose
    :meth:`close` and context manager protocol so callers that care
    about connection pool lifetime can clean up explicitly, but the
    default usage pattern is "construct once, let GC reclaim at
    interpreter exit".
    """

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        self._client = httpx.Client(timeout=config.timeout)

    def __enter__(self) -> "SyncTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - cosmetic
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Execute one request with the full retry loop.

        Returns the parsed JSON body on success, or raises one of the
        :mod:`engram.exceptions` subclasses on failure.
        """
        method_upper = method.upper()
        url = self._url(path)
        headers = self._headers()
        attempts = self._config.max_retries + 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(
                    method_upper,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                last_exc = EngramConnectionError(
                    "Request timed out after {:.1f}s".format(self._config.timeout)
                )
                last_exc.__cause__ = exc
                if method_upper in self._RETRY_METHODS and attempt < attempts:
                    time.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_exc
            except httpx.HTTPError as exc:
                last_exc = EngramConnectionError(
                    "HTTP transport error: {}".format(exc)
                )
                last_exc.__cause__ = exc
                if method_upper in self._RETRY_METHODS and attempt < attempts:
                    time.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_exc

            should_retry, exc_to_raise = self._classify(response)
            if exc_to_raise is None:
                return self._parse_body(response)
            last_exc = exc_to_raise
            if should_retry and attempt < attempts:
                time.sleep(self._backoff_seconds(attempt))
                continue
            raise exc_to_raise

        # Exhausted retries — re-raise whatever we last captured. The
        # loop above always either returns or raises, so this is
        # defensive: if somebody adds a new code path that breaks out
        # without raising, they'll hit this assertion instead of a
        # silent None return.
        assert last_exc is not None  # pragma: no cover
        raise last_exc


class AsyncTransport(_BaseTransport):
    """Non-blocking transport built on :class:`httpx.AsyncClient`.

    Mirrors :class:`SyncTransport` line-for-line with ``await``s in
    place of blocking calls. The duplication is intentional — factoring
    the retry loop into a sans-I/O state machine would obscure the
    control flow without saving meaningful code. Keep the shapes
    identical and it's easy to audit both paths at a glance.
    """

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        self._client = httpx.AsyncClient(timeout=config.timeout)

    async def __aenter__(self) -> "AsyncTransport":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        method_upper = method.upper()
        url = self._url(path)
        headers = self._headers()
        attempts = self._config.max_retries + 1
        last_exc: Optional[Exception] = None

        import asyncio  # local import — saves ~20ms on sync-only entrypoints

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method_upper,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                last_exc = EngramConnectionError(
                    "Request timed out after {:.1f}s".format(self._config.timeout)
                )
                last_exc.__cause__ = exc
                if method_upper in self._RETRY_METHODS and attempt < attempts:
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_exc
            except httpx.HTTPError as exc:
                last_exc = EngramConnectionError(
                    "HTTP transport error: {}".format(exc)
                )
                last_exc.__cause__ = exc
                if method_upper in self._RETRY_METHODS and attempt < attempts:
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_exc

            should_retry, exc_to_raise = self._classify(response)
            if exc_to_raise is None:
                return self._parse_body(response)
            last_exc = exc_to_raise
            if should_retry and attempt < attempts:
                await asyncio.sleep(self._backoff_seconds(attempt))
                continue
            raise exc_to_raise

        assert last_exc is not None  # pragma: no cover
        raise last_exc


__all__ = [
    "SDK_VERSION",
    "DEFAULT_USER_AGENT",
    "TransportConfig",
    "SyncTransport",
    "AsyncTransport",
]
