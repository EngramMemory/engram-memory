# Sourcegraph Cody

**Engram Bridge works with Sourcegraph Cody via manual context
pasting (for the read path) plus the `engram-bridge` CLI + git
hook + pytest plugin for the push path.**

Cody does not currently expose either a `SessionStart` hook or a
public MCP client interface, so the read path is "run
`engram-bridge pull` from your terminal and paste the output into
the Cody chat". It's the most manual of the supported agents, but
the push path — which is where most of the write volume lives —
still works the same as everywhere else.

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
- Cody installed (VS Code extension, JetBrains plugin, or the
  `cody` CLI).

Full bridge install and config walkthrough lives in
**[../../bridge/README.md](../../bridge/README.md)**.

---

## Wiring the read path

Cody has no hook surface, so we pipe the pull output to the
clipboard and paste it into the chat as the first message of the
conversation.

### macOS

```sh
engram-bridge pull | pbcopy
# then ⌘+V in the Cody chat
```

### Linux (X11 / Wayland with `wl-copy`)

```sh
engram-bridge pull | xclip -selection clipboard
# or
engram-bridge pull | wl-copy
```

### A terminal shortcut

Paste into `~/.bashrc` or `~/.zshrc`:

```sh
alias engram-copy='engram-bridge pull | pbcopy'        # macOS
# alias engram-copy='engram-bridge pull | xclip -selection clipboard'  # Linux X11
# alias engram-copy='engram-bridge pull | wl-copy'                     # Wayland
```

Then typing `engram-copy` before opening Cody means your clipboard
is always loaded with current project context, one paste away.

### Optional: Cody custom command

If you're on a Cody version that supports custom commands
(`.vscode/cody.json` or Cody's command settings), you can register
a command that runs a shell script emitting the pull output:

```json
{
  "commands": {
    "engram": {
      "description": "Load Engram memories for this project",
      "prompt": "Background context from my Engram memory:\n\n$(engram-bridge pull)\n\nUse this as preamble for the rest of the conversation."
    }
  }
}
```

Cody's custom commands don't universally support shell
substitution — if your version doesn't, fall back to the clipboard
workflow above.

---

## Wiring the push path

The push path is CLI-based and indistinguishable from every other
agent — Cody just happens to be running next to it.

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

Idempotent; backs up any existing hook.

### 2. Manual milestone push

```bash
engram-bridge push "shipped Cody-driven refactor of auth layer"
engram-bridge push "hive prefers explicit factory functions" \
    --type preference
```

### 3. pytest plugin

Auto-loads via `pytest11` entry point after `pip install -e ./bridge`.

### 4. Shell wrappers for jest / cargo / go

Paste one or more wrappers into your rc file. For a Node project:

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

Swap `npm`/`jest` for `cargo`/`cargo`, or `go`/`go` depending on
your stack.

---

## Wiring hive sharing (Wave 3)

```bash
engram-bridge hive list
engram-bridge hive create "cody-hive" --slug cody-hive
engram-bridge hive add-member <hive_uuid> <user_uuid> --role member
engram-bridge push "picked graphql over REST for this service" \
    --hive <hive_uuid>
engram-bridge pull --scope hive:<hive_uuid>
```

To paste hive context into Cody, replace `engram-copy` in your rc
file with:

```sh
alias engram-copy-hive='engram-bridge pull --scope hive:<hive_uuid> | pbcopy'
```

> **Gap:** Cody doesn't currently expose a public MCP or hook API
> that we can automate against, so there is no "run on session
> start" option — the copy-paste workflow is the only wiring for
> the read path. The push path is unaffected.

---

## Troubleshooting

1. **Config file exists?**
   `test -f ~/.engram/config.yaml && echo ok || echo missing`
2. **`api_key` set?** Must start with `eng_live_`.
3. **`enabled: true`** in `~/.engram/config.yaml`?
4. **`engram-bridge status`** says `api health: ok`?
5. **Bridge log clean?** `tail -50 ~/.engram/bridge.log`

Cody-specific checks:

- If `engram-bridge pull | pbcopy` puts nothing in your
  clipboard, run the bare `engram-bridge pull` first. Empty
  stdout means either the bridge is off or there are zero hits
  for the current project — `engram-bridge status` will tell you
  which.
- If the pasted preamble ends up truncated in Cody's chat input,
  that's a Cody limit, not an Engram one. Trim to your top hits
  with `engram-bridge pull --top-k 3`.
- Cody's free tier doesn't expose custom commands on every
  platform, so the clipboard path is the reliable one. Upgrade
  to Cody Pro / Enterprise if you need the custom-command
  workflow.
