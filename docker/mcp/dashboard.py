"""Engram Memory — Web Dashboard

Single-page dashboard served from the MCP server at /dashboard.
Queries existing REST endpoints for data. No external dependencies.
"""

import html as _html
import json
import logging
import time
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("engram.dashboard")

# Stats cache (60s TTL)
_stats_cache = {"data": None, "ts": 0}


def register_dashboard_routes(app: FastAPI, mcp_server):
    """Register dashboard routes on the FastAPI app."""

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page():
        return HTMLResponse(get_dashboard_html())

    @app.get("/dashboard/timeline")
    async def dashboard_timeline(
        limit: int = Query(50, le=200),
        offset: Optional[str] = Query(None),
        category: Optional[str] = Query(None),
        hours: int = Query(0),
    ):
        engine = mcp_server.engine
        import time as _time

        # Build filter
        must = []
        if hours > 0:
            must.append({"key": "created_at", "range": {"gte": _time.time() - hours * 3600}})
        if category:
            must.append({"key": "category", "match": {"value": category}})

        scroll_filter = {"must": must} if must else None

        url = f"{engine.config.qdrant_url}/collections/{engine.config.collection}/points/scroll"
        payload = {
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        if scroll_filter:
            payload["filter"] = scroll_filter
        if offset:
            payload["offset"] = offset

        try:
            resp = await engine._http.post(url, json=payload)
            data = resp.json().get("result", {})
            points = data.get("points", [])
            next_offset = data.get("next_page_offset")
        except Exception as e:
            logger.error(f"Timeline error: {e}")
            return JSONResponse({"memories": [], "next_offset": None, "total_estimate": 0})

        # Get total
        try:
            col_resp = await engine._http.get(
                f"{engine.config.qdrant_url}/collections/{engine.config.collection}"
            )
            total = col_resp.json().get("result", {}).get("points_count", 0)
        except Exception:
            total = len(points)

        memories = []
        for p in points:
            pl = p.get("payload", {})
            memories.append({
                "id": str(p.get("id", "")),
                "content": pl.get("content", pl.get("text", "")),
                "category": pl.get("category", "other"),
                "created_at": pl.get("created_at", 0),
                "access_count": pl.get("access_count", 0),
                "private": pl.get("private", False),
            })

        return JSONResponse({
            "memories": memories,
            "next_offset": str(next_offset) if next_offset else None,
            "total_estimate": total,
        })

    @app.get("/dashboard/graph-data")
    async def dashboard_graph_data(max_nodes: int = Query(200, le=500)):
        engine = mcp_server.engine
        if not engine.graph:
            return JSONResponse({"nodes": [], "edges": [], "stats": {}})

        try:
            memories = engine.graph.export_all_memories()
            edges_data = engine.graph.export_all_edges()
            stats = engine.graph.get_stats()
        except Exception as e:
            logger.error(f"Graph data error: {e}")
            return JSONResponse({"nodes": [], "edges": [], "stats": {}})

        # Community mapping: preference=0, fact=1, decision=2, entity=3, other=4
        _CAT_COMMUNITY = {"preference": 0, "fact": 1, "decision": 2, "entity": 3, "other": 4}

        # Build nodes (memories + entities)
        nodes = []
        node_ids = set()
        memory_ids = set()
        for m in memories[:max_nodes]:
            mid = m.get("id", "")
            memory_ids.add(mid)
            node_ids.add(mid)
            content = m.get("content", "")
            cat = m.get("category", "other")
            nodes.append({
                "id": mid,
                "label": content[:60] + ("..." if len(content) > 60 else ""),
                "type": "memory",
                "category": cat,
                "community": _CAT_COMMUNITY.get(cat, 4),
                "created_at": m.get("created_at", 0),
                "degree": 0,
            })

            for ent in m.get("entities", []):
                ent_id = f"entity:{ent.get('name', '')}"
                if ent_id not in node_ids:
                    node_ids.add(ent_id)
                    nodes.append({
                        "id": ent_id,
                        "label": ent.get("name", ""),
                        "type": "entity",
                        "entity_type": ent.get("type", ""),
                        "community": 3,
                        "degree": 0,
                    })

        # Build edges
        edges = []
        for mention in edges_data.get("mentions", []):
            src = mention.get("memory_id", "")
            tgt = f"entity:{mention.get('entity_name', '')}"
            if src in memory_ids:
                edges.append({"from": src, "to": tgt, "type": "mentions", "weight": 0.3})

        for cr in edges_data.get("co_retrieved", []):
            src = cr.get("memory_id_1", "")
            tgt = cr.get("memory_id_2", "")
            if src in memory_ids and tgt in memory_ids:
                count = cr.get("count", 1)
                edges.append({
                    "from": src, "to": tgt, "type": "co_retrieved",
                    "count": count,
                    "weight": min(count / 10.0, 1.0),
                })

        for rel in edges_data.get("related_to", []):
            src = rel.get("memory_id_1", "")
            tgt = rel.get("memory_id_2", "")
            if src in memory_ids and tgt in memory_ids:
                edges.append({
                    "from": src, "to": tgt, "type": "related_to",
                    "weight": rel.get("weight", 0.5),
                })

        # Compute degree for each node
        degree_map = {}
        for e in edges:
            degree_map[e["from"]] = degree_map.get(e["from"], 0) + 1
            degree_map[e["to"]] = degree_map.get(e["to"], 0) + 1
        for n in nodes:
            n["degree"] = degree_map.get(n["id"], 0)

        return JSONResponse({"nodes": nodes, "edges": edges, "stats": stats})

    @app.get("/dashboard/graph-html", response_class=HTMLResponse)
    async def dashboard_graph_html(max_nodes: int = Query(500, le=2000)):
        """Generate the full graphify-compatible vis.js HTML graph, identical to
        what vendor/graphify/graphify/export.py to_html() produces.

        Reuses:
          - _html_styles() CSS verbatim (brand tokens, sidebar, search, legend)
          - _html_script() JS verbatim (vis.js network, physics, search with
            focus-and-zoom, click-to-inspect info panel, community legend with
            show/hide, neighbor highlighting, degree-based sizing, edge weight)
          - to_html() HTML skeleton verbatim (graph, header, sidebar layout)
          - COMMUNITY_COLORS palette from export.py
          - Node/edge data format expected by _html_script()
        """
        engine = mcp_server.engine
        if not engine.graph:
            return HTMLResponse(_graph_empty_html("No graph layer available"))

        try:
            memories = engine.graph.export_all_memories()
            edges_data = engine.graph.export_all_edges()
            stats = engine.graph.get_stats()
        except Exception as e:
            logger.error(f"Graph HTML error: {e}")
            return HTMLResponse(_graph_empty_html(str(e)))

        if not memories:
            return HTMLResponse(_graph_empty_html("No memories in graph yet"))

        return HTMLResponse(_build_graph_html(memories, edges_data, stats, max_nodes))

    @app.get("/dashboard/stats")
    async def dashboard_stats():
        global _stats_cache
        now = time.time()
        if _stats_cache["data"] and now - _stats_cache["ts"] < 60:
            return JSONResponse(_stats_cache["data"])

        engine = mcp_server.engine

        # Basic stats
        try:
            col_resp = await engine._http.get(
                f"{engine.config.qdrant_url}/collections/{engine.config.collection}"
            )
            col_data = col_resp.json().get("result", {})
            total = col_data.get("points_count", 0)
        except Exception:
            total = 0

        # Category breakdown via scroll
        categories = {"preference": 0, "fact": 0, "decision": 0, "entity": 0, "other": 0}
        growth = {}
        try:
            scroll_url = f"{engine.config.qdrant_url}/collections/{engine.config.collection}/points/scroll"
            all_offset = None
            while True:
                payload = {
                    "limit": 100,
                    "with_payload": ["category", "created_at"],
                    "with_vector": False,
                }
                if all_offset:
                    payload["offset"] = all_offset
                resp = await engine._http.post(scroll_url, json=payload)
                data = resp.json().get("result", {})
                points = data.get("points", [])
                if not points:
                    break
                for p in points:
                    pl = p.get("payload", {})
                    cat = pl.get("category", "other")
                    if cat in categories:
                        categories[cat] += 1
                    else:
                        categories["other"] += 1

                    # Growth by hour (last 7 days)
                    ct = pl.get("created_at", 0)
                    if ct > now - 7 * 86400:
                        hour_key = time.strftime("%Y-%m-%dT%H:00", time.localtime(ct))
                        growth[hour_key] = growth.get(hour_key, 0) + 1

                all_offset = data.get("next_page_offset")
                if not all_offset:
                    break
        except Exception as e:
            logger.error(f"Stats scroll error: {e}")

        # Graph stats
        graph_stats = {}
        top_entities = []
        if engine.graph:
            try:
                graph_stats = engine.graph.get_stats()
            except Exception:
                pass

        # Hot tier stats
        hot_count = 0
        try:
            hot_count = len(engine.hot_tier.memories) if hasattr(engine, 'hot_tier') and engine.hot_tier else 0
        except Exception:
            pass

        result = {
            "total_memories": total,
            "categories": categories,
            "tiers": {"hot": hot_count, "total": total},
            "graph": graph_stats,
            "growth": sorted([{"hour": k, "count": v} for k, v in growth.items()], key=lambda x: x["hour"]),
            "uptime_seconds": now - engine._start_time if hasattr(engine, '_start_time') else 0,
        }

        _stats_cache = {"data": result, "ts": now}
        return JSONResponse(result)


# ─── Graphify renderer helpers ────────────────────────────────────────────────
# These reproduce the exact output of vendor/graphify/graphify/export.py
# to_html() / _html_styles() / _html_script() without importing the vendored
# package (which is not COPY'd into the container image).

# Brand palette — matches COMMUNITY_COLORS in export.py exactly
_COMMUNITY_COLORS = [
    "#22c55e",  # brand-500 (accent)
    "#4ade80",  # brand-400
    "#16a34a",  # brand-600
    "#86efb0",  # brand-300
    "#15803c",  # brand-700
    "#bbf7d4",  # brand-200
    "#166534",  # brand-800
]

# Category → community id (matches dashboard graph-data logic)
_CAT_COMMUNITY = {"preference": 0, "fact": 1, "decision": 2, "entity": 3, "other": 4}
_CAT_NAMES = {0: "Preferences", 1: "Facts", 2: "Decisions", 3: "Entities", 4: "Other"}


def _js_safe(obj) -> str:
    """JSON-encode an object and escape </script> so embedded data can't break out."""
    return json.dumps(obj).replace("</", "<\\/")


def _graph_empty_html(msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<style>body{{background:#000;color:#999;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}</style>
</head><body><p>{_html.escape(msg)}</p></body></html>"""


def _build_graph_html(memories: list, edges_data: dict, stats: dict, max_nodes: int) -> str:
    """Build the complete graphify vis.js HTML.

    Replicates to_html() from vendor/graphify/graphify/export.py exactly:
    - Same node format: id, label, color (bg/border/highlight/hover), size,
      font, title, community, community_name, source_file, file_type, degree
    - Same edge format: from, to, title, dashes, width, value, color, confidence
    - Same community legend format: cid, color, label, count
    - Same HTML skeleton: #graph canvas, #header, #sidebar with search,
      #info-panel, #legend-wrap, #stats
    - Exact CSS from _html_styles() (brand tokens, scrollbar styles)
    - Exact JS from _html_script() (vis.js network config, physics, events)
    """
    # --- Build nodes ---
    memories = memories[:max_nodes]
    memory_ids = {m["id"] for m in memories}

    # Collect entity nodes too (they appear via entity mentions)
    entity_set: dict = {}  # entity_name -> {type, memories}
    for m in memories:
        for ent in m.get("entities", []):
            ename = ent.get("name", "")
            if ename:
                if ename not in entity_set:
                    entity_set[ename] = {"type": ent.get("type", ""), "degree": 0}

    # Build edge list first (need degree counts)
    raw_edges = []
    entity_name_field = "entity"  # export_all_edges uses "entity" not "entity_name"

    for mention in edges_data.get("mentions", []):
        src = mention.get("memory_id", "")
        ent_name = mention.get("entity", mention.get("entity_name", ""))
        tgt = f"entity:{ent_name}"
        if src in memory_ids and ent_name in entity_set:
            raw_edges.append({
                "from": src, "to": tgt,
                "confidence": "EXTRACTED", "relation": "mentions",
                "weight": 0.5,
            })

    for cr in edges_data.get("co_retrieved", []):
        src = cr.get("from", cr.get("memory_id_1", ""))
        tgt = cr.get("to", cr.get("memory_id_2", ""))
        if src in memory_ids and tgt in memory_ids:
            count = cr.get("count", 1)
            raw_edges.append({
                "from": src, "to": tgt,
                "confidence": "EXTRACTED", "relation": "co_retrieved",
                "weight": min(count / 10.0, 1.0),
            })

    for rel in edges_data.get("related_to", []):
        src = rel.get("from", rel.get("memory_id_1", ""))
        tgt = rel.get("to", rel.get("memory_id_2", ""))
        if src in memory_ids and tgt in memory_ids:
            raw_edges.append({
                "from": src, "to": tgt,
                "confidence": "EXTRACTED", "relation": "related_to",
                "weight": rel.get("weight", 0.5),
            })

    # Degree map
    degree_map: dict = {}
    for e in raw_edges:
        degree_map[e["from"]] = degree_map.get(e["from"], 0) + 1
        degree_map[e["to"]] = degree_map.get(e["to"], 0) + 1

    # --- Vis nodes for memories ---
    all_node_ids = set()
    vis_nodes = []
    max_deg = max(degree_map.values(), default=1) or 1

    for m in memories:
        mid = m["id"]
        all_node_ids.add(mid)
        cat = m.get("category", "other")
        cid = _CAT_COMMUNITY.get(cat, 4)
        color = _COMMUNITY_COLORS[cid % len(_COMMUNITY_COLORS)]
        content = m.get("content", "")
        label = content[:60] + ("..." if len(content) > 60 else "")
        deg = degree_map.get(mid, 1)
        size = 10 + 30 * (deg / max_deg)
        font_size = 12 if deg >= max_deg * 0.15 else 0
        vis_nodes.append({
            "id": mid,
            "label": label,
            "color": {
                "background": "#141414",
                "border": color,
                "highlight": {"background": color, "border": "#4ade80"},
                "hover": {"background": "#1a1a1a", "border": color},
            },
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#ffffff", "face": "'Inter', system-ui, sans-serif"},
            "title": _html.escape(label),
            "community": cid,
            "community_name": _CAT_NAMES.get(cid, f"Community {cid}"),
            "source_file": "",
            "file_type": cat,
            "degree": deg,
        })

    # --- Vis nodes for entities ---
    for ename, edata in entity_set.items():
        nid = f"entity:{ename}"
        all_node_ids.add(nid)
        cid = 3  # entities always community 3
        color = _COMMUNITY_COLORS[cid % len(_COMMUNITY_COLORS)]
        deg = degree_map.get(nid, 1)
        size = 8 + 20 * (deg / max_deg)
        font_size = 11 if deg >= max_deg * 0.15 else 0
        vis_nodes.append({
            "id": nid,
            "label": ename,
            "color": {
                "background": "#141414",
                "border": color,
                "highlight": {"background": color, "border": "#4ade80"},
                "hover": {"background": "#1a1a1a", "border": color},
            },
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#ffffff", "face": "'Inter', system-ui, sans-serif"},
            "title": _html.escape(ename),
            "community": cid,
            "community_name": "Entities",
            "source_file": "",
            "file_type": edata["type"],
            "degree": deg,
        })

    # --- Vis edges ---
    vis_edges = []
    for e in raw_edges:
        confidence = e.get("confidence", "EXTRACTED")
        relation = e.get("relation", "")
        weight = e.get("weight", 1.0)
        vis_edges.append({
            "from": e["from"],
            "to": e["to"],
            "label": relation,
            "title": _html.escape(f"{relation} [{confidence}]"),
            "dashes": confidence != "EXTRACTED",
            "width": 2 if confidence == "EXTRACTED" else 1,
            "value": float(weight),
            "color": {
                "color": "rgba(64, 64, 64, 0.6)" if confidence == "EXTRACTED" else "rgba(64, 64, 64, 0.35)",
                "highlight": "#22c55e",
                "hover": "#4ade80",
                "inherit": False,
                "opacity": 0.75 if confidence == "EXTRACTED" else 0.4,
            },
            "confidence": confidence,
        })

    # --- Community legend ---
    community_counts: dict = {}
    for n in vis_nodes:
        cid = n["community"]
        community_counts[cid] = community_counts.get(cid, 0) + 1

    legend_data = []
    for cid in sorted(community_counts.keys()):
        color = _COMMUNITY_COLORS[cid % len(_COMMUNITY_COLORS)]
        lbl = _html.escape(_CAT_NAMES.get(cid, f"Community {cid}"))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": community_counts[cid]})

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)

    n_nodes = len(vis_nodes)
    n_edges = len(vis_edges)
    n_communities = len(community_counts)
    subtitle = f"{n_nodes} nodes / {n_edges} edges / {n_communities} communities"
    stats_str = f"{n_nodes} nodes &middot; {n_edges} edges &middot; {n_communities} communities"

    # HTML skeleton matches to_html() from export.py exactly
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<meta name="theme-color" content="#0a0a0a">
<title>Engram Memory Graph</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
{_graphify_styles()}
</head>
<body>
<div id="graph"></div>
<div id="header">
  <div class="title">Engram<span class="mark">.</span> Memory Graph</div>
  <div class="subtitle">{subtitle}</div>
</div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search nodes..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Node Info</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="legend-wrap">
    <h3>Communities</h3>
    <div id="legend"></div>
  </div>
  <div id="stats">{stats_str}</div>
</div>
{_graphify_script(nodes_json, edges_json, legend_json)}
</body>
</html>"""


def _graphify_styles() -> str:
    """Exact copy of _html_styles() from vendor/graphify/graphify/export.py."""
    return """<style>
  :root {
    --em-bg: #000000;
    --em-surface: #0a0a0a;
    --em-card: #141414;
    --em-text: #ffffff;
    --em-text-dim: #d4d4d4;
    --em-muted: #999999;
    --em-muted-soft: #666666;
    --em-border: #292929;
    --em-border-soft: #1f1f1f;
    --em-accent: #22c55e;
    --em-accent-soft: #4ade80;
    --em-accent-deep: #15803c;
    --em-font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    --em-font-display: 'Space Grotesk', 'Inter', system-ui, sans-serif;
    --em-font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background: var(--em-bg);
    color: var(--em-text);
    font-family: var(--em-font-sans);
    font-feature-settings: 'cv01','cv03','cv04','cv11';
    -webkit-font-smoothing: antialiased;
    display: flex;
    overflow: hidden;
  }
  #graph { flex: 1; width: 100%; height: 100vh; background: var(--em-bg); }
  #header {
    position: fixed; top: 0; left: 0; z-index: 10;
    padding: 18px 24px; pointer-events: none;
  }
  #header .title {
    font-family: var(--em-font-display);
    font-size: 18px; font-weight: 600; letter-spacing: -0.02em;
    color: var(--em-text);
  }
  #header .title .mark { color: var(--em-accent); }
  #header .subtitle {
    font-family: var(--em-font-mono);
    font-size: 11px; letter-spacing: 0.02em;
    color: var(--em-muted); margin-top: 2px;
    font-variant-numeric: tabular-nums;
  }
  #sidebar {
    width: 300px;
    background: var(--em-surface);
    border-left: 1px solid var(--em-border);
    display: flex; flex-direction: column; overflow: hidden;
  }
  #search-wrap { padding: 14px; border-bottom: 1px solid var(--em-border); }
  #search {
    width: 100%;
    background: var(--em-card);
    border: 1px solid var(--em-border);
    color: var(--em-text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
    font-family: var(--em-font-sans);
    outline: none;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  #search::placeholder { color: var(--em-muted-soft); }
  #search:focus {
    border-color: var(--em-accent);
    box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.18);
  }
  #search-results {
    max-height: 160px; overflow-y: auto;
    padding: 6px 14px 10px;
    border-bottom: 1px solid var(--em-border);
    display: none;
  }
  .search-item {
    padding: 5px 8px;
    cursor: pointer;
    border-radius: 6px;
    font-size: 12px;
    color: var(--em-text-dim);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .search-item:hover { background: var(--em-card); color: var(--em-text); }
  #info-panel { padding: 16px; border-bottom: 1px solid var(--em-border); min-height: 150px; }
  #info-panel h3 {
    font-family: var(--em-font-mono);
    font-size: 10px;
    color: var(--em-muted);
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  #info-content { font-size: 13px; color: var(--em-text-dim); line-height: 1.65; }
  #info-content .field { margin-bottom: 5px; }
  #info-content .field b { color: var(--em-text); font-weight: 600; }
  #info-content .empty { color: var(--em-muted-soft); font-style: normal; }
  .neighbor-link {
    display: block;
    padding: 4px 8px;
    margin: 2px 0;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    color: var(--em-text-dim);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    border-left: 3px solid var(--em-border);
    transition: background 0.15s ease, color 0.15s ease;
  }
  .neighbor-link:hover { background: var(--em-card); color: var(--em-text); }
  #neighbors-list { max-height: 180px; overflow-y: auto; margin-top: 6px; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 14px; }
  #legend-wrap h3 {
    font-family: var(--em-font-mono);
    font-size: 10px;
    color: var(--em-muted);
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  .legend-item {
    display: flex; align-items: center; gap: 10px;
    padding: 5px 6px;
    cursor: pointer;
    border-radius: 6px;
    font-size: 12px;
    color: var(--em-text-dim);
    transition: background 0.15s ease, color 0.15s ease;
  }
  .legend-item:hover { background: var(--em-card); color: var(--em-text); }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend-count {
    color: var(--em-muted-soft);
    font-family: var(--em-font-mono);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
  }
  #stats {
    padding: 12px 16px;
    border-top: 1px solid var(--em-border);
    font-family: var(--em-font-mono);
    font-size: 10px;
    color: var(--em-muted-soft);
    letter-spacing: 0.02em;
    font-variant-numeric: tabular-nums;
  }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #3f3f46; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #52525b; }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">"""


def _graphify_script(nodes_json: str, edges_json: str, legend_json: str) -> str:
    """Exact copy of _html_script() from vendor/graphify/graphify/export.py."""
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};

// HTML-escape helper — prevents XSS when injecting graph data into innerHTML
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Build vis datasets
const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree,
}})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({{
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
  arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
}})));

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes: nodesDS, edges: edgesDS }}, {{
  physics: {{
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{
      gravitationalConstant: -55,
      centralGravity: 0.008,
      springLength: 140,
      springConstant: 0.06,
      damping: 0.6,
      avoidOverlap: 0.85,
    }},
    stabilization: {{ iterations: 200, fit: true }},
    minVelocity: 0.5,
  }},
  interaction: {{
    hover: true,
    tooltipDelay: 120,
    hideEdgesOnDrag: true,
    navigationButtons: false,
    keyboard: false,
  }},
  configure: {{ enabled: false }},
  nodes: {{
    shape: 'dot',
    borderWidth: 2,
    borderWidthSelected: 3,
    font: {{
      face: "'Inter', system-ui, sans-serif",
      color: '#ffffff',
      size: 12,
      strokeWidth: 0,
    }},
    shadow: false,
  }},
  edges: {{
    smooth: {{ type: 'continuous', roundness: 0.2 }},
    selectionWidth: 2,
    color: {{ color: 'rgba(41, 41, 41, 0.75)', highlight: '#22c55e', hover: '#4ade80', inherit: false, opacity: 0.85 }},
    scaling: {{ min: 1, max: 4 }},
    hoverWidth: 1.2,
  }},
}});

