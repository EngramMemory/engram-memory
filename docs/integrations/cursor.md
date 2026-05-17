# Cursor

**Engram Bridge works with Cursor via MCP (for search/store from
inside the chat) plus the `engram-bridge` CLI + git hook + pytest
plugin for the push path.** Cursor supports the Model Context
Protocol natively through `.mcp.json` / `~/.cursor/mcp.json`, so
you get `memory_search`, `memory_store`, `memory_recall`, and the
rest of the Engram toolbox without touching the bridge's session
hook.

Cursor has no equivalent of Claude Code's `SessionStart` hook, so
the "pull context automatically at the start of every session"
behavior is replaced by two things you do explicitly:

1. A Cursor rule that tells the model to call `memory_search` /
   `memory_recall` on the first turn of every chat.
2. (Optional) a shell alias that runs `engram-bridge pull` and
   pipes its markdown output into `cursor` on launch.

---

## Prerequisites

- Engram cloud API key starting with `eng_live_`
  (grab one at [https://engrammemory.ai](https://engrammemory.ai))
- Bridge installed:
  ```bash
  cd engram-memory
  pip install -e ./bridge
  ```
- `~/.engram/config.yaml` with a valid `api_key`:
  ```bash
  engram-bridge install --write-config-template
  $EDITOR ~/.engram/config.yaml
  engram-bridge status
  ```
- Python 3.10+ on `PATH` so Cursor can spawn `mcp/server.py`.

Full bridge install and config walkthrough lives in
**[../../bridge/README.md](../../bridge/README.md)**.

---

## Wiring the read path (MCP server)

Cursor reads MCP config from either:

- `~/.cursor/mcp.json` — global, applies to every project
- `<project>/.cursor/mcp.json` — per-project, checked into the repo

Pick one and add an `engrammemory` entry. The server script lives
at [`mcp/server.py`](../../mcp/server.py) inside this repo:

```json
{
  "mcpServers": {
    "engrammemory": {
      "command": "python",
      "args": [
        "/absolute/path/to/engram-memory/mcp/server.py"
      ],
      "env": {
        "QDRANT_URL": "http://localhost:6333",
        "FASTEMBED_URL": "http://localhost:11435",
        "COLLECTION_NAME": "agent-memory",
        "ENGRAM_API_KEY": "eng_live_...",
        "ENGRAM_API_URL": "https://api.engrammemory.ai"
      }
    }
  }
}
```

Restart Cursor. You should see `engrammemory` in the MCP status
panel with seven tools:

- `memory_store`
- `memory_search`
- `memory_recall`
- `memory_forget`
- `memory_consolidate`
- `memory_feedback`
- `memory_connect`

### Telling Cursor to use the tools

Cursor will call an MCP tool if and only if its system prompt
tells it to. Add a rule at `.cursor/rules/engram.mdc`:

```md
---
description: Engram Memory integration
alwaysApply: true
---

# Engram memory

On the first turn of every new chat, call `memory_recall` with a
short summary of the task the user just described. Use the
returned memories as background context. If nothing relevant
comes back, proceed without it — don't tell the user you checked.

When you finish a non-trivial task, call `memory_store` with a
one-sentence description of the outcome. Category should be
`decision` for design choices, `fact` for reproducible results,
`preference` for user preferences, and `other` otherwise.
```

This keeps memory traffic invisible in the chat transcript and
scopes it to the behavior that matters (start and end of a task).

### Alternative: `engram-bridge pull` on launch

If you'd rather have context in the system prompt (same behavior
as Claude Code's session hook), add a shell alias that runs
`engram-bridge pull` and stuffs the output into Cursor's working
directory as a scratch file the rule can reference:

```sh
cursor_with_memory() {
  local out
  out=$(engram-bridge pull 2>/dev/null || true)
  if [ -n "$out" ]; then
    printf '%s\n' "$out" > .engram-context.md
  fi
  cursor "$@"
}
alias cursor=cursor_with_memory
```

Then tell the rule to read `.engram-context.md` on turn 1. The
bridge is off-unless-configured, so if you're in a repo that
doesn't need memory there's no overhead — the alias runs a pull
that exits 0 silently and Cursor launches with no scratch file.

---

## Wiring the push path

The push path is identical across every agent — Cursor has no
special wiring because `engram-bridge` runs outside the editor.

### 1. Manual milestone push

```bash
engram-bridge push "shipped feature X"
engram-bridge push "user prefers tabs over spaces" --type preference
```

### 2. Git post-commit hook

```bash
cd ~/code/my-repo
engram-bridge install --git-hooks
```

Installs `.git/hooks/post-commit` with a single call to
`engram-bridge push-commit`. Idempotent, backed up, and wrapped in
`|| true` so a broken bridge never blocks a commit.

### 3. pytest plugin

Installing the bridge as a Python package registers a `pytest11`
entry point. Every green pytest session pushes a `test_pass` event
with suite name, duration, and test count. Disable per-run with
`-p no:engram_bridge`.

### 4. Shell wrappers for jest / cargo / go

Since Cursor projects are often JS/TS, you'll probably want the
jest wrapper. Paste into `~/.zshrc` or `~/.bashrc`:

```sh
engram_npm_test() {
  local start end status
  start=$(date +%s)
  npm test "$@"
  status=$?
  end=$(date +%s)
  if [ "$status" -eq 0 ]; then
    engram-bridge push-test "$(basename "$PWD")" \
        "$((end - start))" "0" --runner jest >/dev/null 2>&1 || true
  fi
  return "$status"
}
alias npmtest=engram_npm_test
```

Swap `npm` for `cargo`/`go` if your stack calls for it — all four
runner names (`pytest`, `jest`, `cargo`, `go`) are accepted by
`engram-bridge push-test --runner`.

---

## Wiring hive sharing (Wave 3)

```bash
# List hives
engram-bridge hive list

# Create a hive
engram-bridge hive create "cursor-squad" --slug cursor-squad

# Add a member (owner/admin only)
engram-bridge hive add-member <hive_uuid> <user_uuid> --role member

# Push a memory to personal + hive
engram-bridge push "decided on Tailwind over CSS modules" \
    --hive <hive_uuid>

# Pull context from a hive instead of personal
engram-bridge pull --scope hive:<hive_uuid>
```

If you use the `cursor_with_memory` alias above, swap
`engram-bridge pull` for `engram-bridge pull --scope hive:<uuid>`
to pin Cursor launches to a specific hive.

> **Gap:** the Engram MCP server does not yet expose hive scopes,
> so `memory_search` and `memory_store` from inside Cursor only hit
> your personal collection. Hive reads/writes have to go through
> the bridge CLI until a later wave.

---

## Troubleshooting

1. **Config file exists?**
   `test -f ~/.engram/config.yaml && echo ok || echo missing`
2. **`api_key` set?** Must start with `eng_live_`.
3. **`enabled: true`** in `~/.engram/config.yaml`?
4. **`engram-bridge status`** says `api health: ok`?
5. **Bridge log clean?** `tail -50 ~/.engram/bridge.log`

Cursor-specific checks:

- In Cursor's MCP status panel, does `engrammemory` show as
  "connected" with seven tools? If not, the MCP command failed —
  check Cursor's developer console (Help → Toggle Developer Tools)
  for the stderr from `mcp/server.py`. The most common cause is a
  missing Python dependency: run `pip install mcp` and restart
  Cursor.
- The MCP server prints `Warning: Recall engine not available` to
  stderr if the local `src/recall/` package isn't importable — the
  bridge repo needs to be on `PYTHONPATH` or installed, not just
  cloned.
- If `memory_search` works but `engram-bridge pull` doesn't, the
  two paths use different keys: the MCP server reads
  `ENGRAM_API_KEY` from the env block in `mcp.json`, while the
  bridge reads `api_key` from `~/.engram/config.yaml`. Make sure
  both point at the same key, or drop one of them.
