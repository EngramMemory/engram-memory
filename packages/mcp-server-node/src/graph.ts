/**
 * Pure-TS renderer for `memory_graph` MCP tool.
 *
 * Takes a host-LLM-produced { nodes, edges } spec and writes a
 * self-contained interactive HTML page (vis.js loaded from CDN).
 *
 * No Python, no native deps — keeps the npm package portable.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export interface GraphNodeInput {
  id: string;
  label: string;
  category?: string;
  content?: string;
  entities?: string[];
}

export interface GraphEdgeInput {
  source: string;
  target: string;
  type?: string;
  label?: string;
  weight?: number;
}

export interface GraphSpec {
  nodes: GraphNodeInput[];
  edges: GraphEdgeInput[];
  title?: string;
}

export interface RenderResult {
  htmlPath: string;
  nodes: number;
  edges: number;
  communities: number;
}

const CATEGORY_COLORS: Record<string, string> = {
  preference: "#4f8cff",
  decision: "#ff7a59",
  fact: "#6fcf97",
  entity: "#bb6bd9",
  other: "#9ba3af",
  general: "#9ba3af",
};

function pickColor(category: string | undefined): string {
  const key = (category || "other").toLowerCase();
  return CATEGORY_COLORS[key] || stringColor(key);
}

function stringColor(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  return `hsl(${hue}, 55%, 60%)`;
}

function trim(text: string, limit: number): string {
  const t = (text || "").trim().replace(/\s+/g, " ");
  if (t.length <= limit) return t;
  return t.slice(0, Math.max(0, limit - 1)).trimEnd() + "…";
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Normalise + validate. Drops edges that point at unknown nodes. */
function normalise(spec: GraphSpec): {
  nodes: Array<Required<Pick<GraphNodeInput, "id" | "label">> & {
    category: string;
    content: string;
    entities: string[];
  }>;
  edges: Array<{
    source: string;
    target: string;
    relation: string;
    label: string;
    weight: number;
  }>;
} {
  if (!spec || !Array.isArray(spec.nodes)) {
    throw new Error("graph spec must have a 'nodes' array");
  }
  const seen = new Set<string>();
  const nodes = [] as ReturnType<typeof normalise>["nodes"];
  for (let i = 0; i < spec.nodes.length; i++) {
    const n = spec.nodes[i];
    if (!n || typeof n !== "object") {
      throw new Error(`node ${i} is not an object`);
    }
    if (!n.id || !n.label) {
      throw new Error(`node ${i} missing required 'id' or 'label'`);
    }
    const id = String(n.id);
    if (seen.has(id)) continue;
    seen.add(id);
    nodes.push({
      id,
      label: trim(String(n.label), 60),
      category: (n.category || "other").toString(),
      content: trim(String(n.content || ""), 400),
      entities: Array.isArray(n.entities)
        ? n.entities.map((e) => String(e)).filter(Boolean)
        : [],
    });
  }

  const edgesIn = Array.isArray(spec.edges) ? spec.edges : [];
  const edges = [] as ReturnType<typeof normalise>["edges"];
  for (let i = 0; i < edgesIn.length; i++) {
    const e = edgesIn[i];
    if (!e || typeof e !== "object") continue;
    const src = e.source ? String(e.source) : "";
    const tgt = e.target ? String(e.target) : "";
    if (!src || !tgt || !seen.has(src) || !seen.has(tgt)) continue;
    const weight = typeof e.weight === "number" && Number.isFinite(e.weight)
      ? Math.max(0, Math.min(1, e.weight))
      : 0.5;
    edges.push({
      source: src,
      target: tgt,
      relation: String(e.type || "related"),
      label: e.label ? String(e.label) : "",
      weight,
    });
  }
  return { nodes, edges };
}

/** Union-find community detection over the edge set. */
function communities(
  nodes: ReadonlyArray<{ id: string }>,
  edges: ReadonlyArray<{ source: string; target: string; weight: number }>,
  weightCutoff = 0.3,
): Map<string, number> {
  const parent = new Map<string, string>();
  for (const n of nodes) parent.set(n.id, n.id);
  const find = (x: string): string => {
    let cur = x;
    while (parent.get(cur) !== cur) {
      const p = parent.get(cur)!;
      parent.set(cur, parent.get(p)!);
      cur = parent.get(cur)!;
    }
    return cur;
  };
  const union = (a: string, b: string) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent.set(ra, rb);
  };
  for (const e of edges) {
    if (e.weight >= weightCutoff) union(e.source, e.target);
  }
  const labels = new Map<string, number>();
  const idx = new Map<string, number>();
  let next = 0;
  for (const n of nodes) {
    const root = find(n.id);
    let i = idx.get(root);
    if (i === undefined) {
      i = next++;
      idx.set(root, i);
    }
    labels.set(n.id, i);
  }
  return labels;
}

