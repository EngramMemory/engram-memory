"""
test_integration.py — Engram memory agent integration tests.

Tests hit real services: Qdrant (localhost:6333) + FastEmbed (localhost:11435).
Run with:
    pytest tests/test_integration.py -v -m integration

Each test is independent: it stores its own uniquely-tagged memories so
cross-test interference is impossible even when tests run in parallel.

Collection lifecycle: conftest.py creates agent-memory-test-<uuid> before
the session and deletes it after. The production 'agent-memory' collection
is never touched.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import httpx
import pytest
import pytest_asyncio

# src/recall is on sys.path via conftest.py
from recall_engine import EngramRecallEngine
from models import EngramConfig, RecallEngineHealth

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
EMBED_URL = os.getenv("EMBED_URL", os.getenv("FASTEMBED_URL", "http://localhost:11435"))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def unique_tag() -> str:
    """Short unique string to namespace memories within a test."""
    return uuid.uuid4().hex[:8]


def make_config(collection: str) -> EngramConfig:
    """Build an EngramConfig pointing at the test collection."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return EngramConfig(
        qdrant_url=QDRANT_URL,
        embedding_url=EMBED_URL,
        collection=collection,
        data_dir=os.path.join(repo_root, ".engram-test"),
        auto_persist=False,
        graph_enabled=False,   # skip Kuzu dependency in integration tests
        reranker_enabled=False,
    )


async def engine_store(collection: str, text: str, category: str = "other") -> str:
    """Store a memory and return its doc_id."""
    config = make_config(collection)
    engine = EngramRecallEngine(config)
    await engine.warmup()
    try:
        doc_id, _, _ = await engine.store(content=text, category=category)
        return doc_id
    finally:
        await engine.shutdown()


async def engine_search(collection: str, query: str, limit: int = 5, category: str = None):
    """Search and return results list."""
    config = make_config(collection)
    engine = EngramRecallEngine(config)
    await engine.warmup()
    try:
        return await engine.search(query=query, top_k=limit, category=category)
    finally:
        await engine.shutdown()


async def engine_forget(collection: str, doc_id: str) -> bool:
    config = make_config(collection)
    engine = EngramRecallEngine(config)
    await engine.warmup()
    try:
        return await engine.forget(doc_id)
    finally:
        await engine.shutdown()


def invoke_cli(plugin_py: str, collection: str, tool_name: str, params: dict) -> dict:
    """
    Run plugin.py via subprocess with COLLECTION_NAME set to the test collection.
    Returns parsed JSON response dict.
    """
    env = os.environ.copy()
    env["COLLECTION_NAME"] = collection
    env["QDRANT_URL"] = QDRANT_URL
    env["EMBED_URL"] = EMBED_URL
    env["FASTEMBED_URL"] = EMBED_URL

    result = subprocess.run(
        [sys.executable, plugin_py, tool_name, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0 or result.stdout.strip(), (
        f"plugin.py exited {result.returncode}; stderr: {result.stderr[:500]}"
    )
    raw = result.stdout.strip()
    assert raw, f"plugin.py produced no stdout for tool={tool_name}"
    return json.loads(raw)


# ─── Group 1: memory_store via CLI ────────────────────────────────────────────

@pytest.mark.integration
class TestMemoryStoreCLI:

    def test_happy_path_explicit_category(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] User decided to use PostgreSQL for the main database",
            "category": "decision",
            "importance": 0.8,
        })
        assert resp["success"] is True
        data = resp["data"]
        assert "memory_id" in data, f"Expected memory_id in data: {data}"
        assert isinstance(data["memory_id"], str)
        assert len(data["memory_id"]) > 0
        assert data["category"] == "decision"

    def test_auto_category_decision(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] We decided to migrate our codebase to TypeScript",
        })
        assert resp["success"] is True
        # Engine should detect "decided" → decision
        assert resp["data"]["category"] == "decision"

    def test_auto_category_preference(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] I prefer tabs over spaces in Python files always",
        })
        assert resp["success"] is True
        assert resp["data"]["category"] == "preference"

    def test_auto_category_fact(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Service deployed at version 2.4.1 running on port 8080",
        })
        assert resp["success"] is True
        assert resp["data"]["category"] == "fact"

    def test_auto_category_entity(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Alice is the lead engineer on the infrastructure team",
        })
        assert resp["success"] is True
        # "engineer" and "team" should trigger entity category
        assert resp["data"]["category"] == "entity"

    def test_missing_text_parameter(self, test_collection, plugin_py):
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "category": "fact",
        })
        assert resp["success"] is False
        assert "error" in resp
        assert len(resp["error"]) > 0

    def test_very_long_text(self, test_collection, plugin_py):
        tag = unique_tag()
        long_text = f"[{tag}] " + ("This is a detailed architectural note about the recall system. " * 10)
        assert len(long_text) >= 600
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": long_text,
            "category": "fact",
        })
        assert resp["success"] is True
        assert "memory_id" in resp["data"]

    def test_unicode_content(self, test_collection, plugin_py):
        tag = unique_tag()
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] 日本語のメモ: システムは正常に動作しています。Unicode test 🧠",
            "category": "fact",
        })
        assert resp["success"] is True
        assert "memory_id" in resp["data"]
        # Stored text should survive round-trip
        assert resp["data"]["text"].startswith(f"[{tag}]")

    def test_returns_text_in_response(self, test_collection, plugin_py):
        tag = unique_tag()
        text = f"[{tag}] The API key rotation policy requires 90-day cycles"
        resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": text,
            "category": "fact",
        })
        assert resp["success"] is True
        assert resp["data"]["text"] == text


