# Quick Start Guide

Get Engram running in under 5 minutes.

## Prerequisites

- Docker & Docker Compose
- Python 3.10+
- 4GB+ RAM

## Step 1: Install

```bash
# OpenClaw
clawhub install engrammemory

# Or clone directly
git clone https://github.com/EngramMemory/engram-memory.git
cd engram-memory
```

## Step 2: Setup

```bash
bash scripts/setup.sh
```

This deploys Qdrant (vector DB) and FastEmbed (embedding model), installs context system dependencies, and generates your OpenClaw config.

## Step 3: Configure

Add the generated config to `~/.openclaw/openclaw.json`:

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

Restart the gateway: `openclaw gateway restart`

## Step 4: Test

```bash
# Store a memory
memory_store "I prefer TypeScript over JavaScript" --category preference

# Search memories
memory_search "programming language preferences"

# Ask about your project (after initializing context)
engram-context init . --template web-app
context_ask "How does authentication work?"
```

## Step 5: Add SOUL Rules (Optional)

See `docs/SOUL-RULES.md` for recommended rules that teach your agent to use memory proactively. Adapt what fits your style.

## Verify

```bash
# Check Qdrant
curl http://localhost:6333/collections

# Check FastEmbed
curl http://localhost:11435/health
```

## Troubleshooting

**Port conflicts:**
```bash
sudo lsof -i :6333
sudo lsof -i :11435
```

**Docker issues:**
```bash
docker-compose down && docker-compose up -d
```

**Memory tools not available:**
1. Verify plugin config in `~/.openclaw/openclaw.json`
2. Check `openclaw status` for plugin errors
3. Ensure services are running: `docker-compose ps`

## Next Steps

- [Architecture Overview](ARCHITECTURE.md)
- [Context System](../context/README.md)
- [SOUL Rules](SOUL-RULES.md)
- When ready to scale: [Engram Cloud](ENGRAM_CLOUD.md)
