#!/usr/bin/env python3
"""
Engram Memory — Dockerized MCP Server

Exposes the three-tier recall engine over multiple transports from a single
process so any MCP client can connect:

  - Streamable HTTP (modern MCP transport)  → POST/GET /mcp
  - SSE (legacy MCP transport)              → GET /sse + POST /messages/
  - Stdio (via docker exec -i)              → python /app/mcp_server.py
  - REST (for direct consumers, OpenClaw)   → POST /store, /search, etc.

All transports share one EngramRecallEngine instance backed by Qdrant +
FastEmbed running in the same container.

Usage:
    docker run -p 6333:6333 -p 11435:11435 -p 8585:8585 \\
      -v engram_data:/data \\
      engrammemory/engram-memory:latest

Then point an MCP client at:
    http://localhost:8585/mcp     (streamable-http)
    http://localhost:8585/sse     (SSE)
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

# Order matters: /app first so we can import mcp_server.py, then /app/recall
# for recall_engine and models.
sys.path.insert(0, "/app/recall")
sys.path.insert(0, "/app")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

from mcp_server import EngramMCPServer
from models import EngramConfig
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.sse import SseServerTransport
from dashboard import register_dashboard_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("engram-mcp-docker")

# Config from env
QDRANT_URL = os.getenv("QDRANT_URL", "http://host.docker.internal:6333")
FASTEMBED_URL = os.getenv("FASTEMBED_URL", "http://host.docker.internal:11435")
COLLECTION = os.getenv("COLLECTION_NAME", "agent-memory")
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
API_KEY = os.getenv("ENGRAM_API_KEY", "")
API_URL = os.getenv("ENGRAM_API_URL", "https://api.engrammemory.ai")

# Build the MCP server up front (no I/O in __init__) so the streamable-http
# session manager can hold a reference to its underlying Server instance.
config = EngramConfig(
    qdrant_url=QDRANT_URL,
    embedding_url=FASTEMBED_URL,
    collection=COLLECTION,
    data_dir=DATA_DIR,
    api_key=API_KEY,
    api_url=API_URL,
)
mcp_server = EngramMCPServer(config)

# Modern MCP HTTP transport (streamable-http) — single /mcp endpoint that
# multiplexes both directions over HTTP. Stateless mode is fine here because
# the recall engine is shared and each request is self-contained.
session_manager = StreamableHTTPSessionManager(
    app=mcp_server.server,
    event_store=None,
    json_response=False,
    stateless=True,
)

# Legacy SSE transport — kept for older MCP clients that don't yet speak
# streamable-http. Server-sent events over /sse, client posts to /messages/.
sse_transport = SseServerTransport("/messages/")


async def _wait_for_service(name: str, url: str, timeout: int = 120):
    """Block until a dependency is healthy. Runs at startup so the user never
    hits a half-initialized system."""
    import httpx
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=5.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    logger.info(f"{name} is ready")
                    return
            except Exception:
                pass
            logger.info(f"Waiting for {name} at {url}...")
            await asyncio.sleep(2)
    raise RuntimeError(f"{name} not reachable at {url} after {timeout}s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wait for the bundled Qdrant and FastEmbed services to be healthy
    # before warming the recall engine, then start the streamable-http
    # session manager. The user should never see partial startup.
    await _wait_for_service("Qdrant", f"{QDRANT_URL}/healthz")
    await _wait_for_service("FastEmbed", f"{FASTEMBED_URL}/health")

    await mcp_server.startup()
    logger.info(f"Recall engine ready — Qdrant: {QDRANT_URL}, Collection: {COLLECTION}")

    async with session_manager.run():
        logger.info("MCP transports active: streamable-http (/mcp), SSE (/sse), REST (/store /search ...)")
        yield

    await mcp_server.shutdown()


app = FastAPI(title="Engram Memory MCP Server", version="2.1.0", lifespan=lifespan)
register_dashboard_routes(app, mcp_server)


# ── MCP transports ─────────────────────────────────────────────────────────


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_streamable_http(request: Request):
    """Modern MCP streamable-http transport. This is what
    `claude mcp add --transport http http://localhost:8585/mcp` connects to."""
    return await session_manager.handle_request(
        request.scope, request.receive, request._send
    )


@app.get("/sse")
async def mcp_sse(request: Request):
    """Legacy MCP SSE transport. Some older clients use this instead of
    streamable-http. Server-sent events stream over /sse, client posts
    messages to /messages/."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp_server.server.run(
            read_stream,
            write_stream,
            mcp_server.server.create_initialization_options(),
        )
    # connect_sse handles the response itself; FastAPI just needs a return
    return Response(status_code=200)


