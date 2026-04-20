FROM python:3.12-slim

WORKDIR /app

# System deps for kuzu C++ bindings
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir mcp fastapi uvicorn

# Copy recall engine
COPY src/recall/ /app/recall/

# Copy MCP server
COPY mcp/server.py /app/server.py

# The MCP server reads from stdin/writes to stdout (stdio transport)
# For Docker, we expose it via a port using SSE transport instead
COPY docker/mcp/entrypoint.py /app/entrypoint.py
COPY docker/mcp/dashboard.py /app/dashboard.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/recall

EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://localhost:8585/health || exit 1

CMD ["python", "entrypoint.py"]
