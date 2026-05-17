/**
 * Engram Memory Community Edition — OpenClaw Plugin Entry Point
 *
 * Provides persistent semantic memory via Qdrant + FastEmbed.
 * Hooks into OpenClaw's agent lifecycle for auto-recall and auto-capture.
 */

import { v4 as uuidv4 } from "uuid";

// ─── Types ──────────────────────────────────────────────────────────

interface PluginConfig {
  qdrantUrl: string;
  embeddingUrl: string;
  embeddingModel: string;
  embeddingDimension: number;
  autoRecall: boolean;
  autoCapture: boolean;
  maxRecallResults: number;
  minRecallScore: number;
  debug: boolean;
}

interface Memory {
  id: string;
  text: string;
  category: string;
  importance: number;
  timestamp: string;
  tags: string[];
  score?: number;
}

interface QdrantPoint {
  id: string;
  vector: number[];
  payload: Record<string, unknown>;
}

interface QdrantSearchResult {
  id: string;
  score: number;
  payload: Record<string, unknown>;
}

// ─── Defaults ───────────────────────────────────────────────────────

const DEFAULT_CONFIG: PluginConfig = {
  qdrantUrl: "http://localhost:6333",
  embeddingUrl: "http://localhost:11435",
  embeddingModel: "nomic-ai/nomic-embed-text-v1.5",
  embeddingDimension: 768,
  autoRecall: true,
  autoCapture: true,
  maxRecallResults: 5,
  minRecallScore: 0.35,
  debug: false,
};

// Short messages that don't warrant memory operations
const SKIP_PATTERNS = /^(hi|hey|hello|ok|okay|thanks|ty|yes|no|sure|yep|nah|k|lol|haha|hmm|huh|bye|gn|gm|yo|sup)[\s!?.]*$/i;

// Category detection patterns
const CATEGORY_PATTERNS: Record<string, RegExp> = {
  decision:     /\b(decided|chosen|selected|will use|going with|switched to|moved to|picking|opted for|committed to|approved|rejected|went with|agreed to|adopted|deprecated|migrated to)\b/i,
  preference:   /\b(prefer|like|always|never|want|love|hate|enjoy|favor|rather|choose to|tend to|usually|my favorite|i always|strongly preferred|mandatory)\b/i,
  goal:         /\b(goal|objective|target|aim|milestone|want to achieve|trying to|working toward|by end of|need to finish|deadline|ship by|launch|roadmap|OKR|KPI)\b/i,
  plan:         /\b(plan|strategy|approach|roadmap|next step|will do|going to|schedule|sprint|phase|rollout|timeline|proposal)\b/i,
  error:        /\b(error|bug|issue|broke|failed|crash|exception|stack trace|wrong|incorrect|broken|regression|incident|outage|root cause|fix needed)\b/i,
  insight:      /\b(realized|noticed|discovered|learned|found that|turns out|key insight|pattern|interesting|observation|in retrospect|lesson|takeaway|conclusion)\b/i,
  skill:        /\b(knows how to|can|expert in|proficient|familiar with|experienced with|trained in|certified|specializes in|good at|fluent in|mastered)\b/i,
  event:        /\b(meeting|call|demo|launch|release|deploy|shipped|presentation|interview|review|ceremony|standup|retrospective|happened|occurred|took place|scheduled)\b/i,
  question:     /\b(why|how|what|when|where|who|wondering|unclear|need to know|to be determined|TBD|open question|investigate|figure out)\b/i,
  relationship: /\b(reports to|manages|works with|partner|collaborator|stakeholder|owns|responsible for|depends on|integrated with|blocked by|supports|mentors)\b/i,
  fact:         /\b(completed|version|status|count|running|deployed|migrated|installed|updated|currently|uses|located|configured|set to|costs|port|hosted|stores|contains|supports)\b/i,
  entity:       /\b(company|team|person|project|service|app|platform|organization|department|manager|lead|owner|maintainer|vendor|client|VP|CTO|CEO|engineer)\b/i,
};

// ─── Credential Patterns (never store these) ────────────────────────

