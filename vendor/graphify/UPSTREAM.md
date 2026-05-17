# graphify (vendored)

- Upstream: https://github.com/safishamsi/graphify
- Commit:   9c04b059bec494f524b9ee852f0af4d3aa04bf3d
- Pulled:   2026-04-14

Vendored in-tree so `engram-memory` ships `/graph` as a
first-class feature with no external PyPI install step. Credit to
Safi Shamsi. Original MIT license preserved at
`vendor/graphify/LICENSE`.

## What was dropped

To keep the vendored copy small we removed the per-assistant
`skill-*.md` and `skill.md` documents under `graphify/` (they are
skill installers for other AI coding tools and are unrelated to the
programmatic build path). Everything needed for
`build_from_json` / `cluster` / `to_html` / `to_json` is present.

## What engram uses

`/graph` imports:

- `graphify.build.build_from_json` — assemble NetworkX graph from
  nodes/edges dicts
- `graphify.cluster.cluster` — Leiden/Louvain community detection
- `graphify.export.to_html` — interactive vis.js HTML
- `graphify.export.to_json` — node_link JSON

`graphify.extract` (tree-sitter code parsing) is **not** used — engram
builds the extraction dict directly from memory payloads, so the
tree-sitter language packs listed in upstream `pyproject.toml` are
not required at runtime for `/graph`.
