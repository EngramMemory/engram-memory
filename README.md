<div align="center">

<img src="assets/logo.svg" alt="Engram Memory" width="360" />

**Three-Tiered Brain for AI agents. Self-hosted. Zero API costs.**

[Docs](https://engrammemory.ai/docs) · [Quickstart](#quick-start) · [Dashboard](https://app.engrammemory.ai) · [Cloud SDKs](#engram-cloud)

![npm](https://img.shields.io/npm/v/engrammemory-ai?label=npm&color=10b981&style=flat-square)
![pypi](https://img.shields.io/pypi/v/engrammemory-ai?label=pypi&color=10b981&style=flat-square)
![docs](https://img.shields.io/badge/docs-engrammemory.ai-10b981?style=flat-square)
![license](https://img.shields.io/badge/license-BSL--1.1-green?style=flat-square)
[![SafeSkill 95/100](https://img.shields.io/badge/SafeSkill-95%2F100_Verified%20Safe-brightgreen)](https://safeskill.dev/scan/engrammemory-engram-memory-community)

</div>

---

Engram gives your AI agent persistent memory across sessions. Store, search, recall, and forget memories using semantic embeddings — all running on your own hardware. No API keys, no cloud dependencies, no data leaving your machine.

One container bundles **Qdrant**, **FastEmbed**, and the **MCP server**. One command to install. Works with Claude Code, Cursor, Windsurf, VS Code, OpenClaw, or anything else that speaks MCP.

---

## The Problem

Every AI agent on the market forgets everything when the session ends. You spend 30 minutes explaining your codebase, your preferences, your architectural decisions. You close the tab. Tomorrow you start over.

The current solutions are all bad in different ways. OpenAI's memory is a black box you don't control. Mem0 and Zep charge $19–$249/month for managed cloud memory — your data goes through their servers. Local alternatives (LangChain memory, SQLite stores) don't scale past a few thousand memories and treat every memory equally regardless of how often you access it.

Engram exists because there should be a third option: a serious memory system that runs on your own hardware, costs nothing, and gets faster the more you use it.

---

## How It Works

### The Three-Tiered Brain

Most memory systems do one thing: vector search across everything. That's slow at scale and wasteful for queries you've made before. Engram has three tiers, and a query flows through them in order:

**Tier 1 — Hot-Tier Cache (sub-millisecond lookup)**
The memories you access most often live in an in-memory frequency cache. Each memory has an activation strength that grows with every access and decays exponentially with time — based on the ACT-R cognitive architecture from memory science. When you query, the hot tier checks first. If your query matches a cached memory above the similarity threshold, the tier lookup completes in under a millisecond. No vector search. No disk read.

**Tier 2 — Multi-Head Hash Index (O(1) candidate retrieval)**
If the hot tier misses, the query hits a Locality Sensitive Hashing index. Engram takes the first 64 dimensions of the query embedding (using Matryoshka representation learning from the nomic-embed-text-v1.5 model), runs them through 4 independent hash functions, and looks up candidates in 4 hash buckets simultaneously. This returns a small candidate set in O(1) time. The multi-head design eliminates the false-positive problem that single-hash LSH is known for.

**Tier 3 — Hybrid Vector Search (semantic depth)**
The candidates get re-ranked using full 768-dimensional cosine similarity in Qdrant, combined with BM25 sparse vector search via Reciprocal Rank Fusion. This is the deep semantic search — but it runs on the candidate set from Tier 2, not your entire memory store. Top results get promoted into the hot tier so the next similar query is even faster.

### What this means in practice

The embedding step (generating a vector from your query text) takes ~25ms on Apple Silicon via ONNX. That's the floor for every query. On top of that:

- Repeat query (hot tier hit): **~25ms** total — the tier lookup is sub-ms, embedding dominates
- Similar query (hash + re-rank): **~30ms** total
- Novel query through full MCP pipeline (all tiers + graph expansion): **~190ms**

The more you use it, the more queries hit the fast path. That's the design.

### Community Edition Caps

Engram Community is a real product with deliberate limits:

| Feature | Community | Cloud |
|---|---|---|
| Hot-tier cache | 1,000 entries max | Unlimited |
| Hash index heads | 4 | 8+ with auto-tuning |
| Hash bit size | 12-bit (4,096 buckets) | 16-bit+ adaptive |
| Entity graph | 500 entities, 1-hop | Unlimited, multi-hop |
| Consolidation (dedup) | Manual, fixed 0.95 threshold | Auto-scheduled, tunable |
| Cross-category linking | 3 connections per call | Unlimited |
| ACT-R timestamps | 50 per memory | Unlimited |
| TurboQuant compression | — | ~6x storage reduction |
| Auto category detection | — | LLM-powered |
| Overflow storage | — | Cloud-backed spillover |
| Fleet coordination | — | Multi-agent isolation |
| Analytics dashboard | — | Usage, recall rates, health |

These caps are real. They exist because [Engram Cloud](https://engrammemory.ai) is how the project gets funded. Community is genuinely useful by itself — it just doesn't have the features that matter at scale.

---

## What You Get

Seven MCP tools + a visual graph command:

| Tool | What it does |
|---|---|
| `memory_store` | Save a memory with semantic embedding and auto-classification |
| `memory_search` | Three-tier recall search with confidence scoring and match context |
| `memory_recall` | Auto-inject relevant memories into agent context |
| `memory_forget` | Remove memories by ID or search match |
| `memory_consolidate` | Find and merge near-duplicate memories |
| `memory_connect` | Discover cross-category connections via the entity graph |
| `memory_feedback` | Report which search results were useful — improves future recall |
| `/graph` | Generate an interactive visual graph of your memories (Claude Code slash command) |

**Categories:** preference, fact, decision, entity, other — auto-detected by local keyword classifier.

The recall engine includes a Kuzu-backed entity graph for entity tracking, co-retrieval patterns, spreading activation, and `PREFERRED_OVER` edges from feedback signals. The `/graph` command renders your memory graph as an interactive vis.js visualization — the host LLM does entity extraction, the vendored [graphify](https://github.com/safishamsi/graphify) pipeline handles rendering.

---

## Quick Start

### 1. Start the container

```bash
docker run -d \
  --name engram-memory \
  --restart unless-stopped \
  -p 6333:6333 -p 11435:11435 -p 8585:8585 \
  -v engram_data:/data \
  engrammemory/engram-memory:latest
```

One container. Qdrant, FastEmbed (ONNX, native ARM64 + x86_64), and the MCP server all bundled inside, supervised by s6-overlay. Memories persist in the `engram_data` volume across restarts.

If you've cloned the repo, `bash scripts/setup.sh` does the same thing plus auto-registers the MCP with Claude Code and generates an OpenClaw config.

### 2. Connect your agent

**Claude Code:**
```bash
claude mcp add engrammemory -s user --transport http http://localhost:8585/mcp

# Install slash commands (/graph etc.)
mkdir -p ~/.claude/commands
docker cp engram-memory:/app/commands/. ~/.claude/commands/
```

**Cursor, Windsurf, VS Code, Claude Desktop, Cline, Zed, and 9 other clients** — one command via [`install-mcp`](https://www.npmjs.com/package/install-mcp):
```bash
npx -y install-mcp@latest http://localhost:8585/mcp \
    --client <your-client> --name engrammemory --oauth=no -y
```

**OpenClaw:**
```bash
git clone https://github.com/EngramMemory/engram-memory-community.git
cd engram-memory-community && bash scripts/install-plugin.sh
```

**Manual (any client)** — add to `.mcp.json`:
```json
{
  "mcpServers": {
    "engrammemory": {
      "type": "http",
      "url": "http://localhost:8585/mcp"
    }
  }
}
```

The container exposes four transports off the same recall engine:

| Transport | Endpoint | Use case |
|---|---|---|
| Streamable HTTP | `http://localhost:8585/mcp` | Modern MCP clients |
| SSE | `http://localhost:8585/sse` | Legacy MCP clients |
| Stdio | `docker exec -i engram-memory python /app/mcp_server.py` | Process-per-session |
| REST | `http://localhost:8585/{store,search,...}` | OpenClaw plugin, curl, custom tooling |

### 3. Use it

```python
memory_store("User prefers TypeScript over JavaScript", category="preference")
memory_search("language preferences")
memory_forget(query="old project requirements")
```

Start a conversation. Tell it something. Close the session. Come back tomorrow. It remembers.

---

## Architecture

```
┌─────────────────┐    ┌─────────────────────────────────────────────────┐
│   Your Agent    │    │       engrammemory/engram-memory (one image)    │
│   (Claude Code, │    │  ┌──────────────────────────────────────────┐  │
│    Cursor,      │───▶│  │       Three-Tier Recall Engine           │  │
│    OpenClaw,    │    │  │  Tier 1: Hot Cache  (sub-ms, ACT-R)      │  │
│    Gemini, ...) │    │  │  Tier 2: Hash Index (O(1) LSH, 6 heads)  │  │
│                 │    │  │  Tier 3: Qdrant ANN (dense + BM25 RRF)   │  │
└─────────────────┘    │  │  Graph:  Kuzu entity graph + feedback     │  │
                       │  └────────────────┬─────────────────────────┘  │
                       │                   │                            │
                       │   ┌───────────────┴────────────┐               │
                       │   │  FastEmbed ONNX  ─▶ Qdrant │               │
                       │   └────────────────────────────┘               │
                       │                                                │
                       │   Optional: ENGRAM_API_KEY extends with cloud  │
                       │   compression, dedup, overflow, and category   │
                       │   detection. Local processing stays primary.   │
                       └─────────────────────────────────────────────────┘
              One container. Persistent /data volume. Nothing leaves your network.
```

---

## Connecting to Engram Cloud (Optional)

Engram runs fully local by default. When you need TurboQuant compression, automatic deduplication, overflow storage, or auto-category detection, connect to [Engram Cloud](https://engrammemory.ai):

**For MCP users (Claude Code, Cursor, etc.):**
```bash
# Stop and restart the container with your API key
docker rm -f engram-memory
docker run -d --name engram-memory --restart unless-stopped \
  -p 6333:6333 -p 11435:11435 -p 8585:8585 \
  -v engram_data:/data \
  -e ENGRAM_API_KEY=eng_live_YOUR_KEY \
  engrammemory/engram-memory:latest
```

**For OpenClaw users:**
```bash
openclaw config set "plugins.entries.engram.config.apiKey" "eng_live_YOUR_KEY"
openclaw gateway restart
```

Cloud extends your local stack — it does not replace it. Your FastEmbed still generates embeddings locally. Your Qdrant still stores and searches locally. Cloud adds an intelligence layer on top: the API returns compressed vectors, dedup checks, and category detection for every store, and overflow results for every search when local results are insufficient.

Get an API key (free tier, no credit card) at [app.engrammemory.ai](https://app.engrammemory.ai).

**SDKs:**
- Python: `pip install engrammemory-ai` — [PyPI](https://pypi.org/project/engrammemory-ai/)
- Node: `npm install engrammemory-ai` — [npm](https://www.npmjs.com/package/engrammemory-ai)
- [Dashboard](https://app.engrammemory.ai) | [Privacy](https://engrammemory.ai/privacy)

---

## Configuration

### Container environment variables

| Variable | Default | Description |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector database |
| `FASTEMBED_URL` | `http://localhost:11435` | FastEmbed embedding service |
| `COLLECTION_NAME` | `agent-memory` | Qdrant collection name |
| `DATA_DIR` | `/data/engram` | Recall engine state (hot tier, hash index, graph) |
| `ENGRAM_API_KEY` | *(empty)* | Engram Cloud API key (enables cloud extensions) |
| `ENGRAM_API_URL` | `https://api.engrammemory.ai` | Cloud API endpoint |

### Tunable recall engine parameters

Every parameter is configurable via env var. Pass them to `docker run -e` or set in your compose file.

| Variable | Default | What it controls |
|---|---|---|
| `ENGRAM_HOT_TIER_MAX` | `1000` | Max entries in the in-memory hot cache (higher = more RAM, better hit rate) |
| `ENGRAM_HASH_HEADS` | `6` | Number of independent LSH hash tables (more = fewer false positives) |
| `ENGRAM_HASH_BITS` | `14` | Bits per hash signature (more = finer buckets, sparser tables) |
| `ENGRAM_GRAPH_MAX_ENTITIES` | `500` | Max entity nodes in the Kuzu graph |
| `ENGRAM_GRAPH_MAX_HOPS` | `1` | Graph traversal depth for spreading activation |
| `ENGRAM_ACTR_MAX_TIMESTAMPS` | `50` | Access timestamps stored per memory for ACT-R decay |
| `ENGRAM_DEDUP_THRESHOLD` | `0.95` | Cosine similarity threshold for memory_consolidate |
| `ENGRAM_MAX_CONNECTIONS` | `3` | Max connections per memory_connect call |

For OpenClaw plugin config options (`autoRecall`, `autoCapture`, `maxRecallResults`, `minRecallScore`), see [docs/OPENCLAW_INTEGRATION.md](docs/OPENCLAW_INTEGRATION.md).

---

## Requirements

- Docker
- 4 GB+ RAM
- 10 GB+ storage

Python 3.10+ only needed if running the stdio MCP server or CLI tools directly on the host.

---

## Data & Privacy

Engram Community is local-only by default. No data leaves your machine.

- Embeddings are generated by FastEmbed (ONNX) inside the container
- Vectors are stored in Qdrant inside the container
- No telemetry, no phone-home, no external API calls

When `ENGRAM_API_KEY` is set, the recall engine sends text to `api.engrammemory.ai` for compression, dedup, and category detection. The API returns metadata — your Qdrant still stores the vectors. See [engrammemory.ai/privacy](https://engrammemory.ai/privacy) for cloud data handling.

The Docker image is built from `docker/all-in-one/Dockerfile` in this repo. You can verify and rebuild it yourself.

---

## Contributing

Found a bug? Want to add a feature? PRs welcome.

---

## License

Business Source License 1.1 — Free for internal use, research, and self-hosted deployments. Commercial/SaaS use requires a license from Engram Memory AI, LLC. Converts to Apache 2.0 after 4 years per version. See [LICENSE](LICENSE) for full terms.
