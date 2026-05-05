"""Minimal reranking feedback loop.

Simulates what a real agent loop does: search, let a (stub) judgment
function decide which hits are useful, then call /v1/feedback so the
cloud can boost good ids and penalize bad ones on future queries.

In production the judgment step is your own model — a cheap LLM
rerank, a rule-based classifier, or just the final state of your
context buffer. The key idea is that *any* signal you already
compute is useful to the cloud, so feedback should be cheap to wire
up everywhere search is called.

    export ENGRAM_API_KEY=pr_live_...
    python examples/feedback_loop.py
"""

from __future__ import annotations

import sys
from typing import Iterable, Tuple

from engrammemory import (
    EngramClient,
    EngramError,
    SearchResult,
)


def simulated_judgment(
    query: str,
    hits: Iterable[SearchResult],
) -> Tuple[list[str], list[str]]:
    """Pretend to be a reranker.

    Returns (selected_ids, rejected_ids). Real code would call a
    model here or inspect which ids the final answer cited.

    The heuristic: keep anything above 0.6 score AND containing any
    non-trivial word from the query. Drop the rest. This is just a
    stand-in so the example is runnable offline without a second
    dependency, not a serious reranker.
    """
    keywords = {w.lower() for w in query.split() if len(w) > 3}
    selected: list[str] = []
    rejected: list[str] = []
    for hit in hits:
        text_tokens = {w.lower().strip(".,:;?!") for w in hit.text.split()}
        overlaps = bool(keywords & text_tokens)
        if hit.score >= 0.6 and overlaps:
            selected.append(hit.id)
        else:
            rejected.append(hit.id)
    return selected, rejected


def main() -> int:
    client = EngramClient()
    try:
        # Seed a handful of memories so the example has something to
        # rank. In a real app these would already be in the cloud.
        seed = [
            ("Production postgres lives on port 5433 with pgvector 0.7", "infra"),
            ("Staging postgres is on 5432 for legacy tooling", "infra"),
            ("We chose SQS FIFO for the primary queue", "decisions"),
            ("Mountain biking is fun but expensive", "personal"),
        ]
        for text, category in seed:
            client.store(text, category=category, importance=0.6)

        query = "what port does production postgres use"
        hits = client.search(query, top_k=5)
        print("[search] {} hits for {!r}".format(len(hits.results), query))
        for h in hits.results:
            print("  {:.2f} [{}] {}".format(h.score, h.tier or "?", h.text[:80]))

        selected, rejected = simulated_judgment(query, hits.results)
        print("[judge]  kept {} / dropped {}".format(len(selected), len(rejected)))

        result = client.feedback(
            query=query,
            selected_ids=selected,
            rejected_ids=rejected,
        )
        print(
            "[feedback] success={} boosted={} penalized={} edges_added={}".format(
                result.success,
                result.boosted,
                result.penalized,
                result.edges_added,
            )
        )
    except EngramError as exc:
        print("Engram call failed: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
