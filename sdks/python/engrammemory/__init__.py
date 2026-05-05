"""engram — official Python SDK for the Engram cloud API.

Top-level package. Re-exports everything callers typically want from
the submodules so ``from engram import EngramClient, EngramError``
works without anyone having to remember which file a symbol lives in.

Submodule layout (stable):
    - :mod:`engram.client`      — sync + async client classes
    - :mod:`engram.models`      — dataclass request/response types
    - :mod:`engram.exceptions`  — error hierarchy
    - :mod:`engram._http`       — transport (private, no stable API)

See the README in this package for install instructions and examples.
"""

from __future__ import annotations

from ._http import SDK_VERSION
from .client import (
    AsyncEngramClient,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_TIMEOUT,
    EngramClient,
)
from .exceptions import (
    EngramAPIError,
    EngramAuthError,
    EngramConnectionError,
    EngramError,
    EngramRateLimitError,
    EngramValidationError,
)
from .models import (
    FeedbackRequest,
    FeedbackResponse,
    ForgetRequest,
    ForgetResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
    StoreRequest,
    StoreResponse,
    HiveResponse,
)

__version__ = SDK_VERSION

__all__ = [
    # Version
    "__version__",
    "SDK_VERSION",
    # Clients
    "EngramClient",
    "AsyncEngramClient",
    # Defaults
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF",
    # Exceptions
    "EngramError",
    "EngramAuthError",
    "EngramRateLimitError",
    "EngramAPIError",
    "EngramConnectionError",
    "EngramValidationError",
    # Models
    "StoreRequest",
    "StoreResponse",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "ForgetRequest",
    "ForgetResponse",
    "FeedbackRequest",
    "FeedbackResponse",
    "HiveResponse",
    "HealthResponse",
]
