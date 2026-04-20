"""Engram Memory — Web Dashboard

Single-page dashboard served from the MCP server at /dashboard.
Queries existing REST endpoints for data. No external dependencies.
"""

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

        # Build nodes (memories + entities)
        nodes = []
        memory_ids = set()
        for m in memories[:max_nodes]:
            mid = m.get("id", "")
            memory_ids.add(mid)
            content = m.get("content", "")
            nodes.append({
                "id": mid,
                "label": content[:60] + ("..." if len(content) > 60 else ""),
                "type": "memory",
                "category": m.get("category", "other"),
                "created_at": m.get("created_at", 0),
            })

            for ent in m.get("entities", []):
                ent_id = f"entity:{ent.get('name', '')}"
                if not any(n["id"] == ent_id for n in nodes):
                    nodes.append({
                        "id": ent_id,
                        "label": ent.get("name", ""),
                        "type": "entity",
                        "entity_type": ent.get("type", ""),
                    })

        # Build edges
        edges = []
        for mention in edges_data.get("mentions", []):
            src = mention.get("memory_id", "")
            tgt = f"entity:{mention.get('entity_name', '')}"
            if src in memory_ids:
                edges.append({"from": src, "to": tgt, "type": "mentions"})

        for cr in edges_data.get("co_retrieved", []):
            src = cr.get("memory_id_1", "")
            tgt = cr.get("memory_id_2", "")
            if src in memory_ids and tgt in memory_ids:
                edges.append({
                    "from": src, "to": tgt, "type": "co_retrieved",
                    "count": cr.get("count", 1),
                })

        for rel in edges_data.get("related_to", []):
            src = rel.get("memory_id_1", "")
            tgt = rel.get("memory_id_2", "")
            if src in memory_ids and tgt in memory_ids:
                edges.append({
                    "from": src, "to": tgt, "type": "related_to",
                    "weight": rel.get("weight", 0.5),
                })

        return JSONResponse({"nodes": nodes, "edges": edges, "stats": stats})

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
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
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

#graph-container { width: 100%; height: calc(100vh - 160px); border-radius: 6px; border: 1px solid var(--em-border); }

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
  <section id="tab-graph">
    <div id="graph-container"></div>
  </section>
  <section id="tab-stats">
    <div id="stats-grid"></div>
  </section>
</main>
<script>
const CAT_COLORS = {preference:"#22c55e",fact:"#3b82f6",decision:"#f59e0b",entity:"#8b5cf6",other:"#6b7280"};
let currentCat = "";
let searchTimeout = null;
let graphLoaded = false;

// Tab switching
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll("main section").forEach(s => s.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "graph" && !graphLoaded) loadGraph();
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

async function loadGraph() {
  graphLoaded = true;
  const container = document.getElementById("graph-container");
  container.innerHTML = '<div id="loading">Loading graph...</div>';
  try {
    const r = await fetch("/dashboard/graph-data");
    const d = await r.json();
    if (!d.nodes || !d.nodes.length) {
      container.innerHTML = '<div class="empty-state"><h2>No graph data</h2><p>Graph connections will appear as memories are linked.</p></div>';
      return;
    }
    const nodes = new vis.DataSet(d.nodes.map(n => ({
      id: n.id,
      label: n.label,
      shape: n.type==="entity" ? "diamond" : "dot",
      size: n.type==="entity" ? 8 : 14,
      color: {
        background: n.type==="entity" ? "#0a0a0a" : "#141414",
        border: n.type==="entity" ? "#4ade80" : (CAT_COLORS[n.category]||"#292929"),
        highlight: {background: "#22c55e", border: "#22c55e"},
      },
      font: {color:"#ffffff", face:"Inter", size:11},
      borderWidth: 2,
    })));
    const edges = new vis.DataSet(d.edges.map((e,i) => ({
      id: i,
      from: e.from,
      to: e.to,
      color: {color:"rgba(64,64,64,0.6)", highlight:"#22c55e"},
      width: e.type==="co_retrieved" ? Math.min(e.count||1, 4) : (e.weight ? e.weight*3 : 1),
      dashes: e.type==="co_retrieved",
      smooth: {type:"continuous"},
    })));
    container.innerHTML = "";
    new vis.Network(container, {nodes, edges}, {
      physics: {solver:"forceAtlas2Based", forceAtlas2Based:{gravitationalConstant:-30, centralGravity:0.005, springLength:120, damping:0.6, avoidOverlap:0.85}, stabilization:{iterations:200}},
      interaction: {hover:true, tooltipDelay:200},
    });
  } catch(e) {
    container.innerHTML = '<div class="empty-state"><h2>Error loading graph</h2><p>'+e.message+'</p></div>';
  }
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
