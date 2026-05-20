# Engram Memory — All-in-One Container
#
# Bundles the entire Engram stack in a single image:
#   - Qdrant (vector database)         on port 6333
#   - FastEmbed (ONNX embeddings)      on port 11435
#   - MCP HTTP server (recall engine)  on port 8585
#
# All three are supervised by s6-overlay so they restart independently
# if any one crashes. Persistent state lives in /data:
#   /data/qdrant   — vector database storage
#   /data/engram   — recall engine state (hot tier, hash index, Kuzu graph)
#
# Usage:
#   docker run -d -p 6333:6333 -p 11435:11435 -p 8585:8585 \
#       -v engram_data:/data engrammemory/engram-memory:latest
#
# This is the same product as the 3-container setup, just packaged as
# one image. Every feature is preserved: three-tier recall, ACT-R hot
# cache, multi-head LSH, Matryoshka slicing, BM25 hybrid search,
# Kuzu graph layer, consolidation, all 6 MCP tools.

# ── Stage 1: Python deps for FastEmbed + MCP server ────────────────
FROM python:3.12-slim AS python-deps

RUN pip install --no-cache-dir \
        fastembed>=0.8.0 \
        fastapi>=0.115.0 \
        uvicorn>=0.34.0 \
        numpy>=1.26.0 \
        httpx>=0.28.0 \
        qdrant-client>=1.11.0 \
        kuzu>=0.4.0 \
        scikit-learn>=1.3.0 \
        pyyaml>=6.0 \
        click>=8.0.0 \
        requests>=2.28.0 \
        networkx>=3.0 \
        mcp \
        pypdf>=3.0.0 \
        python-docx>=0.8.11

RUN find /usr/local/lib/python3.12/site-packages -type d \
        \( -name "__pycache__" -o -name "tests" -o -name "test" \
           -o -name "benchmarks" -o -name "docs" \) \
        -exec rm -rf {} + 2>/dev/null; \
    find /usr/local/lib/python3.12/site-packages -type f \
        \( -name "*.pyc" -o -name "*.pyo" -o -name "*.pyi" \) \
        -delete 2>/dev/null; \
    true

# ── Stage 2: Pull Qdrant binary + config ───────────────────────────
FROM qdrant/qdrant:v1.11.3 AS qdrant-src

# ── Stage 3: Runtime ───────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/recall \
    QDRANT_URL=http://localhost:6333 \
    FASTEMBED_URL=http://localhost:11435 \
    COLLECTION_NAME=agent-memory \
    DATA_DIR=/data/engram \
    MODEL_NAME=nomic-ai/nomic-embed-text-v1.5

# Runtime dependencies — libunwind8 is needed for the Qdrant binary
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        curl \
        xz-utils \
        ca-certificates \
        libunwind8 \
    && rm -rf /var/lib/apt/lists/*

# Install s6-overlay (process supervisor)
ARG S6_OVERLAY_VERSION=3.2.0.2
RUN ARCH="$(uname -m)" && \
    case "$ARCH" in \
        x86_64) S6_ARCH=x86_64 ;; \
        aarch64|arm64) S6_ARCH=aarch64 ;; \
        *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -sSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
        | tar -Jxpf - -C / && \
    curl -sSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
        | tar -Jxpf - -C /

# Copy Python packages from build stage
COPY --from=python-deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=python-deps /usr/local/bin /usr/local/bin

# Copy Qdrant binary + default config
COPY --from=qdrant-src /qdrant/qdrant /usr/local/bin/qdrant
COPY --from=qdrant-src /qdrant/config /etc/qdrant

# Copy Engram source
WORKDIR /app
COPY src/recall/ /app/recall/
COPY mcp/server.py /app/mcp_server.py
COPY docker/fastembed/fastembed_service.py /app/fastembed_service.py
COPY docker/mcp/entrypoint.py /app/mcp_entrypoint.py
COPY docker/mcp/dashboard.py /app/dashboard.py
COPY vendor/graphify/graphify/ /app/graphify/
COPY docker/all-in-one/init.sh /app/init.sh
RUN chmod +x /app/init.sh

# Slash commands — shipped inside the image so users can extract them
# via: docker cp engram-memory:/app/commands/ ~/.claude/commands/
COPY .claude/commands/ /app/commands/

# s6 service definitions: Qdrant + FastEmbed + MCP HTTP server, all longrun
COPY docker/all-in-one/services.d /etc/s6-overlay/s6-rc.d
RUN mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/qdrant && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/fastembed && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/mcp-server && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/init

# Persistent data volume
RUN mkdir -p /data/qdrant /data/engram
VOLUME ["/data"]

# Expose all three service ports — same as the 3-container setup, so any
# existing client (OpenClaw plugin, REST consumer, MCP client) works
# unchanged when pointed at this single container.
EXPOSE 6333 11435 8585

# Healthcheck via the MCP server's /health endpoint (which itself reports
# Qdrant + FastEmbed connectivity, so a single check covers all three)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=120s \
    CMD curl -sf http://localhost:8585/health || exit 1

ENTRYPOINT ["/init"]