const CREDENTIAL_PATTERNS = [
  /\b(sk-[a-zA-Z0-9]{20,})\b/,                    // OpenAI keys
  /\b(sk_live_[a-zA-Z0-9]{20,})\b/,               // Stripe live keys
  /\b(sk_test_[a-zA-Z0-9]{20,})\b/,               // Stripe test keys
  /\b(ghp_[a-zA-Z0-9]{36,})\b/,                   // GitHub PATs
  /\b(gho_[a-zA-Z0-9]{36,})\b/,                   // GitHub OAuth
  /\b(glpat-[a-zA-Z0-9-]{20,})\b/,                // GitLab PATs
  /\b(xoxb-[a-zA-Z0-9-]{20,})\b/,                 // Slack bot tokens
  /\b(xoxp-[a-zA-Z0-9-]{20,})\b/,                 // Slack user tokens
  /\b(Bearer\s+[a-zA-Z0-9._-]{20,})\b/,           // Bearer tokens
  /\b(eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})/,// JWT tokens
  /\b(AKIA[a-zA-Z0-9]{16})\b/,                    // AWS access keys
  /\b(pk_live_[a-zA-Z0-9]{20,})\b/,               // Stripe publishable
  /\b(whsec_[a-zA-Z0-9]{20,})\b/,                 // Webhook secrets
  /\b(eng_[a-zA-Z0-9_-]{20,})\b/,                 // Engram API keys
  /\b(m0-[a-zA-Z0-9]{20,})\b/,                    // Mem0 API keys
  /\b(sm_[a-zA-Z0-9]{20,})\b/,                    // Supermemory keys
  /\bpassword\s*[:=]\s*["']?[^\s"']{6,}/i,        // password = xxx
  /\b(ssh-rsa|ssh-ed25519)\s+[A-Za-z0-9+/=]{20,}/,// SSH keys
];

// ─── Noise Filtering ────────────────────────────────────────────────

