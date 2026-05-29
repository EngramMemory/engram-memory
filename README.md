<div align="center">

<img src="assets/logo.svg" alt="Engram Memory" width="360" />

**Three-Tiered Brain for AI agents. Self-hosted. Zero API costs.**

[Docs](https://engrammemory.ai/docs) · [Quickstart](#quick-start) · [Dashboard](https://app.engrammemory.ai) · [Cloud SDKs](#engram-cloud)

![npm](https://img.shields.io/npm/v/engrammemory-ai?label=npm&color=10b981&style=flat-square)
![pypi](https://img.shields.io/pypi/v/engrammemory-ai?label=pypi&color=10b981&style=flat-square)
![docs](https://img.shields.io/badge/docs-engrammemory.ai-10b981?style=flat-square)
![license](https://img.shields.io/badge/license-BSL--1.1-green?style=flat-square)

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

Sixteen MCP tools — eleven core memory tools and five hive-management tools for multi-device sharing — plus a visual graph command:

| Tool | What it does |
|---|---|
| `memory_store` | Save a memory with semantic embedding, auto-classification, and conflict detection |
| `memory_search` | Three-tier recall search with confidence scoring and match context |
| `memory_recall` | Auto-inject relevant memories into agent context |
| `memory_forget` | Remove memories by ID or search match |
| `memory_consolidate` | Find and merge near-duplicate memories |
| `memory_connect` | Discover cross-category connections via the entity graph |
| `memory_feedback` | Report which search results were useful — improves future recall |
| `memory_get` | Fetch one or more memories by UUID |
| `memory_timeline` | Browse memories chronologically with date-range filtering |
| `memory_answer` | Answer a question from stored memories, with cloud synthesis when API key is set |
| `memory_ingest` | Ingest a file (PDF, DOCX, Markdown, plain text) as chunked memories |
| `hive_list` | List all hives this API key has access to (multi-device shared memory pools) |
| `hive_create` | Create a new shared hive (`name`, `slug`) |
| `hive_grant` | Grant another API-key prefix `read` or `readwrite` access to a hive |
| `hive_revoke` | Revoke a granted key prefix from a hive |
| `hive_grants_list` | List active grants on a hive |
| `/graph` | Generate an interactive visual graph of your memories (Claude Code slash command) |

**Categories:** 13 types — `preference`, `fact`, `decision`, `entity`, `goal`, `plan`, `error`, `insight`, `skill`, `event`, `question`, `relationship`, `other` — auto-detected by local keyword classifier across all surfaces (Python engine, TypeScript plugin, MCP tools).

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
git clone https://github.com/EngramMemory/engram-memory.git
cd engram-memory && bash scripts/install-plugin.sh
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

### 4. Configure agent behavior (recommended)

Engram is the primary memory layer for any MCP-aware agent — Claude, ChatGPT, Cursor, Perplexity, OpenClaw — and works best when the agent is configured to **save every turn**, **recall before acting**, and trust the **local + cloud dual-write** as the failsafe / hive backbone.

See [`docs/SOUL-RULES.md`](docs/SOUL-RULES.md) for the recommended ruleset. Drop the rules block into your `SOUL.md` / `CLAUDE.md` / equivalent and the agent will use Engram the way it was designed:

- **Turn-by-turn capture** — `memory_store` for both user input and assistant output on every turn.
- **Local + cloud dual-write** — every store persists locally *and* mirrors to `api.engrammemory.ai` when `ENGRAM_API_KEY` is set. The cloud copy is the failsafe and the hive sync layer; without the key, capture is purely local.
- **No sidecars, no schedulers, no transcript-tailers.** The existing `memory_store` MCP tool is the only channel — there is nothing else to install or run.

### New in this release

**Conflict detection** — `memory_store` now returns a `conflicts` field when a new memory contradicts an existing one:
```python
result = memory_store("User prefers Python over TypeScript")
# result["conflicts"] → [{"id": "...", "text": "User prefers TypeScript", "score": 0.91}]
```

**Temporal queries** — browse memories by date or date range:
```python
# Last 48 hours
memory_timeline(hours=48, category="decision")

# Specific date range
memory_timeline(from_date="2026-05-01", to_date="2026-05-10")

# Everything before a date (point-in-time view)
memory_timeline(from_date="2026-01-01", to_date="2026-05-01", limit=50)
```

**File ingestion** — ingest a PDF, DOCX, Markdown, or text file as chunked memories:
```python
memory_ingest("/path/to/architecture.pdf", category="fact")
# Splits into ~500-char chunks, stores each with source_file metadata
# Supports: .pdf, .docx, .md, .mdx, .txt, .csv, .json, .yaml
```
Install optional deps for full format support:
```bash
pip install pypdf python-docx
```

**Answer from memory** — ask a question, get an answer synthesized from your stored memories:
```python
# Without ENGRAM_API_KEY — returns relevant memory context
memory_answer("What database are we using?")

# With ENGRAM_API_KEY — routes to Engram Cloud for full answer synthesis
memory_answer("What did we decide about the auth system last month?")
```

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

## What's New

### 13-Type Memory Taxonomy

The original 5 categories (preference, fact, decision, entity, other) have been expanded to 13. The classifier runs locally with no API dependency and detects categories from keywords in the text:

| Category | Detected from |
|---|---|
| `decision` | decided, chose, adopted, deprecated, committed to... |
| `preference` | prefer, love, hate, always, never, favor... |
| `goal` | goal, objective, milestone, deadline, roadmap, OKR... |
| `plan` | plan, strategy, next step, sprint, phase, rollout... |
| `error` | error, bug, broke, failed, crash, regression, incident... |
| `insight` | realized, discovered, found that, lesson, takeaway... |
| `skill` | expert in, proficient, familiar with, mastered... |
| `event` | meeting, demo, launch, shipped, deployed, occurred... |
| `question` | wondering, unclear, TBD, open question, investigate... |
| `relationship` | reports to, manages, depends on, blocked by, supports... |
| `fact` | running on, deployed, version, configured, port... |
| `entity` | company, team, person, project, service, engineer... |

### Conflict Detection

Every `memory_store` call automatically checks for near-matches (cosine similarity >= 0.82) that may contradict the new memory. Contradictions are detected by comparing preference patterns and negation signals between the new text and existing memories.

The store response includes a `conflicts` array — empty on no conflicts, populated with `{id, text, score, category}` when a contradiction is detected. The memory is still stored; the agent decides how to handle it.

### Temporal Queries

`memory_timeline` now supports absolute date ranges in addition to the `hours` lookback:

- `from_date` / `to_date` — ISO 8601 strings (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`)
- When both are set, `hours` is ignored
- Useful for point-in-time audits: "what did the agent know before this incident?"

### File Ingestion

`memory_ingest` splits documents into ~500-char overlapping chunks and stores each as a memory with `source_file` and `chunk_index` in the metadata. Supported formats:

| Format | Requirement |
|---|---|
| PDF | `pip install pypdf` |
| DOCX | `pip install python-docx` |
| Markdown | built-in (frontmatter and HTML stripped) |
| Plain text / CSV / JSON / YAML | built-in |

Chunks are searchable immediately after ingestion via `memory_search`.

### Answer from Memory

`memory_answer` retrieves the most relevant memories for a question and either:
- **With `ENGRAM_API_KEY`**: synthesizes a full natural-language answer via Engram Cloud (`/v1/intelligence/answer`)
- **Without key**: returns the retrieved memories formatted as context for the local LLM to reason over

### Hive Memory (multi-device sharing)

A **hive** is a named shared memory pool that multiple API keys can read from or write to as a single brain. When a hive is active on the local container, every `memory_store` also POSTs the memory to `/v1/hives/{hive_id}/memories`, and every `memory_search` queries the hive in parallel with the local tiers. Result: one memory store callable from **your laptop, your phone, another agent, an IoT device — anywhere an MCP client with a granted key can connect.**

Five MCP tools manage hives end-to-end (all require `ENGRAM_API_KEY`):

```python
hive_create(name="Edwin's Brain", slug="edwin-brain")
hive_grant(hive_id="...", key_prefix="eng_live_ab12", permission="readwrite")
hive_list()              # see hives this key can reach
hive_grants_list(hive_id="...")
hive_revoke(hive_id="...", key_prefix="eng_live_ab12")
```

The active hive is selected via the `/hive` command (file-based, persisted under `DATA_DIR/active_hive`); once activated, all `memory_store` / `memory_search` calls in this container also flow through the hive. See [`docs/HIVE.md`](docs/HIVE.md) for setup, permission model, and the failsafe / device-loss recovery flow.

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