# ─── Group 2: memory_search via CLI ───────────────────────────────────────────

@pytest.mark.integration
class TestMemorySearchCLI:

    def test_happy_path_store_then_find(self, test_collection, plugin_py):
        tag = unique_tag()
        text = f"[{tag}] Engram uses nomic-embed-text for vector embeddings"
        # Store
        store_resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": text, "category": "fact",
        })
        assert store_resp["success"] is True

        # Give Qdrant a moment to index
        time.sleep(0.5)

        # Search — top result should contain our unique tag
        search_resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"vector embedding model {tag}",
            "limit": 5,
        })
        assert search_resp["success"] is True
        data = search_resp["data"]
        assert "results" in data
        assert isinstance(data["results"], list)
        assert data["count"] == len(data["results"])

        texts = [r["text"] for r in data["results"]]
        assert any(tag in t for t in texts), (
            f"Tag {tag!r} not found in any result. Got: {texts}"
        )

    def test_result_structure(self, test_collection, plugin_py):
        tag = unique_tag()
        invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Result structure validation memory",
            "category": "fact",
        })
        time.sleep(0.5)

        resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"result structure {tag}",
            "limit": 3,
        })
        assert resp["success"] is True
        results = resp["data"]["results"]
        if results:
            r = results[0]
            assert "id" in r, f"Missing 'id' field: {r}"
            assert "score" in r, f"Missing 'score' field: {r}"
            assert "text" in r, f"Missing 'text' field: {r}"
            assert "category" in r, f"Missing 'category' field: {r}"
            assert "tier" in r, f"Missing 'tier' field: {r}"
            assert isinstance(r["score"], (int, float))
            assert 0.0 <= r["score"] <= 1.0, f"Score out of [0,1]: {r['score']}"

    def test_limit_respected(self, test_collection, plugin_py):
        tag = unique_tag()
        # Store 5 memories
        for i in range(5):
            invoke_cli(plugin_py, test_collection, "memory_store", {
                "text": f"[{tag}] Memory number {i} about the deployment configuration system",
                "category": "fact",
            })
        time.sleep(1.0)

        resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"deployment configuration {tag}",
            "limit": 2,
        })
        assert resp["success"] is True
        assert len(resp["data"]["results"]) <= 2

    def test_category_filter(self, test_collection, plugin_py):
        tag = unique_tag()
        invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] We decided to adopt Rust for performance-critical services",
            "category": "decision",
        })
        invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Team prefers Python for data pipeline scripts",
            "category": "preference",
        })
        time.sleep(0.5)

        resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"programming language choice {tag}",
            "limit": 10,
            "category": "decision",
        })
        assert resp["success"] is True
        for r in resp["data"]["results"]:
            assert r["category"] == "decision", (
                f"Category filter broken — got {r['category']!r}: {r['text'][:60]}"
            )

    def test_missing_query_parameter(self, test_collection, plugin_py):
        resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "limit": 5,
        })
        assert resp["success"] is False
        assert "error" in resp
        assert len(resp["error"]) > 0

    def test_tiers_used_field_present(self, test_collection, plugin_py):
        tag = unique_tag()
        invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Tiers field validation test memory for recall pipeline",
            "category": "fact",
        })
        time.sleep(0.5)

        resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"tiers validation {tag}",
        })
        assert resp["success"] is True
        assert "tiers_used" in resp["data"], (
            f"Expected tiers_used in response data: {resp['data'].keys()}"
        )
        assert isinstance(resp["data"]["tiers_used"], list)