function buildHtml(
  data: ReturnType<typeof normalise>,
  title: string,
  comms: Map<string, number>,
): string {
  const visNodes = data.nodes.map((n) => ({
    id: n.id,
    label: n.label,
    title: buildTooltip(n),
    color: pickColor(n.category),
    group: comms.get(n.id) ?? 0,
    _category: n.category,
    _content: n.content,
    _entities: n.entities,
  }));
  const visEdges = data.edges.map((e, i) => ({
    id: `e${i}`,
    from: e.source,
    to: e.target,
    label: e.label || undefined,
    title: e.relation,
    value: e.weight,
    color: { color: "rgba(150,150,150,0.35)", highlight: "#444" },
  }));
  const safeTitle = escapeHtml(title);
  const dataJson = JSON.stringify({ nodes: visNodes, edges: visEdges });
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>${safeTitle}</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  html, body { margin: 0; height: 100%; background: #0f1115; color: #e6e6e6; font: 14px -apple-system, system-ui, sans-serif; }
  #network { position: absolute; inset: 0; }
  #legend { position: absolute; top: 12px; left: 12px; background: rgba(20,22,28,0.85); padding: 10px 12px; border-radius: 8px; line-height: 1.6; max-width: 240px; }
  #legend h1 { font-size: 13px; margin: 0 0 6px 0; color: #fff; font-weight: 600; }
  #legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  #stats { position: absolute; bottom: 12px; left: 12px; background: rgba(20,22,28,0.85); padding: 6px 10px; border-radius: 6px; font-size: 12px; opacity: 0.8; }
  #detail { position: absolute; top: 12px; right: 12px; max-width: 360px; background: rgba(20,22,28,0.92); padding: 12px 14px; border-radius: 8px; display: none; font-size: 13px; line-height: 1.5; }
  #detail h2 { margin: 0 0 6px 0; font-size: 14px; color: #fff; }
  #detail .cat { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-bottom: 8px; }
  #detail .entities { margin-top: 8px; color: #9ca3af; font-size: 12px; }
</style>
</head>
<body>
<div id="network"></div>
<div id="legend"><h1>${safeTitle}</h1><div id="legend-body"></div></div>
<div id="stats"></div>
<div id="detail"></div>
<script>
const DATA = ${dataJson};
const COLORS = ${JSON.stringify(CATEGORY_COLORS)};
const container = document.getElementById('network');
const nodes = new vis.DataSet(DATA.nodes);
const edges = new vis.DataSet(DATA.edges);
const network = new vis.Network(container, { nodes, edges }, {
  nodes: { shape: 'dot', size: 14, font: { color: '#e6e6e6', size: 12, face: 'system-ui' }, borderWidth: 0 },
  edges: { smooth: { type: 'continuous' }, scaling: { min: 1, max: 6 } },
  physics: { solver: 'forceAtlas2Based', forceAtlas2Based: { gravitationalConstant: -45, springLength: 90 }, stabilization: { iterations: 250 } },
  interaction: { hover: true, tooltipDelay: 120 },
});
const cats = {};
for (const n of DATA.nodes) cats[n._category || 'other'] = (cats[n._category || 'other'] || 0) + 1;
const legendBody = document.getElementById('legend-body');
for (const [cat, count] of Object.entries(cats).sort((a,b) => b[1]-a[1])) {
  const swatchColor = COLORS[cat.toLowerCase()] || '#9ba3af';
  const row = document.createElement('div');
  row.innerHTML = '<span class="swatch" style="background:' + swatchColor + '"></span>' + cat + ' <span style="opacity:.6">(' + count + ')</span>';
  legendBody.appendChild(row);
}
document.getElementById('stats').textContent = DATA.nodes.length + ' memories • ' + DATA.edges.length + ' connections';
const detail = document.getElementById('detail');
network.on('selectNode', (params) => {
  const id = params.nodes[0];
  const node = DATA.nodes.find((n) => n.id === id);
  if (!node) { detail.style.display = 'none'; return; }
  const cat = node._category || 'other';
  const color = COLORS[cat.toLowerCase()] || '#9ba3af';
  detail.innerHTML =
    '<h2>' + escape(node.label) + '</h2>' +
    '<div class="cat" style="background:' + color + '33;color:' + color + '">' + escape(cat) + '</div>' +
    (node._content ? '<div>' + escape(node._content) + '</div>' : '') +
    (node._entities && node._entities.length ? '<div class="entities">entities: ' + node._entities.map(escape).join(', ') + '</div>' : '');
  detail.style.display = 'block';
});
network.on('deselectNode', () => { detail.style.display = 'none'; });
function escape(s) { return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
</script>
</body>
</html>`;
}

function buildTooltip(n: {
  label: string;
  category: string;
  content: string;
  entities: string[];
}): string {
  const lines = [n.label];
  if (n.category) lines.push(`[${n.category}]`);
  if (n.content) lines.push(n.content);
  if (n.entities.length) lines.push(`entities: ${n.entities.join(", ")}`);
  return lines.join("\n");
}

/**
 * Render a graph spec to a self-contained HTML file.
 *
 * @param spec   nodes + edges (from the host LLM)
 * @param outDir destination directory (defaults to ~/.engram/graph-<ts>)
 */
export function renderGraph(spec: GraphSpec, outDir?: string): RenderResult {
  const data = normalise(spec);
  if (data.nodes.length === 0) {
    throw new Error("graph has no nodes — nothing to render");
  }
  const comms = communities(data.nodes, data.edges);
  const title = spec.title || "Engram Memory Graph";
  const html = buildHtml(data, title, comms);

  const dest = outDir
    ? outDir
    : join(
        homedir(),
        ".engram",
        `graph-${new Date().toISOString().replace(/[:.]/g, "-")}`,
      );
  const htmlPath = join(dest, "graph.html");
  mkdirSync(dirname(htmlPath), { recursive: true });
  writeFileSync(htmlPath, html, "utf8");

  const uniqueComms = new Set(comms.values()).size;
  return {
    htmlPath,
    nodes: data.nodes.length,
    edges: data.edges.length,
    communities: uniqueComms,
  };
}
