# Claude Code

**Engram Bridge works with Claude Code via a native `SessionStart`
hook** that injects relevant memories at the start of every session.
Claude Code also speaks MCP natively, so you can point it at the
Engram MCP server for on-demand `memory_search` and `memory_store`
calls mid-session.

This page covers the CLI (`claude`). For the desktop app, see
**[claude-desktop.md](claude-desktop.md)**.

---

## Prerequisites

- Engram cloud API key starting with `eng_live_`
  (grab one at [https://engrammemory.ai](https://engrammemory.ai))
- Bridge installed:
  ```bash
  cd engram-memory
  pip install -e ./bridge
  ```
- Config file at `~/.engram/config.yaml` with a valid `api_key`.
  If you don't have one yet:
  ```bash
  engram-bridge install --write-config-template
  $EDITOR ~/.engram/config.yaml   # paste your key
  engram-bridge status            # must print "enabled: yes"
  ```

See **[../../bridge/README.md](../../bridge/README.md)** for the full
install + config walkthrough.

---

## Wiring the read path (`SessionStart` hook)

The bridge ships a one-shot installer that patches
`~/.claude/settings.json`:

```bash
engram-bridge install --claude-code
```

That command:

1. Creates `~/.claude/settings.json` if missing, else takes a
   timestamped `.bak-YYYYMMDD-HHMMSS` backup.
2. Merges a `hooks.SessionStart` entry into the file. The entry
   looks like this (idempotent — running the installer twice is a
   no-op):
   ```json
   {
     "hooks": {
       "SessionStart": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": "engram bridge pull",
               "id": "engram-bridge-pull"
             }
           ]
         }
       ]
     }
   }
   ```
3. Leaves any other settings keys untouched.

From then on, every new Claude Code session runs `engram bridge pull`
as a prompt prefix. The pull:

1. Detects the current project (git repo basename → directory name
   fallback).
2. Builds a query from repo name + branch + last commit subject.
3. Probes `GET /health` on `api.engrammemory.ai` with a 2 second
   timeout.
4. Calls `POST /v1/search` with `{query, top_k}` and
   `Authorization: Bearer <api_key>`.
5. Renders the returned hits as a markdown preamble and prints it
   to stdout. Claude Code picks it up as context.

On any failure (no config, bad key, unreachable API, zero results)
the pull exits `0` with empty stdout. Your Claude session starts
normally with no error — the worst case is you lose two seconds and
get no added context.

### Verify the hook is firing

1. Inspect the settings file:
   ```bash
   grep -A 8 engram-bridge-pull ~/.claude/settings.json
   ```
   You should see the `id` and the `command`.

2. Run the pull by hand to confirm the command works end-to-end:
   ```bash
   engram-bridge pull
   ```
   If the bridge is configured and there are any relevant memories,
   you'll see a `## Relevant memories` markdown block on stdout.

3. Start a fresh Claude Code session in a git repo and check the
   first assistant message. If the hook is firing and there's
   context to inject, Claude will reference it.

### Remove the hook

Open `~/.claude/settings.json` and delete the block tagged with
`"id": "engram-bridge-pull"`. The installer deliberately avoids an
`--uninstall` flag — it would have to touch user-modified JSON that
it didn't write, and that's more dangerous than a two-line manual
edit.

---

## Hive sharing

Hives let multiple AI agents (across platforms and devices) search
each other's memories. Access is scoped by API key — if your key
has a grant to a hive, you can search all memories from every
other granted key.

```bash
# List hives your api_key has access to
engram-bridge hive list

# Create a new hive — your key gets readwrite access automatically
engram-bridge hive create "my-hive" --slug my-hive

# Grant another API key access to the hive
engram-bridge hive grant <hive_uuid> <key_prefix>

# Revoke access
engram-bridge hive revoke <hive_uuid> <key_prefix>

# Pull context from a hive scope instead of personal
engram-bridge pull --scope hive:<hive_uuid>
```

When you search with `--scope hive:<id>`, the cloud finds every
API key granted to that hive and searches across all their
memories. Results come back attributed by key, so you know which
agent contributed what.

> **Tip:** to pull hive context at session start, edit
> `~/.claude/settings.json` and change the hook command to
> `engram bridge pull --scope hive:<hive_uuid>`.

---

## Optional: wire the MCP server

Claude Code speaks MCP. You can register the Engram MCP server in
addition to (or instead of) the bridge's `SessionStart` hook:

```bash
claude mcp add engrammemory -- python /path/to/engram-memory/mcp/server.py
```

The server ([`mcp/server.py`](../../mcp/server.py)) exposes seven
tools that Claude Code can call on demand:

- `memory_store` — store text with a category + importance score
- `memory_search` — three-tier search (hot cache → hash → vector)
- `memory_recall` — same as search, higher threshold, for context
- `memory_forget` — delete by id or by search-match
- `memory_consolidate` — merge near-duplicates
- `memory_feedback` — tell Engram which results were useful
- `memory_connect` — discover cross-category links

All seven tools talk to the **local** recall engine (Qdrant +
FastEmbed + hash index). If the cloud API is configured, the recall
engine falls back to cloud on local misses — giving you access to
hive-shared memories from other agents automatically.

---

## Troubleshooting

Run through this checklist in order — the bridge is designed to
fail silently, so `engram-bridge status` is your best friend.

1. **Config file exists?**
   ```bash
   test -f ~/.engram/config.yaml && echo ok || echo missing
   ```
2. **`api_key` set?**
   ```bash
   grep '^api_key:' ~/.engram/config.yaml
   ```
   The value must start with `eng_live_`.
3. **`enabled: true`?**
   ```bash
   grep '^enabled:' ~/.engram/config.yaml
   ```
4. **Bridge reports connected?**
   ```bash
   engram-bridge status
   ```
   You want `enabled: yes` and `api health: ok`.
5. **Recent errors in the log?**
   ```bash
   tail -50 ~/.engram/bridge.log
   ```
   Every failed pull or push is logged here with a timestamp.
   The log is rotated at 256 KB with two backups.

If all five check out and the hook still isn't producing context,
confirm that `~/.claude/settings.json` actually contains the
`engram-bridge-pull` id and that the `command` string matches
exactly (`engram bridge pull`). Start a brand new Claude Code
session — hooks only fire on session start, not on a live reload.
