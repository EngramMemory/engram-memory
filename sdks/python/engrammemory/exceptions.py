"""Exception hierarchy for the Engram Python SDK.

Every error raised by the SDK inherits from :class:`EngramError`, so
callers that want "catch anything the SDK can throw" can write::

    try:
        client.store("hello")
    except EngramError as exc:
        ...

More specific errors exist so callers can branch on failure type without
parsing strings. The goal is that the exception class, plus any
attributes on it, tell the caller everything they need to decide what
to do next — no poking at response bodies from inside a handler.
"""

from __future__ import annotations

from typing import Any, Optional


class EngramError(Exception):
    """Base class for every SDK error.

    Attributes set on the base class are available on all subclasses,
    so a single ``except EngramError`` handler can still inspect
    ``status_code`` or ``body`` when present.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        status = self.status_code if self.status_code is not None else "-"
        return "{}(status={}, message={!r})".format(
            self.__class__.__name__, status, self.message
        )


class EngramAuthError(EngramError):
    """Raised on HTTP 401. The API key is missing, wrong, or revoked.

    Retrying will not help. Callers should surface this to a human and
    stop the operation — auto-retry on 401 is a classic way to trip
    rate limiters and lockout policies.
    """


class EngramRateLimitError(EngramError):
    """Raised on HTTP 429.

    ``retry_after`` mirrors the ``Retry-After`` response header (in
    seconds) when the server supplies one, so callers can sleep the
    exact amount the cloud requested instead of guessing. ``None``
    means the server gave no hint — in that case fall back to your own
    backoff strategy.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        body: Any = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class EngramAPIError(EngramError):
    """Raised for non-2xx responses not covered by a more specific
    subclass — typically 4xx validation errors or 5xx after retries
    have been exhausted.

    ``status_code`` is always populated. ``body`` holds the decoded
    JSON payload when available, otherwise the raw text, otherwise
    ``None`` — the SDK does best-effort decoding but never swallows
    the underlying bytes.
    """


class EngramConnectionError(EngramError):
    """Raised when the SDK could not complete an HTTP roundtrip at all
    — DNS failure, TCP reset, TLS error, read timeout, etc.

    The original exception from ``httpx`` is chained via
    ``__cause__`` so callers that want the gory details can walk
    ``exc.__cause__`` without the SDK re-exposing httpx's types in its
    public surface.
    """


class EngramValidationError(EngramError):
    """Raised for client-side input errors before any HTTP roundtrip.

    Used sparingly — the cloud is the source of truth for validation.
    We only raise this for things that would guarantee a 4xx if we
    sent them (e.g. empty api_key, bad base_url).
    """


__all__ = [
    "EngramError",
    "EngramAuthError",
    "EngramRateLimitError",
    "EngramAPIError",
    "EngramConnectionError",
    "EngramValidationError",
]
