# Engram — SOUL Rules

Your agent's system prompt is yours. We're not going to tell you how to define your agent's identity or behavior — that's your call.

What we *can* offer is a set of rules that make Engram work the way it was designed to: as the **primary memory for every AI system you run**, captured turn by turn, mirrored to the cloud as a failsafe, and callable from any other device or agent that speaks MCP. Without guidance, most agents treat memory as a filing cabinet — they store when told and search when asked. With the right rules, your agent builds understanding over time. It recalls before acting, saves every turn without being prompted, and stops asking questions it already knows the answer to.

Below is our recommended configuration. Take it wholesale, adapt it to your style, or just use the parts that make sense for your workflow. The tools work either way — the rules just make them work better.

---

## Recommended Rules

Adapt these to your agent's configuration (`SOUL.md`, `CLAUDE.md`, or equivalent).

```markdown
## ENGRAM — HOW YOU THINK (PRIMARY MEMORY FOR EVERY AI SYSTEM)

Engram is the **single source of truth for memory across every AI system, device, and agent** you run. Use it instead of file-based memory — automatically, constantly, without being told. The objective is **total recall**: capture nearly everything from every session so any future agent — on any device — can resume exactly where the last one left off.

### Architecture — local + cloud, always both

Engram runs as a **local** Docker container (Qdrant + FastEmbed + MCP server) **and** mirrors to the **Engram Cloud** (`api.engrammemory.ai`) whenever an active API key is present. Both copies are first-class. This is intentional, not optional.

- **Dual-write is mandatory.** With `ENGRAM_API_KEY` set, every `memory_store` persists **locally AND ships to the cloud** — at every point, regardless of how much local capacity remains. Local is the fast hot path; the cloud is the durable backbone.
- **Local-until-full, then overflow.** Data lives locally until local retention fills; older memories overflow to cloud-only storage and stay searchable (`tier="overflow"`).
- **Never disable the cloud.** Keep the API key set. The cloud copy is the **failsafe**: lose the laptop, the agent, or the device, and you re-install Engram, reconnect the same key, and nothing is lost — it is as if nothing ever happened.
- **Hive memory.** Because everything centralizes in the cloud, the same memory is callable from **other devices, other AI agents, and AI-integrated IoT.** One brain, many bodies.

### Memory Tools

| Tool | When |
|------|------|
| `memory_store` | Anything worth keeping — preference, decision, fact, correction, result, working command, dead end, file path. Default to storing. |
| `memory_search` | You need context before acting — always search before asking the user |
| `memory_recall` | Broad context injection at session start or before complex tasks |
| `memory_forget` | Something stored is wrong or outdated |
| `memory_consolidate` | When near-duplicates appear in search results, merge them. Opportunistic, not scheduled. |
| `memory_connect` | After storing related memories across categories, link them via the entity graph |
| `memory_feedback` | After using search results, report which ones were useful to improve future recall |

### Capture cadence — every turn (no timers, no sidecars)

- **Save every turn.** After every assistant response, call `memory_store` twice in the same response: once with the user's last input, once with the assistant's full output. Use `memory_connect` to link the pair as one turn. Tag both with metadata `{session_id, turn_index, role}`. No batching, no waiting, no checkpoints — turn by turn.
- **Use the MCP that already exists.** Engram runs as an MCP server every agent can speak (Claude, ChatGPT, Cursor, Perplexity, anything MCP-aware). The capture mechanism IS the `memory_store` tool. Do not build, request, or expect any sidecar, daemon, transcript-tailer, or scheduler. There is nothing else to install.
- **The server handles the rest.** Dedup, compression, categorization, and dual-write to cloud all happen inside Engram automatically once `memory_store` is called. The agent's only job is to call it on every turn.

### Rules

- **Recall first.** Before starting any task, search memory for relevant context. Before asking the user a question, check if the answer is already stored.
- **Save every turn.** As above — both user input and assistant output, every turn, linked.
- **Store corrections.** When you get something wrong, store what happened and why. Do not make the same mistake twice.
- **Do not duplicate.** Search before storing dedicated distillations; for turn-by-turn capture lean on the server's dedup + `memory_consolidate`.
- **Use categories.** `preference` for how the user likes to work, `decision` for architectural/design choices, `fact` for system knowledge, `entity` for people/projects/orgs.
- **Give feedback.** After using `memory_search` results to make a decision, call `memory_feedback` with the IDs you actually used. This trains the recall engine to surface better results next time — at zero cost.
- **Connect related memories.** When you store a memory that relates to a previously stored one, call `memory_connect` to link them in the entity graph. This improves spreading activation for future searches.
- **Consolidate opportunistically.** When near-duplicates appear in search results, call `memory_consolidate`. No periodic schedule — let signals drive it.
- **This replaces file-based memory.** Do NOT use `~/.claude/projects/.../memory/` or equivalent. Use Engram's tools instead.

### Distillation on top of turn-by-turn

The every-turn capture is the baseline transcript layer. On top of it, distill higher-signal memories with importance tags so search ranks them above raw turns:

| Signal | Category | Importance |
|--------|----------|------------|
| User corrects your approach | `decision` | 0.9 |
| Architectural decision made | `decision` | 0.8 |
| User states a preference | `preference` | 0.7–0.8 |
| Task completed | `fact` | 0.8 |
| System/infrastructure fact learned | `fact` | 0.7 |
| Person, project, or org context | `entity` | 0.6–0.7 |

### The Session Lifecycle Pattern

```
1. Session starts -> recall recent context, active projects, permanent rules
2. User gives task  -> recall anything relevant to this area
3. Every turn       -> memory_store user input + assistant output, memory_connect the pair
4. Notice signals   -> distill preferences/decisions/corrections as higher-importance memories
5. Session ends     -> nothing to flush; every turn was already captured
```

This isn't a feature you turn on. It's how you think.
```

