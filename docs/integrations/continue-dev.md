# Continue (VS Code / JetBrains)

**Engram Bridge works with Continue via MCP for the read path plus
the `engram-bridge` CLI + git hook + pytest plugin for the push
path.** Continue supports MCP servers in its `config.json` under
`experimental.modelContextProtocolServers`, so the seven Engram
tools from [`mcp/server.py`](../../mcp/server.py) show up in the
Continue tool menu for both the VS Code and JetBrains extensions.

Continue has no "on session start" hook, so the automatic-context
behavior is replaced by a Continue slash command or rule that
calls `memory_recall` on turn 1.

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
- Python 3.10+ on `PATH` so Continue can spawn `mcp/server.py`.

Full bridge install and config walkthrough lives in
**[../../bridge/README.md](../../bridge/README.md)**.

---

## Wiring the read path (MCP server)

Continue reads MCP config from `~/.continue/config.json`. Open
the file (or create it) and add an `experimental.modelContextProtocolServers`
entry. Continue's schema wraps each server under a `transport`
object:

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "stdio",
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
    ]
  }
}
```

Save and reload Continue (Command Palette → `Continue: Reload
Window` in VS Code, or Tools → Reload in JetBrains). The tool
menu should now list:

- `memory_store`
- `memory_search`
- `memory_recall`
- `memory_forget`
- `memory_consolidate`
- `memory_feedback`
- `memory_connect`

### Telling Continue to use the tools

Add a custom slash command to `~/.continue/config.json` that calls
`memory_recall` with your current task context:

```json
{
  "customCommands": [
    {
      "name": "recall",
      "prompt": "Call the memory_recall tool with a one-sentence summary of the task I just described. Use the returned memories as background context and continue the conversation.",
      "description": "Pull relevant Engram memories into context"
    }
  ]
}
```

Then type `/recall` at the start of any chat. If you want it on
every new chat automatically, add a rule under
`rules` / `systemMessage` (Continue's naming drifts across
versions) that tells the model to run the recall on its own on
turn 1.

### Alternative: `engram-bridge pull` on launch

Continue doesn't expose a pre-launch hook, but you can wrap your
editor launcher:

```sh
code_with_memory() {
  local out
  out=$(engram-bridge pull 2>/dev/null || true)
  if [ -n "$out" ]; then
    printf '%s\n' "$out" > .engram-context.md
  fi
  code "$@"
}
alias code=code_with_memory
```

Then add a rule telling Continue to read `.engram-context.md` on
turn 1.

---

## Wiring the push path

Continue doesn't do anything special for the push path — every
push helper is the bridge CLI running outside the editor.

### 1. Manual milestone push

```bash
engram-bridge push "moved API layer from axios to fetch"
engram-bridge push "user wants explicit error types, never any" --type preference
```

### 2. Git post-commit hook

```bash
cd ~/code/my-repo
engram-bridge install --git-hooks
```

Writes `.git/hooks/post-commit`. Idempotent; backs up any existing
hook before merging our block.

### 3. pytest plugin

Auto-loads via `pytest11` entry point after `pip install -e ./bridge`.

### 4. Shell wrappers for jest / cargo / go

Same wrapper recipe as every other agent. For a TS/JS project
using jest:

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

---

## Wiring hive sharing (Wave 3)

```bash
engram-bridge hive list
engram-bridge hive create "backend-crew" --slug backend-crew
engram-bridge hive add-member <hive_uuid> <user_uuid> --role member
engram-bridge push "standardized on zod for API validation" \
    --hive <hive_uuid>
engram-bridge pull --scope hive:<hive_uuid>
```

If you use the `code_with_memory` launcher, swap the pull for
`engram-bridge pull --scope hive:<uuid>` to pin the Continue
session's context to a specific hive collection.

> **Gap:** the Engram MCP server does not yet expose a `scope`
> argument, so `memory_search` and `memory_recall` from inside
> Continue only hit your personal collection. Hive reads go
> through the bridge CLI until a later wave.

---

## Troubleshooting

1. **Config file exists?**
   `test -f ~/.engram/config.yaml && echo ok || echo missing`
2. **`api_key` set?** Must start with `eng_live_`.
3. **`enabled: true`** in `~/.engram/config.yaml`?
4. **`engram-bridge status`** says `api health: ok`?
5. **Bridge log clean?** `tail -50 ~/.engram/bridge.log`

Continue-specific checks:

- Continue's MCP support sits under `experimental`. If you don't
  see the tool menu populate after reload, update Continue to the
  latest version — MCP wiring changes with almost every release.
- VS Code: open the "Continue" output channel (View → Output →
  Continue) to see the MCP server's stderr. `ImportError: mcp`
  means `pip install mcp` is missing from the Python interpreter
  that Continue is spawning. Use an absolute path to a venv'd
  Python in the `command` field if your system Python isn't the
  one you installed the bridge into.
- JetBrains: logs live under Help → Show Log in Files, look for
  entries tagged `Continue`.
