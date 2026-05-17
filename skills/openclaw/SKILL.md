---
name: engram
description: Persistent semantic memory for AI agents. Store, search, recall, and forget memories across sessions using Qdrant + FastEmbed.
---

# Engram for OpenClaw Agents

Persistent semantic memory that makes your agent remember across sessions.

## What This Provides

**Instead of starting fresh every session, your agent will:**

- Remember your preferences, facts, and past decisions
- Automatically recall relevant context for new conversations
- Search through stored memories semantically
- Categorize and organize knowledge by type

## Quick Start

```bash
# Install the skill
clawhub install engrammemory

# Setup (requires Docker — deploys Qdrant + FastEmbed)
bash scripts/setup.sh

# Store a memory
memory_store "I prefer direct communication style" --category preference

# Search memories
memory_search "communication preferences"
```

## Core Functions

### memory_store(text, category, importance)
Save information to long-term memory with semantic embedding.

```python
# Save preferences
memory_store("User prefers TypeScript over JavaScript for new projects", 
            category="preference", importance=0.8)

# Save facts  
memory_store("Database migration completed on 2024-03-15, moved from SQLite to PostgreSQL",
            category="fact", importance=0.7)

# Save decisions
memory_store("Decided to use React Query for state management in the frontend",
            category="decision", importance=0.9)
```

### memory_recall(query, limit, category)
Search stored memories using semantic similarity.

```python
# Find relevant memories
memory_recall("database migration")
memory_recall("frontend preferences", category="preference")
memory_recall("recent decisions", limit=10)
```

### memory_profile(action, key, value, category)
Manage user profile data (static preferences + dynamic context).

```python
# View profile
memory_profile("view")

# Add static preference  
memory_profile("add", "communication_style", "Direct, no fluff", "static")

# Add dynamic context
memory_profile("add", "current_project", "Building memory system", "dynamic")
```

### memory_forget(query, memory_id)
Remove memories by search or specific ID.

```python
memory_forget("old project requirements")
memory_forget(memory_id="uuid-string")  
```

## Memory Categories

- **preference** — User preferences, communication style, technical choices
- **fact** — Objective information, system states, completed work  
- **decision** — Important decisions made, rationale, outcomes
- **entity** — People, projects, organizations, relationships
- **other** — Miscellaneous information that doesn't fit above

## Context System

Engram includes a context management system that gives your agent structured knowledge about any codebase. Initialize a project, and your agent can search and query its architecture, patterns, and APIs.

### Context Commands

#### engram-context — Core Management
```bash
# Initialize context for a project
engram-context init /path/to/project --template web-app
engram-context init /path/to/project --template python-api
engram-context init /path/to/project --template generic

# Build search index
engram-context index

# Search context files
engram-context find "authentication patterns"

# Check status
engram-context status
```

#### engram-ask — Natural Language Queries
```bash
engram-ask "How does authentication work?"
engram-ask "Where are the API endpoints defined?"
engram-ask interactive
```

#### engram-semantic — Embedding-Based Search
```bash
engram-semantic find "user login process"
engram-semantic index
engram-semantic status
```

### Project Templates

| Template | Best for |
|----------|----------|
| `web-app` | Full-stack web apps (React/Vue + Node/Python + DB) |
| `python-api` | Python API servers (FastAPI, Django) |
| `generic` | Any project type |

### Context Structure

Each project gets a `.context/` directory:

```
.context/
├── metadata.yaml       # Project configuration
├── architecture.md     # System architecture
├── patterns.md         # Code patterns and standards
├── apis.md             # API documentation
├── development.md      # Development workflows
├── troubleshooting.md  # Common issues and solutions
└── index.db            # Search index (auto-generated)
```

All context queries are scoped to the current project.

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   OpenClaw      │    │  FastEmbed      │    │     Qdrant      │
│   Agent         │───▶│  nomic-embed    │───▶│  Vector Store   │
│                 │    │  text-v1.5      │    │  Port 6333      │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

