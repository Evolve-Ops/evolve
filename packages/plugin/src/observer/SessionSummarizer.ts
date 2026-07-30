/**
 * SessionSummarizer
 *
 * Generates a structured session-level summary when a session ends.
 * Written to the same annotation JSONL as turn annotations, as a
 * "session_summary" record.
 *
 * Design goals:
 *   - Capture outcome, complexity, capabilities invoked, promises made
 *   - Detect efficiency problems (high turn count for low complexity)
 *   - Use keyword heuristics for speed/cost; LLM for outcome extraction only
 *   - Keep cost minimal: one optional tier3 call per session, only last 2 turns
 *
 * Schema (type: "session_summary"):
 * {
 *   schema_version: 2,
 *   type: "session_summary",
 *   session_id: string,
 *   ts: string,
 *   bot_id: string,
 *   turn_count: number,
 *   session_class: "productive" | "maintenance" | "ambiguous",
 *   tier_confidence: number,
 *   outcome: string,              — what was accomplished (LLM-extracted or inferred)
 *   complexity: "low"|"medium"|"high",
 *   applications_invoked: string[],
 *   promises_made: string[],      — things the bot committed to do
 *   correction_count: number,
 *   efficiency_flag: boolean,     — true if turns >> expected for complexity
 *   total_input_tokens: number,
 *   total_output_tokens: number,
 * }
 */

import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";

export interface TurnRecord {
  userMessage: string;
  assistantMessage: string;
  session_class: string;   // "productive" | "maintenance" | "ambiguous"
  class_confidence: number;
  correction_detected: boolean;
  input_tokens: number;
  output_tokens: number;
  // Optional fields populated by task extractor
  role?: string;
  content?: string;
  turnId?: string;
}

// Application tag patterns — keyword → application name.
// These are GENERIC defaults. Deployment-specific patterns
// (project names, team members, domain vocabulary) should be
// added via network.json applicationPatterns array:
// [{"keywords": ["acme", "project x"], "tag": "acme-project"}]
const APPLICATION_PATTERNS: Array<{ keywords: string[]; tag: string }> = [
  {
    keywords: ["protein", "fiber", "calories", "nutrition", "diet", "food log", "supplement"],
    tag: "health-nutrition",
  },
  {
    keywords: ["workout", "fitness", "exercise", "recovery", "sleep score", "steps"],
    tag: "health-fitness",
  },
  {
    keywords: ["calendar", "event", "appointment", "schedule", "meeting"],
    tag: "calendar",
  },
  {
    keywords: ["email", "inbox", "reply to", "message from", "vendor email"],
    tag: "email",
  },
  {
    keywords: ["travel", "itinerary", "flight", "hotel", "trip", "booking"],
    tag: "travel",
  },
  {
    keywords: ["home", "repair", "appliance", "contractor", "plumber", "electrician"],
    tag: "home-management",
  },
  {
    keywords: ["task", "project", "deadline", "milestone", "deliverable", "status update"],
    tag: "task-management",
  },
  {
    keywords: ["evolve", "openclaw config", "gateway", "plugin", "bot network"],
    tag: "evolve-system",
  },
  {
    keywords: ["novel", "chapter", "story", "creative writing", "draft"],
    tag: "creative-writing",
  },
  {
    keywords: ["slack", "team message", "weekly report", "channel update"],
    tag: "slack-comms",
  },
  {
    keywords: ["document", "report", "write up", "summary", "docx"],
    tag: "document-generation",
  },
];

// Phrases that signal the bot made a future commitment
const PROMISE_PATTERNS: string[] = [
  "i'll remember",
  "i'll write",
  "i'll note",
  "i'll track",
  "i'll check",
  "i'll follow up",
  "i'll monitor",
  "i'll keep an eye",
  "i'll let you know",
  "i'll update",
  "i'll add",
  "i'll save",
  "i'll log",
];

function detectApplications(
  turns: TurnRecord[],
  extraPatterns?: Array<{ keywords: string[]; tag: string }>
): string[] {
  const combined = turns
    .map((t) => t.userMessage + " " + t.assistantMessage)
    .join(" ")
    .toLowerCase();

  const allPatterns = [...APPLICATION_PATTERNS, ...(extraPatterns ?? [])];
  const found = new Set<string>();
  for (const { keywords, tag } of allPatterns) {
    if (keywords.some((kw) => combined.includes(kw))) {
      found.add(tag);
    }
  }
  return [...found];
}

function detectPromises(turns: TurnRecord[]): string[] {
  const promises: string[] = [];
  for (const turn of turns) {
    const asst = turn.assistantMessage.toLowerCase();
    for (const pattern of PROMISE_PATTERNS) {
      if (asst.includes(pattern)) {
        // Extract the sentence containing the promise (heuristic)
        const sentences = turn.assistantMessage.split(/[.!?]/);
        const relevant = sentences.find((s) =>
          s.toLowerCase().includes(pattern)
        );
        if (relevant) {
          promises.push(relevant.trim().slice(0, 120));
        }
        break; // one promise per turn
      }
    }
  }
  return promises;
}

function dominantClass(turns: TurnRecord[]): { tier: string; confidence: number } {
  const counts: Record<string, number> = { productive: 0, maintenance: 0, ambiguous: 0 };
  let totalConfidence = 0;
  for (const t of turns) {
    counts[t.session_class] = (counts[t.session_class] ?? 0) + 1;
    totalConfidence += t.class_confidence;
  }
  const dominant = Object.entries(counts).sort(([, a], [, b]) => b - a)[0];
  const confidence = turns.length > 0 ? totalConfidence / turns.length : 0.3;
  return { tier: dominant[0] ?? "ambiguous", confidence: Math.round(confidence * 100) / 100 };
}

