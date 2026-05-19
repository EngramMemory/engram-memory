# @engrammemory/mcp-server

Node.js MCP server for [Engram Memory](https://engrammemory.ai) — persistent semantic memory for AI agents.

Works with Claude Desktop, Claude Code, Cursor, Windsurf, and any MCP-compatible client.

## Quick start

**1. Run the Engram container:**

```bash
docker run -d --name engram-memory \
    -p 8585:8585 \
    -v engram-data:/data \
    engrammemory/engram-memory:latest
```

**2. Add to Claude Desktop:**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "engrammemory": {
      "command": "npx",
      "args": ["-y", "@engrammemory/mcp-server"]
    }
  }
}
```

Restart Claude Desktop. The memory tools appear automatically.

## Tools

| Tool | What it does | Hints |
|---|---|---|
| **memory_store** | Store a memory with semantic embedding | write |
| **memory_search** | Three-tier search (hot cache, hash index, vector) | read-only |
| **memory_recall** | Recall context (higher threshold, for auto-injection) | read-only |
| **memory_forget** | Delete a memory from all tiers | destructive |
| **memory_consolidate** | Find and merge near-duplicate memories | destructive |
| **memory_feedback** | Report which results were useful (improves recall) | write |
| **memory_connect** | Discover cross-category connections via entity graph | read-only |
| **memory_graph** | Render a `{nodes, edges}` spec into a self-contained interactive HTML graph (vis.js) | write |

## Prompts

| Prompt | What it does |
|---|---|
| **graph** | End-to-end `/graph` workflow: the model fetches memories, extracts entities, builds edges, then calls `memory_graph` to render. Surfaces as `/mcp__engrammemory__graph` in Claude Code. Optional args: `focus` (topic bias), `limit` (max memories). |

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `ENGRAM_URL` | `http://localhost:8585` | URL of the Engram Memory container |

## How it works

This is a lightweight Node.js MCP server that proxies tool calls to your local Engram Memory container. All data stays on your machine — the container runs Qdrant (vector DB) and FastEmbed (embeddings) locally.

The server communicates with Claude via stdio (standard MCP transport) and forwards each tool call to the container's MCP endpoint over HTTP.

## Privacy

All memory data is stored locally in your Docker volume. No data is sent to external services unless you explicitly configure the Engram Cloud API for cross-device hive sharing.

See our [Privacy Policy](https://engrammemory.ai/privacy) for details.

## License

MIT — see [LICENSE](../../LICENSE).
