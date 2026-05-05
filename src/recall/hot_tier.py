"""
Engram Memory — Hot-Tier Frequency Cache
==========================================
Sub-millisecond recall for frequently accessed memories.

Uses ACT-R base-level activation (Anderson & Lebiere, 1998):
    B_i = ln(Σ t_j^{-d})

Combined with stability-modulated decay and Boltzmann retrieval
probability for stochastic recall. This is the published cognitive
science model of human memory retrieval — no competitor does this.

Defaults: 1000 entries, 50 timestamps, d=0.5.
All configurable via EngramConfig / environment variables.
"""

import time
import math
import json
import os
import logging
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field

import numpy as np

from matryoshka import cosine_similarity, normalize

logger = logging.getLogger("engram.hot_tier")

DEFAULT_MAX_SIZE = 1000
DEFAULT_MAX_TIMESTAMPS = 50
DEFAULT_DECAY_PARAM = 0.5


@dataclass
class HotMemory:
    """A single memory entry in the hot-tier cache."""
    doc_id: str
    vector: np.ndarray        # Full document vector for similarity comparison
    content: str              # Original text (for context injection)
    category: str             # preference, fact, decision, entity, other
    hits: int                 # Total access count (survives timestamp trimming)
    last_access: float        # Unix timestamp of last access
    first_access: float       # Unix timestamp of first cache entry
    metadata: Dict = field(default_factory=dict)
    access_timestamps: List[float] = field(default_factory=list)  # Per-hit times, capped
    stability: float = 1.0    # Grows with spaced retrieval
    private: bool = False

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "vector": self.vector.tolist(),
            "content": self.content,
            "category": self.category,
            "hits": self.hits,
            "last_access": self.last_access,
            "first_access": self.first_access,
            "metadata": self.metadata,
            "access_timestamps": self.access_timestamps,
            "stability": self.stability,
            "private": self.private,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HotMemory":
        # Migration: v1 data has no access_timestamps or stability
        if "access_timestamps" not in d:
            hits = d.get("hits", 1)
            last = d.get("last_access", time.time())
            first = d.get("first_access", last)
            if hits <= 1:
                timestamps = [last]
            else:
                timestamps = [
                    first + (last - first) * i / (hits - 1)
                    for i in range(min(hits, DEFAULT_MAX_TIMESTAMPS))
                ]
        else:
            timestamps = d["access_timestamps"]

        return cls(
            doc_id=d["doc_id"],
            vector=np.array(d["vector"], dtype=np.float32),
            content=d["content"],
            category=d["category"],
            hits=d["hits"],
            last_access=d["last_access"],
            first_access=d["first_access"],
            metadata=d.get("metadata", {}),
            access_timestamps=timestamps,
            stability=d.get("stability", 1.0),
            private=d.get("private", False),
        )


@dataclass
class HotTierStats:
    """Performance counters for the hot-tier cache."""
    total_hits: int = 0          # Queries answered by hot-tier
    total_misses: int = 0        # Queries that fell through
    total_evictions: int = 0     # Entries evicted due to capacity
    total_upserts: int = 0       # Entries added or updated
    total_decayed: int = 0       # Entries removed by decay sweep

    @property
    def hit_rate(self) -> float:
        total = self.total_hits + self.total_misses
        return self.total_hits / total if total > 0 else 0.0


