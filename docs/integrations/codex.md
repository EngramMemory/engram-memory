# OpenAI Codex CLI

**Engram Bridge works with the OpenAI Codex CLI (`codex`) via a
shell wrapper that runs `engram-bridge pull` before launch, plus
the `engram-bridge` CLI + git hook + pytest plugin for the push
path.**

The Codex CLI is a thin REPL around OpenAI's models — it has no
hook system, no MCP client, and no extension mechanism. So wiring
looks exactly like Aider's: you preload context with a shell
wrapper, and you push events with `engram-bridge` outside the
agent.

> "OpenAI Codex CLI" here means the `codex` / `openai-codex` CLI
> project, not the legacy Codex API. If you're using a different
> "codex"-named tool, the wiring recipes are the same — any CLI
> agent that reads an initial prompt can be wrapped the same way.

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
- Codex CLI installed and authenticated with your OpenAI key.

Full bridge install and config walkthrough lives in
**[../../bridge/README.md](../../bridge/README.md)**.

---

## Wiring the read path

The Codex CLI accepts a first-turn prompt via a positional
argument or stdin. Use a shell wrapper to preload Engram context:

```sh
codex_with_memory() {
  local preface
  preface=$(engram-bridge pull 2>/dev/null || true)
  if [ -n "$preface" ]; then
    # Most codex CLIs accept stdin on the first turn; -- ends options.
    printf '%s\n\n%s' "$preface" "$*" | codex
  else
    codex "$@"
  fi
}
alias codex=codex_with_memory
```

Behavior:

- Bridge configured + relevant hits → codex receives a markdown
  preamble on its first turn followed by your actual prompt.
- Bridge off / unreachable / zero results → pull exits `0` with
  empty stdout, the wrapper drops into the plain `codex "$@"`
  branch, nothing changes.

### Alternative: system-prompt injection

If your Codex CLI has a `--system` / `--instructions` flag, feed
the pull output there instead so it doesn't eat the first user
turn:

```sh
codex_with_memory() {
  local preface
  preface=$(engram-bridge pull 2>/dev/null || true)
  if [ -n "$preface" ]; then
    codex --system "$preface" "$@"
  else
    codex "$@"
  fi
}
```

Check `codex --help` for the exact flag name — it has drifted
across releases.

---

## Wiring the push path

### 1. Git post-commit hook

```bash
cd ~/code/my-repo
engram-bridge install --git-hooks
```

Writes `.git/hooks/post-commit`:

```sh
#!/bin/sh
# engram-bridge: push-commit
engram-bridge push-commit >/dev/null 2>&1 || true
```

Idempotent, backs up any existing hook, wrapped in `|| true` so a
broken bridge never blocks a commit.

### 2. Manual milestone push

```bash
engram-bridge push "codex session: migrated to httpx async client"
engram-bridge push "use OpenAI's structured outputs, not JSON-mode hacks" \
    --type preference
```

### 3. pytest plugin

Auto-loads via `pytest11` entry point after `pip install -e ./bridge`.
Green pytest sessions push a `test_pass` event with suite name,
duration, and test count. Disable per-run with `-p no:engram_bridge`.

### 4. Shell wrappers for jest / cargo / go

Paste the wrapper that matches your runner — the pattern is
identical across all four (`pytest`, `jest`, `cargo`, `go`):

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
engram-bridge hive create "codex-pod" --slug codex-pod
engram-bridge hive add-member <hive_uuid> <user_uuid> --role member
engram-bridge push "settled on httpx over aiohttp for this project" \
    --hive <hive_uuid>
engram-bridge pull --scope hive:<hive_uuid>
```

To pin a codex session to a hive, change the
`codex_with_memory` wrapper's pull to
`engram-bridge pull --scope hive:<hive_uuid>`. Revoked hive
memberships never throw — the bridge swallows 403s and the
wrapper drops back to the plain `codex` branch.

> **Gap:** Codex CLI doesn't speak MCP, so the `mcp/server.py`
> tools (`memory_search`, `memory_store`, etc.) aren't reachable
> from inside a codex session. For on-demand search mid-chat, run
> `engram-bridge pull --top-k 5 --project <proj>` from another
> terminal and paste the output.

> **Gap:** `engram-bridge push-commit` doesn't accept `--hive`
> yet. For hive fan-out on every commit, layer a manual
> `engram-bridge push "..." --hive <uuid>` call after the built-in
> `push-commit` in your `post-commit` hook until a later wave
> adds the flag.

---

## Troubleshooting

1. **Config file exists?**
   `test -f ~/.engram/config.yaml && echo ok || echo missing`
2. **`api_key` set?** Must start with `eng_live_`.
3. **`enabled: true`** in `~/.engram/config.yaml`?
4. **`engram-bridge status`** says `api health: ok`?
5. **Bridge log clean?** `tail -50 ~/.engram/bridge.log`

Codex-CLI-specific checks:

- If the injected preamble shows up as a literal first user
  message ("## Relevant memories..."), your Codex CLI doesn't
  treat stdin as a system prompt — switch to the `--system`
  wrapper variant above.
- If `codex` refuses to start when the wrapper is defined, the
  shell function is shadowing the real binary. Rename the
  function to something unique (`codexm`, `codex-mem`) and alias
  it if you want the short name.
- The `push-commit` hook uses `git show --stat` for its summary,
  which can be slow on very large commits. If codex sessions that
  touch hundreds of files feel noticeably laggier after the hook
  lands, check `~/.engram/bridge.log` for slow `push-commit`
  calls — each one is timestamped.