# Mount the SSE POST handler at /messages/. This is where the client sends
# messages back to the server in SSE mode.
app.mount("/messages/", app=sse_transport.handle_post_message)


# ── Convenience: health, tool list ─────────────────────────────────────────


@app.get("/health")
async def health():
    if not mcp_server.engine:
        return JSONResponse({"status": "starting"}, status_code=503)
    try:
        h = await mcp_server.engine.get_health()
        return h.to_dict()
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)


@app.get("/tools")
async def list_tools():
    """List available MCP tools (informational, not part of the MCP protocol)."""
    return {
        "tools": [
            {"name": "memory_store", "description": "Store a memory"},
            {"name": "memory_search", "description": "Search memories (three-tier recall)"},
            {"name": "memory_recall", "description": "Recall memories for context injection"},
            {"name": "memory_forget", "description": "Delete a memory"},
            {"name": "memory_consolidate", "description": "Merge near-duplicate memories"},
            {"name": "memory_connect", "description": "Discover cross-category connections"},
            {"name": "memory_feedback", "description": "Report which search results were useful"},
        ],
        "transports": {
            "streamable_http": "/mcp",
            "sse": "/sse",
            "rest": ["/store", "/search", "/recall", "/forget", "/consolidate", "/connect", "/feedback"],
        },
    }


# ── REST API (kept for OpenClaw plugin and direct consumers) ───────────────


@app.post("/store")
async def store(request: Request):
    body = await request.json()
    text = body.get("text", "")
    category = body.get("category", "other")
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    try:
        doc_id, resolved_category = await mcp_server.engine.store(
            content=text, category=category, metadata=body.get("metadata")
        )
        return {"success": True, "memory_id": doc_id, "category": resolved_category}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/search")
async def search(request: Request):
    body = await request.json()
    query = body.get("query", "")
    limit = body.get("limit", 10)
    category = body.get("category")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    try:
        results = await mcp_server.engine.search(
            query=query, top_k=limit, category=category
        )
        return {
            "query": query,
            "total_results": len(results),
            "tiers_used": list(set(r.tier for r in results)),
            "results": [r.to_dict() for r in results],
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/forget")
async def forget(request: Request):
    body = await request.json()
    memory_id = body.get("memory_id")
    query = body.get("query")
    try:
        if memory_id:
            success = await mcp_server.engine.forget(memory_id)
            return {"success": success, "deleted": memory_id}
        if query:
            results = await mcp_server.engine.search(query=query, top_k=1)
            if not results:
                return {"success": False, "error": "No matching memory"}
            success = await mcp_server.engine.forget(results[0].doc_id)
            return {"success": success, "deleted": results[0].doc_id}
        return {"success": False, "error": "Provide memory_id or query"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/consolidate")
async def consolidate(request: Request):
    body = await request.json()
    if not mcp_server.engine.consolidator:
        return JSONResponse({"error": "Consolidator not available"}, status_code=503)
    try:
        result = await mcp_server.engine.consolidator.consolidate(
            threshold=body.get("threshold", 0.95)
        )
        return {"success": True, **result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/connect")
async def connect(request: Request):
    body = await request.json()
    doc_id = body.get("memory_id")
    query = body.get("query")
    if not mcp_server.engine.consolidator:
        return JSONResponse({"error": "Consolidator not available"}, status_code=503)
    try:
        if not doc_id and query:
            results = await mcp_server.engine.search(query=query, top_k=1)
            if results:
                doc_id = results[0].doc_id
        if not doc_id:
            return {"success": False, "error": "Provide memory_id or query"}
        result = await mcp_server.engine.consolidator.connect(
            doc_id=doc_id, max_connections=body.get("max_connections", 3)
        )
        return {"success": True, **result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/feedback")
async def feedback(request: Request):
    body = await request.json()
    query = body.get("query", "")
    selected_ids = body.get("selected_ids", [])
    rejected_ids = body.get("rejected_ids", [])

    if not query or not selected_ids:
        return JSONResponse({"error": "query and selected_ids are required"}, status_code=400)

    result = await mcp_server.engine.ingest_rerank_feedback(
        query=query,
        selected_ids=selected_ids,
        rejected_ids=rejected_ids,
    )
    return JSONResponse(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8585"))
    logger.info(f"Starting Engram MCP server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
