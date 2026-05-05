"""Basic store + search + forget flow.

Run with:

    export ENGRAM_API_KEY=pr_live_...
    python examples/basic_usage.py

Prints the stored id, every search hit, and the forget outcome. Every
network call is wrapped in a single ``EngramError`` handler so the
example doubles as a template for real error handling — replace the
``print`` calls with your own logging.
"""

from __future__ import annotations

import sys

from engrammemory import (
    EngramAuthError,
    EngramClient,
    EngramError,
)


def main() -> int:
    try:
        client = EngramClient()
    except EngramError as exc:
        # Usually "no api key set". Exit with a clear message instead
        # of a traceback so a CI operator can see what's wrong.
        print("Engram client setup failed: {}".format(exc), file=sys.stderr)
        return 2

    try:
        # 1. Store
        stored = client.store(
            "Production Postgres runs on port 5433 with pgvector 0.7.0",
            category="infra",
            importance=0.9,
            metadata={"source": "example", "owner": "platform-hive"},
        )
        print("[store]   id={} status={}".format(stored.id, stored.status))

        # 2. Search
        hits = client.search(
            "what port does prod postgres use",
            top_k=5,
        )
        print("[search]  {} results (tokens={})".format(len(hits.results), hits.query_tokens))
        for i, hit in enumerate(hits.results, start=1):
            print("  {}. [{}] {:.2f}  {}".format(i, hit.tier or "?", hit.score, hit.text[:80]))

        # 3. Forget the one we just stored
        gone = client.forget(stored.id)
        print("[forget]  status={} found={}".format(gone.status, gone.found))

        # 4. Health probe — last because it's the cheapest check
        health = client.health()
        print("[health]  api={} qdrant={} version={}".format(
            health.api, health.qdrant, health.version,
        ))
    except EngramAuthError as exc:
        print("Auth failed — is ENGRAM_API_KEY current? ({})".format(exc), file=sys.stderr)
        return 1
    except EngramError as exc:
        print("Engram call failed: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
