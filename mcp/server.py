#!/usr/bin/env python3
"""
Engram Memory — MCP Server (Three-Tier Recall Engine)

Universal MCP server exposing memory tools to any MCP-compatible client:
Claude Code, Cursor, Windsurf, VS Code, and other editors.

Now powered by the three-tier recall engine:
  Tier 1: Hot-Tier Cache (sub-ms, in-memory)
  Tier 2: Multi-Head Hash Index (O(1) candidate lookup)
  Tier 3: Qdrant Vector Search (full ANN fallback)

Usage:
    # Claude Code
    claude mcp add engrammemory -- python mcp/server.py

    # Cursor / Windsurf / VS Code — add to .mcp.json:
    {
      "mcpServers": {
        "engrammemory": {
          "command": "python",
          "args": ["mcp/server.py"]
        }
      }
    }

Environment Variables:
    QDRANT_HOST         - Qdrant host (default: localhost)
    QDRANT_PORT         - Qdrant port (default: 6333)
    FASTEMBED_URL       - FastEmbed service URL (default: http://localhost:11435)
    COLLECTION_NAME     - Qdrant collection name (default: agent-memory)
    DEBUG               - Enable debug logging (default: false)
    ENGRAM_API_KEY      - Cloud API key for hive access. Use /hive to select
                          which hive to route memory ops to.
    ENGRAM_API_URL      - Cloud API endpoint (default: https://api.engrammemory.ai)
"""

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# Add src/recall to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "recall"))

try:
    from recall_engine import EngramRecallEngine
    from models import EngramConfig
    RECALL_ENGINE_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Recall engine not available ({e}). Falling back to direct Qdrant.", file=sys.stderr)
    RECALL_ENGINE_AVAILABLE = False

try:
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    from mcp.types import (
        CallToolRequest, CallToolResult, TextContent, Tool, ToolAnnotations,
        ListToolsRequest, ListToolsResult,
    )
