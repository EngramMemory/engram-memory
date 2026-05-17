#!/usr/bin/env python3
"""
Engram OpenClaw Plugin — Three-Tier Recall Engine

Routes memory operations through the recall engine:
  Tier 1: Hot-tier cache (sub-ms, loaded from disk each call)
  Tier 2: Multi-head hash index (O(1) candidates, loaded from disk)
  Tier 3: Qdrant vector search (full ANN fallback)

Context tools still use subprocess scripts.
"""

import sys
import json
import asyncio
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add recall engine to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / "src" / "recall"))

RECALL_AVAILABLE = False
try:
    from recall_engine import EngramRecallEngine
    from models import EngramConfig
    RECALL_AVAILABLE = True
except ImportError:
    pass


def get_config() -> "EngramConfig":
    """Build config from environment or defaults."""
    return EngramConfig(
        qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        embedding_url=os.getenv("EMBED_URL", os.getenv("FASTEMBED_URL", "http://localhost:11435")),
        collection=os.getenv("COLLECTION_NAME", "agent-memory"),
        data_dir=str(SCRIPT_DIR / ".engram"),
        debug=os.getenv("DEBUG", "").lower() in ("true", "1"),
    )


async def _run_with_engine(func):
    """Initialize engine, run function, shutdown."""
    config = get_config()
    engine = EngramRecallEngine(config)
    await engine.warmup()
    try:
        return await func(engine)
    finally:
        await engine.shutdown()


# ── Memory Tools (via Recall Engine) ─────────────────────────────