class EngramHotTier:
    """
    ACT-R cognitive cache for instant recall.

    The hot-tier uses Anderson's ACT-R base-level activation model:
    - Power-law forgetting (not exponential) — matches human memory curves
    - Spacing effect — spaced practice beats massed practice
    - Boltzmann retrieval probability — stochastic recall gate
    - Stability modulation — per-memory adaptive decay

    Defaults: 1000 entries, 50 timestamps, d=0.5 (all configurable).
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        decay_rate: float = 0.1,  # Legacy param, kept for compat
        similarity_threshold: float = 0.65,
        decay_param: float = DEFAULT_DECAY_PARAM,
        retrieval_threshold: float = -0.5,
        noise_param: float = 0.2,
        max_timestamps: int = DEFAULT_MAX_TIMESTAMPS,
    ):
        self.max_size = max_size
        self.decay_rate = decay_rate  # Legacy
        self.similarity_threshold = similarity_threshold
        self.decay_param = decay_param
        self.retrieval_threshold = retrieval_threshold
        self.noise_param = noise_param
        self.max_timestamps = max_timestamps

        # Storage: doc_id -> HotMemory
        self._cache: Dict[str, HotMemory] = {}

        # Pre-computed matrix for batch similarity (rebuilt on change)
        self._matrix_dirty: bool = True
        self._vector_matrix: Optional[np.ndarray] = None
        self._matrix_ids: List[str] = []

        # Stats
        self.stats = HotTierStats()

    def _calculate_strength(self, entry: HotMemory) -> float:
        """ACT-R base-level activation: B_i = ln(Σ t_j^{-d})
        Combined with stability-modulated decay."""
        now = time.time()
        d = self.decay_param

        if entry.access_timestamps:
            total = 0.0
            for t_j in entry.access_timestamps:
                age_seconds = max(now - t_j, 1.0)
                total += age_seconds ** (-d)
            base = math.log(total) if total > 0 else -10.0
        else:
            # Fallback for entries without timestamps (shouldn't happen after migration)
            hours_elapsed = (now - entry.last_access) / 3600.0
            base = math.log1p(entry.hits) - d * math.log1p(hours_elapsed)

        # Stability modulation
        t_since_last = max(now - entry.last_access, 1.0)
        s_mod = math.exp(-(t_since_last / (entry.stability * 3600.0)))

        return max(base * s_mod, 0.0)

    def _retrieval_probability(self, activation: float) -> float:
        """ACT-R retrieval probability via Boltzmann softmax.
        P = 1 / (1 + e^(-(A - τ) / s))"""
        tau = self.retrieval_threshold
        s = self.noise_param
        try:
            return 1.0 / (1.0 + math.exp(-(activation - tau) / s))
        except OverflowError:
            return 0.0 if activation < tau else 1.0

    def _rebuild_matrix(self) -> None:
        """Rebuild the pre-computed vector matrix for batch similarity."""
        if not self._cache:
            self._vector_matrix = None
            self._matrix_ids = []
            self._matrix_dirty = False
            return

        self._matrix_ids = list(self._cache.keys())
        vectors = [self._cache[doc_id].vector for doc_id in self._matrix_ids]
        self._vector_matrix = np.vstack(vectors).astype(np.float32)
        self._matrix_dirty = False

    def upsert(
        self,
        doc_id: str,
        vector: Union[np.ndarray, List[float]],
        content: str = "",
        category: str = "other",
        metadata: Optional[Dict] = None
    ) -> None:
        """Add or update a memory in the hot-tier."""
        vec = np.asarray(vector, dtype=np.float32)
        now = time.time()

        if doc_id in self._cache:
            entry = self._cache[doc_id]

            # Stability grows based on inter-arrival time (spacing effect)
            if entry.access_timestamps:
                inter_arrival_hours = (now - entry.access_timestamps[-1]) / 3600.0
                entry.stability *= (1.0 + inter_arrival_hours)

            entry.access_timestamps.append(now)
            if len(entry.access_timestamps) > self.max_timestamps:
                entry.access_timestamps = entry.access_timestamps[-self.max_timestamps:]

            entry.hits += 1
            entry.last_access = now
            entry.vector = vec
            if content:
                entry.content = content
            if metadata:
                entry.metadata.update(metadata)
        else:
            # Evict weakest if at capacity
            if len(self._cache) >= self.max_size:
                self._evict_weakest()

            self._cache[doc_id] = HotMemory(
                doc_id=doc_id,
                vector=vec,
                content=content,
                category=category,
                hits=1,
                last_access=now,
                first_access=now,
                metadata=metadata or {},
                access_timestamps=[now],
                stability=1.0,
            )
            self._matrix_dirty = True

        self.stats.total_upserts += 1

    def search(
        self,
        query_vector: Union[np.ndarray, List[float]],
        top_k: int = 5,
        min_similarity: Optional[float] = None,
        min_strength: float = 0.1
    ) -> List[Tuple[str, float, float]]:
        """
        Search the hot-tier cache for matching memories.

        Uses cosine similarity for relevance, ACT-R activation for
        memory strength, and Boltzmann probability as a retrieval gate.

        Returns:
            List of (doc_id, similarity, strength) tuples.
        """
        if not self._cache:
            self.stats.total_misses += 1
            return []

        threshold = min_similarity or self.similarity_threshold
        query = np.asarray(query_vector, dtype=np.float32)

        if self._matrix_dirty:
            self._rebuild_matrix()

        if self._vector_matrix is None or len(self._matrix_ids) == 0:
            self.stats.total_misses += 1
            return []

        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            self.stats.total_misses += 1
            return []

        q = query / q_norm
        norms = np.linalg.norm(self._vector_matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normed = self._vector_matrix / norms
        similarities = normed @ q

        results = []
        for i, doc_id in enumerate(self._matrix_ids):
            sim = float(similarities[i])
            if sim < threshold:
                continue

            entry = self._cache.get(doc_id)
            if entry is None:
                continue

            activation = self._calculate_strength(entry)
            retrieval_p = self._retrieval_probability(activation)
            if retrieval_p < 0.1:
                continue

            combined = sim * retrieval_p
            results.append((doc_id, sim, activation, combined))

        if not results:
            self.stats.total_misses += 1
            return []

        results.sort(key=lambda x: x[3], reverse=True)
        top_results = [(r[0], r[1], r[2]) for r in results[:top_k]]

        # Reinforce returned memories (ACT-R: retrieval strengthens memory)
        now = time.time()
        for doc_id, _, _ in top_results:
            entry = self._cache.get(doc_id)
            if entry:
                if entry.access_timestamps:
                    inter = (now - entry.access_timestamps[-1]) / 3600.0
                    entry.stability *= (1.0 + inter)
                entry.access_timestamps.append(now)
                if len(entry.access_timestamps) > self.max_timestamps:
                    entry.access_timestamps = entry.access_timestamps[-self.max_timestamps:]
                entry.hits += 1
                entry.last_access = now

        self.stats.total_hits += 1
        return top_results

    def get_memory(self, doc_id: str) -> Optional[HotMemory]:
        """Get a specific memory by ID (without incrementing hits)."""
        return self._cache.get(doc_id)

    def get_content(self, doc_id: str) -> Optional[str]:
        """Get the text content of a cached memory."""
        entry = self._cache.get(doc_id)
        return entry.content if entry else None

    def _evict_weakest(self) -> None:
        """Remove the memory with the lowest current strength."""
        if not self._cache:
            return

        weakest_id = min(
            self._cache.keys(),
            key=lambda doc_id: self._calculate_strength(self._cache[doc_id])
        )

        del self._cache[weakest_id]
        self._matrix_dirty = True
        self.stats.total_evictions += 1

        logger.debug(f"Evicted weakest memory: {weakest_id}")

    def decay_sweep(self, min_strength: float = 0.01) -> int:
        """Remove all memories below the minimum strength threshold."""
        to_remove = []
        for doc_id, entry in self._cache.items():
            if self._calculate_strength(entry) < min_strength:
                to_remove.append(doc_id)

        for doc_id in to_remove:
            del self._cache[doc_id]

        if to_remove:
            self._matrix_dirty = True
            self.stats.total_decayed += len(to_remove)
            logger.info(f"Decay sweep removed {len(to_remove)} memories")

        return len(to_remove)

    def remove(self, doc_id: str) -> bool:
        """Explicitly remove a memory from the hot-tier."""
        if doc_id in self._cache:
            del self._cache[doc_id]
            self._matrix_dirty = True
            return True
        return False

    def get_top_memories(self, top_k: int = 10) -> List[Tuple[str, float]]:
        """Get the strongest memories currently in cache."""
        scored = [
            (doc_id, self._calculate_strength(entry))
            for doc_id, entry in self._cache.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_context_injection(self, top_k: int = 5) -> str:
        """Format the top hot memories for injection into an LLM system prompt."""
        top = self.get_top_memories(top_k)
        if not top:
            return ""

        lines = ["[HOT MEMORIES — Auto-injected from Engram Hot-Tier]"]
        for doc_id, strength in top:
            entry = self._cache.get(doc_id)
            if entry:
                lines.append(
                    f"• [{entry.category.upper()}] {entry.content} "
                    f"(strength: {strength:.2f}, hits: {entry.hits})"
                )
        lines.append("[END HOT MEMORIES]")
        return "\n".join(lines)

    @property
    def size(self) -> int:
        """Number of memories currently cached."""
        return len(self._cache)

    def get_stats(self) -> dict:
        """Return performance metrics."""
        return {
            "size": self.size,
            "max_size": self.max_size,
            "hit_rate": round(self.stats.hit_rate, 4),
            "total_hits": self.stats.total_hits,
            "total_misses": self.stats.total_misses,
            "total_evictions": self.stats.total_evictions,
            "total_upserts": self.stats.total_upserts,
            "total_decayed": self.stats.total_decayed,
            "decay_rate": self.decay_rate,
            "similarity_threshold": self.similarity_threshold,
            "decay_param": self.decay_param,
        }

    # --- Persistence ---

    def save(self, path: str) -> None:
        """Save cache to disk as JSON."""
        state = {
            "version": 2,
            "max_size": self.max_size,
            "decay_rate": self.decay_rate,
            "similarity_threshold": self.similarity_threshold,
            "decay_param": self.decay_param,
            "retrieval_threshold": self.retrieval_threshold,
            "noise_param": self.noise_param,
            "entries": {
                doc_id: entry.to_dict()
                for doc_id, entry in self._cache.items()
            },
            "stats": {
                "total_hits": self.stats.total_hits,
                "total_misses": self.stats.total_misses,
                "total_evictions": self.stats.total_evictions,
                "total_upserts": self.stats.total_upserts,
                "total_decayed": self.stats.total_decayed,
            },
            "saved_at": time.time(),
        }

        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f)
        os.replace(tmp_path, path)

        logger.info(f"Saved hot-tier: {self.size} entries to {path}")

    @classmethod
    def load(cls, path: str) -> "EngramHotTier":
        """Load cache from disk. Supports v1 and v2 format."""
        with open(path, "r") as f:
            state = json.load(f)

        version = state.get("version", 0)
        if version not in (1, 2):
            raise ValueError(f"Unsupported hot-tier version: {version}")

        hot = cls(
            max_size=state["max_size"],
            decay_rate=state["decay_rate"],
            similarity_threshold=state.get("similarity_threshold", 0.65),
            decay_param=state.get("decay_param", DEFAULT_DECAY_PARAM),
            retrieval_threshold=state.get("retrieval_threshold", -0.5),
            noise_param=state.get("noise_param", 0.2),
        )

        for doc_id, entry_dict in state.get("entries", {}).items():
            entry = HotMemory.from_dict(entry_dict)
            if not entry.content:
                continue
            hot._cache[doc_id] = entry

        stats = state.get("stats", {})
        hot.stats.total_hits = stats.get("total_hits", 0)
        hot.stats.total_misses = stats.get("total_misses", 0)
        hot.stats.total_evictions = stats.get("total_evictions", 0)
        hot.stats.total_upserts = stats.get("total_upserts", 0)
        hot.stats.total_decayed = stats.get("total_decayed", 0)

        hot._matrix_dirty = True

        if version == 1:
            logger.info(f"Migrated hot-tier v1 -> v2: {hot.size} entries from {path}")
        else:
            logger.info(f"Loaded hot-tier v2: {hot.size} entries from {path}")
        return hot