network.once('stabilizationIterationsDone', () => {{
  network.setOptions({{ physics: {{ enabled: false }} }});
}});

function showInfo(nodeId) {{
  const n = nodesDS.get(nodeId);
  if (!n) return;
  const neighborIds = network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.map(nid => {{
    const nb = nodesDS.get(nid);
    const color = nb && nb.color && nb.color.border ? nb.color.border : '#22c55e';
    return `<span class="neighbor-link" style="border-left-color:${{esc(color)}}" onclick="focusNode(${{JSON.stringify(nid)}})">${{esc(nb ? nb.label : nid)}}</span>`;
  }}).join('');
  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${{esc(n.label)}}</b></div>
    <div class="field">Type: ${{esc(n._file_type || 'unknown')}}</div>
    <div class="field">Community: ${{esc(n._community_name)}}</div>
    <div class="field">Source: ${{esc(n._source_file || '-')}}</div>
    <div class="field">Degree: ${{n._degree}}</div>
    ${{neighborIds.length ? `<div class="field" style="margin-top:8px;color:#aaa;font-size:11px">Neighbors (${{neighborIds.length}})</div><div id="neighbors-list">${{neighborItems}}</div>` : ''}}
  `;
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.4, animation: true }});
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}}

// Track hovered node — hover detection is more reliable than click params
let hoveredNodeId = null;
network.on('hoverNode', params => {{
  hoveredNodeId = params.node;
  container.style.cursor = 'pointer';
}});
network.on('blurNode', () => {{
  hoveredNodeId = null;
  container.style.cursor = 'default';
}});
container.addEventListener('click', () => {{
  if (hoveredNodeId !== null) {{
    showInfo(hoveredNodeId);
    network.selectNodes([hoveredNodeId]);
  }}
}});
network.on('click', params => {{
  if (params.nodes.length > 0) {{
    showInfo(params.nodes[0]);
  }} else if (hoveredNodeId === null) {{
    document.getElementById('info-content').innerHTML = '<span class="empty">Click a node to inspect it</span>';
  }}
}});

const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.toLowerCase().trim();
  searchResults.innerHTML = '';
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const matches = RAW_NODES.filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
  if (!matches.length) {{ searchResults.style.display = 'none'; return; }}
  searchResults.style.display = 'block';
  matches.forEach(n => {{
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label;
    el.style.borderLeft = `3px solid ${{n.color && n.color.border ? n.color.border : '#22c55e'}}`;
    el.style.paddingLeft = '8px';
    el.onclick = () => {{
      network.focus(n.id, {{ scale: 1.5, animation: true }});
      network.selectNodes([n.id]);
      showInfo(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    }};
    searchResults.appendChild(el);
  }});
}});
document.addEventListener('click', e => {{
  if (!searchResults.contains(e.target) && e.target !== searchInput)
    searchResults.style.display = 'none';
}});

const hiddenCommunities = new Set();
const legendEl = document.getElementById('legend');
LEGEND.forEach(c => {{
  const item = document.createElement('div');
  item.className = 'legend-item';
  item.innerHTML = `<div class="legend-dot" style="background:${{c.color}}"></div>
    <span class="legend-label">${{c.label}}</span>
    <span class="legend-count">${{c.count}}</span>`;
  item.onclick = () => {{
    if (hiddenCommunities.has(c.cid)) {{
      hiddenCommunities.delete(c.cid);
      item.classList.remove('dimmed');
    }} else {{
      hiddenCommunities.add(c.cid);
      item.classList.add('dimmed');
    }}
    const updates = RAW_NODES
      .filter(n => n.community === c.cid)
      .map(n => ({{ id: n.id, hidden: hiddenCommunities.has(c.cid) }}));
    nodesDS.update(updates);
  }};
  legendEl.appendChild(item);
}});
</script>"""


# ─── Dashboard HTML ───────────────────────────────────────────────────────────

def get_dashboard_html():
    """Return the complete dashboard SPA as an HTML string."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>engram. memory dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
:root {
  --em-bg: #000000;
  --em-surface: #0a0a0a;
  --em-card: #141414;
  --em-text: #ffffff;
  --em-muted: #999999;
  --em-border: #292929;
  --em-accent: #22c55e;
  --em-font-sans: 'Inter', system-ui, sans-serif;
  --em-font-display: 'Space Grotesk', sans-serif;
  --em-font-mono: 'JetBrains Mono', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; background: var(--em-bg); color: var(--em-text); font-family: var(--em-font-sans); font-size: 14px; line-height: 1.5; }

#header { background: var(--em-surface); border-bottom: 1px solid var(--em-border); padding: 16px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.logo { font-family: var(--em-font-display); font-size: 24px; font-weight: 600; }
.logo .dot { color: var(--em-accent); }
.subtitle { color: var(--em-muted); font-size: 14px; }
#stats-bar { margin-left: auto; display: flex; gap: 20px; font-family: var(--em-font-mono); font-size: 12px; color: var(--em-muted); }
#stats-bar .stat-val { color: var(--em-text); font-weight: 500; }

#tabs { background: var(--em-surface); border-bottom: 1px solid var(--em-border); padding: 0 24px; display: flex; gap: 0; }
#tabs button { background: none; border: none; border-bottom: 2px solid transparent; color: var(--em-muted); font-family: var(--em-font-sans); font-size: 13px; font-weight: 500; padding: 10px 16px; cursor: pointer; transition: all 0.2s; }
#tabs button:hover { color: var(--em-text); }
#tabs button.active { color: var(--em-accent); border-bottom-color: var(--em-accent); }

main { height: calc(100vh - 110px); overflow: hidden; }
section { display: none; height: 100%; overflow-y: auto; padding: 24px; }
section.active { display: block; }

#search-bar { margin-bottom: 16px; }
#search-input { width: 100%; max-width: 480px; background: var(--em-card); border: 1px solid var(--em-border); border-radius: 6px; padding: 10px 14px; color: var(--em-text); font-family: var(--em-font-sans); font-size: 14px; outline: none; transition: border-color 0.2s; }
#search-input:focus { border-color: var(--em-accent); }
#search-input::placeholder { color: var(--em-muted); }

#category-filters { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.cat-btn { background: var(--em-card); border: 1px solid var(--em-border); border-radius: 4px; padding: 4px 12px; color: var(--em-muted); font-size: 12px; cursor: pointer; transition: all 0.2s; }
.cat-btn:hover, .cat-btn.active { color: var(--em-text); border-color: var(--em-accent); }

#memory-list { display: flex; flex-direction: column; gap: 8px; }

.memory-card { background: var(--em-card); border: 1px solid var(--em-border); border-radius: 6px; padding: 14px 16px; cursor: pointer; transition: border-color 0.2s; }
.memory-card:hover { border-color: var(--em-accent); }
.card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.category-badge { font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 3px; text-transform: uppercase; letter-spacing: 0.5px; }
.badge-preference { background: rgba(34,197,94,0.15); color: #22c55e; }
.badge-fact { background: rgba(59,130,246,0.15); color: #3b82f6; }
.badge-decision { background: rgba(245,158,11,0.15); color: #f59e0b; }
.badge-entity { background: rgba(139,92,246,0.15); color: #8b5cf6; }
.badge-other { background: rgba(107,114,128,0.15); color: #6b7280; }
.card-time { margin-left: auto; font-family: var(--em-font-mono); font-size: 11px; color: var(--em-muted); }
.card-content { color: var(--em-text); font-size: 13px; line-height: 1.6; word-break: break-word; }
.card-expanded { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--em-border); font-size: 12px; color: var(--em-muted); display: none; }
.card-expanded.show { display: block; }
.card-meta-row { display: flex; gap: 16px; flex-wrap: wrap; }
.card-meta-row span { font-family: var(--em-font-mono); }


#stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.stat-card { background: var(--em-card); border: 1px solid var(--em-border); border-radius: 6px; padding: 20px; }
.stat-card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--em-muted); margin-bottom: 12px; }
.stat-card .big-num { font-family: var(--em-font-mono); font-size: 32px; font-weight: 600; color: var(--em-accent); }
.cat-bar { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
.cat-bar-label { width: 80px; font-size: 12px; color: var(--em-muted); }
.cat-bar-fill { height: 6px; border-radius: 3px; transition: width 0.3s; }
.cat-bar-count { font-family: var(--em-font-mono); font-size: 11px; color: var(--em-muted); min-width: 30px; }

.empty-state { text-align: center; padding: 60px 24px; color: var(--em-muted); }
.empty-state h2 { font-family: var(--em-font-display); font-size: 20px; color: var(--em-text); margin-bottom: 8px; }

#loading { text-align: center; padding: 40px; color: var(--em-muted); }

@media (max-width: 768px) {
  #header { padding: 12px 16px; }
  #stats-bar { display: none; }
  section { padding: 16px; }
  #stats-grid { grid-template-columns: 1fr 1fr; }
}
</style>
</head>
<body>
<header id="header">
  <div class="logo">engram<span class="dot">.</span></div>
  <div class="subtitle">memory dashboard</div>
  <div id="stats-bar"></div>
</header>
<nav id="tabs">
  <button data-tab="timeline" class="active">Timeline</button>
  <button data-tab="graph">Graph</button>
  <button data-tab="stats">Stats</button>
</nav>
<main>
  <section id="tab-timeline" class="active">
    <div id="search-bar"><input type="text" id="search-input" placeholder="Search memories..."></div>
    <div id="category-filters">
      <button class="cat-btn active" data-cat="">All</button>
      <button class="cat-btn" data-cat="preference">Preferences</button>
      <button class="cat-btn" data-cat="fact">Facts</button>
      <button class="cat-btn" data-cat="decision">Decisions</button>
      <button class="cat-btn" data-cat="entity">Entities</button>
      <button class="cat-btn" data-cat="other">Other</button>
    </div>
    <div id="memory-list"><div id="loading">Loading memories...</div></div>
  </section>
  <section id="tab-graph" style="padding:0;overflow:hidden">
    <iframe id="graph-frame" src="/dashboard/graph-html" style="width:100%;height:calc(100vh - 110px);border:none;border-radius:0;display:block"></iframe>
  </section>
  <section id="tab-stats">
    <div id="stats-grid"></div>
  </section>
</main>
<script>
const CAT_COLORS = {preference:"#22c55e",fact:"#3b82f6",decision:"#f59e0b",entity:"#8b5cf6",other:"#6b7280"};
let currentCat = "";
let searchTimeout = null;

// Tab switching
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll("main section").forEach(s => s.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "stats") loadStats();
  });
});

// Category filters
document.querySelectorAll(".cat-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentCat = btn.dataset.cat;
    loadTimeline();
  });
});

// Search
document.getElementById("search-input").addEventListener("input", e => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    const q = e.target.value.trim();
    if (q.length > 2) searchMemories(q);
    else loadTimeline();
  }, 300);
});

function relTime(ts) {
  const d = (Date.now()/1000 - ts);
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d/60) + "m ago";
  if (d < 86400) return Math.floor(d/3600) + "h ago";
  return Math.floor(d/86400) + "d ago";
}

function renderCards(memories) {
  const list = document.getElementById("memory-list");
  if (!memories.length) {
    list.innerHTML = '<div class="empty-state"><h2>No memories yet</h2><p>Memories will appear here as they are stored via engram MCP tools.</p></div>';
    return;
  }
  list.innerHTML = memories.map(m => {
    const preview = m.content ? (m.content.length > 200 ? m.content.slice(0,200)+"..." : m.content) : "";
    const full = m.content || "";
    const cat = m.category || "other";
    return '<article class="memory-card" onclick="this.querySelector(\\'.card-expanded\\').classList.toggle(\\'show\\')">' +
      '<div class="card-header">' +
        '<span class="category-badge badge-'+cat+'">'+cat+'</span>' +
        (m.private ? '<span style="color:#f59e0b;font-size:11px">private</span>' : '') +
        '<span class="card-time">'+relTime(m.created_at)+'</span>' +
      '</div>' +
      '<p class="card-content">'+escHtml(preview)+'</p>' +
      '<div class="card-expanded">' +
        '<p style="color:var(--em-text);margin-bottom:8px">'+escHtml(full)+'</p>' +
        '<div class="card-meta-row">' +
          '<span>ID: '+(m.id||m.doc_id||"").slice(0,8)+'</span>' +
          '<span>Accessed: '+(m.access_count||0)+'x</span>' +
        '</div>' +
      '</div>' +
    '</article>';
  }).join("");
}

function escHtml(s) { const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }

async function loadTimeline() {
  try {
    const params = new URLSearchParams({limit:"50"});
    if (currentCat) params.set("category", currentCat);
    const r = await fetch("/dashboard/timeline?" + params);
    const d = await r.json();
    renderCards(d.memories || []);
  } catch(e) {
    document.getElementById("memory-list").innerHTML = '<div class="empty-state"><h2>Connection error</h2><p>Could not reach the engram server.</p></div>';
  }
}

async function searchMemories(query) {
  try {
    const r = await fetch("/search", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({query, limit:20})});
    const d = await r.json();
    renderCards((d.results||[]).map(m => ({...m, id: m.doc_id})));
  } catch(e) { console.error(e); }
}

async function loadStats() {
  try {
    const r = await fetch("/dashboard/stats");
    const d = await r.json();
    const grid = document.getElementById("stats-grid");
    const maxCat = Math.max(...Object.values(d.categories||{}), 1);
    grid.innerHTML =
      '<div class="stat-card"><h3>Total Memories</h3><div class="big-num">'+(d.total_memories||0)+'</div></div>' +
      '<div class="stat-card"><h3>Categories</h3>' +
        Object.entries(d.categories||{}).map(([k,v]) =>
          '<div class="cat-bar"><span class="cat-bar-label">'+k+'</span>' +
          '<div style="flex:1;background:var(--em-border);border-radius:3px;overflow:hidden">' +
          '<div class="cat-bar-fill" style="width:'+Math.round(v/maxCat*100)+'%;background:'+(CAT_COLORS[k]||"#6b7280")+'"></div></div>' +
          '<span class="cat-bar-count">'+v+'</span></div>'
        ).join("") +
      '</div>' +
      '<div class="stat-card"><h3>Graph</h3>' +
        Object.entries(d.graph||{}).map(([k,v]) =>
          '<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px"><span style="color:var(--em-muted)">'+k.replace(/_/g," ")+'</span><span style="font-family:var(--em-font-mono)">'+v+'</span></div>'
        ).join("") +
      '</div>' +
      '<div class="stat-card"><h3>Hot Tier</h3><div class="big-num">'+(d.tiers?.hot||0)+'</div><div style="color:var(--em-muted);font-size:12px;margin-top:4px">cached in memory</div></div>';

    // Update stats bar
    document.getElementById("stats-bar").innerHTML =
      '<span><span class="stat-val">'+(d.total_memories||0)+'</span> memories</span>' +
      '<span><span class="stat-val">'+(d.tiers?.hot||0)+'</span> hot</span>';
  } catch(e) { console.error(e); }
}

// Initial load
loadTimeline();
loadStats();

// Auto-refresh every 30s
setInterval(loadTimeline, 30000);
</script>
</body>
</html>'''