def memory_store(text: str, category: str = "other", importance: float = 0.5, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
    if not RECALL_AVAILABLE:
        return _fallback_store(text, category, importance, metadata)

    async def _store(engine):
        doc_id, resolved_category, conflicts = await engine.store(content=text, category=category, metadata={"importance": importance, **(metadata or {})})
        return {"success": True, "data": {"memory_id": doc_id, "text": text, "category": resolved_category}}

    return asyncio.run(_run_with_engine(_store))


def memory_search(query: str, limit: int = 10, min_score: float = 0.0, category: str = None) -> Dict[str, Any]:
    if not RECALL_AVAILABLE:
        return _fallback_search(query, limit, min_score, category)

    async def _search(engine):
        results = await engine.search(query=query, top_k=limit, category=category)
        return {
            "success": True,
            "data": {
                "query": query,
                "results": [
                    {
                        "id": r.doc_id,
                        "score": r.score,
                        "text": r.content,
                        "category": r.category,
                        "tier": r.tier,
                        "metadata": r.metadata,
                    }
                    for r in results
                ],
                "count": len(results),
                "tiers_used": list(set(r.tier for r in results)),
            },
        }

    return asyncio.run(_run_with_engine(_search))


def memory_forget(query: str = None, memory_id: str = None) -> Dict[str, Any]:
    if not RECALL_AVAILABLE:
        return _fallback_forget(query, memory_id)

    async def _forget(engine):
        if memory_id:
            success = await engine.forget(memory_id)
            return {"success": success, "deleted": memory_id}

        if query:
            results = await engine.search(query=query, top_k=1)
            if not results:
                return {"success": False, "error": "No matching memory found"}
            target = results[0]
            success = await engine.forget(target.doc_id)
            return {"success": success, "deleted": target.doc_id, "text": target.content[:80]}

        return {"success": False, "error": "Provide query or memory_id"}

    return asyncio.run(_run_with_engine(_forget))


def memory_ingest(path: str, category: str = "fact") -> Dict[str, Any]:
    """Ingest a file (PDF, DOCX, MD, TXT) into memory as chunked entries."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "recall"))
    try:
        from file_ingest import ingest_file
    except ImportError as e:
        return {"success": False, "error": f"file_ingest module unavailable: {e}"}
    try:
        chunks = ingest_file(path)
    except (FileNotFoundError, ImportError, Exception) as e:
        return {"success": False, "error": str(e)}

    if not chunks:
        return {"success": False, "error": "No content extracted from file"}

    stored = []
    errors = []
    for chunk in chunks:
        result = memory_store(
            text=chunk["text"],
            category=category,
            metadata={"source_file": chunk["source"], "chunk_index": chunk["chunk_index"]},
        )
        if result.get("success"):
            stored.append(result["data"]["memory_id"])
        else:
            errors.append(result.get("error", "unknown"))

    return {
        "success": len(stored) > 0,
        "data": {
            "file": path,
            "chunks_total": len(chunks),
            "chunks_stored": len(stored),
            "memory_ids": stored,
            "errors": errors,
        },
    }


# ── Fallbacks (if recall engine unavailable) ─────────────────────

def run_script(script_path: str, args: List[str]) -> Dict[str, Any]:
    full_path = SCRIPT_DIR / script_path
    venv_python = SCRIPT_DIR / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path("python3")

    cmd = [str(venv_python), str(full_path)] + args
    try:
        result = subprocess.run(cmd, cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            try:
                return {"success": True, "data": json.loads(result.stdout)}
            except json.JSONDecodeError:
                return {"success": True, "data": {"output": result.stdout.strip()}}
        return {"success": False, "error": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fallback_store(text, category, importance, metadata):
    args = ["--text", text, "--category", category, "--importance", str(importance)]
    if metadata:
        args.extend(["--metadata", json.dumps(metadata)])
    return run_script("scripts/memory_store_wrapper.py", args)


def _fallback_search(query, limit, min_score, category):
    args = ["--query", query, "--limit", str(limit), "--min-score", str(min_score)]
    if category:
        args.extend(["--category", category])
    return run_script("scripts/memory_search_wrapper.py", args)


def _fallback_forget(query, memory_id):
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(host="localhost", port=6333)
        if memory_id:
            client.delete(collection_name="agent-memory", points_selector=[memory_id])
            return {"success": True, "deleted": memory_id}
        if query:
            result = _fallback_search(query, 1, 0.0, None)
            if result.get("success") and result.get("data", {}).get("results"):
                target = result["data"]["results"][0]
                client.delete(collection_name="agent-memory", points_selector=[str(target["id"])])
                return {"success": True, "deleted": str(target["id"])}
            return {"success": False, "error": "No matching memory found"}
        return {"success": False, "error": "Provide query or memory_id"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Context Tools (subprocess — no recall engine needed) ─────────

def context_search(query: str, project: str = ".", limit: int = 5) -> Dict[str, Any]:
    return run_script("context/cli/context_manager.py", ["find", query, "--limit", str(limit), "--project", project])


def context_ask(question: str, project: str = ".") -> Dict[str, Any]:
    return run_script("context/tools/context_assistant.py", ["ask", question, "--project", project])


# ── Tool Dispatcher ─────────────────────────────────────────────

def handle_tool_call(tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if tool_name == "memory_search":
            return memory_search(
                query=parameters["query"],
                limit=parameters.get("limit", 10),
                min_score=parameters.get("min_score", 0.0),
                category=parameters.get("category"),
            )
        elif tool_name == "memory_store":
            return memory_store(
                text=parameters["text"],
                category=parameters.get("category", "other"),
                importance=parameters.get("importance", 0.5),
                metadata=parameters.get("metadata"),
            )
        elif tool_name == "memory_forget":
            return memory_forget(
                query=parameters.get("query"),
                memory_id=parameters.get("memory_id"),
            )
        elif tool_name == "memory_ingest":
            result = memory_ingest(
                path=parameters.get("path", ""),
                category=parameters.get("category", "fact"),
            )
            return result
        elif tool_name == "context_search":
            return context_search(
                query=parameters["query"],
                project=parameters.get("project", "."),
                limit=parameters.get("limit", 5),
            )
        elif tool_name == "context_ask":
            return context_ask(
                question=parameters["question"],
                project=parameters.get("project", "."),
            )
        else:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
    except KeyError as e:
        return {"success": False, "error": f"Missing required parameter: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Tool execution failed: {str(e)}"}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"success": False, "error": "Usage: plugin.py <tool_name> <parameters_json>"}))
        sys.exit(1)

    tool_name = sys.argv[1]
    try:
        parameters = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "error": f"Invalid JSON: {str(e)}"}))
        sys.exit(1)

    result = handle_tool_call(tool_name, parameters)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
