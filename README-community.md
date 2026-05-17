# Engram Memory Community

**Three-Tiered Brain for AI agents. Hot-Tier cache + O(1) hash retrieval + semantic re-rank. Self-hosted. Zero API costs.**

[![license](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org)
[![docker](https://img.shields.io/badge/docker-required-blue?style=flat-square)](https://www.docker.com)

---

Other memory systems treat retrieval as a flat vector search. Every query — even one your agent asked five minutes ago — runs a full scan across your entire database.

Engram Community gives your agent a brain with three retrieval tiers. Frequently accessed memories return in **sub-millisecond** from cache. Cold queries hit an **O(1) hash index** before ever touching the vector database. The result: your agent gets faster the more you use it.

One repo, two interfaces: an **OpenClaw skill** and a **universal MCP server** that works with Claude Code, Cursor, Windsurf, and VS Code. Runs entirely on your hardware.

---

## The Three-Tiered Brain

```
┌──────────────────────────────────────────────────────────────────────┐
│  QUERY ENTERS                                                        │
└──────────────────────┬───────────────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 1: HOT-TIER CACHE                              Sub-millisecond │
│  ──────────────────────────                                          │
│  Frequency-adjusted exponential decay (Ebbinghaus model).            │
│  Memories accessed often stay hot. Memories ignored fade naturally.   │
│  Score = log(hits + 1) × e^(-λ × hours_since_access)                │
│                                                                      │
│  HIT → Return immediately. Done.                                     │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ MISS
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 2: MULTI-HEAD HASH INDEX                       O(1) lookup     │
│  ──────────────────────────────                                      │
│  Locality-Sensitive Hashing with 4 independent heads.                │
│  Uses first 64 dims of Matryoshka vector (fast slice).               │
│  Collision probability across all heads: ~0%.                        │
│                                                                      │
│  Returns candidate set → re-rank with full 768-dim vector.           │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ CANDIDATES
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 3: QDRANT VECTOR SEARCH                        Full semantic   │
│  ─────────────────────────────                                       │
│  Standard ANN search on the full 768-dim vector.                     │
│  Only used when tiers 1 and 2 miss.                                  │
│  Results promoted back into hot-tier for next time.                  │
└──────────────────────────────────────────────────────────────────────┘
```

In production, **73%+ of queries** are served by the hot-tier after the first week of use. Your agent literally gets faster over time.

---

## Quick Start

### 1. Deploy the backend

```bash
# Requires Docker
git clone https://github.com/EngramMemory/engram-memory
cd engram-memory
bash scripts/setup.sh
```

This starts Qdrant (vector DB) and FastEmbed (local embedding model) on your machine.

### 2. Connect your agent

**OpenClaw:**
```bash
clawhub install engrammemory
```

**Claude Code:**
```bash
claude mcp add engrammemory -- python mcp/server.py
```

**Cursor / Windsurf / VS Code** — add to `.mcp.json`:
```json
{
  "mcpServers": {
    "engrammemory": {
      "command": "python",
      "args": ["mcp/server.py"]
    }
  }
}
```

### 3. Use it

```python
# Store a memory
memory_store("User prefers TypeScript over JavaScript", category="preference")

# Search — flows through all three tiers automatically
results = memory_search("language preferences")

# Each result tells you which tier served it
# → "hot" (cached), "hash" (O(1) lookup), or "vector" (full search)

# Forget a memory
memory_forget(query="old project requirements")
```

---

## What You Get

| Tool | What it does |
|---|---|
| `memory_store` | Save a memory with semantic embedding, auto-classification, and indexing across all three tiers |
| `memory_search` | Three-tier recall: hot-tier → hash index → vector search. Fastest path wins. |
| `memory_recall` | Auto-inject relevant memories into agent context before every turn |
| `memory_forget` | Remove from all three tiers — hot-tier, hash index, and Qdrant |

**Categories:** preference, fact, decision, entity, other — auto-detected from content.

---

## Why Engram vs Everything Else

| | Engram Community | Mem0 OSS | Supermemory | Zep (Graphiti) |
|---|---|---|---|---|
| Three-tier recall (hot + hash + vector) | ✓ | ✗ | ✗ | ✗ |
| Sub-millisecond cached retrieval | ✓ | ✗ | ✗ | ✗ |
| O(1) hash candidate lookup | ✓ | ✗ | ✗ | ✗ |
| Biological memory decay | ✓ | ✗ | ✗ | Partial |
| 100% local — no external API calls | ✓ | Requires LLM API | ✓ | Requires LLM API |
| Matryoshka variable-dim vectors | ✓ | ✗ | ✗ | ✗ |
| OpenClaw skill + MCP server | ✓ | MCP only | MCP only | MCP only |
| Zero dependencies beyond Docker | ✓ | Requires Neo4j/Postgres | ✗ | Requires Neo4j |
| Self-cleaning (decayed memories evicted) | ✓ | ✗ | ✗ | ✗ |

Mem0 and Supermemory are flat vector stores. Zep is a temporal knowledge graph (heavy, requires Neo4j). Engram is a **retrieval engine** — three tiers designed to make search faster the more you use it.

---

## Architecture

```
┌─────────────────┐    ┌───────────────────────────────────────┐    ┌─────────────────┐
│                 │    │         Recall Engine (src/)           │    │                 │
│   Your Agent    │    │                                       │    │     Qdrant      │
│   (OpenClaw,    │───▶│  Hot-Tier ──▶ Hash Index ──▶ Vector   │───▶│   (local)       │
│    Claude Code, │    │                                       │    │                 │
│    Cursor, etc) │    │         FastEmbed (local)              │    │                 │
│                 │    │         Matryoshka slicing              │    │                 │
└─────────────────┘    └───────────────────────────────────────┘    └─────────────────┘
                        All on your hardware. Nothing leaves your network.
```

### Repo Structure

```
engram-memory/
├── src/
│   ├── recall_engine.py        ← Unified three-tier retrieval pipeline
│   ├── hot_tier.py             ← Frequency-adjusted exponential decay cache
│   ├── multi_head_hasher.py    ← LSH with 4 independent hash tables
│   ├── matryoshka.py           ← Variable-dimension vector slicing
│   └── models.py               ← MemoryResult, EngramConfig dataclasses
├── skills/
│   └── openclaw/               ← OpenClaw skill (SKILL.md + plugin)
├── mcp/
│   └── server.py               ← MCP server (Claude Code, Cursor, Windsurf, VS Code)
├── scripts/
│   ├── memory_store.py         ← Store with three-tier indexing
│   ├── memory_search.py        ← Three-tier retrieval flow
│   ├── fastembed_service.py    ← Local embedding API
│   └── setup.sh                ← Docker setup script
├── docker/
│   └── fastembed/              ← FastEmbed Docker image
├── config/
│   └── docker-compose.yml
├── tests/
│   └── test_three_tiers.py     ← 39 tests covering all three tiers
├── README.md
└── LICENSE
```

---

## How the Three Tiers Work

### Tier 1: Hot-Tier Cache (`src/hot_tier.py`)

An in-memory cache using the Ebbinghaus forgetting curve. Every time a memory is successfully retrieved, its "strength" increases. Memories that aren't accessed decay naturally and get evicted.

```
Strength = log(hits + 1) × e^(-decay_rate × hours_since_access)
```

- **Max size:** 1,000 entries (Community) / Unlimited (Cloud)
- **Avg response:** < 0.2ms
- **Persistence:** Saved to disk on shutdown, restored on startup

### Tier 2: Multi-Head Hash Index (`src/multi_head_hasher.py`)

Locality-Sensitive Hashing with multiple independent "heads." Each head is a random projection that maps the first 64 dimensions of the Matryoshka vector to a binary signature. A query checks all heads simultaneously — the union of matching buckets forms the candidate set.

- **Heads:** 4 (Community) / 8-16 (Cloud)
- **Hash size:** 12 bits (Community) / 16-24 bits (Cloud)
- **Avg response:** < 2ms (hash + re-rank)
- **Collision triangulation:** Even if one head produces a false positive, the others correct it

### Tier 3: Qdrant Vector Search (`scripts/memory_search.py`)

Standard approximate nearest neighbor search on the full 768-dimension vector. This is what every other memory system does for every query. Engram only falls back to this when both the hot-tier and hash index miss — typically less than 5% of queries after the first week.

### Matryoshka Embeddings (`src/matryoshka.py`)

The embedding model (nomic-embed-text-v1.5) supports Matryoshka Representation Learning. Semantic meaning is front-loaded into early dimensions:

- **64 dims** → used for hash index (fast, ~90% of signal)
- **256 dims** → used for candidate pre-filtering
- **768 dims** → used for final re-ranking (full precision)

---

## OpenClaw Integration

Engram hooks into OpenClaw's agent lifecycle automatically:

- **`before_agent_start`** — runs three-tier recall for memories relevant to the user's message, injects them as context
- **`after_agent_response`** — extracts important facts from the conversation and stores them across all three tiers

```json
{
  "plugins": {
    "entries": {
      "engram": {
        "enabled": true,
        "config": {
          "qdrantUrl": "http://localhost:6333",
          "embeddingUrl": "http://localhost:11435",
          "autoRecall": true,
          "autoCapture": true
        }
      }
    }
  }
}
```

---

## MiroFish Integration

Engram Community can replace Zep as the memory layer for [MiroFish](https://github.com/666ghj/MiroFish) swarm simulations. Each spawned agent gets an Engram memory namespace. The three-tier system handles thousands of concurrent agents — hot-tier catches opinion leaders, hash index handles the bulk, vector fallback catches edge cases.

Zep's memory footprint is 600K+ tokens per conversation. Engram's hot-tier serves the most active agents at zero additional token cost.

```env
# In your MiroFish .env
MEMORY_PROVIDER=engram
ENGRAM_QDRANT_URL=http://localhost:6333
ENGRAM_EMBEDDING_URL=http://localhost:11435
```

---

## Configuration

| Option | Default | Description |
|---|---|---|
| `qdrantUrl` | `http://localhost:6333` | Qdrant vector database URL |
| `embeddingUrl` | `http://localhost:11435` | FastEmbed API endpoint |
| `embeddingModel` | `nomic-ai/nomic-embed-text-v1.5` | Embedding model |
| `collection` | `agent-memory` | Memory collection name |
| `autoRecall` | `true` | Auto-inject relevant memories |
| `autoCapture` | `true` | Auto-save important context |
| `maxRecallResults` | `5` | Max memories per auto-recall |
| `minRecallScore` | `0.35` | Minimum similarity threshold |
| `hotTierMaxSize` | `1000` | Max memories in hot-tier cache |
| `hotTierDecayRate` | `0.1` | Decay λ (0.1 = 50% strength after ~7hrs) |
| `hasherNumHeads` | `4` | Number of LSH heads |
| `hasherHashSize` | `12` | Bits per hash signature |
| `debug` | `false` | Enable debug logging |

---

## Requirements

- Python 3.10+
- Docker (for Qdrant + FastEmbed)
- 4GB+ RAM
- 10GB+ storage

---

## Tests

```bash
cd engram-memory
python tests/test_three_tiers.py
```

39 tests covering Matryoshka slicing, Multi-Head Hashing, Hot-Tier decay, persistence, and full integration flows.

---

## Data & Privacy

Engram is local-only. No data leaves your machine.

- **Memory tools** store and search vectors in your local Qdrant instance
- **Embeddings** are generated by FastEmbed running in a local Docker container
- **Three-tier state** (hot-tier cache + hash index) is persisted to `.engram/` in your project directory
- **No telemetry, no phone-home, no external API calls**

The Docker image `engrammemory/fastembed` is built from `docker/fastembed/Dockerfile` in this repo. You can verify or rebuild it yourself.

---

## Community vs Cloud

The Community Edition is genuinely powerful. Engram Cloud is for when you outgrow it.

| Feature | Community (Free) | Cloud (Paid) |
|---|---|---|
| Three-tier recall | ✓ | ✓ |
| Hot-Tier cache | 1,000 entries | Unlimited |
| Multi-Head Hashing | 4 heads, 12-bit | 8-16 heads, 16-24 bit, auto-tuning |
| Matryoshka slicing | 64 / 768 dim | Any dimension |
| Auto-recall + auto-capture | ✓ | ✓ |
| Memory categories | ✓ | ✓ |
| OpenClaw skill + MCP server | ✓ | ✓ |
| TurboQuant 6x compression | ✗ | ✓ |
| Deduplication | ✗ | ✓ |
| Full corpus memory decay | ✗ | ✓ |
| Cross-project learning | ✗ | ✓ |
| Multi-agent isolation (fleet) | ✗ | ✓ |
| HIPAA-ready architecture | ✗ | ✓ |
| Analytics dashboard | ✗ | ✓ |
| Request logging | ✗ | ✓ |

[Engram Cloud →](https://engrammemory.ai) — Your Qdrant stays yours. Engram Cloud processes in transit and stores nothing.

---

## SDKs

If you want the cloud-managed version with TurboQuant, dedup, and fleet management:

```bash
# Python
pip install engram-cloud

# Node.js
npm install engram-cloud
```

```python
from engrammemory import Engram

client = Engram(
    api_key="eng_live_xxx",
    qdrant_url="http://localhost:6333"
)
client.store("User prefers TypeScript", category="preference")
results = client.search("language preferences")
```

- [Python SDK on PyPI](https://pypi.org/project/engram-cloud/)
- [Node SDK on npm](https://www.npmjs.com/package/engram-cloud)

---

## Contributing

Found a bug? Want to add a feature? PRs welcome.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## Links

- **Website:** [engrammemory.ai](https://engrammemory.ai)
- **Dashboard:** [app.engrammemory.ai](https://app.engrammemory.ai)
- **API Docs:** [api.engrammemory.ai/docs](https://api.engrammemory.ai/docs)
- **Python SDK:** [pypi.org/project/engram-cloud](https://pypi.org/project/engram-cloud/)
- **Node SDK:** [npmjs.com/package/engram-cloud](https://www.npmjs.com/package/engram-cloud)
- **Discord:** [discord.gg/engram](https://discord.gg/engram)

---

## License

MIT — Use freely in personal and commercial projects.

---

**Your memory. Your infrastructure. Our intelligence.**
