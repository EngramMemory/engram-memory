"""
Engram Memory — Recall Engine
===============================
The unified three-tiered retrieval pipeline.

Query flow:
    1. HOT-TIER CHECK  → Sub-ms frequency cache lookup
    2. HASH LOOKUP     → O(1) LSH candidate retrieval
    3. VECTOR RE-RANK  → Full cosine similarity on candidates (or fallback)

This class replaces the flat `memory_search.py` with a tiered system
that gets faster the more you use it.

Usage:
    engine = EngramRecallEngine(config)
    await engine.warmup()

    # Store
    doc_id, _ = await engine.store("User prefers TypeScript", category="preference")

    # Search (automatically uses best tier)
    results = await engine.search("language preferences", top_k=5)

    # Shutdown (persist state)
    await engine.shutdown()
"""

import hashlib
import math
import os
import re
import time
import asyncio
import logging
from collections import Counter
from typing import List, Optional, Dict, Union

import numpy as np
import httpx

from matryoshka import (
    get_fast_slice,
    get_full_vector,
    cosine_similarity,
    batch_cosine_similarity,
    validate_vector,
    SLICE_FULL,
)
from multi_head_hasher import EngramMultiHeadHasher
from hot_tier import EngramHotTier
from models import MemoryResult, EngramConfig, RecallEngineHealth

logger = logging.getLogger("engram.engine")

# ─── BM25 Sparse Vectors ────────────────────────────────────────────
# Hash-based tokenization: no global vocabulary needed, deterministic,
# works independently across local and cloud deployments.

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "to", "of", "in", "for", "on",
    "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "between", "out", "off", "over", "under", "then",
    "here", "there", "when", "where", "why", "how", "all", "each",
    "both", "more", "most", "other", "some", "no", "not", "only",
    "so", "than", "too", "very", "just", "but", "and", "or", "if",
    "that", "this", "it", "its", "i", "me", "my", "we", "our", "you",
    "your", "he", "him", "his", "she", "her", "they", "them", "their",
    "what", "which", "who",
})

_CATEGORY_PATTERNS = {
    "decision":     re.compile(r"\b(decided|chose|chosen|selected|will use|going with|switched to|moved to|picking|we chose|opted for|committed to|approved|rejected|went with|agreed to|adopted|deprecated|migrated to)\b", re.IGNORECASE),
    "preference":   re.compile(r"\b(prefer|like|always|never|want|love|hate|enjoy|favor|rather|choose to|tend to|usually|my favorite|i always|strongly preferred|mandatory|over spaces|over tabs)\b", re.IGNORECASE),
    "goal":         re.compile(r"\b(goal|objective|target|aim|milestone|want to achieve|trying to|working toward|by end of|need to finish|deadline|ship by|launch|roadmap|OKR|KPI)\b", re.IGNORECASE),
    "plan":         re.compile(r"\b(plan|strategy|approach|roadmap|next step|will do|going to|schedule|sprint|phase|rollout|timeline|proposal|architecture decision)\b", re.IGNORECASE),
    "error":        re.compile(r"\b(error|bug|issue|broke|failed|crash|exception|stack trace|wrong|incorrect|broken|regression|incident|outage|root cause|fix needed|traceback)\b", re.IGNORECASE),
    "insight":      re.compile(r"\b(realized|noticed|discovered|learned|found that|turns out|key insight|pattern|interesting|observation|in retrospect|lesson|takeaway|conclusion)\b", re.IGNORECASE),
    "skill":        re.compile(r"\b(knows how to|can|expert in|proficient|familiar with|experienced with|trained in|certified|specializes in|good at|fluent in|mastered)\b", re.IGNORECASE),
    "event":        re.compile(r"\b(meeting|call|demo|launch|release|deploy|shipped|presentation|interview|review|ceremony|standup|retrospective|happened|occurred|took place|scheduled)\b", re.IGNORECASE),
    "question":     re.compile(r"\b(wondering|unclear about|need to know|to be determined|TBD|open question|need to figure out|investigate|unresolved|pending answer|not sure (why|how|what|when|where)|still unknown|needs clarification|haven't decided|can't figure out)\b", re.IGNORECASE),
    "relationship": re.compile(r"\b(reports to|manages|works with|partner|collaborator|stakeholder|owns|responsible for|depends on|integrated with|blocked by|supports|mentors)\b", re.IGNORECASE),
    "fact":         re.compile(r"\b(completed|version|status|count|runs|running|deployed|migrated|installed|updated|is at|currently|uses|located|configured|set to|costs|takes|measures|port|hosted|serves|stores|contains|supports|weighs|amounts|totals|averages|scheduled|launches?|occurs?|recurring|window|every \w+day|founded|has \d+|pays?\b|expires?|limit|rate|coverage|takes approximately|on port)\b", re.IGNORECASE),
    "entity":       re.compile(r"\b(company|team|person|project|service|app|platform|organization|department|manager|lead|owner|maintainer|vendor|client|VP|CTO|CEO|engineer)\b", re.IGNORECASE),
}

# ─── Privacy ─────────────────────────────────────────────────────────
PRIVATE_TAG_RE = re.compile(r"<private>(.*?)</private>", re.DOTALL)
SENSITIVE_RE = re.compile(
    r"(?:key|secret|token|password|passwd|api[_\-]?key|credentials?)\s*[:=]\s*\S{20,}",
    re.IGNORECASE,
)


def _strip_private_tags(content: str) -> tuple:
    """Strip <private> tags from content. Returns (cleaned_content, had_tags)."""
    if "<private>" not in content:
        return content, False
    cleaned = PRIVATE_TAG_RE.sub(r"\1", content)
    return cleaned, True


def _detect_sensitive(content: str) -> bool:
    """Auto-detect likely secrets in content."""
    return bool(SENSITIVE_RE.search(content))


def text_to_sparse_vector(text: str, boost_specifics: bool = False) -> Dict:
    """Convert text to a sparse vector for Qdrant hybrid search.

    Uses hash-based tokenization with unigrams + bigrams.
    No shared vocabulary needed — each token hashes to a stable uint32 index.
    boost_specifics=True weights numbers/versions higher (use at store time).
    """
    # Keep version strings like "16.2" as single tokens
    tokens = re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", text.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS and len(t) > 1]
    if not tokens:
        return {"indices": [], "values": []}

    tf = Counter(tokens)

    # Bigrams: "postgresql 16.2" and "github actions" as single entries
    for i in range(len(tokens) - 1):
        bigram = f"{tokens[i]} {tokens[i+1]}"
        tf[bigram] = tf.get(bigram, 0) + 1

    indices = []
    values = []
    for token, count in tf.items():
        idx = int(hashlib.md5(token.encode()).hexdigest()[:8], 16) & 0x3FFFFFFF
        weight = 1.0 + math.log(count) if count > 1 else 1.0
        if boost_specifics and re.match(r"^[0-9]", token):
            weight *= 1.5
        indices.append(idx)
        values.append(round(weight, 4))
    return {"indices": indices, "values": values}


