# Hive Memory

A **hive** is a named shared memory pool that multiple Engram clients — on different devices, with different API keys — read from and write to as a single brain. It's the multi-device, multi-agent, hive-mind layer on top of the local-plus-cloud architecture.

When a hive is active on your local container, every `memory_store` also persists the memory to the hive (`POST /v1/hives/{hive_id}/memories`), and every `memory_search` queries the hive in parallel with the local tiers. Activation is local and explicit; nothing is shared without your action.

## Why it exists

The local + cloud dual-write already gives you a personal failsafe — lose the laptop, reinstall, reconnect your key, all your memories are back. The hive extends that to **other clients**:

- **One brain, many bodies.** Your laptop, phone, another AI agent, an IoT device — anywhere an MCP client with a granted key can connect — works against the same memory pool.
- **Team / org memory.** Multiple keys can be granted access to the same hive, so a team or a fleet of agents shares one knowledge base.
- **Device-loss recovery.** A clean Engram install joined to your hive backfills from the cloud copy — nothing has to live on the failing device.

## Prerequisites

- `ENGRAM_API_KEY` set on the container (`eng_live_…`). All hive tools fail closed with `"ENGRAM_API_KEY not configured"` without it.
- An Engram Cloud account ([app.engrammemory.ai](https://app.engrammemory.ai)) — hives live in the cloud control plane; the local container is a participant, not the source of truth.

## The five MCP tools

| Tool | Purpose | Required args |
|---|---|---|
| `hive_create` | Create a new hive | `name`, `slug` |
| `hive_list` | List hives your key has access to | — |
| `hive_grant` | Grant a key prefix access to a hive | `hive_id`, `key_prefix`, `permission` (`read` or `readwrite`, default `readwrite`) |
| `hive_revoke` | Revoke a granted key prefix | `hive_id`, `key_prefix` |
| `hive_grants_list` | List active grants on a hive | `hive_id` |

All five proxy to the Engram Cloud control plane at `/v1/hives/…` under a `Bearer ${ENGRAM_API_KEY}` header. Errors from the cloud are returned verbatim — the MCP server does not invent success on failure.

## Activating a hive locally

Hive selection is **per-container** and persisted to a small file under the data dir:

```
${DATA_DIR}/active_hive
```

(Typically `/data/engram/active_hive` inside the container.) The file holds the active `hive_id` and an `activated_at` timestamp. When present:

- Every `memory_store` writes locally **and** to the hive — `POST /v1/hives/{hive_id}/memories` with the same payload. Failures don't block the local write.
- Every `memory_search` queries the hive via `GET /v1/hives/{hive_id}/memories/search` and merges results with the local tiers. Hive memories carry `active_hive: "hive:{hive_id}"` in the result envelope so they're identifiable.

Selection / deactivation is done through the `/hive` MCP command surface (see `mcp/server.py` `_handle_hive_*`). To deactivate, remove the `active_hive` file.

## Permission model

Hive grants are issued by **API-key prefix** (`hive_grant`):

- `permission="readwrite"` — the granted key can store *and* search this hive.
- `permission="read"` — the granted key can search but not store. Useful for read-only client agents (a phone widget, a dashboard, an IoT sensor).
- Grants are managed through `hive_grants_list` and revoked with `hive_revoke`.

Only the owner key can manage grants. Grant logic is enforced cloud-side, not on the local container.

## Worked example — multi-device personal brain

You want your laptop, your phone agent, and a CI bot to share one memory pool.

```python
# On the laptop (the owner)
hive_create(name="Edwin's Brain", slug="edwin-brain")
# → { hive_id: "hv_…", slug: "edwin-brain", … }

# Grant the phone's key (readwrite) and the CI bot's key (read only)
hive_grant(hive_id="hv_…", key_prefix="eng_live_phone123", permission="readwrite")
hive_grant(hive_id="hv_…", key_prefix="eng_live_cibot456", permission="read")

# Verify
hive_grants_list(hive_id="hv_…")
# → grants for phone123 (readwrite) and cibot456 (read)
```

Then on each device, activate the hive in that container (via the `/hive` command). After activation, every `memory_store` on any of those devices lands in the shared pool, and every `memory_search` on any of them sees the union.

## Device-loss recovery

If a device dies:

1. Install Engram on the replacement device (`docker run … engrammemory/engram-memory:latest`).
2. Re-set `ENGRAM_API_KEY` to the same key (or any granted key).
3. Activate the hive (`/hive activate hv_…`).
4. The first few searches pull from cloud overflow / hive storage; the local Qdrant warms up as memories are re-touched. From the agent's perspective, nothing was lost.

## When you don't need a hive

You don't need a hive if:

- Only one device or one agent uses Engram.
- You want strict per-device isolation (e.g., separating client work from personal memory).

In those cases, the standard local + cloud dual-write (just the API key, no hive) gives you the failsafe without any sharing surface.

## See also

- [`SOUL-RULES.md`](SOUL-RULES.md) — recommended agent rules, including how the save-every-turn rule interacts with hive-active stores.
- [`ENGRAM_CLOUD.md`](ENGRAM_CLOUD.md) — the cloud features hives ride on top of (overflow storage, compression, dedup).
