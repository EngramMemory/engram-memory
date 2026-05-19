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
import { renderGraph } from "./graph.js";

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
  version: "0.2.0",
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

server.tool(
  "memory_graph",
  "Render an interactive HTML visualisation of memories. Pass a host-LLM-built {nodes, edges} spec; returns the path to a self-contained graph.html (vis.js, no server required). Pair with the 'graph' prompt for end-to-end /graph behaviour.",
  {
    nodes: z
      .array(
        z.object({
          id: z.string().describe("Stable identifier (use the memory id)"),
          label: z.string().describe("Short summary, ≤60 chars"),
          category: z
            .string()
            .optional()
            .describe("preference | decision | fact | entity | other"),
          content: z
            .string()
            .optional()
            .describe("First ~200 chars of the memory text"),
          entities: z
            .array(z.string())
            .optional()
            .describe("Noun phrases / named entities you identified"),
        }),
      )
      .describe("Graph nodes (one per memory)"),
    edges: z
      .array(
        z.object({
          source: z.string(),
          target: z.string(),
          type: z
            .string()
            .optional()
            .describe(
              "shared-entity | reference | temporal | topic | related",
            ),
          label: z.string().optional(),
          weight: z
            .number()
            .min(0)
            .max(1)
            .optional()
            .describe("Confidence 0–1; edges <0.3 are visually de-emphasised"),
        }),
      )
      .default([])
      .describe("Graph edges (skip weak/uncertain ones)"),
    title: z
      .string()
      .optional()
      .describe("Page title shown in the legend"),
    output_dir: z
      .string()
      .optional()
      .describe(
        "Where to write graph.html (default: ~/.engram/graph-<timestamp>/)",
      ),
  },
  { readOnlyHint: false, destructiveHint: false, title: "Render Memory Graph" },
  async ({ nodes, edges, title, output_dir }) => {
    try {
      const result = renderGraph(
        { nodes, edges, title },
        output_dir,
      );
      const msg = `Memory graph ready: ${result.htmlPath} — ${result.nodes} nodes, ${result.edges} edges, ${result.communities} communities. Open in a browser to explore.`;
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              {
                html_path: result.htmlPath,
                nodes: result.nodes,
                edges: result.edges,
                communities: result.communities,
                message: msg,
              },
              null,
              2,
            ),
          },
        ],
      };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return {
        isError: true,
        content: [
          { type: "text" as const, text: `memory_graph failed: ${message}` },
        ],
      };
    }
  },
);

// ── Prompts ─────────────────────────────────────────────────────────

server.prompt(
  "graph",
  "Build an interactive vis.js graph of your Engram memories. The model does entity extraction; memory_graph renders.",
  {
    focus: z
      .string()
      .optional()
      .describe(
        "Optional topic to bias the recall query (e.g. 'auth refactor'). If omitted, pulls a broad cross-section.",
      ),
    limit: z
      .string()
      .optional()
      .describe(
        "Optional max memories to include (default 1000). Pass as a string.",
      ),
  },
  ({ focus, limit }) => {
    const focusLine = focus
      ? `Bias the initial recall query toward: ${focus}.`
      : "Pull a broad cross-section of memories.";
    const limitNum = limit ? Number.parseInt(limit, 10) || 1000 : 1000;
    return {
      messages: [
        {
          role: "user" as const,
          content: {
            type: "text" as const,
            text: [
              "Build an interactive graph of my Engram memories.",
              "",
              focusLine,
              "",
              "Steps:",
              `1. Call memory_search with a broad query (use "${focus || "*"}") and limit=${limitNum} to fetch memories. If that returns nothing, fall back to memory_recall with a broad prompt.`,
              "2. If zero memories come back, tell me exactly: 'No memories stored yet — store some via the engram MCP tools first.' and stop.",
              "3. For each memory, build one node: {id, label (≤60 chars summary you write), category, content (first ~200 chars), entities (noun phrases / named entities YOU identify by reading the content — people, projects, technologies, file paths, repos, decisions; lowercase; deduped per node)}.",
              "4. Build edges by reasoning over the nodes:",
              "   - Shared entity → {type:'shared-entity', label:<entity>, weight:0.8}",
              "   - Explicit reference (one names another) → {type:'reference', weight:0.95}",
              "   - Timestamps within ~10 minutes → {type:'temporal', weight:0.4}",
              "   - Clear thematic overlap without a shared named entity → {type:'topic', label:<theme>, weight:0.5}",
              "   Skip edges with weight <0.3. Dedupe symmetric duplicates.",
              "5. Call memory_graph with {nodes, edges, title}. It writes a self-contained HTML file and returns the path.",
              "6. Report the path back to me with the node/edge/community counts.",
              "",
              "Do not modify the memory store. Do not invent memories. Extraction is read-only.",
            ].join("\n"),
          },
        },
      ],
    };
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
