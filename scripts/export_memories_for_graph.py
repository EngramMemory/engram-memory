#!/usr/bin/env python3
"""Export Engram memories to a folder of Markdown files for graphify.

Pulls real memories from the running engram-memory container rather than
the repo's local scratch database. Data flow:

1. Memories + vectors come from Qdrant's REST ``scroll`` API on the live
   collection (default: ``agent-memory`` at ``http://localhost:6333``),
   requesting ``with_vector=true`` so we get each point's embedding.
2. Graph edges are built from Qdrant vector similarity: for each memory
   we POST its vector to ``/collections/<c>/points/search`` and keep the
   top K neighbors above ``--min-similarity``. This avoids contending
   with the engram-memory container's exclusive Kuzu write lock and
   uses the embeddings that already exist in the collection.

Each memory point becomes one ``<id>.md`` file with YAML frontmatter and
the memory content as the body. Fields that are absent from the payload
are simply omitted — nothing is fabricated.

Usage:
    python scripts/export_memories_for_graph.py --output /tmp/engram-graph-input
    python scripts/export_memories_for_graph.py --output /tmp/out --limit 500

Only stdlib is used (urllib, json) — no new dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_FRONTMATTER_KEYS_SKIP = {"content", "text", "memory", "body"}


def safe_filename(memory_id: str) -> str:
    cleaned = _SAFE_NAME.sub("_", str(memory_id)).strip("_")
    return cleaned or "memory"


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _post_json(url: str, body: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scroll_qdrant(
    qdrant_url: str, collection: str, limit: int, with_vector: bool = True
) -> list[dict[str, Any]]:
    """Page through every point in the collection using Qdrant scroll."""
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll"
    points: list[dict[str, Any]] = []
    offset: Any = None
    page_size = min(256, max(1, limit))
    while len(points) < limit:
        body: dict[str, Any] = {
            "limit": page_size,
            "with_payload": True,
            "with_vector": with_vector,
        }
        if offset is not None:
            body["offset"] = offset
        try:
            resp = _post_json(url, body)
        except urllib.error.HTTPError as e:
            raise SystemExit(
                f"[export] Qdrant scroll failed: HTTP {e.code} — {e.read().decode('utf-8', 'replace')[:300]}"
            )
        except urllib.error.URLError as e:
            raise SystemExit(
                f"[export] Could not reach Qdrant at {qdrant_url}: {e.reason}"
            )
        result = resp.get("result") or {}
        batch = result.get("points") or []
        points.extend(batch)
        next_offset = result.get("next_page_offset")
        if not batch or next_offset is None:
            break
        offset = next_offset
    return points[:limit]


def _payload_content(payload: dict[str, Any]) -> str:
    for key in ("content", "text", "memory", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    # Fall back to a deterministic dump if no obvious content field exists.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _render_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{yaml_escape(value)}"'
    return None


def _render_list(values: Iterable[Any]) -> list[str] | None:
    items: list[str] = []
    for v in values:
        rendered = _render_scalar(v)
        if rendered is None:
            return None
        items.append(rendered)
    return items


def render_markdown(point: dict[str, Any]) -> str:
    payload = point.get("payload") or {}
    mem_id = str(point.get("id", ""))
    content = _payload_content(payload)

    lines: list[str] = ["---", f'id: "{yaml_escape(mem_id)}"']

    for key, value in payload.items():
        if key in _FRONTMATTER_KEYS_SKIP:
            continue
        scalar = _render_scalar(value)
        if scalar is not None:
            lines.append(f"{key}: {scalar}")
            continue
        if isinstance(value, list):
            rendered_list = _render_list(value)
            if rendered_list is None:
                continue
            if not rendered_list:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in rendered_list:
                    lines.append(f"  - {item}")

    lines.append("---")
    lines.append("")
    lines.append(content)
    lines.append("")
    return "\n".join(lines)


def _extract_vector(point: dict[str, Any]) -> tuple[Any, str | None]:
    """Return (vector_payload, vector_name).

    Qdrant returns either a raw list (single unnamed vector) or a dict
    keyed by vector name (named vectors). For the named case we return
    the (name, values) pair so search can target that name.
    """
    vec = point.get("vector")
    if vec is None:
        return None, None
    if isinstance(vec, list):
        return vec, None
    if isinstance(vec, dict) and vec:
        # Prefer "dense" if present, else first key deterministically.
        name = "dense" if "dense" in vec else sorted(vec.keys())[0]
        return vec[name], name
    return None, None


def build_similarity_edges(
    qdrant_url: str,
    collection: str,
    points: list[dict[str, Any]],
    neighbors_per_node: int,
    min_similarity: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """For each point, find k nearest neighbors via Qdrant search.

    Returns (edges, warning). Edges are de-duplicated (undirected) and
    filtered by ``min_similarity``.
    """
    if not points:
        return [], None

    search_url = f"{qdrant_url.rstrip('/')}/collections/{collection}/points/search"
    seen: set[tuple[str, str]] = set()
    edges: list[dict[str, Any]] = []
    known_ids = {str(p.get("id")) for p in points}

    for point in points:
        src_id = str(point.get("id"))
        vector, vec_name = _extract_vector(point)
        if vector is None:
            continue

        body: dict[str, Any] = {
            "limit": neighbors_per_node + 1,  # +1 because self is usually in results
            "with_payload": False,
            "with_vector": False,
        }
        if vec_name is not None:
            body["vector"] = {"name": vec_name, "vector": vector}
        else:
            body["vector"] = vector

        try:
            resp = _post_json(search_url, body)
        except urllib.error.HTTPError as e:
            return [], (
                f"Qdrant search failed: HTTP {e.code} — "
                f"{e.read().decode('utf-8', 'replace')[:300]}"
            )
        except urllib.error.URLError as e:
            return [], f"Could not reach Qdrant at {qdrant_url}: {e.reason}"

        results = resp.get("result") or []
        for hit in results:
            tgt_id = str(hit.get("id"))
            if tgt_id == src_id:
                continue
            if tgt_id not in known_ids:
                continue
            score = float(hit.get("score", 0.0))
            if score < min_similarity:
                continue
            key = tuple(sorted((src_id, tgt_id)))
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "source": key[0],
                "target": key[1],
                "weight": score,
                "type": "similarity",
            })

    return edges, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--output", required=True, help="Directory to write .md files into")
    parser.add_argument(
        "--qdrant-url",
        default="http://localhost:6333",
        help="Base URL of the Qdrant REST API",
    )
    parser.add_argument(
        "--collection",
        default="agent-memory",
        help="Qdrant collection name to scroll",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Maximum number of memories to export",
    )
    parser.add_argument(
        "--container",
        default="engram-memory",
        help="Name of the engram Docker container (kept for CLI compatibility)",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.5,
        help="Minimum cosine similarity to emit a similarity edge",
    )
    parser.add_argument(
        "--neighbors-per-node",
        type=int,
        default=5,
        help="Max nearest neighbors queried per memory",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        default=False,
        help="Include private memories in export (excluded by default)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    points = scroll_qdrant(
        args.qdrant_url, args.collection, args.limit, with_vector=True
    )

    if not points:
        (out_dir / "_edges.json").write_text("[]\n")
        print(
            f"[export] Collection '{args.collection}' at {args.qdrant_url} is empty — "
            f"no memories to export."
        )
        return 0

    used_names: set[str] = set()
    written = 0
    for point in points:
        payload = point.get("payload") or {}
        if not args.include_private and payload.get("private", False):
            continue
        base = safe_filename(point.get("id", "memory"))
        name = f"{base}.md"
        i = 1
        while name in used_names:
            i += 1
            name = f"{base}_{i}.md"
        used_names.add(name)
        (out_dir / name).write_text(render_markdown(point))
        written += 1

    edges, edge_warning = build_similarity_edges(
        args.qdrant_url,
        args.collection,
        points,
        neighbors_per_node=args.neighbors_per_node,
        min_similarity=args.min_similarity,
    )
    if edge_warning:
        print(f"[export] warning: {edge_warning}", file=sys.stderr)
    (out_dir / "_edges.json").write_text(json.dumps(edges, indent=2) + "\n")

    print(
        f"[export] {written} memories, {len(edges)} similarity edges "
        f"(threshold={args.min_similarity}, k={args.neighbors_per_node}) → {out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
