#!/usr/bin/env node
/**
 * Engram Memory — Node.js MCP Server
 *
 * Lightweight MCP server that proxies to a local Engram Memory container.
 * Exposes 7 memory tools: store, search, recall, forget, consolidate,
 * feedback, and connect.
 *
 * Usage:
 *   npx @engrammemory/mcp-server
 *   ENGRAM_URL=http://localhost:8585 npx @engrammemory/mcp-server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const ENGRAM_URL = process.env.ENGRAM_URL || "http://localhost:8585";

// ── HTTP helper ─────────────────────────────────────────────────────

async function engramCall(
  endpoint: string,
  body: Record<string, unknown>,
): Promise<unknown> {
  const res = await fetch(`${ENGRAM_URL}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Engram API ${endpoint} returned ${res.status}: ${text}`);
  }
  return res.json();
}

async function engramGet(endpoint: string): Promise<unknown> {
  const res = await fetch(`${ENGRAM_URL}${endpoint}`);
  if (!res.ok) {
    throw new Error(`Engram API ${endpoint} returned ${res.status}`);
  }
  return res.json();
}

// ── Server setup ────────────────────────────────────────────────────

const server = new McpServer({
  name: "engrammemory",
  version: "0.1.0",
});

// ── Tools ───────────────────────────────────────────────────────────

server.tool(
  "memory_store",
  "Store a memory with semantic embedding (indexed into hot-tier cache and hash index)",
  {
    text: z.string().describe("Text content to store"),
    category: z
      .enum(["preference", "fact", "decision", "entity", "other"])
      .default("other")
      .describe("Memory category"),
    importance: z
      .number()
      .min(0)
      .max(1)
      .default(0.5)
      .describe("Importance score (0-1)"),
  },
  { readOnlyHint: false, destructiveHint: false, idempotentHint: true, title: "Store Memory" },
  async ({ text, category, importance }) => {
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_store", arguments: { text, category, importance } },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_search",
  "Search memories using three-tier recall. Results include match_context to help identify the most relevant result.",
  {
    query: z.string().describe("Natural language search query"),
    limit: z.number().int().default(10).describe("Max results"),
    category: z
      .enum(["preference", "fact", "decision", "entity", "other"])
      .optional()
      .describe("Filter by category"),
  },
  { readOnlyHint: true, destructiveHint: false, title: "Search Memories" },
  async ({ query, limit, category }) => {
    const args: Record<string, unknown> = { query, limit };
    if (category) args.category = category;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_search", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_recall",
  "Recall relevant memories for context injection (higher threshold, designed for auto-recall)",
  {
    context: z.string().describe("Context to recall memories for"),
    limit: z.number().int().default(5).describe("Max memories to recall"),
  },
  { readOnlyHint: true, destructiveHint: false, title: "Recall Context" },
  async ({ context, limit }) => {
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_recall", arguments: { context, limit } },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_forget",
  "Delete a memory from all tiers (hot cache, hash index, and vector store)",
  {
    memory_id: z.string().optional().describe("UUID of memory to delete"),
    query: z.string().optional().describe("Search query to find and delete the best match"),
  },
  { readOnlyHint: false, destructiveHint: true, title: "Forget Memory" },
  async ({ memory_id, query }) => {
    const args: Record<string, unknown> = {};
    if (memory_id) args.memory_id = memory_id;
    if (query) args.query = query;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_forget", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_consolidate",
  "Find and merge near-duplicate memories",
  {
    threshold: z
      .number()
      .default(0.95)
      .describe("Similarity threshold for deduplication (default 0.95)"),
  },
  { readOnlyHint: false, destructiveHint: true, title: "Consolidate Memories" },
  async ({ threshold }) => {
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_consolidate", arguments: { threshold } },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_feedback",
  "Report which search results were useful. Improves future search accuracy at zero cost.",
  {
    query: z.string().describe("The original search query"),
    selected_ids: z.array(z.string()).describe("Memory IDs that were useful/relevant"),
    rejected_ids: z
      .array(z.string())
      .optional()
      .describe("Memory IDs that were not relevant (optional)"),
  },
  { readOnlyHint: false, destructiveHint: false, title: "Give Feedback" },
  async ({ query, selected_ids, rejected_ids }) => {
    const args: Record<string, unknown> = { query, selected_ids };
    if (rejected_ids) args.rejected_ids = rejected_ids;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_feedback", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_connect",
  "Discover cross-category connections for a memory via the entity graph",
  {
    memory_id: z.string().optional().describe("UUID of memory to connect"),
    query: z.string().optional().describe("Search to find the memory first"),
    max_connections: z.number().int().default(3).describe("Max connections to discover"),
  },
  { readOnlyHint: true, destructiveHint: false, title: "Discover Connections" },
  async ({ memory_id, query, max_connections }) => {
    const args: Record<string, unknown> = { max_connections };
    if (memory_id) args.memory_id = memory_id;
    if (query) args.query = query;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_connect", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_get",
  "Fetch full details for specific memory IDs. Use after memory_search to get complete content.",
  {
    memory_id: z.string().optional().describe("Single memory UUID"),
    memory_ids: z.array(z.string()).max(10).optional().describe("Batch fetch up to 10 UUIDs"),
  },
  { readOnlyHint: true, destructiveHint: false, title: "Get Memory Details" },
  async ({ memory_id, memory_ids }) => {
    const args: Record<string, unknown> = {};
    if (memory_id) args.memory_id = memory_id;
    if (memory_ids) args.memory_ids = memory_ids;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_get", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

server.tool(
  "memory_timeline",
  "Browse recent memories chronologically. Returns compact results sorted by creation time.",
  {
    hours: z.number().int().default(24).optional().describe("Look back N hours"),
    category: z
      .enum(["preference", "fact", "decision", "entity", "other"])
      .optional()
      .describe("Filter by category"),
    limit: z.number().int().max(50).default(20).optional().describe("Max results"),
  },
  { readOnlyHint: true, destructiveHint: false, title: "Memory Timeline" },
  async ({ hours, category, limit }) => {
    const args: Record<string, unknown> = {};
    if (hours !== undefined) args.hours = hours;
    if (category) args.category = category;
    if (limit !== undefined) args.limit = limit;
    const result = await engramCall("/mcp", {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "memory_timeline", arguments: args },
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
  },
);

// ── Start ───────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
