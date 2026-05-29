"""
Engram Memory — Consolidation Engine
======================================
Three maintenance operations for memory hygiene:

  Janitor:    Find and merge near-duplicate memories
  Librarian:  Discover cross-category connections
  Clustering: Group related memories into concept nodes

All thresholds configurable via EngramConfig / environment variables.
"""

import logging
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np

if TYPE_CHECKING:
    from recall_engine import EngramRecallEngine

logger = logging.getLogger("engram.consolidation")

class EngramConsolidator:
    """Memory maintenance: dedup, cross-linking, and concept clustering."""

    def __init__(self, engine: "EngramRecallEngine"):
        self.engine = engine

    # ─── Janitor: Merge Near-Duplicates ──────────────────────────────

    async def consolidate(self, threshold: float = None) -> Dict:
        """Find and merge near-duplicate memories."""
        if threshold is None:
            threshold = self.engine.config.duplicate_threshold

        # Scroll all points from Qdrant
        all_points = await self._scroll_all_points()

        # Exclude private memories from consolidation
        all_points = [p for p in all_points if not p.get("payload", {}).get("private", False)]

        if len(all_points) < 2:
            return {"clusters_found": 0, "memories_merged": 0, "memories_removed": 0}

        # Build vectors matrix
        ids = []
        vectors = []
        contents = []
        categories = []
        for p in all_points:
            pid = str(p.get("id", ""))
            vec = p.get("vector", {})
            if isinstance(vec, dict):
                vec = vec.get("dense", [])
            if not vec:
                continue
            ids.append(pid)
            vectors.append(vec)
            payload = p.get("payload", {})
            contents.append(payload.get("content", payload.get("text", "")))
            categories.append(payload.get("category", "other"))

        if len(vectors) < 2:
            return {"clusters_found": 0, "memories_merged": 0, "memories_removed": 0}

        mat = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normed = mat / norms

        # Pairwise cosine similarity (batched for memory efficiency)
        # Union-find for clustering
        parent = list(range(len(ids)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Compare in batches to avoid O(n²) memory
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch_end = min(i + batch_size, len(ids))
            sims = normed[i:batch_end] @ normed.T  # (batch, n)
            for bi in range(batch_end - i):
                for j in range(i + bi + 1, len(ids)):
                    if sims[bi, j] >= threshold:
                        union(i + bi, j)

        # Group clusters
        clusters = {}
        for idx in range(len(ids)):
            root = find(idx)
            if root not in clusters:
                clusters[root] = []
            clusters[root].append(idx)

        # Filter to actual duplicates (clusters of size > 1)
        dup_clusters = {k: v for k, v in clusters.items() if len(v) > 1}

        if not dup_clusters:
            return {"clusters_found": 0, "memories_merged": 0, "memories_removed": 0}

        # Merge each cluster
        total_merged = 0
        total_removed = 0
        for cluster_indices in dup_clusters.values():
            # Keep the longest content as base
            best_idx = max(cluster_indices, key=lambda i: len(contents[i]))
            base_content = contents[best_idx]
            base_category = categories[best_idx]
            to_delete = [ids[i] for i in cluster_indices if i != best_idx]

            # Store merged memory
            try:
                new_id, _, _ = await self.engine.store(
                    content=base_content,
                    category=base_category,
                )
                total_merged += 1
            except Exception as e:
                logger.warning(f"Merge store failed: {e}")
                continue

            # Delete originals (including the base — it was re-stored with new ID)
            for doc_id in [ids[best_idx]] + to_delete:
                try:
                    await self.engine.forget(doc_id)
                    total_removed += 1
                except Exception:
                    pass

        return {
            "clusters_found": len(dup_clusters),
            "memories_merged": total_merged,
            "memories_removed": total_removed,
        }

    # ─── Librarian: Cross-Category Connections ───────────────────────

    async def connect(self, doc_id: str, max_connections: int = None) -> Dict:
        """Discover cross-category connections for a memory."""
        if max_connections is None:
            max_connections = self.engine.config.max_connections_per_call

        if not self.engine.graph:
            return {"error": "Graph layer not available", "connections_created": 0}

        # Fetch the memory's vector
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.engine.config.qdrant_url}/collections/{self.engine.config.collection}/points",
                    json={"ids": [doc_id], "with_payload": True, "with_vector": True},
                )
                resp.raise_for_status()
                points = resp.json().get("result", [])
                if not points:
                    return {"error": "Memory not found", "connections_created": 0}

                point = points[0]
                raw_vec = point.get("vector", {})
                if isinstance(raw_vec, dict):
                    raw_vec = raw_vec.get("dense", [])
                vector = raw_vec
                source_category = point.get("payload", {}).get("category", "other")
        except Exception as e:
            return {"error": f"Failed to fetch memory: {e}", "connections_created": 0}

        # Search for similar memories
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.engine.config.qdrant_url}/collections/{self.engine.config.collection}/points/search",
                    json={
                        "vector": {"name": "dense", "vector": vector},
                        "limit": max_connections * 3,
                        "with_payload": True,
                        "score_threshold": 0.3,
                    },
                )
                if resp.status_code != 200:
                    # Fallback to flat vector format
                    resp = await client.post(
                        f"{self.engine.config.qdrant_url}/collections/{self.engine.config.collection}/points/search",
                        json={
                            "vector": vector,
                            "limit": max_connections * 3,
                            "with_payload": True,
                            "score_threshold": 0.3,
                        },
                    )
                resp.raise_for_status()
                results = resp.json().get("result", [])
        except Exception as e:
            return {"error": f"Search failed: {e}", "connections_created": 0}

        # Filter to different categories, excluding private memories
        cross_category = [
            r for r in results
            if str(r.get("id", "")) != doc_id
            and r.get("payload", {}).get("category", "other") != source_category
            and not r.get("payload", {}).get("private", False)
        ]

        # Create RELATED_TO edges
        connected = []
        for r in cross_category[:max_connections]:
            target_id = str(r["id"])
            weight = float(r.get("score", 0))
            try:
                self.engine.graph.conn.execute(
                    "MATCH (a:Memory {id: $a}), (b:Memory {id: $b}) "
                    "MERGE (a)-[:RELATED_TO {weight: $w}]->(b)",
                    {"a": doc_id, "b": target_id, "w": weight},
                )
                connected.append(target_id)
            except Exception as e:
                logger.debug(f"Connection edge failed: {e}")

        return {
            "connections_created": len(connected),
            "connected_to": connected,
            "source_category": source_category,
        }

    # ─── Shadow Clustering ───────────────────────────────────────────

    async def shadow_cluster(self, min_cluster_size: int = 5) -> Dict:
        """Group related memories into concept nodes using HDBSCAN."""
        if not self.engine.graph:
            return {"error": "Graph layer not available"}

        try:
            from sklearn.cluster import HDBSCAN
        except ImportError:
            return {"error": "scikit-learn not installed. pip install scikit-learn"}

        # Scroll all points
        all_points = await self._scroll_all_points()
        if len(all_points) < min_cluster_size:
            return {"clusters_found": 0, "noise_points": len(all_points), "concept_nodes_created": 0}

        ids = []
        vectors = []
        contents = []
        for p in all_points:
            vec = p.get("vector", {})
            if isinstance(vec, dict):
                vec = vec.get("dense", [])
            if not vec:
                continue
            ids.append(str(p.get("id", "")))
            vectors.append(vec)
            payload = p.get("payload", {})
            contents.append(payload.get("content", payload.get("text", "")))

        mat = np.array(vectors, dtype=np.float32)

        # Run HDBSCAN
        clusterer = HDBSCAN(min_cluster_size=min_cluster_size, metric="cosine")
        labels = clusterer.fit_predict(mat)

        # Group by cluster
        clusters = {}
        noise = 0
        for i, label in enumerate(labels):
            if label == -1:
                noise += 1
                continue
            if label not in clusters:
                clusters[label] = []
            clusters[label].append(i)

        # Create Concept nodes for each cluster
        concepts_created = 0
        for cluster_indices in clusters.values():
            if len(cluster_indices) < min_cluster_size:
                continue

            # Find medoid (most central memory)
            cluster_vecs = mat[cluster_indices]
            centroid = cluster_vecs.mean(axis=0)
            dists = np.linalg.norm(cluster_vecs - centroid, axis=1)
            medoid_idx = cluster_indices[np.argmin(dists)]
            label_text = contents[medoid_idx][:200]

            concept_id = str(uuid.uuid4())
            try:
                self.engine.graph.conn.execute(
                    "MERGE (c:Concept {id: $id}) SET c.label = $label, c.created_at = $t",
                    {"id": concept_id, "label": label_text, "t": time.time()},
                )
                concepts_created += 1

                # Link cluster members to concept via their Memory nodes
                for idx in cluster_indices:
                    try:
                        self.engine.graph.conn.execute(
                            "MATCH (m:Memory {id: $mid}), (c:Concept {id: $cid}) "
                            "MERGE (m)-[:RELATED_TO {weight: 1.0}]->(c)",
                            {"mid": ids[idx], "cid": concept_id},
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Concept creation failed: {e}")

        return {
            "clusters_found": len(clusters),
            "noise_points": noise,
            "concept_nodes_created": concepts_created,
        }

    # ─── Helpers ─────────────────────────────────────────────────────

    async def _scroll_all_points(self) -> List[Dict]:
        """Scroll all points from Qdrant."""
        import httpx
        all_points = []
        offset = None

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                body = {"limit": 100, "with_payload": True, "with_vector": True}
                if offset is not None:
                    body["offset"] = offset

                try:
                    resp = await client.post(
                        f"{self.engine.config.qdrant_url}/collections/{self.engine.config.collection}/points/scroll",
                        json=body,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"Scroll failed: {e}")
                    break

                result = data.get("result", {})
                points = result.get("points", [])
                if not points:
                    break

                all_points.extend(points)
                offset = result.get("next_page_offset")
                if offset is None:
                    break

        return all_points