**Components:**
- **Qdrant** — Vector database for semantic storage/search
- **FastEmbed** — Local embedding model (nomic-embed-text-v1.5)
- **Plugin** — OpenClaw integration with auto-recall and capture

## Installation

### Prerequisites
- OpenClaw 2026.3.13+
- Docker and docker-compose
- 4GB+ RAM for embedding model
- 10GB+ storage for vector database

### Automated Setup

```bash
# 1. Install the skill
clawhub install engrammemory

# 2. Run setup script  
cd ~/.openclaw/workspace/skills/engrammemory
bash scripts/setup.sh

# 3. Follow the configuration prompts
# (The script will generate the exact config to add to openclaw.json)

# 4. Restart OpenClaw gateway
openclaw gateway restart
```

### Manual Setup

If you prefer manual installation or need custom configuration:

1. **Deploy Qdrant + FastEmbed:**
   ```bash
   # Copy docker-compose template
   cp config/docker-compose.yml ~/engram-stack/
   cd ~/engram-stack
   docker-compose up -d
   ```

2. **Configure OpenClaw plugin:**
   Add to `~/.openclaw/openclaw.json`:
   ```json
   {
     "plugins": {
       "allow": ["engram"],
       "slots": {
         "memory": "engram"
       },
       "entries": {
         "engram": {
           "enabled": true,
           "config": {
             "qdrantUrl": "http://localhost:6333",
             "embeddingModel": "nomic-ai/nomic-embed-text-v1.5",
             "collection": "agent-memory", 
             "autoRecall": true,
             "autoCapture": true,
             "maxRecallResults": 5,
             "minRecallScore": 0.35,
             "embeddingUrl": "http://localhost:11435"
           }
         }
       }
     }
   }
   ```

3. **Restart gateway:**
   ```bash
   openclaw gateway restart
   ```

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `qdrantUrl` | `http://localhost:6333` | Qdrant vector database URL |
| `embeddingUrl` | `http://localhost:11435` | FastEmbed API endpoint |
| `embeddingModel` | `nomic-ai/nomic-embed-text-v1.5` | Embedding model |
| `collection` | `agent-memory` | Memory collection name |
| `autoRecall` | `true` | Auto-inject relevant memories |
| `autoCapture` | `true` | Auto-save important context |
| `maxRecallResults` | `5` | Max memories per auto-recall |
| `minRecallScore` | `0.35` | Minimum similarity threshold |
| `profileFrequency` | `20` | Update profile every N messages |
| `debug` | `false` | Enable debug logging |

### Example Configuration

```json
{
  "qdrantUrl": "http://localhost:6333",
  "embeddingUrl": "http://localhost:11435",
  "collection": "agent-memory",
  "autoRecall": true,
  "autoCapture": true
}
```

## Multi-Agent Setup

For multiple agents sharing memory:

```json
{
  "agents": {
    "list": [
      {
        "id": "main",
        "plugins": {
          "memory": {
            "collection": "main-agent-memory"
          }
        }
      },
      {
        "id": "coding-assistant", 
        "plugins": {
          "memory": {
            "collection": "coding-agent-memory"
          }
        }
      }
    ]
  }
}
```

## Usage Examples

### Research Assistant
```python
# Save research findings
memory_store("Found that React 18 concurrent rendering improves performance by 15-30% for large lists", 
            category="fact", importance=0.8)

# Later, when discussing performance:
memories = memory_recall("React performance improvements")
# Auto-recalls the research finding
```

### Project Manager  
```python
# Save project decisions
memory_store("Team decided to use PostgreSQL over MongoDB for better ACID compliance in financial app",
            category="decision", importance=0.9)

# Track preferences
memory_profile("add", "deployment_preference", "Docker with Kubernetes", "static")

# Later project planning auto-recalls relevant decisions and preferences
```

### Customer Support
```python
# Remember customer preferences
memory_store("Customer prefers email over phone calls for non-urgent issues",
            category="preference", importance=0.7)

# Track issue patterns
memory_store("Billing module API timeout issue affects 15% of enterprise customers",
            category="fact", importance=0.8)
```