class EngramRecallEngine:
    """
    Three-Tiered Brain for AI memory retrieval.

    Tier 1: Hot-Tier Cache (sub-millisecond)
        Frequency-adjusted decay cache. Memories that are accessed
        often stay hot. Zero search cost.

    Tier 2: Multi-Head Hash Index (O(1))
        LSH with 4 independent heads. Maps 64-dim fast-slice to
        binary signature for instant candidate retrieval.

    Tier 3: Full Vector Re-Rank (standard)
        Qdrant ANN search on the full 768-dim vector. Only used
        as fallback when Tiers 1+2 miss, or to re-rank hash candidates.
    """

    def __init__(self, config: Optional[EngramConfig] = None):
        self.config = config or EngramConfig()
        self.config.ensure_data_dir()

        # Initialize tiers
        self.hot_tier = EngramHotTier(
            max_size=self.config.hot_tier_max_size,
            decay_rate=self.config.hot_tier_decay_rate,
            similarity_threshold=self.config.hot_tier_similarity_threshold,
            decay_param=self.config.actr_decay_param,
            retrieval_threshold=self.config.actr_retrieval_threshold,
            noise_param=self.config.actr_noise_param,
            max_timestamps=self.config.actr_max_timestamps,
        )

        self.hasher = EngramMultiHeadHasher(
            dim=self.config.fast_dim,
            num_heads=self.config.hasher_num_heads,
            hash_size=self.config.hasher_hash_size,
            seed=self.config.hasher_seed,
        )

        # Graph layer (optional, embedded Kuzu)
        self.graph = None
        if self.config.graph_enabled:
            try:
                from graph_layer import EngramGraphLayer
                graph_path = os.path.join(self.config.data_dir, "graph.kuzu")
                self.graph = EngramGraphLayer(graph_path)
            except ImportError:
                logger.info("Kuzu not installed — graph layer disabled. pip install kuzu")
            except Exception as e:
                logger.warning(f"Graph layer init failed: {e}")

        # HTTP client for FastEmbed + Qdrant
        self._http: Optional[httpx.AsyncClient] = None

        # Lifecycle
        self._started_at: float = 0.0
        self._persist_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None

    # --- Lifecycle ---

    async def warmup(self) -> None:
        """
        Initialize the engine: load persisted state, connect to services.

        Call this once at startup before any search/store operations.
        """
        self._started_at = time.time()
        self._http = httpx.AsyncClient(timeout=30.0)

        # Load persisted hot-tier
        hot_path = os.path.join(
            self.config.data_dir, "hot_tier.json"
        )
        if os.path.exists(hot_path):
            try:
                self.hot_tier = EngramHotTier.load(hot_path)
                logger.info(
                    f"Restored hot-tier: {self.hot_tier.size} entries"
                )
            except Exception as e:
                logger.warning(f"Failed to load hot-tier, starting fresh: {e}")

        # Load persisted hash index
        hash_path = os.path.join(
            self.config.data_dir, "hash_index.pkl"
        )
        if os.path.exists(hash_path):
            try:
                self.hasher = EngramMultiHeadHasher.load(hash_path)
                logger.info(
                    f"Restored hash index: {self.hasher.size} docs"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to load hash index, starting fresh: {e}"
                )

        # Start background tasks
        if self.config.auto_persist:
            self._persist_task = asyncio.create_task(
                self._auto_persist_loop()
            )
        self._sweep_task = asyncio.create_task(
            self._decay_sweep_loop()
        )

        # Initialize graph schema
        if self.graph:
            try:
                self.graph.ensure_schema()
                logger.info(f"Graph layer ready: {self.graph.get_stats()}")
            except Exception as e:
                logger.warning(f"Graph schema init failed: {e}")

        # Initialize consolidator (manual-only in Community)
        self.consolidator = None
        if self.config.consolidation_enabled and self.graph:
            try:
                from consolidation import EngramConsolidator
                self.consolidator = EngramConsolidator(self)
            except ImportError:
                logger.debug("Consolidation module not available")
            except Exception as e:
                logger.warning(f"Consolidator init failed: {e}")

        # If hash index is empty but Qdrant has data, rebuild from vectors.
        # This handles fresh volumes, config changes, or corrupted state.
        if self.hasher.size == 0:
            try:
                resp = await self._http.get(
                    f"{self.config.qdrant_url}/collections/{self.config.collection}"
                )
                if resp.status_code == 200:
                    points = resp.json().get("result", {}).get("points_count", 0)
                    if points > 0:
                        logger.info(f"Hash index empty but Qdrant has {points} points — rebuilding...")
                        count = await self.rebuild_hash_index()
                        logger.info(f"Hash index rebuilt: {count} documents indexed")
            except Exception as e:
                logger.debug(f"Hash rebuild check skipped: {e}")

        # Pre-warm hot tier from recent Qdrant data if it's empty.
        # Scroll limit capped at 50 (or 20% of hot_tier_max_size, whichever is
        # smaller) so small benchmarks don't saturate the hot tier and starve
        # the hash tier of any attribution (task #67). Medium corpora still get
        # a warm start via promotion during the first handful of searches.
        prewarm_limit = min(50, max(1, int(self.config.hot_tier_max_size * 0.2)))
        if self.hot_tier.size == 0 and self.hasher.size > 0:
            try:
                # Scroll recent memories to seed the hot tier
                resp = await self._http.post(
                    f"{self.config.qdrant_url}/collections/{self.config.collection}/points/scroll",
                    json={
                        "limit": prewarm_limit,
                        "with_payload": True,
                        "with_vector": True,
                        "order_by": {"key": "created_at", "direction": "desc"},
                    },
                )
                if resp.status_code == 200:
                    points = resp.json().get("result", {}).get("points", [])
                    for point in points:
                        payload = point.get("payload", {})
                        raw_vec = point.get("vector", {})
                        if isinstance(raw_vec, dict):
                            raw_vec = raw_vec.get("dense", [])
                        if raw_vec:
                            import numpy as np
                            vec = np.array(raw_vec, dtype=np.float32)
                            self.hot_tier.upsert(
                                doc_id=str(point.get("id", "")),
                                vector=vec,
                                content=payload.get("content", ""),
                                category=payload.get("category", "other"),
                                metadata={k: v for k, v in payload.items()
                                         if k not in ("content", "category", "created_at")},
                            )
                    if points:
                        logger.info(f"Pre-warmed hot tier with {len(points)} recent memories")
            except Exception as e:
                logger.debug(f"Hot tier pre-warm skipped: {e}")

        # Create float index on created_at for timeline ordering
        try:
            await self._http.put(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/index",
                json={"field_name": "created_at", "field_schema": "float"},
            )
        except Exception:
            pass  # Index may already exist

        logger.info("Engram Recall Engine started (Three-Tiered Brain)")

    async def shutdown(self) -> None:
        """Persist state and clean up."""
        # Cancel background tasks
        if self._persist_task:
            self._persist_task.cancel()
        if self._sweep_task:
            self._sweep_task.cancel()

        # Persist state
        await self._persist()

        # Close HTTP client
        if self._http:
            await self._http.aclose()

        if self.graph:
            try:
                self.graph.close()
            except Exception:
                pass

        logger.info("Engram Recall Engine stopped")

    async def _persist(self) -> None:
        """Save hot-tier and hash index to disk."""
        try:
            hot_path = os.path.join(self.config.data_dir, "hot_tier.json")
            self.hot_tier.save(hot_path)

            hash_path = os.path.join(self.config.data_dir, "hash_index.pkl")
            self.hasher.save(hash_path)
        except Exception as e:
            logger.error(f"Failed to persist state: {e}")

    async def _auto_persist_loop(self) -> None:
        """Background task: auto-persist every N seconds."""
        while True:
            try:
                await asyncio.sleep(self.config.persist_interval)
                await self._persist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-persist error: {e}")

    async def _decay_sweep_loop(self) -> None:
        """Background task: sweep decayed memories from hot-tier."""
        while True:
            try:
                await asyncio.sleep(self.config.hot_tier_sweep_interval)
                removed = self.hot_tier.decay_sweep()
                if removed > 0:
                    logger.info(f"Decay sweep removed {removed} memories")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Decay sweep error: {e}")

    # --- Embedding ---

    async def _embed(self, text: str, type: str = None) -> np.ndarray:
        """
        Generate embedding via local FastEmbed service.

        Args:
            text: Text to embed.
            type: "document" for storage, "query" for search.
                  Adds nomic-embed-text-v1.5 task prefixes for better recall.

        Returns full-dimension vector (768 for nomic-embed-text-v1.5).
        """
        if not self._http:
            raise RuntimeError("Engine not started. Call warmup() first.")

        body = {"texts": [text]}
        if type:
            body["type"] = type

        response = await self._http.post(
            f"{self.config.embedding_url}/embeddings",
            json=body,
        )
        response.raise_for_status()
        data = response.json()

        # FastEmbed service returns {"embeddings": [[...]]}
        vec = np.array(data["embeddings"][0], dtype=np.float32)

        is_valid, msg = validate_vector(vec, self.config.embedding_dim)
        if not is_valid:
            raise ValueError(f"Invalid embedding: {msg}")

        return vec

    # --- Engram Cloud (optional extension layer) ---

    @property
    def _cloud_enabled(self) -> bool:
        return bool(self.config.api_key)

    def _local_classify(self, text: str) -> str:
        """Keyword-based category detection with priority ordering.

        Priority: decision > preference > goal > plan > error > insight > skill > event > question > relationship > fact > entity > other
        When multiple categories match with equal scores, higher-priority wins.
        """
        scores = {}
        for cat, pattern in _CATEGORY_PATTERNS.items():
            matches = pattern.findall(text)
            scores[cat] = len(matches)

        if not scores or max(scores.values()) == 0:
            return "other"

        # Priority ordering: decision > preference > fact > entity
        # _CATEGORY_PATTERNS is ordered this way, so max() with equal
        # scores returns the first (highest priority) category
        max_score = max(scores.values())
        for cat in _CATEGORY_PATTERNS:  # iterates in insertion order
            if scores.get(cat, 0) == max_score:
                return cat
        return "other"

    async def _check_conflicts(self, content: str, embedding: list, threshold: float = 0.75) -> list:
        """Search for near-matches that may contradict the new memory."""
        try:
            results = await self.search(query=content, top_k=5)
            conflicts = []
            for r in results:
                if (r.similarity or r.score) < threshold:
                    continue
                if r.content.strip().lower() == content.strip().lower():
                    continue  # exact duplicate, not a conflict
                # Simple contradiction heuristic: same subject, opposing values
                # Look for preference/value inversions
                contradiction_signals = [
                    # "prefer X" vs "prefer Y" pattern
                    (r"\bprefer[s]?\s+(\w+)", content.lower(), r.content.lower()),
                    # negation flip: "always X" vs "never X" or "don't X"
                    (r"\b(always|never|don't|do not|not)\b", content.lower(), r.content.lower()),
                ]
                is_conflict = False
                for pattern, text_a, text_b in contradiction_signals:
                    matches_a = re.findall(pattern, text_a)
                    matches_b = re.findall(pattern, text_b)
                    if matches_a and matches_b and set(matches_a) != set(matches_b):
                        is_conflict = True
                        break
                if is_conflict:
                    conflicts.append({
                        "id": r.doc_id,
                        "text": r.content,
                        "score": round(r.score, 3),
                        "category": r.category,
                    })
            return conflicts
        except Exception:
            return []

    @staticmethod
    def _build_match_context(result: "MemoryResult", query: str) -> str:
        """Build a human-readable explanation of why this result matched.

        This helps calling models (Claude, GPT, etc.) naturally rerank
        results without Engram needing to pay for LLM inference.
        """
        parts = []

        # Tier info
        tier_desc = {
            "hot": "frequently accessed",
            "hash": "structurally similar",
            "vector": "semantically similar",
            "graph": "related via connections",
            "overflow": "from cloud archive",
        }
        parts.append(tier_desc.get(result.tier, result.tier))

        # Category context
        if result.category and result.category != "other":
            parts.append(f"category: {result.category}")

        # Confidence
        if result.confidence:
            parts.append(f"{result.confidence} confidence")

        # Access frequency (hot tier signal)
        if result.access_count > 5:
            parts.append(f"accessed {result.access_count}x")

        # Preference signal from feedback
        if result.preference_boost > 0:
            parts.append("previously confirmed relevant")

        return " | ".join(parts)

    async def _cloud_intelligence(self, text: str) -> Dict:
        """Call Engram Cloud /v1/intelligence for compression, dedup, and
        category detection. Returns empty dict on failure so local processing
        is never blocked by cloud issues."""
        if not self._cloud_enabled:
            return {}
        try:
            response = await self._http.post(
                f"{self.config.api_url}/v1/intelligence",
                json={"text": text, "compress": True},
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "X-SDK-Version": "recall-engine/2.0",
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                return response.json()
            logger.debug(f"Cloud intelligence returned {response.status_code}")
        except Exception as e:
            logger.debug(f"Cloud intelligence failed (local unaffected): {e}")
        return {}

    async def _cloud_overflow_search(
        self, vector: np.ndarray, limit: int
    ) -> List:
        """Search Engram Cloud overflow storage for memories that exceeded
        local retention. Returns empty list on failure."""
        if not self._cloud_enabled:
            return []
        try:
            response = await self._http.post(
                f"{self.config.api_url}/v1/overflow/search",
                json={"vector": vector.tolist(), "limit": limit},
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "X-SDK-Version": "recall-engine/2.0",
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("results", [])
        except Exception as e:
            logger.debug(f"Cloud overflow search failed (local unaffected): {e}")
        return []

    # --- Store ---

    async def store(
        self,
        content: str,
        category: str = "other",
        metadata: Optional[Dict] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """
        Store a memory across all tiers.

        1. Generate embedding via FastEmbed
        2. Store full vector in Qdrant (Tier 3)
        3. Index fast-slice in Multi-Head Hasher (Tier 2)
        4. (Hot-tier is populated on retrieval, not on store)

        Args:
            content: The text to remember.
            category: Auto-detected or specified category.
            metadata: Optional key-value metadata.
            doc_id: Optional explicit ID (auto-generated if None).

        Returns:
            The document ID.
        """
        import uuid

        if not self._http:
            raise RuntimeError("Engine not started. Call warmup() first.")

        # Generate ID
        if doc_id is None:
            doc_id = str(uuid.uuid4())

        # Privacy: strip <private> tags and auto-detect secrets
        if metadata is None:
            metadata = {}
        content, had_private_tags = _strip_private_tags(content)
        is_private = metadata.get("private", False) or had_private_tags
        if not is_private and _detect_sensitive(content):
            is_private = True
        metadata["private"] = is_private

        # Embed (document prefix for storage) — always local, always fast
        vector = await self._embed(content, type="document")

        # Conflict detection: find near-matches that may contradict this memory
        conflicts = await self._check_conflicts(content, [], threshold=0.75)

        # Cloud extension: compression, dedup, category detection
        # Runs in parallel with local processing, never blocks on failure
        cloud = await self._cloud_intelligence(content)
        if cloud.get("dedup", {}).get("is_duplicate"):
            logger.debug(f"Cloud dedup: skipping duplicate (similarity {cloud['dedup'].get('similarity', '?')})")
            return doc_id, category, conflicts  # return ID but don't write — caller sees success

        # Local keyword classifier — free, instant, no API needed
        if category == "other":
            category = self._local_classify(content)

        # If cloud provided a better category and caller used default, use it
        if cloud.get("category") and category == "other":
            category = cloud["category"]

        # Generate sparse vector for hybrid search
        sparse = text_to_sparse_vector(content, boost_specifics=True)

        # Store in Qdrant (Tier 3) — named vectors for hybrid search
        payload = {
            "text": content,
            "content": content,
            "category": category,
            "created_at": time.time(),
            "access_count": 0,
            **(metadata or {}),
        }

        # Attach cloud metadata to payload if available
        if cloud:
            if cloud.get("compressed_vector"):
                payload["compressed_vector_dim"] = len(cloud["compressed_vector"])
            if cloud.get("compression_ratio"):
                payload["compression_ratio"] = cloud["compression_ratio"]
            if cloud.get("quality_score"):
                payload["quality_score"] = cloud["quality_score"]

        await self._http.put(
            f"{self.config.qdrant_url}/collections/{self.config.collection}/points",
            json={
                "points": [
                    {
                        "id": doc_id,
                        "vector": {
                            "dense": vector.tolist(),
                            "bm25": sparse,
                        },
                        "payload": payload,
                    }
                ]
            },
        )

        # Index in Multi-Head Hasher (Tier 2)
        self.hasher.index(vector, doc_id)

        # Index in graph (entities + memory node) — skip private memories
        if self.graph and not metadata.get("private", False):
            try:
                from graph_layer import extract_entities
                self.graph.upsert_memory_node(doc_id, content, category, time.time())
                entities = extract_entities(content)
                if entities:
                    self.graph.add_entity_mentions(doc_id, entities)
            except Exception as e:
                logger.debug(f"Graph indexing failed: {e}")

        logger.debug(f"Stored memory {doc_id}: {content[:80]}...")
        return doc_id, category, conflicts

    # --- Search (The Three-Tiered Pipeline) ---

    async def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
    ) -> List[MemoryResult]:
        """
        Search for memories using the three-tiered brain.

        Tier 1 (Hot-Tier): Check frequency cache first.
            If sufficient results found → return immediately.

        Tier 2 (Hash): O(1) candidate lookup via LSH.
            Get candidate IDs → fetch from Qdrant by ID → re-rank.

        Tier 3 (Vector): Full ANN search on Qdrant.
            Fallback when Tiers 1+2 don't produce enough results.

        Args:
            query: Natural language search query.
            top_k: Maximum number of results.
            category: Optional filter by memory category.

        Returns:
            List of MemoryResult, sorted by relevance, tagged with tier.
        """
        if not self._http:
            raise RuntimeError("Engine not started. Call warmup() first.")

        # Embed the query (query prefix for search)
        query_vector = await self._embed(query, type="query")
        results: List[MemoryResult] = []

        # ── TIER 1: Hot-Tier Cache ──
        hot_results = self.hot_tier.search(
            query_vector,
            top_k=top_k,
            min_similarity=self.config.hot_tier_similarity_threshold,
        )

        for doc_id, similarity, strength in hot_results:
            entry = self.hot_tier.get_memory(doc_id)
            if entry is None:
                continue
            if category and entry.category != category:
                continue

            retrieval_p = self.hot_tier._retrieval_probability(strength)
            results.append(MemoryResult(
                doc_id=doc_id,
                content=entry.content,
                score=similarity * retrieval_p,
                tier="hot",
                category=entry.category,
                metadata=entry.metadata,
                access_count=entry.hits,
                strength=strength,
                similarity=similarity,
                retrieval_probability=retrieval_p,
            ))

        # Do NOT early-return from hot tier. Hot tier results don't have a
        # doc_vector attached, so trusting ACT-R-weighted ranking without a
        # vector-tier cross-check degrades R@1 when ACT-R and cosine disagree.
        # Always run hash + vector so the final re-rank sees all candidates.
        remaining = top_k
        hot_ids = {r.doc_id for r in results}
        seen_ids = set(hot_ids)

        # ── TIER 2: Multi-Head Hash Index ──
        # Run hash lookup across ALL candidate IDs — do NOT subtract hot-seen
        # ids. When a candidate overlaps with a hot result we REPLACE the hot
        # entry with the hash entry: hash carries doc_vector so the final
        # cosine re-rank operates on real similarity, and the result earns
        # correct "hash" tier attribution instead of masquerading as "hot"
        # (task #67 — hash was dominated by hot's first-seen priority).
        candidate_ids = self.hasher.search_candidates(query_vector)

        if candidate_ids:
            # Fetch candidate vectors from Qdrant by ID
            hash_results = await self._fetch_and_rerank(
                query_vector, candidate_ids, max(remaining, len(candidate_ids)), category
            )

            hash_by_id = {r.doc_id: r for r in hash_results}

            # Replace any hot-tier result that also appeared in hash candidates
            # with the hash version — hash carries the real doc_vector and has
            # a genuine retrieval signal, so it wins attribution.
            for i, r in enumerate(results):
                if r.doc_id in hash_by_id:
                    hash_result = hash_by_id.pop(r.doc_id)
                    hash_result.tier = "hash"
                    results[i] = hash_result

                    self.hot_tier.upsert(
                        doc_id=hash_result.doc_id,
                        vector=hash_result.doc_vector if hash_result.doc_vector is not None else query_vector,
                        content=hash_result.content,
                        category=hash_result.category,
                        metadata=hash_result.metadata,
                    )

            # Append remaining hash-only results (not already in hot)
            for result in hash_results:
                if result.doc_id not in hash_by_id:
                    continue  # Already consumed as a replacement above
                result.tier = "hash"
                results.append(result)
                seen_ids.add(result.doc_id)

                # Promote to hot-tier with actual document vector
                self.hot_tier.upsert(
                    doc_id=result.doc_id,
                    vector=result.doc_vector if result.doc_vector is not None else query_vector,
                    content=result.content,
                    category=result.category,
                    metadata=result.metadata,
                )

        # ── TIER 3: Hybrid Search (always runs) ──
        # Hybrid (dense + BM25) can find keyword matches that semantic-only misses.
        # Always run it and merge with Tier 1+2 — best scores win.
        vector_results = await self._qdrant_search(
            query_vector, top_k * 2, category, query_text=query
        )

        for result in vector_results:
            if result.doc_id in seen_ids:
                continue
            result.tier = "vector"
            results.append(result)
            seen_ids.add(result.doc_id)

            # Promote to hot-tier with actual document vector
            self.hot_tier.upsert(
                doc_id=result.doc_id,
                vector=result.doc_vector if result.doc_vector is not None else query_vector,
                content=result.content,
                category=result.category,
                metadata=result.metadata,
            )

        # ── Graph expansion: spreading activation from top results ──
        if self.graph and results:
            try:
                result_ids = [r.doc_id for r in results]
                self.graph.add_co_retrieval(result_ids)

                graph_candidate_ids = set()
                for r in results[:3]:
                    related = self.graph.get_related_memory_ids(
                        r.doc_id, max_hops=self.config.graph_max_hops
                    )
                    graph_candidate_ids.update(related)
                graph_candidate_ids -= seen_ids

                if graph_candidate_ids:
                    graph_results = await self._fetch_and_rerank(
                        query_vector, list(graph_candidate_ids)[:20],
                        top_k=5, category=category
                    )
                    for r in graph_results:
                        r.tier = "graph"
                        r.score *= self.config.graph_spreading_weight
                        results.append(r)
                        seen_ids.add(r.doc_id)
            except Exception as e:
                logger.debug(f"Graph expansion failed: {e}")

        # ── Cloud overflow: extend local results with cloud storage ──
        # Only runs when API key is set AND local results are insufficient.
        # Cloud overflow memories are ones that exceeded local retention or
        # were archived. They get tagged with tier="overflow".
        if self._cloud_enabled and (len(results) == 0 or len(results) < top_k):
            try:
                overflow = await self._cloud_overflow_search(
                    query_vector, top_k - len(results)
                )
                for mem in overflow:
                    mid = str(mem.get("id", ""))
                    if mid in seen_ids:
                        continue
                    results.append(MemoryResult(
                        doc_id=mid,
                        content=mem.get("content", mem.get("text", "")),
                        score=float(mem.get("score", 0)),
                        tier="overflow",
                        category=mem.get("category", "other"),
                    ))
                    seen_ids.add(mid)
            except Exception as e:
                logger.debug(f"Cloud overflow search failed (local unaffected): {e}")

        # Post-fusion cosine re-rank: re-score EVERY result by pure
        # cosine similarity to the query. For hot tier results (which
        # have no transient doc_vector), look up the vector from the
        # hot tier cache. This was the R@1 regression in the cloud
        # port — hot tier results were keeping their ACT-R-weighted
        # scores instead of getting re-ranked, which let strength-
        # boosted but semantically-distant items win top-1.
        for r in results:
            doc_vec = r.doc_vector
            if doc_vec is None or len(doc_vec) == 0:
                # Try the hot tier cache — every promoted doc stores its vector
                hot_entry = self.hot_tier.get_memory(r.doc_id)
                if hot_entry is not None and hot_entry.vector is not None:
                    doc_vec = hot_entry.vector
            if doc_vec is not None and len(doc_vec) > 0:
                cos_sim = float(cosine_similarity(query_vector, doc_vec))
                r.score = cos_sim
                r.similarity = cos_sim

        # Preference boost: memories confirmed useful by calling models
        # get a score boost proportional to how many times they've been preferred.
        if self.graph:
            for r in results:
                try:
                    pref_count = self.graph.get_preference_boost(r.doc_id)
                    if pref_count > 0:
                        # Logarithmic boost: diminishing returns to prevent runaway scores
                        # 1 preference = 5% boost, 5 = 12% boost, 20 = 18% boost
                        boost = 1.0 + 0.05 * math.log1p(pref_count)
                        r.score *= boost
                        r.preference_boost = boost - 1.0  # Store the delta, not the multiplier
                        logger.debug(f"Preference boost for {r.doc_id[:8]}: {pref_count} prefs → {boost:.3f}x")
                except Exception:
                    pass


        # Cross-encoder reranking: blend semantic similarity with cross-encoder
        # judgment before normalization so blending operates on comparable scales.
        results = await self._rerank_results(query, results, top_k)

        # Score normalization and confidence tagging
        for r in results:
            # Normalize scores: stretch the useful 0.3-1.0 range to 0.0-1.0
            raw = r.score
            r.score = max(0.0, min(1.0, (raw - 0.3) / 0.7))
            # Tag confidence
            if raw >= 0.7:
                r.confidence = "high"
            elif raw >= 0.5:
                r.confidence = "medium"
            else:
                r.confidence = "low"

        # Build match context for each result (helps calling model rerank)
        for r in results:
            r.match_context = self._build_match_context(r, query)

        # Final sort by normalized score, take top_k
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def ingest_rerank_feedback(
        self,
        query: str,
        selected_ids: List[str],
        rejected_ids: Optional[List[str]] = None,
    ) -> Dict:
        """
        Ingest reranking feedback from the calling model.

        When an AI agent uses search results, it can report which results
        were actually useful. This signal is free — the agent's model
        already evaluated the results. We just capture the judgment.

        Effects:
        - Selected memories get boosted in hot tier (access_count +2, last_access refreshed)
        - Rejected memories get penalized for this query context (hits -1, floor 1)
        - PREFERRED_OVER edges stored in Kuzu graph for future searches
        """
        query_hash = hashlib.md5(query.encode()).hexdigest()[:12]
        now = time.time()

        boosted = 0
        penalized = 0
        edges_added = 0

        # Boost selected memories — force-promote to hot tier if not already there
        for doc_id in selected_ids:
            entry = self.hot_tier.get_memory(doc_id)
            if not entry:
                # Memory not in hot tier — fetch from Qdrant and promote
                try:
                    resp = await self._http.post(
                        f"{self.config.qdrant_url}/collections/{self.config.collection}/points",
                        json={"ids": [doc_id], "with_payload": True, "with_vector": True},
                    )
                    if resp.status_code == 200:
                        points = resp.json().get("result", [])
                        if points:
                            point = points[0]
                            payload = point.get("payload", {})
                            raw_vec = point.get("vector", {})
                            if isinstance(raw_vec, dict):
                                raw_vec = raw_vec.get("dense", [])
                            if raw_vec:
                                vec = np.array(raw_vec, dtype=np.float32)
                                self.hot_tier.upsert(
                                    doc_id=doc_id,
                                    vector=vec,
                                    content=payload.get("content", ""),
                                    category=payload.get("category", "other"),
                                    metadata={k: v for k, v in payload.items()
                                             if k not in ("content", "category", "created_at")},
                                )
                                entry = self.hot_tier.get_memory(doc_id)
                                logger.debug(f"Force-promoted {doc_id[:8]} to hot tier for feedback")
                except Exception as e:
                    logger.debug(f"Failed to fetch {doc_id[:8]} from Qdrant for feedback: {e}")

            if entry:
                entry.hits += 5  # Stronger boost (was +2, now +5)
                entry.last_access = time.time()
                boosted += 1
                logger.debug(f"Boosted {doc_id[:8]} (hits now {entry.hits})")

        # Penalize rejected memories (only if already in hot tier)
        for doc_id in (rejected_ids or []):
            entry = self.hot_tier.get_memory(doc_id)
            if entry:
                entry.hits = max(1, entry.hits - 3)  # Stronger penalty (was -1, now -3)
                penalized += 1
                logger.debug(f"Penalized {doc_id[:8]} (hits now {entry.hits})")

        # Store preference edges in graph
        if self.graph and selected_ids and rejected_ids:
            for sel_id in selected_ids:
                for rej_id in rejected_ids:
                    try:
                        self.graph.add_preference(sel_id, rej_id, query_hash)
                        edges_added += 1
                    except Exception as e:
                        logger.debug(f"Preference edge failed: {e}")

        return {
            "success": True,
            "boosted": boosted,
            "penalized": penalized,
            "edges_added": edges_added,
        }

    async def _rerank_results(
        self, query: str, results: List[MemoryResult], top_k: int
    ) -> List[MemoryResult]:
        """Cross-encoder reranking via local FastEmbed model.

        Uses sigmoid normalization (correct for ms-marco models) and
        QMD-style position-aware blending:
        - Rank 1-3:  80% original / 20% reranker (protect high-confidence)
        - Rank 4-10: 55% original / 45% reranker
        - Rank 11+:  35% original / 65% reranker

        Top-rank protection: rank #1 result gets a +0.05 bonus before
        blending to resist demotion (QMD approach).
        """
        if not results or not self.config.reranker_enabled:
            return results

        try:
            documents = [r.content for r in results]
            resp = await self._http.post(
                f"{self.config.embedding_url}/rerank",
                json={"query": query, "documents": documents},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.debug(f"Reranker unavailable ({resp.status_code}), using original order")
                return results

            rerank_scores = resp.json().get("scores", [])
            if len(rerank_scores) != len(results):
                return results

            # Sigmoid normalization: correct for ms-marco cross-encoders.
            # Raw logits are unbounded; sigmoid maps to [0, 1] probability.
            # This is what ms-marco was trained with — NOT min-max.
            import math
            rerank_scores = [1.0 / (1.0 + math.exp(-s)) for s in rerank_scores]

            # Position-aware blending with top-rank protection
            for i, r in enumerate(results):
                rank = i + 1
                rerank_score = rerank_scores[i]
                original_score = r.score

                # Top-rank bonus: protect rank #1 from demotion (QMD approach)
                if rank == 1:
                    original_score += 0.05

                if rank <= 3:
                    r.score = 0.80 * original_score + 0.20 * rerank_score
                elif rank <= 10:
                    r.score = 0.55 * original_score + 0.45 * rerank_score
                else:
                    r.score = 0.35 * original_score + 0.65 * rerank_score

            results.sort(key=lambda r: r.score, reverse=True)
            return results[:top_k]

        except Exception as e:
            logger.debug(f"Reranking failed, using original order: {e}")
            return results

    async def _fetch_and_rerank(
        self,
        query_vector: np.ndarray,
        candidate_ids: List[str],
        top_k: int,
        category: Optional[str] = None,
    ) -> List[MemoryResult]:
        """
        Fetch specific points from Qdrant by ID and re-rank with full vector.
        """
        try:
            response = await self._http.post(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/points",
                json={"ids": candidate_ids, "with_payload": True, "with_vector": True},
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Qdrant fetch error: {e}")
            return []

        points = data.get("result", [])
        results = []

        for point in points:
            payload = point.get("payload", {})
            point_category = payload.get("category", "other")

            if category and point_category != category:
                continue

            # Handle both named vectors (hybrid) and flat vectors (legacy)
            raw_vec = point.get("vector", {})
            if isinstance(raw_vec, dict):
                raw_vec = raw_vec.get("dense", [])
            point_vector = np.array(raw_vec, dtype=np.float32)
            if len(point_vector) == 0:
                continue

            similarity = cosine_similarity(query_vector, point_vector)

            if similarity < self.config.min_recall_score:
                continue

            results.append(MemoryResult(
                doc_id=point.get("id", ""),
                content=payload.get("content", payload.get("text", "")),
                score=float(similarity),
                tier="hash",
                category=point_category,
                metadata={
                    k: v for k, v in payload.items()
                    if k not in ("content", "category", "created_at", "access_count")
                },
                created_at=payload.get("created_at", 0),
                access_count=payload.get("access_count", 0),
                similarity=float(similarity),
                doc_vector=point_vector,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _qdrant_search(
        self,
        query_vector: np.ndarray,
        top_k: int,
        category: Optional[str] = None,
        query_text: Optional[str] = None,
    ) -> List[MemoryResult]:
        """
        Hybrid Qdrant search: dense vectors + BM25 sparse vectors with RRF fusion.
        Falls back to dense-only if hybrid fails (e.g., old collection without sparse vectors).
        """
        filter_clause = None
        if category:
            filter_clause = {
                "must": [
                    {"key": "category", "match": {"value": category}}
                ]
            }

        # Try hybrid search first (dense + sparse with RRF)
        if query_text:
            sparse = text_to_sparse_vector(query_text)
            if sparse["indices"]:
                query_body: Dict = {
                    "prefetch": [
                        {
                            "query": query_vector.tolist(),
                            "using": "dense",
                            "limit": top_k * 3,
                        },
                        {
                            "query": sparse,
                            "using": "bm25",
                            "limit": top_k,
                        },
                    ],
                    "query": {"fusion": "rrf"},
                    "limit": top_k,
                    "with_payload": True,
                    "with_vector": True,
                }
                if filter_clause:
                    query_body["filter"] = filter_clause

                try:
                    response = await self._http.post(
                        f"{self.config.qdrant_url}/collections/{self.config.collection}/points/query",
                        json=query_body,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        return self._parse_qdrant_results(data.get("result", {}).get("points", []))
                    else:
                        logger.debug(f"Hybrid search failed ({response.status_code}), falling back to dense-only")
                except Exception as e:
                    logger.debug(f"Hybrid search error, falling back to dense-only: {e}")

        # Fallback: dense-only search (works with old collections too)
        search_body: Dict = {
            "vector": {"name": "dense", "vector": query_vector.tolist()},
            "limit": top_k,
            "with_payload": True,
            "with_vector": True,
            "score_threshold": self.config.min_recall_score,
        }

        if filter_clause:
            search_body["filter"] = filter_clause

        try:
            response = await self._http.post(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/points/search",
                json=search_body,
            )
            # If named vector search fails, try flat vector (legacy collections)
            if response.status_code != 200:
                search_body["vector"] = query_vector.tolist()
                response = await self._http.post(
                    f"{self.config.qdrant_url}/collections/{self.config.collection}/points/search",
                    json=search_body,
                )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Qdrant search error: {e}")
            return []

        return self._parse_qdrant_results(data.get("result", []))

    def _parse_qdrant_results(self, hits: list) -> List[MemoryResult]:
        """Parse Qdrant search/query results into MemoryResult objects."""
        results = []
        for hit in hits:
            payload = hit.get("payload", {})
            # Extract document vector for hot-tier promotion
            raw_vec = hit.get("vector", {})
            if isinstance(raw_vec, dict):
                raw_vec = raw_vec.get("dense", [])
            doc_vec = np.array(raw_vec, dtype=np.float32) if raw_vec else None

            results.append(MemoryResult(
                doc_id=str(hit.get("id", "")),
                content=payload.get("content", payload.get("text", "")),
                score=float(hit.get("score", 0)),
                tier="vector",
                category=payload.get("category", "other"),
                metadata={
                    k: v for k, v in payload.items()
                    if k not in ("content", "category", "created_at", "access_count")
                },
                created_at=payload.get("created_at", 0),
                access_count=payload.get("access_count", 0),
                similarity=float(hit.get("score", 0)),
                doc_vector=doc_vec if doc_vec is not None and len(doc_vec) > 0 else None,
            ))
        return results

    # --- Forget ---

    async def forget(self, doc_id: str) -> bool:
        """
        Remove a memory from all tiers.

        Args:
            doc_id: The memory ID to forget.

        Returns:
            True if the memory was found and removed.
        """
        removed = False

        # Remove from hot-tier
        if self.hot_tier.remove(doc_id):
            removed = True

        # Remove from hash index
        if self.hasher.remove(doc_id):
            removed = True

        # Remove from graph
        if self.graph:
            try:
                self.graph.remove_memory(doc_id)
            except Exception:
                pass

        # Remove from Qdrant
        try:
            response = await self._http.post(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/points/delete",
                json={"points": [doc_id]},
            )
            if response.status_code == 200:
                removed = True
            # 400/404 means point doesn't exist in Qdrant — not an error
        except Exception as e:
            logger.debug(f"Qdrant delete skipped for {doc_id}: {e}")

        if removed:
            logger.debug(f"Forgot memory: {doc_id}")

        return removed

    # --- Direct Fetch & Timeline ---

    async def get_by_ids(self, doc_ids: list) -> list:
        """Fetch full memory details by ID(s)."""
        if not doc_ids:
            return []

        try:
            resp = await self._http.post(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/points",
                json={"ids": doc_ids, "with_payload": True, "with_vector": False},
            )
            resp.raise_for_status()
            points = resp.json().get("result", [])
        except Exception:
            return []

        results = []
        for p in points:
            pid = str(p.get("id", ""))
            pl = p.get("payload", {})
            content = pl.get("content", pl.get("text", ""))
            category = pl.get("category", "other")
            created_at = pl.get("created_at", 0.0)
            access_count = pl.get("access_count", 0)
            private = pl.get("private", False)

            mr = MemoryResult(
                doc_id=pid,
                content=content,
                score=1.0,
                tier="direct",
                category=category,
                metadata=pl,
                created_at=created_at,
                access_count=access_count,
                private=private,
            )

            if self.graph:
                try:
                    related = self.graph.get_related_memory_ids(pid, max_hops=1)
                    entities = []
                    try:
                        entity_data = self.graph.get_memories_for_entity(pid)
                        if entity_data:
                            entities = [e.get("name", "") for e in entity_data if e.get("name")]
                    except Exception:
                        pass
                    mr.metadata["connections"] = {
                        "related": related[:10],
                        "entities": entities[:10],
                    }
                except Exception:
                    pass

            results.append(mr)

        return results

    @staticmethod
    def parse_date(date_str: str) -> float:
        """Parse ISO 8601 date string to Unix timestamp. Accepts YYYY-MM-DD or full ISO."""
        from datetime import datetime, timezone
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {date_str!r}")

    async def timeline(
        self,
        hours: int = 24,
        category: str = None,
        limit: int = 20,
        from_ts: float = None,
        to_ts: float = None,
    ) -> list:
        """Browse recent memories chronologically."""
        import time as _time
        now = _time.time()

        # Explicit timestamps take priority over hours lookback
        if from_ts is not None or to_ts is not None:
            range_filter: dict = {}
            if from_ts is not None:
                range_filter["gte"] = float(from_ts)
            if to_ts is not None:
                range_filter["lte"] = float(to_ts)
            cutoff_condition = {"key": "created_at", "range": range_filter}
        elif hours > 0:
            cutoff = now - (hours * 3600)
            cutoff_condition = {"key": "created_at", "range": {"gte": cutoff}}
        else:
            cutoff_condition = None

        must_conditions = []
        if cutoff_condition:
            must_conditions.append(cutoff_condition)
        if category:
            must_conditions.append({
                "key": "category",
                "match": {"value": category},
            })

        scroll_filter = {"must": must_conditions} if must_conditions else None

        payload = {
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            "order_by": {"key": "created_at", "direction": "desc"},
        }
        if scroll_filter:
            payload["filter"] = scroll_filter

        try:
            resp = await self._http.post(
                f"{self.config.qdrant_url}/collections/{self.config.collection}/points/scroll",
                json=payload,
            )
            resp.raise_for_status()
            points = resp.json().get("result", {}).get("points", [])
        except Exception as e:
            logger.warning(f"timeline query failed: {e}")
            return []

        results = []
        for p in points:
            pid = str(p.get("id", ""))
            pl = p.get("payload", {})
            content = pl.get("content", pl.get("text", ""))
            category_val = pl.get("category", "other")
            created_at = pl.get("created_at", 0.0)
            private = pl.get("private", False)

            mr = MemoryResult(
                doc_id=pid,
                content=content,
                score=0.0,
                tier="timeline",
                category=category_val,
                created_at=created_at,
                private=private,
            )
            results.append(mr)

        return results

    # --- Context Injection ---

    def get_hot_context(self, top_k: int = 5) -> str:
        """
        Get formatted hot memories for system prompt injection.

        This is the "Personalized Brain" feature — the strongest
        memories are automatically included in the agent's context.
        """
        return self.hot_tier.get_context_injection(top_k)

    # --- Health ---

    async def get_health(self) -> RecallEngineHealth:
        """Check health of all tiers."""
        health = RecallEngineHealth(status="healthy")
        health.uptime_seconds = time.time() - self._started_at

        # Hot-tier
        health.hot_tier_size = self.hot_tier.size
        health.hot_tier_hit_rate = self.hot_tier.stats.hit_rate

        # Hash index
        health.hash_index_size = self.hasher.size
        health.avg_hash_candidates = self.hasher.stats.avg_candidates_per_search

        # Graph stats
        if self.graph:
            try:
                gstats = self.graph.get_stats()
                health.graph_node_count = gstats.get("memory_nodes", 0) + gstats.get("entity_nodes", 0)
                health.graph_edge_count = gstats.get("mentions_edges", 0) + gstats.get("related_edges", 0) + gstats.get("co_retrieved_edges", 0)
            except Exception:
                pass

        # Qdrant connectivity
        try:
            resp = await self._http.get(
                f"{self.config.qdrant_url}/collections/{self.config.collection}"
            )
            health.qdrant_connected = resp.status_code == 200
        except Exception as e:
            health.qdrant_connected = False
            health.errors.append(f"Qdrant: {e}")

        # FastEmbed connectivity
        try:
            resp = await self._http.get(f"{self.config.embedding_url}/health")
            health.fastembed_connected = resp.status_code == 200
        except Exception as e:
            health.fastembed_connected = False
            health.errors.append(f"FastEmbed: {e}")

        if health.errors:
            health.status = "degraded"

        return health

    # --- Index Rebuild ---

    async def rebuild_hash_index(self) -> int:
        """
        Rebuild the hash index from all vectors in Qdrant.

        Use this if the hash file is corrupted or after changing
        hash configuration (num_heads, hash_size).

        Returns:
            Number of documents re-indexed.
        """
        logger.info("Rebuilding hash index from Qdrant...")

        # Scroll through all points in Qdrant
        offset = None
        all_vectors = []
        all_ids = []

        while True:
            scroll_body: Dict = {
                "limit": 100,
                "with_payload": False,
                "with_vector": True,
            }
            if offset is not None:
                scroll_body["offset"] = offset

            try:
                resp = await self._http.post(
                    f"{self.config.qdrant_url}/collections/{self.config.collection}/points/scroll",
                    json=scroll_body,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"Qdrant scroll error during rebuild: {e}")
                break

            result = data.get("result", {})
            points = result.get("points", [])

            if not points:
                break

            for point in points:
                vec = point.get("vector")
                pid = point.get("id")
                if vec and pid:
                    all_vectors.append(vec)
                    all_ids.append(str(pid))

            offset = result.get("next_page_offset")
            if offset is None:
                break

        if not all_vectors:
            logger.warning("No vectors found in Qdrant for rebuild")
            return 0

        vectors_matrix = np.array(all_vectors, dtype=np.float32)
        count = self.hasher.rebuild_from_vectors(vectors_matrix, all_ids)

        # Persist immediately
        hash_path = os.path.join(self.config.data_dir, "hash_index.pkl")
        self.hasher.save(hash_path)

        logger.info(f"Hash index rebuilt: {count} documents")
        return count