# ─── Group 3: memory_forget via CLI ───────────────────────────────────────────

@pytest.mark.integration
class TestMemoryForgetCLI:

    def test_forget_by_id(self, test_collection, plugin_py):
        tag = unique_tag()
        # Store
        store_resp = invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Temporary memory to be forgotten by ID",
            "category": "fact",
        })
        assert store_resp["success"] is True
        memory_id = store_resp["data"]["memory_id"]

        # Forget by ID
        forget_resp = invoke_cli(plugin_py, test_collection, "memory_forget", {
            "memory_id": memory_id,
        })
        assert forget_resp["success"] is True
        assert forget_resp.get("deleted") == memory_id

        # Verify gone: search for the unique tag — should not find that ID
        time.sleep(0.5)
        search_resp = invoke_cli(plugin_py, test_collection, "memory_search", {
            "query": f"temporary memory forgotten {tag}",
            "limit": 10,
        })
        assert search_resp["success"] is True
        found_ids = [r["id"] for r in search_resp["data"]["results"]]
        assert memory_id not in found_ids, (
            f"Memory {memory_id} still found after forget"
        )

    def test_forget_by_query(self, test_collection, plugin_py):
        tag = unique_tag()
        invoke_cli(plugin_py, test_collection, "memory_store", {
            "text": f"[{tag}] Obsolete infrastructure note to be purged by query",
            "category": "fact",
        })
        time.sleep(0.5)

        forget_resp = invoke_cli(plugin_py, test_collection, "memory_forget", {
            "query": f"obsolete infrastructure {tag}",
        })
        assert forget_resp["success"] is True
        assert "deleted" in forget_resp

    def test_neither_query_nor_id(self, test_collection, plugin_py):
        resp = invoke_cli(plugin_py, test_collection, "memory_forget", {})
        assert resp["success"] is False
        assert "error" in resp
        err = resp["error"].lower()
        assert "query" in err or "memory_id" in err or "provide" in err, (
            f"Expected helpful error message, got: {resp['error']}"
        )

    def test_non_existent_id(self, test_collection, plugin_py):
        fake_id = str(uuid.uuid4())
        resp = invoke_cli(plugin_py, test_collection, "memory_forget", {
            "memory_id": fake_id,
        })
        # Engine returns success=True even when the ID didn't exist in Qdrant
        # (DELETE on a non-existent point is idempotent). Either outcome is
        # acceptable — what matters is the response is well-formed JSON.
        assert "success" in resp
        assert isinstance(resp["success"], bool)


# ─── Group 4: CLI dispatcher surface ──────────────────────────────────────────