const GENERIC_RESPONSE_PATTERNS = [
  /^(sure|of course|absolutely|no problem|you're welcome|happy to help)/i,
  /^(here'?s? (what|how|the))/i,
  /^(let me|i'?ll|i can|i will)/i,
  /^(based on|according to|as (you|we|i) (mentioned|discussed))/i,
];

function containsCredential(text: string): boolean {
  return CREDENTIAL_PATTERNS.some((pattern) => pattern.test(text));
}

function isNoise(text: string): boolean {
  if (text.length < 15) return true;
  if (text.length > 500) return true;
  if (SKIP_PATTERNS.test(text)) return true;
  if (GENERIC_RESPONSE_PATTERNS.some((p) => p.test(text))) return true;
  if (/^```[\s\S]*```$/.test(text.trim())) return true; // pure code blocks
  if ((text.match(/\n/g) || []).length > 10) return true; // too many lines
  return false;
}

function scrubCredentials(text: string): string {
  let scrubbed = text;
  for (const pattern of CREDENTIAL_PATTERNS) {
    scrubbed = scrubbed.replace(pattern, "[REDACTED]");
  }
  return scrubbed;
}

// ─── Plugin Class ───────────────────────────────────────────────────

class EngramMemoryPlugin {
  private config: PluginConfig;
  private messageCount = 0;
  private servicesAvailable = true;
  private readonly COLLECTION_NAME = "agent-memory";

  constructor(userConfig: Partial<PluginConfig> = {}) {
    this.config = { ...DEFAULT_CONFIG, ...userConfig };
    console.log("[engram-memory] Engram Community Edition initialized");
  }

  private log(...args: unknown[]) {
    if (this.config.debug) {
      console.log("[engram-memory]", ...args);
    }
  }

  // ─── Service Health Check ───────────────────────────────────────

  private async checkServices(): Promise<boolean> {
    try {
      const [embedRes, qdrantRes] = await Promise.all([
        fetch(`${this.config.embeddingUrl}/health`, { signal: AbortSignal.timeout(3000) }).catch(() => null),
        fetch(`${this.config.qdrantUrl}/health`, { signal: AbortSignal.timeout(3000) }).catch(() => null),
      ]);
      this.servicesAvailable = !!(embedRes?.ok && qdrantRes?.ok);
    } catch {
      this.servicesAvailable = false;
    }
    return this.servicesAvailable;
  }

  // ─── Embedding ──────────────────────────────────────────────────

  private async embed(text: string): Promise<number[]> {
    const res = await fetch(`${this.config.embeddingUrl}/embeddings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: [text] }),
    });

    if (!res.ok) {
      throw new Error(`Embedding failed: ${res.status} ${await res.text()}`);
    }

    const data = await res.json();
    return data.embeddings[0];
  }

  // ─── Qdrant Operations ─────────────────────────────────────────

  private async ensureCollection(): Promise<void> {
    const url = `${this.config.qdrantUrl}/collections/${this.COLLECTION_NAME}`;
    const check = await fetch(url);

    if (check.status === 404) {
      this.log("Creating collection:", this.COLLECTION_NAME);
      const res = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          vectors: {
            size: this.config.embeddingDimension,
            distance: "Cosine",
          },
          quantization_config: {
            scalar: {
              type: "int8",
              quantile: 0.99,
              always_ram: false,
            },
          },
        }),
      });
      if (!res.ok) {
        throw new Error(`Failed to create collection: ${await res.text()}`);
      }
    }
  }

  private async upsertPoint(point: QdrantPoint): Promise<void> {
    const url = `${this.config.qdrantUrl}/collections/${this.COLLECTION_NAME}/points`;
    const res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points: [point] }),
    });
    if (!res.ok) {
      throw new Error(`Qdrant upsert failed: ${await res.text()}`);
    }
  }

  private async searchPoints(
    vector: number[],
    limit: number,
    filter?: Record<string, unknown>
  ): Promise<QdrantSearchResult[]> {
    const url = `${this.config.qdrantUrl}/collections/${this.COLLECTION_NAME}/points/search`;
    const body: Record<string, unknown> = {
      vector,
      limit,
      with_payload: true,
      score_threshold: this.config.minRecallScore,
    };
    if (filter) body.filter = filter;

    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(`Qdrant search failed: ${await res.text()}`);
    }

    const data = await res.json();
    return data.result || [];
  }

  private async deletePoint(id: string): Promise<void> {
    const url = `${this.config.qdrantUrl}/collections/${this.COLLECTION_NAME}/points/delete`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points: [id] }),
    });
    if (!res.ok) {
      throw new Error(`Qdrant delete failed: ${await res.text()}`);
    }
  }

  private async scrollPoints(
    limit: number,
    filter?: Record<string, unknown>,
    offset?: string
  ): Promise<{ points: QdrantSearchResult[]; next_page_offset?: string }> {
    const url = `${this.config.qdrantUrl}/collections/${this.COLLECTION_NAME}/points/scroll`;
    const body: Record<string, unknown> = { limit, with_payload: true, with_vector: false };
    if (filter) body.filter = filter;
    if (offset) body.offset = offset;

    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Qdrant scroll failed: ${await res.text()}`);

    const data = await res.json();
    return { points: data.result?.points || [], next_page_offset: data.result?.next_page_offset };
  }

  // ─── Category Detection ─────────────────────────────────────────

  private detectCategory(text: string): string {
    for (const [category, pattern] of Object.entries(CATEGORY_PATTERNS)) {
      if (pattern.test(text)) return category;
    }
    return "other";
  }

  // ─── Tool Handlers ──────────────────────────────────────────────

  async memoryStore(
    text: string,
    category?: string,
    importance?: number
  ): Promise<string> {
    if (containsCredential(text)) {
      this.log("Blocked: text contains credentials");
      return "Memory not stored: contains sensitive credentials.";
    }

    await this.ensureCollection();
    const vector = await this.embed(text);

    const id = uuidv4();
    const resolvedCategory = category || this.detectCategory(text);
    const now = new Date().toISOString();

    await this.upsertPoint({
      id,
      vector,
      payload: {
        text,
        category: resolvedCategory,
        importance: importance ?? 0.5,
        timestamp: now,
        tags: [],
      },
    });

    this.log("Stored memory:", id, resolvedCategory);
    return `Memory stored [${resolvedCategory}]: ${text.substring(0, 80)}...`;
  }

  async memorySearch(
    query: string,
    limit?: number,
    category?: string,
    minImportance?: number
  ): Promise<Memory[]> {
    await this.ensureCollection();
    const vector = await this.embed(query);

    let filter: Record<string, unknown> | undefined;
    const mustClauses: Record<string, unknown>[] = [];

    if (category) {
      mustClauses.push({ key: "category", match: { value: category } });
    }
    if (minImportance !== undefined) {
      mustClauses.push({ key: "importance", range: { gte: minImportance } });
    }
    if (mustClauses.length > 0) {
      filter = { must: mustClauses };
    }

    const results = await this.searchPoints(
      vector,
      limit || this.config.maxRecallResults,
      filter
    );

    return results.map((r) => ({
      id: r.id,
      text: r.payload.text as string,
      category: r.payload.category as string,
      importance: r.payload.importance as number,
      timestamp: r.payload.timestamp as string,
      tags: (r.payload.tags as string[]) || [],
      score: r.score,
    }));
  }

  async memoryList(limit?: number, category?: string): Promise<Memory[]> {
    await this.ensureCollection();

    let filter: Record<string, unknown> | undefined;
    if (category) {
      filter = { must: [{ key: "category", match: { value: category } }] };
    }

    const result = await this.scrollPoints(limit || 10, filter);

    return result.points.map((p) => ({
      id: p.id,
      text: p.payload.text as string,
      category: p.payload.category as string,
      importance: p.payload.importance as number,
      timestamp: p.payload.timestamp as string,
      tags: (p.payload.tags as string[]) || [],
    }));
  }

  async memoryForget(memoryId?: string, query?: string): Promise<string> {
    if (memoryId) {
      await this.deletePoint(memoryId);
      return `Memory ${memoryId} deleted.`;
    }
    if (query) {
      const results = await this.memorySearch(query, 1);
      if (results.length === 0) return "No matching memory found.";
      await this.deletePoint(results[0].id);
      return `Deleted: "${results[0].text.substring(0, 60)}..."`;
    }
    return "Provide either memoryId or query to forget.";
  }

  // ─── Auto-Capture: Multi-stage fact extraction ────────────────────

  private extractFacts(userMessage: string, agentResponse: string): string[] {
    const facts: string[] = [];

    // Stage 1: Split into sentences
    const combined = `${userMessage}\n${agentResponse}`;
    const sentences = combined
      .split(/[.!?\n]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 20);

    for (const sentence of sentences) {
      // Stage 2: Noise filter
      if (isNoise(sentence)) continue;
      if (sentence.endsWith("?")) continue;

      // Stage 3: Credential filter
      if (containsCredential(sentence)) continue;

      // Stage 4: Signal detection — only keep sentences with category patterns
      for (const pattern of Object.values(CATEGORY_PATTERNS)) {
        if (pattern.test(sentence)) {
          // Stage 5: Scrub any partial credentials that slipped through
          facts.push(scrubCredentials(sentence));
          break;
        }
      }
    }

    return facts.slice(0, 3);
  }

  // ─── OpenClaw Lifecycle Hooks ───────────────────────────────────

  async beforeAgentStart(context: {
    userMessage: string;
    prependContext?: string;
  }): Promise<{ prependContext?: string }> {
    if (!this.config.autoRecall) return {};

    const msg = context.userMessage?.trim();
    if (!msg || SKIP_PATTERNS.test(msg)) return {};

    // Graceful degradation: check services before searching
    if (!this.servicesAvailable) {
      const available = await this.checkServices();
      if (!available) {
        this.log("Services unavailable — skipping auto-recall");
        return {};
      }
    }

    try {
      const memories = await this.memorySearch(msg, this.config.maxRecallResults);

      if (memories.length === 0) return {};

      // Dynamic threshold: drop memories scoring < 50% of the top result
      const topScore = memories[0].score || 1;
      const threshold = topScore * 0.5;
      const relevant = memories.filter((m) => (m.score || 0) >= threshold);

      // Structured context injection with relevance scores
      const memoryBlock = relevant
        .map((m) => {
          const pct = Math.round((m.score || 0) * 100);
          return `  - [${m.category}] (${pct}% relevant) ${m.text}`;
        })
        .join("\n");

      const injection = `
<engram_context>
Relevant memories from past sessions (use silently to inform responses — only reference when directly related to the user's message):

${memoryBlock}
</engram_context>
`;

      this.log(`Auto-recalled ${relevant.length} memories (${memories.length - relevant.length} below threshold)`);

      return { prependContext: injection };
    } catch (e) {
      this.log("Auto-recall failed:", e);
      this.servicesAvailable = false;
      return {};
    }
  }

  async afterAgentResponse(context: {
    userMessage: string;
    agentResponse: string;
  }): Promise<void> {
    this.messageCount++;

    if (this.config.autoCapture) {
      const msg = context.userMessage?.trim();
      if (msg && !SKIP_PATTERNS.test(msg)) {
        // Graceful degradation
        if (!this.servicesAvailable) {
          const available = await this.checkServices();
          if (!available) return;
        }

        try {
          const facts = this.extractFacts(context.userMessage, context.agentResponse);
          for (const fact of facts) {
            await this.memoryStore(fact);
          }
          if (facts.length > 0) {
            this.log(`Auto-captured ${facts.length} facts`);
          }
        } catch (e) {
          this.log("Auto-capture failed:", e);
          this.servicesAvailable = false;
        }
      }
    }
  }
}

// ─── Plugin Registration (OpenClaw entry point) ───────────────────

let plugin: EngramMemoryPlugin;

export function register(config: Partial<PluginConfig> = {}) {
  plugin = new EngramMemoryPlugin(config);

  // Check services on startup
  plugin["checkServices"]().then((ok) => {
    if (ok) {
      console.log("[engram-memory] Community Edition ready — collection: agent-memory");
    } else {
      console.log("[engram-memory] Services not reachable. Run: bash scripts/setup.sh");
      console.log("[engram-memory] Tools will retry when services become available.");
    }
  });

  return {
    hooks: {
      before_agent_start: async (ctx: { userMessage: string; prependContext?: string }) => {
        return plugin.beforeAgentStart(ctx);
      },
      after_agent_response: async (ctx: { userMessage: string; agentResponse: string }) => {
        return plugin.afterAgentResponse(ctx);
      },
    },

    tools: {
      memory_store: async (params: { text: string; category?: string; importance?: number }) => {
        return plugin.memoryStore(params.text, params.category, params.importance);
      },
      memory_search: async (params: { query: string; limit?: number; category?: string; minImportance?: number }) => {
        const results = await plugin.memorySearch(params.query, params.limit, params.category, params.minImportance);
        if (results.length === 0) return "No memories found.";
        return results
          .map((m, i) => `${i + 1}. [${m.category}] (${Math.round((m.score || 0) * 100)}% match) ${m.text}`)
          .join("\n");
      },
      memory_list: async (params: { limit?: number; category?: string }) => {
        const results = await plugin.memoryList(params.limit, params.category);
        if (results.length === 0) return "No memories stored yet.";
        return results
          .map((m, i) => `${i + 1}. [${m.category}] ${m.text} (${m.timestamp})`)
          .join("\n");
      },
      memory_forget: async (params: { memoryId?: string; query?: string }) => {
        return plugin.memoryForget(params.memoryId, params.query);
      },
      memory_profile: async (params: { action?: string; key?: string; value?: string; scope?: string }) => {
        if (params.action === "view" || !params.action) {
          const profiles = await plugin.memorySearch("user profile preferences", 20);
          const profileMemories = profiles.filter((m) => m.category === "preference" || m.tags?.includes("profile"));
          if (profileMemories.length === 0) return "No profile data stored yet.";
          return profileMemories.map((m) => `- ${m.text}`).join("\n");
        }
        if (params.action === "add" && params.key && params.value) {
          const text = `[profile:${params.scope || "static"}] ${params.key}: ${params.value}`;
          return plugin.memoryStore(text, "preference", 0.9);
        }
        if (params.action === "remove" && params.key) {
          return plugin.memoryForget(undefined, `profile ${params.key}`);
        }
        return "Usage: memory_profile(action='view|add|remove', key, value, scope)";
      },
    },
  };
}

export default { register };