## Backup and Migration

### Export Memory
```bash
# Export all memories as JSON
curl "http://localhost:6333/collections/agent-memory/points/scroll" \
  -H "Content-Type: application/json" \
  -d '{"limit": 10000}' > memory_backup.json
```

### Import Memory  
```bash
# Import memories from backup
# Re-import using memory_store
python scripts/memory_store.py --json '{"text": "...", "category": "fact"}'
```

## Performance Tuning

### Vector Quantization (New Feature)
Engram now includes automatic scalar quantization for 4x memory reduction with no recall loss:

- **Memory Usage**: Reduces vector storage by ~75% (32-bit → 8-bit per dimension)
- **Search Speed**: Unchanged (quantized vectors stay in RAM for fast search)
- **Quality**: No degradation (99th percentile quantile preserves accuracy)
- **Automatic**: Enabled by default in new installations

```bash
# Quantization is automatically applied when you run:
bash scripts/setup.sh
```

**Technical Details:**
- Uses int8 scalar quantization with 99th percentile
- Compresses 768-dimension vectors from ~3KB to ~768 bytes each
- Enables storing 4x more memories in the same RAM
- Fully compatible with existing memory collections

### For Large Memory Sets (10K+ memories)
```json
{
  "config": {
    "maxRecallResults": 3,
    "minRecallScore": 0.45,
    "autoCapture": false
  }
}
```

### For High-Frequency Agents
```json  
{
  "config": {
    "profileFrequency": 50,
    "autoRecall": false
  }
}
```

## Troubleshooting

### Common Issues

**Memory not persisting:**
- Check Qdrant is running: `curl http://localhost:6333/collections`
- Verify plugin config in `openclaw status`

**Poor recall quality:**
- Lower `minRecallScore` (try 0.25)
- Check embedding model is loaded: `curl http://localhost:11435/models`

**High memory usage:**
- Increase Docker memory limits
- Reduce `maxRecallResults`
- Enable auto-cleanup in config

### Debug Mode
```json
{
  "config": {
    "debug": true
  }
}
```

Enables detailed logging for memory operations.

## Advanced Usage

### Custom Categories
```python
memory_store("Customer uses advanced React patterns", category="customer_tech_profile")
memory_recall("customer tech preferences", category="customer_tech_profile")
```

### Importance-Based Filtering
```python
# Only recall highly important memories  
memory_recall("project decisions", min_importance=0.8)
```

### Time-Based Queries
```python
# Recent memories (last 30 days)
memory_recall("recent changes", days_back=30)
```

## Security Considerations

- **Local-first:** All data stays on your infrastructure
- **No external APIs:** Embeddings generated locally
- **Encryption:** Use encrypted storage for sensitive data
- **Access control:** Configure Qdrant authentication if needed

## Contributing

Found a bug or want to add features? 

- **GitHub:** [engram-memory](https://github.com/EngramMemory/engram-memory)
- **Issues:** Report bugs and feature requests
- **Docs:** Help improve documentation
- **Examples:** Share usage patterns

## License

MIT License - Use freely in personal and commercial projects.

---

**Transform your agent from stateless to stateful. Install Engram today.**

## OpenClaw Integration

🟢 **Fully Integrated** - Engram is now available as native OpenClaw tools.

### Available Tools

- **`memory_search`** - Search memories using semantic similarity
- **`memory_store`** - Store text with embeddings in long-term memory

### How to Use

Once Engram is set up (FastEmbed service + Qdrant running), these tools are automatically available in OpenClaw sessions:

```javascript
// Search stored memories
memory_search("FastEmbed integration status", 10, 0.3)

// Store new memories
memory_store("User prefers detailed explanations", "preference", 0.8)
```

### Implementation

- **Plugin Type:** Native Python plugin
- **Backend:** FastEmbed (localhost:8000) + Qdrant (localhost:6333)
- **No MCP Server Required:** Direct integration through OpenClaw's plugin system

See [OPENCLAW_INTEGRATION.md](OPENCLAW_INTEGRATION.md) for complete technical details.