except ImportError:
    print("Error: mcp package not found. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "").lower() in ["true", "1"] else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("engram-mcp")


class EngramMCPServer:
    """MCP Server with three-tier recall: hot cache → hash index → vector search."""

    def __init__(self, config: EngramConfig):
        self.config = config
        self._api_key = config.api_key
        self._api_url = config.api_url
        self.engine: Optional[EngramRecallEngine] = None
        self.server = Server("engrammemory")
        self._register_tools()

        logger.info("Engram MCP Server initialized:")
        logger.info(f"  Qdrant: {config.qdrant_url}")
        logger.info(f"  FastEmbed: {config.embedding_url}")
        logger.info(f"  Collection: {config.collection}")
        logger.info(f"  Recall Engine: {'enabled' if RECALL_ENGINE_AVAILABLE else 'disabled (fallback mode)'}")
        if self._api_key:
            logger.info(f"  Cloud: connected ({self._api_url})")

    async def startup(self):
        if RECALL_ENGINE_AVAILABLE:
            self.engine = EngramRecallEngine(self.config)
            await self.engine.warmup()
            logger.info("Recall engine warmed up — three-tier search active")
        else:
            logger.warning("Running without recall engine — single-tier Qdrant only")

    async def shutdown(self):
        """Persist state and clean up."""
        if self.engine:
            await self.engine.shutdown()
            logger.info("Recall engine shut down — state persisted")

    def _get_active_hive(self) -> tuple[Optional[str], Optional[str]]:
        path = os.path.join(self.config.data_dir, "active_hive")
        try:
            with open(path, "r") as f:
                lines = f.read().strip().splitlines()
                hive_id = lines[0].strip() if lines else None
                activated_at = lines[1].strip() if len(lines) > 1 else None
                return (hive_id if hive_id else None, activated_at)
        except FileNotFoundError:
            return (None, None)

    # ── Tool Registration ───────────────────────────────────────────

    def _register_tools(self):
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="memory_store",
                    title="Store Memory",
                    description="Store a memory with semantic embedding (indexed into hot-tier cache and hash index)",
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text content to store"},
                            "category": {
                                "type": "string",
                                "enum": ["preference", "fact", "decision", "entity", "other"],
                                "default": "other",
                                "description": "Memory category",
                            },
                            "importance": {
                                "type": "number",
                                "default": 0.5,
                                "minimum": 0.0,
                                "maximum": 1.0,
                                "description": "Importance score (0-1)",
                            },
                            "private": {
                                "type": "boolean",
                                "default": False,
                                "description": "Mark memory as private. Private memories are excluded from exports, consolidation, and graph connections.",
                            },
                            "share_with": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Hive scopes to also write this memory to (e.g. [\"hive:uuid\"]). Requires ENGRAM_API_KEY.",
                            },
                        },
                        "required": ["text"],
                    },
                ),
                Tool(
                    name="memory_search",
                    title="Search Memories",
                    description="Search memories using three-tier recall. Results include match_context to help you identify the most relevant result.",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Natural language search query"},
                            "limit": {"type": "integer", "default": 10, "description": "Max results"},
                            "category": {
                                "type": "string",
                                "enum": ["preference", "fact", "decision", "entity", "other"],
                                "description": "Filter by category",
                            },
                            "detail": {
                                "type": "string",
                                "enum": ["compact", "full"],
                                "default": "compact",
                                "description": "Response detail level. compact returns 6 fields, full returns all 14.",
                            },
                            "scope": {
                                "type": "string",
                                "description": "Search scope — when set, searches cloud API instead of local recall (e.g. \"hive:uuid\"). Requires ENGRAM_API_KEY.",
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="memory_get",
                    title="Get Memory Details",
                    description="Fetch full details for specific memory IDs. Use after memory_search to get complete content for relevant results.",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "memory_id": {
                                "type": "string",
                                "description": "Single memory UUID to fetch",
                            },
                            "memory_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 10,
                                "description": "Batch fetch up to 10 memory UUIDs",
                            },
                        },
                    },
                ),
                Tool(
                    name="memory_timeline",
                    title="Memory Timeline",
                    description="Browse recent memories chronologically. Returns compact results sorted by creation time.",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "default": 24,
                                "description": "Look back N hours (default 24)",
                            },
                            "category": {
                                "type": "string",
                                "enum": ["preference", "fact", "decision", "entity", "other"],
                                "description": "Filter by category",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 20,
                                "maximum": 50,
                                "description": "Maximum results (default 20, max 50)",
                            },
                        },
                    },
                ),
                Tool(
                    name="memory_recall",
                    title="Recall Context",
                    description="Recall relevant memories for context injection (higher threshold, designed for auto-recall)",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "context": {"type": "string", "description": "Context to recall memories for"},
                            "limit": {"type": "integer", "default": 5, "description": "Max memories to recall"},
                        },
                        "required": ["context"],
                    },
                ),
                Tool(
                    name="memory_forget",
                    title="Forget Memory",
                    description="Delete a memory from all tiers (hot cache, hash index, and vector store)",
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "memory_id": {"type": "string", "description": "UUID of memory to delete"},
                            "query": {"type": "string", "description": "Search query to find and delete the best match"},
                        },
                    },
                ),
                Tool(
                    name="memory_consolidate",
                    title="Consolidate Memories",
                    description=(
                        "Find and merge near-duplicate memories. "
                        "Threshold configurable via ENGRAM_DEDUP_THRESHOLD env var."
                    ),
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "threshold": {
                                "type": "number",
                                "default": 0.95,
                                "description": "Similarity threshold for deduplication (default 0.95)",
                            },
                        },
                    },
                ),
                Tool(
                    name="memory_feedback",
                    title="Give Feedback",
                    description=(
                        "Report which search results were useful. "
                        "After using memory_search results, call this to help Engram learn "
                        "which memories are most relevant. This improves future search accuracy "
                        "at zero cost — your model already evaluated the results."
                    ),
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The original search query",
                            },
                            "selected_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Memory IDs that were useful/relevant",
                            },
                            "rejected_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Memory IDs that were not relevant (optional)",
                            },
                        },
                        "required": ["query", "selected_ids"],
                    },
                ),
                Tool(
                    name="memory_connect",
                    title="Discover Connections",
                    description=(
                        "Discover cross-category connections for a memory via the entity graph."
                    ),
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "memory_id": {"type": "string", "description": "UUID of memory to connect"},
                            "query": {"type": "string", "description": "Search to find the memory first"},
                            "max_connections": {"type": "integer", "default": 3, "description": "Max connections to discover"},
                        },
                    },
                ),
                Tool(
                    name="hive_list",
                    title="List Hives",
                    description="List all hives the authenticated user has access to. Requires ENGRAM_API_KEY.",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name="hive_create",
                    title="Create Hive",
                    description="Create a new shared memory hive. Requires ENGRAM_API_KEY.",
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Human-readable hive name"},
                            "slug": {
                                "type": "string",
                                "description": "URL-safe identifier — lowercase alphanumeric + hyphens, 3-48 chars",
                            },
                        },
                        "required": ["name", "slug"],
                    },
                ),
                Tool(
                    name="hive_grant",
                    title="Grant Hive Access",
                    description="Grant an API key prefix access to a hive. Requires ENGRAM_API_KEY.",
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hive_id": {"type": "string", "description": "Hive UUID"},
                            "key_prefix": {"type": "string", "description": "API key prefix to grant access to"},
                            "permission": {
                                "type": "string",
                                "enum": ["read", "readwrite"],
                                "default": "readwrite",
                                "description": "Permission level",
                            },
                        },
                        "required": ["hive_id", "key_prefix"],
                    },
                ),
                Tool(
                    name="hive_revoke",
                    title="Revoke Hive Access",
                    description="Revoke an API key prefix's access to a hive. Requires ENGRAM_API_KEY.",
                    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hive_id": {"type": "string", "description": "Hive UUID"},
                            "key_prefix": {"type": "string", "description": "API key prefix to revoke"},
                        },
                        "required": ["hive_id", "key_prefix"],
                    },
                ),
                Tool(
                    name="hive_grants_list",
                    title="List Hive Grants",
                    description="List all active grants for a hive. Requires ENGRAM_API_KEY.",
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hive_id": {"type": "string", "description": "Hive UUID"},
                        },
                        "required": ["hive_id"],
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            logger.info(f"Tool call: {name} — {arguments}")

            if name == "memory_store":
                result = await self._handle_store(**arguments)
            elif name == "memory_search":
                result = await self._handle_search(**arguments)
            elif name == "memory_recall":
                result = await self._handle_recall(**arguments)
            elif name == "memory_forget":
                result = await self._handle_forget(**arguments)
            elif name == "memory_consolidate":
                result = await self._handle_consolidate(**arguments)
            elif name == "memory_connect":
                result = await self._handle_connect(**arguments)
            elif name == "memory_feedback":
                result = await self._handle_feedback(**arguments)
            elif name == "memory_get":
                result = await self._handle_get(arguments)
            elif name == "memory_timeline":
                result = await self._handle_timeline(arguments)
            elif name == "hive_list":
                result = await self._handle_hive_list()
            elif name == "hive_create":
                result = await self._handle_hive_create(**arguments)
            elif name == "hive_grant":
                result = await self._handle_hive_grant(**arguments)
            elif name == "hive_revoke":
                result = await self._handle_hive_revoke(**arguments)
            elif name == "hive_grants_list":
                result = await self._handle_hive_grants_list(**arguments)
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    # ── Tool Handlers ───────────────────────────────────────────────

    async def _handle_store(
        self,
        text: str,
        category: str = "other",
        importance: float = 0.5,
        private: bool = False,
        share_with: Optional[List[str]] = None,
        **_,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"success": False, "error": "Recall engine not available"}
        try:
            if self.engine:
                doc_id, resolved_category = await self.engine.store(
                    content=text,
                    category=category,
                    metadata={"importance": importance, "private": private},
                )
                logger.info(f"Stored memory {doc_id} via recall engine")
                result = {"success": True, "memory_id": doc_id, "category": resolved_category}
        except Exception as e:
            logger.error(f"Store failed: {e}")
            result = {"success": False, "error": str(e)}

        active_hive, activated_at = self._get_active_hive()
        all_targets: set = set(share_with or [])
        if active_hive:
            all_targets.add(f"hive:{active_hive}")

        if all_targets and self._api_key:
            hive_results = {}
            for scope in all_targets:
                hive_id = scope.removeprefix("hive:")
                cloud_resp = await self._cloud_request(
                    "POST",
                    f"/v1/hives/{hive_id}/memories",
                    body={"text": text, "category": category, "importance": importance},
                )
                hive_results[scope] = cloud_resp
                if hive_id == active_hive and result.get("memory_id"):
                    doc_id = result["memory_id"]
                    logger.info(f"Memory {doc_id} shared to hive:{active_hive} (active since {activated_at})")
            result["shared"] = hive_results

        return result

    async def _handle_search(
        self,
        query: str,
        limit: int = 10,
        category: Optional[str] = None,
        detail: str = "compact",
        scope: Optional[str] = None,
        **_,
    ) -> Dict[str, Any]:
        active_hive, activated_at = self._get_active_hive()
        if scope:
            target_hive = scope.removeprefix("hive:")
        elif active_hive:
            target_hive = active_hive
        else:
            target_hive = None

        # Hive active: cloud only — local pre-hive memories are excluded
        if target_hive and self._api_key:
            params = f"q={urllib.parse.quote(query)}&limit={limit}"
            if category:
                params += f"&category={urllib.parse.quote(category)}"
            cloud_resp = await self._cloud_request(
                "GET",
                f"/v1/hives/{target_hive}/memories/search?{params}",
            )
            memories = cloud_resp.get("results", cloud_resp.get("memories", []))
            return {
                "query": query,
                "total_results": len(memories),
                "active_hive": f"hive:{target_hive}",
                "activated_at": activated_at,
                "results": memories,
            }

        # No hive: local only
        try:
            if self.engine:
                results = await self.engine.search(query=query, top_k=limit, category=category)
                memories = [r.to_dict() if detail == "full" else r.to_compact_dict() for r in results]
                tiers_used = list(set(r.tier for r in results))
                return {
                    "query": query,
                    "total_results": len(memories),
                    "tiers_used": tiers_used,
                    "active_hive": None,
                    "results": memories,
                }
            return {"query": query, "total_results": 0, "results": [], "error": "Recall engine not available"}
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return {"query": query, "total_results": 0, "results": [], "error": str(e)}

    async def _handle_recall(
        self, context: str, limit: int = 5, **_
    ) -> Dict[str, Any]:
        """Recall relevant memories for context. Routes to cloud hive when one is active."""
        return await self._handle_search(query=context, limit=limit)

    async def _handle_forget(
        self, memory_id: Optional[str] = None, query: Optional[str] = None, **_
    ) -> Dict[str, Any]:
        try:
            if not self.engine:
                return {"success": False, "error": "Recall engine not available"}

            if memory_id:
                success = await self.engine.forget(memory_id)
                return {"success": success, "deleted": memory_id}

            if query:
                results = await self.engine.search(query=query, top_k=1)
                if not results:
                    return {"success": False, "error": "No matching memory found"}
                target = results[0]
                success = await self.engine.forget(target.doc_id)
                return {"success": success, "deleted": target.doc_id, "text": target.content[:80]}

            return {"success": False, "error": "Provide either memory_id or query"}
        except Exception as e:
            logger.error(f"Forget failed: {e}")
            return {"success": False, "error": str(e)}

    async def _handle_consolidate(
        self, threshold: float = 0.95, **_
    ) -> Dict[str, Any]:
        try:
            if not self.engine or not self.engine.consolidator:
                return {"success": False, "error": "Consolidator not available"}
            result = await self.engine.consolidator.consolidate(threshold=threshold)
            return {"success": True, **result}
        except Exception as e:
            logger.error(f"Consolidate failed: {e}")
            return {"success": False, "error": str(e)}

    async def _handle_connect(
        self, memory_id: Optional[str] = None, query: Optional[str] = None,
        max_connections: int = 3, **_
    ) -> Dict[str, Any]:
        try:
            if not self.engine or not self.engine.consolidator:
                return {"success": False, "error": "Consolidator not available"}

            # If query provided, find the memory first
            if not memory_id and query:
                results = await self.engine.search(query=query, top_k=1)
                if not results:
                    return {"success": False, "error": "No matching memory found"}
                memory_id = results[0].doc_id

            if not memory_id:
                return {"success": False, "error": "Provide memory_id or query"}

            result = await self.engine.consolidator.connect(
                doc_id=memory_id, max_connections=max_connections
            )
            return {"success": True, **result}
        except Exception as e:
            logger.error(f"Connect failed: {e}")
            return {"success": False, "error": str(e)}


    async def _handle_feedback(
        self, query: str, selected_ids: list, rejected_ids: list | None = None, **_
    ) -> Dict[str, Any]:
        """Handle memory_feedback tool calls."""
        if not self.engine:
            return {"error": "Recall engine not available"}
        result = await self.engine.ingest_rerank_feedback(
            query=query,
            selected_ids=selected_ids,
            rejected_ids=rejected_ids,
        )
        return result


    async def _handle_get(self, arguments: dict) -> Dict[str, Any]:
        """Handle memory_get tool calls."""
        memory_id = arguments.get("memory_id")
        memory_ids = arguments.get("memory_ids", [])

        ids = []
        if memory_id:
            ids.append(memory_id)
        if memory_ids:
            ids.extend(memory_ids)

        if not ids:
            return {"success": False, "error": "Provide memory_id or memory_ids"}

        ids = ids[:10]  # Cap at 10
        results = await self.engine.get_by_ids(ids)
        return {
            "total_results": len(results),
            "results": [r.to_dict() for r in results],
        }

    async def _handle_timeline(self, arguments: dict) -> Dict[str, Any]:
        """Handle memory_timeline tool calls."""
        hours = arguments.get("hours", 24)
        category = arguments.get("category")
        limit = min(arguments.get("limit", 20), 50)

        results = await self.engine.timeline(hours=hours, category=category, limit=limit)
        return {
            "hours": hours,
            "total_results": len(results),
            "results": [r.to_compact_dict() for r in results],
        }

    # ── Hive Handlers ───────────────────────────────────────────────

    async def _handle_hive_list(self) -> Dict[str, Any]:
        resp = await self._cloud_request("GET", "/v1/hives")
        if "error" in resp:
            return {"success": False, **resp}
        hives = resp.get("hives", resp if isinstance(resp, list) else [])
        return {"success": True, "hives": hives, "total": len(hives)}

    async def _handle_hive_create(self, name: str, slug: str, **_) -> Dict[str, Any]:
        resp = await self._cloud_request("POST", "/v1/hives", body={"name": name, "slug": slug})
        if "error" in resp:
            return {"success": False, **resp}
        return {"success": True, **resp}

    async def _handle_hive_grant(
        self,
        hive_id: str,
        key_prefix: str,
        permission: str = "readwrite",
        **_,
    ) -> Dict[str, Any]:
        resp = await self._cloud_request(
            "POST",
            f"/v1/hives/{hive_id}/grants",
            body={"key_prefix": key_prefix, "permission": permission},
        )
        if "error" in resp:
            return {"success": False, **resp}
        return {"success": True, **resp}

    async def _handle_hive_revoke(self, hive_id: str, key_prefix: str, **_) -> Dict[str, Any]:
        resp = await self._cloud_request(
            "DELETE",
            f"/v1/hives/{hive_id}/grants/{key_prefix}",
        )
        if "error" in resp:
            return {"success": False, **resp}
        return {"success": True, **resp}

    async def _handle_hive_grants_list(self, hive_id: str, **_) -> Dict[str, Any]:
        resp = await self._cloud_request("GET", f"/v1/hives/{hive_id}/grants")
        if "error" in resp:
            return {"success": False, **resp}
        grants = resp.get("grants", [])
        return {"success": True, "hive_id": hive_id, "grants": grants, "total": len(grants)}

    # ── Cloud HTTP ──────────────────────────────────────────────────

    async def _cloud_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if not self._api_key:
            return {"error": "ENGRAM_API_KEY not configured"}
        url = f"{self._api_url.rstrip('/')}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        def _do_request() -> Dict[str, Any]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode()
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"error": f"Non-JSON response from cloud API (status {resp.status})"}
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read().decode())
                except Exception:
                    detail = {}
                return {"error": e.reason, "status": e.code, **detail}
            except Exception as exc:
                return {"error": str(exc)}

        return await asyncio.to_thread(_do_request)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Engram Memory MCP Server")
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--fastembed-url", default=os.getenv("FASTEMBED_URL", "http://localhost:11435"))
    parser.add_argument("--collection", default=os.getenv("COLLECTION_NAME", "agent-memory"))
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", ".engram"),
                        help="Directory for hot-tier, hash index, and graph persistence")

    args = parser.parse_args()

    config = EngramConfig(
        qdrant_url=args.qdrant_url,
        embedding_url=args.fastembed_url,
        collection=args.collection,
        data_dir=args.data_dir,
        api_key=os.getenv("ENGRAM_API_KEY", ""),
        api_url=os.getenv("ENGRAM_API_URL", "https://api.engrammemory.ai"),
        debug=os.getenv("DEBUG", "").lower() in ["true", "1"],
    )

    mcp_server = EngramMCPServer(config)
    await mcp_server.startup()

    from mcp.server.stdio import stdio_server

    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="engrammemory",
                    server_version="2.0.0",
                    capabilities=mcp_server.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await mcp_server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
