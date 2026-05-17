---
description: Generate an interactive visual graph of your memories. Powered by graphify (github.com/safishamsi/graphify, MIT).
argument-hint: "[query hint]"
allowed-tools: Bash, Read, Write, mcp__engrammemory__memory_search, mcp__engrammemory__memory_recall
---

Build an interactive vis.js graph of the user's Engram memories. **You
(the host LLM) do the entity extraction and relationship mapping
yourself** — the Python driver is only a renderer. No external LLM API
key is required. Do NOT call Anthropic, OpenAI, or any other LLM API.
`ANTHROPIC_API_KEY` is NOT needed and must not be referenced.

Arguments: $ARGUMENTS

Steps:

1. **Fetch every memory** via the engram MCP tools. Prefer
   `mcp__engrammemory__memory_search` with a broad query (`"*"` or an
   empty string) and a high `limit` (e.g. 1000). If that returns
   nothing useful, fall back to `mcp__engrammemory__memory_recall`
   with a broad prompt. Collect the full set of memory objects
   (`id`, `content`/`text`, `category`, `created_at`, etc.).

2. **If zero memories come back**, tell the user exactly:
   `No memories stored yet — store some via the engram MCP tools first.`
   and stop.

3. **Build nodes.** For each memory emit one node:
   - `id`: the memory id (string)
   - `label`: short summary you write, ≤60 chars
   - `category`: the memory's `category` field if present, else `"general"`
   - `content`: first ~200 chars of the memory text
   - `entities`: array of noun phrases / named entities **you** identify
     by reading the content — people, projects, technologies, places,
     decisions, file paths, repos, commands. Be specific; lowercase;
     dedupe within a node.

4. **Build edges.** Connect memories using your own reasoning:
   - Shared entity → `{type:"shared-entity", label:<entity>, weight:0.8}`
     (one edge per shared entity pair; dedupe symmetric duplicates)
   - Explicit reference (one memory names another by id or unique topic)
     → `{type:"reference", weight:0.95}`
   - Timestamps within ~10 minutes → `{type:"temporal", weight:0.4}`
   - Clear thematic overlap that isn't a shared named entity →
     `{type:"topic", label:<theme>, weight:0.5}`
   `weight` is your confidence in [0,1]. Skip weak edges (<0.3).

5. **Write the JSON.** Pick a timestamp `TS = $(date +%Y%m%d-%H%M%S)`.
   Use the `Write` tool to create `~/.engram/graph-input-<TS>.json`
   with `{"nodes":[...], "edges":[...]}`.

6. **Render.** Run the graph script using its absolute path. Try these
   locations in order until one exists:
   - `$ENGRAM_REPO_DIR/scripts/engram_graph.py`
   - `$HOME/engram-memory/scripts/engram_graph.py`
   - Search with: `find $HOME -maxdepth 3 -name engram_graph.py -path "*/scripts/*" 2>/dev/null | head -1`
   
   Then run:
   `python3 /path/to/engram_graph.py --mode llm --input ~/.engram/graph-input-<TS>.json --output ~/.engram/graph-output-<TS>`
   The driver validates the JSON, passes it through vendored
   `graphify.build_from_json → cluster → to_html / to_json`, and
   prints a line of the form
   `[engram-graph] mode=llm nodes=N edges=M communities=K → <path>/graph.html`.

7. **Report.** Parse that line, then tell the user exactly:
   `Memory graph ready: <path>/graph.html — open in browser to explore. N nodes, M edges, K communities.`
   Offer to open it (e.g. `xdg-open <path>/graph.html`) but do not
   open without confirmation.

Do not modify the memory store. Do not invent memories. All
extraction is read-only. Credit: graphify
(github.com/safishamsi/graphify, MIT) powers the rendering pipeline.
