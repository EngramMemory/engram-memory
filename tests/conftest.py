"""
conftest.py — Shared fixtures for Engram integration tests.

Each test session gets a unique Qdrant collection (agent-memory-test-<uuid>)
that is created on setup and deleted on teardown. Tests never touch the
production agent-memory collection.
"""

import asyncio
import os
import sys
import uuid
import httpx
import pytest
import pytest_asyncio

# Make src/recall importable from every test module.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src", "recall"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
EMBED_URL = os.getenv("EMBED_URL", os.getenv("FASTEMBED_URL", "http://localhost:11435"))

# Embedding dimension for nomic-embed-text-v1.5
EMBED_DIM = 768


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests that require live Qdrant + FastEmbed services",
    )


@pytest.fixture(scope="session")
def collection_name():
    """Unique collection name for this test session."""
    return f"agent-memory-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop so async fixtures can share state."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_collection(collection_name):
    """
    Create the test Qdrant collection before all tests, delete it after.

    Uses named vectors (dense + bm25) matching the recall engine's
    store() format so hybrid search works in tests.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create collection with named vectors
        resp = await client.put(
            f"{QDRANT_URL}/collections/{collection_name}",
            json={
                "vectors": {
                    "dense": {
                        "size": EMBED_DIM,
                        "distance": "Cosine",
                    }
                },
                "sparse_vectors": {
                    "bm25": {
                        "index": {"type": "sparse", "full_scan_threshold": 5000}
                    }
                },
            },
        )
        assert resp.status_code in (200, 201), (
            f"Failed to create test collection '{collection_name}': {resp.text}"
        )

        yield collection_name

        # Teardown: delete the test collection
        await client.delete(f"{QDRANT_URL}/collections/{collection_name}")


@pytest.fixture(scope="session")
def plugin_py():
    """Absolute path to plugin.py."""
    return os.path.join(_REPO_ROOT, "plugin.py")


@pytest.fixture(scope="session")
def repo_root():
    return _REPO_ROOT