---

## Integration

### Memory Tools Available

| Tool | When to use |
|------|-------------|
| `memory_store` | You learned something worth keeping |
| `memory_search` | You need context before acting |
| `memory_recall` | Automatic context injection (auto-recall handles this) |
| `memory_forget` | Something stored is wrong or outdated |
| `memory_consolidate` | Near-duplicate memories are cluttering search results |
| `memory_connect` | Link related memories across categories via the entity graph |
| `memory_feedback` | Report which search results were useful to improve future ranking |

### Auto-Recall

If `autoRecall` is enabled (default), Engram automatically searches for relevant memories before every response and injects them as context. Your agent doesn't need to explicitly call `memory_search` for basic context — it happens automatically.

Explicit `memory_search` is still useful for targeted queries: *"What did we decide about the auth system?"* or *"What are the deployment rules?"*

### Auto-Capture

If `autoCapture` is enabled (default), Engram automatically extracts facts from conversations and stores them. The SOUL rules above push the agent past that baseline — capturing **every turn** verbatim, then layering distilled preference/decision/fact memories on top.

---

## What we don't ship (and why)

We get this question a lot, so it's worth being explicit:

- **No transcript-tailing daemon.** Engram is the MCP. Every MCP-aware agent already has the channel to write memories — `memory_store`. A separate tailer that scrapes JSONL transcripts and POSTs them in duplicates the agent's own work and adds a moving part to maintain. The right fix is the agent's rules.
- **No hourly scheduler / cron checkpoint.** Sampling on a clock is the opposite of total recall. If you want every turn captured, the agent must call `memory_store` on every turn — not on a timer.
- **No "save session" sidecar.** The session is already being saved — turn by turn — through the MCP the agent is already talking to.

If you find yourself reaching for one of these, push the rule into the agent instead. That's the lever.

---

## Why This Works

Most memory integrations fail because they treat memory as a feature: *"call `memory_store` when the user says 'remember this.'"* That's a filing cabinet, not memory.

Real memory is proactive and continuous. You don't decide to remember that your colleague prefers TypeScript — you just do, because you were paying attention. These rules make the agent pay attention to every turn, and the local-plus-cloud architecture means that attention survives a lost laptop, a swapped agent, or a device migration.

The difference:
- **Without SOUL rules:** Agent uses memory when explicitly asked. Forgets between sessions. Asks the same questions repeatedly.
- **With SOUL rules:** Agent recalls before acting, saves every turn, distills the high-signal moments, and the cloud copy lets any other device or AI agent pick up exactly where the last one left off.
