"""End-to-end hive grant-based access flow.

Creates a hive, grants an API key prefix access, searches within the
hive scope, and then revokes access.

    export ENGRAM_API_KEY=pr_live_...
    python examples/hive_sharing.py <key_prefix_to_grant>

The key_prefix is the first few characters of an API key you want to
grant access to (e.g. "eng_live_abc").
"""

from __future__ import annotations

import sys

from engrammemory import (
    EngramAPIError,
    EngramClient,
    EngramError,
)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python hive_sharing.py <key_prefix>", file=sys.stderr)
        return 2
    key_prefix = argv[1]

    client = EngramClient()
    try:
        # 1. Create a hive. Slug must be lowercase alphanumeric + hyphens.
        hive = client.create_hive(name="Platform Ops", slug="platform-ops-demo")
        print("[create]  hive={} slug={}".format(hive.id, hive.slug))

        # 2. Grant access to an API key prefix.
        client.grant_hive_access(hive.id, key_prefix=key_prefix, permission="readwrite")
        print("[grant]   key_prefix={} permission=readwrite".format(key_prefix))

        # 3. List grants to verify.
        grants = client.list_hive_grants(hive.id)
        print("[grants]  {} active grants".format(len(grants)))
        for g in grants:
            print("  - prefix={} permission={}".format(
                g.get("key_prefix", "?"), g.get("permission", "?")
            ))

        # 4. Search within the hive scope.
        scope = "hive:{}".format(hive.id)
        hits = client.search(
            "what messaging system does the platform hive use",
            top_k=5,
            scope=scope,
        )
        print("[search]  scope={} -> {} hits".format(scope, len(hits.results)))
        for i, hit in enumerate(hits.results, start=1):
            print("  {}. {:.2f}  {}".format(i, hit.score, hit.text[:100]))

        # 5. List every hive the caller has access to.
        hives = client.list_hives()
        print("[list]    caller has access to {} hives".format(len(hives)))
        for t in hives:
            print("  - {} ({}) role={}".format(t.name, t.slug, t.role or "?"))

        # 6. Revoke access.
        client.revoke_hive_access(hive.id, key_prefix=key_prefix)
        print("[revoke]  key_prefix={}".format(key_prefix))

    except EngramError as exc:
        print("Engram call failed: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