function classifyComplexity(turnCount: number): "low" | "medium" | "high" {
  if (turnCount <= 2) return "low";
  if (turnCount <= 6) return "medium";
  return "high";
}

function isEfficiencyFlag(turnCount: number, complexity: string): boolean {
  // Flag when turns significantly exceed expected for complexity
  if (complexity === "low" && turnCount > 3) return true;
  if (complexity === "medium" && turnCount > 9) return true;
  if (complexity === "high" && turnCount > 18) return true;
  return false;
}

// Simple outcome extraction from the last assistant message
// Used as fallback when LLM extraction is skipped
function inferOutcome(turns: TurnRecord[]): string {
  if (turns.length === 0) return "No turns recorded";
  const last = turns[turns.length - 1].assistantMessage;
  // Grab first 120 chars of last assistant message as outcome hint
  return last.slice(0, 120).replace(/\n/g, " ").trim() + (last.length > 120 ? "…" : "");
}

// Build a concise prompt for LLM outcome extraction
// Uses only the last 2 turns to minimize token cost
function buildOutcomePrompt(turns: TurnRecord[]): string {
  const last2 = turns.slice(-2);
  const ctx = last2
    .map((t) => `User: ${t.userMessage.slice(0, 300)}\nAssistant: ${t.assistantMessage.slice(0, 300)}`)
    .join("\n---\n");

  return `In one sentence (max 120 chars), describe what was accomplished in this AI assistant exchange:

${ctx}

Reply with only the one-sentence outcome, no preamble.`;
}

export class SessionSummarizer {
  private config: EvolveConfig;
  private logger: PluginLogger;
  private api: any;

  constructor(config: EvolveConfig, logger: PluginLogger, api: any) {
    this.config = config;
    this.logger = logger;
    this.api = api;
  }

  async summarize(
    sessionId: string,
    turns: TurnRecord[],
    writeFn: (record: Record<string, unknown>) => void
  ): Promise<void> {
    if (turns.length === 0) return;

    const { tier, confidence } = dominantClass(turns);
    const complexity = classifyComplexity(turns.length);
    const applications = detectApplications(turns, this.config.applicationPatterns);
    const promises = detectPromises(turns);
    const correctionCount = turns.filter((t) => t.correction_detected).length;
    const efficiencyFlag = isEfficiencyFlag(turns.length, complexity);
    const totalInput = turns.reduce((s, t) => s + t.input_tokens, 0);
    const totalOutput = turns.reduce((s, t) => s + t.output_tokens, 0);

    // LLM outcome extraction — optional, falls back to keyword inference.
    //
    // Gate on `summarizerMinTurns` (default 2): sub-minimal sessions (1 turn,
    // often "are you alive?" or a single keyword trigger) almost never justify
    // the tier3 call. The inferOutcome fallback is strictly cheaper and good
    // enough — the last assistant message IS the outcome for a 1-turn session.
    // This is the #1 waste source Budget Hawk v2 is designed to eliminate.
    let outcome = inferOutcome(turns);
    const minTurns = this.config.summarizerMinTurns ?? 2;
    if (this.config.enableLLMSummarization !== false && turns.length >= minTurns) {
      try {
        outcome = await this.extractOutcomeLLM(turns);
      } catch (err) {
        this.logger.warn(`Evolve: LLM outcome extraction failed, using fallback: ${err}`);
      }
    } else if (this.config.enableLLMSummarization !== false && turns.length < minTurns) {
      this.logger.debug(
        `Evolve: session ${sessionId.slice(0, 8)} below summarizerMinTurns (${turns.length} < ${minTurns}), skipping LLM outcome extraction`
      );
    }

    const summary: Record<string, unknown> = {
      schema_version: 2,
      type: "session_summary",
      session_id: sessionId,
      ts: new Date().toISOString(),
      bot_id: this.config.botId,
      turn_count: turns.length,
      // session_class is the canonical field name read by measure.py.
      // tier is kept for backward compatibility with older readers.
      session_class: tier,
      tier,
      tier_confidence: confidence,
      // True when the session resolved in a single turn — the user got what
      // they needed without any follow-up.  Used by measure.py for the
      // first_response_resolutions metric.
      first_response_resolution: turns.length === 1,
      outcome,
      complexity,
      applications_invoked: applications,
      promises_made: promises,
      correction_count: correctionCount,
      efficiency_flag: efficiencyFlag,
      total_input_tokens: totalInput,
      total_output_tokens: totalOutput,
    };

    writeFn(summary);
    this.logger.info(
      `Evolve: session ${sessionId.slice(0, 8)} summarized — ${tier}, ${complexity}, ${turns.length} turns, outcome: ${outcome.slice(0, 60)}`
    );
  }

  private async extractOutcomeLLM(turns: TurnRecord[]): Promise<string> {
    const prompt = buildOutcomePrompt(turns);

    const result = await this.api.runtime.subagent.run({
      idempotencyKey: `evolve:session-summary:${Date.now()}`,
      message: prompt,
      model: this.config.classifierModel, // tier3 — cheap
      maxTurns: 1,
    });

    const response = await this.api.runtime.subagent.waitForRun({
      runId: result.runId,
      timeoutMs: 15000,
    });

    const text = (response?.lastMessage ?? "").trim();
    return text.slice(0, 150) || inferOutcome(turns);
  }
}
