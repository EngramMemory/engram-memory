"""
Engram Memory — Data Models
=============================
Shared data structures for the recall engine.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default

def _env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return default


@dataclass
class MemoryResult:
    """
    A single memory returned from the recall engine.

    The `tier` field tells you how fast the retrieval was:
    - "hot"    → Sub-millisecond, from frequency cache
    - "hash"   → O(1) lookup, from multi-head hash index
    - "vector" → Standard ANN search from Qdrant
    """
    doc_id: str
    content: str
    score: float              # Final relevance score (0-1)
    tier: str                 # "hot" | "hash" | "vector"
    category: str             # preference, fact, decision, entity, other
    metadata: Dict = field(default_factory=dict)
    created_at: float = 0.0
    access_count: int = 0
    strength: float = 0.0     # ACT-R activation (hot-tier only)
    similarity: float = 0.0   # Raw cosine similarity
    retrieval_probability: float = 0.0  # ACT-R Boltzmann gate (0-1)
    confidence: str = ""        # "high" | "medium" | "low" — set during search
    match_context: str = ""     # Why this result matched (helps calling model rerank)
    preference_boost: float = 0.0  # Boost from reranking feedback (0 = no feedback yet)
    doc_vector: Any = None    # Transient: actual document vector for hot-tier promotion
    private: bool = False

    def to_dict(self) -> dict:
        d = {
            "doc_id": self.doc_id,
            "content": self.content,
            "score": round(self.score, 4),
            "tier": self.tier,
            "category": self.category,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "access_count": self.access_count,
            "strength": round(self.strength, 4),
            "similarity": round(self.similarity, 4),
            "retrieval_probability": round(self.retrieval_probability, 4),
            "confidence": self.confidence,
            "match_context": self.match_context,
            "preference_boost": round(self.preference_boost, 4),
        }
        if self.private:
            d["private"] = True
        return d

    def to_compact_dict(self) -> dict:
        preview = self.content[:100] + ("..." if len(self.content) > 100 else "")
        d = {
            "doc_id": self.doc_id,
            "content_preview": preview,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "category": self.category,
            "created_at": self.created_at,
        }
        if self.private:
            d["private"] = True
        return d


@dataclass
class EngramConfig:
    """
    Configuration for the Engram Recall Engine.

    All parameters can be overridden via environment variables.
    Defaults are tuned for single-machine use.
    """
    # Qdrant connection
    qdrant_url: str = "http://localhost:6333"
    collection: str = "agent-memory"

    # FastEmbed connection
    embedding_url: str = "http://localhost:11435"
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_dim: int = 768

    # Matryoshka slicing
    fast_dim: int = 64        # For Multi-Head Hashing
    medium_dim: int = 256     # For candidate pre-filtering
    full_dim: int = 768       # For final re-ranking

    # Multi-Head Hasher
    hasher_num_heads: int = field(default_factory=lambda: _env_int("ENGRAM_HASH_HEADS", 6))
    hasher_hash_size: int = field(default_factory=lambda: _env_int("ENGRAM_HASH_BITS", 14))
    hasher_seed: int = 42           # Deterministic projections
    hasher_persist_path: str = ".engram/hash_index.pkl"

    # Hot-Tier Cache
    hot_tier_max_size: int = field(default_factory=lambda: _env_int("ENGRAM_HOT_TIER_MAX", 1000))
    hot_tier_decay_rate: float = 0.1
    hot_tier_similarity_threshold: float = 0.55
    hot_tier_persist_path: str = ".engram/hot_tier.json"
    hot_tier_sweep_interval: float = 3600.0  # Decay sweep every hour

    # ACT-R parameters
    actr_decay_param: float = 0.5           # d in B_i = ln(Σ t_j^{-d})
    actr_retrieval_threshold: float = -0.5  # τ in Boltzmann gate
    actr_noise_param: float = 0.2           # s in Boltzmann gate
    actr_max_timestamps: int = field(default_factory=lambda: _env_int("ENGRAM_ACTR_MAX_TIMESTAMPS", 50))

    # Graph layer
    graph_enabled: bool = True
    graph_db_path: str = ".engram/graph.kuzu"
    graph_max_hops: int = field(default_factory=lambda: _env_int("ENGRAM_GRAPH_MAX_HOPS", 1))
    graph_max_entities: int = field(default_factory=lambda: _env_int("ENGRAM_GRAPH_MAX_ENTITIES", 500))
    graph_spreading_weight: float = 0.15

    # Consolidation
    consolidation_enabled: bool = True
    duplicate_threshold: float = field(default_factory=lambda: _env_float("ENGRAM_DEDUP_THRESHOLD", 0.95))
    max_connections_per_call: int = field(default_factory=lambda: _env_int("ENGRAM_MAX_CONNECTIONS", 3))

    # Recall behavior
    auto_recall: bool = True
    auto_capture: bool = True
    max_recall_results: int = 5
    min_recall_score: float = 0.35
    negative_score_ceiling: float = 0.45  # Scores below this get "low" confidence

    # Search behavior
    search_top_k: int = 10
    hash_fallback_to_vector: bool = True  # If hash returns 0, do full scan
    hot_tier_context_inject: bool = True   # Inject hot memories into prompt

    # Engram Cloud (optional extension — local processing is always primary)
    # When api_key is set, the engine sends text to Engram Cloud AFTER local
    # embed for: compressed vectors, dedup check, category detection, and
    # overflow search. Local FastEmbed + Qdrant still do all the heavy work.
    api_key: str = ""                # eng_live_... — leave empty for local-only
    api_url: str = "https://api.engrammemory.ai"

    # Persistence
    data_dir: str = ".engram"
    auto_persist: bool = True
    persist_interval: float = 300.0  # Auto-save every 5 minutes

    # Reranking
    reranker_enabled: bool = False  # Disabled: cosine re-rank outperforms ms-marco-MiniLM for this use case
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    # Debug
    debug: bool = False

    def ensure_data_dir(self):
        """Create data directory if it doesn't exist."""
        import os
        os.makedirs(self.data_dir, exist_ok=True)


@dataclass
class RecallEngineHealth:
    """Health check response from the recall engine."""
    status: str                       # "healthy" | "degraded" | "error"
    hot_tier_size: int = 0
    hash_index_size: int = 0
    qdrant_connected: bool = False
    fastembed_connected: bool = False
    hot_tier_hit_rate: float = 0.0
    avg_hash_candidates: float = 0.0
    graph_node_count: int = 0
    graph_edge_count: int = 0
    last_consolidation: float = 0.0
    cluster_count: int = 0
    uptime_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tiers": {
                "hot": {
                    "size": self.hot_tier_size,
                    "hit_rate": round(self.hot_tier_hit_rate, 4),
                },
                "hash": {
                    "size": self.hash_index_size,
                    "avg_candidates": round(self.avg_hash_candidates, 2),
                },
                "vector": {
                    "qdrant_connected": self.qdrant_connected,
                    "fastembed_connected": self.fastembed_connected,
                },
            },
            "uptime_seconds": round(self.uptime_seconds, 1),
            "errors": self.errors,
        }
