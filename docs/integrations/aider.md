# Aider

**Engram Bridge works with Aider via a shell wrapper that runs
`engram-bridge pull` before launching `aider`, plus the
`engram-bridge` CLI + git hook + pytest plugin for the push path.**

Aider is a CLI tool — not an MCP client, not a hook host. So the
read path is "run the pull, feed its output to aider". The push
path is just the bridge CLI running outside aider, same as every
other agent.

Aider already pushes to git on every change you accept, which
pairs beautifully with the bridge's git post-commit hook: you
get a memory for every change aider lands, with no explicit
`engram-bridge push` calls at all.

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
- Aider installed (`pip install aider-chat` or via pipx).

Full bridge install and config walkthrough lives in
**[../../bridge/README.md](../../bridge/README.md)**.

---

## Wiring the read path

Aider takes an initial message via `--message` / `-m`. A
`$(engram-bridge pull)` substitution injects the pull output as
that initial message:

```sh
aider_with_memory() {
  local preface
  preface=$(engram-bridge pull 2>/dev/null || true)
  if [ -n "$preface" ]; then
    aider --message "$preface" "$@"
  else
    aider "$@"
  fi
}
alias aider=aider_with_memory
```

Paste that into `~/.bashrc` or `~/.zshrc` and source it. From
then on, `aider` runs a pull first, and:

- If the bridge is configured and returns relevant memories, aider
  starts with a markdown preamble of prior context in its first
  turn.
- If the bridge is off, disabled, unreachable, or returns zero
  results, `engram-bridge pull` exits `0` with empty stdout, the
  wrapper drops into the plain `aider` branch, and nothing is
  different from running aider directly.

### Alternative: conversation-file preamble

If you prefer not to consume aider's `--message` slot (you might
want to pass your own first instruction), write the pull output
to a scratch file and tell aider to load it with `/load`:

```sh
aider_with_memory() {
  engram-bridge pull 2>/dev/null > .engram-context.md || true
  if [ ! -s .engram-context.md ]; then
    rm -f .engram-context.md
  fi
  aider "$@"
}
```

Once aider starts, type `/load .engram-context.md` to fold the
preamble into the session. A `.gitignore` line for
`.engram-context.md` keeps it out of commits.

---

## Wiring the push path

### 1. Git post-commit hook (most important for aider)

Aider commits after every accepted change. Install the post-commit
hook and every one of those commits turns into an Engram memory
automatically:

```bash
cd ~/code/my-repo
engram-bridge install --git-hooks
```

That writes `.git/hooks/post-commit`:

```sh
#!/bin/sh
# engram-bridge: push-commit
engram-bridge push-commit >/dev/null 2>&1 || true
```

The installer is idempotent, backs up any existing hook, and the
`|| true` guard guarantees a broken bridge never blocks aider's
commit flow. This alone is usually enough — you don't need manual
milestones unless you want them.

### 2. Manual milestone push

For deliberate "remember this" moments:

```bash
engram-bridge push "aider session: rewrote retry loop to exponential backoff"
engram-bridge push "prefer stdlib logging, avoid loguru in this repo" \
    --type preference
```

### 3. pytest plugin

Aider projects often run pytest as their acceptance check. Install
the bridge and the pytest plugin auto-loads — green sessions push
a `test_pass` event with suite name, duration, and test count.
Disable per-run with `-p no:engram_bridge`.

### 4. Shell wrappers for jest / cargo / go

If aider is driving a non-Python codebase, paste the matching
wrapper into your rc file:

```sh
engram_go_test() {
  local start end status
  start=$(date +%s)
  go test "$@"
  status=$?
  end=$(date +%s)
  if [ "$status" -eq 0 ]; then
    engram-bridge push-test "$(basename "$PWD")" \
        "$((end - start))" "0" --runner go >/dev/null 2>&1 || true
  fi
  return "$status"
}
alias gotest=engram_go_test
```

---

## Wiring hive sharing (Wave 3)

```bash
engram-bridge hive list
engram-bridge hive create "aider-duo" --slug aider-duo
engram-bridge hive add-member <hive_uuid> <user_uuid> --role member
engram-bridge push "moved retry logic out of main()" --hive <hive_uuid>
engram-bridge pull --scope hive:<hive_uuid>
```

To pin an aider session to a hive's context, change the
`aider_with_memory` wrapper's pull command:

```sh
aider_with_memory() {
  local preface
  preface=$(engram-bridge pull --scope hive:<hive_uuid> 2>/dev/null || true)
  # ...
}
```

The post-commit hook pushes to your personal store by default. If
you want commits to fan out to a hive, wrap the push command
instead:

```sh
# .git/hooks/post-commit  (edit by hand after install)
#!/bin/sh
# engram-bridge: push-commit
engram-bridge push-commit >/dev/null 2>&1 || true
engram-bridge push "$(git log -1 --pretty=%B)" \
    --hive <hive_uuid> >/dev/null 2>&1 || true
```

> **Gap:** `engram-bridge push-commit` doesn't accept a `--hive`
> flag yet. For hive fan-out on every commit, you have to call
> `engram-bridge push` with the commit message after the built-in
> commit push (as shown above). A native `--hive` on
> `push-commit` is a small follow-up in a later wave.

> **Gap:** Aider doesn't speak MCP, so the `memory_search` /
> `memory_store` tools on [`mcp/server.py`](../../mcp/server.py)
> are not reachable from inside an aider session. If you need
> on-demand search in the middle of a chat, run
> `engram-bridge pull --top-k 5 --project <proj>` from another
> terminal and paste the output into aider.

---

## Troubleshooting

1. **Config file exists?**
   `test -f ~/.engram/config.yaml && echo ok || echo missing`
2. **`api_key` set?** Must start with `eng_live_`.
3. **`enabled: true`** in `~/.engram/config.yaml`?
4. **`engram-bridge status`** says `api health: ok`?
5. **Bridge log clean?** `tail -50 ~/.engram/bridge.log`

Aider-specific checks:

- If `aider` starts but the injected preamble looks like raw
  markdown in aider's first assistant reply, your shell isn't
  evaluating `engram-bridge pull` correctly — make sure the
  wrapper is defined as a function, not a `$()` substitution in a
  single-line alias (aliases don't capture multi-line output
  cleanly).
- Aider and the bridge share a git repo, so if aider is using a
  worktree and the post-commit hook isn't firing, check
  `.git/worktrees/<name>/hooks/post-commit` instead — the
  installer resolves `git rev-parse --git-common-dir` and writes
  to the correct location, but older aider versions that
  bypass worktree-aware hooks would miss it.
- The git hook runs from aider's working dir, which may differ
  from your shell's `PWD`. If the hook fires but the pushed
  memory has the wrong project_id, check the bridge log for the
  resolved `repo_root`.