@pytest.mark.integration
class TestCLIDispatcher:

    def test_unknown_tool_returns_error_json(self, test_collection, plugin_py):
        resp = invoke_cli(plugin_py, test_collection, "memory_telepathy", {
            "query": "anything",
        })
        assert resp["success"] is False
        assert "error" in resp
        assert "memory_telepathy" in resp["error"] or "Unknown tool" in resp["error"]

    def test_invalid_json_params(self, plugin_py):
        """plugin.py <tool> <bad-json> → valid error JSON on stdout."""
        env = os.environ.copy()
        env["COLLECTION_NAME"] = "does-not-matter"
        result = subprocess.run(
            [sys.executable, plugin_py, "memory_store", "{not valid json}"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        raw = result.stdout.strip()
        assert raw, "Expected JSON output on stdout even for bad input"
        parsed = json.loads(raw)
        assert parsed["success"] is False
        assert "error" in parsed
        assert "JSON" in parsed["error"] or "json" in parsed["error"].lower()

    def test_all_tools_output_valid_json(self, test_collection, plugin_py):
        """Every tool call must produce parseable JSON."""
        calls = [
            ("memory_store", {"text": f"[{unique_tag()}] JSON output test", "category": "fact"}),
            ("memory_search", {"query": "json output test", "limit": 1}),
            ("memory_forget", {"query": "json output test"}),
            ("memory_store", {}),          # missing required param → error JSON
            ("memory_search", {}),          # missing required param → error JSON
            ("memory_forget", {}),          # neither param → error JSON
        ]
        for tool, params in calls:
            result = subprocess.run(
                [sys.executable, plugin_py, tool, json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "COLLECTION_NAME": test_collection,
                     "QDRANT_URL": QDRANT_URL, "FASTEMBED_URL": EMBED_URL},
            )
            raw = result.stdout.strip()
            assert raw, f"No stdout for {tool}({params})"
            parsed = json.loads(raw)   # raises if not valid JSON
            assert "success" in parsed, f"No 'success' field for {tool}({params}): {parsed}"

    def test_no_args_outputs_error_json(self, plugin_py):
        """Invoking plugin.py with no args → error JSON, not a traceback."""
        result = subprocess.run(
            [sys.executable, plugin_py],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip()
        assert raw, "Expected JSON on stdout when invoked with no args"
        parsed = json.loads(raw)
        assert parsed["success"] is False
        assert "error" in parsed


# ─── Group 5: Recall engine direct — health check ─────────────────────────────

@pytest.mark.integration
class TestRecallEngineHealth:

    @pytest.mark.asyncio
    async def test_health_fields_present(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        try:
            health = await engine.get_health()
        finally:
            await engine.shutdown()

        assert isinstance(health, RecallEngineHealth), (
            f"Expected RecallEngineHealth, got {type(health)}"
        )
        assert health.status in ("healthy", "degraded", "error"), (
            f"Unexpected status: {health.status!r}"
        )
        assert isinstance(health.qdrant_connected, bool)
        assert isinstance(health.fastembed_connected, bool)
        assert isinstance(health.hot_tier_size, int)
        assert isinstance(health.hash_index_size, int)

    @pytest.mark.asyncio
    async def test_health_services_connected(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        try:
            health = await engine.get_health()
        finally:
            await engine.shutdown()

        assert health.qdrant_connected is True, (
            f"Qdrant not connected. Errors: {health.errors}"
        )
        assert health.fastembed_connected is True, (
            f"FastEmbed not connected. Errors: {health.errors}"
        )

    @pytest.mark.asyncio
    async def test_health_to_dict_structure(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        try:
            health = await engine.get_health()
        finally:
            await engine.shutdown()

        d = health.to_dict()
        assert "status" in d
        assert "tiers" in d
        tiers = d["tiers"]
        assert "hot" in tiers
        assert "hash" in tiers
        assert "vector" in tiers
        assert "qdrant_connected" in tiers["vector"]
        assert "fastembed_connected" in tiers["vector"]
        assert "uptime_seconds" in d
        assert isinstance(d["uptime_seconds"], float)
        assert d["uptime_seconds"] >= 0.0

    @pytest.mark.asyncio
    async def test_health_uptime_increases(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        try:
            h1 = await engine.get_health()
            await asyncio.sleep(0.1)
            h2 = await engine.get_health()
        finally:
            await engine.shutdown()

        assert h2.uptime_seconds >= h1.uptime_seconds, (
            "Uptime should be non-decreasing"
        )


# ─── Group 6: Recall engine direct — store / search / forget ──────────────────

@pytest.mark.integration
class TestRecallEngineDirect:

    @pytest.mark.asyncio
    async def test_store_returns_uuid_and_category(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            doc_id, category, _ = await engine.store(
                content=f"[{tag}] Direct engine store test: project uses React 18",
                category="fact",
            )
        finally:
            await engine.shutdown()

        assert isinstance(doc_id, str), f"doc_id should be str, got {type(doc_id)}"
        assert len(doc_id) > 0
        assert category == "fact"

    @pytest.mark.asyncio
    async def test_store_auto_detects_category(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            _, category, _ = await engine.store(
                content=f"[{tag}] We decided to adopt GraphQL over REST for the public API",
                category="other",   # trigger auto-detection
            )
        finally:
            await engine.shutdown()

        assert category == "decision", (
            f"Auto-detect should produce 'decision', got {category!r}"
        )

    @pytest.mark.asyncio
    async def test_search_returns_memory_result_objects(self, test_collection):
        from models import MemoryResult
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            await engine.store(
                content=f"[{tag}] MemoryResult structure validation for recall engine",
                category="fact",
            )
            await asyncio.sleep(0.5)
            results = await engine.search(query=f"MemoryResult structure {tag}", top_k=5)
        finally:
            await engine.shutdown()

        assert isinstance(results, list)
        if results:
            r = results[0]
            assert isinstance(r, MemoryResult), f"Expected MemoryResult, got {type(r)}"
            assert isinstance(r.doc_id, str) and len(r.doc_id) > 0
            assert isinstance(r.content, str) and len(r.content) > 0
            assert isinstance(r.score, float)
            assert 0.0 <= r.score <= 1.0, f"Score out of range: {r.score}"
            assert r.tier in ("hot", "hash", "vector", "graph", "overflow"), (
                f"Unknown tier: {r.tier!r}"
            )
            assert isinstance(r.category, str)
            assert r.confidence in ("high", "medium", "low", ""), (
                f"Unknown confidence: {r.confidence!r}"
            )

    @pytest.mark.asyncio
    async def test_search_top_result_matches_stored(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            doc_id, _, _ = await engine.store(
                content=f"[{tag}] The password rotation interval is set to ninety days",
                category="fact",
            )
            await asyncio.sleep(0.5)
            results = await engine.search(
                query=f"password rotation interval {tag}", top_k=5
            )
        finally:
            await engine.shutdown()

        assert len(results) >= 1, "Expected at least one result"
        top = results[0]
        assert tag in top.content, (
            f"Top result doesn't contain tag {tag!r}. Got: {top.content[:100]}"
        )

    @pytest.mark.asyncio
    async def test_search_category_filter_excludes_others(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            await engine.store(
                content=f"[{tag}] We chose Kubernetes over Docker Swarm for orchestration",
                category="decision",
            )
            await engine.store(
                content=f"[{tag}] Sarah prefers containers over VMs for dev environments",
                category="preference",
            )
            await asyncio.sleep(0.5)
            results = await engine.search(
                query=f"container orchestration {tag}",
                top_k=10,
                category="decision",
            )
        finally:
            await engine.shutdown()

        for r in results:
            assert r.category == "decision", (
                f"Category filter let through {r.category!r}: {r.content[:60]}"
            )

    @pytest.mark.asyncio
    async def test_forget_removes_from_qdrant(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        try:
            doc_id, _, _ = await engine.store(
                content=f"[{tag}] This memory should be deleted from the system",
                category="fact",
            )
            await asyncio.sleep(0.3)

            removed = await engine.forget(doc_id)
            assert removed is True, "forget() should return True for known ID"

            # Verify it's gone via direct Qdrant point fetch
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{QDRANT_URL}/collections/{test_collection}/points",
                    json={"ids": [doc_id], "with_payload": False, "with_vector": False},
                )
                assert resp.status_code == 200
                points = resp.json().get("result", [])
                assert len(points) == 0, (
                    f"Point {doc_id} still exists in Qdrant after forget()"
                )
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_search_empty_collection_returns_list(self, collection_name):
        """
        Use a brand-new ephemeral collection with zero documents.
        search() must return an empty list, not raise.
        """
        # Create a fresh empty collection for this test only
        empty_col = f"agent-memory-empty-{uuid.uuid4().hex[:8]}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            create_resp = await client.put(
                f"{QDRANT_URL}/collections/{empty_col}",
                json={
                    "vectors": {"dense": {"size": 768, "distance": "Cosine"}},
                    "sparse_vectors": {
                        "bm25": {"index": {"type": "sparse", "full_scan_threshold": 5000}}
                    },
                },
            )
            assert create_resp.status_code in (200, 201)

        config = make_config(empty_col)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        try:
            results = await engine.search(query="anything at all", top_k=5)
        finally:
            await engine.shutdown()
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(f"{QDRANT_URL}/collections/{empty_col}")

        assert isinstance(results, list), (
            f"Expected list from empty collection, got {type(results)}"
        )
        assert len(results) == 0, (
            f"Expected 0 results from empty collection, got {len(results)}"
        )

    @pytest.mark.asyncio
    async def test_store_unicode_roundtrip(self, test_collection):
        config = make_config(test_collection)
        engine = EngramRecallEngine(config)
        await engine.warmup()
        tag = unique_tag()
        text = f"[{tag}] 한국어 메모: 시스템이 정상적으로 실행 중입니다. Also: café, naïve, résumé"
        try:
            doc_id, _, _ = await engine.store(content=text, category="fact")
            await asyncio.sleep(0.3)
            results = await engine.search(query=f"Korean memo system {tag}", top_k=5)
        finally:
            await engine.shutdown()

        matching = [r for r in results if tag in r.content]
        assert len(matching) >= 1, (
            f"Unicode memory not retrieved. Results: {[r.content[:60] for r in results]}"
        )
        assert matching[0].content == text, (
            f"Content changed during store/retrieve. "
            f"Expected: {text!r}\nGot: {matching[0].content!r}"
        )
